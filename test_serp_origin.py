# -*- coding: utf-8 -*-
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\amdwt\ad_scraper")
os.chdir(r"C:\Users\amdwt\ad_scraper")

from processors.company_finder import _find_company_origin_from_serp

tests = [
    ("ソニー生命保険株式会社", "sonylife.co.jp"),
    ("PRONI株式会社", "proni.jp"),
    ("株式会社トレタ", "toreta.in"),
    ("マンパワーグループ株式会社", "manpowergroup.jp"),
]

for company, expected_domain in tests:
    result = _find_company_origin_from_serp(company)
    status = "OK" if result and expected_domain in result else "NG"
    print(f"{status}  {company}: {result}")
