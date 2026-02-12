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
# 基本設定
# =========================
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN", "").strip()
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET", "").strip()
TZ_NAME = os.getenv("TZ", "Asia/Taipei").strip()

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("Missing CHANNEL_ACCESS_TOKEN / CHANNEL_SECRET")

TZ = ZoneInfo(TZ_NAME)

# ✅ 非 disk 版：存到專案 data/（可寫）
DATA_DIR = os.getenv("DATA_DIR", "data").strip()
os.makedirs(DATA_DIR, exist_ok=True)

DATA_PATH = os.path.join(DATA_DIR, "boss_data.json")

REMIND_BEFORE_MIN = 5
WARNING_BEFORE_MIN = 30
EXPIRE_GRACE_MIN = 3
CHECK_INTERVAL_SEC = 20

_lock = threading.Lock()

app = Flask(__name__)
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# =========================
# Boss 表（正式名 / 分鐘 / 別名）
# 查詢顯示只顯示正式名，不顯示括號別名
# =========================
BOSS_TABLE = [
    ("巨大鱷魚", 60,  ["鱷魚"]),
    ("單飛龍", 180,  ["單龍"]),
    ("雙飛龍", 180,  ["雙龍"]),
    ("黑長者", 240,  ["黑老"]),
    ("克特", 360,  []),
    ("四色", 180,  []),
    ("魔法師", 180,  []),
    ("死亡騎士", 360, ["死騎", "死"]),
    ("巴風特", 240,  ["小巴"]),
    ("巴列斯", 240,  ["大巴"]),
    ("巨蟻女皇", 360, ["螞蟻"]),
    ("變形怪首領", 300, ["變怪"]),
    ("伊佛利特", 180, ["EF"]),
    ("不死鳥", 360,   ["鳥", "不死"]),
    ("冰之女王", 360, ["冰女"]),
    ("惡魔", 360,     []),
    ("古代巨人", 360, ["古巨"]),
    ("反王肯恩", 240, ["反王"]),
    ("賽尼斯", 240,   []),
    ("巨大牛人", 360, ["牛"]),
    ("潔尼斯女王", 360, ["2樓"]),
    ("幻象眼魔", 360,   ["3樓"]),
    ("吸血鬼", 360,     ["4樓"]),
    ("殭屍王", 360,     ["5樓"]),
    ("黑豹", 360,       ["6樓"]),
    ("木乃伊王", 360,   ["7樓"]),
    ("艾莉絲", 360,     ["8樓"]),
    ("騎士范德", 360,   ["9樓"]),
    ("巫妖", 360,       ["10樓"]),
    ("土精靈王", 120,   ["土"]),
    ("水精靈王", 120,   ["水"]),
    ("風精靈王", 120,   ["風"]),
    ("火精靈王", 120,   ["火"]),
    ("獨角獸", 360,     []),
    ("曼波兔(海賊島)", 360, ["海賊兔", "海賊"]),
    ("庫曼", 360,       []),
    ("德雷克", 180,     []),
    ("曼波兔(精靈墓穴)", 360, ["墓穴兔", "墓穴"]),
    ("深淵之主", 360,   ["深淵"]),
    ("須曼", 360,       []),
    ("安塔瑞斯", 720,   []),
    ("巴拉卡斯", 720,   []),
    ("法利昂", 720,     []),
    ("林德拜爾", 720,   []),
]

BOSS_RESPAWN_MIN = {name: mins for (name, mins, _) in BOSS_TABLE}
OFFICIAL_NAMES = [name for (name, _, _) in BOSS_TABLE]

# alias/正式名 -> set(正式名)
ALIAS_INDEX = {}
for name, _, aliases in BOSS_TABLE:
    for key in [name] + aliases:
        key = key.strip()
        if not key:
            continue
        ALIAS_INDEX.setdefault(key, set()).add(name)

