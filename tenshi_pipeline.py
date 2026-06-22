#!/usr/bin/env python3
"""
tenshi_pipeline.py — 転職チャンネル台本 自動リサーチ・生成パイプライン

バズっている転職Shorts動画を分析し、アニメ台本に変換して量産する。

【初回セットアップ（Cookieが必要）】
1. Chrome で youtube.com を開きログイン状態にする
2. Chrome拡張「Get cookies.txt LOCALLY」で youtube.com のCookieをエクスポート
3. ファイルを cookies.txt という名前で このスクリプトと同じフォルダに保存

使い方:
    python tenshi_pipeline.py --research              # 上位動画をリサーチして一覧表示
    python tenshi_pipeline.py --url <YouTube URL>     # 特定動画を台本化（自動字幕取得）
    python tenshi_pipeline.py --paste                 # 字幕を手動で貼り付けて台本化
    python tenshi_pipeline.py --batch 10              # 上位10本を一括台本化
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

try:
    from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
except ImportError:
    sys.exit("ERROR: pip install youtube-transcript-api を実行してください")

load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")
if not API_KEY:
    sys.exit("ERROR: .env に GOOGLE_API_KEY が設定されていません")

BASE = Path(__file__).parent
OUTPUT_DIR = BASE / "tenshi_scripts"
OUTPUT_DIR.mkdir(exist_ok=True)

GEMINI_TEXT_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent?key={key}"
)

SEARCH_QUERIES = [
    "転職 企業 雑学",
    "転職 意外な事実 会社",
    "企業 驚き 転職活動",
    "転職 会社 知らなかった",
    "転職 企業研究 shorts",
]


# ---------------------------------------------------------------------------
# YouTube検索（yt-dlp使用・API不要）
# ---------------------------------------------------------------------------

def search_tenshi_shorts(max_per_query: int = 30) -> list[dict]:
    """yt-dlpで転職系Shortsを検索し、再生数順で返す"""
    videos = []
    seen = set()

    for query in SEARCH_QUERIES:
        print(f"  検索中: {query} ...")
        cmd = [
            "yt-dlp",
            f"ytsearch{max_per_query}:{query}",
            "--dump-json",
            "--no-download",
            "--flat-playlist",
            "--quiet",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            continue

        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            vid = data.get("id")
            if not vid or vid in seen:
                continue

            duration = data.get("duration") or 0
            # Shorts判定: 60秒以内 (yt-dlpは「shorts/id」形式かduration基準)
            url = data.get("webpage_url", "")
            is_short = duration <= 65 or "/shorts/" in url

            if not is_short:
                continue

            seen.add(vid)
            videos.append({
                "id": vid,
                "title": data.get("title", ""),
                "channel": data.get("channel", ""),
                "views": data.get("view_count") or 0,
                "duration": int(duration),
                "url": f"https://www.youtube.com/shorts/{vid}",
            })

        time.sleep(1)

    # 再生数順にソート
    videos.sort(key=lambda x: x["views"], reverse=True)
    return videos


# ---------------------------------------------------------------------------
# 字幕取得
# ---------------------------------------------------------------------------

COOKIES_PATH = BASE / "cookies.txt"


def get_transcript_ytdlp(video_id: str) -> str | None:
    """yt-dlpで字幕を取得（cookies.txtがある場合に使用）"""
    import tempfile
    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as tmpdir:
        cmd = [
            "yt-dlp", url,
            "--write-auto-subs", "--write-subs",
            "--sub-lang", "ja",
            "--skip-download",
            "--output", f"{tmpdir}/%(id)s",
            "--quiet",
        ]
        if COOKIES_PATH.exists():
            cmd += ["--cookies", str(COOKIES_PATH)]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        # VTT/SRTファイルを探して読み込む
        import glob
        vtt_files = glob.glob(f"{tmpdir}/*.vtt") + glob.glob(f"{tmpdir}/*.srt")
        if not vtt_files:
            return None

        raw = Path(vtt_files[0]).read_text(encoding="utf-8", errors="ignore")
        # VTT/SRTのタグと時間コードを除去してテキストだけ抽出
        text = re.sub(r"WEBVTT.*?\n\n", "", raw, flags=re.DOTALL)
        text = re.sub(r"\d+\n[\d:,\. ]+-->[\d:,\. ]+\n", "", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\n+", " ", text).strip()
        # 重複行を除去（字幕の重複テキスト対策）
        parts = text.split()
        return " ".join(parts) if parts else None


def get_transcript(video_id: str) -> str | None:
    """YouTube動画の字幕を取得（yt-dlp使用）"""
    result = get_transcript_ytdlp(video_id)
    if result:
        return result
    if not COOKIES_PATH.exists():
        print("    ⚠️  cookies.txt が見つかりません。--paste モードで手動入力してください。")
        print("       または cookies.txt をセットアップしてください（READMEの初回セットアップ参照）")
    return None


def extract_video_id(url: str) -> str | None:
    """YouTubeのURLから動画IDを抽出"""
    patterns = [
        r"youtu\.be/([^?&/]+)",
        r"youtube\.com/shorts/([^?&/]+)",
        r"youtube\.com/watch\?.*v=([^&]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Gemini — アニメ台本に変換
# ---------------------------------------------------------------------------

REWRITE_PROMPT = """あなたは転職系アニメYouTubeショート動画の台本作家です。

