# 電話番号の信頼度スコアリング（オーナー最重要ポイント③: 架電可能性）
#
# 「この番号にかけたら本当にその会社に繋がるか」を 0-100 で定量化する。
# DBの phone_confidence カラムに保存し、整合率の定点観測・閾値運用に使う。
# Sheetsの列構成は変えない（ランク降格を通じて間接的に反映される）。
from __future__ import annotations

# phone_source 別の基礎スコア。
# 特商法ページは「会社名と電話番号が同一ページ・同一主体で並ぶ」ため最も信頼できる。
# phone_source の '_hp' サフィックスは「公式HP側でも同番号を確認済み」を意味し、
# 複数ソース裏付けとして加点する（例: tokutei_hp, lp_hp, crawl_playwright_hp）。
_SOURCE_BASE = [
    ('tokutei', 90),            # 特商法ページ（lp_tokutei_section 含む）
    ('json_ld', 80),            # 構造化データ（サイト運営者が自己申告）
    ('crawl', 70),              # 会社概要等の深堀りクロール
    ('lp', 60),                 # LP直接記載
    ('meta', 50),               # Meta広告ページ記載
    ('hp', 70),                 # 公式HP（単独）。lp_hp等を先に拾わないよう最後に置く
]
_DEFAULT_BASE = 50
_HP_CONFIRM_BONUS = 10

# pref_match（NTA登記都道府県×市外局番）による補正
_PREF_ADJUST = {'match': 10, 'near': 5, 'unknown': 0, 'mismatch': -25}

_RANK_ORDER = ['S', 'A', 'B', 'C']


def calc_phone_confidence(phone_source: str, pref_match: str) -> int:
    """電話番号の信頼度を 0-100 で返す。

    - phone_source の由来（特商法 > 構造化データ > クロール > LP）を基礎点とする
    - 公式HP側でも同番号が確認されていれば（'_hp'）裏付けありとして加点
    - NTA登記都道府県と市外局番の整合で加減点
    """
    src = (phone_source or '').lower()
    hp_confirmed = src.endswith('_hp')
    core = src[:-3] if hp_confirmed else src

    base = _DEFAULT_BASE
    for key, score in _SOURCE_BASE:
        if key in core:
            base = score
            break

    bonus = _HP_CONFIRM_BONUS if hp_confirmed else 0
    score = base + bonus + _PREF_ADJUST.get(pref_match or 'unknown', 0)
    return max(0, min(100, score))


def demote_rank(rank: str) -> str:
    """ランクを1段階降格する（S→A→B→C、Cはそのまま）。
    pref_match=mismatch（登記住所と電話の地域が遠隔）のレコードに適用し、
    テレアポ優先度を下げる。削除はしない（番号自体は有効な可能性があるため）。"""
    r = (rank or 'C').strip().upper()
    if r not in _RANK_ORDER:
        return rank or 'C'
    idx = min(_RANK_ORDER.index(r) + 1, len(_RANK_ORDER) - 1)
    return _RANK_ORDER[idx]
