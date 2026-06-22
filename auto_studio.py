"""
auto_studio.py — 台本から全カット画像を一括生成

script.txt を読み込み、Gemini API で各カットの画像を生成して
outputs/cut_001.png, cut_002.png ... と保存する。

使い方:
    python auto_studio.py              # 全カット生成
    python auto_studio.py --resume     # 生成済みをスキップして続きから
    python auto_studio.py --cut 3      # カット3だけ再生成
"""

import argparse
import base64
import json
import os
import re
import shutil
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

BASE    = Path(__file__).parent
ASSETS  = BASE / "assets"
OUTPUTS = BASE / "outputs"
OUTPUTS.mkdir(exist_ok=True)

FACE_DIR = ASSETS / "characters" / "face"
BODY_DIR = ASSETS / "characters" / "body"

_BG = ASSETS / "characters" / "backgrounds"
BACKGROUNDS: dict[str, Path | list[Path]] = {
    "自宅":           _BG / "home_master" / "全体像.png",
    "自宅キッチン":   _BG / "home_master" / "全体像.png",  # キッチン単体画像は他の自宅画像と不整合のため、検証済みの全体像を使用
    "自宅ダイニング": _BG / "home_master" / "ダイニング_向き合い構図.png",
    "自宅リビング":   _BG / "home_master" / "テレビ.png",
    "レジ":           _BG / "スーパー" / "スーパー" / "レジ.png",
    "休憩室": [
        _BG / "スーパー" / "休憩室1.png",
        _BG / "スーパー" / "休憩室2.png",
        _BG / "スーパー" / "休憩室3.png",
    ],
}

# スーパー系ロケーション（幸子→幸子パートに自動切り替え）
SUPER_LOCATIONS = {"レジ", "休憩室"}

# クローズアップ時のキャラ別背後背景（座席ごとに異なる壁を見せる）
# 未登録キャラはBACKGROUNDSの全リストにフォールバック
_S = _BG / "スーパー"
_HOME = _BG / "home_master"
SEAT_BG: dict[str, dict[str, list[Path]]] = {
    "休憩室": {
        "幸子":       [],                                            # 西側・シンプルな壁 → 背景参照なし（AIが自然な壁を生成）
        "幸子パート": [],                                            # 同上
        "中島":       [_S / "休憩室2.png"],                         # 北側・掲示板ドア
        "田中":       [_S / "休憩室1.png", _S / "休憩室3.png"],     # 南側・ロッカー
    },
    "自宅ダイニング": {
        # 幸子の定位置（正夫と向き合う側の席）。1人のシーン（書類・計算用紙等）でも同じ席を使う
        "幸子": [_HOME / "ダイニング_向き合い構図.png"],
    },
}

KNOWN_CHARS = sorted(
    ["幸子パート", "幸子", "夫", "娘", "中島", "田中", "久美", "敦子", "美智代", "美穂"],
    key=len, reverse=True,
)

# 室内空間の構成説明（背景の空間整合性を保つための固定テキスト）
HOME_LAYOUT = (
    "【室内LDKの空間構成 — 絶対厳守】\n"
    "以下のレイアウトを完全に守ること。位置関係を逆にしたり、家具を移動させてはならない。\n"
    "・キッチン：部屋の奥（北側）に配置。白いL字型キャビネットとアイランドカウンター。シンクあり。\n"
    "・ダイニングテーブル：木製長方形テーブル（4人掛け）。キッチンとリビングの中間、部屋の中央に配置。\n"
    "・ソファ：ベージュ〜薄茶色。リビング南側（手前側）に配置。テレビと向き合っている。\n"
    "・テレビ：壁面（ソファの正面の壁）に設置。ソファとテレビは必ず正面で向き合う。絶対に隣り合わない。\n"
    "・窓：南側と東側。サーモンピンクのカーテン。\n"
    "・床：明るい木目調フローリング。リビング中央にラグ。\n"
    "・観葉植物：部屋の角や窓際に数か所。\n"
    "・出入口：南側（手前）にドア。\n"
)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.1-flash-image-preview:generateContent?key={key}"
)

