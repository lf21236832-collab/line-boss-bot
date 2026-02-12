import os
import json
import re
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError

# =========================
# 基本設定
# =========================

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
TZ = os.getenv("TZ", "Asia/Taipei")

app = Flask(__name__)
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

DATA_FILE = "boss_data.json"

REMIND_BEFORE_MIN = 5
WARNING_BEFORE_MIN = 30
EXPIRE_GRACE_MIN = 3
CHECK_INTERVAL_SEC = 20

# =========================
# Boss 表
# =========================

BOSS_TABLE = [
    ("巨大鱷魚", 60, ["鱷魚"]),
    ("單飛龍", 180, ["單龍"]),
    ("雙飛龍", 180, ["雙龍"]),
    ("黑長者", 240, ["黑老"]),
    ("克特", 360, []),
    ("四色", 180, []),
    ("魔法師", 180, []),
    ("死亡騎士", 360, ["死騎"]),
    ("巴風特", 240, ["小巴"]),
    ("巴列斯", 240, ["大巴"]),
    ("巨蟻女皇", 360, ["螞蟻"]),
    ("變形怪首領", 300, ["變怪"]),
    ("伊佛利特", 180, ["EF"]),
    ("不死鳥", 360, ["鳥"]),
    ("冰之女王", 360, ["冰女"]),
    ("惡魔", 360, []),
    ("古代巨人", 360, ["古巨"]),
    ("反王肯恩", 240, []),
    ("賽尼斯", 240, []),
    ("巨大牛人", 360, ["牛"]),
    ("潔尼斯女王", 360, ["2樓"]),
    ("幻象眼魔", 360, ["3樓"]),
    ("吸血鬼", 360, ["4樓"]),
    ("殭屍王", 360, ["5樓"]),
    ("黑豹", 360, ["6樓"]),
    ("木乃伊王", 360, ["7樓"]),
    ("艾莉絲", 360, ["8樓"]),
    ("騎士范德", 360, ["9樓"]),
    ("巫妖", 360, ["10樓"]),
    ("土精靈王", 120, []),
    ("水精靈王", 120, []),
    ("風精靈王", 120, []),
    ("火精靈王", 120, []),
    ("獨角獸", 360, []),
    ("曼波兔(海賊島)", 360, []),
    ("庫曼", 360, []),
    ("德雷克", 180, []),
    ("曼波兔(精靈墓穴)", 360, []),
    ("深淵之主", 360, []),
    ("須曼", 360, []),
    ("安塔瑞斯", 720, []),
    ("巴拉卡斯", 720, []),
    ("法利昂", 720, []),
    ("林德拜爾", 720, [])
]

BOSS_RESPAWN = {name: mins for name, mins, _ in BOSS_TABLE}
BOSS_ALIAS = {}

for name, _, alias_list in BOSS_TABLE:
    for a in alias_list:
        BOSS_ALIAS[a] = name

# =========================
# 資料處理
# =========================

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"boss": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def now():
    return datetime.now()

def parse_time_str(t):
    return datetime.strptime(t, "%H%M").time()

def format_left(dt):
    diff = dt - now()
    if diff.total_seconds() < 0:
        return "已過期"
    h = diff.seconds // 3600
    m = (diff.seconds % 3600) // 60
    return f"{h}h{m}m"

def find_boss(text):
    for name in BOSS_RESPAWN:
        if name in text:
            return name
    for alias, real in BOSS_ALIAS.items():
        if alias in text:
            return real
    return None

# =========================
# 背景檢查
# =========================

def check_loop():
    while True:
        try:
            data = load_data()
            boss_data = data["boss"]
            changed = False

            for boss, info in list(boss_data.items()):
                respawn = datetime.fromisoformat(info["respawn"])
                left = (respawn - now()).total_seconds()

                # 5分鐘提醒
                if 0 < left <= REMIND_BEFORE_MIN*60:
                    if not info.get("reminded"):
                        msg = f"⏰ {boss} 即將重生！剩 {format_left(respawn)}"
                        line_bot_api.broadcast(TextSendMessage(text=msg))
                        info["reminded"] = True
                        changed = True

                # 30分鐘反紅提示
                if left <= WARNING_BEFORE_MIN*60 and not info.get("warned"):
                    info["warned"] = True
                    changed = True

                # 超過自動清除
                if left <= -EXPIRE_GRACE_MIN*60:
                    del boss_data[boss]
                    changed = True

            if changed:
                save_data(data)

        except:
            pass

        time.sleep(CHECK_INTERVAL_SEC)

threading.Thread(target=check_loop, daemon=True).start()

# =========================
# LINE Webhook
# =========================

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    data = load_data()
    boss_data = data["boss"]

    # 王出
    if text == "王出":
        msg = "📜 已登記王表：\n"
        for boss, info in boss_data.items():
            respawn = datetime.fromisoformat(info["respawn"])
            left = format_left(respawn)
            msg += f"{boss} → {respawn.strftime('%H:%M')} ({left})\n"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # 查詢
    if text == "查詢":
        msg = "✨可用指令✨\n"
        msg += "王出 / 王 / Boss1430 / Boss1400出\n"
        msg += "Boss清除 / 王表清除\n"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # 王
    if text == "王":
        names = [b[0] for b in BOSS_TABLE]
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(text="\n".join(names)))
        return

    # 設定時間
    match = re.match(r"(.+?)(\d{4})(出?)$", text)
    if match:
        boss_text, time_str, is_spawn = match.groups()
        boss = find_boss(boss_text)
        if not boss:
            return

        t = parse_time_str(time_str)
        today = now().replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)

        if is_spawn:
            respawn = today
        else:
            respawn = today + timedelta(minutes=BOSS_RESPAWN[boss])

        boss_data[boss] = {
            "respawn": respawn.isoformat(),
            "reminded": False,
            "warned": False
        }

        save_data(data)

        msg = f"✅ {boss} 下次重生 {respawn.strftime('%H:%M')}"
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(text=msg))
        return

    # 清除單隻
    if text.endswith("清除") and text != "王表清除":
        boss = find_boss(text.replace("清除",""))
        if boss and boss in boss_data:
            del boss_data[boss]
            save_data(data)
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage(text=f"🗑 已清除 {boss}"))
        return

    # 清空
    if text == "王表清除":
        boss_data.clear()
        save_data(data)
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(text="⚠ 王表已清空"))
        return

# =========================

if __name__ == "__main__":
    app.run()
