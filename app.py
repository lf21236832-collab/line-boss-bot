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
TZ_NAME = os.getenv("TZ", "Asia/Taipei")
TZ = ZoneInfo(TZ_NAME)

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET", "")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("缺少環境變數：CHANNEL_ACCESS_TOKEN / CHANNEL_SECRET")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# Render 建議掛載 persistent disk，例如 /var/data
DATA_PATH = os.getenv("DATA_PATH", "/var/data/boss_data.json")

# 提醒設定
REMIND_BEFORE_MIN = 5
CHECK_EVERY_SEC = 20  # 背景檢查間隔

# =========================
# Boss 表（分鐘）
# 括號內為別名：可輸入但查詢列表不顯示
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
    ("林德拜爾", 720, []),  # 你原本寫 720分，這裡統一 720 分鐘
]

BOSS_RESPAWN_MIN = {name: minutes for name, minutes, _ in BOSS_TABLE}

# 別名索引
ALIAS_TO_BOSS = {}
for name, _, aliases in BOSS_TABLE:
    for a in aliases:
        ALIAS_TO_BOSS[a] = name


# =========================
# 資料存取（JSON）
# data 結構：
# {
#   "targets": {
#      "<target_id>": {
#          "boss": {
#             "<boss_name>": {"respawn": "<iso>", "set_by": "death/spec", "last_notified": "<iso or ''>"}
#          },
#          "pending_clear_until": "<iso or ''>"
#      }
#   }
# }
# =========================
_lock = threading.Lock()


def _ensure_dir():
    d = os.path.dirname(DATA_PATH)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def load_data():
    _ensure_dir()
    if not os.path.exists(DATA_PATH):
        return {"targets": {}}
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"targets": {}}


def save_data(data):
    _ensure_dir()
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_PATH)


def get_target_id(event):
    src = event.source
    if hasattr(src, "group_id") and src.group_id:
        return src.group_id
    if hasattr(src, "room_id") and src.room_id:
        return src.room_id
    return src.user_id


def ensure_target(data, target_id):
    if "targets" not in data:
        data["targets"] = {}
    if target_id not in data["targets"]:
        data["targets"][target_id] = {"boss": {}, "pending_clear_until": ""}
    if "boss" not in data["targets"][target_id]:
        data["targets"][target_id]["boss"] = {}
    if "pending_clear_until" not in data["targets"][target_id]:
        data["targets"][target_id]["pending_clear_until"] = ""


# =========================
# 時間/解析
# =========================
def now_tz():
    return datetime.now(TZ)


