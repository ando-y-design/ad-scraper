from __future__ import annotations
"""
db_writer.py — SQLite書き込みモジュール

改善点:
  - phone_source カラム追加（整合率トラッキング用）
  - tokutei_cache テーブル追加（同ドメインの特商法URLを72hキャッシュ）
  - ブロックドメインリストはcompany_finderと共有
"""

import sqlite3
import threading
from datetime import date, datetime
from utils.logger import get_logger
from processors.normalizer import normalize_base_url
from processors.rank_calculator import calc_rank

log = get_logger("db_writer")
_lock = threading.Lock()
DB_PATH = "companies.db"


def init_db(path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name    TEXT,
            normalized_name TEXT,
            lp_url          TEXT,
            base_url        TEXT UNIQUE,
            phone           TEXT,
            phones          TEXT,
            phone_source    TEXT,
            ad_sources      TEXT,
            keyword         TEXT,
            area_name       TEXT,
            found_date      TEXT,
            rank            TEXT DEFAULT 'C',
            seen_count      INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS keywords (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword         TEXT UNIQUE,
            source          TEXT DEFAULT 'config',
            last_searched   TEXT,
            is_archived     INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS keyword_area_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword       TEXT,
            area_name     TEXT,
            source        TEXT,
            last_searched TEXT,
            UNIQUE(keyword, area_name, source)
        );

        CREATE TABLE IF NOT EXISTS tokutei_cache (
            base_url      TEXT PRIMARY KEY,
            tokutei_url   TEXT,
            cached_at     TEXT
        );
        """)
        conn.commit()

        # 既存DBへのマイグレーション
        for col, typ, default in [("rank", "TEXT", "'C'"), ("seen_count", "INTEGER", "1")]:
            try:
                conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {typ} DEFAULT {default}")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # カラムが既に存在する場合はスキップ


# ── キーワード管理 ─────────────────────────────────────────────────────────

def upsert_keywords(keywords: list[str], source: str = "config") -> None:
    with sqlite3.connect(DB_PATH) as conn:
        for kw in keywords:
            conn.execute(
                "INSERT OR IGNORE INTO keywords (keyword, source) VALUES (?, ?)",
                (kw, source),
            )
        conn.commit()


def get_next_keyword(
    source: str = "yahoo", cooling_hours: int = 24, area_name: str = "東京"
) -> str | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT k.keyword FROM keywords k
            LEFT JOIN keyword_area_log l
                ON k.keyword = l.keyword AND l.area_name = ? AND l.source = ?
            WHERE k.is_archived = 0
              AND (l.last_searched IS NULL
                   OR datetime(l.last_searched, ? || ' hours') < datetime('now'))
            ORDER BY COALESCE(l.last_searched, '1970-01-01') ASC
            LIMIT 1
            """,
            (area_name, source, str(cooling_hours)),
        ).fetchone()
        return row[0] if row else None


def mark_keyword_searched(keyword: str, source: str, area_name: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO keyword_area_log (keyword, area_name, source, last_searched)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(keyword, area_name, source)
            DO UPDATE SET last_searched = datetime('now')
            """,
            (keyword, area_name, source),
        )
        conn.execute(
            "UPDATE keywords SET last_searched = datetime('now') WHERE keyword = ?",
            (keyword,),
        )
        conn.commit()


def count_active_keywords() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM keywords WHERE is_archived = 0"
        ).fetchone()
        return row[0] if row else 0


def restore_archived_keywords() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("UPDATE keywords SET is_archived = 0 WHERE is_archived = 1")
        conn.commit()
        return cur.rowcount


# ── 特商法URLキャッシュ ────────────────────────────────────────────────────

def get_cached_tokutei_url(lp_url: str, ttl_hours: int = 72) -> str | None:
    base = normalize_base_url(lp_url)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT tokutei_url FROM tokutei_cache
            WHERE base_url = ?
              AND datetime(cached_at, ? || ' hours') > datetime('now')
            """,
            (base, str(ttl_hours)),
        ).fetchone()
        return row[0] if row else None


