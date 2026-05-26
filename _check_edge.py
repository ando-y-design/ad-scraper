# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r'C:\Users\amdwt\ad_scraper')
from processors.company_finder import _normalize_name, _is_valid_company, _is_valid_company_labeled

tests = [
    '田中商店㈲',
    '㈲田中商店',
    'サンプル㈱',
    '田中産業(株)',
    '(有)鈴木製作所',
    '株式会社　田中　産業',
    'SUN・マルシェ株式会社',
    '株式会社○○グループ（株式会社○○）',
    '株式会社テスト商事（旧: 株式会社テスト物産）',
    '株式会社A・B・C',
    '医療法人〇〇会 田中クリニック 大阪市北区',
    '合同会社スキル 03-1111-2222',
]

for t in tests:
    n = _normalize_name(t)
    v = 'VALID' if (_is_valid_company(n) or _is_valid_company_labeled(n)) else 'INVALID'
    print(f'{t!r:45} -> {n!r:35} {v}')
