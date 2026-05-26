import re
import unicodedata
from urllib.parse import urlparse, urlunparse

import tldextract

LEGAL_SUFFIXES = [
    '株式会社', '有限会社', '合同会社', '合資会社', '合名会社',
    '一般社団法人', '公益社団法人', '一般財団法人', '公益財団法人',
    'NPO法人', '特定非営利活動法人', '医療法人', '学校法人',
    '宗教法人', '弁護士法人', '税理士法人', '司法書士法人',
    '社会福祉法人', '協同組合', '農業協同組合',
    'ホールディングス', 'Holdings', 'HD',
    '(株)', '（株）', '㈱', '(有)', '（有）', '㈲',
    'Inc.', 'Inc', 'Corp.', 'Corp', 'Ltd.', 'Ltd', 'LLC',
    'Co.,Ltd.', 'Co., Ltd.',
]
_SORTED_SUFFIXES = sorted(LEGAL_SUFFIXES, key=len, reverse=True)


def normalize_company(name: str) -> str:
    if not name:
        return ''
    name = unicodedata.normalize('NFKC', name)
    name = name.replace('　', '').replace(' ', '')
    name = re.sub(r'[・\-－―\.\。、]', '', name)
    for suffix in _SORTED_SUFFIXES:
        name = name.replace(suffix, '')
    return name.lower().strip()


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if netloc.startswith('www.'):
            netloc = netloc[4:]
        path = parsed.path.rstrip('/')
        normalized = parsed._replace(
            scheme='https',
            netloc=netloc,
            path=path,
            query='',
            fragment=''
        )
        return urlunparse(normalized)
    except Exception:
        return url


def get_base_domain(url: str) -> str:
    try:
        ext = tldextract.extract(url)
        if ext.domain and ext.suffix:
            return f'{ext.domain}.{ext.suffix}'.lower()
        parsed = urlparse(url)
        return parsed.netloc.lower().replace('www.', '')
    except Exception:
        return ''


# 主要3桁市外局番（政令市・県庁所在地クラス）
_THREE_DIGIT_AREA = {
    '011', '017', '018', '019', '022', '023', '024', '025', '026', '027',
    '028', '029', '042', '043', '044', '045', '046', '047', '048', '049',
    '052', '053', '054', '055', '058', '059', '072', '073', '075', '076',
    '077', '078', '079', '082', '083', '084', '086', '087', '088', '089',
    '092', '093', '095', '096', '097', '098', '099',
}


_VALID_PHONE_RE = re.compile(
    r'^('
    r'0120-\d{3}-\d{3,4}'       # フリーダイヤル 0120
    r'|0800-\d{3}-\d{4}'        # フリーダイヤル 0800
    r'|0570-\d{3}-\d{3,4}'      # ナビダイヤル 0570
    r'|0990-\d{2}-\d{4}'        # 情報料課金 0990（10桁）
    r'|0990-\d{3}-\d{3,4}'      # 情報料課金 0990（11桁）
    r'|(070|080|090)-\d{4}-\d{4}'  # 携帯
    r'|050-\d{4}-\d{4}'         # IP電話
    r'|0[36]-\d{4}-\d{4}'       # 東京03・大阪06（2桁市外局番）
    r'|0\d{2}-\d{3}-\d{4}'      # 3桁市外局番の固定電話
    r'|0\d{3}-\d{2}-\d{4}'      # 4桁市外局番の固定電話
    r')$'
)


def is_valid_phone(phone: str) -> bool:
    if not phone:
        return False
    # 既知のサンプル/プレースホルダー番号
    if re.search(r'03-1234-5678|0120-000-000|000-0000-0000', phone):
        return False
    # 基本フォーマット検証
    if not _VALID_PHONE_RE.match(phone):
        return False
    # 加入者番号（市外局番除く）が全て同じ数字のサンプル番号を弾く
    # 例: 03-0000-0000 / 06-1111-1111 / 0120-000-000 など
    digits = re.sub(r'\D', '', phone)
    if len(digits) >= 8:
        # 先頭の市外局番(2〜4桁)を除いたサブスクライバ部分を取得
        for ac_len in (4, 3, 2):
            subscriber = digits[ac_len:]
            if len(subscriber) >= 6 and len(set(subscriber)) == 1:
                return False
            break  # 最初のac_lenだけ試せば十分
    return True


def normalize_phone(digits: str) -> str:
    digits = re.sub(r'[\s\-\(\)\.ー－]', '', digits)
    digits = unicodedata.normalize('NFKC', digits)
    digits = digits.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    digits = re.sub(r'\D', '', digits)

    if len(digits) == 11 and digits[0] == '0':
        # フリーダイヤル11桁: 0120/0800/0570/0990-XXX-XXXX
        if digits[:4] in ('0120', '0800', '0570', '0990'):
            return f'{digits[:4]}-{digits[4:7]}-{digits[7:]}'
        # 携帯/IP電話: 090/080/070/050-XXXX-XXXX
        return f'{digits[:3]}-{digits[3:7]}-{digits[7:]}'

    if len(digits) == 10:
        # フリーダイヤル等
        if digits[:4] in ('0120', '0800', '0570', '0990'):
            return f'{digits[:4]}-{digits[4:7]}-{digits[7:]}'
        # 携帯/IP（10桁は古い形式）
        if digits[:3] in ('090', '080', '070', '050'):
            return f'{digits[:3]}-{digits[3:7]}-{digits[7:]}'
        # 東京・大阪の2桁市外局番
        if digits[:2] in ('03', '06'):
            return f'{digits[:2]}-{digits[2:6]}-{digits[6:]}'
        # 主要3桁市外局番
        if digits[:3] in _THREE_DIGIT_AREA:
            return f'{digits[:3]}-{digits[3:6]}-{digits[6:]}'
        # 4桁市外局番 (0476-xx-xxxx 等) — 日本標準は2+4分割
        return f'{digits[:4]}-{digits[4:6]}-{digits[6:]}'

    return digits
