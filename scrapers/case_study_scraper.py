from __future__ import annotations
from typing import Optional
"""
競合代理店の「導入事例」ページから広告主企業を逆引きするスクレイパー

取得戦略:
  1. 競合代理店の事例一覧ページをフェッチ
  2. 個別事例ページURLを収集（最大30件/代理店）
  3. 各事例ページから: 顧客企業名 + クライアント公式サイトURL を抽出
  4. クライアントURLがあれば find_company_info() で電話番号取得
  5. DBに挿入（ad_sources='<代理店名>事例', keyword='<代理店名>事例'）

実行頻度: 週1回（内部のstate fileで管理）
"""
import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from processors.company_finder import (
    _is_valid_company, _is_valid_company_labeled, _normalize_name, find_company_info,
)
from processors.normalizer import get_base_domain, normalize_company, normalize_url
from storage.database import get_connection, insert_company, is_duplicate, update_ad_sources

BASE_DIR = Path(__file__).parent.parent
_STATE_FILE = BASE_DIR / 'logs' / 'case_study_state.json'
_RUN_INTERVAL = 7 * 24 * 3600  # 週1回

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en-US;q=0.8',
    'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
}

# ─────────────────────────────────────────────
# 競合代理店リスト
# url:           事例一覧ページ
# case_link_re:  個別事例ページへのリンクパターン
# ─────────────────────────────────────────────
COMPETITOR_PAGES = [
    {'agency': 'オプト',          'url': 'https://www.opt.ne.jp/cases/',
     'case_link_re': r'/cases/\d+'},
    {'agency': 'アイレップ',      'url': 'https://www.irep.co.jp/case/',
     'case_link_re': r'/case/[a-z0-9_-]+/?$'},
    {'agency': 'GMO NIKKO',       'url': 'https://www.gmo-nikko.co.jp/cases/',
     'case_link_re': r'/cases/\d+'},
    {'agency': 'ナイル',          'url': 'https://nyle.co.jp/service/cases/',
     'case_link_re': r'/service/cases/\w+'},
    {'agency': 'アナグラム',      'url': 'https://anagrams.jp/case/',
     'case_link_re': r'/case/[a-z0-9_-]+/?$'},
    {'agency': 'オーリーズ',      'url': 'https://aulys.jp/cases/',
     'case_link_re': r'/cases/[a-z0-9_-]+/?$'},
    {'agency': 'Shirofune',       'url': 'https://shirofune.com/case/',
     'case_link_re': r'/case/[a-z0-9_-]+/?$'},
    {'agency': 'PLAN-B',          'url': 'https://www.plan-b.co.jp/case/',
     'case_link_re': r'/case/\d+'},
    {'agency': 'フルスピード',    'url': 'https://www.fullspeed.co.jp/case/',
     'case_link_re': r'/case/[a-z0-9_-]+/?$'},
    {'agency': 'ソウルドアウト',  'url': 'https://soldout.co.jp/results/',
     'case_link_re': r'/results/[a-z0-9_-]+/?$'},
    {'agency': 'セプテーニ',      'url': 'https://www.septeni.co.jp/case/',
     'case_link_re': r'/case/\d+'},
    {'agency': 'トランスコスモス','url': 'https://www.trans-cosmos.co.jp/casestudy/',
     'case_link_re': r'/casestudy/\d+'},
    {'agency': 'DAC',             'url': 'https://www.dac.co.jp/case/',
     'case_link_re': r'/case/[a-z0-9_-]+/?$'},
    {'agency': 'Presco',          'url': 'https://presco.co.jp/case/',
     'case_link_re': r'/case/[a-z0-9_-]+/?$'},
    {'agency': 'ハーモニー',      'url': 'https://www.harmony-digital.com/case/',
     'case_link_re': r'/case/[a-z0-9_-]+/?$'},
]

# 事例ページでクライアント企業名を示すラベル
_CLIENT_LABELS = frozenset({
    'クライアント', '企業名', '会社名', '事業者名', 'お客様', '導入企業',
    'お客さま', 'ご依頼企業', '顧客名', 'client', 'company', 'customer',
    'お客様名', 'クライアント企業',
})

# クライアント公式サイトを示すリンクテキストキーワード
_CLIENT_LINK_KEYWORDS = ('公式サイト', 'ウェブサイト', 'website', 'official', 'ホームページ', 'hp', 'webサイト')

# 外部リンクとして除外するドメイン
_EXCLUDED_DOMAINS = frozenset({
    'facebook.com', 'twitter.com', 'x.com', 'instagram.com', 'youtube.com',
    'google.com', 'google.co.jp', 'yahoo.co.jp', 'linkedin.com',
    'amazon.co.jp', 'amazon.com', 'rakuten.co.jp', 'note.com',
    'prtimes.jp', 'wantedly.com', 'atpress.ne.jp', 'prwire.jp',
})

