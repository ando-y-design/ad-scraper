from __future__ import annotations
"""
rewrite_sheets.py — DBの全レコードでSpreadsheetをクリア＆書き直す（1回実行用）
"""
import json
import sqlite3
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

with open("config.json") as f:
    config = json.load(f)

sheet_id = config["google_sheets"]["sheet_id"]
key_path = config["google_sheets"]["service_account_key_path"]

creds = Credentials.from_service_account_file(
    key_path,
    scopes=["https://www.googleapis.com/auth/spreadsheets"],
)
service = build("sheets", "v4", credentials=creds)

# シート名取得
meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
sheet_name = meta["sheets"][0]["properties"]["title"]
print(f"シート名: {sheet_name}")

# DBから全レコード取得
conn = sqlite3.connect("companies.db")
rows = conn.execute(
    "SELECT company_name, lp_url, phone, keyword, ad_sources, found_date FROM companies ORDER BY id"
).fetchall()
conn.close()
print(f"DB件数: {len(rows)}")

# シートをクリア
service.spreadsheets().values().clear(
    spreadsheetId=sheet_id,
    range=f"{sheet_name}!A:Z",
    body={},
).execute()
print("シートクリア完了")

# ヘッダー＋全データを書き込み
header = [["会社名", "LP URL", "電話番号", "キーワード", "広告ソース", "取得日時"]]
values = header + [list(r) for r in rows]

service.spreadsheets().values().update(
    spreadsheetId=sheet_id,
    range=f"{sheet_name}!C1",
    valueInputOption="RAW",
    body={"values": values},
).execute()
print(f"書き込み完了: {len(rows)} 件")
