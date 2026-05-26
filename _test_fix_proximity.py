# -*- coding: utf-8 -*-
"""Tests for proximity / container fixes:
  Fix A: company_line_idx points to value line (i+1) when label/value on separate lines
  Fix B: _has_phone_number fallback lets divs with unlabeled phones be included
"""
import sys
sys.path.insert(0, r'C:\Users\amdwt\ad_scraper')
from processors.company_finder import (
    _extract_pair_from_text,
    _extract_pair_from_containers,
)
from bs4 import BeautifulSoup

errors = []

def chk(label, got, expected):
    if got != expected:
        errors.append(f'{label}: got {got!r}, expected {expected!r}')

# ─── Fix A: company_line_idx = i+1 when label and value are on separate lines ─
# Phone is 5 lines from the company NAME (line 1), which is 6 lines from the label (line 0).
# Old code: abs(0 - 6) = 6 > 5 → NOT paired
# New code: abs(1 - 6) = 5 ≤ 5 → paired ✓
text_a1 = "\n".join([
    "販売業者",               # line 0: label
    "株式会社サンプル商事",    # line 1: company value (company_line_idx should be 1)
    "〒100-0001 東京都",      # line 2
    "千代田区丸の内1丁目",    # line 3
    "ビル3階201号室",         # line 4
    "担当: 田中太郎",         # line 5
    "TEL：03-5555-6666",     # line 6: 5 lines from company name → should pair
])
c_a1, p_a1 = _extract_pair_from_text(text_a1)
chk('Fix A: company_line_idx i+1 — company found', c_a1, '株式会社サンプル商事')
chk('Fix A: company_line_idx i+1 — phone found', p_a1, '03-5555-6666')

# When label and value are on the SAME line, company_line_idx = i (unchanged behaviour)
text_a2 = "\n".join([
    "販売業者：株式会社同行テスト",  # line 0: same-line label+value → company_line_idx=0
    "〒100-0001 東京都千代田区",   # line 1
    "丸の内1丁目",                 # line 2
    "ビル3階",                     # line 3
    "担当: 鈴木",                  # line 4
    "TEL：06-7777-8888",          # line 5: 5 lines from line 0 → should pair
])
c_a2, p_a2 = _extract_pair_from_text(text_a2)
chk('Fix A: same-line (i+0) — company found', c_a2, '株式会社同行テスト')
chk('Fix A: same-line (i+0) — phone found', p_a2, '06-7777-8888')

# Unlabeled phone scan: phone on line 6 after company name on line 1
# scan_end = min(company_line_idx + 6, n) = min(1+6, 8) = 7 → scans lines 2-6
text_a3 = "\n".join([
    "販売業者",               # line 0: label
    "株式会社スキャン産業",    # line 1: company (company_line_idx=1)
    "〒200-0001 東京都",      # line 2
    "豊島区池袋1丁目",        # line 3
    "東池袋ビル4階",           # line 4
    "営業時間 9:00-18:00",    # line 5
    "090-1234-5678",          # line 6: unlabeled phone — 5 lines after company name
])
c_a3, p_a3 = _extract_pair_from_text(text_a3)
chk('Fix A: unlabeled scan extended — company', c_a3, '株式会社スキャン産業')
chk('Fix A: unlabeled scan extended — phone', p_a3, '090-1234-5678')

# ─── Fix B: _has_phone_number — div with unlabeled phone ─────────────────────
# <div class="company-info">販売業者: 株式会社○○<br>03-xxxx-xxxx</div>
# No TEL/電話番号 label → old code excluded this div
html_b1 = """
<html><body>
<div class="company-info">
  <div class="label">販売業者</div>
  <div class="value">株式会社タナカ産業</div>
  <div class="phone">03-4444-5555</div>
</div>
</body></html>
"""
soup_b1 = BeautifulSoup(html_b1, 'html.parser')
c_b1, p_b1 = _extract_pair_from_containers(soup_b1)
chk('Fix B: unlabeled phone in div — company', c_b1, '株式会社タナカ産業')
chk('Fix B: unlabeled phone in div — phone', p_b1, '03-4444-5555')

# Labeled phone still works (regression check)
html_b2 = """
<html><body>
<div>
  <div>販売業者</div>
  <div>株式会社ラベル商事</div>
  <div>電話番号</div>
  <div>06-2222-3333</div>
</div>
</body></html>
"""
soup_b2 = BeautifulSoup(html_b2, 'html.parser')
c_b2, p_b2 = _extract_pair_from_containers(soup_b2)
chk('Fix B: labeled phone still works — company', c_b2, '株式会社ラベル商事')
chk('Fix B: labeled phone still works — phone', p_b2, '06-2222-3333')

# ─── Summary ─────────────────────────────────────────────────────────────────
if errors:
    print('FAILURES:')
    for e in errors:
        print(' ', e)
    sys.exit(1)
else:
    print('ALL PROXIMITY/CONTAINER FIX TESTS PASSED')
