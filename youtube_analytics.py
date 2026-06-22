"""
youtube_analytics.py — YouTube チャンネル分析レポート

※ 事前に python youtube_auth.py で認証を完了させてください。

使い方:
    python youtube_analytics.py              # 幸子チャンネル・直近28日
    python youtube_analytics.py --channel tenshi  # 転職チャンネル
    python youtube_analytics.py --days 90    # 直近90日
    python youtube_analytics.py --videos     # 動画別詳細
    python youtube_analytics.py --all        # 全レポートを出力
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE        = Path(__file__).parent
TOKEN_FILES = {
    "sachiko": BASE / "token.json",
    "tenshi":  BASE / "token_tenshi.json",
}
TOKEN_FILE = TOKEN_FILES["sachiko"]  # main()で上書き

CHANNELS = {
    "sachiko": "UCL2SaCrx2RZbDe9ILxDVEpw",   # 幸子（62）の老後物語
    "tenshi":  "UCSg9wSPjebvFq3Dyq0ElcWA",    # 転職の勇者
}
CHANNEL_NAMES = {
    "sachiko": "幸子（62）の老後物語",
    "tenshi":  "転職の勇者【会社・業界の口コミ】",
}

# デフォルトは幸子（後でmain()で上書き）
CHANNEL_ID   = CHANNELS["sachiko"]
CHANNEL_BARE = CHANNEL_ID.replace("UC", "channel/UC", 1)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
]


def load_credentials():
    if not TOKEN_FILE.exists():
        sys.exit(
            "token.json が見つかりません。\n"
            "先に  python youtube_auth.py  を実行して認証してください。"
        )
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError:
        sys.exit("pip install google-auth google-auth-oauthlib を実行してください。")

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_services(creds):
    from googleapiclient.discovery import build
    youtube   = build("youtube",        "v3",      credentials=creds)
    analytics = build("youtubeAnalytics", "v2",    credentials=creds)
    return youtube, analytics


def date_range(days: int):
    end   = datetime.today()
    start = end - timedelta(days=days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def fmt(n) -> str:
    try:
        return f"{float(n):,.1f}" if "." in str(n) else f"{int(n):,}"
    except (ValueError, TypeError):
        return str(n)


# ---------------------------------------------------------------------------
# チャンネルサマリー
# ---------------------------------------------------------------------------

def report_channel_summary(youtube, analytics, days: int):
    start, end = date_range(days)

    # 基本統計
    ch = youtube.channels().list(
        part="snippet,statistics", id=CHANNEL_ID
    ).execute()["items"][0]
    s  = ch["statistics"]

    print("=" * 65)
    print(f"  幸子（62）の老後物語 ／ 直近{days}日レポート  ({start} 〜 {end})")
    print("=" * 65)
    print(f"  登録者数   : {fmt(s.get('subscriberCount','?'))} 人")
    print(f"  総再生数   : {fmt(s.get('viewCount','?'))} 回")
    print(f"  動画本数   : {fmt(s.get('videoCount','?'))} 本")
    print()

    # Analytics
    res = analytics.reports().query(
        ids=f"channel=={CHANNEL_ID}",
        startDate=start,
        endDate=end,
        metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
                "subscribersGained,subscribersLost,estimatedRevenue",
    ).execute()

    rows = res.get("rows", [[]])
    r = rows[0] if rows else [0] * 9
    labels = [
        "再生数", "総視聴時間(分)", "平均視聴時間(秒)", "平均視聴率(%)",
        "登録増", "登録減", "推定収益(USD)",
    ]
    print("  【直近パフォーマンス】")
    for label, val in zip(labels, r):
        print(f"  {label:<22}: {fmt(val)}")
    print()


# ---------------------------------------------------------------------------
# 動画別レポート
# ---------------------------------------------------------------------------

def report_videos(youtube, analytics, days: int):
    start, end = date_range(days)

    # 動画一覧取得
    ch      = youtube.channels().list(part="contentDetails", id=CHANNEL_ID).execute()
    uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    pl      = youtube.playlistItems().list(
        part="contentDetails", playlistId=uploads, maxResults=20
    ).execute()
    video_ids = [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
    if not video_ids:
        print("動画が見つかりません。")
        return

    vdata = youtube.videos().list(
        part="snippet,statistics,contentDetails", id=",".join(video_ids)
    ).execute()

    print("=" * 75)
    print(f"  動画別パフォーマンス（直近{days}日）")
    print("=" * 75)
    print(f"  {'#':<3} {'タイトル':<30} {'再生':>7} {'維持率':>6} {'平均秒':>6} {'収益$':>7}  投稿日")
    print("  " + "-" * 72)

    for idx, item in enumerate(vdata.get("items", []), 1):
        vid   = item["id"]
        title = item["snippet"]["title"][:28]
        date  = item["snippet"]["publishedAt"][:10]

        try:
            ar = analytics.reports().query(
                ids=f"channel=={CHANNEL_ID}",
                startDate=start,
                endDate=end,
                metrics="views,averageViewPercentage,averageViewDuration,estimatedRevenue",
                filters=f"video=={vid}",
            ).execute()
            rows = ar.get("rows", [])
            if rows:
                views, retention, avg_sec, revenue = rows[0]
            else:
                views = int(item["statistics"].get("viewCount", 0))
                retention = avg_sec = revenue = "-"
        except Exception as e:
            views = int(item["statistics"].get("viewCount", 0))
            retention = avg_sec = revenue = "-"

        r_str  = f"{retention:.1f}%" if isinstance(retention, float) else "-"
        s_str  = f"{int(avg_sec)}秒"  if isinstance(avg_sec, float)  else "-"
        rev_str = f"${revenue:.2f}"  if isinstance(revenue, float)   else "-"
        print(f"  {idx:<3} {title:<30} {fmt(views):>7} {r_str:>6} {s_str:>6} {rev_str:>7}  {date}")

    print()


# ---------------------------------------------------------------------------
# 流入元レポート
# ---------------------------------------------------------------------------

def report_traffic(analytics, days: int):
    start, end = date_range(days)
    res = analytics.reports().query(
        ids=f"channel=={CHANNEL_ID}",
        startDate=start,
        endDate=end,
        metrics="views",
        dimensions="insightTrafficSourceType",
        sort="-views",
        maxResults=10,
    ).execute()

    print("=" * 65)
    print(f"  流入元（直近{days}日）")
    print("=" * 65)
    for row in res.get("rows", []):
        source, views = row
        print(f"  {source:<35} {fmt(views):>10} 回")
    print()


# ---------------------------------------------------------------------------
# 年齢・性別レポート
# ---------------------------------------------------------------------------

def report_demographics(analytics, days: int):
    start, end = date_range(days)

    print("=" * 65)
    print(f"  視聴者属性（直近{days}日）")
    print("=" * 65)

    # 年齢
    try:
        res = analytics.reports().query(
            ids=f"channel=={CHANNEL_ID}",
            startDate=start,
            endDate=end,
            metrics="viewerPercentage",
            dimensions="ageGroup,gender",
            sort="-viewerPercentage",
        ).execute()
        for row in res.get("rows", [])[:10]:
            age, gender, pct = row
            g = "女性" if gender == "female" else "男性"
            print(f"  {age} {g:<4} : {fmt(pct)}%")
    except Exception as e:
        print(f"  属性データ取得エラー: {e}")
    print()


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="YouTube チャンネル分析レポート")
    parser.add_argument("--channel", default="sachiko", choices=["sachiko","tenshi"], help="チャンネル選択")
    parser.add_argument("--days",    type=int, default=28, help="集計期間（日数）")
    parser.add_argument("--videos",  action="store_true",  help="動画別レポート")
    parser.add_argument("--traffic", action="store_true",  help="流入元レポート")
    parser.add_argument("--demo",    action="store_true",  help="視聴者属性レポート")
    parser.add_argument("--all",     action="store_true",  help="全レポート出力")
    args = parser.parse_args()

    global CHANNEL_ID, CHANNEL_BARE, TOKEN_FILE
    CHANNEL_ID   = CHANNELS[args.channel]
    CHANNEL_BARE = CHANNEL_ID.replace("UC", "channel/UC", 1)
    TOKEN_FILE   = TOKEN_FILES[args.channel]
    print(f"📺 チャンネル: {CHANNEL_NAMES[args.channel]}\n")

    creds = load_credentials()
    youtube, analytics = build_services(creds)

    report_channel_summary(youtube, analytics, args.days)

    if args.all or args.videos:
        report_videos(youtube, analytics, args.days)

    if args.all or args.traffic:
        report_traffic(analytics, args.days)

    if args.all or args.demo:
        report_demographics(analytics, args.days)

    print(f"実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}")


if __name__ == "__main__":
    main()