# 法人格を含む会社名を抽出する正規表現
_COMPANY_NAME_RE = re.compile(
    r'((?:株式会社|有限会社|合同会社|一般社団法人|医療法人|NPO法人|特定非営利活動法人)'
    r'[^\s「」、。・\|｜/／]{1,30}'
    r'|[^\s「」、。・\|｜/／]{1,30}'
    r'(?:株式会社|有限会社|合同会社|ホールディングス|HD|Holdings))'
)


def _fetch(url: str, timeout: int = 10) -> Optional[BeautifulSoup]:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            resp.encoding = resp.apparent_encoding or 'utf-8'
            return BeautifulSoup(resp.text, 'lxml')
    except Exception as e:
        logging.debug(f'[CaseStudy] fetch失敗 {url}: {e}')
    return None


def _extract_case_links(soup: BeautifulSoup, base_url: str, pattern: str) -> list[str]:
    """一覧ページから個別事例ページのURLを抽出する（最大30件）"""
    re_pat = re.compile(pattern)
    seen: set[str] = set()
    links: list[str] = []
    for a in soup.find_all('a', href=True):
        full = urljoin(base_url, a['href'])
        path = urlparse(full).path
        if re_pat.search(path) and full not in seen:
            seen.add(full)
            links.append(full)
    return links[:30]


def _extract_company_from_soup(soup: BeautifulSoup) -> Optional[str]:
    """事例ページから顧客企業名を抽出する（複数パターン対応）"""

    # 1. ラベル-値 構造（th/dt/td/div/span）
    for label_tag in soup.find_all(['th', 'dt', 'td', 'div', 'span', 'p']):
        label_text = label_tag.get_text(strip=True)
        if len(label_text) > 25:
            continue
        if not any(lbl in label_text for lbl in _CLIENT_LABELS):
            continue

        # 直接の次兄弟
        for sibling in label_tag.find_next_siblings(['td', 'dd', 'div', 'span', 'p'], limit=1):
            candidate = _normalize_name(sibling.get_text(strip=True))
            if _is_valid_company(candidate) or _is_valid_company_labeled(candidate):
                return candidate

        # 親要素の行分割
        parent = label_tag.parent
        if parent:
            lines = [l.strip() for l in parent.get_text(separator='\n').split('\n') if l.strip()]
            for i, line in enumerate(lines):
                if any(lbl in line for lbl in _CLIENT_LABELS) and i + 1 < len(lines):
                    candidate = _normalize_name(lines[i + 1])
                    if _is_valid_company(candidate) or _is_valid_company_labeled(candidate):
                        return candidate

    # 2. 見出し（h1/h2/h3）から法人格を含む社名を正規表現で抽出
    for tag in soup.find_all(['h1', 'h2', 'h3']):
        text = tag.get_text(strip=True)
        m = _COMPANY_NAME_RE.search(text)
        if m:
            candidate = _normalize_name(m.group(0).strip('様　 '))
            if _is_valid_company(candidate) or _is_valid_company_labeled(candidate):
                return candidate

    # 3. og:title / twitter:title から抽出
    for attr_name, attr_val in [('property', 'og:title'), ('name', 'twitter:title')]:
        tag = soup.find('meta', attrs={attr_name: attr_val})
        if tag:
            content = tag.get('content', '')
            m = _COMPANY_NAME_RE.search(content)
            if m:
                candidate = _normalize_name(m.group(0))
                if _is_valid_company(candidate) or _is_valid_company_labeled(candidate):
                    return candidate

    # 4. ページ全体テキストから最初に出現する法人名
    full_text = soup.get_text(separator='\n', strip=True)
    for m in _COMPANY_NAME_RE.finditer(full_text):
        candidate = _normalize_name(m.group(0).strip('様　 '))
        if _is_valid_company(candidate):
            return candidate

    return None


def _extract_client_url(soup: BeautifulSoup, agency_url: str) -> Optional[str]:
    """事例ページから顧客企業の公式サイトURLを抽出する。
    明確に「公式サイト」と示されているリンクのみ返す（誤収集防止）。
    """
    agency_domain = get_base_domain(agency_url)
    excluded = _EXCLUDED_DOMAINS | {agency_domain}

    for a in soup.find_all('a', href=True):
        href = a.get('href', '')
        if not href.startswith('http'):
            href = urljoin(agency_url, href)
        if not href.startswith('http'):
            continue

        domain = get_base_domain(href)
        if not domain or domain in excluded:
            continue

        # リンクテキスト or 親要素テキストに「公式サイト」等が含まれる場合のみ採用
        link_text = a.get_text(strip=True).lower()
        parent_text = (a.parent.get_text(strip=True) if a.parent else '').lower()
        combined = link_text + ' ' + parent_text

        if any(kw in combined for kw in _CLIENT_LINK_KEYWORDS):
            return href

    return None


