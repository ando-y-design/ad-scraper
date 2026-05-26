# -*- coding: utf-8 -*-
"""WORSEになった3件のURLを確認する（ネットワーク障害か精度退化か）"""
import sys, sqlite3, requests
sys.path.insert(0, r'C:\Users\amdwt\ad_scraper')

conn = sqlite3.connect(r'C:\Users\amdwt\ad_scraper\companies.db')
cur = conn.cursor()
cur.execute('SELECT id, company_name, phone, lp_url FROM companies WHERE id IN (3233, 3232, 3222)')
rows = cur.fetchall()
conn.close()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36',
}

for row_id, company, phone, url in rows:
    print(f'ID={row_id}')
    print(f'  DB会社名: {company}')
    print(f'  DB電話  : {phone}')
    print(f'  URL     : {url}')
    try:
        r = requests.head(url, headers=HEADERS, timeout=5, allow_redirects=True)
        print(f'  HTTP    : {r.status_code}')
    except Exception as e:
        print(f'  HTTP    : ERROR ({type(e).__name__}: {e})')
    print()
