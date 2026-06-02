from __future__ import annotations
"""
競合他社検索モジュール。
Claude AIを使って同業種・同エリアの競合他社名を取得する。
同じ（キーワード系統 × エリア）の組み合わせはキャッシュして重複API呼び出しを防ぐ。
"""
import json
import logging
import os
import re
from pathlib import Path

_cache: dict[tuple[str, str], list[str]] = {}

_CONFIG_PATH = Path(__file__).parent.parent / 'config.json'


def _get_api_key() -> str:
    # 環境変数優先、なければconfig.json
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if key:
        return key
    try:
        cfg = json.loads(_CONFIG_PATH.read_text(encoding='utf-8'))
        return cfg.get('anthropic_api_key', '')
    except Exception:
        return ''


def _cache_key(keyword: str, area_name: str | None) -> tuple[str, str]:
    """キャッシュキー: (キーワード先頭2語, エリア名)"""
    kw_short = ' '.join(keyword.split()[:2])
    return (kw_short, area_name or '')


def find_competitors_ai(
    keyword: str,
    area_name: str | None,
    limit: int = 3,
) -> list[str]:
    """
    Claude Haiku APIで同業種・同エリアの競合他社名を返す。

    - キャッシュ済みなら即返却（API呼び出しなし）
    - APIキー未設定なら空リスト
    - 失敗時も空リスト（メイン収集を止めない）
    """
    if not keyword:
        return []

    ck = _cache_key(keyword, area_name)
    if ck in _cache:
        return _cache[ck][:limit]

    api_key = _get_api_key()
    if not api_key or api_key == 'YOUR_ANTHROPIC_API_KEY':
        _cache[ck] = []
        return []

    area = area_name or '全国'
    prompt = (
        f'テレアポ営業の事前調査として、競合他社情報を教えてください。\n'
        f'キーワード: {keyword}\n'
        f'エリア: {area}\n\n'
        f'このキーワードに関連する業種で、{area}に実在する競合企業・店舗・クリニック等を'
        f'最大{limit}つ挙げてください。\n'
        f'条件: ① 実在が確実なもののみ ② 架空・不確かなものは含めない ③ 会社名・店舗名のみ（説明不要）\n'
        f'JSON形式のみで返答してください: {{"competitors": ["名前1", "名前2"]}}'
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model='claude-haiku-20240307',
            max_tokens=200,
            messages=[{'role': 'user', 'content': prompt}],
        )
        text = response.content[0].text.strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            _cache[ck] = []
            return []

        data = json.loads(m.group())
        results = [str(c).strip() for c in data.get('competitors', []) if c][:limit]
        _cache[ck] = results
        logging.debug(f'[競合AI] {keyword}/{area} → {results}')
        return results

    except Exception as e:
        logging.debug(f'[競合AI] エラー: {e}')
        _cache[ck] = []
        return []
