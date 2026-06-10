from __future__ import annotations
from typing import Optional
import logging
import random
from datetime import datetime, timedelta

from utils.keyword_data import (
    KEYWORD_GROUPS,
    GOOGLE_YAHOO_KEYWORDS,
    META_KEYWORDS,
    RESERVE_GOOGLE_YAHOO_KEYWORDS,
    RESERVE_META_KEYWORDS,
)

__all__ = [
    'GOOGLE_YAHOO_KEYWORDS', 'META_KEYWORDS',
    'RESERVE_GOOGLE_YAHOO_KEYWORDS', 'RESERVE_META_KEYWORDS',
    'auto_refill_if_low', 'init_keywords', 'get_next_keyword',
    'get_next_keyword_with_area', 'update_keyword_area_searched',
    'update_keyword_searched', 'update_keyword_found',
    'archive_stale_keywords', 'add_keyword', 'archive_keyword',
]


def auto_refill_if_low(conn, source: str, cooling_hours: int = 0,
                       threshold: int = 20, batch_size: int = 50) -> int:
    """
    今すぐ検索可能なキーワード（非アーカイブ & 冷却期間外）が threshold 件未満のとき、
    リザーブから batch_size 件をランダムに追加する。
    追加件数を返す（0 = 補充不要または補充できるものがなかった）。
    """
    sources = ('google_yahoo', 'auto_expanded') if source == 'google_yahoo' else ('meta',)
    placeholders = ','.join('?' for _ in sources)
    threshold_dt = (datetime.now() - timedelta(hours=cooling_hours)).isoformat()
    available = conn.execute(
        f'''SELECT COUNT(*) FROM keywords
            WHERE source IN ({placeholders}) AND is_archived = 0
              AND (last_searched IS NULL OR last_searched < ?)''',
        sources + (threshold_dt,),
    ).fetchone()[0]

    if available >= threshold:
        return 0

    reserve = RESERVE_GOOGLE_YAHOO_KEYWORDS if source != 'meta' else RESERVE_META_KEYWORDS
    existing = {row[0] for row in conn.execute('SELECT keyword FROM keywords').fetchall()}
    new_kws = [kw for kw in reserve if kw not in existing]

    if not new_kws:
        # リザーブ枯渇 → アーカイブ済みを古い順にリセット（Tier1に戻す）
        rows = conn.execute(
            f'''SELECT keyword FROM keywords
                WHERE source IN ({placeholders}) AND is_archived = 1
                ORDER BY last_searched ASC LIMIT ?''',
            sources + (batch_size,),
        ).fetchall()
        if not rows:
            return 0
        for (kw,) in rows:
            conn.execute('UPDATE keywords SET is_archived = 0, last_searched = NULL WHERE keyword = ?', (kw,))
        conn.commit()
        logging.info(f'[Refill] キーワードサイクルリセット: {len(rows)}件 ({source})')
        return len(rows)

    selected = random.sample(new_kws, min(batch_size, len(new_kws)))
    db_source = 'google_yahoo' if source != 'meta' else 'meta'
    added = 0
    for kw in selected:
        conn.execute(
            'INSERT OR IGNORE INTO keywords (keyword, source, is_archived) VALUES (?, ?, 0)',
            (kw, db_source),
        )
        added += 1
    conn.commit()
    logging.info(
        f'[Refill] リザーブからキーワード補充: {added}件 ({db_source}) '
        f'[残り在庫: {len(new_kws) - added}件]'
    )
    return added


def init_keywords(conn):
    try:
        from state import config
        assigned = config.get('assigned_industries', [])
    except Exception:
        assigned = []

    existing = {
        row[0]
        for row in conn.execute('SELECT keyword FROM keywords').fetchall()
    }

    added = 0
    for industry, data in KEYWORD_GROUPS.items():
        if assigned and industry not in assigned:
            continue
        for kw in data.get('google_yahoo', []):
            if kw not in existing:
                conn.execute(
                    'INSERT OR IGNORE INTO keywords (keyword, source) VALUES (?, ?)',
                    (kw, 'google_yahoo')
                )
                existing.add(kw)
                added += 1
        for kw in data.get('meta', []):
            if kw not in existing:
                conn.execute(
                    'INSERT OR IGNORE INTO keywords (keyword, source) VALUES (?, ?)',
                    (kw, 'meta')
                )
                existing.add(kw)
                added += 1

    conn.commit()
    label = f'業界フィルタ: {assigned}' if assigned else '全業界'
    logging.info(f'キーワード初期化: {added}件追加 ({label})')


def get_next_keyword(
    conn,
    source: str,
    cooling_hours: int,
    boost_patterns: Optional[list[str]] = None,
    boost_factor: float = 10.0,
) -> Optional[dict]:
    """
    利用可能キーワードから重み付きランダムで1件選ぶ。
    重み = sqrt(total_found + 1)  ← 実績の多いキーワードを優先しつつ偏りを抑える。
    未検索キーワードは total_found=0 だが weight=1 で必ず候補に入る。
    google_yahoo ソース要求時は auto_expanded キーワードも含める。

    boost_patterns: このパターンに部分一致するキーワードを boost_factor 倍に優先する。
    """
    threshold = (datetime.now() - timedelta(hours=cooling_hours)).isoformat()
    sources = (source, 'auto_expanded') if source == 'google_yahoo' else (source,)
    placeholders = ','.join('?' for _ in sources)
    rows = conn.execute(
        f'''
        SELECT keyword, source, total_found FROM keywords
        WHERE source IN ({placeholders})
          AND is_archived = 0
          AND (last_searched IS NULL OR last_searched < ?)
        ''',
        sources + (threshold,)
    ).fetchall()

    if not rows:
        return None

    def _weight(row):
        w = (row[2] + 1) ** 0.5
        if boost_patterns and any(p in row[0] for p in boost_patterns):
            w *= boost_factor
        return w

    weights = [_weight(r) for r in rows]
    total_w = sum(weights)
    r_val = random.random() * total_w
    cumulative = 0.0
    for row, w in zip(rows, weights):
        cumulative += w
        if r_val <= cumulative:
            return {'keyword': row[0], 'source': row[1]}
    return {'keyword': rows[-1][0], 'source': rows[-1][1]}