# ロケーション別スタイルアンカー
# キーはBACKGROUNDSのキー（前方一致）と対応
LOCATION_ANCHORS: dict[str, Path] = {
    "自宅":   OUTPUTS / "cut_001.png",
    "休憩室": OUTPUTS / "cut_021.png",
    "レジ":   OUTPUTS / "cut_001.png",  # レジ用は初回生成後に更新
}
ANCHOR_PATH = OUTPUTS / "cut_001.png"  # フォールバック用


def _anchor_for(cut: dict) -> Path:
    loc = cut.get("場所") or ""
    for prefix, path in LOCATION_ANCHORS.items():
        if loc.startswith(prefix):
            return path
    return ANCHOR_PATH


# ---------------------------------------------------------------------------
# 台本パーサ
# ---------------------------------------------------------------------------

def parse_script(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"\n{2,}", text.strip())
    cuts = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        cut: dict = {"場所": None, "キャラ": [], "カメラ": "", "内容": "", "秒数": None, "再利用": None, "プロップ": None, "シーン参照": None}
        for line in block.splitlines():
            line = line.strip()
            if re.search(r"場所[：:]", line):
                cut["場所"] = re.sub(r".*場所[：:]", "", line).strip().strip("【】")
            elif re.search(r"キャラ[：:]", line):
                raw = re.sub(r".*キャラ[：:]", "", line).strip()
                seen: set[str] = set()
                for name in KNOWN_CHARS:
                    if name in raw and name not in seen:
                        idx   = raw.index(name)
                        after = raw[idx + len(name):]
                        is_face = bool(re.match(r"\s*[（(]アップ[）)]", after))
                        cut["キャラ"].append({"name": name, "face": is_face})
                        seen.add(name)
            elif re.search(r"カメラ[：:]", line):
                cut["カメラ"] = re.sub(r".*カメラ[：:]", "", line).strip()
            elif re.search(r"内容[：:]", line):
                cut["内容"] = re.sub(r".*内容[：:]", "", line).strip()
            elif re.search(r"秒数[：:]", line):
                val = re.sub(r".*秒数[：:]", "", line).strip()
                try:
                    cut["秒数"] = float(val)
                except ValueError:
                    pass
            elif re.search(r"再利用[：:]", line):
                cut["再利用"] = re.sub(r".*再利用[：:]", "", line).strip()
            elif re.search(r"プロップ[：:]", line):
                cut["プロップ"] = re.sub(r".*プロップ[：:]", "", line).strip()
            elif re.search(r"シーン参照[：:]", line):
                cut["シーン参照"] = re.sub(r".*シーン参照[：:]", "", line).strip()
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


def find_char(name: str, use_face: bool) -> Path | None:
    p = (FACE_DIR if use_face else BODY_DIR) / f"{name}.png"
    return p if p.exists() else None


def resolve_char(name: str, use_face: bool, location: str) -> Path | None:
    """幸子の顔参照戦略:
    - アップ(use_face=True): face/幸子.png（最高品質の顔を切り出した参照画像）
    - 全身・スーパー系(use_face=False): body/幸子パート.png（制服・体型の基準）
    - 全身・自宅/カフェ系(use_face=False): body/幸子.png（私服・体型の基準）
    衣装はプロンプトのCOSTUME指示で制御する。
    """
    if name in ("幸子", "幸子パート"):
        if use_face:
            return find_char("幸子", True)   # face/幸子.png（高品質顔）
        else:
            if location in SUPER_LOCATIONS:
                return find_char("幸子パート", False)  # body/幸子パート.png（制服）
            else:
                return find_char("幸子", False)  # body/幸子.png（私服）
    return find_char(name, use_face)


# ---------------------------------------------------------------------------
# Gemini API 呼び出し（リトライ付き）
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
                # safety filter 等で content がない場合は再試行
                if "content" not in candidate:
                    reason = candidate.get("finishReason", "unknown")
                    print(f"    content なし（finishReason: {reason}）... 再試行")
                    time.sleep(5)
                    continue
                for part in candidate["content"]["parts"]:
                    if "inlineData" in part:
                        return base64.b64decode(part["inlineData"]["data"])
                raise ValueError("レスポンスに画像データがありませんでした。")

            elif resp.status_code in (429, 503):
                wait = 20 + attempt * 5
                print(f"    混雑中（{resp.status_code}）... {wait}秒待機してリトライ [{attempt}/{max_retries}]")
                time.sleep(wait)

            else:
                raise RuntimeError(f"APIエラー {resp.status_code}: {data}")

        except requests.RequestException as e:
            print(f"    通信エラー: {e} ... 20秒後にリトライ [{attempt}/{max_retries}]")
            time.sleep(20)

    raise RuntimeError(f"{max_retries}回リトライしても失敗しました。")


