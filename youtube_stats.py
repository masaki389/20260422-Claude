"""
youtube_stats.py — 幸子チャンネルのYouTube統計を取得・表示

YouTube Data API v3 を使用（既存の GOOGLE_API_KEY を流用）。
公開データ（再生数・登録者数・動画リスト）を取得する。

使い方:
    python youtube_stats.py              # チャンネル概要 + 動画一覧
    python youtube_stats.py --video <ID> # 特定動画の詳細
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY     = os.getenv("GOOGLE_API_KEY")
CHANNEL_ID  = "UCL2SaCrx2RZbDe9ILxDVEpw"
BASE_URL    = "https://www.googleapis.com/youtube/v3"


def get(endpoint: str, params: dict) -> dict:
    params["key"] = API_KEY
    resp = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=30)
    if resp.status_code != 200:
        sys.exit(f"API エラー {resp.status_code}: {resp.text}")
    return resp.json()


def fmt_num(n: int | str) -> str:
    try:
        return f"{int(n):,}"
    except (ValueError, TypeError):
        return str(n)


def channel_stats() -> dict:
    data = get("channels", {
        "part": "snippet,statistics,contentDetails",
        "id": CHANNEL_ID,
    })
    if not data.get("items"):
        sys.exit("チャンネルが見つかりません。CHANNEL_ID を確認してください。")
    return data["items"][0]


def video_list(max_results: int = 20) -> list[dict]:
    """アップロード済み動画を新着順で取得"""
    ch = channel_stats()
    uploads_playlist = ch["contentDetails"]["relatedPlaylists"]["uploads"]

    data = get("playlistItems", {
        "part": "contentDetails",
        "playlistId": uploads_playlist,
        "maxResults": max_results,
    })
    video_ids = [item["contentDetails"]["videoId"] for item in data.get("items", [])]
    if not video_ids:
        return []

    vdata = get("videos", {
        "part": "snippet,statistics,contentDetails",
        "id": ",".join(video_ids),
    })
    return vdata.get("items", [])


def parse_duration(iso: str) -> str:
    """PT4M30S → 4:30"""
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    if not m:
        return iso
    h, mn, s = (int(x or 0) for x in m.groups())
    if h:
        return f"{h}:{mn:02d}:{s:02d}"
    return f"{mn}:{s:02d}"


def print_channel_summary(ch: dict):
    s = ch["statistics"]
    sn = ch["snippet"]
    print("=" * 60)
    print(f"📺 {sn['title']}")
    print(f"   登録者数    : {fmt_num(s.get('subscriberCount', '非公開'))}")
    print(f"   総再生数    : {fmt_num(s.get('viewCount', 0))}")
    print(f"   動画本数    : {fmt_num(s.get('videoCount', 0))}")
    print(f"   開設日      : {sn['publishedAt'][:10]}")
    print("=" * 60)


def print_video_table(videos: list[dict]):
    print(f"\n{'#':<3} {'タイトル':<35} {'再生数':>8} {'高評価':>6} {'コメ':>5} {'長さ':>6}  投稿日")
    print("-" * 85)
    for i, v in enumerate(videos, 1):
        sn = v["snippet"]
        st = v["statistics"]
        cd = v["contentDetails"]
        title = sn["title"][:33]
        views    = fmt_num(st.get("viewCount", 0))
        likes    = fmt_num(st.get("likeCount", 0))
        comments = fmt_num(st.get("commentCount", 0))
        dur  = parse_duration(cd.get("duration", ""))
        date = sn["publishedAt"][:10]
        print(f"{i:<3} {title:<35} {views:>8} {likes:>6} {comments:>5} {dur:>6}  {date}")


def video_detail(video_id: str):
    data = get("videos", {
        "part": "snippet,statistics,contentDetails",
        "id": video_id,
    })
    items = data.get("items", [])
    if not items:
        print(f"動画 {video_id} が見つかりません。")
        return
    v  = items[0]
    sn = v["snippet"]
    st = v["statistics"]
    cd = v["contentDetails"]
    print(f"\n【{sn['title']}】")
    print(f"  投稿日    : {sn['publishedAt'][:10]}")
    print(f"  長さ      : {parse_duration(cd.get('duration',''))}")
    print(f"  再生数    : {fmt_num(st.get('viewCount',0))}")
    print(f"  高評価    : {fmt_num(st.get('likeCount',0))}")
    print(f"  コメント  : {fmt_num(st.get('commentCount',0))}")
    print(f"  説明文    :\n{sn.get('description','')[:300]}")


def main():
    if not API_KEY:
        sys.exit("ERROR: .env に GOOGLE_API_KEY が設定されていません。")

    parser = argparse.ArgumentParser(description="幸子チャンネルのYouTube統計を表示")
    parser.add_argument("--video", default=None, help="特定動画IDの詳細を表示")
    parser.add_argument("--n", type=int, default=10, help="取得する動画数（デフォルト10）")
    args = parser.parse_args()

    if args.video:
        video_detail(args.video)
    else:
        ch = channel_stats()
        print_channel_summary(ch)
        print(f"\n最新 {args.n} 本の動画:")
        videos = video_list(args.n)
        print_video_table(videos)
        print(f"\n実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}")


if __name__ == "__main__":
    main()
