# -*- coding: utf-8 -*-
"""Test the three accuracy gap fixes:
 1. _BUSINESS_TYPE_TRIM_RE - missing business types
 2. _is_name_part - 区/市 ending address detection
 3. ホールディングス in trim patterns
"""
import sys
sys.path.insert(0, r'C:\Users\amdwt\ad_scraper')
from processors.company_finder import _normalize_name, _is_valid_company, _is_valid_company_labeled

errors = []


def chk(label, got, expected):
    if got != expected:
        errors.append(f'{label}: got {got!r}, expected {expected!r}')


# ─── Gap 1: _BUSINESS_TYPE_TRIM_RE extended ───

chk('不動産+address',
    _normalize_name('田中不動産 大阪市中央区1-2'),
    '田中不動産')

chk('学院+address',
    _normalize_name('田中学院 東京都新宿区1-2-3'),
    '田中学院')

chk('塾+address',
    _normalize_name('山田塾 横浜市中区2-3'),
    '山田塾')

chk('サロン+address',
    _normalize_name('ビューティーサロン 渋谷区1-1'),
    'ビューティーサロン')

chk('ホテル+address',
    _normalize_name('田中ホテル 京都市東山区'),
    '田中ホテル')

chk('商会+address',
    _normalize_name('山田商会 福岡市博多区1-2'),
    '山田商会')

chk('スクール+slogan',
    _normalize_name('田中スクール 初心者歓迎'),
    '田中スクール')

# ─── Gap 2: 区/市 ending in trailing text = address ───

# "田中建設 株式会社 渋谷区" → trailing "渋谷区" ends with 区 → trim
chk('kk+space+区',
    _normalize_name('田中建設 株式会社 渋谷区'),
    '田中建設 株式会社')

# "田中工務店 有限会社 大和市" → trailing "大和市" ends with 市 → trim
chk('kk+space+市',
    _normalize_name('田中工務店 有限会社 大和市'),
    '田中工務店 有限会社')

# "一級建築士事務所 株式会社 北条工務店" → trailing "北条工務店" ends with 工務店
# → _BUSINESS_TYPE_RE matches → _is_name_part=True → NOT trimmed
chk('arch+kk+name_still_preserved',
    _normalize_name('一級建築士事務所 株式会社 北条工務店'),
    '一級建築士事務所 株式会社 北条工務店')

# ─── Gap 3: ホールディングス in trim patterns ───

# _ATOKAB_TRIM_RE: "田中ホールディングス サービス一覧" → trim
chk('holdings+slogan',
    _normalize_name('田中ホールディングス サービス一覧'),
    '田中ホールディングス')

# _JP_LEGAL_ENTITY_TRIM_RE: "ABC 田中ホールディングス 東京都港区" → trim
chk('ABC+holdings+address',
    _normalize_name('ABC 田中ホールディングス 東京都港区'),
    'ABC 田中ホールディングス')

# Valid company check
chk('valid: ホールディングス', _is_valid_company('田中ホールディングス'), True)

# ─── Validation ───
if errors:
    print('FAILURES:')
    for e in errors:
        print(' ', e)
    sys.exit(1)
else:
    print('ALL GAP FIX TESTS PASSED')
