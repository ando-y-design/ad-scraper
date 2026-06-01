from __future__ import annotations
"""
国税庁法人番号公表データのローカルDBキャッシュ。
NTA Web APIキーが未有効化の間も、ローカルで法人名照合できる。

【準備（初回のみ）】
  1. https://www.houjin-bangou.nta.go.jp/download/zenken/
     から「全件データ（UTF-8）」を選びダウンロード → ZIP解凍
  2. python -m utils.nta_local_db setup path/to/extracted_csv_dir/
     ※ 処理時間: 5〜10分、DBサイズ: 約1GB

【更新】
  毎月1日に新しいデータが公表される。月1回 setup を再実行するか、
  差分のみなら python -m utils.nta_local_db update で対応。

【APIとの違い】
  - APIキー不要
  - レート制限なし（ローカルSQLite）
  - 月次更新（APIはリアルタイム）
  - 法人種別1（国内法人）のみ対象
"""
import csv
import logging
import re
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / 'data' / 'nta_corp.db'

# 法人格を除いた検索用パターン（API版と同じ）
_STRIP_LEGAL_RE = re.compile(
    r'^(株式会社|有限会社|合同会社|合資会社|合名会社|医療法人|社会福祉法人|'
    r'宗教法人|一般社団法人|公益社団法人|一般財団法人|公益財団法人|'
    r'学校法人|弁護士法人|税理士法人|司法書士法人|NPO法人|特定非営利活動法人|'
    r'協同組合|農業協同組合)\s*'
    r'|'
    r'\s*(株式会社|有限会社|合同会社)$'
)


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def is_db_ready() -> bool:
    """ローカルDBが使用可能かどうかを確認する"""
    if not DB_PATH.exists():
        return False
    try:
        conn = _get_conn()
        count = conn.execute('SELECT COUNT(*) FROM nta_corp').fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        return False


def setup_db(csv_dir: str | Path) -> int:
    """
    NTA公表データCSVからSQLite DBを構築する。
    csv_dir: ZIPを解凍したディレクトリ（複数CSVファイルが入っている場合も対応）

    Returns: ロードした件数
    """
    csv_dir = Path(csv_dir)
    csv_files = list(csv_dir.glob('*.csv')) + list(csv_dir.glob('**/*.csv'))
    if not csv_files:
        # 単一ファイルが渡された場合
        if csv_dir.is_file() and csv_dir.suffix == '.csv':
            csv_files = [csv_dir]
        else:
            raise FileNotFoundError(f'CSVファイルが見つかりません: {csv_dir}')

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()

    conn.execute('DROP TABLE IF EXISTS nta_corp')
    conn.execute('''
        CREATE TABLE nta_corp (
            corp_number TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            pref        TEXT,
            city        TEXT,
            close_date  TEXT
        )
    ''')
    # FTS5 trigram: 日本語部分一致検索に対応（SQLite 3.34+ 必須）
    # フォールバック: trigram非対応の場合は unicode61 で前方一致のみ
    try:
        conn.execute('''
            CREATE VIRTUAL TABLE nta_corp_fts USING fts5(
                name,
                content='nta_corp',
                content_rowid='rowid',
                tokenize='trigram'
            )
        ''')
        _use_trigram = True
    except Exception:
        conn.execute('''
            CREATE VIRTUAL TABLE nta_corp_fts USING fts5(
                name,
                content='nta_corp',
                content_rowid='rowid',
                tokenize='unicode61'
            )
        ''')
        _use_trigram = False

    conn.execute('BEGIN')
    total = 0

    for csv_file in csv_files:
        logging.info(f'[NTA-DB] {csv_file.name} をロード中...')
        with open(csv_file, encoding='utf-8', errors='replace') as f:
            reader = csv.reader(f)
            for i, row in enumerate(reader):
                if i == 0:
                    continue  # ヘッダー行をスキップ
                if len(row) < 14:
                    continue

                corp_number = row[1].strip()   # 2列目: 法人番号
                kind        = row[5].strip()   # 6列目: 法人種別
                name        = row[6].strip()   # 7列目: 法人名
                pref        = row[11].strip()  # 12列目: 都道府県名
                city        = row[13].strip()  # 14列目: 市区町村名
                close_date  = row[18].strip() if len(row) > 18 else ''  # 19列目: 閉鎖日

                # 国内普通法人のみ（kind=301: 株式会社, 302: 有限会社, etc.）
                # kind が '01' or '3xx' で始まるものが国内法人
                if not name or not corp_number:
                    continue
                # 閉鎖法人はスキップ（APIと同じ挙動）
                if close_date:
                    continue

                try:
                    conn.execute(
                        'INSERT OR IGNORE INTO nta_corp VALUES (?,?,?,?,?)',
                        (corp_number, name, pref, city, close_date)
                    )
                    total += 1
                    if total % 100000 == 0:
                        logging.info(f'[NTA-DB] {total:,}件ロード済み...')
                except Exception:
                    continue

    conn.execute('COMMIT')

    # FTS インデックスを更新
    logging.info('[NTA-DB] FTSインデックス構築中...')
    conn.execute("INSERT INTO nta_corp_fts(nta_corp_fts) VALUES('rebuild')")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_nta_name ON nta_corp(name)')
    conn.commit()
    conn.close()

    logging.info(f'[NTA-DB] セットアップ完了: {total:,}件')
    return total


