import sqlite3, sys, os
sys.path.insert(0, '.')

# キーワードDBのパスを特定
from utils.keywords import _get_db_path
db_path = _get_db_path()
print('Keywords DB:', db_path)

conn = sqlite3.connect(db_path)
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print('Tables:', tables)

# キーワード一覧
cols = [r[1] for r in conn.execute('PRAGMA table_info(keywords)').fetchall()]
print('Columns:', cols)

total_kw = conn.execute('SELECT COUNT(*) FROM keywords').fetchone()[0]
active_kw = conn.execute("SELECT COUNT(*) FROM keywords WHERE status='active'").fetchone()[0]
print(f'\n総キーワード: {total_kw}件 / アクティブ: {active_kw}件')

# 非歯科キーワード（アクティブのみ）
dental_patterns = [
    '%歯科%', '%インプラント%', '%矯正%', '%ホワイトニング%',
    '%入れ歯%', '%審美%', '%マウスピース%', '%インビザライン%',
    '%クリニック%', '%歯%', '%口腔%',
]
where_dental = ' OR '.join([f"keyword LIKE '{p}'" for p in dental_patterns])
dental_count = conn.execute(f"SELECT COUNT(*) FROM keywords WHERE status='active' AND ({where_dental})").fetchone()[0]
non_dental = conn.execute(f"SELECT COUNT(*) FROM keywords WHERE status='active' AND NOT ({where_dental})").fetchone()[0]

print(f'歯科関連: {dental_count}件')
print(f'非歯科（アーカイブ対象）: {non_dental}件')

# 非歯科キーワードのサンプル表示
samples = conn.execute(f"""
    SELECT keyword FROM keywords
    WHERE status='active' AND NOT ({where_dental})
    ORDER BY keyword LIMIT 20
""").fetchall()
print('\nサンプル（最初の20件）:')
for (kw,) in samples:
    print(f'  {kw}')

# アーカイブ実行
result = conn.execute(f"""
    UPDATE keywords SET status='archived'
    WHERE status='active' AND NOT ({where_dental})
""")
conn.commit()
print(f'\n✅ {result.rowcount}件をアーカイブ')

remaining = conn.execute("SELECT COUNT(*) FROM keywords WHERE status='active'").fetchone()[0]
print(f'残アクティブ: {remaining}件')

conn.close()
