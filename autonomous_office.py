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
        # 管理部門
        "secretary":        {"name": "秘書",              "dept": "mgmt",   "status": "idle", "task": ""},
        # 幸子チャンネル部署
        "researcher":       {"name": "幸子リサーチャー",  "dept": "sachiko", "status": "idle", "task": ""},
        "scriptwriter":     {"name": "脚本家",             "dept": "sachiko", "status": "idle", "task": ""},
        # Xマーケティング部署
        "x_strategist":    {"name": "X戦略家",             "dept": "xmkt",   "status": "idle", "task": ""},
        "x_writer":        {"name": "X投稿ライター",        "dept": "xmkt",   "status": "idle", "task": ""},
        "x_poster":        {"name": "X投稿管理",            "dept": "xmkt",   "status": "idle", "task": ""},
        # noteコンテンツ部署
        "note_researcher": {"name": "noteリサーチャー",    "dept": "note",   "status": "idle", "task": ""},
        "note_writer":     {"name": "note記事ライター",    "dept": "note",   "status": "idle", "task": ""},
        # 事業開発部署
        "tieup_researcher":{"name": "タイアップ探索",      "dept": "bizdev", "status": "idle", "task": ""},
        # 外注
        "kikuchi":          {"name": "菊地（外注）",        "dept": "external","status": "idle", "task": ""},
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

        context = (
            f"現在の状況（{today} 07:30 JST）\n"
            f"承認待ち: X投稿{x_pend}件・note記事{n_pend}件\n"
            f"承認済み（未投稿）: X投稿{x_appr}件・note記事{n_appr}件\n"
            f"最近の生成ファイル: {', '.join(recent) if recent else 'なし'}\n\n"
            "今日の自動実行:\n"
            "・09:00 幸子テーマリサーチ\n"
            "・09:30 X投稿ドラフト7件生成\n"
            "・12:00 & 20:00 承認済みX投稿からランダム投稿"
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


# ─── Job: X戦略学習 (weekly Sun 23:00) ──────────────────────────────────
def job_x_strategy_learn():
    log("🧠 X戦略学習エージェント起動")
    try:
        set_status("x_strategist", "working", "X戦略をリサーチ中...")
        agent = make_agent_soul("x_strategist")
        result = run_single(
            "AI副業・YouTube収益化・一人会社運営ジャンルでXを伸ばす最新法則を分析:\n"
            "①バズる投稿の型5つ（テンプレート付き）\n"
            "②最適な投稿頻度・時間帯（JST）\n"
            "③このジャンルで特に刺さるキーワード15個\n"
            "④避けるべきNG表現\n"
            "⑤フォロワーを増やすエンゲージメント戦術3つ\n"
            "⑥noteへの自然な誘導を組み込む方法",
            "Xマーケティング戦略ガイド（マークダウン形式）", agent)
        set_status("x_strategist", "done", "戦略ガイド更新 ✓")
        (RESEARCH_DIR / "x_strategy.md").write_text(
            f"# X投稿戦略ガイド\n更新: {datetime.now(JST).strftime('%Y-%m-%d')}\n\n{result}", encoding="utf-8")
        log("💾 X戦略ガイド更新: research/x_strategy.md")
        with lock:
            state["result"] = result
    except Exception as e:
        log(f"❌ X戦略学習エラー: {str(e)[:200]}")
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
        log(f"❌ X投稿生成エラー: {str(e)[:200]}")
        set_status("x_writer", "error", "エラー")


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
        agent_status = "working" if 0 < pct < 100 else ("done" if pct >= 100 else "idle")
        with lock:
            state["agents"]["kikuchi"]["status"] = agent_status
            state["agents"]["kikuchi"]["task"]   = f"{ep_name} {pct}%"
            state["kikuchi_progress"] = {
                "episode": ep_name, "progress": pct,
                "done": done, "total": total,
                "status": status, "due": due_date
            }
        log(f"📊 菊地進捗更新: {ep_name} {pct}% ({done}/{total}章)", "kikuchi")
    except Exception as e:
        log(f"⚠ 菊地進捗取得失敗: {str(e)[:80]}")


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
scheduler.add_job(job_x_random_post,        CronTrigger(hour=20, minute=0,  timezone=JST), id="x_post_evening",    name="X投稿（夜20時）")
# Hourly — 菊地進捗
scheduler.add_job(job_kikuchi_progress_update, CronTrigger(minute=0, timezone=JST), id="kikuchi_check", name="菊地進捗チェック")
# Weekly
scheduler.add_job(job_x_strategy_learn,    CronTrigger(day_of_week="sun", hour=23, minute=0,  timezone=JST), id="x_strategy_weekly",   name="X戦略学習（週1）")
scheduler.add_job(job_note_research,        CronTrigger(day_of_week="sun", hour=23, minute=30, timezone=JST), id="note_research_weekly", name="noteリサーチ（週1）")
scheduler.add_job(job_generate_note_draft,  CronTrigger(day_of_week="mon", hour=10, minute=0,  timezone=JST), id="note_draft_weekly",    name="note記事ドラフト生成")
scheduler.add_job(job_tieup_research,       CronTrigger(day_of_week="mon", hour=11, minute=0,  timezone=JST), id="tieup_weekly",         name="タイアップリサーチ")

JOB_FUNCS = {
    "secretary_daily":      job_secretary_briefing,
    "sachiko_daily":        job_sachiko_research,
    "scriptwriter_daily":   job_scriptwriter_daily,
    "x_draft_daily":        job_generate_x_drafts,
    "x_post_morning":       job_x_random_post,
    "x_post_noon":          job_x_random_post,
    "x_post_evening":       job_x_random_post,
    "kikuchi_check":        job_kikuchi_progress_update,
    "x_strategy_weekly":    job_x_strategy_learn,
    "note_research_weekly": job_note_research,
    "note_draft_weekly":    job_generate_note_draft,
    "tieup_weekly":         job_tieup_research,
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
    with lock:
        return jsonify(state.get("kikuchi_progress", {}))


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
