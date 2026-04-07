#!/usr/bin/env python3
"""
THSRC Booking Engine — Pure Requests + ddddocr 驗證碼辨識
不需要 Playwright，使用 requests.Session 模擬完整訂票流程

驗證碼策略:
  1. 圖片預處理（參考 maxmilian/thsrc_captcha 的去噪、二值化、去弧線）
  2. ddddocr OCR 辨識
  3. 辨識失敗自動重試（每次驗證碼都不同）
"""

import os
import io
import re
import time
import logging
from datetime import datetime

import requests
import numpy as np
from PIL import Image, ImageFilter

log = logging.getLogger(__name__)

THSRC_BASE = "https://irs.thsrc.com.tw/IMINT/"

STATION_MAP = {
    "南港": "1", "台北": "2", "板橋": "3", "桃園": "4",
    "新竹": "5", "苗栗": "6", "台中": "7", "彰化": "8",
    "雲林": "9", "嘉義": "10", "台南": "11", "左營": "12",
}

SEAT_MAP = {
    "無座位偏好": "0",
    "靠窗": "1",
    "靠走道": "2",
}

# 高鐵驗證碼可能的字元（0-9 + 大寫英文，排除易混淆的 I O）
ALLOWED_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

# ─── ddddocr OCR ─────────────────────────────────────────

_ocr = None


def get_ocr():
    """取得 ddddocr 實例（延遲載入）"""
    global _ocr
    if _ocr is not None:
        return _ocr
    try:
        import ddddocr
        _ocr = ddddocr.DdddOcr(show_ad=False)
        log.info("✅ ddddocr OCR 引擎載入完成")
        return _ocr
    except ImportError:
        log.warning("ddddocr 未安裝，驗證碼辨識不可用")
        return None
    except Exception as e:
        log.error(f"ddddocr 初始化失敗: {e}")
        return None


# ─── 驗證碼圖片預處理（參考 maxmilian/thsrc_captcha）───────

def preprocess_captcha_image(img_bytes: bytes) -> bytes:
    """
    預處理驗證碼圖片：去噪 → 灰階 → 二值化 → 去弧線 → 清理

    參考 maxmilian/thsrc_captcha 的預處理方法：
    1. 快速非局部均值去噪（cv2.fastNlMeansDenoisingColored 的 PIL 替代）
    2. 灰階 + 反轉二值化
    3. 多項式回歸擬合弧線並移除
    4. 中值濾波去除殘餘噪點
    """
    try:
        img = Image.open(io.BytesIO(img_bytes))
        original_size = img.size  # (width, height)

        # Step 1: 中值濾波去噪（替代 cv2.fastNlMeansDenoisingColored）
        img_denoised = img.filter(ImageFilter.MedianFilter(3))

        # Step 2: 灰階化
        img_gray = img_denoised.convert("L")
        arr = np.array(img_gray)

        # Step 3: 反轉二值化（背景變黑，文字變白，與 thsrc_captcha 一致）
        threshold = 127
        arr_binary = np.where(arr < threshold, 255, 0).astype(np.uint8)

        # Step 4: 嘗試用多項式回歸移除弧線
        arr_cleaned = _remove_arc_line(arr_binary, original_size)

        # Step 5: 再次中值濾波去除殘餘噪點
        img_clean = Image.fromarray(arr_cleaned)
        img_clean = img_clean.filter(ImageFilter.MedianFilter(3))

        # Step 6: 腐蝕 + 膨脹去除小噪點（開運算）
        img_clean = img_clean.filter(ImageFilter.MinFilter(3))
        img_clean = img_clean.filter(ImageFilter.MaxFilter(3))

        # 輸出
        buf = io.BytesIO()
        img_clean.save(buf, format="PNG")
        return buf.getvalue()

    except Exception as e:
        log.warning(f"預處理失敗，使用原圖: {e}")
        return img_bytes


