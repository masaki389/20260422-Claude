#!/usr/bin/env python3
"""
watermark_remover.py — ウォーターマーク一括除去

【対応】
  動画（MP4）: CapCut AI ウォーターマーク（左上）
  画像（PNG/JPG）: Gemini ウォーターマーク（左下）

【使い方】
  python watermark_remover.py                  # outputs/フォルダを処理
  python watermark_remover.py --input my_folder
  python watermark_remover.py --mode video     # 動画のみ
  python watermark_remover.py --mode image     # 画像のみ
  python watermark_remover.py --zoom 8         # ズーム率を変更（デフォルト6%）
"""

import argparse
import sys
from pathlib import Path
from PIL import Image

# moviepy のログを抑制
import logging
logging.getLogger("moviepy").setLevel(logging.ERROR)


def remove_video_watermarks(input_dir: Path, output_dir: Path, zoom_pct: float = 6.0):
    from moviepy.editor import VideoFileClip

    videos = sorted(input_dir.glob("*.mp4"))
    if not videos:
        print("  MP4ファイルが見つかりません")
        return

    print(f"\n【動画】{len(videos)}ファイルを処理中...")
    output_dir.mkdir(parents=True, exist_ok=True)

    for path in videos:
        out = output_dir / path.name
        print(f"  処理中: {path.name}", end="", flush=True)
        try:
            clip = VideoFileClip(str(path))
            w, h = clip.size
            # 上下左右をzoom_pct%ずつクロップ → ウォーターマークが四隅から消える
            mx = int(w * zoom_pct / 100)
            my = int(h * zoom_pct / 100)
            cropped = clip.crop(x1=mx, y1=my, x2=w - mx, y2=h - my)
            resized = cropped.resize((w, h))  # 元サイズに戻す
            resized.write_videofile(
                str(out),
                codec="libx264",
                audio_codec="aac",
                logger=None,
            )
            clip.close()
            print(" ✅")
        except Exception as e:
            print(f" ❌ ({e})")


def remove_image_watermarks(input_dir: Path, output_dir: Path, crop_px: int = 55):
    images = sorted(
        list(input_dir.glob("*.png")) + list(input_dir.glob("*.jpg")) + list(input_dir.glob("*.jpeg"))
    )
    if not images:
        print("  画像ファイルが見つかりません")
        return

    print(f"\n【画像】{len(images)}ファイルを処理中...")
    output_dir.mkdir(parents=True, exist_ok=True)

    for path in images:
        out = output_dir / path.name
        try:
            img = Image.open(path)
            w, h = img.size
            # Geminiウォーターマークは左下 → 左と下をcrop_pxだけ削ってリサイズ
            cropped = img.crop((crop_px, 0, w, h - crop_px))
            resized = cropped.resize((w, h), Image.LANCZOS)
            resized.save(out, quality=95)
            print(f"  ✅ {path.name}")
        except Exception as e:
            print(f"  ❌ {path.name}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Watermark remover")
    parser.add_argument("--input", default="outputs", help="input folder (default: outputs)")
    parser.add_argument("--output", help="output folder (default: input/cleaned/)")
    parser.add_argument("--mode", choices=["video", "image", "all"], default="all", help="target: video / image / all")
    parser.add_argument("--zoom", type=float, default=6.0, help="video zoom pct (default: 6)")
    parser.add_argument("--crop", type=int, default=55, help="image crop px (default: 55)")
    args = parser.parse_args()

    input_dir = Path(args.input)
    if not input_dir.exists():
        print(f"フォルダが見つかりません: {input_dir}")
        sys.exit(1)

    output_dir = Path(args.output) if args.output else input_dir / "cleaned"
    print(f"入力: {input_dir}")
    print(f"出力: {output_dir}")

    if args.mode in ("video", "all"):
        remove_video_watermarks(input_dir, output_dir, args.zoom)
    if args.mode in ("image", "all"):
        remove_image_watermarks(input_dir, output_dir, args.crop)

    print(f"\n完了。出力先: {output_dir}")


if __name__ == "__main__":
    main()
