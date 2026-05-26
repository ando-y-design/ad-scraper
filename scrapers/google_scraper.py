import logging
import random
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from scrapers.serp_api_scraper import scrape_google_via_api, is_serp_all_failed
from utils.geo_utils import set_area_geolocation

BASE_DIR = Path(__file__).parent.parent
SNAPSHOT_DIR = BASE_DIR / 'logs' / 'snapshots'
_CAPTCHA_STATE_FILE = BASE_DIR / 'logs' / '.google_captcha_state'

# 優先順: より特定的なセレクタを先に試す
AD_LINK_SELECTORS = [
    '#tads a[href*="/aclk"]',           # 上部広告セクション（最確実）
    '#bottomads a[href*="/aclk"]',       # 下部広告セクション
    'a[href*="/aclk"]',                  # ページ全体から /aclk リンク
    '[data-text-ad] a[href^="http"]',    # テキスト広告コンテナ内リンク
    'div[data-text-ad] a',               # テキスト広告（属性値なし）
    '.uEierd a[href^="http"]',           # 広告カードのリンク
    'div[id^="tads"] a[href^="http"]',   # tads IDプレフィックス
]

WARMUP_SITES = [
    'https://www.nikkei.com',
    'https://www.asahi.com',
    'https://www.yomiuri.co.jp',
    'https://news.yahoo.co.jp',
    'https://www.nhk.or.jp',
]

DECOY_KEYWORDS = [
    'ニュース 今日', '天気予報', '映画 おすすめ', 'レストラン 渋谷',
    'Python プログラミング', '旅行 国内',
]


def warmup(page: Page):
    site = random.choice(WARMUP_SITES)
    try:
        page.goto(site, timeout=10000, wait_until='domcontentloaded')
        time.sleep(random.uniform(2, 4))
        page.mouse.move(random.randint(200, 800), random.randint(200, 600))
    except Exception:
        pass


def _extract_lp_from_aclk(href: str) -> str | None:
    """Google /aclk トラッキングURLからランディングページURLを抽出する"""
    try:
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        # パラメータ名の優先順
        for param in ('adurl', 'q', 'url'):
            val = params.get(param, [None])[0]
            if val and val.startswith('http') and 'google' not in val:
                return unquote(val)
    except Exception:
        pass
    return None


def _save_snapshot(page: Page, keyword: str, reason: str = '') -> None:
    """診断用HTMLスナップショットを保存する（直近5件のみ保持）"""
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        safe_kw = ''.join(c if c.isalnum() or c in '-_' else '_' for c in keyword)[:30]
        snap_path = SNAPSHOT_DIR / f'google_{safe_kw}_{ts}.html'

        html = page.content()
        # 50KB上限で切り詰め（self_repairのプロンプトサイズ管理）
        if len(html) > 50000:
            html = html[:50000] + '\n<!-- truncated -->'
        snap_path.write_text(html, encoding='utf-8', errors='replace')

        # 古いgoogleスナップショットを削除（5件超過分）
        snaps = sorted(SNAPSHOT_DIR.glob('google_*.html'), key=lambda p: p.stat().st_mtime)
        for old in snaps[:-5]:
            old.unlink(missing_ok=True)

        logging.debug(f'[Google] スナップショット保存: {snap_path.name} ({reason})')
    except Exception as e:
        logging.debug(f'[Google] スナップショット保存失敗: {e}')


def _is_captcha_page(page: Page) -> bool:
    """URLとページコンテンツ両方でCAPTCHAページを検出する"""
    if 'sorry.google' in page.url or 'captcha' in page.url.lower():
        return True
    try:
        content = page.content()
        captcha_signals = [
            'id="captcha-form"',
            'g-recaptcha',
            'solveSimpleChallenge',
            'recaptcha/enterprise',
            'id="recaptcha"',
        ]
        if any(signal in content for signal in captcha_signals):
            return True
    except Exception:
        pass
    return False


def _get_captcha_backoff() -> tuple[int, float]:
    """CAPTCHAバックオフ状態をファイルから読み込む: (連続検出回数, 解除時刻)"""
    try:
        if _CAPTCHA_STATE_FILE.exists():
            data = _CAPTCHA_STATE_FILE.read_text().strip().split(',')
            return int(data[0]), float(data[1])
    except Exception:
        pass
    return 0, 0.0


