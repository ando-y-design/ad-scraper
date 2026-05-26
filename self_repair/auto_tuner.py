"""
自動パフォーマンスチューナー

2時間ごとに実行され、収集ペースと診断指標を見てconfig.jsonを自動調整する。

調整ロジック:
  1. キーワード利用可能率 < 15%  → keyword_cooling_hours を1h短縮（下限4h）
  2. Yahoo ゼロ率 > 70%          → delay変更なし（セレクタ問題として診断に任せる）
  3. 抽出率 < 8%                 → delay変更なし（コンテンツ/ネットワーク問題）
  4. N回連続でペース安定/上昇    → delay を15%縮小（下限 5s/20s）
  5. ペース大幅低下（前期比-40%）→ 直近の変更を1段階戻す
"""
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

_BASE_DIR = Path(__file__).parent.parent
_CONFIG_PATH = _BASE_DIR / 'config.json'
_STATE_PATH = _BASE_DIR / 'logs' / 'tuner_state.json'

_FLOOR = {
    'keyword_cooling_hours': 4,
    'min_delay_seconds': 5,
    'max_delay_seconds': 20,
}
_CEIL = {
    'keyword_cooling_hours': 24,
    'min_delay_seconds': 60,
    'max_delay_seconds': 300,
}

_SHRINK_FACTOR = 0.85   # 安定時にdelayを15%縮小
_GROW_FACTOR   = 1.20   # ペース低下時にdelayを20%拡大（過負荷緩和）
_STABLE_STREAK_NEEDED = 3   # 連続N回安定でdelay縮小


