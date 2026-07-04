"""
StudioOgawa 自律AIオフィス v2
- 4部署制: 幸子チャンネル / Xマーケティング / noteコンテンツ / 事業開発
- 承認パイプライン: 下書き生成 → 監督承認 → 承認済みキュー → ランダム自動投稿
- 自律学習: X戦略家/noteリサーチャーが週次で戦略を更新 → 投稿品質が自己改善
"""

import os
import sys
import copy
import threading
import subprocess
import json
import uuid
import random
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv
from crewai import Agent, Task, Crew, Process
from crewai.tools import tool as crewai_tool
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

load_dotenv()
os.environ.setdefault("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", ""))


# ─── Search Tools（CrewAIエージェントに渡すツール） ────────────────────────────

@crewai_tool("YouTube動画検索")
def youtube_search_tool(query: str) -> str:
    """YouTube Data APIでJP向け動画を検索し、タイトル・チャンネル名・説明を返す。
    シニア向けYouTubeトレンドリサーチに使う。"""
    import urllib.request, urllib.parse, json as _json
    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        return "YouTube API key not configured"
    try:
        params = urllib.parse.urlencode({
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": 10,
            "order": "viewCount",
            "regionCode": "JP",
            "relevanceLanguage": "ja",
            "key": api_key,
        })
        url = f"https://www.googleapis.com/youtube/v3/search?{params}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = _json.loads(resp.read())
        items = data.get("items", [])
        if not items:
            return "検索結果なし"
        lines = [
            f"・「{it['snippet']['title']}」({it['snippet']['channelTitle']}) — "
            f"{it['snippet']['description'][:80]}"
            for it in items
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"YouTube検索エラー: {str(e)[:100]}"


@crewai_tool("Webサイト検索")
def web_search_tool(query: str) -> str:
    """DuckDuckGoでWeb検索を行い最新記事・ニュース・トレンドを返す。
    YouTubeトレンド分析や業界調査に使う。"""
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5, region="jp-jp"))
        if not results:
            return "検索結果なし"
        lines = [f"【{r.get('title', '')}】{r.get('body', '')[:150]}" for r in results]
        return "\n\n".join(lines)
    except Exception as e:
        return f"Web検索エラー: {str(e)[:100]}"


def _restore_session(env_var: str, dest_path: Path):
    """Railway等でenv変数にbase64セッションが入っている場合、ファイルに展開する。"""
    import base64
    val = os.getenv(env_var, "")
    if val and not dest_path.exists():
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(base64.b64decode(val))


app = Flask(__name__)
lock = threading.Lock()
LLM = "gemini/gemini-2.5-flash"
JST = pytz.timezone("Asia/Tokyo")

BASE_DIR = Path(__file__).parent
RESEARCH_DIR = BASE_DIR / "research"
RESEARCH_DIR.mkdir(exist_ok=True)
AUTOMATION_DIR = BASE_DIR / "automation"

# キューはDATA_DIR（Railway Volume）に永続化。未設定時はresearch/にフォールバック
_DATA_DIR = Path(os.getenv("DATA_DIR", str(RESEARCH_DIR)))
_DATA_DIR.mkdir(exist_ok=True, parents=True)
PENDING_PATH  = _DATA_DIR / "pending_queue.json"
APPROVED_PATH = _DATA_DIR / "approved_queue.json"

# 研究成果物もDATA_DIRに統一（Railway Volumeがあれば再起動後も永続化される）
RESEARCH_DIR = _DATA_DIR

AGENTS_DIR    = BASE_DIR / "agents"
CONTEXT_DIR   = BASE_DIR / "shared-context"

NOTE_ROADMAP_SPREADSHEET_ID = "1xHIRrC4e4eJGuvnE84n7xERZYEzu4SApTroB4xYknM0"
NOTE_ROADMAP_GID            = 1883610657
DIRECTIVES_PATH             = _DATA_DIR / "agent_directives.json"


