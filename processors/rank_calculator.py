from __future__ import annotations


def _count_sources(ad_sources: str) -> int:
    return len(set((ad_sources or "").split(",")) - {""})



def calc_rank(seen_count: int, ad_sources: str) -> str:
    """
    媒体数 × 収集回数でSABCランクを算出する。

    S: 3媒体以上 or 累計5回以上
    A: 2媒体以上 or 累計3回以上
    B: 累計2回
    C: 初回（1回のみ）
    """
    source_count = len(set((ad_sources or "").split(",")) - {""})
    if source_count >= 3 or seen_count >= 5:
        return "S"
    if source_count >= 2 or seen_count >= 3:
        return "A"
    if seen_count >= 2:
        return "B"
    return "C"
