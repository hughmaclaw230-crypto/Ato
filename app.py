#!/usr/bin/env python3
"""
ATO (AutoTHSR) — Flask Web Service
Telegram Bot 指令控制 + 用戶認證 + 管理員審核 + 高鐵時刻表查詢
Firestore 雲端持久化 + 6 小時保活
"""

import os
import re
import json
import time
import logging
import threading
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from telegram_form import (start_search_form, handle_form_callback,
                           get_completed_form, clear_form, parse_smart_search)
from firestore_db import (
    get_user, save_user, get_pending_users, get_all_users,
    get_db as get_firestore_db, is_available as is_firestore_available,
)

# ─── 設定 ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.json.ensure_ascii = False  # JSON 回應顯示中文而非 Unicode 轉義

# ─── 環境變數 ─────────────────────────────────────────
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_TG_CHAT_ID = os.environ.get("ADMIN_TELEGRAM_CHAT_ID", "")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
PORT = int(os.environ.get("PORT", "5000"))

# ═══════════════════════════════════════════════════════════
#  用戶資料庫（Firestore 雲端持久化）
#  get_user / save_user / get_pending_users / get_all_users
#  均從 firestore_db 模組匯入
# ═══════════════════════════════════════════════════════════


# ── 角色定義 ──
ROLE_SUPERADMIN = "superadmin"
ROLE_ADMIN = "admin"
ROLE_USER = "user"

# ── Super Admin Chat ID（從環境變數讀取，不寫入程式碼）──
SUPERADMIN_CHAT_ID = os.environ.get("SUPERADMIN_CHAT_ID", "")


def register_user(chat_id: str, name: str, username: str = "") -> tuple[str, str]:
    """
    Telegram 註冊新用戶或取得現有用戶狀態
    回傳: (status, user_id)
    status: 'new' | 'pending' | 'approved' | 'rejected'
    """
    user_id = f"tg_{chat_id}"
    existing = get_user(user_id)
    if existing:
        return existing.get("status", "pending"), user_id

    # Super Admin 自動核准
    if str(chat_id) == SUPERADMIN_CHAT_ID:
        now = datetime.now().isoformat()
        user_data = {
            "provider": "telegram",
            "provider_id": chat_id,
            "name": name or "Owner",
            "username": username,
            "status": "approved",
            "role": ROLE_SUPERADMIN,
            "telegram_chat_id": chat_id,
            "created_at": now,
            "reviewed_at": now,
        }
        save_user(user_id, user_data)
        log.info(f"👑 Super Admin {chat_id} 自動核准")
        return "approved", user_id

    # 新建一般用戶
    now = datetime.now().isoformat()
    user_data = {
        "provider": "telegram",
        "provider_id": chat_id,
        "name": name,
        "username": username,
        "status": "pending",
        "role": ROLE_USER,
        "telegram_chat_id": chat_id,
        "created_at": now,
        "reviewed_at": None,
    }

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
    if not user.get("role"):
        user["role"] = ROLE_USER
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


def is_superadmin(chat_id: str) -> bool:
    """檢查是否為 Super Admin"""
    return str(chat_id) == SUPERADMIN_CHAT_ID


def is_admin_telegram(chat_id: str) -> bool:
    """檢查是否為 Telegram 管理員（Super Admin 或 env 指定的 Admin）"""
    if is_superadmin(chat_id):
        return True
    return ADMIN_TG_CHAT_ID and str(chat_id) == str(ADMIN_TG_CHAT_ID)


def get_user_role(chat_id: str) -> str:
    """取得用戶角色"""
    if is_superadmin(chat_id):
        return ROLE_SUPERADMIN
    user = get_user(f"tg_{chat_id}")
    if user:
        return user.get("role", ROLE_USER)
    return ROLE_USER


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
    "max_retries":    int(os.environ.get("MAX_RETRIES", "720")),
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

# 高鐵時刻表查詢用站名對應（英文 ID）
STATION_EN_MAP = {
    "南港": "NanGang", "台北": "TaiPei", "板橋": "BanQiao", "桃園": "TaoYuan",
    "新竹": "HsinChu", "苗栗": "MiaoLi", "台中": "TaiChung", "彰化": "ChangHua",
    "雲林": "YunLin", "嘉義": "ChiaYi", "台南": "TaiNan", "左營": "ZuoYing",
}

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
#  高鐵時刻表查詢
# ═══════════════════════════════════════════════════════════

