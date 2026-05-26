"""
修復ワーカー: Watchdogから呼ばれる修復専用スレッドのエントリーポイント

【設計方針】
- 診断 → 問題検知 → pending_fixes.jsonl に記録（コードは触らない）
- config.json の auto_code_repair=true のときだけ実際に修復を試みる（デフォルト: false）
- サーキットブレーカー: 同一問題タイプで3回連続失敗 → 7日間封印
- REPAIR_SYSTEM_FAILING は廃止（自己参照ループの根本原因だったため）
"""
import importlib
import json
import logging
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from self_repair.diagnostics import get_diagnostics

_BASE_DIR = Path(__file__).parent.parent
_PENDING_FIXES_PATH = _BASE_DIR / 'logs' / 'pending_fixes.jsonl'


def _is_auto_code_repair_enabled() -> bool:
    """config.json の auto_code_repair フラグを読む（デフォルト: False）"""
    try:
        cfg = json.loads((_BASE_DIR / 'config.json').read_text(encoding='utf-8'))
        return bool(cfg.get('auto_code_repair', False))
    except Exception:
        return False


def _write_pending_fix(problem) -> None:
    """問題をpending_fixes.jsonlに記録する（コードは変更しない）"""
    try:
        _PENDING_FIXES_PATH.parent.mkdir(exist_ok=True)
        entry = {
            'timestamp': datetime.now().isoformat(),
            'problem_type': problem.type.value,
            'affected_file': problem.affected_file,
            'severity': problem.severity,
            'description': problem.description,
            'status': 'pending',
        }
        with open(_PENDING_FIXES_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        logging.info(
            f'[Repair] 修正候補を記録しました（コードは変更していません）: '
            f'{problem.type.value} → {problem.affected_file}'
        )
    except Exception as e:
        logging.warning(f'[Repair] pending_fixes 書き込み失敗: {e}')

# 修復は同時に1件のみ実行する
_repair_lock = threading.Lock()
_repair_in_progress = False

# サーキットブレーカー: 問題タイプごとの連続失敗カウント
_consecutive_failures: dict = defaultdict(int)
_CIRCUIT_BREAKER_THRESHOLD = 3      # 連続N回失敗でトリップ
_CIRCUIT_BREAKER_COOLDOWN = 7 * 24 * 3600  # トリップ後7日間封印


def _load_attempt_repair():
    """self_repair.repairer をディスクから再ロードして attempt_repair を返す。"""
    try:
        module_name = 'self_repair.repairer'
        if module_name in sys.modules:
            importlib.reload(sys.modules[module_name])
        import self_repair.repairer as _repairer
        return _repairer.attempt_repair
    except Exception as e:
        logging.warning(f'[Repair] repairer再ロード失敗（前回版を使用）: {e}')
        from self_repair.repairer import attempt_repair
        return attempt_repair


def run_repair_cycle(on_repaired=None):
    """
    診断 → 問題検知 → 修復 のサイクルを1回実行する。
    on_repaired(problem): 修復成功時に呼ばれるコールバック（スレッド再起動等に使う）
    """
    global _repair_in_progress

    if not _repair_lock.acquire(blocking=False):
        logging.debug('[Repair] 修復が既に進行中です。スキップ')
        return

    _repair_in_progress = True
    try:
        attempt_repair = _load_attempt_repair()

        diag = get_diagnostics()
        logging.info(f'[Repair] 診断: {diag.summary()}')

        problems = diag.diagnose()
        if not problems:
            logging.debug('[Repair] 問題なし')
            return

        for problem in problems:
            logging.warning(
                f'[Repair] 問題検知: [{problem.severity}] '
                f'{problem.type.value} — {problem.description}'
            )

        # 優先順位: critical → warning
        problems.sort(key=lambda p: 0 if p.severity == 'critical' else 1)
        target = problems[0]

        # auto_code_repair が無効（デフォルト）の場合は pending に記録して終了
        if not _is_auto_code_repair_enabled():
            _write_pending_fix(target)
            diag.mark_repair_failed(target.type, cooldown_seconds=3600)  # 1時間クールダウン
            return

        success = attempt_repair(target)
        if success:
            # 成功 → 連続失敗カウントリセット
            _consecutive_failures[target.type] = 0
            diag.mark_repaired(target.type)
            logging.info(
                f'[Repair] {target.affected_file} の修復が完了しました。'
                f'関連スレッドを再起動します。'
            )
            if on_repaired:
                on_repaired(target)
        else:
            # 失敗 → サーキットブレーカー判定
            _consecutive_failures[target.type] += 1
            consecutive = _consecutive_failures[target.type]

            if consecutive >= _CIRCUIT_BREAKER_THRESHOLD:
                # サーキットブレーカートリップ: 7日間封印
                cooldown = _CIRCUIT_BREAKER_COOLDOWN
                days = cooldown // 86400
                logging.error(
                    f'[Repair] {target.type.value} が{consecutive}回連続失敗。'
                    f'サーキットブレーカー発動 → {days}日間スキップします。'
                )
                _consecutive_failures[target.type] = 0  # カウントリセット
            else:
                # 通常の失敗クールダウン: 30分
                cooldown = 1800
                remaining = _CIRCUIT_BREAKER_THRESHOLD - consecutive
                logging.error(
                    f'[Repair] {target.affected_file} の修復に失敗（{consecutive}回連続 / '
                    f'あと{remaining}回でサーキットブレーカー）。'
                    f'{cooldown // 60}分後に再試行します。'
                )

            diag.mark_repair_failed(target.type, cooldown)

    except Exception as e:
        logging.error(f'[Repair] 修復サイクル中にエラー: {e}', exc_info=True)
    finally:
        _repair_in_progress = False
        _repair_lock.release()


def is_repair_in_progress() -> bool:
    return _repair_in_progress
