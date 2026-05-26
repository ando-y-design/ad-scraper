import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from storage.database import get_connection
conn = get_connection()
total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
today = conn.execute("SELECT COUNT(*) FROM companies WHERE found_date=date('now','localtime')").fetchone()[0]
print(f"総件数: {total}  本日: {today}")
for r in conn.execute("SELECT company_name, phone, ad_sources, found_date FROM companies ORDER BY id DESC LIMIT 5").fetchall():
    print(f"  {r['company_name'][:35]} / {r['phone']} ({r['ad_sources']}) {r['found_date']}")
