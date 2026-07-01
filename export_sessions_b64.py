"""
Railway等のenv変数にセッションファイルをbase64で登録するための変換スクリプト。

実行:
  python export_sessions_b64.py

出力をRailwayのVariablesにコピペする:
  SESSION_X_B64    = xxxxxxxx...
  SESSION_NOTE_B64 = xxxxxxxx...
"""
import base64
from pathlib import Path

BASE = Path(__file__).parent / "automation" / "sessions"

for name, env_key in [("session_x.json", "SESSION_X_B64"), ("session_note.json", "SESSION_NOTE_B64")]:
    path = BASE / name
    if path.exists():
        b64 = base64.b64encode(path.read_bytes()).decode()
        print(f"\n{env_key}={b64[:60]}...  (total {len(b64)} chars)")
        print(f"  → Railwayのenv変数名: {env_key}")
    else:
        print(f"\n{env_key}: {path} が見つかりません")
