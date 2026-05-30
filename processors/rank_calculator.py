"""
ランク算出モジュール
広告費規模をシグナルから推定してS/A/B/Cランクを付与

スコア方式:
  URL系 (追加フェッチなし)
    gclid/yclid/fbclid 各1件 → +2pt
    /lp/ 専用パス or UTM 5個以上 → +1pt
  キーワード系
    高CPC業種 → +3pt
    中CPC業種 → +2pt
  HTML系 (既フェッチ済みのsoupを使用)
    GTM 存在 → +2pt
    FB Pixel 存在 → +1pt
    フォーム or CTAボタン 3個以上 → +1pt

合計: S=8+, A=5-7, B=3-4, C=0-2
"""
import re

# ── キーワードCPCティア定義 ──────────────────────────────────
# 高CPC（1クリック数千円以上が相場）
_HIGH_CPC_KEYWORDS = {
    'インプラント', 'AGA', 'AGA治療', 'AGA クリニック', '薄毛 治療', '植毛',
    '不動産投資', '投資用マンション', 'M&A', '事業承継',
    'ビジネスローン', 'ファクタリング', '資金調達',
    '二重整形', '脂肪吸引', '豊胸', '輪郭形成', '鼻整形',
    '医療脱毛', 'ヒアルロン酸', 'ダーマペン', 'ピコレーザー',
    '弁護士', '離婚 弁護士', '交通事故 弁護士', '過払い金', '債務整理',
    '税理士', '顧問弁護士',
    'ED 治療', 'ED クリニック',
    '矯正歯科', 'マウスピース矯正',
}

# 中CPC（数百〜千円程度）
_MID_CPC_KEYWORDS = {
    '整骨院', '接骨院', '整体', '骨盤矯正', '鍼灸',
    '外壁塗装', '屋根工事', 'リフォーム', '太陽光発電', '蓄電池',
    '結婚相談所', '婚活', 'マッチングアプリ',
    '美容クリニック', 'ホワイトニング', '脱毛サロン',
    '不動産 売却', '不動産 買取', '任意売却',
    'パーソナルジム', 'パーソナルトレーニング',
    '葬儀', '家族葬',
    'プログラミングスクール', '英会話スクール',
    '補助金', '助成金',
    '車 買取', '廃車',
    '給湯器', 'エコキュート',
    'ネイルサロン', 'まつ毛エクステ',
}


def _keyword_score(keyword: str) -> int:
    if not keyword:
        return 0
    for kw in _HIGH_CPC_KEYWORDS:
        if kw in keyword:
            return 3
    for kw in _MID_CPC_KEYWORDS:
        if kw in keyword:
            return 2
    return 0


def _url_score(lp_url: str) -> int:
    if not lp_url:
        return 0
    score = 0
    # クリックIDの有無（1つあたり+2）
    if 'gclid=' in lp_url:
        score += 2
    if 'yclid=' in lp_url:
        score += 2
    if 'fbclid=' in lp_url:
        score += 2
    # LP専用パス or UTMパラメータ5個以上
    has_lp_path = bool(re.search(r'/lp[/_\-]|/lp$|/lp\?', lp_url, re.IGNORECASE))
    utm_count = lp_url.count('utm_')
    if has_lp_path or utm_count >= 5:
        score += 1
    return score


def _html_score(soup) -> int:
    if soup is None:
        return 0
    score = 0
    try:
        html_str = str(soup)
        # GTM
        if 'googletagmanager.com' in html_str or 'GTM-' in html_str:
            score += 2
        # Facebook Pixel
        if 'facebook.com/tr' in html_str or 'fbq(' in html_str or 'connect.facebook.net' in html_str:
            score += 1
        # フォーム or CTAボタン 3個以上
        forms = soup.find_all('form')
        buttons = soup.find_all('button')
        # type=submitのinput or CTAっぽいボタン
        cta_inputs = soup.find_all('input', {'type': 'submit'})
        cta_anchors = [a for a in soup.find_all('a') if a.get('class') and
                       any('btn' in c.lower() or 'button' in c.lower() or 'cta' in c.lower()
                           for c in a.get('class', []))]
        cta_total = len(forms) + len(buttons) + len(cta_inputs) + len(cta_anchors)
        if cta_total >= 3:
            score += 1
    except Exception:
        pass
    return score


def extract_ad_signals(lp_url: str, soup=None) -> dict:
    """URL と LP soup からシグナルを抽出して dict で返す"""
    return {
        'url_score': _url_score(lp_url),
        'html_score': _html_score(soup),
        'has_gtm': soup is not None and ('googletagmanager.com' in str(soup) or 'GTM-' in str(soup)),
    }


def calc_rank(ad_sources: str = '', seen_count: int = 1,
              all_keywords: str = '', keyword: str = '',
              lp_url: str = '', ad_signals: dict | None = None) -> str:
    """
    各シグナルのスコアを合算してランクを返す
    """
    score = 0

    # キーワードCPCティア
    kw = keyword or (all_keywords.split(',')[0].strip() if all_keywords else '')
    score += _keyword_score(kw)

    # URL・HTML系シグナル（ad_signalsがあればそれを優先、なければlp_urlから直接計算）
    if ad_signals:
        score += ad_signals.get('url_score', 0)
        score += ad_signals.get('html_score', 0)
    elif lp_url:
        score += _url_score(lp_url)

    if score >= 8:
        return 'S'
    elif score >= 5:
        return 'A'
    elif score >= 3:
        return 'B'
    else:
        return 'C'
