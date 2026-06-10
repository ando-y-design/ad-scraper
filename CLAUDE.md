# ad_scraper アーキテクチャマップ

## プロジェクト概要
Google/Yahoo/Meta広告を収集し、会社名・電話番号を抽出してSQLiteとGoogle Sheetsに保存する自律型スクレイパー。

---

## ★ 最重要4ポイント（オーナー明示・全判断の基準）

このスクレイパーが達成すべき4つの絶対条件。設計・修正の優先順位はこの順序で判断する。

1. **法人番号が取れていること**
   国税庁(NTA)法人番号APIで正しい法人番号が付与されること。

2. **G列の会社名が「正式な法人登録名」であること**
   抽出した会社名をそのまま使わず、NTA正式法人名に補正したものをG列に出力する。

3. **LP/HPから会社名と電話番号が正しく取れていること**
   - 特商法ページ・会社概要ページを最優先で見るのがベスト。
   - **最重要: 電話番号にかけたとき実際にその会社へ繋がること**（電話番号の正確性＝架電可能性）。
   - 会社名と電話番号が「別主体のもの」を取り違えないこと（販売業者/運営会社の混同に注意）。

4. **収集スピードが最大化されていること**
   Google / Yahoo / Meta（必要ならBingも）を**並列実行**し、各ソースがそれぞれ安定して回ること。

> 精度（1〜3）を優先しつつ、スピード（4）を最大化する。精度とスピードが衝突する場合は精度を優先。

---

## ディレクトリ構成

```
ad_scraper/
├── main.py                    # エントリーポイント・シグナル処理・起動制御
├── state.py                   # 共有グローバル（config / queues / events / heartbeat）
├── config.json                # 設定（キーワード、API keys、タイミング）
├── control.json               # 実行制御（pause/stop）
├── companies.db               # SQLite WAL
├── workers/
│   ├── yahoo_worker.py        # Yahoo/Google広告収集（Playwright）
│   ├── meta_worker.py         # Meta広告収集（Playwright）
│   ├── processor_worker.py    # LP解析・会社情報抽出（config.processor_workers並列・既定3）
│   ├── writer_worker.py       # SQLite + Google Sheets書き込み
│   └── watchdog_worker.py     # ハング検知・スレッド再起動・自己修復
├── scrapers/
│   ├── google_scraper.py      # Google広告 URL抽出
│   ├── yahoo_scraper.py       # Yahoo広告 URL抽出
│   ├── bing_scraper.py        # Bing広告 URL抽出（Playwright・無料）
│   └── meta_scraper.py        # Meta広告ライブラリ
├── processors/
│   ├── company_finder.py      # 特商法ページ解析・会社名抽出
│   ├── legal_name_resolver.py # 英語法人名→国税庁API→正式日本法人名
│   ├── normalizer.py          # 会社名・電話番号正規化
│   └── phone_finder.py        # 電話番号抽出
├── storage/
│   ├── database.py            # SQLite操作
│   └── sheets_writer.py       # Google Sheets書き込み
├── utils/
│   ├── keywords.py            # キーワードDB操作（ロジックのみ）
│   ├── keyword_data.py        # キーワードリストデータ（500件）← 通常読まない
│   ├── browser.py             # Playwright コンテキスト管理
│   ├── config_loader.py       # config.json読み込み
│   ├── daily_briefing.py      # 朝報生成
│   └── db_janitor.py          # ゴミデータ自律クリーナー
├── self_repair/
│   ├── diagnostics.py         # メトリクス収集・問題検知
│   ├── repairer.py            # Claude CLI呼び出し・コード修復
│   └── repair_worker.py       # 修復サイクル制御
└── logs/
    ├── main.log               # メインログ（RotatingFileHandler, 10MB×5）
    ├── error.log              # エラーログ（10MB×3）
    ├── pending_fixes.jsonl    # 修正候補リスト（daily_briefingが参照）
    └── repair_history.jsonl   # 修復履歴
```

---

## スレッドパイプライン

```
yahoo_worker / yahoo2_worker (Google/Yahoo広告収集)
meta_worker  (Meta広告収集)
    ↓ lp_queue (広告URL・メタデータ)
processor_worker (LP解析・会社情報抽出・config.processor_workers並列・既定3)
    ↓ result_queue (会社情報)
writer_worker (SQLite INSERT + Google Sheets追記)

watchdog_worker → ハートビート監視・ゾンビ再起動（30秒ごと）
                → 自己修復サイクル（30分ごと）
                → AutoTuner（2時間ごと）
```

---

## データフロー

```
config.json → キーワード取得
    ↓
yahoo/meta workers: Playwrightで広告URL収集
    ↓ lp_queue
processor_worker:
    ① LP取得（requests + BeautifulSoup）
    ② 特商法ページ解析 → 会社名
    ③ NTA API で英語法人名を日本語正式名に変換
    ④ 電話番号抽出
    ↓ result_queue
writer_worker → companies.db + Google Sheets
    ↓
diagnostics.record_*() でメトリクス蓄積
    ↓ (30分ごと)
watchdog → repair_worker → 問題検知 → pending_fixes.jsonlに記録
```

---

## 設定ファイル

### config.json（主要キー）
```json
{
  "nta_api_key": "",
  "keywords": [...],
  "auto_code_repair": false,
  "phone_strategy": "direct",   // direct=直通/代表番号優先(架電到達率最大) / sme / enterprise
  "sources": {"google": true, "yahoo": true, "meta": true, "bing": true},
  // bing=Bingリスティング広告収集。Playwright直接スクレイピング（無料・bot検知が緩い）。
  // yahooワーカーの同一ブラウザで収集。primary(yahoo)スレッドのみ実行（yahoo2では走らない）。
  // ※HasData Bing SERP API版も serp_api_scraper.py に休眠状態で残置（クレジット契約時に切替可）。
  "timing": {"min_delay_seconds": 60, "max_delay_seconds": 300}
}
```

### control.json
```json
{"pause": false, "stop": false, "add_keywords": [], "remove_keywords": []}
```

---

## 作業ルール

| ルール | 内容 |
|-------|------|
| 多ファイル変更前 | `grep -rn "対象シンボル" --include="*.py" .` で全参照を確認してから着手 |
| スクリプト経由編集後 | 必ず Grep/Read で変更箇所を目視確認してから完了報告 |
| PowerShell+日本語 | `python -c "..."` に日本語を含めない。`.py` ファイルに書いて実行 |
| 正規表現追加時 | `python -c "import re; print(re.search(r'...', 'テスト'))"` で確認 |
| self_repair/ のコード | 変更しない（自己修復ループ再発リスク）|
| keyword_data.py | キーワードデータ専用 — レビュー時は通常読まない |

---

## デバッグの入り口

| 症状 | 確認場所 |
|------|---------|
| 広告が取れない | `logs/main.log` → GOOGLE/YAHOO_ZERO_RESULTS |
| 会社名が空欄 | `processors/company_finder.py` の `find_company_info()` |
| 自己修復が失敗 | `logs/repair_history.jsonl` |
| Sheets書き込みエラー | `storage/sheets_writer.py` + GCP認証確認 |
| スレッドハング | `workers/watchdog_worker.py`、`state._heartbeat` dict確認 |
| 修正候補リスト | `logs/pending_fixes.jsonl` |
