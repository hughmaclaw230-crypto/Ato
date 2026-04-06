#!/usr/bin/env python3
"""
Telegram 互動表單模組 — 高鐵時刻表查詢 / 訂票設定
使用 Inline Keyboard 逐步引導用戶完成表單
"""

import logging
import threading
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# ─── 表單狀態管理 ─────────────────────────────────────
_form_states = {}  # chat_id -> { step, from_station, to_station, date, time }
_form_lock = threading.Lock()

STATIONS = ["南港", "台北", "板橋", "桃園", "新竹", "苗栗",
            "台中", "彰化", "雲林", "嘉義", "台南", "左營"]

AFTERNOON_TIMES = [
    "12:00", "12:30", "13:00", "13:30", "14:00", "14:30",
    "15:00", "15:30", "16:00", "16:30", "17:00", "17:30",
    "18:00", "18:30", "19:00", "19:30", "20:00", "20:30",
    "21:00", "21:30", "22:00", "22:30",
]

MORNING_TIMES = [
    "06:00", "06:30", "07:00", "07:30", "08:00", "08:30",
    "09:00", "09:30", "10:00", "10:30", "11:00", "11:30",
]


def get_form(chat_id: str) -> dict | None:
    with _form_lock:
        return _form_states.get(chat_id)


def set_form(chat_id: str, data: dict):
    with _form_lock:
        _form_states[chat_id] = data


def clear_form(chat_id: str):
    with _form_lock:
        _form_states.pop(chat_id, None)


# ─── 表單啟動 ──────────────────────────────────────────

def start_search_form(chat_id: str) -> tuple[str, dict]:
    """啟動查詢表單，回傳 (text, reply_markup)"""
    set_form(chat_id, {"step": "from_station"})

    text = "🔍 <b>高鐵時刻表查詢</b>\n\n📍 請選擇<b>出發站</b>："

    buttons = []
    for i in range(0, len(STATIONS), 3):
        row = []
        for st in STATIONS[i:i+3]:
            row.append({"text": st, "callback_data": f"sf:from:{st}"})
        buttons.append(row)
    buttons.append([{"text": "❌ 取消", "callback_data": "sf:cancel"}])

    return text, {"inline_keyboard": buttons}


