# -*- coding: utf-8 -*-
"""Test new _normalize_name features: decorator strip, 本社 strip, trailing annotation strip."""
import sys
sys.path.insert(0, r'C:\Users\amdwt\ad_scraper')
from processors.company_finder import _normalize_name, _is_valid_company, _is_valid_company_labeled

errors = []


def chk(label, got, expected):
    if got != expected:
        errors.append(f'{label}: got {got!r}, expected {expected!r}')


# ─── Decorator strip ───
chk('※ leading',
    _normalize_name('※株式会社田中産業'),
    '株式会社田中産業')

chk('▶ leading',
    _normalize_name('▶ 株式会社田中産業'),
    '株式会社田中産業')

chk('■ leading',
    _normalize_name('■田中クリニック'),
    '田中クリニック')

chk('【 leading bracket',
    _normalize_name('【株式会社田中産業】'),
    '株式会社田中産業')

# ─── 本社/支社 bracket strip ───
chk('東京本社 strip',
    _normalize_name('株式会社田中産業（東京本社）'),
    '株式会社田中産業')

chk('支店 strip',
    _normalize_name('田中建設株式会社（大阪支店）'),
    '田中建設株式会社')

# ─── Trailing annotation strip ───
chk('フランチャイズ',
    _normalize_name('株式会社田中（フランチャイズ加盟店）'),
    '株式会社田中')

chk('仮称',
    _normalize_name('田中建設株式会社（仮称）'),
    '田中建設株式会社')

chk('東京支社',
    _normalize_name('田中株式会社（東京支社）'),
    '田中株式会社')

chk('double annotation',
    _normalize_name('田中株式会社（東京支社）（フランチャイズ）'),
    '田中株式会社')

# ─── Validate after normalization ───
chk('valid after franchise strip',
    _is_valid_company('株式会社田中（フランチャイズ加盟店）'),
    True)

chk('valid after decorator strip',
    _is_valid_company('※株式会社田中産業'),
    True)

chk('valid after 本社 strip',
    _is_valid_company('株式会社田中産業（東京本社）'),
    True)

# ─── Make sure legal entity parens NOT stripped ───
# (株式会社サンテク) → 後続の _PAREN_COMPANY_RE が処理する → 正しく抽出
chk('paren company preserved',
    _normalize_name('サンテク(株式会社山陽テクノサービス)'),
    '株式会社山陽テクノサービス')

if errors:
    print('FAILURES:')
    for e in errors:
        print(' ', e)
    sys.exit(1)
else:
    print('ALL NORMALIZE NEW TESTS PASSED')
