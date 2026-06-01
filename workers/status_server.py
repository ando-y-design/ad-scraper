import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import state
from utils.logger import get_logger

log = get_logger("status_server")

_STATUS_HTML = ""
_html_lock = threading.Lock()


def _build_html() -> str:
    metrics = state.get_metrics()
    threads = state.get_threads()
    now = time.time()
    elapsed = int(now - metrics.get("start_time", now))
    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)

    rows = []
    for name, t in threads.items():
        alive = "✅" if t.is_alive() else "❌ DEAD"
        last = state.last_beat(name)
        age = int(now - last) if last else -1
        age_str = f"{age}s ago" if last else "never"
        rows.append(f"<tr><td>{name}</td><td>{alive}</td><td>{age_str}</td></tr>")

    thread_table = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>ad_scraper status</title>
<style>body{{font-family:monospace;padding:20px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccc;padding:6px 12px;text-align:left}}
th{{background:#eee}}</style>
</head><body>
<h1>ad_scraper status</h1>
<p>Uptime: {h}h {m}m {s}s &nbsp;|&nbsp; Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}</p>
<h2>Metrics</h2>
<ul>
  <li>LP enqueued: {metrics.get('lp_enqueued', 0)}</li>
  <li>LP processed: {metrics.get('lp_processed', 0)}</li>
  <li>Companies saved: {metrics.get('companies_saved', 0)}</li>
  <li>Errors: {metrics.get('errors', 0)}</li>
  <li>lp_queue size: {state.lp_queue.qsize()}</li>
  <li>result_queue size: {state.result_queue.qsize()}</li>
</ul>
<h2>Threads</h2>
<table>
<tr><th>Name</th><th>Status</th><th>Last heartbeat</th></tr>
{thread_table}
</table>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _html_lock:
            html = _STATUS_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        pass  # suppress access log


def _refresh_loop() -> None:
    global _STATUS_HTML
    while not state.stop_event.is_set():
        try:
            html = _build_html()
            with _html_lock:
                _STATUS_HTML = html
        except Exception as e:
            log.debug(f"Status build error: {e}")
        time.sleep(30)


def run(port: int = 8080) -> None:
    global _STATUS_HTML
    _STATUS_HTML = _build_html()

    refresher = threading.Thread(target=_refresh_loop, daemon=True)
    refresher.start()

    # ポート競合時は最大5回リトライ（前プロセスが残っている間だけ失敗する）
    server = None
    for attempt in range(5):
        try:
            server = HTTPServer(("localhost", port), _Handler)
            log.info(f"Status server on http://localhost:{port}")
            break
        except OSError as e:
            if attempt < 4:
                log.debug(f"Status server port {port} busy, retrying in 5s ({e})")
                time.sleep(5)
            else:
                log.warning(f"Status server could not bind to port {port}, skipping")
                return

    try:
        while not state.stop_event.is_set():
            server.handle_request()
    except Exception as e:
        log.error(f"Status server error: {e}")
    log.info("status_server exited")
