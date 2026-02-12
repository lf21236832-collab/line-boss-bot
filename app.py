import os
import re
import json
import threading
import time
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
if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("Missing CHANNEL_ACCESS_TOKEN or CHANNEL_SECRET")

TZ_NAME = os.getenv("TZ", "Asia/Taipei")
TZ = ZoneInfo(TZ_NAME)

app = Flask(__name__)
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# =========================
# 存檔（非 disk 版：存在專案目錄 data/）
# =========================
DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)

DATA_PATH = os.path.join(DATA_DIR, "boss_data.json")
PENDING_CLEAR_PATH = os.path.join(DATA_DIR, "pending_clear.json")

# =========================
# 提醒/清除規則
# =========================
REMIND_BEFORE_MIN = 5        # 提醒：重生前 5 分鐘
WARNING_BEFORE_MIN = 30      # 清單顯示：<=30 分鐘用紅色標記
EXPIRE_GRACE_MIN = 3         # 超過重生時間 +3 分鐘，自動清除
CHECK_INTERVAL_SEC = 20      # 背景檢查間隔

# =========================
# Boss 表（正式名, 重生分鐘, 別名list）
# 查詢顯示只顯示正式名
# =========================
BOSS_TABLE = [
    ("巨大鱷魚", 60, []),
    ("單飛龍", 180, []),
    ("雙飛龍", 180, []),
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
    ("林德拜爾", 720, []),
]

BOSS_RESPAWN_MIN = {name: mins for name, mins, _ in BOSS_TABLE}

# 建立「可搜尋字典」：正式名+別名 -> 正式名
ALIAS_TO_CANON = {}
CANON_NAMES = []
for name, _, aliases in BOSS_TABLE:
    CANON_NAMES.append(name)
    ALIAS_TO_CANON[name] = name
    for a in aliases:
        ALIAS_TO_CANON[a] = name

# =========================
# 讀寫 JSON
# =========================
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_data():
    # 格式:
    # {
    #   "targets": [groupId1, ...],
    #   "boss": {
    #      "巴風特": {"respawn": "2026-02-13T14:00:00+08:00", "last_notified": "" , "mode": "death/spawn"}
    #   }
    # }
    return load_json(DATA_PATH, {"targets": [], "boss": {}})

def save_data(data):
    save_json(DATA_PATH, data)

def load_pending():
    # {"confirm_all_clear": {"token": "...", "expires_at": "..."}}
    return load_json(PENDING_CLEAR_PATH, {})

def save_pending(p):
    save_json(PENDING_CLEAR_PATH, p)

# =========================
# 工具
# =========================
def now_tz():
    return datetime.now(TZ)

def parse_hhmm(text: str):
    """接受 4 碼 1430 或 14:30"""
    text = text.strip()
    m = re.search(r"(\d{2}):?(\d{2})", text)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    return hh, mm

def fmt_dt(dt: datetime):
    # 顯示 HH:MM
    return dt.astimezone(TZ).strftime("%H:%M")

def fmt_left(delta: timedelta):
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    h = secs // 3600
    m = (secs % 3600) // 60
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"

def normalize_text(s: str):
    return re.sub(r"\s+", "", s.strip())

def fuzzy_find_boss(query: str):
    """
    支援：
    - 完整名 / 別名
    - 子字串（例如 '鳥'）
    回傳：
    - (canon_name, None) 若唯一命中
    - (None, [list]) 若多命中
    - (None, []) 若沒命中
    """
    q = query.strip()
    if not q:
        return None, []

    # 先直接別名/正式名命中
    if q in ALIAS_TO_CANON:
        return ALIAS_TO_CANON[q], None

    # 子字串搜尋（對正式名與別名都做）
    hits = set()
    for alias, canon in ALIAS_TO_CANON.items():
        if q in alias:
            hits.add(canon)

    hits = sorted(list(hits))
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, hits
    return None, []

