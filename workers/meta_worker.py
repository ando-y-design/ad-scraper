"""Meta 広告収集ワーカー"""
import logging
import random
import time

from playwright.sync_api import sync_playwright

from scrapers.meta_api_scraper import scrape_meta_via_api
from scrapers.meta_scraper import scrape_meta
from storage.database import get_connection
from utils.browser import create_browser_context, new_stealth_page
from utils.keywords import (
    auto_refill_if_low,
    get_next_keyword,
    update_keyword_searched,
)
from self_repair.diagnostics import get_diagnostics

from state import (
    config, shutdown_event, pause_event,
    _ctx_dead, beat,
)

from workers.yahoo_worker import _enqueue_lp, _interruptible_sleep, _get_boost_patterns


# ─────────────────────────────────────────────
# Meta ブラウザ生死確認 / 再作成ヘルパー
# ─────────────────────────────────────────────
def _check_meta_page_alive(page) -> bool:
    """
    Metaページが生きているか軽量確認。
    scrape_meta() が内部で例外を飲んだ場合でも dead を検出できる。
    """
    if page is None:
        return False
    try:
        _ = page.url  # コンテキストが死んでいると例外になる
        return True
    except Exception:
        return False


def _recreate_meta_page(ctx):
    """
    Metaページを再作成する。
    ページのみ死んでいる場合はページ再作成、コンテキストごと死んでいる場合は None を返す。
    """
    try:
        new_page = new_stealth_page(ctx)
        logging.info('[Meta] ページ再作成完了')
        return new_page
    except Exception as e:
        logging.error(
            f'[Meta] ページ再作成失敗（コンテキストも死亡の可能性）: {e}。'
            f'スレッドを終了してWatchdogに再起動を委譲します'
        )
        return None


