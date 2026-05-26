import json, requests, sys

cfg = json.load(open('config.json', encoding='utf-8'))
keys = cfg.get('serp_apis', [])

PROVIDERS = {
    'serpapi': {
        'url': 'https://serpapi.com/search.json',
        'params': lambda k: {'engine': 'google', 'q': 'テスト', 'gl': 'jp', 'hl': 'ja', 'api_key': k, 'num': 3},
    },
    'valueserp': {
        'url': 'https://api.valueserp.com/search',
        'params': lambda k: {'api_key': k, 'q': 'テスト', 'gl': 'jp', 'hl': 'ja', 'location': 'Japan', 'num': 3},
    },
    'zenserp': {
        'url': 'https://app.zenserp.com/api/v2/search',
        'params': lambda k: {'apikey': k, 'q': 'テスト', 'gl': 'jp', 'hl': 'ja', 'num': 3},
    },
    'serpstack': {
        'url': 'http://api.serpstack.com/search',
        'params': lambda k: {'access_key': k, 'query': 'テスト', 'gl': 'jp', 'hl': 'ja', 'num': 3},
    },
}

for entry in keys:
    provider = entry.get('provider', 'serpapi')
    key = entry.get('key', '')
    spec = PROVIDERS.get(provider)
    if not spec:
        print(f'[{provider}] 未知のプロバイダー')
        continue
    try:
        resp = requests.get(spec['url'], params=spec['params'](key), timeout=10)
        body = resp.json() if resp.headers.get('content-type', '').startswith('application/json') else {}
        ads = body.get('ads', [])
        print(f'[{provider}] HTTP {resp.status_code} | ads={len(ads)} | error={body.get("error") or body.get("message") or ""}')
    except Exception as e:
        print(f'[{provider}] 例外: {e}')
