"""Watchdog ワーカー: スレッド監視・自己修復・設定再読み込み"""
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from self_repair.auto_tuner import run_auto_tuner
from self_repair.diagnostics import get_diagnostics
from self_repair.repair_worker import run_repair_cycle
from storage.database import get_connection
from utils.db_janitor import run_db_janitor
from utils.drive_uploader import upload_status_html
from utils.keywords import add_keyword, archive_keyword
from utils.status_reporter import generate_status_html
from utils.config_loader import load_config

from state import (
    config, shutdown_event, pause_event,
    threads, thread_restart_count, thread_restart_time,
    _heartbeat, _ctx_dead,
    HANG_TIMEOUT_SECONDS, ZOMBIE_RESTART_SECONDS,
    BASE_DIR, beat,
)

CONTROL_PATH = BASE_DIR / 'control.json'


def _kill_scraper_chrome(thread_name: str = 'both'):
    """
    ゾンビ化した scraper Chrome を段階的に終了する。
    thread_name: 'yahoo' / 'meta' / 'both' — 対象プロファイルを絞り込む。
    1. WM_CLOSE (graceful) を送って最大10秒待つ
    2. まだ生きていれば Force Kill
    プロファイルパスで絞り込み、ユーザーの Chrome を巻き込まない。
    """
    import subprocess as _sp
    from utils.browser import PROFILE_DIR, _wait_chrome_exit

    # 対象プロファイルディレクトリを決定
    yahoo_profile = (BASE_DIR / 'browser_profile_yahoo').resolve()
    yahoo2_profile = (BASE_DIR / 'browser_profile_yahoo2').resolve()
    meta_profile = PROFILE_DIR  # browser_profile

    if thread_name == 'yahoo':
        target_profiles = [yahoo_profile]
    elif thread_name == 'yahoo2':
        target_profiles = [yahoo2_profile]
    elif thread_name == 'meta':
        target_profiles = [meta_profile]
    else:  # 'both'
        target_profiles = [yahoo_profile, yahoo2_profile, meta_profile]

    for profile_dir in target_profiles:
        profile_name = profile_dir.name
        # Step 1: graceful close
        try:
            _sp.run(
                ['powershell', '-NoProfile', '-Command',
                 f'Get-WmiObject Win32_Process -Filter "name=\'chrome.exe\'" | '
                 f'Where-Object {{ $_.CommandLine -like \'*{profile_name}*\' }} | '
                 f'ForEach-Object {{ $_.Terminate() }}'],
                capture_output=True, text=True, timeout=10,
            )
            logging.info(f'[Watchdog] Chrome ({profile_name}) に graceful 終了を要求しました')
        except Exception as e:
            logging.debug(f'[Watchdog] Chrome graceful 終了失敗: {e}')

        # Step 2: 最大10秒待つ
        _wait_chrome_exit(profile_dir, timeout=10.0)

        # Step 3: まだ生きていれば force kill
        try:
            result = _sp.run(
                ['powershell', '-NoProfile', '-Command',
                 f'$procs = Get-WmiObject Win32_Process -Filter "name=\'chrome.exe\'" | '
                 f'Where-Object {{ $_.CommandLine -like \'*{profile_name}*\' }}; '
                 f'if ($procs) {{ $procs | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}; Write-Output "forced" }}'],
                capture_output=True, text=True, timeout=15,
            )
            if 'forced' in (result.stdout or ''):
                logging.warning(f'[Watchdog] Chrome ({profile_name}) が graceful 終了せず — force kill しました')
            else:
                logging.info(f'[Watchdog] Chrome ({profile_name}) は graceful に終了しました')
        except Exception as e:
            logging.debug(f'[Watchdog] Chrome force kill 失敗: {e}')