以下の【元の動画文字起こし】を参考に、同じテーマ・題材で
完全に新しいアニメ台本を作成してください。

【元の動画文字起こし】
{transcript}

【台本の要件】
- 同じ題材・事実を扱うが、表現・構成は完全に作り直す
- 以下の構成で60秒以内
  1. 認知（5秒）: 題材を紹介し注目させる
  2. 衝撃（10秒）: 知られていない驚きの事実
  3. ロジック（20秒）: なぜそうなのか背景・理由
  4. 権威性（10秒）: 数字・実績で裏付け
  5. リマインド（5秒）: 衝撃を短く再強調
  6. CTA（10秒）: 転職への自然な誘導

- ナレーション（語りかけ調）のみ。セリフ・会話なし
- 1文20文字以内。合計200〜250文字
- キャラクターは「田村拓」（30歳・転職経験者）が語る設定

【出力形式】

=== テーマ ===
（この動画の題材・会社名・キーワードを1行で）

=== ナレーション ===
1. [認知] （文章）
2. [衝撃] （文章）
3. [ロジック] （文章）
4. [権威性] （文章）
5. [リマインド] （文章）
6. [CTA] （文章）

=== script.txt形式 ===
以下のフォーマットで20〜25カット出力してください。

場所は以下4種類のみ使用:
- オフィス: 現代的なオフィス（デスク・PC・窓）
- テキスト: 数字・事実をドンと見せるシーン（シンプルな背景）
- 外観: ビル外観・街並み
- キャラ: 田村拓が語るシーン

【カット1】
場所：[場所]
キャラ：[田村拓（アップ）or なし]
内容：[映像で見せる具体的な内容]
秒数：[秒数]

