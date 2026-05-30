#!/bin/bash
# 自動セットアップ（対話入力なし）
set -e

REPO="https://github.com/ando-y-design/ad-scraper.git"
INSTALL_DIR="$HOME/ad_scraper"

echo "=== ad_scraper セットアップ開始 ==="

# clone or pull
if [ -d "$INSTALL_DIR/.git" ]; then
  echo "既存フォルダあり → git pull"
  cd "$INSTALL_DIR" && git pull
else
  git clone "$REPO" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# config.jsonをCテンプレートから作成
cp config_C.template.json config.json

# APIキーを設定（変更不要）
python3 - << 'PYEOF'
import json
cfg = json.load(open('config.json'))
cfg['nta_api_key'] = 'Kv3egYQ5pbATb'
cfg['anthropic_api_key'] = ''
cfg['serp_apis'][0]['key'] = '125a9cd2-9a48-45b6-9b9f-4b6eb24e65df'
json.dump(cfg, open('config.json', 'w'), ensure_ascii=False, indent=2)
print('config.json 設定完了')
PYEOF

# keyword_data.py
cp utils/keyword_data.example.py utils/keyword_data.py
echo "keyword_data.py 作成完了"

# credentials.json の確認
if [ ! -f credentials.json ]; then
  echo ""
  echo "⚠️  credentials.json が見つかりません"
  echo "このPCにコピーしてから再実行してください:"
  echo "  ~/ad_scraper/credentials.json"
  echo ""
  echo "コピー後: cd ~/ad_scraper && python3 main.py"
  exit 1
fi

# 依存パッケージ
echo "パッケージインストール中..."
pip3 install -r requirements.txt -q
playwright install chromium

echo ""
echo "=== セットアップ完了 ==="
echo "起動: cd ~/ad_scraper && python3 main.py"
