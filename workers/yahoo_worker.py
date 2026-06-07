from __future__ import annotations
"""Yahoo/Google 広告収集ワーカー"""
import logging
import queue
import random
import time

from playwright.sync_api import sync_playwright

from scrapers.google_scraper import scrape_google, warmup
from scrapers.yahoo_scraper import scrape_yahoo
from storage.database import get_connection
from utils.browser import create_browser_context, new_stealth_page
from utils.keywords import (
    archive_stale_keywords,
    auto_refill_if_low,
    get_next_keyword_with_area,
    update_keyword_area_searched,
)
from self_repair.diagnostics import get_diagnostics

from state import (
    config, shutdown_event, pause_event, lp_queue,
    _ctx_dead, BASE_DIR, beat,
)


def _get_boost_patterns() -> list[str]:
    """config.json の priority_keywords をブーストパターンとして返す"""
    return config.get('priority_keywords', [])


def _enqueue_lp(url: str, source: str, keyword: str, meta_company: str | None = None,
                area_name: str | None = None, serp_phone: str | None = None):
    try:
        lp_queue.put(
            {'lp_url': url, 'source': source, 'keyword': keyword,
             'meta_company': meta_company, 'area_name': area_name,
             'serp_phone': serp_phone},
            timeout=10
        )
    except queue.Full:
        logging.debug(f'lp_queueが満杯。スキップ: {url}')


def _interruptible_sleep(seconds: float, beat_name: str | None = None):
    end = time.time() + seconds
    last_beat = time.time()
    while time.time() < end and not shutdown_event.is_set():
        time.sleep(min(1.0, end - time.time()))
        if beat_name and time.time() - last_beat >= 30:
            beat(beat_name)
            last_beat = time.time()


