from __future__ import annotations
"""
英語法人格サフィックス（Inc./Corp./Ltd. 等）を含む会社名を
国税庁法人番号公表サイト WebAPI で正式な日本語法人登録名に解決する。

config.json の nta_api_key が設定されている場合のみ有効。
未設定の場合は元の名前をそのまま維持する。

API 登録（無料・即時）:
  https://www.houjin-bangou.nta.go.jp/webapi/
  → 取得した「アプリケーション ID」を config.json の nta_api_key に設定する。
"""
import logging
import re
import xml.etree.ElementTree as ET

import requests

# ── パターン ────────────────────────────────────────────────────────────────

_JP_LEGAL_RE = re.compile(
    r'株式会社|有限会社|合同会社|合資会社|合名会社|'
    r'一般社団法人|公益社団法人|一般財団法人|公益財団法人|'
    r'医療法人|社会福祉法人|学校法人|宗教法人|NPO法人|'
    r'弁護士法人|税理士法人'
)

# 末尾に付く英語法人格サフィックス（前のカンマ・スペースも含めて除去）
_EN_LEGAL_SUFFIX_RE = re.compile(
    r'[\s,，]*'
    r'(?:Holdings?\.?|Incorporated\.?|Inc\.?|Corp(?:oration)?\.?'
    r'|Co\.?,?\s*Ltd\.?|Ltd\.?|LLC|LLP|GmbH|PLC|S\.A\.?)'
    r'\s*$',
    re.IGNORECASE
)

_NTA_API_URL = 'https://api.houjin-bangou.nta.go.jp/4/name'

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; AdScraper/1.0)',
    'Accept': 'application/xml',
}

# プロセス内メモリキャッシュ（同一名の重複 API 呼び出しを防ぐ）
# (resolved_name, corporate_number) のタプルを保持
_cache: dict[str, tuple[str | None, str | None]] = {}

# 法人番号専用キャッシュ（日本語名 → 法人番号）
_corp_num_cache: dict[str, str | None] = {}

# API キー未設定の警告を 1 回だけ出す
_warned_no_key = False


# ── 公開 API ────────────────────────────────────────────────────────────────

def needs_jp_name_lookup(name: str) -> bool:
    """
    日本語法人格なし・英語法人格サフィックスありの名前を検出する。
    例: "M-Style Japan Co., Ltd." → True
        "株式会社ユニモト"         → False
        "株式会社LayerX"           → False
    """
    if not name:
        return False
    if _JP_LEGAL_RE.search(name):
        return False  # 既に日本語法人格あり → 調べ直し不要
    return bool(_EN_LEGAL_SUFFIX_RE.search(name))


def resolve_legal_name(name: str, api_key: str) -> str | None:
    """
    NTA 法人番号 WebAPI で正式な日本語法人登録名を検索して返す。
    ヒットしない・API キーが空の場合は None を返す（元の名前を維持）。

    結果はプロセス内でキャッシュされる（同じ名前の 2 回目以降は API 呼び出しなし）。

    Args:
        name:    英語サフィックス付き会社名 例: "M-Style Japan Co., Ltd."
        api_key: 国税庁法人番号システム WebAPI のアプリケーション ID

    Returns:
        正式な日本語法人登録名 例: "M-Style Japan株式会社"、または None
    """
    global _warned_no_key

    if not api_key:
        if not _warned_no_key:
            logging.warning(
                '[NTA] nta_api_key が未設定です。英語法人格名はそのまま保持されます。'
                ' 無料登録: https://www.houjin-bangou.nta.go.jp/webapi/'
            )
            _warned_no_key = True
        return None

    # キャッシュヒット
    if name in _cache:
        return _cache[name][0]

    # 末尾英語サフィックスを除いた検索クエリを作成
    core_name = _EN_LEGAL_SUFFIX_RE.sub('', name).strip()
    if not core_name:
        _cache[name] = (None, None)
        return None

    resolved_name, corp_number = _query_nta(core_name, api_key)
    _cache[name] = (resolved_name, corp_number)

    if resolved_name:
        logging.info(f'[NTA] 法人登録名解決: "{name}" → "{resolved_name}"')
    else:
        logging.debug(f'[NTA] 未解決（NTA にヒットなし）: "{name}"')

    return resolved_name


