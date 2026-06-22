"""
幸子LINEBot起動スクリプト
このファイルを実行するだけでBotが起動します
"""
import subprocess
import sys
import time
import os

# サーバーをバックグラウンドで起動
print("🤖 幸子Bot起動中...")
server = subprocess.Popen([sys.executable, "sachiko_line_bot.py"])
time.sleep(2)

# ngrokでトンネルを作成
from pyngrok import ngrok

# ngrokトークンがあれば設定（なくてもOK）
ngrok_token = os.getenv('NGROK_AUTH_TOKEN', '')
if ngrok_token:
    ngrok.set_auth_token(ngrok_token)

tunnel = ngrok.connect(5000)
public_url = tunnel.public_url.replace("http://", "https://")

print("\n" + "="*50)
print("✅ 幸子Bot起動完了！")
print("="*50)
print(f"\n📋 LINE WebhookURLに貼るURL：")
print(f"\n  {public_url}/callback\n")
print("="*50)
print("\n⚠️  このウィンドウを閉じるとBotが止まります")
print("   Ctrl+C で終了\n")

try:
    server.wait()
except KeyboardInterrupt:
    print("\n停止しました")
    server.terminate()
    ngrok.kill()
