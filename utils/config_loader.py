"""config.json の読み込みユーティリティ。
main.py と watchdog_worker.py の両方から import して使う。
循環import を避けるため state.py や main.py には依存しない。
"""
import json
from pathlib import Path

_BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = _BASE_DIR / 'config.json'


def load_config() -> dict:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)
