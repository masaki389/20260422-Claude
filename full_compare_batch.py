"""第6話 全120カットのBefore(生成画像)/After(動画フレーム)を一括でDriveアップロード＋スプシ反映"""
import sys, time, json
from pathlib import Path
sys.path.insert(0, "/workspaces/20260422-Claude")
from auto_studio import parse_script
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE = Path("/workspaces/20260422-Claude")
TOKEN_FILE = BASE / "token_drive.json"
SCOPES = ["https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/spreadsheets"]
SS_ID = "1xHIRrC4e4eJGuvnE84n7xERZYEzu4SApTroB4xYknM0"
SACHIKO_ROOT = "1zBZCnum_bBe92rxB5Z5ZFoI5ld3FiPlw"

creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
if creds.expired and creds.refresh_token:
    creds.refresh(Request())
drive = build("drive", "v3", credentials=creds)
sheets = build("sheets", "v4", credentials=creds)

def get_or_create_folder(name, parent_id=None):
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    res = drive.files().list(q=q, fields="files(id,name)").execute().get("files", [])
    if res:
        return res[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    return drive.files().create(body=meta, fields="id").execute()["id"]

folder_id = get_or_create_folder("第6話_全カット比較", SACHIKO_ROOT)

# 既存リンクキャッシュ（途中失敗時の再実行対応）
cache_path = Path("/tmp/full_compare_links.json")
links = json.load(open(cache_path)) if cache_path.exists() else {}

def upload_and_link(path: Path) -> str:
    key = path.name
    if key in links:
        return links[key]
    mt = "image/png" if path.suffix == ".png" else "image/jpeg"
    media = MediaFileUpload(str(path), mimetype=mt)
    up = drive.files().create(body={"name": key, "parents": [folder_id]}, media_body=media, fields="id").execute()
    fid = up["id"]
    drive.permissions().create(fileId=fid, body={"role": "reader", "type": "anyone"}).execute()
    url = f"https://drive.google.com/uc?id={fid}"
    links[key] = url
    json.dump(links, open(cache_path, "w"))
    return url

# 台本読み込み
cuts = parse_script(BASE / "script.txt")
total = len(cuts)

# テロップ動画フレーム（コールドオープン分=最初の11個をスキップして本編とみなす）
telop_dir = BASE / "outputs" / "video_frames_telop"
frame_files = sorted(telop_dir.glob("tframe_*.jpg"), key=lambda p: int(p.stem.split("_")[1]))
COLD_OPEN_OFFSET = 11
usable_frames = frame_files[COLD_OPEN_OFFSET:]

print(f"カット数: {total} / 使用可能フレーム数: {len(usable_frames)}")

rows = []
for i in range(total):
    cut_no = i + 1
    cut = cuts[i]
    gen_path = BASE / "outputs" / f"cut_{cut_no:03d}.png"
    frame_path = usable_frames[i] if i < len(usable_frames) else None

    before_url = ""
    after_url = ""
    try:
        if gen_path.exists():
            before_url = upload_and_link(gen_path)
    except Exception as e:
        print(f"カット{cut_no} ビフォーアップロード失敗: {e}")
    try:
        if frame_path is not None:
            after_url = upload_and_link(frame_path)
    except Exception as e:
        print(f"カット{cut_no} アフターアップロード失敗: {e}")

    before_cell = f'=IMAGE("{before_url}")' if before_url else "(画像なし)"
    after_cell = f'=IMAGE("{after_url}")' if after_url else "(フレーム未対応)"

    rows.append([
        str(cut_no),
        before_cell,
        after_cell,
        cut.get("場所") or "",
        cut.get("内容") or "",
        cut.get("カメラ") or "",
        f"推定対応（コールドオープン分オフセット{COLD_OPEN_OFFSET}フレームで自動算出。要目視確認）",
    ])
    if cut_no % 10 == 0:
        print(f"  {cut_no}/{total} 処理済み")

print("全カットのリンク準備完了。スプシに書き込みます。")

title = "第6話_全カット比較（自動・要確認）"
meta = sheets.spreadsheets().get(spreadsheetId=SS_ID).execute()
sid = None
for s in meta["sheets"]:
    if s["properties"]["title"] == title:
        sid = s["properties"]["sheetId"]
        break
if sid is None:
    res = sheets.spreadsheets().batchUpdate(spreadsheetId=SS_ID, body={"requests": [
        {"addSheet": {"properties": {"title": title}}}
    ]}).execute()
    sid = res["replies"][0]["addSheet"]["properties"]["sheetId"]

header = [["カット", "ビフォー（生成画像）", "アフター（動画フレーム・推定対応）", "場所", "台本内容", "カメラ", "メモ"]]
sheets.spreadsheets().values().clear(spreadsheetId=SS_ID, range=f"'{title}'").execute()
sheets.spreadsheets().values().update(
    spreadsheetId=SS_ID, range=f"'{title}'!A1",
    valueInputOption="USER_ENTERED", body={"values": header + rows}
).execute()

def rgb(h):
    h = h.lstrip("#")
    return {"red": int(h[0:2],16)/255, "green": int(h[2:4],16)/255, "blue": int(h[4:6],16)/255}

reqs = [
    {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 7},
        "cell": {"userEnteredFormat": {"backgroundColor": rgb("0b5394"), "textFormat": {"bold": True, "foregroundColor": rgb("ffffff")}, "horizontalAlignment": "CENTER"}},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"}},
    {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
    {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 1, "endIndex": 1+total}, "properties": {"pixelSize": 140}, "fields": "pixelSize"}},
    {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 3}, "properties": {"pixelSize": 200}, "fields": "pixelSize"}},
    {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 4, "endIndex": 5}, "properties": {"pixelSize": 260}, "fields": "pixelSize"}},
]
sheets.spreadsheets().batchUpdate(spreadsheetId=SS_ID, body={"requests": reqs}).execute()
print("完了")
