# -*- coding: utf-8 -*-
import sqlite3

conn = sqlite3.connect(r'C:\Users\amdwt\ad_scraper\companies.db')
cur = conn.cursor()

cur.execute('SELECT COUNT(*) FROM companies')
total = cur.fetchone()[0]

cur.execute('SELECT COUNT(*) FROM companies WHERE company_name IS NOT NULL AND company_name != ""')
has_co = cur.fetchone()[0]

cur.execute('SELECT COUNT(*) FROM companies WHERE phone IS NOT NULL AND phone != ""')
has_ph = cur.fetchone()[0]

cur.execute('''SELECT COUNT(*) FROM companies
               WHERE (company_name IS NULL OR company_name = "")''')
no_co = cur.fetchone()[0]

cur.execute('''SELECT COUNT(*) FROM companies
               WHERE (phone IS NULL OR phone = "")''')
no_ph = cur.fetchone()[0]

cur.execute('''SELECT COUNT(*) FROM companies
               WHERE company_name IS NOT NULL AND company_name != ""
               AND phone IS NOT NULL AND phone != ""''')
both = cur.fetchone()[0]

print(f'総件数       : {total}')
print(f'会社名あり   : {has_co} ({has_co*100//total}%)')
print(f'電話あり     : {has_ph} ({has_ph*100//total}%)')
print(f'両方あり     : {both} ({both*100//total}%)')
print(f'会社名なし   : {no_co}')
print(f'電話なし     : {no_ph}')

# ゴミデータ（ラベル名混入）
cur.execute('''SELECT COUNT(*) FROM companies
               WHERE company_name LIKE "%販売業者%"
               OR company_name LIKE "%事業者名%"
               OR company_name LIKE "%運営会社%"''')
garbage = cur.fetchone()[0]
print(f'\nラベル混入ゴミ: {garbage}件')

# 会社名文字数分布
cur.execute('''SELECT
    CASE
        WHEN LENGTH(company_name) <= 5 THEN "5文字以下"
        WHEN LENGTH(company_name) <= 10 THEN "6-10文字"
        WHEN LENGTH(company_name) <= 20 THEN "11-20文字"
        ELSE "21文字以上"
    END as bucket,
    COUNT(*) as cnt
    FROM companies
    WHERE company_name IS NOT NULL AND company_name != ""
    GROUP BY bucket ORDER BY cnt DESC''')
print('\n会社名文字数分布:')
for bucket, cnt in cur.fetchall():
    print(f'  {bucket}: {cnt}件')

# 最近30件
cur.execute('SELECT company_name, phone FROM companies ORDER BY id DESC LIMIT 30')
rows = cur.fetchall()
print('\n最近30件:')
for co, ph in rows:
    co_s = (co or 'NULL')[:30]
    print(f'  {co_s:32} | {ph or "NULL"}')

conn.close()
