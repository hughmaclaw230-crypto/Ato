#!/usr/bin/env python3
"""
THSRC Sniper v2.0 — Flask Web Service
LINE & Telegram 雙 Bot 指令控制 + 用戶認證 + 管理員審核
6 小時保活，收到訊息自動重置計時
"""

import os
import re
import json
import hmac
import hashlib
import base64
import time
import logging
import threading
import requests
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, request, jsonify

# ─── 設定 ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ─── 環境變數 ─────────────────────────────────────────
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_TG_CHAT_ID = os.environ.get("ADMIN_TELEGRAM_CHAT_ID", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
PORT = int(os.environ.get("PORT", "5000"))

# ═══════════════════════════════════════════════════════════
#  用戶資料庫（JSON 檔案持久化）
# ═══════════════════════════════════════════════════════════

USERS_FILE = Path("data/users.json")
_users_lock = threading.Lock()


def _load_users() -> dict:
    """從 JSON 讀取用戶資料"""
    if not USERS_FILE.exists():
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_users(users: dict):
    """寫入用戶資料"""
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def get_user(user_id: str) -> dict | None:
    """取得用戶資料（by tg_chatId or line_userId）"""
    with _users_lock:
        users = _load_users()
        return users.get(user_id)


def save_user(user_id: str, data: dict):
    """新增/更新用戶"""
    with _users_lock:
        users = _load_users()
        users[user_id] = data
        _save_users(users)


def find_user_by_field(field: str, value: str) -> tuple[str, dict] | None:
    """依欄位搜尋用戶"""
    with _users_lock:
        users = _load_users()
        for uid, data in users.items():
            if data.get(field) == value:
                return uid, data
    return None


def register_user(provider: str, provider_id: str, name: str, username: str = "") -> tuple[str, str]:
    """
    註冊新用戶或取得現有用戶狀態
    回傳: (status, user_id)
    status: 'new' | 'pending' | 'approved' | 'rejected'
    """
    # 檢查是否已存在
    if provider == "telegram":
        user_id = f"tg_{provider_id}"
    else:
        user_id = f"line_{provider_id}"

    existing = get_user(user_id)
    if existing:
        return existing.get("status", "pending"), user_id

    # 新建用戶
    now = datetime.now().isoformat()
    user_data = {
        "provider": provider,
        "provider_id": provider_id,
        "name": name,
        "username": username,
        "status": "pending",
        "created_at": now,
        "reviewed_at": None,
    }

    if provider == "telegram":
        user_data["telegram_chat_id"] = provider_id
    else:
        user_data["line_user_id"] = provider_id

    save_user(user_id, user_data)

    # 通知管理員
    notify_admin_new_user(user_id, user_data)

    return "new", user_id


def approve_user(user_id: str) -> bool:
    """核准用戶"""
    user = get_user(user_id)
    if not user:
        return False
    user["status"] = "approved"
    user["reviewed_at"] = datetime.now().isoformat()
    save_user(user_id, user)
    return True


def reject_user(user_id: str) -> bool:
    """拒絕用戶"""
    user = get_user(user_id)
    if not user:
        return False
    user["status"] = "rejected"
    user["reviewed_at"] = datetime.now().isoformat()
    save_user(user_id, user)
    return True


def is_user_approved(user_id: str) -> bool:
    """檢查用戶是否已核准"""
    user = get_user(user_id)
    return user is not None and user.get("status") == "approved"


def is_admin_telegram(chat_id: str) -> bool:
    """檢查是否為 Telegram 管理員"""
    return ADMIN_TG_CHAT_ID and str(chat_id) == str(ADMIN_TG_CHAT_ID)


# ═══════════════════════════════════════════════════════════
#  Session / Keep-Alive（6 小時保活）
# ═══════════════════════════════════════════════════════════

SESSION_TIMEOUT = 6 * 60 * 60
KEEPALIVE_INTERVAL = 5 * 60
_last_activity = datetime.now()
_session_active = True
_keepalive_thread = None


def touch_session():
    global _last_activity, _session_active
    _last_activity = datetime.now()
    _session_active = True


def is_session_alive() -> bool:
    global _session_active
    elapsed = (datetime.now() - _last_activity).total_seconds()
    if elapsed > SESSION_TIMEOUT:
        _session_active = False
    return _session_active


def keepalive_worker():
    while True:
        time.sleep(KEEPALIVE_INTERVAL)
        if not is_session_alive():
            log.debug("💤 Session 已到期，暫停 keep-alive")
            continue
        ping_url = RENDER_EXTERNAL_URL or f"http://localhost:{PORT}"
        try:
            requests.get(f"{ping_url}/api/health", timeout=10)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════
#  訂票設定 & 狀態
# ═══════════════════════════════════════════════════════════

booking_config = {
    "id_number":      os.environ.get("THSRC_ID", ""),
    "phone":          os.environ.get("THSRC_PHONE", ""),
    "from_station":   os.environ.get("FROM_STATION", "南港"),
    "to_station":     os.environ.get("TO_STATION", "左營"),
    "travel_date":    os.environ.get("TRAVEL_DATE", ""),
    "travel_time":    os.environ.get("TRAVEL_TIME", ""),
    "adult_count":    int(os.environ.get("ADULT_COUNT", "1")),
    "seat_type":      os.environ.get("SEAT_TYPE", "無座位偏好"),
    "max_retries":    int(os.environ.get("MAX_RETRIES", "30")),
    "retry_interval": float(os.environ.get("RETRY_INTERVAL", "3")),
}

booking_status = {
    "running": False,
    "last_result": None,
    "last_run": None,
    "attempts": 0,
}

STATION_MAP = {
    "南港": "1", "台北": "2", "板橋": "3", "桃園": "4",
    "新竹": "5", "苗栗": "6", "台中": "7", "彰化": "8",
    "雲林": "9", "嘉義": "10", "台南": "11", "左營": "12",
}
STATION_NAMES = list(STATION_MAP.keys())

TIME_SLOTS = [
    "06:00", "06:30", "07:00", "07:30", "08:00", "08:30",
    "09:00", "09:30", "10:00", "10:30", "11:00", "11:30",
    "12:00", "12:30", "13:00", "13:30", "14:00", "14:30",
    "15:00", "15:30", "16:00", "16:30", "17:00", "17:30",
    "18:00", "18:30", "19:00", "19:30", "20:00", "20:30",
    "21:00", "21:30", "22:00", "22:30",
]

SEAT_MAP = {"無座位偏好": "0", "靠窗": "1", "靠走道": "2"}


# ═══════════════════════════════════════════════════════════
#  Telegram 發送工具
# ═══════════════════════════════════════════════════════════

def send_telegram(chat_id: str, text: str, parse_mode: str = "HTML",
                  reply_markup: dict | None = None):
    if not TG_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            log.warning(f"Telegram 回應異常: {r.text}")
    except Exception as e:
        log.error(f"Telegram 發送失敗: {e}")


def edit_telegram_message(chat_id: str, message_id: int, text: str):
    """編輯已發送的 Telegram 訊息（移除按鈕）"""
    if not TG_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/editMessageText"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=10)
    except Exception:
        pass


def answer_callback(callback_query_id: str, text: str = ""):
    if not TG_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={
            "callback_query_id": callback_query_id,
            "text": text,
        }, timeout=10)
    except Exception:
        pass


