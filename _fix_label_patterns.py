from __future__ import annotations
# -*- coding: utf-8 -*-
"""Add missing company labels to _COMPANY_LABEL_PATTERNS in company_finder.py."""
path = r'C:\Users\amdwt\ad_scraper\processors\company_finder.py'
with open(path, encoding='utf-8') as f:
    content = f.read()

# The patterns are on lines starting with r'(?:販売業者|...
# We need to add: 法人の名称|ストア名|運営者|発行者|管理者|相手方|事業者等
# to both pattern 0 (inline colon) and pattern 1 (newline variant)

ADDITIONS = '|法人の名称|ストア名|運営者|発行者|管理者|相手方|事業者等'

# Pattern 0: ends with |受注者)\s*[：:：]
old0 = r'|申込先会社|注文先|受注者)\s*[：:：]\s*([^\n：:]{2,80})'
new0 = r'|申込先会社|注文先|受注者' + ADDITIONS + r')\s*[：:：]\s*([^\n：:]{2,80})'

# Pattern 1: ends with |受注者)\s*[：:]?\s*\n
# Note: this line uses actual ： character (U+FF1A), not ： escape
old1_search = '|申込先会社|注文先|受注者)'
# We need to distinguish the two occurrences. Pattern 1 has \s*[：:]?\s*\n
# Pattern 0 has \s*[：:：]\s* (note: ： is the escape sequence)

count0 = content.count(old0)
print(f'Pattern 0 occurrences: {count0}')

if count0 == 1:
    content = content.replace(old0, new0, 1)
    print(f'Pattern 0 replaced OK')
else:
    print(f'ERROR: Pattern 0 not found exactly once')

# Pattern 1 replacement - find the second occurrence of |申込先会社|注文先|受注者)
# by looking at the context: pattern 1 has \s*[：:]?\s*\n after the closing paren
old1 = '|申込先会社|注文先|受注者)\\s*[：:]?\\s*\\n'
new1 = '|申込先会社|注文先|受注者' + ADDITIONS + ')\\s*[：:]?\\s*\\n'
count1 = content.count(old1)
print(f'Pattern 1 occurrences: {count1}')

if count1 == 1:
    content = content.replace(old1, new1, 1)
    print(f'Pattern 1 replaced OK')
else:
    print(f'ERROR: Pattern 1 not found exactly once')
    # Debug: show what we have
    idx = content.find('申込先会社|注文先|受注者)')
    if idx >= 0:
        print(f'Context around pattern: {repr(content[idx:idx+60])}')

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print('Done writing file')
