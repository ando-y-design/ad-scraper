# -*- coding: utf-8 -*-
import sys, re, sqlite3, requests, unicodedata
sys.stdout.reconfigure(encoding='utf-8')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'ja-JP,ja;q=0.9',
}

NG_CASES = [
    ('一般社団法人日本自動車購入協会(JPUC)', '0120-049-656', 'https://jpuc.or.jp/'),
    ('株式会社リーフ', '03-5969-8121', None),
    ('ソニー生命保険株式会社', '03-5290-6100', None),
    ('株式会社リクルートマネジメントソリューションズ', '0120-878-300', None),
    ('Gramn Inc.', '090-1239-1163', None),
    ('マンパワーグループ株式会社', '042-540-7561', None),
    ('株式会社カーネクスト', '06-7657-7808', None),
    ('PRONI株式会社', '06-6767-0883', None),
    ('株式会社ホームプロ', '0120-864-626', None),
    ('株式会社シュガーテイスト', '080-3517-0662', None),
    ('株式会社トレタ', '03-6431-9006', None),
    ('合同会社アクトリンク', '06-7650-2289', None),
]

conn = sqlite3.connect('companies.db')
conn.row_factory = sqlite3.Row

print("会社名 / 電話番号 / 分析")
print('=' * 90)

for company, phone, override_url in NG_CASES:
    # DBからlp_urlを取得
    row = conn.execute(
        "SELECT lp_url FROM companies WHERE company_name=? LIMIT 1", (company,)
    ).fetchone()
    lp_url = override_url or (row['lp_url'] if row else None)
    if not lp_url:
        print(f"{company}: URLなし → スキップ")
        continue

    from urllib.parse import urlparse
    origin = '{0}://{1}'.format(*urlparse(lp_url)[:2])
    digits = re.sub(r'\D', '', unicodedata.normalize('NFKC', phone))

    # より広範囲にチェック（script tags, data attrs, comments含む）
    found_in = []
    html_raw = ''
    for url in [lp_url, origin, origin+'/tokutei', origin+'/legal',
                origin+'/contact', origin+'/about', origin+'/company',
                origin+'/sitemap.xml']:
        try:
            r = requests.get(url, headers=HEADERS, timeout=8, allow_redirects=True)
            if r.status_code == 200:
                raw = r.text
                raw_digits = re.sub(r'\D', '', unicodedata.normalize('NFKC', raw))
                if digits in raw_digits:
                    found_in.append(url.replace(origin, ''))
                    if not html_raw:
                        html_raw = raw
        except Exception:
            continue

    if found_in:
        verdict = f"HTMLに存在 ({', '.join(found_in[:2])}) → JS描画問題（番号は正しい）"
    else:
        # 番号がどこにも見つからない → 実際に間違いの可能性
        verdict = "HTMLに存在しない → 番号が間違いの可能性あり"

    short_url = lp_url.replace('https://','').replace('http://','')[:40]
    print(f"{company}")
    print(f"  番号: {phone}  LP: {short_url}")
    print(f"  判定: {verdict}")
    print()

conn.close()