def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {'last_run': 0, 'processed_urls': []}


def _save_state(state: dict):
    try:
        _STATE_FILE.parent.mkdir(exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    except Exception as e:
        logging.warning(f'[CaseStudy] state保存失敗: {e}')


def run_case_study_scrape(conn=None, force: bool = False) -> int:
    """
    競合代理店の事例ページをスクレイプしてDBに挿入する。
    週1回だけ実行（state fileで管理）。force=Trueで強制実行。
    Returns: 新規挿入件数
    """
    state = _load_state()
    now = time.time()

    if not force and now - state.get('last_run', 0) < _RUN_INTERVAL:
        logging.debug('[CaseStudy] 前回実行から7日未満のためスキップ')
        return 0

    if conn is None:
        conn = get_connection()

    state['last_run'] = now
    processed_urls: set[str] = set(state.get('processed_urls', []))
    total_inserted = 0

    logging.info(f'[CaseStudy] 競合事例スクレイプ開始 ({len(COMPETITOR_PAGES)}社)')

    for agency_info in COMPETITOR_PAGES:
        agency = agency_info['agency']
        index_url = agency_info['url']
        case_link_re = agency_info.get('case_link_re', '')

        logging.info(f'[CaseStudy] {agency} 取得中: {index_url}')

        soup = _fetch(index_url)
        if not soup:
            logging.warning(f'[CaseStudy] {agency}: 一覧ページ取得失敗')
            continue

        # 個別事例ページURLを収集
        case_urls = _extract_case_links(soup, index_url, case_link_re) if case_link_re else []

        if not case_urls:
            # 個別ページが見つからなければ一覧ページ自体から企業名を抽出
            logging.debug(f'[CaseStudy] {agency}: 個別事例URLなし → 一覧ページから直接抽出')
            case_urls = [index_url]

        logging.info(f'[CaseStudy] {agency}: {len(case_urls)}件の事例を処理')

        for case_url in case_urls:
            if case_url in processed_urls:
                continue

            time.sleep(random.uniform(2.0, 4.0))

            case_soup = _fetch(case_url)
            if not case_soup:
                processed_urls.add(case_url)
                continue

            company_name = _extract_company_from_soup(case_soup)
            if not company_name:
                logging.debug(f'[CaseStudy] 企業名取得失敗: {case_url}')
                processed_urls.add(case_url)
                continue

            client_url = _extract_client_url(case_soup, index_url)

            # 電話番号取得
            phone = None
            if client_url:
                found_name, phone = find_company_info(client_url, company_name)
                company_name = found_name or company_name

            if not phone:
                logging.debug(f'[CaseStudy] {company_name}: 電話番号取得失敗')
                processed_urls.add(case_url)
                continue

            normalized = normalize_company(company_name)
            base_url = get_base_domain(client_url) if client_url else ''

            # 重複チェック
            if is_duplicate(conn, normalized, base_url, phone):
                existing = conn.execute(
                    'SELECT normalized_name FROM companies '
                    'WHERE normalized_name=? OR base_url=? OR phone=?',
                    (normalized, base_url, phone)
                ).fetchone()
                if existing:
                    update_ad_sources(conn, existing['normalized_name'], f'{agency}事例')
                processed_urls.add(case_url)
                continue

            data = {
                'company_name': company_name.strip(),
                'normalized_name': normalized,
                'lp_url': normalize_url(client_url) if client_url else '',
                'base_url': base_url,
                'phone': phone,
                'ad_sources': f'{agency}事例',
                'keyword': f'{agency}事例',
                'found_date': datetime.now().strftime('%Y-%m-%d'),
            }

            if insert_company(conn, data):
                total_inserted += 1
                logging.info(
                    f'[CaseStudy] ✓ {company_name} / {phone} (出典: {agency})'
                )

            processed_urls.add(case_url)

        # 代理店ごとにstate保存（途中クラッシュ時に再実行でスキップできる）
        state['processed_urls'] = list(processed_urls)[-500:]
        _save_state(state)

        time.sleep(random.uniform(5.0, 10.0))

    state['processed_urls'] = list(processed_urls)[-500:]
    _save_state(state)
    logging.info(f'[CaseStudy] 完了: {total_inserted}件を新規登録')
    return total_inserted
