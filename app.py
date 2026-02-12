import os
import json
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET", "")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# =========================
# 你的最新 Boss 表（正式名稱 -> 重生分鐘）
# =========================
BOSS_RESPAWN_MIN = {
    "巨大鱷魚": 60,
    "單飛龍": 180,
    "雙飛龍": 180,
    "黑長者": 240,
    "克特": 360,
    "四色": 180,
    "魔法師": 180,
    "死亡騎士": 360,
    "巴風特": 240,
    "巴列斯": 240,
    "巨蟻女皇": 360,
    "變形怪首領": 300,
    "伊佛利特": 180,
    "不死鳥": 360,
    "冰之女王": 360,
    "惡魔": 360,
    "古代巨人": 360,
    "反王肯恩": 240,
    "賽尼斯": 240,
    "巨大牛人": 360,
    "潔尼斯女王": 360,      # (2樓)
    "幻象眼魔": 360,        # (3樓)
    "吸血鬼": 360,          # (4樓)
    "殭屍王": 360,          # (5樓)
    "黑豹": 360,            # (6樓)
    "木乃伊王": 360,        # (7樓)
    "艾莉絲": 360,          # (8樓)
    "騎士范德": 360,        # (9樓)
    "巫妖": 360,            # (10樓)
    "土精靈王": 120,
    "水精靈王": 120,
    "風精靈王": 120,
    "火精靈王": 120,
    "獨角獸": 360,
    "曼波兔(海賊島)": 360,
    "庫曼": 360,
    "德雷克": 180,
    "曼波兔(精靈墓穴)": 360,
    "深淵之主": 360,
    "須曼": 360,
    "安塔瑞斯": 720,
    "巴拉卡斯": 720,
    "法利昂": 720,
    "林德拜爾": 720,
}

# =========================
# 別名：可輸入，但「王/王出」不顯示別名
# =========================
ALIASES_TO_CANON = {
    "黑老": "黑長者",
    "小巴": "巴風特",
    "大巴": "巴列斯",
    "螞蟻": "巨蟻女皇",
    "EF": "伊佛利特",
    "2樓": "潔尼斯女王",
    "3樓": "幻象眼魔",
    "4樓": "吸血鬼",
    "5樓": "殭屍王",
    "6樓": "黑豹",
    "7樓": "木乃伊王",
    "8樓": "艾莉絲",
    "9樓": "騎士范德",
    "10樓": "巫妖",
}

DATA_FILE = "boss_data.json"
REMIND_BEFORE_MIN = 5
CHECK_INTERVAL_SEC = 20
CLEAR_CONFIRM_TTL_SEC = 60  # 王表清除 -> 確認清除 有效秒數


# =========================
# 基本工具
# =========================
def normalize_text(s: str) -> str:
    return s.strip().replace("　", " ").replace("：", ":")


