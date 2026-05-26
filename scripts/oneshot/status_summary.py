import sqlite3, time, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

conn = sqlite3.connect('companies.db')

total = conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0]
today = conn.execute("SELECT COUNT(*) FROM companies WHERE found_date >= date('now','localtime')").fetchone()[0]
this_week = conn.execute("SELECT COUNT(*) FROM companies WHERE found_date >= date('now','localtime','-7 days')").fetchone()[0]

print('=== ad_scraper ステータス ===')
print(f'累計取得会社数: {total}件')
print(f'今日:           {today}件')
print(f'今週:           {this_week}件')
print()

by_source = conn.execute(
    "SELECT ad_sources, COUNT(*) FROM companies GROUP BY ad_sources ORDER BY COUNT(*) DESC"
).fetchall()
print('【ソース別】')
for src, cnt in by_source:
    print(f'  {src or "不明"}: {cnt}件')
print()

recent = conn.execute(
    "SELECT company_name, phone, ad_sources, keyword, found_date FROM companies ORDER BY found_date DESC LIMIT 10"
).fetchall()
print('【直近10件】')
for row in recent:
    print(f'  {row[4]} | {row[0]} | {row[1] or "-"} | {row[2]} | {row[3]}')
print()

captcha_file = 'logs/.google_captcha_state'
if os.path.exists(captcha_file):
    data = open(captcha_file).read().strip().split(',')
    consecutive, until = int(data[0]), float(data[1])
    remaining = int(until - time.time())
    if remaining > 0:
        print(f'Google CAPTCHA: {consecutive}連続 / 残り{remaining//3600}h{(remaining%3600)//60}m')
    else:
        print('Google CAPTCHA: 解除済み')
else:
    print('Google CAPTCHA: なし（正常）')

kw_gy = conn.execute("SELECT COUNT(*) FROM keywords WHERE source IN ('google_yahoo','auto_expanded') AND is_archived=0").fetchone()[0]
kw_meta = conn.execute("SELECT COUNT(*) FROM keywords WHERE source='meta' AND is_archived=0").fetchone()[0]
print(f'キーワード: Google/Yahoo={kw_gy}件  Meta={kw_meta}件')