def handle_form_callback(chat_id: str, cb_data: str) -> tuple[str, dict | None, bool]:
    """
    處理表單回調
    回傳: (text, reply_markup_or_None, is_final)
    is_final=True 表示表單完成，text 是時刻表查詢結果
    """
    parts = cb_data.split(":", 2)
    if len(parts) < 2:
        return "❌ 無效操作", None, False

    action = parts[1]

    if action == "cancel":
        clear_form(chat_id)
        return "❌ 已取消查詢", None, False

    form = get_form(chat_id)
    if not form:
        return "⚠️ 表單已過期，請重新 /search", None, False

    step = form.get("step", "")

    # ── Step 1: 出發站 ──
    if step == "from_station" and action == "from":
        station = parts[2] if len(parts) > 2 else ""
        if station not in STATIONS:
            return "❌ 無效站名", None, False
        form["from_station"] = station
        form["step"] = "to_station"
        set_form(chat_id, form)

        text = (f"🔍 <b>高鐵時刻表查詢</b>\n\n"
                f"🟢 出發站：<b>{station}</b>\n\n"
                f"📍 請選擇<b>到達站</b>：")

        buttons = []
        for i in range(0, len(STATIONS), 3):
            row = []
            for st in STATIONS[i:i+3]:
                if st == station:
                    continue
                row.append({"text": st, "callback_data": f"sf:to:{st}"})
            if row:
                buttons.append(row)
        buttons.append([{"text": "⬅️ 上一步", "callback_data": "sf:back:from"},
                        {"text": "❌ 取消", "callback_data": "sf:cancel"}])

        return text, {"inline_keyboard": buttons}, False

    # ── Step 2: 到達站 ──
    if step == "to_station" and action == "to":
        station = parts[2] if len(parts) > 2 else ""
        if station not in STATIONS:
            return "❌ 無效站名", None, False
        form["to_station"] = station
        form["step"] = "date"
        set_form(chat_id, form)

        return _build_date_step(form)

    # ── Step 3: 日期 ──
    if step == "date" and action == "date":
        date_val = parts[2] if len(parts) > 2 else ""
        form["date"] = date_val
        form["step"] = "time"
        set_form(chat_id, form)

        return _build_time_step(form)

    # ── Step 4: 時間 ──
    if step == "time" and action == "time":
        time_val = parts[2] if len(parts) > 2 else ""
        form["time"] = time_val
        form["step"] = "confirm"
        set_form(chat_id, form)

        return _build_confirm_step(form)

    # ── 時段切換 ──
    if action == "timegroup":
        group = parts[2] if len(parts) > 2 else "am"
        return _build_time_step(form, group=group)

    # ── 確認查詢 ──
    if action == "exec":
        # 注意：不在此處 clear_form，由 app.py 讀取表單資料後再清除
        return "", None, True  # is_final=True

    # ── 返回上一步 ──
    if action == "back":
        target = parts[2] if len(parts) > 2 else ""
        if target == "from":
            form["step"] = "from_station"
            form.pop("from_station", None)
            set_form(chat_id, form)
            _, markup = start_search_form(chat_id)
            return "🔍 <b>高鐵時刻表查詢</b>\n\n📍 請選擇<b>出發站</b>：", markup, False
        elif target == "to":
            form["step"] = "from_station"
            set_form(chat_id, form)
            # Re-trigger from step with saved from_station
            return handle_form_callback(chat_id, f"sf:from:{form.get('from_station','台北')}")
        elif target == "date":
            form["step"] = "to_station"
            set_form(chat_id, form)
            return handle_form_callback(chat_id, f"sf:to:{form.get('to_station','左營')}")
        elif target == "time":
            form["step"] = "date"
            set_form(chat_id, form)
            return _build_date_step(form)

    return "❓ 未知操作", None, False


# ─── 表單步驟構建 ──────────────────────────────────────

def _build_date_step(form: dict) -> tuple[str, dict, bool]:
    today = datetime.now()
    text = (f"🔍 <b>高鐵時刻表查詢</b>\n\n"
            f"🟢 出發站：<b>{form['from_station']}</b>\n"
            f"🟢 到達站：<b>{form['to_station']}</b>\n\n"
            f"📅 請選擇<b>日期</b>：")

    buttons = []
    for i in range(7):
        d = today + timedelta(days=i)
        label = d.strftime("%m/%d")
        weekday = ["一", "二", "三", "四", "五", "六", "日"][d.weekday()]
        if i == 0:
            label += " (今天)"
        elif i == 1:
            label += " (明天)"
        else:
            label += f" ({weekday})"
        date_str = d.strftime("%Y/%m/%d")
        buttons.append([{"text": f"📅 {label}", "callback_data": f"sf:date:{date_str}"}])

    buttons.append([{"text": "⬅️ 上一步", "callback_data": "sf:back:to"},
                    {"text": "❌ 取消", "callback_data": "sf:cancel"}])

    return text, {"inline_keyboard": buttons}, False


