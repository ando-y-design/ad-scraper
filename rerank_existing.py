from __future__ import annotations
#!/usr/bin/env python3
"""
rerank_existing.py
DBの全企業を3シグナルで再調査してS/A/B/Cランクを更新し、
Google SheetsのF列（ランク列）も更新する。

シグナル:
  1. LP品質チェック（requests）: 最大4点
     - アクセス可能（200）: +1
     - 特商法ページあり: +2
     - 内部リンク20本以上: +1
  2. Meta広告ライブラリ（Playwright）: 最大3点
     - 同名企業が見つかる: +2
     - アクティブ広告あり: +1（上記に追加）
  3. Google/Yahoo再検索（Playwright）: 最大5点
     - Google広告に出現: +3
     - Yahoo広告に出現: +2

ランク基準:
  S: 8点以上
  A: 5〜7点
  B: 3〜4点
  C: 0〜2点
"""

import sqlite3
import json
import time
import random
import re
import logging
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin

warnings.filterwarnings("ignore")

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ── ログ設定 ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── 定数 ─────────────────────────────────────────────────────
DB_PATH = "/Users/holy/Downloads/ad_scraper/companies.db"
CONFIG_PATH = "/Users/holy/Downloads/ad_scraper/config.json"
CREDS_PATH = "/Users/holy/Downloads/ad_scraper/credentials.json"
SHEET_ID = "1NQysFvXeQzV76d4EfVQWn2z8HobJLMxCqx6GYNMBis4"
SHEET_NAME = "リスト"

LP_WORKERS = 10           # LP確認は並列
PLAYWRIGHT_DELAY = (3, 8)   # Playwright間のランダム待機（秒）

TOKUTEI_PATTERNS = re.compile(
    r"(tokutei|tokusho|law|commercial|legal|toku|kiyaku|kin_shi|regulation|terms)",
    re.IGNORECASE,
)


# ── ランク計算 ───────────────────────────────────────────────
def score_to_rank(score: int) -> str:
    if score >= 8:
        return "S"
    if score >= 5:
        return "A"
    if score >= 3:
        return "B"
    return "C"


# ══════════════════════════════════════════════════════════════
# 1. LP品質チェック（requests）
# ══════════════════════════════════════════════════════════════

def _normalize_lp_url(url: str) -> str:
    """LPのUTMパラメータを除去してベースURLを返す"""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("?")
    except Exception:
        return url


def check_lp_quality(lp_url: str, base_url: str) -> int:
    """LP品質チェック。最大4点。"""
    if not lp_url and not base_url:
        return 0

    # まずLP URLを試み、次にbase_urlのルートを試す
    urls_to_try = []
    if lp_url:
        urls_to_try.append(_normalize_lp_url(lp_url))
    if base_url:
        scheme = "https"
        domain = base_url.replace("https://", "").replace("http://", "").rstrip("/")
        urls_to_try.append(f"{scheme}://{domain}/")

    score = 0
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en-US;q=0.9",
    }

    html_content = None
    final_url = None

    for url in urls_to_try:
        try:
            resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
            if resp.status_code == 200:
                score += 1  # アクセス可能
                html_content = resp.text
                final_url = resp.url
                break
        except Exception:
            continue

    if html_content is None:
        return score

    # 特商法ページ確認（サイトリンクを探す）
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        all_links = soup.find_all("a", href=True)
        internal_links = []

        # ドメイン取得
        if final_url:
            domain = urlparse(final_url).netloc
        elif base_url:
            domain = base_url.replace("https://", "").replace("http://", "").rstrip("/")
        else:
            domain = ""

        for link in all_links:
            href = link["href"]
            abs_href = urljoin(final_url or url, href)
            parsed_href = urlparse(abs_href)

            # 内部リンク判定
            if domain and domain in parsed_href.netloc:
                internal_links.append(abs_href)

            # 特商法チェック
            if TOKUTEI_PATTERNS.search(href):
                score += 2
                break  # 1回だけ加算

        # 内部リンク20本以上
        if len(set(internal_links)) >= 20:
            score += 1

    except Exception:
        pass

    return score


