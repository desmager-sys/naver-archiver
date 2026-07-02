"""
Google Drive API v3 — 서비스 계정 방식.
환경변수: GDRIVE_SERVICE_ACCOUNT_JSON, GDRIVE_FOLDER_ID
"""
import io, os, json, re


def get_drive_service():
    sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        sa_info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        print(f"  [Drive] 서비스 초기화 실패: {e}")
        return None


def get_or_create_folder(service, parent_id: str, name: str) -> str:
    q = (
        f"'{parent_id}' in parents"
        f" and name='{name}'"
        f" and mimeType='application/vnd.google-apps.folder'"
        f" and trashed=false"
    )
    res = service.files().list(q=q, fields="files(id)").execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]

    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]


def save_text_to_drive(service, content: str, folder_id: str, filename: str):
    from googleapiclient.http import MediaIoBaseUpload

    media = MediaIoBaseUpload(
        io.BytesIO(content.encode("utf-8")), mimetype="text/plain; charset=utf-8"
    )
    meta = {"name": filename, "parents": [folder_id]}
    service.files().create(body=meta, media_body=media, fields="id").execute()


def safe_filename(title: str, max_len: int = 80) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title)[:max_len]
