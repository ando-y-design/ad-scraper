import pathlib
import re

p = pathlib.Path(r'C:\Users\amdwt\ad_scraper\logs\snapshots')
files = sorted(p.glob('*.html'), key=lambda x: x.stat().st_mtime)
print(f'Total snapshots: {len(files)}')
print()

for f in files:
    html = f.read_text('utf-8', errors='replace')
    has_captcha = 'captcha' in html.lower()
    has_pr = 'PR' in html or 'スポンサー' in html or 'スポンサード' in html
    has_tracking = any(x in html for x in ['rd.listing.yahoo', 'cl.search.yahoo', 'yclid', 'yads.c.yimg'])
    has_ads_badge = 'ad' in html.lower() and ('sponsored' in html.lower() or 'PromotedAd' in html)
    data_ual = 'data-ual' in html

    # Count search results
    result_count = html.count('"page_type": "result"') + html.count("page_type: 'result'")

    status = []
    if has_captcha: status.append('CAPTCHA?')
    if has_pr: status.append('PR')
    if has_tracking: status.append('TRACKING')
    if data_ual: status.append('UAL')
    if not status: status.append('NO_ADS')

    print(f'{f.name[-30:]}: {", ".join(status)}')

# Read the oldest snapshot (more likely to be different condition)
print()
print('=== Checking if bot detection by comparing page length ===')
for f in files[:3]:
    html = f.read_text('utf-8', errors='replace')
    print(f'{f.name[-25:]}: {len(html)} chars, captcha_word_count={html.lower().count("captcha")}')
    # Find captcha mention context
    idx = html.lower().find('captcha')
    if idx >= 0:
        print(f'  Context: ...{html[max(0,idx-50):idx+100]}...')