def batch_check_lp(companies: list) -> dict:
    """LP品質を並列チェック。{id: score}を返す"""
    results = {}

    def task(row):
        cid, company_name, lp_url, base_url = row[0], row[1], row[2], row[3]
        try:
            s = check_lp_quality(lp_url, base_url)
            return cid, s
        except Exception as e:
            log.debug(f"LP check error [{company_name}]: {e}")
            return cid, 0

    with ThreadPoolExecutor(max_workers=LP_WORKERS) as ex:
        futures = {ex.submit(task, row): row for row in companies}
        done = 0
        total = len(companies)
        for f in as_completed(futures):
            cid, s = f.result()
            results[cid] = s
            done += 1
            if done % 50 == 0:
                log.info(f"  LP確認進捗: {done}/{total}")

    return results


# ══════════════════════════════════════════════════════════════
# 2. Meta広告ライブラリ（Playwright）
# ══════════════════════════════════════════════════════════════

META_ADS_URL = (
    "https://www.facebook.com/ads/library/"
    "?active_status=active&ad_type=all&country=JP"
    "&q={query}&search_type=keyword_unordered"
)


def check_meta_ads(page, company_name: str) -> int:
    """Meta広告ライブラリで検索。最大3点。"""
    score = 0
    url = META_ADS_URL.format(query=requests.utils.quote(company_name))
    try:
        page.goto(url, timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(2)

        content = page.content()

        # 「結果が見つかりません」系の否定テキスト
        no_result_patterns = [
            "No ads match your search",
            "広告が見つかりませんでした",
            "No results found",
            "広告は見つかりませんでした",
        ]
        found_no_result = any(p in content for p in no_result_patterns)

        if not found_no_result:
            # 会社名が含まれているか（広告カードの存在確認）
            if company_name[:4] in content:  # 会社名の先頭4文字で判定
                score += 2  # 同名企業が見つかる

                # アクティブ広告カードの確認
                active_indicators = [
                    'data-testid="ad_library_preview_card"',
                    "class=\"_7jys\"",
                    "アクティブ",
                    "現在実施中",
                ]
                if any(ind in content for ind in active_indicators):
                    score += 1

    except Exception as e:
        log.debug(f"Meta ads error [{company_name}]: {e}")

    return score


# ══════════════════════════════════════════════════════════════
# 3. Google/Yahoo再検索（Playwright）
# ══════════════════════════════════════════════════════════════

def check_google_ads(page, company_name: str, base_url: str) -> int:
    """Google広告検索。会社ドメインが広告に出ていれば+3点。"""
    if not base_url:
        return 0

    domain = base_url.replace("https://", "").replace("http://", "").split("/")[0].strip()
    query = f'"{company_name}"'
    search_url = f"https://www.google.co.jp/search?q={requests.utils.quote(query)}&gl=jp&hl=ja"

    try:
        page.goto(search_url, timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        time.sleep(2)

        content = page.content()

        # 広告ブロック内にドメインが含まれているか確認
        # Google広告は #tads, .uEierd, [data-text-ad] などのセレクタ
        # コンテンツ中のドメイン名チェック（広告部分に絞る）
        soup = BeautifulSoup(content, "html.parser")

        # 広告セクションを探す
        ad_sections = soup.select("#tads, .uEierd, [data-text-ad], .commercial-unit-desktop-top")
        ad_text = " ".join(s.get_text() + " " + str(s) for s in ad_sections)

        if domain in ad_text:
            return 3

        # フォールバック: ページ全体でのドメイン確認（広告タグ付近）
        # 「広告」「スポンサー」付近にドメインが出現するか
        ad_context_patterns = [
            f"スポンサー.*?{re.escape(domain)}",
            f"広告.*?{re.escape(domain)}",
            f"{re.escape(domain)}.*?スポンサー",
        ]
        for pat in ad_context_patterns:
            if re.search(pat, content, re.DOTALL | re.IGNORECASE):
                return 3

    except Exception as e:
        log.debug(f"Google ads error [{company_name}]: {e}")

    return 0


def check_yahoo_ads(page, company_name: str, base_url: str) -> int:
    """Yahoo広告検索。会社ドメインが広告に出ていれば+2点。"""
    if not base_url:
        return 0

    domain = base_url.replace("https://", "").replace("http://", "").split("/")[0].strip()
    query = f'"{company_name}"'
    search_url = f"https://search.yahoo.co.jp/search?p={requests.utils.quote(query)}&ei=UTF-8"

    try:
        page.goto(search_url, timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        time.sleep(2)

        content = page.content()
        soup = BeautifulSoup(content, "html.parser")

        # Yahoo広告セクション
        ad_sections = soup.select(".PR, .Ad, [data-ad], .yjSponsor, #yspl")
        ad_text = " ".join(s.get_text() + " " + str(s) for s in ad_sections)

        if domain in ad_text:
            return 2

        # フォールバック
        ad_context_patterns = [
            f"スポンサー.*?{re.escape(domain)}",
            f"PR.*?{re.escape(domain)}",
            f"{re.escape(domain)}.*?スポンサー",
        ]
        for pat in ad_context_patterns:
            if re.search(pat, content, re.DOTALL | re.IGNORECASE):
                return 2

    except Exception as e:
        log.debug(f"Yahoo ads error [{company_name}]: {e}")

    return 0


# ══════════════════════════════════════════════════════════════
# Google Sheets 更新
# ══════════════════════════════════════════════════════════════

def update_sheets(phone_rank_map: dict):
    """
    Sheetsのリストシートを読み込み、I列（電話番号）でDBレコードと照合し
    F列（ランク列）を一括更新する。
    """
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(CREDS_PATH, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SHEET_ID)
        ws = sh.worksheet(SHEET_NAME)

        log.info("Sheets: 全データ読み込み中...")
        all_values = ws.get_all_values()
        log.info(f"Sheets: {len(all_values)}行 読み込み完了")

        # ヘッダー行確認（row 0）
        # F列 = index 5, I列 = index 8
        updates = []
        updated_count = 0

        for row_idx, row in enumerate(all_values):
            if row_idx == 0:
                continue  # ヘッダー行スキップ

            # I列（index 8）が電話番号
            if len(row) < 9:
                continue

            phone = row[8].strip()
            if not phone:
                continue

            if phone in phone_rank_map:
                new_rank = phone_rank_map[phone]
                current_rank = row[5] if len(row) > 5 else ""

                if current_rank != new_rank:
                    # gspreadのrow番号は1始まり、ヘッダー行があるので+1
                    sheet_row = row_idx + 1
                    updates.append({
                        "range": f"F{sheet_row}",
                        "values": [[new_rank]],
                    })
                    updated_count += 1

        log.info(f"Sheets: 更新対象 {updated_count}行")

        if updates:
            # バッチ更新（50件ずつ）
            batch_size = 50
            for i in range(0, len(updates), batch_size):
                batch = updates[i:i+batch_size]
                ws.batch_update(batch)
                log.info(f"Sheets: バッチ更新 {i+batch_size}/{len(updates)} 完了")
                time.sleep(1)

        log.info("Sheets更新完了")

    except Exception as e:
        log.error(f"Sheets更新エラー: {e}")
        raise


# ══════════════════════════════════════════════════════════════
# メイン処理
# ══════════════════════════════════════════════════════════════

def main():
    log.info("=== rerank_existing.py 開始 ===")

    # DB読み込み
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT id, company_name, lp_url, base_url, phone, keyword FROM companies ORDER BY id"
    )
    rows = cur.fetchall()
    companies = [dict(r) for r in rows]
    total = len(companies)
    log.info(f"DB読み込み完了: {total}社")

    # ── ステップ1: LP品質チェック（並列）────────────────────
    log.info("【ステップ1】LP品質チェック開始（並列10件）")
    lp_data = [(r["id"], r["company_name"], r["lp_url"], r["base_url"]) for r in companies]
    lp_scores = batch_check_lp(lp_data)
    log.info("LP品質チェック完了")

    # ── ステップ2 & 3: Playwright検索 ───────────────────────
    log.info("【ステップ2&3】Meta/Google/Yahoo検索開始（Playwright）")

    meta_scores = {}
    google_scores = {}
    yahoo_scores = {}

    def _make_browser_and_page(pw):
        """ブラウザとページを新規作成して返す"""
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        page = context.new_page()
        page.set_extra_http_headers({"Accept-Language": "ja,en-US;q=0.9"})
        return browser, context, page

    with sync_playwright() as pw:
        browser, context, page = _make_browser_and_page(pw)

        for idx, company in enumerate(companies):
            cid = company["id"]
            name = company["company_name"]
            base_url = company["base_url"] or ""

            # 進捗ログ（10件ごと）
            if (idx + 1) % 10 == 0 or idx == 0:
                log.info(f"  Playwright進捗: {idx+1}/{total} ({name})")

            # ブラウザ再起動チェック（30社ごと、またはページが死んでいたら）
            if (idx + 1) % 30 == 0:
                try:
                    context.close()
                    browser.close()
                except Exception:
                    pass
                log.info(f"  ブラウザ再起動（{idx+1}社目）")
                browser, context, page = _make_browser_and_page(pw)

            # Meta広告ライブラリ
            try:
                ms = check_meta_ads(page, name)
                meta_scores[cid] = ms
            except Exception as e:
                log.warning(f"  Meta error [{name}]: {e}")
                meta_scores[cid] = 0
                # ページが壊れた場合は再起動
                try:
                    context.close()
                    browser.close()
                except Exception:
                    pass
                try:
                    browser, context, page = _make_browser_and_page(pw)
                except Exception as e2:
                    log.error(f"  ブラウザ再起動失敗: {e2}")

            # ランダム待機
            time.sleep(random.uniform(*PLAYWRIGHT_DELAY))

            # Google検索
            try:
                gs = check_google_ads(page, name, base_url)
                google_scores[cid] = gs
            except Exception as e:
                log.warning(f"  Google error [{name}]: {e}")
                google_scores[cid] = 0

            time.sleep(random.uniform(*PLAYWRIGHT_DELAY))

            # Yahoo検索
            try:
                ys = check_yahoo_ads(page, name, base_url)
                yahoo_scores[cid] = ys
            except Exception as e:
                log.warning(f"  Yahoo error [{name}]: {e}")
                yahoo_scores[cid] = 0

            # 次の会社へ（レート制限対策）
            # 10社ごとに少し長めの待機
            if (idx + 1) % 10 == 0:
                time.sleep(random.uniform(10, 15))
            else:
                time.sleep(random.uniform(2, 4))

        try:
            context.close()
            browser.close()
        except Exception:
            pass

    log.info("Playwright検索完了")

    # ── スコア集計 & DB更新 ──────────────────────────────────
    log.info("スコア集計・DB更新中...")

    phone_rank_map = {}  # {phone: rank}
    rank_dist = {"S": 0, "A": 0, "B": 0, "C": 0}

    for company in companies:
        cid = company["id"]
        phone = company["phone"] or ""

        lp_s = lp_scores.get(cid, 0)
        meta_s = meta_scores.get(cid, 0)
        google_s = google_scores.get(cid, 0)
        yahoo_s = yahoo_scores.get(cid, 0)

        total_score = lp_s + meta_s + google_s + yahoo_s
        rank = score_to_rank(total_score)

        rank_dist[rank] += 1

        if phone:
            phone_rank_map[phone] = rank

        # DB更新
        cur.execute(
            "UPDATE companies SET rank = ? WHERE id = ?",
            (rank, cid),
        )

    conn.commit()
    conn.close()

    log.info("DB更新完了")
    log.info(f"ランク分布: S={rank_dist['S']}, A={rank_dist['A']}, B={rank_dist['B']}, C={rank_dist['C']}")

    # ── Google Sheets更新 ─────────────────────────────────────
    log.info("【ステップ4】Google Sheets更新中...")
    update_sheets(phone_rank_map)

    log.info("=== rerank_existing.py 完了 ===")
    log.info(f"最終ランク分布: S={rank_dist['S']}, A={rank_dist['A']}, B={rank_dist['B']}, C={rank_dist['C']}")

    return rank_dist


if __name__ == "__main__":
    main()
