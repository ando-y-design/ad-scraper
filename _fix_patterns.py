# -*- coding: utf-8 -*-
"""Fix _COMPANY_LABEL_PATTERNS to add new labels."""
import sys

path = r'C:\Users\amdwt\ad_scraper\processors\company_finder.py'
with open(path, encoding='utf-8') as f:
    content = f.read()

new_labels = '|販売会社|代理店|主催者|主催会社|代理店名|申込先会社|注文先|受注者'

# Find the exact old strings in the file
# Pattern 1 ends with: 事業所名)\s*[：:：]\s*([^\n：:]{2,80})'
# In the file ： is literal 6 ASCII chars (it's inside an r'...' raw string)

# Locate pattern 1
idx1_start = content.find("事業所名)\\s*[")
if idx1_start < 0:
    # find without the backslash interpretation
    idx1_start = content.find('事業所名)')
    if idx1_start >= 0:
        print("Found 事業所名) at:", idx1_start)
        snippet = content[idx1_start:idx1_start+60]
        print("Snippet repr:", repr(snippet))
    sys.exit(1)

# Find the end of pattern 1 line
idx1_end = content.find("',\n    # ラベルの次", idx1_start)
if idx1_end < 0:
    print("Could not find end of pattern 1")
    sys.exit(1)

old1 = content[idx1_start:idx1_end + 1]  # include trailing '
print("old1 repr:", repr(old1))

new1 = old1.replace('事業所名)', '事業所名' + new_labels + ')', 1)
print("new1 repr:", repr(new1))

content = content.replace(old1, new1, 1)
print("Pattern 1 updated:", old1 != new1)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Done")
