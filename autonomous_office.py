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
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv
from crewai import Agent, Task, Crew, Process
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

load_dotenv()
os.environ.setdefault("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", ""))


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

PENDING_PATH  = RESEARCH_DIR / "pending_queue.json"
APPROVED_PATH = RESEARCH_DIR / "approved_queue.json"
AGENTS_DIR    = BASE_DIR / "agents"
CONTEXT_DIR   = BASE_DIR / "shared-context"

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
        "tieup_researcher":    {"name": "タイアップ探索",      "dept": "newbiz",   "status": "idle", "task": ""},
        "ip_strategist":       {"name": "IP戦略家",            "dept": "newbiz",   "status": "idle", "task": ""},
        "bizdev_researcher":   {"name": "外部マネタイズ探索",   "dept": "newbiz",   "status": "idle", "task": ""},
        # 外注
        "kikuchi":             {"name": "菊地（外注）",        "dept": "external", "status": "idle", "task": ""},
    },
    "logs": [],
    "kikuchi_progress": {"episode": "-", "progress": 0, "status": "-", "due": "-"},
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


def run_single(description, expected_output, agent_obj) -> str:
    task = Task(description=description, expected_output=expected_output, agent=agent_obj)
    crew = Crew(agents=[agent_obj], tasks=[task], process=Process.sequential, verbose=False)
    return str(crew.kickoff())


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


