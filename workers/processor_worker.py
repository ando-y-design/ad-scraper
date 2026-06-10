from __future__ import annotations
from typing import Optional
"""情報取得・解析ワーカー（並列処理）"""
import concurrent.futures
import logging
import queue
import re as _re_industry
import time
from datetime import datetime

from processors.company_finder import find_company_info
from processors.normalizer import get_base_domain, normalize_company, normalize_url
from storage.database import (
    get_connection, is_duplicate, update_ad_sources, append_keyword, get_competitors,
)
from self_repair.diagnostics import get_diagnostics

from state import (
    config, shutdown_event, lp_queue, result_queue, beat,
)

# キーワードから業界を分類するルール（新しい業界は末尾に追記するだけ）
_INDUSTRY_RULES: list[tuple[list[str], str]] = [
    (["外壁塗装", "屋根塗装", "外壁・屋根"],   "外壁・屋根塗装"),
    (["塗装"],                                  "塗装"),
    (["リフォーム", "リノベーション"],           "住宅リフォーム"),
    (["引越", "引っ越し", "引越し"],             "引越"),
    (["不動産", "賃貸", "売買", "物件", "査定"], "不動産"),
    (["保険"],                                  "保険"),
    (["弁護士", "法律事務所"],                  "法律"),
    (["税理士", "会計"],                        "税理士"),
    (["脱毛", "エステ"],                        "美容・脱毛"),
    (["歯科", "歯医者", "デンタル"],             "歯科"),
    (["クリニック", "病院", "医院", "整形外科",
      "皮膚科", "眼科", "内科"],                "医療"),
    (["害虫", "駆除"],                          "害虫駆除"),
    (["給湯器", "エアコン", "設備工事"],         "設備工事"),
    (["解体", "撤去"],                          "解体"),
    (["防水", "雨漏り"],                        "防水"),
    (["葬儀", "葬祭", "セレモニー"],             "葬儀"),
    (["ペット", "トリミング"],                   "ペット"),
    (["広告代理店", "PR会社", "広告"],           "広告"),
    (["コインランドリー"],                       "コインランドリー"),
    (["結婚", "婚活", "マリッジ"],               "婚活"),
    (["学院", "塾", "スクール", "予備校"],        "教育"),
    (["整体", "整骨", "鍼灸"],                   "整体・整骨"),
    (["買取", "リサイクル"],                     "買取"),
    (["太陽光", "蓄電池"],                       "太陽光"),
]


def classify_industry(keyword: str) -> str:
    """キーワードから業界ラベルを返す。どれにも該当しなければ空文字。"""
    for keywords, label in _INDUSTRY_RULES:
        for kw in keywords:
            if kw in keyword:
                return label
    return ""


def is_blocked_domain(domain: str) -> bool:
    blocked = config.get('filters', {}).get('blocked_domains', [])
    domain = domain.lower()
    return any(domain == b or domain.endswith('.' + b) for b in blocked)


# ─────────────────────────────────────────────
# THREAD 2: 情報取得・解析（並列処理）
# ─────────────────────────────────────────────
_PROCESSOR_WORKERS = 1  # LP取得を逐次処理（PC負荷軽減）


