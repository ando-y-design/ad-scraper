from __future__ import annotations
from typing import Optional
import json
import logging
import random
import shutil
import subprocess
import time
from pathlib import Path

from playwright_stealth import Stealth

_stealth = Stealth(navigator_languages_override=('ja-JP', 'ja'))

PROFILE_DIR = (Path(__file__).parent.parent / 'browser_profile').resolve()

# ロック・クラッシュ系（再起動のたびに消す）
_LOCK_FILES = (
    'LOCK', 'lockfile', 'SingletonLock', 'SingletonSocket',
    'SingletonCookie', 'CrashpadMetrics-active.pma',
)

# キャッシュ系ディレクトリ（破損しやすいが消しても無害）
_CACHE_DIRS = (
    'Cache', 'GPUCache', 'Code Cache',
    'DawnGraphiteCache', 'DawnWebGPUCache', 'ShaderCache',
)

# nuclear reset 時に保持するファイル（Facebook ログイン状態）
_PRESERVE_IN_DEFAULT = frozenset({
    'Cookies', 'Cookies-journal',
    'Login Data', 'Login Data-journal',
    'Local State',
    'Web Data', 'Web Data-journal',
})


def _cleanup_locks(profile_dir: Path) -> None:
    for name in _LOCK_FILES:
        for p in (profile_dir / name, profile_dir / 'Default' / name):
            if p.exists():
                try:
                    p.unlink()
                    logging.warning(f'[Browser] ロックファイル削除: {p.relative_to(profile_dir)}')
                except Exception as e:
                    logging.debug(f'[Browser] ロックファイル削除失敗 {p.name}: {e}')


def _cleanup_caches(profile_dir: Path) -> None:
    default_dir = profile_dir / 'Default'
    for name in _CACHE_DIRS:
        for target in (profile_dir / name, default_dir / name):
            if target.exists():
                try:
                    shutil.rmtree(target, ignore_errors=True)
                    logging.info(f'[Browser] キャッシュ削除: {target.relative_to(profile_dir)}')
                except Exception:
                    pass

    # Preferences が JSON として壊れていたら削除
    prefs = default_dir / 'Preferences'
    if prefs.exists():
        try:
            json.loads(prefs.read_text(encoding='utf-8', errors='replace'))
        except Exception:
            prefs.unlink(missing_ok=True)
            logging.warning('[Browser] 破損した Preferences を削除')


def _nuclear_reset(profile_dir: Path) -> None:
    """
    ログイン情報だけ残してプロファイルを最小構成にリセットする。
    通常のキャッシュ削除でも起動できない場合の最終手段。
    """
    default_dir = profile_dir / 'Default'
    if not default_dir.exists():
        return

    deleted = []
    for item in list(default_dir.iterdir()):
        if item.name in _PRESERVE_IN_DEFAULT:
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            deleted.append(item.name)
        except Exception:
            pass

    # Default 直下以外のロック・キャッシュも消す
    _cleanup_locks(profile_dir)
    for name in _CACHE_DIRS:
        p = profile_dir / name
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)

    logging.warning(f'[Browser] nuclear reset 完了: {len(deleted)} 項目を削除 '
                    f'(保持: {sorted(_PRESERVE_IN_DEFAULT)})')


def _wait_chrome_exit(profile_dir: Path, timeout: float = 20.0) -> None:
    """このプロファイルを使っている Chrome が全部終了するまで最大 timeout 秒待つ"""
    profile_name = profile_dir.name
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 f'(Get-WmiObject Win32_Process -Filter "name=\'chrome.exe\'" | '
                 f'Where-Object {{ $_.CommandLine -like \'*{profile_name}*\' }}).Count'],
                capture_output=True, text=True, timeout=6,
            )
            if int(result.stdout.strip() or '0') == 0:
                return
        except Exception:
            return
        time.sleep(1.5)
    logging.warning('[Browser] Chrome 終了待機タイムアウト — 続行します')


