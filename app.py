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
    "潔尼斯女王": 360,
    "幻象眼魔": 360,
    "吸血鬼": 360,
    "殭屍王": 360,
    "黑豹": 360,
    "木乃伊王": 360,
    "艾莉絲": 360,
    "騎士范德": 360,
    "巫妖": 360,
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
# 別名（你可輸入，但不顯示在王/王出）
# 你說「括號裡的是代表我輸入那些名字也可以」
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
CLEAR_CONFIRM_TTL_SEC = 60  # 王表清除確認有效秒數


# =========================
# 基本工具
# =========================
def normalize_text(s: str) -> str:
    return s.strip().replace("　", " ").replace("：", ":")


def load_data():
    if not os.path.exists(DATA_FILE):
        # boss[boss] = {"respawn": iso_str, "last_notified": iso_str or ""}
        # targets = [groupId/userId...]
        # pending_clear = { target_id: epoch_seconds_deadline }
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


def next_occurrence_today_or_tomorrow(hh: int, mm: int) -> datetime:
    """把 HH:MM 放到今天；若已過就放到明天同一時間"""
    now = datetime.now()
    dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if dt <= now:
        dt += timedelta(days=1)
    return dt


def roll_respawn_to_future(boss: str, respawn_dt: datetime) -> datetime:
    """若重生時間已過，按週期往後滾到未來"""
    interval = timedelta(minutes=BOSS_RESPAWN_MIN[boss])
    now = datetime.now()
    while respawn_dt <= now:
        respawn_dt += interval
    return respawn_dt


# =========================
# Boss 名稱解析（含別名 + 模糊匹配）
# =========================
def all_boss_names_for_matching():
    # 回傳 (name, canonical) 列表：包含正式名與別名
    pairs = []
    for canon in BOSS_RESPAWN_MIN.keys():
        pairs.append((canon, canon))
    for alias, canon in ALIASES_TO_CANON.items():
        pairs.append((alias, canon))
    return pairs


def resolve_boss(query: str):
    """
    回傳：
      (canonical_name, None) 若唯一匹配
      (None, [候選canonical...]) 若多個候選
      (None, []) 若找不到
    """
    q = normalize_text(query)
    if not q:
        return None, []

    # 1) 完全相等：正式名
    if q in BOSS_RESPAWN_MIN:
        return q, None

    # 2) 完全相等：別名
    if q in ALIASES_TO_CANON:
        return ALIASES_TO_CANON[q], None

    # 3) 模糊：包含關鍵字（在正式名或別名中）
    candidates = []
    seen = set()
    for name, canon in all_boss_names_for_matching():
        if q in name:
            if canon not in seen:
                seen.add(canon)
                candidates.append(canon)

    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        # 依照「最短名稱優先」讓常用的較前面，再用字典序穩定排序
        candidates.sort(key=lambda x: (len(x), x))
        return None, candidates

    return None, []


def format_candidates(cands):
    # 最多列 12 個避免太長
    show = cands[:12]
    lines = ["我找到多個可能："]
    for i, c in enumerate(show, 1):
        lines.append(f"{i}) {c}")
    if len(cands) > 12:
        lines.append(f"...還有 {len(cands)-12} 個")
    lines.append("請再多打 1~2 個字，或直接打完整名稱。")
    return "\n".join(lines)


# =========================
# 解析指令
# =========================
def extract_boss_clear(text: str):
    """
    支援：
      鳥清除 / 鳥 清除
      小巴清除 / 小巴 清除
    回傳 (boss_query) 或 None
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
    支援（Boss 可模糊、可別名）：
      鳥1430
      鳥 1430
      鳥1400出
      鳥 1400出
      鳥14:30出
    回傳 (boss_query, time_token, is_respawn_input) 或 (None,None,False)
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

    # 無空格：最後抓時間 + 可選出
    # 允許 4 碼 or H:MM (1~2位小時)
    # 例：鳥1430出、鳥14:30出
    for suffix in ("出", ""):
        if suffix and not text.endswith(suffix):
            continue
        core = text[:-1] if suffix else text
        # 嘗試 4 碼
        if len(core) >= 4 and core[-4:].isdigit():
            boss_q = core[:-4].strip()
            token = core[-4:]
            if boss_q:
                return boss_q, token, bool(suffix)
        # 嘗試 : 格式（從尾端找最後一個冒號）
        # 例：鳥14:30
        if ":" in core:
            # 取最後 5 個字符形如 4:30 或 14:30（4或5長）
            tail5 = core[-5:]
            tail4 = core[-4:]
            if len(tail5) == 5 and tail5[2] == ":" and tail5[:2].isdigit() and tail5[3:].isdigit():
                boss_q = core[:-5].strip()
                token = tail5
                if boss_q:
                    return boss_q, token, bool(suffix)
            if len(tail4) == 4 and tail4[1] == ":" and tail4[0].isdigit() and tail4[2:].isdigit():
                boss_q = core[:-4].strip()
                token = tail4
                if boss_q:
                    return boss_q, token, bool(suffix)

    return None, None, False


def cmd_help_text():
    return (
        "【可用指令】\n"
        "1) 王：列出所有Boss名稱（正式名）\n"
        "2) 王出：顯示所有Boss下一次重生時間\n"
        "3) 死亡時間：Boss1430 / Boss 1430\n"
        "4) 指定重生：Boss1400出 / Boss 1400出（重生就是14:00，不往後推）\n"
        "5) 清空全部：王表清除（需再輸入「確認清除」）\n"
        "6) 清除單隻：Boss清除 / Boss 清除\n"
        "7) 查詢：顯示本訊息\n"
        "（Boss 名稱可模糊：例如輸入「鳥」會找不死鳥；若命中多個會請你再縮小）"
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

                    # 自動把下一次重生滾到未來
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
                                f"⏰
