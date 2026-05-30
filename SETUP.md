# セットアップ手順（新しいPC）

## 1. リポジトリをclone

```
git clone https://github.com/amdwtwitter-ai/ad-scraper.git
cd ad-scraper
```

## 2. 依存パッケージをインストール

```
pip install -r requirements.txt
playwright install chromium
```

## 3. config.json を作成（このPCの設定）

`config.json` は共有されないので、このPCの情報を設定する。

```
cp config.example.json config.json
```

`config.json` を開いて以下を設定：
- `nta_api_key`: NTA APIキー
- `anthropic_api_key`: Anthropic APIキー
- `spreadsheet_id`: **このアカウント専用の** Google スプレッドシートID

## 4. keyword_data.py を作成（このPCのKW）

```
cp utils/keyword_data.example.py utils/keyword_data.py
```

`utils/keyword_data.py` を開いて、このアカウントで検索したいキーワードを設定する。

## 5. credentials.json を配置

Google Sheets APIの認証ファイルをプロジェクトルートに置く。

## 6. 起動

```
python main.py
```

---

## 改良を取り込む（毎日の運用）

```
git pull
```

これだけ。`keyword_data.py` と `config.json` はローカルのまま保持される。
