import os
import json
import re
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage


# =========================
# 基本設定（Render 用環境變數）
# =========================
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN", "").strip()
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET", "").strip()
TZ_NAME = os.getenv("TZ", "Asia/Taipei").strip()

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("Missing CHANNEL_ACCESS_TOKEN or CHANNEL_SECRET in environment variables.")

TZ = ZoneInfo(TZ_NAME)

app = Flask(__name__)
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)


# =========================
# 資料儲存（Render 建議掛 Persistent Disk 到 /var/data）
# =========================
DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_PATH = os.path.join(DATA_DIR, "boss_data.json")
PENDING_CLEAR_PATH = os.path.join(DATA_DIR, "pending_clear.json")


REMIND_BEFORE_MIN = 5
WARNING_BEFORE_MIN = 30          # ✅ 30 分鐘反紅
EXPIRE_GRACE_MIN = 3             # ✅ 超過重生 +3 分鐘就自動清除
CHECK_INTERVAL_SEC = 20


# =========================
# Boss 表（正式名 + 分鐘 + 別名）
# =========================
BOSS_TABLE = [
    ("巨大鱷魚", 60, ["鱷魚"]),
    ("單飛龍", 180, ["單龍"]),
    ("雙飛龍", 180, ["雙龍"]),
    ("黑長者", 240, ["黑老"]),
    ("克特", 360, []),
    ("四色", 180, []),
    ("魔法師", 180, []),
    ("死亡騎士", 360, ["死騎", "死"]),
    ("巴風特", 240, ["小巴"]),
    ("巴列斯", 240, ["大巴"]),
    ("巨蟻女皇", 360, ["螞蟻"]),
    ("變形怪首領", 300, ["變怪"]),
    ("伊佛利特", 180, ["EF"]),
    ("不死鳥", 360, ["鳥", "不死"]),
    ("冰之女王", 360, ["冰女"]),
    ("惡魔", 360, []),
    ("古代巨人", 360, ["古巨"]),
    ("反王肯恩", 240, ["反王"]),
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
    ("土精靈王", 120, ["土"]),
    ("水精靈王", 120, ["水"]),
    ("風精靈王", 120, ["風"]),
    ("火精靈王", 120, ["火"]),
    ("獨角獸", 360, []),
    ("曼波兔(海賊島)", 360, ["海賊兔", "海賊"]),
    ("庫曼", 360, []),
    ("德雷克", 180, []),
    ("曼波兔(精靈墓穴)", 360, ["墓穴兔", "墓穴"]),
    ("深淵之主", 360, ["深淵"]),
    ("須曼", 360, []),
    ("安塔瑞斯", 720, []),
    ("巴拉卡斯", 720, []),
    ("法利昂", 720, []),
    ("林德拜爾", 720, []),
]

BOSS_RESPAWN_MIN = {name: mins for (name, mins, _) in BOSS_TABLE}

ALIAS_TO_OFFICIAL = {}
OFFICIAL_NAMES = []
for name, mins, aliases in BOSS_TABLE:
    OFFICIAL_NAMES.append(name)
    ALIAS_TO_OFFICIAL[name] = name
    for a in aliases:
        ALIAS_TO_OFFICIAL[a] = name


# =========================
# 工具：讀寫資料
# =========================
LOCK = threading.Lock()

def load_data():
    if not os.path.exists(DATA_PATH):
        return {"boss": {}, "targets": []}
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"boss": {}, "targets": []}

def save_data(data):
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_PATH)

def load_pending_clear():
    if not os.path.exists(PENDING_CLEAR_PATH):
        return {}
    try:
        with open(PENDING_CLEAR_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_pending_clear(obj):
    tmp = PENDING_CLEAR_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PENDING_CLEAR_PATH)

def now_tz():
    return datetime.now(tz=TZ)

def fmt_hhmm(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%H:%M")

def parse_hhmm(s: str):
    m = re.fullmatch(r"([01]\d|2[0-3])([0-5]\d)", s)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))

def ensure_future(dt: datetime) -> datetime:
    n = now_tz()
    if dt < n - timedelta(minutes=1):
        dt = dt + timedelta(days=1)
    return dt

