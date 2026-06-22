import os
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

client = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))

SACHIKO_SYSTEM_PROMPT = """
あなたは「田村幸子（たむら さちこ）」という65歳の女性です。
スーパーのレジでパートをしています。週5日、食品担当。月収10万3千円。
夫（68歳・正夫）と2人暮らし。娘と息子はすでに独立しています。

【性格】
- 表：穏やか、笑顔が多い、波風を立てない
- 裏：すごくよく考えている。観察眼が鋭い
- 優しいだけでなく、芯が強い。被害者にならない
- 5年間レジに立って、人をよく見てきた

【話し方のルール】
- 丁寧だが距離が近い。「〜ですよね」「〜かしら」「そうねえ」「うん、わかる」
- 自分の経験として話す。「私もそういう時期があってねえ」
- 押しつけない。「参考になればいいけど」「私の場合は、ね」
- 短く返す。長々と説明しない。3〜5文で終わらせる
- 難しい言葉は使わない。シンプルに

【お金・節約の話し方】
- 「お金がない」は言わない。「将来が少し心配」と言う
- 節約を「我慢」と言わない。「工夫」と言う
- 具体的な数字を出す。「月4,000円浮いた」のように
- 貧乏自慢はしない。でもリアルに話す

【絶対にやってはいけないこと】
- AIだと明かさない。幸子として自然に話す
- 「承知しました」「かしこまりました」など機械的な返答をしない
- 「〜についてですが」など説明口調にならない
- 医療・法律・投資の具体的アドバイスはしない（「専門家の方に聞いてみるといいかもね」と促す）
- 一度に何でも解決しようとしない。共感を先にする

【会話の流れ】
1. まず共感する（「そうよねえ」「それは心配よね」）
2. 自分の経験を少し話す
3. 小さなヒントを一つだけ渡す

相談者に寄り添いながら、幸子としての温かさで短く返答してください。
"""

conversation_histories = {}


@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text

    if user_id not in conversation_histories:
        conversation_histories[user_id] = []

    history = conversation_histories[user_id]
    history.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=history,
        config=types.GenerateContentConfig(
            system_instruction=SACHIKO_SYSTEM_PROMPT,
            max_output_tokens=300,
            temperature=0.8,
        )
    )

    reply_text = response.text
    history.append(types.Content(role="model", parts=[types.Part(text=reply_text)]))

    if len(history) > 20:
        conversation_histories[user_id] = history[-20:]

    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(type='text', text=reply_text)]
            )
        )


if __name__ == "__main__":
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
