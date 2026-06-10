# NTA法人番号リトライワーカー（オーナー最重要ポイント①: 法人番号取得率の最大化）
#
# 法人番号が未取得のままDBに残ったレコードを定期的に再照会する。
# 取得時のNTA一時障害（レート制限・タイムアウト・400スキップ）で落ちた分を
# 時間を置いて回収し、取得率を底上げする。
#
# 成功したレコードは:
# - corporate_number / 正式法人名 / 登記都道府県 / pref_match / phone_confidence を更新
# - exported=0 のままなので、writer の定期再送で自動的にSheetsへ流れる
#   （Sheetsには法人番号確定レコードのみ載せるポリシーと整合）
#
# 無限リトライを防ぐため nta_retry_count をDBに永続化し、上限到達で打ち切る。
from __future__ import annotations
import logging
import time

from state import beat, config, shutdown_event
from storage.database import get_connection

_DEFAULT_INTERVAL_HOURS = 6
_DEFAULT_BATCH_SIZE = 100
_MAX_RETRY_COUNT = 5
_FIRST_RUN_DELAY = 300        # 起動5分後に初回実行（起動直後の負荷集中を避ける）
_PER_LOOKUP_DELAY = 0.5       # NTA APIへの礼儀（バックオフは resolver 側にもある）


def _sleep_with_beat(seconds: float) -> bool:
    """ハートビートを打ちながら待機する。shutdown時は True を返して中断。"""
    end = time.time() + seconds
    while time.time() < end:
        if shutdown_event.is_set():
            return True
        beat('nta_retry')
        time.sleep(min(30, max(1, end - time.time())))
    return False


def _retry_batch(conn) -> tuple[int, int]:
    """未取得レコードを1バッチ再照会する。(試行数, 成功数) を返す。"""
    from processors.legal_name_resolver import get_prefecture, lookup_corporate_number
    from processors.quality import calc_phone_confidence, demote_rank
    from utils.area_codes import pref_match_level

    nta_key = config.get('nta_api_key', '')
    if not nta_key:
        return 0, 0

    batch_size = int(config.get('nta_retry', {}).get('batch_size', _DEFAULT_BATCH_SIZE))
    rows = conn.execute(
        '''
        SELECT id, company_name, phone, phones, phone_source, rank,
               COALESCE(nta_retry_count, 0) AS retry_count
        FROM companies
        WHERE (corporate_number IS NULL OR corporate_number = '')
          AND company_name != ''
          AND COALESCE(nta_retry_count, 0) < ?
        ORDER BY id DESC
        LIMIT ?
        ''',
        (_MAX_RETRY_COUNT, batch_size)
    ).fetchall()

    tried = ok = 0
    for row in rows:
        if shutdown_event.is_set():
            break
        beat('nta_retry')
        tried += 1

        corp_num, official_name = lookup_corporate_number(row['company_name'], nta_key)
        if corp_num == '__NTA_ERROR__':
            # 一時障害はリトライ回数を消費しない（次サイクルで再挑戦）
            time.sleep(_PER_LOOKUP_DELAY)
            continue

        if corp_num:
            new_name = official_name or row['company_name']
            nta_pref = get_prefecture(corp_num)
            pref_match = pref_match_level(nta_pref, row['phone'] or '')
            confidence = calc_phone_confidence(row['phone_source'] or '', pref_match)
            rank = row['rank'] or 'C'
            if pref_match == 'mismatch':
                rank = demote_rank(rank)
            # normalized_name は重複判定キーのため変更しない（UNIQUE衝突防止）
            conn.execute(
                '''
                UPDATE companies
                SET corporate_number=?, company_name=?, nta_prefecture=?,
                    pref_match=?, phone_confidence=?, rank=?,
                    nta_retry_count=COALESCE(nta_retry_count, 0) + 1
                WHERE id=?
                ''',
                (corp_num, new_name, nta_pref, pref_match, confidence, rank, row['id'])
            )
            ok += 1
            logging.info(
                f'[NTA-Retry] 法人番号回収: "{row["company_name"]}" → {corp_num}'
                + (f' / 正式名: "{new_name}"' if new_name != row['company_name'] else '')
            )
        else:
            # NTA正常応答でヒットなし → リトライ回数を消費
            conn.execute(
                'UPDATE companies SET nta_retry_count=COALESCE(nta_retry_count, 0) + 1 WHERE id=?',
                (row['id'],)
            )
        conn.commit()
        time.sleep(_PER_LOOKUP_DELAY)

    conn.commit()
    return tried, ok


def nta_retry_worker():
    logging.info('[NTA-Retry] 起動')
    beat('nta_retry')

    if _sleep_with_beat(_FIRST_RUN_DELAY):
        return

    conn = get_connection()
    while not shutdown_event.is_set():
        try:
            tried, ok = _retry_batch(conn)
            if tried:
                logging.info(f'[NTA-Retry] サイクル完了: {tried}件試行 / {ok}件取得')
            else:
                logging.debug('[NTA-Retry] 対象レコードなし')
        except Exception as e:
            logging.error(f'[NTA-Retry] エラー: {e}', exc_info=True)

        interval_h = float(config.get('nta_retry', {}).get('interval_hours', _DEFAULT_INTERVAL_HOURS))
        if _sleep_with_beat(interval_h * 3600):
            break

    logging.info('[NTA-Retry] 終了')
