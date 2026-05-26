"""
status.html を生成するモジュール。Watchdogから5分ごとに呼ばれる。
"""
import json
import logging
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
STATUS_PATH = BASE_DIR / 'status.html'
REPAIR_LOG_PATH = BASE_DIR / 'logs' / 'repair_history.jsonl'
ERROR_LOG_PATH = BASE_DIR / 'logs' / 'error.log'
SHARE_URL_PATH = BASE_DIR / 'share_url.txt'


def _read_share_url() -> str:
    try:
        return SHARE_URL_PATH.read_text(encoding='utf-8').strip()
    except Exception:
        return ''


def _read_repair_history(n: int = 5) -> list[dict]:
    try:
        lines = REPAIR_LOG_PATH.read_text(encoding='utf-8').strip().splitlines()
        entries = []
        for line in reversed(lines[-n * 2:]):
            try:
                entries.append(json.loads(line))
                if len(entries) >= n:
                    break
            except Exception:
                continue
        return entries
    except Exception:
        return []


def _read_recent_errors(n: int = 8) -> list[str]:
    try:
        lines = ERROR_LOG_PATH.read_text(encoding='utf-8').strip().splitlines()
        return lines[-n:]
    except Exception:
        return []


def _esc(text: str) -> str:
    return (str(text)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;'))


def generate_status_html(conn) -> None:
    try:
        _write_html(conn)
    except Exception as e:
        logging.error(f'[Status] HTML生成失敗: {e}')


def _write_html(conn) -> None:
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    share_url = _read_share_url()

    # ── DB統計 ──────────────────────────────────────
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    exported = conn.execute("SELECT COUNT(*) FROM companies WHERE exported=1").fetchone()[0]
    today = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE found_date=date('now','localtime')"
    ).fetchone()[0]

    source_rows = conn.execute(
        "SELECT ad_sources, COUNT(*) as cnt FROM companies "
        "GROUP BY ad_sources ORDER BY cnt DESC"
    ).fetchall()

    recent_rows = conn.execute(
        "SELECT company_name, phone, keyword, ad_sources, found_date "
        "FROM companies ORDER BY id DESC LIMIT 15"
    ).fetchall()

    keyword_rows = conn.execute(
        "SELECT keyword, source, total_found, last_searched "
        "FROM keywords WHERE is_archived=0 ORDER BY total_found DESC LIMIT 10"
    ).fetchall()

    repairs = _read_repair_history()
    errors = _read_recent_errors()

    # ── ソース集計 ──────────────────────────────────
    source_map: dict[str, int] = {}
    for r in source_rows:
        for src in str(r[0] or '').split('+'):
            src = src.strip()
            if src:
                source_map[src] = source_map.get(src, 0) + r[1]

    google_cnt = source_map.get('Google', 0)
    yahoo_cnt = source_map.get('Yahoo', 0)
    meta_cnt = source_map.get('Meta', 0)

    # ── HTML ────────────────────────────────────────
    html = f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="60">
