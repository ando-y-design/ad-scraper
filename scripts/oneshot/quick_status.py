import sqlite3, time, os

conn = sqlite3.connect('companies.db')
total = conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0]
today = conn.execute("SELECT COUNT(*) FROM companies WHERE found_date >= date('now','localtime')").fetchone()[0]
print(f'DB企業数: {total}件 (今日: {today}件)')

# CAPTCHA状态
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

# keyword stats
meta_avail = conn.execute(
    "SELECT COUNT(*) FROM keywords WHERE source='meta' AND is_archived=0"
).fetchone()[0]
gy_avail = conn.execute(
    "SELECT COUNT(*) FROM keywords WHERE source IN ('google_yahoo','auto_expanded') AND is_archived=0"
).fetchone()[0]
print(f'利用可能キーワード: Meta={meta_avail}件 / Google+Yahoo={gy_avail}件')
