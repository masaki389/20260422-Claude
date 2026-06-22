"""
tenshi_prep.py — 新動画の事前リサーチ必須チェックスクリプト

【使い方】
    python tenshi_prep.py --script TOP0_山崎製パン_285K

このスクリプトを実行してリサーチファイルを作成しないと
tenshi_studio.py は起動できません。

【チェック項目】
    1. 事実確認（年収・給与・福利厚生などの数字）
    2. 制服・作業着の確認（Web検索で実物を確認）
    3. 職場環境の確認（工場・オフィス・現場の雰囲気）
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

BASE        = Path(__file__).parent
SCRIPTS_DIR = BASE / "tenshi_scripts"
RESEARCH_DIR = BASE / "research"


TEMPLATE = """\
# {script_name} — 事前リサーチ記録
作成日時: {datetime}
ステータス: DONE

## 1. 事実確認（数字・データ）
<!-- Web検索で確認した数字・情報を記載 -->

{facts}

## 2. 制服・作業着
<!-- Web検索で確認した実際の制服の特徴を記載 -->

{uniform}

## 3. 職場環境
<!-- 工場・現場・オフィスの実際の雰囲気を記載 -->

{workplace}

## 4. 確認済みWebソース
<!-- 参照したURLを記載 -->

{sources}
"""


def main():
    parser = argparse.ArgumentParser(description="新動画の事前リサーチ登録")
    parser.add_argument("--script", required=True, help="台本名（tenshi_scripts/内のファイル名、拡張子なし）")
    parser.add_argument("--facts",     default="（要記入）", help="確認した事実・数字")
    parser.add_argument("--uniform",   default="（要記入）", help="確認した制服・作業着")
    parser.add_argument("--workplace", default="（要記入）", help="確認した職場環境")
    parser.add_argument("--sources",   default="（要記入）", help="参照したWebソース")
    args = parser.parse_args()

    script_path = SCRIPTS_DIR / f"{args.script}.txt"
    if not script_path.exists():
        sys.exit(f"ERROR: 台本ファイルが見つかりません: {script_path}")

    research_path = RESEARCH_DIR / f"{args.script}.md"

    content = TEMPLATE.format(
        script_name=args.script,
        datetime=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        facts=args.facts,
        uniform=args.uniform,
        workplace=args.workplace,
        sources=args.sources,
    )

    RESEARCH_DIR.mkdir(exist_ok=True)
    research_path.write_text(content, encoding="utf-8")

    print(f"✅ リサーチファイルを作成しました: {research_path}")
    print(f"   → python tenshi_studio.py --script {args.script} が実行可能になりました")


if __name__ == "__main__":
    main()
