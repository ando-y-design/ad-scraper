"""
共有状態モジュール: 全ワーカーが参照するグローバル変数を一箇所に集約する。
各ワーカーファイルはここから import して使う。
config は dict のまま（mutable）なので update() で更新すれば全モジュールに即反映される。
"""
import queue
import threading
from pathlib import Path

BASE_DIR = Path(__file__).parent

# ── 設定・制御 ──────────────────────────────────────────────────────────
config: dict = {}

# ── シャットダウン / 一時停止 ────────────────────────────────────────────
shutdown_event = threading.Event()
pause_event = threading.Event()

# ── スレッド間キュー ─────────────────────────────────────────────────────
lp_queue: queue.Queue = queue.Queue(maxsize=200)
result_queue: queue.Queue = queue.Queue(maxsize=100)

# ── スレッド管理 ─────────────────────────────────────────────────────────
threads: dict = {
    'yahoo': None,
    'yahoo2': None,
    'meta': None,
    'processor': None,
    'writer': None,
}
thread_restart_count: dict = {}
thread_restart_time: dict = {}

# ── ハートビート ─────────────────────────────────────────────────────────
_heartbeat: dict = {}

HANG_TIMEOUT_SECONDS = 300    # 5分応答なしでハング警告
ZOMBIE_RESTART_SECONDS = 120  # 2分応答なしで新スレッド強制起動

# ── コンテキスト死亡シグナル ─────────────────────────────────────────────
# sync_playwright().__exit__() がChromeクラッシュ後にハングするため、
# is_alive()=True のままになるケースをWatchdogに即時通知する。
_ctx_dead: dict = {
    'yahoo': threading.Event(),
    'yahoo2': threading.Event(),
    'meta': threading.Event(),
}

META_FAILURE_ALERT_THRESHOLD = 5


def beat(name: str) -> None:
    """スレッドが生存中であることを記録する"""
    _heartbeat[name] = __import__('time').time()
