import sqlite3, sys
sys.stdout.reconfigure(encoding="utf-8")
conn = sqlite3.connect(r"C:\Users\amdwt\ad_scraper\companies.db")
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT company_name, phone, lp_url, ad_sources, found_date
    FROM companies
    ORDER BY rowid DESC
    LIMIT 15
""").fetchall()
for r in rows:
    print(f"{r['company_name'][:25]:25} | {r['phone']:15} | {r['ad_sources']:8} | {r['lp_url'][:40]}")
conn.close()