# =========================
# 資料存取
# data 結構：
# {
#   "targets": ["<group_id>", "<room_id>"],  # ✅ 只存群組/聊天室，不存 user_id
#   "boss": {
#     "<official_boss>": {
#        "respawn": "<iso with tz>",
#        "last_notified": "<respawn_iso or ''>",
#        "mode": "death" | "respawn"
#     }
#   },
#   "_pending_clear_until": "<iso>" or ""
# }
# =========================
def load_data():
    if not os.path.exists(DATA_PATH):
        return {"targets": [], "boss": {}, "_pending_clear_until": ""}
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
            if "targets" not in d: d["targets"] = []
            if "boss" not in d: d["boss"] = {}
            if "_pending_clear_until" not in d: d["_pending_clear_until"] = ""
            return d
    except:
        return {"targets": [], "boss": {}, "_pending_clear_until": ""}

def save_data(d):
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_PATH)

def now_tz():
    return datetime.now(TZ)

# =========================
# 時間解析
# 支援：1430 / 0140 / 14:30 / 14：30
# =========================
def parse_hhmm(token: str):
    token = token.strip().replace("：", ":")
    m = re.fullmatch(r"(\d{1,2})(?::?)(\d{2})", token)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return hh, mm
    return None

def dt_today(hh: int, mm: int):
    n = now_tz()
    return n.replace(hour=hh, minute=mm, second=0, microsecond=0)

def roll_forward_by_period(dt: datetime, period_min: int):
    """把 dt 往後加週期，直到在未來（用於死亡時間推算）。"""
    if period_min <= 0:
        return dt
    n = now_tz()
    step = timedelta(minutes=period_min)
    for _ in range(2000):
        if dt > n:
            return dt
        dt += step
    return dt

def next_occurrence_clock(hh: int, mm: int):
    """指定重生：找下一次出現的時刻（今天未到就今天，到過了就明天）。"""
    n = now_tz()
    candidate = n.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= n:
        candidate += timedelta(days=1)
    return candidate

def remain_text(respawn_dt: datetime) -> str:
    n = now_tz()
    diff = respawn_dt - n
    sec = int(diff.total_seconds())
    if sec < 0:
        sec = 0
    mins = sec // 60
    h = mins // 60
    m = mins % 60
    if h <= 0:
        return f"{m}m"
    return f"{h}h{m:02d}m"

