"""
video_assembler.py — 生成画像をケンバーンズ動画に変換

outputs/ の cut_001.png ... を読み込み、
・ケンバーンズ効果（ズーム・パン）
・クロスフェードつなぎ
・BGM（assets/bgm/ に音楽ファイルがあれば自動追加）
・字幕（assets/font/ に日本語フォントがあれば自動表示）
で outputs/final_YYYYMMDD_HHMMSS.mp4 を生成する。

使い方:
    python video_assembler.py
    python video_assembler.py --no-subtitle   # 字幕なし
    python video_assembler.py --size 1280x720 # 出力サイズ変更
    python video_assembler.py --fps 30        # フレームレート変更
"""

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

BASE    = Path(__file__).parent
OUTPUTS = BASE / "outputs"
ASSETS  = BASE / "assets"
FONT_DIR = ASSETS / "font"
BGM_DIR  = ASSETS / "bgm"

DEFAULT_SECONDS_PER_CUT = 5.0   # 秒数指定なし時のデフォルト
FADE_DURATION           = 0.5   # クロスフェード秒数
FPS                     = 24
OUT_SIZE                = (1920, 1080)


# ---------------------------------------------------------------------------
# 台本パーサ（秒数だけ使う）
# ---------------------------------------------------------------------------

def parse_script_timing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"\n{2,}", text.strip())
    result = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        entry: dict = {"内容": "", "秒数": None}
        for line in block.splitlines():
            line = line.strip()
            if re.search(r"内容[：:]", line):
                entry["内容"] = re.sub(r".*内容[：:]", "", line).strip()
            elif re.search(r"秒数[：:]", line):
                val = re.sub(r".*秒数[：:]", "", line).strip()
                try:
                    entry["秒数"] = float(val)
                except ValueError:
                    pass
        result.append(entry)
    return result


def estimate_duration(text: str) -> float:
    """文字数から読み上げ時間を推定（4文字/秒、最低3秒・最大12秒）"""
    secs = max(3.0, min(12.0, len(text) / 4.0)) if text else DEFAULT_SECONDS_PER_CUT
    return secs


# ---------------------------------------------------------------------------
# フォント読み込み
# ---------------------------------------------------------------------------

def load_font(size: int = 42) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    # assets/font/ 内の .ttf .otf を探す
    for ext in ("*.ttf", "*.otf"):
        for p in FONT_DIR.glob(ext):
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                pass
    # システムフォントのフォールバック
    for candidate in [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    ]:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:
                pass
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# ケンバーンズ効果
# ---------------------------------------------------------------------------

def ken_burns_clip(img_path: Path, duration: float, effect: str | None = None,
                   out_size: tuple[int, int] = OUT_SIZE, fps: int = FPS):
    """静止画にケンバーンズ効果をかけた動画クリップデータを返す（numpy配列リスト）"""
    import random
    EFFECTS = ["zoom_in", "zoom_out", "pan_lr", "pan_rl", "pan_diag"]
    effect = effect or random.choice(EFFECTS)

    out_w, out_h = out_size
    img = Image.open(img_path).convert("RGB")

    # 20%余白をもたせてリサイズ
    scale = max(out_w / img.width, out_h / img.height) * 1.2
    big_w = max(int(img.width * scale), out_w + 4)
    big_h = max(int(img.height * scale), out_h + 4)
    img_big = np.array(img.resize((big_w, big_h), Image.LANCZOS), dtype=np.uint8)

    n_frames = max(1, int(duration * fps))

    def get_crop(t: float) -> np.ndarray:
        p = t / duration  # 0.0 → 1.0

        if effect == "zoom_in":
            s = 1.0 + 0.12 * p
            cw, ch = int(out_w / s), int(out_h / s)
            x = (big_w - cw) // 2
            y = (big_h - ch) // 2
        elif effect == "zoom_out":
            s = 1.12 - 0.12 * p
            cw, ch = int(out_w / s), int(out_h / s)
            x = (big_w - cw) // 2
            y = (big_h - ch) // 2
        elif effect == "pan_lr":
            cw, ch = out_w, out_h
            x = int(p * max(big_w - out_w, 0))
            y = (big_h - out_h) // 2
        elif effect == "pan_rl":
            cw, ch = out_w, out_h
            x = int((1 - p) * max(big_w - out_w, 0))
            y = (big_h - out_h) // 2
        else:  # pan_diag
            cw, ch = out_w, out_h
            x = int(p * max(big_w - out_w, 0))
            y = int(p * max(big_h - out_h, 0))

        x = max(0, min(x, big_w - cw))
        y = max(0, min(y, big_h - ch))
        crop = img_big[y:y + ch, x:x + cw]

        if crop.shape[:2] != (out_h, out_w):
            crop = np.array(
                Image.fromarray(crop).resize((out_w, out_h), Image.LANCZOS),
                dtype=np.uint8,
            )
        return crop

    frames = [get_crop(i / max(n_frames - 1, 1) * duration) for i in range(n_frames)]
    return frames, fps