def _run_self_repair():
    """修復サイクルをバックグラウンドスレッドで実行する"""
    from self_repair.repair_worker import is_repair_in_progress
    if is_repair_in_progress():
        return

    def _on_repaired(problem):
        import importlib
        # 修復成功後: まずモジュールを reload してから影響スレッドを再起動 (穴13修正)
        # file_to_thread: affected_fileのプレフィックス → (スレッド名, モジュール名)
        file_to_thread = {
            'scrapers/google':  ('yahoo',     'scrapers.google_scraper'),
            'scrapers/yahoo':   ('yahoo',     'scrapers.yahoo_scraper'),
            'scrapers/meta':    ('meta',      'scrapers.meta_scraper'),
            'processors/':      ('processor', 'processors.company_finder'),
            'storage/':         ('writer',    'storage.sheets_writer'),
        }
        # yahoo2 も Yahoo スクレイパーに依存するため同一モジュールを使用
        yahoo2_prefixes = {'scrapers/yahoo'}
        for prefix, (thread_name, mod_name) in file_to_thread.items():
            if problem.affected_file.startswith(prefix):
                # モジュールキャッシュを更新（スレッド再起動前に必須）
                try:
                    import sys as _sys
                    if mod_name in _sys.modules:
                        importlib.reload(_sys.modules[mod_name])
                        logging.info(f'[Repair] モジュール再読込完了: {mod_name}')
                except Exception as _e:
                    logging.warning(f'[Repair] モジュール再読込失敗 {mod_name}: {_e}')
                # 対象スレッドを再起動（yahoo修復時はyahoo2も再起動）
                restart_targets = [thread_name]
                if prefix in yahoo2_prefixes and 'yahoo2' in threads:
                    restart_targets.append('yahoo2')
                for tname in restart_targets:
                    thread = threads.get(tname)
                    if thread and thread.is_alive():
                        threads[tname] = None
                        logging.info(
                            f'[Repair] {tname} に再起動をリクエストしました'
                            f'（Watchdog経由、~30秒後に反映）'
                        )
                    else:
                        logging.info(f'[Repair] {tname} スレッドを再起動します')
                        _start_thread(tname)
                break

    t = threading.Thread(
        target=run_repair_cycle,
        args=(_on_repaired,),
        name='repair',
        daemon=True
    )
    t.start()


def _refill_keywords_if_empty():
    """アクティブなキーワードが残り5件以下になったらアーカイブを再有効化する"""
    try:
        conn = get_connection()
        for source in ('google_yahoo', 'auto_expanded', 'meta'):
            active_count = conn.execute(
                'SELECT COUNT(*) FROM keywords WHERE source=? AND is_archived=0',
                (source,)
            ).fetchone()[0]
            if active_count <= 5:
                restored = conn.execute(
                    'UPDATE keywords SET is_archived=0 WHERE source=? AND is_archived=1',
                    (source,)
                ).rowcount
                conn.commit()
                if restored > 0:
                    logging.info(
                        f'[Watchdog] {source} キーワード枯渇 → '
                        f'{restored}件を再有効化しました'
                    )
    except Exception as e:
        logging.error(f'[Watchdog] キーワード補充エラー: {e}')


