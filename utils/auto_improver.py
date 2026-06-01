from __future__ import annotations
"""
自律改善エンジン v1 — Measure → Diagnose → Fix → Record

整合率・収集スピードを毎日計測し、失敗パターンを診断して
pending_fixes に自動登録する。

タスクスケジューラーから呼ばれる想定（7時・23時）:
  python -m utils.auto_improver

または単体デバッグ:
  python -m utils.auto_improver --dry-run    # pending_fixes に書かない
  python -m utils.auto_improver --no-check   # 整合率チェックをスキップ（高速）
"""

import argparse
import io
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
PENDING_FIXES_PATH = BASE_DIR / 'logs' / 'pending_fixes.jsonl'
ACCURACY_LOG_PATH  = BASE_DIR / 'logs' / 'accuracy_log.jsonl'
IMPROVE_LOG_PATH   = BASE_DIR / 'logs' / 'improve_log.jsonl'

# 整合率チェックのサンプル数
_CHECK_SAMPLE = 50

# 自動 pending_fix 生成のしきい値（割合）
_THRESHOLD_PHONE_MISSING    = 0.20   # phone_missing が 20% 超で pending_fix 生成
_THRESHOLD_COMPANY_MISSING  = 0.20   # company_missing が 20% 超
_THRESHOLD_NO_PAGE          = 0.35   # no_page が 35% 超
_THRESHOLD_BOTH_MISSING     = 0.20   # both_missing が 20% 超


# ─────────────────────────────────────────────────────────
# 収集速度計測
# ─────────────────────────────────────────────────────────

def _measure_speed(hours: int = 24) -> dict:
    """直近 hours 時間のDB取得件数から収集スピードを計算する。"""
    try:
        db_path = BASE_DIR / 'companies.db'
        conn = sqlite3.connect(str(db_path))
        since = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d')
        total = conn.execute(
            'SELECT COUNT(*) FROM companies WHERE found_date >= ?', (since,)
        ).fetchone()[0]
        today = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE found_date = date('now','localtime')"
        ).fetchone()[0]
        yesterday = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE found_date = date('now','localtime','-1 day')"
        ).fetchone()[0]
        grand_total = conn.execute('SELECT COUNT(*) FROM companies').fetchone()[0]
        conn.close()
        return {
            'recent_hours': hours,
            'recent_count': total,
            'today': today,
            'yesterday': yesterday,
            'grand_total': grand_total,
            'per_hour': round(total / hours, 2),
        }
    except Exception as e:
        return {'error': str(e)}


# ─────────────────────────────────────────────────────────
# 診断ロジック
# ─────────────────────────────────────────────────────────