def badge(respawn_dt: datetime) -> str:
    n = now_tz()
    diff = respawn_dt - n
    mins = int(diff.total_seconds() // 60)
    if mins <= REMIND_BEFORE_MIN:
        return "🔔🔴"
    if mins <= WARNING_BEFORE_MIN:
        return "🔴"
    return "🟢"

# =========================
# Boss 模糊搜尋
# - 精準：完全等於 alias/正式名
# - 模糊：子字串命中（alias/正式名）
# - 多命中：回候選
# =========================
def resolve_boss(query: str):
    q = query.strip()
    if not q:
        return ("none", [])

    if q in ALIAS_INDEX:
        names = sorted(list(ALIAS_INDEX[q]))
        if len(names) == 1:
            return ("ok", names[0])
        return ("multi", names)

    hits = set()
    for key, nameset in ALIAS_INDEX.items():
        if q in key:
            hits |= nameset

    hits = sorted(list(hits))
    if len(hits) == 1:
        return ("ok", hits[0])
    if len(hits) >= 2:
        return ("multi", hits[:12])
    return ("none", [])

# =========================
# 指令文字
# =========================
def help_text():
    return (
        "✨【可用指令】✨\n"
        "1) 王 😈：列出所有 Boss 名稱（只顯示正式名）\n"
        "2) 王出 ⏰：只顯示『已登記』Boss 的下一次重生（30 分內🔴）\n"
        "3) 死亡時間 ☠️：Boss1430 / Boss 14:30\n"
        "   → 代表 14:30 死亡，自動算下一次重生（若已過會自動補週期）\n"
        "4) 指定重生 🐣：Boss1400出 / Boss 14:00出\n"
        "   → 代表下一次重生在 14:00（不先 + 週期）\n"
        "5) 清除單隻 🧹：Boss清除（必須 boss名稱+清除）\n"
        "6) 清空全部 ⚠️：王表清除 → 再輸入 王表清除確認\n"
        "7) 查詢 📌：顯示本說明\n"
        "🔎 模糊搜尋：例如打『鳥』可找不死鳥；若命中多個會請你縮小\n\n"
        "📌 本機器人『只對群組/聊天室提醒』：請在群組內輸入指令讓我記住群組。"
    )

# =========================
# Targets（只記錄群組/聊天室；不記 user）
# =========================
def get_group_or_room_id(event):
    src = event.source
    if hasattr(src, "group_id") and src.group_id:
        return src.group_id
    if hasattr(src, "room_id") and src.room_id:
        return src.room_id
    return None  # ✅ 1對1 不記錄、不推播

def remember_target_group_only(event):
    tid = get_group_or_room_id(event)
    if not tid:
        return
    with _lock:
        data = load_data()
        targets = data.get("targets", [])
        if tid not in targets:
            targets.append(tid)
            data["targets"] = targets
            save_data(data)

def push_to_groups_only(text: str):
    with _lock:
        data = load_data()
        targets = data.get("targets", [])
    for tid in targets:
        try:
            line_bot_api.push_message(tid, TextSendMessage(text=text))
        except:
            pass

# =========================
# 背景提醒 + 過期自動清除
# =========================
def reminder_loop():
    while True:
        try:
            with _lock:
                data = load_data()
                boss_map = data.get("boss", {})
                changed = False

                n = now_tz()

                for boss, rec in list(boss_map.items()):
                    iso = (rec or {}).get("respawn", "")
                    if not iso:
                        continue
                    try:
                        respawn_dt = datetime.fromisoformat(iso).astimezone(TZ)
                    except:
                        continue

                    # ✅ 超過重生 + 緩衝 => 自動清除
                    if n > respawn_dt + timedelta(minutes=EXPIRE_GRACE_MIN):
                        boss_map.pop(boss, None)
                        changed = True
                        continue

                    # ✅ 5 分鐘提醒（只提醒一次）
                    remind_at = respawn_dt - timedelta(minutes=REMIND_BEFORE_MIN)
                    if remind_at <= n <= respawn_dt:
                        key = respawn_dt.isoformat()
                        last = (rec or {}).get("last_notified", "")
                        if last != key:
                            msg = (
                                f"🔔🔴【5分鐘提醒】\n"
                                f"👑 {boss}\n"
                                f"⏰ 重生：{respawn_dt.strftime('%H:%M')}\n"
                                f"⏳ 剩：{remain_text(respawn_dt)}"
                            )
                            # ✅ 只推群組/聊天室
                            push_to_groups_only(msg)
                            boss_map[boss]["last_notified"] = key
                            changed = True

                if changed:
                    data["boss"] = boss_map
                    save_data(data)

        except:
            pass

        time.sleep(CHECK_INTERVAL_SEC)

# ✅ gunicorn import 時就啟動
threading.Thread(target=reminder_loop, daemon=True).start()

# =========================
# Flask routes
# =========================
@app.route("/", methods=["GET"])
def health():
    return "OK", 200

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
# 訊息處理（沒命中就沉默）
# =========================
TIME_CMD_RE = re.compile(r"^(?P<boss>.+?)\s*(?P<time>\d{1,2}[:：]?\d{2})\s*(?P<out>出)?$")

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = (event.message.text or "").strip()
    if not text:
        return

    # ✅ 只記錄群組/聊天室（不記個人）
    try:
        remember_target_group_only(event)
    except:
        pass

    # 固定指令
    if text in ("查詢", "help", "指令"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text()))
        return

    if text == "王":
        msg = "😈【Boss清單】😈\n" + "\n".join([f"• {n}" for n in OFFICIAL_NAMES])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    if text == "王出":
        with _lock:
            data = load_data()
            boss_map = data.get("boss", {})

        rows = []
        n = now_tz()
        for boss, rec in boss_map.items():
            iso = (rec or {}).get("respawn", "")
            if not iso:
                continue
            try:
                respawn_dt = datetime.fromisoformat(iso).astimezone(TZ)
            except:
                continue
            if n > respawn_dt + timedelta(minutes=EXPIRE_GRACE_MIN):
                continue
            rows.append((respawn_dt, boss))

        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🫥 目前沒有任何已登記的 Boss。"))
            return

        rows.sort(key=lambda x: x[0])
        lines = ["⏰【已登記王出】⏰"]
        for respawn_dt, boss in rows:
            lines.append(
                f"{badge(respawn_dt)} {boss}：{respawn_dt.strftime('%H:%M')}（剩 {remain_text(respawn_dt)}）"
            )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(lines)))
        return

    # 王表清除（二段防誤刪）
    if text == "王表清除":
        with _lock:
            data = load_data()
            data["_pending_clear_until"] = (now_tz() + timedelta(seconds=60)).isoformat()
            save_data(data)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="⚠️ 確定要清空全部王表嗎？\n請在 60 秒內再輸入：王表清除確認")
        )
        return

    if text == "王表清除確認":
        with _lock:
            data = load_data()
            until = data.get("_pending_clear_until", "")
            ok = False
            if until:
                try:
                    ok = now_tz() <= datetime.fromisoformat(until).astimezone(TZ)
                except:
                    ok = False
            if not ok:
                return
            data["boss"] = {}
            data["_pending_clear_until"] = ""
            save_data(data)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🧹✅ 已清空全部王表時間。"))
        return

    # 單隻清除：Boss清除
    if text.endswith("清除") and text not in ("王表清除", "王表清除確認"):
        boss_raw = text[:-2].strip()
        status, res = resolve_boss(boss_raw)
        if status == "none":
            return
        if status == "multi":
            msg = "🤔 命中多個 Boss，請再縮小：\n" + "\n".join([f"• {x}" for x in res])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
            return

        boss = res
        with _lock:
            data = load_data()
            boss_map = data.get("boss", {})
            if boss in boss_map:
                boss_map.pop(boss, None)
                data["boss"] = boss_map
                save_data(data)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🧹✅ 已清除：{boss}"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🫥 {boss} 目前沒有登記時間。"))
        return

    # 時間指令：死亡 or 指定重生
    m = TIME_CMD_RE.match(text)
    if not m:
        # 沒命中任何指令/格式 => 沉默
        return

    boss_raw = (m.group("boss") or "").strip()
    time_raw = (m.group("time") or "").strip()
    is_out = (m.group("out") is not None)

    status, res = resolve_boss(boss_raw)
    if status == "none":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"找不到 Boss：「{boss_raw}」。輸入「王」看清單。"))
        return
    if status == "multi":
        msg = "🤔 命中多個 Boss，請再縮小：\n" + "\n".join([f"• {x}" for x in res])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    boss = res
    hm = parse_hhmm(time_raw)
    if not hm:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⛔ 時間格式錯誤，請用 1430 或 14:30（也支援 0100）"))
        return
    hh, mm = hm

    with _lock:
        data = load_data()
        boss_map = data.get("boss", {})

        if is_out:
            respawn_dt = next_occurrence_clock(hh, mm)
            boss_map[boss] = {
                "respawn": respawn_dt.isoformat(),
                "last_notified": "",
                "mode": "respawn",
            }
            data["boss"] = boss_map
            save_data(data)

            msg = (
                f"🐣 已設定重生\n"
                f"👑 {boss}\n"
                f"⏰ 下一次：{respawn_dt.strftime('%H:%M')}\n"
                f"⏳ 剩：{remain_text(respawn_dt)}\n"
                f"🔔 前 {REMIND_BEFORE_MIN} 分鐘提醒（只發群組/聊天室）"
            )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
            return

        period = BOSS_RESPAWN_MIN.get(boss, 0)
        death_dt = dt_today(hh, mm)
        respawn_dt = death_dt + timedelta(minutes=period)
        respawn_dt = roll_forward_by_period(respawn_dt, period)

        boss_map[boss] = {
            "respawn": respawn_dt.isoformat(),
            "last_notified": "",
            "mode": "death",
        }
        data["boss"] = boss_map
        save_data(data)

        msg = (
            f"☠️ 已登記死亡\n"
            f"👑 {boss}\n"
            f"⏰ 下一次：{respawn_dt.strftime('%H:%M')}（{period} 分鐘）\n"
            f"⏳ 剩：{remain_text(respawn_dt)}\n"
            f"🔔 前 {REMIND_BEFORE_MIN} 分鐘提醒（只發群組/聊天室）"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
