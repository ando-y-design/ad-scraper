import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from storage.database import get_connection, init_db
from utils.status_reporter import generate_status_html
from utils.drive_uploader import upload_status_html

conn = get_connection()
generate_status_html(conn)
print('status.html 生成完了')

creds_path = 'credentials.json'
html_path = 'status.html'
url = upload_status_html(creds_path, html_path, share_emails=[])
if url:
    print(f'共有URL: {url}')
else:
    print('アップロード失敗')
