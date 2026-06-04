"""フリーダイヤル（0120/0800/0570/0990）をDBとSheetsから削除する"""
import sqlite3
import gspread
from google.oauth2.service_account import Credentials

DB_PATH = '/Users/holy/Downloads/ad_scraper/companies.db'
CREDS_PATH = '/Users/holy/Downloads/ad_scraper/credentials.json'
SHEET_ID = '1NQysFvXeQzV76d4EfVQWn2z8HobJLMxCqx6GYNMBis4'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

FREE_PATTERNS = ('0120', '0800', '0570', '0990')

def is_freephone(phone: str) -> bool:
    digits = ''.join(c for c in (phone or '') if c.isdigit())
    return any(digits.startswith(p) for p in FREE_PATTERNS)

def main():
    # --- DB から対象を取得 ---
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    targets = conn.execute(
        "SELECT id, company_name, phone FROM companies "
        "WHERE phone LIKE '0120%' OR phone LIKE '0800%' OR phone LIKE '0570%' OR phone LIKE '0990%'"
    ).fetchall()
    free_phones = {r['phone'] for r in targets}
    free_names  = {r['company_name'] for r in targets}
    print(f'DB削除対象: {len(targets)}件')

    # --- Sheets から該当行を検索して削除 ---
    creds = Credentials.from_service_account_file(CREDS_PATH, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SHEET_ID)
    ws = spreadsheet.worksheet('リスト')

    all_values = ws.get_all_values()
    header = all_values[0] if all_values else []

    # 列インデックスを特定（電話番号列・会社名列）
    try:
        phone_col = header.index('電話番号')
        name_col  = header.index('会社名')
    except ValueError:
        # フォールバック: 列名で探す
        phone_col = next((i for i, h in enumerate(header) if '電話' in h), 8)
        name_col  = next((i for i, h in enumerate(header) if '会社' in h), 6)

    # 削除対象行番号（Sheets は1始まり、ヘッダーは行1）
    rows_to_delete = []
    for i, row in enumerate(all_values[1:], start=2):
        phone = row[phone_col] if len(row) > phone_col else ''
        name  = row[name_col]  if len(row) > name_col  else ''
        if is_freephone(phone) or phone in free_phones or name in free_names:
            rows_to_delete.append(i)

    print(f'Sheets削除対象: {len(rows_to_delete)}行')

    # 下から削除（行番号がずれないように）
    import time
    for sheet_row in sorted(rows_to_delete, reverse=True):
        for attempt in range(5):
            try:
                ws.delete_rows(sheet_row)
                print(f'  Sheets行{sheet_row} 削除')
                time.sleep(1.2)
                break
            except Exception as e:
                if '429' in str(e):
                    wait = 60 * (attempt + 1)
                    print(f'  レート制限 → {wait}秒待機...')
                    time.sleep(wait)
                else:
                    print(f'  エラー行{sheet_row}: {e}')
                    break

    # --- DB から削除 ---
    ids = [r['id'] for r in targets]
    if ids:
        placeholders = ','.join('?' * len(ids))
        conn.execute(f'DELETE FROM companies WHERE id IN ({placeholders})', ids)
        conn.commit()
        print(f'DB {len(ids)}件 削除完了')

    conn.close()
    print('完了')

if __name__ == '__main__':
    main()
