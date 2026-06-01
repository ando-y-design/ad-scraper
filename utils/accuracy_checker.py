from __future__ import annotations
"""
整合率チェッカー v2

整合率の定義:
  「電話番号にかけたとき、そのペアの会社に繋がる確率」

測定方法（自動化できる最善の近似）:
  優先①: 特商法ページで会社名＋電話番号の両方を確認
          （特定商取引法で法的に正確な記載が義務 → 最も信頼性が高い）
  優先②: base_url（会社メインサイト）で電話番号を確認
  優先③: NTA法人番号DBで会社名が実在法人か確認

判定分類:
  tokutei_ok       … 特商法に会社名＋電話の両方あり ✅ (高信頼)
  base_ok          … メインサイトに電話あり ✅ (中信頼)
  nta_only         … NTAで法人確認のみ（電話不明） △
  phone_mismatch   … 特商法に会社名はあるが電話が違う ❌
  not_found        … 特商法もメインサイトも電話なし ❌
  lp_dead          … LPが取得できない（URLが死んでいる） ❌

使い方:
  python -m utils.accuracy_checker              # 直近30日 50件サンプル
  python -m utils.accuracy_checker --sample 30
  python -m utils.accuracy_checker --verbose    # 進捗を表示
"""

import argparse
import html as _html_lib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent.parent
LOG_PATH = BASE_DIR / 'logs' / 'accuracy_log.jsonl'

_FETCH_TIMEOUT = 8
_REQUEST_INTERVAL = 0.5

_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/136.0.0.0 Safari/537.36'
)

# 特商法ページを示すリンクテキスト・URLパターン
_TOKUTEI_LINK_PATTERNS = re.compile(
    r'特定商取引|特商法|会社概要|運営会社|事業者情報|プライバシー|'
    r'tokutei|company|about|legal|law|kiyaku|gaiyou|shouji',
    re.IGNORECASE,
)

# 法人格パターン（会社名コア抽出用）
_CORP_RE = re.compile(
    r'(株式会社|有限会社|合同会社|医療法人|一般社団法人|公益社団法人|'
    r'社会福祉法人|NPO法人|宗教法人|協同組合)',
)

# フリーダイヤル判定
_FREEPHONE_RE = re.compile(r'^(0120|0800)')


# ─────────────────────────────────────────────────────────
# HTML フェッチ
# ─────────────────────────────────────────────────────────

def _fetch(url: str, timeout: int = _FETCH_TIMEOUT) -> Optional[str]:
    """URLを取得してプレーンテキストを返す。失敗時は None。"""
    try:
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': _UA,
                'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
                'Accept-Language': 'ja,en-US;q=0.8',
                'Connection': 'close',
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = 'utf-8'
            ct = resp.headers.get('Content-Type', '')
            if 'charset=' in ct:
                charset = ct.split('charset=')[-1].split(';')[0].strip() or 'utf-8'
            raw = resp.read(500_000)
    except Exception:
        return None

    for enc in (charset, 'utf-8', 'shift_jis', 'euc-jp'):
        try:
            text = raw.decode(enc, errors='strict')
            break
        except Exception:
            text = None
    if text is None:
        text = raw.decode('utf-8', errors='replace')

    # metaタグでcharset再確認
    m = re.search(r'charset=["\']?([\w\-]+)', text[:2000], re.IGNORECASE)
    if m:
        meta_enc = m.group(1)
        if meta_enc.lower() not in ('utf-8', charset.lower()):
            try:
                text = raw.decode(meta_enc, errors='replace')
            except Exception:
                pass

    text = _html_lib.unescape(text)
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'[ \t\r\n　]+', ' ', text)
    return text