# ─── Manual Run ──────────────────────────────────────────────────────────────
def run_crew(theme: str):
    with lock:
        state["running"] = True
        state["theme"]   = theme
        state["result"]  = ""
        for aid in ["researcher", "scriptwriter"]:
            state["agents"][aid].update({"status": "idle", "task": ""})

    log(f"🏢 台本エージェント起動 — {theme}")
    try:
        set_status("researcher", "working", f"「{theme}」分析中...")
        sequence, tick = ["researcher", "scriptwriter"], [0]

        def cb(output):
            set_status(sequence[tick[0]], "done", "完了 ✓")
            tick[0] += 1
            if tick[0] < len(sequence):
                nxt = sequence[tick[0]]
                set_status(nxt, "working", "台本骨子を執筆中..." if nxt == "scriptwriter" else "作業中...")

        ra = make_agent("シニア動画リサーチャー",
                        "指定テーマについてシニア女性視聴者が共感する切り口と台本の方向性を提案する",
                        "幸子チャンネル（累計95万再生）専門。冒頭30秒で45%離脱するデータを熟知。")
        sa = make_agent("シニアドラマ脚本家",
                        "リサーチを元に幸子チャンネル用台本骨子を黄金フォーマット6フェーズで作成する",
                        "幸子（65歳）・田中さん（57歳・無自覚悪役）・中島さん（67歳・サポーター）の三角関係が基本構造。")
        t1 = Task(description=f"テーマ「{theme}」の①痛みポイント3つ ②メタファー2つ ③冒頭案2つ ④田中の一言2つ を提案。",
                  expected_output="4項目の日本語レポート", agent=ra)
        t2 = Task(description="t1をもとに黄金フォーマット6フェーズの台本骨子を作成。各フェーズにセリフ・心の声・カメラ指示を含める。",
                  expected_output="6フェーズ台本骨子（800〜1200文字）", agent=sa)
        crew = Crew(agents=[ra, sa], tasks=[t1, t2], process=Process.sequential,
                    verbose=False, task_callback=cb)
        with lock:
            state["result"] = str(crew.kickoff())
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
    log("🌅 幸子チャンネル デイリーリサーチ開始")
    try:
        set_status("researcher", "working", "トレンドリサーチ中...")
        agent = make_agent(
            "シニア動画トレンドリサーチャー",
            "シニア女性YouTubeチャンネル向けの今週最適エピソードテーマを3つ提案する",
            "幸子チャンネル（登録2900人・累計95万再生）専門。バズるテーマの公式：すかっとする・感動・共感。")
        result = run_single(
            "幸子チャンネル向けエピソードテーマを3つ提案。\n"
            "各テーマ：①タイトル案 ②冒頭の一景（0-5秒） ③メタファー候補 ④田中さんの一言",
            "テーマ提案3件（各4項目）の日本語レポート", agent)
        set_status("researcher", "done", "リサーチ完了 ✓")
        (RESEARCH_DIR / f"sachiko_auto_{today}.txt").write_text(
            f"=== 幸子デイリーリサーチ {today} ===\n\n{result}", encoding="utf-8")
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
        # 既存ファイルの先頭に追記（最新が上）
        strategy_path = RESEARCH_DIR / "x_strategy.md"
        existing = strategy_path.read_text(encoding="utf-8") if strategy_path.exists() else ""
        strategy_path.write_text(result + "\n\n---\n\n" + existing, encoding="utf-8")
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
        result = run_single(
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
        (RESEARCH_DIR / "note_strategy.md").write_text(
            f"# noteコンテンツ戦略\n更新: {datetime.now(JST).strftime('%Y-%m-%d')}\n\n{result}", encoding="utf-8")
        log("💾 note戦略更新: research/note_strategy.md")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ noteリサーチエラー: {str(e)[:200]}")
        set_status("note_researcher", "error", "エラー")


# ─── Job: note記事ドラフト生成 (weekly Mon 10:00) ────────────────────────
def job_generate_note_draft():
    log("📝 note記事ドラフト生成開始")
    try:
        set_status("note_writer", "working", "記事執筆中...")
        strategy = _read_strategy("note_strategy.md")
        agent = make_agent(
            "noteコンテンツライター",
            "AIアニメ制作・YouTube収益化ノウハウをnote向けに2000〜3000文字の記事にまとめる",
            "StudioOgawaのAIアニメ制作専門家。95万再生の実績。" +
            ("\nnote戦略:\n" + strategy if strategy else ""))
        result = run_single(
            "AIアニメ動画制作に関するnote記事を1本作成。最も伸びそうなテーマを選ぶ:\n"
            "①Gemini AIで月1000枚の画像を生成した話\n"
            "②65歳主婦アニメで95万再生した制作方法\n"
            "③CrewAI自律エージェントで動画制作を自動化した話\n"
            "④AIアニメで月10万円稼ぐ方法（実数字公開）\n"
            "形式：タイトル・冒頭共感フック・本文H2×3章（各500文字）・まとめ・ハッシュタグ5個",
            "完成したnote記事（タイトル＋本文2000〜3000文字）", agent)

        lines = result.strip().split("\n")
        title = lines[0].lstrip("# ").strip() if lines else "AI動画制作ノウハウ"
        body  = "\n".join(lines[1:]).strip() if len(lines) > 1 else result

        add_to_pending("note_drafts", {
            "id":         str(uuid.uuid4())[:8],
            "title":      title,
            "body":       body,
            "created_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        })
        set_status("note_writer", "done", "記事ドラフト → 承認待ち ✓")
        log(f"📥 note記事ドラフトを承認待ちキューに追加: {title[:30]}...")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ note記事生成エラー: {str(e)[:200]}")
        set_status("note_writer", "error", "エラー")


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
        (RESEARCH_DIR / f"tieup_auto_{today}.txt").write_text(
            f"=== タイアップリサーチ {today} ===\n\n{result}", encoding="utf-8")
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
        (RESEARCH_DIR / f"script_draft_{today}.txt").write_text(
            f"=== 台本骨子ドラフト {today} ===\n\n{result}", encoding="utf-8")
        log(f"💾 台本骨子保存: script_draft_{today}.txt")
        with lock:
            state["result"] = f"🎬 台本骨子ドラフト\n\n{result}"
    except Exception as e:
        log(f"❌ 台本骨子エラー: {str(e)[:200]}")
        set_status("scriptwriter", "error", "エラー")


# ─── Job: 菊地進捗チェック (hourly) ──────────────────────────────────────
def job_kikuchi_progress_update():
    import urllib.request
    try:
        url = ("https://docs.google.com/spreadsheets/d/"
               "1xHIRrC4e4eJGuvnE84n7xERZYEzu4SApTroB4xYknM0"
               "/export?format=csv&gid=595521756")
        with urllib.request.urlopen(url, timeout=15) as resp:
            content = resp.read().decode("utf-8")
        lines = content.split("\n")
        current_ep = None
        for line in lines[2:]:
            parts = line.split(",")
            if len(parts) >= 6 and "菊地" in parts[1]:
                status = parts[4].strip()
                if status in ("制作中", "未着手"):
                    current_ep = parts
                    break
        if not current_ep:
            return
        ep_name  = current_ep[0].strip()
        status   = current_ep[4].strip()
        due_date = current_ep[2].strip()
        checks   = [current_ep[i].strip() for i in range(5, 10) if i < len(current_ep)]
        done     = sum(1 for c in checks if c.upper() == "TRUE")
        total    = len(checks)
        pct      = int(done / total * 100) if total > 0 else 0
        # 菊地は担当エピソードがある間は常に working（着座）
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
        # 進捗率が変化したときだけログ出力（30分ごとの無音更新は記録しない）
        if pct != prev_pct:
            log(f"📊 菊地進捗: {ep_name} {pct}% ({done}/{total}章)", "kikuchi")
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
        (RESEARCH_DIR / f"sachiko_analysis_{today}.txt").write_text(
            f"=== 幸子チャンネル分析 {today} ===\n\n{result}", encoding="utf-8")
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
        (RESEARCH_DIR / f"sales_prospects_{today}.txt").write_text(
            f"=== BtoB見込み客リスト {today} ===\n\n{result}", encoding="utf-8")
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
        (RESEARCH_DIR / f"proposals_{today}.txt").write_text(
            f"=== BtoB提案書 {today} ===\n\n{result}", encoding="utf-8")
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
        (RESEARCH_DIR / f"tenshi_analysis_{today}.txt").write_text(
            f"=== 転職アニメ分析 {today} ===\n\n{result}", encoding="utf-8")
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
        (RESEARCH_DIR / f"tenshi_script_draft_{today}.txt").write_text(
            f"=== 転職アニメスクリプト骨子 {today} ===\n\n{result}", encoding="utf-8")
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
        ip_path = RESEARCH_DIR / "ip_strategy.md"
        existing_full = ip_path.read_text(encoding="utf-8") if ip_path.exists() else ""
        ip_path.write_text(result + "\n\n---\n\n" + existing_full, encoding="utf-8")
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
        bd_path = RESEARCH_DIR / "bizdev.md"
        existing_full = bd_path.read_text(encoding="utf-8") if bd_path.exists() else ""
        bd_path.write_text(result + "\n\n---\n\n" + existing_full, encoding="utf-8")
        log("💾 外部マネタイズレポート追記: research/bizdev.md", "bizdev_researcher")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ 外部マネタイズ探索エラー: {str(e)[:200]}")
        set_status("bizdev_researcher", "error", "エラー")


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
        out_path = RESEARCH_DIR / f"weekly_summary_{date_label}.md"
        out_path.write_text(result, encoding="utf-8")
        log(f"💾 週次サマリー保存: weekly_summary_{date_label}.md")
        with lock:
            state["result"] = f"📋 週次サマリー {week_label}\n\n{result}"
    except Exception as e:
        log(f"❌ 週次サマリーエラー: {str(e)[:200]}")


# ─── Scheduler ───────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone=JST, job_defaults={"misfire_grace_time": 300})
# Daily 07:30 — 秘書
scheduler.add_job(job_secretary_briefing,   CronTrigger(hour=7,  minute=30, timezone=JST), id="secretary_daily",   name="秘書ブリーフィング")
# Daily 09:00 — 並列4ジョブ（幸子リサーチ・台本骨子・X投稿・note下書き）
scheduler.add_job(job_sachiko_research,     CronTrigger(hour=9,  minute=0,  timezone=JST), id="sachiko_daily",     name="幸子リサーチ")
scheduler.add_job(job_scriptwriter_daily,   CronTrigger(hour=9,  minute=0,  timezone=JST), id="scriptwriter_daily",name="台本骨子ドラフト生成")
scheduler.add_job(job_generate_x_drafts,    CronTrigger(hour=9,  minute=0,  timezone=JST), id="x_draft_daily",     name="X投稿ドラフト生成")
# Daily X投稿
scheduler.add_job(job_x_random_post,        CronTrigger(hour=8,  minute=0,  timezone=JST), id="x_post_morning",    name="X投稿（朝8時）")
scheduler.add_job(job_x_random_post,        CronTrigger(hour=12, minute=0,  timezone=JST), id="x_post_noon",       name="X投稿（昼12時）")
scheduler.add_job(job_x_random_post,        CronTrigger(hour=18, minute=0,  timezone=JST), id="x_post_afternoon",  name="X投稿（夕方18時）")
scheduler.add_job(job_x_random_post,        CronTrigger(hour=20, minute=0,  timezone=JST), id="x_post_evening",    name="X投稿（夜20時）")
# 30分ごと — 菊地進捗（毎時0分・30分）
scheduler.add_job(job_kikuchi_progress_update, CronTrigger(minute="0,30", timezone=JST), id="kikuchi_check", name="菊地進捗チェック（30分ごと）")
# Daily 08:30 — X戦略デイリーリサーチ
scheduler.add_job(job_x_strategy_learn,    CronTrigger(hour=8,  minute=30, timezone=JST), id="x_strategy_daily",    name="X戦略デイリーリサーチ（毎日）")
# Weekly Mon 08:00 — 週次サマリーレポート
scheduler.add_job(job_weekly_summary,      CronTrigger(day_of_week="mon", hour=8, minute=0, timezone=JST), id="weekly_summary",         name="週次サマリーレポート（月曜8時）")
# Weekly — noteリサーチ
scheduler.add_job(job_note_research,        CronTrigger(day_of_week="sun", hour=23, minute=30, timezone=JST), id="note_research_weekly",  name="noteリサーチ（週1）")
scheduler.add_job(job_generate_note_draft,  CronTrigger(hour=10, minute=30, timezone=JST), id="note_draft_daily",      name="note記事ドラフト生成（毎日）")
scheduler.add_job(job_tieup_research,       CronTrigger(hour=11, minute=0,  timezone=JST), id="tieup_daily",           name="タイアップリサーチ（毎日）")
# Weekly — 幸子分析 (火曜9時)
scheduler.add_job(job_sachiko_analytics,    CronTrigger(day_of_week="tue", hour=9,  minute=0,  timezone=JST), id="sachiko_analytics",     name="幸子チャンネル週次分析")
# Daily — 営業（毎日9:30）
scheduler.add_job(job_sales_research,       CronTrigger(hour=9,  minute=30, timezone=JST), id="sales_research_daily",  name="BtoB営業リサーチ（毎日）")
# Daily — 転職分析（毎日10:00）+ 脚本（毎日11:30）
scheduler.add_job(job_tenshi_analyze,       CronTrigger(hour=10, minute=0,  timezone=JST), id="tenshi_analysis_daily",  name="転職アニメ分析（毎日）")
scheduler.add_job(job_tenshi_script,        CronTrigger(hour=11, minute=30, timezone=JST), id="tenshi_script_daily",    name="転職アニメ脚本（毎日・GO判定時のみ生成）")
# Daily — 新規事業部（13:00〜）
scheduler.add_job(job_ip_strategy,          CronTrigger(hour=13, minute=0,  timezone=JST), id="ip_strategy_daily",      name="IP戦略デイリーリサーチ（毎日）")
scheduler.add_job(job_bizdev_research,      CronTrigger(hour=13, minute=30, timezone=JST), id="bizdev_daily",           name="外部マネタイズ探索（毎日）")

JOB_FUNCS = {
    "secretary_daily":      job_secretary_briefing,
    "sachiko_daily":        job_sachiko_research,
    "scriptwriter_daily":   job_scriptwriter_daily,
    "x_draft_daily":        job_generate_x_drafts,
    "x_post_morning":       job_x_random_post,
    "x_post_noon":          job_x_random_post,
    "x_post_afternoon":     job_x_random_post,
    "x_post_evening":       job_x_random_post,
    "kikuchi_check":          job_kikuchi_progress_update,
    "x_strategy_daily":       job_x_strategy_learn,
    "weekly_summary":         job_weekly_summary,
    "note_research_weekly":   job_note_research,
    "note_draft_daily":       job_generate_note_draft,
    "tieup_daily":            job_tieup_research,
    "sachiko_analytics":      job_sachiko_analytics,
    "sales_research_daily":   job_sales_research,
    "tenshi_analysis_daily":  job_tenshi_analyze,
    "tenshi_script_daily":    job_tenshi_script,
    "ip_strategy_daily":      job_ip_strategy,
    "bizdev_daily":           job_bizdev_research,
    "weekly_summary":         job_weekly_summary,
}


def get_schedule_info():
    return [
        {"id": j.id, "name": j.name,
         "next_run": j.next_run_time.strftime("%m/%d %H:%M JST") if j.next_run_time else "未定"}
        for j in scheduler.get_jobs()
    ]


# ─── Flask Routes ────────────────────────────────────────────────────────────
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
    if approve_item(type_, item_id):
        log(f"✅ 承認: {type_} [{item_id}]")
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
    # スプシから最新を取得してから返す（キャッシュより最新優先）
    threading.Thread(target=job_kikuchi_progress_update, daemon=True).start()
    with lock:
        return jsonify(state.get("kikuchi_progress", {}))


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