# ---------------------------------------------------------------------------
# カット1枚を生成
# ---------------------------------------------------------------------------

def _is_closeup(cut: dict) -> bool:
    """視点が空間と根本的に相容れないショット（俯瞰・POV）のみTrueを返す。
    バストアップ・クローズアップは背景参照を送りつつボケ指示で対応するためFalse。
    """
    camera = cut.get("カメラ", "")
    skip_keywords = ["俯瞰", "視点ショット", "POV", "見下ろす", "覗き込む"]
    return any(k in camera for k in skip_keywords)


def _bg_ref_instruction(cut: dict) -> str:
    """ショット距離に応じた背景参照画像への指示文を返す。
    カメラキーワードを優先し、face_charはフォールバック用。
    """
    camera = cut.get("カメラ", "")
    face_char = any(c["face"] for c in cut["キャラ"])

    if "クローズアップ" in camera:
        return (
            "【背景固定: 照明・色調のみ参照】"
            "クローズアップショットです。この空間の照明色・壁の色調だけを参照し、"
            "背景は完全にボケ処理（被写界深度を最大限浅く）。背景の形状・家具は映さない。"
        )
    if "横顔" in camera:
        return (
            "【背景固定: 照明・色調のみ参照】"
            "横顔アップショットです。横顔の奥にこの空間の色調・光が浅いボケで広がります。"
            "背景はほんのり見える程度にボカしてください。壁の色・照明の色調を一致させること。"
            "参照画像に写っている家具・カーテン等の色（例：カーテンはピンク系）も同じ色で見せること。"
            "参照画像に存在しない新しい家具・棚・物を絶対に追加で描かないこと。"
        )
    if "バストアップ" in camera or face_char:
        return (
            "【背景固定: バストアップ背景として使用】"
            "バストアップショットです。キャラクターの背後に、この空間の壁・照明が"
            "浅いボケで見えます。壁の色・照明の色調・空間の質感を参照画像と完全に一致させること。"
            "背景の色・照明を変更することは禁止。"
            "参照画像に写っている家具・カーテン等の色（例：カーテンはピンク系）はそのままの色で背後に見せること。"
            "参照画像に存在しない新しい棚・家具・物を絶対に追加で描かないこと（背景に写っていないものは存在しない）。"
        )
    if "手元" in camera:
        return (
            "【背景固定: 手元背景として使用】"
            "手元クローズアップショットです。手元の奥にテーブル・カウンター面が見えます。"
            "背景はボケ処理。この空間の照明色・質感を合わせてください。"
        )
    # ミディアム・全体ショット — 最強の固定指示
    return (
        "【背景固定 — 再デザイン完全禁止】"
        "この画像が今シーンの完成した実際の背景です。"
        "家具・壁・床・照明・色調・配置を一切変更してはならない。"
        "あなたの仕事はこの背景の中にキャラクターを自然に配置することだけ。"
        "背景を再描画・再デザインすることは絶対禁止。"
        "アイレベル（目線の高さ）の自然な視点を維持すること。"
    )


def _bg_draw_instruction(cut: dict, is_outdoor: bool) -> str:
    """メインプロンプト内の背景描写指示（ショット距離に応じたボケ量を指定）。"""
    camera = cut.get("カメラ", "")
    face_char = any(c["face"] for c in cut["キャラ"])

    if is_outdoor:
        return "背景は屋外の自然な街並み・空・建物。アイレベルの自然な視点で描くこと。"
    if "クローズアップ" in camera:
        return (
            "背景は完全にボケ処理（被写界深度を最大限浅く）。"
            "空間の照明色・壁の色調だけが背景に滲む。家具・形状は見えない。"
        )
    if "横顔" in camera:
        return "横顔の奥に空間の色調・光が浅いボケで広がる。背景はほんのり見える程度。"
    if "バストアップ" in camera or face_char:
        return (
            "背景の壁・空間が浅いボケで見える。照明・壁の色・質感は参照画像と完全統一。"
            "輪郭はぼんやり分かる程度のボケ量。背景の色調・照明を変えないこと。"
        )
    if "手元" in camera:
        return "手元の奥にテーブル面が見える。背景はボケ処理。照明色を空間と統一。"
    # ミディアム・全体ショット
    return (
        "背景参照画像の家具・壁・照明・色調を100%再現すること。"
        "背景を再デザインしない。アイレベルの自然な視点で描くこと。真上からの俯瞰・鳥瞰構図は絶対禁止。"
    )