def get_next_keyword_with_area(
    conn,
    source: str,
    cooling_hours: int,
    areas: list[dict],
    boost_patterns: Optional[list[str]] = None,
    boost_factor: float = 10.0,
) -> Optional[dict]:
    """
    Returns {'keyword': str, 'area': Optional[dict]}.
    Each (keyword, area) pair has independent cooling via keyword_area_log.
    With 150 keywords × 10 areas = 1500 unique search slots before any cooldown.
    Falls back to area=None if no areas configured.
    """
    if not areas:
        result = get_next_keyword(conn, source, cooling_hours, boost_patterns, boost_factor)
        if result:
            result['area'] = None
        return result

    sources = (source, 'auto_expanded') if source == 'google_yahoo' else (source,)
    placeholders = ','.join('?' for _ in sources)
    rows = conn.execute(
        f'''
        SELECT keyword, total_found FROM keywords
        WHERE source IN ({placeholders})
          AND is_archived = 0
        ''',
        sources
    ).fetchall()

    if not rows:
        return None

    cooled: set[tuple[str, str]] = set()
    if cooling_hours > 0:
        threshold = (datetime.now() - timedelta(hours=cooling_hours)).isoformat()
        cooled_rows = conn.execute(
            'SELECT keyword, area_name FROM keyword_area_log WHERE last_searched > ?',
            (threshold,)
        ).fetchall()
        cooled = {(r[0], r[1]) for r in cooled_rows}

    candidates: list[tuple[str, dict, float]] = []
    for row in rows:
        kw, total_found = row[0], row[1]
        w = (total_found + 1) ** 0.5
        if boost_patterns and any(p in kw for p in boost_patterns):
            w *= boost_factor
        for area in areas:
            if (kw, area['name']) not in cooled:
                candidates.append((kw, area, w))

    if not candidates:
        return None

    total_w = sum(c[2] for c in candidates)
    r_val = random.random() * total_w
    cumulative = 0.0
    for kw, area, w in candidates:
        cumulative += w
        if r_val <= cumulative:
            return {'keyword': kw, 'area': area}
    kw, area, _ = candidates[-1]
    return {'keyword': kw, 'area': area}


def update_keyword_area_searched(conn, keyword: str, area_name: Optional[str]):
    """
    (keyword, area) ペアの冷却を記録。keywords.last_searched も更新して
    archive_stale_keywords との互換性を保つ。
    """
    now = datetime.now().isoformat()
    if area_name:
        conn.execute(
            '''
            INSERT INTO keyword_area_log (keyword, area_name, last_searched)
            VALUES (?, ?, ?)
            ON CONFLICT(keyword, area_name) DO UPDATE SET last_searched = excluded.last_searched
            ''',
            (keyword, area_name, now)
        )
    conn.execute(
        'UPDATE keywords SET last_searched = ? WHERE keyword = ?',
        (now, keyword)
    )
    conn.commit()


def update_keyword_searched(conn, keyword: str):
    conn.execute(
        'UPDATE keywords SET last_searched = ? WHERE keyword = ?',
        (datetime.now().isoformat(), keyword)
    )
    conn.commit()


def update_keyword_found(conn, keyword: str):
    conn.execute(
        '''
        UPDATE keywords
        SET last_new_company = ?,
            total_found = total_found + 1
        WHERE keyword = ?
        ''',
        (datetime.now().isoformat(), keyword)
    )
    conn.commit()


def archive_stale_keywords(conn, archive_days: int):
    threshold = (datetime.now() - timedelta(days=archive_days)).isoformat()
    result = conn.execute(
        '''
        UPDATE keywords
        SET is_archived = 1
        WHERE is_archived = 0
          AND last_searched IS NOT NULL
          AND (last_new_company IS NULL OR last_new_company < ?)
          AND last_searched < ?
        ''',
        (threshold, threshold)
    )
    if result.rowcount > 0:
        logging.info(f'キーワードアーカイブ: {result.rowcount}件')
    conn.commit()


def add_keyword(conn, keyword: str, source: str = 'google_yahoo'):
    conn.execute(
        '''
        INSERT OR REPLACE INTO keywords (keyword, source, is_archived)
        VALUES (?, ?, 0)
        ''',
        (keyword, source)
    )
    conn.commit()
    logging.info(f'キーワード追加: "{keyword}" ({source})')


def archive_keyword(conn, keyword: str):
    conn.execute(
        'UPDATE keywords SET is_archived = 1 WHERE keyword = ?',
        (keyword,)
    )
    conn.commit()
    logging.info(f'キーワードアーカイブ: "{keyword}"')
