# セットアップ手順

パソコンに慣れていなくても、この手順通りにやれば動きます。

---

## ステップ1：Pythonをインストール

1. https://www.python.org/downloads/ を開く
2. 「Download Python」ボタンをクリックしてインストール
3. インストール画面で **「Add Python to PATH」にチェックを入れる**（重要）

> すでにインストール済みの場合はスキップ

---

## ステップ2：ターミナルを開く

**Mac の場合：**
`Command（⌘）+ スペース` → 「ターミナル」と入力 → Enter

**Windows の場合：**
スタートメニュー →「PowerShell」と検索 → 開く

---

## ステップ3：セットアップコマンドを実行

**Mac の場合：** 以下をターミナルにコピペしてEnter

```
curl -fsSL https://raw.githubusercontent.com/ando-y-design/ad-scraper/main/setup_mac_auto.sh | bash
```

**Windows の場合：** 以下をPowerShellにコピペしてEnter

```
git clone https://github.com/ando-y-design/ad-scraper.git %USERPROFILE%\ad_scraper && cd %USERPROFILE%\ad_scraper && setup_admin.bat
```

---

## ステップ4：credentials.json を受け取る

村上からもらった `credentials.json` ファイルを以下の場所に置く：

- **Mac：** `/Users/あなたのユーザー名/ad_scraper/credentials.json`
- **Windows：** `C:\Users\あなたのユーザー名\ad_scraper\credentials.json`

> ファイルを ad_scraper フォルダの中に入れるだけでOK

---

## ステップ5：起動

**Mac：**
```
cd ~/ad_scraper && python3 main.py
```

**Windows：**
```
cd %USERPROFILE%\ad_scraper
python main.py
```

---

## 最新版に更新する方法

改良があったときは以下のコマンドだけでOK：

**Mac：**
```
cd ~/ad_scraper && git pull
```

**Windows：**
```
cd %USERPROFILE%\ad_scraper
git pull
```

---

## うまくいかないときは

村上に連絡してください。
