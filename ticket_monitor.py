#!/usr/bin/env python3
"""
THSR Ticket Monitor — 高鐵票券可用性監控引擎
靈感來自 scott0127/thsr-ticket-monitor

純 requests 實作（輕量，適合 Render），持續監控指定路線/日期的票券可用性
找到可訂票班次後透過 Telegram 通知用戶，不自動訂票

監控策略：
1. 定期查詢高鐵訂票頁面
2. 提交查詢表單（含驗證碼辨識）
3. 若進入選車次頁 → 有票，發送通知
4. 若查無可售車次 → 等待後重試
5. 驗證碼錯誤 → 立即重試（不計入冷卻）

參考：
- scott0127/thsr-ticket-monitor (Selenium 版)
- 本專案 booking_engine.py (requests 版)
"""

import os
import re
import time
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import requests

log = logging.getLogger(__name__)

THSRC_BASE = "https://irs.thsrc.com.tw/IMINT/?locale=tw"

STATION_MAP = {
    "南港": "1", "台北": "2", "板橋": "3", "桃園": "4",
    "新竹": "5", "苗栗": "6", "台中": "7", "彰化": "8",
    "雲林": "9", "嘉義": "10", "台南": "11", "左營": "12",
}

STATION_REVERSE = {v: k for k, v in STATION_MAP.items()}

SEAT_MAP = {"無座位偏好": "0", "靠窗": "1", "靠走道": "2"}

TIME_VALUE_MAP = {
    "00:00": "1201A",
    "05:30": "0530A", "06:00": "0600A", "06:30": "0630A",
    "07:00": "0700A", "07:30": "0730A",
    "08:00": "0800A", "08:30": "0830A",
    "09:00": "0900A", "09:30": "0930A",
    "10:00": "1000A", "10:30": "1030A",
    "11:00": "1100A", "11:30": "1130A",
    "12:00": "1200N", "12:30": "1230A",
    "13:00": "0100P", "13:30": "0130P",
    "14:00": "0200P", "14:30": "0230P",
    "15:00": "0300P", "15:30": "0330P",
    "16:00": "0400P", "16:30": "0430P",
    "17:00": "0500P", "17:30": "0530P",
    "18:00": "0600P", "18:30": "0630P",
    "19:00": "0700P", "19:30": "0730P",
    "20:00": "0800P", "20:30": "0830P",
    "21:00": "0900P", "21:30": "0930P",
    "22:00": "1000P", "22:30": "1030P",
    "23:00": "1100P", "23:30": "1130P",
}


def _convert_time(time_str: str) -> str:
    """將 HH:MM 轉為高鐵表單值"""
    if time_str in TIME_VALUE_MAP:
        return TIME_VALUE_MAP[time_str]
    try:
        h, m = map(int, time_str.split(":"))
        if h == 0:
            return "1201A"
        elif h < 12:
            return f"{h:02d}{m:02d}A"
        elif h == 12:
            return "1200N" if m == 0 else f"12{m:02d}A"
        else:
            return f"{h - 12:02d}{m:02d}P"
    except (ValueError, AttributeError):
        return "0800A"


# ─── HTML 解析 ─────────────────────────────────────────

def _extract_form_action(html: str) -> Optional[str]:
    patterns = [
        r'<form[^>]*id="BookingS1Form"[^>]*action="([^"]+)"',
        r'<form[^>]*action="([^"]*BookingS1Form[^"]*)"',
        r'action="(/IMINT/[^"]*IFormSubmitListener[^"]*)"',
    ]
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            return m.group(1).replace("&amp;", "&")
    return None


def _extract_captcha_url(html: str, page_url: str) -> Optional[str]:
    patterns = [
        r'<img[^>]*id="BookingS1Form_homeCaptcha_captchaImage"[^>]*src="([^"]+)"',
        r'<img[^>]*id="BookingS1Form_homeCaptcha_passCode"[^>]*src="([^"]+)"',
        r'<img[^>]*src="([^"]*captcha[^"]*)"',
        r'<img[^>]*src="([^"]*passCode[^"]*IResourceListener[^"]*)"',
    ]
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            src = m.group(1).replace("&amp;", "&")
            if src.startswith("/"):
                return f"https://irs.thsrc.com.tw{src}"
            elif src.startswith("http"):
                return src
            else:
                return urljoin(page_url, src)
    return None


