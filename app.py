import os
import re
import json
import threading
from datetime import datetime, timedelta

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")

app = Flask(__name__)
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# Boss重生時間（小時）
BOSS_TIMES = {
    "小巴": 4,
    "大巴": 4,
    "四色": 3,
    "單龍": 6,
    "雙龍": 6,
    "黑老": 4,
    "克特": 6,
    "變怪": 6,
    "反王": 6,
    "螞蟻": 6,
    "死騎": 6,
    "土": 2,
    "風": 2,
    "火": 2,
    "水": 2,
    "獨角獸": 6,
    "EF": 3,
    "不死鳥": 6,
    "蜘蛛": 6,
    "吸血鬼": 6,
    "殭屍王": 6,
    "艾莉絲": 6,
    "牛": 6,
    "惡魔": 6
}

boss_data = {}
DATA_FILE = "boss_data.json"


def load_data():
    global boss_data
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            boss_data = json.load(f)


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(boss_data, f, ensure_ascii=False)


def format_time(dt):
    return dt.strftime("%H:%M")


def schedule_reminder(group_id, boss_name, respawn_time):
    def remind():
        now = datetime.now()
        wait_seconds = (respawn_time - timedelta(minutes=5) - now).total_seconds()
        if wait_seconds > 0:
            threading.Timer(wait_seconds, send_reminder).start()

    def send_reminder():
        line_bot_api.push_message(
            group_id,
            TextSendMessage(text=f"⚠️ {boss_name} 即將在 5 分鐘後重生！")
        )

    remind()


@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except Exception:
        abort(400)

    return 'OK'


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    group_id = event.source.group_id if event.source.type == "group" else event.source.user_id

    if text == "王出":
        if not boss_data:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="目前沒有任何Boss時間")
            )
            return

        msg = "📜 Boss 重生時間：\n"
        for boss, time_str in boss_data.items():
            msg += f"{boss} ➜ {time_str}\n"

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=msg)
        )
        return

    match = re.match(r"(.+?)\s*(\d{4})出?$", text)
    if match:
        boss_name = match.group(1)
        time_str = match.group(2)

        if boss_name not in BOSS_TIMES:
            return

        hour = int(time_str[:2])
        minute = int(time_str[2:])
        now = datetime.now()
        spawn_time = now.replace(hour=hour, minute=minute, second=0)

        if spawn_time < now:
            spawn_time += timedelta(days=1)

        respawn_time = spawn_time + timedelta(hours=BOSS_TIMES[boss_name])
        boss_data[boss_name] = format_time(respawn_time)
        save_data()

        schedule_reminder(group_id, boss_name, respawn_time)

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"✅ {boss_name} 下次重生時間 {format_time(respawn_time)}")
        )


if __name__ == "__main__":
    load_data()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
