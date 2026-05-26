# -*- coding: utf-8 -*-
"""Add 取扱業者|取扱事業者|サービス提供元|サービス責任者 to _COMPANY_LABEL_PATTERNS."""
path = r'C:\Users\amdwt\ad_scraper\processors\company_finder.py'
with open(path, encoding='utf-8') as f:
    content = f.read()

ADDITIONS = '|取扱業者|取扱事業者|サービス提供元|サービス責任者'

# Check current state
idx0 = content.find('相手方|事業者等')
count = content.count('相手方|事業者等')
print(f'Occurrences of 相手方|事業者等: {count}')
for i in range(count):
    pos = content.find('相手方|事業者等', 0 if i == 0 else pos + 1)
    print(f'  [{i}] at {pos}: {repr(content[pos:pos+60])}')

# Pattern 0: ends with |相手方|事業者等)\s*[：:：]
old0 = '|相手方|事業者等)\\s*[：:'
new0 = '|相手方|事業者等' + ADDITIONS + ')\\s*[：:'
count0 = content.count(old0)
print(f'Pattern 0 match count: {count0}')
if count0 == 1:
    content = content.replace(old0, new0, 1)
    print('Pattern 0 OK')
else:
    print('ERROR: Pattern 0 not found exactly once')

# Pattern 1: ends with |相手方|事業者等)\s*[：:]?\s*\n
old1 = '|相手方|事業者等)\\s*[：:]?\\s*\\n'
new1 = '|相手方|事業者等' + ADDITIONS + ')\\s*[：:]?\\s*\\n'
count1 = content.count(old1)
print(f'Pattern 1 match count: {count1}')
if count1 == 1:
    content = content.replace(old1, new1, 1)
    print('Pattern 1 OK')
else:
    print('ERROR: Pattern 1 not found exactly once')
    # Show context
    idx1 = content.find('|相手方|事業者等)')
    while idx1 >= 0:
        print(f'  at {idx1}: {repr(content[idx1:idx1+50])}')
        idx1 = content.find('|相手方|事業者等)', idx1 + 1)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print('Done')
