from __future__ import annotations
"""
brushup.py — DBの既存レコードを再フェッチして電話番号・社名をブラッシュアップする
"""
import re
import sqlite3
import requests
from processors.company_finder import find_company_info, find_fixed_phone_from_hp, _clean_company_name
from processors.normalizer import normalize_company_name, normalize_phone, is_valid_company_name

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
        r = session.get(url, headers=_HEADERS, timeout=12, allow_redirects=True)
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:
        print(f"  fetch error: {e}")
        return None


def main():
    conn = sqlite3.connect("companies.db")
    rows = conn.execute(
        "SELECT id, company_name, phone, phone_source, lp_url FROM companies ORDER BY id"
    ).fetchall()
    print(f"処理対象: {len(rows)} 件\n")

    session = requests.Session()
    updated = 0
    removed = 0

    for id_, company_name, phone, phone_source, lp_url in rows:
        print(f"[{id_}] {company_name} | {phone} ({phone_source})")

        html = fetch(lp_url, session)
        if not html:
            print("  → LP取得失敗、スキップ")
            continue

        # 1. 会社情報を再抽出（最新コードで）
        info = find_company_info(lp_url, html, session=session)
        new_name = info.get("company_name", "") or company_name
        new_phone = info.get("phone", "") or phone
        new_source = info.get("phone_source", "") or phone_source

        # 2. フリーダイヤルならHP固定電話を探す
        if is_freephone(new_phone):
            fixed = find_fixed_phone_from_hp(lp_url, html, session=session)
            if fixed and not is_freephone(fixed):
                print(f"  → HP固定電話発見: {fixed}")
                new_phone = fixed
                new_source = new_source + "_hp"

        # 3. 社名クリーニング
        cleaned_name = _clean_company_name(new_name) or _clean_company_name(company_name)

        # 4. バリデーション
        if not cleaned_name or not new_phone or not is_valid_company_name(cleaned_name):
            print(f"  → 無効: name='{cleaned_name}' phone='{new_phone}' → 削除")
            conn.execute("DELETE FROM companies WHERE id=?", (id_,))
            removed += 1
            continue

        normalized_name = normalize_company_name(cleaned_name)
        new_phone = normalize_phone(new_phone)

        # 変化があれば更新
        if cleaned_name != company_name or new_phone != phone or new_source != phone_source:
            print(f"  → 更新: '{cleaned_name}' | {new_phone} [{new_source}]")
            conn.execute(
                "UPDATE companies SET company_name=?, normalized_name=?, phone=?, phone_source=? WHERE id=?",
                (cleaned_name, normalized_name, new_phone, new_source, id_),
            )
            updated += 1
        else:
            print(f"  → 変化なし")

    conn.commit()
    conn.close()
    print(f"\n完了: 更新={updated} 削除={removed}")


if __name__ == "__main__":
    main()
