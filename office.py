import os
from dotenv import load_dotenv
from crewai import Agent, Task, Crew, Process

load_dotenv()

# CrewAI 1.x はlitellm経由でGeminiを使う
os.environ.setdefault("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", ""))

LLM = "gemini/gemini-2.5-flash"

# ─── エージェント定義 ───────────────────────────────────────

researcher = Agent(
    role="シニア動画リサーチャー",
    goal="指定テーマについて、シニア女性視聴者（55〜65歳）が共感する切り口と台本の方向性を提案する",
    backstory=(
        "あなたはYouTubeのシニア向けチャンネルを専門に分析するリサーチャー。"
        "視聴維持率・CTR・コメント傾向から「何が刺さるか」を見抜く能力を持つ。"
        "幸子チャンネル（登録者2900人・累計95万再生）のデータも把握している。"
    ),
    verbose=True,
    allow_delegation=False,
    llm=LLM
)

scriptwriter = Agent(
    role="シニアドラマ脚本家",
    goal="リサーチャーの分析をもとに、幸子チャンネルのルールに完全準拠した台本骨子を作成する",
    backstory=(
        "あなたはシニア向けアニメドラマの脚本家。"
        "主人公・幸子（65歳）はスーパーのレジパート、年金月10万3千円。"
        "田中さん（57歳・無自覚な悪役）と中島さん（67歳・サポーター）の三角関係が基本構造。"
        "「教わった」ではなく「幸子と一緒に気づいた」と視聴者が思う作りにする。"
        "冒頭30秒で45%離脱するデータを知っており、必ず「事件・痛み・不安」から始める。"
        "お金の描き方：「なくなる」ではなく「将来の不安」として描く。"
    ),
    verbose=True,
    allow_delegation=False,
    llm=LLM
)

# ─── タスク定義 ────────────────────────────────────────────

def run_office(theme: str):
    task1 = Task(
        description=(
            f"テーマ「{theme}」について以下を調査・提案してください：\n"
            "1. シニア女性が最も共感するであろう「痛みのポイント」3つ\n"
            "2. このテーマで有効なメタファー（物語を貫く象徴物）の候補2つ\n"
            "3. 冒頭0〜5秒で使える「事件・一景」の案2つ\n"
            "4. 田中さんが使える「無自覚な一言」の例2つ"
        ),
        expected_output="上記4項目を箇条書きでまとめた日本語のリサーチレポート。",
        agent=researcher
    )

    task2 = Task(
        description=(
            "task1のリサーチレポートをもとに、幸子チャンネル用の台本骨子を作成してください。\n"
            "黄金フォーマットに沿って構成すること：\n"
            "①冒頭の一景（0:00-0:05）\n"
            "②問題発生（0:22-2:00）\n"
            "③飲み込み（2:00-4:00）\n"
            "④ターニングポイント（4:00-6:00）：幸子自身が気づく\n"
            "⑤行動へ（6:00-8:00）\n"
            "⑥小さな着地（8:00-10:00）：正夫の一言・締めナレーション\n"
            "各フェーズに主要なセリフ・心の声・カメラ指示（バストアップ/ミディアム等）を1〜2行で添えること。"
        ),
        expected_output="黄金フォーマット6フェーズで構成された台本骨子（日本語・800〜1200文字）。",
        agent=scriptwriter
    )

    office = Crew(
        agents=[researcher, scriptwriter],
        tasks=[task1, task2],
        process=Process.sequential,
        verbose=True
    )

    print(f"\n{'='*50}")
    print(f"AIオフィス始動 — テーマ：{theme}")
    print(f"{'='*50}\n")

    result = office.kickoff()

    print(f"\n{'='*50}")
    print("最終成果物（台本骨子）")
    print(f"{'='*50}\n")
    print(result)
    return result


if __name__ == "__main__":
    theme = "年金だけでは足りないと気づいた日"
    run_office(theme)
