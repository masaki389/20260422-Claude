#!/usr/bin/env python3
"""
inpaint_remover.py — ウォーターマーク・テキストを物理的に除去

周辺ピクセルから背景を推定して塗り潰す（ズーム不要）

【プリセット】
  capcut   : CapCut AI ウォーターマーク（左上）
  gemini   : Gemini ダイヤマーク（左下）

【使い方】
  # プリセットで除去（座標を自動設定）
  python inpaint_remover.py --input video.mp4 --preset capcut
  python inpaint_remover.py --input video.mp4 --preset gemini
  python inpaint_remover.py --input video.mp4 --preset capcut gemini  # 両方

  # 座標を手動指定（x, y, 幅, 高さ）— 誤って入れたテキスト除去など
  python inpaint_remover.py --input video.mp4 --region 100 200 300 50

  # 画像にも使える
  python inpaint_remover.py --input image.png --preset gemini

  # フォルダ内を一括処理
  python inpaint_remover.py --input_dir outputs/ --preset capcut
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

# ── プリセット定義（x, y, width, height）──────────────────────────────────
# 実際のウォーターマーク位置に合わせて調整してください
PRESETS = {
    "capcut": (0, 0, 200, 55),    # CapCut AI（左上）
    "gemini": (0, -70, 80, 70),   # Gemini ダイヤ（左下）: y=-70は下からの指定
}


def make_mask(h: int, w: int, regions: list[tuple]) -> np.ndarray:
    """指定領域のマスク画像を生成（白=除去対象）"""
    mask = np.zeros((h, w), dtype=np.uint8)
    for (x, y, rw, rh) in regions:
        # y が負の値 → 下からの相対位置
        if y < 0:
            y = h + y
        x1 = max(0, x)
        y1 = max(0, y)
        x2 = min(w, x + rw)
        y2 = min(h, y + rh)
        mask[y1:y2, x1:x2] = 255
    return mask


def inpaint_frame(frame: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """1フレームのinpainting処理"""
    return cv2.inpaint(frame, mask, inpaintRadius=4, flags=cv2.INPAINT_TELEA)


def process_image(input_path: Path, output_path: Path, regions: list[tuple]):
    img = cv2.imread(str(input_path))
    if img is None:
        print(f"  ❌ 読み込み失敗: {input_path}")
        return
    h, w = img.shape[:2]
    mask = make_mask(h, w, regions)
    result = inpaint_frame(img, mask)
    cv2.imwrite(str(output_path), result)
    print(f"  ✅ {input_path.name} → {output_path.name}")


def process_video(input_path: Path, output_path: Path, regions: list[tuple]):
    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        print(f"  ❌ 読み込み失敗: {input_path}")
        return

    fps    = cap.get(cv2.CAP_PROP_FPS)
    width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))

    mask = make_mask(height, width, regions)

    print(f"  処理中: {input_path.name}  ({total}フレーム)", end="", flush=True)
    count = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        out.write(inpaint_frame(frame, mask))
        count += 1
        if count % 30 == 0:
            pct = int(count / total * 100)
            print(f"\r  処理中: {input_path.name}  {pct}%", end="", flush=True)

    cap.release()
    out.release()
    print(f"\r  ✅ {input_path.name} → {output_path.name}          ")


def resolve_regions(preset_names: list[str], manual: tuple | None) -> list[tuple]:
    regions = []
    for name in preset_names:
        if name in PRESETS:
            regions.append(PRESETS[name])
        else:
            print(f"  不明なプリセット: {name}")
    if manual:
        regions.append(manual)
    return regions


def main():
    parser = argparse.ArgumentParser(description="Inpaint watermark remover")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input",     help="input file (image or video)")
    group.add_argument("--input_dir", help="input folder (batch)")

    parser.add_argument("--output",  help="output file/folder (default: cleaned/)")
    parser.add_argument("--preset",  nargs="+", choices=list(PRESETS.keys()),
                        help="preset: capcut / gemini")
    parser.add_argument("--region",  nargs=4, type=int, metavar=("X", "Y", "W", "H"),
                        help="manual region: X Y Width Height")

    args = parser.parse_args()

    if not args.preset and not args.region:
        print("--preset か --region のどちらかを指定してください")
        sys.exit(1)

    regions = resolve_regions(args.preset or [], tuple(args.region) if args.region else None)
    if not regions:
        print("除去領域が指定されていません")
        sys.exit(1)

    IMAGE_EXT = {".png", ".jpg", ".jpeg"}
    VIDEO_EXT = {".mp4", ".mov", ".avi"}

    # ── 単一ファイル ──────────────────────────────────────────────────────
    if args.input:
        path = Path(args.input)
        out_dir = Path(args.output) if args.output else path.parent / "cleaned"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / path.name
        ext = path.suffix.lower()
        if ext in IMAGE_EXT:
            process_image(path, out_path, regions)
        elif ext in VIDEO_EXT:
            process_video(path, out_path, regions)
        else:
            print(f"対応していない形式: {ext}")

    # ── フォルダ一括 ───────────────────────────────────────────────────────
    else:
        in_dir = Path(args.input_dir)
        out_dir = Path(args.output) if args.output else in_dir / "cleaned"
        out_dir.mkdir(parents=True, exist_ok=True)

        files = [f for f in sorted(in_dir.iterdir())
                 if f.suffix.lower() in IMAGE_EXT | VIDEO_EXT]
        print(f"{len(files)}ファイルを処理します")

        for f in files:
            out_path = out_dir / f.name
            ext = f.suffix.lower()
            if ext in IMAGE_EXT:
                process_image(f, out_path, regions)
            elif ext in VIDEO_EXT:
                process_video(f, out_path, regions)

    print("\n完了")


if __name__ == "__main__":
    main()