def _remove_arc_line(arr: np.ndarray, size: tuple) -> np.ndarray:
    """
    使用多項式回歸擬合弧線並移除
    參考 maxmilian/thsrc_captcha 的 findRegression + dePolynomial
    """
    try:
        height, width = arr.shape

        # 只取左右邊緣的白色像素來擬合弧線
        margin_left = min(14, width // 10)
        margin_right = min(7, width // 20)
        mask = arr.copy()
        mask[:, margin_left:width - margin_right] = 0

        # 找到白色像素位置
        ys, xs = np.where(mask == 255)
        if len(xs) < 3:
            return arr  # 沒有足夠的點，跳過

        # 多項式回歸擬合
        Y = height - ys  # 座標轉換
        coeffs = np.polyfit(xs, Y, 2)
        poly = np.poly1d(coeffs)

        # 沿著擬合曲線移除像素
        result = arr.copy()
        offset = 4
        for x in range(width):
            y_pred = height - int(round(poly(x)))
            y_start = max(0, y_pred - offset)
            y_end = min(height, y_pred + offset)
            # 反轉弧線區域
            result[y_start:y_end, x] = 255 - result[y_start:y_end, x]

        return result

    except Exception as e:
        log.debug(f"弧線移除失敗: {e}")
        return arr


def decode_captcha(img_bytes: bytes) -> str:
    """用 ddddocr 辨識驗證碼圖片"""
    ocr = get_ocr()
    if ocr is None:
        raise RuntimeError("OCR 引擎不可用")

    result = ocr.classification(img_bytes)
    # 高鐵驗證碼為 4 碼英數混合，過濾並轉大寫
    cleaned = "".join(c for c in result.upper() if c.isalnum())[:4]
    log.info(f"驗證碼辨識: '{result}' → '{cleaned}'")
    return cleaned


# ─── Requests 訂票主邏輯 ────────────────────────────────

def run_booking(config: dict, status: dict) -> dict:
    """同步入口 — 使用 requests.Session 全程模擬"""
    ocr = get_ocr()
    if ocr is None:
        return {"success": False, "error": "缺少 ddddocr 套件"}

    from_code = STATION_MAP.get(config["from_station"])
    to_code = STATION_MAP.get(config["to_station"])
    if not from_code or not to_code:
        return {"success": False, "error": f"站名對照失敗: {config['from_station']} → {config['to_station']}"}

    date_val = config["travel_date"].replace("-", "/")
    travel_time = config["travel_time"]
    adult_count = config.get("adult_count", 1)
    seat_code = SEAT_MAP.get(config.get("seat_type", "無座位偏好"), "0")

    max_retries = config.get("max_retries", 720)
    retry_interval = config.get("retry_interval", 3)
    captcha_pass_count = 0

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    })

    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    for attempt in range(1, max_retries + 1):
        if not status.get("running", True):
            return {"success": False, "error": "使用者手動中止"}

        status["attempts"] = attempt
        log.info(f"── 第 {attempt} 次嘗試 ──")

        try:
            # ─── Step 1: 取得訂票頁面 → 獲得 session + 表單資訊 ───
            resp = session.get(THSRC_BASE, timeout=20, verify=False)
            if resp.status_code != 200:
                log.warning(f"取頁面失敗: HTTP {resp.status_code}")
                time.sleep(retry_interval)
                continue

            html = resp.text
            # 從 HTML 取得 form action URL
            form_action = _extract_form_action(html)
            if not form_action:
                log.warning("找不到表單 action URL")
                time.sleep(retry_interval)
                continue

            # 取得驗證碼圖片 URL
            captcha_url = _extract_captcha_url(html, resp.url)
            if not captcha_url:
                log.warning("找不到驗證碼圖片 URL")
                time.sleep(retry_interval)
                continue

            # ─── Step 2: 下載驗證碼圖片 ───
            captcha_resp = session.get(captcha_url, timeout=10, verify=False)
            if captcha_resp.status_code != 200 or len(captcha_resp.content) < 100:
                log.warning(f"驗證碼圖片下載失敗: {captcha_resp.status_code}")
                time.sleep(retry_interval)
                continue

            # 預處理 + OCR
            processed = preprocess_captcha_image(captcha_resp.content)
            captcha_text = decode_captcha(processed)

            if len(captcha_text) != 4:
                log.warning(f"驗證碼長度不對({len(captcha_text)}: '{captcha_text}')，重試")
                time.sleep(retry_interval)
                continue

            # ─── Step 3: 提交訂票表單 ───
            # 構建完整 form action URL
            if form_action.startswith("/"):
                action_url = f"https://irs.thsrc.com.tw{form_action}"
            elif form_action.startswith("http"):
                action_url = form_action
            else:
                action_url = f"https://irs.thsrc.com.tw/IMINT/{form_action}"

            # 票數格式：成人 "1F", 兒童 "0H", etc.
            ticket_adult = f"{adult_count}F"

            form_data = {
                "BookingS1Form:hf:0": "",
                "selectStartStation": from_code,
                "selectDestinationStation": to_code,
                "trainCon:trainRadioGroup": "0",  # 標準車廂
                "seatCon:seatRadioGroup": seat_code,
                "tripCon:typesoftrip": "0",  # 單程
                "toTimeInputField": date_val,
                "toTimeTable": travel_time,  # 直接用 HH:MM 格式
                "toTrainIDInputField": "",
                "backTimeInputField": "",
                "backTimeTable": "",
                "backTrainIDInputField": "",
                "ticketPanel:rows:0:ticketAmount": ticket_adult,
                "ticketPanel:rows:1:ticketAmount": "0H",
                "ticketPanel:rows:2:ticketAmount": "0W",
                "ticketPanel:rows:3:ticketAmount": "0E",
                "ticketPanel:rows:4:ticketAmount": "0P",
                "homeCaptcha:securityCode": captcha_text,
                "SubmitButton": "開始查詢",
            }

            log.info(f"提交: {from_code}→{to_code} {date_val} {travel_time} 驗證碼={captcha_text}")

            submit_resp = session.post(
                action_url,
                data=form_data,
                timeout=20,
                verify=False,
                allow_redirects=True,
            )

            # ─── Step 4: 判斷結果 ───
            result_html = submit_resp.text
            result_url = submit_resp.url

            if "BookingS2" in result_url or "確認車次" in result_html:
                captcha_pass_count += 1
                log.info(f"✅ 驗證碼通過（累計 {captcha_pass_count} 次），進入選車次頁")

                # 選取班次並確認
                result = _select_train_and_confirm(
                    session, result_html, result_url, config
                )
                if result and result.get("success"):
                    return result
                else:
                    log.warning("選車次或確認步驟失敗，重新嘗試")
                    time.sleep(retry_interval)
                    continue

            # 檢查錯誤類型
            error_msg = _extract_error_message(result_html)
            if error_msg:
                log.warning(f"頁面錯誤: {error_msg}")
                if "驗證碼" in error_msg or "security" in error_msg.lower():
                    log.info("→ 驗證碼錯誤，重新嘗試")
                elif "班次" in error_msg or "無" in error_msg or "沒有" in error_msg:
                    log.warning("→ 查無班次")
                elif "過多" in error_msg or "too many" in error_msg.lower():
                    log.warning("→ 請求過多，等待較長時間")
                    time.sleep(retry_interval * 3)
                    continue
            else:
                log.warning(f"未知頁面狀態 (URL={result_url[:60]})")

        except requests.exceptions.Timeout:
            log.warning("請求逾時，重試中...")
        except RuntimeError as e:
            log.error(f"OCR 錯誤: {e}")
            return {"success": False, "error": str(e)}
        except Exception as e:
            log.error(f"例外: {e}")

        time.sleep(retry_interval)

    return {
        "success": False,
        "error": f"已嘗試 {max_retries} 次（驗證碼通過 {captcha_pass_count} 次）"
    }