def lookup_corporate_number(name: str, api_key: str) -> str | None:
    """
    会社名から国税庁 WebAPI 経由で法人番号（13桁）を取得して返す。
    API キーが未設定・ヒットなしの場合は None を返す。

    日本語法人名・英語サフィックス名どちらにも対応。
    結果はプロセス内でキャッシュされる。
    """
    if not api_key or not name:
        return None

    if name in _corp_num_cache:
        return _corp_num_cache[name]

    # resolve_legal_name キャッシュに法人番号がある場合はそちらを使う
    if name in _cache:
        corp_number = _cache[name][1]
        _corp_num_cache[name] = corp_number
        return corp_number

    # 法人格サフィックス（日英）を除去してコア名を作成
    core = _JP_LEGAL_RE.sub('', name).strip()
    core = _EN_LEGAL_SUFFIX_RE.sub('', core).strip()
    if not core:
        _corp_num_cache[name] = None
        return None

    _, corp_number = _query_nta(core, api_key)
    _corp_num_cache[name] = corp_number

    if corp_number:
        logging.info(f'[NTA] 法人番号取得: "{name}" → {corp_number}')
    else:
        logging.debug(f'[NTA] 法人番号未取得: "{name}"')

    return corp_number


# ── 内部処理 ─────────────────────────────────────────────────────────────────

def _query_nta(core_name: str, api_key: str) -> tuple[str | None, str | None]:
    """NTA API にリクエストして (最適法人名, 法人番号) を返す。"""
    try:
        resp = requests.get(
            _NTA_API_URL,
            params={
                'id': api_key,
                'name': core_name,
                'type': '12',   # XML形式
            },
            headers=_HEADERS,
            timeout=10,
        )

        if resp.status_code == 400:
            logging.warning(f'[NTA] API bad request (キー不正？): "{core_name}"')
            return None, None
        if resp.status_code == 404:
            return None, None
        if resp.status_code != 200:
            logging.warning(f'[NTA] HTTP {resp.status_code}: "{core_name}"')
            return None, None

        root = ET.fromstring(resp.content)

        # 日本語法人格を含む候補だけを収集 (name, corporateNumber)
        candidates: list[tuple[str, str]] = []
        for corp in root.findall('.//corporation'):
            name_elem = corp.find('name')
            number_elem = corp.find('corporateNumber')
            if name_elem is not None and name_elem.text:
                corp_name = name_elem.text.strip()
                if corp_name and _JP_LEGAL_RE.search(corp_name):
                    corp_number = (
                        number_elem.text.strip()
                        if number_elem is not None and number_elem.text
                        else ''
                    )
                    candidates.append((corp_name, corp_number))

        if not candidates:
            return None, None
        if len(candidates) == 1:
            return candidates[0]

        # 複数ヒット: コア名との文字一致スコアで最良候補を選ぶ
        names = [c[0] for c in candidates]
        best_name = _best_match(core_name, names)
        best_idx = names.index(best_name)
        return candidates[best_idx]

    except ET.ParseError as e:
        logging.warning(f'[NTA] XML parse error "{core_name}": {e}')
        return None, None
    except requests.RequestException as e:
        logging.warning(f'[NTA] network error "{core_name}": {e}')
        return None, None
    except Exception as e:
        logging.warning(f'[NTA] unexpected error "{core_name}": {e}')
        return None, None


def _best_match(core_name: str, candidates: list[str]) -> str:
    """
    コア名（英語サフィックス除去後）と最も一致するNTA候補を選ぶ。
    スコア = (コア名の文字が候補コア名に含まれる数, −長さ差)
    """
    core_lower = core_name.lower()

    def score(c: str) -> tuple:
        c_core = _JP_LEGAL_RE.sub('', c).strip().lower()
        common = sum(1 for ch in core_lower if ch in c_core)
        length_diff = -abs(len(c_core) - len(core_lower))
        return (common, length_diff)

    return max(candidates, key=score)
