from __future__ import annotations
from typing import Optional
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / 'companies.db'

_local = threading.local()


def get_connection() -> sqlite3.Connection:
    if not hasattr(_local, 'conn') or _local.conn is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA foreign_keys=ON')
        _local.conn = conn
        _register_thread_cleanup()
    return _local.conn


def _register_thread_cleanup() -> None:
    """スレッド終了時にDB接続をcloseするfinalizer登録。"""
    thread = threading.current_thread()
    if getattr(thread, '_db_cleanup_registered', False):
        return
    thread._db_cleanup_registered = True

    original_run = getattr(thread, 'run', None)
    if original_run is None:
        return

    def _patched_run():
        try:
            original_run()
        finally:
            conn = getattr(_local, 'conn', None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            _local.conn = None

    thread.run = _patched_run


def init_db() -> sqlite3.Connection:
    conn = get_connection()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS companies (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name     TEXT NOT NULL,
            normalized_name  TEXT UNIQUE NOT NULL,
            lp_url           TEXT,
            base_url         TEXT UNIQUE,
            phone            TEXT UNIQUE,
            phones           TEXT,
            phone_source     TEXT,
            industry         TEXT,
            ad_sources       TEXT,
            sheet_row        INTEGER,
            found_date       TEXT NOT NULL,
            keyword          TEXT,
            exported         INTEGER DEFAULT 0,
            contact_name     TEXT,
            lp_headline      TEXT,
            all_keywords     TEXT,
            area_name        TEXT,
            corporate_number TEXT,
            seen_count       INTEGER DEFAULT 1,
            rank             TEXT,
            nta_prefecture   TEXT,
            pref_match       TEXT,
            phone_confidence INTEGER
        );

        CREATE TABLE IF NOT EXISTS keywords (
            keyword          TEXT PRIMARY KEY,
            source           TEXT NOT NULL,
            last_searched    TEXT,
            last_new_company TEXT,
            total_found      INTEGER DEFAULT 0,
            is_archived      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS api_usage (
            date             TEXT PRIMARY KEY,
            nta_count        INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS keyword_area_log (
            keyword          TEXT NOT NULL,
            area_name        TEXT NOT NULL,
            last_searched    TEXT,
            PRIMARY KEY (keyword, area_name)
        );

        CREATE INDEX IF NOT EXISTS idx_normalized_name ON companies(normalized_name);
        CREATE INDEX IF NOT EXISTS idx_base_url ON companies(base_url);
        CREATE INDEX IF NOT EXISTS idx_phone ON companies(phone);
        CREATE INDEX IF NOT EXISTS idx_exported ON companies(exported);
    ''')
    conn.commit()

    # 既存DBへのマイグレーション（カラムが存在しない場合のみ追加）
    existing_cols = {row[1] for row in conn.execute('PRAGMA table_info(companies)').fetchall()}
    for col, typedef in [
        ('contact_name', 'TEXT'), ('lp_headline', 'TEXT'),
        ('all_keywords', 'TEXT'), ('area_name', 'TEXT'),
        ('corporate_number', 'TEXT'), ('phone_source', 'TEXT'),
        ('seen_count', 'INTEGER DEFAULT 1'), ('rank', 'TEXT'),
        ('nta_prefecture', 'TEXT'), ('pref_match', 'TEXT'),
        ('phone_confidence', 'INTEGER'), ('nta_retry_count', 'INTEGER DEFAULT 0'),
    ]:
        if col not in existing_cols:
            conn.execute(f'ALTER TABLE companies ADD COLUMN {col} {typedef}')
    conn.commit()

    # マイグレーション: phone列のUNIQUE制約を除去
    # 同一電話番号を複数会社が共有するケース（コールセンター/番号譲渡）で正しいペアが消える問題を解消
    _remove_phone_unique_if_needed(conn)

    return conn


def _remove_phone_unique_if_needed(conn: sqlite3.Connection):
    """phone列のUNIQUE制約が存在する場合、テーブルを再作成して除去する。
    SQLiteはALTER TABLE DROP CONSTRAINTをサポートしないため、再作成方式を採用。"""
    # phone列にUNIQUEインデックスがあるか確認
    has_phone_unique = False
    for row in conn.execute("PRAGMA index_list(companies)").fetchall():
        idx_name = row[1]
        is_unique = row[2]
        if not is_unique:
            continue
        cols = [r[2] for r in conn.execute(f"PRAGMA index_info({idx_name})").fetchall()]
        if cols == ['phone']:
            has_phone_unique = True
            break
    if not has_phone_unique:
        return

    import logging
    logging.info('[DB] phone UNIQUE制約を除去します（テーブル再作成）')
    conn.executescript('''
        PRAGMA foreign_keys=OFF;

        CREATE TABLE IF NOT EXISTS companies_v2 (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name     TEXT NOT NULL,
            normalized_name  TEXT UNIQUE NOT NULL,
            lp_url           TEXT,
            base_url         TEXT UNIQUE,
            phone            TEXT,
            phones           TEXT,
            industry         TEXT,
            ad_sources       TEXT,
            sheet_row        INTEGER,
            found_date       TEXT NOT NULL,
            keyword          TEXT,
            exported         INTEGER DEFAULT 0,
            contact_name     TEXT,
            lp_headline      TEXT,
            all_keywords     TEXT,
            area_name        TEXT,
            corporate_number TEXT
        );

        INSERT OR IGNORE INTO companies_v2
            SELECT id, company_name, normalized_name, lp_url, base_url,
                   phone, phones, industry, ad_sources, sheet_row, found_date,
                   keyword, exported, contact_name, lp_headline, all_keywords, area_name,
                   corporate_number
            FROM companies;

        DROP TABLE companies;
        ALTER TABLE companies_v2 RENAME TO companies;

        CREATE INDEX IF NOT EXISTS idx_normalized_name ON companies(normalized_name);
        CREATE INDEX IF NOT EXISTS idx_base_url ON companies(base_url);
        CREATE INDEX IF NOT EXISTS idx_phone ON companies(phone);
        CREATE INDEX IF NOT EXISTS idx_exported ON companies(exported);

        PRAGMA foreign_keys=ON;
    ''')
    conn.commit()
    logging.info('[DB] phone UNIQUE制約の除去が完了しました')


def is_duplicate(conn: sqlite3.Connection, normalized_name: str, base_url: str, phone: str) -> bool:
    if normalized_name:
        if conn.execute(
            'SELECT 1 FROM companies WHERE normalized_name = ?', (normalized_name,)
        ).fetchone():
            return True
    if base_url:
        if conn.execute(
            'SELECT 1 FROM companies WHERE base_url = ?', (base_url,)
        ).fetchone():
            return True
    if phone:
        # 同じ電話番号でも「別会社・別ドメイン」なら重複扱いしない
        # （コールセンター共用番号、番号引き継ぎ等で正しいペアが消えるのを防ぐ）
        row = conn.execute(
            'SELECT normalized_name, base_url FROM companies WHERE phone = ?', (phone,)
        ).fetchone()
        if row:
            same_company = normalized_name and row['normalized_name'] == normalized_name
            same_domain  = base_url and row['base_url'] == base_url
            if same_company or same_domain:
                return True
            # 同電話・別会社・別ドメイン → 通過させる（insertは一意でないためOK）
            import logging
            logging.info(f'[DB] 電話番号共有（別会社）: {phone} — {row["normalized_name"]} / {normalized_name}')
    return False


def get_existing_sources(conn: sqlite3.Connection, normalized_name: str) -> Optional[str]:
    row = conn.execute(
        'SELECT ad_sources FROM companies WHERE normalized_name = ?',
        (normalized_name,)
    ).fetchone()
    return row['ad_sources'] if row else None


def update_ad_sources(conn: sqlite3.Connection, normalized_name: str, new_source: str):
    row = conn.execute(
        'SELECT ad_sources FROM companies WHERE normalized_name = ?',
        (normalized_name,)
    ).fetchone()
    if not row:
        return
    existing = row['ad_sources'] or ''
    sources = [s.strip() for s in existing.split('+') if s.strip()]
    if new_source not in sources:
        sources.append(new_source)
        merged = '+'.join(sources)
        conn.execute(
            'UPDATE companies SET ad_sources = ? WHERE normalized_name = ?',
            (merged, normalized_name)
        )
        conn.commit()



def insert_company(conn: sqlite3.Connection, data: dict) -> bool:
    try:
        kw = data.get('keyword') or ''
        conn.execute(
            '''
            INSERT OR IGNORE INTO companies
              (company_name, normalized_name, lp_url, base_url, phone, phones,
               phone_source, ad_sources, found_date, keyword, exported,
               contact_name, lp_headline, all_keywords, area_name, corporate_number,
               nta_prefecture, pref_match, phone_confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                data['company_name'], data['normalized_name'],
                data['lp_url'], data['base_url'], data['phone'],
                data.get('phones'),
                data.get('phone_source', ''),
                data['ad_sources'],
                data['found_date'], kw,
                data.get('contact_name'),
                data.get('lp_headline'),
                kw,
                data.get('area_name'),
                data.get('corporate_number'),
                data.get('nta_prefecture', ''),
                data.get('pref_match', ''),
                data.get('phone_confidence'),
            )
        )
        conn.commit()
        return conn.execute('SELECT changes()').fetchone()[0] > 0
    except sqlite3.IntegrityError:
        return False


def append_keyword(conn: sqlite3.Connection, normalized_name: str, keyword: str):
    """既存レコードに新しいキーワードを追記する（重複は除外）。"""
    if not keyword:
        return
    row = conn.execute(
        'SELECT all_keywords FROM companies WHERE normalized_name = ?',
        (normalized_name,)
    ).fetchone()
    if not row:
        return
    existing = row['all_keywords'] or ''
    kws = [k.strip() for k in existing.split(' / ') if k.strip()]
    if keyword not in kws:
        kws.append(keyword)
        conn.execute(
            'UPDATE companies SET all_keywords = ? WHERE normalized_name = ?',
            (' / '.join(kws), normalized_name)
        )
        conn.commit()


def get_competitors(
    conn: sqlite3.Connection,
    keyword: str,
    normalized_name: str,
    area_name: Optional[str] = None,
    limit: int = 3,
) -> list[str]:
    """
    競合他社名を返す（自社除く、最大limit件）。
    ① 自社DBから同エリア × 同キーワード系
    ② DB不足ならClaude AIで補完
    """
    if not keyword:
        return []

    results: list[str] = []

    # ① DBから: 同エリア × 同キーワード系（最優先）
    if area_name:
        rows = conn.execute(
            '''
            SELECT DISTINCT company_name FROM companies
            WHERE (keyword LIKE ? OR all_keywords LIKE ?)
              AND area_name = ?
              AND normalized_name != ?
            ORDER BY id DESC LIMIT ?
            ''',
            (f'%{keyword.split()[0]}%', f'%{keyword.split()[0]}%',
             area_name, normalized_name, limit)
        ).fetchall()
        results = [r['company_name'] for r in rows]

    # ② DBから: エリア不問で同キーワード系
    if len(results) < limit:
        kw0 = keyword.split()[0]
        rows = conn.execute(
            '''
            SELECT DISTINCT company_name FROM companies
            WHERE (keyword LIKE ? OR all_keywords LIKE ?)
              AND normalized_name != ?
            ORDER BY id DESC LIMIT ?
            ''',
            (f'%{kw0}%', f'%{kw0}%', normalized_name, limit - len(results))
        ).fetchall()
        for r in rows:
            if r['company_name'] not in results:
                results.append(r['company_name'])

    # ③ まだ足りなければ Claude AI で補完
    if len(results) < limit:
        try:
            from utils.competitor_finder import find_competitors_ai
            ai_names = find_competitors_ai(keyword, area_name, limit - len(results))
            for name in ai_names:
                if name not in results:
                    results.append(name)
        except Exception:
            pass

    return results[:limit]


def get_unexported(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # 法人番号が確定したレコードのみ再送対象にする。
    # writerの新規書き込みはNTA未マッチをスキップするため、再送経路でも同じ
    # 基準を適用しないと未確定レコードがSheetsに漏れる（Sheetsは整合データのみ）。
    # 未確定分は nta_retry_worker が法人番号を回収した時点で自動的に対象になる。
    return conn.execute(
        '''SELECT * FROM companies
           WHERE exported = 0
             AND corporate_number IS NOT NULL AND corporate_number != ''
           ORDER BY id'''
    ).fetchall()


def mark_exported(conn: sqlite3.Connection, company_id: int, sheet_row: int):
    conn.execute(
        'UPDATE companies SET exported = 1, sheet_row = ? WHERE id = ?',
        (sheet_row, company_id)
    )
    conn.commit()
