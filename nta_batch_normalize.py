"""
バッチ4: NTA未確認103件に対して
  A. パターンクリーニング（事業部除去・括弧抽出・イベント名除去）
  B. ゴミ検出 → LP再取得
  C. 正常なNTA未登録（英語名等）→ スキップ
"""
from __future__ import annotations
import json
import logging
import re
import sys
import time
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

BASE_DIR = Path(__file__).parent
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / 'logs' / 'nta_batch.log', encoding='utf-8'),
    ]
)

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
COMPANY_COL = 7
LP_URL_COL  = 8

_JP_LEGAL_RE = re.compile(
    r'株式会社|有限会社|合同会社|合資会社|医療法人|社会福祉法人|'
    r'一般社団法人|公益社団法人|弁護士法人|税理士法人|農業協同組合|生活協同組合'
)

# 明らかなゴミパターン（会社名としてあり得ない先頭文字列）
_GARBAGE_STARTS = re.compile(
    r'^(価格プラン|基本情報|よくあるご質問|プライバシーポリシー|'
    r'家業を継が|オーナー専用|令和\S+年|大手町\S+ビル|'
    r'たつの市|販売業者名|商号を|屋号|本社|事業者名称)'
)

# 不可能な法人格の組み合わせ
_IMPOSSIBLE_COMBO = re.compile(
    r'行政書士法人.*(株式会社|有限会社)|'
    r'(株式会社|有限会社).*行政書士法人'
)

# 末尾の事業部・支店・店舗名を除去
_UNIT_SUFFIX = re.compile(
    r'\s+(?:[^\s]+(?:事業部|支店|本店|センター店|直営店|FC店))$'
)

# 末尾のイベント・説明文を除去
_EVENT_SUFFIX = re.compile(
    r'\s*\d+周年.*$|新車[・・]\S*販売$|新車・中古車.*$'
)

# 括弧内の会社名を抽出: Nissho(日昭株式会社) → 日昭株式会社
_PARENS_COMPANY = re.compile(r'\(([^)]*(?:株式会社|有限会社|合同会社)[^)]*)\)')

# 2社名が混合しているパターン（法人格が2つ以上）
def _count_legal(name: str) -> int:
    return len(_JP_LEGAL_RE.findall(name))


def _pattern_clean(name: str) -> str | None:
    """パターンベースのクリーニング。クリーンになった名前またはNoneを返す"""
    original = name

    # 括弧内に会社名があれば抽出
    m = _PARENS_COMPANY.search(name)
    if m:
        candidate = m.group(1).strip()
        if _JP_LEGAL_RE.search(candidate):
            return candidate

    # 末尾の事業部・支店を除去
    cleaned = _UNIT_SUFFIX.sub('', name).strip()

    # 末尾のイベント名・販売説明を除去
    cleaned = _EVENT_SUFFIX.sub('', cleaned).strip()

    # 先頭ゴミパターン検出
    if _GARBAGE_STARTS.match(cleaned):
        return None  # LP再取得に任せる

    # 不可能な法人格組み合わせ
    if _IMPOSSIBLE_COMBO.search(cleaned):
        return None  # LP再取得に任せる

    # 2つ以上の法人格（2社名混在）
    if _count_legal(cleaned) >= 2:
        return None  # LP再取得に任せる

    if cleaned != original:
        return cleaned
    return original  # 変化なし（=pattern cleaningは不要）


def _extract_from_reliable_pages(lp_url: str) -> str | None:
    """特商法・会社概要ページのみから会社名を抽出"""
    from urllib.parse import urlparse
    from processors.company_finder import (
        TOKUTEI_PATHS, COMPANY_PATHS,
        _fetch, _find_tokutei_links, _find_company_page_links,
        _parallel_search, _normalize_name, _is_valid_company,
    )
    try:
        origin = urlparse(lp_url).scheme + '://' + urlparse(lp_url).netloc
    except Exception:
        return None

    try:
        lp_soup = _fetch(lp_url, timeout=8, max_retries=1)
        dynamic_tokutei = _find_tokutei_links(lp_soup, origin, page_url=lp_url) if lp_soup else []
        dynamic_company = _find_company_page_links(lp_soup, origin, page_url=lp_url) if lp_soup else []
    except Exception:
        lp_soup = None
        dynamic_tokutei = []
        dynamic_company = []

    for urls in [
        dynamic_tokutei + [origin + p for p in TOKUTEI_PATHS],
        dynamic_company + [origin + p for p in COMPANY_PATHS],
    ]:
        try:
            company, _ = _parallel_search(urls, need_company=True, need_phone=False, max_workers=3, timeout=6.0)
            if company:
                name = _normalize_name(company)
                if name and _is_valid_company(name) and not _GARBAGE_STARTS.match(name):
                    if _count_legal(name) < 2:
                        return name
        except Exception:
            continue
    return None


