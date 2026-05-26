# -*- coding: utf-8 -*-
"""Debug: find what's around 受注者 in company_finder.py"""
path = r'C:\Users\amdwt\ad_scraper\processors\company_finder.py'
with open(path, encoding='utf-8') as f:
    content = f.read()

idx = 0
while True:
    idx = content.find('受注者)', idx)
    if idx < 0:
        break
    print(f'At {idx}: {repr(content[idx:idx+30])}')
    idx += 1