def _load_directives() -> dict:
    """agent_directives.json を読み込む。{agent_id: ["指示1", "指示2", ...]}"""
    if DIRECTIVES_PATH.exists():
        try:
            return json.loads(DIRECTIVES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_directive(agent_id: str, instruction: str):
    """エージェントへの指示を永続化する。"""
    directives = _load_directives()
    directives.setdefault(agent_id, [])
    entry = {"text": instruction, "saved_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M")}
    directives[agent_id].insert(0, entry)
    directives[agent_id] = directives[agent_id][:10]  # 最新10件まで保持
    DIRECTIVES_PATH.write_text(json.dumps(directives, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_agent_directive_text(agent_id: str) -> str:
    """agent_idに紐づく有効な指示をプロンプト用テキストに変換して返す。"""
    directives = _load_directives()
    items = directives.get(agent_id, []) + directives.get("global", [])
    if not items:
        return ""
    lines = [f"・{d['text']}" for d in items[:5]]
    return "\n【監督からの指示（優先順位：最高）】\n" + "\n".join(lines) + "\n\n"


def _get_sheets_client():
    """Google Sheets書き込み用クライアントを返す。環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が必要。"""
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        return None
    try:
        import json as _json, base64, gspread
        from google.oauth2.service_account import Credentials
        try:
            sa_dict = _json.loads(base64.b64decode(sa_json).decode())
        except Exception:
            sa_dict = _json.loads(sa_json)
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/spreadsheets",
        ]
        creds = Credentials.from_service_account_info(sa_dict, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        print(f"[Sheets] 認証失敗: {e}", flush=True)
        return None


def note_roadmap_mark_done(roadmap_date: str) -> bool:
    """ロードマップの指定日付行（col0）の「完了」列（col7）をTRUEにする。"""
    gc = _get_sheets_client()
    if not gc:
        return False
    try:
        sh = gc.open_by_key(NOTE_ROADMAP_SPREADSHEET_ID)
        # gidでワークシートを特定
        ws = None
        for w in sh.worksheets():
            if w.id == NOTE_ROADMAP_GID:
                ws = w; break
        if ws is None:
            return False
        records = ws.get_all_values()
        for i, row in enumerate(records, start=1):
            if row and row[0].strip() == roadmap_date.strip():
                ws.update_cell(i, 7, True)  # col7 = 完了
                return True
        return False
    except Exception as e:
        print(f"[Sheets] 更新失敗: {e}", flush=True)
        return False

def _backup_to_sheets(category: str, filename: str, content: str):
    """成果物をGoogleSheetsの「成果物アーカイブ」シートにバックアップする（非同期呼び出し推奨）。"""
    gc = _get_sheets_client()
    if not gc:
        return
    try:
        sh = gc.open_by_key(NOTE_ROADMAP_SPREADSHEET_ID)
        try:
            ws = sh.worksheet("成果物アーカイブ")
        except Exception:
            ws = sh.add_worksheet("成果物アーカイブ", rows=2000, cols=5)
            ws.update("A1:E1", [["日時", "カテゴリ", "ファイル名", "内容（先頭3000文字）", "文字数"]])
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        ws.append_row(
            [now_str, category, filename, content[:3000], len(content)],
            value_input_option="RAW"
        )
    except Exception as e:
        print(f"[Sheets backup] 失敗: {e}", flush=True)


def _save_research(filename: str, content: str, category: str):
    """研究成果物をファイル保存 + Sheetsにバックアップ（非同期）。"""
    path = RESEARCH_DIR / filename
    path.write_text(content, encoding="utf-8")
    threading.Thread(target=_backup_to_sheets, args=(category, filename, content), daemon=True).start()
    return path


def _append_research(filename: str, new_content: str, existing: str, category: str):
    """先頭追記型成果物（x_strategy.md等）の保存 + Sheetsバックアップ（新規分のみ）。"""
    path = RESEARCH_DIR / filename
    path.write_text(new_content + "\n\n---\n\n" + existing, encoding="utf-8")
    threading.Thread(target=_backup_to_sheets, args=(category, filename, new_content), daemon=True).start()
    return path


_restore_session("SESSION_X_B64",    BASE_DIR / "automation" / "sessions" / "session_x.json")
_restore_session("SESSION_NOTE_B64", BASE_DIR / "automation" / "sessions" / "session_note.json")

# ─── Queue helpers ───────────────────────────────────────────────────────────
def _load_queue(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"x_posts": [], "note_drafts": []}


def _save_queue(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_to_pending(type_: str, item: dict):
    q = _load_queue(PENDING_PATH)
    q[type_].append(item)
    _save_queue(PENDING_PATH, q)


def approve_item(type_: str, item_id: str) -> bool:
    pending  = _load_queue(PENDING_PATH)
    approved = _load_queue(APPROVED_PATH)
    item = next((i for i in pending[type_] if i["id"] == item_id), None)
    if not item:
        return False
    pending[type_]  = [i for i in pending[type_] if i["id"] != item_id]
    item["approved_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    item["posted"]      = False
    approved[type_].append(item)
    _save_queue(PENDING_PATH,  pending)
    _save_queue(APPROVED_PATH, approved)
    return True


def reject_item(type_: str, item_id: str) -> bool:
    q = _load_queue(PENDING_PATH)
    before = len(q[type_])
    q[type_] = [i for i in q[type_] if i["id"] != item_id]
    if len(q[type_]) < before:
        _save_queue(PENDING_PATH, q)
        return True
    return False


# ─── State ───────────────────────────────────────────────────────────────────
state = {
    "running": False,
    "theme": "",
    "result": "",
    "agents": {
        # 幸子チャンネル部
        "researcher":          {"name": "幸子リサーチャー",  "dept": "sachiko",  "status": "idle", "task": ""},
        "scriptwriter":        {"name": "脚本家",             "dept": "sachiko",  "status": "idle", "task": ""},
        "sachiko_analyst":     {"name": "幸子分析",           "dept": "sachiko",  "status": "idle", "task": ""},
        # コンテンツ部（X + note 統合）
        "x_strategist":        {"name": "X戦略家",            "dept": "content",  "status": "idle", "task": ""},
        "x_writer":            {"name": "Xライター",           "dept": "content",  "status": "idle", "task": ""},
        "x_poster":            {"name": "X投稿管理",           "dept": "content",  "status": "idle", "task": ""},
        "note_researcher":     {"name": "noteリサーチャー",    "dept": "content",  "status": "idle", "task": ""},
        "note_writer":         {"name": "noteライター",        "dept": "content",  "status": "idle", "task": ""},
        # 営業部門
        "sales_researcher":    {"name": "営業リサーチャー",    "dept": "sales",    "status": "idle", "task": ""},
        "proposal_writer":     {"name": "提案書ライター",      "dept": "sales",    "status": "idle", "task": ""},
        # 転職アニメ部
        "tenshi_analyst":      {"name": "転職アニメ分析",      "dept": "tenshi",   "status": "idle", "task": ""},
        "tenshi_scriptwriter": {"name": "転職脚本家",          "dept": "tenshi",   "status": "idle", "task": ""},
        # 新規事業部
        "tieup_researcher":    {"name": "タイアップ探索",      "dept": "sachiko",  "status": "idle", "task": ""},
        "ip_strategist":       {"name": "IP戦略家",            "dept": "newbiz",   "status": "idle", "task": ""},
        "bizdev_researcher":   {"name": "外部マネタイズ探索",   "dept": "newbiz",   "status": "idle", "task": ""},
        # SNSマネタイズチーム
        "sns_planner":         {"name": "SNS企画担当",          "dept": "content",  "status": "idle", "task": ""},
        "sns_educator":        {"name": "SNS教育担当",          "dept": "content",  "status": "idle", "task": ""},
        "sns_marketer":        {"name": "SNSマーケ担当",        "dept": "content",  "status": "idle", "task": ""},
        # 秘書
        "secretary":           {"name": "秘書",                 "dept": "external", "status": "idle", "task": ""},
        # 外注
        "kikuchi":             {"name": "菊地（外注）",          "dept": "external", "status": "idle", "task": ""},
    },
    "logs": [],
    "kikuchi_progress":       {"episode": "-", "progress": 0, "status": "-", "due": "-"},
    "note_roadmap_progress":  {"done": 0, "total": 0, "progress": 0, "next_title": "-", "next_date": "-"},
    "marketing_insights":     [],
    "kpi": {
        "sachiko":  {"monthly_drafts": 0, "monthly_target": 6, "monthly_tieup": 0, "monthly_tieup_target": 30, "monthly_sponsor": 0, "monthly_sponsor_target": 20, "daily_draft": False, "daily_tieup": False, "daily_sponsor": False},
        "content":  {"monthly_note": 0, "monthly_note_target": 30, "monthly_x": 0, "monthly_x_target": 150, "monthly_education": 0, "monthly_education_target": 20, "monthly_marketing": 0, "monthly_marketing_target": 20, "daily_note": False, "daily_x": False, "daily_education": False, "daily_marketing": False},
        "sales":    {"monthly_lists": 0, "monthly_target": 30, "daily_list": False, "daily_proposal": False},
        "tenshi":   {"monthly_analysis": 0, "monthly_target": 20, "daily_analysis": False},
        "newbiz":   {"monthly_yt": 0, "monthly_yt_target": 20, "daily_yt": False},
        "last_updated": "",
    },
}


# ─── Core helpers ────────────────────────────────────────────────────────────
def log(msg: str, agent_id: str = None):
    with lock:
        state["logs"].insert(0, {
            "time": datetime.now(JST).strftime("%H:%M:%S"),
            "msg": msg,
            "agent": agent_id,
        })
        if len(state["logs"]) > 100:
            state["logs"] = state["logs"][:100]


def set_status(agent_id: str, status: str, task: str = ""):
    with lock:
        state["agents"][agent_id]["status"] = status
        state["agents"][agent_id]["task"]   = task
    log(f"{state['agents'][agent_id]['name']} → {task or status}", agent_id)


def make_agent(role, goal, backstory):
    return Agent(role=role, goal=goal, backstory=backstory,
                 verbose=False, allow_delegation=False, llm=LLM)


def run_single(description, expected_output, agent_obj, max_retries=3) -> str:
    for attempt in range(max_retries):
        try:
            task = Task(description=description, expected_output=expected_output, agent=agent_obj)
            crew = Crew(agents=[agent_obj], tasks=[task], process=Process.sequential, verbose=False)
            return str(crew.kickoff())
        except Exception as e:
            err = str(e)
            is_overload = any(k in err for k in ("503", "UNAVAILABLE", "high demand", "overloaded", "quota"))
            if is_overload and attempt < max_retries - 1:
                wait_sec = 30 * (attempt + 1)
                log(f"⏳ API高負荷 再試行({attempt+1}/{max_retries-1}) {wait_sec}秒後...")
                time.sleep(wait_sec)
                continue
            raise


def parse_x_posts(text: str) -> list[str]:
    """【投稿N】フォーマットからX投稿テキストを抽出する。"""
    posts, current, in_post = [], [], False
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("【投稿") and "】" in s:
            if current:
                posts.append(" ".join(current).strip())
                current = []
            in_post = True
            after = s[s.find("】") + 1:].strip()
            if after:
                current.append(after)
        elif in_post and s:
            current.append(s)
    if current:
        posts.append(" ".join(current).strip())
    return posts if posts else [p.strip() for p in text.split("\n\n") if p.strip()][:7]


def _read_strategy(filename: str) -> str:
    p = RESEARCH_DIR / filename
    return p.read_text(encoding="utf-8")[:2000] if p.exists() else ""


def load_soul(agent_id: str) -> dict:
    """agents/{id}/SOUL.md から role/goal/backstory を読み込む。"""
    path = AGENTS_DIR / agent_id / "SOUL.md"
    if not path.exists():
        return {}
    result, key, buf = {}, None, []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if line.startswith("## "):
            if key and buf:
                result[key] = "\n".join(buf).strip()
            key = line[3:].strip().lower()
            buf = []
        elif key and not line.startswith("#"):
            buf.append(line)
    if key and buf:
        result[key] = "\n".join(buf).strip()
    return result


def make_agent_soul(agent_id: str):
    """SOUL.md を読んでエージェントを生成する。"""
    soul = load_soul(agent_id)
    return Agent(
        role=soul.get("role", agent_id),
        goal=soul.get("goal", ""),
        backstory=soul.get("backstory", ""),
        verbose=False, allow_delegation=False, llm=LLM
    )


def make_researcher_with_tools() -> Agent:
    """YouTube + DuckDuckGo 検索ツール付きの幸子リサーチャーを生成する。"""
    soul = load_soul("researcher")
    return Agent(
        role=soul.get("role", "幸子リサーチャー"),
        goal=soul.get("goal", ""),
        backstory=soul.get("backstory", ""),
        tools=[youtube_search_tool, web_search_tool],
        verbose=False,
        allow_delegation=False,
        llm=LLM,
    )


# ─── Manual Run ──────────────────────────────────────────────────────────────
def run_crew(theme: str):
    """YouTube/Web検索 → 聖書準拠脚本家の2フェーズパイプライン（手動トリガー用）。"""
    with lock:
        state["running"] = True
        state["theme"]   = theme
        state["result"]  = ""
        for aid in ["researcher", "scriptwriter"]:
            state["agents"][aid].update({"status": "idle", "task": ""})

    log(f"🏢 台本エージェント起動 — {theme}")
    try:
        set_status("researcher", "working", f"「{theme}」YouTube・Webリサーチ中...")
        sequence, tick = ["researcher", "scriptwriter"], [0]

        def cb(output):
            set_status(sequence[tick[0]], "done", "完了 ✓")
            tick[0] += 1
            if tick[0] < len(sequence):
                nxt = sequence[tick[0]]
                set_status(nxt, "working", "台本骨子を執筆中..." if nxt == "scriptwriter" else "作業中...")

        # Phase1: 検索ツール付きリサーチャー
        ra = make_researcher_with_tools()
        # Phase2: 聖書ルール内蔵の脚本家（SOUL.md から読み込み済み）
        sa = make_agent_soul("scriptwriter")

        t1 = Task(
            description=(
                f"テーマ「{theme}」について幸子チャンネル向けのリサーチを実施してください。\n"
                "必ずyoutube_search_toolとweb_search_toolを使うこと（AIの記憶だけで書かない）。\n"
                "① 類似テーマでYouTubeで伸びている動画のタイトル・共通パターン（ツール検索結果から）\n"
                "② 視聴者が「これ私のことだ」と感じるポイント3つ\n"
                "③ メタファー候補2つ（前半→後半で意味が変わるもの）\n"
                "④ 田中さんの無自覚な一言の例2つ（本人は心配しているつもりの発言）"
            ),
            expected_output="4項目のリサーチレポート（ツール取得データを根拠として明記）",
            agent=ra,
        )
        t2 = Task(
            description=(
                "リサーチ結果をもとに、SOUL.mdの台本聖書ルールに完全準拠した台本骨子を1本作成してください。\n"
                "必ず以下のフォーマット通りに出力すること：\n"
                "【タイトル案】A・B各1つ / 【冒頭の一景（0-5秒）】/ 【メタファー】/ "
                "【第一幕〜第五幕】各100文字 / 【田中さんの一言】/ 【中島さんのフォロー】"
            ),
            expected_output="台本骨子（SOUL.md指定フォーマット・800〜1200文字）",
            agent=sa,
        )
        crew = Crew(agents=[ra, sa], tasks=[t1, t2], process=Process.sequential,
                    verbose=False, task_callback=cb)
        result = str(crew.kickoff())
        today = datetime.now(JST).strftime("%Y%m%d")
        _save_research(f"script_draft_{today}.txt",
                       f"=== 台本骨子ドラフト {today} ===\nテーマ：{theme}\n\n{result}", "sachiko")
        with lock:
            state["result"] = result
        log("✅ 台本骨子完了！")
    except Exception as e:
        log(f"❌ エラー: {str(e)[:200]}")
        for aid in ["researcher", "scriptwriter"]:
            if state["agents"][aid]["status"] == "working":
                set_status(aid, "error", "エラー発生")
    finally:
        with lock:
            state["running"] = False


# ─── Job: 秘書ブリーフィング (daily 07:30) ──────────────────────────────
def job_secretary_briefing():
    today = datetime.now(JST).strftime("%Y-%m-%d")
    log("🎩 秘書エージェント起動 — 朝のブリーフィング作成")
    try:
        set_status("secretary", "working", "状況を集約中...")
        pending  = _load_queue(PENDING_PATH)
        approved = _load_queue(APPROVED_PATH)
        x_pend   = len(pending["x_posts"])
        n_pend   = len(pending["note_drafts"])
        x_appr   = len([p for p in approved["x_posts"]     if not p.get("posted")])
        n_appr   = len([p for p in approved["note_drafts"] if not p.get("published")])
        recent   = [f.name for f in sorted(RESEARCH_DIR.glob("*.txt"), reverse=True)[:3]]

        # roadmap.mdから今月タスクを抽出
        roadmap_hint = ""
        roadmap_path = BASE_DIR / "roadmap.md"
        if roadmap_path.exists():
            rm_text = roadmap_path.read_text(encoding="utf-8")
            import calendar
            month_jp = f"{datetime.now(JST).month}月"
            lines = rm_text.split("\n")
            in_month, buf = False, []
            for line in lines:
                if f"## {month_jp}" in line:
                    in_month = True
                elif in_month and line.startswith("## ") and f"{month_jp}" not in line:
                    break
                elif in_month:
                    buf.append(line)
            if buf:
                roadmap_hint = "\n今月のロードマップ（抜粋）:\n" + "\n".join(buf[:30])

        context = (
            f"現在の状況（{today} 07:30 JST）\n"
            f"承認待ち: X投稿{x_pend}件・note記事{n_pend}件\n"
            f"承認済み（未投稿）: X投稿{x_appr}件・note記事{n_appr}件\n"
            f"最近の生成ファイル: {', '.join(recent) if recent else 'なし'}\n"
            + roadmap_hint +
            "\n\n今日の自動実行:\n"
            "・08:30 X戦略家デイリーリサーチ（AIアニメXトレンド把握・毎日）\n"
            "・09:00 幸子テーマリサーチ＋台本骨子＋Xドラフト7件生成\n"
            "・09:30 営業リサーチ→提案書生成（毎日）\n"
            "・10:00 転職アニメ分析（毎日）\n"
            "・10:30 note記事ドラフト生成（毎日）\n"
            "・11:00 タイアップリサーチ（毎日）\n"
            "・11:30 転職脚本（GO判定時のみ）\n"
            "・月曜8:00 全部門週次サマリーレポート生成\n"
            "・8/12/18/20時 承認済みX投稿から自動投稿（1日4回）"
        )
        agent = make_agent_soul("secretary")
        result = run_single(
            f"以下の状況を踏まえ、監督への朝のブリーフィングを作成:\n\n{context}\n\n"
            "形式（必ず守る）:\n"
            "【今すぐやること（優先度順）】\n"
            "1. ...\n"
            "2. ...\n"
            "【今日の自動実行スケジュール】（1〜2行で）\n"
            "【一言メッセージ】（モチベーション or 気づき、1行）\n"
            "全体200文字以内・箇条書きのみ・余計な説明不要",
            "朝のブリーフィング（200文字以内）",
            agent
        )
        set_status("secretary", "done", "ブリーフィング完成 ✓")
        log("🎩 朝のブリーフィング完成", "secretary")
        with lock:
            state["result"] = f"🎩 朝のブリーフィング {today}\n\n{result}"
    except Exception as e:
        log(f"❌ 秘書エラー: {str(e)[:200]}")
        set_status("secretary", "error", "エラー")


# ─── Job: 幸子デイリーリサーチ (daily 09:00) ─────────────────────────────
def job_sachiko_research():
    today = datetime.now(JST).strftime("%Y%m%d")
    log("🌅 幸子チャンネル デイリーリサーチ開始（YouTube・Web検索）")
    try:
        set_status("researcher", "working", "YouTube・Webリサーチ中...")
        agent = make_researcher_with_tools()
        result = run_single(
            "幸子チャンネル向けエピソードテーマを3つ提案してください。\n"
            "必ずyoutube_search_toolとweb_search_toolを使うこと（AIの記憶だけで書かない）。\n"
            "検索クエリ例：「シニア 年金 YouTube」「老後 パート 女性 動画」「65歳 主婦 悩み」\n\n"
            "各テーマ：\n"
            "①タイトル案（60文字目安）\n"
            "②冒頭の一景（0-5秒）\n"
            "③メタファー候補（前半→後半で意味が変わる物）\n"
            "④田中さんの無自覚な一言（本人は心配しているつもりの発言）\n\n"
            "参考：幸子65歳・スーパーレジパート・年金10.3万・視聴者55-64歳女性70%",
            "テーマ提案3件（各4項目・ツール検索データ根拠付き）",
            agent,
        )
        set_status("researcher", "done", "リサーチ完了 ✓")
        _save_research(f"sachiko_auto_{today}.txt",
                       f"=== 幸子デイリーリサーチ {today} ===\n\n{result}", "sachiko")
        log(f"💾 保存: sachiko_auto_{today}.txt")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ 幸子リサーチエラー: {str(e)[:200]}")
        set_status("researcher", "error", "エラー")


# ─── Job: X戦略デイリーリサーチ (daily 08:30) ───────────────────────────
def job_x_strategy_learn():
    today = datetime.now(JST).strftime("%Y/%m/%d")
    log("🧠 X戦略家デイリーリサーチ開始")
    try:
        set_status("x_strategist", "working", "AIアニメXトレンドをリサーチ中...")
        agent = make_agent_soul("x_strategist")
        result = run_single(
            f"今日（{today}）のAIアニメ・AI動画・YouTube収益化界隈のXトレンドを分析してください。\n\n"
            "SOUL.mdの形式に従い以下を必ず含めること:\n"
            "①今日のAIアニメ界隈トレンド（AIコンテスト情報含む。コロテック等のコンテストがあれば必ず記録）\n"
            "②今日のXマーケティング学習（1つの具体的な知識・データ）\n"
            "③明日の投稿戦略（テーマ2〜3つ・参加すべき会話・避けるテーマ）\n\n"
            f"必ずこの形式で出力すること:\n"
            f"=== Xデイリー戦略レポート {today} ===\n\n"
            "【本日のAIアニメ界隈トレンド】\n"
            "【本日のXマーケ学習】\n"
            "【明日の投稿戦略】",
            "Xデイリー戦略レポート（マークダウン形式）", agent)
        set_status("x_strategist", "done", "デイリーレポート完成 ✓")
        existing = (RESEARCH_DIR / "x_strategy.md").read_text(encoding="utf-8") if (RESEARCH_DIR / "x_strategy.md").exists() else ""
        _append_research("x_strategy.md", result, existing, "content")
        log("💾 X戦略デイリーレポート追記: research/x_strategy.md")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ X戦略リサーチエラー: {str(e)[:200]}")
        set_status("x_strategist", "error", "エラー")


# ─── Job: X投稿ドラフト生成 (daily 09:30) ────────────────────────────────
def job_generate_x_drafts():
    log("✍️ X投稿ドラフト生成開始")
    try:
        set_status("x_writer", "working", "投稿ドラフト生成中...")
        strategy = _read_strategy("x_strategy.md")
        agent = make_agent_soul("x_writer")
        if strategy:
            agent.backstory = agent.backstory + f"\n\n最新戦略:\n{strategy[:1500]}"
        directive_text = _get_agent_directive_text("x_writer")
        result = run_single(
            directive_text +
            "X投稿を7件作成してください。SOUL.mdのゴール・ターゲット・テーマの柱に従うこと。\n"
            "テーマのローテーション必須（AI実績の数字・制作ノウハウ・一人会社リアル・逆張り・読者の悩みへの共感）\n"
            "各投稿140文字以内・絵文字あり・ハッシュタグ2〜3個。\n"
            "各投稿の前に【投稿1】【投稿2】...と番号を必ず付けること。",
            "7件のX投稿ドラフト（各140文字以内）", agent)
        posts = parse_x_posts(result)
        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        for post in posts:
            add_to_pending("x_posts", {
                "id":  str(uuid.uuid4())[:8],
                "text": post,
                "created_at": now_str,
            })
        set_status("x_writer", "done", f"{len(posts)}件生成 → 承認待ち ✓")
        log(f"📥 {len(posts)}件のX投稿ドラフトを承認待ちキューに追加")
        with lock:
            state["result"] = result
    except Exception as e:
        import traceback
        log(f"❌ X投稿生成エラー [{type(e).__name__}]: {str(e)[:300]}")
        log(f"詳細: {traceback.format_exc()[-300:]}")
        set_status("x_writer", "error", f"{type(e).__name__}")


# ─── Job: X承認済みをランダム投稿 (08:00 / 12:00 / 20:00) ──────────────
def job_x_random_post():
    approved = _load_queue(APPROVED_PATH)
    unposted = [p for p in approved["x_posts"] if not p.get("posted")]
    if not unposted:
        log("𝕏 承認済みキューが空 — 投稿スキップ", "x_poster")
        return

    post = random.choice(unposted)
    preview = post["text"][:30]
    set_status("x_poster", "working", f"投稿中: {preview}...")

    session_path = AUTOMATION_DIR / "sessions" / "session_x.json"
    x_auto = os.getenv("X_AUTO_POST", "false").lower() == "true"

    if session_path.exists() and x_auto:
        try:
            cmd = [sys.executable, str(AUTOMATION_DIR / "post_x.py"),
                   "--text", post["text"], "--publish"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=90, cwd=str(BASE_DIR))
            if r.returncode == 0:
                post["posted"]    = True
                post["posted_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
                _save_queue(APPROVED_PATH, approved)
                log(f"✅ X投稿完了: {preview}...", "x_poster")
                set_status("x_poster", "done", "投稿完了 ✓")
            else:
                log(f"⚠ X投稿失敗: {r.stderr[:80]}", "x_poster")
                set_status("x_poster", "error", "投稿失敗")
        except Exception as e:
            log(f"❌ X投稿例外: {str(e)[:80]}", "x_poster")
            set_status("x_poster", "error", "エラー")
    else:
        reason = "X_AUTO_POST=falseのためドライラン" if session_path.exists() else "session_x.json なし"
        log(f"𝕏 {reason}: '{preview}...'", "x_poster")
        set_status("x_poster", "done", f"ドライラン ({reason})")


# ─── Job: noteリサーチ (weekly Sun 23:30) ────────────────────────────────
def job_note_research():
    log("🔍 noteリサーチャー起動")
    try:
        set_status("note_researcher", "working", "noteトレンドリサーチ中...")
        agent = make_agent(
            "noteマーケットリサーチャー",
            "noteでバズる記事の法則を分析しAI・副業・YouTube収益化ジャンルのコンテンツ戦略を立案する",
            "note.comの日本語マーケット専門家。AI・副業・YouTube収益化ジャンルのトレンドを追跡。")
        result = run_single(
            "noteで「AI・YouTube収益化・副業」ジャンルの人気記事パターンを分析:\n"
            "①バズるタイトルの法則5つ（テンプレート付き）\n"
            "②読まれる冒頭フック3パターン（文例付き）\n"
            "③今売れるテーマキーワード10個\n"
            "④有料記事（500〜1000円）と無料記事の使い分け戦略\n"
            "⑤AIアニメ制作ノウハウが売れる理由と適正価格帯",
            "noteコンテンツ戦略レポート（マークダウン形式）", agent)
        set_status("note_researcher", "done", "戦略ガイド更新 ✓")
        _save_research("note_strategy.md",
                       f"# noteコンテンツ戦略\n更新: {datetime.now(JST).strftime('%Y-%m-%d')}\n\n{result}", "content")
        log("💾 note戦略更新: research/note_strategy.md")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ noteリサーチエラー: {str(e)[:200]}")
        set_status("note_researcher", "error", "エラー")


# ─── Job: noteロードマップ進捗チェック ─────────────────────────────────────
NOTE_ROADMAP_URL = ("https://docs.google.com/spreadsheets/d/"
                    "1xHIRrC4e4eJGuvnE84n7xERZYEzu4SApTroB4xYknM0"
                    "/export?format=csv&gid=1883610657")

def job_note_roadmap_progress_update():
    """note 30日ロードマップスプシの完了列（col6: TRUE/FALSE）を読んで進捗を更新。"""
    import urllib.request, csv as _csv
    from datetime import date as _date
    try:
        with urllib.request.urlopen(NOTE_ROADMAP_URL, timeout=15) as resp:
            content = resp.read().decode("utf-8")
        rows = list(_csv.reader(content.splitlines()))
        done = total = 0
        next_title = next_date = None
        today_title = today_date_str = None
        today_dt = datetime.now(JST).date()
        for row in rows:
            if len(row) >= 7 and row[6].strip().upper() in ("TRUE", "FALSE"):
                col0 = row[0].strip()
                if "/" in col0 and col0.split("/")[0].isdigit():
                    total += 1
                    if row[6].strip().upper() == "TRUE":
                        done += 1
                    else:
                        if next_title is None:
                            next_title = row[2].strip()
                            next_date  = col0
                        # 今日10:30に生成される記事（日付≤今日の最初の未完了）
                        if today_title is None:
                            try:
                                m, d = int(col0.split("/")[0]), int(col0.split("/")[1])
                                if _date(today_dt.year, m, d) <= today_dt:
                                    today_title    = row[2].strip()
                                    today_date_str = col0
                            except Exception:
                                pass
        pct = int(done / total * 100) if total > 0 else 0
        with lock:
            prev = state["note_roadmap_progress"].get("progress", -1)
            state["note_roadmap_progress"] = {
                "done": done, "total": total, "progress": pct,
                "next_title":  next_title      or "-",
                "next_date":   next_date       or "-",
                "today_title": today_title     or "-",
                "today_date":  today_date_str  or "-",
            }
        if pct != prev:
            log(f"📝 note進捗: {pct}% ({done}/{total}本)", "note_writer")
    except Exception as e:
        log(f"⚠ note進捗取得失敗: {str(e)[:80]}")


# ─── Job: note記事ドラフト生成（ロードマップ対応）──────────────────────────
def job_generate_note_draft():
    """ロードマップから今日の記事を特定→リサーチ→執筆→承認キューへ。"""
    import urllib.request, csv as _csv
    from datetime import date as _date
    log("📝 note記事ドラフト生成開始")
    try:
        # ── 1. ロードマップから執筆対象を特定 ──
        with urllib.request.urlopen(NOTE_ROADMAP_URL, timeout=15) as resp:
            content = resp.read().decode("utf-8")
        rows = list(_csv.reader(content.splitlines()))
        today = datetime.now(JST)
        target = None
        for row in rows:
            if len(row) >= 7 and row[6].strip().upper() == "FALSE":
                col0 = row[0].strip()
                if "/" in col0 and col0.split("/")[0].isdigit():
                    m, d = int(col0.split("/")[0]), int(col0.split("/")[1])
                    if _date(today.year, m, d) <= today.date():
                        target = row; break
        if not target:
            # 今日以前に未完了がない → 次回予定の最初の記事
            for row in rows:
                if len(row) >= 7 and row[6].strip().upper() == "FALSE":
                    col0 = row[0].strip()
                    if "/" in col0 and col0.split("/")[0].isdigit():
                        target = row; break
        if not target:
            log("📝 note: ロードマップ上の未完了記事なし（全完了）")
            return

        art_date  = target[0].strip()
        art_title = target[2].strip()
        art_cat   = target[3].strip()   # 無料 / 有料

        set_status("note_writer", "working", f"{art_title[:20]}... 執筆中")

        # ── 2. リサーチフェーズ（URL付き） ──
        researcher = make_researcher_with_tools()
        research = run_single(
            f"次のnote記事を書くためにリサーチしてください。\n"
            f"記事タイトル：「{art_title}」\n"
            f"YouTubeで再生数の多い関連動画を3本（タイトル・URL・なぜ伸びたか）と"
            f"Web参考記事を3本（タイトル・URL・核心の学び）を調べてください。",
            "リサーチ結果（YouTube URL・Web URL・要点を含むレポート）",
            researcher
        )

        # ── 3. 執筆フェーズ ──
        strategy = _read_strategy("note_strategy.md")
        writer = make_agent(
            "noteコンテンツライター",
            "AIアニメ制作・YouTube収益化ノウハウをnote向けに2000〜3000文字の記事にまとめる",
            "StudioOgawaのAIアニメ制作専門家。幸子チャンネル95万再生・転職アフィリ月20万の実績。"
            + ("\nnote戦略:\n" + strategy if strategy else "")
            + f"\nリサーチ結果:\n{research}"
        )
        result = run_single(
            f"タイトル：「{art_title}」の記事を書いてください。\n"
            f"カテゴリ：{art_cat}記事（{'無料公開' if art_cat == '無料' else '有料500〜800円'}）\n"
            f"形式：タイトル・冒頭共感フック（150文字）・H2×3〜4章（各400〜600文字）・まとめ・ハッシュタグ5個\n"
            f"具体的な数字・体験・ノウハウを盛り込み、参照URL一覧を末尾に記載してください。",
            "完成したnote記事（タイトル＋本文2000〜3000文字、参考URL付き）",
            writer
        )

        lines  = result.strip().split("\n")
        title  = lines[0].lstrip("# ").strip() if lines else art_title
        body   = "\n".join(lines[1:]).strip() if len(lines) > 1 else result

        add_to_pending("note_drafts", {
            "id":           str(uuid.uuid4())[:8],
            "title":        title,
            "body":         body,
            "roadmap_date": art_date,   # 承認時にスプシを自動チェックするために保存
            "created_at":   datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        })
        set_status("note_writer", "done", f"{title[:20]}... → 承認待ち ✓")
        log(f"📥 note記事ドラフト追加: {title[:30]}...")
        with lock:
            state["result"] = result
        job_note_roadmap_progress_update()
    except Exception as e:
        log(f"❌ note記事生成エラー: {str(e)[:200]}")
        set_status("note_writer", "error", "エラー")


# ─── Job: X/noteマーケリサーチ（毎週月・水・金）────────────────────────────
MARKETING_TOPICS = [
    "note 2026年 人気記事 フォロワー増 稼ぎ方 コツ",
    "X Twitter フォロワー増やし方 2026 バズ投稿 戦略",
    "AIアニメ YouTube 副業 月10万 稼ぐ方法",
    "note 有料記事 売れる書き方 タイトル 冒頭フック",
    "シニア向け YouTube チャンネル 収益化 RPM",
    "note AIコンテンツ 副業 売上 実績公開",
]

def job_marketing_research():
    """X/noteマーケノウハウをYouTube・Webで定期リサーチ。URL付きで報告。"""
    log("🔬 マーケリサーチ開始")
    try:
        set_status("note_researcher", "working", "マーケノウハウ吸収中...")
        topic_idx = datetime.now(JST).weekday() % len(MARKETING_TOPICS)
        topic = MARKETING_TOPICS[topic_idx]

        researcher = make_researcher_with_tools()
        result = run_single(
            f"テーマ：「{topic}」でYouTubeとWebをリサーチしてください。\n"
            f"① YouTube人気動画3本（タイトル・URL・再生数・なぜ伸びたか分析）\n"
            f"② Web参考記事3本（タイトル・URL・核心の学び1行）\n"
            f"③ これらから得るX/note実践インサイト3〜5箇条（具体的なアクション付き）\n"
            f"URLは必ず実際のものを記載してください。",
            "マーケリサーチレポート（YouTube URL・Web URL・実践インサイト付き）",
            researcher
        )
        with lock:
            insights = state.setdefault("marketing_insights", [])
            insights.insert(0, {
                "date":    datetime.now(JST).strftime("%m/%d %H:%M"),
                "topic":   topic,
                "content": result,
            })
            state["marketing_insights"] = insights[:10]
        set_status("note_researcher", "done", "マーケインサイト更新 ✓")
        log(f"💡 マーケリサーチ完了: {topic[:30]}")
        fname = f"marketing_{datetime.now(JST).strftime('%Y%m%d_%H%M')}.md"
        _save_research(fname, f"# マーケインサイト\nトピック: {topic}\n\n{result}", "content")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ マーケリサーチエラー: {str(e)[:200]}")
        set_status("note_researcher", "error", "エラー")


# ─── Job: タイアップリサーチ (weekly Mon 11:00) ──────────────────────────
def job_tieup_research():
    today = datetime.now(JST).strftime("%Y%m%d")
    log("🤝 タイアップリサーチャー起動")
    try:
        set_status("tieup_researcher", "working", "パートナー候補を探索中...")
        agent = make_agent(
            "タイアップ・マーケティングリサーチャー",
            "シニア向けYouTubeチャンネルへのタイアップ候補を発掘し具体的なアプローチ戦略を立てる",
            "幸子チャンネル（登録2900人・累計95万再生・RPM¥329）。視聴者：55-65歳女性70%。")
        result = run_single(
            "幸子チャンネルへのタイアップ・スポンサー候補を分析:\n"
            "①業種別候補5件（会社名・理由・想定月額・アプローチ方法）\n"
            "②現在の規模（登録2900人）で受注は現実的か？\n"
            "③最初にアプローチすべき1社と具体的なDMメッセージ例（200文字）\n"
            "④タイアップ受注しやすくなる目標登録者数の目安",
            "タイアップ候補リスト＋アプローチ戦略レポート", agent)
        set_status("tieup_researcher", "done", "候補リスト完成 ✓")
        _save_research(f"tieup_auto_{today}.txt",
                       f"=== タイアップリサーチ {today} ===\n\n{result}", "newbiz")
        log(f"💾 タイアップレポート: tieup_auto_{today}.txt")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ タイアップリサーチエラー: {str(e)[:200]}")
        set_status("tieup_researcher", "error", "エラー")


# ─── Job: 台本骨子ドラフト生成 (daily 09:00) ─────────────────────────────
def job_scriptwriter_daily():
    today = datetime.now(JST).strftime("%Y%m%d")
    log("🎬 台本骨子ドラフト生成開始")
    try:
        set_status("scriptwriter", "working", "台本骨子を執筆中...")
        research_file = RESEARCH_DIR / f"sachiko_auto_{today}.txt"
        research_hint = research_file.read_text(encoding="utf-8")[:1500] if research_file.exists() else ""
        agent = make_agent_soul("scriptwriter")
        result = run_single(
            "今日の幸子チャンネル向けエピソードの台本骨子を1本作成してください。\n" +
            (f"参考リサーチ:\n{research_hint}\n\n" if research_hint else "") +
            "SOUL.mdの指定フォーマット通りに出力すること（タイトル案2つ・6フェーズ・メタファー・田中の一言・中島のフォロー）",
            "台本骨子（6フェーズ・800〜1200文字）", agent)
        set_status("scriptwriter", "done", "台本骨子完了 ✓")
        _save_research(f"script_draft_{today}.txt",
                       f"=== 台本骨子ドラフト {today} ===\n\n{result}", "sachiko")
        log(f"💾 台本骨子保存: script_draft_{today}.txt")
        with lock:
            state["result"] = f"🎬 台本骨子ドラフト\n\n{result}"
    except Exception as e:
        log(f"❌ 台本骨子エラー: {str(e)[:200]}")
        set_status("scriptwriter", "error", "エラー")


# ─── Job: 菊地進捗チェック (hourly) ──────────────────────────────────────
def job_kikuchi_progress_update():
    import urllib.request, csv as _csv
    try:
        url = ("https://docs.google.com/spreadsheets/d/"
               "1xHIRrC4e4eJGuvnE84n7xERZYEzu4SApTroB4xYknM0"
               "/export?format=csv&gid=595521756")
        with urllib.request.urlopen(url, timeout=15) as resp:
            content = resp.read().decode("utf-8")
        rows = list(_csv.reader(content.splitlines()))

        kikuchi_rows = []
        for row in rows[2:]:
            if len(row) >= 6 and "菊地" in row[1]:
                kikuchi_rows.append(row)
        if not kikuchi_rows:
            return

        def detail_progress(ep_str):
            """詳細タスクセクション「【第N話】」のTRUE/FALSEを集計して (done, total) を返す。"""
            hdr = f"【{ep_str}】"
            start = None
            for i, r in enumerate(rows):
                if r and r[0] and hdr in r[0]:
                    start = i + 1; break
            d, t = 0, 0
            if start is not None:
                for r in rows[start:]:
                    if not r: continue
                    c0 = r[0].strip()
                    if c0.startswith("【") and "話】" in c0 and hdr not in c0:
                        break
                    if len(r) >= 4 and r[3].strip().upper() in ("TRUE", "FALSE"):
                        t += 1
                        if r[3].strip().upper() == "TRUE":
                            d += 1
            return d, t

        # 制作中→未着手の順で「詳細タスクが未完了のもの」を探す
        # 詳細100%なら完了扱いにして次候補へ進む
        target_row, done, total = None, 0, 0
        for priority in ("制作中", "未着手"):
            for row in kikuchi_rows:
                if row[4].strip() == priority:
                    ep = row[0].strip()
                    d, t = detail_progress(ep)
                    if t == 0 or d < t:   # 詳細未完了 or 詳細シートなし
                        target_row = row; done = d; total = t; break
            if target_row:
                break

        # 全エピソード完了 → 最後のエピソードを表示
        if not target_row:
            target_row = kikuchi_rows[-1]
            ep = target_row[0].strip()
            done, total = detail_progress(ep)

        # 詳細シートがない場合はサマリーの5チェックにフォールバック
        if total == 0:
            checks = [target_row[i].strip() for i in range(5, 10) if i < len(target_row)]
            done   = sum(1 for c in checks if c.upper() == "TRUE")
            total  = len(checks) if checks else 5

        ep_name  = target_row[0].strip()
        status   = target_row[4].strip()
        due_date = target_row[2].strip()
        pct = int(done / total * 100) if total > 0 else 0
        agent_status = "done" if pct >= 100 else "working"
        with lock:
            prev_pct = state["kikuchi_progress"].get("progress", -1)
            state["agents"]["kikuchi"]["status"] = agent_status
            state["agents"]["kikuchi"]["task"]   = f"{ep_name} {pct}%"
            state["kikuchi_progress"] = {
                "episode": ep_name, "progress": pct,
                "done": done, "total": total,
                "status": status, "due": due_date
            }
        if pct != prev_pct:
            log(f"📊 菊地進捗: {ep_name} {pct}% ({done}/{total}タスク)", "kikuchi")
    except Exception as e:
        log(f"⚠ 菊地進捗取得失敗: {str(e)[:80]}")


# ─── Job: 幸子チャンネル分析 (weekly Tue 09:00) ──────────────────────────────
def job_sachiko_analytics():
    today = datetime.now(JST).strftime("%Y%m%d")
    log("📊 幸子チャンネル分析開始", "sachiko_analyst")
    try:
        set_status("sachiko_analyst", "working", "チャンネルデータ分析中...")
        # 直近の分析ファイル・リサーチファイルを参照
        research_files = sorted(RESEARCH_DIR.glob("sachiko_auto_*.txt"))
        past_data = ""
        if research_files:
            past_data = research_files[-1].read_text(encoding="utf-8")[:3000]
        agent = make_agent_soul("sachiko_analyst")
        result = run_single(
            "幸子チャンネルの直近パフォーマンスを分析してください。\n"
            "利用可能なデータ:\n" + (past_data or "（直近リサーチファイルなし）") + "\n\n"
            "分析項目:\n"
            "①現状の再生数トレンドと月間目標達成率\n"
            "②最もパフォーマンスが良かった話のタイトル傾向とその理由\n"
            "③視聴者55-64歳女性層に刺さるテーマキーワード（次回作向け）\n"
            "④競合シニア系アニメチャンネルとの差別化ポイント\n"
            "⑤次の1本で変えるべき点3つ（具体的に）",
            "幸子チャンネル週次分析レポート", agent)
        set_status("sachiko_analyst", "done", "週次レポート完成 ✓")
        _save_research(f"sachiko_analysis_{today}.txt",
                       f"=== 幸子チャンネル分析 {today} ===\n\n{result}", "sachiko")
        log(f"💾 幸子分析レポート: sachiko_analysis_{today}.txt", "sachiko_analyst")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ 幸子分析エラー: {str(e)[:200]}")
        set_status("sachiko_analyst", "error", "エラー")


# ─── Job: 営業リサーチ (weekly Wed 09:00) ────────────────────────────────────
def job_sales_research():
    today = datetime.now(JST).strftime("%Y%m%d")
    log("🔍 営業リサーチャー起動", "sales_researcher")
    try:
        set_status("sales_researcher", "working", "見込み客リストを調査中...")
        agent = make_agent_soul("sales_researcher")
        result = run_single(
            "StudioOgawaのAIアニメ制作受託（1本¥30,000〜50,000・制作期間2〜3日）の\n"
            "見込み客企業を10社リストアップしてください。\n"
            "各社について：①会社名 ②業種 ③なぜアニメ動画が刺さるか ④担当部署・役職\n"
            "⑤最適アプローチ方法 ⑥優先度（高/中/低）\n"
            "優先度「高」は採用PR・会社紹介・サービス説明に今すぐ動画が必要そうな会社。\n"
            "実在する日本企業・実名で記載すること。",
            "BtoB見込み客リスト10社（Markdown表形式）", agent)
        set_status("sales_researcher", "done", "リスト10社完成 ✓")
        _save_research(f"sales_prospects_{today}.txt",
                       f"=== BtoB見込み客リスト {today} ===\n\n{result}", "sales")
        log(f"💾 営業リスト: sales_prospects_{today}.txt", "sales_researcher")
        with lock:
            state["result"] = result
        # リサーチ完了後に提案書生成を自動起動
        threading.Thread(target=job_proposal_generate, daemon=True).start()
    except Exception as e:
        log(f"❌ 営業リサーチエラー: {str(e)[:200]}")
        set_status("sales_researcher", "error", "エラー")


# ─── Job: 提案書生成 (営業リサーチ後に自動実行) ──────────────────────────────
def job_proposal_generate():
    today = datetime.now(JST).strftime("%Y%m%d")
    log("📄 提案書ライター起動", "proposal_writer")
    try:
        set_status("proposal_writer", "working", "提案書・DM文を生成中...")
        # 最新の営業リストを読み込む
        prospect_files = sorted(RESEARCH_DIR.glob("sales_prospects_*.txt"))
        prospects = ""
        if prospect_files:
            prospects = prospect_files[-1].read_text(encoding="utf-8")[:2000]
        agent = make_agent_soul("proposal_writer")
        result = run_single(
            "以下の見込み客リストの中から優先度「高」の企業3社を選び、\n"
            "各社向けのアプローチDM（200文字）と提案書テキスト（A4 1枚相当）を生成してください。\n\n"
            "見込み客リスト:\n" + (prospects or "（リストなし）") + "\n\n"
            "StudioOgawa実績：95万再生・登録者2,900人・制作コスト1本¥1,000〜1,500・期間2〜3日",
            "3社分のDM文＋提案書テキスト", agent)
        set_status("proposal_writer", "done", "提案書3社分完成 ✓")
        _save_research(f"proposals_{today}.txt",
                       f"=== BtoB提案書 {today} ===\n\n{result}", "sales")
        log(f"💾 提案書: proposals_{today}.txt", "proposal_writer")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ 提案書生成エラー: {str(e)[:200]}")
        set_status("proposal_writer", "error", "エラー")


# ─── Job: 転職アニメ分析 (weekly Thu 09:00) ──────────────────────────────────
def job_tenshi_analyze():
    today = datetime.now(JST).strftime("%Y%m%d")
    log("🎬 転職アニメアナリスト起動", "tenshi_analyst")
    try:
        set_status("tenshi_analyst", "working", "非成約原因を分析中...")
        # 既存スクリプトのリストを取得
        tenshi_scripts_dir = BASE_DIR / "tenshi_scripts"
        script_list = ""
        if tenshi_scripts_dir.exists():
            files = sorted(tenshi_scripts_dir.glob("*.txt"))[:5]  # 直近5本
            for f in files:
                content = f.read_text(encoding="utf-8")[:500]
                script_list += f"\n--- {f.name} ---\n{content}\n"
        agent = make_agent_soul("tenshi_analyst")
        result = run_single(
            "転職アニメチャンネルで10本制作したが成約（アフィリエイト）が0件です。\n"
            "以下の直近スクリプトを参考に、非成約の原因を分析してください。\n\n"
            + script_list +
            "\n分析軸：①ターゲットズレ ②CTA弱さ ③企業選定 ④構成の問題 ⑤競合との比較\n"
            "最後に「次の1本を作るべきか」判定（GO/WAIT/方向転換）と理由を明記してください。\n"
            "GOの場合は成約率を上げるための変更点3つを具体的に挙げること。",
            "転職アニメ週次分析レポート（GO/WAIT/方向転換判定含む）", agent)
        set_status("tenshi_analyst", "done", "分析レポート完成 ✓")
        _save_research(f"tenshi_analysis_{today}.txt",
                       f"=== 転職アニメ分析 {today} ===\n\n{result}", "tenshi")
        log(f"💾 転職分析: tenshi_analysis_{today}.txt", "tenshi_analyst")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ 転職分析エラー: {str(e)[:200]}")
        set_status("tenshi_analyst", "error", "エラー")


# ─── Job: 転職アニメ脚本（手動トリガー専用・tenshi_analystのGO判定後） ────────
def job_tenshi_script():
    today = datetime.now(JST).strftime("%Y%m%d")
    log("✍ 転職アニメ脚本家起動（手動トリガー）", "tenshi_scriptwriter")
    try:
        set_status("tenshi_scriptwriter", "working", "分析結果を確認中...")
        # 最新の分析レポートを読み込む
        analysis_files = sorted(RESEARCH_DIR.glob("tenshi_analysis_*.txt"))
        analysis = ""
        if analysis_files:
            analysis = analysis_files[-1].read_text(encoding="utf-8")[:2000]
        else:
            log("⚠ 転職分析レポートなし — tenshi_analystを先に実行してください", "tenshi_scriptwriter")
            set_status("tenshi_scriptwriter", "error", "分析レポートなし")
            return
        agent = make_agent_soul("tenshi_scriptwriter")
        result = run_single(
            "以下の分析レポートをもとに、成約率を上げる転職アニメショートのスクリプト骨子を1本生成してください。\n\n"
            "分析レポート:\n" + analysis + "\n\n"
            "CLAUDE.mdの転職チャンネル台本ルールに完全準拠すること。\n"
            "含めるもの：①企業名と年収 ②主人公の低スペック経歴 ③カット構成（25〜30カット）\n"
            "④CTA文言 ⑤冒頭フックカット（数字or非日常）",
            "転職アニメスクリプト骨子（カット構成付き）", agent)
        set_status("tenshi_scriptwriter", "done", "スクリプト骨子完成 ✓")
        _save_research(f"tenshi_script_draft_{today}.txt",
                       f"=== 転職アニメスクリプト骨子 {today} ===\n\n{result}", "tenshi")
        log(f"💾 転職脚本: tenshi_script_draft_{today}.txt", "tenshi_scriptwriter")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ 転職脚本エラー: {str(e)[:200]}")
        set_status("tenshi_scriptwriter", "error", "エラー")


# ─── Job: IP戦略デイリーリサーチ (daily 13:00) ──────────────────────────
def job_ip_strategy():
    today = datetime.now(JST).strftime("%Y/%m/%d")
    log("💎 IP戦略家デイリーリサーチ開始", "ip_strategist")
    try:
        set_status("ip_strategist", "working", "IPトレンドをリサーチ中...")
        agent = make_agent_soul("ip_strategist")
        # 既存のビズデブ情報を参考に
        existing = _read_strategy("ip_strategy.md")[:600]
        result = run_single(
            f"今日（{today}）のIP市場をリサーチし、幸子IPの育成・外部展開について提案してください。\n\n"
            "SOUL.mdの形式に従い以下を必ず含めること:\n"
            "①今日のIP市場インサイト（AIアニメIPや独立系キャラクターIPの事例）\n"
            "②幸子IP 今日の提案（施策名・難易度・期待効果・Next Action）\n"
            "③クオリティ投資タイミング判断（Seedance 2.0等への投資判断）\n\n"
            "幸子の現状：95万再生・登録者2,900人・月収益数万円・6ヶ月以内月100万目標\n"
            + (f"\n直近のIPリサーチ（参考）:\n{existing}" if existing else ""),
            "IP戦略デイリーレポート", agent)
        set_status("ip_strategist", "done", "IPレポート完成 ✓")
        existing_full = (RESEARCH_DIR / "ip_strategy.md").read_text(encoding="utf-8") if (RESEARCH_DIR / "ip_strategy.md").exists() else ""
        _append_research("ip_strategy.md", result, existing_full, "newbiz")
        log("💾 IPレポート追記: research/ip_strategy.md", "ip_strategist")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ IP戦略エラー: {str(e)[:200]}")
        set_status("ip_strategist", "error", "エラー")


# ─── Job: 外部マネタイズ探索 (daily 13:30) ──────────────────────────────
def job_bizdev_research():
    today = datetime.now(JST).strftime("%Y/%m/%d")
    log("🚀 外部マネタイズ探索開始", "bizdev_researcher")
    try:
        set_status("bizdev_researcher", "working", "新収益源を探索中...")
        agent = make_agent_soul("bizdev_researcher")
        existing = _read_strategy("bizdev.md")[:600]
        result = run_single(
            f"今日（{today}）、StudioOgawaが取り組めるAdSense以外の新しい収益源を調査してください。\n\n"
            "SOUL.mdの形式に従い以下を必ず含めること:\n"
            "①今日発掘した収益源（施策名・概要・初期コスト・月次収益予測・着手時期・難易度）\n"
            "②優先度評価（今すぐ実装すべき理由 or 理由なし）\n"
            "③今週のFOCUS（今日の推薦1件と具体的なFirst Step）\n\n"
            "前提：一人会社・30歳・アウトバウンド営業嫌い・AIアニメ得意・月100万目標\n"
            + (f"\n直近の探索結果（重複提案を避けるため）:\n{existing}" if existing else ""),
            "外部マネタイズ探索レポート", agent)
        set_status("bizdev_researcher", "done", "マネタイズ案完成 ✓")
        existing_full = (RESEARCH_DIR / "bizdev.md").read_text(encoding="utf-8") if (RESEARCH_DIR / "bizdev.md").exists() else ""
        _append_research("bizdev.md", result, existing_full, "newbiz")
        log("💾 外部マネタイズレポート追記: research/bizdev.md", "bizdev_researcher")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ 外部マネタイズ探索エラー: {str(e)[:200]}")
        set_status("bizdev_researcher", "error", "エラー")


# ─── Job: SNS企画担当 (月曜 10:00) ────────────────────────────────────────────
def job_sns_planner():
    today = datetime.now(JST)
    week_label = today.strftime("%Y%m%d")
    log("📅 SNS企画担当起動 — 今週のコンテンツ企画生成", "sns_planner")
    try:
        set_status("sns_planner", "working", "週間企画生成中...")
        agent = make_agent_soul("sns_planner")
        x_strategy = _read_strategy("x_strategy.md")[:800]
        result = run_single(
            f"今週（{today.strftime('%Y年%m月%d日')}週）のStudioOgawa SNSコンテンツ週間企画を生成してください。\n\n"
            "参考：X戦略直近レポート\n" + (x_strategy or "（なし）") + "\n\n"
            "SOUL.mdのフォーマットに従い以下を含めること：\n"
            "・今週のテーマの柱（3本）\n"
            "・月〜日の投稿ネタ7件（フック型・断言型・逆張り型を混ぜる）\n"
            "・今週のスレッド企画1本\n"
            "・noteとの連動案1件\n"
            "月100万円目標を念頭に、最もフォロワー増・note誘導に効く企画を選ぶこと。",
            "週間SNS企画（Markdown）", agent)
        set_status("sns_planner", "done", "週間企画完成 ✓")
        _save_research(f"sns_plan_{week_label}.md", result, "content")
        log(f"💾 週間SNS企画: sns_plan_{week_label}.md", "sns_planner")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ SNS企画エラー: {str(e)[:200]}")
        set_status("sns_planner", "error", "エラー")


# ─── Job: SNS教育コンテンツ (毎週水曜 11:00) ───────────────────────────────────
def job_sns_educator():
    today = datetime.now(JST).strftime("%Y%m%d")
    log("📚 SNS教育担当起動 — 教育素材生成", "sns_educator")
    try:
        set_status("sns_educator", "working", "教育コンテンツ生成中...")
        agent = make_agent_soul("sns_educator")
        result = run_single(
            "今週のStudioOgawa教育コンテンツ素材を生成してください。\n\n"
            "SOUL.mdのフォーマット通りに：\n"
            "①X用の共感投稿（ターゲットの言えない悩みを言語化した140文字）\n"
            "②無料note素材（タイトル・冒頭フック・教育の流れ・有料noteへの誘導）\n"
            "③LINE教育メッセージ素材（200文字）\n\n"
            "売り込まず「この方法がないと損してますよ」と気づかせる視点で書くこと。",
            "週次教育コンテンツ素材（Markdown）", agent)
        set_status("sns_educator", "done", "教育素材完成 ✓")
        _save_research(f"sns_education_{today}.md", result, "content")
        log(f"💾 教育素材: sns_education_{today}.md", "sns_educator")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ SNS教育エラー: {str(e)[:200]}")
        set_status("sns_educator", "error", "エラー")


# ─── Job: 新規事業 非属人YouTubeチャンネルリサーチ (daily 16:00) ────────────
def job_newbiz_youtube_research():
    today = datetime.now(JST).strftime("%Y%m%d")
    log("📺 新規事業 非属人YTチャンネルリサーチ開始", "bizdev_researcher")
    try:
        set_status("bizdev_researcher", "working", "成長中のYTチャンネルを探索中...")
        agent = make_researcher_with_tools()
        directive_text = _get_agent_directive_text("bizdev_researcher")
        result = run_single(
            directive_text +
            "StudioOgawaがアニメ化・参入できる成長中の非属人YouTubeチャンネルをリサーチしてください。\n"
            "必ずyoutube_search_toolとweb_search_toolを使い、実際のチャンネルURLを含めること。\n\n"
            "リサーチ軸：\n"
            "①非属人チャンネル（顔出しなし・アニメ・解説系・ゆっくり・朗読等）で急成長中のもの3〜5本\n"
            "  各チャンネル：URL・チャンネル名・登録者数・なぜ伸びているか・月間推定再生数\n"
            "②これらをアニメ化するとしたらどんな形式が合うか（ジャンル・ターゲット・差別化ポイント）\n"
            "③StudioOgawaが今すぐ始められる新規アニメチャンネル企画1件\n"
            "  （チャンネルコンセプト・ターゲット・収益化戦略・6ヶ月後の月間収益目標）",
            "非属人YouTube成長チャンネルリサーチ（URL付き・新規事業提案含む）",
            agent,
        )
        set_status("bizdev_researcher", "done", "新規事業リサーチ完了 ✓")
        _save_research(f"newbiz_yt_{today}.txt",
                       f"=== 非属人YTチャンネルリサーチ {today} ===\n\n{result}", "newbiz")
        log(f"💾 新規事業リサーチ: newbiz_yt_{today}.txt", "bizdev_researcher")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ 新規事業リサーチエラー: {str(e)[:200]}")
        set_status("bizdev_researcher", "error", "エラー")


# ─── Job: SNSマーケ担当 (毎週金曜 11:00) ────────────────────────────────────
def job_sns_marketer():
    today = datetime.now(JST).strftime("%Y%m%d")
    log("📣 SNSマーケ担当起動 — 導線・販売設計レポート", "sns_marketer")
    try:
        set_status("sns_marketer", "working", "マーケ施策レポート生成中...")
        agent = make_agent_soul("sns_marketer")
        result = run_single(
            "今週のStudioOgawaのSNS導線チェックと来週の販売施策を生成してください。\n\n"
            "SOUL.mdのフォーマット通りに：\n"
            "①今週の導線チェック（X→note誘導・無料→有料の流れ）\n"
            "②今週の販売施策提案（優先度順）\n"
            "③想定反論と切り返し（「高い」「自分にできるか不安」など）\n"
            "④来週の重点KPI（X投稿のnote誘導数・note閲覧数目標）\n\n"
            "StudioOgawa現状：幸子チャンネル95万再生・note教材販売準備中・月100万目標",
            "SNSマーケ週次レポート（Markdown）", agent)
        set_status("sns_marketer", "done", "マーケレポート完成 ✓")
        _save_research(f"sns_marketing_{today}.md", result, "content")
        log(f"💾 マーケレポート: sns_marketing_{today}.md", "sns_marketer")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ SNSマーケエラー: {str(e)[:200]}")
        set_status("sns_marketer", "error", "エラー")


# ─── Job: KPI更新 (30分ごと) ─────────────────────────────────────────────────
def job_kpi_update():
    """各部門のKPIをファイルカウント・キュー状態から集計してstateに反映する（6部署対応）。"""
    now = datetime.now(JST)
    today_str = now.strftime("%Y%m%d")
    month_prefix = now.strftime("%Y%m")
    try:
        def count_this_month(pattern):
            return sum(1 for f in RESEARCH_DIR.glob(pattern) if f.stem[-8:][:6] == month_prefix)

        def exists_today(pattern):
            return any(True for f in RESEARCH_DIR.glob(pattern) if today_str in f.name)

        # 幸子チャンネル部
        monthly_drafts  = count_this_month("script_draft_*.txt")
        daily_draft     = exists_today("script_draft_*.txt")
        monthly_tieup   = count_this_month("tieup_auto_*.txt")
        daily_tieup     = exists_today("tieup_auto_*.txt")
        monthly_sponsor = count_this_month("sponsor_auto_*.txt")
        daily_sponsor   = exists_today("sponsor_auto_*.txt")

        # コンテンツ・販売部
        nr = state.get("note_roadmap_progress", {})
        monthly_note = nr.get("done", 0)
        daily_note   = (nr.get("today_title", "-") != "-")
        approved = _load_queue(APPROVED_PATH)
        monthly_x = len([p for p in approved["x_posts"] if p.get("approved_at", "").startswith(now.strftime("%Y-%m"))])
        daily_x          = exists_today("x_strategy*.md")
        monthly_education = count_this_month("sns_education_*.md")
        daily_education   = exists_today("sns_education_*.md")
        monthly_marketing = count_this_month("sns_marketing_*.md")
        daily_marketing   = exists_today("sns_marketing_*.md")

        # toB受託営業部
        monthly_lists  = count_this_month("sales_prospects_*.txt")
        daily_list     = exists_today("sales_prospects_*.txt")
        daily_proposal = exists_today("proposals_*.txt")

        # 転職アニメ部
        monthly_analysis = count_this_month("tenshi_analysis_*.txt")
        daily_analysis   = exists_today("tenshi_analysis_*.txt")

        # 事業開発部
        monthly_yt = count_this_month("newbiz_yt_*.txt")
        daily_yt   = exists_today("newbiz_yt_*.txt")

        with lock:
            state["kpi"] = {
                "sachiko":  {"monthly_drafts": monthly_drafts, "monthly_target": 6, "monthly_tieup": monthly_tieup, "monthly_tieup_target": 30, "monthly_sponsor": monthly_sponsor, "monthly_sponsor_target": 20, "daily_draft": daily_draft, "daily_tieup": daily_tieup, "daily_sponsor": daily_sponsor},
                "content":  {"monthly_note": monthly_note, "monthly_note_target": 30, "monthly_x": monthly_x, "monthly_x_target": 150, "monthly_education": monthly_education, "monthly_education_target": 20, "monthly_marketing": monthly_marketing, "monthly_marketing_target": 20, "daily_note": daily_note, "daily_x": daily_x, "daily_education": daily_education, "daily_marketing": daily_marketing},
                "sales":    {"monthly_lists": monthly_lists, "monthly_target": 30, "daily_list": daily_list, "daily_proposal": daily_proposal},
                "tenshi":   {"monthly_analysis": monthly_analysis, "monthly_target": 20, "daily_analysis": daily_analysis},
                "newbiz":   {"monthly_yt": monthly_yt, "monthly_yt_target": 20, "daily_yt": daily_yt},
                "last_updated": now.strftime("%H:%M"),
            }
    except Exception as e:
        log(f"⚠ KPI更新失敗: {str(e)[:80]}")


# ─── Job: 週次サマリーレポート (Mon 08:00) ────────────────────────────────
def job_weekly_summary():
    today = datetime.now(JST)
    week_label = today.strftime("%Y年%m月第%Wの週")
    date_label = today.strftime("%Y-%m-%d")
    log("📋 週次サマリーレポート生成開始")
    try:
        # 各部門の直近ファイルを収集
        def _latest(pattern: str, chars: int = 800) -> str:
            files = sorted(RESEARCH_DIR.glob(pattern))
            if not files:
                return "（今週のデータなし）"
            return files[-1].read_text(encoding="utf-8")[:chars]

        sachiko  = _latest("sachiko_auto_*.txt")
        tieup    = _latest("tieup_auto_*.txt")
        sales    = _latest("sales_prospects_*.txt")
        proposal = _latest("proposals_*.txt")
        tenshi   = _latest("tenshi_analysis_*.txt")
        script   = _latest("script_draft_*.txt")
        x_strat  = _read_strategy("x_strategy.md")[:800]
        note_str = _read_strategy("note_strategy.md")[:500]
        ip_str   = _read_strategy("ip_strategy.md")[:600]
        bd_str   = _read_strategy("bizdev.md")[:600]

        context = (
            f"【幸子リサーチ】\n{sachiko}\n\n"
            f"【タイアップリサーチ】\n{tieup}\n\n"
            f"【営業見込み客リスト】\n{sales}\n\n"
            f"【提案書】\n{proposal}\n\n"
            f"【転職アニメ分析】\n{tenshi}\n\n"
            f"【台本骨子】\n{script}\n\n"
            f"【X戦略（直近）】\n{x_strat}\n\n"
            f"【note戦略】\n{note_str}\n\n"
            f"【IP戦略（直近）】\n{ip_str}\n\n"
            f"【外部マネタイズ探索（直近）】\n{bd_str}"
        )
        agent = make_agent(
            "事業サマリーアナリスト",
            "各部門の週次実績を1枚のレポートにまとめ、監督が5分で状況を把握できるようにする",
            "StudioOgawa専属。幸子チャンネル・Xマーケ・営業・転職アニメの4部門を横断管理。")
        result = run_single(
            f"以下の今週の各部門アウトプットをもとに、週次サマリーレポートを作成してください。\n\n"
            f"{context}\n\n"
            "形式（必ず守ること）:\n"
            f"# StudioOgawa 週次サマリー {week_label}\n\n"
            "## 幸子チャンネル部\n- 今週のリサーチ結果一言\n- 台本骨子のテーマ\n\n"
            "## Xマーケティング部\n- 今週の主要トレンド2つ\n- 学んだXマーケ知識1つ\n\n"
            "## 営業部\n- 優先度「高」の見込み客TOP3\n- 提案書の状況\n\n"
            "## 転職アニメ部\n- 分析結果（GO/WAIT/方向転換）\n- 理由一言\n\n"
            "## note部\n- 今週のドラフトテーマ\n\n"
            "## 新規事業部\n- IP戦略の今週の提案TOP1\n- 外部マネタイズ探索の今週のFOCUS案\n\n"
            "## 今週の総評と来週のFOCUS\n- 最も重要な動き1つ\n- 来週最優先でやること1つ\n\n"
            "簡潔に。各項目2〜3行まで。",
            "週次サマリーレポート（Markdown）", agent)
        _save_research(f"weekly_summary_{date_label}.md", result, "weekly")
        log(f"💾 週次サマリー保存: weekly_summary_{date_label}.md")
        with lock:
            state["result"] = f"📋 週次サマリー {week_label}\n\n{result}"
    except Exception as e:
        log(f"❌ 週次サマリーエラー: {str(e)[:200]}")


# ─── Scheduler ── 月100万円逆算スケジュール ──────────────────────────────────
# 目標：6ヶ月以内に月100万円
# 収益柱：① 幸子AdSense ② 転職アフィリ ③ note/Brain教材 ④ BtoB受託
# 原則：「週1」「月1」は収益につながらない。毎日動かすか、最低でも週3。
scheduler = BackgroundScheduler(timezone=JST, job_defaults={"misfire_grace_time": 300})

# ── ① X投稿（1日4回・承認済みから自動投稿）──────────────────────────────────
scheduler.add_job(job_x_random_post, CronTrigger(hour=8,  minute=0,  timezone=JST), id="x_post_morning",   name="X投稿（朝8時）")
scheduler.add_job(job_x_random_post, CronTrigger(hour=12, minute=0,  timezone=JST), id="x_post_noon",      name="X投稿（昼12時）")
scheduler.add_job(job_x_random_post, CronTrigger(hour=18, minute=0,  timezone=JST), id="x_post_afternoon", name="X投稿（夕方18時）")
scheduler.add_job(job_x_random_post, CronTrigger(hour=20, minute=0,  timezone=JST), id="x_post_evening",   name="X投稿（夜20時）")

# ── ② 朝ブロック：コンテンツ量産（幸子AdSense + note教材）──────────────────
scheduler.add_job(job_secretary_briefing,  CronTrigger(hour=7,  minute=0,  timezone=JST), id="secretary_daily",    name="秘書ブリーフィング（毎日7時）")
scheduler.add_job(job_x_strategy_learn,    CronTrigger(hour=7,  minute=30, timezone=JST), id="x_strategy_daily",   name="X戦略デイリーリサーチ（毎日）")
scheduler.add_job(job_sachiko_research,    CronTrigger(hour=8,  minute=30, timezone=JST), id="sachiko_daily",      name="幸子リサーチ（毎日）")
scheduler.add_job(job_scriptwriter_daily,  CronTrigger(hour=9,  minute=0,  timezone=JST), id="scriptwriter_daily", name="台本骨子ドラフト生成（毎日）")
scheduler.add_job(job_generate_x_drafts,   CronTrigger(hour=9,  minute=30, timezone=JST), id="x_draft_daily",      name="X投稿ドラフト生成（毎日）")
scheduler.add_job(job_generate_note_draft, CronTrigger(hour=10, minute=0,  timezone=JST), id="note_draft_daily",   name="note記事ドラフト生成（毎日・ロードマップ対応）")

# ── ③ 午前ブロック：営業 + 転職アフィリ（毎日）─────────────────────────────
scheduler.add_job(job_sales_research,   CronTrigger(hour=10, minute=30, timezone=JST), id="sales_research_daily",  name="BtoB営業リサーチ（毎日）")
scheduler.add_job(job_tenshi_analyze,   CronTrigger(hour=11, minute=0,  timezone=JST), id="tenshi_analysis_daily", name="転職アニメ分析（毎日）")
scheduler.add_job(job_tieup_research,   CronTrigger(hour=11, minute=30, timezone=JST), id="tieup_daily",           name="タイアップリサーチ（毎日）")
scheduler.add_job(job_tenshi_script,    CronTrigger(hour=12, minute=0,  timezone=JST), id="tenshi_script_daily",   name="転職アニメ脚本（毎日・GO判定時のみ生成）")

# ── ④ 午後ブロック：SNSマネタイズ + 新規事業（毎日）──────────────────────────
scheduler.add_job(job_sns_educator,     CronTrigger(hour=13, minute=0,  timezone=JST), id="sns_educator_daily",    name="SNS教育コンテンツ（毎日）")
scheduler.add_job(job_ip_strategy,      CronTrigger(hour=13, minute=30, timezone=JST), id="ip_strategy_daily",     name="IP戦略デイリーリサーチ（毎日）")
scheduler.add_job(job_bizdev_research,  CronTrigger(hour=14, minute=0,  timezone=JST), id="bizdev_daily",          name="外部マネタイズ探索（毎日）")
scheduler.add_job(job_marketing_research, CronTrigger(hour=14, minute=30, timezone=JST), id="marketing_research_daily", name="X/noteマーケリサーチ（毎日）")
scheduler.add_job(job_sns_marketer,            CronTrigger(hour=15, minute=0,  timezone=JST), id="sns_marketer_daily",       name="SNSマーケ施策（毎日）")
scheduler.add_job(job_newbiz_youtube_research, CronTrigger(hour=16, minute=0,  timezone=JST), id="newbiz_yt_daily",          name="新規事業YTリサーチ（毎日）")

# ── ⑤ 夜ブロック：翌日の下地作り ───────────────────────────────────────────
scheduler.add_job(job_note_research,    CronTrigger(hour=22, minute=0,  timezone=JST), id="note_research_daily",   name="noteリサーチ（毎日22時）")

# ── ⑥ 週3以上：分析・改善（月水金）──────────────────────────────────────────
scheduler.add_job(job_sachiko_analytics, CronTrigger(day_of_week="mon,wed,fri", hour=15, minute=30, timezone=JST), id="sachiko_analytics",   name="幸子チャンネル分析（週3）")

# ── ⑦ 週次：戦略見直し（月曜）────────────────────────────────────────────────
scheduler.add_job(job_weekly_summary,  CronTrigger(day_of_week="mon", hour=6, minute=0, timezone=JST), id="weekly_summary",       name="週次サマリーレポート（月曜6時）")
scheduler.add_job(job_sns_planner,     CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=JST), id="sns_planner_weekly",    name="SNS週間企画（月曜9時）")

# ── ⑧ 常時監視（30分ごと）──────────────────────────────────────────────────
scheduler.add_job(job_kikuchi_progress_update,      CronTrigger(minute="0,30",  timezone=JST), id="kikuchi_check",      name="菊地進捗チェック（30分ごと）")
scheduler.add_job(job_note_roadmap_progress_update, CronTrigger(minute="15,45", timezone=JST), id="note_roadmap_check", name="noteロードマップ進捗（30分ごと）")
scheduler.add_job(job_kpi_update,                   CronTrigger(minute="5,35",  timezone=JST), id="kpi_update",         name="KPI更新（30分ごと）")

JOB_FUNCS = {
    # X投稿
    "x_post_morning":          job_x_random_post,
    "x_post_noon":             job_x_random_post,
    "x_post_afternoon":        job_x_random_post,
    "x_post_evening":          job_x_random_post,
    # 朝ブロック
    "secretary_daily":         job_secretary_briefing,
    "x_strategy_daily":        job_x_strategy_learn,
    "sachiko_daily":           job_sachiko_research,
    "scriptwriter_daily":      job_scriptwriter_daily,
    "x_draft_daily":           job_generate_x_drafts,
    "note_draft_daily":        job_generate_note_draft,
    # 午前ブロック
    "sales_research_daily":    job_sales_research,
    "tenshi_analysis_daily":   job_tenshi_analyze,
    "tieup_daily":             job_tieup_research,
    "tenshi_script_daily":     job_tenshi_script,
    # 午後ブロック
    "sns_educator_daily":      job_sns_educator,
    "ip_strategy_daily":       job_ip_strategy,
    "bizdev_daily":            job_bizdev_research,
    "marketing_research_daily":job_marketing_research,
    "sns_marketer_daily":      job_sns_marketer,
    "newbiz_yt_daily":         job_newbiz_youtube_research,
    # 夜ブロック
    "note_research_daily":     job_note_research,
    # 週3
    "sachiko_analytics":       job_sachiko_analytics,
    # 週次
    "weekly_summary":          job_weekly_summary,
    "sns_planner_weekly":      job_sns_planner,
    # 常時監視
    "kikuchi_check":           job_kikuchi_progress_update,
    "note_roadmap_check":      job_note_roadmap_progress_update,
    "kpi_update":              job_kpi_update,
}


_JOB_DEPT = {
    "x_post_morning": "content", "x_post_noon": "content",
    "x_post_afternoon": "content", "x_post_evening": "content",
    "secretary_daily": "sachiko", "x_strategy_daily": "content",
    "sachiko_daily": "sachiko", "scriptwriter_daily": "sachiko",
    "x_draft_daily": "content", "note_draft_daily": "content",
    "sales_research_daily": "sales", "tenshi_analysis_daily": "tenshi",
    "tieup_daily": "sachiko", "tenshi_script_daily": "tenshi",
    "sns_educator_daily": "content", "ip_strategy_daily": "newbiz",
    "bizdev_daily": "newbiz", "marketing_research_daily": "content",
    "sns_marketer_daily": "content", "newbiz_yt_daily": "newbiz",
    "note_research_daily": "content", "sachiko_analytics": "sachiko",
    "weekly_summary": "sachiko", "sns_planner_weekly": "content",
    "kikuchi_check": "sachiko", "note_roadmap_check": "content",
    "kpi_update": "",
}

def get_schedule_info():
    nr = state.get("note_roadmap_progress", {})
    note_subtitle = ""
    if nr.get("today_title") and nr["today_title"] != "-":
        note_subtitle = f"今日: {nr['today_date']} 「{nr['today_title'][:18]}…」"
    result = []
    for j in scheduler.get_jobs():
        entry = {
            "id": j.id, "name": j.name,
            "next_run": j.next_run_time.strftime("%m/%d %H:%M JST") if j.next_run_time else "未定",
            "dept": _JOB_DEPT.get(j.id, ""),
        }
        if j.id == "note_draft_daily" and note_subtitle:
            entry["subtitle"] = note_subtitle
        result.append(entry)
    return result


# ─── Flask Routes ────────────────────────────────────────────────────────────
@app.route("/ping")
def ping():
    return jsonify({"ok": True})


@app.route("/")
def index():
    return render_template("autonomous.html")


@app.route("/api/status")
def api_status():
    with lock:
        data = copy.deepcopy(state)
    data["schedule"] = get_schedule_info()
    # Queue summary (x post text included; note body excluded)
    pending  = _load_queue(PENDING_PATH)
    approved = _load_queue(APPROVED_PATH)
    data["pending"] = {
        "x_posts":    [{"id": p["id"], "text": p["text"], "created_at": p["created_at"]}
                       for p in pending["x_posts"]],
        "note_drafts":[{"id": p["id"], "title": p["title"], "created_at": p["created_at"]}
                       for p in pending["note_drafts"]],
    }
    data["approved"] = {
        "x_posts":    [{"id": p["id"], "text": p["text"][:80] + "...", "posted": p.get("posted", False)}
                       for p in approved["x_posts"]],
        "note_drafts":[{"id": p["id"], "title": p["title"], "published": p.get("published", False)}
                       for p in approved["note_drafts"]],
    }
    return jsonify(data)


@app.route("/api/run", methods=["POST"])
def api_run():
    with lock:
        if state["running"]:
            return jsonify({"error": "実行中です"}), 400
    data  = request.get_json(silent=True) or {}
    theme = (data.get("theme") or "").strip() or "年金だけでは足りないと気づいた日"
    threading.Thread(target=run_crew, args=(theme,), daemon=True).start()
    return jsonify({"ok": True, "theme": theme})


@app.route("/api/trigger/<job_id>", methods=["POST"])
def api_trigger(job_id):
    fn = JOB_FUNCS.get(job_id)
    if not fn:
        return jsonify({"error": "不明なジョブID"}), 404
    threading.Thread(target=fn, daemon=True).start()
    log(f"▶ 手動起動: {job_id}")
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/approve", methods=["POST"])
def api_approve():
    data = request.get_json() or {}
    type_, item_id = data.get("type"), data.get("id")

    # noteドラフト承認時: ロードマップスプシを自動チェック
    roadmap_date = None
    if type_ == "note_drafts":
        pending = _load_queue(PENDING_PATH)
        item = next((i for i in pending.get("note_drafts", []) if i["id"] == item_id), None)
        if item:
            roadmap_date = item.get("roadmap_date")

    if approve_item(type_, item_id):
        log(f"✅ 承認: {type_} [{item_id}]")
        if roadmap_date:
            def _mark():
                ok = note_roadmap_mark_done(roadmap_date)
                if ok:
                    log(f"📝 スプシ自動チェック: {roadmap_date} ✓")
                    job_note_roadmap_progress_update()
                else:
                    log(f"⚠ スプシ自動チェック失敗（GOOGLE_SERVICE_ACCOUNT_JSON未設定の可能性）: {roadmap_date}")
            threading.Thread(target=_mark, daemon=True).start()
        return jsonify({"ok": True})
    return jsonify({"error": "item not found"}), 404


@app.route("/api/reject", methods=["POST"])
def api_reject():
    data = request.get_json() or {}
    type_, item_id = data.get("type"), data.get("id")
    if reject_item(type_, item_id):
        log(f"🗑 却下: {type_} [{item_id}]")
        return jsonify({"ok": True})
    return jsonify({"error": "item not found"}), 404


@app.route("/api/kikuchi_progress")
def api_kikuchi_progress():
    threading.Thread(target=job_kikuchi_progress_update, daemon=True).start()
    with lock:
        return jsonify(state.get("kikuchi_progress", {}))


@app.route("/api/note_roadmap_progress")
def api_note_roadmap_progress():
    threading.Thread(target=job_note_roadmap_progress_update, daemon=True).start()
    with lock:
        return jsonify(state.get("note_roadmap_progress", {}))


@app.route("/api/kpi")
def api_kpi():
    threading.Thread(target=job_kpi_update, daemon=True).start()
    with lock:
        return jsonify(state.get("kpi", {}))


@app.route("/api/marketing_insights")
def api_marketing_insights():
    with lock:
        return jsonify(state.get("marketing_insights", []))


@app.route("/api/debug_kikuchi")
def api_debug_kikuchi():
    """スプシのCSV生データを返す（デバッグ用）。"""
    import urllib.request
    try:
        url = ("https://docs.google.com/spreadsheets/d/"
               "1xHIRrC4e4eJGuvnE84n7xERZYEzu4SApTroB4xYknM0"
               "/export?format=csv&gid=595521756")
        with urllib.request.urlopen(url, timeout=15) as resp:
            content = resp.read().decode("utf-8")
        lines = content.split("\n")
        parsed = []
        for i, line in enumerate(lines[:20]):
            import csv as _csv
            reader = list(_csv.reader([line]))
            parts = reader[0] if reader else []
            parsed.append({"row": i, "raw": line[:200], "cols": parts})
        return jsonify({"ok": True, "rows": parsed, "total_rows": len(lines)})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/debug_kikuchi_full")
def api_debug_kikuchi_full():
    """スプシ全行をCSV解析して返す（詳細タスクシートの構造確認用）。"""
    import urllib.request, csv as _csv
    try:
        url = ("https://docs.google.com/spreadsheets/d/"
               "1xHIRrC4e4eJGuvnE84n7xERZYEzu4SApTroB4xYknM0"
               "/export?format=csv&gid=595521756")
        with urllib.request.urlopen(url, timeout=15) as resp:
            content = resp.read().decode("utf-8")
        rows = list(_csv.reader(content.splitlines()))
        # 全行を返す（最大200行）、col0〜col4のみ
        parsed = [{"row": i, "cols": row[:5]} for i, row in enumerate(rows[:200])]
        return jsonify({"ok": True, "rows": parsed, "total": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)})



@app.route("/api/edit_pending", methods=["POST"])
def api_edit_pending():
    data    = request.get_json() or {}
    type_   = data.get("type")
    item_id = data.get("id")
    q = _load_queue(PENDING_PATH)
    item = next((i for i in q.get(type_, []) if i["id"] == item_id), None)
    if not item:
        return jsonify({"error": "item not found"}), 404
    if type_ == "x_posts" and "text" in data:
        item["text"] = data["text"]
    elif type_ == "note_drafts":
        if "title" in data:
            item["title"] = data["title"]
        if "body" in data:
            item["body"] = data["body"]
    item["edited_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    _save_queue(PENDING_PATH, q)
    log(f"✏ 編集保存: {type_} [{item_id}]")
    return jsonify({"ok": True})


@app.route("/api/instruct", methods=["POST"])
def api_instruct():
    """エージェントへの指示を保存する（次回ジョブ実行時に自動反映）。"""
    data       = request.get_json() or {}
    agent_id   = data.get("agent_id", "global").strip() or "global"
    instruction = data.get("instruction", "").strip()
    if not instruction:
        return jsonify({"error": "instruction が必要です"}), 400
    _save_directive(agent_id, instruction)
    target_name = state["agents"].get(agent_id, {}).get("name", agent_id) if agent_id != "global" else "全エージェント共通"
    log(f"📌 指示保存 [{target_name}]: {instruction[:40]}")
    return jsonify({"ok": True, "agent": target_name})


@app.route("/api/instruct", methods=["GET"])
def api_instruct_list():
    """保存済みの指示一覧を返す。"""
    return jsonify(_load_directives())


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json() or {}
    agent_id = data.get("agent_id", "").strip()
    message  = data.get("message", "").strip()
    if not agent_id or not message:
        return jsonify({"error": "agent_id と message が必要です"}), 400
    if agent_id not in state["agents"]:
        return jsonify({"error": "不明なエージェントID"}), 404
    soul = load_soul(agent_id)
    if not soul:
        return jsonify({"error": f"SOUL.md が見つかりません: {agent_id}"}), 404
    try:
        agent_name = state["agents"][agent_id]["name"]
        set_status(agent_id, "working", "チャット中...")
        log(f"💬 [{agent_name}] ← {message[:40]}", agent_id)
        agent_obj = make_agent_soul(agent_id)
        result = run_single(
            f"監督からの質問・指示：{message}\n\n"
            "SOUL.mdの役割・知識に基づき日本語で回答してください。"
            "簡潔に（300文字程度）。箇条書き歓迎。",
            "日本語での返答（300文字目安）",
            agent_obj
        )
        set_status(agent_id, "done", "チャット完了 ✓")
        log(f"💬 [{agent_name}] → 返答完了", agent_id)
        return jsonify({"ok": True, "response": result, "agent": agent_name})
    except Exception as e:
        import traceback
        set_status(agent_id, "error", "チャットエラー")
        log(f"❌ チャットエラー [{agent_id}]: {str(e)[:100]}")
        return jsonify({"error": str(e)[:200]}), 500


@app.route("/api/results")
def api_results():
    """各部門の最新成果物ファイルを返す。"""
    def latest(pattern, n=10):
        files = sorted(RESEARCH_DIR.glob(pattern), reverse=True)[:n]
        out = []
        for f in files:
            try:
                out.append({"name": f.name, "content": f.read_text(encoding="utf-8")[:6000]})
            except Exception:
                pass
        return out

    def read_md(name):
        p = RESEARCH_DIR / name
        if p.exists():
            try:
                return [{"name": name, "content": p.read_text(encoding="utf-8")[:6000]}]
            except Exception:
                pass
        return []

    return jsonify({
        "sachiko":  latest("sachiko_auto_*.txt") + latest("script_draft_*.txt"),
        "content":  read_md("x_strategy.md") + read_md("note_strategy.md"),
        "sales":    latest("sales_prospects_*.txt") + latest("proposals_*.txt"),
        "tenshi":   latest("tenshi_analysis_*.txt") + latest("tenshi_script_draft_*.txt"),
        "newbiz":   read_md("ip_strategy.md") + read_md("bizdev.md") + latest("tieup_auto_*.txt"),
        "weekly":   latest("weekly_summary_*.md"),
    })


@app.route("/api/ai_rewrite_x", methods=["POST"])
def api_ai_rewrite_x():
    """X投稿をAIで指示通りに書き直してキューを更新する。"""
    data = request.get_json() or {}
    item_id = data.get("id", "").strip()
    instruction = data.get("instruction", "").strip()
    if not item_id or not instruction:
        return jsonify({"error": "id と instruction が必要"}), 400
    q = _load_queue(PENDING_PATH)
    item = next((p for p in q["x_posts"] if p["id"] == item_id), None)
    if not item:
        return jsonify({"error": "item not found"}), 404
    try:
        import google.generativeai as genai
        genai.configure(api_key=os.getenv("GOOGLE_API_KEY", ""))
        model = genai.GenerativeModel("gemini-2.5-flash")
        prompt = (
            f"以下のX投稿を指示通りに修正してください。\n\n"
            f"元の投稿:\n{item['text']}\n\n"
            f"修正指示:\n{instruction}\n\n"
            "修正後の投稿テキストのみを出力。説明・前置き不要。140文字以内。"
        )
        resp = model.generate_content(prompt)
        new_text = resp.text.strip()[:280]
        item["text"] = new_text
        item["edited_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        _save_queue(PENDING_PATH, q)
        log(f"🤖 AI修正完了: [{item_id}] {instruction[:20]}...")
        return jsonify({"ok": True, "text": new_text})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@app.route("/api/note_body/<item_id>")
def api_note_body(item_id):
    for path in [PENDING_PATH, APPROVED_PATH]:
        q = _load_queue(path)
        item = next((p for p in q["note_drafts"] if p["id"] == item_id), None)
        if item:
            return jsonify({"title": item["title"], "body": item.get("body", "")})
    return jsonify({"error": "not found"}), 404


# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    scheduler.start()
    log("🕐 スケジューラー起動 — 自律モード ON")
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
