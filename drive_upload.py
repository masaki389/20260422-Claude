"""
drive_upload.py — Google Driveアップロードユーティリティ
転職アニメ：「転職アニメ/[会社名]/」にアップロード
幸子チャンネル：「幸子チャンネル/[第X話]/」にアップロード
"""

import sys
from pathlib import Path

BASE              = Path(__file__).parent
TOKEN_FILE        = BASE / "token_drive.json"
SCOPES            = ["https://www.googleapis.com/auth/drive.file"]
DRIVE_ROOT_ID     = "17yzl03rXCp0GsLjj0dr5B9Ykv9_5WeEd"  # 転職アニメフォルダ
SACHIKO_ROOT_ID   = "1zBZCnum_bBe92rxB5Z5ZFoI5ld3FiPlw"   # 幸子チャンネルフォルダ


def _get_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    if not TOKEN_FILE.exists():
        print("⚠  Google Drive未認証です。先に以下を実行してください：")
        print("   python drive_auth.py")
        return None

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    if not creds.valid:
        print("⚠  Drive認証トークンが無効です。python drive_auth.py を再実行してください。")
        return None

    return build("drive", "v3", credentials=creds)


def _get_or_create_folder(service, name, parent_id=None):
    """フォルダを検索し、なければ作成してIDを返す"""
    query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = service.files().list(q=query, fields="files(id,name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def upload_folder(local_folder: Path, script_name: str) -> bool:
    """
    local_folder内のPNGをGoogle Drive「転職アニメ/script_name/」にアップロード
    戻り値: 成功=True / 未認証=False
    """
    from googleapiclient.http import MediaFileUpload

    service = _get_service()
    if service is None:
        return False

    company_id = _get_or_create_folder(service, script_name, DRIVE_ROOT_ID)

    # 既存ファイル名を取得（重複アップロード防止）
    existing = service.files().list(
        q=f"'{company_id}' in parents and trashed=false",
        fields="files(id,name)"
    ).execute().get("files", [])
    existing_names = {f["name"] for f in existing}

    png_files = sorted(local_folder.glob("*.png"))
    uploaded = 0
    for png in png_files:
        if png.name in existing_names:
            continue
        media = MediaFileUpload(str(png), mimetype="image/png", resumable=False)
        service.files().create(
            body={"name": png.name, "parents": [company_id]},
            media_body=media,
            fields="id"
        ).execute()
        uploaded += 1
        print(f"    ↑ Drive: {png.name}")

    print(f"✅ Google Drive: 転職アニメ/{script_name}/ に {uploaded}枚アップロード完了 🔗 https://drive.google.com/drive/folders/{DRIVE_ROOT_ID}")
    return True


def upload_sachiko(episode_name: str) -> bool:
    """
    outputs/cut_*.png を Google Drive「幸子チャンネル/episode_name/」にアップロード
    例: upload_sachiko("第5話")
    """
    from googleapiclient.http import MediaFileUpload

    service = _get_service()
    if service is None:
        return False

    local_folder = BASE / "outputs"
    episode_id = _get_or_create_folder(service, episode_name, SACHIKO_ROOT_ID)

    existing = []
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{episode_id}' in parents and trashed=false",
            fields="files(id,name)", pageSize=200, pageToken=page_token
        ).execute()
        existing.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    existing_map = {f["name"]: f["id"] for f in existing}

    png_files = sorted(local_folder.glob("cut_*.png"))
    uploaded = 0
    for png in png_files:
        media = MediaFileUpload(str(png), mimetype="image/png", resumable=False)
        if png.name in existing_map:
            # 既存ファイルを上書き更新
            service.files().update(
                fileId=existing_map[png.name],
                media_body=media
            ).execute()
        else:
            service.files().create(
                body={"name": png.name, "parents": [episode_id]},
                media_body=media,
                fields="id"
            ).execute()
        uploaded += 1
        print(f"    ↑ Drive: {png.name}")

    print(f"✅ Google Drive: 幸子チャンネル/{episode_name}/ に {uploaded}枚アップロード完了 🔗 https://drive.google.com/drive/folders/{SACHIKO_ROOT_ID}")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使い方:")
        print("  転職: python drive_upload.py tenshi C01_日本製鉄")
        print("  幸子: python drive_upload.py sachiko 第5話")
        sys.exit(1)

    channel = sys.argv[1]
    if channel == "sachiko":
        episode = sys.argv[2] if len(sys.argv) > 2 else "第X話"
        upload_sachiko(episode)
    elif channel == "tenshi":
        script_name  = sys.argv[2]
        local_folder = BASE / "outputs" / "tenshi" / script_name
        if not local_folder.exists():
            sys.exit(f"エラー: {local_folder} が見つかりません")
        upload_folder(local_folder, script_name)
    else:
        sys.exit(f"エラー: channel は sachiko または tenshi を指定してください")
