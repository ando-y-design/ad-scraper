# ad_scraper 完全ガイド

> Google・Yahoo・Meta の広告を自動収集し、出稿企業の **会社名・電話番号** をSQLite＆Google Sheetsに保存する自律型スクレイパー。

---

## 目次

1. [全体アーキテクチャ概要](#1-全体アーキテクチャ概要)
2. [広告収集（Yahoo / Google / Meta）](#2-広告収集yahoo--google--meta)
3. [LP解析・会社名抽出](#3-lp解析会社名抽出)
4. [データ保存（SQLite / Google Sheets）](#4-データ保存sqlite--google-sheets)
5. [監視・自己修復（Watchdog）](#5-監視自己修復watchdog)

---

## 1. 全体アーキテクチャ概要

### パイプライン全体図

```
[ config.json ]
       │ キーワード
       ▼
┌────────────────────────────────────────┐
│  収集層                                │
│  yahoo_worker  ──→ Google/Yahoo 広告  │
│  yahoo2_worker ──→ Google/Yahoo 広告  │
│  meta_worker   ──→ Meta 広告          │
└────────────────┬───────────────────────┘
                 │ lp_queue（LP URL）
                 ▼
┌────────────────────────────────────────┐
│  解析層                                │
│  processor_worker × 8 並列            │
│  └── LP取得 → 会社名抽出 → 電話番号抽出│
└────────────────┬───────────────────────┘
                 │ result_queue（会社情報）
                 ▼
┌────────────────────────────────────────┐
│  保存層                                │
│  writer_worker                         │
│  └── SQLite INSERT + Google Sheets 追記│
└────────────────────────────────────────┘

           ↑↑↑ 常時監視 ↑↑↑
┌────────────────────────────────────────┐
│  watchdog_worker                       │
│  ハートビート監視・ゾンビ再起動         │
│  自己修復サイクル（30分ごと）           │
└────────────────────────────────────────┘
```

### スレッド構成

| スレッド名 | 多重度 | 役割 |
|-----------|--------|------|
| yahoo | 1 | Yahoo/Google 広告収集（ブラウザ1号） |
| yahoo2 | 1 | Yahoo/Google 広告収集（ブラウザ2号・並列） |
| meta | 1 | Meta 広告ライブラリ収集 |
| processor | 8（並列） | LP解析・会社情報抽出 |
| writer | 1 | SQLite + Google Sheets書き込み |
| watchdog | 1 | 全スレッド監視・再起動 |
| status_server | 1 | HTTP ステータスページ（localhost:8080） |

### データフロー（キュー）

| キュー | 最大サイズ | 生産者 → 消費者 |
|--------|-----------|----------------|
| `lp_queue` | 200 | yahoo/meta → processor |
| `result_queue` | 100 | processor → writer |

### 主要ファイル一覧

| ファイル | 役割 |
|---------|------|
| `main.py` | エントリーポイント・スレッド管理 |
| `state.py` | 共有グローバル（queues / events / config） |
| `config.json` | キーワード・APIキー・タイミング設定 |
| `control.json` | 実行制御（pause / stop / キーワード追加） |
| `companies.db` | SQLite データベース（WALモード） |
| `workers/` | 各スレッドの実装 |
| `scrapers/` | 広告URL収集ロジック |
| `processors/` | 会社名・電話番号抽出ロジック |
| `storage/` | DB・Sheets書き込み |
| `utils/` | 共通ユーティリティ |
| `self_repair/` | 自己診断・Claude CLI修復サイクル |

### Google Sheets カラム構成

| A | B | C | D | E | F |
|---|---|---|---|---|---|
| 会社名 | LP URL | 電話番号 | キーワード | 広告ソース | 取得日 |

### 設定ファイル早見

**config.json（主要キー）**
```json
{
  "nta_api_key": "...",
  "serp_apis": [...],
  "sources": { "google": true, "yahoo": true, "meta": true },
  "timing": { "min_delay_seconds": 60, "max_delay_seconds": 300 },
  "areas": ["東京", "大阪", "名古屋", ...]
}
```

**control.json（ランタイム制御）**
```json
{
  "status": "running",
  "pause": false,
  "stop": false,
  "add_keywords": [],
  "remove_keywords": []
}
```

### 起動・停止

```bash
# 起動
python main.py

# 一時停止
# control.json の "status" を "paused" に変更

# 停止
# control.json の "status" を "stopped" に変更
# → 次のwatchdogサイクル（30秒以内）でシャットダウン
```

---

## 2. 広告収集（Yahoo / Google / Meta）

### Yahoo / Google 収集フロー

```
[ keywords DB からキーワード取得 ]
         │
         ▼
 ┌── Google 収集 ──────────────────────────────────────────┐
 │  1. SERP API（hasdata / serpapi / valueserp / zenserp）  │
 │     └─ 成功: LP URL を lp_queue へ                       │
 │     └─ 全キー失敗: Playwright フォールバック              │
 │  2. Playwright（Chromium）で google.co.jp を人間操作      │
 │     └─ /aclk リンク直接抽出 or クリックでポップアップ取得 │
 └──────────────────────────────────────────────────────────┘
         │ beat(name) + delay
         ▼
 ┌── Yahoo 収集 ──────────────────────────────────────────┐
 │  1. search.yahoo.co.jp をPlaywrightで直接スクレイピング  │
 │  2. DOM解析（UAL属性 / トラッキングURL / PRラベル 3段階）│
 │  3. rd.listing.yahoo.co.jp → LP URL を直接抽出          │
 │     └─ 抽出失敗: クリック → ポップアップで URL 取得      │
 └──────────────────────────────────────────────────────────┘
         │
         ▼
  lp_queue に投入 → processor_worker へ
```

### SERP API キーローテーション

レートリミット（429）や認証エラー（402）が出ると自動で次のキーに切り替わる。全キー失敗時はPlaywrightへフォールバック。

```json
"serp_apis": [
  { "provider": "hasdata",   "key": "..." },
  { "provider": "serpapi",   "key": "..." },
  { "provider": "valueserp", "key": "..." },
  { "provider": "zenserp",   "key": "..." }
]
```

### Meta 収集フロー

```
[ keywords DB からキーワード取得 ]
         │
         ▼
 facebook.com/ads/library をPlaywrightで検索
         │
         ▼
 広告カード一覧をスクロール取得
         │
         ▼
 広告主名・LP URL を抽出 → lp_queue へ
```

### キーワード管理

| テーブル | 用途 |
|---------|------|
| `keywords` | 全キーワード（source / last_searched / is_archived） |
| `keyword_area_log` | キーワード×エリアの最終検索日時（重複回避） |

**冷却システム**
- Google/Yahoo: `keyword_cooling_hours`（デフォルト 24h）
- Meta: `meta_keyword_cooling_hours`（デフォルト 48h）
- 残り5件以下 → アーカイブを自動再有効化（watchdog）

### エリア対応

10都市（東京/大阪/名古屋/福岡/札幌/仙台/広島/京都/神戸/横浜）を対象にPlaywrightへGPS座標を注入してローカル広告を収集。

### よくある問題

| 症状 | 原因 | 対処 |
|------|------|------|
| Yahoo 0件続く | CAPTCHA / セレクタ変更 | `logs/snapshots/yahoo_*.html` を確認 |
| Google 全スキップ | CAPTCHA バックオフ中 | `logs/.google_captcha_state` を削除 |
| SerpAPI 全失敗 | 全キーのクォータ超過 | `config.json` に新しいキーを追加 |
| yahoo スレッドハング | Playwrightページフリーズ | watchdog が120秒後に自動再起動 |

---

## 3. LP解析・会社名抽出

### 解析フロー

```
lp_queue から LP URL を取得
         │
         ▼
 LP 取得（requests + BeautifulSoup）
         │ ブロックドメインはスキップ
         ▼
 特商法ページ解析
   company_finder.find_company_info(url, html)
   1. 特商法ページへの内部リンクを探す
      (/tokutei, /legal, /company 等)
   2. 特商法ページから会社名テキストを抽出
   3. 見つからなければ LP本体・フッターから推定
         │
         ▼
 法人名正規化
   normalizer.normalize_company_name()
   - 全角スペース・記号除去
   - 法人格の揺れを統一
         │
         ▼
 英語法人名 → 国税庁API変換（オプション）
   legal_name_resolver.resolve_legal_name()
   "M-Style Japan Co., Ltd." → "M-Style Japan株式会社"
   ※ nta_api_key が有効な場合のみ
         │
         ▼
 電話番号抽出
   phone_finder.find_phone_numbers(html)
   strategy=sme: 固定電話優先 → 携帯 → フリーダイヤル
         │
         ▼
 result_queue に投入 → writer_worker へ
```

### 会社名の探索順

1. **特商法ページ** — 「販売業者」「運営会社」「会社名」ラベルに続くテキスト
2. **フッター・会社情報エリア** — `<footer>`, `class="company"`, `id="about"` 等
3. **メタタグ** — `<meta name="author">`, OGP タグ

### 電話番号の優先順（strategy=sme）

1. 市外局番付き固定電話（テレアポに最適）
2. 携帯電話番号
3. フリーダイヤル・IP電話

### 国税庁API（nta_lookup.py）

| 状態 | 動作 |
|------|------|
| APIキー有効 | 会社名を国税庁DBと照合して正式法人名に補正 |
| APIキー404 | セッション中の全API呼び出しを停止（1回のみ警告） |
| APIキー未設定 | スキップ |

APIキー取得: https://www.houjin-bangou.nta.go.jp/webapi/

### 重複排除

`base_url`（ドメイン）をキーにSQLiteで重複チェック。同一ドメインの企業は再登録しない。

### 主要ファイル

| ファイル | 役割 |
|---------|------|
| `workers/processor_worker.py` | 並列解析ワーカー（8スレッド） |
| `processors/company_finder.py` | 特商法ページ解析・会社名抽出 |
| `processors/legal_name_resolver.py` | 英語法人名→国税庁API変換 |
| `processors/normalizer.py` | 会社名・電話番号正規化 |
| `processors/phone_finder.py` | 電話番号抽出 |

### よくある問題

| 症状 | 原因 | 対処 |
|------|------|------|
| 会社名が空欄 | 特商法ページへのリンクなし | `processors/company_finder.py` を確認 |
| 電話番号が空欄 | 電話番号が画像埋め込み | 手動補完または対象ページを個別解析 |
| NTA API 警告連発 | APIキーが未有効化 | `config.json` の `nta_api_key` を再発行 |

---

## 4. データ保存（SQLite / Google Sheets）

### 保存フロー

```
result_queue から会社情報 dict を取得
         │
         ▼
 重複チェック（SQLite）
   base_url が companies テーブルに存在?
   ├─ 存在する → スキップ
   └─ 存在しない → INSERT
         │
         ▼
 SQLite INSERT
   companies テーブルに1行追加
   WALモードで並列書き込みに対応
         │
         ▼
 Google Sheets 追記
   sheets_writer.append_row()
   Service Account 認証（credentials.json）
```

### SQLite スキーマ（companies テーブル主要カラム）

| カラム | 説明 |
|-------|------|
| id | 自動採番 |
| company_name | 会社名（正式名） |
| normalized_name | 正規化済み会社名（重複判定用） |
| lp_url | ランディングページURL |
| base_url | ドメイン（重複排除キー） |
| phone | 代表電話番号 |
| phones | 全電話番号（カンマ区切り） |
| ad_sources | 広告ソース（Google/Yahoo/Meta） |
| found_date | 取得日（YYYY-MM-DD） |
| keyword | 検索キーワード |
| area_name | エリア名（東京/大阪等） |

### Google Sheets 連携設定

1. GCP でサービスアカウントを作成
2. Google Sheets API を有効化
3. 秘密鍵JSONを `credentials.json` として配置
4. 対象スプレッドシートにサービスアカウントのメールを共有

```json
"google_sheets": {
  "sheet_id": "スプレッドシートのID",
  "service_account_key_path": "credentials.json"
}
```

### データ確認コマンド

```python
import sqlite3
conn = sqlite3.connect('companies.db')

# 総件数
conn.execute('SELECT COUNT(*) FROM companies').fetchone()

# 今日の取得件数
conn.execute(
  "SELECT COUNT(*) FROM companies WHERE found_date = date('now','localtime')"
).fetchone()

# 最新10件
conn.execute('''
  SELECT company_name, phone, ad_sources, found_date
  FROM companies ORDER BY id DESC LIMIT 10
''').fetchall()
```

### 主要ファイル

| ファイル | 役割 |
|---------|------|
| `workers/writer_worker.py` | 保存ワーカー本体 |
| `storage/database.py` | SQLite操作（connection管理・UPSERT） |
| `storage/sheets_writer.py` | Google Sheets書き込み・ヘッダー管理 |
| `utils/db_janitor.py` | ゴミデータ自律クリーナー（10分ごと） |

### よくある問題

| 症状 | 原因 | 対処 |
|------|------|------|
| Sheets書き込みエラー | 認証切れ / API無効 | `credentials.json` を再発行 |
| 重複データが登録される | base_url の正規化ミス | `normalizer.py` の URL正規化ロジックを確認 |
| Sheets API 429 | 書き込みレート超過 | `writer_worker.py` のretry待機時間を調整 |

---

## 5. 監視・自己修復（Watchdog）

### Watchdog ループのタスク一覧

| 頻度 | タスク |
|------|-------|
| 毎ループ（30秒） | スレッド死亡検知 → 再起動 |
| 毎ループ（30秒） | ハング検知（120秒超 → ゾンビ強制置換） |
| 毎ループ（30秒） | `config.json` 再読み込み（ランタイム変更反映） |
| 毎ループ（30秒） | `control.json` 処理（pause/stop/キーワード追加） |
| 毎ループ（30秒） | キーワード自動補充（枯渇時にアーカイブを再有効化） |
| 10分ごと | DBゴミクリーニング（db_janitor） |
| 5分ごと | status.html 更新（localhost:8080） |
| 30分ごと | 自己修復サイクル（diagnostics → repairer） |
| 2時間ごと | AutoTuner（delay/cooling を自動最適化） |

### スレッド死活監視とハートビート

```
各ワーカースレッド
    └── beat(name) を定期呼び出し
              │ state._heartbeat[name] = time.time()
              ▼
watchdog（30秒ごと）
    └── now - _heartbeat[name] を計算
          ├─ > 300秒 → ハング警告ログ
          ├─ > 120秒 → Chrome強制終了 + 新スレッド起動
          └─ is_alive() == False → 再起動
```

**再起動バックオフ** — 1時間あたり `max_restart_per_hour`（デフォルト3回）を超えると指数バックオフ（60秒→120秒→240秒…最大1800秒）

### control.json によるランタイム制御

| status 値 | 動作 |
|---------|------|
| `running` | 通常稼働 |
| `paused` | 全ワーカーを一時停止（watchdog は継続） |
| `stopped` | シャットダウン開始 |

`add_keywords` / `remove_keywords` は処理後に自動で空配列にリセット。

### 自己修復サイクル（30分ごと）

```
diagnostics.py — メトリクス収集（会社名空欄率・電話番号空欄率・ハング頻度）
         │
         ▼
問題検知 → pending_fixes.jsonl に記録
         │
         ▼
repair_worker.py — Claude CLI 経由でコード修正
         │
         ▼
修復成功 → 対象スレッドをリロード・再起動
修復失敗 → repair_history.jsonl に記録
```

> **注意**: `self_repair/` 配下のコードは手動変更禁止（修復ループ再発リスク）

### ログファイル

| ファイル | 内容 | サイズ制限 |
|---------|------|----------|
| `logs/main.log` | 全スレッドの動作ログ | 10MB × 5世代 |
| `logs/error.log` | ERRORレベル以上のみ | 10MB × 3世代 |
| `logs/pending_fixes.jsonl` | 自己修復の候補リスト | — |
| `logs/repair_history.jsonl` | 修復履歴 | — |
| `logs/snapshots/*.html` | 広告取得失敗時のHTMLスナップショット | 直近5件 |

### ステータスページ

http://localhost:8080 — watchdog が5分ごとに更新

### 運用チェックリスト

**毎日確認**
- [ ] `logs/main.log` にERRORが増えていないか
- [ ] Google Sheets の行が増えているか（今日の取得件数）
- [ ] `localhost:8080` でステータス確認

**週次確認**
- [ ] `logs/pending_fixes.jsonl` に未修復の問題がないか
- [ ] SerpAPI クォータの残量

**緊急停止**
```json
// control.json を編集
{ "status": "stopped" }
// 30秒以内に全スレッドが終了する
```

**トラブル時の初動**
1. `logs/main.log` の最新ERRORを確認
2. `control.json` の status が `running` になっているか確認
3. プロセスが生きているか確認（`tasklist | findstr python`）
4. 死んでいれば `python main.py` で再起動