def send_line_reply(reply_token: str, messages: list):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        return
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    try:
        r = requests.post(url, json={
            "replyToken": reply_token,
            "messages": messages[:5],
        }, headers=headers, timeout=10)
        if r.status_code != 200:
            log.warning(f"LINE reply 失敗: {r.text}")
    except Exception as e:
        log.error(f"LINE reply 錯誤: {e}")


def send_line_push(user_id: str, messages: list):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        return
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    try:
        requests.post(url, json={
            "to": user_id,
            "messages": messages[:5],
        }, headers=headers, timeout=10)
    except Exception:
        pass


def get_line_display_name(user_id: str) -> str:
    """透過 API 取得 LINE 使用者名稱"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        return "LINE用戶"
    try:
        r = requests.get(
            f"https://api.line.me/v2/bot/profile/{user_id}",
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            timeout=5,
        )
        if r.status_code == 200:
            return r.json().get("displayName", "LINE用戶")
    except Exception:
        pass
    return "LINE用戶"


def notify_admin(text: str):
    if ADMIN_TG_CHAT_ID:
        send_telegram(ADMIN_TG_CHAT_ID, text)


# ═══════════════════════════════════════════════════════════
#  管理員審核通知
# ═══════════════════════════════════════════════════════════

def notify_admin_new_user(user_id: str, user_data: dict):
    """新用戶註冊 → 通知管理員 Telegram（附核准/拒絕按鈕）"""
    if not ADMIN_TG_CHAT_ID:
        log.warning("⚠️ ADMIN_TELEGRAM_CHAT_ID 未設定，無法通知管理員")
        return

    provider = user_data.get("provider", "unknown")
    provider_icons = {"telegram": "📨 Telegram", "line": "💚 LINE"}

    text = "\n".join([
        "🆕 <b>新用戶註冊申請</b>",
        "",
        f"👤 姓名：<b>{user_data.get('name', '未知')}</b>",
        f"📡 來源：{provider_icons.get(provider, provider)}",
        f"🔖 帳號：{user_data.get('username') or user_data.get('provider_id', '-')}",
        f"🆔 ID：<code>{user_id}</code>",
        f"🕐 時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "請點擊按鈕進行審核：",
    ])

    buttons = {
        "inline_keyboard": [[
            {"text": "✅ 通過", "callback_data": f"approve:{user_id}"},
            {"text": "❌ 拒絕", "callback_data": f"reject:{user_id}"},
        ]]
    }

    send_telegram(ADMIN_TG_CHAT_ID, text, reply_markup=buttons)


def handle_admin_callback(data: dict):
    """處理管理員按下核准/拒絕按鈕"""
    cb = data.get("callback_query")
    if not cb or not cb.get("data"):
        return

    from_id = str(cb["from"]["id"])
    if not is_admin_telegram(from_id):
        answer_callback(cb["id"], "⚠️ 只有管理者可以執行此操作")
        return

    action, user_id = cb["data"].split(":", 1)
    user = get_user(user_id)

    if not user:
        answer_callback(cb["id"], "❌ 找不到此用戶")
        return

    if action == "approve":
        approve_user(user_id)
        answer_callback(cb["id"], "✅ 已通過")
        status_text = "✅ 已通過"
    elif action == "reject":
        reject_user(user_id)
        answer_callback(cb["id"], "❌ 已拒絕")
        status_text = "❌ 已拒絕"
    else:
        answer_callback(cb["id"])
        return

    # 更新管理員訊息（移除按鈕）
    if cb.get("message"):
        new_text = "\n".join([
            f"{status_text} <b>用戶審核完成</b>",
            "",
            f"👤 {user.get('name', '未知')}",
            f"📋 結果：{status_text}",
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ])
        edit_telegram_message(
            str(cb["message"]["chat"]["id"]),
            cb["message"]["message_id"],
            new_text,
        )

    # 通知用戶
    provider = user.get("provider", "")
    if action == "approve":
        notify_text = (
            "🎉 <b>帳號已通過審核！</b>\n\n"
            "您現在可以使用所有功能了：\n"
            "📌 /settings — 查看設定\n"
            "📌 /book — 開始訂票\n"
            "📌 /help — 所有指令\n\n"
            "開始設定訂票吧！🚀"
        )
    else:
        notify_text = (
            "⚠️ <b>帳號審核未通過</b>\n\n"
            "很抱歉，您的帳號未通過管理者審核。\n"
            "如有疑問，請聯繫管理者。"
        )

    if provider == "telegram" and user.get("telegram_chat_id"):
        send_telegram(user["telegram_chat_id"], notify_text)
    elif provider == "line" and user.get("line_user_id"):
        send_line_push(user["line_user_id"], [{
            "type": "text",
            "text": strip_html(notify_text),
        }])

    log.info(f"🔐 Admin {'approved' if action == 'approve' else 'rejected'} user {user.get('name')} ({user_id})")


# ═══════════════════════════════════════════════════════════
#  統一指令處理器
# ═══════════════════════════════════════════════════════════

# 不需要核准就能用的指令
OPEN_COMMANDS = {"start", "help", "status"}


def get_config_summary() -> str:
    c = booking_config
    elapsed = (datetime.now() - _last_activity).total_seconds()
    left = max(0, SESSION_TIMEOUT - elapsed)
    h, m = int(left // 3600), int((left % 3600) // 60)
    session_text = f"🟢 Session 剩餘：{h}h {m}m" if is_session_alive() else "🔴 Session 已到期"

    lines = [
        "⚙️ <b>目前訂票設定</b>",
        "",
        f"🚉 路線：{c['from_station']} → {c['to_station']}",
        f"📅 日期：{c['travel_date'] or '❌ 未設定'}",
        f"🕐 時間：{c['travel_time'] or '❌ 未設定'}",
        f"👤 人數：{c['adult_count']} 人",
        f"💺 座位：{c['seat_type']}",
        f"🆔 身分證：{'✅ 已設定' if c['id_number'] else '❌ 未設定'}",
        f"📱 手機：{'✅ 已設定' if c['phone'] else '❌ 未設定'}",
        f"🔄 重試：最多 {c['max_retries']} 次，間隔 {c['retry_interval']}s",
    ]

    s = booking_status
    if s["running"]:
        lines.append(f"\n🔥 <b>正在訂票中...</b>（已嘗試 {s['attempts']} 次）")
    elif s["last_result"]:
        lines.append(f"\n📋 上次結果：{'✅ 成功' if s['last_result'].get('success') else '❌ 失敗'}")
        lines.append(f"🕐 時間：{s['last_run']}")

    lines.append(f"\n{session_text}")
    return "\n".join(lines)


def get_help_text() -> str:
    return "\n".join([
        "📖 <b>THSRC Sniper 指令一覽</b>",
        "",
        "🔧 <b>設定</b>",
        "/from &lt;站名&gt; — 出發站",
        "/to &lt;站名&gt; — 到達站",
        "/date &lt;日期&gt; — 日期（2025/06/01）",
        "/time &lt;時間&gt; — 時間（07:30）",
        "/count &lt;人數&gt; — 成人票數",
        "/seat &lt;偏好&gt; — 座位偏好",
        "/id &lt;身分證&gt; — 身分證字號",
        "/phone &lt;手機&gt; — 手機號碼",
        "",
        "🚀 <b>操作</b>",
        "/book — 開始訂票",
        "/stop — 停止訂票",
        "/status — 訂票狀態",
        "/settings — 目前設定",
        "",
        "📊 <b>其他</b>",
        "/stations — 車站列表",
        "/times — 可選時段",
        "/help — 本說明",
    ])


def process_command(cmd: str, args: str) -> str:
    cmd = cmd.lower().strip()

    if cmd in ("start", "help"):
        return get_help_text()
    elif cmd == "settings":
        return get_config_summary()
    elif cmd == "status":
        s = booking_status
        if s["running"]:
            return f"🔥 <b>訂票進行中</b>\n已嘗試 {s['attempts']} 次\n路線：{booking_config['from_station']} → {booking_config['to_station']}"
        elif s["last_result"]:
            r = s["last_result"]
            return f"📋 上次訂票結果：{'✅ 成功' if r.get('success') else '❌ 失敗'}\n時間：{s['last_run']}"
        return "📋 尚未執行過訂票\n輸入 /settings 查看設定\n輸入 /book 開始訂票"
    elif cmd == "from":
        if args in STATION_MAP:
            booking_config["from_station"] = args
            return f"✅ 出發站：<b>{args}</b>"
        return f"❌ 無效站名「{args}」\n{' '.join(STATION_NAMES)}"
    elif cmd == "to":
        if args in STATION_MAP:
            booking_config["to_station"] = args
            return f"✅ 到達站：<b>{args}</b>"
        return f"❌ 無效站名「{args}」\n{' '.join(STATION_NAMES)}"
    elif cmd == "date":
        d = args.replace("-", "/")
        booking_config["travel_date"] = d
        return f"✅ 日期：<b>{d}</b>"
    elif cmd == "time":
        t = args.strip()
        if t in TIME_SLOTS:
            booking_config["travel_time"] = t
            return f"✅ 時間：<b>{t}</b>"
        return f"❌ 無效時間「{t}」\n輸入 /times 查看可選時段"
    elif cmd == "count":
        try:
            n = int(args)
            if 1 <= n <= 10:
                booking_config["adult_count"] = n
                return f"✅ 票數：<b>{n}</b>"
            return "❌ 票數需為 1-10"
        except ValueError:
            return "❌ 請輸入數字"
    elif cmd == "seat":
        if args in SEAT_MAP:
            booking_config["seat_type"] = args
            return f"✅ 座位：<b>{args}</b>"
        return "❌ 請選擇：無座位偏好 / 靠窗 / 靠走道"
    elif cmd == "id":
        if args:
            booking_config["id_number"] = args
            return "✅ 身分證字號已設定"
        return "❌ 請提供身分證字號"
    elif cmd == "phone":
        if args:
            booking_config["phone"] = args
            return "✅ 手機號碼已設定"
        return "❌ 請提供手機號碼"
    elif cmd == "stations":
        return "🚉 <b>車站列表</b>\n\n" + "\n".join(
            f"  {i+1}. {name}" for i, name in enumerate(STATION_NAMES)
        )
    elif cmd == "times":
        lines = ["🕐 <b>可選時段</b>\n"]
        for i in range(0, len(TIME_SLOTS), 4):
            lines.append("  ".join(TIME_SLOTS[i:i+4]))
        return "\n".join(lines)
    elif cmd == "book":
        return start_booking()
    elif cmd == "stop":
        if booking_status["running"]:
            booking_status["running"] = False
            return "🛑 已發送停止訊號"
        return "📋 目前沒有正在進行的訂票"
    elif cmd == "retry":
        try:
            n = int(args)
            if 1 <= n <= 100:
                booking_config["max_retries"] = n
                return f"✅ 重試次數：<b>{n}</b>"
            return "❌ 請在 1-100 之間"
        except ValueError:
            return "❌ 請輸入數字"
    elif cmd == "interval":
        try:
            f = float(args)
            if 1 <= f <= 30:
                booking_config["retry_interval"] = f
                return f"✅ 重試間隔：<b>{f}s</b>"
            return "❌ 請在 1-30 之間"
        except ValueError:
            return "❌ 請輸入數字"
    else:
        return f"❓ 未知指令 <code>/{cmd}</code>\n輸入 /help 查看所有指令"


def start_booking() -> str:
    c = booking_config
    missing = []
    if not c["id_number"]: missing.append("身分證字號 (/id)")
    if not c["phone"]:     missing.append("手機號碼 (/phone)")
    if not c["travel_date"]: missing.append("出發日期 (/date)")
    if not c["travel_time"]: missing.append("出發時間 (/time)")
    if missing:
        return "❌ <b>缺少必填資訊：</b>\n" + "\n".join(f"  • {m}" for m in missing)
    if booking_status["running"]:
        return f"⚠️ 訂票已在進行中（第 {booking_status['attempts']} 次）\n/stop 可停止"

    booking_status["running"] = True
    booking_status["attempts"] = 0
    booking_status["last_result"] = None
    threading.Thread(target=run_booking_thread, daemon=True).start()

    return (
        f"🚅 <b>開始訂票！</b>\n"
        f"─────────────────\n"
        f"路線：{c['from_station']} → {c['to_station']}\n"
        f"日期：{c['travel_date']}　時間：{c['travel_time']}\n"
        f"人數：{c['adult_count']} 人\n"
        f"─────────────────\n"
        f"最多 {c['max_retries']} 次，間隔 {c['retry_interval']}s\n"
        f"完成後自動通知 📩"
    )


def run_booking_thread():
    try:
        from booking_engine import run_booking
        result = run_booking(booking_config, booking_status)
    except ImportError:
        log.info("⚠️ booking_engine 未找到，執行模擬")
        result = simulate_booking()

    booking_status["running"] = False
    booking_status["last_result"] = result
    booking_status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if result.get("success"):
        msg = f"✅ <b>訂票成功！</b>\n─────────────────\n路線：{booking_config['from_station']} → {booking_config['to_station']}\n日期：{booking_config['travel_date']}\n"
        for k, v in result.items():
            if k not in ("success", "url", "timestamp"):
                msg += f"{k}：{v}\n"
    else:
        msg = f"❌ <b>訂票失敗</b>\n已嘗試 {booking_status['attempts']} 次\n原因：{result.get('error', '未知')}"

    notify_admin(msg)


def simulate_booking():
    c = booking_config
    for i in range(min(3, c["max_retries"])):
        if not booking_status["running"]:
            return {"success": False, "error": "使用者手動中止"}
        booking_status["attempts"] = i + 1
        time.sleep(2)
    return {
        "success": True, "訂位代號": "DEMO12345",
        "車次": "0605", "出發時間": f"{c['travel_date']} {c['travel_time']}",
        "座位": "8 車 12A",
    }


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace("&gt;", ">")


# ═══════════════════════════════════════════════════════════
#  Telegram Webhook
# ═══════════════════════════════════════════════════════════

@app.route("/api/webhook/telegram", methods=["POST"])
def telegram_webhook():
    touch_session()
    data = request.get_json(silent=True) or {}

    # 處理管理員按鈕回調
    if "callback_query" in data:
        handle_admin_callback(data)
        return jsonify({"ok": True})

    message = data.get("message")
    if not message or not message.get("text"):
        return jsonify({"ok": True})

    chat_id = str(message["chat"]["id"])
    text = message["text"].strip()
    from_user = message.get("from", {})
    name = " ".join(filter(None, [from_user.get("first_name"), from_user.get("last_name")]))
    username = from_user.get("username", "")

    log.info(f"🤖 TG {chat_id} ({name}): {text}")

    # 解析指令
    cmd_match = re.match(r"^/(\w+)(?:@\w+)?\s*(.*)", text, re.DOTALL)
    if not cmd_match:
        send_telegram(chat_id, "💡 請輸入指令，例如 /help")
        return jsonify({"ok": True})

    cmd = cmd_match.group(1).lower()
    args = cmd_match.group(2).strip()

    # /start → 處理註冊
    if cmd == "start":
        status, user_id = register_user("telegram", chat_id, name, username)

        if status == "new":
            send_telegram(chat_id, "\n".join([
                f"👋 嗨 <b>{name}</b>！歡迎使用 <b>THSRC Sniper</b>",
                "",
                "📝 您的帳號已建立，目前狀態：<b>⏳ 待審核</b>",
                "",
                "管理者會收到通知，審核通過後您將收到訊息 📩",
                "",
                "輸入 /help 預覽可用指令",
            ]))
        elif status == "pending":
            send_telegram(chat_id, "\n".join([
                f"⏳ 嗨 <b>{name}</b>！您的帳號正在審核中",
                "",
                "管理者收到通知後會盡快處理。",
                "通過後您會收到通知 📩",
            ]))
        elif status == "approved":
            send_telegram(chat_id, "\n".join([
                f"✅ <b>{name}</b>，您已通過審核！",
                "",
                f"Chat ID：<code>{chat_id}</code>",
                "輸入 /help 查看所有指令",
            ]))
        elif status == "rejected":
            send_telegram(chat_id, "⚠️ 您的帳號未通過審核，請聯繫管理者。")

        return jsonify({"ok": True})

    # 管理員可以直接使用所有指令
    if is_admin_telegram(chat_id):
        reply_text = process_command(cmd, args)
        send_telegram(chat_id, reply_text)
        return jsonify({"ok": True})

    # 一般用戶：開放指令不需審核
    if cmd in OPEN_COMMANDS:
        reply_text = process_command(cmd, args)
        send_telegram(chat_id, reply_text)
        return jsonify({"ok": True})

    # 需要審核的指令 → 檢查用戶狀態
    user_id = f"tg_{chat_id}"
    user = get_user(user_id)

    if not user:
        send_telegram(chat_id, "❌ 尚未註冊，請先使用 /start")
        return jsonify({"ok": True})

    if user.get("status") == "pending":
        send_telegram(chat_id, "⏳ 帳號審核中，通過後即可使用此功能")
        return jsonify({"ok": True})

    if user.get("status") != "approved":
        send_telegram(chat_id, f"⚠️ 帳號狀態：{user.get('status', '未知')}，無法使用此功能")
        return jsonify({"ok": True})

    reply_text = process_command(cmd, args)
    send_telegram(chat_id, reply_text)
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════
#  LINE Webhook
# ═══════════════════════════════════════════════════════════

def verify_line_signature(body: str, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        return True
    h = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(h).decode("utf-8") == signature


@app.route("/api/webhook/line", methods=["POST"])
def line_webhook():
    touch_session()

    raw_body = request.get_data(as_text=True)
    signature = request.headers.get("x-line-signature", "")

    if LINE_CHANNEL_SECRET and not verify_line_signature(raw_body, signature):
        log.warning("⚠️ LINE webhook 簽名驗證失敗")
        return jsonify({"error": "Invalid signature"}), 403

    body = request.get_json(silent=True) or {}
    events = body.get("events", [])

    for event in events:
        if event.get("type") == "follow":
            # 用戶加入好友 → 自動註冊
            user_id = event.get("source", {}).get("userId", "")
            if user_id:
                handle_line_follow(user_id, event.get("replyToken", ""))
            continue

        if event.get("type") != "message":
            continue
        if event.get("message", {}).get("type") != "text":
            continue

        reply_token = event.get("replyToken", "")
        text = event["message"]["text"].strip()
        source = event.get("source", {})
        user_id = source.get("userId", "")

        log.info(f"💚 LINE {user_id}: {text}")

        # 處理 LINE 訊息（含認證檢查）
        handle_line_message(user_id, text, reply_token)

    return jsonify({"ok": True})


def handle_line_follow(line_user_id: str, reply_token: str):
    """LINE 加好友事件 → 自動註冊"""
    name = get_line_display_name(line_user_id)
    status, user_id = register_user("line", line_user_id, name)

    if status == "new":
        send_line_reply(reply_token, [{"type": "text", "text": "\n".join([
            f"👋 嗨 {name}！歡迎使用 THSRC Sniper 🚅",
            "",
            "📝 您的帳號已建立，目前狀態：⏳ 待審核",
            "",
            "管理者會收到通知，審核通過後您將收到訊息 📩",
            "",
            "輸入「幫助」預覽可用指令",
        ])}])
    elif status == "approved":
        send_line_reply(reply_token, [{"type": "text", "text": "\n".join([
            f"✅ {name}，歡迎回來！",
            "輸入「幫助」查看所有指令",
        ])}])


def handle_line_message(line_user_id: str, text: str, reply_token: str):
    """處理 LINE 訊息"""

    # 中文指令對照
    zh_cmd_map = {
        "幫助": "help", "說明": "help",
        "設定": "settings", "查看設定": "settings",
        "狀態": "status",
        "訂票": "book", "開始訂票": "book", "搶票": "book",
        "停止": "stop", "取消": "stop",
        "車站": "stations", "站別": "stations",
        "時段": "times", "時間表": "times",
        "註冊": "start",
    }

    zh_arg_patterns = [
        (r"^出發站?\s*(.+)", "from"),
        (r"^到達站?\s*(.+)", "to"),
        (r"^目的地?\s*(.+)", "to"),
        (r"^日期\s*(.+)", "date"),
        (r"^時間\s*(.+)", "time"),
        (r"^人數\s*(.+)", "count"),
        (r"^座位\s*(.+)", "seat"),
        (r"^身分證\s*(.+)", "id"),
        (r"^手機\s*(.+)", "phone"),
    ]

    # 解析指令
    cmd, args = None, ""

    cmd_match = re.match(r"^/(\w+)\s*(.*)", text, re.DOTALL)
    if cmd_match:
        cmd = cmd_match.group(1).lower()
        args = cmd_match.group(2).strip()
    else:
        for zh, en in zh_cmd_map.items():
            if text == zh:
                cmd = en
                break

        if not cmd:
            for pattern, en_cmd in zh_arg_patterns:
                m = re.match(pattern, text)
                if m:
                    cmd = en_cmd
                    args = m.group(1).strip()
                    break

    if not cmd:
        send_line_reply(reply_token, [{"type": "text", "text": "💡 我是 THSRC Sniper 🚅\n請輸入「幫助」或 /help 查看指令"}])
        return

    # /start / 註冊 → 處理註冊
    if cmd == "start":
        name = get_line_display_name(line_user_id)
        status, user_id = register_user("line", line_user_id, name)

        if status == "new":
            msg = f"👋 嗨 {name}！您的帳號已建立\n\n目前狀態：⏳ 待審核\n管理者會盡快審核 📩"
        elif status == "pending":
            msg = f"⏳ {name}，您的帳號正在審核中\n通過後您會收到通知 📩"
        elif status == "approved":
            msg = f"✅ {name}，您已通過審核！\n輸入「幫助」查看指令"
        else:
            msg = "⚠️ 您的帳號未通過審核，請聯繫管理者。"

        send_line_reply(reply_token, [{"type": "text", "text": msg}])
        return

    # 開放指令
    if cmd in OPEN_COMMANDS:
        reply_text = process_command(cmd, args)
        send_line_reply(reply_token, [{"type": "text", "text": strip_html(reply_text)}])
        return

    # 需要認證的指令
    db_user_id = f"line_{line_user_id}"
    user = get_user(db_user_id)

    if not user:
        send_line_reply(reply_token, [{"type": "text", "text": "❌ 尚未註冊\n請輸入「註冊」來建立帳號"}])
        return

    if user.get("status") == "pending":
        send_line_reply(reply_token, [{"type": "text", "text": "⏳ 帳號審核中，通過後即可使用"}])
        return

    if user.get("status") != "approved":
        send_line_reply(reply_token, [{"type": "text", "text": f"⚠️ 帳號狀態：{user.get('status', '未知')}"}])
        return

    reply_text = process_command(cmd, args)
    send_line_reply(reply_token, [{"type": "text", "text": strip_html(reply_text)}])


# ═══════════════════════════════════════════════════════════
#  Health & API Routes
# ═══════════════════════════════════════════════════════════

@app.route("/api/health", methods=["GET"])
def health():
    elapsed = (datetime.now() - _last_activity).total_seconds()
    session_remaining = max(0, SESSION_TIMEOUT - elapsed)

    return jsonify({
        "status": "ok",
        "service": "THSRC Sniper",
        "timestamp": datetime.now().isoformat(),
        "telegram_bot": bool(TG_TOKEN),
        "line_bot": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "session": {
            "active": is_session_alive(),
            "remaining_minutes": round(session_remaining / 60, 1),
            "timeout_hours": SESSION_TIMEOUT / 3600,
        },
        "booking": {
            "running": booking_status["running"],
            "from": booking_config["from_station"],
            "to": booking_config["to_station"],
        },
    })


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "name": "🚅 THSRC Sniper",
        "version": "2.0",
        "description": "高鐵自動訂票 — LINE & Telegram 指令控制 + 管理員審核",
        "session_timeout": "6 小時",
        "endpoints": {
            "health": "/api/health",
            "telegram_webhook": "/api/webhook/telegram",
            "line_webhook": "/api/webhook/line",
        },
    })


# ═══════════════════════════════════════════════════════════
#  Startup
# ═══════════════════════════════════════════════════════════

def register_telegram_webhook():
    if not TG_TOKEN or not RENDER_EXTERNAL_URL:
        log.info("  Telegram webhook: 略過（缺少 TOKEN 或 URL）")
        return

    webhook_url = f"{RENDER_EXTERNAL_URL}/api/webhook/telegram"
    url = f"https://api.telegram.org/bot{TG_TOKEN}/setWebhook"
    try:
        r = requests.post(url, json={"url": webhook_url}, timeout=10)
        data = r.json()
        if data.get("ok"):
            log.info(f"  ✅ Telegram webhook: {webhook_url}")
        else:
            log.warning(f"  ❌ Telegram webhook 失敗: {data}")
    except Exception as e:
        log.error(f"  Telegram webhook 錯誤: {e}")


def set_telegram_commands():
    if not TG_TOKEN:
        return
    commands = [
        {"command": "start", "description": "註冊 / 歡迎"},
        {"command": "help", "description": "顯示說明"},
        {"command": "settings", "description": "查看設定"},
        {"command": "book", "description": "開始訂票"},
        {"command": "stop", "description": "停止訂票"},
        {"command": "status", "description": "訂票狀態"},
        {"command": "from", "description": "出發站"},
        {"command": "to", "description": "到達站"},
        {"command": "date", "description": "出發日期"},
        {"command": "time", "description": "出發時間"},
        {"command": "count", "description": "票數"},
        {"command": "seat", "description": "座位偏好"},
        {"command": "stations", "description": "車站列表"},
        {"command": "times", "description": "可選時段"},
    ]
    url = f"https://api.telegram.org/bot{TG_TOKEN}/setMyCommands"
    try:
        requests.post(url, json={"commands": commands}, timeout=10)
        log.info("  ✅ Telegram 指令選單已設定")
    except Exception:
        pass


def startup():
    global _keepalive_thread

    log.info("🚅 THSRC Sniper v2.0 啟動中...")
    log.info(f"  Telegram Bot: {'✅' if TG_TOKEN else '❌'}")
    log.info(f"  LINE Bot: {'✅' if LINE_CHANNEL_ACCESS_TOKEN else '❌'}")
    log.info(f"  Admin TG Chat: {'✅ ' + ADMIN_TG_CHAT_ID if ADMIN_TG_CHAT_ID else '❌'}")
    log.info(f"  Render URL: {RENDER_EXTERNAL_URL or '(local)'}")
    log.info(f"  Session: {SESSION_TIMEOUT // 3600}h | Keep-alive: {KEEPALIVE_INTERVAL // 60}min")

    # 確保 data 目錄存在
    Path("data").mkdir(exist_ok=True)

    register_telegram_webhook()
    set_telegram_commands()

    if LINE_CHANNEL_ACCESS_TOKEN and RENDER_EXTERNAL_URL:
        log.info(f"  💚 LINE webhook: {RENDER_EXTERNAL_URL}/api/webhook/line")
        log.info("  ℹ️ 請到 LINE Developers → Webhook URL 設定上述網址")

    # 管理員自動核准
    if ADMIN_TG_CHAT_ID:
        admin_id = f"tg_{ADMIN_TG_CHAT_ID}"
        if not get_user(admin_id):
            save_user(admin_id, {
                "provider": "telegram",
                "provider_id": ADMIN_TG_CHAT_ID,
                "name": "Admin",
                "username": "",
                "status": "approved",
                "telegram_chat_id": ADMIN_TG_CHAT_ID,
                "created_at": datetime.now().isoformat(),
                "reviewed_at": datetime.now().isoformat(),
            })
            log.info("  ✅ 管理員帳號已自動建立")

    _keepalive_thread = threading.Thread(target=keepalive_worker, daemon=True)
    _keepalive_thread.start()
    log.info("  🏓 Keep-alive 已啟動")

    # 通知管理員服務啟動
    notify_admin("🚅 <b>THSRC Sniper 已啟動</b>\n\n"
                 f"Telegram: {'✅' if TG_TOKEN else '❌'}\n"
                 f"LINE: {'✅' if LINE_CHANNEL_ACCESS_TOKEN else '❌'}\n"
                 f"Session: {SESSION_TIMEOUT // 3600}h\n"
                 f"時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


# ═══════════════════════════════════════════════════════════
#  Entry Point
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    startup()
    app.run(host="0.0.0.0", port=PORT, debug=False)
