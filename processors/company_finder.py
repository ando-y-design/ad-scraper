from __future__ import annotations
from typing import Optional
import html as _html
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

from processors.normalizer import get_base_domain, normalize_url
from processors.phone_finder import extract_phone, extract_all_phones, is_freephone

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en-US;q=0.8',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

TOKUTEI_PATHS = [
    '/tokutei', '/tokusho', '/tokushoho', '/legal', '/law', '/scta',
    '/tokuteishoho', '/kaijijikou', '/company/legal', '/about/legal',
    '/info/legal', '/terms/tokutei', '/tokutei.html', '/tokusho.html',
    '/legal.html', '/law.html', '/company/tokutei', '/company/tokusho',
    '/specified-commercial-transactions', '/act',
    '/tokutei-shoho', '/shougyou', '/teishi',
    '/legal/tokutei', '/info/tokutei', '/policy/legal',
    '/service/legal', '/help/legal', '/support/legal',
    '/terms/legal', '/company/law', '/corporate/legal',
    '/kiyaku', '/terms', '/tos',
    '/tokushohou', '/tokuteishouho',
    '/company/specified-commercial-transactions',
    '/pages/tokutei', '/pages/legal', '/pages/law',
    '/static/tokutei', '/static/legal',
    '/lp/tokutei', '/lp/legal',
    '/doc/tokutei', '/doc/legal',
    '/help/tokutei', '/support/tokutei',
    '/commercial', '/commerce',
    '/notation', '/disclosure',
    '/corporate/law',
    # trailing slash variants
    '/tokutei/', '/legal/', '/law/', '/tokusho/',
    # additional common paths
    '/specified-commercial-transactions-law',
    '/tokutei-shoho-ni-motozuku-hyoki',
    '/tokuteishouhohou',
    '/company/tokutei-shoho',
    '/about/tokutei',
    '/tokutei_shoho',
    '/legal_notice',
    '/legalnotice',
    '/legal-notice',
    '/tokushohou.html',
    '/commerce.html',
    '/notation.html',
    '/act.html',
    # パターン追加（欠落カバー）
    '/company-info', '/companyinfo',
    '/contact-info', '/contactinfo',
    '/about-company', '/aboutcompany',
    '/company-profile', '/companyprofile',
    '/info/company', '/info/contact',
    '/page/tokutei', '/page/legal',
    '/content/tokutei', '/content/legal',
    '/privacy', '/privacy.html',
    '/agreement', '/agreement.html',
    '/特商法', '/特定商取引',
    # 追加: 一般的なルール・規約パターン
    '/rule', '/rules', '/regulations',
    '/terms-of-service', '/tos.html',
    '/company/rules', '/legal/rules',
]

COMPANY_PATHS = [
    '/company', '/about', '/corporate', '/kaisha', '/gaiyou',
    '/company/profile', '/about/company', '/corporate/profile',
    '/aboutus', '/company.html', '/about.html', '/corporate.html',
    '/info', '/profile',
    '/company/about', '/about/us', '/our-company',
    '/overview', '/outline', '/kaisha-gaiyou',
    '/company/overview', '/corporate/overview',
    '/company/info', '/corporate/info',
    '/about/profile', '/about/overview',
    '/kaisha/gaiyou', '/kaisha/profile',
    '/company/', '/about/', '/corporate/',
    # パターン追加（欠落カバー）
    '/company-info', '/companyinfo',
    '/company-profile', '/companyprofile',
    '/about-us', '/aboutus',
    '/our-company', '/ourcompany',
    '/team', '/team.html',
    '/page/company', '/page/about',
    '/content/company', '/content/about',
]

# 電話番号が記載されやすいお問い合わせ・サポートページ
CONTACT_PATHS = [
    '/contact', '/contact/', '/contact.html',
    '/inquiry', '/inquiry/', '/inquiry.html',
    '/contact-us', '/contactus', '/contact_us',
    '/support', '/support/contact', '/support/',
    '/info/contact', '/company/contact',
    '/access', '/access.html', '/access/',
    # パターン追加（欠落カバー）
    '/contact-info', '/contactinfo',
    '/contact-form', '/contactform',
    '/お問い合わせ', '/問い合わせ',
    '/page/contact', '/content/contact',
]

_COMPANY_LABEL_PATTERNS = [
    # ラベル: 値 が同一行（半角・全角コロン対応）
    r'(?:販売業者|運営会社|事業者名|法人名|会社名|商号|企業名|提供者|サービス提供者|事業者|販売者|運営事業者|運営主体|運営元|提供会社|サービス提供会社|屋号|開業者|申込先|運営責任者|施設名|組織名|法人名称|ショップ名|店舗名|出店者名|医療機関名|診療所名|事業所名|販売会社|代理店|主催者|主催会社|代理店名|申込先会社|注文先|受注者|法人の名称|ストア名|運営者|発行者|管理者|相手方|事業者等|取扱業者|取扱事業者|サービス提供元|サービス責任者)\s*[：:\uff1a]\s*([^\n：:]{2,80})',
    # ラベルの次の行に値（テーブル形式）
    r'(?:販売業者|運営会社|事業者名|法人名|会社名|商号|企業名|提供者|サービス提供者|事業者|販売者|運営事業者|運営主体|運営元|提供会社|サービス提供会社|屋号|開業者|申込先|運営責任者|施設名|組織名|法人名称|ショップ名|店舗名|出店者名|医療機関名|診療所名|事業所名|販売会社|代理店|主催者|主催会社|代理店名|申込先会社|注文先|受注者|法人の名称|ストア名|運営者|発行者|管理者|相手方|事業者等|取扱業者|取扱事業者|サービス提供元|サービス責任者)\s*[：:]?\s*\n\s*([^\n：:]{2,80})',
    # Copyright行からの抽出（慎重に使う）
    r'(?:Copyright|©)\s*(?:\d{4}[年\-\s]*)?(?:\d{4}[年\-\s]*)?\s*([^\n,，\.]{4,50}?)(?:\s+All\s+Rights|\s*$)',
]

_COMPANY_TABLE_LABELS = {
    '販売業者', '運営会社', '事業者名', '法人名', '会社名', '商号', '企業名',
    '提供者', 'サービス提供者', '運営者', '発行者', '管理者',
    '事業者', '販売者', '運営事業者', '運営主体', '運営元',
    '提供会社', 'サービス提供会社',
    # 追加: 個人事業主・屋号・その他表記
    '屋号', '開業者', '申込先', '相手方', '事業者等',
    '運営責任者', 'オーナー', '代表者名', '施設名',
    '組織名', '法人名称', '正式名称',
    # EC・店舗特商法
    'ショップ名', '店舗名', '出店者名', 'ストア名',
    # 医療・士業
    '医療機関名', '診療所名', '法人の名称', '事業所名',
    # 不動産・金融・イベント
    '販売会社', '代理店', '主催者', '主催会社', '代理店名',
    # 特商法追加表記
    '申込先会社', '注文先', '受注者',
    # 金融・通信・SaaS特商法用
    '取扱業者', '取扱事業者', 'サービス提供元', 'サービス責任者',
}

_PHONE_TABLE_LABELS = {
    'TEL', 'Tel', 'tel', '電話', 'お電話', '電話番号', '連絡先',
    'お問い合わせ先', '代表電話', '代表番号', '問い合わせ電話',
    'お電話番号', '電話（代表）', 'お電話でのお問い合わせ',
    # 追加: カバレッジ拡張
    '電話番号（代表）', 'フリーダイヤル', '電話でのお問い合わせ',
    '問い合わせ電話番号', '電話受付', 'お電話受付', 'ご連絡先',
    'Phone', 'phone', 'PHONE', 'Telephone', 'telephone',
    '電話番号・FAX', '電話・FAX',
    # 携帯・直通
    '携帯', '携帯電話', '携帯番号', '直通', '直通電話', '直通番号',
    # EC・特商法用
    '返品・交換の連絡先', '問い合わせ電話', '購入・問い合わせ',
}

_PHONE_LABEL_PATTERNS = [
    r'(?:TEL|Tel|電話|お電話|電話番号|連絡先|フリーダイヤル)\s*[：:\s]\s*([\d\-\(\)\s０-９]{7,20})',
]

# 担当者名抽出パターン（ラベル + 日本語氏名）
# 漢字 + ひらがな + カタカナ すべてを氏名文字として許可
# 例: 田中太郎 / 田中ゆかり / 佐藤ケンジ / さとう花子
_JP_CHAR = r'[一-鿿ぁ-んァ-ン]'
_JP_NAME_PAT = (
    r'(' + _JP_CHAR + r'{1,5}[\s　]?' + _JP_CHAR + r'{1,5}'
    r'|' + _JP_CHAR + r'{2,8})'
)
_CONTACT_NAME_LABEL_RE = re.compile(
    r'(?:院長|所長|代表取締役|代表者|代表|理事長|会長|塾長|オーナー|先生|鍼灸師|施術者|カウンセラー|獣医師)'
    r'\s*[：:・\s]\s*' + _JP_NAME_PAT
)
# 後置ラベル: 「田中太郎 院長」
_CONTACT_NAME_SUFFIX_RE = re.compile(
    _JP_NAME_PAT +
    r'\s*(?:院長|所長|代表|理事長|オーナー|先生)'
)


def _extract_contact_name(soup: BeautifulSoup) -> Optional[str]:
    """HPから担当者名（院長・代表・所長等）を抽出する。"""
    text = soup.get_text(separator='\n', strip=True)
    # ラベル前置き: 「院長：田中太郎」
    m = _CONTACT_NAME_LABEL_RE.search(text)
    if m:
        name = m.group(1).replace('　', '').replace(' ', '').strip()
        if 2 <= len(name) <= 8:
            return name
    # ラベル後置き: 「田中太郎 院長」
    m = _CONTACT_NAME_SUFFIX_RE.search(text)
    if m:
        name = m.group(1).replace('　', '').replace(' ', '').strip()
        if 2 <= len(name) <= 8:
            return name
    return None


def _extract_lp_headline(soup: BeautifulSoup) -> Optional[str]:
    """LPのキャッチコピー・見出しを抽出する（H1 → og:title → title → og:description）。"""
    # H1（最優先: LPキャッチコピーが入ることが多い）
    h1 = soup.find('h1')
    if h1:
        text = h1.get_text(separator=' ', strip=True)
        text = re.sub(r'\s+', ' ', text).strip()
        if 2 <= len(text) <= 80:
            return text
        # 80文字超でも先頭80文字を切り出して使う（タイトル候補として）
        if len(text) > 80:
            return text[:80].rstrip()
    # og:title（OGP title、ページタイトルより詳細なキャッチコピーが入ることがある）
    og_title = soup.find('meta', attrs={'property': 'og:title'})
    if og_title:
        text = (og_title.get('content') or '').strip()
        for sep in ['|', '｜', ' - ', '–', '—']:
            if sep in text:
                text = text.split(sep)[0].strip()
                break
        text = re.sub(r'\s+', ' ', text).strip()
        if 2 <= len(text) <= 80:
            return text
    # <title>
    title = soup.find('title')
    if title:
        text = title.get_text(strip=True)
        # 「◯◯ | 会社名」形式の場合、先頭部分だけ取る
        for sep in ['|', '｜', ' - ', '–', '—']:
            if sep in text:
                text = text.split(sep)[0].strip()
                break
        text = re.sub(r'\s+', ' ', text).strip()
        if 2 <= len(text) <= 80:
            return text
    # og:description（最終フォールバック）
    og_desc = soup.find('meta', attrs={'property': 'og:description'})
    if og_desc:
        text = (og_desc.get('content') or '').strip()
        if 2 <= len(text) <= 80:
            return text
    return None