def _build_time_step(form: dict, group: str = "am") -> tuple[str, dict, bool]:
    group_label = "上午 06:00-11:30" if group == "am" else "下午 12:00-22:30"
    text = (f"🔍 <b>高鐵時刻表查詢</b>\n\n"
            f"🟢 出發站：<b>{form['from_station']}</b>\n"
            f"🟢 到達站：<b>{form['to_station']}</b>\n"
            f"🟢 日期：<b>{form.get('date','')}</b>\n\n"
            f"🕐 請選擇<b>出發時間</b>（{group_label}）：")

    times = AFTERNOON_TIMES if group == "pm" else MORNING_TIMES

    buttons = []
    # 時段切換
    toggle = [
        {"text": "🌅 上午" + (" ✓" if group == "am" else ""),
         "callback_data": "sf:timegroup:am"},
        {"text": "🌆 下午" + (" ✓" if group == "pm" else ""),
         "callback_data": "sf:timegroup:pm"},
    ]
    buttons.append(toggle)

    for i in range(0, len(times), 4):
        row = []
        for t in times[i:i+4]:
            row.append({"text": t, "callback_data": f"sf:time:{t}"})
        buttons.append(row)

    buttons.append([{"text": "⬅️ 上一步", "callback_data": "sf:back:date"},
                    {"text": "❌ 取消", "callback_data": "sf:cancel"}])

    return text, {"inline_keyboard": buttons}, False


def _build_confirm_step(form: dict) -> tuple[str, dict, bool]:
    text = (f"🔍 <b>高鐵時刻表查詢 — 確認</b>\n\n"
            f"🚉 路線：<b>{form['from_station']} → {form['to_station']}</b>\n"
            f"📅 日期：<b>{form.get('date','')}</b>\n"
            f"🕐 時間：<b>{form.get('time','')} 起</b>\n\n"
            f"確認查詢？")

    buttons = {"inline_keyboard": [
        [{"text": "🔍 確認查詢", "callback_data": "sf:exec:go"}],
        [{"text": "⬅️ 修改時間", "callback_data": "sf:back:time"},
         {"text": "❌ 取消", "callback_data": "sf:cancel"}],
    ]}

    return text, buttons, False


def get_completed_form(chat_id: str) -> dict | None:
    """取得已完成的表單資料（from_station, to_station, date, time）"""
    form = get_form(chat_id)
    if not form:
        return None
    if all(k in form for k in ["from_station", "to_station", "date", "time"]):
        return form
    return None


# ─── 智慧搜尋解析 ──────────────────────────────────────

STATION_ALIASES = {
    "高雄": "左營", "左營(高雄)": "左營", "高雄站": "左營",
    "台北車站": "台北", "北車": "台北", "台北站": "台北",
    "板橋站": "板橋", "桃園站": "桃園", "新竹站": "新竹",
    "台中站": "台中", "嘉義站": "嘉義", "台南站": "台南",
    "南港站": "南港", "苗栗站": "苗栗", "彰化站": "彰化", "雲林站": "雲林",
}


def resolve_station(name: str) -> str:
    """站名別名解析"""
    return STATION_ALIASES.get(name, name)


def parse_relative_date(text: str) -> str:
    """解析相對日期 (今天/明天/後天) 或直接回傳"""
    today = datetime.now()
    mapping = {
        "今天": 0, "今日": 0,
        "明天": 1, "明日": 1,
        "後天": 2, "后天": 2,
        "大後天": 3,
    }
    if text in mapping:
        d = today + timedelta(days=mapping[text])
        return d.strftime("%Y/%m/%d")
    return text.replace("-", "/")


def parse_smart_search(args: str) -> dict | None:
    """
    解析一行式搜尋: 左營 台北 明天 18:00
    回傳 { from_station, to_station, date, time } 或 None
    """
    import re
    parts = args.strip().split()
    if len(parts) < 3:
        return None

    from_st = resolve_station(parts[0])
    to_st = resolve_station(parts[1])

    if from_st not in STATIONS or to_st not in STATIONS:
        return None

    date_str = parse_relative_date(parts[2])
    time_str = parts[3] if len(parts) >= 4 else ""

    # 驗證日期
    try:
        datetime.strptime(date_str, "%Y/%m/%d")
    except ValueError:
        return None

    # 驗證時間格式
    if time_str and not re.match(r"^\d{2}:\d{2}$", time_str):
        return None

    return {
        "from_station": from_st,
        "to_station": to_st,
        "date": date_str,
        "time": time_str,
    }
