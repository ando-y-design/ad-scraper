from __future__ import annotations
# -*- coding: utf-8 -*-
"""
Facebook Ad Library API スクレイパー
公式APIでMetaの広告主LP URLを取得（Playwrightより安定・高速・無料）

必要な設定 (config.json):
  "meta_api": {
    "app_id": "1018342207539085",
    "app_secret": "<App Secret here>",
    "enabled": true
  }

アクセストークン: {app_id}|{app_secret} 形式のアプリトークンを使用
日本は「全広告」対象国のためcommercial広告も取得可能
"""
import json
import logging
import threading
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent.parent
_GRAPH_VERSION = 'v21.0'
_BASE_URL = f'https://graph.facebook.com/{_GRAPH_VERSION}/ads_archive'

# レート制限: app tokenは200call/hour相当 → 保守的に3秒間隔
_LAST_CALL_TIME = 0.0
_MIN_INTERVAL = 3.0
_THROTTLE_LOCK = threading.Lock()  # HOLE 2 修正: スレッドセーフなスロットル

# 連続エラーキャッシュ（全失敗後30分スキップ）
_STATE_FILE = BASE_DIR / 'logs' / '.meta_api_state.json'
_ALL_FAILED_SKIP_SECONDS = 30 * 60

# HOLE 3 修正: ブロックドメインをメモリキャッシュ（config.json を毎 URL 読まない）
_BLOCKED_DOMAINS_CACHE: list[str] = []
_BLOCKED_DOMAINS_LOADED_AT: float = 0.0
_BLOCKED_DOMAINS_TTL: float = 300.0  # 5 分ごとに再読込
_BLOCKED_DOMAINS_LOCK = threading.Lock()


def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(state), encoding='utf-8')
        tmp.replace(_STATE_FILE)
    except Exception:
        pass


def _is_failed_cached() -> bool:
    state = _load_state()
    skip_until = state.get('skip_until', 0)
    return time.time() < skip_until


def _set_failed_cache() -> None:
    state = _load_state()
    state['skip_until'] = time.time() + _ALL_FAILED_SKIP_SECONDS
    _save_state(state)
    logging.info(f'[MetaAPI] 失敗キャッシュ: {_ALL_FAILED_SKIP_SECONDS//60}分間スキップ')


def _load_config() -> dict | None:
    """config.jsonからmeta_api設定を読む"""
    try:
        cfg_path = BASE_DIR / 'config.json'
        cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
        meta_cfg = cfg.get('meta_api', {})
        if not meta_cfg.get('enabled', False):
            return None
        if not meta_cfg.get('app_id') or not meta_cfg.get('app_secret'):
            return None
        return meta_cfg
    except Exception:
        return None


def _get_app_token(app_id: str, app_secret: str) -> str:
    return f'{app_id}|{app_secret}'


def _throttle() -> None:
    """スレッドセーフなレート制限（3 秒間隔）。"""
    global _LAST_CALL_TIME
    with _THROTTLE_LOCK:
        elapsed = time.time() - _LAST_CALL_TIME
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        _LAST_CALL_TIME = time.time()


def _get_blocked_domains() -> list[str]:
    """ブロックドメインリストをメモリキャッシュから返す（5 分 TTL）。"""
    global _BLOCKED_DOMAINS_CACHE, _BLOCKED_DOMAINS_LOADED_AT
    now = time.time()
    with _BLOCKED_DOMAINS_LOCK:
        if now - _BLOCKED_DOMAINS_LOADED_AT < _BLOCKED_DOMAINS_TTL:
            return _BLOCKED_DOMAINS_CACHE
        try:
            cfg_path = BASE_DIR / 'config.json'
            cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
            _BLOCKED_DOMAINS_CACHE = cfg.get('filters', {}).get('blocked_domains', [])
        except Exception:
            pass
        _BLOCKED_DOMAINS_LOADED_AT = now
        return _BLOCKED_DOMAINS_CACHE