def _extract_hidden_fields(html: str) -> dict:
    fields = {}
    for m in re.finditer(
        r'<input[^>]*type="hidden"[^>]*name="([^"]*)"[^>]*value="([^"]*)"',
        html, re.IGNORECASE
    ):
        fields[m.group(1)] = m.group(2)
    for m in re.finditer(
        r'<input[^>]*value="([^"]*)"[^>]*type="hidden"[^>]*name="([^"]*)"',
        html, re.IGNORECASE
    ):
        fields[m.group(2)] = m.group(1)
    for m in re.finditer(
        r'<input[^>]*name="([^"]*)"[^>]*type="hidden"[^>]*value="([^"]*)"',
        html, re.IGNORECASE
    ):
        if m.group(1) not in fields:
            fields[m.group(1)] = m.group(2)
    return fields


def _extract_error(html: str) -> Optional[str]:
    patterns = [
        r'<span[^>]*class="feedbackPanelERROR"[^>]*>(.*?)</span>',
        r'<div[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)</div>',
        r'<ul[^>]*class="feedbackPanel"[^>]*>.*?<li[^>]*>(.*?)</li>',
    ]
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE | re.DOTALL)
        if m:
            text = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            if text:
                return text[:200]
    return None


def _parse_available_trains(html: str) -> list:
    """從結果頁解析可訂票班次資訊"""
    trains = []
    radio_pattern = r'<input[^>]*name="TrainQueryDataViewPanel:TrainGroup"[^>]*value="([^"]*)"'
    values = re.findall(radio_pattern, html, re.IGNORECASE)
    if not values:
        radio_pattern = r'<input[^>]*value="([^"]*)"[^>]*name="TrainQueryDataViewPanel:TrainGroup"'
        values = re.findall(radio_pattern, html, re.IGNORECASE)

    for val in values:
        info = {"value": val}
        tr_pattern = rf'<tr[^>]*>(?:(?!</tr>).)*?value="{re.escape(val)}"(?:(?!</tr>).)*?</tr>'
        tr_match = re.search(tr_pattern, html, re.IGNORECASE | re.DOTALL)
        if tr_match:
            td_texts = re.findall(r'<td[^>]*>(.*?)</td>', tr_match.group(0), re.DOTALL)
            td_texts = [re.sub(r'<[^>]+>', '', t).strip() for t in td_texts]
            if len(td_texts) >= 4:
                info["train_no"] = td_texts[1] if len(td_texts) > 1 else ""
                info["depart"] = td_texts[2] if len(td_texts) > 2 else ""
                info["arrive"] = td_texts[3] if len(td_texts) > 3 else ""
        trains.append(info)
    return trains


# ─── 監控主邏輯 ─────────────────────────────────────────

