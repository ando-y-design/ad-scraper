#!/usr/bin/env python3
"""
広告営業テレアポリスト自動収集ツール
DYM Web事業部向け
"""
import argparse
import http.server
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from processors.phone_finder import set_phone_strategy
from scrapers.meta_api_scraper import test_api_access as meta_api_test
from storage.database import init_db
from utils.browser import create_browser_context, new_stealth_page
from utils.keywords import init_keywords
from utils.logger import setup_logging
from utils.config_loader import load_config

import state
from state import (
    config, shutdown_event, pause_event,
    threads, BASE_DIR,
)
from workers.yahoo_worker import yahoo_worker, yahoo2_worker
from workers.meta_worker import meta_worker
from workers.processor_worker import processor_worker
from workers.writer_worker import writer_worker
from workers.watchdog_worker import watchdog_worker

CONFIG_PATH = BASE_DIR / 'config.json'
CONTROL_PATH = BASE_DIR / 'control.json'


def is_blocked_domain(domain: str) -> bool:
    blocked = config.get('filters', {}).get('blocked_domains', [])
    domain = domain.lower()
    return any(domain == b or domain.endswith('.' + b) for b in blocked)


# ─────────────────────────────────────────────
# STATUS HTTP サーバー（status.html を外部公開）
# ─────────────────────────────────────────────
def _status_server_worker():
    """status.html を HTTP で配信するシンプルサーバー。
    config.json の status_server.port（デフォルト8080）でリッスン。
    """
    port = config.get('status_server', {}).get('port', 8080)

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(BASE_DIR), **kwargs)

        def log_message(self, fmt, *args):
            pass  # アクセスログを scraper ログに混入させない

        def do_GET(self):
            # ルートアクセスは status.html にリダイレクト
            if self.path == '/':
                self.send_response(302)
                self.send_header('Location', '/status.html')
                self.end_headers()
                return
            super().do_GET()

    try:
        server = http.server.HTTPServer(('0.0.0.0', port), _Handler)
        logging.info(f'[StatusServer] http://localhost:{port} で配信開始')
        while not shutdown_event.is_set():
            server.handle_request()
        server.server_close()
    except OSError as e:
        logging.warning(f'[StatusServer] 起動失敗 (port={port}): {e}')
    except Exception as e:
        logging.error(f'[StatusServer] エラー: {e}')


def _start_ngrok_tunnel():
    """ngrok トンネルを起動して固定URLをログに表示する。
    config.json の ngrok.enabled が true のときのみ動作。
    """
    ngrok_cfg = config.get('ngrok', {})
    if not ngrok_cfg.get('enabled', False):
        return
    authtoken = ngrok_cfg.get('authtoken', '').strip()
    domain = ngrok_cfg.get('domain', '').strip()
    port = config.get('status_server', {}).get('port', 8080)

    def _run():
        try:
            import ngrok as _ngrok
            if authtoken:
                _ngrok.set_auth_token(authtoken)
            kwargs = {'domain': domain} if domain else {}
            listener = _ngrok.forward(port, **kwargs)
            url = listener.url()
            logging.info(f'[ngrok] ✓ 公開URL: {url}')
            logging.info(f'[ngrok] チームに送るURL: {url}/status.html')
            # シャットダウンまで維持
            shutdown_event.wait()
            _ngrok.disconnect(url)
        except ImportError:
            logging.warning('[ngrok] 未インストール。venv\\Scripts\\pip install ngrok で追加してください')
        except Exception as e:
            logging.error(f'[ngrok] トンネル起動失敗: {e}')

    t = threading.Thread(target=_run, name='ngrok', daemon=True)
    t.start()


# ─────────────────────────────────────────────
# セットアップモード（初回Metaログイン）
# ─────────────────────────────────────────────
def run_setup():
    print('\n=== Meta セットアップ ===')
    print('ブラウザが開きます。Facebookにログインしてください。')
    print('ログイン完了を自動検知して保存します（最大3分待機）\n')

    with sync_playwright() as playwright:
        ctx = create_browser_context(playwright)
        page = new_stealth_page(ctx)

        page.goto('https://www.facebook.com', timeout=20000, wait_until='domcontentloaded')
        time.sleep(2)

        # すでにログイン済みか確認
        if 'login' not in page.url and page.query_selector('[aria-label*="ホーム"], [aria-label*="Home"], [data-testid="blue_bar_profile_link"]'):
            print('ログイン済みを確認しました。プロファイルを保存しています...')
        else:
            print('ブラウザでFacebookにログインしてください。自動で検知します...')
            # ログイン完了（ホームページへの遷移）を最大180秒待つ
            try:
                page.wait_for_url(
                    lambda url: 'facebook.com' in url and 'login' not in url and 'checkpoint' not in url,
                    timeout=180000
                )
                print('ログイン検知！プロファイルを保存しています...')
            except Exception:
                print('タイムアウト。現在の状態で保存します...')

        time.sleep(3)  # プロファイル書き込み待機
        ctx.close()
        time.sleep(2)  # close後の書き込み完了待機

    print('セットアップ完了。venv\\Scripts\\python.exe main.py で本番起動できます。')


# ─────────────────────────────────────────────
# 二重起動防止（PID ロックファイル）
# ─────────────────────────────────────────────
_PID_LOCK_PATH = BASE_DIR / 'logs' / 'scraper.pid'


