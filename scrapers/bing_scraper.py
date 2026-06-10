from __future__ import annotations
from typing import Optional
"""Bing 広告（リスティング）収集スクレイパー（Playwright・無料）

Bing は Google と違い bot 検知が緩く、広告（スポンサー結果）も HTML に直接出るため
Playwright 直接スクレイピングが安定して回せる。広告リンクは www.bing.com/aclick?...&u=...
のトラッキングURL経由で、u パラメータ（'a1'+base64 もしくは直URL）から遷移先LPを抽出する。
"""
import logging
import random
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from scrapers.serp_api_scraper import _extract_lp_from_bing_aclick
from utils.geo_utils import set_area_geolocation

BASE_DIR = Path(__file__).parent.parent
SNAPSHOT_DIR = BASE_DIR / 'logs' / 'snapshots'

# Bing 広告コンテナ/リンクのセレクタ（優先順）
# Bing の広告は ol#b_results 内の li.b_ad（上部）/ .b_adBottom（下部）に入る。
# 見出しリンクの href がそのまま /aclick トラッキングURL。
AD_LINK_SELECTORS = [
    'li.b_ad a[href*="/aclick"]',
    '.b_adTop a[href*="/aclick"]',
    '.b_adBottom a[href*="/aclick"]',
    'aside a[href*="/aclick"]',          # サイドバー広告
    'a[href*="/aclick"]',                # ページ全体から /aclick（最後の砦）
    'li.b_ad h2 a[href^="http"]',
    'li.b_ad .b_adurl a[href^="http"]',
]


def _save_snapshot(page: Page, keyword: str, reason: str = '') -> None:
    """診断用HTMLスナップショットを保存する（直近5件のみ保持）"""
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        safe_kw = ''.join(c if c.isalnum() or c in '-_' else '_' for c in keyword)[:30]
        snap_path = SNAPSHOT_DIR / f'bing_{safe_kw}_{ts}.html'
        html = page.content()
        if len(html) > 50000:
            html = html[:50000] + '\n<!-- truncated -->'
        snap_path.write_text(html, encoding='utf-8', errors='replace')
        snaps = sorted(SNAPSHOT_DIR.glob('bing_*.html'), key=lambda p: p.stat().st_mtime)
        for old in snaps[:-5]:
            old.unlink(missing_ok=True)
        logging.debug(f'[Bing] スナップショット保存: {snap_path.name} ({reason})')
    except Exception as e:
        logging.debug(f'[Bing] スナップショット保存失敗: {e}')


