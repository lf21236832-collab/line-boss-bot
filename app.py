import os
import re
import json
import time
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage


# =========================
# 版本號
# =========================
VERSION = "v2026-02-14-0815"  # ✅ 你要的：0214-完成時間（0815可自行改成你完成的HHMM）


# =========================
# 基本設定
# =========================
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN", "").strip()
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET", "").strip()

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("Missing CHANNEL_ACCESS_TOKEN or CHANNEL_SECRET in environment variables.")

TZ_NAME = os.getenv("TZ", "Asia/Taipei").strip()
TZ = ZoneInfo(TZ_NAME)

DATA_DIR = os.getenv("DATA_DIR", "data")  # ✅ 雲端版：相對路徑
os.makedirs(DATA_DIR, exist_ok=True)

DATA_PATH = os.path.join(DATA_DIR, "boss_data.json")

# 提醒/警示/清除策略
REMIND_BEFORE_MIN = 5
WARNING_BEFORE_MIN = 30
EXPIRE_GRACE_MIN = 3          # 超過重生時間 + N 分鐘就自動清除
CHECK_INTERVAL_SEC = 20       # 背景檢查頻率

# 王表清除防誤刪：請在幾秒內二次確認
CLEAR_CONFIRM_TTL_SEC = 60


# =========================
# Boss 表（正式名 + 分鐘 + 別名）
# =========================
BOSS_TABLE = [
    ("巨大鱷魚", 60, ["鱷魚"]),
    ("單飛龍", 180, ["單龍"]),
    ("雙飛龍", 180, ["雙龍"]),
    ("黑長者", 240, ["黑老"]),
    ("克特", 360, ["克"]),
    ("四色", 180, []),
    ("魔法師", 180, []),
    ("死亡騎士", 360, ["死騎", "死"]),
    ("巴風特", 240, ["小巴"]),
    ("巴列斯", 240, ["大巴"]),
    ("巨蟻女皇", 360, ["螞蟻"]),
    ("變形怪首領", 300, ["變怪"]),
    ("伊佛利特", 180, ["EF"]),
    ("不死鳥", 360, ["鳥"]),
    ("冰之女王", 360, ["冰女"]),
    ("惡魔", 360, []),
    ("古代巨人", 360, ["古巨"]),
    ("反王肯恩", 240, ["反"]),
    ("賽尼斯", 240, ["賽"]),
    ("巨大牛人", 360, ["牛"]),
    ("潔尼斯女王", 360, ["2樓", "蜘蛛"]),
    ("幻象眼魔", 360, ["3樓", "大眼"]),
    ("吸血鬼", 360, ["4樓", "血鬼"]),
    ("殭屍王", 360, ["5樓", "殭屍"]),
    ("黑豹", 360, ["6樓"]),
    ("木乃伊王", 360, ["7樓"]),
    ("艾莉絲", 360, ["8樓"]),
    ("騎士范德", 360, ["9樓", "范德"]),
    ("巫妖", 360, ["10樓"]),
    ("鐮刀死神", 360, ["頂樓"]),
    ("土精靈王", 120, ["土"]),
    ("水精靈王", 120, ["水"]),
    ("風精靈王", 120, ["風"]),
    ("火精靈王", 120, ["火"]),
    ("獨角獸", 360, []),
    ("曼波兔(海賊島)", 360, ["海賊曼波", "海兔"]),
    ("庫曼", 360, []),
    ("德雷克", 180, []),
    ("曼波兔(精靈墓穴)", 360, ["墓穴曼波", "精兔"]),
    ("深淵之主", 360, []),
    ("須曼", 360, []),
    ("安塔瑞斯", 1440, ["地龍"]),
    ("巴拉卡斯", 1440, ["火龍"]),
    ("法利昂", 1440, ["水龍"]),
    ("林德拜爾", 1440, ["風龍"]),
]

BOSS_RESPAWN_MIN = {name: minutes for name, minutes, _aliases in BOSS_TABLE}

# 別名/正式名索引
ALIAS_MAP = {}
OFFICIAL_NAMES = []
for name, _m, aliases in BOSS_TABLE:
    OFFICIAL_NAMES.append(name)
    ALIAS_MAP[name] = name
    for a in aliases:
        ALIAS_MAP[a] = name


# =========================
# 小工具
# =========================
_lock = threading.Lock()

