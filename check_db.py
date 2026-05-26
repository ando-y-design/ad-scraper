import sqlite3
conn = sqlite3.connect(r'C:\Users\amdwt\ad_scraper\companies.db')
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cur.fetchall()
print('テーブル:', tables)
for (t,) in tables:
    cur.execute(f'PRAGMA table_info({t})')
    cols = [c[1] for c in cur.fetchall()]
    print(f'  {t}: {cols}')
    cur.execute(f'SELECT COUNT(*) FROM {t}')
    print(f'  件数: {cur.fetchone()[0]}')
    cur.execute(f'SELECT * FROM {t} ORDER BY rowid DESC LIMIT 3')
    for r in cur.fetchall():
        print(f'  {r}')
conn.close()
