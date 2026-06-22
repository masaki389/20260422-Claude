"""
stamp_generator.py — 幸子LINEスタンプ用画像を一括生成

outputs/stamps/ に stamp_001.png 〜 stamp_008.png を生成する。
LINE スタンプ規格: 370×320px PNG

使い方:
    python stamp_generator.py
    python stamp_generator.py --stamp 3   # 3番だけ再生成
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image
import io

load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")
if not API_KEY:
    sys.exit("ERROR: .env に GOOGLE_API_KEY が設定されていません。")

BASE     = Path(__file__).parent
ASSETS   = BASE / "assets"
FACE_DIR = ASSETS / "characters" / "face"
STAMP_OUT = BASE / "outputs" / "stamps"
STAMP_OUT.mkdir(parents=True, exist_ok=True)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash-image:generateContent?key={key}"
)

# LINEスタンプ 8種の定義
# ターゲット：55〜64歳女性が毎日LINEで送る高頻度フレーズ
STAMPS = [
    {
        "id": 1,
        "text": "おはようございます",
        "expression": "朝の清々しい笑顔。目がぱっちりしていて、元気そうな表情。",
        "pose": "両手を胸の前でそっと合わせ、軽くお辞儀している。背筋がすっと伸びている。",
    },
    {
        "id": 2,
        "text": "ありがとう！",
        "expression": "満面の笑み。目が細くなるほど嬉しそう。感謝が溢れている表情。",
        "pose": "両手を頬に当て、体をわずかに前傾みにして喜びを体全体で表している。",
    },
    {
        "id": 3,
        "text": "了解です！",
        "expression": "はっきりとした明るい笑顔。テキパキした雰囲気。",
        "pose": "片手を顔の横でサムズアップ。もう一方の手は軽く腰に当てている。",
    },
    {
        "id": 4,
        "text": "わかるわ〜！",
        "expression": "深く共感している表情。目を細めてうんうんと頷いている感じ。",
        "pose": "両手を胸に当て、大きく頷いている。体が前のめりになっている。",
    },
    {
        "id": 5,
        "text": "えー！ホント？",
        "expression": "目を大きく見開いて驚いている。口も少し開いている。",
        "pose": "両手を口元に当てて驚きのジェスチャー。体が少し後ろに引いている。",
    },
    {
        "id": 6,
        "text": "笑える〜！",
        "expression": "お腹を抱えて笑っている。目が三日月のように細くなっている。",
        "pose": "お腹に手を当てて前かがみに笑っている。",
    },
    {
        "id": 7,
        "text": "がんばってね！",
        "expression": "力強い笑顔。応援する温かみがある目。",
        "pose": "両手でガッツポーズ。体全体でエールを送っている雰囲気。",
    },
    {
        "id": 8,
        "text": "お疲れさまでした",
        "expression": "優しい労いの笑顔。温かみのある目。",
        "pose": "軽く頭を下げてお辞儀している。両手を前で重ねている。",
    },
]


def encode_image(path: Path) -> str:
    img = Image.open(path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def call_gemini(parts: list[dict], max_retries: int = 10) -> bytes:
    url = GEMINI_URL.format(key=API_KEY)
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    headers = {"Content-Type": "application/json"}

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=120)
            data = resp.json()

            if resp.status_code == 200:
                candidate = data.get("candidates", [{}])[0]
                if "content" not in candidate:
                    reason = candidate.get("finishReason", "unknown")
                    print(f"    content なし（{reason}）... 再試行")
                    time.sleep(5)
                    continue
                for part in candidate["content"]["parts"]:
                    if "inlineData" in part:
                        return base64.b64decode(part["inlineData"]["data"])
                raise ValueError("レスポンスに画像データなし")

            elif resp.status_code in (429, 503):
                wait = 20 + attempt * 5
                print(f"    混雑中（{resp.status_code}）... {wait}秒待機 [{attempt}/{max_retries}]")
                time.sleep(wait)
            else:
                raise RuntimeError(f"APIエラー {resp.status_code}: {data}")

        except requests.RequestException as e:
            print(f"    通信エラー: {e} ... 20秒後リトライ")
            time.sleep(20)

    raise RuntimeError(f"{max_retries}回リトライしても失敗")


FONT_PATH = ASSETS / "font" / "NotoSansCJKjp-Bold.otf"
LINE_W, LINE_H = 370, 320
CHAR_AREA_H = 230   # キャラクター画像に使う高さ（残り90pxにテキスト）


def fit_into_canvas(img_bytes: bytes, width: int, height: int) -> Image.Image:
    """アスペクト比を保持したまま width×height のキャンバスに収める（余白は透明）"""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    img.thumbnail((width, height), Image.LANCZOS)
    canvas = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    x = (width  - img.width)  // 2
    y = (height - img.height) // 2
    canvas.paste(img, (x, y), img)
    return canvas


def add_speech_bubble(canvas: Image.Image, text: str) -> Image.Image:
    """キャラクター画像の下に吹き出し＋テキストを合成する"""
    from PIL import ImageDraw, ImageFont

    result = Image.new("RGBA", (LINE_W, LINE_H), (255, 255, 255, 0))
    result.paste(canvas, (0, 0), canvas)

    draw = ImageDraw.Draw(result)

    # --- フォントサイズを文字数に応じて自動調整 ---
    max_font = 32
    min_font = 18
    font_size = max(min_font, max_font - max(0, len(text) - 6) * 2)

    if FONT_PATH.exists():
        font = ImageFont.truetype(str(FONT_PATH), font_size)
    else:
        font = ImageFont.load_default()

    # テキスト領域（下部 90px）
    text_area_top = CHAR_AREA_H
    text_area_h   = LINE_H - text_area_top      # 90px

    # テキストサイズ計測
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    # 吹き出し背景（角丸矩形）
    pad_x, pad_y = 18, 10
    bubble_x1 = (LINE_W - tw) // 2 - pad_x
    bubble_y1 = text_area_top + (text_area_h - th) // 2 - pad_y
    bubble_x2 = (LINE_W + tw) // 2 + pad_x
    bubble_y2 = text_area_top + (text_area_h + th) // 2 + pad_y
    radius = 20

    # 吹き出し：白塗り＋アウトライン
    draw.rounded_rectangle(
        [bubble_x1 - 2, bubble_y1 - 2, bubble_x2 + 2, bubble_y2 + 2],
        radius=radius + 2, fill=(80, 80, 80, 220)
    )
    draw.rounded_rectangle(
        [bubble_x1, bubble_y1, bubble_x2, bubble_y2],
        radius=radius, fill=(255, 255, 255, 245)
    )

    # テキスト本体（黒＋白アウトラインで視認性UP）
    tx = (LINE_W - tw) // 2
    ty = text_area_top + (text_area_h - th) // 2
    for ox, oy in [(-2,0),(2,0),(0,-2),(0,2)]:
        draw.text((tx + ox, ty + oy), text, font=font, fill=(255, 255, 255, 255))
    draw.text((tx, ty), text, font=font, fill=(40, 40, 40, 255))

    return result


def process_stamp(img_bytes: bytes, text: str) -> bytes:
    """生成画像をキャラクターエリアに収め、吹き出しテキストを追加して保存用に変換"""
    char_canvas = fit_into_canvas(img_bytes, LINE_W, CHAR_AREA_H)
    # キャラクターを LINE_H 高さのキャンバス上部に配置
    full = Image.new("RGBA", (LINE_W, LINE_H), (255, 255, 255, 0))
    full.paste(char_canvas, (0, 0), char_canvas)
    result = add_speech_bubble(full, text)
    buf = io.BytesIO()
    result.save(buf, format="PNG")
    return buf.getvalue()


def generate_stamp(stamp: dict) -> Path:
    out_path = STAMP_OUT / f"stamp_{stamp['id']:03d}.png"

    face_path = FACE_DIR / "幸子.png"
    parts: list[dict] = []

    if face_path.exists():
        parts.append({"text": "【キャラクター参照】このキャラクターのデザインを忠実に再現してください。62歳の日本人女性。"})
        parts.append({"inlineData": {"mimeType": "image/png", "data": encode_image(face_path)}})

    parts.append({"text": (
        "【絶対禁止 — 最優先】\n"
        "画像内に一切の文字・テキスト・字幕・吹き出し・ロゴを描かないこと。これは絶対厳守。\n\n"
        "【LINEスタンプ生成指示】\n"
        "日本のLINEスタンプ風イラスト。シンプルで感情が伝わるアイコン的なスタイル。\n"
        "縦長の構図（portrait）でキャラクターを中央に大きく配置すること。横幅より縦が長い画像。\n"
        "背景は白または非常にシンプルな単色。\n"
        "日本アニメ風の丸みのあるキャラクターデザイン。太めのアウトライン。\n\n"
        f"【表情】{stamp['expression']}\n\n"
        f"【ポーズ・動作】{stamp['pose']}\n\n"
        "【スタイル条件】\n"
        "LINEスタンプらしい明るく見やすいイラスト。感情が一目で伝わること。"
        "背景はほぼ白か薄いパステルカラー。"
    )})

    img_bytes = call_gemini(parts)
    final = process_stamp(img_bytes, stamp["text"])
    out_path.write_bytes(final)
    return out_path


def main():
    parser = argparse.ArgumentParser(description="幸子LINEスタンプ画像を生成")
    parser.add_argument("--stamp", type=int, default=None, help="指定番号だけ再生成")
    args = parser.parse_args()

    targets = [s for s in STAMPS if args.stamp is None or s["id"] == args.stamp]
    total = len(targets)
    print(f"スタンプ生成開始: {total}枚\n")

    success = fail = 0
    for stamp in targets:
        print(f"[{stamp['id']}/{len(STAMPS)}] {stamp['text']}")
        try:
            path = generate_stamp(stamp)
            print(f"         ✓ 保存: {path.name}\n")
            success += 1
        except Exception as e:
            print(f"         ✗ エラー: {e}\n")
            fail += 1

    print(f"完了 — 生成:{success}枚 / 失敗:{fail}枚")
    print(f"保存先: {STAMP_OUT}")


if __name__ == "__main__":
    main()
