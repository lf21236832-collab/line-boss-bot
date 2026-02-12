from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage

from datetime import datetime, timedelta
import os
import json
import threading
import time

app = Flask(__name__)

# 在 Render / 雲端用環境變數放這兩個
CHANNEL_ACCESS_TOKEN = os.getenv("etnRCWtR9qy3BL2fkzoG4FpCjD6f5o3bFsKi89eAoKctRaFfFMxrhRqsag3gp0qk25C+7AuIPZO4X1ADwcCG80tkhnqm0eQYcINVcSC6NG62JCeh3HE/GVQHSacouO8wpve0YW3R/CobUX4QafNRyAdB04t89/1O/w1cDnyilFU=", "")
CHANNEL_SECRET = os.getenv("b9555c1aa96e47227f890fbfa5bd0122", "")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# Boss 重生時間（分鐘）
BOSS_RESPAWN_MIN = {
    "小巴": 4 * 60,
    "大巴": 4 * 60,
    "四色": 3 * 60,
    "單龍": 6 * 60,
    "雙龍": 6 * 60,
    "黑老": 4 * 60,
    "克特": 6 * 60,
    "變怪": 6 * 60,
    "反王": 6 * 60,
    "螞蟻": 6 * 60,
    "死騎": 6 * 60,
    "土": 2 * 60,
    "風": 2 * 60,
    "火": 2 * 60,
    "水": 2 * 60,
    "獨角獸": 6 * 60,
    "EF": 3 * 60,
    "不死鳥": 6 * 60,
    "蜘蛛": 6 * 60,
    "吸血鬼": 6 * 60,
    "殭屍王": 6 * 60,
    "艾莉絲": 6 * 60,
    "牛": 6 * 60,
    "惡魔": 6 * 60,
}

DATA_FILE = "boss_data.json"
REMIND_BEFORE_MIN = 5          # 重生前幾分鐘提醒
CHECK_INTERVAL_SEC = 20        # 掃描間隔秒數


