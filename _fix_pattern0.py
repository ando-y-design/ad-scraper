# -*- coding: utf-8 -*-
"""Fix Pattern 0 of _COMPANY_LABEL_PATTERNS - add missing labels."""
path = r'C:\Users\amdwt\ad_scraper\processors\company_finder.py'
with open(path, encoding='utf-8') as f:
    content = f.read()

ADDITIONS = '|法人の名称|ストア名|運営者|発行者|管理者|相手方|事業者等'

# Pattern 0 uses literal ： (6 ASCII chars) in the character class
# Find the exact sequence: |受注者)\s*[ full-width-colon :： ]
# We need to match against what's actually in the file
idx = content.find('|受注者)\\s*[')
if idx >= 0:
    snippet = content[idx:idx+50]
    print(f'Found at {idx}: {repr(snippet)}')

    # The end marker of the label alternation before the )\s*[
    # Replace "|受注者)\s*[" with "|受注者ADDITIONS)\s*["
    old_str = '|受注者)\\s*[\\uff1a' # won't work - this is unicode

    # Let's find it by the surrounding context
    # Pattern 0 line: r'(?:...受注者)\s*[：:：]\s*([^\n：:]{2,80})'
    # We know at position idx we have: |受注者)\s*[
    # Replace |受注者)\s* with |受注者ADDITIONS)\s*

    # More reliable: replace the exact segment between |受注者) and \s*
    old = '|受注者)\\s*'
    new = '|受注者' + ADDITIONS + ')\\s*'

    count = content.count(old)
    print(f'count of old string: {count}')
    if count == 1:
        content = content.replace(old, new, 1)
        print('Pattern 0 replaced OK')
    else:
        # There might be multiple \s* - use context
        # Pattern 0 context: ends with )\s*[：:：]\s*([^\n
        old_p0 = '|受注者)\\s*[：:\\uff1a]\\s*([^\\n'
        new_p0 = '|受注者' + ADDITIONS + ')\\s*[：:\\uff1a]\\s*([^\\n'
        count_p0 = content.count(old_p0)
        print(f'count of pattern0 context: {count_p0}')
        if count_p0 == 1:
            content = content.replace(old_p0, new_p0, 1)
            print('Pattern 0 replaced via context OK')
        else:
            print('ERROR: Could not replace pattern 0')
            # Show what comes after |受注者) for diagnosis
            idx2 = 0
            while True:
                idx2 = content.find('|受注者)', idx2)
                if idx2 < 0:
                    break
                print(f'  occurrence at {idx2}: {repr(content[idx2:idx2+30])}')
                idx2 += 1
else:
    print('|受注者)\\s*[ not found - trying alternative')
    # Maybe it already has additions from a previous script
    idx2 = content.find('受注者')
    while idx2 >= 0:
        print(f'  受注者 at {idx2}: {repr(content[idx2:idx2+20])}')
        idx2 = content.find('受注者', idx2+1)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print('Done')