def scrape_meta_via_api(keyword: str) -> list[dict] | None:
    """
    Facebook Ad Library APIでキーワード検索し、広告主のLP URLとpage_nameを返す。

    Returns:
        list[dict]: {'url': str, 'page_name': str|None} のリスト（0件の場合も空リストを返す）
        None: API未設定またはスキップ中
    """
    cfg = _load_config()
    if not cfg:
        return None

    if _is_failed_cached():
        return None

    token = _get_app_token(cfg['app_id'], cfg['app_secret'])

    params = {
        'access_token': token,
        'search_terms': keyword,
        'ad_reached_countries': ['JP'],
        'ad_active_status': 'ACTIVE',
        'ad_type': 'ALL',
        'fields': ','.join([
            'page_name',
            'ad_creative_link_urls',
            'ad_creative_bodies',
            'ad_creative_link_titles',
            'publisher_platforms',
        ]),
        'limit': 20,
    }

    _throttle()

    try:
        resp = requests.get(_BASE_URL, params=params, timeout=20)

        if resp.status_code == 401 or resp.status_code == 403:
            logging.warning(f'[MetaAPI] 認証エラー {resp.status_code}: App SecretまたはTokenを確認')
            _set_failed_cache()
            return None

        if resp.status_code == 429:
            logging.warning('[MetaAPI] レート制限 → 30分スキップ')
            _set_failed_cache()
            return None

        if not resp.ok:
            logging.warning(f'[MetaAPI] HTTP {resp.status_code}: {resp.text[:200]}')
            return None

        data = resp.json()

        if 'error' in data:
            err = data['error']
            code = err.get('code', 0)
            msg = err.get('message', '')
            # 190: invalid token, 200: permission denied
            if code in (190, 200):
                logging.warning(f'[MetaAPI] APIエラー code={code}: {msg}')
                _set_failed_cache()
                return None
            logging.warning(f'[MetaAPI] APIエラー: {msg}')
            return None

        ads = data.get('data', [])
        raw_items: list[tuple[str, str | None]] = []  # (url, page_name)

        for ad in ads:
            page_name = ad.get('page_name') or None
            link_urls = ad.get('ad_creative_link_urls') or []
            for u in link_urls:
                if u and u.startswith('http') and not _is_blocked(u):
                    raw_items.append((u, page_name))

        # 重複除去・ドメイン正規化
        seen_domains: set[str] = set()
        deduped: list[dict] = []
        for u, pn in raw_items:
            from urllib.parse import urlparse
            domain = urlparse(u).netloc
            if domain not in seen_domains:
                seen_domains.add(domain)
                deduped.append({'url': u, 'page_name': pn})

        if deduped:
            logging.info(f'[MetaAPI] {len(deduped)}件取得: "{keyword}"')
        else:
            logging.debug(f'[MetaAPI] 広告0件: "{keyword}"')

        return deduped

    except requests.Timeout:
        logging.warning('[MetaAPI] タイムアウト')
        return None
    except Exception as e:
        logging.warning(f'[MetaAPI] エラー: {e}')
        return None


def _is_blocked(url: str) -> bool:
    """ブロック対象ドメインかチェック（メモリキャッシュ使用）。"""
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        blocked = _get_blocked_domains()
        return any(b in domain for b in blocked)
    except Exception:
        return False


def test_api_access() -> bool:
    """API接続テスト（起動時に呼ぶ）"""
    cfg = _load_config()
    if not cfg:
        logging.info('[MetaAPI] 設定なし（スキップ）')
        return False

    token = _get_app_token(cfg['app_id'], cfg['app_secret'])
    try:
        resp = requests.get(
            _BASE_URL,
            params={
                'access_token': token,
                'search_terms': 'テスト',
                'ad_reached_countries': ['JP'],
                'ad_active_status': 'ACTIVE',
                'fields': 'page_name',
                'limit': 1,
            },
            timeout=15,
        )
        data = resp.json()
        if 'error' in data:
            err = data['error']
            logging.warning(f'[MetaAPI] テスト失敗: {err.get("message", "")}')
            return False
        logging.info('[MetaAPI] API接続OK ✓')
        return True
    except Exception as e:
        logging.warning(f'[MetaAPI] テスト失敗: {e}')
        return False
