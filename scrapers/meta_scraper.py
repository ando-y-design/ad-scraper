from __future__ import annotations
import json
import logging
import random
import threading
import time
from urllib.parse import quote

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

META_LIBRARY_URL = (
    'https://www.facebook.com/ads/library/'
    '?active_status=active&ad_type=all&country=JP'
    '&q={keyword}&search_type=keyword_unordered'
)

COMPANY_KEY_CANDIDATES = [
    'page_name', 'advertiser_name', 'page_identity',
    'name', 'title', 'entity_name', 'page_profile_uri',
]
URL_KEY_CANDIDATES = [
    'link_url', 'url', 'destination_url',
    'landing_url', 'target_url', 'website_url',
    'display_url',
]

# ログインモーダルを閉じるセレクター（複数パターン対応）
_LOGIN_MODAL_CLOSE_SELECTORS = [
    '[aria-label="閉じる"]',
    '[aria-label="Close"]',
    '[aria-label="Dismiss"]',
    'div[role="dialog"] [role="button"]:first-child',
]


def _find_key(data, candidates: list) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in candidates:
        if key in data and data[key] and isinstance(data[key], str):
            val = data[key].strip()
            if val:
                return val
    for val in data.values():
        if isinstance(val, dict):
            result = _find_key(val, candidates)
            if result:
                return result
        elif isinstance(val, list):
            for item in val:
                result = _find_key(item, candidates)
                if result:
                    return result
    return None


def _extract_ads_from_graphql(body) -> list[dict]:
    ads = []
    body_str = json.dumps(body, ensure_ascii=False)

    if 'ad_library_main' not in body_str and 'edges' not in body_str:
        return ads

    stack = [body]
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            company = _find_key(obj, COMPANY_KEY_CANDIDATES)
            url = _find_key(obj, URL_KEY_CANDIDATES)
            if url and url.startswith('http') and 'facebook.com' not in url:
                ads.append({'url': url, 'company': company})
            stack.extend(obj.values())
        elif isinstance(obj, list):
            stack.extend(obj)

    return ads


def _dismiss_login_modal(page: Page) -> None:
    """ログインを促すモーダルが表示されていれば閉じる（ログイン不要のため）"""
    for selector in _LOGIN_MODAL_CLOSE_SELECTORS:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                time.sleep(0.3)
                return
        except Exception:
            continue

    # Escキーでも閉じられる場合がある
    try:
        page.keyboard.press('Escape')
    except Exception:
        pass


def scrape_meta(page: Page, keyword: str) -> list[dict]:
    """
    Meta広告ライブラリから広告を収集する。
    ログイン不要（公開データ）。ログインモーダルが出ても自動で閉じる。
    """
    results = []
    results_lock = threading.Lock()

    def handle_response(response):
        if 'facebook.com/api/graphql' not in response.url:
            return
        try:
            body = response.json()
            ads = _extract_ads_from_graphql(body)
            with results_lock:
                results.extend(ads)
        except Exception:
            pass

    page.on('response', handle_response)

    try:
        url = META_LIBRARY_URL.format(keyword=quote(keyword))
        page.goto(url, timeout=20000, wait_until='domcontentloaded')

        # ログインページへリダイレクトされた場合はリセットして終了
        if 'login' in page.url or 'checkpoint' in page.url:
            logging.warning(f'[Meta] ログインページ検出 → ページリセット "{keyword}"')
            try:
                page.goto('about:blank', timeout=5000)
            except Exception:
                pass
            return []

        time.sleep(random.uniform(1, 2))

        # ログインモーダルを閉じる
        _dismiss_login_modal(page)

        time.sleep(random.uniform(1, 2))

        # スクロールで追加広告を読み込む
        for _ in range(4):
            page.mouse.wheel(0, 900)
            time.sleep(random.uniform(1.2, 2.0))

    except PlaywrightTimeoutError:
        logging.warning(f'[Meta] タイムアウト "{keyword}"')
        return []
    except Exception as e:
        logging.warning(f'[Meta] 失敗 "{keyword}": {e}')
        return []
    finally:
        try:
            page.remove_listener('response', handle_response)
        except Exception:
            pass

    # 重複URL除去
    seen: set[str] = set()
    unique = []
    for ad in results:
        ad_url = ad.get('url', '')
        if ad_url and ad_url not in seen:
            seen.add(ad_url)
            unique.append(ad)

    logging.info(f'[Meta] 広告 {len(unique)}件発見: "{keyword}"')
    return unique


def check_meta_login(page: Page) -> bool:
    """ページが広告ライブラリに到達できているか確認（ログイン不要モードでも使用可）"""
    try:
        current_url = page.url
        if not current_url:
            return False
        
        url_lower = current_url.lower()
        
        # ログイン/チェックポイント/認証ページの検出（複数パターン）
        logout_patterns = ['login', 'checkpoint', 'auth', 'accounts.facebook.com', '/signin']
        if any(pattern in url_lower for pattern in logout_patterns):
            return False
        
        # ページ内容でセッション切れを検出
        try:
            content = page.content().lower()
            # セッション切れの典型的なメッセージ
            expired_keywords = ['session expired', 'logged out', 'please log in', 'unauthorized', 'your session']
            if any(keyword in content for keyword in expired_keywords):
                return False
        except Exception:
            pass
        
        return True
    except Exception:
        return False


def do_meta_login(page: Page, email: str, password: str) -> bool:
    """Metaにログイン（ログイン不要モードでは呼ばれないが互換性のため残す）"""
    try:
        page.goto('https://www.facebook.com/login/', timeout=15000, wait_until='domcontentloaded')
        time.sleep(2)
        page.fill('input[name="email"]', email)
        time.sleep(0.5)
        page.fill('input[name="pass"]', password)
        time.sleep(0.5)
        page.click('button[name="login"]')
        time.sleep(5)
        if 'login' not in page.url and 'checkpoint' not in page.url:
            logging.info('[Meta] ログイン成功')
            return True
        else:
            logging.error('[Meta] ログイン後もログインページに留まっている')
            return False
    except Exception as e:
        logging.error(f'[Meta] ログイン失敗: {e}')
        return False