def _fetch(url: str, timeout: int = 15, max_retries: int = 3) -> Optional[BeautifulSoup]:
    """URLをフェッチして BeautifulSoup を返す。リトライロジック+エンコーディング改善付き。
    タイムアウト15s・リトライ3回: 特商法ページの遅いサーバーに対応。
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200:
                # エンコーディング検出: 明示的なcharset > apparent_encoding > utf-8
                if 'charset=' in resp.headers.get('content-type', '').lower():
                    resp.encoding = resp.encoding
                else:
                    resp.encoding = resp.apparent_encoding or 'utf-8'
                
                try:
                    soup = BeautifulSoup(resp.text, 'lxml')
                    # ルビ（振り仮名）タグを除去: <rt>（読み仮名）<rp>（括弧）が
                    # get_text() に混入して会社名抽出を妨げるため先に除去する
                    for tag in soup.find_all(['rt', 'rp']):
                        tag.decompose()
                    return soup
                except Exception as parse_error:
                    # lxml失敗時は html.parser にフォールバック
                    try:
                        soup = BeautifulSoup(resp.text, 'html.parser')
                        for tag in soup.find_all(['rt', 'rp']):
                            tag.decompose()
                        return soup
                    except Exception:
                        last_error = f'Parse failed: {type(parse_error).__name__}'
                        if attempt < max_retries - 1:
                            time.sleep(1.0 * (attempt + 1))
            elif resp.status_code == 404:
                # 404 は確定的な失敗、リトライしない
                return None
            else:
                # その他のエラーステータスはリトライ
                last_error = f'HTTP {resp.status_code}'
                if attempt < max_retries - 1:
                    time.sleep(1.0 * (attempt + 1))
        except requests.Timeout:
            last_error = 'Timeout'
            if attempt < max_retries - 1:
                time.sleep(1.0 * (attempt + 1))
        except requests.ConnectionError:
            last_error = 'ConnectionError'
            if attempt < max_retries - 1:
                time.sleep(1.0 * (attempt + 1))
        except Exception as e:
            last_error = str(type(e).__name__)
            if attempt < max_retries - 1:
                time.sleep(1.0 * (attempt + 1))
    
    return None


_LEGAL_ENTITY_RE = re.compile(
    r'(?:株式会社|有限会社|合同会社|合資会社|合名会社|一般社団法人|公益社団法人|'
    r'一般財団法人|公益財団法人|医療法人|学校法人|弁護士法人|税理士法人|司法書士法人|'
    r'行政書士法人|社会保険労務士法人|弁理士法人|監査法人|土地家屋調査士法人|'
    r'社会福祉法人|NPO法人|特定非営利活動法人|協同組合|農業協同組合|'
    r'国立大学法人|公立大学法人|独立行政法人|地方独立行政法人|'
    r'Inc\.?|Corp\.?|Ltd\.?|LLC|GmbH|ホールディングス)'
)

# ラベル付きコンテキスト専用: 法人格なしでも業種サフィックスで事業者と判定
# （tokutei ページの「事業者名: ○○クリニック」など）
_BUSINESS_TYPE_RE = re.compile(
    r'(?:クリニック|歯科|医院|病院|診療所|整形外科|皮膚科|眼科|内科|外科|耳鼻科|小児科|'
    r'薬局|調剤薬局|'
    r'法律事務所|弁護士事務所|司法書士事務所|行政書士事務所|税理士事務所|'
    r'公認会計士事務所|社会保険労務士事務所|弁理士事務所|'
    r'不動産|仲介|買取|リフォーム|建設|工務店|建築|設計|'
    r'保険代理店|ファイナンシャル|FP事務所|'
    r'塾|学院|スクール|教室|予備校|アカデミー|'
    r'サロン|エステ|整体院|整骨院|接骨院|鍼灸院|美容室|ヘアサロン|'
    r'ペットショップ|トリミングサロン|トリミング|動物病院|'
    r'葬儀社|葬祭|セレモニー|'
    r'保育所|保育園|幼稚園|'
    r'ホテル|旅館|民宿|ペンション|'
    r'飲食店|レストラン|居酒屋|カフェ|'
    r'商会|商店|ストア|ショップ|工房|スタジオ)'
)

# 業種サフィックスで終わる名称の後に続く住所・説明をトリムする（前株なし社名用）
# 例: "田中クリニック 大阪市○○" → "田中クリニック"
_BUSINESS_TYPE_TRIM_RE = re.compile(
    r'^(\S+(?:クリニック|歯科|医院|病院|診療所|法律事務所|弁護士事務所|司法書士事務所|'
    r'行政書士事務所|税理士事務所|公認会計士事務所|社会保険労務士事務所|'
    r'整体院|整骨院|接骨院|鍼灸院|薬局|調剤薬局|'
    r'動物病院|葬儀社|葬祭|工務店|建設|建築事務所|設計事務所|'
    r'保育園|幼稚園|保育所|'
    r'不動産|仲介センター|買取センター|リフォーム|'
    r'学院|塾|スクール|教室|予備校|'
    r'サロン|エステ|美容室|ヘアサロン|'
    r'ホテル|旅館|民宿|ペンション|'
    r'商会|商店|ストア|ショップ|'
    r'保険代理店|FP事務所|ファイナンシャル事務所|'
    r'ペットショップ|トリミングサロン|動物病院))'
)

# 前株パターンで始まる法人格プレフィックスのタプル（startswith用）
_LEGAL_ENTITY_PREFIXES = (
    '株式会社', '有限会社', '合同会社', '合資会社', '合名会社',
    '一般社団法人', '公益社団法人', '一般財団法人', '公益財団法人',
    '医療法人', '学校法人', '弁護士法人', '税理士法人', '司法書士法人',
    '行政書士法人', '社会保険労務士法人', '弁理士法人', '監査法人',
    '土地家屋調査士法人', '社会福祉法人', 'NPO法人', '特定非営利活動法人',
    '協同組合', '農業協同組合', '国立大学法人', '公立大学法人',
    '独立行政法人', '地方独立行政法人',
)

# スペース含み後株のトリム用: 英語法人格を除く（Corp/Inc等の部分マッチ誤トリム防止）
_JP_LEGAL_ENTITY_TRIM_RE = re.compile(
    r'(?:株式会社|有限会社|合同会社|合資会社|合名会社|一般社団法人|公益社団法人|'
    r'一般財団法人|公益財団法人|医療法人|学校法人|弁護士法人|税理士法人|司法書士法人|'
    r'行政書士法人|社会保険労務士法人|弁理士法人|監査法人|土地家屋調査士法人|'
    r'社会福祉法人|NPO法人|特定非営利活動法人|協同組合|農業協同組合|'
    r'国立大学法人|公立大学法人|独立行政法人|地方独立行政法人|ホールディングス)'
)

# 会社名の後に続く説明文・住所をトリムする（スペース区切りで最初のトークンのみ取る）
# 前株: 「有限会社○○ 説明文」→「有限会社○○」
_MAEKAB_TRIM_RE = re.compile(
    r'^((?:株式会社|有限会社|合同会社|合資会社|合名会社|一般社団法人|公益社団法人|'
    r'一般財団法人|公益財団法人|医療法人|学校法人|弁護士法人|税理士法人|司法書士法人|'
    r'行政書士法人|社会保険労務士法人|弁理士法人|監査法人|土地家屋調査士法人|'
    r'社会福祉法人|NPO法人|特定非営利活動法人|協同組合|農業協同組合|'
    r'国立大学法人|公立大学法人|独立行政法人|地方独立行政法人)\S+)'
)
# 後株: 「○○株式会社 説明文」→「○○株式会社」
_ATOKAB_TRIM_RE = re.compile(
    r'^(\S+(?:株式会社|有限会社|合同会社|合資会社|合名会社|一般社団法人|公益社団法人|'
    r'一般財団法人|公益財団法人|医療法人|学校法人|弁護士法人|税理士法人|司法書士法人|'
    r'行政書士法人|社会保険労務士法人|弁理士法人|監査法人|土地家屋調査士法人|'
    r'社会福祉法人|NPO法人|特定非営利活動法人|協同組合|農業協同組合|'
    r'国立大学法人|公立大学法人|独立行政法人|地方独立行政法人|ホールディングス))'
)
# カッコ内の会社名を抽出: 「サンテク(株式会社山陽テクノサービス)」→「株式会社山陽テクノサービス」
_PAREN_COMPANY_RE = re.compile(
    r'[（(]('
    r'(?:株式会社|有限会社|合同会社|合資会社|合名会社|一般社団法人|公益社団法人|'
    r'一般財団法人|公益財団法人|医療法人|学校法人|弁護士法人|税理士法人|司法書士法人|'
    r'行政書士法人|社会保険労務士法人|弁理士法人|監査法人|土地家屋調査士法人|'
    r'社会福祉法人|NPO法人|特定非営利活動法人|協同組合|農業協同組合|'
    r'国立大学法人|公立大学法人|独立行政法人|地方独立行政法人)'
    r'[^）)]+)'
    r'[）)]'
)
# 前置き説明を除去: 「内装工事の株式会社○○」「説明 株式会社○○」→「株式会社○○」
_LEADING_DESC_RE = re.compile(
    r'^.+?(?:の|[ 　])('
    r'(?:株式会社|有限会社|合同会社|合資会社|合名会社|一般社団法人|公益社団法人|'
    r'一般財団法人|公益財団法人|医療法人|学校法人|弁護士法人|税理士法人|司法書士法人|'
    r'行政書士法人|社会保険労務士法人|弁理士法人|監査法人|土地家屋調査士法人|'
    r'社会福祉法人|NPO法人|特定非営利活動法人|協同組合|農業協同組合|'
    r'国立大学法人|公立大学法人|独立行政法人|地方独立行政法人)'
    r'\S+)'
)

# 末尾の役職タイトル除去（会社名に直結またはスペース区切りで付いている場合）
# 例: "株式会社ハツカ代表取締役CEO" → "株式会社ハツカ"
# 例: "合同会社○○ 取締役社長" → "合同会社○○"
_ROLE_TITLE_SUFFIX_RE = re.compile(
    r'[\s　]*(?:代表取締役|取締役|代表|会長|社長|副社長|専務|常務|理事長|院長|所長|'
    r'オーナー|主宰|塾長|校長|園長|施設長|事業主|センター長|執行役員|最高経営責任者)'
    r'(?:\s*(?:CEO|COO|CFO|CTO|CMO|CCO|社長|会長|副社長|専務|常務))?'
    r'\s*$'
    r'|[\s　]+(?:CEO|COO|CFO|CTO|CMO|CCO)(?:\s*/\s*(?:CEO|COO|CFO|CTO|CMO|CCO))*\s*$'
)

# 法人格略称 → 正式表記マッピング
_CORP_ABBREV = [
    ('㈱',     '株式会社'),
    ('（株）', '株式会社'),
    ('(株)',   '株式会社'),
    ('㈲',     '有限会社'),
    ('（有）', '有限会社'),
    ('(有)',   '有限会社'),
    ('（合）', '合同会社'),
    ('(合)',   '合同会社'),
]


def _expand_corp_abbrev(name: str) -> str:
    """略称法人格（㈱ / (株) 等）を正式形に展開し、前株を後株に統一する。"""
    for abbrev, full in _CORP_ABBREV:
        if abbrev not in name:
            continue
        if name.startswith(abbrev):
            # 前株: "㈱○○" → "株式会社○○"
            name = full + name[len(abbrev):].strip()
        else:
            # 後株・中間: "○○(株)" → "○○株式会社"
            name = name.replace(abbrev, full)
    return name.strip()

# ゴミ判定パターン（先頭一致）
_GARBAGE_RE = re.compile(
    r'^(?:'
    r'©[︎️]?|︎|️|\(C\)|Copyright|\d{4}|【|「|http|www\.'
    r'|All Rights|Privacy|Terms|Policy'
    r'|iPhone|Android|PC|お問い合わせ|ショッピング|カート|ログイン|メニュー'
    r'|企業情報|会社情報|サービス情報|運営情報|運営者情報'
    r'|本利用規約|本規約|利用規約|以下|募集代理店'
    r'|PR\s*[：:]|広告'
    r'|運営会社[\/／]|販売業者[：:]?|事業者名[：:]?|商号[：:を]|屋号'
    r'|例[：:＊\s]|例えば[：:＊\s]|例）|例示|サンプル[：:\s]|テスト[：:\s]'
    # 今回のバッチで学んだゴミパターン
    r'|よくあるご質問|プライバシーポリシー|基本情報|価格プラン'
    r'|家業を継が|オーナー専用|令和\S*年'
    r'|ImageMagick|Google LLC'
    r'|販売業者名|事業者名称'
    r')',
    re.IGNORECASE
)

# 末尾に事業部・支店等が付いている → 正式社名ではない
_UNIT_SUFFIX_RE = re.compile(
    r'(?:事業部|営業所|出張所)$'
)

# 不可能な法人格の組み合わせ（行政書士法人+株式会社 など）
_IMPOSSIBLE_LEGAL_COMBO_RE = re.compile(
    r'(?:行政書士法人|司法書士法人|弁護士法人|税理士法人).{0,20}(?:株式会社|有限会社)|'
    r'(?:株式会社|有限会社).{0,20}(?:行政書士法人|司法書士法人)'
)

# 「株式会社」だけで終わる不完全な会社名を拒否
_INCOMPLETE_RE = re.compile(
    r'^(?:株式会社|有限会社|合同会社|合資会社|合名会社|医療法人|一般社団法人|公益社団法人|'
    r'一般財団法人|公益財団法人|学校法人|弁護士法人|税理士法人|司法書士法人|'
    r'行政書士法人|社会保険労務士法人|弁理士法人|監査法人|土地家屋調査士法人|'
    r'社会福祉法人|NPO法人|特定非営利活動法人|協同組合|農業協同組合|'
    r'国立大学法人|公立大学法人|独立行政法人|地方独立行政法人)$'
)


def _unescape_all_entities(text: str) -> str:
    """全HTMLエンティティ（名前・数字・16進）を確実に処理"""
    # Step 1: 名前付きエンティティ（&nbsp; など）
    text = _html.unescape(text)
    
    # Step 2: 10進数エンティティ（&#123; など）
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)
    
    # Step 3: 16進数エンティティ（&#x1f; など）
    text = re.sub(r'&#x([0-9a-fA-F]+);', lambda m: chr(int(m.group(1), 16)), text)
    
    # Step 4: 残存する不完全なエンティティを除去（セミコロン忘れ等）
    text = re.sub(r'&[a-zA-Z]{2,8}(?!;)', '', text)
    
    return text


def _is_japanese_company(name: str) -> bool:
    """日本語法人名（漢字・ひらがな・カタカナを含む）かどうか"""
    return bool(
        re.search(r'[぀-鿿＀-￯]', name)
        and _LEGAL_ENTITY_RE.search(name)
    )


def _is_english_only_company(name: str) -> bool:
    """法人格を除いた部分が英語/数字のみ（日本語文字を含まない）か"""
    stripped = re.sub(r'\b(?:Inc\.?|Corp\.?|Ltd\.?|LLC|GmbH|Holdings?)\b', '', name, flags=re.I).strip()
    return bool(stripped) and not re.search(r'[ぁ-んァ-ン一-鿿]', stripped)


def _normalize_name(name: str) -> str:
    """バリデーション前の正規化: HTMLエンティティ解除・空白統一・ゴミ除去"""
    name = _unescape_all_entities(name)
    # 著作権・登録商標記号＋バリエーションセレクタ（©︎ の U+FE0E / U+FE0F）を除去
    name = re.sub(r'[©℗®™℠][︎️]?', '', name)
    name = re.sub(r'[︎️]', '', name)  # 残存バリエーションセレクタ
    # ©YYYY / Copyright YYYY 形式で社名が後に続く場合 → 先頭の年号を除去
    # 例: "©2024 株式会社田中産業" → "株式会社田中産業"
    name = re.sub(r'^(?:Copyright\s*)?(?:\d{4}[-–—\s]*\d{4}|\d{4})\s+', '', name).strip()
    # 先頭の装飾記号を除去（LPで使われる ※ ▶ ◆ □ ■ ▲ ★ ● 等）
    # 注: 「 / 『 / 【 / （ は後続のペア括弧解除処理で扱うため含めない
    name = re.sub(r'^[※▶◆□■▲★●◎◇▼►◄➤➔→←↑↓\[\s]+', '', name).strip()
    # 先頭の【...】型プレフィックス注記を除去（後に続く会社名がある場合のみ）
    # 例: "【公式】株式会社田中産業" → "株式会社田中産業"
    # 例: "【PR】田中クリニック" → "田中クリニック"
    # 注: "【株式会社田中産業】" のように全体が括弧に囲まれている場合は除去しない（後続のペア括弧解除で処理）
    name = re.sub(r'^【[^】]{1,8}】\s*(?=\S)', '', name).strip()
    # 法人格略称を正式形に展開（先に行う: (有)/(株)/(合) をプレフィックス除去より前に処理）
    name = _expand_corp_abbrev(name)
    # (公式) / (PR) 等の半角括弧プレフィックス（後に内容がある場合のみ）
    # 注: 法人格略称 (株)/(有)/(合) は上の _expand_corp_abbrev で展開済み → 誤マッチ防止
    name = re.sub(r'^\([^)]{1,8}\)\s*(?=\S)', '', name).strip()
    name = re.sub(r'[　\s]+', ' ', name).strip()
    # 「」/ 『』/ 【】/ ""（全角ダブルクォート）等で囲まれた社名を解除
    # 例: 「株式会社田中産業」→ 株式会社田中産業
    # 例: 【株式会社田中産業】→ 株式会社田中産業
    name = re.sub(r'^[「『""「『【](.+)[」』""」』】]$', r'\1', name)
    # ダッシュ + ページ種別サフィックスを除去（末尾記号除去より先に処理）
    # 例: "株式会社オオクシ‐採用サイト" → "株式会社オオクシ"
    name = re.sub(
        r'\s*[\-‐–—－]\s*(?:採用サイト|採用情報|採用ページ|採用|求人情報|求人|'
        r'公式サイト|公式ページ|公式HP|公式|ホームページ|'
        r'お問い合わせ|問い合わせ)\s*$',
        '', name
    ).strip()
    # 末尾のハイフン・アンダースコア・スラッシュ・孤立括弧を除去
    name = re.sub(r'[\-‐–—－_/\\|｜・）)]+$', '', name).strip()
    # 末尾の日本語文字に続く孤立ピリオドを除去
    # 例: "株式会社タキオン." → "株式会社タキオン"（英字末尾 "U.S.J." は保持）
    name = re.sub(r'(?<=[ぁ-んァ-ン一-鿿])\.\s*$', '', name).strip()
    # 末尾の本社/支社/支店等の括弧付き注記を除去
    # 例: "株式会社田中産業（東京本社）" → "株式会社田中産業"
    name = re.sub(
        r'\s*[（(](?:本社|東京本社|大阪本社|名古屋本社|支社|支店|本店|営業所|分室|出張所)[）)]\s*$',
        '', name
    ).strip()
    # 末尾の旧社名・以下弊社等の補足情報を除去（スペース有無に関わらず適用）
    # 例: "株式会社田中産業（旧: 旧社名）" → "株式会社田中産業"
    # 例: "株式会社田中産業(以下、弊社)" → "株式会社田中産業"
    if re.search(r'[（(][旧以]', name):
        name = re.sub(r'\s*[（(][旧以][^）)]*[）)]?\s*$', '', name).strip()
    # 末尾の住所・所在地ラベルが直結している場合を除去（括弧なし）
    # 例: "株式会社小野写真館本社所在地" → "株式会社小野写真館"
    name = re.sub(r'(?:本社所在地|事業所所在地|所在地|本社住所)\s*$', '', name).strip()
    # 末尾の括弧内業態注記を繰り返し除去（法人格を含まない ≤12文字の補足）
    # 例: "株式会社田中（フランチャイズ加盟店）" → "株式会社田中"
    # 例: "田中建設株式会社（仮称）（東京支社）" → "田中建設株式会社"
    # ただし「（株式会社○○）」型は _PAREN_COMPANY_RE が後続処理するため除外
    for _ in range(3):  # 最大3回繰り返し
        m_trail = re.search(r'\s*[（(]([^（(）)]{1,12})[）)]\s*$', name)
        if m_trail and not _LEGAL_ENTITY_RE.search(m_trail.group(1)):
            name = name[:m_trail.start()].strip()
        else:
            break
    # カッコ内に会社名があれば抽出: 「サンテク(株式会社山陽テクノサービス)」→「株式会社山陽テクノサービス」
    m = _PAREN_COMPANY_RE.search(name)
    if m:
        name = m.group(1).strip()
    # 前置き説明を除去: 「内装工事の株式会社○○」「説明 株式会社○○」→「株式会社○○」
    # 前株で始まる場合は適用しない（前株名自体が「leading desc」として誤マッチする防止）
    if not name.startswith(_LEGAL_ENTITY_PREFIXES):
        m = _LEADING_DESC_RE.match(name)
        if m:
            name = m.group(1)
    # 後株: スペース区切り3トークン以上 + 末尾トークンが完全な後株法人名 → 前置きLP名を除去
    # 例: "無人内見 MUJIN24 物件一覧 セキスイハイム東海株式会社" → "セキスイハイム東海株式会社"
    if ' ' in name and not name.startswith(_LEGAL_ENTITY_PREFIXES):
        _parts = name.split(' ')
        if len(_parts) >= 3:
            _last = _parts[-1]
            _m_last = _ATOKAB_TRIM_RE.match(_last)
            if _m_last and _m_last.end() == len(_last):
                _prefix = ' '.join(_parts[:-1])
                if not _JP_LEGAL_ENTITY_TRIM_RE.search(_prefix):
                    name = _last
    # 会社名の後に続く説明文・住所・キャッチコピーを除去
    # 例: 「有限会社○○ 佐世保市・長崎のFP」→「有限会社○○」
    if ' ' in name:
        m = _MAEKAB_TRIM_RE.match(name)
        if m and m.end() < len(name):
            return m.group(1)
        m = _ATOKAB_TRIM_RE.match(name)
        if m and m.end() < len(name):
            return m.group(1)
        # スペース含み後株パターン: "SUN マルシェ株式会社 住所..." → "SUN マルシェ株式会社"
        # _ATOKAB_TRIM_RE は \S+ なのでスペース含み社名をカバーできない。
        # 英語法人格(Corp/Inc等)は部分マッチの誤トリム防止のため日本語法人格のみ対象。
        # 前株でない場合に限り、最後の日本語法人格位置でトリムする。
        if not name.startswith(_LEGAL_ENTITY_PREFIXES):
            m_list = list(_JP_LEGAL_ENTITY_TRIM_RE.finditer(name))
            if m_list:
                last = m_list[-1]
                trailing = name[last.end():].strip()
                # 後続テキストが社名の一部（業種サフィックス等の短い和語）ならトリムしない。
                # 例: "一級建築士事務所 株式会社 北条工務店" → trailing="北条工務店" (業種suffix) → 保持
                # 例: "SUN マルシェ株式会社 東京都渋谷区" → trailing="東京都渋谷区" (住所) → トリム
                _is_name_part = (
                    trailing
                    and len(trailing) <= 10
                    and not re.match(
                        r'^(?:東京|大阪|京都|神奈川|愛知|北海道|福岡|広島|宮城|'
                        r'兵庫|埼玉|千葉|静岡|茨城|栃木|群馬|岡山|長野|新潟|岐阜|三重|'
                        r'熊本|鹿児島|沖縄|滋賀|山口|愛媛|長崎|奈良|青森|岩手|秋田|'
                        r'山形|福島|富山|石川|福井|山梨|和歌山|徳島|香川|高知|佐賀|'
                        r'大分|宮崎|[東西南北]?\d|〒|\d)',
                        trailing
                    )
                    # 区・市・町・村・都・道・府・県で終わる = 住所
                    and not re.search(r'(?:市|区|町|村|都|道|府|県)$', trailing)
                    and (
                        _BUSINESS_TYPE_RE.search(trailing)
                        or (len(trailing) <= 5
                            and re.match(r'^[぀-鿿\w]+$', trailing))
                    )
                )
                if not _is_name_part:
                    candidate = name[:last.end()].strip()
                    if 3 < len(candidate) < len(name):
                        return candidate
    # 業種サフィックス付き名称（前株なし）の後続する住所・説明文をトリム
    # 例: "田中クリニック 大阪市○○区1-2" → "田中クリニック"
    if ' ' in name and not name.startswith(_LEGAL_ENTITY_PREFIXES):
        m = _BUSINESS_TYPE_TRIM_RE.match(name)
        if m and m.end() < len(name):
            return m.group(1)
    # 前株 + スペース + 社名 + 住所/説明 をトリム
    # 例: "株式会社 千成屋 〒123-4567 東京都..." → "株式会社 千成屋"
    # 例: "株式会社 SUN マルシェ 渋谷区1-1" → "株式会社 SUN マルシェ"
    _ADDR_PREFIXES = (
        r'^(?:東京|大阪|京都|神奈川|愛知|北海道|福岡|広島|宮城|兵庫|埼玉|千葉|'
        r'静岡|茨城|栃木|群馬|岡山|長野|新潟|岐阜|三重|熊本|鹿児島|沖縄|滋賀|'
        r'山口|愛媛|長崎|奈良|青森|岩手|秋田|山形|福島|富山|石川|福井|山梨|'
        r'和歌山|徳島|香川|高知|佐賀|大分|宮崎)'
    )

    def _is_address_part(token: str) -> bool:
        return bool(
            token.startswith('〒')
            or re.match(r'^[東西南北]?\d', token)
            or re.match(_ADDR_PREFIXES, token)
            or re.match(r'^\d+[-－]\d+', token)
            or re.search(r'(?:市|区|町|村|都|道|府|県)$', token)
            # 区/市に数字番地が続くパターン: 渋谷区1-1, 大阪市1-2-3
            or re.search(r'(?:市|区|町|村|都|道|府|県)\d', token)
            # 丁目/番地/番町: 中目黒1丁目, 丸の内1番地
            or re.search(r'(?:丁目|番地|番町)', token)
        )

    if name.startswith(_LEGAL_ENTITY_PREFIXES) and name.count(' ') >= 2:
        parts = name.split(' ')
        if len(parts) >= 3:
            # 3番目以降が住所・説明らしい場合はトリム
            third = parts[2]
            if _is_address_part(third):
                return ' '.join(parts[:2])
            # 末尾のパーツが住所の場合（4語以上）: 末尾から住所トークンを逐次除去
            if len(parts) >= 4 and _is_address_part(parts[-1]):
                trimmed = parts[:]
                while len(trimmed) > 2 and _is_address_part(trimmed[-1]):
                    trimmed = trimmed[:-1]
                if len(trimmed) < len(parts):
                    candidate = ' '.join(trimmed)
                    if 3 < len(candidate) < len(name):
                        return candidate
    # 末尾の役職タイトルを除去（スペースなし直結も対象）
    # 例: "株式会社ハツカ代表取締役CEO" → "株式会社ハツカ"
    if _LEGAL_ENTITY_RE.search(name) or name.startswith(_LEGAL_ENTITY_PREFIXES):
        m_role = _ROLE_TITLE_SUFFIX_RE.search(name)
        if m_role and m_role.start() > 0:
            candidate = name[:m_role.start()].strip()
            if len(candidate) >= 3:
                name = candidate
    return name


def _is_valid_company(name: str) -> bool:
    """厳格バリデーション: 法人形態の明記を必須とする（ラベルなしコンテキスト用）"""
    name = _normalize_name(name)
    if not name or len(name) < 4 or len(name) > 60:
        return False
    # セパレータ文字（ページタイトルやスローガンの特徴）
    if '|' in name or '｜' in name or '/' in name or '／' in name:
        return False
    if '･' in name:  # 半角中黒: 「A･B･C」型スローガンを排除
        return False
    if '。' in name or '、' in name or '（以下' in name or '（旧' in name or '(以下' in name or '(旧' in name:
        return False
    if re.search(r'（.{3,}）', name):
        return False
    # 株式会社が2回以上 = 会社設立サービスのページタイトル
    if name.count('株式会社') >= 2 or name.count('有限会社') >= 2:
        return False
    # 会社設立サービス・スローガン系
    if re.search(r'会社設立|起業(する|なら|を)', name):
        return False
    # 文節を示す助詞・動詞（文章であって社名でない）
    if re.search(r'[ぁ-ん]なら|なら[ぁ-ん]|として|のため|をお', name):
        return False
    # 所有格 + 名詞型スローガン「株式会社○○のサービス」「株式会社○○の事業」など
    # 法人格の後に「の」→名詞が続く場合はスローガン混入と判定
    if re.search(
        r'(?:株式会社|有限会社|合同会社|一般社団法人|医療法人|社会福祉法人).+の'
        r'(?:サービス|事業|商品|製品|一覧|ご案内|情報|詳細|特徴|ポイント|ご提供|ご紹介|実績|取り組み)',
        name
    ):
        return False
    if _GARBAGE_RE.search(name):
        return False
    if _INCOMPLETE_RE.match(name):
        return False
    if not _LEGAL_ENTITY_RE.search(name):
        return False
    # 不可能な法人格の組み合わせ（行政書士法人+株式会社 等）
    if _IMPOSSIBLE_LEGAL_COMBO_RE.search(name):
        return False
    # 末尾に事業部・支店が残っている → 正式名称ではない
    if _UNIT_SUFFIX_RE.search(name):
        return False
    return True


def _is_valid_company_labeled(name: str) -> bool:
    """ラベル直後用バリデーション: 法人形態必須"""
    name = _normalize_name(name)
    name_clean = re.sub(r'\s+', '', name)
    if not name_clean or len(name_clean) < 2 or len(name) > 80:
        return False
    if '|' in name or '｜' in name:
        return False
    if '･' in name:
        return False
    if '。' in name or '、' in name or '（以下' in name or '（旧' in name or '(以下' in name or '(旧' in name:
        return False
    if re.search(r'（.{3,}）', name):
        return False
    if name.count('株式会社') >= 2 or name.count('有限会社') >= 2:
        return False
    if re.search(r'会社設立|起業(する|なら|を)', name):
        return False
    if _GARBAGE_RE.search(name):
        return False
    if _INCOMPLETE_RE.match(name):
        return False
    if _IMPOSSIBLE_LEGAL_COMBO_RE.search(name):
        return False
    if _UNIT_SUFFIX_RE.search(name):
        return False
    if re.match(r'^[\d\s\-\(\)\+]+$', name):
        return False
    if re.search(r'https?://', name):
        return False
    # 法人格 or 業種サフィックスのいずれかが必要
    # （ラベル付き=事業者名欄なので、クリニック・事務所等もOK）
    if not _LEGAL_ENTITY_RE.search(name) and not _BUSINESS_TYPE_RE.search(name):
        return False
    # 業種語のみで法人格なし: 名前部分が必要（"不動産" / "歯科" 単体は拒否）
    if not _LEGAL_ENTITY_RE.search(name):
        # 業種語以外の有効な文字（漢字・かな・英数）が3文字以上必要
        type_stripped = _BUSINESS_TYPE_RE.sub('', name_clean)
        if len(type_stripped) < 2:
            return False
    # 文節助詞・動詞: 会社名ではなく説明文
    if re.search(r'[ぁ-ん]の|のため|として|について|に関する|をお|にて|より', name):
        return False
    # 所有格 + サービス一覧等（スローガン混入: ラベル付きでも拒否）
    if re.search(
        r'(?:株式会社|有限会社|合同会社|一般社団法人|医療法人|社会福祉法人).+の'
        r'(?:サービス|事業|商品|製品|一覧|ご案内|情報|詳細|特徴|ポイント|ご提供|ご紹介|実績|取り組み)',
        name
    ):
        return False
    return True


def _extract_from_json_ld(soup: BeautifulSoup) -> tuple[Optional[str], Optional[str]]:
    """JSON-LD 構造化データから会社名・電話番号を抽出する（SPA対策）。
    @graph 構造にも対応。"""
    company = None
    phone = None
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string or '')
            items = data if isinstance(data, list) else [data]

            # @graph 展開
            expanded: list = []
            for item in items:
                if isinstance(item, dict) and '@graph' in item:
                    graph = item['@graph']
                    if isinstance(graph, list):
                        expanded.extend(graph)
                    else:
                        expanded.append(item)
                else:
                    expanded.append(item)
            items = expanded

            # Organization型として扱うtypeの集合（医療・法律・動物病院等を含む）
            # Schema.org の LocalBusiness サブタイプを網羅的に追加（日本で多いクリニック・士業・美容等）
            _ORG_TYPES = (
                'Organization', 'Corporation', 'LocalBusiness', 'Company',
                # 医療系
                'MedicalOrganization', 'MedicalBusiness', 'MedicalClinic',
                'Dentist', 'Physician', 'Hospital', 'Pharmacy',
                'PhysicalTherapy', 'Optician',
                # 法務・士業
                'LegalService', 'Attorney', 'Notary',
                # 動物・ペット
                'VeterinaryCare', 'PetStore',
                # 美容・健康
                'HealthAndBeautyBusiness', 'BeautySalon', 'HairSalon',
                'NailSalon', 'DaySpa', 'BodyCare',
                # 金融・保険
                'FinancialService', 'AccountingService', 'InsuranceAgency',
                # 不動産・建設
                'RealEstateAgent', 'HomeAndConstructionBusiness',
                'ConstructionContractor', 'GeneralContractor', 'HVACBusiness',
                'HousePainter', 'Plumber', 'RoofingContractor',
                # 自動車
                'AutoDealer', 'AutoRepair', 'AutoBodyShop',
                # 宿泊
                'LodgingBusiness', 'Hotel', 'Motel', 'BedAndBreakfast',
                # 飲食
                'FoodEstablishment', 'Restaurant', 'CafeOrCoffeeShop',
                # 教育
                'EducationalOrganization', 'School', 'CollegeOrUniversity',
                # 旅行・葬祭・保育
                'TravelAgency', 'FuneralHome', 'ChildCare',
                # 専門サービス
                'ProfessionalService',
            )

            def _extract_org_name_phone(obj: dict) -> tuple[Optional[str], Optional[str]]:
                """organizationオブジェクトから (name, phone) を抽出するヘルパー"""
                # name / legalName がリスト（多言語JSON-LD）の場合、最初の要素を使用
                name_raw = obj.get('legalName', '') or obj.get('name', '')
                if isinstance(name_raw, list):
                    name_raw = next((n for n in name_raw if n), '')
                name = str(name_raw).strip() if name_raw else ''
                cname = None
                if name and (_is_valid_company(name) or _is_valid_company_labeled(name)):
                    cname = name
                # telephone がリスト（複数番号）の場合、最初の有効な番号を使用
                tel_raw = obj.get('telephone', '')
                cphone = None
                if isinstance(tel_raw, list):
                    for t in tel_raw:
                        if t:
                            cphone = extract_phone(str(t))
                            if cphone:
                                break
                elif tel_raw:
                    cphone = extract_phone(str(tel_raw))
                if not cphone:
                    cp = obj.get('contactPoint')
                    if isinstance(cp, dict):
                        cp_tel = cp.get('telephone', '')
                        if cp_tel:
                            cphone = extract_phone(str(cp_tel))
                    elif isinstance(cp, list):
                        for cp_item in cp:
                            if isinstance(cp_item, dict):
                                cp_tel = cp_item.get('telephone', '')
                                if cp_tel:
                                    cphone = extract_phone(str(cp_tel))
                                    if cphone:
                                        break
                return cname, cphone

            for item in items:
                if not isinstance(item, dict):
                    continue
                type_val = item.get('@type', '')
                if isinstance(type_val, list):
                    type_val = ' '.join(type_val)
                if any(t in type_val for t in _ORG_TYPES):
                    _c, _p = _extract_org_name_phone(item)
                    if not company and _c:
                        company = _c
                    if not phone and _p:
                        phone = _p
                    # parentOrganization / foundingOrganization のネスト探索
                    # 例: LocalBusiness(店舗) → parentOrganization(法人) → 法人名・電話
                    if not company or not phone:
                        for nested_key in ('parentOrganization', 'foundingOrganization', 'memberOf'):
                            nested = item.get(nested_key)
                            if not isinstance(nested, dict):
                                continue
                            n_type = str(nested.get('@type', ''))
                            if any(t in n_type for t in _ORG_TYPES):
                                _nc, _np = _extract_org_name_phone(nested)
                                if not company and _nc:
                                    company = _nc
                                if not phone and _np:
                                    phone = _np
                # 非Organization型: publisher / provider / creator にOrganizationがネストしている場合
                # 例: {"@type": "WebPage", "publisher": {"@type": "Organization", ...}}
                elif not company or not phone:
                    for nested_key in ('publisher', 'provider', 'creator', 'author'):
                        nested = item.get(nested_key)
                        if not isinstance(nested, dict):
                            continue
                        n_type = str(nested.get('@type', ''))
                        if any(t in n_type for t in _ORG_TYPES):
                            _nc, _np = _extract_org_name_phone(nested)
                            if not company and _nc:
                                company = _nc
                            if not phone and _np:
                                phone = _np
        except Exception:
            continue
    return company, phone


def _extract_from_meta(soup: BeautifulSoup) -> Optional[str]:
    """og:site_name / application-name メタタグから会社名候補を取得する。"""
    for attrs in [
        {'property': 'og:site_name'},
        {'name': 'application-name'},
        {'name': 'author'},
    ]:
        tag = soup.find('meta', attrs=attrs)
        if not tag:
            continue
        content = (tag.get('content') or '').strip()
        if not content:
            continue
        # セパレータがある場合は各パーツを試す
        for sep in ['|', '｜', ' - ', '–', '—', ' / ', '／']:
            if sep in content:
                for part in content.split(sep):
                    part = part.strip()
                    if _is_valid_company(part):
                        return _normalize_name(part)
                    # site_name は構造化メタデータとして信頼度高い
                    if attrs.get('property') == 'og:site_name' and _is_valid_company_labeled(part):
                        return _normalize_name(part)
                break
        if _is_valid_company(content):
            return _normalize_name(content)
        # site_name はラベルなしだが構造化メタデータとして信頼度が高い
        if attrs.get('property') == 'og:site_name' and _is_valid_company_labeled(content):
            return _normalize_name(content)
    return None


def _extract_company_from_table(soup: BeautifulSoup) -> Optional[str]:
    """th/dt → td/dd のテーブル構造から会社名を取得する。
    複数のHTML構造パターンに対応:
      1. <th>ラベル</th><td>値</td>（同一tr内の兄弟）
      2. <tr><th>ラベル</th></tr><tr><td>値</td></tr>（次行）
      3. <dt>ラベル</dt><dd>値</dd>（定義リスト）
      4. <td>ラベル</td><td>値</td>（thなしの2列テーブル）
    """
    # th/dt を最優先で確認してから td-as-label も試す
    for label_tag in soup.find_all(['th', 'dt', 'td']):
        label_text = label_tag.get_text(strip=True)
        if not any(kw in label_text for kw in _COMPANY_TABLE_LABELS):
            continue
        if len(label_text) > 30:
            continue
        # td-as-label: コロン後に長い値が続く場合（インライン label:value）は
        # _extract_company_from_divs の _INLINE_LABEL_COMPANY_RE に任せてスキップ
        # 会社名そのものが入っている td はラベルではなく値なので除外
        if label_tag.name == 'td':
            if re.search(r'[：:]\s*\S{3,}', label_text):
                continue
            if _is_valid_company(label_text) or _is_valid_company_labeled(label_text):
                continue

        value_tag = None

        # パターン1: 直接の次兄弟 (th → td, dt → dd)
        value_tag = label_tag.find_next_sibling(['td', 'dd'])

        if not value_tag:
            # パターン2: 同一tr内で後続のtdを探す
            parent = label_tag.parent
            if parent and parent.name == 'tr':
                tds = parent.find_all('td')
                if tds:
                    value_tag = tds[0]

        if not value_tag:
            # パターン3: 次のtr行のtd（同一親 or thead→tbody 境界を越えて探す）
            parent_tr = label_tag.find_parent('tr')
            if parent_tr:
                next_tr = parent_tr.find_next_sibling('tr')
                if not next_tr:
                    # thead/tbody 分離の場合: find_next('tr') でtbody側の最初のtrを取得
                    next_tr = parent_tr.find_next('tr')
                if next_tr:
                    value_tag = next_tr.find(['td', 'dd'])

        if value_tag:
            candidate = value_tag.get_text(separator=' ', strip=True)
            if _is_valid_company(candidate):
                return candidate
            if _is_valid_company_labeled(candidate):
                return candidate

    return None


_INLINE_LABEL_COMPANY_RE = re.compile(
    r'(?:販売業者|運営会社|事業者名|法人名|会社名|商号|企業名|提供者|'
    r'サービス提供者|事業者|販売者|運営事業者|運営主体|運営元|提供会社|'
    r'サービス提供会社|屋号|開業者|申込先|運営責任者|施設名|組織名|'
    r'法人名称|法人の名称|ショップ名|店舗名|ストア名|出店者名|'
    r'医療機関名|診療所名|事業所名|'
    r'販売会社|代理店|主催者|主催会社|代理店名|申込先会社|注文先|受注者|'
    r'運営者|発行者|管理者|相手方|事業者等|'
    r'取扱業者|取扱事業者|サービス提供元|サービス責任者)'
    r'\s*[：:：]\s*(.{2,60})'
)


def _extract_company_from_divs(soup: BeautifulSoup) -> Optional[str]:
    """div/p/span のラベル-値ペア構造から会社名を抽出する。
    CSS flexbox/gridで作られた疑似テーブルに対応。"""
    for label_tag in soup.find_all(['div', 'p', 'span', 'li']):
        label_text = label_tag.get_text(strip=True)
        if not any(kw in label_text for kw in _COMPANY_TABLE_LABELS):
            continue
        # アンカーリンクを含む要素 or アンカーの直接子要素はナビゲーション項目として除外
        if label_tag.find('a'):
            continue
        if label_tag.parent and label_tag.parent.name == 'a':
            continue

        # パターン0: ラベルと値が同一要素内のコロン区切り（例: <li>販売業者: 株式会社○○</li>）
        # 長いテキストを持つ要素にも対応（80文字上限）
        if len(label_text) <= 80:
            for m0 in _INLINE_LABEL_COMPANY_RE.finditer(label_text):
                candidate = _normalize_name(m0.group(1).strip())
                if _is_valid_company(candidate) or _is_valid_company_labeled(candidate):
                    return candidate

        # 30文字以内のラベル要素を処理（パターン1/2 は純粋ラベル要素のみ）
        if len(label_text) > 30:
            continue

        # パターン1: 次の兄弟要素が値
        next_tag = label_tag.find_next_sibling()
        if next_tag:
            candidate = next_tag.get_text(separator=' ', strip=True)
            if _is_valid_company_labeled(candidate):
                return candidate

        # パターン2: 親要素のテキストをラベル/値に分割
        parent = label_tag.parent
        if parent:
            parent_text = parent.get_text(separator='\n', strip=True)
            lines = [ln.strip() for ln in parent_text.split('\n') if ln.strip()]
            for i, line in enumerate(lines):
                if any(kw in line for kw in _COMPANY_TABLE_LABELS) and i + 1 < len(lines):
                    candidate = lines[i + 1]
                    if _is_valid_company_labeled(candidate):
                        return candidate

    return None


def _extract_phone_from_element(el) -> Optional[str]:
    """指定要素内から電話番号を抽出する（同一コンテナスコープ用）。
    tel:リンク → ラベル付きth/dt/td → テキスト全体の順で探す。"""
    for a in el.find_all('a', href=True):
        href = a.get('href', '')
        if href.lower().startswith('tel:'):
            p = extract_phone(href)
            if p:
                return p
    # th/dt/td をラベルとして探す（tdは純粋なラベルのみ: 短くて電話番号を含まない）
    for label_tag in el.find_all(['th', 'dt', 'td']):
        label_text = label_tag.get_text(strip=True)
        if not any(kw in label_text for kw in _PHONE_TABLE_LABELS):
            continue
        if len(label_text) > 30:
            continue
        # td-as-label: 電話番号が既に含まれている場合は値セルなのでスキップ
        if label_tag.name == 'td' and extract_phone(label_text):
            continue
        value_tag = label_tag.find_next_sibling(['td', 'dd'])
        if not value_tag:
            parent = label_tag.parent
            if parent and parent.name == 'tr':
                tds = parent.find_all('td')
                if tds:
                    value_tag = tds[0]
        if value_tag:
            p = extract_phone(value_tag.get_text(separator=' ', strip=True))
            if p:
                return p
    return extract_phone(el.get_text(separator='\n', strip=True))


def _verify_phone_on_domain(origin: str, phone: str) -> bool:
    """SERPフォールバックで取得した電話番号が会社ドメイン上に実在するか確認する。
    サブドメインの場合は親ドメインも確認する（info.toreta.in → toreta.in）。"""
    digits = re.sub(r'\D', '', phone)
    if not digits:
        return False
    # 確認対象: origin + 必要なら親ドメイン
    origins: list[str] = [origin]
    try:
        netloc = urlparse(origin).netloc
        parts = netloc.split('.')
        if len(parts) > 2:
            parent_netloc = '.'.join(parts[-2:])
            parent_origin = f'{urlparse(origin).scheme}://{parent_netloc}'
            if parent_origin != origin:
                origins.append(parent_origin)
    except Exception:
        pass
    # 電話番号が最も出現しやすい3パスのみ確認（逐次なので件数を絞りタイムアウトも短縮）
    _CHECK_PATHS = ('', '/contact', '/tokutei', '/company')
    for base in origins:
        for path in _CHECK_PATHS:
            try:
                r = requests.get(base + path, headers=_HEADERS, timeout=3, allow_redirects=True)
                if r.status_code == 200 and digits in re.sub(r'\D', '', r.text):
                    return True
            except Exception:
                continue
    return False


def _has_company_label(el) -> bool:
    """要素内に会社ラベルが存在するか（高速チェック用）"""
    text = el.get_text(separator=' ', strip=True)
    return any(kw in text for kw in _COMPANY_TABLE_LABELS)


def _has_phone_label(el) -> bool:
    """要素内に電話ラベルが存在するか（高速チェック用）"""
    text = el.get_text(separator=' ', strip=True)
    return any(kw in text for kw in _PHONE_TABLE_LABELS)


def _has_phone_number(el) -> bool:
    """要素内に電話番号パターンが存在するか（ラベルなし電話番号のフォールバック検出用）。
    TEL/電話番号ラベルがない素の数字だけの番号を含む要素を対象に含めるために使用する。"""
    text = el.get_text(separator=' ', strip=True)
    return bool(re.search(r'0\d[\d\-]{6,12}\d', text))


def _extract_pair_from_containers(soup: BeautifulSoup) -> tuple[Optional[str], Optional[str]]:
    """同一コンテナ内で会社名・電話番号をペア抽出する。
    table/dl → section/article → div（両ラベルを含む最小ブロック）の順で探す。
    両方が同一コンテナ内に見つかった場合のみ返す。"""
    # 1. table / dl（最も構造化されていて信頼度高）
    for container in [*soup.find_all('table'), *soup.find_all('dl')]:
        company = _extract_company_from_table(container) or _extract_company_from_divs(container)
        if not company:
            continue
        phone = _extract_phone_from_element(container)
        if phone:
            return _normalize_name(company), phone

    # 2. section / article / footer / main（特商法ブロックとして使われることが多い）
    for container in [
        *soup.find_all('section'),
        *soup.find_all('article'),
        *soup.find_all('footer'),
        *soup.find_all('main'),
    ]:
        if not _has_company_label(container):
            continue
        company = _extract_company_from_table(container) or _extract_company_from_divs(container)
        if not company:
            continue
        phone = _extract_phone_from_element(container)
        if phone:
            return _normalize_name(company), phone

    # 3. div（両ラベルを含む最小の div ブロックを探す）
    # テキスト長で昇順ソートし、最も具体的な（最小の）divを先に試す。
    # 最小 div = ラベル-値ペアが直接含まれる最内コンテナ → 誤ペア防止に有効。
    div_candidates = [
        d for d in soup.find_all('div')
        if _has_company_label(d) and (_has_phone_label(d) or _has_phone_number(d))
    ]
    div_candidates.sort(key=lambda d: len(d.get_text()))
    for div in div_candidates:
        company = _extract_company_from_table(div) or _extract_company_from_divs(div)
        if not company:
            continue
        phone = _extract_phone_from_element(div)
        if phone:
            return _normalize_name(company), phone

    return None, None


def _extract_company_from_spa_scripts(soup: BeautifulSoup) -> Optional[str]:
    """SPA初期状態変数（__NEXT_DATA__ / window.__INITIAL_STATE__ 等）から会社名を抽出。"""
    # 1. <script id="__NEXT_DATA__" type="application/json"> など
    for script in soup.find_all('script'):
        sid = str(script.get('id', '') or '')
        stype = str(script.get('type', '') or '')
        js = getattr(script, 'string', None) or ''
        if not js or len(js) > 500000:
            continue
        if '__NEXT_DATA__' in sid or ('application/json' in stype and sid):
            try:
                data = json.loads(js)
                _COMPANY_JSON_KEYS = frozenset({
                    'companyName', 'legalName', 'company_name', 'legal_name',
                    'organizationName', 'organization_name', 'corp_name', 'corpName',
                })

                def _find_in_json(obj: object, depth: int = 0) -> Optional[str]:
                    if depth > 8:
                        return None
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if k in _COMPANY_JSON_KEYS and isinstance(v, str) and v.strip():
                                cand = _normalize_name(v.strip())
                                if _is_valid_company(cand) or _is_valid_company_labeled(cand):
                                    return cand
                        for v in obj.values():
                            r = _find_in_json(v, depth + 1)
                            if r:
                                return r
                    elif isinstance(obj, list):
                        for item in obj[:30]:
                            r = _find_in_json(item, depth + 1)
                            if r:
                                return r
                    return None

                found = _find_in_json(data)
                if found:
                    return found
            except Exception:
                pass

    # 2. インラインJS: "companyName":"株式会社○○" / 'legalName':'...' 形式
    for script in soup.find_all('script'):
        js = getattr(script, 'string', None) or ''
        if not js or len(js) > 100000:
            continue
        for m in re.finditer(
            r'["\'](?:companyName|legalName|company_name|legal_name|'
            r'organizationName|organization_name|corpName|corp_name)["\']'
            r'\s*:\s*["\']([^"\']{3,60})["\']',
            js, re.IGNORECASE
        ):
            candidate = _normalize_name(m.group(1).strip())
            if _is_valid_company(candidate) or _is_valid_company_labeled(candidate):
                return candidate
    return None


def _extract_company_from_soup(soup: BeautifulSoup) -> Optional[str]:
    # JSON-LD（SPA含む静的埋め込み）を最優先
    company_ld, _ = _extract_from_json_ld(soup)
    if company_ld:
        return company_ld
    # Schema.org microdata: itemprop="name" or "legalName" on Organization type
    for el in soup.find_all(True, attrs={'itemtype': True}):
        if 'Organization' in str(el.get('itemtype', '')):
            for child in el.find_all(True, attrs={'itemprop': True}):
                prop = str(child.get('itemprop', '')).lower()
                if prop in ('legalname', 'name'):
                    val = str(child.get('content') or child.get_text(strip=True)).strip()
                    if _is_valid_company(val) or _is_valid_company_labeled(val):
                        return _normalize_name(val)
    # SPA初期状態変数: Next.js / Nuxt / Vue 等のデータストア
    spa_result = _extract_company_from_spa_scripts(soup)
    if spa_result:
        return spa_result
    # og:site_name / application-name（構造化メタデータとして高信頼）
    meta_result = _extract_from_meta(soup)
    if meta_result:
        return meta_result
    # og:title / <title> のセパレータパターン抽出
    # 例: "サービス名 | 株式会社○○" or "株式会社○○ - サービス名"
    for tag_src in [
        soup.find('meta', attrs={'property': 'og:title'}),
        soup.find('title'),
    ]:
        if not tag_src:
            continue
        raw = (tag_src.get('content') if tag_src.name == 'meta' else tag_src.get_text())
        raw = (raw or '').strip()
        if not raw:
            continue
        found_from_sep = False
        for sep in ['|', '｜', ' - ', '–', '—', ' / ', '／']:
            if sep in raw:
                found_from_sep = True
                for part in raw.split(sep):
                    part = part.strip()
                    if _is_valid_company(part):
                        return _normalize_name(part)
                break
        # セパレータなし: タイトル全体が社名の場合（"株式会社田中" 等）
        if not found_from_sep and len(raw) <= 50 and _is_valid_company(raw):
            return _normalize_name(raw)
    # h1 / h2 見出し（会社概要・特商法ページのページタイトルが法人名の場合）
    # <title> より優先度は低いが構造化されたページで有効。厳格バリデーションのみ適用。
    for heading_tag in soup.find_all(['h1', 'h2']):
        raw = heading_tag.get_text(strip=True)
        if not raw or len(raw) > 50:
            continue
        # セパレータで分割して各パーツを試す
        found_in_sep = False
        for sep in ['|', '｜', ' - ', '–', '—', ' / ', '／']:
            if sep in raw:
                found_in_sep = True
                for part in raw.split(sep):
                    part = part.strip()
                    if _is_valid_company(part):
                        return _normalize_name(part)
                break
        if not found_in_sep and _is_valid_company(raw):
            return _normalize_name(raw)

    # hCard vcard microformat: class="org" / class="organization"
    _ORG_CLASS_WORDS = frozenset({'org', 'organization', 'company', 'companyname', 'company-name'})
    for el in soup.find_all(True, class_=True):
        cls_words = set(
            ' '.join(el.get('class', [])).lower().replace('-', ' ').replace('_', ' ').split()
        )
        if not cls_words & _ORG_CLASS_WORDS:
            continue
        val = str(el.get('content') or el.get_text(strip=True)).strip()
        if val and len(val) <= 60:
            if _is_valid_company(val) or _is_valid_company_labeled(val):
                return _normalize_name(val)
    # テーブル構造（th/dt）
    result = _extract_company_from_table(soup)
    if result:
        return _normalize_name(result)
    # div/p/span のCSS疑似テーブル構造
    result = _extract_company_from_divs(soup)
    if result:
        return _normalize_name(result)
    # テキストパターンにフォールバック
    # 最初の1マッチではなく全マッチを試し、_is_valid_company（厳格）で通るものを優先する
    # 例: パターン0が「イベント会社名」でパターン1が「株式会社○○」の場合、後者を返す
    text = soup.get_text(separator='\n', strip=True)
    labeled_fallback: Optional[str] = None  # _is_valid_company_labeled のみ通った候補を保持
    for pattern in _COMPANY_LABEL_PATTERNS:
        is_copyright = 'Copyright' in pattern
        for m in re.finditer(pattern, text, re.MULTILINE):
            candidate = _normalize_name(m.group(1).strip())
            if not candidate:
                continue
            if _is_valid_company(candidate):
                return candidate
            if not is_copyright and not labeled_fallback and _is_valid_company_labeled(candidate):
                labeled_fallback = candidate
    # 厳格バリデーションで通るものがなければ、ラベル付き緩和バリデーション候補を返す
    return labeled_fallback


_TOKUTEI_COMPANY_LABELS = re.compile(
    r'販売業者|運営者|事業者名?|会社名|法人名|商号|屋号', re.IGNORECASE
)
_TOKUTEI_PHONE_LABELS = re.compile(
    r'(?:電話|TEL|Tel|tel)[\s番号：:]*', re.IGNORECASE
)


def _pick_phone_from_tokutei_section(lines: list[str], company_core: str, phones: list[str]) -> Optional[str]:
    """特商法形式テキストから、会社ラベル行の近傍にある電話ラベル行の番号を返す。
    会社名コアが会社ラベル行に含まれ、±8行以内に電話ラベル行があり、
    その行に含まれる番号が candidates に入っていれば採用する。"""
    candidate_digits = {re.sub(r'\D', '', p): p for p in phones if p}
    for i, line in enumerate(lines):
        if _TOKUTEI_COMPANY_LABELS.search(line) and company_core in line:
            # 会社ラベル行を基点に ±8 行以内で電話ラベル行を探す
            window_start = max(0, i - 8)
            window_end = min(len(lines), i + 9)
            for j in range(window_start, window_end):
                if j == i:
                    continue
                if _TOKUTEI_PHONE_LABELS.search(lines[j]):
                    found = extract_all_phones(lines[j])
                    for p in found:
                        digits = re.sub(r'\D', '', p)
                        if digits in candidate_digits:
                            return candidate_digits[digits]
    return None


def _pick_phone_nearest_company(text: str, company: str, phones: list[str]) -> Optional[str]:
    """複数電話番号から会社名テキスト位置に最も近いものを選ぶ。
    優先順:
      1. 特商法セクション（販売業者/運営者ラベル行 ±8行の電話ラベル）
      2. 直番（非フリーダイヤル）の近接スコア
      3. フリーダイヤルのみの場合は近接スコア
    電話番号が1件しかなければそのまま返す。
    """
    if not phones:
        return None
    if len(phones) == 1:
        return phones[0]

    # 直番 vs フリーダイヤルで分類
    direct = [p for p in phones if not is_freephone(p)]
    candidates = direct if direct else phones

    if len(candidates) == 1:
        return candidates[0]

    # 会社名コア（法人格を除く最初の10文字）
    company_core = re.sub(
        r'株式会社|有限会社|合同会社|一般社団法人|NPO法人|医療法人|社会福祉法人|'
        r'一般財団法人|公益財団法人|公益社団法人',
        '', company
    ).strip()[:10]
    if not company_core or len(company_core) < 2:
        return candidates[0]

    # ① 特商法セクション優先: 会社ラベル行 ±8行の電話ラベル行にある番号
    lines = text.splitlines()
    tokutei_phone = _pick_phone_from_tokutei_section(lines, company_core, candidates)
    if tokutei_phone:
        logging.debug(f'[PickPhone] tokutei_section: {tokutei_phone}')
        return tokutei_phone

    # ② 会社名コアのテキスト位置への近接スコア
    company_pos = text.find(company_core)
    if company_pos < 0:
        return candidates[0]

    # 各候補電話番号の出現位置を探す
    # 検索精度: ① フォーマット済み("03-1234-5678") → ② 数字のみ("0312345678") → ③ 先頭4桁("0312")
    best_phone = candidates[0]
    best_dist = float('inf')
    for p in candidates:
        digits = re.sub(r'\D', '', p)
        if len(digits) < 7:
            continue
        found_pos = False
        for m in re.finditer(re.escape(p), text):
            d = abs(m.start() - company_pos)
            if d < best_dist:
                best_dist = d
                best_phone = p
                found_pos = True
        if not found_pos:
            for m in re.finditer(re.escape(digits), text):
                d = abs(m.start() - company_pos)
                if d < best_dist:
                    best_dist = d
                    best_phone = p
                    found_pos = True
        if not found_pos:
            for m in re.finditer(re.escape(digits[:4]), text):
                d = abs(m.start() - company_pos)
                if d < best_dist:
                    best_dist = d
                    best_phone = p

    return best_phone


@lru_cache(maxsize=256)
def _search_phone_by_company_name(company: str, origin: Optional[str] = None) -> Optional[str]:
    """
    会社名でYahoo検索し、SERPに表示された電話番号をフォールバック取得する。
    origin が指定された場合、取得した番号がそのドメイン上に実在するか検証する。
    検証できない場合は None を返して破棄する（他社番号混入防止）。
    lru_cache により同一会社・同一originへの重複SERP呼び出しを抑制する。
    """
    try:
        from urllib.parse import quote
        query = quote(f'{company} 電話番号')
        url = f'https://search.yahoo.co.jp/search?p={query}&ei=UTF-8'
        soup = _fetch(url, timeout=10, max_retries=1)
        if not soup:
            return None
        phone = None
        # 1st pass: find phone in a SERP block that also mentions the company name
        company_core = re.sub(
            r'株式会社|有限会社|合同会社|一般社団法人|Inc\.?|Corp\.?|Ltd\.?|LLC',
            '', company, flags=re.I
        ).strip()
        if company_core:
            for block in soup.find_all(['div', 'li', 'article', 'section'], limit=50):
                if company_core not in block.get_text():
                    continue
                for a in block.find_all('a', href=True):
                    href = a.get('href', '')
                    if href.lower().startswith('tel:'):
                        phone = extract_phone(href)
                        if phone:
                            break
                if not phone:
                    phone = extract_phone(block.get_text(separator='\n'))
                if phone:
                    break
        # 2nd/3rd passは無効化: 1st passで会社名と紐付けできなかった場合は破棄。
        # SERP全体からの無差別取得は他社番号混入リスクが高く整合率を下げるため。
        if not phone:
            return None
        # ドメイン検証: SERPで取った番号が会社サイトに実在するか確認
        if origin and not _verify_phone_on_domain(origin, phone):
            # LPドメインで確認できない場合、会社の公式ドメインで再確認
            co_origin = _find_company_origin_from_serp(company)
            if co_origin and co_origin.rstrip('/') != origin.rstrip('/'):
                if _verify_phone_on_domain(co_origin, phone):
                    logging.debug(
                        f'[Finder] SERP番号を公式ドメインで確認: {company} → {phone} ({co_origin})'
                    )
                    return phone
            # 逆SERP検証: JS描画でドメイン確認不可の場合のフォールバック
            if _verify_phone_via_reverse_serp(company, phone):
                logging.debug(f'[Finder] SERP番号を逆検索で確認: {company} → {phone}')
                return phone
            logging.debug(f'[Finder] SERP番号がドメイン未確認のため破棄: {company} → {phone}')
            return None
        logging.debug(f'[Finder] Yahoo SERP電話番号取得（検証済み）: {company} → {phone}')
        return phone
    except Exception as e:
        logging.debug(f'[Finder] Yahoo検索フォールバック失敗: {e}')
        return None


def _extract_phones_from_soup(soup: BeautifulSoup) -> list[str]:
    """ページから全ての有効な電話番号を優先順位順で返す。"""
    seen: set[str] = set()
    result: list[str] = []

    def _add(phone: Optional[str]):
        if phone and phone not in seen:
            seen.add(phone)
            result.append(phone)

    # 1. tel: リンク（最も確実）
    for a in soup.find_all('a', href=True):
        href = a.get('href', '')
        if href.lower().startswith('tel:'):
            _add(extract_phone(href))

    # 1a. <button>/<label>/<span>/<div>/<a> の onclick / data-href / href 等に tel: が含まれる場合
    for tag in soup.find_all(['button', 'label', 'span', 'div', 'a'], attrs=True):
        for attr_name in ('onclick', 'data-href', 'data-tel', 'data-phone'):
            val = str(tag.get(attr_name, '') or '')
            if val.lower().startswith('tel:') or 'tel:' in val.lower():
                # "tel:03-xxxx" を抽出
                m = re.search(r'tel:[+\d\-\(\) ]{5,20}', val, re.IGNORECASE)
                if m:
                    _add(extract_phone(m.group(0)))

    # 1b-pre. <address> タグ（HTML5セマンティック連絡先タグ）
    # 日本語サイトでは会社住所・電話番号が <address> に記載されることが多い
    for addr in soup.find_all('address'):
        addr_text = addr.get_text(separator=' ', strip=True)
        if addr_text:
            for p in extract_all_phones(addr_text):
                _add(p)
        # <address> 内の tel: リンクも確認
        for a in addr.find_all('a', href=True):
            href = a.get('href', '')
            if href.lower().startswith('tel:'):
                _add(extract_phone(href))

    # 1b. aria-label に電話番号を含む要素（アクセシビリティ対応サイト）
    # 例: <button aria-label="電話する 03-1234-5678">
    for tag in soup.find_all(True, attrs={'aria-label': True}):
        aria = str(tag.get('aria-label', ''))
        if '0' in aria and any(c.isdigit() for c in aria):
            _add(extract_phone(aria))

    # 1c. img の alt テキストに電話番号が含まれる場合
    # 例: <img alt="お電話: 03-1234-5678" src="tel_image.png">
    for img in soup.find_all('img', alt=True):
        alt = str(img.get('alt', ''))
        if '0' in alt and len(alt) <= 40:
            _add(extract_phone(alt))

    # 1d. <input type="tel"> / <input name="*tel*|*phone*"> の value 属性
    # LPフォーム等で readonly / hidden インプットに電話番号が埋め込まれている場合
    for inp in soup.find_all('input'):
        inp_type = str(inp.get('type', '')).lower()
        inp_name = str(inp.get('name', '')).lower()
        inp_val  = str(inp.get('value', '') or '')
        if not inp_val:
            continue
        is_tel_input = (
            inp_type == 'tel'
            or any(k in inp_name for k in ('tel', 'phone', 'contact'))
        )
        if is_tel_input and len(inp_val) <= 20:
            _add(extract_phone(inp_val))

    # 2. data-phone / data-tel 等の属性値（JSレンダリング前に埋め込まれた番号）
    for tag in soup.find_all(True):
        for attr_name, attr_val in (tag.attrs or {}).items():
            if not isinstance(attr_name, str) or not isinstance(attr_val, str):
                continue
            if any(k in attr_name.lower() for k in ('phone', 'tel', 'contact')):
                _add(extract_phone(attr_val))

    # 3. インラインJSの電話番号（var phone = "..."  /  "telephone":"..."  形式）
    for script in soup.find_all('script'):
        sid = str(script.get('id', '') or '')
        stype = str(script.get('type', '') or '')
        js = getattr(script, 'string', None) or ''
        if not js:
            continue
        is_next_data = '__NEXT_DATA__' in sid or ('application/json' in stype and sid)
        max_size = 500000 if is_next_data else 50000
        if len(js) > max_size:
            continue
        # __NEXT_DATA__ / type=application/json: JSONを再帰解析
        if is_next_data:
            try:
                data = json.loads(js)
                _TEL_JSON_KEYS = frozenset({
                    'telephone', 'tel', 'phone', 'phoneNumber', 'phone_number',
                    'contact_phone', 'contactPhone', 'freephone', 'freeDial',
                })

                def _find_phones_in_json(obj: object, depth: int = 0):
                    if depth > 8:
                        return
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if k in _TEL_JSON_KEYS and isinstance(v, str):
                                _add(extract_phone(v))
                            else:
                                _find_phones_in_json(v, depth + 1)
                    elif isinstance(obj, list):
                        for item in obj[:50]:
                            _find_phones_in_json(item, depth + 1)

                _find_phones_in_json(data)
            except Exception:
                pass
            continue  # __NEXT_DATA__はregexも不要
        # 一般的な文字列リテラル中の電話番号パターン
        for m in re.finditer(r'["\'](\d[\d\-\(\)\.\s]{7,18})["\']', js):
            _add(extract_phone(m.group(1)))
        # SPA 初期状態変数: window.__INITIAL_STATE__ / window.APP_DATA / __next_data__ 等
        # "telephone": "03-xxxx-xxxx" / "tel": "03-xxxx-xxxx" / "phone": "03-xxxx-xxxx"
        for m in re.finditer(
            r'["\'](?:telephone|tel|phone|phoneNumber|contact_phone)["\']'
            r'\s*:\s*["\']([0-9][0-9\-\(\) ]+)["\']',
            js, re.IGNORECASE
        ):
            _add(extract_phone(m.group(1)))

    # 4. OGP phone / contact meta tags
    for attrs in [
        {'property': 'og:phone_number'},
        {'name': 'contact:phone_number'},
        {'name': 'phone'},
        {'itemprop': 'telephone'},
    ]:
        tag = soup.find('meta', attrs=attrs)
        if tag:
            _add(extract_phone(str(tag.get('content') or '')))

    # 5. Schema.org microdata (itemprop="telephone")
    for el in soup.find_all(True, attrs={'itemprop': True}):
        prop = str(el.get('itemprop', '')).lower()
        if 'telephone' in prop or 'phone' in prop:
            val = str(el.get('content') or el.get_text(strip=True))
            _add(extract_phone(val))

    # 5b. hCard / hProduct microformat: class="tel" or class="phone"
    # 注意: 'tel' in cls は "hotel" にもマッチするため単語境界で比較する
    _TEL_CLASS_WORDS = frozenset({
        'tel', 'phone', 'contact', 'telephone', 'tel-number', 'phone-number',
        'tel_number', 'phone_number', 'phonenumber', 'telnumber',
    })
    for el in soup.find_all(True, class_=True):
        cls_words = set(' '.join(el.get('class', [])).lower().replace('-', ' ').replace('_', ' ').split())
        if not cls_words & _TEL_CLASS_WORDS:
            continue
        val = str(el.get('content') or el.get_text(strip=True))
        if len(val) <= 25:  # 電話番号は長くても20文字程度
            _add(extract_phone(val))

    # 6. ラベル付き電話番号
    text = soup.get_text(separator='\n', strip=True)
    for pattern in _PHONE_LABEL_PATTERNS:
        for m in re.finditer(pattern, text, re.MULTILINE):
            for p in extract_all_phones(m.group(1)):
                _add(p)

    # 7. テキスト全体
    for p in extract_all_phones(text):
        _add(p)

    return result


def _extract_phone_from_soup(soup: BeautifulSoup) -> Optional[str]:
    phones = _extract_phones_from_soup(soup)
    return phones[0] if phones else None


def _fetch_and_extract(url: str, timeout: int = 8, max_retries: int = 1) -> tuple[Optional[str], Optional[str]]:
    """1URLを取得して (company, phone) を返す。失敗時は (None, None)。
    優先順: JSON-LDペア → 同一コンテナペア → テキストラベル近接ペア → 別々フォールバック"""
    soup = _fetch(url, timeout=timeout, max_retries=max_retries)
    if soup is None:
        return None, None
    # 1. JSON-LDペア（最高信頼度: 同一Organizationオブジェクト内）
    c_ld, p_ld = _extract_from_json_ld(soup)
    if c_ld and p_ld:
        logging.debug(f'[Pair] json_ld_pair: {url}')
        return c_ld, p_ld
    # 2. 同一コンテナペア（同一table/dl/div内に両方存在）
    c_pair, p_pair = _extract_pair_from_containers(soup)
    if c_pair and p_pair:
        logging.debug(f'[Pair] container_pair: {url}')
        return c_pair, p_pair
    # 3. テキストラベル近接ペア（会社ラベル行と電話ラベル行が同ページ内で近接）
    # separate_fallbackより前に試みることで、ページ内に複数電話がある場合の誤ペア防止。
    # 特商法ページの「販売業者: ○○\nTEL: xxx」型に対応。
    text = soup.get_text(separator='\n', strip=True)
    c_text, p_text = _extract_pair_from_text(text)
    if c_text and p_text:
        logging.debug(f'[Pair] text_label_pair: {url}')
        return c_text, p_text
    # 4. フォールバック: 別々取得（信頼度低 — 会社名と電話が異なるコンテキストから来る可能性）
    c = c_ld or c_pair or c_text or _extract_company_from_soup(soup)
    p = p_ld or p_pair or p_text or _extract_phone_from_soup(soup)
    if c and p:
        logging.debug(f'[Pair] separate_fallback: {url}')
    return c, p


def _parallel_search(
    urls: list[str],
    need_company: bool,
    need_phone: bool,
    max_workers: int = 3,
    timeout: float = 10.0,
) -> tuple[Optional[str], Optional[str]]:
    """
    URLリストを並列フェッチして最初に見つかった company/phone を返す。
    同一URLで両方取れた場合を最優先。それ以外は別URL結合をフォールバックとする。
    """
    company: Optional[str] = None
    phone: Optional[str] = None

    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {executor.submit(_fetch_and_extract, u): u for u in urls}
        try:
            for future in as_completed(futures, timeout=timeout):
                try:
                    c, p = future.result()
                except Exception:
                    continue
                # 同一URLで両方取れた場合は即返却（クロスURL汚染なし）
                if c and p and need_company and need_phone:
                    executor.shutdown(wait=False, cancel_futures=True)
                    return c, p
                # 同一URLで両方取れた場合を優先 — 既に別URLから片方だけ取れていても
                # このURLが両方持っていればペアを差し替える（クロスURL誤ペア防止）
                if c and p:
                    if need_company and need_phone:
                        company, phone = c, p
                        break
                    elif need_company and not need_phone:
                        if not company:
                            company = c
                    elif need_phone and not need_company:
                        if not phone:
                            phone = p
                else:
                    if c and need_company and not company:
                        company = c
                    if p and need_phone and not phone:
                        phone = p
                if (not need_company or company) and (not need_phone or phone):
                    break
        except Exception:
            pass
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return company, phone


def _extract_company_from_text(text: str) -> Optional[str]:
    """プレーンテキストから会社名をパターンマッチングで抽出する（最終手段）。
    全マッチを試し、_is_valid_company（厳格）で通るものを優先する。"""
    labeled_fallback: Optional[str] = None
    for pattern in _COMPANY_LABEL_PATTERNS:
        is_copyright = 'Copyright' in pattern
        for m in re.finditer(pattern, text, re.MULTILINE):
            candidate = _normalize_name(m.group(1).strip())
            if not candidate:
                continue
            if _is_valid_company(candidate):
                return candidate
            if not is_copyright and not labeled_fallback and _is_valid_company_labeled(candidate):
                labeled_fallback = candidate
    return labeled_fallback


def _extract_pair_from_text(text: str) -> tuple[Optional[str], Optional[str]]:
    """プレーンテキストから会社名・電話番号をペアで抽出する。
    会社ラベル行と電話ラベル行が隣接（±5行以内）している場合に両方を返す。
    複数ラベルがある場合は最も近い（会社, 電話）ペアを採用する。
    同一コンテナが見つからなかった場合のテキストフォールバックとして使用。"""
    lines = [ln.strip() for ln in text.split('\n') if ln.strip()]

    co_pat = _PAIR_CO_PAT
    ph_pat = _PAIR_PH_PAT

    # 全ての会社候補・電話候補を収集する（上書きではなくリストで保持）
    co_candidates: list[tuple[str, int]] = []  # (company, line_idx)
    ph_candidates: list[tuple[str, int]] = []  # (phone, line_idx)

    for i, line in enumerate(lines):
        # 行頭の装飾記号を除去してからラベルマッチング（【TEL】・◆電話番号 等に対応）
        clean = _PAIR_DECO_STRIP_RE.sub('', line)
        # 会社ラベル: ラベル + 値が同行 or 次行
        m = co_pat.match(clean)
        if m:
            val = clean[m.end():].strip()
            if not val and i + 1 < len(lines):
                val = lines[i + 1]
            candidate = _normalize_name(val)
            if candidate and (_is_valid_company(candidate) or _is_valid_company_labeled(candidate)):
                # 次行が値の場合は次行の行番号を基準にする（近接計算の精度向上）
                idx = (i + 1) if (not clean[m.end():].strip() and i + 1 < len(lines)) else i
                co_candidates.append((candidate, idx))
            continue
        # 電話ラベル: ラベル + 値が同行 or 次行
        m2 = ph_pat.match(clean)
        if m2:
            val2 = clean[m2.end():].strip()
            if not val2 and i + 1 < len(lines):
                val2 = lines[i + 1]
            p = extract_phone(val2) if val2 else None
            if p:
                idx2 = (i + 1) if (not clean[m2.end():].strip() and i + 1 < len(lines)) else i
                ph_candidates.append((p, idx2))

    # ラベル付き電話が存在する場合: 最も近い (会社, 電話) ペアを探す
    if co_candidates and ph_candidates:
        best_co, best_ph, best_dist = None, None, float('inf')
        for co, co_idx in co_candidates:
            for ph, ph_idx in ph_candidates:
                dist = abs(co_idx - ph_idx)
                if dist < best_dist:
                    best_dist = dist
                    best_co, best_ph = co, ph
        if best_co and best_ph and best_dist <= 8:
            return best_co, best_ph

    # ラベルなし電話スキャン: 各会社候補の直後7行に電話番号があるか調べる
    # 住所が複数行にわたっても捕捉できるよう +8 行（値行から7行）
    if co_candidates and not ph_candidates:
        for co, co_idx in co_candidates:
            scan_start = co_idx + 1
            scan_end = min(co_idx + 8, len(lines))
            for j in range(scan_start, scan_end):
                p = extract_phone(lines[j])
                if p:
                    return co, p

    # 片方だけでも返す（フォールバック）
    company = co_candidates[0][0] if co_candidates else None
    phone   = ph_candidates[0][0] if ph_candidates else None  # (phone_str, line_idx)[0]
    return company, phone


# _extract_pair_from_text 用モジュールレベル定数（関数内コンパイル回避）
_PAIR_CO_PAT = re.compile(
    r'(?:販売業者|運営会社|事業者名|法人名|会社名|商号|企業名|提供者|サービス提供者|'
    r'事業者|販売者|運営事業者|運営主体|運営元|提供会社|サービス提供会社|屋号|開業者|'
    r'申込先|運営責任者|施設名|組織名|法人名称|法人の名称|ショップ名|店舗名|ストア名|'
    r'出店者名|医療機関名|診療所名|事業所名|販売会社|代理店|主催者|主催会社|代理店名|'
    r'申込先会社|注文先|受注者|運営者|発行者|管理者|相手方|事業者等|'
    r'取扱業者|取扱事業者|サービス提供元|サービス責任者)\s*[：:：]?\s*'
)
_PAIR_PH_PAT = re.compile(
    r'(?:TEL|Tel|tel|電話|お電話|電話番号|フリーダイヤル|代表電話|直通|'
    r'連絡先|お問い合わせ先|ご連絡先|お問い合わせ電話|お電話番号|電話受付|'
    r'Phone|phone|PHONE|Telephone)\s*[：:：\s]?\s*'
)
_PAIR_DECO_STRIP_RE = re.compile(r'^[　\s・【】（）◆■▶▲●○◎※□■]+')

_TOKUTEI_LINK_KEYWORDS = [
    '特定商取引', '特商法', '特定商取引法に基づく', '商取引法',
    'tokutei', 'tokusho', 'specified-commercial', 'legal-notice',
]

_COMPANY_LINK_KEYWORDS = [
    '会社概要', '会社情報', '企業情報', '運営会社', '運営者情報',
    'about', 'company', 'corporate', 'about-us', 'aboutus',
]

# onclick から URL を抽出するパターン
# location.href = '/tokutei' / window.location = 'https://...' など
_ONCLICK_URL_RE = re.compile(
    r'(?:location\.href|window\.location(?:\.href)?)\s*=\s*[\'"]([^\'"]+)[\'"]',
    re.IGNORECASE
)


def _resolve_link(href: str, origin: str, base_dir: str, allow_external: bool = False) -> Optional[str]:
    """href を絶対 URL に解決する。解決できない場合は None。
    urljoin で / ./ ../ 相対パスを正しく処理する。"""
    if not href or href.startswith('#') or href.startswith('mailto:') or href.startswith('tel:'):
        return None
    if href.startswith('javascript:'):
        return None
    # urljoin で絶対URLを解決（/ ./ ../ 相対パス・フル URL すべてに対応）
    try:
        resolved = urljoin(base_dir, href)
    except Exception:
        return None
    if not resolved.startswith('http'):
        return None
    origin_netloc = urlparse(origin).netloc
    resolved_netloc = urlparse(resolved).netloc
    if resolved_netloc == origin_netloc:
        return resolved
    if allow_external and resolved_netloc and '.' in resolved_netloc:
        return resolved
    return None


def _collect_nav_links(soup: BeautifulSoup, keywords: list[str], origin: str, base_dir: str,
                       allow_external: bool = False) -> list[str]:
    """ナビゲーション要素（a/span/div/button）からキーワードに合致するリンクを収集する。
    href属性 + javascript: onclick の両パターンに対応。"""
    links: list[str] = []

    # Pass 1: 通常の <a href="..."> リンク（最も多い）
    for a in soup.find_all('a', href=True):
        href = str(a.get('href', ''))
        text = a.get_text(strip=True).lower()
        combined = text + href.lower()
        if not any(kw in combined for kw in keywords):
            continue
        if href.startswith('#') or href.startswith('mailto:'):
            continue
        # javascript: href: onclick から URL を抽出
        if href.startswith('javascript:'):
            onclick = str(a.get('onclick', '') or '')
            m_onclick = _ONCLICK_URL_RE.search(onclick)
            if m_onclick:
                href = m_onclick.group(1)
            else:
                continue
        url = _resolve_link(href, origin, base_dir, allow_external)
        if url:
            links.append(url)

    # Pass 2: onclick を持つ非 a 要素（span/div/button/li）で、テキストにキーワードを含むもの
    for tag in soup.find_all(['span', 'div', 'button', 'li'], attrs={'onclick': True}):
        onclick = str(tag.get('onclick', '') or '')
        text = tag.get_text(strip=True).lower()
        combined = text + onclick.lower()
        if not any(kw in combined for kw in keywords):
            continue
        m_onclick = _ONCLICK_URL_RE.search(onclick)
        if not m_onclick:
            continue
        url = _resolve_link(m_onclick.group(1), origin, base_dir, allow_external)
        if url:
            links.append(url)

    return list(dict.fromkeys(links))[:5]


def _find_tokutei_links(soup: BeautifulSoup, origin: str, allow_external: bool = False, page_url: str = '') -> list[str]:
    """LPページから特商法ページへのリンクを動的に発見する。
    固定パスリストに含まれないカスタムパスをカバーする。
    allow_external=True の場合、LPビルダー等で外部ドメインへのリンクも許可する。
    page_url が指定された場合、相対パス（/なし）の href を絶対URLに変換する。
    javascript: href の onclick から URL を抽出する。"""
    if page_url:
        parsed_page = urlparse(page_url)
        base_dir = parsed_page.scheme + '://' + parsed_page.netloc + '/'.join(parsed_page.path.split('/')[:-1]) + '/'
    else:
        base_dir = origin + '/'
    return _collect_nav_links(soup, _TOKUTEI_LINK_KEYWORDS, origin, base_dir, allow_external)


def _find_company_page_links(soup: BeautifulSoup, origin: str, page_url: str = '') -> list[str]:
    """LPページから会社概要・about ページへのリンクを動的に発見する。
    LPビルダードメインで静的パス検索をスキップする場合のカバー。
    page_url が指定された場合、相対パス（/なし）の href を絶対URLに変換する。
    javascript: href の onclick から URL を抽出する。"""
    if page_url:
        parsed_page = urlparse(page_url)
        base_dir = parsed_page.scheme + '://' + parsed_page.netloc + '/'.join(parsed_page.path.split('/')[:-1]) + '/'
    else:
        base_dir = origin + '/'
    return _collect_nav_links(soup, _COMPANY_LINK_KEYWORDS, origin, base_dir, allow_external=False)


# LPビルダー/マルチテナントプラットフォームのドメイン。
# これらが LP の origin として検出された場合、origin ベースの tokutei/company パス検索を
# スキップして LP 本文のみから抽出する（ビルダー自身の法人情報を誤取得防止）。
_LP_BUILDER_DOMAINS = {
    'peraichi.com',
    'studio.site',
    'wix.com', 'wixsite.com',
    'jimdosite.com', 'jimdo.com', 'jimdofree.com',
    'strikingly.com',
    'weebly.com',
    'squarespace.com',
    'webflow.io',
    'stores.jp', 'baseshop.jp', 'base.shop', 'thebase.in',
    'myshopify.com', 'shopify.com',
    'localplace.jp',
    'coubic.com',
    'ameblo.jp', 'amebaownd.com',
    'lit.link',
    'linktree.ee', 'linktr.ee',
    'note.com',
    'canva.site',
    'fc2.com', 'fc2web.com',
    'page.line.me',
    # Blog / CMS プラットフォーム
    'wordpress.com',
    'blogspot.com', 'blogger.com',
    'tumblr.com',
    'hatenablog.com', 'hateblo.jp',
    # 日本語LPビルダー追加
    'colorfulbox.jp',
    'studio.design',
    'lp.colorful.io',
    'ferret-plus.com',
    # Google Sites
    'sites.google.com',
    # Carrd / Notion公開
    'carrd.co',
    # 新規追加: Notionサイト公開
    'notion.site',
    # フレーム系ノーコードビルダー
    'framer.com', 'framer.website', 'framer.app',
    # Glide / Softr / Super（Notionベース）
    'glide.page', 'glideapp.io',
    'softr.io',
    'super.so',
    # Webflow hosted（webflow.ioは既存、webflow.comを追加）
    'webflow.com',
    # Typedream / Umso / Tilda
    'typedream.app',
    'umso.com',
    'tilda.ws', 'tilda.cc',
    # 予約・問い合わせSaaS系（LP代わりに使われる）
    'tayori.com',
    # ポートフォリオ / リンクまとめ系
    'bio.link', 'beacons.ai',
}


def _is_lp_builder_domain(origin: str) -> bool:
    """origin が LP ビルダープラットフォームかどうかを判定する。"""
    try:
        netloc = urlparse(origin).netloc.lower()
        # www.xxx.com → xxx.com
        if netloc.startswith('www.'):
            netloc = netloc[4:]
        return any(netloc == d or netloc.endswith('.' + d) for d in _LP_BUILDER_DOMAINS)
    except Exception:
        return False


_SERP_BAD_DOMAINS = {
    # 検索エンジン・地図
    'yahoo.co.jp', 'yahoo.com', 'google.co.jp', 'google.com', 'bing.com',
    'maps.google', 'map.google',
    # 百科事典・辞書
    'wikipedia.org', 'wikidata.org', 'wikimedia.org', 'kotobank.jp',
    # EC・ショッピング
    'amazon.co.jp', 'amazon.com', 'rakuten.co.jp', 'rakuten.com',
    'yahoo.co.jp', 'mercari.com', 'paypay.ne.jp',
    # グルメ・観光・レビュー
    'tabelog.com', 'hotpepper.jp', 'jalan.net', 'ikyu.com',
    'tripadvisor.com', 'tripadvisor.jp', 'yelp.com', 'yelp.co.jp',
    'kakaku.com', 'review.com', 'cosme.net',
    # SNS・動画
    'instagram.com', 'facebook.com', 'twitter.com', 'x.com',
    'linkedin.com', 'youtube.com', 'tiktok.com', 'line.me',
    'pinterest.com', 'snapchat.com',
    # 求人・転職
    'indeed.com', 'indeed.co.jp', 'mynavi.jp', 'recruit.co.jp',
    'rikunabi.com', 'doda.jp', 'en-japan.com', 'type.jp',
    'townwork.net', 'hellowork.mhlw.go.jp', 'job.nikkei.co.jp',
    # 不動産ポータル
    'suumo.jp', 'homes.co.jp', 'athome.co.jp', 'chintai.net',
    'rehouse.co.jp', 'nomu.com',
    # プレス・ニュース
    'nikkei.com', 'prtimes.jp', 'entrex.net', 'businesswire.com',
    'sankei.com', 'asahi.com', 'yomiuri.co.jp', 'mainichi.jp',
    # 電話帳・タウンページ
    'itp.ne.jp', 'townpage.co.jp', 'navitime.co.jp',
    'benri.com', '0120gohgo.jp', 'p-search.jp', 'telphone.co.jp',
    # ブログ・SNSプラットフォーム
    'ameblo.jp', 'note.com', 'qiita.com', 'zenn.dev',
    'hatenablog.com', 'fc2.com',
    # その他アグリゲータ
    'openstreetmap.org', 'mapion.co.jp', 'zoho.com',
    'softbank.jp', 'docomo.ne.jp', 'au.com',
}


@lru_cache(maxsize=256)
def _verify_phone_via_reverse_serp(company: str, phone: str) -> bool:
    """電話番号 + 会社名でYahoo逆検索し、会社名が結果に出現するか確認する。
    JS描画でドメイン検証不可の場合のフォールバック。"""
    from urllib.parse import quote
    digits = re.sub(r'\D', '', phone)
    if not digits:
        return False
    # 会社名の主要部分（法人格除く最初の10文字）
    company_core = re.sub(
        r'株式会社|有限会社|合同会社|一般社団法人|Inc\.?|Corp\.?|Ltd\.?|LLC',
        '', company, flags=re.I
    ).strip()[:10]
    if not company_core or len(company_core) < 3:
        return False
    try:
        query = quote(f'{digits} {company_core}')
        url = f'https://search.yahoo.co.jp/search?p={query}&ei=UTF-8'
        resp = requests.get(url, headers=_HEADERS, timeout=8, allow_redirects=True)
        if resp.status_code != 200:
            return False
        # 会社名の主要部分がSERP結果テキストに存在するか
        return company_core in resp.text
    except Exception:
        return False


@lru_cache(maxsize=256)
def _find_company_origin_from_serp(company: str) -> Optional[str]:
    """会社名でYahoo検索し、公式サイトのoriginを推定して返す。失敗時None。"""
    from urllib.parse import quote
    try:
        url = f'https://search.yahoo.co.jp/search?p={quote(company)}&ei=UTF-8'
        resp = requests.get(url, headers=_HEADERS, timeout=8, allow_redirects=True)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, 'lxml')
        # Yahoo SERPのcite要素（検索結果のURL表示部分）からドメインを抽出
        # 形式例: "https://example.co.jp › about" / "example.co.jp/path" /
        #         "example.co.jp" 等、スキーム有無・パス有無が混在する
        for cite in soup.find_all('cite'):
            raw = cite.get_text(strip=True)
            # スキームを除去した後、ドメイン部分（英数字・ハイフン・ドット）だけを抽出
            without_scheme = re.sub(r'^https?://', '', raw)
            m_domain = re.match(r'([a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,})', without_scheme)
            if not m_domain:
                continue
            domain = m_domain.group(1)
            if not domain or '.' not in domain:
                continue
            if any(bad in domain for bad in _SERP_BAD_DOMAINS):
                continue
            return f'https://{domain}'
        # fallback: extract from anchor hrefs in result snippets
        for a in soup.find_all('a', href=True):
            href = str(a.get('href', ''))
            if not href.startswith('http'):
                continue
            try:
                netloc = urlparse(href).netloc.lower()
            except Exception:
                continue
            if not netloc or '.' not in netloc:
                continue
            if any(bad in netloc for bad in _SERP_BAD_DOMAINS):
                continue
            return f'https://{netloc}'
    except Exception:
        pass
    return None


@lru_cache(maxsize=256)
def _search_company_by_phone(phone: str) -> Optional[str]:
    """電話番号でYahoo検索し、会社名を逆引きするフォールバック。
    電話番号は取れたが会社名が取れなかった場合の最終手段。"""
    from urllib.parse import quote
    digits = re.sub(r'\D', '', phone)
    if not digits or len(digits) < 9:
        return None
    try:
        # "電話番号 会社名" 検索
        query = quote(f'{phone} 会社名')
        url = f'https://search.yahoo.co.jp/search?p={query}&ei=UTF-8'
        resp = requests.get(url, headers=_HEADERS, timeout=8, allow_redirects=True)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, 'lxml')
        # SERPの各ブロックからラベル付きの会社名を探す
        for block in soup.find_all(['div', 'li', 'article'], limit=40):
            # 電話番号が含まれるブロックに絞る
            if digits not in re.sub(r'\D', '', block.get_text()):
                continue
            text = block.get_text(separator='\n', strip=True)
            company = _extract_company_from_text(text)
            if company and _is_valid_company(company):
                logging.debug(f'[Finder] 電話→会社名逆引き: {phone} → {company}')
                return company
    except Exception as e:
        logging.debug(f'[Finder] 電話→会社名逆引き失敗: {e}')
    return None


# ─────────────────────────────────────────────────────────────
# 最終クリーニング・ゲート
# 新しい汚れパターンを見つけたらここに追記する。
# ─────────────────────────────────────────────────────────────

# スペース区切り・ダッシュ区切り共通のページ種別サフィックス
_PAGE_SUFFIX_RE = re.compile(
    r'(?:\s+|[\-‐–—－]\s*)(?:'
    r'採用サイト|採用情報|採用ページ|採用|求人情報|求人|'
    r'公式サイト|公式ページ|公式HP|公式|ホームページ|'
    r'コーポレートサイト|コーポレートページ|コーポレート|'
    r'オフィシャルサイト|オフィシャル|'
    r'会社概要|会社情報|企業情報|企業概要|会社案内|グループ会社|'
    r'お問い合わせ|問い合わせ|'
    r'本社所在地|事業所所在地|所在地|'
    r'アクセス|MAP|地図|サービスサイト|ブランドサイト'
    r')\s*$'
)


def _sanitize_company_final(name: str) -> Optional[str]:
    """find_company_info の最終 return 直前に呼ぶ包括的クリーニング。
    Noneを返した場合はその会社名を保存しない。
    新しい汚れパターンは STEP 2〜4 に追記するだけでよい。"""
    if not name:
        return None

    # STEP 1: 標準ノーマライズ（べき等）
    name = _normalize_name(name)
    if not name:
        return None

    # STEP 2: ページ種別サフィックスを除去（ダッシュ区切り・スペース区切り両対応）
    # 例: "株式会社ABC 採用情報" / "株式会社ABC‐公式サイト" → "株式会社ABC"
    prev = None
    while prev != name:
        prev = name
        name = _PAGE_SUFFIX_RE.sub('', name).strip()
    if not name:
        return None

    # STEP 3: 役職タイトルを除去（スペース区切り・直結両対応、再適用）
    # 例: "株式会社ABC 代表取締役CEO" / "株式会社ABC代表取締役" → "株式会社ABC"
    if _LEGAL_ENTITY_RE.search(name) or name.startswith(_LEGAL_ENTITY_PREFIXES):
        m_role = _ROLE_TITLE_SUFFIX_RE.search(name)
        if m_role and m_role.start() > 0:
            candidate = name[:m_role.start()].strip()
            if len(candidate) >= 3:
                name = candidate

    # STEP 4: 2トークン後株の前置きサービス名除去（保守的）
    # 対象: 7文字以上の全カタカナ prefix / FC本部・代理店・フランチャイズ系 prefix のみ
    # 例: "トータルリペアマスター FC本部エンパワーメント株式会社" → "FC本部エンパワーメント株式会社"
    # 保持: "SUN マルシェ株式会社"（SUN は3文字ASCII → 除去しない）
    if ' ' in name and not name.startswith(_LEGAL_ENTITY_PREFIXES):
        _parts = name.split(' ')
        if len(_parts) == 2:
            _first, _last = _parts
            _m_last = _ATOKAB_TRIM_RE.match(_last)
            if _m_last and _m_last.end() == len(_last):
                _is_service_prefix = (
                    (len(_first) >= 7 and re.match(r'^[ァ-ヶー・]+$', _first))
                    or re.match(r'^(?:FC本部|代理店|フランチャイズ|正規|公認|認定|加盟)', _first)
                )
                if _is_service_prefix:
                    name = _last

    # STEP 5: 「サービス名in法人名」パターンから法人名を抽出
    # 例: "エクステンションプリーズinこちらは全保険株式会社" → "全保険株式会社"
    _m_in = re.search(r'\bin\b(.+)', name, re.IGNORECASE)
    if _m_in:
        after_in = _m_in.group(1).strip()
        # "こちらは" / "当社" / "弊社" 等の前置きを除去
        after_in = re.sub(r'^(?:こちらは|当社|弊社|私達は|私たちは)\s*', '', after_in).strip()
        if _LEGAL_ENTITY_RE.search(after_in) and len(after_in) >= 4:
            name = after_in

    # STEP 6: 最終ノーマライズ（上記変更後に再適用）
    name = _normalize_name(name)
    if not name:
        return None

    # STEP 7: 残存ページ種別があれば拒否
    if _PAGE_SUFFIX_RE.search(name):
        return None

    # STEP 8: 明らかにページタイトルのゴミを拒否（法人格を含まない長い文字列）
    if len(name) > 25 and not _LEGAL_ENTITY_RE.search(name):
        return None

    return name.strip() or None


def find_company_info(
    lp_url: str,
    meta_company: Optional[str] = None,
    nta_api_key: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """(company, best_phone, all_phones_str, contact_name, lp_headline) を返す。"""
    if not lp_url:
        return None, None, None, None, None, {}

    try:
        from urllib.parse import urlparse
        parsed = urlparse(lp_url)
        origin = f'{parsed.scheme}://{parsed.netloc}'
    except Exception:
        return None, None, None, None, None, {}

    if not origin or '://' not in origin:
        return None, None, None, None, None, {}

    # LP ビルダープラットフォーム判定
    # peraichi.com / wix.com 等が origin の場合、origin 配下のパスは
    # ビルダー自社の法人情報ページになるため origin ベースのパス検索を無効化する。
    _skip_origin_paths = _is_lp_builder_domain(origin)
    if _skip_origin_paths:
        logging.debug(f'[Finder] LP ビルダードメイン検出 → originパス検索スキップ: {origin}')

    company: Optional[str] = None
    phone: Optional[str] = None
    contact_name: Optional[str] = None
    lp_headline: Optional[str] = None
    _phone_seen: set[str] = set()
    _extra_phones: list[str] = []
    _contact_phone: Optional[str] = None  # contactページ由来（低信頼）: 最終フォールバック専用
    all_text = ""

    def _record_phones(phones: list[str]):
        for p in phones:
            if p and p not in _phone_seen:
                _phone_seen.add(p)
                _extra_phones.append(p)

    # ① 特商法ページ（タイムアウト: 5秒に短縮、リトライ1回）
    # LP本体のリンクから特商法ページを動的発見してURLリストを補完する
    # LP ビルダードメインの場合は動的発見のみ（origin ベースの静的パスはスキップ）
    try:
        lp_soup_for_links = _fetch(lp_url, timeout=5, max_retries=1)
        # LP ビルダードメインの場合は外部ドメインへのリンクも許可（本社tokuteiページ対応）
        dynamic_tokutei = _find_tokutei_links(
            lp_soup_for_links, origin, allow_external=_skip_origin_paths, page_url=lp_url
        ) if lp_soup_for_links else []
        dynamic_company_pages = _find_company_page_links(lp_soup_for_links, origin, page_url=lp_url) if lp_soup_for_links else []
    except Exception:
        dynamic_tokutei = []
        dynamic_company_pages = []
    try:
        if _skip_origin_paths:
            # ビルダードメイン: 動的発見リンクのみ（ビルダーの /tokutei を見ない）
            tokutei_urls = dynamic_tokutei
        else:
            static_urls = [origin + p for p in TOKUTEI_PATHS]
            tokutei_urls = dynamic_tokutei + [u for u in static_urls if u not in dynamic_tokutei]
        for retry in range(2):
            try:
                company, phone = _parallel_search(
                    tokutei_urls,
                    need_company=True,
                    need_phone=True,
                    max_workers=2,
                    timeout=5.0,
                )
                if company:
                    logging.debug(f'特商法ページで取得: {origin}')
                    company = _normalize_name(company)
                    if company and phone and not is_freephone(phone):
                        _record_phones([phone])
                        return company.strip(), phone, ' / '.join(_extra_phones) or None, contact_name, lp_headline, {}
                    # フリーダイヤルのみ/電話なし → 日本語社名は保持したまま電話番号を探し続ける
                    if phone:
                        _record_phones([phone])
                break
            except Exception as e:
                if retry == 0:
                    logging.debug(f'特商法ページ取得リトライ: {e}')
                else:
                    logging.debug(f'特商法ページ取得エラー: {e}')
    except Exception as e:
        logging.debug(f'特商法ページ処理エラー: {e}')

    # ② 会社概要ページ
    # LP ビルダードメインの場合は静的パス検索をスキップし、動的発見リンクのみを使う
    _need_better_company = not company or not _is_japanese_company(company)
    if _need_better_company or not phone:
        try:
            if _skip_origin_paths:
                # ビルダードメイン: 動的発見リンクのみ（ビルダーの /company を見ない）
                company_urls = dynamic_company_pages
            else:
                company_urls = [origin + p for p in COMPANY_PATHS]
            for retry in range(2):
                try:
                    c2, p2 = _parallel_search(
                        company_urls,
                        need_company=_need_better_company,
                        need_phone=not phone,
                        max_workers=2,
                        timeout=5.0,
                    )
                    # 日本語社名が既にある場合は上書きしない
                    if c2 and (not company or not _is_japanese_company(company)):
                        company = c2
                    if p2:
                        phone = p2
                    if company:
                        logging.debug(f'会社概要ページで取得: {origin}')
                        company = _normalize_name(company)
                        if company and phone and not is_freephone(phone):
                            _record_phones([phone])
                            return company.strip(), phone, ' / '.join(_extra_phones) or None, contact_name, lp_headline, {}
                    break
                except Exception as e:
                    if retry == 0:
                        logging.debug(f'会社概要ページ取得リトライ: {e}')
                    else:
                        logging.debug(f'会社概要ページ取得エラー: {e}')
        except Exception as e:
            logging.debug(f'会社概要ページ処理エラー: {e}')

    # ② -b お問い合わせページ（会社名取得・電話番号は低信頼フォールバック専用）
    # LP ビルダードメインの場合はスキップ（ビルダー自社の問い合わせ先を取得してしまう）
    # 電話番号は _contact_phone に保留し、LP/SERP等で取得できなかった場合のみ最終採用する。
    # コールセンター・代理店番号が混入するリスクがあるため直接 phone に入れない。
    if not _skip_origin_paths and (not company or not phone or is_freephone(phone)):
        try:
            contact_urls = [origin + p for p in CONTACT_PATHS]
            for retry in range(2):
                try:
                    c3, p3 = _parallel_search(
                        contact_urls,
                        need_company=not company,
                        need_phone=True,
                        max_workers=1,
                        timeout=5.0,
                    )
                    if c3:
                        company = c3
                    if p3:
                        _record_phones([p3])
                        # 直番のみ低信頼候補として保持（フリーダイヤルは除外）
                        if not is_freephone(p3) and not _contact_phone:
                            _contact_phone = p3
                            logging.debug(f'[Finder] contactページ直番を保留: {p3} ({origin})')
                    break
                except Exception as e:
                    if retry == 0:
                        logging.debug(f'contactページ取得リトライ: {e}')
                    else:
                        logging.debug(f'contactページ取得エラー: {e}')
        except Exception as e:
            logging.debug(f'contactページ処理エラー: {e}')

    # ③ LPページ自体（JavaScript SPA対応・8秒でフル読み込み待機、リトライ1回）
    _lp_needs_company = not company or not _is_japanese_company(company)
    if _lp_needs_company or not phone:
        try:
            for retry in range(2):
                try:
                    soup = _fetch(lp_url, timeout=8.0)
                    if soup:
                        all_text = soup.get_text(separator='\n', strip=True)

                        # JSON-LD抽出（日本語社名がなければ上書き可）
                        c_ld, p_ld = _extract_from_json_ld(soup)
                        if c_ld and (not company or not _is_japanese_company(company)):
                            company = c_ld
                        if not phone and p_ld:
                            phone = p_ld

                        # LP本体内コンテナペア抽出
                        # 特商法・会社情報セクションがLPに埋め込みの場合（LPビルダー系に多い）
                        if not company or not phone:
                            try:
                                c_lp_pair, p_lp_pair = _extract_pair_from_containers(soup)
                                if c_lp_pair and (not company or not _is_japanese_company(company)):
                                    company = c_lp_pair
                                    logging.debug(f'[Finder] LP埋め込みコンテナペアで会社名取得: {c_lp_pair}')
                                if p_lp_pair:
                                    _record_phones([p_lp_pair])
                                    if not phone or (is_freephone(phone) and not is_freephone(p_lp_pair)):
                                        phone = p_lp_pair
                                        logging.debug(f'[Finder] LP埋め込みコンテナペアで電話取得: {p_lp_pair}')
                            except Exception:
                                pass

                        # Meta タグ抽出（日本語社名がなければ）
                        if not company or not _is_japanese_company(company):
                            company_meta = _extract_from_meta(soup)
                            if company_meta:
                                company = company_meta

                        # 電話番号抽出（全番号収集）。直番があればフリーダイヤルで上書きしない
                        _record_phones(_extract_phones_from_soup(soup))
                        if not phone and _extra_phones:
                            # 複数の候補がある場合は会社名に最も近い番号を選ぶ
                            if company and len(_extra_phones) > 1 and all_text:
                                phone = _pick_phone_nearest_company(all_text, company, _extra_phones)
                            else:
                                phone = _extra_phones[0]
                        elif phone and is_freephone(phone):
                            direct = next((p for p in _extra_phones if not is_freephone(p)), None)
                            if direct:
                                # 直番が複数ある場合は近接スコアで選ぶ
                                direct_phones = [p for p in _extra_phones if not is_freephone(p)]
                                if company and len(direct_phones) > 1 and all_text:
                                    phone = _pick_phone_nearest_company(all_text, company, direct_phones)
                                else:
                                    phone = direct

                        # Footer → 全体 の順で会社名抽出（日本語社名がなければ）
                        if not company or not _is_japanese_company(company):
                            _raw_from_soup = None
                            footer = soup.find('footer')
                            if footer:
                                _raw_from_soup = _extract_company_from_soup(footer)
                            if not _raw_from_soup:
                                # フッターで取れなかった場合はページ全体から試みる
                                _raw_from_soup = _extract_company_from_soup(soup)
                            if _raw_from_soup:
                                company = _normalize_name(_raw_from_soup)

                        # Bodyのテキストから会社名パターンマッチング（最後の手段）
                        if (not company or not _is_japanese_company(company)) and all_text:
                            company = _extract_company_from_text(all_text)

                        # テキストブロックペア抽出（構造抽出で取れなかった場合のフォールバック）
                        # 会社ラベルと電話ラベルが近接行にある場合、ペアで採用する
                        if all_text and (not company or not phone):
                            try:
                                c_txt, p_txt = _extract_pair_from_text(all_text)
                                if c_txt and (not company or not _is_japanese_company(company)):
                                    company = c_txt
                                    logging.debug(f'[Finder] テキストペアで会社名取得: {c_txt}')
                                if p_txt and not phone:
                                    phone = p_txt
                                    _record_phones([phone])
                                    logging.debug(f'[Finder] テキストペアで電話取得: {p_txt}')
                            except Exception:
                                pass

                        # LP見出し（キャッチコピー）抽出
                        if not lp_headline:
                            lp_headline = _extract_lp_headline(soup)

                        # 担当者名抽出（LP自体から）
                        if not contact_name:
                            contact_name = _extract_contact_name(soup)

                    break
                except Exception as e:
                    if retry == 0:
                        logging.debug(f'LPページ取得リトライ ({lp_url}): {e}')
                    else:
                        logging.debug(f'LPページ取得エラー ({lp_url}): {e}')
        except Exception as e:
            logging.debug(f'LPページ処理エラー ({lp_url}): {e}')

    # ③.5 英語社名の場合、LP から日本語名を追加探索する
    # 例: Meta広告の「Gramn Inc.」→ LP上の「株式会社グラン」を優先
    if company and _is_english_only_company(company):
        try:
            soup_lp = _fetch(lp_url, timeout=8, max_retries=1)
            if soup_lp:
                # JSON-LD legalName が日本語なら優先
                c_ld2, _ = _extract_from_json_ld(soup_lp)
                if c_ld2 and _is_japanese_company(c_ld2):
                    logging.debug(f'[Finder] 英語社名を日本語名に差し替え: "{company}" → "{c_ld2}"')
                    company = c_ld2
                else:
                    # og:site_name / テーブル / フッターから日本語名を探す
                    jp_raw = (
                        _extract_from_meta(soup_lp)
                        or _extract_company_from_table(soup_lp)
                        or _extract_company_from_divs(soup_lp)
                    )
                    jp_candidate = _normalize_name(jp_raw) if jp_raw else None
                    if jp_candidate and _is_japanese_company(jp_candidate):
                        logging.debug(f'[Finder] 英語社名を日本語名に差し替え: "{company}" → "{jp_candidate}"')
                        company = jp_candidate
        except Exception as e:
            logging.debug(f'[Finder] 英語社名→日本語名変換エラー: {e}')

    # ③.9 meta_companyが日本語法人名として有効な場合の早期採用
    # LP解析で日本語法人名が取れなかった場合、またはLP上に言及がない別会社が取れた場合に適用
    if meta_company:
        try:
            clean_meta = _normalize_name(meta_company.strip())
            if clean_meta and _is_valid_company(clean_meta) and _is_japanese_company(clean_meta):
                if not company or not _is_japanese_company(company):
                    # LP解析で日本語法人名が取れなかった
                    logging.debug(f'[Finder] meta_company早期採用: "{clean_meta}"')
                    company = clean_meta
                elif company != clean_meta and all_text:
                    # LP解析で別の日本語法人名が取れた → LP上にmeta_companyの言及があるか確認
                    meta_core = re.sub(
                        r'株式会社|有限会社|合同会社|一般社団法人|NPO法人',
                        '', clean_meta
                    ).strip()
                    if meta_core and len(meta_core) >= 3 and meta_core not in all_text:
                        # LPに言及なし → このLP は代理ページの可能性 → meta_companyを優先
                        logging.debug(
                            f'[Finder] LPに"{meta_core}"言及なし → '
                            f'"{company}"を破棄してmeta_company"{clean_meta}"優先'
                        )
                        company = clean_meta
                        # LP電話番号が本当にmeta_company以外か逆引きで確認してから破棄
                        # 確認できない場合は電話を保持（無条件破棄によるカバー落ちを防ぐ）
                        if phone and not _verify_phone_via_reverse_serp(clean_meta, phone):
                            # meta_companyと関係ない番号なので破棄
                            phone = None
                            _extra_phones.clear()
                            _phone_seen.clear()
                            logging.debug(
                                f'[Finder] LP電話番号が"{clean_meta}"と無関係のため破棄: 電話SERPで再探索'
                            )
                        elif phone:
                            logging.debug(
                                f'[Finder] LP電話番号が"{clean_meta}"と紐付き確認 → 保持'
                            )
        except Exception:
            pass

    # ④ Meta page_name フォールバック
    # 法人格（株式会社等）が明記されていれば直接採用。
    # 法人格なし(マーケティング名)の場合はNTA照合で確認。
    if not company and meta_company:
        clean_meta = _normalize_name(meta_company.strip())
        if clean_meta and _is_valid_company(clean_meta):
            # 法人格あり → NTA不要で信頼できる
            company = clean_meta
            logging.debug(f'[Finder] Metaページ名（法人格確認済み）採用: "{company}"')
        else:
            try:
                from utils.nta_lookup import verify_and_normalize as _nta_meta_verify
                nta_meta = _nta_meta_verify(meta_company.strip())
                if nta_meta['verified'] and nta_meta['confidence'] in ('exact', 'partial'):
                    company = nta_meta['official_name']
                    logging.debug(
                        f'[NTA] Facebookページ名を法人確認: "{meta_company}" → "{company}" '
                        f'({nta_meta["confidence"]})'
                    )
            except Exception:
                pass

    # ⑤ NTA法人番号APIで正式法人名を確認・補正（全会社対象）
    if company:
        try:
            from utils.nta_lookup import verify_and_normalize as _nta_verify
            nta = _nta_verify(company)
            if nta['verified']:
                if nta['confidence'] == 'exact':
                    pass  # 完全一致: 既に正式名、変更不要
                elif nta['confidence'] == 'partial':
                    old_name = company
                    company = nta['official_name']
                    logging.debug(f'[NTA] 法人名補正: "{old_name}" → "{company}"')
        except Exception as e:
            logging.debug(f'NTA API解決エラー: {e}')
        # 英語法人格サフィックス解決（旧フォールバック）
        if nta_api_key:
            try:
                from processors.legal_name_resolver import needs_jp_name_lookup, resolve_legal_name
                if needs_jp_name_lookup(company):
                    resolved = resolve_legal_name(company, nta_api_key)
                    if resolved:
                        company = resolved
            except Exception as e:
                logging.debug(f'NTA 英語法人格解決エラー: {e}')

    # ⑤.4 担当者名が未取得の場合、会社概要ページ上位2件のみ軽量チェック
    # （②で既にフェッチ済みページへの再アクセスを最小限に抑える）
    if not contact_name:
        for cu in [origin + '/about', origin + '/company']:
            try:
                soup_c = _fetch(cu, timeout=3, max_retries=1)
                if soup_c:
                    contact_name = _extract_contact_name(soup_c)
                    if contact_name:
                        logging.debug(f'[Finder] 担当者名取得: {contact_name} ({cu})')
                        break
            except Exception:
                pass

    # ⑤.5 電話番号が未取得の場合、会社名でYahoo SERP検索（最終フォールバック）
    if company and not phone:
        try:
            phone_from_serp = _search_phone_by_company_name(company, origin=origin)
            if phone_from_serp:
                phone = phone_from_serp
                _record_phones([phone])
                logging.debug(f'[Finder] Yahoo SERP電話番号取得: {company} → {phone}')
        except Exception as e:
            logging.debug(f'Yahoo SERP電話番号検索エラー: {e}')

    # ⑤.6 会社自体の公式ドメインをSERPから発見して電話番号を探す
    # LP domainが会社のドメインでない場合（Meta広告のキャンペーンLPなど）のカバー
    if company and not phone:
        try:
            co_origin = _find_company_origin_from_serp(company)
            if co_origin and co_origin.rstrip('/') != origin.rstrip('/'):
                key_paths = ['', '/company', '/about', '/contact',
                             '/tokutei', '/legal', '/corporate']
                _, phone_co = _parallel_search(
                    [co_origin + p for p in key_paths],
                    need_company=False,
                    need_phone=True,
                    max_workers=2,
                    timeout=6.0,
                )
                if phone_co:
                    # ⑤.6 検証: 逆引き検索で「phone_co + company名」がSERPに出るか確認
                    # _verify_phone_on_domain は JS描画サイトで失敗するため reverse_serp を使う
                    if _verify_phone_via_reverse_serp(company, phone_co):
                        phone = phone_co
                        _record_phones([phone])
                        logging.debug(
                            f'[Finder] 公式ドメイン({co_origin})から電話番号取得(検証済み): {company} → {phone}'
                        )
                    else:
                        logging.debug(
                            f'[Finder] 公式ドメイン電話番号が逆引き未確認のため破棄: {company} → {phone_co}'
                        )
        except Exception as e:
            logging.debug(f'[Finder] 公式ドメイン検索エラー: {e}')

    # ⑤.7 会社名が未取得のまま電話番号だけある場合: 逆引きで会社名を補完
    if not company and phone:
        try:
            company_from_phone = _search_company_by_phone(phone)
            if company_from_phone:
                company = company_from_phone
                logging.debug(f'[Finder] 電話→会社名逆引き採用: {phone} → {company}')
        except Exception as e:
            logging.debug(f'[Finder] 電話→会社名逆引きエラー: {e}')

    # ⑤.8 contactページ由来の低信頼電話番号（最終フォールバック）
    # LP・SERP・公式ドメイン等で取得できなかった場合にのみ採用する。
    # フリーダイヤルしかない場合は直番なら採用する。
    if not phone and _contact_phone:
        phone = _contact_phone
        logging.debug(f'[Finder] contactページ保留番号を最終採用: {phone}')
    elif phone and is_freephone(phone) and _contact_phone and not is_freephone(_contact_phone):
        phone = _contact_phone
        logging.debug(f'[Finder] フリーダイヤルをcontact直番に差し替え: {phone}')

    # ⑥ 最終クリーニング（_sanitize_company_final が一元ゲート）
    if company:
        company = _sanitize_company_final(company)

    if phone:
        _record_phones([phone])

    phones_str = ' / '.join(_extra_phones) if _extra_phones else None

    # 広告シグナル抽出（追加フェッチなし・既取得soupを使用）
    try:
        from processors.rank_calculator import extract_ad_signals
        _soup_for_signals = lp_soup_for_links if 'lp_soup_for_links' in dir() else None
        ad_signals = extract_ad_signals(lp_url, _soup_for_signals)
    except Exception:
        ad_signals = {}

    return company, phone, phones_str, contact_name, lp_headline, ad_signals
