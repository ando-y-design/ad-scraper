# -*- coding: utf-8 -*-
"""Test the improved _JP_LEGAL_ENTITY_TRIM_RE logic."""
import sys
sys.path.insert(0, r'C:\Users\amdwt\ad_scraper')
from processors.company_finder import _normalize_name, _is_valid_company, _is_valid_company_labeled

errors = []


def chk(label, got, expected):
    if got != expected:
        errors.append(f'{label}: got {got!r}, expected {expected!r}')


# Cases where the trailing text is a BUSINESS NAME PART → should NOT trim
chk('arch+kk+name',
    _normalize_name('一級建築士事務所 株式会社 北条工務店'),
    '一級建築士事務所 株式会社 北条工務店')

chk('desc+kk+name',
    _normalize_name('住宅設計 有限会社 田中設計'),
    '住宅設計 有限会社 田中設計')

# Cases where trailing is address/description → SHOULD trim
chk('kk+address',
    _normalize_name('SUN マルシェ株式会社 東京都渋谷区'),
    'SUN マルシェ株式会社')

chk('kk+slogan',
    _normalize_name('SUN マルシェ株式会社 ホームページ制作サービス'),
    'SUN マルシェ株式会社')

# Standard cases unchanged
chk('maekab+address',
    _normalize_name('株式会社田中産業 大阪市中央区1-2'),
    '株式会社田中産業')

chk('atokab+address',
    _normalize_name('田中産業株式会社 サービスの紹介'),
    '田中産業株式会社')

chk('leading_desc',
    _normalize_name('内装工事の株式会社田中'),
    '株式会社田中')

chk('clinic+space+addr',
    _normalize_name('田中クリニック 大阪市北区1-2'),
    '田中クリニック')

# Validation
chk('valid: arch firm',
    _is_valid_company('一級建築士事務所 株式会社 北条工務店'),
    True)

chk('valid: sun marche',
    _is_valid_company('SUN マルシェ株式会社'),
    True)

if errors:
    print('FAILURES:')
    for e in errors:
        print(' ', e)
    sys.exit(1)
else:
    print('ALL TRIM TESTS PASSED')
