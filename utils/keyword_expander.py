"""
キーワード自動拡張

あるキーワードが一定数（GEO_THRESHOLD件以上）の企業を見つけた場合、
地域バリアントを自動生成してDBに追加する。

制約:
  - 1キーワードあたり最大 MAX_PER_KEYWORD バリアント
  - 自動生成キーワードの総数は MAX_AUTO_TOTAL まで
  - meta ソースは地域バリアントを生成しない（B2Cは全国向けのため）
"""
import logging
import random
from datetime import datetime

GEO_THRESHOLD = 3       # この件数以上で拡張対象
MAX_PER_KEYWORD = 15    # 1キーワードあたりの最大拡張数（地域15都市フル展開）
MAX_AUTO_TOTAL = 10000  # 自動生成キーワードの上限

_GEO_SUFFIXES = [
    '東京', '大阪', '名古屋', '福岡', '横浜',
    '埼玉', '千葉', '神戸', '京都', '札幌',
    '仙台', '広島', '静岡', '新潟', '熊本',
]

_SYNONYM_MAP = {
    '業者': ['会社', '専門', '業者 おすすめ'],
    '会社': ['業者', '法人'],
    '支援': ['代行', 'サービス'],
    '代行': ['支援', 'サービス', 'アウトソーシング'],
    'サービス': ['会社', '業者'],
    'コンサルティング': ['コンサル', '支援'],
    '導入': ['活用', '利用'],
    '設置': ['工事', '取り付け'],
}


def _count_auto_keywords(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM keywords WHERE source='auto_expanded'"
    ).fetchone()[0]


def _already_expanded(conn, base_keyword: str) -> int:
    """このキーワードから生成済みの地域バリアント数を返す"""
    return conn.execute(
        "SELECT COUNT(*) FROM keywords WHERE keyword LIKE ? AND source='auto_expanded'",
        (base_keyword + ' %',)  # 地域バリアントは "keyword geo" 形式のためスペース必須
    ).fetchone()[0]


def _add_if_new(conn, keyword: str) -> bool:
    existing = conn.execute(
        'SELECT 1 FROM keywords WHERE keyword=?', (keyword,)
    ).fetchone()
    if existing:
        return False
    conn.execute(
        "INSERT OR IGNORE INTO keywords (keyword, source, is_archived) VALUES (?, 'auto_expanded', 0)",
        (keyword,)
    )
    conn.commit()
    return True


def maybe_expand_keyword(conn, keyword: str, source: str) -> None:
    """
    キーワードの成果数が閾値を超えた場合にバリアントを自動生成する。
    writer_workerで企業挿入成功後に呼ぶ。
    source は ad_sources (Yahoo/Google/Meta) または keyword.source を渡す。
    """
    if source.lower() == 'meta':
        return  # Metaは全国向けB2Cなので地域バリアント不要
    # Metaキーワード由来の成果も除外（キーワードテーブルで確認）
    kw_row = conn.execute('SELECT source FROM keywords WHERE keyword=?', (keyword,)).fetchone()
    if kw_row and kw_row[0] == 'meta':
        return

    try:
        # このキーワードの累計件数を確認
        row = conn.execute(
            'SELECT total_found FROM keywords WHERE keyword=?', (keyword,)
        ).fetchone()
        if not row or row[0] < GEO_THRESHOLD:
            return

        # 拡張上限チェック
        if _count_auto_keywords(conn) >= MAX_AUTO_TOTAL:
            return

        already = _already_expanded(conn, keyword)
        if already >= MAX_PER_KEYWORD:
            return

        remaining_slots = min(
            MAX_PER_KEYWORD - already,
            MAX_AUTO_TOTAL - _count_auto_keywords(conn),
        )
        if remaining_slots <= 0:
            return

        added = []

        # 地域バリアント（ランダム選択で偏りを防ぐ）
        geo_candidates = [f'{keyword} {geo}' for geo in random.sample(_GEO_SUFFIXES, len(_GEO_SUFFIXES))]
        for candidate in geo_candidates:
            if len(added) >= remaining_slots:
                break
            if _add_if_new(conn, candidate):
                added.append(candidate)

        # 地域バリアントで枠が余ればシノニム展開
        if len(added) < remaining_slots:
            for orig, synonyms in _SYNONYM_MAP.items():
                if orig in keyword:
                    for syn in synonyms:
                        if len(added) >= remaining_slots:
                            break
                        candidate = keyword.replace(orig, syn, 1)
                        if candidate != keyword and _add_if_new(conn, candidate):
                            added.append(candidate)

        if added:
            logging.info(
                f'[Expander] "{keyword}"({row[0]}件) からキーワード自動拡張: {added}'
            )

    except Exception as e:
        logging.debug(f'[Expander] エラー: {e}')