def query_thsr_timetable(from_station: str, to_station: str,
                         date: str, time_str: str = "") -> str:
    """
    查詢高鐵時刻表
    from_station / to_station: 中文站名
    date: 格式 YYYY/MM/DD
    time_str: 格式 HH:MM (可選)
    回傳: 格式化的時刻表字串 (HTML)
    """
    from_en = STATION_EN_MAP.get(from_station)
    to_en = STATION_EN_MAP.get(to_station)

    if not from_en or not to_en:
        return f"❌ 無效站名：{from_station} 或 {to_station}"

    if from_en == to_en:
        return "❌ 出發站與到達站不能相同"

    # 預設時間為當前時間
    if not time_str:
        time_str = datetime.now().strftime("%H:%M")

    # 確保日期格式正確
    date = date.replace("-", "/")

    url = "https://www.thsrc.com.tw/TimeTable/Search"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json, text/javascript, */*",
        "Referer": "https://www.thsrc.com.tw/ArticleContent/a3b630bb-1066-4352-a1ef-58c7b4e8ef7c",
        "Origin": "https://www.thsrc.com.tw",
    }

    payload = {
        "SearchType": "S",
        "Lang": "TW",
        "StartStation": from_en,
        "EndStation": to_en,
        "OutWardSearchDate": date,
        "OutWardSearchTime": time_str,
        "ReturnSearchDate": date,
        "ReturnSearchTime": time_str,
        "DiscountType": "",
    }

    try:
        log.info(f"🔍 查詢時刻表: {from_station}→{to_station} {date} {time_str}")
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        r = requests.post(url, data=payload, headers=headers, timeout=15, verify=False)

        if r.status_code != 200:
            log.warning(f"時刻表查詢失敗: HTTP {r.status_code}")
            return f"❌ 查詢失敗 (HTTP {r.status_code})"

        data = r.json()
        text, _ = format_timetable_result(data, from_station, to_station, date, time_str)
        return text

    except requests.exceptions.Timeout:
        return "❌ 查詢逾時，請稍後再試"
    except json.JSONDecodeError:
        log.warning("時刻表回應不是 JSON 格式")
        return "❌ 高鐵時刻表暫時無法查詢，請稍後再試"
    except Exception as e:
        log.error(f"時刻表查詢錯誤: {e}")
        return f"❌ 查詢失敗: {str(e)[:50]}"


def format_timetable_result(data: dict, from_st: str, to_st: str,
                            date: str, time_str: str,
                            with_buttons: bool = False) -> tuple[str, list | None]:
    """
    格式化時刻表查詢結果 — 基於 thsrc.com.tw API 回傳結構
    with_buttons=True 時回傳可點選的班次按鈕列表
    回傳: (text, buttons_or_None)
    """

    # API 回傳結構: { success: true, data: { DepartureTable: { TrainItem: [...] } } }
    trains = []

    if isinstance(data, dict):
        # 正式結構
        inner = data.get("data", data)

        # 取得 DepartureTable.TrainItem
        dep_table = inner.get("DepartureTable", {})
        if isinstance(dep_table, dict):
            trains = dep_table.get("TrainItem", [])

        # 如果沒有，嘗試其他 key
        if not trains:
            for key in ["TrainItem", "ResultList", "Trains", "items"]:
                if key in inner and isinstance(inner[key], list):
                    trains = inner[key]
                    break

    lines = [
        f"🚅 <b>高鐵時刻表查詢結果</b>",
        f"",
        f"📍 {from_st} → {to_st}",
        f"📅 {date}　🕐 {time_str} 起",
        f"─────────────────",
    ]

    if not trains:
        lines.append("")
        lines.append("📋 未找到符合的班次")
        lines.append("💡 可能原因：日期超出範圍或無此區間列車")
        return "\n".join(lines), None

    # 限制顯示前 8 班
    display_trains = trains[:8] if len(trains) > 8 else trains

    inline_buttons = []  # 收集每個班次的按鈕

    for i, train in enumerate(display_trains):
        if not isinstance(train, dict):
            continue

        train_no = train.get("TrainNumber", f"#{i+1}")
        depart = train.get("DepartureTime", "-")
        arrive = train.get("DestinationTime", train.get("ArrivalTime", "-"))
        duration = train.get("Duration", "")
        non_reserved = train.get("NonReservedCar", "")
        note = train.get("Note", "")

        # 折扣資訊
        discounts = train.get("Discount", [])
        discount_text = ""
        if discounts and isinstance(discounts, list):
            disc_names = [d.get("Name", "") for d in discounts if isinstance(d, dict)]
            disc_names = [n for n in disc_names if n]
            if disc_names:
                discount_text = "🏷" + "/".join(disc_names)

        line = f"🔹 <b>{train_no}</b>　{depart} → {arrive}"
        if duration:
            line += f"　⏱{duration}"
        lines.append(line)

        # 附加資訊（第二行）
        extras = []
        if non_reserved:
            extras.append(f"🚃自由座:{non_reserved}")
        if discount_text:
            extras.append(discount_text)
        if note:
            extras.append(f"📌{note}")
        if extras:
            lines.append(f"     {' | '.join(extras)}")

        # 生成按鈕 callback_data: bk:<from>:<to>:<date>:<depart_time>:<train_no>
        if with_buttons and depart != "-":
            # 用出發時間取代 travel_time（更精確）
            cb_data = f"bk:{from_st}:{to_st}:{date}:{depart}:{train_no}"
            btn_text = f"🎫 {train_no} — {depart}"
            inline_buttons.append([{"text": btn_text, "callback_data": cb_data}])

    if len(trains) > 8:
        lines.append(f"\n... 還有 {len(trains) - 8} 班次")

    lines.append("")
    lines.append(f"💡 共 {len(trains)} 班次")

    if with_buttons and inline_buttons:
        lines.append("")
        lines.append("👇 點選班次直接訂票：")

    buttons = inline_buttons if inline_buttons else None
    return "\n".join(lines), buttons



def query_thsr_timetable_with_buttons(from_station: str, to_station: str,
                                       date: str, time_str: str = "") -> tuple[str, list | None]:
    """
    查詢高鐵時刻表（帶可點選班次按鈕）
    回傳: (text, buttons_list_or_None)
    """
    from_en = STATION_EN_MAP.get(from_station)
    to_en = STATION_EN_MAP.get(to_station)

    if not from_en or not to_en:
        return f"❌ 無效站名：{from_station} 或 {to_station}", None
    if from_en == to_en:
        return "❌ 出發站與到達站不能相同", None

    if not time_str:
        time_str = datetime.now().strftime("%H:%M")
    date = date.replace("-", "/")

    url = "https://www.thsrc.com.tw/TimeTable/Search"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json, text/javascript, */*",
        "Referer": "https://www.thsrc.com.tw/ArticleContent/a3b630bb-1066-4352-a1ef-58c7b4e8ef7c",
        "Origin": "https://www.thsrc.com.tw",
    }
    payload = {
        "SearchType": "S", "Lang": "TW",
        "StartStation": from_en, "EndStation": to_en,
        "OutWardSearchDate": date, "OutWardSearchTime": time_str,
        "ReturnSearchDate": date, "ReturnSearchTime": time_str,
        "DiscountType": "",
    }

    try:
        log.info(f"🔍 查詢時刻表(帶按鈕): {from_station}→{to_station} {date} {time_str}")
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        r = requests.post(url, data=payload, headers=headers, timeout=15, verify=False)
        if r.status_code != 200:
            return f"❌ 查詢失敗 (HTTP {r.status_code})", None
        data = r.json()
        return format_timetable_result(data, from_station, to_station, date, time_str,
                                       with_buttons=True)
    except requests.exceptions.Timeout:
        return "❌ 查詢逾時，請稍後再試", None
    except json.JSONDecodeError:
        return "❌ 高鐵時刻表暫時無法查詢，請稍後再試", None
    except Exception as e:
        log.error(f"時刻表查詢錯誤: {e}")
        return f"❌ 查詢失敗: {str(e)[:50]}", None


