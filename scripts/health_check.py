"""稼働状況ヘルスチェック（1時間ごとの監視用）。

スクレイパーの生存・直近ログのエラー・ソース別取得数・ハートビートを
1画面に要約する。修正判断は人間/エージェント側が行うため、ここは診断のみ。

使い方: python3 scripts/health_check.py
"""
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent.parent
PID_LOCK = BASE / 'logs' / 'scraper.pid'
DB = BASE / 'companies.db'


def _proc_alive(pid: int) -> bool:
    try:
        import psutil
        if not psutil.pid_exists(pid):
            return False
        return 'main.py' in ' '.join(psutil.Process(pid).cmdline())
    except Exception:
        # psutil 無しの簡易フォールバック
        try:
            import os
            os.kill(pid, 0)
            return True
        except Exception:
            return False


def check_process():
    print('== プロセス ==')
    pid_txt = ''
    try:
        pid_txt = PID_LOCK.read_text(encoding='utf-8').strip()
    except Exception:
        pass
    if not pid_txt:
        print('  [NG] PIDロックなし（未起動の可能性）')
        return False
    pid = int(pid_txt)
    alive = _proc_alive(pid)
    print(f'  PID={pid} : {"[OK] 稼働中" if alive else "[NG] 死亡（要再起動）"}')
    return alive


def latest_log():
    logs = sorted((BASE / 'logs').glob('run_*.out'), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def check_log():
    print('== 直近ログ ==')
    log = latest_log()
    if not log:
        print('  ログなし')
        return
    print(f'  file: {log.name}')
    try:
        lines = log.read_text(encoding='utf-8', errors='replace').splitlines()
    except Exception as e:
        print(f'  読込失敗: {e}')
        return
    tail = lines[-400:]
    # 直近1時間のソース別「件発見/取得」集計
    src_counts = {}
    err_lines = []
    cutoff = datetime.now() - timedelta(hours=1)
    pat_found = re.compile(r'(Google|Yahoo|Bing|Meta).*?(\d+)件発見')
    pat_ts = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
    for ln in tail:
        m = pat_found.search(ln)
        if m:
            src_counts[m.group(1)] = src_counts.get(m.group(1), 0) + int(m.group(2))
        if any(k in ln for k in ('ERROR', 'CRITICAL', 'CAPTCHA', 'クラッシュ',
                                 '接続失敗', '再接続失敗', '3回失敗', 'NTA')):
            err_lines.append(ln)
    print('  直近ログ内 ソース別発見数:', src_counts or '（なし）')
    print(f'  注意ログ {len(err_lines)}件（末尾5件）:')
    for ln in err_lines[-5:]:
        print('   ', ln[:160])


def check_db():
    print('== DB ==')
    if not DB.exists():
        print('  DBなし')
        return
    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM companies')
    print('  総件数:', cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM companies WHERE found_date = date('now','localtime')")
    print('  本日取得:', cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM companies WHERE corporate_number IS NOT NULL AND corporate_number != ''")
    print('  法人番号あり:', cur.fetchone()[0])
    cur.execute('SELECT ad_sources, COUNT(*) FROM companies GROUP BY ad_sources ORDER BY 2 DESC')
    for r in cur.fetchall():
        print('   ソース別:', r)
    # Sheets未反映の残り
    try:
        cur.execute('SELECT COUNT(*) FROM companies WHERE exported_at IS NULL')
        print('  Sheets未反映:', cur.fetchone()[0])
    except Exception:
        pass
    conn.close()


if __name__ == '__main__':
    print(f'==== ヘルスチェック {datetime.now():%Y-%m-%d %H:%M:%S} ====')
    alive = check_process()
    check_log()
    check_db()
    print('==== 終了 ====' + ('' if alive else '  ★プロセス再起動が必要'))
