import logging
import threading
import time
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

HEADERS = ['会社名', 'LP URL', '電話番号', '取得日']


def get_sheets_client(credentials_path: str):
    creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
    return gspread.authorize(creds)


def get_worksheet(client, sheet_id: str):
    spreadsheet = client.open_by_key(sheet_id)
    return spreadsheet.sheet1


def setup_sheet(spreadsheet, worksheet):
    sheet_id = worksheet.id

    worksheet.update('A1:D1', [HEADERS])

    col_widths = [
        (0, 1, 180),   # A: 会社名
        (1, 2, 280),   # B: LP URL
        (2, 3, 130),   # C: 電話番号
        (3, 4, 100),   # D: 取得日
    ]
    dim_requests = [
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": s, "endIndex": e,
                },
                "properties": {"pixelSize": px},
                "fields": "pixelSize",
            }
        }
        for s, e, px in col_widths
    ]

    requests_body = {
        "requests": [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0, "endRowIndex": 1
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {
                                "red": 0.26, "green": 0.52, "blue": 0.96
                            },
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": {
                                    "red": 1.0, "green": 1.0, "blue": 1.0
                                }
                            }
                        }
                    },
                    "fields": "userEnteredFormat"
                }
            },
            *dim_requests,
        ]
    }
    spreadsheet.batch_update(requests_body)
    logging.info('スプレッドシート初期書式設定完了')


class SheetsWriter:
    def __init__(self, worksheet, batch_size: int = 50, batch_timeout: int = 300,
                 shutdown_event: threading.Event | None = None,
                 heartbeat_callback=None):
        self.worksheet = worksheet
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout
        self._shutdown = shutdown_event
        self._heartbeat = heartbeat_callback
        self._batch: list[tuple[str, list]] = []
        self._last_flush = datetime.now()
        self._next_row = self._get_next_row()

    def _get_next_row(self) -> int:
        values = self.worksheet.get_all_values()
        return max(len(values) + 1, 2)

    def sync_headers(self):
        """ヘッダー行が HEADERS と一致しない場合のみ更新する。
        旧8列フォーマット（D-G列あり）が検出された場合はその列を削除してから更新する。
        """
        try:
            current = self.worksheet.row_values(1)
            if current == HEADERS:
                return

            # 旧フォーマット（D-G列）からの移行: D-G列を一括削除
            old_extra_cols = {'担当者名', '出稿KW（全て）', '競合他社', '広告ソース'}
            if old_extra_cols & set(current):
                try:
                    sheet_id = self.worksheet.id
                    spreadsheet = self.worksheet.spreadsheet
                    spreadsheet.batch_update({
                        "requests": [{
                            "deleteDimension": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "dimension": "COLUMNS",
                                    "startIndex": 3,  # D列（0始まり）
                                    "endIndex": 7,    # G列の次（exclusive）
                                }
                            }
                        }]
                    })
                    logging.info('[Writer] 旧フォーマットのD-G列を削除しました')
                except Exception as e:
                    logging.warning(f'[Writer] 旧列削除失敗: {e}')

            end_col = chr(ord('A') + len(HEADERS) - 1)
            self.worksheet.update(f'A1:{end_col}1', [HEADERS])
            logging.info(f'[Writer] ヘッダー行を更新: {current} → {HEADERS}')
        except Exception as e:
            logging.warning(f'[Writer] ヘッダー同期失敗: {e}')

    def add(self, data: dict) -> list[tuple[str, int]] | None:
        """
        データを追加する。フラッシュが実行された場合は
        [(normalized_name, sheet_row), ...] を返す。それ以外はNone。
        """
        phone_display = data.get('phone') or ''
        row = [
            data['company_name'],
            data['lp_url'],
            phone_display,
            data['found_date'],
        ]
        self._batch.append((data.get('normalized_name', ''), row))
        elapsed = (datetime.now() - self._last_flush).total_seconds()
        if len(self._batch) >= self.batch_size or elapsed >= self.batch_timeout:
            return self.flush()
        return None

    def flush(self) -> list[tuple[str, int]] | None:
        """
        バッチをSheetsに書き込む。
        成功時は [(normalized_name, sheet_row), ...] を返す。
        """
        if not self._batch:
            return None

        names = [item[0] for item in self._batch]
        rows = [item[1] for item in self._batch]
        start_row = self._next_row

        for attempt in range(3):
            try:
                self.worksheet.append_rows(rows, value_input_option='USER_ENTERED')
                self._next_row += len(rows)
                self._batch.clear()
                self._last_flush = datetime.now()
                logging.info(f'Sheets書き込み: {len(rows)}件 (行{start_row}〜{start_row + len(rows) - 1})')
                return [(name, start_row + i) for i, name in enumerate(names)]

            except gspread.exceptions.APIError as e:
                if e.response.status_code == 429:
                    wait = 60 * (attempt + 1)
                    logging.warning(f'Sheets APIレート制限。{wait}秒待機')
                    self._interruptible_sleep(wait)
                else:
                    logging.error(f'Sheets APIエラー: {e}')
                    try:
                        from self_repair.diagnostics import get_diagnostics
                        get_diagnostics().record_sheets_error()
                    except Exception:
                        pass
                    raise

        logging.error('Sheetsへの書き込みに3回失敗しました')
        try:
            from self_repair.diagnostics import get_diagnostics
            get_diagnostics().record_sheets_error()
        except Exception:
            pass
        return None

    def flush_if_timeout(self) -> list[tuple[str, int]] | None:
        elapsed = (datetime.now() - self._last_flush).total_seconds()
        if elapsed >= self.batch_timeout and self._batch:
            return self.flush()
        return None

    def _interruptible_sleep(self, seconds: float):
        end = time.time() + seconds
        last_beat = time.time()
        while time.time() < end:
            if self._shutdown and self._shutdown.is_set():
                break
            time.sleep(min(5.0, end - time.time()))
            if self._heartbeat and time.time() - last_beat >= 30:
                self._heartbeat()
                last_beat = time.time()
