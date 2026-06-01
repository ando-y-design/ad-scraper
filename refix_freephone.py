from __future__ import annotations
"""
refix_freephone.py — フリーダイヤルのレコードを再スクレイプして固定電話に差し替える

対象: DBのphone が 0120/0800/0570/0990 のレコード
処理: LP → 特商法ページまで含めて find_company_info を実行
      固定電話が取れたら更新、取れなければそのまま
"""
import re
import sqlite3
import requests
from processors.company_finder import find_company_info, find_fixed_phone_from_hp
from processors.phone_directory import find_phone_from_directory
from processors.normalizer import normalize_phone, is_valid_company_name, normalize_company_name

_FREEPHONE = ("0120", "0800", "0570", "0990")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
}


def is_freephone(phone: str) -> bool:
    return re.sub(r"\D", "", phone).startswith(_FREEPHONE)


def fetch(url: str, session: requests.Session) -> str | None:
    try:
        r = session.get(url, headers=_HEADERS, timeout=15, allow_redirects=True)
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:
        print(f"  fetch error: {e}")
        return None


def main():
    conn = sqlite3.connect("companies.db")
    rows = conn.execute("""
        SELECT id, company_name, phone, lp_url
        FROM companies
        WHERE phone LIKE '0120%'
           OR phone LIKE '0800%'
           OR phone LIKE '0570%'
           OR phone LIKE '0990%'
        ORDER BY id
    """).fetchall()

    print(f"フリーダイヤル対象: {len(rows)} 件\n")
    session = requests.Session()
    updated = 0
    skipped = 0

    for id_, company_name, old_phone, lp_url in rows:
        print(f"[{id_}] {company_name} | {old_phone}")

        html = fetch(lp_url, session)
        new_phone = ""
        new_source = ""
        new_name = company_name

        if html:
            info = find_company_info(lp_url, html, session=session)
            new_phone = info.get("phone", "")
            new_source = info.get("phone_source", "")
            new_name = info.get("company_name", "") or company_name

            # LP→HP経由で特商法を探す（フリーダイヤルしか取れなかった場合）
            if not new_phone or is_freephone(new_phone):
                fixed = find_fixed_phone_from_hp(lp_url, html, session=session)
                if fixed and not is_freephone(fixed):
                    new_phone = fixed
                    new_source = (new_source or "hp") + "_hp"
                    print(f"  → HP経由で固定電話発見: {new_phone}")
        else:
            print("  → LP取得失敗、電話帳のみ試行")

        # 電話帳サービスで補完（LP死活に関係なく会社名で検索）
        if not new_phone or is_freephone(new_phone):
            dir_phone = find_phone_from_directory(new_name, session)
            if dir_phone and not is_freephone(dir_phone):
                new_phone = dir_phone
                new_source = "_dir"
                print(f"  → 電話帳で固定電話発見: {new_phone}")

        if not new_phone or is_freephone(new_phone):
            print(f"  → 固定電話取れず（{new_phone or 'なし'}）、スキップ")
            skipped += 1
            continue

        new_phone = normalize_phone(new_phone)
        print(f"  → 固定電話発見: {new_phone} [{new_source}]")

        # 社名も改善されていれば一緒に更新
        update_name = new_name if (new_name and is_valid_company_name(new_name)) else company_name
        norm_name = normalize_company_name(update_name)

        conn.execute(
            "UPDATE companies SET phone=?, phone_source=?, company_name=?, normalized_name=? WHERE id=?",
            (new_phone, new_source, update_name, norm_name, id_),
        )
        conn.commit()
        updated += 1

    conn.close()
    print(f"\n完了: 更新={updated} スキップ={skipped}")


if __name__ == "__main__":
    main()
