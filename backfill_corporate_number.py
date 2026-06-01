#!/usr/bin/env python3
"""既存Sheetsの会社名（H列）からNTA APIで法人番号を取得してA列に書き込む。"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

BASE = Path(__file__).parent
config = json.loads((BASE / 'config.json').read_text())

SHEET_ID   = config['google_sheets']['sheet_id']
CREDS_PATH = BASE / config.get('service_account_key_path', 'credentials.json')
NTA_KEY    = config.get('nta_api_key', '')

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

# 現在のヘッダー構成
# 旧: リスト持主(A), CRM(B), ..., 会社名(G)
# 新: 法人番号(A), リスト持主(B), CRM(C), ..., 会社名(H)
# どちらの場合も会社名列とA列を動的に検出する


def get_col_index(headers: list[str], name: str) -> int | None:
    try:
        return headers.index(name)
    except ValueError:
        return None


def main():
    if not NTA_KEY:
        print('ERROR: nta_api_key が config.json に設定されていません')
        sys.exit(1)

    # Sheets接続
    creds = Credentials.from_service_account_file(str(CREDS_PATH), scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SHEET_ID)
    ws = spreadsheet.worksheet('リスト')

    all_values = ws.get_all_values()
    if not all_values:
        print('シートが空です')
        return

    headers = all_values[0]
    company_col = get_col_index(headers, '会社名')
    corp_num_col = get_col_index(headers, '法人番号')

    if company_col is None:
        print('ERROR: 会社名列が見つかりません')
        sys.exit(1)

    # A列（index 0）に法人番号を上書き。ヘッダーも更新。
    corp_num_col = 0
    ws.update(values=[['法人番号']], range_name='A1')
    print(f'会社名: {chr(65 + company_col)}列  → A列に法人番号を上書き')

    from processors.legal_name_resolver import lookup_corporate_number

    # A列（法人番号）が空の行だけ処理
    targets = []
    for i, row in enumerate(all_values[1:], start=2):  # 2行目から（1行目はヘッダー）
        company = row[company_col] if company_col < len(row) else ''
        corp_num = row[corp_num_col] if corp_num_col < len(row) else ''
        if company and not corp_num:
            targets.append((i, company))

    print(f'対象: {len(targets)} 件（法人番号が空の行）')
    if not targets:
        print('全行に法人番号が入っています。終了します。')
        return

    # 一括更新用データを構築（セルを個別更新するとレート制限に当たるため batch_update）
    updates = []
    col_letter = chr(65 + corp_num_col)  # 0=A, 1=B, ...

    for idx, (sheet_row, company_name) in enumerate(targets, 1):
        print(f'[{idx}/{len(targets)}] {company_name} ...', end=' ', flush=True)

        corp_num = lookup_corporate_number(company_name, NTA_KEY)

        if corp_num:
            print(corp_num)
            updates.append({
                'range': f'{col_letter}{sheet_row}',
                'values': [[corp_num]],
            })
        else:
            print('未取得')

        # NTA API への過負荷を防ぐ
        if idx % 10 == 0:
            time.sleep(2)
        else:
            time.sleep(0.5)

    # Sheetsへ一括書き込み
    if updates:
        ws.batch_update(updates, value_input_option='USER_ENTERED')
        print(f'\n完了: {len(updates)} 件を法人番号A列に書き込みました')
    else:
        print('\n書き込み対象なし（NTAでヒットした法人番号がありませんでした）')


if __name__ == '__main__':
    main()
