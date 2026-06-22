"""
tenshi_studio.py — 転職チャンネル用 画像一括生成スクリプト

tenshi_scripts/ の台本を読み込み、Gemini API で各カットの画像を生成して
outputs/tenshi/<動画名>/cut_001.png ... と保存する。

使い方:
    python tenshi_studio.py                          # 全カット生成（script指定なし時はTOP1）
    python tenshi_studio.py --script TOP1_山岡家     # 台本名を指定
    python tenshi_studio.py --resume                 # 生成済みをスキップして続きから
    python tenshi_studio.py --cut 3                  # カット3だけ再生成

台本フォーマット（tenshi_scripts/XXX.txt）:
    【カット1】
    場所：ラーメン店外観
    キャラ：田中悠斗（アップ）
    衣装：黒Tシャツ・黒鉢巻き・黒エプロン
    内容：夜の繁盛するラーメン店。行列ができている
    カメラ：ローアングル・見上げ構図
    秒数：3
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image

load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")
if not API_KEY:
    sys.exit("ERROR: .env に GOOGLE_API_KEY が設定されていません。")

BASE         = Path(__file__).parent
ASSETS       = BASE / "assets"
TENSHI_CHARS = ASSETS / "characters" / "tenshi"
OUTPUTS_ROOT = BASE / "outputs" / "tenshi"
SCRIPTS_DIR  = BASE / "tenshi_scripts"
RESEARCH_DIR = BASE / "research"
GEN_LOG      = BASE / "generation_log.txt"

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.1-flash-image-preview:generateContent?key={key}"
)

# キャラクター参照画像（衣装ごとに使い分け）
CHAR_REFS = {
    "私服":   TENSHI_CHARS / "田中悠斗_私服.png",
    "制服":   TENSHI_CHARS / "田中悠斗_制服.png",
    "default": TENSHI_CHARS / "田中悠斗_私服.png",
}

# キャラクター固定設定
CHAR_DESCRIPTION = (
    "田中悠斗（25歳）: 短い黒髪・自然なくせ毛・茶色の目・"
    "やや低めの等身・親しみやすい印象のアニメキャラクター。"
)

IMAGE_SIZE = "1080x1920"  # 縦型Shorts用


# ---------------------------------------------------------------------------
# 台本パーサ
# ---------------------------------------------------------------------------

def parse_script(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"\n{2,}", text.strip())
    cuts = []
    for block in blocks:
        block = block.strip()
        if not block or not re.search(r"【カット\d+】", block):
            continue
        cut: dict = {
            "場所": "",
            "キャラ": "",
            "衣装": "",
            "内容": "",
            "カメラ": "",
            "秒数": None,
        }
        for line in block.splitlines():
            line = line.strip()
            if re.search(r"場所[：:]", line):
                cut["場所"] = re.sub(r".*場所[：:]", "", line).strip()
            elif re.search(r"キャラ[：:]", line):
                cut["キャラ"] = re.sub(r".*キャラ[：:]", "", line).strip()
            elif re.search(r"衣装[：:]", line):
                cut["衣装"] = re.sub(r".*衣装[：:]", "", line).strip()
            elif re.search(r"内容[：:]", line):
                cut["内容"] = re.sub(r".*内容[：:]", "", line).strip()
            elif re.search(r"カメラ[：:]", line):
                cut["カメラ"] = re.sub(r".*カメラ[：:]", "", line).strip()
            elif re.search(r"秒数[：:]", line):
                val = re.sub(r".*秒数[：:]", "", line).strip()
                try:
                    cut["秒数"] = float(val)
                except ValueError:
                    pass
        if cut["場所"] or cut["内容"]:
            cuts.append(cut)
    return cuts


# ---------------------------------------------------------------------------
# 画像ユーティリティ
# ---------------------------------------------------------------------------

def encode_image(path: Path) -> str:
    img = Image.open(path).convert("RGB")
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def has_character(cut: dict) -> bool:
    c = cut.get("キャラ", "")
    return bool(c) and c not in ("なし", "ナレーション", "テキスト")


def is_closeup(cut: dict) -> bool:
    return "アップ" in cut.get("キャラ", "") or "クローズアップ" in cut.get("カメラ", "")


def pick_char_refs(cut: dict) -> list[Path]:
    """キャラクター参照画像リストを返す（私服＋制服の2枚）
    顔・髪型・体型の一貫性のために両方参照するが、衣装はプロンプトで制御。
    """
    refs = []
    for key in ("default", "制服"):
        p = CHAR_REFS.get(key)
        if p and p.exists():
            refs.append(p)
    return refs


# ---------------------------------------------------------------------------
# プロンプト構築
# ---------------------------------------------------------------------------

def build_prompt(cut: dict) -> str:
    場所 = cut.get("場所", "")
    キャラ = cut.get("キャラ", "")
    衣装 = cut.get("衣装", "")
    内容 = cut.get("内容", "")
    カメラ = cut.get("カメラ", "")

    lines = []

    lines.append(
        "縦型（9:16）アニメイラスト。映画的で美しい作画。"
        "やわらかい線・落ち着いたトーン・スタジオジブリや新海誠作品に近い質感。"
    )

    if 場所:
        lines.append(f"場所: {場所}。")

    if has_character(cut):
        lines.append(f"登場キャラクター: {CHAR_DESCRIPTION}")
        if 衣装:
            lines.append(f"衣装: {衣装}。企業ロゴ・ブランドマーク・文字は一切入れない。")
        if is_closeup(cut):
            lines.append("バストアップ〜顔のクローズアップ。背景は浅いボケ。")
        else:
            lines.append("全身または上半身が見えるショット。")
        lines.append("参照画像のキャラクターの顔・髪型・体型を忠実に再現すること。")
    else:
        lines.append("人物なし。場所・物だけのショット。")

    if 内容:
        lines.append(f"シーン内容: {内容}")

    if カメラ:
        lines.append(f"カメラ: {カメラ}")

    # 室内カットは屋外・雪の混入を明示的に禁止
    if is_indoor(cut):
        lines.append("室内シーンです。屋外・雪・雪原・吹雪・白い地面は一切描かないこと。窓の外も室内の照明のみ。")

    lines.append(
        "テキスト・字幕・吹き出し・ロゴ・数字・記号・ブランドマーク・企業名は画像に一切入れないこと。"
        "アイレベルの自然な視点。真上からの俯瞰は禁止。"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gemini API 呼び出し
# ---------------------------------------------------------------------------

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
                    print(f"    content なし（finishReason: {reason}）... 再試行")
                    time.sleep(5)
                    continue
                for part in candidate["content"]["parts"]:
                    if "inlineData" in part:
                        return base64.b64decode(part["inlineData"]["data"])
                raise ValueError("レスポンスに画像データがありません。")

            elif resp.status_code in (429, 503):
                wait = 20 + attempt * 5
                print(f"    混雑中（{resp.status_code}）... {wait}秒待機 [{attempt}/{max_retries}]")
                time.sleep(wait)
            else:
                raise RuntimeError(f"APIエラー {resp.status_code}: {data}")

        except requests.RequestException as e:
            print(f"    通信エラー: {e} ... 20秒後リトライ [{attempt}/{max_retries}]")
            time.sleep(20)

    raise RuntimeError(f"{max_retries}回リトライしても失敗しました。")


# ---------------------------------------------------------------------------
# 1カット生成
# ---------------------------------------------------------------------------

INDOOR_KEYWORDS = ["個室", "食堂", "研究室", "観測棟", "室内", "屋内", "食堂", "廊下", "基地内"]

def is_indoor(cut: dict) -> bool:
    場所 = cut.get("場所", "")
    return any(k in 場所 for k in INDOOR_KEYWORDS)


def generate_cut(cut: dict, out_path: Path, anchor_path: Path | None = None) -> None:
    parts = []

    # スタイルアンカー（室内カットは屋外雪原が混入するためスキップ）
    if anchor_path and anchor_path.exists() and not is_indoor(cut):
        parts.append({
            "inlineData": {
                "mimeType": "image/png",
                "data": encode_image(anchor_path),
            }
        })
        parts.append({"text": "【スタイル参照】上の画像のアニメ絵柄・色調・線の質感を維持してください。"})

    # キャラクター参照画像
    if has_character(cut):
        char_refs = pick_char_refs(cut)
        for ref in char_refs:
            parts.append({
                "inlineData": {
                    "mimeType": "image/png",
                    "data": encode_image(ref),
                }
            })
        if char_refs:
            parts.append({"text": (
                "【キャラクター参照】上の参照画像から顔・髪型・体型のみを忠実に再現してください。"
                "参照画像の衣装・服装は完全に無視すること。衣装はプロンプトの指示に従うこと。"
            )})

    # メインプロンプト
    prompt = build_prompt(cut)
    parts.append({"text": prompt})

    img_bytes = call_gemini(parts)
    out_path.write_bytes(img_bytes)
    print(f"    → 保存: {out_path.name}")

    # 生成ログに記録
    from datetime import datetime
    with open(GEN_LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {out_path.parent.name} | {out_path.name}\n")


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="転職チャンネル用画像一括生成")
    parser.add_argument("--script", default=None, help="台本名（tenshi_scripts/ 内のファイル名、拡張子なし）")
    parser.add_argument("--resume", action="store_true", help="生成済みをスキップして続きから")
    parser.add_argument("--cut", type=int, default=None, help="特定カット番号だけ再生成")
    args = parser.parse_args()

    # 台本ファイルを解決
    if args.script:
        script_path = SCRIPTS_DIR / f"{args.script}.txt"
    else:
        # デフォルト: tenshi_scripts/ 内の最初のファイル
        scripts = sorted(SCRIPTS_DIR.glob("*.txt"))
        if not scripts:
            sys.exit("ERROR: tenshi_scripts/ に台本ファイルがありません。")
        script_path = scripts[0]

    if not script_path.exists():
        sys.exit(f"ERROR: 台本ファイルが見つかりません: {script_path}")

    folder_name = script_path.stem

    # ========== 事前リサーチ必須チェック ==========
    # --resume や --cut での再生成はスキップ
    if not args.resume and args.cut is None:
        research_path = RESEARCH_DIR / f"{folder_name}.md"
        if not research_path.exists():
            print("=" * 60)
            print("  ERROR: 事前リサーチが完了していません")
            print("=" * 60)
            print()
            print("  画像生成の前に必ず以下の2点をWeb検索で確認し、")
            print("  tenshi_prep.py でリサーチファイルを作成してください。")
            print()
            print("  【確認必須項目】")
            print("  1. 事実確認（年収・給与・福利厚生などの数字）")
            print("  2. 制服・作業着・職場環境の実物確認")
            print()
            print(f"  実行コマンド例:")
            print(f"  python tenshi_prep.py --script {folder_name} \\")
            print(f'      --facts "年収〇〇万円（確認済み）" \\')
            print(f'      --uniform "白い衛生白衣（確認済み）" \\')
            print(f'      --workplace "工場の雰囲気（確認済み）"')
            print()
            sys.exit(1)

    print(f"台本: {script_path.name}")
    out_dir = OUTPUTS_ROOT / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)

    cuts = parse_script(script_path)
    if not cuts:
        sys.exit("ERROR: 台本にカットが見つかりませんでした。")

    print(f"カット数: {len(cuts)}")
    print(f"出力先: {out_dir}\n")

    # キャラクター参照画像の確認
    default_ref = CHAR_REFS.get("default")
    if default_ref and not default_ref.exists():
        print(f"⚠️  キャラクター参照画像が見つかりません: {default_ref}")
        print(f"   assets/characters/tenshi/ に田中悠斗_私服.png を置いてください。")
        print(f"   参照画像なしで続行します...\n")

    anchor_path = None

    for idx, cut in enumerate(cuts):
        cut_num = idx + 1
        out_path = out_dir / f"cut_{cut_num:03d}.png"

        # --cut 指定時は該当カットのみ
        if args.cut is not None and cut_num != args.cut:
            continue

        # --resume 時はすでにある画像をスキップ
        if args.resume and out_path.exists():
            print(f"[{cut_num}/{len(cuts)}] スキップ（生成済み）: {out_path.name}")
            anchor_path = out_path
            continue

        print(f"[{cut_num}/{len(cuts)}] 生成中...")
        print(f"    場所: {cut['場所']}  キャラ: {cut['キャラ']}  衣装: {cut['衣装']}")
        print(f"    内容: {cut['内容'][:40]}...")

        try:
            generate_cut(cut, out_path, anchor_path)
            # 1カット目の出力をスタイルアンカーに設定
            if anchor_path is None:
                anchor_path = out_path
        except Exception as e:
            print(f"    ERROR: {e}")
            print(f"    このカットをスキップして続行します。")

        time.sleep(2)  # API レート制限対策

    print(f"\n完了！ {out_dir} に画像が保存されました。")

    # Google Drive 自動アップロード
    try:
        from drive_upload import upload_folder
        upload_folder(out_dir, folder_name)
    except Exception as e:
        print(f"⚠  Google Driveアップロードをスキップしました: {e}")


def show_log():
    """生成ログを集計して表示"""
    if not GEN_LOG.exists():
        print("ログがまだありません。")
        return
    lines = GEN_LOG.read_text(encoding="utf-8").strip().splitlines()
    total = len(lines)
    print(f"\n累計生成枚数: {total}枚")
    print(f"残高目安: ¥1,750 ÷ {total}枚 = 約¥{1750 // total if total else 0}/枚" if total else "")
    print("\n--- 最新10件 ---")
    for line in lines[-10:]:
        print(" ", line)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--log":
        show_log()
    else:
        main()
