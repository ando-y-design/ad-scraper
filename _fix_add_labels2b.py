from __future__ import annotations
# -*- coding: utf-8 -*-
"""Fix Pattern 0 only (uses ： literal) for _COMPANY_LABEL_PATTERNS."""
path = r'C:\Users\amdwt\ad_scraper\processors\company_finder.py'
with open(path, encoding='utf-8') as f:
    content = f.read()

ADDITIONS = '|取扱業者|取扱事業者|サービス提供元|サービス責任者'

# Pattern 0 is unique: ends with |相手方|事業者等)\s*[：:：]
# (the ： distinguishes it from _INLINE_LABEL_COMPANY_RE which uses [：:：])
old0 = '|相手方|事業者等)\\s*[：:\\uff1a]'
new0 = '|相手方|事業者等' + ADDITIONS + ')\\s*[：:\\uff1a]'
count0 = content.count(old0)
print(f'Pattern 0 match count: {count0}')
if count0 == 1:
    content = content.replace(old0, new0, 1)
    print('Pattern 0 OK')
    # Verify
    print(f'Verify: {repr(content[content.find(new0):content.find(new0)+80])}')
else:
    print('ERROR: Pattern 0 not found exactly once')
    idx = 0
    while True:
        idx = content.find('|相手方|事業者等)', idx)
        if idx < 0:
            break
        print(f'  at {idx}: {repr(content[idx:idx+50])}')
        idx += 1

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print('Done')