def set_tokutei_cache(lp_url: str, tokutei_url: str) -> None:
    base = normalize_base_url(lp_url)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO tokutei_cache (base_url, tokutei_url, cached_at)
            VALUES (?, ?, datetime('now'))
            """,
            (base, tokutei_url),
        )
        conn.commit()


# ── 会社データ書き込み ─────────────────────────────────────────────────────

def insert_company(data: dict) -> bool:
    base_url = normalize_base_url(data.get("lp_url", ""))
    if not base_url:
        return False
    phone = data.get("phone", "")
    normalized_name = data.get("normalized_name", "")
    with _lock:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                # 電話番号重複チェック（同じ番号 = 同じ会社）
                if phone:
                    existing = conn.execute(
                        "SELECT 1 FROM companies WHERE phone = ? LIMIT 1", (phone,)
                    ).fetchone()
                    if existing:
                        return False  # 電話番号重複
                # 会社名重複チェック（同じ正規化名 = 同じ会社が別URLで出ている）
                if normalized_name:
                    existing = conn.execute(
                        "SELECT 1 FROM companies WHERE normalized_name = ? LIMIT 1",
                        (normalized_name,)
                    ).fetchone()
                    if existing:
                        return False  # 会社名重複
                ad_source = data.get("ad_source", "")
                initial_rank = calc_rank(1, ad_source)
                conn.execute(
                    """
                    INSERT INTO companies
                        (company_name, normalized_name, lp_url, base_url,
                         phone, phones, phone_source, ad_sources,
                         keyword, area_name, found_date, rank, seen_count)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        data.get("company_name", ""),
                        data.get("normalized_name", ""),
                        data.get("lp_url", ""),
                        base_url,
                        phone,
                        ",".join(data.get("phones", [])),
                        data.get("phone_source", ""),
                        ad_source,
                        data.get("keyword", ""),
                        data.get("area_name", ""),
                        datetime.now().strftime("%Y-%m-%d %H:%M"),
                        initial_rank,
                        1,
                    ),
                )
                conn.commit()
                return True
        except sqlite3.IntegrityError:
            return False  # base_url 重複


def is_duplicate(lp_url: str, phone: str = "", normalized_name: str = "") -> bool:
    base_url = normalize_base_url(lp_url)
    with sqlite3.connect(DB_PATH) as conn:
        if conn.execute(
            "SELECT 1 FROM companies WHERE base_url = ? LIMIT 1", (base_url,)
        ).fetchone():
            return True
        if phone and conn.execute(
            "SELECT 1 FROM companies WHERE phone = ? LIMIT 1", (phone,)
        ).fetchone():
            return True
        if normalized_name and conn.execute(
            "SELECT 1 FROM companies WHERE normalized_name = ? LIMIT 1", (normalized_name,)
        ).fetchone():
            return True
        return False


def update_company_seen(lp_url: str, phone: str, ad_source: str) -> str | None:
    """重複検出時にseen_countとad_sourcesを更新し、新しいrankを返す"""
    base_url = normalize_base_url(lp_url)
    with _lock:
        with sqlite3.connect(DB_PATH) as conn:
            row = None
            if phone:
                row = conn.execute(
                    "SELECT id, seen_count, ad_sources FROM companies WHERE phone=? LIMIT 1",
                    (phone,),
                ).fetchone()
            if not row and base_url:
                row = conn.execute(
                    "SELECT id, seen_count, ad_sources FROM companies WHERE base_url=? LIMIT 1",
                    (base_url,),
                ).fetchone()
            if not row:
                return None
            row_id, seen_count, existing_sources = row
            seen_count = (seen_count or 1) + 1
            srcs = set((existing_sources or "").split(",")) - {""}
            if ad_source:
                srcs.add(ad_source)
            new_sources = ",".join(sorted(srcs))
            new_rank = calc_rank(seen_count, new_sources)
            conn.execute(
                "UPDATE companies SET seen_count=?, ad_sources=?, rank=? WHERE id=?",
                (seen_count, new_sources, new_rank, row_id),
            )
            conn.commit()
            return new_rank


def clean_junk(max_age_days: int = 90) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "DELETE FROM companies "
            "WHERE company_name = '' AND phone = '' "
            "AND found_date < date('now', ? || ' days')",
            (f"-{max_age_days}",),
        )
        conn.commit()
        return cur.rowcount


# ── 整合率トラッキング ─────────────────────────────────────────────────────

def alignment_stats() -> dict:
    """整合率診断用の統計を返す"""
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        blank_name = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE company_name = ''"
        ).fetchone()[0]
        blank_phone = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE phone = ''"
        ).fetchone()[0]
        from_tokutei = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE phone_source LIKE 'tokutei%'"
        ).fetchone()[0]
    return {
        "total": total,
        "blank_name_rate": blank_name / total if total else 0,
        "blank_phone_rate": blank_phone / total if total else 0,
        "tokutei_phone_rate": from_tokutei / total if total else 0,
    }
