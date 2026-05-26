# -*- coding: utf-8 -*-
"""Fix smart quotes in company_finder.py"""
path = r'C:\Users\amdwt\ad_scraper\processors\company_finder.py'
with open(path, encoding='utf-8') as f:
    content = f.read()

before = len([c for c in content if ord(c) in (0x201c, 0x201d)])
# Replace smart double quotes
content = content.replace('“', '"').replace('”', '"')
# Replace smart single quotes
content = content.replace('‘', "'").replace('’', "'")
after = len([c for c in content if ord(c) in (0x201c, 0x201d)])

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print(f'Fixed: {before} smart quotes → {after} remaining')
