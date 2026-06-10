from __future__ import annotations
from typing import Optional
# -*- coding: utf-8 -*-
import json
import logging
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
_STATE_FILE = BASE_DIR / 'logs' / '.drive_file_id'


def _get_drive_service(creds_path: str):
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_service_account_file(
        creds_path,
        scopes=['https://www.googleapis.com/auth/drive'],
    )
    return build('drive', 'v3', credentials=creds)


def upload_status_html(creds_path: str, html_path: str, share_emails: Optional[list[str]] = None) -> Optional[str]:
    """
    status.html を Google Drive にアップロードして公開URLを返す。
    初回はファイルを新規作成してIDを保存、2回目以降は同じIDに上書きする。
    share_emails: 初回のみ、指定メールアドレスのDriveに共有する。
    """
    try:
        from googleapiclient.http import MediaFileUpload

        service = _get_drive_service(creds_path)
        media = MediaFileUpload(html_path, mimetype='text/html', resumable=False)

        file_id = _load_file_id()

        if file_id:
            # 既存ファイルを上書き
            service.files().update(fileId=file_id, media_body=media).execute()
            logging.debug(f'[Drive] status.html 更新完了 (id={file_id})')
        else:
            # 初回: 新規作成 → IDを保存
            metadata = {'name': 'ad_scraper_status.html'}
            f = service.files().create(body=metadata, media_body=media, fields='id').execute()
            file_id = f['id']
            _save_file_id(file_id)

            # 「リンクを知っている全員が閲覧可能」に設定
            service.permissions().create(
                fileId=file_id,
                body={'role': 'reader', 'type': 'anyone'},
            ).execute()

            # 指定メールアドレスのDriveに共有（「共有済みアイテム」に表示される）
            for email in (share_emails or []):
                try:
                    service.permissions().create(
                        fileId=file_id,
                        body={'role': 'writer', 'type': 'user', 'emailAddress': email},
                        sendNotificationEmail=False,
                    ).execute()
                    logging.info(f'[Drive] {email} に共有しました')
                except Exception as e:
                    logging.warning(f'[Drive] {email} への共有失敗: {e}')

            logging.info(f'[Drive] status.html を公開しました')

        url = f'https://drive.google.com/file/d/{file_id}/view'

        # share_url.txt に書き出す（コピペ用）
        try:
            (BASE_DIR / 'share_url.txt').write_text(url, encoding='utf-8')
        except Exception:
            pass

        return url

    except Exception as e:
        logging.warning(f'[Drive] アップロード失敗: {e}')
        return None


def _load_file_id() -> Optional[str]:
    try:
        if _STATE_FILE.exists():
            return _STATE_FILE.read_text().strip()
    except Exception:
        pass
    return None


def _save_file_id(file_id: str):
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(file_id)
    except Exception:
        pass