# ═══════════════════════════════════════════════════════════
#  班次訂票回調（點選班次 → 詢問個人資料 → 訂票）
# ═══════════════════════════════════════════════════════════

_pending_bookings = {}  # chat_id -> { from, to, date, time, train_no, step, id_number, phone }
_pending_lock = threading.Lock()


def get_pending_booking(chat_id: str) -> dict | None:
    with _pending_lock:
        return _pending_bookings.get(chat_id)


def set_pending_booking(chat_id: str, data: dict):
    with _pending_lock:
        _pending_bookings[chat_id] = data


def clear_pending_booking(chat_id: str):
    with _pending_lock:
        _pending_bookings.pop(chat_id, None)


def handle_train_booking_callback(cb: dict, cb_data: str, chat_id: str):
    """
    處理點選班次按鈕回調
    cb_data 格式: bk:<from>:<to>:<date>:<time>:<train_no>
    """
    answer_callback(cb["id"], "🎫 準備訂票...")

    parts = cb_data.split(":", 5)
    if len(parts) < 6:
        send_telegram(chat_id, "❌ 資料格式錯誤，請重新搜尋")
        return

    _, from_st, to_st, date, depart_time, train_no = parts

    # 檢查是否已有個人資料
    c = booking_config
    has_id = bool(c.get("id_number"))
    has_phone = bool(c.get("phone"))

    if has_id and has_phone:
        # 個人資料已齊全 → 直接顯示確認畫面
        set_pending_booking(chat_id, {
            "from_station": from_st,
            "to_station": to_st,
            "date": date,
            "time": depart_time,
            "train_no": train_no,
            "step": "confirm",
            "id_number": c["id_number"],
            "phone": c["phone"],
        })
        _send_booking_confirm(chat_id, from_st, to_st, date, depart_time, train_no)
    else:
        # 需要個人資料 → 開始詢問
        set_pending_booking(chat_id, {
            "from_station": from_st,
            "to_station": to_st,
            "date": date,
            "time": depart_time,
            "train_no": train_no,
            "step": "ask_id",
            "id_number": c.get("id_number", ""),
            "phone": c.get("phone", ""),
        })
        send_telegram(chat_id, "\n".join([
            f"🎫 <b>訂票準備 — 車次 {train_no}</b>",
            f"",
            f"🚉 {from_st} → {to_st}",
            f"📅 {date}　🕐 {depart_time}",
            f"─────────────────",
            f"",
            f"📝 請輸入您的<b>身分證字號</b>：",
            f"",
            f"💡 直接輸入即可（例：A123456789）",
            f"❌ 輸入 /cancel 取消",
        ]))


def _send_booking_confirm(chat_id: str, from_st: str, to_st: str,
                           date: str, time_str: str, train_no: str):
    """發送訂票確認畫面（含按鈕）"""
    pb = get_pending_booking(chat_id)
    id_masked = pb["id_number"][:3] + "***" + pb["id_number"][-2:] if len(pb.get("id_number", "")) >= 5 else "✅"
    phone_masked = pb["phone"][:4] + "***" + pb["phone"][-2:] if len(pb.get("phone", "")) >= 6 else "✅"

    text = "\n".join([
        f"🎫 <b>訂票確認</b>",
        f"",
        f"🚅 車次：<b>{train_no}</b>",
        f"🚉 路線：<b>{from_st} → {to_st}</b>",
        f"📅 日期：<b>{date}</b>",
        f"🕐 出發：<b>{time_str}</b>",
        f"👤 人數：<b>{booking_config['adult_count']} 人</b>",
        f"💺 座位：<b>{booking_config['seat_type']}</b>",
        f"─────────────────",
        f"🆔 身分證：{id_masked}",
        f"📱 手機：{phone_masked}",
        f"🔄 最多重試：{booking_config['max_retries']} 次",
        f"",
        f"確認開始訂票？",
    ])

    buttons = {"inline_keyboard": [
        [{"text": "🚀 確認訂票", "callback_data": f"bkconfirm:go:{chat_id}"}],
        [{"text": "✏️ 修改資料", "callback_data": f"bkconfirm:edit:{chat_id}"},
         {"text": "❌ 取消", "callback_data": f"bkconfirm:cancel:{chat_id}"}],
    ]}

    send_telegram(chat_id, text, reply_markup=buttons)