def _fetch_html_raw(url: str, timeout: int = _FETCH_TIMEOUT) -> Optional[str]:
    """リンク抽出用に生HTML（タグ除去前）を返す。"""
    try:
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': _UA,
                'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
                'Accept-Language': 'ja,en-US;q=0.8',
                'Connection': 'close',
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = 'utf-8'
            ct = resp.headers.get('Content-Type', '')
            if 'charset=' in ct:
                charset = ct.split('charset=')[-1].split(';')[0].strip() or 'utf-8'
            raw = resp.read(500_000)
    except Exception:
        return None

    for enc in (charset, 'utf-8', 'shift_jis', 'euc-jp'):
        try:
            return raw.decode(enc, errors='strict')
        except Exception:
            pass
    return raw.decode('utf-8', errors='replace')


# ─────────────────────────────────────────────────────────
# 特商法ページ探索
# ─────────────────────────────────────────────────────────

def _extract_links(html: str, base_url: str) -> list[str]:
    """HTMLから href リンクを抽出して絶対URLリストで返す。"""
    from urllib.parse import urljoin, urlparse

    links = []
    for m in re.finditer(r'href=["\']([^"\'>\s]+)["\']', html, re.IGNORECASE):
        href = m.group(1).strip()
        if href.startswith('#') or href.startswith('javascript'):
            continue
        abs_url = urljoin(base_url, href)
        # 同一ドメイン内のみ
        base_domain = urlparse(base_url).netloc
        link_domain = urlparse(abs_url).netloc
        if base_domain and link_domain == base_domain:
            links.append(abs_url)
    return links


def _find_tokutei_candidates(lp_url: str, html: str) -> list[str]:
    """
    LPのHTMLから特商法・会社概要ページ候補のURLを返す（最大5件）。
    リンクテキストとURLパスの両方で判定する。
    """
    from urllib.parse import urlparse

    candidates = []
    seen = set()

    # <a href="..." ...>テキスト</a> をまとめて抽出
    for m in re.finditer(
        r'<a\s[^>]*href=["\']([^"\'>\s]+)["\'][^>]*>(.*?)</a>',
        html, re.IGNORECASE | re.DOTALL
    ):
        href_raw = m.group(1).strip()
        link_text = re.sub(r'<[^>]+>', '', m.group(2)).strip()

        if href_raw.startswith('#') or href_raw.startswith('javascript'):
            continue

        from urllib.parse import urljoin
        abs_url = urljoin(lp_url, href_raw)

        # 同一ドメイン内のみ
        base_domain = urlparse(lp_url).netloc
        if urlparse(abs_url).netloc != base_domain:
            continue

        # リンクテキストまたはURLパスがキーワードにマッチ
        path = urlparse(abs_url).path
        if (
            _TOKUTEI_LINK_PATTERNS.search(link_text)
            or _TOKUTEI_LINK_PATTERNS.search(path)
        ):
            if abs_url not in seen:
                candidates.append(abs_url)
                seen.add(abs_url)
                if len(candidates) >= 5:
                    break

    return candidates


# ─────────────────────────────────────────────────────────
# マッチング
# ─────────────────────────────────────────────────────────

def _norm_phone(phone: str) -> str:
    """電話番号を数字のみに正規化。"""
    return re.sub(r'\D', '', phone)


def _phone_in_text(text: str, phone: str) -> bool:
    """テキストに電話番号（数字正規化）が含まれているか。"""
    digits = _norm_phone(phone)
    if len(digits) < 9:
        return False
    return digits in re.sub(r'\D', '', text)


def _company_in_text(text: str, company: str) -> bool:
    """テキストに会社名（コア部分）が含まれているか。"""
    core = _CORP_RE.sub('', company).strip(' 　・（()）')
    if len(core) < 2:
        core = company
    return core in text


# ─────────────────────────────────────────────────────────
# NTA確認
# ─────────────────────────────────────────────────────────

def _nta_verified(company: str) -> bool:
    """NTA法人番号DBで会社名が実在法人として確認できるか。"""
    try:
        from utils.nta_lookup import search_by_name
        hits = search_by_name(company, mode=2)
        if not hits:
            return False
        core = _CORP_RE.sub('', company).strip()
        for h in hits:
            if core in h.get('name', ''):
                return True
        return False
    except Exception:
        return False


