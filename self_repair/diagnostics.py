from __future__ import annotations
"""
自己診断モジュール: 収集成功率・エラー頻度を記録し、問題を検知する
"""
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

_BASE_DIR = Path(__file__).parent.parent
_COOLDOWN_FILE = _BASE_DIR / 'logs' / 'repair_cooldown.json'


def _load_cooldown_file() -> dict:
    """修復クールダウン状態をファイルから読み込む"""
    try:
        if _COOLDOWN_FILE.exists():
            data = json.loads(_COOLDOWN_FILE.read_text(encoding='utf-8'))
            now = time.time()
            # 期限切れのエントリを除去
            return {k: v for k, v in data.items() if v > now}
    except Exception:
        pass
    return {}


def _save_cooldown_file(last_repair: dict):
    """修復クールダウン状態をファイルに保存する"""
    try:
        _COOLDOWN_FILE.parent.mkdir(exist_ok=True)
        _COOLDOWN_FILE.write_text(
            json.dumps(last_repair, ensure_ascii=False), encoding='utf-8'
        )
    except Exception as e:
        logging.debug(f'[Diag] クールダウンファイル保存失敗: {e}')


class ProblemType(Enum):
    GOOGLE_ZERO_RESULTS = 'google_zero_results'
    YAHOO_ZERO_RESULTS = 'yahoo_zero_results'
    META_SESSION_EXPIRED = 'meta_session_expired'
    COMPANY_EXTRACTION_LOW = 'company_extraction_low'
    SHEETS_WRITE_FAILING = 'sheets_write_failing'
    SCRAPER_HANG = 'scraper_hang'
    # REPAIR_SYSTEM_FAILING は廃止:
    # 「修復システムが50KB超のファイルを10KBに切り捨てて自己生成」という
    # 設計ミスが根本原因。自己参照ループを引き起こすため完全削除。


@dataclass
class Problem:
    type: ProblemType
    description: str
    affected_file: str
    severity: str  # 'critical' / 'warning'
    detected_at: float = field(default_factory=time.time)


