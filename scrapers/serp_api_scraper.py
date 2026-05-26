import base64
import json
import logging
import time
from pathlib import Path
from urllib.parse import quote

import requests

BASE_DIR = Path(__file__).parent.parent
_STATE_FILE = BASE_DIR / 'logs' / '.serp_api_state.json'

PROVIDERS = {
    'serpapi': {
        'url': 'https://serpapi.com/search.json',
        'params': lambda key, kw, loc: {
            'engine': 'google', 'q': kw, 'gl': 'jp', 'hl': 'ja',
            'api_key': key, 'num': 10,
            **({'location': loc} if loc else {}),
        },
        'extract': lambda data: [
            ad.get('link') or ad.get('url', '')
            for ad in data.get('ads', [])
        ],
    },
    'valueserp': {
        'url': 'https://api.valueserp.com/search',
        'params': lambda key, kw, loc: {
            'api_key': key, 'q': kw, 'gl': 'jp', 'hl': 'ja',
            'location': loc or 'Japan', 'num': 10,
        },
        'extract': lambda data: [
            ad.get('link') or ad.get('url', '')
            for ad in data.get('ads', [])
        ],
    },
    'zenserp': {
        'url': 'https://app.zenserp.com/api/v2/search',
        'params': lambda key, kw, loc: {
            'apikey': key, 'q': kw, 'gl': 'jp', 'hl': 'ja',
            'location': loc or 'Japan', 'num': 10,
        },
        'extract': lambda data: [
            ad.get('url') or ad.get('link', '')
            for ad in data.get('ads', [])
        ],
    },
    'serpstack': {
        'url': 'http://api.serpstack.com/search',
        'params': lambda key, kw, loc: {
            'access_key': key, 'query': kw, 'gl': 'jp', 'hl': 'ja', 'num': 10,
        },
        'extract': lambda data: [
            ad.get('url') or ad.get('link', '')
            for ad in (
                data.get('ads') if isinstance(data.get('ads'), list)
                else (data.get('ads') or {}).get('results', [])
            )
        ],
    },
    'scaleserp': {
        'url': 'https://api.scaleserp.com/search',
        'params': lambda key, kw, loc: {
            'api_key': key, 'q': kw, 'gl': 'jp', 'hl': 'ja',
            'num': 10, 'include_fields': 'ads',
            **({'location': loc} if loc else {}),
        },
        'extract': lambda data: [
            ad.get('link') or ad.get('url', '')
            for ad in data.get('ads', [])
        ],
    },
    # hasdata: x-api-key ヘッダー認証 + includeAds パラメータが必要
    # PROVIDERS に含めず、provider=='hasdata' 判定で専用関数に分岐する
    'serphouse': {
        'url': 'https://api.serphouse.com/serp/live',
        'params': lambda key, kw, loc: {
            'api_token': key, 'q': kw, 'country': 'jp', 'lang': 'ja',
            'num': 10, 'engine': 'google',
            **({'loc': loc} if loc else {}),
        },
        'extract': lambda data: [
            ad.get('link') or ad.get('url', '')
            for ad in data.get('results', {}).get('results', {}).get('ads', [])
        ],
    },
}


_ALL_FAILED_SKIP_SECONDS = 10 * 60  # 全キー失敗後10分はAPI呼び出しをスキップ (旧: 30分)


def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text())
    except Exception:
        pass
    return {'current_index': 0}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state))
    except Exception:
        pass


def _is_all_failed_cached() -> bool:
    """全キー失敗のキャッシュが有効かどうかを返す（30分間スキップ）"""
    state = _load_state()
    skip_until = state.get('all_failed_until', 0)
    return time.time() < skip_until


def is_serp_all_failed() -> bool:
    """外部から呼ぶ用: 全SerpAPIキーが失敗キャッシュ中か確認する"""
    return _is_all_failed_cached()


def _set_all_failed_cache() -> None:
    """全キー失敗をキャッシュする（30分後まで）"""
    state = _load_state()
    state['all_failed_until'] = time.time() + _ALL_FAILED_SKIP_SECONDS
    _save_state(state)
    logging.info(f'[SerpAPI] 全キー失敗をキャッシュ: {_ALL_FAILED_SKIP_SECONDS//60}分間スキップ')


def _load_config_keys() -> list[dict]:
    """config.json の serp_apis リストを読む"""
    try:
        cfg_path = BASE_DIR / 'config.json'
        cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
        return [e for e in cfg.get('serp_apis', []) if e.get('key')]
    except Exception:
        return []


