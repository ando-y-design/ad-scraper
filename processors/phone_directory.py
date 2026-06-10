from __future__ import annotations
"""
phone_directory.py — 電話帳・求人サイトで固定電話を補完検索する

試行順:
  A. iタウンページ (itp.ne.jp) — NTT公式電話帳、精度最高
  D. Indeed 求人ページ         — 会社情報欄に固定電話が載っていることがある
"""

import re
import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from utils.logger import get_logger

log = get_logger("phone_directory")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_FREEPHONE_PREFIXES = ("0120", "0800", "0570", "0990", "050")

# 実在しない日本の市外局番プレフィックス
_INVALID_AREA_RE = re.compile(
    r"^00"          # 国際プレフィックス
    r"|^010"        # 存在しない
    r"|^041"        # 存在しない（042/043は有効）
    r"|^051"        # 存在しない（052は有効）
    r"|^056[01]"    # 存在しない
    r"|^057"        # 存在しない
    r"|^061"        # 存在しない
    r"|^06[2-5]"    # 存在しない（06は有効、062等は無効）
    r"|^06[7-9]"    # 存在しない
    r"|^071"        # 存在しない（072〜は有効）
    r"|^081"        # 存在しない（082〜は有効）
    r"|^085"        # 存在しない
    r"|^091"        # 存在しない（092〜は有効）
)

_PHONE_RE = re.compile(
    r"0\d{1,4}[-\s()（）・]{0,3}\d{1,4}[-\s()（）・]{0,3}\d{3,4}"
)

_LEGAL_STRIP = re.compile(
    r"株式会社|有限会社|合同会社|合資会社|一般社団法人|公益社団法人"
    r"|特定非営利活動法人|社会福祉法人|医療法人"
)


def _is_fixed(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone)
    if not (10 <= len(digits) <= 11):
        return False
    if digits.startswith(_FREEPHONE_PREFIXES):
        return False
    if _INVALID_AREA_RE.match(digits):
        return False
    return True


def _extract_fixed_phones(text: str) -> list[str]:
    return [m.group(0) for m in _PHONE_RE.finditer(text) if _is_fixed(m.group(0))]


def _core_name(company_name: str) -> str:
    """法人格・スペースを除いた4文字以上の固有部分を返す（一致確認用）"""
    name = _LEGAL_STRIP.sub("", company_name).strip()
    name = re.sub(r"[\s　（）()株式有限合同一般社団法人]", "", name)
    return name[:6]  # 先頭6文字で照合


def search_itp(company_name: str, session: requests.Optional[Session] = None) -> str:
    """
    iタウンページで会社名検索 → 固定電話を返す。
    結果が複数ある場合は会社名が最も近いエントリを優先する。
    """
    try:
        getter = session or requests
        url = (
            "https://itp.ne.jp/searchtm/entry/top/search/"
            f"?keyword={quote(company_name)}&type=1"
        )
        r = getter.get(url, headers=_HEADERS, timeout=12, allow_redirects=True)
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "lxml")

        core = _core_name(company_name)

        # 結果リストの各エントリを走査（名前一致のみ採用）
        # セレクタが合わない場合は li / article / div 等を広く試す
        entries = soup.select(
            ".p-searchList__item, .searchListBox, .resultListItem, li.item"
        )
        if not entries:
            entries = soup.find_all(["li", "article", "div"], recursive=True)

        for entry in entries:
            text = entry.get_text(" ", strip=True)
            if len(text) < 4 or len(text) > 500:
                continue
            phones = _extract_fixed_phones(text)
            if not phones:
                continue
            # 会社名の固有部分がエントリに含まれていれば採用
            if core and core in re.sub(r"[\s　]", "", text):
                log.debug(f"itp match: {phones[0]} for '{company_name}'")
                return phones[0]

    except Exception as e:
        log.debug(f"itp search error '{company_name}': {e}")
    return ""


def search_indeed(company_name: str, session: requests.Optional[Session] = None) -> str:
    """
    Indeed 企業ページで会社名検索 → 会社情報欄の固定電話を返す。
    """
    try:
        getter = session or requests
        # 企業名スラグ（スペース→ハイフン、法人格除去）
        slug = re.sub(r"[\s　]", "-", _LEGAL_STRIP.sub("", company_name).strip())
        slug = re.sub(r"-+", "-", slug).strip("-")
        core = _core_name(company_name)

        # 1. 企業プロフィールページ直接アクセス（URLにslugが含まれる場合のみ信頼）
        profile_url = f"https://jp.indeed.com/cmp/{quote(slug)}"
        r = getter.get(profile_url, headers=_HEADERS, timeout=12, allow_redirects=True)
        r.encoding = r.apparent_encoding or "utf-8"
        # リダイレクト後のURLがプロフィールページかどうか確認（検索結果に飛ばされた場合はスキップ）
        final_path = r.url.split("?")[0].lower()
        if "/cmp/" in final_path:
            page_text = BeautifulSoup(r.text, "lxml").get_text(" ", strip=True)
            # 会社名の固有部分がページに含まれていれば採用
            if core and core in re.sub(r"[\s　]", "", page_text):
                phones = _extract_fixed_phones(r.text)
                if phones:
                    log.debug(f"indeed profile: {phones[0]} for '{company_name}'")
                    return phones[0]

    except Exception as e:
        log.debug(f"indeed search error '{company_name}': {e}")
    return ""


def find_phone_from_directory(
    company_name: str,
    session: requests.Optional[Session] = None,
) -> str:
    """
    固定電話を電話帳・求人サイトで補完検索する。
    A: iタウンページ → D: Indeed の順に試みる。
    見つかれば固定電話番号を返す。見つからなければ空文字。
    """
    if not company_name:
        return ""

    # A: iタウンページ
    phone = search_itp(company_name, session)
    if phone:
        return phone

    time.sleep(1)  # サービスへの負荷軽減

    # D: Indeed
    phone = search_indeed(company_name, session)
    if phone:
        return phone

    return ""