def _set_captcha_backoff(consecutive: int) -> None:
    """CAPTCHAバックオフ状態をファイルに保存する（段階的バックオフ）"""
    # 連続検出回数に応じて待機時間を段階的に延長:
    #   1-4回: 指数バックオフ（30分〜2時間）
    #   5-7回: 6時間（IPが一時ブロックされている可能性）
    #   8回以上: 24時間（持続的ブロック → 長時間休止）
    if consecutive >= 8:
        wait = 86400   # 24h
    elif consecutive >= 5:
        wait = 21600   # 6h
    else:
        wait = min(1800 * (2 ** (consecutive - 1)), 7200)  # 最大2h
    until = time.time() + wait
    try:
        _CAPTCHA_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CAPTCHA_STATE_FILE.write_text(f'{consecutive},{until}')
    except Exception:
        pass
    logging.warning(
        f'[Google] CAPTCHA バックオフ設定: {consecutive}回連続検出、{wait//60}分間スキップ '
        f'(解除: {time.strftime("%H:%M:%S", time.localtime(until))})'
    )


def _reset_captcha_backoff() -> None:
    """広告取得成功時にCAPTCHAバックオフ状態をリセットする"""
    try:
        _CAPTCHA_STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _human_type(page: Page, selector: str, text: str) -> None:
    """人間らしく1文字ずつタイプする（80〜180ms間隔、たまに誤字→バックスペース）"""
    elem = page.query_selector(selector)
    if not elem:
        return
    elem.click()
    time.sleep(random.uniform(0.3, 0.7))

    i = 0
    while i < len(text):
        char = text[i]
        # 3%の確率で誤字を打って即バックスペース（人間らしさ）
        if random.random() < 0.03 and char.isalpha():
            typo = random.choice('abcdefghijklmnopqrstuvwxyz')
            page.keyboard.type(typo)
            time.sleep(random.uniform(0.1, 0.3))
            page.keyboard.press('Backspace')
            time.sleep(random.uniform(0.1, 0.2))
        page.keyboard.type(char)
        # 日本語文字は長め、英数字は短め
        if ord(char) > 0x7F:
            time.sleep(random.uniform(0.12, 0.22))
        else:
            time.sleep(random.uniform(0.06, 0.15))
        i += 1


def _human_scroll(page: Page, times: int | None = None) -> None:
    """結果ページを人間らしくスクロールして「読む」動作を模倣する"""
    n = times or random.randint(2, 5)
    for _ in range(n):
        scroll_px = random.randint(200, 500)
        page.mouse.wheel(0, scroll_px)
        time.sleep(random.uniform(0.4, 1.2))
    # たまに少し上にスクロールして戻る（読み返し）
    if random.random() < 0.3:
        page.mouse.wheel(0, -random.randint(100, 300))
        time.sleep(random.uniform(0.3, 0.8))


def _human_search(page: Page, keyword: str, location_hint: str | None = None) -> bool:
    """
    google.co.jp を開いて検索ボックスにキーワードを人間らしくタイプして検索する。
    Returns: True=成功, False=失敗
    """
    # まずGoogle TOPページへ
    try:
        # すでにgoogle.co.jpにいればそのまま使う
        if 'google.co.jp' not in page.url:
            page.goto('https://www.google.co.jp/', timeout=12000, wait_until='domcontentloaded')
            time.sleep(random.uniform(0.8, 2.0))
    except Exception:
        try:
            page.goto('https://www.google.co.jp/', timeout=12000, wait_until='domcontentloaded')
            time.sleep(random.uniform(0.8, 2.0))
        except Exception as e:
            logging.warning(f'[Google] TOPページ読み込み失敗: {e}')
            return False

    # 検索ボックスのセレクタ（複数試す）
    search_box_selectors = [
        'textarea[name="q"]',
        'input[name="q"]',
        '[aria-label="検索"]',
        '#APjFqb',
    ]

    box = None
    for sel in search_box_selectors:
        try:
            page.wait_for_selector(sel, timeout=5000)
            box = page.query_selector(sel)
            if box:
                break
        except Exception:
            continue

    if not box:
        # 検索ボックスが見つからない → 直接URLで検索（フォールバック）
        logging.debug('[Google] 検索ボックス未検出 → 直接URLでフォールバック')
        q = keyword
        if location_hint:
            q += f' {location_hint}'
        page.goto(
            f'https://www.google.co.jp/search?q={quote(q)}&gl=jp&hl=ja',
            timeout=15000, wait_until='domcontentloaded',
        )
        return True

    # 検索ボックスをクリックして入力
    box.click()
    time.sleep(random.uniform(0.3, 0.8))

    # 既存テキストをクリア（triple_clickはPython APIに存在しないためclick×3）
    box.click(click_count=3)
    page.keyboard.press('Control+a')
    page.keyboard.press('Delete')
    time.sleep(random.uniform(0.1, 0.3))

    # キーワードをタイプ（エリアヒントがあれば末尾に追加）
    full_query = keyword
    if location_hint:
        full_query += f' {location_hint}'
    _human_type(page, 'textarea[name="q"], input[name="q"]', full_query)

    # Enter前の自然な間（考えているふり）
    time.sleep(random.uniform(0.5, 1.5))
    page.keyboard.press('Enter')

    # 検索結果を待つ
    try:
        page.wait_for_load_state('domcontentloaded', timeout=15000)
    except PlaywrightTimeoutError:
        logging.warning(f'[Google] 検索結果ロードタイムアウト: "{keyword}"')
        return False

    # 結果を「読む」ための自然な待機 + スクロール
    time.sleep(random.uniform(1.0, 2.5))
    _human_scroll(page, times=random.randint(1, 3))
    time.sleep(random.uniform(0.5, 1.5))

    return True


