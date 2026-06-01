"""
migrate_ranks.py — 既存データへのランク付け移行スクリプト

実行:
    cd /Users/holy/Downloads/ad_scraper
    python3 migrate_ranks.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time

DB_PATH = "companies.db"
CONFIG_PATH = "config.json"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── Step 1: DBマイグレーション ─────────────────────────────────────────────

def migrate_db() -> int:
    """rank / seen_count カラムを追加し、全レコードのランクを計算して書き込む"""
    from processors.rank_calculator import calc_rank

    with sqlite3.connect(DB_PATH) as conn:
        # カラム追加（既存の場合は無視）
        for col, typ, default in [("rank", "TEXT", "'C'"), ("seen_count", "INTEGER", "1")]:
            try:
                conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {typ} DEFAULT {default}")
                conn.commit()
                print(f"  DB: {col} カラム追加")
            except sqlite3.OperationalError:
                print(f"  DB: {col} カラムは既に存在")

        # 全レコードのランクを計算して更新
        rows = conn.execute(
            "SELECT id, seen_count, ad_sources FROM companies"
        ).fetchall()

        updates = []
        for row_id, seen_count, ad_sources in rows:
            rank = calc_rank(seen_count or 1, ad_sources or "")
            updates.append((rank, row_id))

        conn.executemany("UPDATE companies SET rank=? WHERE id=?", updates)
        conn.commit()
        print(f"  DB: {len(updates)} 件のランクを更新")
        return len(updates)


# ── Step 2: Sheets列挿入 & ランク書き込み ──────────────────────────────────

def migrate_sheets(config: dict) -> None:
    """
    Sheetsの既存データにランク列を挿入する。

    現在の列構成（C列起点）:
      C=キーワード D=広告ソース E=取得日時 F=会社名 G=LP URL H=電話番号

    移行後:
      C=キーワード D=広告ソース E=取得日時 F=ランク G=会社名 H=LP URL I=電話番号
    """
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        print("  Sheets: google-api-python-client 未インストール。Sheetsスキップ。")
        return

    key_path = config["google_sheets"].get("service_account_key_path", "credentials.json")
    sheet_id = config["google_sheets"]["sheet_id"]

    try:
        creds = Credentials.from_service_account_file(
            key_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        service = build("sheets", "v4", credentials=creds)
    except Exception as e:
        print(f"  Sheets: 認証失敗 -> {e}")
        print("  Sheets: credentials.json が未配置の場合はスキップします。")
        return

    # シート名取得
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet_props = None
    for s in meta.get("sheets", []):
        if not s["properties"].get("hidden", False):
            sheet_props = s["properties"]
            break
    if not sheet_props:
        print("  Sheets: 表示中のシートが見つかりません")
        return

    sheet_name = sheet_props["title"]
    sheet_gid = sheet_props["sheetId"]
    print(f"  Sheets: シート名={sheet_name}")

    # 現在のデータ取得（C列: キーワードが入っている行数を基準にする）
    col_c = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{sheet_name}!C:C",
    ).execute()
    c_values = col_c.get("values", [])
    data_rows = len(c_values)
    if data_rows == 0:
        print("  Sheets: データなし。スキップ。")
        return
    print(f"  Sheets: データ行数={data_rows}")

    # F列（インデックス5, 0-based）が既にランク列かチェック
    # F列の最初の非空セルを確認
    col_f = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{sheet_name}!F1:F5",
    ).execute()
    f_sample = [r[0] if r else "" for r in col_f.get("values", [])]
    if any(v in ("S", "A", "B", "C") for v in f_sample):
        print("  Sheets: F列に既にランク値が存在します。列挿入をスキップします。")
        # ランク値の更新のみ行う
        _update_ranks_only(service, sheet_id, sheet_name, data_rows)
        return

    # ── F列（0-based=5）に空白列を挿入 ──
    print("  Sheets: F列に新しいランク列を挿入中...")
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "requests": [{
                "insertDimension": {
                    "range": {
                        "sheetId": sheet_gid,
                        "dimension": "COLUMNS",
                        "startIndex": 5,  # F列（0-based）
                        "endIndex": 6,
                    },
                    "inheritFromBefore": False,
                }
            }]
        },
    ).execute()
    print("  Sheets: 列挿入完了")
    time.sleep(1)  # API レート制限対策

    # 挿入後: H列（0-based=7）= 旧H列 = 電話番号、G列 = 会社名
    # 電話番号でDBのrankを引いてF列に書き込む
    _update_ranks_only(service, sheet_id, sheet_name, data_rows)


def _update_ranks_only(service, sheet_id: str, sheet_name: str, data_rows: int) -> None:
    """電話番号（I列）を使ってDBのrankをF列に書き込む"""
    # I列（電話番号）を読む
    phone_col = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{sheet_name}!I1:I{data_rows}",
    ).execute()
    phone_values = phone_col.get("values", [])

    # DBから phone → rank のマップを作成
    with sqlite3.connect(DB_PATH) as conn:
        phone_rank_map: dict[str, str] = dict(
            conn.execute("SELECT phone, rank FROM companies WHERE phone != ''").fetchall()
        )

    # F列に書き込む値を準備
    rank_cells = []
    matched = 0
    for i, row in enumerate(phone_values):
        phone = row[0].strip() if row else ""
        # 電話番号の正規化: ハイフンなし版でも照合
        rank = phone_rank_map.get(phone)
        if not rank:
            phone_no_hyphen = phone.replace("-", "").replace("－", "")
            for db_phone, db_rank in phone_rank_map.items():
                if db_phone.replace("-", "").replace("－", "") == phone_no_hyphen:
                    rank = db_rank
                    break
        if rank:
            matched += 1
        rank_cells.append([rank or "C"])

    if not rank_cells:
        print("  Sheets: 書き込む行なし")
        return

    # F列一括書き込み
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{sheet_name}!F1:F{len(rank_cells)}",
        valueInputOption="RAW",
        body={"values": rank_cells},
    ).execute()
    print(f"  Sheets: {len(rank_cells)} 行にランク書き込み完了（DB照合={matched}件、デフォルトC={len(rank_cells)-matched}件）")


# ── メイン ────────────────────────────────────────────────────────────────

def main() -> None:
    print("=== ランク移行スクリプト ===\n")

    print("[Step 1] DBマイグレーション")
    count = migrate_db()
    print(f"  完了: {count} 件処理\n")

    print("[Step 2] Google Sheetsランク列追加")
    config = load_config()
    migrate_sheets(config)
    print()

    print("=== 移行完了 ===")


if __name__ == "__main__":
    main()