def run_monitor(config: dict, status: dict, notify_fn=None) -> dict:
    """
    票券監控主函式

    config 格式:
        from_station: 中文站名
        to_station:   中文站名
        travel_date:  YYYY-MM-DD 或 YYYY/MM/DD
        travel_time:  HH:MM
        check_interval: 查無票冷卻秒數 (預設 90)
        max_checks:     最大監控輪數 (預設 200, 約 5 小時)
        adult_count:    全票數量 (預設 1)

    status 格式 (dict, 由外部傳入, 可讀取/修改):
        running:      bool
        checks:       int — 已完成的監控輪數
        captcha_ok:   int — 驗證碼通過次數
        last_error:   str

    notify_fn: 回呼函式 notify_fn(message: str) 用於發送通知

    回傳:
        {"found": True/False, "trains": [...], "error": "..."}
    """
    from booking_engine import decode_captcha

    from_name = config["from_station"]
    to_name = config["to_station"]
    from_code = STATION_MAP.get(from_name)
    to_code = STATION_MAP.get(to_name)
    if not from_code or not to_code:
        return {"found": False, "error": f"站名錯誤: {from_name} / {to_name}"}

    date_val = config["travel_date"].replace("-", "/")
    time_str = config["travel_time"]
    time_form_val = _convert_time(time_str)
    adult_count = config.get("adult_count", 1)

    check_interval = config.get("check_interval", 90)
    max_checks = config.get("max_checks", 200)
    captcha_retry_limit = config.get("captcha_retry_limit", 30)

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    })

    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    status["checks"] = 0
    status["captcha_ok"] = 0
    status["last_error"] = ""

    def _notify(msg: str):
        log.info(msg)
        if notify_fn:
            try:
                notify_fn(msg)
            except Exception as e:
                log.error(f"通知失敗: {e}")

    _notify(
        f"🔍 高鐵監控啟動\n"
        f"路線：{from_name} → {to_name}\n"
        f"日期：{date_val}　時間：{time_str}\n"
        f"間隔：{check_interval}s　最大輪數：{max_checks}"
    )

    for check_round in range(1, max_checks + 1):
        if not status.get("running", True):
            _notify("⏹️ 監控已手動停止")
            return {"found": False, "error": "手動停止"}

        status["checks"] = check_round
        captcha_retries = 0

        while captcha_retries < captcha_retry_limit:
            if not status.get("running", True):
                return {"found": False, "error": "手動停止"}

            try:
                # Step 1: 取得訂票頁面
                resp = session.get(THSRC_BASE, timeout=20, verify=False)
                if resp.status_code != 200:
                    log.warning(f"取頁面失敗: HTTP {resp.status_code}")
                    time.sleep(3)
                    captcha_retries += 1
                    continue

                html = resp.text
                page_url = resp.url

                form_action = _extract_form_action(html)
                captcha_url = _extract_captcha_url(html, page_url)
                if not form_action or not captcha_url:
                    log.warning("找不到表單/驗證碼")
                    time.sleep(3)
                    captcha_retries += 1
                    continue

                # Step 2: 下載 & 辨識驗證碼
                cap_resp = session.get(captcha_url, timeout=10, verify=False)
                if cap_resp.status_code != 200 or len(cap_resp.content) < 100:
                    time.sleep(2)
                    captcha_retries += 1
                    continue

                captcha_text = decode_captcha(cap_resp.content)
                if len(captcha_text) != 4:
                    captcha_retries += 1
                    continue

                # Step 3: 提交查詢表單
                if form_action.startswith("/"):
                    action_url = f"https://irs.thsrc.com.tw{form_action}"
                elif form_action.startswith("http"):
                    action_url = form_action
                else:
                    action_url = urljoin(page_url, form_action)

                hidden = _extract_hidden_fields(html)
                form_data = {
                    **hidden,
                    "selectStartStation": from_code,
                    "selectDestinationStation": to_code,
                    "trainCon:trainRadioGroup": "0",
                    "tripCon:typesoftrip": "0",
                    "seatCon:seatRadioGroup": "0",
                    "trainCon:trainDate": date_val,
                    "trainCon:trainTime": time_form_val,
                    "trainCon:trainNumber": "",
                    "ticketCon:ticketCasualRecord:0:ticketCount": str(adult_count),
                    "ticketCon:ticketCasualRecord:1:ticketCount": "0",
                    "ticketCon:ticketCasualRecord:2:ticketCount": "0",
                    "ticketCon:ticketCasualRecord:3:ticketCount": "0",
                    "ticketCon:ticketCasualRecord:4:ticketCount": "0",
                    "homeCaptcha:securityCode": captcha_text,
                    "SubmitButton": "開始查詢",
                }

                submit_resp = session.post(
                    action_url, data=form_data, timeout=20,
                    verify=False, allow_redirects=True,
                )
                result_html = submit_resp.text
                result_url = submit_resp.url

                # Step 4: 判斷結果

                # ✅ 有票！進入選車次頁
                if ("BookingS2" in result_url or
                    "確認車次" in result_html or
                    "TrainQueryDataViewPanel" in result_html):

                    status["captcha_ok"] += 1
                    trains = _parse_available_trains(result_html)
                    train_count = len(trains)

                    log.info(f"🎉 第 {check_round} 輪：找到 {train_count} 個可訂票班次！")

                    # 組合通知訊息
                    train_lines = []
                    for t in trains[:8]:  # 最多顯示 8 個班次
                        no = t.get("train_no", "?")
                        dep = t.get("depart", "?")
                        arr = t.get("arrive", "?")
                        train_lines.append(f"  🚅 {no}  {dep} → {arr}")

                    notify_msg = "\n".join([
                        "🎉🎉🎉",
                        "",
                        f"✅ <b>高鐵有票了！</b>",
                        f"═══════════════",
                        f"🚉 {from_name} → {to_name}",
                        f"📅 {date_val}　🕐 {time_str} 起",
                        f"🎫 找到 {train_count} 個班次",
                        "",
                        *train_lines,
                        "",
                        f"═══════════════",
                        f"🔄 共監控 {check_round} 輪",
                        "",
                        "⚡ <b>立即訂票：</b>",
                        '🖥 <a href="https://irs.thsrc.com.tw/IMINT/?locale=tw">電腦版訂票</a>',
                        '📱 <a href="https://m.thsrc.com.tw/tw/TimeTable/SearchResult">行動版訂票</a>',
                        '🏪 <a href="https://www.thsrc.com.tw/ArticleContent/a3b630bb-1066-4352-a1ef-58c7b4e8ef7c">超商取票</a>',
                        "",
                        "💡 或使用 /book 自動訂票",
                    ])
                    _notify(notify_msg)

                    return {
                        "found": True,
                        "trains": trains,
                        "round": check_round,
                    }

                # 檢查錯誤
                error_msg = _extract_error(result_html)

                # ❌ 驗證碼錯誤 → 重試
                if error_msg and ("驗證碼" in error_msg or "security" in error_msg.lower()):
                    log.info(f"⏳ R{check_round}: 驗證碼 '{captcha_text}' 錯誤，重試...")
                    captcha_retries += 1
                    time.sleep(1)
                    continue

                # ⚠️ 查無可售車次 / 已售完 → 進入冷卻
                if error_msg and ("班次" in error_msg or "無" in error_msg or "沒有" in error_msg or "售完" in error_msg):
                    status["captcha_ok"] += 1
                    log.info(f"⏳ R{check_round}: 查無班次，冷卻 {check_interval}s...")
                    status["last_error"] = error_msg
                    break  # 跳出 captcha 重試循環，進入冷卻

                # 也檢查常見的「去程查無可售車次」
                if "去程查無可售車次" in result_html or "選購的車票已售完" in result_html:
                    status["captcha_ok"] += 1
                    log.info(f"⏳ R{check_round}: 查無班次，冷卻 {check_interval}s...")
                    status["last_error"] = "查無可售車次"
                    break

                # ⚠️ 請求過多
                if error_msg and ("過多" in error_msg or "too many" in error_msg.lower()):
                    log.warning(f"R{check_round}: 請求過多，冷卻 {check_interval * 2}s...")
                    time.sleep(check_interval * 2)
                    break

                # 未知結果
                log.warning(f"R{check_round}: 未知結果 (URL={result_url[:60]})")
                captcha_retries += 1
                time.sleep(2)

            except requests.exceptions.Timeout:
                log.warning("請求逾時")
                captcha_retries += 1
                time.sleep(3)
            except Exception as e:
                log.error(f"監控例外: {e}")
                captcha_retries += 1
                time.sleep(3)

        # 冷卻等待（每 10 秒檢查一次是否被停止）
        elapsed = 0
        while elapsed < check_interval:
            if not status.get("running", True):
                _notify("⏹️ 監控已手動停止")
                return {"found": False, "error": "手動停止"}
            wait = min(10, check_interval - elapsed)
            time.sleep(wait)
            elapsed += wait

        # 每 10 輪發一次進度通知
        if check_round % 10 == 0:
            _notify(
                f"📊 監控進度\n"
                f"已完成 {check_round}/{max_checks} 輪\n"
                f"驗證碼通過 {status['captcha_ok']} 次\n"
                f"最後狀態：{status.get('last_error', '正常')}"
            )

    # 監控結束
    _notify(
        f"⏰ 監控結束\n"
        f"共 {max_checks} 輪未找到票\n"
        f"路線：{from_name} → {to_name}\n"
        f"日期：{date_val}　時間：{time_str}"
    )
    return {"found": False, "error": f"已監控 {max_checks} 輪未找到票"}