# -----------------------
# 資料存取
# -----------------------
def load_data():
    if not os.path.exists(DATA_FILE):
        return {"boss": {}, "targets": [], "notified": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("boss", {})
        data.setdefault("targets", [])
        data.setdefault("notified", {})
        return data
    except:
        return {"boss": {}, "targets": [], "notified": {}}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# -----------------------
# 文字/時間工具
# -----------------------
def normalize_text(s: str) -> str:
    # 全形冒號/空白統一
    return s.strip().replace("　", " ").replace("：", ":")


def parse_time_token(token: str):
    """
    支援：
      1430
      14:30
      1430出 / 14:30出  -> is_respawn=True
    回傳 (hour, minute, is_respawn)
    """
    token = token.strip()
    is_respawn = False

    if token.endswith("出"):
        is_respawn = True
        token = token[:-1].strip()

    token = token.replace("：", ":")

    if len(token) == 4 and token.isdigit():
        hh = int(token[:2])
        mm = int(token[2:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm, is_respawn

    if ":" in token:
        try:
            t = datetime.strptime(token, "%H:%M")
            return t.hour, t.minute, is_respawn
        except:
            pass

    raise ValueError("bad time token")


def remain_text(target_dt: datetime) -> str:
    sec = (target_dt - datetime.now()).total_seconds()
    if sec <= 0:
        sec = abs(sec)
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        return f"已過 {h}h{m}m"
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    return f"剩餘 {h}h{m}m"


def compute_from_death(boss: str, death_dt: datetime) -> datetime:
    return death_dt + timedelta(minutes=BOSS_RESPAWN_MIN[boss])


def compute_from_respawn(boss: str, respawn_dt: datetime) -> datetime:
    # 反推死亡時間
    return respawn_dt - timedelta(minutes=BOSS_RESPAWN_MIN[boss])


def extract_command(text: str):
    """
    解析以下輸入：
      小巴 1430
      小巴1430
      小巴 1400出
      小巴1400出
      小巴 14:30
      小巴14:30出
    回傳 (boss, time_token) 或 (None, None)
    """
    text = normalize_text(text)

    # 先處理有空格
    parts = [p for p in text.split(" ") if p]
    if len(parts) == 2:
        return parts[0], parts[1]

    # 沒空格：用 boss 名稱做前綴匹配
    for boss in sorted(BOSS_RESPAWN_MIN.keys(), key=len, reverse=True):
        if text.startswith(boss):
            token = text[len(boss):].strip()
            if token:
                return boss, token

    return None, None


# -----------------------
# 背景提醒（重生前 5 分鐘）
# -----------------------
def reminder_loop():
    while True:
        try:
            data = load_data()
            boss_data = data.get("boss", {})
            targets = data.get("targets", [])
            notified = data.get("notified", {})

            if boss_data and targets:
                now = datetime.now()

                for boss, rec in boss_data.items():
                    if boss not in BOSS_RESPAWN_MIN:
                        continue
                    death_iso = rec.get("death")
                    if not death_iso:
                        continue

                    try:
                        death_dt = datetime.fromisoformat(death_iso)
                    except:
                        continue

                    respawn_dt = compute_from_death(boss, death_dt)
                    remind_at = respawn_dt - timedelta(minutes=REMIND_BEFORE_MIN)

                    # 在提醒時間到重生之間提醒一次
                    if remind_at <= now <= respawn_dt:
                        key = respawn_dt.isoformat()
                        if notified.get(boss) != key:
                            msg = (
                                f"⏰【{boss}】{REMIND_BEFORE_MIN}分鐘後重生\n"
                                f"重生：{respawn_dt.strftime('%H:%M')}\n"
                                f"{remain_text(respawn_dt)}"
                            )

                            for tid in targets:
                                try:
                                    line_bot_api.push_message(tid, TextSendMessage(text=msg))
                                except:
                                    pass

                            notified[boss] = key
                            data["notified"] = notified
                            save_data(data)

        except:
            pass

        time.sleep(CHECK_INTERVAL_SEC)


def start_reminder_thread_once():
    t = threading.Thread(target=reminder_loop, daemon=True)
    t.start()


# -----------------------
# Webhook
# -----------------------
@app.route("/", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except:
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = normalize_text(event.message.text)

    data = load_data()

    # 記住推播對象（群組 + 私聊）
    src = event.source
    gid = getattr(src, "group_id", None)
    uid = getattr(src, "user_id", None)

    if gid and gid not in data["targets"]:
        data["targets"].append(gid)
    if uid and uid not in data["targets"]:
        data["targets"].append(uid)

    save_data(data)

    # 指令：列表
    if text in ("列表", "boss", "BOSS"):
        names = "、".join(BOSS_RESPAWN_MIN.keys())
        reply = (
            f"可用Boss：\n{names}\n\n"
            "用法：\n"
            "1) 死亡時間：小巴 1430 / 小巴1430\n"
            "2) 指定重生：小巴 1400出 / 小巴1400出\n"
            "3) 全部重生：王出\n"
            "4) 查單隻：查 小巴"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 指令：王出
    if text == "王出":
        lines = ["【王出】"]
        rows = []
        for boss in BOSS_RESPAWN_MIN.keys():
            rec = data["boss"].get(boss)
            if rec and rec.get("death"):
                try:
                    death_dt = datetime.fromisoformat(rec["death"])
                    respawn_dt = compute_from_death(boss, death_dt)
                    rows.append((respawn_dt, f"{boss}：{respawn_dt.strftime('%H:%M')}（{remain_text(respawn_dt)}）"))
                except:
                    rows.append((datetime.max, f"{boss}：未紀錄"))
            else:
                rows.append((datetime.max, f"{boss}：未紀錄"))

        # 有紀錄的排前面（按重生時間）
        rows.sort(key=lambda x: x[0])

        for _, s in rows:
            lines.append(s)

        reply = "\n".join(lines)
        # 太長就切兩段（避免 LINE 限制）
        if len(reply) > 3500:
            first = "\n".join(lines[:60])
            second = "\n".join(["（續）"] + lines[60:])
            line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=first), TextSendMessage(text=second)])
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 指令：查 <boss>
    parts = [p for p in text.split(" ") if p]
    if len(parts) == 2 and parts[0] in ("查", "查詢"):
        boss = parts[1]
        if boss not in BOSS_RESPAWN_MIN:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="Boss錯誤，輸入「列表」看可用Boss"))
            return
        rec = data["boss"].get(boss)
        if not rec or not rec.get("death"):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"「{boss}」尚未紀錄\n用法：{boss} 1430 或 {boss} 1400出"))
            return

        death_dt = datetime.fromisoformat(rec["death"])
        respawn_dt = compute_from_death(boss, death_dt)
        msg = (
            f"【{boss}】\n"
            f"死亡：{death_dt.strftime('%H:%M')}\n"
            f"重生：{respawn_dt.strftime('%H:%M')}\n"
            f"{remain_text(respawn_dt)}"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # 一般：Boss 時間 / Boss時間 / Boss 1400出
    boss, token = extract_command(text)
    if boss and token:
        if boss not in BOSS_RESPAWN_MIN:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="Boss錯誤，輸入「列表」看可用Boss"))
            return

        try:
            hh, mm, is_respawn = parse_time_token(token)
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="時間格式錯誤\n例：小巴 1430 或 小巴 1400出"))
            return

        now = datetime.now()
        input_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

        if is_respawn:
            # 你指定「重生時間」
            respawn_dt = input_dt
            death_dt = compute_from_respawn(boss, respawn_dt)
        else:
            # 你輸入「死亡時間」
            death_dt = input_dt
            respawn_dt = compute_from_death(boss, death_dt)

        # 存檔：都以「死亡時間」為準（因為提醒是依死亡+週期算）