# ─────────────────────────────────────────────────────────
# 1件チェック
# ─────────────────────────────────────────────────────────

def _check_one(row: dict, verbose: bool = False) -> dict:
    """
    1レコードの整合率チェックを実行する。

    Returns:
        {
            'result': str,           # 判定結果
            'confidence': str,       # 'high'|'medium'|'low'|'unknown'
            'detail': str,           # 詳細説明
            'tokutei_url': str|None, # 特商法ページURL（見つかれば）
        }
    """
    company = (row.get('company_name') or '').strip()
    phone   = (row.get('phone') or '').strip()
    lp_url  = (row.get('lp_url') or '').strip()
    base_url_val = (row.get('base_url') or '').strip()

    # ── STEP 1: LP取得 ─────────────────────────────────────
    time.sleep(_REQUEST_INTERVAL)
    lp_html = _fetch_html_raw(lp_url)

    if lp_html is None:
        # base_url があればそちらを試す
        if base_url_val:
            time.sleep(_REQUEST_INTERVAL)
            lp_html = _fetch_html_raw(base_url_val)
            if lp_html is None:
                return {
                    'result': 'lp_dead',
                    'confidence': 'unknown',
                    'detail': 'LP・メインサイト両方取得失敗',
                    'tokutei_url': None,
                }
        else:
            return {
                'result': 'lp_dead',
                'confidence': 'unknown',
                'detail': 'LP取得失敗（base_urlなし）',
                'tokutei_url': None,
            }

    # ── STEP 2: 特商法候補ページを探す ─────────────────────
    tokutei_candidates = _find_tokutei_candidates(lp_url, lp_html)

    # LP自体に特商法情報が埋め込まれている場合もチェック
    lp_text = _html_lib.unescape(lp_html)
    lp_text = re.sub(r'<script[^>]*>.*?</script>', ' ', lp_text, flags=re.DOTALL | re.IGNORECASE)
    lp_text = re.sub(r'<style[^>]*>.*?</style>', ' ', lp_text, flags=re.DOTALL | re.IGNORECASE)
    lp_text = re.sub(r'<[^>]+>', ' ', lp_text)

    lp_has_tokutei_section = bool(re.search(
        r'特定商取引|販売業者|運営者|事業者名', lp_text
    ))

    # ── STEP 3: 特商法ページを1件ずつ確認 ──────────────────
    checked_tokutei: list[str] = []
    tokutei_found_url: Optional[str] = None

    # LP自体に特商法セクションがあれば最初にチェック
    if lp_has_tokutei_section:
        if _company_in_text(lp_text, company) and _phone_in_text(lp_text, phone):
            return {
                'result': 'tokutei_ok',
                'confidence': 'high',
                'detail': f'LPページ内の特商法セクションに会社名・電話が一致',
                'tokutei_url': lp_url,
            }
        if _company_in_text(lp_text, company) and not _phone_in_text(lp_text, phone):
            return {
                'result': 'phone_mismatch',
                'confidence': 'high',
                'detail': f'LPの特商法セクションに会社名はあるが電話が不一致',
                'tokutei_url': lp_url,
            }

    for candidate_url in tokutei_candidates:
        time.sleep(_REQUEST_INTERVAL)
        t_text = _fetch(candidate_url)
        if not t_text:
            continue
        checked_tokutei.append(candidate_url)

        has_company = _company_in_text(t_text, company)
        has_phone   = _phone_in_text(t_text, phone)

        if has_company and has_phone:
            return {
                'result': 'tokutei_ok',
                'confidence': 'high',
                'detail': f'特商法ページに会社名・電話が一致: {candidate_url}',
                'tokutei_url': candidate_url,
            }

        if has_company and not has_phone:
            # 会社名は確認できた → 電話が違う可能性が高い
            return {
                'result': 'phone_mismatch',
                'confidence': 'high',
                'detail': f'特商法に会社名はあるが電話番号が不一致: {candidate_url}',
                'tokutei_url': candidate_url,
            }

    # ── STEP 4: base_url（メインサイト）で電話を確認 ────────
    if base_url_val and base_url_val != lp_url:
        time.sleep(_REQUEST_INTERVAL)
        base_text = _fetch(base_url_val)
        if base_text and _phone_in_text(base_text, phone):
            return {
                'result': 'base_ok',
                'confidence': 'medium',
                'detail': f'メインサイトに電話番号を確認: {base_url_val}',
                'tokutei_url': tokutei_found_url,
            }

    # ── STEP 5: NTAで会社名の実在確認 ───────────────────────
    nta_ok = _nta_verified(company)
    if nta_ok:
        return {
            'result': 'nta_only',
            'confidence': 'low',
            'detail': 'NTAで法人確認済み（電話の紐づけは未確認）',
            'tokutei_url': None,
        }

    # ── STEP 6: 特商法が見つからなかった ────────────────────
    detail = (
        f'特商法ページ未発見（候補{len(tokutei_candidates)}件試行、'
        f'確認済み{len(checked_tokutei)}件）'
    )
    return {
        'result': 'not_found',
        'confidence': 'unknown',
        'detail': detail,
        'tokutei_url': None,
    }


