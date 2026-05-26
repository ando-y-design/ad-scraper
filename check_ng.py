import sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8')
conn = sqlite3.connect('companies.db')
conn.row_factory = sqlite3.Row
cases = [
    '一般社団法人日本自動車購入協会(JPUC)',
    '株式会社リーフ',
    'ソニー生命保険株式会社',
    'Gramn Inc.',
    '株式会社カーネクスト',
    'PRONI株式会社',
    '合同会社アクトリンク',
    '株式会社トレタ',
]
for name in cases:
    row = conn.execute('SELECT company_name, phone, lp_url, ad_sources FROM companies WHERE company_name=? LIMIT 1', (name,)).fetchone()
    if row:
        print(f'{row["company_name"]} | {row["phone"]} | {row["lp_url"]} | {row["ad_sources"]}')
    else:
        print(f'{name}: NOT FOUND')
conn.close()
