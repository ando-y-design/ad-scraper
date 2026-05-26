import sqlite3, datetime, time

conn = sqlite3.connect('companies.db')

total = conn.execute("SELECT COUNT(*) FROM keywords WHERE source='meta'").fetchone()[0]
archived = conn.execute("SELECT COUNT(*) FROM keywords WHERE source='meta' AND is_archived=1").fetchone()[0]
no_search = conn.execute("SELECT COUNT(*) FROM keywords WHERE source='meta' AND is_archived=0 AND last_searched IS NULL").fetchone()[0]
cooling = conn.execute("SELECT COUNT(*) FROM keywords WHERE source='meta' AND is_archived=0 AND last_searched IS NOT NULL").fetchone()[0]
print(f"[Meta keywords] total={total} archived={archived} unsearched={no_search} cooling={cooling}")

gy_total = conn.execute("SELECT COUNT(*) FROM keywords WHERE source='google_yahoo'").fetchone()[0]
gy_arch = conn.execute("SELECT COUNT(*) FROM keywords WHERE source='google_yahoo' AND is_archived=1").fetchone()[0]
gy_available = conn.execute("SELECT COUNT(*) FROM keywords WHERE source='google_yahoo' AND is_archived=0").fetchone()[0]
print(f"[GY keywords] total={gy_total} archived={gy_arch} available={gy_available}")

try:
    kal = conn.execute('SELECT COUNT(*) FROM keyword_area_log').fetchone()[0]
    print(f"[keyword_area_log] entries={kal}")
except Exception as e:
    print(f"[keyword_area_log] not found: {e}")

companies = conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0]
print(f"[Companies] DB total={companies}")

# CAPTCHA backoff
try:
    data = open('logs/.google_captcha_state').read().strip().split(',')
    consecutive = int(data[0])
    until = float(data[1])
    remaining = int(until - time.time())
    print(f"[CAPTCHA] {consecutive}連続検出 / バックオフ残り={remaining}秒 ({remaining//3600}時間{(remaining%3600)//60}分)")
except:
    print("[CAPTCHA] バックオフなし")