def scrape_bing(page: Page, keyword: str, area: Optional[dict] = None, heartbeat=None) -> list[str]:
    """Bing 広告LPを収集する。

    area: config.json の areas 要素（指定時はクエリ末尾にエリア名を付与）
    heartbeat: 呼び出し元スレッドのハートビートコールバック（長時間操作の前後で呼ぶ）
    Returns:
        list[str]: 収集したLP URLリスト（空リスト=広告なし）
    """
    def _hb():
        if heartbeat:
            heartbeat()

    urls: list[str] = []

    if area:
        try:
            set_area_geolocation(page.context, area)
        except Exception:
            pass

    query = keyword
    if area and area.get('name'):
        query = f'{keyword} {area["name"]}'
    search_url = f'https://www.bing.com/search?q={quote(query)}&cc=jp&setlang=ja&mkt=ja-JP'

    for _attempt in range(2):
        try:
            page.goto(search_url, timeout=35000, wait_until='domcontentloaded')
            _hb()
            try:
                page.wait_for_load_state('networkidle', timeout=12000)
            except PlaywrightTimeoutError:
                pass
            _hb()
            break
        except PlaywrightTimeoutError:
            if _attempt == 0:
                logging.warning(f'Bing検索タイムアウト "{keyword}" → リトライ')
                time.sleep(3)
                continue
            logging.warning(f'Bing検索タイムアウト "{keyword}" (2回失敗)')
            _hb()
            return []
        except Exception as e:
            _hb()
            err_lower = str(e).lower()
            if any(k in err_lower for k in ('target closed', 'context or browser has been closed',
                                            'connection reset', 'browser closed', 'crash')):
                logging.warning(f'Bing検索失敗(ページクラッシュ) "{keyword}": {e}')
                raise  # 上位（worker）でページ再生成させる
            logging.warning(f'Bing検索失敗 "{keyword}": {e}')
            return []

    # CAPTCHA / ブロック検出（Bingはまれ）
    if 'captcha' in page.url.lower() or '/challenge' in page.url.lower():
        logging.warning(f'Bing CAPTCHA/ブロックを検出。"{keyword}" をスキップ')
        _save_snapshot(page, keyword, 'captcha')
        return []

    # 広告は遅延描画されることがあるため、広告コンテナの出現を最大6秒待つ
    try:
        page.wait_for_selector('li.b_ad, .b_ad, .sb_add, .b_adLastChild', timeout=6000)
    except PlaywrightTimeoutError:
        pass  # 広告なしも正常。後続のJS抽出で最終判定する
    time.sleep(random.uniform(0.8, 1.8))
    _hb()

    # 広告コンテナ内の全アンカーをJSで収集する。
    # Bingの広告リンクは (a) /aclick・/aclk トラッキングURL、(b) 広告主への直URL、
    # の両形態がありレイアウトで変わるため、両方を拾って後段で振り分ける。
    seen: set[str] = set()
    try:
        ad_hrefs = page.evaluate("""() => {
            const out = [];
            const push = (h) => { if (h && h.startsWith('http') && !out.includes(h)) out.push(h); };
            // 広告コンテナ（クラス名に b_ad / sb_add を含む要素）配下のリンク
            const containers = document.querySelectorAll(
                'li.b_ad, .b_adTop, .b_adBottom, .b_adLastChild, .sb_add, [class*="b_ad"], aside[aria-label]'
            );
            for (const c of containers) {
                for (const a of c.querySelectorAll('a[href]')) push(a.href || '');
            }
            // ページ全体の広告リダイレクトリンクも拾う（コンテナ外の広告対策）。
            // Bingの実際の広告リダイレクトは /ck/a?...&u=a1<base64>&ntb=1 形式。
            // 旧来の /aclick・/aclk も後方互換で残す。
            for (const a of document.querySelectorAll('a[href*="/ck/a"], a[href*="/aclick"], a[href*="/aclk"]')) {
                push(a.href || '');
            }
            return out.slice(0, 40);
        }""") or []
    except Exception as e:
        logging.debug(f'Bing リンク収集エラー: {e}')
        ad_hrefs = []

    for href in ad_hrefs:
        if '/ck/a' in href or '/aclick' in href or '/aclk' in href:
            lp = _extract_lp_from_bing_aclick(href)
            if lp and lp not in seen:
                seen.add(lp)
                urls.append(lp)
        elif href.startswith('http') and 'bing.com' not in href and 'microsoft.com' not in href \
                and 'msn.com' not in href and 'go.microsoft' not in href:
            # 広告主への直リンク（コンテナ内アンカー）。検索結果の通常リンクも混じり得るが
            # 広告コンテナ配下のみ収集しているため広告とみなす。
            if href not in seen:
                seen.add(href)
                urls.append(href)

    if not urls:
        # 0件時はライブDOM構造を診断ログに残す（ヘッドレス実DOMでセレクタを調整するため）
        try:
            diag = page.evaluate("""() => {
                const adC = document.querySelectorAll('li.b_ad, .sb_add, .b_adLastChild').length;
                const sample = Array.from(
                    document.querySelectorAll('li.b_ad a[href], .sb_add a[href], [class*="b_ad"] a[href]')
                ).map(a => a.href).filter(h => h && h.startsWith('http')).slice(0, 5);
                const adClasses = [...new Set(
                    Array.from(document.querySelectorAll('[class*="b_ad"]')).map(e => String(e.className))
                )].slice(0, 8);
                return JSON.stringify({adC, sample, adClasses});
            }""")
            logging.info(f'[Bing] 0件診断: {diag} / "{keyword}"')
        except Exception:
            pass
        _save_snapshot(page, keyword, 'zero_ads')
        logging.info(f'Bing広告 0件: "{keyword}" — スナップショット保存済み')
        return []

    logging.info(f'Bing広告 {len(urls)}件発見: "{keyword}"')
    return urls
