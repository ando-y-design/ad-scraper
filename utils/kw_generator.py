"""
キーワード自動生成モジュール
リザーブKWが枯渇したときにClaude APIで新KWを生成してDBに追加する
"""
import json
import logging
import sqlite3

import anthropic

from state import config


def _get_existing_keywords(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute('SELECT keyword FROM keywords').fetchall()
    return {r[0] for r in rows}


def _build_prompt(source: str, existing: set[str], count: int) -> str:
    if source == 'meta':
        context = (
            "Meta（Instagram/Facebook）広告に出稿している企業を見つけるための検索キーワードを生成してください。"
            "BtoC向け高単価サービス（美容・健康・通販・金融・不動産・スクール等）を中心に。"
        )
    else:
        context = (
            "Google/Yahoo検索広告に出稿している企業を見つけるための検索キーワードを生成してください。"
            "広告費を使っているBtoB・BtoC企業（転職・不動産・士業・IT・医療・建設等）を中心に。"
            "テレアポ営業で決裁者に繋がりやすい業種を優先してください。"
        )

    existing_sample = list(existing)[:50]
    return f"""{context}

条件:
- 日本語のキーワード（1〜4単語）
- 実際に検索広告が多く出る競合キーワード
- 以下の既存キーワードと被らないこと（重複NG）: {json.dumps(existing_sample, ensure_ascii=False)}
- {count}個生成すること

出力形式（JSONのみ、説明不要）:
["KW1", "KW2", "KW3", ...]"""


def generate_keywords(conn: sqlite3.Connection, source: str, count: int = 50) -> int:
    """
    Claude APIで新キーワードを生成してDBに追加する。
    追加件数を返す（0=失敗）。
    """
    api_key = config.get('anthropic_api_key', '')
    if not api_key:
        logging.warning('[KWGen] anthropic_api_key が未設定。自動生成スキップ')
        return 0

    existing = _get_existing_keywords(conn)
    prompt = _build_prompt(source, existing, count)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=2048,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = message.content[0].text.strip()

        # JSON部分を抽出
        start = raw.find('[')
        end = raw.rfind(']') + 1
        if start == -1 or end == 0:
            logging.error(f'[KWGen] JSONが見つからない: {raw[:200]}')
            return 0

        keywords: list[str] = json.loads(raw[start:end])
    except Exception as e:
        logging.error(f'[KWGen] Claude API エラー: {e}')
        return 0

    db_source = 'meta' if source == 'meta' else 'auto_expanded'
    added = 0
    for kw in keywords:
        kw = kw.strip()
        if not kw or kw in existing:
            continue
        try:
            conn.execute(
                'INSERT OR IGNORE INTO keywords (keyword, source, is_archived) VALUES (?, ?, 0)',
                (kw, db_source),
            )
            existing.add(kw)
            added += 1
        except Exception:
            pass

    if added:
        conn.commit()
        logging.info(f'[KWGen] Claude生成キーワード追加: {added}件 ({db_source})')

    return added
