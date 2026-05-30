#!/bin/bash
set -e

REPO="https://github.com/amdwtwitter-ai/ad-scraper.git"
INSTALL_DIR="$HOME/ad_scraper"

echo "=== ad_scraper セットアップ ==="

# clone
if [ -d "$INSTALL_DIR" ]; then
  echo "既存フォルダあり → git pull"
  cd "$INSTALL_DIR" && git pull
else
  git clone "$REPO" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# config
if [ ! -f config.json ]; then
  cp config_C.template.json config.json
  echo "config.json を作成しました"
fi

# keyword_data
if [ ! -f utils/keyword_data.py ]; then
  cp utils/keyword_data.example.py utils/keyword_data.py
  echo "keyword_data.py を作成しました"
fi

# API keys
echo ""
echo "--- APIキーを入力してください ---"

read -p "nta_api_key: " NTA_KEY
read -p "anthropic_api_key: " ANTHROPIC_KEY
read -p "hasdata API key (serp_apis): " HASDATA_KEY

python3 -c "
import json, sys
with open('config.json', 'r') as f:
    cfg = json.load(f)
cfg['nta_api_key'] = '$NTA_KEY'
cfg['anthropic_api_key'] = '$ANTHROPIC_KEY'
if cfg.get('serp_apis'):
    cfg['serp_apis'][0]['key'] = '$HASDATA_KEY'
with open('config.json', 'w') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print('config.json 更新完了')
"

# credentials.json
echo ""
read -p "credentials.json のパス（Enterでスキップ）: " CREDS_PATH
if [ -n "$CREDS_PATH" ] && [ -f "$CREDS_PATH" ]; then
  cp "$CREDS_PATH" "$INSTALL_DIR/credentials.json"
  echo "credentials.json を配置しました"
fi

# dependencies
echo ""
echo "--- 依存パッケージをインストール ---"
pip3 install -r requirements.txt
playwright install chromium

echo ""
echo "=== セットアップ完了 ==="
echo "起動するには: cd $INSTALL_DIR && python3 main.py"