# ─────────────────────────────────────────────────────────
# メイン計測
# ─────────────────────────────────────────────────────────

# 整合率 = 高信頼判定（tokutei_ok + base_ok）/ チェック件数
_OK_RESULTS = {'tokutei_ok', 'base_ok'}

def run_check(
    sample_size: int = 50,
    days: int = 30,
    verbose: bool = False,
) -> dict:
    """
    直近N日のDBエントリからサンプリングして整合率を計測する。

    Returns:
        {
            'timestamp': str,
            'sample_size': int,
            'checked': int,
            'ok_count': int,
            'seigoritsu': float,    # 0.0–1.0 (tokutei_ok + base_ok の割合)
            'breakdown': {
                'tokutei_ok': int,
                'base_ok': int,
                'nta_only': int,
                'phone_mismatch': int,
                'not_found': int,
                'lp_dead': int,
            },
            'failures': [...],      # 問題レコードのサンプル
        }
    """
    import sqlite3

    db_path = BASE_DIR / 'companies.db'
    if not db_path.exists():
        return {'error': 'DB not found', 'timestamp': datetime.now().isoformat(), 'seigoritsu': None}

    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            '''
            SELECT id, company_name, phone, lp_url, base_url, ad_sources
            FROM companies
            WHERE lp_url  IS NOT NULL AND lp_url  != ''
              AND phone   IS NOT NULL AND phone   != ''
              AND company_name IS NOT NULL AND company_name != ''
              AND found_date >= ?
            ORDER BY RANDOM()
            LIMIT ?
            ''',
            (since, sample_size),
        ).fetchall()
        conn.close()
    except Exception as e:
        return {'error': str(e), 'timestamp': datetime.now().isoformat(), 'seigoritsu': None}

    if not rows:
        return {
            'timestamp': datetime.now().isoformat(),
            'sample_size': 0, 'checked': 0, 'ok_count': 0,
            'seigoritsu': None,
            'breakdown': {k: 0 for k in ('tokutei_ok','base_ok','nta_only','phone_mismatch','not_found','lp_dead')},
            'failures': [],
            'note': '対象レコードなし',
        }

    breakdown = {k: 0 for k in ('tokutei_ok','base_ok','nta_only','phone_mismatch','not_found','lp_dead')}
    failures: list[dict] = []
    checked = 0

    for i, row in enumerate(rows):
        company = (row['company_name'] or '').strip()
        phone   = (row['phone'] or '').strip()

        if verbose:
            print(f'  [{i+1}/{len(rows)}] {company[:28]} | {phone}', flush=True)

        r = _check_one(dict(row), verbose=verbose)
        result_key = r['result']
        breakdown[result_key] = breakdown.get(result_key, 0) + 1
        checked += 1

        if result_key not in _OK_RESULTS:
            failures.append({
                'id': row['id'],
                'company_name': company,
                'phone': phone,
                'lp_url': row['lp_url'],
                'ad_source': row['ad_sources'] or '',
                'result': result_key,
                'confidence': r['confidence'],
                'detail': r['detail'],
                'tokutei_url': r['tokutei_url'],
            })

    ok_count = breakdown['tokutei_ok'] + breakdown['base_ok']
    seigoritsu = ok_count / checked if checked > 0 else 0.0

    return {
        'timestamp': datetime.now().isoformat(),
        'sample_size': len(rows),
        'checked': checked,
        'ok_count': ok_count,
        'seigoritsu': round(seigoritsu, 4),
        'breakdown': breakdown,
        'failures': failures[:20],
    }


