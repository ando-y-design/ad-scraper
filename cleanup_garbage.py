import sqlite3

conn = sqlite3.connect('C:/Users/amdwt/ad_scraper/companies.db')

# 削除対象の特定（法人格なし かつ 医療機関名なし）
targets = conn.execute("""
    SELECT id, company_name, phone, ad_sources, keyword FROM companies
    WHERE company_name NOT LIKE '%株式会社%'
      AND company_name NOT LIKE '%有限会社%'
      AND company_name NOT LIKE '%合同会社%'
      AND company_name NOT LIKE '%医療法人%'
      AND company_name NOT LIKE '%社団法人%'
      AND company_name NOT LIKE '%財団法人%'
      AND company_name NOT LIKE '%法人%'
      AND company_name NOT LIKE '%クリニック%'
      AND company_name NOT LIKE '%歯科%'
      AND company_name NOT LIKE '%病院%'
      AND company_name NOT LIKE '%医院%'
      AND company_name NOT LIKE '%診療所%'
""").fetchall()

print(f'削除対象: {len(targets)}件')
for r in targets:
    print(f'  ID:{r[0]} {r[1]} / {r[2]}')

ids = [r[0] for r in targets]
if ids:
    conn.execute(f"DELETE FROM companies WHERE id IN ({','.join(str(i) for i in ids)})")
    conn.commit()
    print(f'\n✅ {len(ids)}件削除完了')

# 残件数確認
remaining = conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0]
print(f'残件数: {remaining}件')

conn.close()
