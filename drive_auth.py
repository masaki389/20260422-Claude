"""
drive_auth.py — Google Drive OAuth認証（初回のみ実行）
Codespaces対応版: ブラウザ不要の手動フロー
"""

import json
import sys
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from dotenv import load_dotenv

load_dotenv()

BASE          = Path(__file__).parent
CLIENT_SECRET = BASE / "client_secret.json"
TOKEN_FILE    = BASE / "token_drive.json"

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
]
REDIRECT_URI = "http://localhost:8080/"


def main():
    print("🔑 Google Drive 認証を開始します\n")

    if not CLIENT_SECRET.exists():
        sys.exit("ERROR: client_secret.json が見つかりません。")

    raw = json.loads(CLIENT_SECRET.read_text())
    info = raw.get("installed") or raw.get("web")
    client_id     = info["client_id"]
    client_secret = info["client_secret"]
    token_uri     = info.get("token_uri", "https://oauth2.googleapis.com/token")

    # 既存トークンの確認
    if TOKEN_FILE.exists():
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
                print("✓ トークンを自動更新しました。")
                return
            elif creds.valid:
                print("✓ 既に認証済みです（token_drive.json が有効です）")
                return
        except Exception as e:
            print(f"⚠ token_drive.json が無効です（{e}）→ 再認証します\n")
            TOKEN_FILE.unlink(missing_ok=True)

    params = {
        "client_id":     client_id,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         " ".join(SCOPES),
        "access_type":   "offline",
        "prompt":        "consent",
    }
    auth_url = "https://accounts.google.com/o/oauth2/auth?" + urlencode(params)

    print("=" * 60)
    print("【手順】")
    print()
    print("① 以下のURLをブラウザにコピーして開いてください：")
    print()
    print(auth_url)
    print()
    print("② Googleアカウントでログイン → 許可")
    print()
    print("③「このサイトにアクセスできません」と出たらOK")
    print("  ブラウザのアドレスバーのURLを全部コピー")
    print("  （http://localhost:8080/?code=... のようなURL）")
    print("=" * 60)
    print()

    redirected = input("④ コピーしたURLをここに貼り付けてEnter: ").strip()

    parsed = urlparse(redirected)
    params_got = parse_qs(parsed.query)
    if "code" not in params_got:
        sys.exit("URLにcodeが含まれていません。手順をやり直してください。")

    code = params_got["code"][0]

    resp = requests.post(token_uri, data={
        "code":          code,
        "client_id":     client_id,
        "client_secret": client_secret,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    })

    if resp.status_code != 200:
        sys.exit(f"トークン取得エラー: {resp.text}")

    token_data = resp.json()

    from google.oauth2.credentials import Credentials
    creds = Credentials(
        token=token_data.get("access_token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    print()
    print("✓ 認証完了！token_drive.json を保存しました。")
    print("  次回からの画像生成時に自動でGoogle Driveにアップロードされます。")


if __name__ == "__main__":
    main()