def make_help_text():
    return (
        "✨【可用指令】✨\n"
        "1) 王 😈：列出所有 Boss 名稱（只顯示正式名）\n"
        "2) 王出 ⏰：只顯示「已登記」Boss 下一次重生\n"
        "3) 死亡時間 ☠️：Boss1430 / Boss 1430\n"
        "   → 代表 Boss 14:30 死亡，會自動算下一次重生（會往未來推）\n"
        "4) 指定重生 🐣：Boss1400出 / Boss 1400出\n"
        "   → 代表 Boss 14:00 重生（只記下一個 14:00，不會先 +週期）\n"
        "5) 清除單隻 🧹：Boss清除 / Boss 清除（必須 Boss+清除）\n"
        "6) 清空全部 ⚠️：王表清除（需要二次確認）\n"
        "7) 查詢 📌：顯示本訊息\n"
        "\n"
        "🔎 模糊搜尋：例如打「鳥」可找不死鳥；若命中多個會請你縮小"
    )

def is_cmd_help(t): return normalize_text(t) == "查詢"
def is_cmd_list_all(t): return normalize_text(t) == "王"
def is_cmd_list_registered(t): return normalize_text(t) == "王出"
def is_cmd_clear_all(t): return normalize_text(t) == "王表清除"

def is_confirm_clear_all(t):
    # 二次確認：王表清除確認
    return normalize_text(t) == "王表清除確認"

def extract_clear_single(text: str):
    # 必須 boss 名稱 + 清除
    # e.g. "小巴清除" "巴風特 清除"
    t = text.strip()
    if "清除" not in t:
        return None
    t2 = normalize_text(t)
    if not t2.endswith("清除"):
        return None
    boss_part = t2[:-2]  # 去掉 "清除"
    if not boss_part:
        return None
    return boss_part

def parse_death_cmd(text: str):
    # Boss1430 或 Boss 14:30 （不能含 出）
    t = normalize_text(text)
    if "出" in t:
        return None
    # 找時間
    hhmm = parse_hhmm(t)
    if not hhmm:
        return None
    # boss 名 = 把時間拿掉剩下文字
    boss_part = re.sub(r"\d{2}:?\d{2}", "", t)
    boss_part = boss_part.strip()
    if not boss_part:
        return None
    return boss_part, hhmm

def parse_spawn_cmd(text: str):
    # Boss1400出 / Boss 14:00出
    t = normalize_text(text)
    if not t.endswith("出"):
        return None
    core = t[:-1]  # 去掉 出
    hhmm = parse_hhmm(core)
    if not hhmm:
        return None
    boss_part = re.sub(r"\d{2}:?\d{2}", "", core).strip()
    if not boss_part:
        return None
    return boss_part, hhmm

def compute_next_spawn_by_clock(hh, mm, now):
    """指定重生：記下一個指定時刻（今天未過就今天，過了就明天）"""
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate

def compute_respawn_from_death(hh, mm, canon, now):
    """死亡時間：死亡時刻 + 週期；若算出來已過，往未來推到下一次"""
    mins = BOSS_RESPAWN_MIN[canon]
    death = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    # 如果使用者輸入的死亡時間 > 現在（例如現在 03:00 卻輸入 23:00）
    # 當作昨天的 23:00
    if death > now:
        death = death - timedelta(days=1)

    respawn = death + timedelta(minutes=mins)
    while respawn <= now:
        respawn += timedelta(minutes=mins)
    return respawn

def push_to_groups(text: str):
    data = load_data()
    targets = data.get("targets", [])
    if not targets:
        return
    msg = TextSendMessage(text=text)
    for gid in targets:
        try:
            line_bot_api.push_message(gid, msg)
        except Exception as e:
            print("push failed:", gid, e)

def reply(event, text: str):
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))


