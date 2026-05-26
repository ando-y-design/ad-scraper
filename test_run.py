#!/usr/bin/env python3
"""
動作確認スクリプト
実行: python test_run.py
全テストがPASSすれば本番稼働可能
"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))


def test_config():
    path = BASE_DIR / 'config.json'
    assert path.exists(), 'config.json が見つかりません'
    with open(path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    assert 'google_sheets' in config
    assert 'sheet_id' in config['google_sheets']
    assert config['google_sheets']['sheet_id'] != 'YOUR_SHEET_ID', \
        'config.json の sheet_id を設定してください'
    print('[PASS] config.json')


def test_credentials():
    path = BASE_DIR / 'credentials.json'
    assert path.exists(), 'credentials.json が見つかりません'
    print('[PASS] credentials.json')


def test_sqlite():
    from storage.database import init_db
    conn = init_db()
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert 'companies' in tables, 'companies テーブルがありません'
    assert 'keywords' in tables, 'keywords テーブルがありません'
    assert 'api_usage' in tables, 'api_usage テーブルがありません'
    print('[PASS] SQLite スキーマ')


def test_sheets_connection():
    with open(BASE_DIR / 'config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    from storage.sheets_writer import get_sheets_client, get_worksheet
    client = get_sheets_client(config['google_sheets']['service_account_key_path'])
    ws = get_worksheet(client, config['google_sheets']['sheet_id'])
    print(f'[PASS] Google Sheets 接続 (シート名: {ws.title})')


def test_playwright():
    from playwright.sync_api import sync_playwright
    from utils.browser import create_browser_context, new_stealth_page
    with sync_playwright() as pw:
        ctx = create_browser_context(pw)
        page = new_stealth_page(ctx)
        page.goto('https://www.google.co.jp', timeout=15000)
        assert 'Google' in page.title(), 'Googleページのタイトルが取得できません'
        page.close()
        ctx.close()
    print('[PASS] Playwright + stealth')


def test_normalizer():
    from processors.normalizer import get_base_domain, normalize_company, normalize_url
    assert normalize_company('株式会社リクルート') == 'リクルート'
    assert normalize_company('（株）リクルート') == 'リクルート'
    assert normalize_company('リクルート株式会社') == 'リクルート'
    assert 'www.' not in normalize_url('http://www.example.co.jp/lp/?utm=google')
    assert 'query' not in normalize_url('https://example.co.jp/lp?a=1')
    assert get_base_domain('https://www.example.co.jp/path') == 'example.co.jp'
    print('[PASS] 正規化ロジック')


def test_phone_extractor():
    from processors.phone_finder import extract_phone
    assert extract_phone('TEL: 03-1234-5678') == '03-1234-5678'
    assert extract_phone('フリーダイヤル：0120-123-456') == '0120-123-456'
    # FAX除外テスト
    result = extract_phone('FAX: 03-9999-9999\nTEL: 03-1234-5678')
    assert result == '03-1234-5678', f'FAX除外失敗: {result}'
    assert extract_phone('電話番号の記載なし') is None
    print('[PASS] 電話番号抽出')


def test_keywords():
    from storage.database import init_db
    from utils.keywords import init_keywords
    conn = init_db()
    init_keywords(conn)
    count = conn.execute('SELECT COUNT(*) FROM keywords').fetchone()[0]
    assert count > 0, 'キーワードが登録されていません'
    print(f'[PASS] キーワード初期化 ({count}件登録済み)')


def test_duplicate_check():
    from storage.database import init_db, is_duplicate, insert_company
    from datetime import datetime
    conn = init_db()

    # テスト用データ挿入
    test_data = {
        'company_name': 'テスト株式会社_pytest',
        'normalized_name': 'テスト_pytest_unique',
        'lp_url': 'https://test-pytest-unique.co.jp/lp',
        'base_url': 'test-pytest-unique.co.jp',
        'phone': '03-0000-0001',
        'industry': 'テスト',
        'ad_sources': 'Google',
        'keyword': 'テスト',
        'found_date': datetime.now().strftime('%Y-%m-%d'),
    }
    insert_company(conn, test_data)
    assert is_duplicate(conn, 'テスト_pytest_unique', '', ''), '名前重複チェック失敗'
    assert is_duplicate(conn, '', 'test-pytest-unique.co.jp', ''), 'URL重複チェック失敗'
    assert is_duplicate(conn, '', '', '03-0000-0001'), '電話重複チェック失敗'
    # クリーンアップ
    conn.execute("DELETE FROM companies WHERE normalized_name='テスト_pytest_unique'")
    conn.commit()
    print('[PASS] 重複チェック (3-way dedup)')


if __name__ == '__main__':
    tests = [
        ('config', test_config),
        ('credentials', test_credentials),
        ('sqlite', test_sqlite),
        ('sheets', test_sheets_connection),
        ('playwright', test_playwright),
        ('normalizer', test_normalizer),
        ('phone', test_phone_extractor),
        ('keywords', test_keywords),
        ('dedup', test_duplicate_check),
    ]

    failed = []
    for name, test_fn in tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f'[FAIL] {name}: {e}')
            failed.append(name)
        except Exception as e:
            print(f'[ERROR] {name}: {e}')
            failed.append(name)

    print()
    if failed:
        print(f'✗ {len(failed)}件失敗: {failed}')
        sys.exit(1)
    else:
        print('✓ 全テストPASS。本番稼働可能です。')
