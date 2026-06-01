#!/bin/bash
set -e
cd "$(dirname "$0")"

# 仮想環境がなければ作成
if [ ! -d ".venv" ]; then
    echo "[setup] 仮想環境を作成します..."
    python3 -m venv .venv
fi

source .venv/bin/activate

# 依存関係インストール
echo "[setup] 依存関係をインストールします..."
pip install -q -r requirements.txt

# Playwright ブラウザがなければインストール
if ! python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); p.stop()" 2>/dev/null; then
    echo "[setup] Playwright Chromium をインストールします..."
    playwright install chromium
fi

# 古いプロセスを確実に停止（python/python3 どちらのパスでも対応）
pkill -f "main\.py" 2>/dev/null || true
sleep 2

echo "[start] ad_scraper を起動します..."
python main.py
