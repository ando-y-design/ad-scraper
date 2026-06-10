# スキル: NTA法人名正規化

## トリガー
「NTA」「法人番号」「正式な法人名」「会社名のゴミ削除」「G列を正式名に」などのキーワードとスプレッドシートURLがセットで出てきたとき。

## ゴール
SpreadsheetのG列（会社名）を正式な法人名に統一し、A列に法人番号を書く。

**鉄則: NTAが確認した名前のみG列に書く。自前の推測名は絶対に書かない。**

## 前提
- 作業ディレクトリ: `/Users/holy/Downloads/ad_scraper/`
- 認証: `credentials.json`（Google Sheets + Drive）
- NTA APIキー: `config.json` の `nta_api_key`
- ad_scraperのモジュール群が使用可能

## 手順

### STEP 1: スプレッドシートのシート構造を確認
URLからSheet IDを抽出。タブ一覧とヘッダー行を表示して、会社名列・法人番号列の位置を確認する。

```python
import sys, warnings; warnings.filterwarnings('ignore'); sys.path.insert(0,'.')
import json, gspread
from google.oauth2.service_account import Credentials

SHEET_ID = '<URLから抽出>'
with open('config.json') as f:
    config = json.load(f)
creds = Credentials.from_service_account_file('credentials.json', scopes=[
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
])
gc = gspread.authorize(creds)
sh = gc.open_by_key(SHEET_ID)
for ws in sh.worksheets():
    print(f'  id={ws.id} title={ws.title}')
ws = sh.worksheet('リスト')  # タブ名を適宜変更
all_vals = ws.get_all_values()
print('ヘッダー:', all_vals[0])
print(f'データ行数: {len(all_vals)-1}')
```

### STEP 2: 処理件数を事前確認（dry-run）
法人番号なし件数・ゴミ疑いの件数を表示して確認を取る。

```python
no_corp = sum(1 for r in all_vals[1:] if not r[CORP_COL].strip() and r[NAME_COL].strip())
print(f'法人番号なし: {no_corp}件')
```

### STEP 3: NTA一括処理
**ルール（厳守）:**
- NTA lookup はまず元の名前で試みる
- 失敗したら `_strip_garbage()` で軽度クリーニング後に再トライ
- G列更新は「NTA返却の official_name」のみ。自前クリーニング結果は書かない
- A列更新は `corp_num` が取れたときのみ

```python
import time
from processors.legal_name_resolver import lookup_corporate_number, _strip_garbage, _is_safe_nta_match

nta_key = config.get('nta_api_key', '')
CORP_COL = 0   # A列（0始まり）
NAME_COL = 6   # G列（0始まり）
ok = 0
skipped = 0

for row_idx, row in enumerate(all_vals[1:], start=2):
    corp = row[CORP_COL].strip() if len(row) > CORP_COL else ''
    name = row[NAME_COL].strip() if len(row) > NAME_COL else ''
    if not name or corp:   # 既に法人番号あり → スキップ
        continue

    # まず元の名前でNTA検索
    corp_num, official_name = lookup_corporate_number(name, nta_key)

    if corp_num == '__NTA_ERROR__':
        skipped += 1
        time.sleep(2)
        continue
    if not corp_num:
        skipped += 1
        continue

    # A列: 法人番号
    ws.update_cell(row_idx, CORP_COL + 1, corp_num)
    time.sleep(0.3)

    # G列: NTA正式名（元の名前と違う場合のみ更新）
    if official_name and official_name != name:
        ws.update_cell(row_idx, NAME_COL + 1, official_name)
        print(f'  行{row_idx}: {name!r} → {official_name!r} / {corp_num}')
        time.sleep(0.3)
    else:
        print(f'  行{row_idx}: {name!r} / {corp_num}')

    ok += 1
    time.sleep(0.3)

print(f'完了: 法人番号取得{ok}件 / 未取得{skipped}件')
```

### STEP 4: 結果サマリーをユーザーに報告
- 法人番号取得件数
- G列に正式名を書いた件数
- 未取得件数（NTA未登録・屋号・外資など）
- スプレッドシートのリンクを必ず貼る

## 注意事項
- `_normalize_name()` は使わない。英語スペース区切りの会社名を誤って切る
- `_strip_garbage()` + `_is_safe_nta_match()` の組み合わせは `lookup_corporate_number` 内で自動適用される
- 誤マッチ防止: 法人格一致 + コア名完全一致のみ採用（部分一致・類似一致は全却下）
- 処理後はスプレッドシートのURLを提示する
