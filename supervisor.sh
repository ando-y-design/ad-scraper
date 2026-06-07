#!/bin/bash
# supervisor.sh — スクレイが死んだら自動再起動する外部監視スクリプト
# nohup bash supervisor.sh > logs/supervisor.log 2>&1 & で起動

SCRAPER_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRAPER_DIR/logs/scraper.pid"
LOG="$SCRAPER_DIR/logs/supervisor.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [Supervisor] $1" | tee -a "$LOG"
}

log "起動 (監視間隔: 60秒)"

while true; do
    sleep 60

    # PIDファイルがあればそのプロセスの生死を確認
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            # 生きている → 何もしない
            continue
        else
            log "スクレイ死亡検知 (PID=$pid) → 再起動します"
            rm -f "$PID_FILE"
        fi
    else
        # PIDファイルがない場合もプロセス確認
        if pgrep -qf "python.*main\.py"; then
            continue
        fi
        log "PIDファイルなし・プロセス不在 → 起動します"
    fi

    # 再起動
    cd "$SCRAPER_DIR"
    nohup python3 main.py >> logs/nohup.log 2>&1 &
    new_pid=$!
    log "再起動完了 (新PID=$new_pid)"
done
