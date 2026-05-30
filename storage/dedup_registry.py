"""
3アカウント共通の重複チェック台帳
Google Sheetsをマスター台帳として使い、アカウント間の会社重複を防ぐ

動作:
- 起動時に台帳を全ロードしてメモリキャッシュ
- 書き込み前にキャッシュで重複チェック（高速）
- 新規なら台帳に追記＋キャッシュ更新
- 5分ごとに台帳を再同期（他アカウントの追加を反映）
"""
import logging
import threading
import time
from datetime import datetime

import gspread

DEDUP_SHEET_ID = '1NfokaW3Y6Inkzo2ux55Oqk7qTubz1zNMkz0LJNPZp10'
_SYNC_INTERVAL = 300  # 5分ごとに再同期


class DedupRegistry:
    def __init__(self, sheets_client, account_id: str = 'A'):
        self._client = sheets_client
        self._account_id = account_id
        self._phones: set[str] = set()
        self._lock = threading.Lock()
        self._ws = None
        self._last_sync = 0
        self._enabled = False
        self._init()

    def _init(self):
        try:
            ss = self._client.open_by_key(DEDUP_SHEET_ID)
            self._ws = ss.sheet1
            self._load()
            self._enabled = True
            logging.info(f'[Dedup] 台帳接続完了: {len(self._phones)}件読み込み')
        except Exception as e:
            logging.warning(f'[Dedup] 台帳接続失敗（重複チェック無効）: {e}')

    def _load(self):
        try:
            rows = self._ws.get_all_values()
            phones = {r[0].strip() for r in rows[1:] if r and r[0].strip()}
            with self._lock:
                self._phones = phones
            self._last_sync = time.time()
        except Exception as e:
            logging.warning(f'[Dedup] 台帳読み込み失敗: {e}')

    def _maybe_sync(self):
        if time.time() - self._last_sync > _SYNC_INTERVAL:
            self._load()

    def is_duplicate(self, phone: str) -> bool:
        if not self._enabled or not phone:
            return False
        self._maybe_sync()
        normalized = phone.replace('-', '').replace('(', '').replace(')', '').strip()
        with self._lock:
            return normalized in self._phones or phone in self._phones

    def register(self, phone: str, company_name: str) -> bool:
        """
        新規登録。重複していればFalse、登録成功したらTrue。
        """
        if not self._enabled or not phone:
            return True  # 無効時は常にOK

        self._maybe_sync()
        normalized = phone.replace('-', '').replace('(', '').replace(')', '').strip()

        with self._lock:
            if normalized in self._phones or phone in self._phones:
                return False
            self._phones.add(normalized)
            self._phones.add(phone)

        try:
            self._ws.append_row(
                [phone, company_name, self._account_id, datetime.now().strftime('%Y-%m-%d %H:%M')],
                value_input_option='USER_ENTERED'
            )
        except Exception as e:
            logging.warning(f'[Dedup] 台帳追記失敗: {e}')

        return True


_registry: DedupRegistry | None = None


def init_registry(sheets_client, account_id: str = 'A'):
    global _registry
    _registry = DedupRegistry(sheets_client, account_id)


def check_and_register(phone: str, company_name: str) -> bool:
    """True=書き込みOK、False=重複スキップ"""
    if _registry is None:
        return True
    return _registry.register(phone, company_name)