（20〜25カット続ける）
"""


def rewrite_to_anime_script(transcript: str) -> str | None:
    """文字起こしをアニメ台本に変換"""
    prompt = REWRITE_PROMPT.format(transcript=transcript[:1500])
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 3000},
    }
    resp = requests.post(
        GEMINI_TEXT_URL.format(key=API_KEY),
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=90,
    )
    if resp.status_code != 200:
        print(f"    API エラー: {resp.status_code}")
        return None
    try:
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return None


def extract_script_section(full_text: str) -> str:
    """script.txt形式セクションを抽出"""
    match = re.search(r"=== script\.txt形式 ===\n(.*)", full_text, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r"(【カット1】.*)", full_text, re.DOTALL)
    return match.group(1).strip() if match else full_text


def save_script(video_id: str, title: str, full_text: str) -> Path:
    """台本ファイルを保存"""
    script_content = extract_script_section(full_text)
    safe_title = re.sub(r'[^\w\s-]', '', title[:30]).strip().replace(" ", "_")
    out_path = OUTPUT_DIR / f"{video_id}_{safe_title}.txt"
    out_path.write_text(script_content, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# メインコマンド
# ---------------------------------------------------------------------------

def cmd_research(_args):
    """上位動画をリサーチして一覧表示"""
    print("転職Shorts 上位動画を検索中...\n")
    videos = search_tenshi_shorts(max_per_query=30)

    if not videos:
        print("動画が見つかりませんでした。")
        return

    print(f"\n{'順位':>4} {'再生数':>10}  {'秒':>4}  タイトル")
    print("-" * 75)
    for i, v in enumerate(videos[:30], 1):
        views_str = f"{v['views']:,}"
        print(f"{i:>4} {views_str:>10}  {v['duration']:>3}s  {v['title'][:45]}")
        print(f"           ch: {v['channel'][:35]}")
        print(f"           {v['url']}")

    research_path = BASE / "tenshi_research.json"
    research_path.write_text(json.dumps(videos, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n合計 {len(videos)} 本を保存: {research_path}")
    print(f"\n台本化する場合:")
    print(f"  python tenshi_pipeline.py --url <URL>   # 1本だけ")
    print(f"  python tenshi_pipeline.py --batch 10    # 上位10本一括")


def cmd_url(args):
    """特定URLの動画を台本化"""
    video_id = extract_video_id(args.url)
    if not video_id:
        sys.exit("ERROR: 有効なYouTube URLを指定してください")

    print(f"動画ID: {video_id}")
    print("字幕を取得中...")
    transcript = get_transcript(video_id)
    if not transcript:
        sys.exit("ERROR: この動画の字幕が取得できませんでした")

    print(f"字幕取得完了（{len(transcript)}文字）")
    print("アニメ台本に変換中...")

    full_text = rewrite_to_anime_script(transcript)
    if not full_text:
        sys.exit("ERROR: 台本変換に失敗しました")

    print("\n" + "=" * 60)
    # テーマとナレーションだけ表示
    for section in ["=== テーマ ===", "=== ナレーション ==="]:
        m = re.search(rf"{re.escape(section)}\n(.*?)(?===|$)", full_text, re.DOTALL)
        if m:
            print(section)
            print(m.group(1).strip()[:300])
            print()

    out_path = save_script(video_id, video_id, full_text)
    print(f"保存完了: {out_path}")
    print(f"\n画像生成する場合:")
    print(f"  cp \"{out_path}\" script.txt && python auto_studio.py")


def cmd_batch(args):
    """上位N本を一括台本化"""
    research_path = BASE / "tenshi_research.json"
    if not research_path.exists():
        print("リサーチデータがありません。先に --research を実行します...")
        videos = search_tenshi_shorts(max_per_query=30)
        research_path.write_text(json.dumps(videos, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        with open(research_path, encoding="utf-8") as f:
            videos = json.load(f)

    target = videos[:args.batch]
    print(f"上位 {len(target)} 本を台本化します\n")

    success = 0
    for i, v in enumerate(target, 1):
        print(f"[{i}/{len(target)}] {v['title'][:50]}")
        print(f"  再生数: {v['views']:,}  URL: {v['url']}")

        transcript = get_transcript(v["id"])
        if not transcript:
            print("  字幕なし → スキップ\n")
            continue

        print(f"  字幕 {len(transcript)}文字 → 変換中...")
        full_text = rewrite_to_anime_script(transcript)
        if not full_text:
            print("  変換失敗 → スキップ\n")
            continue

        out_path = save_script(v["id"], v["title"], full_text)
        print(f"  完了: {out_path.name}\n")
        success += 1
        time.sleep(3)

    print(f"\n完了: {success}/{len(target)} 本の台本を生成しました")
    print(f"保存先: {OUTPUT_DIR}/")


def cmd_paste(_args):
    """字幕テキストを手動で貼り付けて台本化"""
    print("=== 手動貼り付けモード ===")
    print("YouTubeの字幕テキストを貼り付けてください。")
    print("終了したら空行を2回押してください:\n")

    lines = []
    empty_count = 0
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "":
            empty_count += 1
            if empty_count >= 2:
                break
        else:
            empty_count = 0
            lines.append(line)

    transcript = " ".join(lines).strip()
    if not transcript:
        print("テキストが入力されませんでした。")
        return

    print(f"\n入力テキスト: {len(transcript)}文字")
    print("アニメ台本に変換中...")

    full_text = rewrite_to_anime_script(transcript)
    if not full_text:
        print("変換失敗")
        return

    print("\n" + "=" * 60)
    for section in ["=== テーマ ===", "=== ナレーション ==="]:
        m = re.search(rf"{re.escape(section)}\n(.*?)(?===|$)", full_text, re.DOTALL)
        if m:
            print(section)
            print(m.group(1).strip())
            print()

    out_path = save_script("manual", "manual", full_text)
    print(f"\n保存完了: {out_path}")
    print(f"\n画像生成する場合:")
    print(f"  cp \"{out_path}\" script.txt && python auto_studio.py")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    flag = sys.argv[1]
    if flag == "--research":
        cmd_research(None)
    elif flag == "--url" and len(sys.argv) >= 3:
        cmd_url(argparse.Namespace(url=sys.argv[2]))
    elif flag == "--paste":
        cmd_paste(None)
    elif flag == "--batch":
        n = int(sys.argv[2]) if len(sys.argv) >= 3 else 10
        cmd_batch(argparse.Namespace(batch=n))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