def _diagnose(result: dict) -> list[dict]:
    """
    accuracy_checker の計測結果を解析して pending_fix 候補を返す。
    各要素: {problem_type, severity, description, affected_file, detail}
    """
    candidates: list[dict] = []
    checked = result.get('checked', 0)
    if checked < 5:
        return []  # サンプル不足では診断しない

    bd = result.get('breakdown', {})
    failures = result.get('failures', [])

    phone_missing_rate   = bd.get('phone_missing', 0)   / checked
    company_missing_rate = bd.get('company_missing', 0) / checked
    no_page_rate         = bd.get('no_page', 0)         / checked
    both_missing_rate    = bd.get('both_missing', 0)    / checked

    # ── 電話番号不一致が多い ─────────────────────────────
    if phone_missing_rate > _THRESHOLD_PHONE_MISSING:
        # ad_source 別に集計
        src_count: dict[str, int] = {}
        for f in failures:
            if f.get('failure_type') == 'phone_missing':
                src = f.get('ad_source', 'unknown')
                src_count[src] = src_count.get(src, 0) + 1
        top_src = max(src_count, key=src_count.get) if src_count else 'unknown'
        pct = f'{phone_missing_rate*100:.0f}%'
        candidates.append({
            'problem_type': 'accuracy_phone_missing',
            'severity': 'critical' if phone_missing_rate > 0.35 else 'warning',
            'affected_file': 'processors/company_finder.py',
            'description': (
                f'整合率チェック: 電話番号がLPに見つからない割合 {pct} (>{_THRESHOLD_PHONE_MISSING*100:.0f}%)。'
                f' 主なソース: {top_src}。'
                f' 抽出電話番号がLP掲載番号と一致していない可能性。'
                f' _pick_phone_from_tokutei_section の優先度向上を検討。'
            ),
            'sample_failures': [f for f in failures if f.get('failure_type') == 'phone_missing'][:3],
        })

    # ── 会社名不一致が多い ─────────────────────────────
    if company_missing_rate > _THRESHOLD_COMPANY_MISSING:
        pct = f'{company_missing_rate*100:.0f}%'
        # company_missing のサンプルから短い社名を確認
        short_names = [
            f['company_name'] for f in failures
            if f.get('failure_type') == 'company_missing'
            and len(f.get('company_name', '')) < 8
        ]
        note = f' 短い社名（{len(short_names)}件）が多い。コア抽出が問題の可能性。' if short_names else ''
        candidates.append({
            'problem_type': 'accuracy_company_missing',
            'severity': 'warning',
            'affected_file': 'processors/company_finder.py',
            'description': (
                f'整合率チェック: 会社名がLPに見つからない割合 {pct} (>{_THRESHOLD_COMPANY_MISSING*100:.0f}%)。'
                f'{note}'
                f' _strip_corp や _check_company_on_page の部分一致ロジックを改善することで解消する可能性がある。'
            ),
            'sample_failures': [f for f in failures if f.get('failure_type') == 'company_missing'][:3],
        })

    # ── LP取得失敗が多い ─────────────────────────────
    if no_page_rate > _THRESHOLD_NO_PAGE:
        pct = f'{no_page_rate*100:.0f}%'
        candidates.append({
            'problem_type': 'accuracy_no_page',
            'severity': 'warning',
            'affected_file': 'processors/company_finder.py',
            'description': (
                f'整合率チェック: LP取得失敗率 {pct} (>{_THRESHOLD_NO_PAGE*100:.0f}%)。'
                f' LP URLが無効化・リダイレクト先変更の可能性。'
                f' DBの古いエントリクリーンアップ、または scraper のURL有効性チェック強化を検討。'
            ),
            'sample_failures': [f for f in failures if f.get('failure_type') == 'no_page'][:3],
        })

    # ── 両方不一致（データ品質最悪ケース）─────────────────────────
    if both_missing_rate > _THRESHOLD_BOTH_MISSING:
        pct = f'{both_missing_rate*100:.0f}%'
        candidates.append({
            'problem_type': 'accuracy_both_missing',
            'severity': 'critical',
            'affected_file': 'processors/company_finder.py',
            'description': (
                f'整合率チェック: 会社名・電話番号の両方がLPに見つからない割合 {pct} (>{_THRESHOLD_BOTH_MISSING*100:.0f}%)。'
                f' LP URLと会社/電話情報のペアリングが根本的にずれている可能性。'
                f' SERP 由来のURL採用を見直し、特商法ページ優先度をさらに上げることを検討。'
            ),
            'sample_failures': [f for f in failures if f.get('failure_type') == 'both_missing'][:3],
        })

    return candidates


# ─────────────────────────────────────────────────────────
# pending_fixes 登録
# ─────────────────────────────────────────────────────────

