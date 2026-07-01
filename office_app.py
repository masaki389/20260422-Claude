import os
import threading
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv
from crewai import Agent, Task, Crew, Process

load_dotenv()
os.environ.setdefault("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", ""))

app = Flask(__name__)
lock = threading.Lock()
LLM = "gemini/gemini-2.5-flash"

state = {
    "running": False,
    "theme": "",
    "result": "",
    "agents": {
        "researcher":    {"name": "リサーチャー", "dept": "sachiko", "status": "idle", "task": "", "avatar": "🔍"},
        "scriptwriter":  {"name": "脚本家",       "dept": "sachiko", "status": "idle", "task": "", "avatar": "✍️"},
        "imagegen":      {"name": "画像生成",      "dept": "sachiko", "status": "idle", "task": "", "avatar": "🎨"},
        "x_research":    {"name": "Xリサーチ",    "dept": "note",    "status": "idle", "task": "", "avatar": "𝕏"},
        "note_writer":   {"name": "記事作成",      "dept": "note",    "status": "idle", "task": "", "avatar": "📝"},
    },
    "logs": [],
}

def log(msg, agent_id=None):
    with lock:
        state["logs"].insert(0, {
            "time": datetime.now().strftime("%H:%M:%S"),
            "msg": msg,
            "agent": agent_id,
        })
        if len(state["logs"]) > 80:
            state["logs"] = state["logs"][:80]

def set_status(agent_id, status, task=""):
    with lock:
        state["agents"][agent_id]["status"] = status
        state["agents"][agent_id]["task"] = task
    name = state["agents"][agent_id]["name"]
    log(f"{name} → {task or status}", agent_id)

def run_crew(theme):
    with lock:
        state["running"] = True
        state["theme"] = theme
        state["result"] = ""
        for aid in state["agents"]:
            state["agents"][aid]["status"] = "idle"
            state["agents"][aid]["task"] = ""

    log(f"🏢 AIオフィス始動 — {theme}")

    try:
        set_status("researcher", "working", f"「{theme}」を分析中...")

        sequence = ["researcher", "scriptwriter"]
        tick = [0]

        def task_callback(output):
            done = sequence[tick[0]]
            set_status(done, "done", "完了 ✓")
            tick[0] += 1
            if tick[0] < len(sequence):
                nxt = sequence[tick[0]]
                label = "台本骨子を執筆中..." if nxt == "scriptwriter" else "作業中..."
                set_status(nxt, "working", label)

        r_agent = Agent(
            role="シニア動画リサーチャー",
            goal="指定テーマについて、シニア女性視聴者が共感する切り口と台本の方向性を提案する",
            backstory="YouTubeシニア向けチャンネルの分析専門家。幸子チャンネル（累計95万再生）のデータを熟知。",
            verbose=False, allow_delegation=False, llm=LLM
        )
        s_agent = Agent(
            role="シニアドラマ脚本家",
            goal="リサーチを元に幸子チャンネル用台本骨子を黄金フォーマット6フェーズで作成する",
            backstory="幸子（65歳）・田中さん（57歳・無自覚悪役）・中島さん（67歳・サポーター）の三角関係が基本構造。冒頭30秒で45%離脱するデータを熟知。",
            verbose=False, allow_delegation=False, llm=LLM
        )

        t1 = Task(
            description=f"テーマ「{theme}」の①痛みポイント3つ ②メタファー2つ ③冒頭0〜5秒の案2つ ④田中さんの一言2つ を提案。",
            expected_output="4項目を箇条書きにした日本語レポート",
            agent=r_agent,
        )
        t2 = Task(
            description="task1をもとに黄金フォーマット6フェーズの台本骨子（冒頭/問題発生/飲み込み/ターニングポイント/行動へ/小さな着地）を作成。各フェーズにセリフ・心の声・カメラ指示を含める。",
            expected_output="黄金フォーマット6フェーズの台本骨子（日本語・800〜1200文字）",
            agent=s_agent,
        )

        crew = Crew(
            agents=[r_agent, s_agent],
            tasks=[t1, t2],
            process=Process.sequential,
            verbose=False,
            task_callback=task_callback,
        )

        result = crew.kickoff()
        with lock:
            state["result"] = str(result)
        log("✅ 全タスク完了！")

    except Exception as e:
        msg = str(e)[:200]
        log(f"❌ エラー: {msg}")
        with lock:
            for aid in state["agents"]:
                if state["agents"][aid]["status"] == "working":
                    state["agents"][aid]["status"] = "error"
                    state["agents"][aid]["task"] = "エラー発生"
    finally:
        with lock:
            state["running"] = False


@app.route("/")
def index():
    return render_template("office.html")

@app.route("/api/status")
def api_status():
    with lock:
        import copy
        return jsonify(copy.deepcopy(state))

@app.route("/api/run", methods=["POST"])
def api_run():
    with lock:
        if state["running"]:
            return jsonify({"error": "実行中です"}), 400
    data = request.get_json(silent=True) or {}
    theme = (data.get("theme") or "").strip() or "年金だけでは足りないと気づいた日"
    threading.Thread(target=run_crew, args=(theme,), daemon=True).start()
    return jsonify({"ok": True, "theme": theme})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
