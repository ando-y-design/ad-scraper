# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r'C:\Users\amdwt\ad_scraper')

from processors.company_finder import (
    _is_valid_company, _is_valid_company_labeled, _is_lp_builder_domain,
    _extract_from_json_ld, _extract_pair_from_containers,
    _extract_company_from_divs,
)
from bs4 import BeautifulSoup

errors = []

# --- Test 1: Paren trim + validation ---
# After trim, core company is extracted so _is_valid_company returns True.
cases_valid = [
    ('株式会社田中産業', True),
    ('株式会社田中産業(旧: 株式会社大田中)', True),    # normalized to 株式会社田中産業
    ('株式会社田中産業(以下、弊社)', True),             # normalized to 株式会社田中産業
    ('株式会社田中産業（旧: 株式会社大田中）', True),   # normalized to 株式会社田中産業
    ('株式会社田中産業（以下、弊社）', True),           # normalized to 株式会社田中産業
]
for name, expected in cases_valid:
    got = _is_valid_company(name)
    if got != expected:
        errors.append(f'_is_valid_company({name!r}) = {got}, expected {expected}')

# Also check _is_valid_company_labeled
for name, expected in cases_valid:
    got = _is_valid_company_labeled(name)
    if got != expected:
        errors.append(f'_is_valid_company_labeled({name!r}) = {got}, expected {expected}')

# --- Test 2: LP builder domain detection ---
lp_tests = [
    ('https://example.amebaownd.com', True),
    ('https://abc.jimdofree.com', True),
    ('https://page.line.me/abcde', True),
    ('https://example.fc2.com', True),
    ('https://example.co.jp', False),
    ('https://example.peraichi.com', True),
    ('https://user.wixsite.com/mysite', True),
]
for url, expected in lp_tests:
    got = _is_lp_builder_domain(url)
    if got != expected:
        errors.append(f'_is_lp_builder_domain({url!r}) = {got}, expected {expected}')

# --- Test 3: JSON-LD list telephone ---
html_list_tel = '''<html><head>
<script type="application/ld+json">{"@type":"Organization","name":"株式会社テスト","telephone":["03-1111-2222","03-3333-4444"]}</script>
</head><body></body></html>'''
soup_lt = BeautifulSoup(html_list_tel, 'lxml')
c_lt, p_lt = _extract_from_json_ld(soup_lt)
if c_lt != '株式会社テスト':
    errors.append(f'list_tel company: got {c_lt!r}')
if p_lt != '03-1111-2222':
    errors.append(f'list_tel phone: got {p_lt!r}')

# --- Test 4: JSON-LD list name ---
html_list_name = '''<html><head>
<script type="application/ld+json">{"@type":"Organization","name":["株式会社テスト社","Test Corp"],"telephone":"06-2222-3333"}</script>
</head><body></body></html>'''
soup_ln = BeautifulSoup(html_list_name, 'lxml')
c_ln, p_ln = _extract_from_json_ld(soup_ln)
if c_ln != '株式会社テスト社':
    errors.append(f'list_name company: got {c_ln!r}')
if p_ln != '06-2222-3333':
    errors.append(f'list_name phone: got {p_ln!r}')

# --- Test 5: _extract_pair_from_containers uses section before huge wrapper div ---
html_pair = '''<html><body>
<div id="wrapper">
  <section id="tokutei">
    <table>
      <tr><th>会社名</th><td>株式会社正解テスト</td></tr>
      <tr><th>電話番号</th><td>03-5678-9012</td></tr>
    </table>
  </section>
</div>
</body></html>'''
soup_pair = BeautifulSoup(html_pair, 'lxml')
c_pair, p_pair = _extract_pair_from_containers(soup_pair)
if c_pair != '株式会社正解テスト':
    errors.append(f'container_pair company: got {c_pair!r}')
if p_pair != '03-5678-9012':
    errors.append(f'container_pair phone: got {p_pair!r}')

# --- Test 6: nav anchor exclusion in _extract_company_from_divs ---
html_nav = '''<html><body>
<nav><div><a href="/company">会社名: 株式会社ナビ</a></div></nav>
<div><div>会社名</div><div>株式会社正解</div></div>
</body></html>'''
soup_nav = BeautifulSoup(html_nav, 'lxml')
res_nav = _extract_company_from_divs(soup_nav)
# Should return 株式会社正解 (nav div skipped because it contains <a>)
if res_nav != '株式会社正解':
    errors.append(f'nav_exclusion: got {res_nav!r}, expected 株式会社正解')

# --- Test 7: div_candidates sorted smallest-first ---
# Wrapper div contains both labels but inner div has the clean pair
html_small_div = '''<html><body>
<div id="big">
  <p>このサービスは不動産会社です。お電話: 09-9999-9999</p>
  <div id="small">
    <table>
      <tr><th>運営会社</th><td>合同会社正解社</td></tr>
      <tr><th>電話番号</th><td>06-4444-5555</td></tr>
    </table>
  </div>
</div>
</body></html>'''
soup_small = BeautifulSoup(html_small_div, 'lxml')
c_small, p_small = _extract_pair_from_containers(soup_small)
if c_small != '合同会社正解社':
    errors.append(f'small_div_first company: got {c_small!r}')
if p_small != '06-4444-5555':
    errors.append(f'small_div_first phone: got {p_small!r}')

# --- Report ---
if errors:
    print('FAILURES:')
    for e in errors:
        print(' ', e)
    sys.exit(1)
else:
    print('ALL TESTS PASSED')