<title>ad_scraper ステータス</title>
<style>
  body {{ font-family: "Segoe UI", sans-serif; background:#f0f2f5; margin:0; padding:20px; color:#333; }}
  h1 {{ font-size:1.4rem; margin:0 0 4px; }}
  .updated {{ color:#888; font-size:.85rem; margin-bottom:20px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:12px; margin-bottom:20px; }}
  .card {{ background:#fff; border-radius:8px; padding:16px; box-shadow:0 1px 3px rgba(0,0,0,.1); }}
  .card h2 {{ font-size:.8rem; color:#888; margin:0 0 6px; text-transform:uppercase; letter-spacing:.05em; }}
  .card .val {{ font-size:2rem; font-weight:700; color:#1a73e8; }}
  .card .sub {{ font-size:.8rem; color:#888; margin-top:2px; }}
  .section {{ background:#fff; border-radius:8px; padding:16px; margin-bottom:16px; box-shadow:0 1px 3px rgba(0,0,0,.1); }}
  .section h2 {{ font-size:1rem; margin:0 0 12px; border-bottom:1px solid #eee; padding-bottom:8px; }}
  table {{ width:100%; border-collapse:collapse; font-size:.85rem; }}
  th {{ text-align:left; color:#888; font-weight:600; padding:4px 8px; }}
  td {{ padding:5px 8px; border-top:1px solid #f0f0f0; }}
  tr:hover td {{ background:#f8f9fa; }}
  .src-g {{ color:#34a853; }} .src-y {{ color:#ea4335; }} .src-m {{ color:#1877f2; }}
  .ok {{ color:#34a853; }} .err {{ color:#ea4335; }}
  .error-box {{ background:#fff8f8; border-left:3px solid #ea4335; padding:8px 12px; font-size:.8rem;
               font-family:monospace; white-space:pre-wrap; word-break:break-all; color:#555; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:12px; font-size:.75rem; font-weight:600; }}
  .badge-ok {{ background:#e8f5e9; color:#2e7d32; }}
  .badge-err {{ background:#ffebee; color:#c62828; }}
  .share-bar {{ background:#fff; border-radius:8px; padding:14px 16px; margin-bottom:20px;
               box-shadow:0 1px 3px rgba(0,0,0,.1); display:flex; align-items:center; gap:12px; }}
  .share-bar span {{ font-size:.8rem; color:#888; white-space:nowrap; }}
  .share-url {{ flex:1; font-size:.9rem; color:#1a73e8; word-break:break-all; }}
  .copy-btn {{ background:#1a73e8; color:#fff; border:none; border-radius:6px;
              padding:7px 16px; font-size:.85rem; cursor:pointer; white-space:nowrap; }}
  .copy-btn:active {{ background:#1558b0; }}
  .copy-ok {{ background:#34a853 !important; }}
</style>
</head>
<body>
<h1>📊 ad_scraper ステータス</h1>
<p class="updated">最終更新: {_esc(now)} （60秒ごと自動更新）</p>

{f"""<div class="share-bar">
  <span>共有URL</span>
  <span class="share-url" id="shareUrl">{_esc(share_url)}</span>
  <button class="copy-btn" onclick="copyUrl()">コピー</button>
</div>
<script>
function copyUrl(){{
  navigator.clipboard.writeText('{share_url}').then(function(){{
    var btn=document.querySelector('.copy-btn');
    btn.textContent='コピーしました！';
    btn.classList.add('copy-ok');
    setTimeout(function(){{btn.textContent='コピー';btn.classList.remove('copy-ok');}},2000);
  }});
}}
</script>""" if share_url else ''}

<div class="grid">
  <div class="card">
    <h2>総取得件数</h2>
    <div class="val">{total}</div>
    <div class="sub">Sheets出力済: {exported}件</div>
  </div>
  <div class="card">
    <h2>本日取得</h2>
    <div class="val">{today}</div>
    <div class="sub">&nbsp;</div>
  </div>
  <div class="card">
    <h2>Google</h2>
    <div class="val src-g">{google_cnt}</div>
    <div class="sub">累計</div>
  </div>
  <div class="card">
    <h2>Yahoo</h2>
    <div class="val src-y">{yahoo_cnt}</div>
    <div class="sub">累計</div>
  </div>
  <div class="card">
    <h2>Meta</h2>
    <div class="val src-m">{meta_cnt}</div>
    <div class="sub">累計</div>
  </div>
</div>

<div class="section">
  <h2>直近15件</h2>
  <table>
    <tr><th>会社名</th><th>電話番号</th><th>キーワード</th><th>ソース</th><th>日付</th></tr>
    {''.join(
        f'<tr><td>{_esc(r["company_name"])}</td>'
        f'<td>{_esc(r["phone"] or "")}</td>'
        f'<td>{_esc(r["keyword"] or "")}</td>'
        f'<td>{_esc(r["ad_sources"] or "")}</td>'
        f'<td>{_esc(r["found_date"])}</td></tr>'
        for r in recent_rows
    ) if recent_rows else '<tr><td colspan="5" style="color:#aaa;text-align:center">データなし</td></tr>'}
  </table>
</div>

<div class="section">
  <h2>アクティブキーワード（取得数上位）</h2>
  <table>
    <tr><th>キーワード</th><th>ソース</th><th>取得数</th><th>最終検索</th></tr>
    {''.join(
        f'<tr><td>{_esc(r["keyword"])}</td>'
        f'<td>{_esc(r["source"])}</td>'
        f'<td>{r["total_found"]}</td>'
        f'<td>{_esc(r["last_searched"][:16] if r["last_searched"] else "未検索")}</td></tr>'
        for r in keyword_rows
    )}
  </table>
</div>

<div class="section">
  <h2>自己修復履歴（直近5件）</h2>
  {'<table><tr><th>日時</th><th>対象ファイル</th><th>問題種別</th><th>結果</th></tr>' +
   ''.join(
       f'<tr><td>{_esc(r.get("timestamp","")[:16])}</td>'
       f'<td>{_esc(r.get("affected_file",""))}</td>'
       f'<td>{_esc(r.get("problem_type",""))}</td>'
       f'<td><span class="badge {"badge-ok" if r.get("success") else "badge-err"}">'
       f'{"成功" if r.get("success") else "失敗"}</span></td></tr>'
       for r in repairs
   ) + '</table>'
   if repairs else '<p style="color:#aaa;font-size:.85rem">修復履歴なし</p>'}
</div>

<div class="section">
  <h2>最近のエラー</h2>
  {''.join(f'<div class="error-box">{_esc(e)}</div>' for e in errors)
   if errors else '<p style="color:#aaa;font-size:.85rem">エラーなし</p>'}
</div>

</body>
</html>'''

    tmp_path = STATUS_PATH.with_suffix('.tmp')
    tmp_path.write_text(html, encoding='utf-8')
    tmp_path.replace(STATUS_PATH)
