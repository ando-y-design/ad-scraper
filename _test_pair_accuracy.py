# -*- coding: utf-8 -*-
"""Tests for pair-accuracy improvements:
  - _pick_phone_nearest_company: formatted phone search
  - _is_address_part: 丁目/番地/番町 patterns
  - _COMPANY_LABEL_PATTERNS: new labels
  - _extract_pair_from_text: unlabeled phone scan
"""
import sys
sys.path.insert(0, r'C:\Users\amdwt\ad_scraper')
from processors.company_finder import (
    _normalize_name,
    _is_valid_company,
    _is_valid_company_labeled,
    _pick_phone_nearest_company,
    _extract_pair_from_text,
    _extract_company_from_text,
)

errors = []

def chk(label, got, expected):
    if got != expected:
        errors.append(f'{label}: got {got!r}, expected {expected!r}')

# ─── _pick_phone_nearest_company: uses formatted phone search ───────────────
# Company name is near phone A in the text, phone B appears far away
text_proximity = (
    "株式会社田中産業\n"
    "販売業者\n"
    "TEL 03-3333-4444\n"   # phone A — near company
    "-----\n" * 50 +        # spacer
    "フリーダイヤル 0120-999-888\n"  # phone B — far away
)
result = _pick_phone_nearest_company(
    text_proximity,
    '株式会社田中産業',
    ['03-3333-4444', '0120-999-888'],
)
chk('proximity: formatted phone finds nearest', result, '03-3333-4444')

# Both phones are direct, but phone B is nearer company
text_two_direct = (
    "090-9999-8888\n" * 5 +   # phone A far from company
    "株式会社山田建設\n"
    "06-9876-5432\n"           # phone B right next to company
)
result2 = _pick_phone_nearest_company(
    text_two_direct,
    '株式会社山田建設',
    ['090-9999-8888', '06-9876-5432'],
)
chk('proximity: phone B nearer company wins', result2, '06-9876-5432')

# Single phone returns as-is
chk('proximity: single phone', _pick_phone_nearest_company("x", "株式会社A", ["03-3333-4444"]), "03-3333-4444")

# Empty phones returns None
chk('proximity: empty list', _pick_phone_nearest_company("x", "株式会社A", []), None)

# ─── _is_address_part via _normalize_name ─────────────────────────────────
# 丁目 should be trimmed as address
chk('丁目 trim',
    _normalize_name('株式会社 田中 千代田区丸の内1丁目2番地'),
    '株式会社 田中')

chk('番地 trim',
    _normalize_name('合同会社 フィールド 大阪市北区梅田1番地'),
    '合同会社 フィールド')

chk('番町 trim',
    _normalize_name('株式会社 スプリング 千代田区五番町'),
    '株式会社 スプリング')

# ─── New labels in _COMPANY_LABEL_PATTERNS ────────────────────────────────
# 法人の名称
text_hougin = "法人の名称：一般社団法人田中協会"
c = _extract_company_from_text(text_hougin)
chk('法人の名称 in pattern', c, '一般社団法人田中協会')

# ストア名
text_store = "ストア名：田中美容室"
c2 = _extract_company_from_text(text_store)
chk('ストア名 in pattern', c2, '田中美容室')

# 運営者
text_operator = "運営者：株式会社田中商事"
c3 = _extract_company_from_text(text_operator)
chk('運営者 in pattern', c3, '株式会社田中商事')

# 管理者 (newline variant)
text_mgr = "管理者\n有限会社田中事務"
c4 = _extract_company_from_text(text_mgr)
chk('管理者 newline pattern', c4, '有限会社田中事務')

# ─── _extract_pair_from_text: unlabeled phone near company label ─────────
# Format: 会社ラベル → 社名 → 住所 → 電話番号（ラベルなし）
text_unlabeled_phone = "\n".join([
    "特定商取引法に基づく表記",
    "販売業者",
    "株式会社田中産業",
    "〒100-0001 東京都千代田区丸の内1-1",
    "03-3456-7890",   # unlabeled phone, 3 lines after label
])
company_u, phone_u = _extract_pair_from_text(text_unlabeled_phone)
chk('unlabeled phone scan: company found', company_u, '株式会社田中産業')
chk('unlabeled phone scan: phone found', phone_u, '03-3456-7890')

# 会社ラベル → 社名 → 電話番号（ラベルなし、直後行）
text_phone_direct = "\n".join([
    "事業者名：株式会社山田産業",
    "06-4444-5555",
])
company_d, phone_d = _extract_pair_from_text(text_phone_direct)
chk('unlabeled phone direct: company', company_d, '株式会社山田産業')
chk('unlabeled phone direct: phone', phone_d, '06-4444-5555')

# Phone label still works normally
text_labeled = "\n".join([
    "運営会社：合同会社テスト商事",
    "電話番号：050-5555-6666",
])
company_l, phone_l = _extract_pair_from_text(text_labeled)
chk('labeled phone still works: company', company_l, '合同会社テスト商事')
chk('labeled phone still works: phone', phone_l, '050-5555-6666')

# ─── Summary ────────────────────────────────────────────────────────────────
if errors:
    print('FAILURES:')
    for e in errors:
        print(' ', e)
    sys.exit(1)
else:
    print('ALL PAIR ACCURACY TESTS PASSED')
