from __future__ import annotations
import sys, requests, re, time, sqlite3

sys.stdout.reconfigure(line_buffering=True)

conn = sqlite3.connect("companies.db")
rows = conn.execute(
    "SELECT id, company_name, phone, lp_url FROM companies ORDER BY id"
).fetchall()
conn.close()
print(f"DBレコード: {len(rows)}件")

def core_name(name):
    name = re.sub(r"株式会社|有限会社|合同会社|一般社団法人|医療法人|社会福祉法人", "", name)
    name = re.sub(r"[\s　・（）()【】「」〔〕]", "", name)
    return name.lower()

def itp_reverse(digits, session):
    try:
        r = session.get(
            "https://itp.ne.jp/searchtm/entry/top/search/",
            params={"SearchCode": "", "type": "By", "TelNo": digits},
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        )
        soup_text = r.text
        # 正規表現でタイトル抽出（BeautifulSoup不要）
        m = re.search(r'class="[^"]*p-searchList__itemTitle[^"]*"[^>]*>([^<]+)<', soup_text)
        if m:
            return m.group(1).strip()
        m = re.search(r'class="[^"]*p-infoCard__name[^"]*"[^>]*>([^<]+)<', soup_text)
        if m:
            return m.group(1).strip()
    except Exception as e:
        print(f"  [ERR] {digits}: {e}")
    return None

session = requests.Session()
mismatches = []
checked = 0
found = 0

for db_id, company, phone, lp_url in rows:
    if not company or not phone:
        continue
    digits = re.sub(r"\D", "", phone)
    if not digits.startswith("0") or len(digits) < 10:
        continue

    itp_name = itp_reverse(digits, session)
    checked += 1

    if itp_name:
        found += 1
        c1 = core_name(company)
        c2 = core_name(itp_name)
        if c1 not in c2 and c2 not in c1:
            mismatches.append((db_id, company, phone, itp_name))
            print(f"  [不一致] id={db_id}: 【{company}】→ itp:【{itp_name}】({phone})")

    if checked % 10 == 0:
        print(f"  {checked}/{len(rows)}件完了 (itp返答:{found}件, 不一致:{len(mismatches)}件)")

    time.sleep(0.8)

print(f"\n=== 完了: {checked}件チェック / itp返答:{found}件 / 不一致:{len(mismatches)}件 ===")
for db_id, c, p, itp in mismatches:
    print(f"  id={db_id}: 【{c}】| {p} → itp:【{itp}】")
