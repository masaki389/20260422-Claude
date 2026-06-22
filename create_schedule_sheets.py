#!/usr/bin/env python3
"""幸子まとめスプシに「制作スケジュール」シートを作成（スケジュール＋詳細TODO統合版）"""

from datetime import date
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

BASE = Path(__file__).parent
TOKEN_FILE = BASE / "token_drive.json"
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
]
SS_ID = "1xHIRrC4e4eJGuvnE84n7xERZYEzu4SApTroB4xYknM0"

# ── 曜日 ────────────────────────────────────────────────
DAYS_JP = ["月","火","水","木","金","土","日"]

def day_jp(m, d):
    return DAYS_JP[date(2026, m, d).weekday()]

# ── スケジュールデータ（曜日は自動計算） ─────────────────
RAW_SCHEDULE = [
    ("第6話",  "監督", 6, 18, "投稿予定", ""),
    ("第7話",  "外注", 6, 23, "制作中",   ""),
    ("第8話",  "監督", 6, 26, "未着手",   "6/23当日スタート必須"),
    ("第9話",  "監督", 6, 30, "未着手",   ""),
    ("第10話", "外注", 7,  3, "未着手",   ""),
    ("第11話", "監督", 7,  7, "未着手",   ""),
    ("第12話", "監督", 7, 10, "未着手",   "7/7当日スタート必須"),
    ("第13話", "外注", 7, 13, "未着手",   ""),
    ("第14話", "監督", 7, 17, "未着手",   ""),
    ("第15話", "監督", 7, 20, "未着手",   "7/17当日スタート必須"),
    ("第16話", "外注", 7, 23, "未着手",   ""),
    ("第17話", "監督", 7, 27, "未着手",   ""),
    ("第18話", "監督", 7, 30, "未着手",   "7/27当日スタート必須"),
    ("第19話", "外注", 8,  2, "未着手",   ""),
    ("第20話", "監督", 8,  6, "未着手",   ""),
]

SCHEDULE = [
    (ep, who, f"{m}/{d}", day_jp(m, d), status, memo)
    for ep, who, m, d, status, memo in RAW_SCHEDULE
]

CONTRACTOR_EPISODES = [
    {"ep": "第7話",  "post": "6/23（火）", "deadline": "6/22（月）までに納品"},
    {"ep": "第10話", "post": "7/3（金）",  "deadline": "7/2（木）までに納品"},
    {"ep": "第13話", "post": "7/13（月）", "deadline": "7/12（日）までに納品"},
    {"ep": "第16話", "post": "7/23（木）", "deadline": "7/22（水）までに納品"},
    {"ep": "第19話", "post": "8/2（日）",  "deadline": "8/1（土）までに納品"},
]

CHAPTERS      = ["第1章", "第2章", "第3章", "第4章", "第5章"]
DAY_RANGES    = ["Day 1-2", "Day 3-4", "Day 5-6", "Day 7-8", "Day 9-10"]
TASKS         = ["音声", "画像修正", "動画化", "SE", "他編集（エフェクト・微調整）"]
CHAPTER_COLORS = ["fffde7", "e8f5e9", "e3f2fd", "fce4ec", "f3e5f5"]

# ── ユーティリティ ────────────────────────────────────────
def rgb(h):
    h = h.lstrip("#")
    return {"red": int(h[0:2],16)/255, "green": int(h[2:4],16)/255, "blue": int(h[4:6],16)/255}

def get_service():
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
    return build("sheets", "v4", credentials=creds)

def get_or_add_sheet(service, title):
    meta = service.spreadsheets().get(spreadsheetId=SS_ID).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == title:
            return s["properties"]["sheetId"]
    res = service.spreadsheets().batchUpdate(
        spreadsheetId=SS_ID,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]}
    ).execute()
    return res["replies"][0]["addSheet"]["properties"]["sheetId"]

def delete_sheet_if_exists(service, title):
    meta = service.spreadsheets().get(spreadsheetId=SS_ID).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == title:
            service.spreadsheets().batchUpdate(
                spreadsheetId=SS_ID,
                body={"requests": [{"deleteSheet": {"sheetId": s["properties"]["sheetId"]}}]}
            ).execute()
            print(f"  旧シート '{title}' を削除")
            return

def write_values(service, title, values):
    service.spreadsheets().values().clear(spreadsheetId=SS_ID, range=f"'{title}'").execute()
    service.spreadsheets().values().update(
        spreadsheetId=SS_ID, range=f"'{title}'!A1",
        valueInputOption="USER_ENTERED", body={"values": values}
    ).execute()

def batch(service, reqs):
    if reqs:
        service.spreadsheets().batchUpdate(spreadsheetId=SS_ID, body={"requests": reqs}).execute()

def cell_fmt(sid, r0, r1, c0, c1, bg=None, bold=False, align=None, fg=None, fs=None):
    fmt, tf = {}, {}
    if bg:    fmt["backgroundColor"] = rgb(bg)
    if bold:  tf["bold"] = True
    if fg:    tf["foregroundColor"] = rgb(fg)
    if fs:    tf["fontSize"] = fs
    if tf:    fmt["textFormat"] = tf
    if align: fmt["horizontalAlignment"] = align
    fields_list = []
    if bg:    fields_list.append("backgroundColor")
    if tf:    fields_list.append("textFormat")
    if align: fields_list.append("horizontalAlignment")
    return {"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": c0, "endColumnIndex": c1},
        "cell": {"userEnteredFormat": fmt},
        "fields": "userEnteredFormat(" + ",".join(fields_list) + ")"
    }}

def col_px(sid, ci, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": ci, "endIndex": ci+1},
        "properties": {"pixelSize": px}, "fields": "pixelSize"
    }}

