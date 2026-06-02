from __future__ import annotations
from typing import Optional
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
import unicodedata
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


def _to_fullwidth(text: str) -> str:
    """半角英数字・記号を全角に変換する（NTA APIは全角のみ受け付ける）。"""
    result = []
    for ch in text:
        cp = ord(ch)
        if 0x21 <= cp <= 0x7E:   # ! ～ ~ の半角ASCII
            result.append(chr(cp + 0xFEE0))
        elif ch == ' ':
            result.append('　')  # 半角スペース → 全角スペース
        else:
            result.append(ch)
    return ''.join(result)

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; AdScraper/1.0)',
    'Accept': 'application/xml',
}

# プロセス内メモリキャッシュ（同一名の重複 API 呼び出しを防ぐ）
# (resolved_name, corporate_number) のタプルを保持
_cache: dict[str, tuple[Optional[str], Optional[str]]] = {}

# 法人番号専用キャッシュ（日本語名 → 法人番号）
_corp_num_cache: dict[str, Optional[str]] = {}

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


def resolve_legal_name(name: str, api_key: str) -> Optional[str]:
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


def lookup_corporate_number(name: str, api_key: str) -> tuple[Optional[str], Optional[str]]:
    """
    会社名から国税庁 WebAPI 経由で (法人番号, 正式法人名) を返す。
    マッチしない場合は (None, None)、一時エラーは ('__NTA_ERROR__', None)。

    日本語法人名・英語サフィックス名どちらにも対応。
    結果はプロセス内でキャッシュされる。
    """
    if not api_key or not name:
        return None, None

    if name in _corp_num_cache:
        cached = _corp_num_cache[name]
        # 旧キャッシュ（文字列）との互換
        if isinstance(cached, tuple):
            return cached
        return cached, None

    # resolve_legal_name キャッシュに法人番号がある場合はそちらを使う
    if name in _cache:
        official_name, corp_number = _cache[name]
        _corp_num_cache[name] = (corp_number, official_name)
        return corp_number, official_name

    # 法人格サフィックス（日英）を除去してコア名を作成
    core = _JP_LEGAL_RE.sub('', name).strip()
    core = _EN_LEGAL_SUFFIX_RE.sub('', core).strip()
    if not core:
        _corp_num_cache[name] = (None, None)
        return None, None

    resolved_name, corp_number = _query_nta(core, api_key, original_name=name)
    if resolved_name == '__NTA_ERROR__':
        return '__NTA_ERROR__', None

    _corp_num_cache[name] = (corp_number, resolved_name)

    if corp_number:
        logging.info(f'[NTA] 法人番号取得: "{name}" → {corp_number} / 正式名: "{resolved_name}"')
    else:
        logging.debug(f'[NTA] 法人番号未取得: "{name}"')

    return corp_number, resolved_name


# ── 内部処理 ─────────────────────────────────────────────────────────────────

def _query_nta(core_name: str, api_key: str, original_name: str = '') -> tuple[Optional[str], Optional[str]]:
    """NTA API にリクエストして (最適法人名, 法人番号) を返す。"""
    try:
        resp = requests.get(
            _NTA_API_URL,
            params={
                'id': api_key,
                'name': _to_fullwidth(core_name),  # 半角→全角変換（NTA API要件）
                'type': '12',   # XML形式
            },
            headers=_HEADERS,
            timeout=20,
        )

        if resp.status_code == 400:
            logging.warning(f'[NTA] HTTPエラー: {resp.status_code}')
            return '__NTA_ERROR__', None  # 一時エラー（ヒットなしと区別）
        if resp.status_code == 404:
            return None, None
        if resp.status_code != 200:
            logging.warning(f'[NTA] HTTP {resp.status_code}: "{core_name}"')
            return '__NTA_ERROR__', None  # 一時エラー

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
        best_name = _best_match(core_name, names, original_name=original_name)
        best_idx = names.index(best_name)
        return candidates[best_idx]

    except ET.ParseError as e:
        logging.warning(f'[NTA] XML parse error "{core_name}": {e}')
        return '__NTA_ERROR__', None
    except requests.RequestException as e:
        logging.warning(f'[NTA] network error "{core_name}": {e}')
        return '__NTA_ERROR__', None
    except Exception as e:
        logging.warning(f'[NTA] unexpected error "{core_name}": {e}')
        return '__NTA_ERROR__', None


def _normalize_for_match(s: str) -> str:
    """全角→半角正規化 + 小文字化（マッチング用）。"""
    return unicodedata.normalize('NFKC', s).lower()


def _best_match(core_name: str, candidates: list[str], original_name: str = '') -> str:
    """
    コア名と最も一致するNTA候補を選ぶ。
    優先順: 完全一致 > 元名と同じ法人格 > 文字一致数 > 長さ差
    全角・半角を正規化してから比較する。
    """
    core_lower = _normalize_for_match(core_name)

    # 元の名前に含まれる法人格種別（株式会社・有限会社 等）を抽出
    original_kind = ''
    for kind in ('株式会社', '有限会社', '合同会社', '合資会社', '合名会社',
                 '一般社団法人', '公益社団法人', '医療法人', '税理士法人'):
        if kind in original_name:
            original_kind = kind
            break

    core_nfkc = unicodedata.normalize('NFKC', core_name)  # lowercase前の正規化

    def score(c: str) -> tuple:
        c_core_raw = _JP_LEGAL_RE.sub('', c).strip()
        c_core_nfkc = unicodedata.normalize('NFKC', c_core_raw)
        c_core = c_core_nfkc.lower()
        exact_strict = 1 if c_core_nfkc == core_nfkc else 0   # 大小文字区別あり
        exact_loose  = 1 if c_core == core_lower else 0         # 大小文字区別なし
        same_kind    = 1 if (original_kind and original_kind in c) else 0
        common = sum(1 for ch in core_lower if ch in c_core)
        length_diff  = -abs(len(c_core) - len(core_lower))
        return (exact_strict, exact_loose, same_kind, common, length_diff)

    return max(candidates, key=score)