def compute_next_respawn_from_death(boss: str, death_dt: datetime) -> datetime:
    mins = BOSS_RESPAWN_MIN[boss]
    return death_dt + timedelta(minutes=mins)

def remaining_minutes(respawn_dt: datetime) -> int:
    n = now_tz()
    diff = respawn_dt - n
    return int(diff.total_seconds() // 60)

def compute_remaining_str(respawn_dt: datetime) -> str:
    mins = remaining_minutes(respawn_dt)
    if mins <= 0:
        return "00h00m"
    h = mins // 60
    m = mins % 60
    return f"{h:02d}h{m:02d}m"

def urgency_badge(respawn_dt: datetime) -> str:
    mins = remaining_minutes(respawn_dt)
    # ✅ 30 分鐘內反紅
    if mins <= 0:
        return "✅"  # 到點了（但通常會自動清除）
    if mins <= REMIND_BEFORE_MIN:
        return "🟥🟥🔔"
    if mins <= WARNING_BEFORE_MIN:
        return "🟥"
    return "🟩"

def normalize_text(t: str) -> str:
    t = t.replace("　", " ").strip()
    t = re.sub(r"\s+", " ", t)
    return t


# =========================
# Boss 模糊搜尋
# =========================
def match_boss(keyword: str):
    keyword = keyword.strip()
    if not keyword:
        return None, []
    if keyword in ALIAS_TO_OFFICIAL:
        return ALIAS_TO_OFFICIAL[keyword], []
    hits = set()
    for alias, official in ALIAS_TO_OFFICIAL.items():
        if keyword in alias:
            hits.add(official)
    hits = sorted(list(hits))
    if len(hits) == 1:
        return hits[0], []
    if len(hits) >= 2:
        return None, hits
    return None, []


HELP_TEXT = """✨【可用指令】✨
1) 王 😈：列出所有 Boss 名稱（只顯示正式名）
2) 王出 ⏰：只顯示「已登記」的 Boss 下一次重生（30 分鐘內🟥）
3) 死亡時間 ☠️：Boss1430 / Boss 1430
   → 代表 Boss 14:30 死亡，會自動算下一次重生
4) 指定重生 🐣：Boss1400出 / Boss 1400出
   → 代表 Boss 14:00 重生（先記 14:00，不會先 + 週期）
5) 清除單隻 🧹：Boss清除 / Boss 清除
6) 清空全部 ⚠️：王表清除（需再輸入「王表確認」才會清空）
7) 查詢 📌：顯示本訊息

🔎 小技巧：Boss 可打縮寫/單字（例：輸入「鳥」可找不死鳥；若命中多個會請你縮小）
"""


TIME_RE = re.compile(r"^(?P<boss>.+?)[ ]*(?P<time>(?:[01]\d|2[0-3])[0-5]\d)(?P<out>出)?$")
DEATH_SPLIT_RE = re.compile(r"^(?P<boss>.+?)[ ]*(?P<time>(?:[01]\d|2[0-3])[0-5]\d)$")
CLEAR_ONE_RE = re.compile(r"^(?P<boss>.+?)[ ]*清除$")


def reply_text(token: str, text: str):
    line_bot_api.reply_message(token, TextSendMessage(text=text))


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    text_raw = event.message.text or ""
    text = normalize_text(text_raw)

    # ✅ 沒打到關鍵字就不要回（避免洗版）
    is_candidate = (
        text in ["王", "王出", "查詢", "王表清除", "王表確認"]
        or CLEAR_ONE_RE.match(text) is not None
        or TIME_RE.match(text) is not None
        or DEATH_SPLIT_RE.match(text) is not None
    )
    if not is_candidate:
        return

    if text == "查詢":
        reply_text(event.reply_token, HELP_TEXT)
        return

    if text == "王":
        msg = "😈【Boss 清單】😈\n" + "\n".join([f"・{n}" for n in OFFICIAL_NAMES])
        reply_text(event.reply_token, msg)
        return

    with LOCK:
        data = load_data()

    if text == "王出":
        boss_data = data.get("boss", {})
        items = []
        for boss, rec in boss_data.items():
            respawn_iso = rec.get("respawn")
            if not respawn_iso:
                continue
            try:
                respawn_dt = datetime.fromisoformat(respawn_iso).astimezone(TZ)
            except:
                continue

            # ✅ 如果已過期（理論上背景會清掉），這裡也做一次保險：略過
            if now_tz() > respawn_dt + timedelta(minutes=EXPIRE_GRACE_MIN):
                continue

            items.append((boss, respawn_dt))

        if not items:
            reply_text(event.reply_token, "⏰ 目前沒有已登記的王喔～\n先用：Boss1430 或 Boss1400出")
            return

        items.sort(key=lambda x: x[1])
        lines = ["⏰【已登記王出】⏰"]
        for boss, respawn_dt in items:
            badge = urgency_badge(respawn_dt)
            lines.append(f"{badge} {boss}：{fmt_hhmm(respawn_dt)}（剩 {compute_remaining_str(respawn_dt)}）")
        reply_text(event.reply_token, "\n".join(lines))
        return

    if text == "王表清除":
        pending = load_pending_clear()
        key = str(event.source.group_id or event.source.user_id or "default")
        pending[key] = {"ts": now_tz().isoformat()}
        save_pending_clear(pending)
        reply_text(event.reply_token, "⚠️ 你確定要清空全部王表嗎？\n請在 2 分鐘內輸入：王表確認")
        return

    if text == "王表確認":
        pending = load_pending_clear()
        key = str(event.source.group_id or event.source.user_id or "default")
        rec = pending.get(key)
        if not rec:
            reply_text(event.reply_token, "❗找不到清空請求，請先輸入：王表清除")
            return
        try:
            ts = datetime.fromisoformat(rec["ts"])
        except:
            ts = now_tz() - timedelta(hours=1)

        if now_tz() - ts > timedelta(minutes=2):
            pending.pop(key, None)
            save_pending_clear(pending)
            reply_text(event.reply_token, "⏳ 超過 2 分鐘，已取消清空。")
            return

        with LOCK:
            data = load_data()
            data["boss"] = {}
            save_data(data)

        pending.pop(key, None)
        save_pending_clear(pending)
        reply_text(event.reply_token, "✅ 王表已清空完成！")
        return

    m_clear = CLEAR_ONE_RE.match(text)
    if m_clear:
        boss_kw = m_clear.group("boss").strip()
        official, hits = match_boss(boss_kw)
        if hits:
            msg = "🤔 命中多個 Boss，請再縮小：\n" + "\n".join([f"・{h}" for h in hits[:8]])
            reply_text(event.reply_token, msg)
            return
        if not official:
            reply_text(event.reply_token, f"❗找不到 Boss：「{boss_kw}」\n可輸入「王」查看清單")
            return

        with LOCK:
            data = load_data()
            boss_data = data.get("boss", {})
            if official in boss_data:
                boss_data.pop(official, None)
                data["boss"] = boss_data
                save_data(data)
                reply_text(event.reply_token, f"🧹 已清除：{official}")
            else:
                reply_text(event.reply_token, f"🧹 {official} 本來就沒有登記時間")
        return

    m = TIME_RE.match(text)
    if not m:
        m2 = DEATH_SPLIT_RE.match(text)
        if not m2:
            return
        boss_kw = m2.group("boss").strip()
        hhmm = m2.group("time")
        is_out = False
    else:
        boss_kw = m.group("boss").strip()
        hhmm = m.group("time")
        is_out = (m.group("out") is not None)

    official, hits = match_boss(boss_kw)
    if hits:
        msg = "🤔 命中多個 Boss，請再縮小：\n" + "\n".join([f"・{h}" for h in hits[:8]])
        reply_text(event.reply_token, msg)
        return
    if not official:
        reply_text(event.reply_token, f"❗找不到 Boss：「{boss_kw}」\n可輸入「王」查看清單")
        return

    hhmm_parsed = parse_hhmm(hhmm)
    if not hhmm_parsed:
        reply_text(event.reply_token, "⛔ 時間格式錯誤，請用 4 碼 0000~2359，例如：1430 / 0100")
        return

    hh, mm = hhmm_parsed
    n = now_tz()
    base_dt = n.replace(hour=hh, minute=mm, second=0, microsecond=0)
    base_dt = ensure_future(base_dt)

    with LOCK:
        data = load_data()
        boss_data = data.get("boss", {})

        if is_out:
            respawn_dt = base_dt
            boss_data[official] = {
                "respawn": respawn_dt.isoformat(),
                "last_notified": "",
                "mode": "respawn"
            }
            data["boss"] = boss_data
            save_data(data)

            badge = urgency_badge(respawn_dt)
            reply_text(
                event.reply_token,
                f"{badge} 🐣 已設定重生：{fmt_hhmm(respawn_dt)}\n"
                f"⏳ 剩 {compute_remaining_str(respawn_dt)}（前 {REMIND_BEFORE_MIN} 分鐘提醒）"
            )
            return

        death_dt = base_dt
        respawn_dt = compute_next_respawn_from_death(official, death_dt)
        boss_data[official] = {
            "respawn": respawn_dt.isoformat(),
            "last_notified": "",
            "mode": "death"
        }
        data["boss"] = boss_data
        save_data(data)

    badge = urgency_badge(respawn_dt)
    reply_text(
        event.reply_token,
        f"☠️ 已登記死亡：{fmt_hhmm(death_dt)}\n"
        f"{badge} ⏰ 下一次重生：{fmt_hhmm(respawn_dt)}\n"
        f"⏳ 剩 {compute_remaining_str(respawn_dt)}（前 {REMIND_BEFORE_MIN} 分鐘提醒）"
    )


# =========================
# 推播目標（群組/個人）
# =========================
def push_to_targets(text: str):
    with LOCK:
        data = load_data()
        targets = data.get("targets", [])

    for tid in targets:
        try:
            line_bot_api.push_message(tid, TextSendMessage(text=text))
        except:
            pass

def ensure_targets(event):
    tid = None
    if event.source.type == "group":
        tid = event.source.group_id
    elif event.source.type == "room":
        tid = event.source.room_id
    else:
        tid = event.source.user_id

    if not tid:
        return

    with LOCK:
        data = load_data()
        targets = data.get("targets", [])
        if tid not in targets:
            targets.append(tid)
            data["targets"] = targets
            save_data(data)

@handler.add(MessageEvent)
def handle_any_event(event):
    try:
        ensure_targets(event)
    except:
        pass


# =========================
# 背景提醒 + 過期自動清除
# =========================
def reminder_loop():
    while True:
        try:
            with LOCK:
                data = load_data()
                boss_data = data.get("boss", {})
                targets = data.get("targets", [])

            if not targets or not boss_data:
                threading.Event().wait(CHECK_INTERVAL_SEC)
                continue

            n = now_tz()
            changed = False

            for boss, rec in list(boss_data.items()):
                respawn_iso = rec.get("respawn")
                if not respawn_iso:
                    continue
                try:
                    respawn_dt = datetime.fromisoformat(respawn_iso).astimezone(TZ)
                except:
                    continue

                # ✅ 超過重生時間後自動清除（+1分鐘緩衝）
                if n > respawn_dt + timedelta(minutes=EXPIRE_GRACE_MIN):
                    boss_data.pop(boss, None)
                    changed = True
                    continue

                remind_at = respawn_dt - timedelta(minutes=REMIND_BEFORE_MIN)

                # 到提醒區間：推一次
                if remind_at <= n <= respawn_dt:
                    key = respawn_dt.isoformat()
                    if rec.get("last_notified", "") != key:
                        msg = (
                            f"🟥🟥🔔【快重生】{boss}\n"
                            f"⏰ {fmt_hhmm(respawn_dt)}（剩 {compute_remaining_str(respawn_dt)}）"
                        )
                        push_to_targets(msg)
                        rec["last_notified"] = key
                        boss_data[boss] = rec
                        changed = True

            if changed:
                with LOCK:
                    data = load_data()
                    data["boss"] = boss_data
                    save_data(data)

        except:
            pass

        threading.Event().wait(CHECK_INTERVAL_SEC)


threading.Thread(target=reminder_loop, daemon=True).start()


@app.route("/", methods=["GET"])
def health():
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
