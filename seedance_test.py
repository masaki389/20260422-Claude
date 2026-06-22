"""
seedance_test.py — Seedance 2.0 API テスト
既存の画像1枚をアニメーション動画に変換して品質を確認する

【使い方】
    python seedance_test.py

【事前準備】
    .env に FAL_KEY=xxx を追加する
    → https://fal.ai/dashboard/keys でAPIキー取得
"""

import os
import sys
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

FAL_KEY = os.getenv("FAL_KEY")
if not FAL_KEY:
    print("=" * 50)
    print("  ERROR: FAL_KEY が .env にありません")
    print("=" * 50)
    print()
    print("  1. https://fal.ai/dashboard/keys にアクセス")
    print("  2. 「Generate New Key」でAPIキーを取得")
    print("  3. .env に以下を追加:")
    print("     FAL_KEY=your_api_key_here")
    sys.exit(1)

import fal_client

BASE = Path(__file__).parent

# テスト対象の画像（山崎製パン・カット1）
TEST_IMAGE = BASE / "outputs/tenshi/TOP0_山崎製パン_285K/cut_001.png"
OUTPUT_PATH = BASE / "outputs/test_seedance_output.mp4"


def on_queue_update(update):
    if hasattr(update, "logs"):
        for log in update.logs:
            print(f"  [{log.get('level','INFO')}] {log.get('message','')}")


def main():
    if not TEST_IMAGE.exists():
        sys.exit(f"ERROR: テスト画像が見つかりません: {TEST_IMAGE}")

    print(f"テスト画像: {TEST_IMAGE.name}")
    print("画像をBase64エンコード中...")

    import base64
    with open(TEST_IMAGE, "rb") as f:
        image_data = base64.b64encode(f.read()).decode()
    image_url = f"data:image/png;base64,{image_data}"
    print("エンコード完了")

    print()
    print("Seedance 2.0 API で動画生成中（30〜60秒かかります）...")

    result = fal_client.subscribe(
        "bytedance/seedance-2.0/image-to-video",
        arguments={
            "prompt": "深夜の大型パン工場の外観。白い金属パネルの建物。赤と白のトラックが静かに停まっている。照明がゆっくり揺れている。アニメスタイル。",
            "image_url": image_url,
            "resolution": "720p",
            "duration": "5",
            "aspect_ratio": "9:16",
        },
        with_logs=True,
        on_queue_update=on_queue_update,
    )

    video_url = result["video"]["url"]
    print(f"\n生成完了: {video_url[:60]}...")

    print("動画をダウンロード中...")
    resp = requests.get(video_url, timeout=60)
    OUTPUT_PATH.write_bytes(resp.content)

    size_kb = OUTPUT_PATH.stat().st_size // 1024
    print()
    print("=" * 50)
    print(f"  ✅ 保存完了: {OUTPUT_PATH}")
    print(f"  ファイルサイズ: {size_kb} KB")
    print("=" * 50)
    print()
    print("CapCutで開いて品質を確認してください。")


if __name__ == "__main__":
    main()