def _already_pending(problem_type: str) -> bool:
    """同じ problem_type が既に pending で登録されているか確認する。"""
    if not PENDING_FIXES_PATH.exists():
        return False
    try:
        for line in PENDING_FIXES_PATH.read_text(encoding='utf-8').splitlines():
            try:
                entry = json.loads(line)
                if entry.get('problem_type') == problem_type and entry.get('status') == 'pending':
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _register_pending_fix(candidate: dict) -> bool:
    """candidate を pending_fixes.jsonl に追記する。"""
    PENDING_FIXES_PATH.parent.mkdir(exist_ok=True)
    entry = {
        'timestamp': datetime.now().isoformat(),
        'problem_type': candidate['problem_type'],
        'affected_file': candidate['affected_file'],
        'severity': candidate['severity'],
        'description': candidate['description'],
        'status': 'pending',
        'source': 'auto_improver',
        'sample_failures': candidate.get('sample_failures', []),
    }
    try:
        with PENDING_FIXES_PATH.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────
# 改善ログ記録
# ─────────────────────────────────────────────────────────

def _log_improve_run(run_data: dict) -> None:
    """improve_log.jsonl に実行記録を追記する。"""
    IMPROVE_LOG_PATH.parent.mkdir(exist_ok=True)
    try:
        with IMPROVE_LOG_PATH.open('a', encoding='utf-8') as f:
            f.write(json.dumps(run_data, ensure_ascii=False) + '\n')
    except Exception:
        pass


# ─────────────────────────────────────────────────────────
# トレンド分析
# ─────────────────────────────────────────────────────────

def _trend_summary(history: list[dict]) -> str:
    """過去計測の趨勢を1行で返す。"""
    valid = [h for h in history if h.get('seigoritsu') is not None]
    if not valid:
        return '（履歴なし）'
    if len(valid) == 1:
        return f'初回計測: {valid[0]["seigoritsu"]*100:.1f}%'

    latest = valid[0]['seigoritsu']
    oldest = valid[-1]['seigoritsu']
    delta = (latest - oldest) * 100
    direction = '↑改善中' if delta > 1 else '↓悪化中' if delta < -1 else '→横ばい'
    return (
        f'{direction} ({valid[-1]["timestamp"][:10]}→{valid[0]["timestamp"][:10]})'
        f'  {oldest*100:.1f}% → {latest*100:.1f}% ({delta:+.1f}pp)'
    )


# ─────────────────────────────────────────────────────────
# メインループ
# ─────────────────────────────────────────────────────────