def scrape_google_via_api(keyword: str, location: str | None = None) -> list[str] | None:
    """
    SERP APIを使ってGoogle広告LPを取得する。
    複数キーをラウンドロビンでローテーション。
    location: 都市名 (例: "Tokyo,Japan") を指定すると位置情報を反映した結果を取得。

    Returns:
        list[str]: 取得したLP URLリスト
        None: 有効なAPIキーが未設定
    """
    keys = _load_config_keys()
    if not keys:
        return None

    # 全キー失敗キャッシュ中は即スキップ（30分間）
    if _is_all_failed_cached():
        return None

    state = _load_state()
    idx = state.get('current_index', 0) % len(keys)

    # 全キーを1周試す
    for attempt in range(len(keys)):
        entry = keys[(idx + attempt) % len(keys)]
        provider = entry.get('provider', 'serpapi')
        key = entry.get('key', '')

        try:
            # HasData は x-api-key ヘッダー認証（PROVIDERS外で処理）
            if provider == 'hasdata':
                urls = _scrape_hasdata(key, keyword, location)
                if urls is not None:
                    next_idx = (idx + attempt + 1) % len(keys)
                    _save_state({'current_index': next_idx})
                    return urls
                continue

            # DataForSEO は POST + Basic 認証（PROVIDERS外で処理）
            if provider == 'dataforseo':
                urls = _scrape_dataforseo(key, keyword, location)
                if urls is not None:
                    next_idx = (idx + attempt + 1) % len(keys)
                    _save_state({'current_index': next_idx})
                    return urls
                continue

            # 汎用プロバイダー（serpapi/valueserp/zenserp/serpstack/scaleserp等）
            spec = PROVIDERS.get(provider)
            if not spec:
                logging.warning(f'[SerpAPI] 未知のプロバイダー: {provider}')
                continue

            resp = requests.get(
                spec['url'],
                params=spec['params'](key, keyword, location),
                timeout=15,
            )

            if resp.status_code == 401 or resp.status_code == 403:
                logging.warning(f'[SerpAPI] 認証エラー ({provider}): キーを次に切り替え')
                continue

            if resp.status_code == 429:
                logging.warning(f'[SerpAPI] レート超過 ({provider}): キーを次に切り替え')
                continue

            if not resp.ok:
                logging.warning(f'[SerpAPI] HTTP {resp.status_code} ({provider})')
                continue

            data = resp.json()

            # API エラー応答チェック（200 OK でもエラーフィールドがある場合がある）
            # serpapi: {"error": "..."} / valueserp: {"request_info": {"success": false}}
            # zenserp: {"error": "..."} など
            api_error = (
                data.get('error')
                or data.get('message')
                or (not data.get('request_info', {}).get('success', True)
                    and data.get('request_info', {}).get('message', ''))
            )
            if api_error:
                err_str = str(api_error)[:120]
                logging.warning(f'[SerpAPI] API エラー応答 ({provider}): {err_str} → 次キーへ')
                continue

            urls = [u for u in spec['extract'](data) if u and u.startswith('http') and 'google' not in u]

            # 成功 → 次回は次のキーから開始（ローテーション）
            next_idx = (idx + attempt + 1) % len(keys)
            _save_state({'current_index': next_idx})

            if urls:
                logging.info(f'[SerpAPI] {len(urls)}件取得 ({provider}): "{keyword}"')
            else:
                logging.info(f'[SerpAPI] 広告0件 ({provider}): "{keyword}"')

            return urls

        except requests.Timeout:
            logging.warning(f'[SerpAPI] タイムアウト ({provider})')
        except Exception as e:
            logging.warning(f'[SerpAPI] エラー ({provider}): {e}')

    logging.error('[SerpAPI] 全キーで失敗 → Playwrightにフォールバック')
    _set_all_failed_cache()  # 30分間スキップキャッシュをセット
    return None


def _scrape_dataforseo(key: str, keyword: str, location: str | None) -> list[str] | None:
    """
    DataForSEO Google Paid Ads エンドポイント。
    key フォーマット: "login:password"（コロン区切り）
    config.json 例:
      {"provider": "dataforseo", "key": "your@email.com:your_password"}
    """
    try:
        login, _, password = key.partition(':')
        if not login or not password:
            logging.warning('[DataForSEO] keyフォーマット不正 (login:password が必要)')
            return None

        cred = base64.b64encode(f'{login}:{password}'.encode()).decode()
        headers = {
            'Authorization': f'Basic {cred}',
            'Content-Type': 'application/json',
        }

        # Google Paid Ads (スポンサーリンク) 専用エンドポイント
        payload = [{
            'keyword': keyword,
            'language_code': 'ja',
            'location_code': 1009294,  # Japan
            'device': 'desktop',
        }]
        if location:
            # Tokyo=1028853, Osaka=1009091, etc. — location は文字列のみ使用
            payload[0]['location_name'] = location

        resp = requests.post(
            'https://api.dataforseo.com/v3/serp/google/paid/live/advanced',
            headers=headers,
            json=payload,
            timeout=20,
        )

        if resp.status_code in (401, 403):
            logging.warning('[DataForSEO] 認証エラー')
            return None
        if not resp.ok:
            logging.warning(f'[DataForSEO] HTTP {resp.status_code}')
            return None

        data = resp.json()
        urls = []
        for task in data.get('tasks', []):
            for result in task.get('result', []) or []:
                for item in result.get('items', []) or []:
                    if item.get('type') == 'paid':
                        url = item.get('url') or item.get('domain', '')
                        if url and url.startswith('http') and 'google' not in url:
                            urls.append(url)

        if urls:
            logging.info(f'[DataForSEO] {len(urls)}件取得: "{keyword}"')
        else:
            logging.info(f'[DataForSEO] 広告0件: "{keyword}"')
        return urls

    except requests.Timeout:
        logging.warning('[DataForSEO] タイムアウト')
        return None
    except Exception as e:
        logging.warning(f'[DataForSEO] エラー: {e}')
        return None


