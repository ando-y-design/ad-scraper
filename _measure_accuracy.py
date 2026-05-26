# -*- coding: utf-8 -*-
"""
今回の変更が精度に与える影響を測定する。
アプローチ: 実際のLPのURLをDBから取得し、新しいコードで再処理して
既存DBの値と比較する。

測定指標:
1. 会社名・電話番号の両方が取れている割合（カバレッジ）
2. 同じLPから以前より良い値（またはそれまでNullだった値）が取れた件数

変更が効いたケースは主に「テキストフォールバックパス」に入るケース。
構造化ペア抽出(JSON-LD/table/dl)が成功したケースには影響しない。
"""
import sys, sqlite3, time, re
sys.path.insert(0, r'C:\Users\amdwt\ad_scraper')

# DBから最近追加されたURLを取得（見つかりやすい最新データ）
conn = sqlite3.connect(r'C:\Users\amdwt\ad_scraper\companies.db')
cur = conn.cursor()

# 全件のカバレッジを先に計算
cur.execute('SELECT COUNT(*) FROM companies')
total = cur.fetchone()[0]
cur.execute('SELECT COUNT(*) FROM companies WHERE company_name IS NOT NULL AND company_name != ""')
has_co = cur.fetchone()[0]
cur.execute('SELECT COUNT(*) FROM companies WHERE phone IS NOT NULL AND phone != ""')
has_ph = cur.fetchone()[0]
cur.execute('''SELECT COUNT(*) FROM companies
               WHERE company_name IS NOT NULL AND company_name != ""
               AND phone IS NOT NULL AND phone != ""''')
both = cur.fetchone()[0]

print(f'=== DBの現状カバレッジ ===')
print(f'総件数       : {total}')
print(f'会社名あり   : {has_co} ({has_co*100//total}%)')
print(f'電話あり     : {has_ph} ({has_ph*100//total}%)')
print(f'両方あり     : {both} ({both*100//total}%)')
print()

# 最近取得したURL 20件でre-runテスト
cur.execute('''
    SELECT id, company_name, phone, lp_url
    FROM companies
    WHERE lp_url IS NOT NULL AND lp_url != ""
    ORDER BY id DESC
    LIMIT 20
''')
rows = cur.fetchall()
conn.close()

from processors.company_finder import find_company_info

print(f'=== 最近の20件でre-run（タイムアウト各10秒）===')
improved = 0
same = 0
worse = 0
error = 0

for row_id, db_company, db_phone, lp_url in rows:
    try:
        new_company, new_phone, _, _, _ = find_company_info(lp_url)

        db_co_ok = bool(db_company and db_company.strip())
        db_ph_ok = bool(db_phone and db_phone.strip())
        new_co_ok = bool(new_company and new_company.strip())
        new_ph_ok = bool(new_phone and new_phone.strip())

        db_score = (1 if db_co_ok else 0) + (1 if db_ph_ok else 0)
        new_score = (1 if new_co_ok else 0) + (1 if new_ph_ok else 0)

        if new_score > db_score:
            status = 'IMPROVED'
            improved += 1
        elif new_score == db_score:
            status = 'SAME'
            same += 1
        else:
            status = 'WORSE'
            worse += 1

        print(f'[{status:8s}] id={row_id}')
        if status != 'SAME':
            print(f'  DB : co={db_company!r:35} ph={db_phone!r}')
            print(f'  NEW: co={new_company!r:35} ph={new_phone!r}')
    except Exception as e:
        print(f'[ERROR   ] id={row_id}: {e}')
        error += 1

print()
print(f'=== re-run結果 ===')
print(f'IMPROVED: {improved}/20')
print(f'SAME    : {same}/20')
print(f'WORSE   : {worse}/20')
print(f'ERROR   : {error}/20')