def handle_booking_confirm_callback(cb: dict, cb_data: str, chat_id: str):
    """處理訂票確認按鈕回調"""
    parts = cb_data.split(":", 2)
    if len(parts) < 3:
        answer_callback(cb["id"], "❌ 無效操作")
        return

    action = parts[1]

    if action == "cancel":
        answer_callback(cb["id"], "❌ 已取消")
        clear_pending_booking(chat_id)
        if cb.get("message"):
            edit_telegram_message(chat_id, cb["message"]["message_id"],
                                  "❌ 已取消訂票")
        return

    if action == "edit":
        answer_callback(cb["id"], "✏️ 重新輸入")
        pb = get_pending_booking(chat_id)
        if pb:
            pb["step"] = "ask_id"
            set_pending_booking(chat_id, pb)
            send_telegram(chat_id, "📝 請重新輸入<b>身分證字號</b>：")
        else:
            send_telegram(chat_id, "⚠️ 訂票資料已過期，請重新搜尋 /search")
        return

    if action == "go":
        answer_callback(cb["id"], "🚀 開始訂票！")
        pb = get_pending_booking(chat_id)
        if not pb:
            send_telegram(chat_id, "⚠️ 訂票資料已過期，請重新搜尋 /search")
            return

        # 更新 booking_config
        booking_config["from_station"] = pb["from_station"]
        booking_config["to_station"] = pb["to_station"]
        booking_config["travel_date"] = pb["date"]
        booking_config["travel_time"] = pb["time"]
        booking_config["id_number"] = pb["id_number"]
        booking_config["phone"] = pb["phone"]

        clear_pending_booking(chat_id)

        # 更新確認訊息
        if cb.get("message"):
            edit_telegram_message(chat_id, cb["message"]["message_id"],
                f"🚀 <b>訂票已啟動！</b>\n\n"
                f"🚅 車次 {pb['train_no']}\n"
                f"🚉 {pb['from_station']} → {pb['to_station']}\n"
                f"📅 {pb['date']}　🕐 {pb['time']}")

        # 啟動訂票
        result = start_booking()
        send_telegram(chat_id, result)


def handle_pending_booking_input(chat_id: str, text: str) -> bool:
    """
    處理用戶輸入的個人資料（身分證/手機）
    回傳 True 表示已處理（不再走指令邏輯），False 表示不是訂票輸入
    """
    pb = get_pending_booking(chat_id)
    if not pb:
        return False

    step = pb.get("step", "")

    if text.strip().lower() == "/cancel":
        clear_pending_booking(chat_id)
        send_telegram(chat_id, "❌ 已取消訂票")
        return True

    if step == "ask_id":
        id_num = text.strip().upper()
        # 簡單驗證台灣身分證格式
        if not re.match(r'^[A-Z][12]\d{8}$', id_num):
            send_telegram(chat_id, "\n".join([
                "❌ 身分證字號格式不正確",
                "",
                "✅ 正確格式：英文字母 + 9 位數字",
                "📝 例：A123456789",
                "",
                "請重新輸入，或 /cancel 取消",
            ]))
            return True

        pb["id_number"] = id_num
        pb["step"] = "ask_phone"
        set_pending_booking(chat_id, pb)

        send_telegram(chat_id, "\n".join([
            f"✅ 身分證已記錄",
            f"",
            f"📱 請輸入您的<b>手機號碼</b>：",
            f"",
            f"💡 例：0912345678",
            f"❌ 輸入 /cancel 取消",
        ]))
        return True

    if step == "ask_phone":
        phone = text.strip()
        if not re.match(r'^09\d{8}$', phone):
            send_telegram(chat_id, "\n".join([
                "❌ 手機號碼格式不正確",
                "",
                "✅ 正確格式：09 開頭 + 8 位數字",
                "📝 例：0912345678",
                "",
                "請重新輸入，或 /cancel 取消",
            ]))
            return True

        pb["phone"] = phone
        pb["step"] = "confirm"
        set_pending_booking(chat_id, pb)

        # 個人資料收集完畢 → 顯示確認畫面
        _send_booking_confirm(
            chat_id,
            pb["from_station"], pb["to_station"],
            pb["date"], pb["time"], pb["train_no"]
        )
        return True

    return False


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


def notify_admin(text: str):
    """通知所有管理員（Super Admin + env Admin）"""
    sent = set()
    if SUPERADMIN_CHAT_ID:
        send_telegram(SUPERADMIN_CHAT_ID, text)
        sent.add(SUPERADMIN_CHAT_ID)
    if ADMIN_TG_CHAT_ID and ADMIN_TG_CHAT_ID not in sent:
        send_telegram(ADMIN_TG_CHAT_ID, text)


# ═══════════════════════════════════════════════════════════
#  管理員審核通知
# ═══════════════════════════════════════════════════════════