def scrape_google(page: Page, keyword: str, area: dict | None = None) -> list[str] | None:
    """
    area: config.json の areas リストの1要素 (name/lat/lng/serp_location)。
          指定するとブラウザGPS座標を変更し、Google検索URLに near= を追加する。
    Returns:
        list[str]: 収集したLP URLリスト（空リストは「広告なし」）
        None: バックオフ中でスキップ（診断記録不要）
    """
    urls = []

    # SERP APIキーが設定済みなら優先（Playwrightによるブロックリスクを回避）
    location = area.get('serp_location') if area else None
    api_result = scrape_google_via_api(keyword, location=location)
    if api_result is not None:  # SerpAPIが成功（0件含む）→ Playwrightをスキップ（CAPTCHA防止）
        return api_result
    # api_result が None（APIキー未設定 or 全キー失敗）の場合のみPlaywrightで試す
    # ※ SerpAPI全失敗時でも人間模倣Playwrightで試みる（CAPTCHAバックオフが守る）

    # SerpAPI未設定時のみPlaywright直接スクレイピングにフォールバック
    # CAPTCHAバックオフ中はスキップ
    consecutive, until = _get_captcha_backoff()
    if time.time() < until:
        remaining = int(until - time.time())
        logging.warning(
            f'[Google] CAPTCHAバックオフ中 (残り{remaining}秒)、APIキーも未設定: "{keyword}"'
        )
        return None

    # ブラウザのGPS座標をエリアに変更
    if area:
        set_area_geolocation(page.context, area)

    # デコイ検索（5回に1回）: 人間らしくタイプして検索
    if random.random() < 0.2:
        try:
            decoy = random.choice(DECOY_KEYWORDS)
            _human_search(page, decoy)
            time.sleep(random.uniform(2, 5))
            _human_scroll(page)
        except Exception:
            pass

    # 本命キーワードを人間らしくタイプして検索
    location_hint = area['name'] if area else None
    try:
        ok = _human_search(page, keyword, location_hint=location_hint)
        if not ok:
            return []
    except PlaywrightTimeoutError:
        logging.warning(f'Google検索タイムアウト "{keyword}"')
        return []
    except Exception as e:
        logging.warning(f'Google検索失敗 "{keyword}": {e}')
        return []

    # 強化版CAPTCHA検出: 既存セレクタ判定 + HTML内容チェック
    # スナップショットで確認されたreCAPTCHA Enterprise形式に対応
    def _is_captcha_enhanced(p) -> bool:
        if _is_captcha_page(p):
            return True
        try:
            content = p.content()
            indicators = [
                'id="captcha-form"',
                'id="recaptcha"',
                'class="g-recaptcha"',
                'recaptcha/enterprise.js',
                'solveSimpleChallenge',
                "getElementById('captcha')",
                'data-sitekey=',
                'recaptcha__ja.js',
            ]
            return any(ind in content for ind in indicators)
        except Exception:
            return False

    if _is_captcha_enhanced(page):
        _save_snapshot(page, keyword, 'captcha')
        consecutive, _ = _get_captcha_backoff()
        _set_captcha_backoff(consecutive + 1)
        logging.warning(
            f'[Google] CAPTCHA検出（reCAPTCHA Enterprise）: "{keyword}" → バックオフ設定 (連続{consecutive + 1}回)'
        )
        # None を返して診断カウンターに記録させない（CAPTCHAは「広告なし」ではなく「一時ブロック」）
        return None

    # CAPTCHA検出なし → バックオフカウンタをリセット
    if consecutive > 0:
        _reset_captcha_backoff()

    time.sleep(random.uniform(1.5, 3))

    # 広告セクションが描画されるまで短時間待機
    try:
        page.wait_for_selector('#tads, [data-text-ad], a[href*="/aclk"]', timeout=5000)
    except PlaywrightTimeoutError:
        pass  # 広告なしも正常。後続で検証

    # 戦略1: セレクタでaclkリンクを直接抽出（クリック不要・高速）
    for selector in AD_LINK_SELECTORS:
        try:
            elements = page.query_selector_all(selector)
            if not elements:
                continue
            for elem in elements[:8]:
                href = elem.get_attribute('href') or ''
                # /aclk を含むリンクから LP URL を抽出
                if '/aclk' in href:
                    lp = _extract_lp_from_aclk(href)
                    if lp:
                        urls.append(lp)
                        logging.debug(f'Google LP(直接抽出): {lp}')
                # http始まりの外部リンクをそのまま採用（[data-text-ad]内のみ）
                elif href.startswith('http') and 'google' not in href and 'data-text-ad' in selector:
                    urls.append(href)
                    logging.debug(f'Google LP(direct): {href}')
            if urls:
                break
        except Exception:
            continue

    # 戦略2: /aclkリンクが取れなかった場合はクリック方式にフォールバック
    if not urls:
        urls = _scrape_google_click(page, keyword)

    if not urls:
        _save_snapshot(page, keyword, 'zero_results')
        logging.info(f'Google広告 0件: "{keyword}" — スナップショット保存済み')
        return []
    _reset_captcha_backoff()
    logging.info(f'Google広告 {len(urls)}件発見: "{keyword}"')

    # SERP広告のコール表示（電話番号表示拡張）から domain→phone マッピングを取得
    # 取得できなかった場合は URL のみのリストとして返す
    serp_phones: dict[str, str] = {}
    try:
        serp_phones = page.evaluate("""() => {
            const res = {};
            for (const el of document.querySelectorAll('[data-text-ad]')) {
                let phone = '';
                const callEl = el.querySelector('[data-tel]') || el.querySelector('[data-dpn]');
                if (callEl) phone = callEl.getAttribute('data-tel') || callEl.getAttribute('data-dpn') || '';
                if (!phone) {
                    const tl = el.querySelector('a[href^="tel:"]');
                    if (tl) phone = tl.href.replace('tel:', '').trim();
                }
                if (!phone) {
                    const m = (el.textContent || '').match(/0[\\d]{1,4}[- ][\\d]{1,4}[- ][\\d]{4}/);
                    if (m) phone = m[0];
                }
                if (!phone) continue;
                let domain = '';
                for (const a of el.querySelectorAll('a[href]')) {
                    try {
                        const u = new URL(a.href);
                        if (!u.hostname.includes('google')) {
                            domain = u.hostname.replace(/^www\\./, '');
                            break;
                        }
                    } catch(e) {}
                }
                if (domain) res[domain] = phone;
            }
            return res;
        }""") or {}
    except Exception:
        pass

    # URL と SERP 電話を対応づけて返す
    from urllib.parse import urlparse as _up
    result = []
    for url in urls:
        try:
            domain = _up(url).netloc.replace('www.', '')
        except Exception:
            domain = ''
        result.append({'url': url, 'serp_phone': serp_phones.get(domain) or None})
    return result


