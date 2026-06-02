from __future__ import annotations
from typing import Optional
"""SQLite + Sheets 書き込みワーカー"""
import logging
import queue

from processors.rank_calculator import calc_rank
from storage.database import (
    get_connection, get_unexported, insert_company, mark_exported,
)
from storage.sheets_writer import SheetsWriter, get_sheets_client, get_worksheet
from utils.keyword_expander import maybe_expand_keyword
from utils.keywords import update_keyword_found

from state import (
    config, shutdown_event, result_queue, BASE_DIR, beat,
)


# ─────────────────────────────────────────────
# THREAD 3: SQLite + Sheets書き込み
# ─────────────────────────────────────────────
def _mark_batch_exported(conn, flush_results: list[tuple[str, int]]):
    """フラッシュ結果の全アイテムに対してmark_exportedを呼ぶ"""
    for normalized_name, sheet_row in flush_results:
        if not normalized_name:
            continue
        row = conn.execute(
            'SELECT id FROM companies WHERE normalized_name=?', (normalized_name,)
        ).fetchone()
        if row:
            mark_exported(conn, row['id'], sheet_row)


def writer_worker():
    logging.info('[Writer] 起動')
    conn = get_connection()
    beat('writer')

    sheets_cfg = config.get('google_sheets', {})
    sheet_id = sheets_cfg.get('sheet_id', '')
    creds_path = str(BASE_DIR / sheets_cfg.get('service_account_key_path', 'credentials.json'))

    writer: Optional[SheetsWriter] = None
    try:
        client = get_sheets_client(creds_path)
        ws = get_worksheet(client, sheet_id)
        writer = SheetsWriter(
            ws,
            batch_size=config.get('timing', {}).get('batch_size', 50),
            batch_timeout=config.get('timing', {}).get('batch_timeout_seconds', 300),
            shutdown_event=shutdown_event,
            heartbeat_callback=lambda: beat('writer'),
        )
        logging.info('[Writer] Google Sheets接続成功')
        writer.sync_headers()  # ヘッダー行が古い場合は自動更新

        # 重複チェック台帳を初期化
        try:
            from storage.dedup_registry import init_registry
            account_id = config.get('account_id', 'A')
            init_registry(client, account_id)
        except Exception as e:
            logging.warning(f'[Writer] 重複チェック台帳初期化失敗（無効化）: {e}')

        # 起動時: 前回未送信データをSheetsに再送
        unexported = get_unexported(conn)
        if unexported:
            logging.info(f'[Writer] 未送信データ {len(unexported)}件を再送します')
            for db_row in unexported:
                keys = db_row.keys()
                flush_results = writer.add({
                    'company_name': db_row['company_name'],
                    'normalized_name': db_row['normalized_name'],
                    'lp_url': db_row['lp_url'] or '',
                    'phone': db_row['phone'] or '',
                    'phones': db_row['phones'] if 'phones' in keys else None,
                    'ad_sources': db_row['ad_sources'] or '',
                    'keyword': db_row['keyword'] or '',
                    'found_date': db_row['found_date'],
                    'rank': db_row['rank'] if 'rank' in keys else '',
                    'corporate_number': db_row['corporate_number'] if 'corporate_number' in keys else '',
                    'contact_name': db_row['contact_name'] if 'contact_name' in keys else None,
                    'lp_headline': db_row['lp_headline'] if 'lp_headline' in keys else None,
                    'all_keywords': db_row['all_keywords'] if 'all_keywords' in keys else None,
                    'competitors': '',
                })
                if flush_results:
                    _mark_batch_exported(conn, flush_results)
            flush_results = writer.flush()
            if flush_results:
                _mark_batch_exported(conn, flush_results)

    except Exception as e:
        logging.error(f'[Writer] Google Sheets接続失敗: {e}')

    while not shutdown_event.is_set():
        beat('writer')
        try:
            data = result_queue.get(timeout=5)
        except queue.Empty:
            if writer:
                flush_results = writer.flush_if_timeout()
                if flush_results:
                    _mark_batch_exported(conn, flush_results)
            continue

        try:
            inserted = insert_company(conn, data)
            if not inserted:
                # 重複: seen_count と rank を更新してDBに反映
                row = conn.execute(
                    'SELECT id, seen_count, ad_sources FROM companies WHERE normalized_name=?',
                    (data.get('normalized_name', ''),)
                ).fetchone()
                if row:
                    new_count = (row['seen_count'] or 1) + 1
                    srcs = set((row['ad_sources'] or '').split(',')) - {''}
                    new_src = data.get('ad_sources', '')
                    if new_src:
                        srcs.add(new_src)
                    merged = ','.join(sorted(srcs))
                    new_rank = calc_rank(new_count, merged)
                    conn.execute(
                        'UPDATE companies SET seen_count=?, ad_sources=?, rank=? WHERE id=?',
                        (new_count, merged, new_rank, row['id'])
                    )
                    conn.commit()
                continue

            # 他アカウントとの重複チェック
            from storage.dedup_registry import check_and_register
            if not check_and_register(data.get('phone', ''), data.get('company_name', '')):
                logging.debug(f'[Writer] 他アカウント重複スキップ: {data.get("company_name")}')
                continue

            # processorが計算したrankをDBに保存（INSERT時はrankカラムが含まれないため）
            if data.get('rank'):
                conn.execute(
                    'UPDATE companies SET rank=? WHERE normalized_name=?',
                    (data['rank'], data.get('normalized_name', ''))
                )
                conn.commit()


            if data.get('keyword'):
                update_keyword_found(conn, data['keyword'])
                maybe_expand_keyword(conn, data['keyword'], data.get('ad_sources', ''))

            if writer:
                flush_results = writer.add(data)
                if flush_results:
                    _mark_batch_exported(conn, flush_results)
            beat('writer')

        except Exception as e:
            logging.error(f'[Writer] エラー: {e}', exc_info=True)
        finally:
            result_queue.task_done()

    # 終了前に残バッチをフラッシュ
    if writer:
        flush_results = writer.flush()
        if flush_results:
            _mark_batch_exported(conn, flush_results)

    logging.info('[Writer] 終了')