# ---------------------------------------------------------------------------
# 字幕オーバーレイ
# ---------------------------------------------------------------------------

def add_subtitle_to_frames(
    frames: list[np.ndarray],
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    out_size: tuple[int, int] = OUT_SIZE,
) -> list[np.ndarray]:
    if not text:
        return frames

    W, H = out_size
    result = []
    for frame in frames:
        img = Image.fromarray(frame)
        draw = ImageDraw.Draw(img)

        # テキストを折り返し（25文字ごと）
        line_len = 25
        lines = [text[i:i + line_len] for i in range(0, len(text), line_len)]

        try:
            bbox = draw.textbbox((0, 0), lines[0], font=font)
            line_h = bbox[3] - bbox[1] + 6
        except Exception:
            line_h = 50

        total_h = line_h * len(lines)
        margin_bottom = 60

        for j, line in enumerate(lines):
            try:
                bbox = draw.textbbox((0, 0), line, font=font)
                tw = bbox[2] - bbox[0]
            except Exception:
                tw = len(line) * 20

            tx = (W - tw) // 2
            ty = H - total_h - margin_bottom + j * line_h

            # 黒縁
            for dx, dy in [(-2, -2), (2, -2), (-2, 2), (2, 2), (0, -2), (0, 2), (-2, 0), (2, 0)]:
                draw.text((tx + dx, ty + dy), line, fill=(0, 0, 0), font=font)
            # 白文字
            draw.text((tx, ty), line, fill=(255, 255, 255), font=font)

        result.append(np.array(img))
    return result


# ---------------------------------------------------------------------------
# クロスフェード
# ---------------------------------------------------------------------------

def crossfade(frames_a: list, frames_b: list, fade_secs: float, fps: int) -> list:
    n = int(fade_secs * fps)
    n = min(n, len(frames_a), len(frames_b))
    if n == 0:
        return frames_a + frames_b

    fused = []
    for i in range(n):
        alpha = (i + 1) / (n + 1)
        fa = frames_a[-(n - i)].astype(np.float32)
        fb = frames_b[i].astype(np.float32)
        fused.append(np.clip(fa * (1 - alpha) + fb * alpha, 0, 255).astype(np.uint8))

    return frames_a[:-n] + fused + frames_b[n:]


# ---------------------------------------------------------------------------
# BGM 読み込み
# ---------------------------------------------------------------------------