def notify_admin_new_user(user_id: str, user_data: dict):
    """新用戶註冊 → 通知管理員 Telegram（附核准/拒絕按鈕）"""
    # 通知 Super Admin（優先）
    notify_targets = set()
    if SUPERADMIN_CHAT_ID:
        notify_targets.add(SUPERADMIN_CHAT_ID)
    if ADMIN_TG_CHAT_ID:
        notify_targets.add(ADMIN_TG_CHAT_ID)

    if not notify_targets:
        log.warning("⚠️ 無管理員可通知")
        return

    # 遮蔽敏感資訊：只顯示用戶名稱和 ID，不暴露完整 provider_id
    display_name = user_data.get('name', '未知')
    display_username = user_data.get('username', '')
    if display_username:
        display_account = f"@{display_username}"
    else:
        # 遮蔽 Chat ID 中間數字
        pid = str(user_data.get('provider_id', ''))
        if len(pid) > 4:
            display_account = f"{pid[:2]}***{pid[-2:]}"
        else:
            display_account = pid

    text = "\n".join([
        "🆕 <b>新用戶註冊申請</b>",
        "",
        f"👤 姓名：<b>{display_name}</b>",
        f"📡 來源：📨 Telegram",
        f"🔖 帳號：{display_account}",
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

    for target_id in notify_targets:
        send_telegram(target_id, text, reply_markup=buttons)


# get_pending_users / get_all_users 已從 firestore_db 匯入


def notify_pending_users_to_admin():
    """將所有待審核用戶重新推播給管理員（含核准/拒絕按鈕）"""
    pending = get_pending_users()
    if not pending:
        return 0

    for user_id, user_data in pending:
        notify_admin_new_user(user_id, user_data)

    log.info(f"📋 已重新推播 {len(pending)} 位待審核用戶給管理員")
    return len(pending)


def handle_pending_command(chat_id: str):
    """管理員指令：/pending — 查看並重新推播待審核名單"""
    pending = get_pending_users()
    if not pending:
        send_telegram(chat_id, "✅ 目前沒有待審核的用戶")
        return

    send_telegram(chat_id, f"📋 共 {len(pending)} 位待審核用戶，正在重新發送審核通知...")

    for user_id, user_data in pending:
        notify_admin_new_user(user_id, user_data)

    send_telegram(chat_id, f"✅ 已重新發送 {len(pending)} 筆審核通知，請查看上方按鈕")


def handle_listusers_command(chat_id: str):
    """管理員指令：/listusers — 列出所有用戶"""
    all_users = get_all_users()
    if not all_users:
        send_telegram(chat_id, "📋 目前沒有任何用戶")
        return

    status_emoji = {
        "approved": "✅",
        "pending": "⏳",
        "rejected": "❌",
    }
    role_emoji = {
        ROLE_SUPERADMIN: "👑",
        ROLE_ADMIN: "🔧",
        ROLE_USER: "👤",
    }

    lines = [f"📋 <b>用戶列表</b>（共 {len(all_users)} 人）", ""]

    for uid, udata in all_users:
        s = status_emoji.get(udata.get('status', ''), '❓')
        r = role_emoji.get(udata.get('role', ''), '👤')
        name = udata.get('name', '未知')
        status = udata.get('status', '未知')
        lines.append(f"{r}{s} <b>{name}</b> — {status}")
        lines.append(f"     🆔 <code>{uid}</code>")

    send_telegram(chat_id, "\n".join(lines))


def handle_admin_callback(data: dict):
    """處理管理員按下核准/拒絕按鈕"""
    cb = data.get("callback_query")
    if not cb or not cb.get("data"):
        return

    cb_data_str = cb.get("data", "")

    # 只處理 approve: 和 reject: 開頭的回調
    if not cb_data_str.startswith("approve:") and not cb_data_str.startswith("reject:"):
        return

    from_id = str(cb["from"]["id"])
    if not is_admin_telegram(from_id):
        answer_callback(cb["id"], "⚠️ 只有管理者可以執行此操作")
        return

    action, user_id = cb_data_str.split(":", 1)
    user = get_user(user_id)

    if not user:
        answer_callback(cb["id"], "❌ 找不到此用戶")
        return

    if user.get("status") != "pending":
        answer_callback(cb["id"], f"⚠️ 此用戶狀態已是：{user.get('status')}")
        # Still update the message
        if cb.get("message"):
            edit_telegram_message(
                str(cb["message"]["chat"]["id"]),
                cb["message"]["message_id"],
                f"ℹ️ 用戶 <b>{user.get('name', '未知')}</b> 已處理過（{user.get('status')}）",
            )
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
    if action == "approve":
        notify_text = (
            "🎉 <b>帳號已通過審核！</b>\n\n"
            "您現在可以使用所有功能了：\n"
            "📌 /settings — 查看設定\n"
            "📌 /timetable — 查詢時刻表\n"
            "📌 /book — 開始訂票\n"
            "📌 /help — 所有指令\n\n"
            "開始使用吧！🚀"
        )
    else:
        notify_text = (
            "⚠️ <b>帳號審核未通過</b>\n\n"
            "很抱歉，您的帳號未通過管理者審核。\n"
            "如有疑問，請聯繫管理者。"
        )

    if user.get("telegram_chat_id"):
        send_telegram(user["telegram_chat_id"], notify_text)

    log.info(f"🔐 Admin {'approved' if action == 'approve' else 'rejected'} user {user.get('name')} ({user_id})")


# ═══════════════════════════════════════════════════════════
#  統一指令處理器
# ═══════════════════════════════════════════════════════════

# 不需要核准就能用的指令
OPEN_COMMANDS = {"start", "help", "status", "search", "selfapprove"}


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
        "📖 <b>AutoTHSR 指令一覽</b>",
        "",
        "🔍 <b>快速查詢（推薦）</b>",
        "/search &lt;出發站&gt; &lt;到達站&gt; &lt;日期&gt; [時間]",
        "  <code>/search 高雄 台北 明天 18:00</code>",
        "  <code>/search 左營 台北 後天</code>",
        "  💡 /search （無參數）→ 互動表單",
        "",
        "🗂 <b>進階時刻表</b>",
        "/timetable &lt;出發站&gt; &lt;到達站&gt; &lt;日期&gt; [時間]",
        "",
        "🔧 <b>訂票設定</b>",
        "/from /to /date /time /count /seat /id /phone",
        "",
        "🚀 <b>操作</b>",
        "/book — 開始訂票 | /stop — 停止",
        "/status — 狀態 | /settings — 設定",
        "",
        "📊 <b>其他</b>",
        "/stations — 車站列表 | /help — 本說明",
        "",
        "🔐 <b>管理員指令</b>",
        "/pending — 重發待審核名單",
        "/listusers — 列出所有用戶",
        "/approve <code>user_id</code> — 核准用戶",
        "/reject <code>user_id</code> — 拒絕用戶",
        "",
        "🆘 <b>緊急指令</b>",
        "/selfapprove — Super Admin 自我核准",
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
    elif cmd == "search":
        # search 在 webhook 層處理（需要 reply_markup）
        return "💡 請直接使用 /search 指令"
    elif cmd == "timetable":
        return handle_timetable_command(args)
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
            if 1 <= n <= 1000:
                booking_config["max_retries"] = n
                return f"✅ 重試次數：<b>{n}</b>"
            return "❌ 請在 1-1000 之間"
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


def handle_timetable_command(args: str) -> str:
    """
    處理 /timetable 指令
    格式: /timetable <出發站> <到達站> <日期> [時間]
    範例: /timetable 台北 左營 2026/04/10 08:00
    """
    if not args.strip():
        return "\n".join([
            "🔍 <b>時刻表查詢用法</b>",
            "",
            "<code>/timetable 出發站 到達站 日期 [時間]</code>",
            "",
            "📝 <b>範例：</b>",
            "<code>/timetable 台北 左營 2026/04/10 08:00</code>",
            "<code>/timetable 南港 台中 2026/04/10</code>",
            "",
            "💡 時間可省略，預設為當前時間",
            f"🚉 可用車站：{' '.join(STATION_NAMES)}",
        ])

    parts = args.strip().split()

    if len(parts) < 3:
        return "❌ 格式錯誤\n用法: /timetable <出發站> <到達站> <日期> [時間]"

    from_st = parts[0]
    to_st = parts[1]
    date = parts[2].replace("-", "/")
    time_str = parts[3] if len(parts) >= 4 else ""

    # 驗證站名
    if from_st not in STATION_MAP:
        return f"❌ 無效出發站「{from_st}」\n🚉 {' '.join(STATION_NAMES)}"
    if to_st not in STATION_MAP:
        return f"❌ 無效到達站「{to_st}」\n🚉 {' '.join(STATION_NAMES)}"

    # 驗證日期格式
    try:
        datetime.strptime(date, "%Y/%m/%d")
    except ValueError:
        return "❌ 日期格式錯誤，請使用 YYYY/MM/DD（例: 2026/04/10）"

    return query_thsr_timetable(from_st, to_st, date, time_str)


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


# ═══════════════════════════════════════════════════════════
#  Telegram Webhook
# ═══════════════════════════════════════════════════════════

@app.route("/api/webhook/telegram", methods=["POST"])
def telegram_webhook():
    touch_session()
    data = request.get_json(silent=True) or {}

    # 處理按鈕回調 (管理員審核 + 搜尋表單 + 班次訂票)
    if "callback_query" in data:
        cb = data["callback_query"]
        cb_data = cb.get("data", "")
        cb_chat_id = str(cb["message"]["chat"]["id"]) if cb.get("message") else ""

        if cb_data.startswith("sf:"):
            # 搜尋表單回調
            answer_callback(cb["id"])
            text, markup, is_final = handle_form_callback(cb_chat_id, cb_data)

            if is_final:
                # 表單完成 → 執行查詢（帶按鈕）
                form = get_completed_form(cb_chat_id)
                if form:
                    clear_form(cb_chat_id)
                    result_text, result_buttons = query_thsr_timetable_with_buttons(
                        form["from_station"], form["to_station"],
                        form["date"], form["time"])
                    edit_telegram_message(cb_chat_id, cb["message"]["message_id"],
                                          "🔍 查詢中...")
                    if result_buttons:
                        send_telegram(cb_chat_id, result_text,
                                      reply_markup={"inline_keyboard": result_buttons})
                    else:
                        send_telegram(cb_chat_id, result_text)
                else:
                    send_telegram(cb_chat_id, "❌ 表單資料不完整，請重新 /search")
            elif markup:
                edit_telegram_message(cb_chat_id, cb["message"]["message_id"], text)
                send_telegram(cb_chat_id, text, reply_markup=markup)
            else:
                edit_telegram_message(cb_chat_id, cb["message"]["message_id"], text)
            return jsonify({"ok": True})

        if cb_data.startswith("bk:"):
            # 班次訂票回調: bk:<from>:<to>:<date>:<time>:<train_no>
            handle_train_booking_callback(cb, cb_data, cb_chat_id)
            return jsonify({"ok": True})

        if cb_data.startswith("bkconfirm:"):
            # 確認訂票回調
            handle_booking_confirm_callback(cb, cb_data, cb_chat_id)
            return jsonify({"ok": True})

        if cb_data.startswith("approve:") or cb_data.startswith("reject:"):
            handle_admin_callback(data)
            return jsonify({"ok": True})

        # 未知 callback — 回應但不做處理
        answer_callback(cb.get("id", ""), "⚠️ 未知操作")
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

    # ── 優先處理訂票個人資料輸入（身分證/手機）──
    if handle_pending_booking_input(chat_id, text):
        return jsonify({"ok": True})

    # 解析指令
    cmd_match = re.match(r"^/(\w+)(?:@\w+)?\s*(.*)", text, re.DOTALL)
    if not cmd_match:
        send_telegram(chat_id, "💡 請輸入指令，例如 /help")
        return jsonify({"ok": True})

    cmd = cmd_match.group(1).lower()
    args = cmd_match.group(2).strip()

    # ── 管理員自動核准 ──
    if is_admin_telegram(chat_id):
        admin_uid = f"tg_{chat_id}"
        admin_user = get_user(admin_uid)
        role = ROLE_SUPERADMIN if is_superadmin(chat_id) else ROLE_ADMIN
        if not admin_user or admin_user.get("status") != "approved":
            save_user(admin_uid, {
                "provider": "telegram", "provider_id": chat_id,
                "name": name or "Admin", "username": username,
                "status": "approved", "role": role,
                "telegram_chat_id": chat_id,
                "created_at": datetime.now().isoformat(),
                "reviewed_at": datetime.now().isoformat(),
            })
            log.info(f"🔐 {'👑 Super Admin' if role == ROLE_SUPERADMIN else '管理員'} {chat_id} 已自動核准")

    # /selfapprove → 緊急自我核准（僅 Super Admin 可用）
    if cmd == "selfapprove":
        if SUPERADMIN_CHAT_ID and str(chat_id) == str(SUPERADMIN_CHAT_ID):
            user_id = f"tg_{chat_id}"
            save_user(user_id, {
                "provider": "telegram",
                "provider_id": chat_id,
                "name": name or "Owner",
                "username": username,
                "status": "approved",
                "role": ROLE_SUPERADMIN,
                "telegram_chat_id": chat_id,
                "created_at": datetime.now().isoformat(),
                "reviewed_at": datetime.now().isoformat(),
            })
            log.info(f"👑 Super Admin {chat_id} 透過 /selfapprove 自我核准")
            send_telegram(chat_id, "\n".join([
                "👑 <b>Super Admin 已自我核准！</b>",
                "",
                f"Chat ID：<code>{chat_id}</code>",
                "狀態：<b>✅ 已核准</b>",
                "",
                "📌 /search — 快速查詢時刻表",
                "📌 /help — 所有指令",
                "📌 /pending — 查看待審核用戶",
            ]))
        else:
            send_telegram(chat_id, "❌ 此指令僅限 Super Admin 使用")
        return jsonify({"ok": True})

    # /start → 處理註冊
    if cmd == "start":
        if is_admin_telegram(chat_id):
            role_label = "👑 Super Admin" if is_superadmin(chat_id) else "🔧 Admin"
            send_telegram(chat_id, "\n".join([
                f"👑 嗨 <b>{name}</b>！歡迎回來 🚅",
                "",
                f"角色：<b>{role_label}</b>",
                "狀態：<b>✅ 已核准</b>",
                "",
                "📌 /search — 快速查詢時刻表",
                "📌 /help — 所有指令",
            ]))
            return jsonify({"ok": True})

        status, user_id = register_user(chat_id, name, username)

        if status == "new":
            send_telegram(chat_id, "\n".join([
                f"👋 嗨 <b>{name}</b>！歡迎使用 <b>ATO</b> 🚅",
                "",
                "📝 您的帳號已建立，目前狀態：<b>⏳ 待審核</b>",
                "",
                "管理者會收到通知，審核通過後您將收到訊息 📩",
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

    # /search → 智慧搜尋（管理員 + 已核准用戶）
    if cmd == "search":
        # 檢查權限（管理員直接通過，其他檢查核准）
        if not is_admin_telegram(chat_id) and not is_user_approved(f"tg_{chat_id}"):
            send_telegram(chat_id, "⏳ 請先 /start 註冊並等待審核")
            return jsonify({"ok": True})

        if args.strip():
            parsed = parse_smart_search(args)
            if parsed:
                send_telegram(chat_id, "🔍 查詢中...")
                result_text, result_buttons = query_thsr_timetable_with_buttons(
                    parsed["from_station"], parsed["to_station"],
                    parsed["date"], parsed["time"])
                if result_buttons:
                    send_telegram(chat_id, result_text,
                                  reply_markup={"inline_keyboard": result_buttons})
                else:
                    send_telegram(chat_id, result_text)
            else:
                send_telegram(chat_id, "\n".join([
                    "❌ 格式錯誤",
                    "",
                    "📝 <b>用法：</b>",
                    "<code>/search 出發站 到達站 日期 [時間]</code>",
                    "",
                    "📝 <b>範例：</b>",
                    "<code>/search 高雄 台北 明天 18:00</code>",
                    "<code>/search 左營 台北 2026/04/06</code>",
                    "",
                    "💡 支援：高雄→左營、北車→台北",
                    "💡 日期：今天/明天/後天 或 YYYY/MM/DD",
                ]))
        else:
            text, markup = start_search_form(chat_id)
            send_telegram(chat_id, text, reply_markup=markup)

        return jsonify({"ok": True})

    # 管理員專用指令
    if is_admin_telegram(chat_id):
        if cmd == "pending":
            handle_pending_command(chat_id)
            return jsonify({"ok": True})
        if cmd == "listusers":
            handle_listusers_command(chat_id)
            return jsonify({"ok": True})
        if cmd == "approve" and args.strip():
            target_uid = args.strip()
            if approve_user(target_uid):
                target_user = get_user(target_uid)
                send_telegram(chat_id, f"✅ 已核准用戶：<b>{target_user.get('name', target_uid)}</b>")
                # 通知被核准的用戶
                if target_user and target_user.get("telegram_chat_id"):
                    send_telegram(target_user["telegram_chat_id"],
                        "🎉 <b>帳號已通過審核！</b>\n\n"
                        "您現在可以使用所有功能了：\n"
                        "📌 /search — 查詢時刻表\n"
                        "📌 /help — 所有指令\n\n"
                        "開始使用吧！🚀")
            else:
                send_telegram(chat_id, f"❌ 找不到用戶 <code>{target_uid}</code>\n💡 用 /listusers 查看所有用戶 ID")
            return jsonify({"ok": True})
        if cmd == "reject" and args.strip():
            target_uid = args.strip()
            if reject_user(target_uid):
                send_telegram(chat_id, f"❌ 已拒絕用戶：<code>{target_uid}</code>")
            else:
                send_telegram(chat_id, f"❌ 找不到用戶 <code>{target_uid}</code>")
            return jsonify({"ok": True})

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
#  Health & API Routes
# ═══════════════════════════════════════════════════════════

@app.route("/api/health", methods=["GET"])
def health():
    elapsed = (datetime.now() - _last_activity).total_seconds()
    session_remaining = max(0, SESSION_TIMEOUT - elapsed)

    # 檢查 Firestore 連線
    try:
        get_firestore_db()
        firestore_ok = True
    except Exception:
        firestore_ok = False

    return jsonify({
        "status": "ok",
        "service": "ATO",
        "timestamp": datetime.now().isoformat(),
        "telegram_bot": bool(TG_TOKEN),
        "firestore": firestore_ok,
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
        "name": "🚅 ATO",
        "version": "3.1",
        "description": "高鐵自動訂票 + 時刻表查詢 — Telegram Bot + Firestore 持久化",
        "session_timeout": "6 小時",
        "endpoints": {
            "health": "/api/health",
            "telegram_webhook": "/api/webhook/telegram",
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
        {"command": "search", "description": "🔍 快速查詢時刻表"},
        {"command": "help", "description": "顯示說明"},
        {"command": "timetable", "description": "查詢高鐵時刻表（進階）"},
        {"command": "settings", "description": "查看設定"},
        {"command": "book", "description": "開始訂票"},
        {"command": "stop", "description": "停止訂票"},
        {"command": "status", "description": "訂票狀態"},
        {"command": "stations", "description": "車站列表"},
        {"command": "pending", "description": "🔐 待審核名單（管理員）"},
        {"command": "listusers", "description": "🔐 所有用戶（管理員）"},
        {"command": "approve", "description": "🔐 核准用戶（管理員）"},
        {"command": "reject", "description": "🔐 拒絕用戶（管理員）"},
        {"command": "selfapprove", "description": "🆘 Super Admin 自我核准"},
    ]
    url = f"https://api.telegram.org/bot{TG_TOKEN}/setMyCommands"
    try:
        requests.post(url, json={"commands": commands}, timeout=10)
        log.info("  ✅ Telegram 指令選單已設定")
    except Exception:
        pass


def startup():
    global _keepalive_thread

    log.info("🚅 ATO v3.2 啟動中...")
    log.info(f"  Telegram Bot: {'✅' if TG_TOKEN else '❌'}")
    log.info(f"  Admin TG Chat: {'✅ ' + ADMIN_TG_CHAT_ID if ADMIN_TG_CHAT_ID else '❌'}")
    log.info(f"  Render URL: {RENDER_EXTERNAL_URL or '(local)'}")
    log.info(f"  Session: {SESSION_TIMEOUT // 3600}h | Keep-alive: {KEEPALIVE_INTERVAL // 60}min")

    # ── 初始化 Firestore（失敗不會 crash，但後續跳過 DB 操作）──
    firestore_ok = False
    try:
        get_firestore_db()
        firestore_ok = is_firestore_available()
        if firestore_ok:
            log.info("  🔥 Firestore 已連線")
        else:
            log.error("  ❌ Firestore 初始化後仍不可用")
    except Exception as e:
        log.error(f"  ❌ Firestore 連線失敗: {e}")

    if not firestore_ok:
        log.error("  ⚠️ Firestore 不可用 — 用戶資料將無法持久化！")
        log.error("  ⚠️ 跳過 Super Admin / Admin 帳號初始化")

    register_telegram_webhook()
    set_telegram_commands()

    # ── 以下全部依賴 Firestore，不可用時跳過 ──
    if firestore_ok:
        # Super Admin 自動建立/修復（最高權限，強制核准）
        if SUPERADMIN_CHAT_ID:
            sa_id = f"tg_{SUPERADMIN_CHAT_ID}"
            sa_user = get_user(sa_id)
            needs_update = (
                not sa_user
                or sa_user.get("role") != ROLE_SUPERADMIN
                or sa_user.get("status") != "approved"
            )
            if needs_update:
                existing_name = sa_user.get("name", "Owner") if sa_user else "Owner"
                existing_username = sa_user.get("username", "") if sa_user else ""
                save_user(sa_id, {
                    "provider": "telegram",
                    "provider_id": SUPERADMIN_CHAT_ID,
                    "name": existing_name,
                    "username": existing_username,
                    "status": "approved",
                    "role": ROLE_SUPERADMIN,
                    "telegram_chat_id": SUPERADMIN_CHAT_ID,
                    "created_at": sa_user.get("created_at", datetime.now().isoformat()) if sa_user else datetime.now().isoformat(),
                    "reviewed_at": datetime.now().isoformat(),
                })
                log.info(f"  👑 Super Admin 帳號已{'修復' if sa_user else '建立'}並強制核准")
            else:
                log.info("  👑 Super Admin 帳號已存在且已核准")
        else:
            log.warning("  ⚠️ SUPERADMIN_CHAT_ID 未設定，無法建立 Super Admin")

        # 環境變數指定的管理員
        if ADMIN_TG_CHAT_ID and ADMIN_TG_CHAT_ID != SUPERADMIN_CHAT_ID:
            admin_id = f"tg_{ADMIN_TG_CHAT_ID}"
            if not get_user(admin_id):
                save_user(admin_id, {
                    "provider": "telegram",
                    "provider_id": ADMIN_TG_CHAT_ID,
                    "name": "Admin",
                    "username": "",
                    "status": "approved",
                    "role": ROLE_ADMIN,
                    "telegram_chat_id": ADMIN_TG_CHAT_ID,
                    "created_at": datetime.now().isoformat(),
                    "reviewed_at": datetime.now().isoformat(),
                })
                log.info("  ✅ 管理員帳號已自動建立")

    _keepalive_thread = threading.Thread(target=keepalive_worker, daemon=True)
    _keepalive_thread.start()
    log.info("  🏓 Keep-alive 已啟動")

    # 通知管理員服務啟動
    db_status = "🔥 Firestore: ✅" if firestore_ok else "⚠️ Firestore: ❌ 離線模式"
    pending_count = len(get_pending_users()) if firestore_ok else 0
    pending_info = f"\n⏳ 待審核用戶：{pending_count} 人" if pending_count > 0 else ""
    notify_admin("🚅 <b>ATO 已啟動</b>\n\n"
                 f"Telegram: {'✅' if TG_TOKEN else '❌'}\n"
                 f"{db_status}\n"
                 f"Session: {SESSION_TIMEOUT // 3600}h\n"
                 f"時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                 f"{pending_info}")

    # 啟動時自動重新推播待審核用戶
    if firestore_ok and pending_count > 0:
        log.info(f"📋 發現 {pending_count} 位待審核用戶，重新推播審核通知...")
        notify_pending_users_to_admin()


# ═══════════════════════════════════════════════════════════
#  Entry Point
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    startup()
    app.run(host="0.0.0.0", port=PORT, debug=False)
