"""
日次ブリーフィング: 「状況は？」と聞かれたときに使うサマリー生成モジュール。

使い方:
    from utils.daily_briefing import generate_briefing
    print(generate_briefing(conn))

または単体実行:
    python -m utils.daily_briefing
"""
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
LOG_PATH = BASE_DIR / 'logs' / 'main.log'      # logger.pyが出力するファイル名に合わせる
REPAIR_LOG_PATH = BASE_DIR / 'logs' / 'repair_history.jsonl'
PENDING_FIXES_PATH = BASE_DIR / 'logs' / 'pending_fixes.jsonl'


# ─────────────────────────────────────────────
# ログ集計
# ─────────────────────────────────────────────

def _parse_log_stats(hours: int = 24) -> dict:
    """scraper.log から直近 N 時間の収集統計を集計する"""
    since = datetime.now() - timedelta(hours=hours)
    stats = {
        'google': 0, 'yahoo': 0, 'meta': 0,
        'captcha_yahoo': 0, 'captcha_google': 0,
        'errors': [],
        'browser_fail': 0,
        'log_start': None,
    }
    try:
        lines = LOG_PATH.read_text(encoding='utf-8', errors='replace').splitlines()
    except Exception:
        return stats

    for line in lines:
        # タイムスタンプ抽出
        m = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
        if ts < since:
            continue
        if stats['log_start'] is None:
            stats['log_start'] = ts

        low = line.lower()
        # 収集数カウント（実際のログフォーマットに合わせる）
        # Google: "[SerpAPI] N件取得 (provider)" or Playwright "Google N件"
        gm = re.search(r'SerpAPI\].*?(\d+)件取得', line) or \
             re.search(r'google.*?(\d+)件', line, re.IGNORECASE)
        if gm:
            stats['google'] += int(gm.group(1))
        # Yahoo: "Yahoo広告 N件発見" など
        ym = re.search(r'yahoo.*?(\d+)件', line, re.IGNORECASE)
        if ym:
            stats['yahoo'] += int(ym.group(1))
        # Meta: "[Meta] 広告 N件発見" など
        mm = re.search(r'meta.*?(\d+)件', line, re.IGNORECASE)
        if mm:
            stats['meta'] += int(mm.group(1))

        # CAPTCHA検知（大文字小文字を区別しない）
        if 'captcha' in low and 'yahoo' in low:
            stats['captcha_yahoo'] += 1
        if 'captcha' in low and 'google' in low:
            stats['captcha_google'] += 1

        # ブラウザ起動失敗
        if 'spawn unknown' in low or 'executable doesn' in low or '起動失敗' in line:
            stats['browser_fail'] += 1

        # エラー行収集（直近5件）
        if 'error' in low and len(stats['errors']) < 5:
            stats['errors'].append(line[20:100])  # タイムスタンプ除いて80字

    return stats


# ─────────────────────────────────────────────
# 未解決修正候補
# ─────────────────────────────────────────────

def _read_pending_fixes() -> list[dict]:
    """pending_fixes.jsonl から未解決（status=pending）の修正候補を返す"""
    fixes = []
    try:
        lines = PENDING_FIXES_PATH.read_text(encoding='utf-8').splitlines()
        seen_types = set()
        for line in reversed(lines):
            try:
                entry = json.loads(line)
                if entry.get('status') == 'pending':
                    ptype = entry.get('problem_type', '')
                    if ptype not in seen_types:
                        fixes.append(entry)
                        seen_types.add(ptype)
            except Exception:
                continue
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return fixes


# ─────────────────────────────────────────────
# 修復履歴
# ─────────────────────────────────────────────

def _read_repair_history(n: int = 3) -> list[dict]:
    try:
        lines = REPAIR_LOG_PATH.read_text(encoding='utf-8').splitlines()
        results = []
        for line in reversed(lines):
            try:
                results.append(json.loads(line))
                if len(results) >= n:
                    break
            except Exception:
                continue
        return results
    except Exception:
        return []


# ─────────────────────────────────────────────
# DB統計
# ─────────────────────────────────────────────

def _db_stats(conn) -> dict:
    try:
        total = conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0]
        today = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE found_date=date('now','localtime')"
        ).fetchone()[0]
        yesterday = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE found_date=date('now','localtime','-1 day')"
        ).fetchone()[0]
        unexported = conn.execute(
            'SELECT COUNT(*) FROM companies WHERE exported=0'
        ).fetchone()[0]
        return {'total': total, 'today': today, 'yesterday': yesterday, 'unexported': unexported}
    except Exception:
        return {'total': 0, 'today': 0, 'yesterday': 0, 'unexported': 0}


# ─────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────