def _load_state() -> dict:
    try:
        if _STATE_PATH.exists():
            return json.loads(_STATE_PATH.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {
        'last_run': 0,
        'stable_streak': 0,
        'last_daily_count': 0,
    }


def _save_state(state: dict):
    try:
        _STATE_PATH.parent.mkdir(exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8'
        )
    except Exception as e:
        logging.warning(f'[Tuner] state保存失敗: {e}')


def _load_config() -> dict:
    with open(_CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)


def _save_config(cfg: dict):
    tmp_fd, tmp_path = tempfile.mkstemp(dir=_CONFIG_PATH.parent, suffix='.tmp')
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, _CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def _get_recent_daily_counts(conn) -> list[int]:
    """直近7日の日別件数リスト（今日→先週順）"""
    rows = conn.execute(
        '''SELECT COUNT(*) FROM companies
           WHERE found_date = ?
        ''',
        (datetime.now().strftime('%Y-%m-%d'),)
    ).fetchone()
    today_count = rows[0] if rows else 0

    hist = conn.execute(
        '''SELECT found_date, COUNT(*) AS cnt
           FROM companies
           WHERE found_date < ?
           GROUP BY found_date
           ORDER BY found_date DESC
           LIMIT 6
        ''',
        (datetime.now().strftime('%Y-%m-%d'),)
    ).fetchall()

    past = [r[1] for r in hist]
    return [today_count] + past


def _get_keyword_availability_ratio(conn, cooling_hours: int) -> float:
    """google_yahoo + meta 合計のアクティブキーワードのうち利用可能な割合"""
    threshold = (datetime.now() - timedelta(hours=cooling_hours)).isoformat()
    total = conn.execute(
        'SELECT COUNT(*) FROM keywords WHERE is_archived=0'
    ).fetchone()[0]
    available = conn.execute(
        '''SELECT COUNT(*) FROM keywords
           WHERE is_archived=0
             AND (last_searched IS NULL OR last_searched < ?)
        ''',
        (threshold,)
    ).fetchone()[0]
    return available / total if total > 0 else 1.0


def run_auto_tuner(conn) -> None:
    """
    パフォーマンス指標を評価してconfig.jsonを自動調整する。
    Watchdogから2時間ごとに呼ばれる想定。
    """
    state = _load_state()
    now = time.time()

    if now - state.get('last_run', 0) < 7200:
        return

    state['last_run'] = now

    try:
        cfg = _load_config()
        timing = cfg.setdefault('timing', {})

        current_cooling = timing.get('keyword_cooling_hours', 8)
        current_min    = timing.get('min_delay_seconds', 10)
        current_max    = timing.get('max_delay_seconds', 60)

        # ── 1. 診断指標取得 ──
        avail_ratio = _get_keyword_availability_ratio(conn, current_cooling)
        daily_counts = _get_recent_daily_counts(conn)
        today_count = daily_counts[0]

        # 過去3〜6日の平均（今日・昨日を除いた安定期間）
        if len(daily_counts) >= 4:
            baseline = sum(daily_counts[2:]) / len(daily_counts[2:])
        else:
            baseline = today_count or 1

        # Diagnostics から直近の運用指標を取得
        from self_repair.diagnostics import get_diagnostics
        stats = get_diagnostics().get_stats()
        yahoo_zero_rate = stats.get('yahoo_zero_rate', 0.0)
        extraction_rate = stats.get('extraction_rate', 0.5)

        logging.info(
            f'[Tuner] 診断: today={today_count}件 baseline={baseline:.1f}件 '
            f'avail={avail_ratio:.0%} yahoo_zero={yahoo_zero_rate:.0%} '
            f'extract={extraction_rate:.0%} '
            f'cooling={current_cooling}h delay={current_min}-{current_max}s'
        )

        changed = False

        # ── 2. キーワード枯渇補正 ──
        if avail_ratio < 0.15:
            new_cooling = max(_FLOOR['keyword_cooling_hours'], current_cooling - 1)
            if new_cooling != current_cooling:
                timing['keyword_cooling_hours'] = new_cooling
                logging.info(f'[Tuner] キーワード枯渇({avail_ratio:.0%}) → cooling: {current_cooling}h → {new_cooling}h')
                changed = True

        # ── 3. delay調整（セレクタ問題・抽出失敗中は触らない） ──
        if yahoo_zero_rate > 0.70:
            logging.info('[Tuner] Yahooゼロ率高 → delay調整スキップ（セレクタ修復待ち）')
            state['stable_streak'] = 0
        elif extraction_rate < 0.08:
            logging.info('[Tuner] 抽出率低 → delay調整スキップ（コンテンツ/ネットワーク問題）')
            state['stable_streak'] = 0
        elif baseline > 0 and today_count < baseline * 0.55:
            # ペース大幅低下 → delay を拡大（負荷軽減）
            new_min = min(_CEIL['min_delay_seconds'], int(current_min * _GROW_FACTOR))
            new_max = min(_CEIL['max_delay_seconds'], int(current_max * _GROW_FACTOR))
            if new_min != current_min or new_max != current_max:
                timing['min_delay_seconds'] = new_min
                timing['max_delay_seconds'] = new_max
                logging.info(
                    f'[Tuner] ペース低下(today={today_count} < baseline*0.55={baseline*0.55:.0f}) '
                    f'→ delay拡大: {current_min}-{current_max}s → {new_min}-{new_max}s'
                )
                changed = True
            state['stable_streak'] = 0
        else:
            # ペース安定 → streak +1
            state['stable_streak'] = state.get('stable_streak', 0) + 1
            streak = state['stable_streak']

            if streak >= _STABLE_STREAK_NEEDED:
                new_min = max(_FLOOR['min_delay_seconds'], int(current_min * _SHRINK_FACTOR))
                new_max = max(_FLOOR['max_delay_seconds'], int(current_max * _SHRINK_FACTOR))
                if new_min != current_min or new_max != current_max:
                    timing['min_delay_seconds'] = new_min
                    timing['max_delay_seconds'] = new_max
                    logging.info(
                        f'[Tuner] {streak}回連続安定 → delay縮小: '
                        f'{current_min}-{current_max}s → {new_min}-{new_max}s'
                    )
                    changed = True
                state['stable_streak'] = 0  # 調整後リセット

        if changed:
            cfg['timing'] = timing
            _save_config(cfg)

        state['last_daily_count'] = today_count
        _save_state(state)

        logging.info(
            f'[Tuner] 完了: cooling={timing["keyword_cooling_hours"]}h '
            f'delay={timing["min_delay_seconds"]}-{timing["max_delay_seconds"]}s '
            f'streak={state["stable_streak"]}'
        )

    except Exception as e:
        logging.error(f'[Tuner] エラー: {e}', exc_info=True)
        _save_state(state)
