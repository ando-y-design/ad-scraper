# -*- coding: utf-8 -*-
"""Regression suite — covers core behaviors that must not break."""
import sys
sys.path.insert(0, r'C:\Users\amdwt\ad_scraper')

from processors.company_finder import (
    _normalize_name, _is_valid_company, _is_valid_company_labeled,
    _extract_from_json_ld, _extract_pair_from_containers,
    _extract_company_from_soup, _extract_phones_from_soup,
)
from processors.phone_finder import extract_phone, extract_all_phones
from bs4 import BeautifulSoup

errors = []


def chk(label, got, expected):
    if got != expected:
        errors.append(f'{label}: got {got!r}, expected {expected!r}')


# ───────────────────────────────────────────────
# normalize_name
# ───────────────────────────────────────────────
chk('norm: 前株+説明', _normalize_name('株式会社田中産業 大阪市中央区1-2'), '株式会社田中産業')
chk('norm: 後株+説明', _normalize_name('田中産業株式会社 サービスの紹介'), '田中産業株式会社')
chk('norm: leading desc', _normalize_name('内装工事の株式会社田中'), '株式会社田中')
chk('norm: paren 前株', _normalize_name('サンテク(株式会社山陽テクノサービス)'), '株式会社山陽テクノサービス')
chk('norm: ©除去', _normalize_name('©2024 株式会社サンプル'), '株式会社サンプル')
chk('norm: スペース付き後株', _normalize_name('SUN マルシェ株式会社 東京都'), 'SUN マルシェ株式会社')
chk('norm: クリニック+住所', _normalize_name('田中クリニック 大阪市北区1-2'), '田中クリニック')
# 前株+スペース+旧社名: LEADING_DESC_RE を前株に適用しないことを確認
chk('norm: 前株(旧) ASCII', _normalize_name('株式会社田中産業(旧: 株式会社大田中)'), '株式会社田中産業')
chk('norm: 前株（旧） full', _normalize_name('株式会社田中産業（旧: 株式会社大田中）'), '株式会社田中産業')

# ───────────────────────────────────────────────
# _is_valid_company
# ───────────────────────────────────────────────
chk('valid: 株式会社', _is_valid_company('株式会社田中産業'), True)
chk('valid: 合同会社', _is_valid_company('合同会社テスト'), True)
chk('valid: 旧社名あり ASCII', _is_valid_company('株式会社田中産業(旧: 株式会社大田中)'), True)   # trim → 株式会社田中産業
chk('valid: 旧社名あり full', _is_valid_company('株式会社田中産業（旧: 株式会社大田中）'), True)   # trim → 株式会社田中産業
chk('valid: 以下弊社', _is_valid_company('株式会社田中産業(以下、弊社)'), True)                   # trim → 株式会社田中産業
chk('valid: セパレータ', _is_valid_company('株式会社田中産業 | サービス名'), True)  # normalize trims after |
chk('valid: 法人格なし', _is_valid_company('田中産業'), False)
chk('valid: スローガン', _is_valid_company('株式会社田中のサービス一覧'), False)

# ───────────────────────────────────────────────
# _is_valid_company_labeled
# ───────────────────────────────────────────────
chk('labeled: クリニック', _is_valid_company_labeled('田中クリニック'), True)
chk('labeled: 法律事務所', _is_valid_company_labeled('山田法律事務所'), True)
chk('labeled: 業種のみ', _is_valid_company_labeled('不動産'), False)
chk('labeled: 説明文', _is_valid_company_labeled('不動産仲介について'), False)
chk('labeled: 旧社名 ASCII', _is_valid_company_labeled('株式会社田中(旧: 田中産業)'), True)   # trim → 株式会社田中

# ───────────────────────────────────────────────
# JSON-LD extraction
# ───────────────────────────────────────────────
html_ld = '''<html><head>
<script type="application/ld+json">{"@type":"Organization","legalName":"株式会社田中産業","telephone":"03-5555-1234"}</script>
</head><body></body></html>'''
c_ld, p_ld = _extract_from_json_ld(BeautifulSoup(html_ld, 'lxml'))
chk('ld: company', c_ld, '株式会社田中産業')
chk('ld: phone', p_ld, '03-5555-1234')

# JSON-LD list telephone
html_ldt = '''<html><head>
<script type="application/ld+json">{"@type":"LocalBusiness","name":"有限会社山田","telephone":["06-1111-2222","06-3333-4444"]}</script>
</head><body></body></html>'''
c_ldt, p_ldt = _extract_from_json_ld(BeautifulSoup(html_ldt, 'lxml'))
chk('ld_list_tel: company', c_ldt, '有限会社山田')
chk('ld_list_tel: phone', p_ldt, '06-1111-2222')

# JSON-LD @graph
html_graph = '''<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
  {"@type":"WebPage","name":"サービスページ"},
  {"@type":"Organization","legalName":"株式会社グラフ企業","telephone":"052-111-2222"}
]}</script>
</head><body></body></html>'''
c_gr, p_gr = _extract_from_json_ld(BeautifulSoup(html_graph, 'lxml'))
chk('ld_graph: company', c_gr, '株式会社グラフ企業')
chk('ld_graph: phone', p_gr, '052-111-2222')

# ───────────────────────────────────────────────
# Container pair extraction
# ───────────────────────────────────────────────
html_table_pair = '''<html><body>
<table>
  <tr><th>運営会社</th><td>株式会社テーブル企業</td></tr>
  <tr><th>TEL</th><td>078-333-4444</td></tr>
</table>
</body></html>'''
c_tb, p_tb = _extract_pair_from_containers(BeautifulSoup(html_table_pair, 'lxml'))
chk('table_pair: company', c_tb, '株式会社テーブル企業')
chk('table_pair: phone', p_tb, '078-333-4444')

html_section_pair = '''<html><body>
<section>
  <dl>
    <dt>販売業者</dt><dd>合同会社セクション</dd>
    <dt>電話番号</dt><dd>092-555-6666</dd>
  </dl>
</section>
</body></html>'''
c_sc, p_sc = _extract_pair_from_containers(BeautifulSoup(html_section_pair, 'lxml'))
chk('section_pair: company', c_sc, '合同会社セクション')
chk('section_pair: phone', p_sc, '092-555-6666')

# ───────────────────────────────────────────────
# Phone extraction
# ───────────────────────────────────────────────
chk('phone: tel_link', extract_phone('tel:+81-3-5555-1234'), '03-5555-1234')
chk('phone: full-width', extract_phone('０３－５５５５－１２３４'), '03-5555-1234')
chk('phone: intl fmt', extract_phone('+81-3-5555-1234'), '03-5555-1234')
chk('phone: placeholder filtered', extract_phone('03-1234-5678'), None)
chk('phone: 携帯', extract_phone('090-1234-5678'), '090-1234-5678')
chk('phone: フリーダイヤル', extract_phone('0120-123-456'), '0120-123-456')

# ───────────────────────────────────────────────
# Report
# ───────────────────────────────────────────────
if errors:
    print('FAILURES:')
    for e in errors:
        print(' ', e)
    sys.exit(1)
else:
    print(f'ALL {30} REGRESSION TESTS PASSED')