def _is_outdoor(cut: dict) -> bool:
    camera = cut.get("カメラ", "")
    content = cut.get("内容", "")
    keywords = ["屋外", "外観", "街", "帰り道", "スーパーの前", "通り過ぎ", "外の景色"]
    return any(k in camera + content for k in keywords)


def generate_cut(cut: dict, cut_no: int, auto_scene_ref: Path | None = None) -> Path:
    out_path = OUTPUTS / f"cut_{cut_no:03d}.png"

    is_closeup = _is_closeup(cut)
    is_outdoor = _is_outdoor(cut)

    location = cut.get("場所") or ""

    bg_val = BACKGROUNDS.get(location) if location else None
    if isinstance(bg_val, list):
        bg_paths = [p for p in bg_val if p.exists()]
    else:
        bg_paths = [bg_val] if (bg_val is not None and bg_val.exists()) else []

    # クローズアップ系ショットはキャラ別の背後背景に切り替え（対面会話のリバースショット対応）
    camera = cut.get("カメラ", "")
    face_char = any(c["face"] for c in cut["キャラ"])
    is_close_shot = face_char or any(k in camera for k in ["バストアップ", "クローズアップ", "横顔"])
    if is_close_shot and not is_closeup:
        seat_map = SEAT_BG.get(location, {})
        for c in cut["キャラ"]:
            resolved_name = "幸子パート" if (c["name"] == "幸子" and location in SUPER_LOCATIONS) else c["name"]
            seat_paths = [p for p in seat_map.get(resolved_name, []) if p.exists()]
            if resolved_name in seat_map:
                bg_paths = seat_paths  # 空リストの場合も上書き（背景参照なし）
                break  # 最初にマッチしたキャラの背景を使用

    bg_exists = len(bg_paths) > 0

    # 屋外・POV・俯瞰以外は全ショットに背景参照を送る（ボケ量は指示で制御）
    use_bg_ref = bg_exists and not is_closeup and not is_outdoor
    char_paths = [p for c in cut["キャラ"] if (p := resolve_char(c["name"], c["face"], location))]

    parts: list[dict] = []

    # バストアップ以上のクローズアップショットかどうか（手元もここに含める。
    # 含めないと「部屋全体をpixel-perfectに再現せよ」という広角向けの指示と
    # 「クローズアップにせよ」という指示が矛盾し、寄った画にならない）
    is_face_shot = face_char or any(
        k in camera for k in ["バストアップ", "クローズアップ", "アップ", "横顔", "手元"]
    )

    # --- 背景参照を最優先で最初に配置（モデルへの最初のインプットが最も重視される）---
    # 顔アップ・バストアップ等でも背景参照は必ず送る（ボケ量はinstructionで制御）。
    # 詳細な家具レイアウト指示(HOME_LAYOUT)はワイドショットのみ。顔ショットは_bg_ref_instructionの
    # ボケ対応版を使う（こちらは元々バストアップ/クローズアップ/横顔の分岐を持っている）。
    if use_bg_ref:
        is_home = (cut.get("場所") or "").startswith("自宅")
        for i, bp in enumerate(bg_paths):
            if i == 0:
                if is_home and not is_face_shot:
                    label = (
                        "【背景固定 — 再デザイン完全禁止】"
                        "この画像が今シーンの完成した実際の部屋です。"
                        "家具・壁・床・照明・色調・配置を一切変更してはならない。"
                        "キャラクターをこの部屋の中に自然に配置することだけが仕事。\n"
                        + HOME_LAYOUT
                    )
                else:
                    label = _bg_ref_instruction(cut)
            else:
                label = "【同じ空間の別角度参照】椅子・扉・家具の色と形を1枚目の背景と完全に統一してください。"
            parts.append({"text": label})
            parts.append({"inlineData": {"mimeType": "image/png", "data": encode_image(bp)}})

    # --- スタイルアンカー（ワイド・全体ショットのみ。クローズアップでは顔をキャラ参照に集中させる）---
    anchor = _anchor_for(cut)
    anchor_active = (
        anchor.exists()
        and not is_face_shot                # ← 顔ショットではアンカー無効
        and not (cut_no == 1 and anchor == ANCHOR_PATH)
        and not (cut_no == 21 and (cut.get("場所") or "").startswith("休憩室"))
    )
    if anchor_active:
        parts.append({"text": (
            "【スタイル統一の参照フレーム】"
            "以下の1枚目が今作品の絵柄・配色・画風の基準です。"
            "キャラクターの顔・髪・衣装の色調を必ずこれに統一してください。"
        )})
        parts.append({"inlineData": {"mimeType": "image/png", "data": encode_image(anchor)}})

    # --- キャラクター参照 ---
    is_home_scene = (cut.get("場所") or "").startswith("自宅")
    for cp in char_paths:
        is_sachiko_ref = cp.stem in ("幸子", "幸子パート")
        if is_sachiko_ref:
            label = (
                "CHARACTER REFERENCE — MAIN PROTAGONIST SACHIKO (SACRED): "
                "This is the EXACT face you MUST reproduce. "
                "Her facial features, eye shape, eye color, nose, lip shape, skin tone, "
                "hair color (dark brown), and bob-cut hairstyle must match this reference with 100% fidelity. "
                "Do NOT simplify, idealize, or alter her face in any way. "
                "She is 65 years old — keep her natural age features (slight wrinkles, mature expression). "
                "Any deviation from this face is a generation failure."
            )
        else:
            label = f"CHARACTER REFERENCE ({cp.stem}): Reproduce this character's design faithfully."
        parts.append({"text": label})
        parts.append({"inlineData": {"mimeType": "image/png", "data": encode_image(cp)}})

        # 幸子の顔アップ（自宅/カフェ系）に私服の衣装参照を追加
        if cp.stem == "幸子" and is_home_scene:
            outfit_ref = find_char("幸子", False)  # body/幸子.png（私服）
            if outfit_ref and outfit_ref.exists():
                parts.append({"text": (
                    "SACHIKO OUTFIT REFERENCE (mandatory, CLOTHING ONLY — IGNORE THE FACE IN THIS IMAGE): "
                    "This image is provided ONLY to show her exact home/casual clothing "
                    "(the floral blouse — same color, same pattern, same fit). "
                    "Do NOT change the outfit design. "
                    "IMPORTANT: this reference image's face is NOT authoritative. "
                    "The face must continue to follow ONLY the first reference image "
                    "labeled 'MAIN PROTAGONIST SACHIKO (SACRED)'. "
                    "Disregard any facial differences between this image and that one."
                )})
                parts.append({"inlineData": {"mimeType": "image/png", "data": encode_image(outfit_ref)}})

    # --- プロップ参照（家計簿・計算用紙など小道具） ---
    prop_path_str = cut.get("プロップ")
    if prop_path_str:
        prop_path = BASE / prop_path_str
        if prop_path.exists():
            parts.append({"text": (
                "PROP REFERENCE (mandatory): This image shows the exact prop that appears in this scene. "
                "Reproduce the style, texture, and overall appearance of this object faithfully. "
                "For any handwritten text or numbers visible on the prop, match the handwriting style exactly. "
                "The prop must look like it belongs in the same anime art style as the characters."
            )})
            parts.append({"inlineData": {"mimeType": "image/png", "data": encode_image(prop_path)}})

    # --- シーン参照（空間継続性の維持）---
    # 優先順位: 手動指定(script.txtの「シーン参照：」) > 自動チェーン(同場所の直前生成カット)
    scene_ref_str = cut.get("シーン参照")
    scene_ref_path: Path | None = None
    if scene_ref_str:
        p = BASE / scene_ref_str
        if p.exists():
            scene_ref_path = p
    elif auto_scene_ref and auto_scene_ref.exists():
        scene_ref_path = auto_scene_ref

    if scene_ref_path:
        parts.append({"text": (
            "SCENE CONTINUITY REFERENCE (CRITICAL — TOP PRIORITY): "
            "This is the immediately preceding shot from the EXACT SAME scene/location. "
            "All spatial elements are already established: table shape and surface, chair design, "
            "props on the table (cups, books, papers, etc.), window position, wall color, "
            "lighting direction and warmth, and character positions. "
            "You are shooting a DIFFERENT ANGLE or CLOSER FRAMING of this same physical space. "
            "Any prop visible in this reference image that could appear in the new framing MUST appear. "
            "The spatial layout is LOCKED — do not alter or remove any element."
        )})
        parts.append({"inlineData": {"mimeType": "image/png", "data": encode_image(scene_ref_path)}})

    # --- メインプロンプト ---
    camera = cut.get("カメラ") or (
        "ウエストアップ（腰から上）。キャラクターの表情・動作にカメラを近づけること。"
    )

    bg_instruction = _bg_draw_instruction(cut, is_outdoor)

    # 衣装フラグ
    sachiko_chars = [c for c in cut["キャラ"] if c["name"] in ("幸子", "幸子パート")]
    sachiko_in_work = bool(sachiko_chars) and location in SUPER_LOCATIONS

    parts.append({"text": (
        # --- Quality header (English first — model responds strongest here) ---
        "Masterpiece high-end anime frame. Makoto Shinkai / Studio Ghibli style. "
        "Emotional cinematic lighting. Ultra detailed 2D digital art. 8K quality. "
        "STRICT 16:9 HORIZONTAL ASPECT RATIO.\n\n"

        # --- Absolute prohibitions ---
        "ABSOLUTE PROHIBITION (top priority): "
        "No text, subtitles, speech bubbles, logos, numbers, or symbols anywhere in the image. "
        "Violation = generation failure.\n\n"

        # --- Costume (conditional) ---
        + (
            "COSTUME (mandatory): Sachiko wears the EXACT SAME floral blouse shown in her outfit reference "
            "image — a cream/beige base with small muted lavender-gray flowers. "
            "Do NOT substitute a different floral pattern, a different base color, or a different flower color. "
            "ABSOLUTELY NO blue, navy, or dark-colored floral pattern — this is a recurring mistake, avoid it. "
            "At home, her outfit is ALWAYS this exact cream/beige floral. Non-negotiable.\n\n"
            if (is_home_scene and sachiko_chars) else
            "COSTUME (mandatory): Sachiko is at her WORKPLACE (supermarket). "
            "She wears a WHITE BLOUSE with a BLUE APRON tied over it — her standard work uniform. "
            "Absolutely NO floral outfit, NO home clothes. "
            "Match the character reference image exactly.\n\n"
            if sachiko_in_work else ""
        ) +

        # --- Camera ---
        "CAMERA & FRAMING: "
        f"{camera} "
        "Character must fill the frame as the clear subject. "
        "Wide establishing shots, long shots, and full-room bird's-eye views are FORBIDDEN. "
        "Always bring the camera close to the character's face and hands.\n\n"

        # --- Background lock (wide shots: pixel-perfect / face shots: bokeh-matched) ---
        + (
            (
                "BACKGROUND LOCK (CRITICAL — HIGHEST PRIORITY): "
                "The background reference image IS the actual, final background for this scene. "
                "DO NOT redraw, redesign, or alter any background elements whatsoever. "
                "Furniture positions, wall colors, floor, lighting, and room layout must be pixel-perfect matches. "
                "Your ONLY task is to place the character(s) naturally into this existing scene.\n\n"
                if not is_face_shot else
                "BACKGROUND LOCK (CRITICAL — HIGHEST PRIORITY): "
                "The background reference image shows the actual room for this scene. "
                "Even though it will be heavily blurred behind the character, the wall color, "
                "lighting tone, and any visible furniture silhouette MUST match this reference exactly. "
                "DO NOT invent a different room, different wall color, or different furniture.\n\n"
            )
            if use_bg_ref else ""
        ) +

        # --- Background ---
        "BACKGROUND: "
        f"{bg_instruction}\n\n"

        # --- Scene ---
        "SCENE CONTENT: "
        f"{cut['内容']}\n\n"

        # --- Final style lock ---
        "Prioritize character emotion and hand gestures. Soft depth-of-field on backgrounds. "
        "No doll-house overhead perspective. Production-ready animation frame."
    )})

    img_bytes = call_gemini(parts)
    # 1280x720（CapCut互換の標準HD）にリサイズして保存
    from io import BytesIO
    img = Image.open(BytesIO(img_bytes))
    if img.size != (1280, 720):
        img = img.resize((1280, 720), Image.LANCZOS)
    img.save(out_path)
    return out_path


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="台本から全カット画像を一括生成")
    parser.add_argument("--resume", action="store_true", help="生成済みのカットをスキップ")
    parser.add_argument("--cut", type=int, default=None, help="指定カットのみ再生成（例: --cut 3）")
    parser.add_argument("--cuts", type=int, nargs="+", default=None, help="複数カット再生成（例: --cuts 3 5 10）")
    parser.add_argument("--episode", type=str, default=None, help="Google Driveに保存する話数（例: --episode 第5話）")
    args = parser.parse_args()

    script_path = BASE / "script.txt"
    if not script_path.exists():
        print("script.txt が見つかりません。以下の形式で作成してください:\n")
        print("【カット1】")
        print("場所：自宅")
        print("キャラ：幸子（アップ）")
        print("内容：幸子が帰宅してため息をつく")
        print("秒数：6\n")
        return

    cuts = parse_script(script_path)
    total = len(cuts)
    print(f"台本読み込み完了: {total} カット\n")

    if args.cuts:
        targets = args.cuts
    elif args.cut:
        targets = [args.cut]
    else:
        targets = list(range(1, total + 1))

    # 場所ごとの直前カット追跡（自動シーン参照チェーン）
    # 既存ファイルで先に埋めておく（--resume / --cuts のとき前カットを参照できるよう）
    location_last_cut: dict[str, Path] = {}
    for j in range(1, total + 1):
        loc_j = cuts[j - 1].get("場所") or ""
        path_j = OUTPUTS / f"cut_{j:03d}.png"
        if loc_j and path_j.exists():
            location_last_cut[loc_j] = path_j

    success, skip, fail = 0, 0, 0

    for i in targets:
        if i < 1 or i > total:
            print(f"カット {i} は範囲外です（1〜{total}）")
            continue

        cut = cuts[i - 1]
        location = cut.get("場所") or ""
        out_path = OUTPUTS / f"cut_{i:03d}.png"

        if args.resume and out_path.exists():
            print(f"[{i}/{total}] スキップ（生成済み）: {out_path.name}")
            skip += 1
            continue

        # 再利用指定がある場合はAPIを呼ばずにコピー
        if cut.get("再利用"):
            src = BASE / cut["再利用"]
            if src.exists():
                shutil.copy(src, out_path)
                if location:
                    location_last_cut[location] = out_path
                print(f"[{i}/{total}] ♻️  再利用: {src.name} → {out_path.name}")
                skip += 1
                continue
            else:
                print(f"[{i}/{total}] ⚠️  再利用ファイルが見つかりません: {cut['再利用']} → 新規生成します")

        shot_info = []
        for c in cut["キャラ"]:
            shot_info.append(f"{c['name']}({'アップ' if c['face'] else '全身'})")
        auto_ref = location_last_cut.get(location) if location else None
        ref_label = f" [自動参照: {auto_ref.name}]" if auto_ref else ""
        print(f"[{i}/{total}] 場所:{cut['場所']} | キャラ:{' '.join(shot_info) or 'なし'}{ref_label}")
        print(f"         内容: {cut['内容']}")

        try:
            path = generate_cut(cut, i, auto_scene_ref=auto_ref)
            if location:
                location_last_cut[location] = path
            print(f"         ✓ 保存: {path.name}\n")
            success += 1
            # レート制限対策：連続生成時に少し待機
            if targets[-1] != i:
                time.sleep(8)
        except Exception as e:
            print(f"         ✗ エラー: {e}\n")
            fail += 1

    print(f"完了 — 生成:{success}枚 / スキップ:{skip}枚 / 失敗:{fail}枚")
    print("次は  python video_assembler.py  で動画に変換してください。")

    # Google Drive 自動アップロード
    if args.episode:
        try:
            from drive_upload import upload_sachiko
            upload_sachiko(args.episode)
        except Exception as e:
            print(f"⚠  Google Driveアップロードをスキップしました: {e}")


if __name__ == "__main__":
    main()