# ─── HTML 解析工具 ──────────────────────────────────────

def _extract_form_action(html: str) -> str | None:
    """從 HTML 取得 BookingS1Form 的 action URL"""
    # 找 <form id="BookingS1Form" action="...">
    patterns = [
        r'<form[^>]*id="BookingS1Form"[^>]*action="([^"]+)"',
        r'<form[^>]*action="([^"]*BookingS1Form[^"]*)"',
        r'action="(/IMINT/[^"]*IFormSubmitListener[^"]*)"',
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return m.group(1).replace("&amp;", "&")
    return None


def _extract_captcha_url(html: str, page_url: str) -> str | None:
    """從 HTML 取得驗證碼圖片 URL"""
    patterns = [
        r'<img[^>]*id="BookingS1Form_homeCaptcha_passCode"[^>]*src="([^"]+)"',
        r'<img[^>]*src="([^"]*passCode[^"]*IResourceListener[^"]*)"',
        r'src="([^"]*homeCaptcha:passCode[^"]*)"',
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            src = m.group(1).replace("&amp;", "&")
            if src.startswith("/"):
                return f"https://irs.thsrc.com.tw{src}"
            elif src.startswith("http"):
                return src
            else:
                # 相對路徑
                base = page_url.rsplit("/", 1)[0]
                return f"{base}/{src}"
    return None


def _extract_error_message(html: str) -> str | None:
    """從 HTML 擷取錯誤訊息"""
    patterns = [
        r'<span[^>]*class="feedbackPanelERROR"[^>]*>(.*?)</span>',
        r'<div[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)</div>',
        r'<ul[^>]*class="feedbackPanel"[^>]*>.*?<li[^>]*>(.*?)</li>',
        r'class="alert[^"]*alert-danger[^"]*"[^>]*>(.*?)</(?:div|p)',
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            raw = m.group(1)
            # 移除 HTML tags
            text = re.sub(r'<[^>]+>', '', raw).strip()
            if text:
                return text[:200]
    return None


def _extract_form_fields(html: str, form_id: str = "") -> dict:
    """從 HTML 擷取所有 hidden input 和已有的 form fields"""
    fields = {}
    # 找所有 hidden inputs
    for m in re.finditer(
        r'<input[^>]*type="hidden"[^>]*name="([^"]*)"[^>]*value="([^"]*)"',
        html, re.IGNORECASE
    ):
        fields[m.group(1)] = m.group(2)
    # 反向也找（value 在 name 前面）
    for m in re.finditer(
        r'<input[^>]*value="([^"]*)"[^>]*type="hidden"[^>]*name="([^"]*)"',
        html, re.IGNORECASE
    ):
        fields[m.group(2)] = m.group(1)
    return fields


# ─── Step 2: 選車次 ────────────────────────────────────

def _select_train_and_confirm(
    session: requests.Session,
    html: str,
    url: str,
    config: dict,
) -> dict | None:
    """
    Step 2: 選取班次
    Step 3: 填寫個資
    Step 4: 取得結果
    """
    target_time = config["travel_time"]
    target_train_no = config.get("train_no", "")
    log.info(f"目標: 時間={target_time}, 班次={target_train_no}")

    # 取得 form action
    form_action = _extract_s2_form_action(html)
    if not form_action:
        log.error("找不到 S2 表單 action")
        return None

    if form_action.startswith("/"):
        action_url = f"https://irs.thsrc.com.tw{form_action}"
    else:
        action_url = form_action

    # 解析班次列表
    trains = _parse_train_list(html)
    if not trains:
        log.error("找不到班次列表")
        return None

    log.info(f"找到 {len(trains)} 個班次選項")

    # 選最佳班次
    best_value = _find_best_train(trains, target_time, target_train_no)
    if not best_value:
        log.error("無法找到合適的班次")
        return None

    log.info(f"選取班次: value={best_value}")

    # 取得隱藏欄位
    hidden_fields = _extract_form_fields(html)

    form_data = {
        **hidden_fields,
        "TrainQueryDataViewPanel:TrainGroup": best_value,
        "SubmitButton": "確認車次",
    }

    try:
        resp = session.post(action_url, data=form_data, timeout=20,
                           verify=False, allow_redirects=True)
    except Exception as e:
        log.error(f"提交班次選取失敗: {e}")
        return None

    result_html = resp.text
    result_url = resp.url

    # Step 3: 填寫個資
    if "BookingS3" in result_url or "身分證" in result_html or "idNumber" in result_html:
        log.info("進入個資頁，填寫身分證和手機...")
        return _fill_personal_info(session, result_html, result_url, config)

    # 可能直接到確認頁
    if "BookingS4" in result_url or "訂位代號" in result_html:
        log.info("直接到確認頁！")
        return _extract_booking_result(result_html, result_url)

    log.warning(f"S2 提交後未知頁面: {result_url[:80]}")
    error = _extract_error_message(result_html)
    if error:
        log.warning(f"S2 錯誤: {error}")
    return None


def _extract_s2_form_action(html: str) -> str | None:
    """取得 S2 頁面的 form action"""
    patterns = [
        r'<form[^>]*id="BookingS2Form"[^>]*action="([^"]+)"',
        r'<form[^>]*action="([^"]*BookingS2Form[^"]*)"',
        r'action="(/IMINT/[^"]*)"',
    ]
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            return m.group(1).replace("&amp;", "&")
    return None


def _parse_train_list(html: str) -> list:
    """
    從 S2 頁面解析班次列表
    回傳: [{"value": "...", "train_no": "...", "depart": "...", "arrive": "..."}]
    """
    trains = []

    # 找所有 radio input
    radio_pattern = r'<input[^>]*name="TrainQueryDataViewPanel:TrainGroup"[^>]*value="([^"]*)"'
    values = re.findall(radio_pattern, html, re.IGNORECASE)

    if not values:
        # 嘗試反向
        radio_pattern = r'<input[^>]*value="([^"]*)"[^>]*name="TrainQueryDataViewPanel:TrainGroup"'
        values = re.findall(radio_pattern, html, re.IGNORECASE)

    # 解析每個班次的時間資訊
    # 嘗試從 table rows 取得班次資訊
    row_pattern = r'<tr[^>]*>.*?TrainGroup.*?value="([^"]*)".*?</tr>'
    rows = re.findall(row_pattern, html, re.IGNORECASE | re.DOTALL)

    for val in values:
        train_info = {"value": val}

        # 嘗試從 value 周圍的 HTML 取得班次資訊
        # 找到包含這個 value 的 <tr>
        tr_pattern = rf'<tr[^>]*>(?:(?!</tr>).)*?value="{re.escape(val)}"(?:(?!</tr>).)*?</tr>'
        tr_match = re.search(tr_pattern, html, re.IGNORECASE | re.DOTALL)
        if tr_match:
            tr_html = tr_match.group(0)
            # 取得所有 <td> 內容
            td_texts = re.findall(r'<td[^>]*>(.*?)</td>', tr_html, re.DOTALL)
            td_texts = [re.sub(r'<[^>]+>', '', t).strip() for t in td_texts]

            # 典型的欄位順序: [radio, 車次, 出發, 到達, 行車時間, ...]
            if len(td_texts) >= 4:
                train_info["train_no"] = td_texts[1] if len(td_texts) > 1 else ""
                train_info["depart"] = td_texts[2] if len(td_texts) > 2 else ""
                train_info["arrive"] = td_texts[3] if len(td_texts) > 3 else ""

        trains.append(train_info)

    return trains


def _find_best_train(trains: list, target_time: str, target_train_no: str) -> str | None:
    """找到最佳班次的 radio value"""
    if not trains:
        return None

    target_minutes = _time_to_minutes(target_time)

    # 優先：完全匹配班次號碼
    if target_train_no:
        for t in trains:
            if t.get("train_no", "").strip() == target_train_no.strip():
                log.info(f"✅ 找到目標班次 {target_train_no}")
                return t["value"]

    # 次選：最接近目標時間的班次
    best = None
    best_delta = float("inf")

    for t in trains:
        depart = t.get("depart", "")
        if depart and ":" in depart:
            delta = abs(_time_to_minutes(depart[:5]) - target_minutes)
            if delta < best_delta:
                best_delta = delta
                best = t["value"]

    # 如果沒找到時間資訊，直接選第一個
    if best is None and trains:
        best = trains[0]["value"]
        log.info("未找到時間資訊，選取第一個班次")

    if best:
        log.info(f"選取最接近的班次（差 {best_delta} 分鐘）")

    return best


# ─── Step 3: 個資 ────────────────────────────────────

def _fill_personal_info(
    session: requests.Session,
    html: str,
    url: str,
    config: dict,
) -> dict | None:
    """填寫身分證和手機"""
    form_action = None
    for p in [
        r'<form[^>]*id="BookingS3Form"[^>]*action="([^"]+)"',
        r'<form[^>]*action="([^"]*BookingS3Form[^"]*)"',
        r'action="(/IMINT/[^"]*)"',
    ]:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            form_action = m.group(1).replace("&amp;", "&")
            break

    if not form_action:
        log.error("找不到 S3 表單 action")
        return None

    if form_action.startswith("/"):
        action_url = f"https://irs.thsrc.com.tw{form_action}"
    else:
        action_url = form_action

    hidden_fields = _extract_form_fields(html)

    form_data = {
        **hidden_fields,
        "idNumber": config["id_number"],
        "mobilePhone": config["phone"],
        "email": "",
        "agree": "on",
        "SubmitButton": "確認訂位",
        "diffOver": "1",
        "isSPro498": "0",
        "passengerCount": str(config.get("adult_count", 1)),
    }

    # 也嘗試加入 Wicket 需要的欄位
    if "BookingS3Form:hf:0" not in form_data:
        form_data["BookingS3Form:hf:0"] = ""

    try:
        resp = session.post(action_url, data=form_data, timeout=20,
                           verify=False, allow_redirects=True)
    except Exception as e:
        log.error(f"提交個資失敗: {e}")
        return None

    result_html = resp.text
    result_url = resp.url

    if "BookingS4" in result_url or "訂位代號" in result_html:
        log.info("✅ 進入訂位結果頁！")
        return _extract_booking_result(result_html, result_url)

    error = _extract_error_message(result_html)
    if error:
        log.error(f"S3 錯誤: {error}")

    log.warning(f"S3 提交後未知頁面: {result_url[:80]}")
    return None


# ─── Step 4: 結果 ────────────────────────────────────

def _extract_booking_result(html: str, url: str) -> dict | None:
    """從結果頁面擷取訂票資訊"""
    info = {
        "url": url,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "success": True,
    }

    # 嘗試多種 pattern 擷取訂位代號
    pnr_patterns = [
        r'<span[^>]*class="pnr-code"[^>]*>(.*?)</span>',
        r'訂位代號.*?<span[^>]*>([\w\d]+)</span>',
        r'PNR.*?[:：]\s*([\w\d]+)',
        r'<td[^>]*>訂位代號</td>\s*<td[^>]*>(.*?)</td>',
    ]
    for p in pnr_patterns:
        m = re.search(p, html, re.IGNORECASE | re.DOTALL)
        if m:
            pnr = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            if pnr:
                info["訂位代號"] = pnr
                break

    # 車次
    train_patterns = [
        r'<td[^>]*>車次</td>\s*<td[^>]*>(.*?)</td>',
        r'車次.*?[:：]\s*(\d+)',
    ]
    for p in train_patterns:
        m = re.search(p, html, re.IGNORECASE | re.DOTALL)
        if m:
            info["車次"] = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            break

    # 座位
    seat_patterns = [
        r'<td[^>]*>車廂.*?座位</td>\s*<td[^>]*>(.*?)</td>',
        r'座位.*?[:：]\s*([\d車A-Z\s]+)',
    ]
    for p in seat_patterns:
        m = re.search(p, html, re.IGNORECASE | re.DOTALL)
        if m:
            info["座位"] = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            break

    # 票價
    price_patterns = [
        r'<td[^>]*>票價</td>\s*<td[^>]*>(.*?)</td>',
        r'金額.*?[:：]\s*([\d,]+)',
    ]
    for p in price_patterns:
        m = re.search(p, html, re.IGNORECASE | re.DOTALL)
        if m:
            info["票價"] = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            break

    if "訂位代號" not in info:
        if "完成" in html or "Success" in html or "BookingS4" in url:
            info["訂位代號"] = "（頁面上請自行確認）"
        else:
            log.warning("訂票結果頁面解析失敗")
            return None

    log.info(f"✅ 訂票資訊: {info}")
    return info


# ─── 工具函數 ──────────────────────────────────────────

def _time_to_minutes(t: str) -> int:
    """將 HH:MM 轉成分鐘數"""
    try:
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 0