# ─────────────────────────────────────────────
# THREAD 1b: Meta 広告収集（独立ブラウザ・並列）
# ─────────────────────────────────────────────
def meta_worker():
    """Meta 広告収集スレッド（独立ブラウザ・Yahoo と並列動作）"""
    logging.info('[Meta] 起動')
    conn = get_connection()

    while not shutdown_event.is_set():
        try:
            with sync_playwright() as playwright:
                ctx = create_browser_context(playwright)

                meta_page = None
                if config.get('sources', {}).get('meta', True):
                    try:
                        meta_page = new_stealth_page(ctx)
                        logging.info('[Meta] ページ作成完了（ログイン不要・公開モード）')
                    except Exception as e:
                        logging.error(f'[Meta] page作成失敗: {e}', exc_info=True)

                if not meta_page:
                    logging.error('[Meta] ページ初期化失敗 → 終了')
                    return

                recycle_every = config.get('browser_recycle_keywords', 80)
                keyword_count = 0
                beat('meta')
                while not shutdown_event.is_set() and keyword_count < recycle_every:
                    if pause_event.is_set():
                        beat('meta')
                        time.sleep(5)
                        continue

                    meta_cooling = config.get('timing', {}).get('meta_keyword_cooling_hours', 0)
                    min_delay = config.get('timing', {}).get('min_delay_seconds', 30)
                    max_delay = config.get('timing', {}).get('max_delay_seconds', 180)
                    boost = _get_boost_patterns()

                    try:
                        beat('meta')

                        # meta_page が None（再作成失敗）ならスレッド終了してWatchdogに任せる
                        if meta_page is None:
                            logging.error('[Meta] ページが無効。スレッドを終了してWatchdogに再起動を委譲します')
                            _ctx_dead['meta'].set()  # Watchdogに即時再起動を通知
                            return

                        processed = False
                        auto_refill_if_low(conn, 'meta', cooling_hours=meta_cooling, threshold=15, batch_size=40)

                        kw_info = get_next_keyword(conn, 'meta', meta_cooling, boost_patterns=boost)
                        if kw_info:
                            keyword = kw_info['keyword']
                            logging.info(f'[Meta] キーワード: "{keyword}"')
                            try:
                                # Meta API（公式）を優先 → 失敗時はPlaywrightにフォールバック
                                api_ads = scrape_meta_via_api(keyword)
                                if api_ads is not None:
                                    # API成功（0件も含む）
                                    diag = get_diagnostics()
                                    diag.record_scrape('Meta', len(api_ads))
                                    if api_ads:
                                        diag.reset_meta_failures()
                                    for ad_item in api_ads:
                                        ad_url = ad_item['url'] if isinstance(ad_item, dict) else ad_item
                                        ad_pname = ad_item.get('page_name') if isinstance(ad_item, dict) else None
                                        _enqueue_lp(ad_url, 'Meta', keyword, ad_pname)
                                    update_keyword_searched(conn, keyword)
                                    processed = True
                                    keyword_count += 1
                                    delay = random.uniform(min_delay, max_delay)
                                    _interruptible_sleep(delay, 'meta')
                                    continue  # Playwrightスキップ

                                ads = scrape_meta(meta_page, keyword)
                                diag = get_diagnostics()

                                # ── ページ生死確認（scrape_meta が内部で例外を飲んでも検出） ──
                                page_alive = _check_meta_page_alive(meta_page)
                                if not page_alive:
                                    logging.warning('[Meta] ページ死亡検知（browser closed） → コンテキスト再作成')
                                    meta_page = _recreate_meta_page(ctx)
                                    # キーワード消費せずに次のループへ（このキーワードは再試行される）
                                    _interruptible_sleep(5, 'meta')
                                    continue

                                diag.record_scrape('Meta', len(ads))
                                if ads:
                                    diag.reset_meta_failures()
                                else:
                                    # 0件の場合: ログインリダイレクト時のみ失敗カウント
                                    # (キーワードにMetaアドがない正常ケースは除外)
                                    try:
                                        if 'login' in meta_page.url or 'checkpoint' in meta_page.url:
                                            logging.warning('[Meta] ログインページ検出 → ページリセット')
                                            meta_page.goto('about:blank', timeout=5000)
                                            diag.record_meta_failure()
                                    except Exception:
                                        pass
                                for ad in ads:
                                    _enqueue_lp(ad['url'], 'Meta', keyword, ad.get('company'))
                            except Exception as e:
                                err_lower = str(e).lower()
                                _BROWSER_DEAD_SIGNALS = (
                                    'target closed', 'target page', 'context or browser',
                                    'has been closed', 'connection reset', 'browser closed',
                                    'crash', 'connection closed while reading',
                                )
                                if any(k in err_lower for k in _BROWSER_DEAD_SIGNALS):
                                    logging.warning(f'[Meta] ブラウザ死亡例外 → コンテキスト再作成: {e}')
                                    meta_page = _recreate_meta_page(ctx)
                                    _interruptible_sleep(5, 'meta')
                                    continue  # キーワード消費しない
                                else:
                                    logging.error(f'[Meta] エラー: {e}')
                            beat('meta')

                            update_keyword_searched(conn, keyword)
                            processed = True
                            keyword_count += 1

                            delay = random.uniform(min_delay, max_delay)
                            _interruptible_sleep(delay, 'meta')

                        if not processed:
                            logging.info('[Meta] 利用可能なキーワードなし（全アーカイブ済み）。60秒待機')
                            _interruptible_sleep(60, 'meta')

                    except Exception as e:
                        logging.error(f'[Meta] 予期しないエラー: {e}')
                        _interruptible_sleep(30, 'meta')

                try:
                    ctx.close()
                except Exception:
                    pass

            if not shutdown_event.is_set():
                logging.info(f'[Meta] ブラウザリサイクル ({keyword_count}件処理済み) → 再起動します')

        except Exception as e:
            logging.error(f'[Meta] 致命的エラーで終了: {e}', exc_info=True)
            if not shutdown_event.is_set():
                time.sleep(10)

    logging.info('[Meta] 終了')
