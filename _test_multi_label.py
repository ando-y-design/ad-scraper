# -*- coding: utf-8 -*-
"""Tests for multi-label pair selection in _extract_pair_from_text."""
import sys
sys.path.insert(0, r'C:\Users\amdwt\ad_scraper')
from processors.company_finder import _extract_pair_from_text

errors = []

def chk(label, got, expected):
    if got != expected:
        errors.append(f'{label}: got {got!r}, expected {expected!r}')

# ─── 複数会社ラベルがある場合：電話に近い会社を選ぶ ───────────────────────
# 会社Aは電話から遠い（10行差）、会社Bは電話から近い（1行差）→ 会社Bを選ぶべき
text_multi = "\n".join([
    "事業者名：株式会社第一商事",    # line 0: company A
    "〒100-0001 東京都",            # line 1
    "千代田区",                     # line 2
    "丸の内1丁目",                  # line 3
    "ビル2階",                      # line 4
    "---",                          # line 5
    "---",                          # line 6
    "---",                          # line 7
    "---",                          # line 8
    "運営会社：株式会社近接産業",    # line 9: company B (close to phone)
    "TEL：06-9988-7766",            # line 10: phone (1 line from company B)
])
c, p = _extract_pair_from_text(text_multi)
chk('multi-label: picks company nearest phone', c, '株式会社近接産業')
chk('multi-label: phone correct', p, '06-9988-7766')

# 会社Aが電話に近い場合（会社Aを選ぶ）
text_multi2 = "\n".join([
    "販売業者：株式会社最初産業",   # line 0: company A (close to phone)
    "TEL：03-7777-8888",           # line 1: phone (1 line from company A)
    "---",                         # line 2
    "---",                         # line 3
    "---",                         # line 4
    "---",                         # line 5
    "---",                         # line 6
    "事業者名：株式会社後半商事",   # line 7: company B (far from phone)
])
c2, p2 = _extract_pair_from_text(text_multi2)
chk('multi-label: picks first when first is nearest', c2, '株式会社最初産業')
chk('multi-label: phone correct 2', p2, '03-7777-8888')

# 取扱業者ラベル（新しく追加したラベル）
text_toriatsukae = "\n".join([
    "取扱業者：株式会社トリアツ商会",
    "TEL：050-3456-7890",
])
c3, p3 = _extract_pair_from_text(text_toriatsukae)
chk('取扱業者 label: company', c3, '株式会社トリアツ商会')
chk('取扱業者 label: phone', p3, '050-3456-7890')

# サービス提供元ラベル
text_service = "\n".join([
    "サービス提供元：合同会社サービス提供",
    "電話番号：03-2233-4455",
])
c4, p4 = _extract_pair_from_text(text_service)
chk('サービス提供元 label: company', c4, '合同会社サービス提供')
chk('サービス提供元 label: phone', p4, '03-2233-4455')

# ─── Summary ─────────────────────────────────────────────────────────────────
if errors:
    print('FAILURES:')
    for e in errors:
        print(' ', e)
    sys.exit(1)
else:
    print('ALL MULTI-LABEL TESTS PASSED')