def _read_latest_seigoritsu() -> str:
    """accuracy_log.jsonl から最新の整合率を1行で返す。"""
    log_path = BASE_DIR / 'logs' / 'accuracy_log.jsonl'
    if not log_path.exists():
        return '未計測（auto_improver.py を初回実行してください）'
    try:
        lines = log_path.read_text(encoding='utf-8').strip().splitlines()
        for line in reversed(lines):
            try:
                entry = json.loads(line)
                s = entry.get('seigoritsu')
                if s is None:
                    continue
                ts = entry.get('timestamp', '')[:16]
                pct = f'{s * 100:.1f}%'
                ok = entry.get('ok_count', 0)
                checked = entry.get('checked', 0)
                icon = '🟢' if s >= 0.8 else '🟡' if s >= 0.6 else '🔴'
                bd = entry.get('breakdown', {})
                detail = (
                    f'LP失敗={bd.get("no_page",0)} 電話不一致={bd.get("phone_missing",0)} '
                    f'社名不一致={bd.get("company_missing",0)} 両方不一致={bd.get("both_missing",0)}'
                )
                return f'{icon} {pct} ({ok}/{checked}件OK, {ts})  [{detail}]'
            except Exception:
                continue
    except Exception:
        pass
    return '読み取りエラー'


def generate_briefing(conn=None) -> str:
    """
    日次ブリーフィングテキストを生成して返す。
    conn: SQLite接続（Noneの場合はDB統計をスキップ）
    """
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    lines = [f'📊 ad_scraper ブリーフィング  {now_str}', '']

    # ── 整合率（最重要指標）─────────────────────
    lines.append('【整合率】')
    lines.append(f'  {_read_latest_seigoritsu()}')
    lines.append('')

    # ── 収集統計（直近24時間）──────────────────
    stats = _parse_log_stats(hours=24)
    total_lp = stats['google'] + stats['yahoo'] + stats['meta']
    lines.append('【直近24時間の収集】')
    lines.append(f'  LP取得合計: {total_lp}件')
    lines.append(f'  Google: {stats["google"]}件  Yahoo: {stats["yahoo"]}件  Meta: {stats["meta"]}件')
    if stats['captcha_yahoo'] or stats['captcha_google']:
        lines.append(f'  ⚠️  CAPTCHA検知: Yahoo {stats["captcha_yahoo"]}回 / Google {stats["captcha_google"]}回')
    if stats['browser_fail']:
        lines.append(f'  ❌ ブラウザ起動失敗: {stats["browser_fail"]}回')
    lines.append('')

    # ── DB統計 ────────────────────────────────
    if conn:
        db = _db_stats(conn)
        lines.append('【データベース】')
        lines.append(f'  本日取得: {db["today"]}件  昨日: {db["yesterday"]}件  累計: {db["total"]}件')
        if db['unexported'] > 0:
            lines.append(f'  ⚠️  未エクスポート: {db["unexported"]}件（Sheets未送信）')
        lines.append('')

    # ── 未解決の修正候補 ──────────────────────
    pending = _read_pending_fixes()
    if pending:
        lines.append(f'【修正候補（{len(pending)}件）】 ← 「N番やって」で実行')
        for i, fix in enumerate(pending, 1):
            sev_icon = '🔴' if fix.get('severity') == 'critical' else '🟡'
            ts = fix.get('timestamp', '')[:16]
            ptype = fix.get('problem_type', '')
            desc = fix.get('description', '')[:60]
            lines.append(f'  {i}. {sev_icon} [{ptype}] {desc}')
            lines.append(f'     対象: {fix.get("affected_file", "")}  検知: {ts}')
    else:
        lines.append('【修正候補】 なし（問題検知なし）')
    lines.append('')

    # ── 最近のエラー ──────────────────────────
    if stats['errors']:
        lines.append('【直近エラー（抜粋）】')
        for e in stats['errors'][:3]:
            lines.append(f'  {e}')
        lines.append('')

    # ── 修復履歴 ──────────────────────────────
    history = _read_repair_history(3)
    if history:
        lines.append('【直近の修復履歴】')
        for h in history:
            icon = '✅' if h.get('success') else '❌'
            ts = h.get('timestamp', '')[:16]
            lines.append(f'  {icon} {ts} {h.get("problem_type","")} → {h.get("detail","")}')
        lines.append('')

    return '\n'.join(lines)


def mark_fix_done(problem_type: str) -> bool:
    """pending_fixes.jsonl の指定problem_typeを done にマークする"""
    try:
        lines = PENDING_FIXES_PATH.read_text(encoding='utf-8').splitlines()
        new_lines = []
        marked = False
        for line in lines:
            try:
                entry = json.loads(line)
                if entry.get('problem_type') == problem_type and entry.get('status') == 'pending':
                    entry['status'] = 'done'
                    entry['done_at'] = datetime.now().isoformat()
                    marked = True
                new_lines.append(json.dumps(entry, ensure_ascii=False))
            except Exception:
                new_lines.append(line)
        PENDING_FIXES_PATH.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')
        return marked
    except Exception:
        return False


# ─────────────────────────────────────────────
# 単体実行
# ─────────────────────────────────────────────

if __name__ == '__main__':
    sys.path.insert(0, str(BASE_DIR))
    try:
        from storage.database import get_connection
        conn = get_connection()
    except Exception:
        conn = None
    print(generate_briefing(conn))