def load_data():
    """
    data schema:
      {
        "boss": {
          "不死鳥": {"respawn": "ISO", "last_notified": "ISO or ''"},
          ...
        },
        "targets": ["groupId/userId", ...],
        "pending_clear": {"targetId": epoch_deadline_int}
      }
    """
    if not os.path.exists(DATA_FILE):
        return {"boss": {}, "targets": [], "pending_clear": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("boss", {})
        data.setdefault("targets", [])
        data.setdefault("pending_clear", {})
        return data
    except:
        return {"boss": {}, "targets": [], "pending_clear": {}}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parse_time_token(token: str):
    """
    支援：
      1430
      14:30
    回傳 (hh, mm)
    """
    token = token.strip().replace("：", ":")
    if len(token) == 4 and token.isdigit():
        hh = int(token[:2])
        mm = int(token[2:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    if ":" in token:
        t = datetime.strptime(token, "%H:%M")
        return t.hour, t.minute
    raise ValueError("bad time")


def next_occurrence_today_or_tomorrow(hh: int, mm: int) -> datetime:
    """把 HH:MM 放到今天；若已過就放到明天同一時間"""
    now = datetime.now()
    dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if dt <= now:
        dt += timedelta(days=1)
    return dt


def roll_respawn_to_future(boss: str, respawn_dt: datetime) -> datetime:
    """若已過，按週期往後滾到未來"""
    interval = timedelta(minutes=BOSS_RESPAWN_MIN[boss])
    now = datetime.now()
    while respawn_dt <= now:
        respawn_dt += interval
    return respawn_dt


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


# =========================
# Boss 名稱解析（含別名 + 模糊匹配）
# =========================
def all_match_pairs():
    # (name_for_match, canonical)
    pairs = [(canon, canon) for canon in BOSS_RESPAWN_MIN.keys()]
    pairs += [(alias, canon) for alias, canon in ALIASES_TO_CANON.items()]
    return pairs


def resolve_boss(query: str):
    """
    回傳：
      (canonical, None)         唯一匹配
      (None, [canon...])        多個候選
      (None, [])                找不到
    """
    q = normalize_text(query)
    if not q:
        return None, []

    # 完全相等：正式名
    if q in BOSS_RESPAWN_MIN:
        return q, None

    # 完全相等：別名
    if q in ALIASES_TO_CANON:
        return ALIASES_TO_CANON[q], None

    # 模糊：包含關鍵字（在正式名或別名中）
    candidates = []
    seen = set()
    for name, canon in all_match_pairs():
        if q in name and canon not in seen:
            seen.add(canon)
            candidates.append(canon)

    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        candidates.sort(key=lambda x: (len(x), x))
        return None, candidates

    return None, []


def format_candidates(cands):
    show = cands[:12]
    lines = ["我找到多個可能："]
    for i, c in enumerate(show, 1):
        lines.append(f"{i}) {c}")
    if len(cands) > 12:
        lines.append(f"...還有 {len(cands) - 12} 個")
    lines.append("請再多打 1~2 個字，或直接打完整名稱。")
    return "\n".join(lines)


# =========================
# 指令解析
# =========================
def extract_boss_clear(text: str):
    """
    支援：
      鳥清除 / 鳥 清除
      小巴清除 / 小巴 清除
    回傳 boss_query 或 None
    """
    text = normalize_text(text)
    parts = [p for p in text.split(" ") if p]

    if len(parts) == 2 and parts[1] == "清除":
        return parts[0]

    if text.endswith("清除"):
        return text[:-2].strip()

    return None


def extract_boss_and_time(text: str):
    """
    支援（Boss 可模糊/別名）：
      鳥1430
      鳥 1430
      鳥1400出
      鳥 1400出
      鳥14:30出

    回傳 (boss_query, time_token, is_respawn_input)
    """
    text = normalize_text(text)

    # 有空格：Boss + token
    parts = [p for p in text.split(" ") if p]
    if len(parts) == 2:
        boss_q = parts[0]
        token = parts[1]
        is_respawn = token.endswith("出")
        if is_respawn:
            token = token[:-1].strip()
        return boss_q, token, is_respawn

    # 無空格：嘗試從尾端抓時間 + 可選出
    # 先處理是否有「出」
    has_out = text.endswith("出")
    core = text[:-1].strip() if has_out else text

    # 4碼時間
    if len(core) >= 4 and core[-4:].isdigit():
        boss_q = core[:-4].strip()
        token = core[-4:]
        if boss_q:
            return boss_q, token, has_out

    # HH:MM / H:MM（尾端 5 或 4 字）
    if ":" in core:
        tail5 = core[-5:]
        tail4 = core[-4:]
        if len(tail5) == 5 and tail5[2] == ":" and tail5[:2].isdigit() and tail5[3:].isdigit():
            boss_q = core[:-5].strip()
            if boss_q:
                return boss_q, tail5, has_out
        if len(tail4) == 4 and tail4[1] == ":" and tail4[0].isdigit() and tail4[2:].isdigit():
            boss_q = core[:-4].strip()
            if boss_q:
                return boss_q, tail4, has_out

    return None, None, False


def cmd_help_text():
    return (
        "【可用指令】\n"
        "1) 王：列出所有Boss名稱（只顯示正式名）\n"
        "2) 王出：顯示所有Boss下一次重生時間\n"
        "3) 死亡時間：Boss1430 / Boss 1430\n"
        "4) 指定重生：Boss1400出 / Boss 1400出（重生就是14:00，不往後推）\n"
        "5) 清空全部：王表清除（需再輸入「確認清除」）\n"
        "6) 清除單隻：Boss清除 / Boss 清除\n"
        "7) 查詢：顯示本訊息\n"
        "（Boss 支援模糊/別名：例如「鳥」可找不死鳥；命中多個會請你縮小）"
    )


# =========================
# Web routes
# =========================
@app.route("/", methods=["GET"])
def home():
    return "Bot is running!", 200


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200


# =========================
# Reminder thread
# =========================
def reminder_loop():
    while True:
        try:
            data = load_data()
            targets = data.get("targets", [])
            boss_data = data.get("boss", {})

            if targets and boss_data:
                now = datetime.now()
                changed = False

                for boss, rec in list(boss_data.items()):
                    if boss not in BOSS_RESPAWN_MIN:
                        continue

                    respawn_iso = rec.get("respawn")
                    if not respawn_iso:
                        continue

                    try:
                        respawn_dt = datetime.fromisoformat(respawn_iso)
                    except:
                        continue

                    # 保持為「下一次」重生時間
                    new_respawn = roll_respawn_to_future(boss, respawn_dt)
                    if new_respawn != respawn_dt:
                        boss_data[boss]["respawn"] = new_respawn.isoformat()
                        boss_data[boss]["last_notified"] = ""
                        changed = True
                        respawn_dt = new_respawn

                    remind_at = respawn_dt - timedelta(minutes=REMIND_BEFORE_MIN)
                    if remind_at <= now <= respawn_dt:
                        key = respawn_dt.isoformat()
                        if boss_data[boss].get("last_notified", "") != key:
                            msg = (
                                f"⏰{REMIND_BEFORE_MIN}分鐘後重生\n"
                                f"重生：{respawn_dt.strftime('%H:%M')}\n"
                                f"{remain_text(respawn_dt)}"
                            )
                            for tid in targets:
                                try:
                                    line_bot_api.push_message(tid, TextSendMessage(text=msg))
                                except:
                                    pass
                            boss_data[boss]["last_notified"] = key
                            changed = True

                if changed:
                    data["boss"] = boss_data
                    save_data(data)

        except:
            pass

        time.sleep(CHECK_INTERVAL_SEC)


def start_reminder_thread():
    t = threading.Thread(target=reminder_loop, daemon=True)
    t.start()


# =========================
# Message handler
# =========================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = normalize_text(event.message.text)
    data = load_data()

    # 這次訊息來自哪個 target（群組優先）
    src = event.source
    target_id = getattr(src, "group_id", None) or getattr(src, "user_id", None)

    # 記住推播目標
    if target_id and target_id not in data["targets"]:
        data["targets"].append(target_id)
        save_data(data)

    # ---- 查詢：顯示指令 ----
    if text == "查詢":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=cmd_help_text()))
        return

    # ---- 王：列正式Boss名稱（不顯示別名）----
    if text == "王":
        names = "、".join(BOSS_RESPAWN_MIN.keys())
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"【Boss名稱（正式）】\n{names}"))
        return

    # ---- 王表清除：第一段 ----
    if text == "王表清除":
        if not target_id:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="無法取得群組/使用者資訊，請再試一次。"))
            return
        deadline = int(time.time()) + CLEAR_CONFIRM_TTL_SEC
        data["pending_clear"][target_id] = deadline
        save_data(data)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"⚠️ 防誤刪：請在 {CLEAR_CONFIRM_TTL_SEC} 秒內輸入「確認清除」才會清空全部紀錄。")
        )
        return

    # ---- 確認清除：第二段 ----
    if text == "確認清除":
        if not target_id:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="無法取得群組/使用者資訊，請再試一次。"))
            return

        deadline = data.get("pending_clear", {}).get(target_id, 0)
        now_ts = int(time.time())
        if deadline and now_ts <= deadline:
            data["boss"] = {}
            data["pending_clear"].pop(target_id, None)
            save_data(data)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 已清空全部Boss紀錄"))
        else:
            data["pending_clear"].pop(target_id, None)
            save_data(data)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⏱️ 已超時或未啟動清除，請重新輸入「王表清除」。"))
        return

    # ---- 單隻清除：Boss清除 / Boss 清除 ----
    boss_q = extract_boss_clear(text)
    if boss_q:
        canon, cands = resolve_boss(boss_q)
        if canon:
            if canon in data["boss"]:
                data["boss"].pop(canon, None)
                save_data(data)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 已清除：{canon}"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"{canon} 沒有紀錄"))
        else:
            if cands:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=format_candidates(cands)))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"找不到 Boss：「{boss_q}」。輸入「王」看清單。"))
        return

    # ---- 王出：列所有正式Boss的下一次重生 ----
    if text == "王出":
        boss_data = data.get("boss", {})
        rows = []
        for boss in BOSS_RESPAWN_MIN.keys():
            rec = boss_data.get(boss)
            if rec and rec.get("respawn"):
                try:
                    rdt = datetime.fromisoformat(rec["respawn"])
                    rdt = roll_respawn_to_future(boss, rdt)
                    rows.append((rdt, f"{boss}：{rdt.strftime('%H:%M')}（{remain_text(rdt)}）"))
                except:
                    rows.append((datetime.max, f"{boss}：未紀錄"))
            else:
                rows.append((datetime.max, f"{boss}：未紀錄"))

        rows.sort(key=lambda x: x[0])
        lines = ["【王出 / 下一次重生】"] + [s for _, s in rows]
        reply = "\n".join(lines)

        if len(reply) > 3500:
            first = "\n".join(lines[:60])
            second = "\n".join(["（續）"] + lines[60:])
            line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=first), TextSendMessage(text=second)])
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # ---- 一般：Boss + 時間（+出）----
    boss_q, token, is_respawn_input = extract_boss_and_time(text)
    if boss_q and token:
        canon, cands = resolve_boss(boss_q)
        if not canon:
            if cands:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=format_candidates(cands)))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"找不到 Boss：「{boss_q}」。輸入「王」看清單。"))
            return

        try:
            hh, mm = parse_time_token(token)
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="時間格式錯誤\n例：鳥1430 或 鳥1400出"))
            return

        boss_data = data.get("boss", {})
        boss_data.setdefault(canon, {})

        if is_respawn_input:
            # ✅ 你指定「下一次重生時間」：就是那個時間（不往後推）
            respawn_dt = next_occurrence_today_or_tomorrow(hh, mm)
            respawn_dt = roll_respawn_to_future(canon, respawn_dt)
        else:
            # 你輸入死亡時間：死亡+週期=重生
            death_dt = next_occurrence_today_or_tomorrow(hh, mm)
            respawn_dt = death_dt + timedelta(minutes=BOSS_RESPAWN_MIN[canon])
            respawn_dt = roll_respawn_to_future(canon, respawn_dt)

        boss_data[canon]["respawn"] = respawn_dt.isoformat()
        boss_data[canon]["last_notified"] = ""  # 重設提醒狀態
        data["boss"] = boss_data
        save_data(data)

        msg = (
            f"\n"
            f"下一次重生：{respawn_dt.strftime('%H:%M')}\n"
            f"{remain_text(respawn_dt)}\n"
            f"（重生前 {REMIND_BEFORE_MIN} 分鐘提醒）"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # ---- 其他：回指令說明 ----
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=cmd_help_text()))


if __name__ == "__main__":
    start_reminder_thread()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
