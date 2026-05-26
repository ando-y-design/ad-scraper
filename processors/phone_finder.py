import re
import unicodedata

from processors.normalizer import normalize_phone, is_valid_phone

# 電話番号らしき文字列を広めに取り、後で検証する
# 全角括弧（）・全角スラッシュ／・半角スラッシュ/も許容
# 例: （03）1234-5678 / 0120／000／000
_RAW_PHONE_PATTERN = re.compile(
    r'0[\d\s\-\(\)（）\.ー－・／/]{8,16}'
)

_PRIORITY_PREFIXES = [
    ('mobile',         re.compile(r'^0[789]0\d{8}$')),      # 携帯 070/080/090 を先に捕捉
    ('ip',             re.compile(r'^050\d{8}$')),
    ('toll_free',      re.compile(r'^0(120|800|570|990)\d{6,7}$')),
    ('landline_tokyo', re.compile(r'^0[36]\d{8}$')),        # 03（東京）・06（大阪）のみ
    ('landline',       re.compile(r'^0[1-9]\d{8}$')),       # 一般固定 10桁
    ('landline_11',    re.compile(r'^0[1-9]\d{9}$')),       # 11桁固定
]

# 戦略別優先順位
# SME（中小・ベンチャー）: 携帯→固定→IP→フリーダイヤル（決裁権者直結）
_PRIORITY_SME = ['mobile', 'landline_tokyo', 'landline', 'ip', 'landline_11', 'toll_free']
# Enterprise（中堅・大手）: 固定→IP→携帯→フリーダイヤル（組織攻略）
_PRIORITY_ENTERPRISE = ['landline_tokyo', 'landline', 'ip', 'mobile', 'landline_11', 'toll_free']

_strategy: str = 'sme'


def set_phone_strategy(strategy: str) -> None:
    """main.pyの起動時に呼ぶ。'sme' or 'enterprise'"""
    global _strategy
    _strategy = strategy if strategy in ('sme', 'enterprise') else 'sme'


def get_priority_order() -> list[str]:
    return _PRIORITY_ENTERPRISE if _strategy == 'enterprise' else _PRIORITY_SME


def is_freephone(phone: str) -> bool:
    """フリーダイヤル（0120/0800/0570/0990）かどうかを判定する。
    ハイフン付きフォーマット・生桁数字の両方に対応。"""
    if not phone:
        return False
    digits = re.sub(r'\D', '', phone)
    return digits[:4] in ('0120', '0800', '0570', '0990')


def _normalize_text_for_phone(text: str) -> str:
    text = unicodedata.normalize('NFKC', text)
    text = text.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    text = re.sub(
        r'(?:F\.?A\.?X|Fax|fax|ファクス|ファックス|ＦＡＸ)\s*[：:＊\*]?\s*[\d\s\-\(\)\.]{7,18}',
        ' ', text, flags=re.IGNORECASE
    )
    # 国際電話形式 +81-3-xxxx → 03-xxxx
    # +81 (0)3-xxxx / +81-3-xxxx / +81 3 xxxx など
    def _convert_intl(m):
        rest = re.sub(r'[\s\-\(\)]', '', m.group(1))
        # +81 の後の最初の 0 を除く（+81-03-... は +81-3-... と同義）
        if rest.startswith('0'):
            rest = rest[1:]
        return '0' + rest
    text = re.sub(r'\+81[\s\-\(\)]0?(\d[\d\s\-\(\)\.]{7,16})', _convert_intl, text)
    return text


_TEL_URL_RE = re.compile(r'^tel:[+\d\-\(\) ]{5,20}$', re.IGNORECASE)


def extract_all_phones(text: str) -> list[str]:
    """テキストから有効な電話番号を全て抽出し、現在の戦略順で返す。"""
    if not text:
        return []

    # tel: URLスキーム（href値として渡された場合のみ処理）
    stripped = text.strip()
    if _TEL_URL_RE.match(stripped):
        inner = stripped[4:].strip().replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
        if inner.startswith('+81'):
            inner = '0' + inner[3:]
        if inner and inner[0] == '0':
            phone = normalize_phone(inner)
            if is_valid_phone(phone):
                return [phone]
        return []

    text = _normalize_text_for_phone(text)

    all_ptypes = [p for p, _ in _PRIORITY_PREFIXES]
    found: dict[str, list[str]] = {k: [] for k in all_ptypes}

    for raw_match in _RAW_PHONE_PATTERN.finditer(text):
        digits = re.sub(r'\D', '', raw_match.group(0))
        for ptype, pattern in _PRIORITY_PREFIXES:
            if pattern.match(digits):
                if digits not in found[ptype]:
                    found[ptype].append(digits)
                break

    result = []
    seen: set[str] = set()
    for ptype in get_priority_order():
        for digits in found.get(ptype, []):
            formatted = normalize_phone(digits)
            if is_valid_phone(formatted) and formatted not in seen:
                seen.add(formatted)
                result.append(formatted)
    return result


def extract_phone(text: str) -> str | None:
    if not text:
        return None

    # tel: URL スキームが直接渡された場合（href="tel:03-xxxx-xxxx" / "tel:+81-3-..."）
    stripped = text.strip()
    if _TEL_URL_RE.match(stripped):
        inner = stripped[4:].strip().replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
        if inner.startswith('+81'):
            inner = '0' + inner[3:]  # 国際番号 +81312345678 → 0312345678
        if inner and inner[0] == '0':
            return normalize_phone(inner)

    # _normalize_text_for_phone で全角変換・FAX除去・+81変換を一括処理
    text = _normalize_text_for_phone(text)

    found: dict[str, str] = {}

    for raw_match in _RAW_PHONE_PATTERN.finditer(text):
        raw = raw_match.group(0)
        # 数字だけ抽出
        digits = re.sub(r'\D', '', raw)

        for ptype, pattern in _PRIORITY_PREFIXES:
            if pattern.match(digits):
                if ptype not in found:
                    found[ptype] = digits
                break

    for ptype in get_priority_order():
        if ptype in found:
            formatted = normalize_phone(found[ptype])
            if is_valid_phone(formatted):
                return formatted

    return None