def _acquire_pid_lock() -> bool:
    """
    PIDロックファイルを取得する。
    すでに同一プロセスが動いていれば False を返す。
    """
    import psutil  # 標準ライブラリ外だが requirements.txt に追加済み想定
    _PID_LOCK_PATH.parent.mkdir(exist_ok=True)

    if _PID_LOCK_PATH.exists():
        try:
            existing_pid = int(_PID_LOCK_PATH.read_text(encoding='utf-8').strip())
            # そのPIDのプロセスが存在するか確認
            if psutil.pid_exists(existing_pid):
                try:
                    proc = psutil.Process(existing_pid)
                    cmdline = ' '.join(proc.cmdline())
                    is_main = 'main.py' in cmdline
                    is_zombie = proc.status() == psutil.STATUS_ZOMBIE
                    if is_main and not is_zombie:
                        logging.error(
                            f'[起動] 既に稼働中のプロセスを検出 (PID={existing_pid})。'
                            f'二重起動を防止するため終了します。'
                        )
                        return False
                except psutil.NoSuchProcess:
                    pass  # PID消滅 → ロック取得OK
                except psutil.AccessDenied:
                    # cmdline読めない場合: 同PIDのpythonプロセスが存在するなら保守的に拒否
                    try:
                        if proc.name().lower().startswith('python'):
                            logging.error(
                                f'[起動] PID={existing_pid} のプロセスが存在します（cmdline不明）。'
                                f'二重起動を防止するため終了します。'
                            )
                            return False
                    except Exception:
                        pass  # 何も分からなければロック取得OK
        except (ValueError, OSError):
            pass  # ファイル破損 → 上書きして続行

    # ロックファイルに自分のPIDを書き込む
    try:
        _PID_LOCK_PATH.write_text(str(os.getpid()), encoding='utf-8')
        return True
    except OSError as e:
        logging.warning(f'[起動] PIDロックファイル書き込み失敗（続行）: {e}')
        return True  # 書けなくても起動は続行


def _release_pid_lock():
    """PIDロックファイルを削除する（終了時）"""
    try:
        if _PID_LOCK_PATH.exists():
            pid_in_file = int(_PID_LOCK_PATH.read_text(encoding='utf-8').strip())
            if pid_in_file == os.getpid():
                _PID_LOCK_PATH.unlink()
    except Exception:
        pass


def _signal_handler(sig, frame):
    logging.info('シャットダウン要求。処理中のデータを保存してから終了します...')
    shutdown_event.set()


# ─────────────────────────────────────────────
# スレッド管理
# ─────────────────────────────────────────────
_THREAD_TARGETS = {
    'yahoo': yahoo_worker,
    'yahoo2': yahoo2_worker,
    'meta': meta_worker,
    'processor': processor_worker,
    'writer': writer_worker,
}


def _start_thread(name: str):
    t = threading.Thread(
        target=_THREAD_TARGETS[name],
        name=name,
        daemon=True
    )
    t.start()
    threads[name] = t
    logging.info(f'スレッド起動: {name}')


# ─────────────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────────────
def main():
    global config

    parser = argparse.ArgumentParser(description='広告営業リスト収集ツール')
    parser.add_argument('--setup', action='store_true', help='初回セットアップ（Metaログイン）')
    args = parser.parse_args()

    setup_logging()
    loaded = load_config()
    config.update(loaded)
    set_phone_strategy(config.get('phone_strategy', 'direct'))

    if args.setup:
        run_setup()
        return

    # ── 二重起動チェック ──────────────────────────────
    try:
        import psutil as _psutil_check  # noqa: F401
        if not _acquire_pid_lock():
            sys.exit(1)
        import atexit
        atexit.register(_release_pid_lock)
    except ImportError:
        logging.warning('[起動] psutil未インストール。二重起動チェックをスキップします（pip install psutil で有効化）')

    # 設定チェック
    sheet_id = config.get('google_sheets', {}).get('sheet_id', '')
    if sheet_id == 'YOUR_SHEET_ID':
        logging.error('config.json の sheet_id を設定してください')
        sys.exit(1)

    logging.info('=== 広告営業リスト収集ツール 起動 ===')

    # Meta Ad Library API 接続確認（設定済みの場合）
    try:
        meta_api_test()
    except Exception:
        pass

    # DB初期化
    conn = init_db()
    init_keywords(conn)
    logging.info('データベース初期化完了')

    # シグナルハンドラ
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    # Windows: Ctrl+Break で安全停止
    if sys.platform == 'win32' and hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, _signal_handler)

    # ワーカースレッド起動
    # yahoo/yahoo2/meta は並列動作（それぞれ独立ブラウザ）
    # Chrome同時起動によるリソース競合を防ぐため10秒ずつずらす
    for name in ['writer', 'processor']:
        _start_thread(name)
    _start_thread('yahoo')
    time.sleep(10)
    if config.get('sources', {}).get('yahoo2', True):
        _start_thread('yahoo2')
        time.sleep(10)
    else:
        logging.info('yahoo2 は config で無効化されています（メモリ節約モード）')
    _start_thread('meta')

    # Watchdog起動
    watchdog = threading.Thread(target=watchdog_worker, name='watchdog', daemon=True)
    watchdog.start()

    # StatusHTTPサーバー起動（status.html を外部公開）
    if config.get('status_server', {}).get('enabled', True):
        status_srv = threading.Thread(target=_status_server_worker, name='status_server', daemon=True)
        status_srv.start()
        _start_ngrok_tunnel()

    logging.info('全スレッド起動完了。Ctrl+C で終了')

    # メインスレッドはshutdownを待つ
    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown_event.set()

    logging.info('シャットダウン中...')

    # ワーカースレッドの終了を待つ（最大30秒）
    # q.join()はshutdown後にworkerが終了するとデッドロックするためthread.join()を使う
    deadline = time.time() + 30
    for name, thread in list(threads.items()):
        if thread and thread.is_alive():
            remaining = max(0.1, deadline - time.time())
            thread.join(timeout=remaining)

    logging.info('=== 終了 ===')


if __name__ == '__main__':
    main()