# =========================
# 背景提醒：只推群組
# =========================
def reminder_loop():
    while True:
        try:
            data = load_data()
            boss_data = data.get("boss", {})
            if not boss_data:
                time.sleep(CHECK_INTERVAL_SEC)
                continue

            now = now_tz()
            changed = False

            # 遍歷 copy（因為可能刪除）
            for canon, rec in list(boss_data.items()):
                if canon not in BOSS_RESPAWN_MIN:
                    continue

                respawn_iso = rec.get("respawn")
                if not respawn_iso:
                    continue

                try:
                    respawn_dt = datetime.fromisoformat(respawn_iso)
                    if respawn_dt.tzinfo is None:
                        respawn_dt = respawn_dt.replace(tzinfo=TZ)
                    else:
                        respawn_dt = respawn_dt.astimezone(TZ)
                except:
                    continue

                # 超過重生時間 + grace：自動清除
                if now > respawn_dt + timedelta(minutes=EXPIRE_GRACE_MIN):
                    del boss_data[canon]
                    changed = True
                    continue

                remind_at = respawn_dt - timedelta(minutes=REMIND_BEFORE_MIN)
                if remind_at <= now <= respawn_dt:
                    key = respawn_dt.isoformat()
                    if rec.get("last_notified", "") != key:
                        left = respawn_dt - now
                        # 🔔 群組提醒
                        msg = (
                            f"🔔快重生啦！\n"
                            f"⏳ 剩餘：{fmt_left(left)}\n"
                            f"🕒 重生：{fmt_dt(respawn_dt)}"
                        )
                        push_to_groups(msg)
                        boss_data[canon]["last_notified"] = key
                        changed = True

            if changed:
                data["boss"] = boss_data
                save_data(data)

        except Exception as e:
            print("reminder loop error:", e)

        time.sleep(CHECK_INTERVAL_SEC)


threading.Thread(target=reminder_loop, daemon=True).start()