def now_tz() -> datetime:
    return datetime.now(TZ)

def load_data() -> dict:
    with _lock:
        if not os.path.exists(DATA_PATH):
            return {"groups": {}, "pending_clear": {}}
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {"groups": {}, "pending_clear": {}}

def save_data(data: dict) -> None:
    with _lock:
        with open(DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def normalize(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip())

def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%H:%M")

def fmt_left(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 0:
        total = 0
    h = total // 3600
    m = (total % 3600) // 60
    if h > 0:
        return f"{h}h{m}m"
    return f"{m}m"

def parse_hhmm(text: str) -> tuple[int, int] | None:
    t = (text or "").strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", t):
        hh, mm = t.split(":")
        hh = int(hh); mm = int(mm)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
        return None
    if re.fullmatch(r"\d{3,4}", t):
        if len(t) == 3:
            hh = int(t[0])
            mm = int(t[1:])
        else:
            hh = int(t[:2])
            mm = int(t[2:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    return None

def resolve_boss(query: str) -> list[str]:
    q = normalize(query)
    if not q:
        return []

    # 直接別名命中
    if q in ALIAS_MAP:
        return [ALIAS_MAP[q]]

    # 子字串模糊命中（含 1~2 字）
    hits = []
    for name in OFFICIAL_NAMES:
        if q in normalize(name):
            hits.append(name)
            continue
        # 別名也算
        for a, canon in ALIAS_MAP.items():
            if canon == name and q in normalize(a):
                hits.append(name)
                break

    # 去重保序
    seen = set()
    out = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out

def ensure_group(data: dict, group_id: str) -> None:
    if group_id not in data["groups"]:
        data["groups"][group_id] = {"boss": {}, "seen_at": datetime.utcnow().isoformat()}

def set_boss_respawn(data: dict, group_id: str, canon: str, respawn_dt: datetime) -> None:
    ensure_group(data, group_id)
    data["groups"][group_id]["boss"][canon] = {
        "respawn": respawn_dt.astimezone(TZ).isoformat(),
        "last_notified": ""  # 用 respawn iso 當 key，避免重複提醒
    }

def clear_boss(data: dict, group_id: str, canon: str) -> bool:
    if group_id in data["groups"] and canon in data["groups"][group_id].get("boss", {}):
        del data["groups"][group_id]["boss"][canon]
        return True
    return False

def list_registered(data: dict, group_id: str) -> list[tuple[str, datetime]]:
    out = []
    g = data["groups"].get(group_id, {})
    boss_data = g.get("boss", {})
    for canon, rec in boss_data.items():
        iso = rec.get("respawn")
        if not iso:
            continue
        try:
            dt = datetime.fromisoformat(iso).astimezone(TZ)
        except:
            continue
        out.append((canon, dt))
    out.sort(key=lambda x: x[1])
    return out

def should_speak(msg: str) -> bool:
    """
    ✅ 沒打到關鍵字就不回話
    - 指令：查詢/王/王出/王表清除/王表確認清除
    - 清除：*清除
    - 登記：含時間（HHMM或HH:MM）
    """
    m = normalize(msg)
    if m in ["查詢", "王", "王出", "王表清除", "王表確認清除", "群組ID"]:
        return True
    if m.endswith("清除"):
        return True
    if re.search(r"\d{3,4}", m) or re.search(r"\d{1,2}:\d{2}", m):
        return True
    return False


# =========================
# LINE 設定
# =========================
app = Flask(__name__)
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

def reply(event, text: str):
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))

def push_to_group(group_id: str, text: str):
    try:
        line_bot_api.push_message(group_id, TextSendMessage(text=text))
    except Exception:
        pass


# =========================
# 指令文字
# =========================
HELP_TEXT = (
    f"🛠 天堂王表機器人\n"
    f"{VERSION}\n"
    "──────────────────\n"
    "✨【可用指令】✨\n"
    "1) 王 😈：列出所有 Boss 名稱（只顯示正式名）\n"
    "2) 王出 ⏰：只顯示「已登記」的 Boss 下一次重生\n"
    "3) 死亡時間 ☠️：Boss1430 / Boss 14:30\n"
    "   → 代表 Boss 在該時間死亡，會自動算下一次重生\n"
    "4) 指定重生 🐣：Boss1400出 / Boss 14:00出\n"
    "   → 代表 Boss 在該時間重生（不會先 + 週期）\n"
    "5) 清除單隻 🧹：Boss清除（必須『Boss名稱+清除』）\n"
    "6) 清空全部 ⚠️：王表清除 → 60 秒內再輸入「王表確認清除」\n"
    "7) 查詢 📌：顯示本訊息\n"
    "🧠 模糊搜尋：例如「鳥」可找不死鳥；命中多個會請你縮小"
)


# =========================
# 主要處理：收到訊息
# =========================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text_raw = event.message.text or ""
    text = normalize(text_raw)

    # 如果不是對 bot 的格式，就完全沉默（避免群組被刷）
    if not should_speak(text_raw):
        return

    source = event.source
    group_id = getattr(source, "group_id", None)

    # ✅ 只做「群組提醒」：私訊可查詢但不記錄提醒目標
    in_group = bool(group_id)

    data = load_data()
    if in_group:
        ensure_group(data, group_id)

    now = now_tz()

    # --- 指令：查詢 ---
    if text == "查詢":
        reply(event, HELP_TEXT)
        if in_group:
            save_data(data)
        return
    # --- 測試：取得群組ID ---
    if text == "群組ID":
        group_id = getattr(event.source, "group_id", None)
        user_id = getattr(event.source, "user_id", None)

        reply(event,
              f"Group ID:\n{group_id}\n\nUser ID:\n{user_id}")
        return

    # --- 指令：王（全部正式名）---
    if text == "王":
        lines = ["😈【Boss 清單（正式名）】"]
        for n in OFFICIAL_NAMES:
            lines.append(f"• {n}")
        reply(event, "\n".join(lines))
        if in_group:
            save_data(data)
        return

    # --- 指令：王出（只顯示已登記）---
    if text == "王出":
        if not in_group:
            reply(event, "⚠️ 請在群組使用「王出」，我才會顯示該群組已登記的王。")
            return

        rows = list_registered(data, group_id)
        if not rows:
            reply(event, "📭 目前這個群組還沒有登記任何 Boss 時間。")
            return

        lines = ["⏰【已登記 Boss 下一次重生】"]
        for canon, respawn_dt in rows:
            left = respawn_dt - now
            mark = "🟥" if 0 <= left.total_seconds() <= WARNING_BEFORE_MIN * 60 else "🟩"
            lines.append(f"{mark} {canon} → {fmt_dt(respawn_dt)}（剩 {fmt_left(left)}）")

        reply(event, "\n".join(lines))
        return

    # --- 王表清除（防誤刪）---
    if text == "王表清除":
        if not in_group:
            reply(event, "⚠️ 請在群組使用王表清除。")
            return
        data["pending_clear"][group_id] = int(time.time())
        save_data(data)
        reply(event, "⚠️【防誤刪】請在 60 秒內再輸入：王表確認清除")
        return

    if text == "王表確認清除":
        if not in_group:
            reply(event, "⚠️ 請在群組使用王表確認清除。")
            return
        ts = data.get("pending_clear", {}).get(group_id)
        if not ts or (time.time() - ts) > CLEAR_CONFIRM_TTL_SEC:
            reply(event, "⏳ 已超時，請重新輸入：王表清除")
            return
        data["groups"][group_id]["boss"] = {}
        data["pending_clear"].pop(group_id, None)
        save_data(data)
        reply(event, "✅ 已清空本群組所有 Boss 記錄。")
        return

    # --- 單隻清除：Boss清除 ---
    if text.endswith("清除"):
        if not in_group:
            reply(event, "⚠️ 請在群組使用：Boss清除")
            return
        key = text[:-2]  # 去掉「清除」
        hits = resolve_boss(key)
        if len(hits) == 0:
            reply(event, f"找不到 Boss：「{key}」。輸入「王」看清單。")
            return
        if len(hits) > 1:
            reply(event, "🤔 命中多個 Boss，請再縮小：\n" + "\n".join([f"• {h}" for h in hits[:10]]))
            return
        canon = hits[0]
        ok = clear_boss(data, group_id, canon)
        save_data(data)
        if ok:
            reply(event, f"🧹已清除。")
        else:
            reply(event, f"📭本來就沒有登記。")
        return

    # --- 解析：指定重生（...出） or 死亡時間（沒出）---
    # 支援：Boss1400出 / Boss 14:00出 / Boss1400 / Boss 14:00
    m = re.match(r"^(.*?)(\d{1,2}:\d{2}|\d{3,4})(出)?$", text)
    if not m:
        return

    boss_key = m.group(1)
    time_str = m.group(2)
    is_respawn_mark = bool(m.group(3))  # 有「出」代表指定重生

    hhmm = parse_hhmm(time_str)
    if not hhmm:
        return
    hh, mm = hhmm

    hits = resolve_boss(boss_key)
    if len(hits) == 0:
        reply(event, f"找不到 Boss：「{boss_key}」。輸入「王」看清單。")
        return
    if len(hits) > 1:
        reply(event, "🤔 命中多個 Boss，請再縮小：\n" + "\n".join([f"• {h}" for h in hits[:10]]))
        return

    canon = hits[0]
    respawn_min = BOSS_RESPAWN_MIN.get(canon)
    if not respawn_min:
        reply(event, f"⚠️沒有設定重生週期。")
        return

    if not in_group:
        reply(event, "⚠️ 請在群組使用登記（死亡/指定重生），我才會對該群組做提醒。")
        return

    base = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    if is_respawn_mark:
        respawn_dt = base
        while respawn_dt <= now:
            respawn_dt += timedelta(minutes=respawn_min)

        set_boss_respawn(data, group_id, canon, respawn_dt)
        save_data(data)

        reply(event,
              f"🐣【{canon}】指定重生已登記\n"
              f"下一次重生：{fmt_dt(respawn_dt)}\n"
              f"剩餘：{fmt_left(respawn_dt - now)}\n"
              f"（重生前 {REMIND_BEFORE_MIN} 分鐘提醒）")
        return
    else:
        death_dt = base
        if death_dt > now + timedelta(minutes=1):
            death_dt -= timedelta(days=1)

        respawn_dt = death_dt + timedelta(minutes=respawn_min)
        while respawn_dt <= now:
            respawn_dt += timedelta(minutes=respawn_min)

        set_boss_respawn(data, group_id, canon, respawn_dt)
        save_data(data)

        reply(event,
              f"☠️【{canon}】死亡時間已登記\n"
              f"下一次重生：{fmt_dt(respawn_dt)}\n"
              f"剩餘：{fmt_left(respawn_dt - now)}\n"
              f"（重生前 {REMIND_BEFORE_MIN} 分鐘提醒）")
        return


# =========================
# Webhook
# =========================
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


# =========================
# 背景提醒任務（群組推播）
# =========================
def reminder_loop():
    """
    ✅ 修正重點：5 分鐘提醒「不會漏」
    原本用 0 < left <= 300 太嚴格，loop若剛好延遲/重啟跨過0秒會直接錯過。
    現在加入 tolerance，允許小幅負秒數也補發一次（仍用 last_notified 防止重複）。
    """
    tolerance = CHECK_INTERVAL_SEC * 2  # 例如 40 秒容錯

    while True:
        try:
            data = load_data()
            changed = False
            now = now_tz()

            for group_id, g in list(data.get("groups", {}).items()):
                boss_data = g.get("boss", {})
                for canon, rec in list(boss_data.items()):
                    iso = rec.get("respawn")
                    if not iso:
                        continue
                    try:
                        respawn_dt = datetime.fromisoformat(iso).astimezone(TZ)
                    except:
                        continue

                    # 1) 超過重生時間 + 寬限 → 自動清除
                    if now > respawn_dt + timedelta(minutes=EXPIRE_GRACE_MIN):
                        del boss_data[canon]
                        changed = True
                        continue

                    # 2) 5 分鐘提醒（只提醒一次，且容錯避免漏）
                    left = respawn_dt - now
                    left_sec = left.total_seconds()

                    if -tolerance <= left_sec <= REMIND_BEFORE_MIN * 60:
                        key = respawn_dt.isoformat()
                        if rec.get("last_notified", "") != key:
                            msg = (
                                f"🔔【{canon}】快重生啦！\n"
                                f"⏳ 剩餘：{fmt_left(left)}\n"
                                f"🕒 重生：{fmt_dt(respawn_dt)}"
                            )
                            push_to_group(group_id, msg)
                            rec["last_notified"] = key
                            changed = True

            if changed:
                save_data(data)

        except Exception:
            pass

        time.sleep(CHECK_INTERVAL_SEC)


# =========================
# 啟動
# =========================
threading.Thread(target=reminder_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
