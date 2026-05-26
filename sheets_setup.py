#!/usr/bin/env python3
"""
Google Sheets セットアップスクリプト
credentials.json を置いたら実行するだけで全部自動で完了します
"""
import json
import sys
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))


def main():
    print("=" * 50)
    print(" Google Sheets セットアップ")
    print("=" * 50)

    # Step 1: credentials.json 確認
    creds_path = BASE_DIR / 'credentials.json'
    if not creds_path.exists():
        print("\n【まずこれだけやってください】")
        print()
        print("① 以下のURLをブラウザで開く（自動で開きます）")
        print("② 「新しいプロジェクト」を作成（名前は何でもOK）")
        print("③ 「APIとサービス」→「ライブラリ」→ 以下2つを有効化:")
        print("   - Google Sheets API")
        print("   - Google Drive API")
        print("④ 「APIとサービス」→「認証情報」→「認証情報を作成」")
        print("   →「サービスアカウント」→名前を入力→作成")
        print("   →作成したサービスアカウントをクリック")
        print("   →「キー」タブ→「鍵を追加」→「新しい鍵を作成」→JSON")
        print("   →ダウンロードされたJSONファイルを")
        print(f"   → {BASE_DIR} にコピーして credentials.json にリネーム")
        print()

        input("Enterを押すとGoogle Cloud Consoleが開きます > ")
        webbrowser.open("https://console.cloud.google.com/projectcreate")

        print()
        print("上記の手順を完了したら、このスクリプトを再実行してください:")
        print("  venv\\Scripts\\python.exe sheets_setup.py")
        return

    print("\n[OK] credentials.json 確認済み")

    # Step 2: サービスアカウントのメールを取得
    with open(creds_path, 'r', encoding='utf-8') as f:
        creds_data = json.load(f)

    service_email = creds_data.get('client_email', '')
    if not service_email:
        print("[ERROR] credentials.json が正しくありません")
        sys.exit(1)

    print(f"[OK] サービスアカウント: {service_email}")

    # Step 3: スプレッドシートを自動作成
    print("\nスプレッドシートを自動作成中...")

    from storage.sheets_writer import get_sheets_client, setup_sheet
    from storage.database import init_db

    try:
        client = get_sheets_client(str(creds_path))
    except Exception as e:
        print(f"[ERROR] 認証失敗: {e}")
        print("credentials.json が正しいか確認してください")
        sys.exit(1)

    # 既存設定確認
    config_path = BASE_DIR / 'config.json'
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    sheet_id = config.get('google_sheets', {}).get('sheet_id', 'YOUR_SHEET_ID')

    if sheet_id != 'YOUR_SHEET_ID':
        print(f"[INFO] config.json に既存のシートID: {sheet_id}")
        use_existing = input("このシートを使いますか？ (y/n) > ").strip().lower()
        if use_existing == 'y':
            try:
                spreadsheet = client.open_by_key(sheet_id)
                ws = spreadsheet.sheet1
                print(f"[OK] 既存シート接続: {ws.title}")
                setup_sheet(spreadsheet, ws)
                print("[OK] 書式設定完了")
                _finalize(config, config_path, sheet_id, service_email)
                return
            except Exception as e:
                print(f"[WARN] 既存シートに接続できません: {e}")

    # 新規作成
    try:
        spreadsheet = client.create('広告営業リスト')
        sheet_id = spreadsheet.id
        ws = spreadsheet.sheet1
        print(f"[OK] スプレッドシート作成: 広告営業リスト (ID: {sheet_id})")
    except Exception as e:
        print(f"[ERROR] スプレッドシート作成失敗: {e}")
        print()
        print("手動でスプレッドシートを作成する場合:")
        print("1. sheets.google.com で新しいシートを作成")
        print(f"2. {service_email} を編集者として共有")
        print("3. URLの /d/ と /edit の間のIDをコピー")
        print("4. config.json の sheet_id に貼り付け")
        sys.exit(1)

    # 書式設定
    setup_sheet(spreadsheet, ws)
    print("[OK] ヘッダー・条件付き書式設定完了（緑=複数ソース）")

    _finalize(config, config_path, sheet_id, service_email)


def _finalize(config, config_path, sheet_id, service_email):
    # config.json 更新
    config['google_sheets']['sheet_id'] = sheet_id
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"[OK] config.json 更新完了 (sheet_id: {sheet_id})")

    print()
    print("=" * 50)
    print(" セットアップ完了！")
    print("=" * 50)
    print()
    print("次のステップ: Metaログイン（初回のみ）")
    print("  venv\\Scripts\\python.exe main.py --setup")
    print()
    print("本番起動:")
    print("  venv\\Scripts\\python.exe main.py")


if __name__ == '__main__':
    main()