_LAUNCH_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--disable-infobars',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-sync',
    '--window-position=-32000,-32000',  # 画面外に配置して非表示に
    # ── リソース節約（PC負荷軽減）──────────────────────────────
    '--blink-settings=imagesEnabled=false',   # 画像読み込みを全停止（広告リンク抽出に不要）
    '--disable-extensions',                    # 拡張機能無効
    '--disable-background-networking',         # バックグラウンドネットワーク処理無効
    '--disable-background-timer-throttling',   # バックグラウンドタイマー無効
    '--disable-default-apps',                  # デフォルトアプリ無効
    '--js-flags=--max-old-space-size=128',    # V8ヒープ上限128MB（デフォルト無制限）
    '--renderer-process-limit=2',              # レンダラープロセス数上限（メモリ削減）
    '--disable-dev-shm-usage',                 # 共有メモリ使用無効（低メモリ環境での安定性向上）
    '--disable-gpu-shader-disk-cache',         # GPUシェーダーキャッシュ無効
]

# headlessモード時に "HeadlessChrome" UA を隠すため Chrome 実ブラウザUAを明示する
# tmual（Yahoo広告インジェクション）がHeadlessChrome UAを検知して広告を非表示にするため
_CHROME_USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36',
]


def _try_launch(playwright, profile_dir: Path, headless: bool = False):
    # channel='chrome' はシステムChrome を使うが、Chrome 148 以降で
    # --remote-debugging-pipe の互換性が壊れ exitCode=21 で即死する。
    # Playwright 同梱の Chromium (147) を使うことで安定起動する。
    # 一般的な日本語環境のディスプレイ解像度をランダム選択（Bot指紋対策）
    # メモリ節約のため小さめのviewportを使う（スクレイピング精度に影響なし）
    _VIEWPORTS = [
        {'width': 1280, 'height': 800},
        {'width': 1366, 'height': 768},
        {'width': 1024, 'height': 768},
    ]
    viewport = random.choice(_VIEWPORTS)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        viewport=viewport,
        locale='ja-JP',
        timezone_id='Asia/Tokyo',
        ignore_https_errors=True,
        args=_LAUNCH_ARGS,
        user_agent=random.choice(_CHROME_USER_AGENTS),
    )


def create_browser_context(playwright, profile_dir: Optional[Path] = None):
    """
    Chrome を起動して Playwright コンテキストを返す。
    profile_dir を指定すると別プロファイルで起動できる（並列ワーカー用）。
    段階的フォールバック:
      試行1: headed モード（ロック削除のみ）
      試行2: headed モード + キャッシュ削除
      試行3: headed モード + nuclear reset
      試行4: headless モード + nuclear reset（セッションロック・GPU 不在対策）
    """
    if profile_dir is None:
        profile_dir = PROFILE_DIR

    profile_dir.mkdir(exist_ok=True)
    _wait_chrome_exit(profile_dir)
    _cleanup_locks(profile_dir)

    steps = [
        # (headless, pre_action, label)
        # headless=True を優先: headed (False) は spawn UNKNOWN で即死するため
        (True,  None,                                  'headlessモード'),
        (True,  _cleanup_caches,                       'headless + キャッシュ削除後'),
        (True,  _nuclear_reset,                        'headless + nuclear reset後'),
        (False, None,                                  'headedモード（最終手段）'),
    ]

    last_exc = None
    for headless, pre_action, label in steps:
        if pre_action:
            pre_action(profile_dir)
        _cleanup_locks(profile_dir)
        try:
            ctx = _try_launch(playwright, profile_dir, headless=headless)
            mode = 'headless' if headless else 'headed'
            logging.info(f'ブラウザコンテキスト作成完了 ({label}, {mode}, profile={profile_dir.name})')
            return ctx
        except Exception as e:
            last_exc = e
            logging.warning(f'[Browser] 起動失敗 ({label}): {type(e).__name__}')
            _wait_chrome_exit(profile_dir)
            time.sleep(3)

    raise RuntimeError(f'[Browser] 全フォールバック試行失敗: {last_exc}')


def new_stealth_page(context):
    page = context.new_page()
    _stealth.apply_stealth_sync(page)
    page.set_default_navigation_timeout(20000)
    page.set_default_timeout(15000)
    return page
