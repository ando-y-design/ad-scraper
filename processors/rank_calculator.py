"""
ランク算出モジュール
媒体数 × 収集回数でS/A/B/Cランクを算出

S: 3媒体以上 または 累計5回以上
A: 2媒体以上 または 累計3回以上
B: 累計2回
C: 初回（1回のみ）
"""


def calc_rank(ad_sources: str, seen_count: int) -> str:
    """
    ad_sources: 'Google', 'Yahoo', 'Meta' などをカンマ or スラッシュ区切りで持つ文字列
    seen_count: この会社の累計収集回数（DBから取得）
    """
    if not ad_sources:
        source_count = 1
    else:
        sources = set()
        for s in ad_sources.replace('/', ',').replace('|', ',').split(','):
            s = s.strip()
            if s:
                sources.add(s)
        source_count = max(len(sources), 1)

    if source_count >= 3 or seen_count >= 5:
        return 'S'
    elif source_count >= 2 or seen_count >= 3:
        return 'A'
    elif seen_count >= 2:
        return 'B'
    else:
        return 'C'
