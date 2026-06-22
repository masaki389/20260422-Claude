"""テロップ照合済みのアンカーポイントから線形補間して全カットを再対応づけし、スプシを再構築"""
import sys, json
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

# テロップを実際に読み取って確認した (タイムスタンプ秒, カット番号) のアンカーポイント
ANCHORS = [
    (0.0, 1), (36.0, 5), (55.2, 5), (64.9, 16), (75.0, 17), (84.4, 27),
    (96.2, 29), (101.2, 35), (111.2, 36), (117.8, 38), (129.6, 39),
    (137.8, 41), (142.9, 43), (147.9, 46), (164.1, 48), (184.1, 55),
    (194.4, 61), (207.3, 62), (215.3, 65), (225.8, 68), (240.0, 69),
    (266.6, 72), (281.0, 74), (286.0, 76), (290.7, 77), (295.8, 78),
    (328.6, 80), (335.5, 85), (337.6, 87), (342.6, 90), (348.6, 93),
    (354.2, 94), (373.6, 98), (384.0, 100), (387.3, 101), (437.4, 108),
    (459.2, 117), (488.3, 120),
]

def cut_to_timestamp(cut_no: float) -> float:
    """カット番号から補間でタイムスタンプを推定"""
    for i in range(len(ANCHORS) - 1):
        t0, c0 = ANCHORS[i]
        t1, c1 = ANCHORS[i+1]
        if c0 <= cut_no <= c1:
            if c1 == c0:
                return t0
            ratio = (cut_no - c0) / (c1 - c0)
            return t0 + (t1 - t0) * ratio
    # 範囲外は端を返す
    if cut_no < ANCHORS[0][1]:
        return ANCHORS[0][0]
    return ANCHORS[-1][0]

# テロップフレーム一覧（コールドオープン分は除外）
telop_dir = BASE / "outputs" / "video_frames_telop"
frame_files = sorted(telop_dir.glob("tframe_*.jpg"), key=lambda p: int(p.stem.split("_")[1]))

def nearest_frame(target_t: float):
    best = min(frame_files, key=lambda p: abs(float(p.stem.split("_")[2].rstrip("s")) - target_t))
    return best

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

folder_id = get_or_create_folder("第6話_全カット比較v2", SACHIKO_ROOT)

cache_path = Path("/tmp/full_compare_links_v2.json")
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

cuts = parse_script(BASE / "script.txt")
total = len(cuts)
print(f"カット数: {total}")

rows = []
used_frames = set()
for i in range(total):
    cut_no = i + 1
    cut = cuts[i]
    gen_path = BASE / "outputs" / f"cut_{cut_no:03d}.png"

    target_t = cut_to_timestamp(cut_no)
    frame_path = nearest_frame(target_t)
    is_anchor = any(c == cut_no for _, c in ANCHORS)
    dup_note = ""
    if frame_path.name in used_frames:
        dup_note = "（前後のカットと同フレーム＝編集で短時間にまとめられた可能性）"
    used_frames.add(frame_path.name)

    before_url = ""
    after_url = ""
    try:
        if gen_path.exists():
            before_url = upload_and_link(gen_path)
    except Exception as e:
        print(f"カット{cut_no} before失敗: {e}")
    try:
        after_url = upload_and_link(frame_path)
    except Exception as e:
        print(f"カット{cut_no} after失敗: {e}")

    before_cell = f'=IMAGE("{before_url}")' if before_url else "(画像なし)"
    after_cell = f'=IMAGE("{after_url}")' if after_url else "(取得失敗)"

    confidence = "テロップ実測" if is_anchor else "補間推定"
    rows.append([
        str(cut_no), before_cell, after_cell,
        cut.get("場所") or "", cut.get("内容") or "", cut.get("カメラ") or "",
        f"{confidence}（{target_t:.1f}秒付近）{dup_note}",
    ])
    if cut_no % 10 == 0:
        print(f"  {cut_no}/{total} 処理済み")

print("アップロード完了。スプシに書き込みます。")

title = "第6話_全カット比較v2（テロップ照合・補間）"
meta = sheets.spreadsheets().get(spreadsheetId=SS_ID).execute()
sid = None
for s in meta["sheets"]:
    if s["properties"]["title"] == title:
        sid = s["properties"]["sheetId"]
if sid is None:
    res = sheets.spreadsheets().batchUpdate(spreadsheetId=SS_ID, body={"requests": [
        {"addSheet": {"properties": {"title": title}}}
    ]}).execute()
    sid = res["replies"][0]["addSheet"]["properties"]["sheetId"]

header = [["カット", "ビフォー（生成画像）", "アフター（動画フレーム）", "場所", "台本内容", "カメラ", "対応精度・メモ"]]
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
    {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 4, "endIndex": 4}, "properties": {"pixelSize": 260}, "fields": "pixelSize"}},
]
# テロップ実測の行を緑色で強調
for i, (_, c) in enumerate(ANCHORS):
    if 1 <= c <= total:
        r = c  # header分+1だが0-indexedなのでcがそのまま行インデックス
        reqs.append({"repeatCell": {"range": {"sheetId": sid, "startRowIndex": r, "endRowIndex": r+1, "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {"backgroundColor": rgb("d9ead3")}}, "fields": "userEnteredFormat.backgroundColor"}})

sheets.spreadsheets().batchUpdate(spreadsheetId=SS_ID, body={"requests": reqs}).execute()
print("完了")