def _process_one_lp(item: dict, conn=None) -> Optional[dict]:
    """1件のLPを処理して結果dictを返す。失敗時はNone。
    conn は使用しない（後方互換のため残す）。
    ThreadPoolExecutor から呼ばれるため、スレッドローカル接続を使う。
    """
    conn = get_connection()  # 各ワーカースレッドのスレッドローカル接続を取得
    lp_url = item['lp_url']
    source = item['source']
    keyword = item['keyword']
    meta_company = item.get('meta_company')
    area_name = item.get('area_name')

    if not lp_url or not lp_url.startswith('http'):
        return None

    base_domain = get_base_domain(lp_url)

    if is_blocked_domain(base_domain):
        logging.debug(f'[Processor] ブロック済みドメイン: {base_domain}')
        return None

    # テスト/デモ/サンプルLPを除外（プレースホルダーデータが多い）
    import re as _re
    if _re.search(r'[/?&=#](test|demo|sample|preview|staging|sandbox|dummy)[/?&#=]|'
                  r'/test$|/demo$|/sample$|/preview$|/staging$',
                  lp_url, flags=_re.IGNORECASE):
        logging.debug(f'[Processor] テスト/デモURLをスキップ: {lp_url}')
        return None

    # 記事/コラム/ブログページを除外（会社情報が取れない）
    if _re.search(
        r'/(column|columns|article|articles|blog|blogs|media|news|topics|'
        r'knowledge|magazine|guide|howto|faq|qa|help|feature|special|'
        r'column\d|media\d|note|journal|report|review|ranking|compare)/',
        lp_url, flags=_re.IGNORECASE
    ):
        logging.debug(f'[Processor] 記事/コラムURLをスキップ: {lp_url}')
        return None

    if is_duplicate(conn, '', base_domain, ''):
        existing = conn.execute(
            'SELECT normalized_name FROM companies WHERE base_url=?', (base_domain,)
        ).fetchone()
        if existing:
            update_ad_sources(conn, existing['normalized_name'], source)
            append_keyword(conn, existing['normalized_name'], keyword)
        return None

    serp_phone = item.get('serp_phone')

    nta_key = config.get('nta_api_key', '')
    result = find_company_info(lp_url, meta_company, nta_api_key=nta_key)
    if len(result) == 6:
        company_name, phone, phones_str, contact_name, lp_headline, phone_source = result
    else:
        company_name, phone, phones_str, contact_name, lp_headline = result
        phone_source = ''
    beat('processor')

    # 法人番号と正式法人名を取得（NTA APIキー設定済みの場合のみ）
    corporate_number = ''
    nta_errored = False
    if company_name and nta_key:
        from processors.legal_name_resolver import lookup_corporate_number
        corp_num, official_name = lookup_corporate_number(company_name, nta_key)
        if corp_num == '__NTA_ERROR__':
            nta_errored = True
        else:
            corporate_number = corp_num or ''
            if official_name and official_name != company_name:
                logging.info(f'[Processor] NTA正式名に補正: "{company_name}" → "{official_name}"')
                company_name = official_name

    # SERP コール表示の電話番号をフォールバックとして使用
    if not phone and serp_phone:
        phone = serp_phone
        logging.debug(f'[Processor] SERP電話番号をフォールバック採用: {phone}')

    get_diagnostics().record_extraction(bool(company_name))

    if not company_name or not phone:
        logging.debug(f'[Processor] 会社名/電話番号取得失敗: {lp_url}')
        return None

    # フリーダイヤル（0120/0800/0570/0990）のみの場合はスキップ
    from processors.phone_finder import is_freephone
    if is_freephone(phone):
        logging.debug(f'[Processor] フリーダイヤルのみのためスキップ: {phone} / {lp_url}')
        return None

    normalized = normalize_company(company_name)

    if is_duplicate(conn, normalized, base_domain, phone):
        existing_src = conn.execute(
            'SELECT ad_sources, normalized_name FROM companies '
            'WHERE normalized_name=? OR base_url=? OR phone=?',
            (normalized, base_domain, phone)
        ).fetchone()
        if existing_src:
            old_sources = existing_src['ad_sources'] or ''
            update_ad_sources(conn, existing_src['normalized_name'], source)
            append_keyword(conn, existing_src['normalized_name'], keyword)
            # 新媒体が追加された場合はランクを再計算してSheets更新イベントを送る
            new_sources = conn.execute(
                'SELECT ad_sources, all_keywords FROM companies WHERE normalized_name=?',
                (existing_src['normalized_name'],)
            ).fetchone()
            if new_sources and existing_src.get('sheet_row'):
                from processors.rank_calculator import calc_rank, _count_sources
                old_rank_src = _count_sources(old_sources)
                new_rank_src = _count_sources(new_sources['ad_sources'] or '')
                if new_rank_src > old_rank_src:
                    seen = conn.execute('SELECT seen_count FROM companies WHERE normalized_name=?', (existing_src['normalized_name'],)).fetchone()
                    new_rank = calc_rank(seen['seen_count'] if seen else 2, new_sources['ad_sources'] or '')
                    try:
                        result_queue.put({
                            '_type': 'rank_update',
                            'sheet_row': existing_src['sheet_row'],
                            'rank': new_rank,
                            'normalized_name': existing_src['normalized_name'],
                        }, timeout=5)
                    except Exception:
                        pass
        return None

    # 同業種 × 同エリアの競合他社名を付与（Sheets表示用）
    competitors = get_competitors(conn, keyword, normalized, area_name=area_name)
    competitors_str = ' / '.join(competitors) if competitors else ''

    from processors.rank_calculator import calc_rank
    rank = calc_rank(1, source)


    return {
        'company_name': company_name.strip(),
        'normalized_name': normalized,
        'lp_url': normalize_url(lp_url),
        'base_url': base_domain,
        'phone': phone,
        'phones': phones_str,
        'phone_source': phone_source,
        'ad_sources': source,
        'keyword': keyword,
        'area_name': area_name,
        'found_date': datetime.now().strftime('%Y-%m-%d'),
        'contact_name': contact_name,
        'lp_headline': lp_headline,
        'competitors': competitors_str,
        'industry': classify_industry(keyword),
        'rank': rank,
        'corporate_number': corporate_number,
        'nta_errored': nta_errored,
    }


def processor_worker():
    logging.info(f'[Processor] 起動 (並列数={_PROCESSOR_WORKERS})')
    beat('processor')

    with concurrent.futures.ThreadPoolExecutor(max_workers=_PROCESSOR_WORKERS) as executor:
        pending: dict[concurrent.futures.Future, dict] = {}

        while not shutdown_event.is_set():
            beat('processor')

            # 完了したfutureを処理
            done = [f for f in list(pending) if f.done()]
            for f in done:
                item = pending.pop(f)
                try:
                    result = f.result()
                    if result:
                        try:
                            result_queue.put(result, timeout=10)
                            logging.info(
                                f'[Processor] 新規取得: {result["company_name"]} / '
                                f'{result["phone"]} ({result["ad_sources"]})'
                            )
                        except queue.Full:
                            logging.warning('[Processor] result_queueが満杯 → DBへ直接保存を試みます')
                            # キューが詰まった場合でもデータを消さずにDBへ直接保存
                            try:
                                from storage.database import get_connection, insert_company
                                _direct_conn = get_connection()
                                inserted = insert_company(_direct_conn, result)
                                if inserted:
                                    logging.info(
                                        f'[Processor] DB直接保存成功: {result["company_name"]}'
                                    )
                                else:
                                    logging.warning(
                                        f'[Processor] DB直接保存スキップ（重複）: {result["company_name"]}'
                                    )
                            except Exception as _e:
                                logging.error(f'[Processor] DB直接保存失敗: {_e}')
                except Exception as e:
                    logging.error(f'[Processor] エラー: {e}', exc_info=True)
                finally:
                    lp_queue.task_done()

            # 空きスロット分だけキューから取り出して並列投入
            while len(pending) < _PROCESSOR_WORKERS:
                try:
                    item = lp_queue.get(timeout=0.2)
                    f = executor.submit(_process_one_lp, item)
                    pending[f] = item
                except queue.Empty:
                    break

            if not pending:
                time.sleep(1)

    logging.info('[Processor] 終了')