def find_bgm() -> Path | None:
    for ext in ("*.mp3", "*.wav", "*.aac", "*.m4a"):
        for p in BGM_DIR.glob(ext):
            return p
    return None


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="生成画像をケンバーンズ動画に変換")
    parser.add_argument("--no-subtitle", action="store_true")
    parser.add_argument("--size", default="1920x1080", help="出力サイズ 例: 1280x720")
    parser.add_argument("--fps", type=int, default=FPS)
    args = parser.parse_args()

    try:
        out_w, out_h = map(int, args.size.lower().split("x"))
        out_size = (out_w, out_h)
    except ValueError:
        print(f"--size の形式が不正です: {args.size}")
        sys.exit(1)

    # 画像一覧
    img_paths = sorted(OUTPUTS.glob("cut_*.png"))
    if not img_paths:
        print("outputs/ に cut_*.png が見つかりません。先に auto_studio.py を実行してください。")
        sys.exit(1)
    print(f"画像 {len(img_paths)} 枚を読み込みます。")

    # 台本タイミング
    script_data = parse_script_timing(BASE / "script.txt")

    # フォント
    font = None if args.no_subtitle else load_font(42)
    if font and not args.no_subtitle:
        print("字幕フォント: 読み込み成功")
    elif not args.no_subtitle:
        print("字幕フォント: 未検出（assets/font/ に .ttf を置くと日本語字幕が使えます）")

    # 全フレーム生成
    all_frames: list[np.ndarray] = []
    import random
    effects_cycle = ["zoom_in", "zoom_out", "pan_lr", "pan_rl", "pan_diag"]

    for idx, img_path in enumerate(img_paths):
        # 秒数取得
        entry = script_data[idx] if idx < len(script_data) else {}
        duration = entry.get("秒数") or estimate_duration(entry.get("内容", ""))
        subtitle  = entry.get("内容", "") if not args.no_subtitle else ""

        effect = effects_cycle[idx % len(effects_cycle)]
        print(f"  [{idx+1}/{len(img_paths)}] {img_path.name} ({duration:.1f}秒, {effect})")

        frames, fps = ken_burns_clip(img_path, duration, effect, out_size, args.fps)

        if font and subtitle:
            frames = add_subtitle_to_frames(frames, subtitle, font, out_size)

        if all_frames and FADE_DURATION > 0:
            all_frames = crossfade(all_frames, frames, FADE_DURATION, args.fps)
        else:
            all_frames.extend(frames)

    print(f"\n総フレーム数: {len(all_frames)} ({len(all_frames)/args.fps:.1f}秒)")

    # 出力パス
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUTS / f"final_{timestamp}.mp4"

    # FFmpeg で書き出し
    _write_video(all_frames, out_path, args.fps, out_size)

    # BGM 合成
    bgm = find_bgm()
    if bgm:
        out_with_bgm = OUTPUTS / f"final_{timestamp}_bgm.mp4"
        _add_bgm(out_path, bgm, out_with_bgm, len(all_frames) / args.fps)
        print(f"BGM合成: {out_with_bgm}")
    else:
        print("BGMなし（assets/bgm/ に音楽ファイルを置くと自動追加されます）")

    # SRTサブタイトルファイル生成
    if script_data and not args.no_subtitle:
        _write_srt(script_data, img_paths, OUTPUTS / f"final_{timestamp}.srt", args.fps)

    print(f"\n完成: {out_path}")


# ---------------------------------------------------------------------------
# 書き出しヘルパー
# ---------------------------------------------------------------------------

def _write_video(frames: list[np.ndarray], out_path: Path, fps: int, size: tuple[int, int]):
    import imageio_ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    import subprocess, io

    W, H = size
    cmd = [
        ffmpeg_exe, "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", f"{W}x{H}",
        "-pix_fmt", "rgb24",
        "-r", str(fps),
        "-i", "pipe:0",
        "-vcodec", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", "18",
        "-preset", "fast",
        str(out_path),
    ]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    for frame in frames:
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    proc.wait()
    print(f"動画書き出し完了: {out_path}")


def _add_bgm(video_path: Path, bgm_path: Path, out_path: Path, duration: float):
    import imageio_ffmpeg, subprocess
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe, "-y",
        "-i", str(video_path),
        "-stream_loop", "-1", "-i", str(bgm_path),
        "-shortest",
        "-t", str(duration),
        "-map", "0:v:0", "-map", "1:a:0",
        "-vcodec", "copy",
        "-acodec", "aac", "-b:a", "192k",
        str(out_path),
    ]
    subprocess.run(cmd, stderr=subprocess.DEVNULL)


def _write_srt(script_data: list, img_paths: list, out_path: Path, fps: int):
    lines = []
    t = 0.0
    for i, img_path in enumerate(img_paths):
        entry = script_data[i] if i < len(script_data) else {}
        dur  = entry.get("秒数") or estimate_duration(entry.get("内容", ""))
        text = entry.get("内容", "")
        if not text:
            t += dur
            continue

        def fmt(sec):
            h = int(sec // 3600)
            m = int((sec % 3600) // 60)
            s = int(sec % 60)
            ms = int((sec % 1) * 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        lines.append(str(i + 1))
        lines.append(f"{fmt(t)} --> {fmt(t + dur - 0.1)}")
        lines.append(text)
        lines.append("")
        t += dur

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"SRT字幕: {out_path}")


if __name__ == "__main__":
    main()
