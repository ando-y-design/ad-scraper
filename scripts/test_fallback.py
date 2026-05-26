"""
フォールバック検索の動作テスト
既存DBから「会社名あり・電話番号あり」の企業を使って
Yahoo検索経由でも電話番号が取れるか検証する
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from processors.company_finder import _search_phone_by_company_name

conn = sqlite3.connect(str(Path(__file__).parent.parent / 'companies.db'))
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT company_name, phone FROM companies WHERE phone IS NOT NULL LIMIT 5"
).fetchall()
conn.close()

print('=== フォールバック検索テスト ===')
hit = 0
for row in rows:
    company = row['company_name']
    expected = row['phone']
    found = _search_phone_by_company_name(company)
    match = 'OK' if found else 'NG'
    print(f'{match} {company}')
    print(f'   期待: {expected}  取得: {found}')
    if found:
        hit += 1

print(f'\n結果: {hit}/{len(rows)} 件でYahoo検索から電話番号取得成功')
