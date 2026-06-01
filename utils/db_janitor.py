from __future__ import annotations
"""
DBゴミデータ自律クリーナー
Watchdogから定期的に呼ばれ、不正な会社名・電話番号レコードを自動削除する。
Claude CLI不要で決定論的に動作する。
"""
import logging
import re
import unicodedata

# 会社名のゴミパターン（先頭一致）
_GARBAGE_NAME_PATTERNS = [
    re.compile(r'^\(C\)', re.IGNORECASE),
    re.compile(r'^©'),
    re.compile(r'^Copyright', re.IGNORECASE),
    re.compile(r'^本利用規約'),
    re.compile(r'^本規約'),
    re.compile(r'^利用規約'),
    re.compile(r'^以下'),
    re.compile(r'^募集代理店'),           # $なし（後続文字があっても除外）
    re.compile(r'^PR\s*[：:]', re.IGNORECASE),
    re.compile(r'^広告\s*[：:]'),
    re.compile(r'^運営者情報'),           # コロンなしも含む
    re.compile(r'^運営会社'),             # コロンなしも含む
    re.compile(r'^販売業者[：:]'),
    re.compile(r'^事業者名[：:]'),
    re.compile(r'^\※'),                  # ※ で始まる不完全な名称
    re.compile(r'^-?\d{4}\s'),           # "-2026 Company" 等の年号プレフィックス
    re.compile(r'^【'),                   # 見出し括弧
    re.compile(r'^「'),                   # 引用括弧
    re.compile(r'^http', re.IGNORECASE),  # URL
    re.compile(r'^www\.', re.IGNORECASE), # URL
    re.compile(r'^Privacy', re.IGNORECASE),
    re.compile(r'^Terms', re.IGNORECASE),
    re.compile(r'^Policy', re.IGNORECASE),
    re.compile(r'^お問い合わせ'),
    re.compile(r'^ショッピング'),
    re.compile(r'^ログイン'),
    re.compile(r'^メニュー'),
    re.compile(r'^企業情報'),
    re.compile(r'^会社情報'),
    re.compile(r'^サービス情報'),
    re.compile(r'^運営情報'),
]

# 会社名に含まれてはいけない部分文字列
_GARBAGE_CONTAINS = [
    '以下「本規約」',
    '（以下',
    '（旧',           # 旧社名注記（例: 株式会社ABC（旧：XYZ））
    'といいます）',
    'この規約',
    'All Rights Reserved',
    'all rights reserved',
    'ドロップシッピング',   # EC仲介サービス（通常BtoCリード）
]

# 法人格なし（Inc./Ltd.等英字も含め全部なければ不正）
_LEGAL_ENTITY_RE = re.compile(
    r'(?:株式会社|有限会社|合同会社|合資会社|合名会社|一般社団法人|公益社団法人|'
    r'一般財団法人|公益財団法人|医療法人|学校法人|弁護士法人|税理士法人|司法書士法人|'
    r'社会福祉法人|NPO法人|特定非営利活動法人|協同組合|農業協同組合|'
    r'Inc\.?|Corp\.?|Ltd\.?|LLC|GmbH|ホールディングス)'
)

# 法人格なし個人事業主・クリニック・事務所等の業種サフィックス
# これらが含まれる場合は法人格がなくても有効な事業者名と判断する
_BUSINESS_TYPE_RE = re.compile(
    r'(?:クリニック|歯科|医院|病院|診療所|整形外科|皮膚科|眼科|内科|外科|耳鼻科|小児科|'
    r'薬局|調剤薬局|'
    r'法律事務所|弁護士事務所|司法書士事務所|行政書士事務所|税理士事務所|'
    r'公認会計士事務所|社会保険労務士事務所|弁理士事務所|'
    r'整体院|整骨院|接骨院|鍼灸院|'
    r'動物病院|ペットショップ|トリミングサロン|'
    r'葬儀社|葬祭|セレモニー|'
    r'保育所|保育園|幼稚園|'
    r'工務店|建設|建築事務所|設計事務所|'
    r'不動産|買取|リフォーム|'
    r'塾|学院|スクール|教室|予備校|'
    r'サロン|エステ|美容室|ヘアサロン|'
    r'商会|商店|'
    r'保険代理店|FP事務所)'
)

# 不完全な会社名（法人格だけ）
_INCOMPLETE_NAME_RE = re.compile(
    r'^(?:株式会社|有限会社|合同会社|合資会社|医療法人|一般社団法人|NPO法人)$'
)

# 有効な電話番号パターン（ハイフンなし10〜11桁）
_VALID_PHONE_RE = re.compile(
    r'^(?:0[1-9]\d{8,9}|0(?:120|800|570|990)\d{6,7}|050\d{8}|0[789]0\d{8})$'
)


def _is_garbage_name(name: str) -> bool:
    if not name:
        return True
    name = name.strip()
    # 長さチェック
    if len(name) < 4 or len(name) > 60:
        return True
    # セパレータ・スローガン系文字
    if '|' in name or '｜' in name or '/' in name or '／' in name:
        return True
    if '･' in name:  # 半角中黒: A･B･C 型スローガンを排除
        return True
    # 句読点・読点・旧社名注記含む文章
    if '。' in name or '、' in name or '（以下' in name or '（旧' in name:
        return True
    # 括弧付き注記（読み・旧名・サービス名等）: （XXX） 形式で3文字以上
    if re.search(r'（.{3,}）', name):
        return True
    # 部分文字列チェック
    for substr in _GARBAGE_CONTAINS:
        if substr in name:
            return True
    # 先頭パターン
    for pat in _GARBAGE_NAME_PATTERNS:
        if pat.search(name):
            return True
    # 不完全な名前
    if _INCOMPLETE_NAME_RE.match(name):
        return True
    # 法人格なし かつ 業種サフィックスもなし → 不正な会社名
    if not _LEGAL_ENTITY_RE.search(name) and not _BUSINESS_TYPE_RE.search(name):
        return True
    return False


def _is_garbage_phone(phone: str) -> bool:
    if not phone:
        return True
    phone = unicodedata.normalize('NFKC', phone)
    digits = re.sub(r'[\-\s\(\)\.・ー－]', '', phone)
    digits = re.sub(r'\D', '', digits)
    return not _VALID_PHONE_RE.match(digits)


def run_db_janitor(conn) -> int:
    """
    DBをスキャンしてゴミレコードを削除する。
    Returns: 削除件数
    """
    try:
        rows = conn.execute(
            'SELECT id, company_name, phone FROM companies'
        ).fetchall()
    except Exception as e:
        logging.error(f'[Janitor] DBスキャン失敗: {e}')
        return 0

    garbage_ids = []
    for row in rows:
        name = row['company_name'] or ''
        phone = row['phone'] or ''
        if _is_garbage_name(name) or _is_garbage_phone(phone):
            garbage_ids.append(row['id'])
            logging.warning(
                f'[Janitor] ゴミデータ検出 ID={row["id"]}: '
                f'"{name}" / {phone}'
            )

    if not garbage_ids:
        logging.debug('[Janitor] ゴミデータなし')
        return 0

    try:
        placeholders = ','.join('?' for _ in garbage_ids)
        conn.execute(
            f'DELETE FROM companies WHERE id IN ({placeholders})',
            garbage_ids
        )
        conn.commit()
        logging.info(f'[Janitor] {len(garbage_ids)}件のゴミデータを自動削除しました')
        return len(garbage_ids)
    except Exception as e:
        logging.error(f'[Janitor] 削除失敗: {e}')
        return 0
