# -*- coding: utf-8 -*-
"""Fix _COMPANY_LABEL_PATTERNS[1]: add optional colon before newline."""
path = r'C:\Users\amdwt\ad_scraper\processors\company_finder.py'
with open(path, encoding='utf-8') as f:
    content = f.read()

# Pattern 1 (newline variant) ends with: \s*\n\s*([^\n
# We want: \s*[：:]?\s*\n\s*([^\n
# The ： here is U+FF1A (full-width colon) - same as in the existing pattern.

old = r'\s*\n\s*([^\n：:]{2,80})'
new = r'\s*[：:]?\s*\n\s*([^\n：:]{2,80})'

# Verify old string is in the file
if old in content:
    content = content.replace(old, new, 1)
    print(f'Replaced: {old!r} → {new!r}')
else:
    # Try with actual unicode character
    import unicodedata
    fwcolon = '：'
    old2 = rf'\s*\n\s*([^\n{fwcolon}:]{"{2,80}"})'
    print(f'old not found. File snippet around \\s*\\n\\s*:')
    idx = content.find(r'\s*\n\s*([^\n')
    if idx >= 0:
        print(repr(content[idx:idx+40]))
    else:
        print('Pattern not found in file at all')

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print('Done')
