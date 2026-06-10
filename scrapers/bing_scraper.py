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

    time.sleep(random.uniform(1.0, 2.2))
    _hb()

    # 戦略1: セレクタで aclick リンクを直接抽出（クリック不要・高速）
    seen: set[str] = set()
    for selector in AD_LINK_SELECTORS:
        try:
            elements = page.query_selector_all(selector)
            if not elements:
                continue
            for elem in elements[:8]:
                href = elem.get_attribute('href') or ''
                if '/aclick' in href:
                    lp = _extract_lp_from_bing_aclick(href)
                    if lp and lp not in seen:
                        seen.add(lp)
                        urls.append(lp)
                        logging.debug(f'Bing LP(直接抽出): {lp}')
                elif href.startswith('http') and 'bing.com' not in href:
                    if href not in seen:
                        seen.add(href)
                        urls.append(href)
                        logging.debug(f'Bing LP(direct): {href}')
            if urls:
                break
        except Exception:
            continue

    # 戦略2: JS fallback — 全リンクから aclick を拾って u= をデコード
    if not urls:
        try:
            ad_hrefs = page.evaluate("""() => {
                const out = [];
                for (const a of document.querySelectorAll('a[href]')) {
                    const h = a.href || '';
                    if (h.includes('/aclick')) out.push(h);
                }
                return out.slice(0, 12);
            }""") or []
            for href in ad_hrefs:
                lp = _extract_lp_from_bing_aclick(href)
                if lp and lp not in seen:
                    seen.add(lp)
                    urls.append(lp)
            if urls:
                logging.info(f'Bing広告 JS fallback {len(urls)}件発見: "{keyword}"')
        except Exception as e:
            logging.debug(f'Bing JS fallback エラー: {e}')

    if not urls:
        _save_snapshot(page, keyword, 'zero_ads')
        logging.info(f'Bing広告 0件: "{keyword}" — スナップショット保存済み')
        return []

    logging.info(f'Bing広告 {len(urls)}件発見: "{keyword}"')
    return urls