def _run_yahoo_worker(name: str, profile_dir):
    """Yahoo/Google 広告収集ワーカー実装（yahoo / yahoo2 で共用）"""
    tag = f'[{name.upper()}]'
    logging.info(f'{tag} 起動')
    conn = get_connection()

    while not shutdown_event.is_set():
        try:
            with sync_playwright() as playwright:
                ctx = create_browser_context(playwright, profile_dir=profile_dir)
                try:
                    page = new_stealth_page(ctx)
                except Exception as e:
                    logging.error(f'{tag} stealth page作成失敗: {e}', exc_info=True)
                    return

                try:
                    warmup(page)
                except Exception:
                    pass

                recycle_every = config.get('browser_recycle_keywords', 80)
                keyword_count = 0
                beat(name)
                while not shutdown_event.is_set() and keyword_count < recycle_every:
                    if pause_event.is_set():
                        beat(name)
                        time.sleep(5)
                        continue

                    cooling_hours = config.get('timing', {}).get('keyword_cooling_hours', 72)
                    min_delay = config.get('timing', {}).get('min_delay_seconds', 30)
                    max_delay = config.get('timing', {}).get('max_delay_seconds', 180)
                    boost = _get_boost_patterns()
                    areas = config.get('areas', [])

                    try:
                        beat(name)
                        archive_stale_keywords(conn, config.get('filters', {}).get('keyword_archive_days', 14))
                        auto_refill_if_low(conn, 'google_yahoo', cooling_hours=cooling_hours, threshold=30, batch_size=60)
                        processed = False

                        if config.get('sources', {}).get('google', True) or \
                           config.get('sources', {}).get('yahoo', True):
                            kw_info = get_next_keyword_with_area(
                                conn, 'google_yahoo', cooling_hours, areas, boost_patterns=boost
                            )
                            if kw_info:
                                keyword = kw_info['keyword']
                                area = kw_info.get('area')

                                # 他スレッド（yahoo2等）との (keyword, area) 重複選択を防ぐため、
                                # スクレイプ開始前に冷却ログを仮予約する（楽観的ロック）。
                                # 処理完了後の update_keyword_area_searched は冪等なので問題なし。
                                try:
                                    update_keyword_area_searched(conn, keyword, area['name'] if area else None)
                                except Exception:
                                    pass

                                if area:
                                    logging.info(f'{tag} キーワード: "{keyword}" / エリア: {area["name"]}')
                                else:
                                    logging.info(f'{tag} キーワード: "{keyword}"')
                                diag = get_diagnostics()

                                google_hit = False
                                if config.get('sources', {}).get('google', True):
                                    beat(name)
                                    try:
                                        google_items = scrape_google(page, keyword, area=area)
                                        if google_items is not None:
                                            diag.record_scrape('Google', len(google_items))
                                            _aname = area['name'] if area else None
                                            for item in google_items:
                                                _enqueue_lp(
                                                    item['url'], 'Google', keyword,
                                                    area_name=_aname,
                                                    serp_phone=item.get('serp_phone'),
                                                )
                                            google_hit = len(google_items) > 0
                                    except Exception as e:
                                        logging.error(f'{tag} Google エラー: {e}')
                                        diag.record_scrape('Google', 0)
                                    beat(name)
                                    _interruptible_sleep(random.uniform(
                                        config.get('google_min_delay', 180),
                                        config.get('google_max_delay', 600),
                                    ), name)
                                    beat(name)

                                # google_first は廃止: SerpAPI(Google)と Yahoo は常に両方実行
                                # 以前は google_hit で yahoo_skip=True にしていたが
                                # これが Yahoo 0件の根本原因だったため無効化 (穴6修正)
                                yahoo_skip = False

                                yahoo_playwright_enabled = config.get('sources', {}).get('yahoo_playwright', True)
                                if config.get('sources', {}).get('yahoo', True) and not yahoo_skip and yahoo_playwright_enabled:
                                    try:
                                        urls = scrape_yahoo(page, keyword, area=area, heartbeat=lambda: beat(name))
                                        diag.record_scrape('Yahoo', len(urls))
                                        _aname = area['name'] if area else None
                                        for url in urls:
                                            _enqueue_lp(url, 'Yahoo', keyword, area_name=_aname)
                                    except Exception as e:
                                        err_lower = str(e).lower()
                                        if any(k in err_lower for k in ('target closed', 'context or browser has been closed',
                                                                         'connection reset', 'browser closed', 'crash')):
                                            logging.warning(f'{tag} ページクラッシュ検知 → ページ再作成: {e}')
                                            try:
                                                page.close()
                                            except Exception:
                                                pass
                                            try:
                                                page = new_stealth_page(ctx)
                                                logging.info(f'{tag} ページ再作成完了')
                                            except Exception as e2:
                                                logging.error(
                                                    f'{tag} ページ再作成失敗（コンテキストも死亡）: {e2}。'
                                                    f'スレッドを終了してWatchdogに再起動を委譲します'
                                                )
                                                _ctx_dead[name].set()
                                                return
                                        else:
                                            logging.error(f'{tag} エラー: {e}')
                                        diag.record_scrape('Yahoo', 0)
                                    beat(name)

                                update_keyword_area_searched(conn, keyword, area['name'] if area else None)
                                processed = True
                                keyword_count += 1

                                delay = random.uniform(min_delay, max_delay)
                                logging.info(f'{tag} 次まで {delay:.0f}秒待機')
                                _interruptible_sleep(delay, name)

                        if not processed:
                            logging.info(f'{tag} 全キーワード×エリアが冷却中。60秒待機')
                            _interruptible_sleep(60, name)

                    except Exception as e:
                        logging.error(f'{tag} 予期しないエラー: {e}')
                        _interruptible_sleep(30, name)

                try:
                    ctx.close()
                except Exception:
                    pass

            if not shutdown_event.is_set():
                logging.info(f'{tag} ブラウザリサイクル ({keyword_count}件処理済み) → 再起動します')

        except Exception as e:
            logging.error(f'{tag} 致命的エラーで終了: {e}', exc_info=True)
            if not shutdown_event.is_set():
                time.sleep(10)

    logging.info(f'{tag} 終了')


def yahoo_worker():
    """Yahoo/Google 広告収集スレッド（ブラウザ1号）"""
    _run_yahoo_worker('yahoo', BASE_DIR / 'browser_profile_yahoo')


def yahoo2_worker():
    """Yahoo/Google 広告収集スレッド（ブラウザ2号・並列）"""
    _run_yahoo_worker('yahoo2', BASE_DIR / 'browser_profile_yahoo2')
