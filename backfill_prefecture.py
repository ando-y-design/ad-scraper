# 既存レコードへの遡及適用: NTA登記都道府県 × 市外局番の整合チェック
#
# corporate_number 取得済みの全レコードについて、NTA法人番号API（番号検索）で
# 登記都道府県を取得し、pref_match / phone_confidence を計算してDBに保存する。
# mismatch はランクを1段階降格する（Sheetsの行は削除しない・DBのみ更新）。
#
# 使い方:
#   python3 backfill_prefecture.py            # 実行
#   python3 backfill_prefecture.py --dry-run  # 更新せず集計のみ
import argparse
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from processors.quality import calc_phone_confidence, demote_rank  # noqa: E402
from utils.area_codes import pref_match_level  # noqa: E402
from utils.config_loader import load_config  # noqa: E402

_NTA_NUM_API = 'https://api.houjin-bangou.nta.go.jp/4/num'
_DELAY = 0.25  # NTA APIレート制限への配慮


def fetch_prefecture(corp_number: str, api_key: str) -> str:
    """法人番号から登記都道府県を返す（取得不能は空文字）。"""
    try:
        resp = requests.get(
            _NTA_NUM_API,
            params={'id': api_key, 'number': corp_number, 'type': '12', 'history': '0'},
            timeout=20,
        )
        if resp.status_code != 200:
            return ''
        root = ET.fromstring(resp.content)
        elem = root.find('.//corporation/prefectureName')
        return elem.text.strip() if elem is not None and elem.text else ''
    except Exception:
        return ''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    config = load_config()
    api_key = config.get('nta_api_key', '')
    if not api_key:
        print('nta_api_key が未設定です')
        sys.exit(1)

    conn = sqlite3.connect(BASE_DIR / 'companies.db', timeout=30)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        '''SELECT id, company_name, phone, phones, phone_source, rank, corporate_number
           FROM companies
           WHERE corporate_number IS NOT NULL AND corporate_number != ''
             AND (nta_prefecture IS NULL OR nta_prefecture = '')'''
    ).fetchall()
    print(f'対象: {len(rows)}件')

    stats = {'match': 0, 'near': 0, 'mismatch': 0, 'unknown': 0, 'api_fail': 0}
    demoted = 0
    for i, row in enumerate(rows, 1):
        corp_num = re.sub(r'\D', '', row['corporate_number'])
        if not corp_num:
            continue
        pref = fetch_prefecture(corp_num, api_key)
        if not pref:
            stats['api_fail'] += 1
            time.sleep(_DELAY)
            continue

        match = pref_match_level(pref, row['phone'] or '')
        confidence = calc_phone_confidence(row['phone_source'] or '', match)
        rank = row['rank'] or 'C'
        if match == 'mismatch':
            new_rank = demote_rank(rank)
            if new_rank != rank:
                demoted += 1
            rank = new_rank
        stats[match] += 1

        if not args.dry_run:
            # 1行ごとに即コミットする。バッチコミットだとAPI待ちの間も
            # 書き込みロックを保持し続け、本体スレッドが database is locked になる
            conn.execute(
                '''UPDATE companies
                   SET nta_prefecture=?, pref_match=?, phone_confidence=?, rank=?
                   WHERE id=?''',
                (pref, match, confidence, rank, row['id'])
            )
            conn.commit()
        if i % 100 == 0:
            print(f'  {i}/{len(rows)} 処理済み {stats}', flush=True)
        time.sleep(_DELAY)

    conn.commit()
    print(f'完了: {stats} / ランク降格 {demoted}件')
    total = sum(stats[k] for k in ('match', 'near', 'mismatch', 'unknown'))
    if total:
        ok = stats['match'] + stats['near']
        print(f'地理整合率（match+near）: {ok / total * 100:.1f}% ({ok}/{total})')
    conn.close()


if __name__ == '__main__':
    main()
