from __future__ import annotations
"""
国税庁法人番号システムWeb-API クライアント

APIキーは config.json の nta_api_key に設定する。
有効化まで数時間かかる場合がある（発行直後は404になる）。

主な用途:
  - スクレイプした会社名を正式な法人登録名に補正する
  - 実在しない会社名（スクレイプミス）を除外する

API仕様: https://www.houjin-bangou.nta.go.jp/webapi/
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
_NS = 'http://www.houjin-bangou.nta.go.jp/webapi/corporation/'
_API_BASE = 'https://api.houjin-bangou.nta.go.jp/4'

# 1リクエストあたりの最大ヒット数
_MAX_HIT = 5

# 同一名称の重複リクエストを避けるインメモリキャッシュ（最大500件）
_cache: dict[str, list[dict]] = {}
_CACHE_MAX = 500

# レートリミット: 8スレッド共有・1.5秒間隔（スレッドセーフ）
_rate_lock = threading.Lock()
_last_request_time: float = 0.0
_MIN_INTERVAL = 1.5

# 404受信後はセッション中の全APIコールを無効化
_api_disabled: bool = False

# 400エラーが返った名前はセッション内でスキップ（繰り返しリクエスト防止）
_nta_400_skip: set[str] = set()


def _get_app_id() -> str:
    """config.json から NTA APIキーを取得する"""
    try:
        cfg = json.loads((BASE_DIR / 'config.json').read_text(encoding='utf-8'))
        return cfg.get('nta_api_key', '').strip()
    except Exception:
        return ''


def _xml_text(elem, tag: str, ns: str = _NS) -> str:
    child = elem.find(f'{{{ns}}}{tag}') if ns else elem.find(tag)
    if child is None:
        child = elem.find(tag)  # nsなしでも試す
    return child.text.strip() if child is not None and child.text else ''


def search_by_name(name: str, mode: int = 2):
    """
    会社名で法人番号APIを検索する。

    Args:
        name: 検索する会社名（部分一致）
        mode: 未使用（API互換性のため残存）

    Returns:
        list of {
            'corporate_number': str,  # 法人番号（13桁）
            'name': str,              # 正式法人名
            'address': str,           # 登記上の住所
            'kind': str,              # 法人種別
            'close_date': str,        # 閉鎖日（空=現存）
        }
        APIキー未設定・ヒットなしの場合は []
        通信エラー・400エラーなど一時的な失敗の場合は None
    """
    global _last_request_time, _api_disabled

    if _api_disabled:
        return []

    app_id = _get_app_id()
    if not app_id:
        logging.debug('[NTA] APIキー未設定。スキップ')
        return []

    # 短すぎる名称は誤ヒットが多いためスキップ
    name = name.strip()
    if len(name) < 3:
        return []

    cache_key = f'{name}:{mode}'
    if cache_key in _cache:
        return _cache[cache_key]

    if name in _nta_400_skip:
        logging.debug(f'[NTA] 400スキップ: "{name}"')
        return None

    # レートリミット: 8スレッド共有、最低1.5秒間隔（スレッドセーフ）
    with _rate_lock:
        elapsed = time.time() - _last_request_time
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        _last_request_time = time.time()

    params = urllib.parse.urlencode({
        'id': app_id,
        'name': name,
        'type': '12',       # XML形式（Unicode）
        'target': '1',      # 法人名のみ
        'hit_count': str(_MAX_HIT),
        'close': '0',       # 閉鎖法人を除外
    })
    url = f'{_API_BASE}/name?{params}'

    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            raw = r.read()

        root = ET.fromstring(raw)

        results = []
        for corp in root.findall(f'{{{_NS}}}corporation') or root.findall('corporation'):
            results.append({
                'corporate_number': _xml_text(corp, 'corporateNumber'),
                'name':             _xml_text(corp, 'name'),
                'address':          _xml_text(corp, 'address'),
                'kind':             _xml_text(corp, 'kind'),
                'close_date':       _xml_text(corp, 'closeDate'),
            })

        # キャッシュに保存
        if len(_cache) >= _CACHE_MAX:
            # 古いエントリを半分削除（LRUの代わりに簡易版）
            for k in list(_cache.keys())[:_CACHE_MAX // 2]:
                del _cache[k]
        _cache[cache_key] = results

        logging.debug(f'[NTA] "{name}" → {len(results)}件ヒット')
        return results

    except urllib.error.HTTPError as e:
        if e.code == 404:
            _api_disabled = True
            logging.warning(
                '[NTA] APIが404。キーが未有効化の可能性あり — セッション中のNTA呼び出しを停止します。'
                ' 再有効化: https://www.houjin-bangou.nta.go.jp/webapi/'
            )
            return None
        elif e.code == 400:
            _nta_400_skip.add(name)
            logging.warning(f'[NTA] 400エラー → セッション内スキップ登録: "{name}"')
            return None  # 一時的なエラー → None（ヒットなしと区別）
        else:
            logging.warning(f'[NTA] HTTPエラー: {e.code}')
            return None  # 一時的なエラー → None（ヒットなしと区別）
    except Exception as e:
        logging.debug(f'[NTA] 検索失敗 "{name}": {e}')
        return None  # 通信エラー等 → None


def verify_and_normalize(raw_name: str) -> dict:
    """
    スクレイプした会社名を法人DBで照合・補正する。
    優先順: NTA Web API → ローカルDB（nta_corp.db）→ 未確認

    Returns:
        {
            'verified': bool,        # 実在法人として確認できたか
            'official_name': str,    # 正式法人名（見つからなければ raw_name のまま）
            'corporate_number': str, # 法人番号（見つからなければ空）
            'address': str,          # 住所
            'confidence': str,       # 'exact' / 'partial' / 'none'
        }
    """
    # 法人格を除いた検索用キー（「株式会社」等を省いて検索精度を上げる）
    import re
    search_name = re.sub(
        r'^(株式会社|有限会社|合同会社|医療法人|社会福祉法人|宗教法人|一般社団法人|公益社団法人)\s*',
        '', raw_name
    ).strip()
    search_name = re.sub(
        r'\s*(株式会社|有限会社|合同会社)$', '', search_name
    ).strip()

    # 検索名が短すぎる場合はそのまま使う
    if len(search_name) < 3:
        search_name = raw_name

    hits = search_by_name(search_name, mode=2)

    if hits is None:
        # 通信エラー・一時的な失敗 → ローカルDBにフォールバック（書き込みはブロックしない）
        try:
            from utils.nta_local_db import verify_local, is_db_ready
            if is_db_ready():
                result = verify_local(raw_name)
                result['nta_error'] = True
                return result
        except Exception:
            pass
        return {
            'verified': False,
            'official_name': raw_name,
            'corporate_number': '',
            'address': '',
            'confidence': 'none',
            'nta_error': True,  # 通信エラーなので弾かない
        }

    if not hits:
        # ヒットなし（APIは正常応答） → ローカルDBにフォールバック
        try:
            from utils.nta_local_db import verify_local, is_db_ready
            if is_db_ready():
                result = verify_local(raw_name)
                result['nta_error'] = False
                return result
        except Exception:
            pass
        return {
            'verified': False,
            'official_name': raw_name,
            'corporate_number': '',
            'address': '',
            'confidence': 'none',
            'nta_error': False,  # 正常にヒットなし → ゴミ名の可能性
        }

    _LEGAL_STRIP = re.compile(
        r'(株式会社|有限会社|合同会社|合資会社|合名会社|医療法人|社会福祉法人|'
        r'宗教法人|一般社団法人|公益社団法人|一般財団法人|公益財団法人|'
        r'学校法人|NPO法人|弁護士法人|税理士法人)'
    )

    def _core(name: str) -> str:
        return _LEGAL_STRIP.sub('', name).strip()

    search_core = _core(search_name)

    # 完全一致を優先（raw_nameそのもの、またはコア名一致）
    for h in hits:
        if h['name'] == raw_name:
            return {
                'verified': True,
                'official_name': h['name'],
                'corporate_number': h['corporate_number'],
                'address': h['address'],
                'confidence': 'exact',
                'nta_error': False,
            }

    # 部分一致: NTA結果のコア名 == 検索コア名 の場合のみ許可
    for h in hits:
        h_core = _core(h['name'])
        if h_core == search_core:
            return {
                'verified': True,
                'official_name': h['name'],
                'corporate_number': h['corporate_number'],
                'address': h['address'],
                'confidence': 'partial',
                'nta_error': False,
            }

    return {
        'verified': False,
        'official_name': raw_name,
        'corporate_number': '',
        'address': '',
        'confidence': 'none',
        'nta_error': False,  # APIは正常応答したがマッチなし
    }


# ─────────────────────────────────────────────
# 単体テスト
# ─────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    test_names = [
        'トヨタ自動車',
        'ソフトバンク',
        'デンタルクリニック',
    ]
    for n in test_names:
        print(f'\n--- {n} ---')
        result = verify_and_normalize(n)
        for k, v in result.items():
            print(f'  {k}: {v}')