def ensure_tz(dt: datetime) -> datetime:
    # 如果沒時區，補上；如果有時區，轉到本地時區
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def parse_hhmm(token: str):
    """
    支援 1430 / 14:30 / 930 / 09:30
    回傳 (hh, mm) 或 None
    """
    token = token.strip()
    if re.fullmatch(r"\d{3,4}", token):
        if len(token) == 3:
            hh = int(token[0])
            mm = int(token[1:])
        else:
            hh = int(token[:2])
            mm = int(token[2:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", token)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    return None


def smart_day_datetime(hh: int, mm: int) -> datetime:
    """
    依照現在時間推斷是今天還是昨天：
    - 若輸入時間比現在「晚很多」（>5分鐘），多半是在補登剛剛過去的王 → 當作昨天
    - 其餘 → 今天
    """
    n = now_tz()
    candidate = n.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate > n + timedelta(minutes=5):
        candidate = candidate - timedelta(days=1)
    return candidate


def roll_respawn_to_future(boss: str, respawn_dt: datetime) -> datetime:
    """
    顯示/提醒用：如果時間已過，就往後加週期直到未來
    """
    respawn_dt = ensure_tz(respawn_dt)
    interval = timedelta(minutes=BOSS_RESPAWN_MIN.get(boss, 0))
    if interval.total_seconds() <= 0:
        return respawn_dt
    n = now_tz()
    # 避免 while 卡太久：最多跳 500 次
    for _ in range(500):
        if respawn_dt >= n:
            break
        respawn_dt += interval
    return respawn_dt


def remain_text(respawn_dt: datetime) -> str:
    n = now_tz()
    respawn_dt = ensure_tz(respawn_dt)
    delta = respawn_dt - n
    sec = int(delta.total_seconds())
    if sec < 0:
        sec = 0
    mins = sec // 60
    h = mins // 60
    m = mins % 60
    if h <= 0:
        return f"{m}m"
    return f"{h}h{m:02d}m"


# =========================
# Boss 名稱解析（支援別名/模糊）
# =========================
def normalize_text(s: str) -> str:
    return re.sub(r"\s+", "", s.strip())


def resolve_boss(query: str):
    """
    回傳 (boss_name or None, suggestions[list])
    - 先精準：正名/別名完全相等
    - 再模糊：包含關係（可打 1~2 字）
    """
    q = query.strip()
    if not q:
        return None, []

    # 先做去空白版本
    qn = normalize_text(q)

    # 精準：正名
    for name, _, _ in BOSS_TABLE:
        if normalize_text(name) == qn:
            return name, []

    # 精準：別名
    for alias, name in ALIAS_TO_BOSS.items():
        if normalize_text(alias) == qn:
            return name, []

    # 模糊：包含
    hits = []
    for name, _, aliases in BOSS_TABLE:
        if q in name:
            hits.append(name)
            continue
        for a in aliases:
            if q == a or q in a:
                hits.append(name)
                break

    # 去重保持順序
    uniq = []
    for x in hits:
        if x not in uniq:
            uniq.append(x)

    if len(uniq) == 1:
        return uniq[0], []
    return None, uniq[:10]


# =========================
# 指令說明（含小表情）
# =========================
def help_text():
    return (
        "✨【可用指令】✨\n"
        "1) 王 😈：列出所有Boss名稱（只顯示正式名）\n"
        "2) 王出 ⏰：只顯示「已登記」的Boss下一次重生\n"
        "3) 死亡時間 ☠️：Boss1430 / Boss 1430\n"
        "   → 代表 Boss 14:30 死亡，會自動算下一次重生\n"
        "4) 指定重生 🐣：Boss1400出 / Boss 1400出\n"
        "   → 代表 Boss 14:00 重生（先記下 14:00，不會先+週期）\n"
        "5) 清除單隻 🧹：Boss清除\n"
        "6) 清空全部 ⚠️：王表清除（需要二次確認）\n"
        "7) 查詢 📌：顯示本說明\n"
        "🌟小技巧：可用簡稱/一兩個字，例如「鳥」= 不死鳥（若命中多個會請你縮小）"
    )


# =========================
# Flask / Webhook
# =========================
app = Flask(__name__)


@app.route("/health", methods=["GET"])
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
# 背景提醒：5分鐘前推播
# =========================
def reminder_loop():
    while True:
        try:
            with _lock:
                data = load_data()
                targets = data.get("targets", {})
                changed = False

                for target_id, tdata in targets.items():
                    boss_map = tdata.get("boss", {})
                    if not boss_map:
                        continue

                    for boss, rec in list(boss_map.items()):
                        if boss not in BOSS_RESPAWN_MIN:
                            continue
                        iso = (rec or {}).get("respawn", "")
                        if not iso:
                            continue

                        try:
                            respawn_dt = ensure_tz(datetime.fromisoformat(iso))
                        except:
                            continue

                        # 顯示/提醒用時間（滾到未來）
                        future_respawn = roll_respawn_to_future(boss, respawn_dt)
                        remind_at = future_respawn - timedelta(minutes=REMIND_BEFORE_MIN)

                        n = now_tz()
                        if remind_at <= n <= future_respawn:
                            key = future_respawn.isoformat()
                            last = (rec or {}).get("last_notified", "")
                            if last != key:
                                msg = (
                                    f"🔔【5分鐘提醒】\n"
                                    f"👑 {boss} 快重生啦！\n"
                                    f"⏳ 目標：{future_respawn.strftime('%H:%M')}\n"
                                    f"⚡ 剩餘：{remain_text(future_respawn)}"
                                )
                                try:
                                    line_bot_api.push_message(target_id, TextSendMessage(text=msg))
                                    boss_map[boss]["last_notified"] = key
                                    changed = True
                                except:
                                    # 可能是沒開 push 權限或 bot 不在群
                                    pass

                    tdata["boss"] = boss_map

                if changed:
                    save_data(data)

        except:
            pass

        time.sleep(CHECK_EVERY_SEC)


threading.Thread(target=reminder_loop, daemon=True).start()


# =========================
# 訊息處理（重點：沒命中指令就沉默）
# =========================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = (event.message.text or "").strip()
    if not text:
        return

    target_id = get_target_id(event)

    with _lock:
        data = load_data()
        ensure_target(data, target_id)
        tdata = data["targets"][target_id]
        boss_map = tdata["boss"]

    # 只在命中關鍵字/格式時才回覆，否則完全不出聲 ✅
    # 允許的指令：王 / 王出 / 查詢 / 王表清除 / 王表確認清除 / Boss清除 / Boss時間 / Boss時間出

    # 查詢/說明
    if text in ("查詢", "help", "指令"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text()))
        return

    # 列出 Boss 名稱
    if text == "王":
        names = [name for name, _, _ in BOSS_TABLE]
        msg = "😈【Boss清單】😈\n" + "\n".join([f"👑 {n}" for n in names])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # 防誤刪：清空全部（第一次）
    if text == "王表清除":
        until = (now_tz() + timedelta(seconds=60)).isoformat()
        with _lock:
            data = load_data()
            ensure_target(data, target_id)
            data["targets"][target_id]["pending_clear_until"] = until
            save_data(data)

        msg = (
            "⚠️【王表清除】⚠️\n"
            "你確定要清空『全部Boss時間』嗎？\n"
            "✅ 請在 60 秒內再輸入：王表確認清除\n"
            "❌ 取消就輸入：取消"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # 防誤刪：確認清空
    if text == "王表確認清除":
        with _lock:
            data = load_data()
            ensure_target(data, target_id)
            until = data["targets"][target_id].get("pending_clear_until", "")
            ok = False
            if until:
                try:
                    ok = now_tz() <= ensure_tz(datetime.fromisoformat(until))
                except:
                    ok = False

            if ok:
                data["targets"][target_id]["boss"] = {}
                data["targets"][target_id]["pending_clear_until"] = ""
                save_data(data)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🧹 已清空全部Boss時間 ✅"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⏳ 已超時或未發起清除，請重新輸入：王表清除"))
        return

    # 取消清空
    if text == "取消":
        with _lock:
            data = load_data()
            ensure_target(data, target_id)
            data["targets"][target_id]["pending_clear_until"] = ""
            save_data(data)
        # 這個也算命中指令才回覆
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 已取消"))
        return

    # 王出：只顯示已登記的王
    if text == "王出":
        with _lock:
            data = load_data()
            ensure_target(data, target_id)
            boss_map = data["targets"][target_id].get("boss", {})

        rows = []
        for boss, rec in boss_map.items():
            if boss not in BOSS_RESPAWN_MIN:
                continue
            iso = (rec or {}).get("respawn", "")
            if not iso:
                continue
            try:
                rdt = ensure_tz(datetime.fromisoformat(iso))
                rdt = roll_respawn_to_future(boss, rdt)
                rows.append((rdt, boss))
            except:
                continue

        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="😴 目前沒有任何Boss已登記時間。"))
            return

        rows.sort(key=lambda x: x[0])
        lines = ["⏰【王出 / 已登記的下一次重生】⏰"]
        for rdt, boss in rows:
            lines.append(f"👑 {boss}：{rdt.strftime('%H:%M')}（剩 {remain_text(rdt)}）")

        reply = "\n".join(lines)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 清除單隻：Boss清除
    if text.endswith("清除") and text not in ("王表清除", "王表確認清除"):
        q = text[:-2].strip()
        boss, suggestions = resolve_boss(q)
        if not boss and suggestions:
            msg = "🤔 命中多個Boss，請再縮小：\n" + "\n".join([f"• {s}" for s in suggestions])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
            return
        if not boss:
            # 不命中 → 沉默（避免洗版）
            return

        with _lock:
            data = load_data()
            ensure_target(data, target_id)
            boss_map = data["targets"][target_id].get("boss", {})
            if boss in boss_map:
                boss_map.pop(boss, None)
                data["targets"][target_id]["boss"] = boss_map
                save_data(data)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🧹 已清除：{boss} ✅"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"ℹ️ {boss} 目前沒有紀錄可清除"))
        return

    # =========================
    # 解析兩種時間指令：
    # 1) 死亡時間：Boss1430 / Boss 1430
    # 2) 指定重生：Boss1400出 / Boss 1400出
    #
    # 支援 1430 / 14:30
    # =========================
    is_spec = False
    raw = text

    if raw.endswith("出"):
        is_spec = True
        raw = raw[:-1].strip()

    # 找最後的時間 token
    m = re.match(r"^(.*?)(\d{1,2}:?\d{2})$", raw.replace(" ", ""))
    if not m:
        # 沒命中任何指令/格式 → 完全沉默 ✅
        return

    boss_part = m.group(1).strip()
    time_part = m.group(2).strip()

    boss, suggestions = resolve_boss(boss_part)
    if not boss and suggestions:
        msg = "🤔 命中多個Boss，請再縮小：\n" + "\n".join([f"• {s}" for s in suggestions])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return
    if not boss:
        # 找不到 Boss → 回覆一次（這種是你在用指令時才會發生）
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 找不到 Boss：『{boss_part}』，輸入「王」看清單"))
        return

    hm = parse_hhmm(time_part)
    if not hm:
        # 時間格式錯誤才回
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⛔ 時間格式錯誤，請用 1430 或 14:30"))
        return
    hh, mm = hm

    base_dt = smart_day_datetime(hh, mm)

    if is_spec:
        # 指定重生：記下「重生時間點」(不先 + 週期)
        respawn_dt = base_dt
        set_by = "spec"
        tip = "🐣 指定重生"
    else:
        # 死亡時間：自動 + 週期
        interval = timedelta(minutes=BOSS_RESPAWN_MIN[boss])
        respawn_dt = base_dt + interval
        set_by = "death"
        tip = "☠️ 死亡時間"

    # 存檔
    with _lock:
        data = load_data()
        ensure_target(data, target_id)
        boss_map = data["targets"][target_id].get("boss", {})
        boss_map[boss] = {
            "respawn": ensure_tz(respawn_dt).isoformat(),
            "set_by": set_by,
            "last_notified": ""
        }
        data["targets"][target_id]["boss"] = boss_map
        save_data(data)

    # 顯示用：滾到未來（避免剩餘時間亂）
    show_dt = roll_respawn_to_future(boss, respawn_dt)
    msg = (
        f"✅ {tip} 登記成功 🎉\n"
        f"👑 Boss：{boss}\n"
        f"⏰ 下一次重生：{show_dt.strftime('%H:%M')}\n"
        f"⏳ 剩餘：{remain_text(show_dt)}\n"
        f"🔔 會在重生前 {REMIND_BEFORE_MIN} 分鐘提醒"
    )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
    return


if __name__ == "__main__":
    # 本機測試用
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
