import sqlite3, sys
sys.stdout.reconfigure(encoding="utf-8")
conn = sqlite3.connect(r"C:\Users\amdwt\ad_scraper\companies.db")
rows = conn.execute("SELECT COUNT(*) total, MAX(found_date) latest FROM companies").fetchone()
print(f"Total: {rows[0]}, Latest date: {rows[1]}")
conn.close()