# ─────────────────────────────────────────────────────────
# ログ・フォーマット
# ─────────────────────────────────────────────────────────

def save_result(result: dict) -> None:
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open('a', encoding='utf-8') as f:
        f.write(json.dumps(result, ensure_ascii=False) + '\n')


def load_history(n: int = 10) -> list[dict]:
    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text(encoding='utf-8').strip().splitlines()
    results: list[dict] = []
    for line in reversed(lines):
        try:
            results.append(json.loads(line))
            if len(results) >= n:
                break
        except Exception:
            continue
    return results


def format_report(result: dict, history: list[dict] | None = None) -> str:
    ts = result.get('timestamp', '')[:16]
    s  = result.get('seigoritsu')
    if s is None:
        return f'⚪ 整合率: 計測不可  [{ts}]  {result.get("note", result.get("error", ""))}'

    pct  = f'{s * 100:.1f}%'
    ok   = result.get('ok_count', 0)
    chk  = result.get('checked', 0)
    bd   = result.get('breakdown', {})
    icon = '🟢' if s >= 0.7 else '🟡' if s >= 0.4 else '🔴'

    trend = ''
    if history and len(history) >= 2:
        prev = history[1].get('seigoritsu')
        if prev is not None:
            delta = (s - prev) * 100
            trend = f'  ({delta:+.1f}pp vs 前回)'

    lines = [
        f'{icon} 整合率: {pct}  ({ok}/{chk}件OK)  [{ts}]{trend}',
        f'  特商法一致={bd.get("tokutei_ok",0)} | メインサイト一致={bd.get("base_ok",0)} | '
        f'NTA確認のみ={bd.get("nta_only",0)} | 電話不一致={bd.get("phone_mismatch",0)} | '
        f'確認不可={bd.get("not_found",0)} | LP死亡={bd.get("lp_dead",0)}',
    ]
    return '\n'.join(lines)


# ─────────────────────────────────────────────────────────
# 単体実行
# ─────────────────────────────────────────────────────────

if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    parser = argparse.ArgumentParser(description='整合率チェッカー v2（特商法ベース）')
    parser.add_argument('--sample', type=int, default=30, help='サンプル数 (default: 30)')
    parser.add_argument('--days',   type=int, default=30, help='対象日数 (default: 30)')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--no-save', action='store_true')
    args = parser.parse_args()

    print(f'整合率チェック開始（特商法ベース、{args.sample}件サンプル）...', flush=True)
    result = run_check(sample_size=args.sample, days=args.days, verbose=args.verbose)

    if 'error' in result:
        print(f'エラー: {result["error"]}')
        sys.exit(1)

    history = load_history(5)
    print(format_report(result, history))

    if not args.no_save:
        save_result(result)
        print(f'→ ログ保存: {LOG_PATH}')

    failures = result.get('failures', [])
    if failures:
        print(f'\n問題サンプル（{min(len(failures),5)}件）:')
        for f in failures[:5]:
            print(f'  [{f["result"]:18s}] {f["company_name"][:25]} | {f["phone"]}')
            print(f'    {f["detail"][:80]}')