def run(dry_run: bool = False, skip_check: bool = False, sample: int = _CHECK_SAMPLE) -> str:
    """
    Measure → Diagnose → Fix → Record を実行して結果レポートを返す。

    Args:
        dry_run:    True の場合 pending_fixes への書き込みをスキップ
        skip_check: True の場合 整合率チェック（LP取得）をスキップ（高速モード）
        sample:     整合率チェックのサンプル数
    """
    now = datetime.now()
    report_lines = [
        f'',
        f'━━━ 自律改善レポート  {now.strftime("%Y-%m-%d %H:%M")} ━━━',
    ]

    # ── 1. 収集スピード計測 ─────────────────────────────────────
    speed = _measure_speed(hours=24)
    if 'error' not in speed:
        report_lines += [
            f'',
            f'【収集スピード（直近24時間）】',
            f'  本日: {speed["today"]}件  昨日: {speed["yesterday"]}件  累計: {speed["grand_total"]}件',
            f'  時間あたり: {speed["per_hour"]}件/h',
        ]
    else:
        report_lines.append(f'  収集スピード計測エラー: {speed["error"]}')

    # ── 2. 整合率計測 ───────────────────────────────────────────
    check_result: dict = {}
    if not skip_check:
        report_lines += ['', f'【整合率チェック（{sample}件サンプル）】']
        from utils.accuracy_checker import run_check, save_result, load_history, format_report
        check_result = run_check(sample_size=sample, days=30, verbose=False)

        if 'error' in check_result:
            report_lines.append(f'  エラー: {check_result["error"]}')
        else:
            history = load_history(5)
            report_lines.append('  ' + format_report(check_result, history).replace('\n', '\n  '))
            report_lines.append(f'  トレンド: {_trend_summary(history)}')
            if not dry_run:
                save_result(check_result)
    else:
        # skip_check の場合は最新ログから読む
        from utils.accuracy_checker import load_history, format_report
        history = load_history(3)
        if history:
            check_result = history[0]
            report_lines += [
                '', '【整合率（最新ログ）】',
                '  ' + format_report(check_result, history).replace('\n', '\n  '),
                f'  トレンド: {_trend_summary(history)}',
            ]
        else:
            report_lines += ['', '【整合率】 ログなし（初回は --no-skip-check で実行してください）']

    # ── 3. 診断 ─────────────────────────────────────────────────
    fix_candidates: list[dict] = []
    if check_result and check_result.get('seigoritsu') is not None:
        fix_candidates = _diagnose(check_result)

    # ── 4. pending_fixes 登録 ────────────────────────────────────
    registered: list[str] = []
    skipped: list[str] = []
    if fix_candidates:
        report_lines += ['', '【診断結果】']
        for c in fix_candidates:
            ptype = c['problem_type']
            desc_short = c['description'][:80]
            sev_icon = '🔴' if c['severity'] == 'critical' else '🟡'
            if _already_pending(ptype):
                skipped.append(ptype)
                report_lines.append(f'  {sev_icon} {ptype}: 既にpending登録済み → スキップ')
            else:
                if not dry_run:
                    ok = _register_pending_fix(c)
                    if ok:
                        registered.append(ptype)
                        report_lines.append(f'  {sev_icon} {ptype}: pending_fix に新規登録 ✓')
                    else:
                        report_lines.append(f'  {sev_icon} {ptype}: 登録失敗')
                else:
                    report_lines.append(f'  {sev_icon} {ptype}: [dry-run] 登録スキップ')
    else:
        if check_result.get('seigoritsu') is not None:
            s = check_result['seigoritsu']
            if s >= 0.8:
                report_lines += ['', '【診断結果】 🟢 全指標正常（整合率 80%超）。修正候補なし。']
            else:
                report_lines += ['', '【診断結果】 しきい値以下の問題なし。継続モニタリング中。']

    # ── 5. 既存 pending_fixes のサマリー ────────────────────────
    from utils.daily_briefing import _read_pending_fixes
    pending_all = _read_pending_fixes()
    if pending_all:
        report_lines += ['', f'【未解決 pending_fixes ({len(pending_all)}件）】']
        for i, p in enumerate(pending_all, 1):
            sev = '🔴' if p.get('severity') == 'critical' else '🟡'
            report_lines.append(f'  {i}. {sev} [{p.get("problem_type","")}] {p.get("description","")[:60]}')

    # ── 6. 実行ログ記録 ─────────────────────────────────────────
    run_data = {
        'timestamp': now.isoformat(),
        'dry_run': dry_run,
        'speed': speed,
        'seigoritsu': check_result.get('seigoritsu'),
        'breakdown': check_result.get('breakdown'),
        'fix_candidates_count': len(fix_candidates),
        'registered': registered,
        'skipped': skipped,
    }
    if not dry_run:
        _log_improve_run(run_data)

    report_lines.append('')
    report_lines.append(f'━━━ 実行完了 ━━━')

    return '\n'.join(report_lines)


# ─────────────────────────────────────────────────────────
# 単体実行
# ─────────────────────────────────────────────────────────

if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    parser = argparse.ArgumentParser(description='自律改善エンジン (Measure → Diagnose → Fix → Record)')
    parser.add_argument('--dry-run', action='store_true', help='pending_fixes に書き込まない（テスト）')
    parser.add_argument('--no-check', action='store_true', help='整合率LP取得をスキップ（高速）')
    parser.add_argument('--sample', type=int, default=_CHECK_SAMPLE, help=f'サンプル数 (default: {_CHECK_SAMPLE})')
    args = parser.parse_args()

    report = run(dry_run=args.dry_run, skip_check=args.no_check, sample=args.sample)
    print(report)
