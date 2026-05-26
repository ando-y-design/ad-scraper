# -*- coding: utf-8 -*-
"""Test all new features added in this session."""
import sys
sys.path.insert(0, r'C:\Users\amdwt\ad_scraper')
from processors.company_finder import _normalize_name, _is_valid_company, _is_valid_company_labeled

errors = []

def chk(label, got, expected):
    if got != expected:
        errors.append(f'{label}: got {got!r}, expected {expected!r}')


# ─── New legal entity types ───
chk('行政書士法人',
    _is_valid_company('行政書士法人田中事務所'),
    True)

chk('監査法人',
    _is_valid_company('監査法人田中会計'),
    True)

chk('弁理士法人',
    _is_valid_company('弁理士法人田中特許'),
    True)

chk('社会保険労務士法人',
    _is_valid_company('社会保険労務士法人田中事務所'),
    True)

chk('独立行政法人',
    _is_valid_company('独立行政法人国立科学博物館'),
    True)

chk('国立大学法人',
    _is_valid_company('国立大学法人東京大学'),
    True)

# ─── Year prefix strip after © ───
chk('©2024 company',
    _normalize_name('©2024 株式会社田中産業'),
    '株式会社田中産業')

chk('2024 company',
    _normalize_name('2024 株式会社田中産業'),
    '株式会社田中産業')

chk('2020-2024 company',
    _normalize_name('2020-2024 株式会社田中産業'),
    '株式会社田中産業')

# ─── Front-legal-entity + address trim ───
chk('前株+マルシェ+区番地',
    _normalize_name('株式会社 SUN マルシェ 渋谷区1-1'),
    '株式会社 SUN マルシェ')

chk('前株+名前+大阪市番地',
    _normalize_name('株式会社 田中 大阪市北区1-2-3'),
    '株式会社 田中')

chk('前株+名前+郵便番号',
    _normalize_name('株式会社 千成屋 〒123-4567 東京都渋谷区'),
    '株式会社 千成屋')

chk('前株+多語+府県トリム',
    _normalize_name('合同会社 フィールド 神奈川県横浜市'),
    '合同会社 フィールド')

# ─── Business type expansion ───
chk('美容室 valid',
    _is_valid_company_labeled('田中美容室'),
    True)

chk('ヘアサロン valid',
    _is_valid_company_labeled('田中ヘアサロン'),
    True)

chk('工房 valid',
    _is_valid_company_labeled('田中工房'),
    True)

chk('スタジオ valid',
    _is_valid_company_labeled('田中スタジオ'),
    True)

chk('アカデミー valid',
    _is_valid_company_labeled('田中アカデミー'),
    True)

# ─── Inline label-value in divs ───
# _LEADING_DESC_RE strips the label prefix before legal entity
chk('inline div label strip',
    _normalize_name('販売業者: 株式会社田中産業'),
    '株式会社田中産業')  # _LEADING_DESC_RE strips the label prefix

# ─── Normalize should NOT strip valid multi-word names ───
chk('前株multi word preserved',
    _normalize_name('株式会社 SUN マルシェ'),
    '株式会社 SUN マルシェ')

chk('独立行政法人 full name preserved',
    _normalize_name('独立行政法人 国立科学博物館'),
    '独立行政法人 国立科学博物館')


if errors:
    print('FAILURES:')
    for e in errors:
        print(' ', e)
    sys.exit(1)
else:
    print('ALL NEW FEATURE TESTS PASSED')