def search_local(name: str, limit: int = 5) -> list[dict]:
    """
    ローカルDBで法人名を部分一致検索する。
    DB未構築の場合は空リストを返す（サイレント）。
    """
    if not is_db_ready():
        return []

    try:
        conn = _get_conn()

        # FTS5 で検索（trigram対応なら部分一致、そうでなければ前方一致）
        try:
            rows = conn.execute(
                'SELECT corp_number, name, pref, city FROM nta_corp_fts '
                'WHERE name MATCH ? LIMIT ?',
                (name, limit)
            ).fetchall()
        except Exception:
            # FTS失敗時: LIKE 部分一致にフォールバック（遅いが確実）
            rows = conn.execute(
                'SELECT corp_number, name, pref, city FROM nta_corp '
                'WHERE name LIKE ? AND close_date="" LIMIT ?',
                (f'%{name}%', limit)
            ).fetchall()

        conn.close()
        return [
            {
                'corporate_number': r['corp_number'],
                'name': r['name'],
                'address': f"{r['pref']}{r['city']}",
                'kind': '',
                'close_date': '',
            }
            for r in rows
        ]
    except Exception as e:
        logging.debug(f'[NTA-DB] 検索エラー: {e}')
        return []


def verify_local(raw_name: str) -> dict:
    """
    nta_lookup.verify_and_normalize と同じシグネチャ。
    APIの代わりにローカルDBを使う。
    """
    name = raw_name.strip()
    if len(name) < 2:
        return {'verified': False, 'official_name': raw_name,
                'corporate_number': '', 'address': '', 'confidence': 'none'}

    # 法人格を除いた検索名
    search_name = _STRIP_LEGAL_RE.sub('', name).strip()
    if len(search_name) < 2:
        search_name = name

    hits = search_local(search_name)
    if not hits:
        return {'verified': False, 'official_name': raw_name,
                'corporate_number': '', 'address': '', 'confidence': 'none'}

    # 完全一致優先
    for h in hits:
        if h['name'] == raw_name:
            return {'verified': True, 'official_name': h['name'],
                    'corporate_number': h['corporate_number'],
                    'address': h['address'], 'confidence': 'exact'}

    best = hits[0]
    return {'verified': True, 'official_name': best['name'],
            'corporate_number': best['corporate_number'],
            'address': best['address'], 'confidence': 'partial'}


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────
if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'

    if cmd == 'setup':
        if len(sys.argv) < 3:
            print('使い方: python -m utils.nta_local_db setup <CSVディレクトリ or CSVファイル>')
            sys.exit(1)
        logging.basicConfig(level=logging.INFO, format='%(message)s')
        n = setup_db(sys.argv[2])
        print(f'完了: {n:,}件をロードしました')

    elif cmd == 'status':
        if is_db_ready():
            conn = _get_conn()
            count = conn.execute('SELECT COUNT(*) FROM nta_corp').fetchone()[0]
            conn.close()
            print(f'ローカルDB: 有効 ({count:,}件)')
        else:
            print('ローカルDB: 未構築')
            print('→ https://www.houjin-bangou.nta.go.jp/download/zenken/ から')
            print('  「全件データ（UTF-8）」をダウンロードして:')
            print('  python -m utils.nta_local_db setup <解凍フォルダ>')

    elif cmd == 'search':
        if len(sys.argv) < 3:
            print('使い方: python -m utils.nta_local_db search <会社名>')
            sys.exit(1)
        result = verify_local(sys.argv[2])
        for k, v in result.items():
            print(f'  {k}: {v}')
    else:
        print(f'不明なコマンド: {cmd}')
