import sqlite3
conn = sqlite3.connect('companies.db')
total = conn.execute("SELECT COUNT(*) FROM keywords WHERE source='meta'").fetchone()[0]
archived = conn.execute("SELECT COUNT(*) FROM keywords WHERE source='meta' AND is_archived=1").fetchone()[0]
active = conn.execute("SELECT COUNT(*) FROM keywords WHERE source='meta' AND is_archived=0").fetchone()[0]
with_search = conn.execute("SELECT COUNT(*) FROM keywords WHERE source='meta' AND is_archived=0 AND last_searched IS NOT NULL").fetchone()[0]
null_search = conn.execute("SELECT COUNT(*) FROM keywords WHERE source='meta' AND is_archived=0 AND last_searched IS NULL").fetchone()[0]
print(f'Meta keywords: total={total} archived={archived} active={active}')
print(f'Active with last_searched: {with_search}  without: {null_search}')
sample = conn.execute("SELECT keyword, last_searched, is_archived FROM keywords WHERE source='meta' ORDER BY last_searched DESC LIMIT 10").fetchall()
print('Most recently searched:')
for row in sample:
    print(f'  {row[1]} | archived={row[2]} | {row[0]}')