def main():
    cfg = json.loads((BASE_DIR / 'config.json').read_text(encoding='utf-8'))
    sheet_id = cfg['google_sheets']['sheet_id']

    creds = Credentials.from_service_account_file(str(BASE_DIR / 'credentials.json'), scopes=SCOPES)
    client = gspread.authorize(creds)
    ws = client.open_by_key(sheet_id).worksheet('リスト')

    logging.info('Sheets読み込み中...')
    all_values = ws.get_all_values()

    # NTA未確認ファイルから対象行を読み込む
    unverified_file = BASE_DIR / 'logs' / 'nta_unverified.txt'
    if not unverified_file.exists():
        logging.error('logs/nta_unverified.txt が見つかりません。先にスキャンを実行してください。')
        return

    target_rows: dict[int, tuple[str, str]] = {}  # {sheet_row: (name, lp_url)}
    with open(unverified_file, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line.startswith('row'):
                continue
            parts = line.split('\t')
            if len(parts) >= 3:
                row_num = int(parts[0].replace('row', ''))
                name = parts[1]
                lp_url = parts[2]
                target_rows[row_num] = (name, lp_url)

    total = len(target_rows)
    logging.info(f'対象（NTA未確認）: {total}件')

    from utils.nta_lookup import verify_and_normalize

    changed = 0
    emptied = 0
    skipped = 0
    batch: list[gspread.Cell] = []

    for idx, (sheet_row, (current_name, lp_url)) in enumerate(sorted(target_rows.items()), 1):

        # A. パターンクリーニング
        pattern_result = _pattern_clean(current_name)

        if pattern_result is None:
            # ゴミ確定 → LP再取得
            logging.debug(f'[{idx}/{total}] ゴミパターン検出: "{current_name}" → LP再取得')
            extracted = _extract_from_reliable_pages(lp_url) if lp_url.startswith('http') else None

            if not extracted:
                # 取得できない → 空欄化
                logging.info(f'[{idx}/{total}] row{sheet_row}: "{current_name}" → 空欄（取得不可）')
                batch.append(gspread.Cell(sheet_row, COMPANY_COL, ''))
                emptied += 1
                continue

            # NTA確認
            try:
                nta = verify_and_normalize(extracted)
                final = nta['official_name'] if nta['verified'] else extracted
            except Exception:
                final = extracted

            if final != current_name:
                logging.info(f'[{idx}/{total}] row{sheet_row}: "{current_name}" → "{final}"（LP再取得）')
                batch.append(gspread.Cell(sheet_row, COMPANY_COL, final))
                changed += 1
            else:
                skipped += 1

        elif pattern_result != current_name:
            # パターンで改善できた
            try:
                nta = verify_and_normalize(pattern_result)
                final = nta['official_name'] if nta['verified'] else pattern_result
            except Exception:
                final = pattern_result

            logging.info(f'[{idx}/{total}] row{sheet_row}: "{current_name}" → "{final}"（パターン修正）')
            batch.append(gspread.Cell(sheet_row, COMPANY_COL, final))
            changed += 1

        else:
            # 変化なし（英語名等の正常なNTA未登録）
            skipped += 1

        if idx % 20 == 0:
            logging.info(f'進捗: {idx}/{total} | 変更:{changed} 空欄化:{emptied} スキップ:{skipped}')

        time.sleep(0.5)

    if batch:
        logging.info(f'Sheets更新中: {len(batch)}件...')
        ws.update_cells(batch, value_input_option='RAW')
        logging.info('Sheets更新完了')
    else:
        logging.info('変更なし')

    logging.info('=' * 50)
    logging.info(f'完了 | 変更={changed} / 空欄化={emptied} / スキップ={skipped} / 合計={total}')


if __name__ == '__main__':
    main()
