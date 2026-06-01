"""
NTAバッチ: 法人番号なしレコードに法人番号を一括付与してSheetsのA列も更新する。
実行: python3 scripts/nta_batch.py
"""
from __future__ import annotations
import json
import sqlite3
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

import gspread
from google.oauth2.service_account import Credentials
from processors.legal_name_resolver import lookup_corporate_number

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]


def main():
    cfg = json.loads((BASE_DIR / 'config.json').read_text())
    api_key = cfg.get('nta_api_key', '')
    if not api_key:
        print('ERROR: nta_api_key が config.json に未設定')
        return

    sheets_cfg = cfg.get('google_sheets', {})
    creds_path = str(BASE_DIR / sheets_cfg.get('service_account_key_path', 'credentials.json'))
    sheet_id = sheets_cfg.get('sheet_id', '')

    # Sheets接続
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    ws = client.open_by_key(sheet_id).worksheet('リスト')
    print('Sheets接続OK')

    # 対象レコード取得
    conn = sqlite3.connect(str(BASE_DIR / 'companies.db'))
    conn.row_factory = sqlite3.Row
    rows = conn.execute('''
        SELECT id, company_name, sheet_row
        FROM companies
        WHERE (corporate_number IS NULL OR corporate_number = "")
          AND sheet_row IS NOT NULL AND sheet_row > 0
        ORDER BY id
    ''').fetchall()
    total = len(rows)
    print(f'対象: {total}件')

    hit = 0
    miss = 0
    sheets_updates: list[gspread.Cell] = []

    for i, row in enumerate(rows, 1):
        name = row['company_name'] or ''
        if not name:
            miss += 1
            continue

        corp_num = lookup_corporate_number(name, api_key)

        if corp_num:
            # SQLite更新
            conn.execute(
                'UPDATE companies SET corporate_number=? WHERE id=?',
                (corp_num, row['id'])
            )
            conn.commit()

            # Sheets更新セルをバッファ
            sheets_updates.append(
                gspread.Cell(row=row['sheet_row'], col=1, value=corp_num)
            )
            hit += 1
        else:
            miss += 1

        # 進捗表示
        if i % 50 == 0 or i == total:
            print(f'  {i}/{total}  取得: {hit}件  未取得: {miss}件')

        # Sheetsバッチ書き込み（100件ごと）
        if len(sheets_updates) >= 100:
            ws.update_cells(sheets_updates, value_input_option='USER_ENTERED')
            print(f'  → Sheets書き込み: {len(sheets_updates)}件')
            sheets_updates.clear()
            time.sleep(1)  # レート制限

        time.sleep(0.3)  # NTA APIレート制限

    # 残りをSheetsに書き込み
    if sheets_updates:
        ws.update_cells(sheets_updates, value_input_option='USER_ENTERED')
        print(f'  → Sheets書き込み: {len(sheets_updates)}件')

    conn.close()
    print(f'\n完了: 法人番号取得 {hit}件 / 未取得 {miss}件 / 合計 {total}件')


if __name__ == '__main__':
    main()
