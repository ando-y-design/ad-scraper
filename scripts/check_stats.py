import sqlite3
from pathlib import Path

conn = sqlite3.connect(str(Path(__file__).parent.parent / 'companies.db'))
cur = conn.cursor()

cur.execute("PRAGMA table_info(keywords)")
kw_cols = [r[1] for r in cur.fetchall()]
print('keywordsカラム:', kw_cols)

cur.execute('SELECT COUNT(*) FROM keywords')
print('総KW数:', cur.fetchone()[0])

cur.execute('SELECT COUNT(*) FROM companies')
print('総件数:', cur.fetchone()[0])

# found_date で今日分
cur.execute("SELECT COUNT(*) FROM companies WHERE found_date = date('now', 'localtime')")
print('今日の取得数:', cur.fetchone()[0])

cur.execute("SELECT ad_sources, COUNT(*) FROM companies GROUP BY ad_sources ORDER BY COUNT(*) DESC")
for row in cur.fetchall():
    print(' ソース別:', row)

conn.close()