# ─── CLI 入口（GitHub Actions 使用）──────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 從環境變數讀取設定
    config = {
        "from_station": os.environ.get("MONITOR_FROM", "台北"),
        "to_station": os.environ.get("MONITOR_TO", "左營"),
        "travel_date": os.environ.get("MONITOR_DATE", ""),
        "travel_time": os.environ.get("MONITOR_TIME", "08:00"),
        "check_interval": int(os.environ.get("MONITOR_INTERVAL", "90")),
        "max_checks": int(os.environ.get("MONITOR_MAX_CHECKS", "200")),
        "adult_count": int(os.environ.get("MONITOR_ADULT_COUNT", "1")),
        "captcha_retry_limit": int(os.environ.get("MONITOR_CAPTCHA_RETRIES", "30")),
    }

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    def telegram_notify(msg: str):
        """透過 Telegram Bot API 發送通知"""
        if not tg_token or not tg_chat_id:
            return
        url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
        try:
            requests.post(url, json={
                "chat_id": tg_chat_id,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=10)
        except Exception as e:
            log.error(f"Telegram 通知失敗: {e}")

    status = {"running": True}

    if not config["travel_date"]:
        log.error("未設定 MONITOR_DATE")
        exit(1)

    result = run_monitor(config, status, notify_fn=telegram_notify)

    if result.get("found"):
        log.info("✅ 監控成功：找到可訂票班次")
    else:
        log.info(f"❌ 監控結束：{result.get('error', '未知')}")