class Diagnostics:
    def __init__(self, window_seconds: int = 3600):
        self._lock = threading.Lock()
        self._window = window_seconds

        # 各ソースの収集件数（タイムスタンプ付き）
        self._google_counts: deque = deque()   # (timestamp, count)
        self._yahoo_counts: deque = deque()
        self._meta_counts: deque = deque()

        # 会社名取得成功/失敗
        self._extraction_success: deque = deque()  # (timestamp, bool)

        # Sheets書き込みエラー
        self._sheets_errors: deque = deque()   # timestamp

        # Meta連続失敗カウンタ
        self._meta_consecutive_failures: int = 0

        # 最後の修復時刻（過剰修復防止）- ファイルから復元してプロセス再起動後もクールダウンを維持
        self._repair_cooldown = 7200  # 2時間は同じ問題を再修復しない
        raw_cooldown = _load_cooldown_file()
        self._last_repair: dict[ProblemType, float] = {}
        for k, v in raw_cooldown.items():
            try:
                pt = ProblemType(k)
                self._last_repair[pt] = v - self._repair_cooldown  # until → last_repairに変換
            except ValueError:
                pass

    # ─────────────────────────────────────────────
    # 記録API
    # ─────────────────────────────────────────────
    def record_scrape(self, source: str, count: int):
        now = time.time()
        with self._lock:
            if source == 'Google':
                self._google_counts.append((now, count))
            elif source == 'Yahoo':
                self._yahoo_counts.append((now, count))
            elif source == 'Meta':
                self._meta_counts.append((now, count))

    def record_extraction(self, success: bool):
        with self._lock:
            self._extraction_success.append((time.time(), success))

    def record_sheets_error(self):
        with self._lock:
            self._sheets_errors.append(time.time())

    def record_meta_failure(self):
        """Metaセッション切れを記録する（main.pyから呼び出す）"""
        with self._lock:
            self._meta_consecutive_failures += 1

    def reset_meta_failures(self):
        """Meta収集成功時にカウンタをリセット"""
        with self._lock:
            self._meta_consecutive_failures = 0

    # ─────────────────────────────────────────────
    # 診断
    # ─────────────────────────────────────────────
    def _sync_cooldown_from_file(self):
        """
        クールダウンファイルを再読み込みしてin-memoryと同期する。
        ファイル側が長い（より未来の until）場合は上書き採用。
        _lock の中から呼ぶこと。
        """
        try:
            file_data = _load_cooldown_file()  # {key: until_timestamp} 期限切れ除外済み
            for k, until in file_data.items():
                try:
                    pt = ProblemType(k)
                    # until → last_repair の変換: last_repair = until - repair_cooldown
                    file_last_repair = until - self._repair_cooldown
                    current_last_repair = self._last_repair.get(pt, 0)
                    if file_last_repair > current_last_repair:
                        self._last_repair[pt] = file_last_repair
                        logging.debug(
                            f'[Diag] クールダウンファイルから更新: {k} '
                            f'until={until:.0f} (+{(until - time.time()) / 3600:.1f}h)'
                        )
                except ValueError:
                    pass
        except Exception as e:
            logging.debug(f'[Diag] クールダウン同期失敗: {e}')

    def diagnose(self) -> list[Problem]:
        problems = []
        now = time.time()
        cutoff = now - self._window

        with self._lock:
            # ファイルから最新クールダウンを同期（外部からのファイル書き換えに対応）
            self._sync_cooldown_from_file()
            self._prune(cutoff)

            # Google: 直近1時間で10回以上試みて全部0件
            g_attempts = len(self._google_counts)
            g_total = sum(c for _, c in self._google_counts)
            if g_attempts >= 10 and g_total == 0:
                problems.append(Problem(
                    type=ProblemType.GOOGLE_ZERO_RESULTS,
                    description=(
                        f'Google広告が直近{g_attempts}回のスクレイプで1件も取得できていない。'
                        'セレクタが変更された可能性がある。'
                    ),
                    affected_file='scrapers/google_scraper.py',
                    severity='critical',
                ))

            # Yahoo: 同様
            y_attempts = len(self._yahoo_counts)
            y_total = sum(c for _, c in self._yahoo_counts)
            if y_attempts >= 10 and y_total == 0:
                problems.append(Problem(
                    type=ProblemType.YAHOO_ZERO_RESULTS,
                    description=(
                        f'Yahoo広告が直近{y_attempts}回のスクレイプで1件も取得できていない。'
                        'セレクタが変更された可能性がある。'
                    ),
                    affected_file='scrapers/yahoo_scraper.py',
                    severity='critical',
                ))

            # 会社名取得率: 直近100件で10%未満
            # ※ LPの大半はJS SPAのためrequests.getで取れないケースが多く、
            #    20〜30%台の取得率は正常動作。本当にパーサが壊れた場合に
            #    限って修復が必要なため、閾値は厳しくしすぎない。
            ext_recent = list(self._extraction_success)[-100:]
            if len(ext_recent) >= 50:
                success_rate = sum(1 for _, ok in ext_recent if ok) / len(ext_recent)
                if success_rate < 0.10:
                    problems.append(Problem(
                        type=ProblemType.COMPANY_EXTRACTION_LOW,
                        description=(
                            f'会社名取得率が{success_rate:.0%}と極めて低い（基準: 10%）。'
                            '特商法ページのパターンマッチまたはネットワーク障害の可能性。'
                        ),
                        affected_file='processors/company_finder.py',
                        severity='warning',
                    ))

            # Meta: 連続5回以上セッション切れ
            if self._meta_consecutive_failures >= 5:
                problems.append(Problem(
                    type=ProblemType.META_SESSION_EXPIRED,
                    description=(
                        f'Metaセッション切れが{self._meta_consecutive_failures}回連続。'
                        '再ログインが必要。`main.py --setup`を実行してください。'
                    ),
                    affected_file='scrapers/meta_scraper.py',
                    severity='warning',
                ))

            # Sheets書き込みエラー: 直近1時間で5回以上
            sheets_errors = len(self._sheets_errors)
            if sheets_errors >= 5:
                problems.append(Problem(
                    type=ProblemType.SHEETS_WRITE_FAILING,
                    description=(
                        f'Google Sheets書き込みエラーが直近1時間で{sheets_errors}回発生。'
                        '認証切れまたはAPI quota超過の可能性。'
                    ),
                    affected_file='storage/sheets_writer.py',
                    severity='warning',
                ))

        # 修復クールダウン中の問題を除外
        active_problems = [
            p for p in problems
            if now - self._last_repair.get(p.type, 0) > self._repair_cooldown
        ]
        return active_problems

    def mark_repaired(self, problem_type: ProblemType):
        """修復成功時: 長いクールダウンを設定（2h）"""
        with self._lock:
            self._last_repair[problem_type] = time.time()
            self._persist_cooldown()

    def mark_repair_failed(self, problem_type: ProblemType, cooldown_seconds: int = 1800):
        """修復失敗時: クールダウンを設定して高頻度リトライを防ぐ。
        cooldown_seconds が repair_cooldown より大きくてもサポートする。"""
        with self._lock:
            now = time.time()
            # until = last_repair + repair_cooldown が now + cooldown_seconds になるように設定
            # → last_repair = now + cooldown_seconds - repair_cooldown
            # cooldown_seconds < repair_cooldown の場合: last_repair < now（過去）
            # cooldown_seconds > repair_cooldown の場合: last_repair > now（未来）
            new_last_repair = now + cooldown_seconds - self._repair_cooldown
            new_until = new_last_repair + self._repair_cooldown  # = now + cooldown_seconds

            # 既に設定されているクールダウンより長い場合のみ上書き
            existing_until = self._last_repair.get(problem_type, 0) + self._repair_cooldown
            if new_until > existing_until:
                self._last_repair[problem_type] = new_last_repair
            self._persist_cooldown()

    def _persist_cooldown(self):
        """クールダウン状態をファイルに保存する（_lockの中から呼ぶこと）"""
        try:
            # until = last_repair + repair_cooldown として保存
            data = {
                pt.value: ts + self._repair_cooldown
                for pt, ts in self._last_repair.items()
            }
            _save_cooldown_file(data)
        except Exception as e:
            logging.debug(f'[Diag] クールダウン永続化失敗: {e}')

    def _prune(self, cutoff: float):
        for dq in [self._google_counts, self._yahoo_counts, self._meta_counts,
                   self._extraction_success]:
            while dq and dq[0][0] < cutoff:
                dq.popleft()
        while self._sheets_errors and self._sheets_errors[0] < cutoff:
            self._sheets_errors.popleft()

    # ─────────────────────────────────────────────
    # 生指標取得（auto_tuner向け）
    # ─────────────────────────────────────────────
    def get_stats(self) -> dict:
        """直近1時間の生指標を辞書で返す（auto_tunerが参照する）"""
        with self._lock:
            cutoff = time.time() - self._window
            self._prune(cutoff)

            y_attempts = len(self._yahoo_counts)
            y_zeros = sum(1 for _, c in self._yahoo_counts if c == 0)
            m_total = sum(c for _, c in self._meta_counts)

            ext = list(self._extraction_success)[-100:]
            extraction_rate = (sum(1 for _, ok in ext if ok) / len(ext)) if ext else 0.5

            return {
                'yahoo_attempts': y_attempts,
                'yahoo_zero_rate': y_zeros / y_attempts if y_attempts > 0 else 0.0,
                'meta_lp_found': m_total,
                'extraction_rate': extraction_rate,
            }

    # ─────────────────────────────────────────────
    # ステータスサマリー（ログ出力用）
    # ─────────────────────────────────────────────
    def summary(self) -> str:
        with self._lock:
            cutoff = time.time() - self._window
            self._prune(cutoff)
            g = sum(c for _, c in self._google_counts)
            y = sum(c for _, c in self._yahoo_counts)
            m = sum(c for _, c in self._meta_counts)
            ext = list(self._extraction_success)[-100:]
            rate = (sum(1 for _, ok in ext if ok) / len(ext) * 100) if ext else 0
            return (
                f'[直近1時間] Google:{g}件 Yahoo:{y}件 Meta:{m}件 '
                f'会社名取得率:{rate:.0f}%'
            )


# シングルトン
_instance: Optional[Diagnostics] = None


def get_diagnostics() -> Diagnostics:
    global _instance
    if _instance is None:
        _instance = Diagnostics()
    return _instance