def _scrape_google_click(page: Page, keyword: str) -> list[str]:
    """クリック方式でGoogle広告LPを取得する（フォールバック）"""
    urls = []

    ad_elements = []
    for selector in AD_LINK_SELECTORS:
        try:
            elements = page.query_selector_all(selector)
            if elements:
                ad_elements = elements[:5]
                break
        except Exception:
            continue

    if not ad_elements:
        logging.debug(f'Google広告なし（クリック方式）: "{keyword}"')
        return []

    for i, elem in enumerate(ad_elements):
        try:
            link = elem if elem.get_attribute('href') else elem.query_selector('a')
            if not link:
                continue

            href = link.get_attribute('href') or ''
            # /aclk リンクならクリック前に URL 抽出を試みる
            if '/aclk' in href:
                lp = _extract_lp_from_aclk(href)
                if lp:
                    urls.append(lp)
                    continue

            try:
                with page.expect_popup(timeout=6000) as popup_info:
                    link.click()
                popup = popup_info.value
                popup.wait_for_load_state('domcontentloaded', timeout=10000)
                url = popup.url
                popup.close()
            except PlaywrightTimeoutError:
                url = page.url

            if 'google.com/amp/s/' in url:
                url = url.replace('https://www.google.com/amp/s/', 'https://')

            if url and url.startswith('http') and 'google' not in url:
                urls.append(url)

            time.sleep(random.uniform(1, 3))

            try:
                page.go_back(timeout=5000, wait_until='domcontentloaded')
                time.sleep(random.uniform(0.5, 1.5))
            except Exception:
                fallback_url = f'https://www.google.co.jp/search?q={quote(keyword)}&gl=jp&hl=ja'
                page.goto(fallback_url, timeout=15000, wait_until='domcontentloaded')

        except Exception as e:
            logging.debug(f'Google広告クリックエラー (#{i}): {e}')
            continue

    return urls