def _process_control():
    if not CONTROL_PATH.exists():
        return
    try:
        # バイナリ読み込みでBOMを確実に除去（utf-8-sig + json.loads の相性問題を回避）
        raw_bytes = CONTROL_PATH.read_bytes()
        if raw_bytes.startswith(b'\xef\xbb\xbf'):  # UTF-8 BOM
            raw = raw_bytes[3:].decode('utf-8')
        elif raw_bytes.startswith(b'\xff\xfe'):  # UTF-16 LE BOM
            raw = raw_bytes.decode('utf-16-le', errors='replace').lstrip('﻿')
        elif raw_bytes.startswith(b'\xfe\xff'):  # UTF-16 BE BOM
            raw = raw_bytes.decode('utf-16-be', errors='replace').lstrip('﻿')
        else:
            raw = raw_bytes.decode('utf-8').lstrip('﻿')
        control = json.loads(raw)

        conn = get_connection()

        # add_keywords: 文字列 or {"keyword": "...", "source": "meta"} の両形式に対応
        for kw_entry in control.get('add_keywords', []):
            if isinstance(kw_entry, dict):
                add_keyword(conn, kw_entry['keyword'], kw_entry.get('source', 'google_yahoo'))
            else:
                add_keyword(conn, str(kw_entry))

        for kw in control.get('remove_keywords', []):
            archive_keyword(conn, kw)

        status = control.get('status', 'running')
        if status == 'paused' and not shutdown_event.is_set():
            if not pause_event.is_set():
                pause_event.set()
                logging.info('[Watchdog] control.json: paused — スクレイパーを一時停止しました')
        elif status == 'running' and pause_event.is_set():
            pause_event.clear()
            logging.info('[Watchdog] control.json: running — スクレイパーを再開しました')
        elif status == 'stopped':
            logging.info('[Watchdog] control.json: stopped — 終了します')
            shutdown_event.set()

        control['add_keywords'] = []
        control['remove_keywords'] = []
        # 原子書き込み: tmpファイルに書いてからrename
        tmp_fd, tmp_path = tempfile.mkstemp(dir=CONTROL_PATH.parent, suffix='.tmp')
        try:
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                json.dump(control, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, CONTROL_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise

    except Exception as e:
        logging.error(f'[Watchdog] control.json処理エラー: {e}')


def _start_thread(name: str):
    """スレッドを起動する（main.py の _start_thread を呼び出す）"""
    # 循環importを避けるため遅延import
    import main as _main
    _main._start_thread(name)


def watchdog_worker():
    logging.info('[Watchdog] 起動')
    check_interval = config.get('watchdog', {}).get('check_interval_seconds', 30)
    max_restart = config.get('watchdog', {}).get('max_restart_per_hour', 3)

    while not shutdown_event.is_set():
        time.sleep(check_interval)

        # config.json をランタイム再読み込み（timing/sources等の動的変更に対応）
        try:
            new_cfg = load_config()
            # clearとupdateを分離しない（瞬間的な空dict防止）
            stale_keys = [k for k in config if k not in new_cfg]
            config.update(new_cfg)
            for k in stale_keys:
                config.pop(k, None)
            check_interval = config.get('watchdog', {}).get('check_interval_seconds', 30)
            max_restart = config.get('watchdog', {}).get('max_restart_per_hour', 3)
        except Exception as e:
            logging.warning(f'[Watchdog] config.json再読み込み失敗（前回値を継続）: {e}')

        # control.json 処理
        _process_control()

        now = time.time()

        for name, thread in list(threads.items()):
            # yahoo2 が config で無効化されている場合は再起動しない
            if name == 'yahoo2' and not config.get('sources', {}).get('yahoo2', True):
                continue
            # ① スレッド死亡検知 → 再起動
            # _ctx_dead: sync_playwright().__exit__() がChromeクラッシュ後にハングし
            # is_alive()=True のままになるケースを検知するためのシグナル
            ctx_signaled = _ctx_dead.get(name, threading.Event()).is_set()
            if ctx_signaled:
                _ctx_dead[name].clear()
                if name in ('yahoo', 'yahoo2', 'meta'):
                    logging.error(
                        f'[Watchdog] {name} コンテキスト死亡シグナル検知 → '
                        f'Chrome終了 + 新スレッド起動（旧スレッドはdaemonとして回収）'
                    )
                    _kill_scraper_chrome(thread_name=name)
            if thread is None or not thread.is_alive() or ctx_signaled:
                hour_key = f'{name}_{int(now // 3600)}'
                count = thread_restart_count.get(hour_key, 0)

                if count >= max_restart:
                    # 上限超過時は諦めずに指数バックオフで再試行
                    # 1回目超過: 60s, 2回目: 120s, 3回目: 240s ... 最大1800s(30分)
                    excess = count - max_restart
                    backoff = min(60 * (2 ** excess), 1800)
                    last_restart = thread_restart_time.get(name, 0)
                    if now - last_restart < backoff:
                        remaining = int(backoff - (now - last_restart))
                        logging.warning(
                            f'[Watchdog] {name} バックオフ中 '
                            f'(残り{remaining}秒 / 通算{count}回再起動済み)'
                        )
                        continue
                    logging.warning(
                        f'[Watchdog] {name} バックオフ解除 → 再起動 '
                        f'(通算{count + 1}回目 / バックオフ{backoff}秒)'
                    )
                else:
                    logging.warning(f'[Watchdog] {name} 停止検知 → 再起動 ({count + 1}回目)')

                thread_restart_count[hour_key] = count + 1
                thread_restart_time[name] = now
                _start_thread(name)
                continue

            # ② ハング検知（HANG_TIMEOUT_SECONDS以上ハートビートなし）
            last_beat = _heartbeat.get(name, now)
            hang_duration = now - last_beat
            if hang_duration > HANG_TIMEOUT_SECONDS:
                logging.error(
                    f'[Watchdog] {name} ハング検知 '
                    f'({int(hang_duration)}秒間応答なし)'
                )
                # 20分以上ハングしていれば新スレッドを強制起動（ゾンビ置換）
                # 旧スレッドはdaemonなのでプロセス終了時に回収される
                if hang_duration > ZOMBIE_RESTART_SECONDS:
                    logging.error(
                        f'[Watchdog] {name} ゾンビ化検知 ({int(hang_duration)}秒) '
                        f'→ 旧Chromeを終了して新スレッドを強制起動します'
                    )
                    if name in ('yahoo', 'yahoo2', 'meta'):
                        _kill_scraper_chrome(thread_name=name)
                    _start_thread(name)
                    # 新スレッドがビートを更新するまでの猶予としてリセット
                    _heartbeat[name] = now

        # ③ キーワード自動補充（全アーカイブされたとき再有効化）
        _refill_keywords_if_empty()

        # ④ 自己修復サイクル（30分ごとに診断・必要なら修復）
        if int(now) % 1800 < check_interval:
            _run_self_repair()

        # ④-b 自動チューナー（2時間ごとにdelay/coolingを自動調整）
        if int(now) % 7200 < check_interval:
            try:
                run_auto_tuner(get_connection())
            except Exception as e:
                logging.error(f'[Watchdog] AutoTunerエラー: {e}')

        # ⑤ DBゴミクリーンアップ（10分ごと・自律実行）
        if int(now) % 600 < check_interval:
            try:
                run_db_janitor(get_connection())
            except Exception as e:
                logging.error(f'[Watchdog] Janitorエラー: {e}')

        # ⑥ status.html 更新（5分ごと）+ Google Drive 自動アップロード
        if int(now) % 300 < check_interval:
            try:
                generate_status_html(get_connection())
            except Exception as e:
                logging.error(f'[Watchdog] status.html生成エラー: {e}')
            share_emails = config.get('drive_share_emails', [])
            if share_emails:
                try:
                    sheets_cfg = config.get('google_sheets', {})
                    creds_path = str(BASE_DIR / sheets_cfg.get('service_account_key_path', 'credentials.json'))
                    html_path = str(BASE_DIR / 'status.html')
                    url = upload_status_html(creds_path, html_path, share_emails=share_emails)
                    if url:
                        logging.info(f'[Drive] 共有URL: {url}')
                except Exception as e:
                    logging.debug(f'[Watchdog] Drive アップロードエラー: {e}')

        # ⑦ 競合代理店事例スクレイプ（1日1回チェック・内部で週1回制御）
        if int(now) % 86400 < check_interval:
            try:
                from scrapers.case_study_scraper import run_case_study_scrape
                t = threading.Thread(
                    target=run_case_study_scrape,
                    kwargs={'conn': get_connection()},
                    name='case_study',
                    daemon=True,
                )
                t.start()
            except Exception as e:
                logging.error(f'[Watchdog] 競合事例スクレイプエラー: {e}')

    logging.info('[Watchdog] 終了')
