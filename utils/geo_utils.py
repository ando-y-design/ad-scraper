import logging
import random


def set_area_geolocation(context, area: dict) -> None:
    """ブラウザコンテキストのGPS座標をエリア座標に変更する。
    Google/YahooのブラウザGeolocation APIに反映される。
    """
    try:
        context.grant_permissions(['geolocation'])
        context.set_geolocation({
            'latitude': area['lat'],
            'longitude': area['lng'],
            'accuracy': random.uniform(50, 200),
        })
        logging.debug(f'[Geo] ジオロケーション設定: {area["name"]} ({area["lat"]}, {area["lng"]})')
    except Exception as e:
        logging.debug(f'[Geo] ジオロケーション設定失敗（続行）: {e}')