# =========================
# Flask endpoints
# =========================
@app.route("/health", methods=["GET"])
def health():
    return "ok", 200

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
# LINE message handler
# =========================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = (event.message.text or "").strip()
    if not text:
        return

    # ✅ 自動記錄群組ID（只要群組有人講話一次就存）
    if event.source.type == "group":
        gid = event.source.group_id
        data = load_data()
        if "targets" not in data:
            data["targets"] = []
        if gid and gid not in data["targets"]:
            data["targets"].append(gid)
            save_data(data)
            print("✅ 已儲存群組ID:", gid)

    tnorm = normalize_text(text)

    # 1) 查詢：顯示指令
    if is_cmd_help(text):
        reply(event, make_help_text())
        return

    # 2) 王：列所有 boss 名稱（正式名）
    if is_cmd_list_all(text):
        lines = ["😈【Boss 名單】"]
        for name in CANON_NAMES:
            lines.append(f"• {name}")
        reply(event, "\n".join(lines))
        return

    # 3) 王出：只顯示已登記
    if is_cmd_list_registered(text):
        data = load_data()
        boss_data = data.get("boss", {})
        now = now_tz()

        # 只保留有 respawn 的
        items = []
        for canon, rec in boss_data.items():
            respawn_iso = rec.get("respawn")
            if not respawn_iso:
                continue
            try:
                dt = datetime.fromisoformat(respawn_iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=TZ)
                dt = dt.astimezone(TZ)
            except:
                continue
            # 已過 + grace 會被背景清掉，但這邊也防呆
            if now > dt + timedelta(minutes=EXPIRE_GRACE_MIN):
                continue
            items.append((dt, canon))

        if not items:
            reply(event, "📭 目前沒有已登記的 Boss。\n（用：Boss1430 或 Boss1400出 來登記）")
            return

        items.sort(key=lambda x: x[0])
        lines = ["⏰【王出清單】（只顯示已登記）"]
        for dt, canon in items:
            left = dt - now
            tag = "🔴" if left <= timedelta(minutes=WARNING_BEFORE_MIN) else "🟢"
            lines.append(f"{tag} {canon}：{fmt_dt(dt)}（剩 {fmt_left(left)}）")
        reply(event, "\n".join(lines))
        return

    # 4) 王表清除（二次確認）
    if is_cmd_clear_all(text):
        p = load_pending()
        token = f"{int(time.time())}"
        p["confirm_all_clear"] = {
            "token": token,
            "expires_at": (now_tz() + timedelta(minutes=3)).isoformat()
        }
        save_pending(p)
        reply(event, "⚠️ 你確定要清空全部紀錄嗎？\n請在 3 分鐘內輸入：\n✅ 王表清除確認")
        return

    if is_confirm_clear_all(text):
        p = load_pending()
        info = p.get("confirm_all_clear")
        if not info:
            reply(event, "⏳ 沒有待確認的清除指令（已過期或未發起）。")
            return
        try:
            exp = datetime.fromisoformat(info.get("expires_at")).astimezone(TZ)
        except:
            exp = now_tz() - timedelta(seconds=1)

        if now_tz() > exp:
            p.pop("confirm_all_clear", None)
            save_pending(p)
            reply(event, "⏳ 確認已過期，請重新輸入：王表清除")
            return

        data = load_data()
        data["boss"] = {}
        save_data(data)
        p.pop("confirm_all_clear", None)
        save_pending(p)
        reply(event, "🧹 已清空所有 Boss 時間紀錄。")
        return

    # 5) 單隻清除：Boss清除
    boss_part = extract_clear_single(text)
    if boss_part:
        canon, multi = fuzzy_find_boss(boss_part)
        if multi:
            reply(event, "🤔 命中多個 Boss，請再縮小：\n" + "\n".join([f"• {x}" for x in multi]))
            return
        if not canon:
            # 沒命中：不出聲（依你要求）
            return

        data = load_data()
        boss_data = data.get("boss", {})
        if canon in boss_data:
            boss_data.pop(canon, None)
            data["boss"] = boss_data
            save_data(data)
            reply(event, f"🧹 已清除的時間紀錄。")
        else:
            reply(event, f"📭目前沒有紀錄可清除。")
        return

    # 6) 指定重生：Boss1400出（不加週期）
    spawn = parse_spawn_cmd(text)
    if spawn:
        boss_raw, (hh, mm) = spawn
        canon, multi = fuzzy_find_boss(boss_raw)
        if multi:
            reply(event, "🤔 命中多個 Boss，請再縮小：\n" + "\n".join([f"• {x}" for x in multi]))
            return
        if not canon:
            return

        now = now_tz()
        respawn_dt = compute_next_spawn_by_clock(hh, mm, now)

        data = load_data()
        boss_data = data.get("boss", {})
        boss_data[canon] = {
            "respawn": respawn_dt.isoformat(),
            "last_notified": "",
            "mode": "spawn"
        }
        data["boss"] = boss_data
        save_data(data)

        left = respawn_dt - now
        reply(event, f"🐣指定重生已登記\n下一次重生：{fmt_dt(respawn_dt)}\n剩餘 {fmt_left(left)}\n（重生前 {REMIND_BEFORE_MIN} 分鐘提醒）")
        return

    # 7) 死亡時間：Boss1430（加週期，會往未來推）
    death = parse_death_cmd(text)
    if death:
        boss_raw, (hh, mm) = death
        canon, multi = fuzzy_find_boss(boss_raw)
        if multi:
            reply(event, "🤔 命中多個 Boss，請再縮小：\n" + "\n".join([f"• {x}" for x in multi]))
            return
        if not canon:
            return

        now = now_tz()
        respawn_dt = compute_respawn_from_death(hh, mm, canon, now)

        data = load_data()
        boss_data = data.get("boss", {})
        boss_data[canon] = {
            "respawn": respawn_dt.isoformat(),
            "last_notified": "",
            "mode": "death"
        }
        data["boss"] = boss_data
        save_data(data)

        left = respawn_dt - now
        reply(event, f"☠️死亡時間已登記\n下一次重生：{fmt_dt(respawn_dt)}\n剩餘 {fmt_left(left)}\n（重生前 {REMIND_BEFORE_MIN} 分鐘提醒）")
        return

    # ✅ 其他任何沒命中指令/格式：完全不出聲（避免干擾群組）
    return


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
