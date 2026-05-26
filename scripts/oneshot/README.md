# scripts/oneshot/

一回限り実行スクリプトのアーカイブ。

## 命名規則

```
{説明}_{YYYYMMDD}.py         # 実行前
{説明}_{YYYYMMDD}_done.py    # 実行済み（削除しない）
```

## ルール

- 実行前: ファイル名に `_done` なし
- 実行後: `_done` をつけてここに残す（削除しない）
- 再実行防止のため、スクリプト冒頭に実行済みガードを書くことを推奨

```python
# 実行済みガード例
import sys
print("このスクリプトは実行済みです。再実行する場合は _done を外してください。")
sys.exit(0)
```

## アーカイブ済みスクリプト

| ファイル | 目的 | 実行日 |
|---------|------|-------|
| rewrite_sheet_v2_done.py | Google Sheetsを7列→6列（業種削除）に移行 | 2026-05-08 |
| fix_labeled_done.py | `_is_valid_company_labeled` を全角スペース対応に更新 | 2026-05-08 |
