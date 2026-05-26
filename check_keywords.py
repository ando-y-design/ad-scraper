import sqlite3

# キーワードDBを探す
import os
for f in os.listdir('.'):
    if f.endswith('.db'):
        print('DB:', f)

# companies.dbのキーワード一覧から非歯科系を特定
conn = sqlite3.connect('companies.db')
non_dental_kw = conn.execute("""
    SELECT DISTINCT keyword, COUNT(*) as cnt
    FROM companies
    WHERE keyword NOT LIKE '%歯科%'
      AND keyword NOT LIKE '%インプラント%'
      AND keyword NOT LIKE '%矯正%'
      AND keyword NOT LIKE '%ホワイトニング%'
      AND keyword NOT LIKE '%入れ歯%'
      AND keyword NOT LIKE '%審美%'
      AND keyword NOT LIKE '%マウスピース%'
      AND keyword NOT LIKE '%インビザライン%'
      AND keyword NOT LIKE '%クリニック%'
      AND keyword IS NOT NULL AND keyword != ''
    GROUP BY keyword ORDER BY cnt DESC
""").fetchall()

print(f'\n非歯科系キーワード ({len(non_dental_kw)}種類):')
for kw, cnt in non_dental_kw[:30]:
    print(f'  {cnt}件: {kw}')

conn.close()

# キーワードテーブルを確認
for dbfile in ['keywords.db', 'ad_leads.db']:
    if os.path.exists(dbfile):
        c = sqlite3.connect(dbfile)
        tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        print(f'\n{dbfile} tables:', tables)
        c.close()