def _scrape_hasdata(key: str, keyword: str, location: str | None) -> list[str] | None:
    """
    HasData Google SERP API（x-api-key ヘッダー認証）。
    HasDataのJSONパーサーは広告抽出が不安定なため、
    HTMLを直接取得して /aclk リンクを自前でパースする方式。
    """
    try:
        from urllib.parse import parse_qs, unquote, urlparse
        from html.parser import HTMLParser

        headers = {'x-api-key': key}
        params = {
            'q': keyword,
            'gl': 'jp',
            'hl': 'ja',
            'num': 10,
            'domain': 'google.co.jp',   # 日本向けドメイン
            'deviceType': 'desktop',
        }
        if location:
            params['location'] = location

        resp = requests.get(
            'https://api.hasdata.com/scrape/google/serp',
            headers=headers,
            params=params,
            timeout=20,
        )

        if resp.status_code in (401, 403):
            logging.warning('[HasData] 認証エラー: APIキーを確認してください')
            return None
        if not resp.ok:
            logging.warning(f'[HasData] HTTP {resp.status_code}')
            return None

        data = resp.json()

        # HasDataのHTML（生Google HTML）をダウンロードして自前でパース
        html_url = data.get('requestMetadata', {}).get('html', '')
        html_urls: list[str] = []
        if html_url:
            try:
                html_resp = requests.get(html_url, timeout=15)
                html = html_resp.text

                # /aclk リンクを抽出（google_scraper.py と同じ手法）
                i = 0
                while True:
                    idx = html.find('/aclk', i)
                    if idx == -1:
                        break
                    # href="..." の前後を探す
                    start = html.rfind('href="', max(0, idx - 200), idx)
                    if start != -1:
                        start += 6
                        end = html.find('"', start)
                        if end != -1:
                            href = html[start:end].replace('&amp;', '&')
                            if '/aclk' in href:
                                lp = _extract_lp_from_aclk(href)
                                if lp and lp not in html_urls:
                                    html_urls.append(lp)
                    i = idx + 5

                if html_urls:
                    logging.info(f'[HasData] {len(html_urls)}件取得(HTML解析): "{keyword}"')
                    return html_urls
                else:
                    logging.debug(f'[HasData] HTML解析0件、JSONフォールバック試行: "{keyword}"')

            except Exception as html_e:
                logging.debug(f'[HasData] HTML解析失敗、JSONフォールバック: {html_e}')

        # JSON ads フィールドから抽出（HTMLが0件 or 取得不可の場合）
        # 複数フィールド名・複数リンクキーに対応
        json_ads: list = []
        for ads_key in ('ads', 'paidResults', 'searchAds', 'sponsored'):
            cand = data.get(ads_key, [])
            if isinstance(cand, list) and cand:
                json_ads = cand
                break

        json_urls: list[str] = []
        for a in json_ads:
            if not isinstance(a, dict):
                continue
            link = None
            for lk in ('link', 'url', 'trackedLink', 'displayLink', 'domain'):
                v = a.get(lk, '')
                if v and str(v).startswith('http') and 'google' not in str(v):
                    link = str(v)
                    break
            if link and link not in json_urls:
                json_urls.append(link)

        if json_urls:
            logging.info(f'[HasData] {len(json_urls)}件取得(JSON): "{keyword}"')
        else:
            # どちらも0件の場合、デバッグ用に実際のキーを記録
            top_keys = list(data.keys())[:8]
            logging.debug(f'[HasData] 広告0件: "{keyword}" | レスポンスキー: {top_keys}')
            logging.info(f'[HasData] 広告0件: "{keyword}"')
        return json_urls

    except requests.Timeout:
        logging.warning('[HasData] タイムアウト')
        return None
    except Exception as e:
        logging.warning(f'[HasData] エラー: {e}')
        return None


def _extract_lp_from_aclk(href: str) -> str | None:
    """Google /aclk トラッキングURLからランディングページURLを抽出する（HasData用）"""
    try:
        from urllib.parse import parse_qs, unquote, urlparse
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        for param in ('adurl', 'q', 'url'):
            val = params.get(param, [None])[0]
            if val and val.startswith('http') and 'google' not in val:
                return unquote(val)
    except Exception:
        pass
    return None