def merge(sid, r0, r1, c0, c1):
    return {"mergeCells": {
        "range": {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": c0, "endColumnIndex": c1},
        "mergeType": "MERGE_ALL"
    }}

def checkbox(sid, r0, r1, c0, c1):
    return {"setDataValidation": {
        "range": {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": c0, "endColumnIndex": c1},
        "rule": {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True}
    }}

# ── メイン ────────────────────────────────────────────────
def main():
    svc = get_service()

    # 旧シート削除
    for old in ["投稿スケジュール", "外注_制作TODO"]:
        delete_sheet_if_exists(svc, old)

    title = "制作スケジュール"
    sid = get_or_add_sheet(svc, title)

    # ═══════════════════════════════════════════════
    #  値の組み立て
    # ═══════════════════════════════════════════════
    values = []
    fmt_reqs = []
    merge_reqs = []
    chk_rows_schedule = []   # スケジュール部のチェックボックス行
    chk_rows_todo = []       # TODO部のチェックボックス行

    # ── 凡例行 (row 0)
    values.append([
        "監督：①台本　②画像　③QC　④編集　　／　　外注：①第1章　②第2章　③第3章　④第4章　⑤第5章",
        "","","","","","","","","","",""
    ])
    merge_reqs.append(merge(sid, 0, 1, 0, 12))
    fmt_reqs.append(cell_fmt(sid, 0, 1, 0, 12, bg="f3f3f3", bold=False, align="LEFT", fg="666666", fs=9))

    # ── スケジュールヘッダー行 (row 1)
    values.append(["話数","担当","投稿日","曜日","ステータス","①","②","③","④","⑤","メモ",""])
    fmt_reqs.append(cell_fmt(sid, 1, 2, 0, 11, bg="1a3a5c", bold=True, align="CENTER", fg="ffffff", fs=11))

    # ── スケジュール行 (rows 2-16)
    for i, row in enumerate(SCHEDULE):
        ep, who, date_str, day, status, memo = row
        values.append([ep, who, date_str, day, status, "FALSE","FALSE","FALSE","FALSE","FALSE" if who=="外注" else "", memo, ""])
        r = 2 + i
        bg = "fce5cd" if who == "外注" else "cfe2f3"
        fmt_reqs.append(cell_fmt(sid, r, r+1, 0, 11, bg=bg))
        # チェックボックス（監督は①〜④、外注は①〜⑤）
        end_col = 10 if who == "外注" else 9
        chk_rows_schedule.append((r, 5, end_col))

    # ── 区切り行 (row 17)
    values.append([""] * 12)

    # ── 詳細TODOセクションヘッダー (row 18)
    values.append(["▼ 外注 詳細作業リスト（10日間TODO）","","","","","","","","","","",""])
    merge_reqs.append(merge(sid, 18, 19, 0, 12))
    fmt_reqs.append(cell_fmt(sid, 18, 19, 0, 12, bg="d45f00", bold=True, fg="ffffff", fs=12))

    # ── 詳細TODOブロック (row 19+)
    r = 19
    for ep_info in CONTRACTOR_EPISODES:
        # エピソードヘッダー
        values.append([f"【{ep_info['ep']}】　投稿日：{ep_info['post']}　{ep_info['deadline']}", "","","","","","","","","","",""])
        merge_reqs.append(merge(sid, r, r+1, 0, 12))
        fmt_reqs.append(cell_fmt(sid, r, r+1, 0, 12, bg="f6b26b", bold=True, fs=11))
        r += 1

        # 列ヘッダー
        values.append(["日程目安", "章", "作業内容", "完了", "メモ", "","","","","","",""])
        fmt_reqs.append(cell_fmt(sid, r, r+1, 0, 5, bg="d9d9d9", bold=True, align="CENTER"))
        r += 1

        # 章ごとタスク
        for ci, (chapter, day_range) in enumerate(zip(CHAPTERS, DAY_RANGES)):
            bg = CHAPTER_COLORS[ci]
            for task in TASKS:
                values.append([day_range, chapter, task, "FALSE", "", "","","","","","",""])
                fmt_reqs.append(cell_fmt(sid, r, r+1, 0, 5, bg=bg))
                chk_rows_todo.append(r)
                r += 1

        # 仕上げ
        for final in ["全体確認・微調整", "書き出し・納品"]:
            values.append(["Day 10", "仕上げ", final, "FALSE", "", "","","","","","",""])
            fmt_reqs.append(cell_fmt(sid, r, r+1, 0, 5, bg="e0e0e0", bold=True))
            chk_rows_todo.append(r)
            r += 1

        # 区切り
        values.append([""] * 12)
        r += 1

    # ═══════════════════════════════════════════════
    #  書き込み＆フォーマット
    # ═══════════════════════════════════════════════
    write_values(svc, title, values)

    # マージ → フォーマット の順
    batch(svc, merge_reqs)
    batch(svc, fmt_reqs)

    # チェックボックス
    chk_reqs = []
    for (row_r, c0, c1) in chk_rows_schedule:
        chk_reqs.append(checkbox(sid, row_r, row_r+1, c0, c1))
    for row_r in chk_rows_todo:
        chk_reqs.append(checkbox(sid, row_r, row_r+1, 3, 4))

    # 列幅
    for ci, px in enumerate([65, 55, 65, 48, 90, 50, 50, 50, 50, 50, 180]):
        chk_reqs.append(col_px(sid, ci, px))

    # スケジュール部の1・2行目を固定（スクロール時も見える）
    chk_reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 2}},
        "fields": "gridProperties.frozenRowCount"
    }})

    batch(svc, chk_reqs)

    print(f"✅ '{title}' 作成完了")
    print(f"🔗 https://docs.google.com/spreadsheets/d/{SS_ID}")


if __name__ == "__main__":
    main()
