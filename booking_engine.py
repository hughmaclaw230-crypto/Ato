#!/usr/bin/env python3
"""
THSRC Booking Engine — Pure Requests + CNN/ddddocr 雙引擎驗證碼辨識
基於 Playwright codegen 實際錄製 https://irs.thsrc.com.tw/IMINT/?locale=tw 頁面結構
更新日期: 2026-04-09

表單結構（由 Playwright codegen 確認）:
  Form ID: BookingS1Form
  Form Action: 動態 Wicket URL (含 jsessionid)

  出發站:  select#BookingS1Form_selectStartStation  name="selectStartStation"
  到達站:  select#BookingS1Form_selectDestinationStation  name="selectDestinationStation"
  車廂類型: select#BookingS1Form_trainCon_trainRadioGroup  name="trainCon:trainRadioGroup"
           值: 0=標準車廂對號座, 1=商務車廂
  行程類型: select#BookingS1Form_tripCon_typesoftrip  name="tripCon:typesoftrip"
           值: 0=單程, 1=去回程
  座位偏好: select#BookingS1Form_seatCon_seatRadioGroup  name="seatCon:seatRadioGroup"
           值: 0=無座位偏好, 1=靠窗, 2=靠走道
  出發日期: input[name="trainCon:trainDate"]  格式: YYYY/MM/DD
  出發時間: select.out-time  name="trainCon:trainTime"
           值: "1201A"=00:00, "0600A"=06:00, "0630A"=06:30, ...
  全票:    select[name*="0:ticketCount"]  name="ticketCon:ticketCasualRecord:0:ticketCount"
  孩童票:  select[name*="1:ticketCount"]  name="ticketCon:ticketCasualRecord:1:ticketCount"
  愛心票:  select[name*="2:ticketCount"]  name="ticketCon:ticketCasualRecord:2:ticketCount"
  敬老票:  select[name*="3:ticketCount"]  name="ticketCon:ticketCasualRecord:3:ticketCount"
  大學生票: select[name*="4:ticketCount"]  name="ticketCon:ticketCasualRecord:4:ticketCount"
  驗證碼:  input#securityCode  name="homeCaptcha:securityCode"
  驗證碼圖片: img#BookingS1Form_homeCaptcha_captchaImage
  送出:    input#SubmitButton  name="SubmitButton"  value="開始查詢"

驗證碼策略:
  1. CNN模型辨識（ONNX Runtime，94.5%準確率，gary9987/keras-TaiwanHighSpeedRail-captcha）
  2. ddddocr OCR 辨識（備援）
  3. 圖片預處理（參考 maxmilian/thsrc_captcha 的去噪、二值化、去弧線）
  4. 辨識失敗自動重試（每次驗證碼都不同）
"""

import os
import io
import re
import time
import logging
from datetime import datetime
from urllib.parse import urljoin

import requests
import numpy as np
from PIL import Image, ImageFilter

log = logging.getLogger(__name__)

THSRC_BASE = "https://irs.thsrc.com.tw/IMINT/?locale=tw"

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

# 高鐵時間欄位值對照（由 Playwright codegen 確認）
# 格式: "HHmmA" — 例如 "0600A" = 06:00, "1230A" = 12:30
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

    完全對齊 maxmilian/thsrc_captcha 的預處理流程：
    1. cv2.fastNlMeansDenoisingColored (h=30, hColor=30, templateWindowSize=7, searchWindowSize=21)
       — 若無 cv2 則退回 PIL MedianFilter
    2. cv2.threshold THRESH_BINARY_INV (threshold=127)
    3. 多項式回歸擬合弧線並移除 (margin_left=14, margin_right=7, offset=4)
    4. 中值濾波去除殘餘噪點
    """
    try:
        # ── 嘗試使用 cv2 (maxmilian 原版方法，效果最好) ──
        try:
            import cv2
            return _preprocess_with_cv2(img_bytes)
        except ImportError:
            pass

        # ── 退回 PIL 實現 ──
        img = Image.open(io.BytesIO(img_bytes))
        original_size = img.size  # (width, height)

        # Step 1: 中值濾波去噪（替代 cv2.fastNlMeansDenoisingColored）
        img_denoised = img.filter(ImageFilter.MedianFilter(3))
        img_denoised = img_denoised.filter(ImageFilter.SMOOTH_MORE)

        # Step 2: 灰階化
        img_gray = img_denoised.convert("L")
        arr = np.array(img_gray)

        # Step 3: 反轉二值化（背景變黑，文字變白，與 thsrc_captcha 一致）
        threshold = 127
        arr_binary = np.where(arr < threshold, 255, 0).astype(np.uint8)

        # Step 4: 多項式回歸移除弧線（參數完全對齊 maxmilian/thsrc_captcha）
        arr_cleaned = _remove_arc_line(arr_binary)

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


def _preprocess_with_cv2(img_bytes: bytes) -> bytes:
    """
    使用 cv2 的預處理 — 完全對齊 maxmilian/thsrc_captcha 的 preprocessBatch.py

    流程:
    1. imgDenoise: cv2.fastNlMeansDenoisingColored(img, None, 30, 30, 7, 21)
    2. img2Gray: cv2.threshold(img, 127, 255, cv2.THRESH_BINARY_INV)
    3. findRegression: 取左右邊緣 (14, WIDTH-7) 白色像素做 PolynomialFeatures(degree=2)
    4. dePolynomial: 沿擬合曲線 ±4 像素反轉
    """
    import cv2
    from sklearn.preprocessing import PolynomialFeatures
    from sklearn.linear_model import LinearRegression

    # 解碼圖片
    arr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("cv2 無法解碼圖片")

    height, width = img.shape[:2]

    # Step 1: 去噪 — 與 maxmilian 完全一致
    dst = cv2.fastNlMeansDenoisingColored(img, None, 30, 30, 7, 21)

    # Step 2: 反轉二值化 — 與 maxmilian 完全一致
    ret, thresh = cv2.threshold(dst, 127, 255, cv2.THRESH_BINARY_INV)

    # Step 3: 多項式回歸找弧線 — 與 maxmilian 完全一致
    gray_for_reg = cv2.cvtColor(thresh, cv2.COLOR_BGR2GRAY)
    gray_for_reg[:, 14:width - 7] = 0
    imagedata = np.where(gray_for_reg == 255)

    if len(imagedata[0]) >= 3:
        X = np.array([imagedata[1]])
        Y = height - imagedata[0]

        poly_reg = PolynomialFeatures(degree=2)
        X_ = poly_reg.fit_transform(X.T)
        regr = LinearRegression()
        regr.fit(X_, Y)

        # Step 4: 沿擬合曲線移除弧線 — 與 maxmilian 完全一致
        X2 = np.array([[i for i in range(0, width)]])
        X2_ = poly_reg.fit_transform(X2.T)
        offset = 4

        newimg = cv2.cvtColor(thresh, cv2.COLOR_BGR2GRAY)
        for ele in np.column_stack([regr.predict(X2_).round(2), X2[0]]):
            pos = height - int(ele[0])
            newimg[pos - offset:pos + offset, int(ele[1])] = (
                255 - newimg[pos - offset:pos + offset, int(ele[1])]
            )
    else:
        newimg = cv2.cvtColor(thresh, cv2.COLOR_BGR2GRAY)

    # 編碼為 PNG
    success, encoded = cv2.imencode('.png', newimg)
    if not success:
        raise ValueError("cv2 編碼失敗")
    return encoded.tobytes()


def _remove_arc_line(arr: np.ndarray) -> np.ndarray:
    """
    使用多項式回歸擬合弧線並移除 (PIL fallback)
    參數完全對齊 maxmilian/thsrc_captcha:
    - 左邊緣 margin: 14 pixels
    - 右邊緣 margin: 7 pixels
    - offset: 4 pixels
    - degree: 2 (二次多項式)
    """
    try:
        height, width = arr.shape

        # 只取左右邊緣的白色像素來擬合弧線
        # maxmilian: img[:, 14:WIDTH - 7] = 0
        mask = arr.copy()
        mask[:, 14:width - 7] = 0

        # 找到白色像素位置
        ys, xs = np.where(mask == 255)
        if len(xs) < 3:
            return arr  # 沒有足夠的點，跳過

        # 多項式回歸擬合 (degree=2，與 maxmilian 一致)
        Y = height - ys  # 座標轉換
        coeffs = np.polyfit(xs, Y, 2)
        poly = np.poly1d(coeffs)

        # 沿著擬合曲線移除像素 (offset=4，與 maxmilian 一致)
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
    """雙引擎辨識驗證碼圖片：CNN 優先 → ddddocr 備援"""
    # 嘗試 CNN 模型（準確率較高）
    try:
        from captcha_cnn import decode_captcha_cnn
        cnn_result = decode_captcha_cnn(img_bytes)
        if len(cnn_result) == 4:
            log.info(f"✅ CNN 驗證碼辨識: '{cnn_result}'")
            return cnn_result
        else:
            log.warning(f"CNN 辨識結果長度異常: '{cnn_result}'，切換 ddddocr")
    except Exception as e:
        log.warning(f"CNN 辨識失敗: {e}，切換 ddddocr")

    # 備援: ddddocr（使用預處理後的圖片提高準確率）
    ocr = get_ocr()
    if ocr is None:
        raise RuntimeError("所有 OCR 引擎都不可用")

    processed = preprocess_captcha_image(img_bytes)
    result = ocr.classification(processed)
    # 高鐵驗證碼為 4 碼英數混合，過濾並轉大寫
    cleaned = "".join(c for c in result.upper() if c.isalnum())[:4]
    log.info(f"📝 ddddocr 驗證碼辨識: '{result}' → '{cleaned}'")
    return cleaned


# ─── 時間格式轉換 ────────────────────────────────────────

def _convert_time_to_form_value(time_str: str) -> str:
    """
    將 HH:MM 格式轉換為高鐵表單使用的時間值
    由 Playwright codegen 確認表單時間 select 的 option value 格式

    例如: "08:00" → "0800A", "13:30" → "0130P", "12:00" → "1200N"
    """
    # 先查直接對照表
    if time_str in TIME_VALUE_MAP:
        return TIME_VALUE_MAP[time_str]

    # 動態計算：將 24 小時制轉為 12 小時制 + AM/PM 標記
    try:
        h, m = map(int, time_str.split(":"))
        if h == 0:
            return f"1201A"  # 午夜
        elif h < 12:
            return f"{h:02d}{m:02d}A"
        elif h == 12:
            if m == 0:
                return "1200N"  # 正午
            return f"12{m:02d}A"
        else:
            h12 = h - 12
            return f"{h12:02d}{m:02d}P"
    except (ValueError, AttributeError):
        log.warning(f"無法轉換時間: {time_str}，使用預設")
        return "0800A"


# ─── Requests 訂票主邏輯 ────────────────────────────────

def run_booking(config: dict, status: dict) -> dict:
    """
    同步入口 — 使用 requests.Session 全程模擬

    基於 Playwright codegen 錄製的表單結構:
    - Form: BookingS1Form
    - 出發站: selectStartStation (value: 1-12)
    - 到達站: selectDestinationStation (value: 1-12)
    - 車廂: trainCon:trainRadioGroup (0=標準, 1=商務)
    - 行程: tripCon:typesoftrip (0=單程)
    - 座位: seatCon:seatRadioGroup (0=無偏好, 1=靠窗, 2=走道)
    - 日期: trainCon:trainDate (YYYY/MM/DD)
    - 時間: trainCon:trainTime (e.g. "0800A")
    - 票數: ticketCon:ticketCasualRecord:N:ticketCount (N=0全票,1孩童,2愛心,3敬老,4大學生)
    - 驗證碼: homeCaptcha:securityCode
    - 送出: SubmitButton = "開始查詢"
    """
    ocr = get_ocr()
    if ocr is None:
        return {"success": False, "error": "缺少 ddddocr 套件"}

    from_code = STATION_MAP.get(config["from_station"])
    to_code = STATION_MAP.get(config["to_station"])
    if not from_code or not to_code:
        return {"success": False, "error": f"站名對照失敗: {config['from_station']} → {config['to_station']}"}

    date_val = config["travel_date"].replace("-", "/")
    travel_time = config["travel_time"]
    time_form_value = _convert_time_to_form_value(travel_time)
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
            page_url = resp.url

            # 從 HTML 取得 form action URL
            form_action = _extract_form_action(html)
            if not form_action:
                log.warning("找不到表單 action URL")
                time.sleep(retry_interval)
                continue

            # 取得驗證碼圖片 URL
            # Playwright codegen 確認: img#BookingS1Form_homeCaptcha_captchaImage
            captcha_url = _extract_captcha_url(html, page_url)
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

            # 驗證碼辨識（CNN 直接處理原圖，ddddocr 用預處理後的圖）
            captcha_text = decode_captcha(captcha_resp.content)

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
                action_url = urljoin(page_url, form_action)

            # 取得所有隱藏欄位（含 Wicket 框架需要的 token）
            hidden_fields = _extract_form_fields(html)

            # ── 表單資料（欄位名稱由 Playwright codegen 確認）──
            form_data = {
                **hidden_fields,
                # 出發站/到達站 (select value: 1-12)
                "selectStartStation": from_code,
                "selectDestinationStation": to_code,
                # 車廂類型: 0=標準車廂對號座
                "trainCon:trainRadioGroup": "0",
                # 行程類型: 0=單程
                "tripCon:typesoftrip": "0",
                # 座位偏好: 0=無偏好, 1=靠窗, 2=走道
                "seatCon:seatRadioGroup": seat_code,
                # 出發日期 (YYYY/MM/DD)
                "trainCon:trainDate": date_val,
                # 出發時間 (e.g. "0800A", "0130P")
                "trainCon:trainTime": time_form_value,
                # 車次需求（空白=所有車次）
                "trainCon:trainNumber": "",
                # 票數（由 Playwright codegen 確認新的欄位名稱格式）
                "ticketCon:ticketCasualRecord:0:ticketCount": str(adult_count),  # 全票
                "ticketCon:ticketCasualRecord:1:ticketCount": "0",  # 孩童票
                "ticketCon:ticketCasualRecord:2:ticketCount": "0",  # 愛心票
                "ticketCon:ticketCasualRecord:3:ticketCount": "0",  # 敬老票
                "ticketCon:ticketCasualRecord:4:ticketCount": "0",  # 大學生票
                # 驗證碼 (input#securityCode)
                "homeCaptcha:securityCode": captcha_text,
                # 送出按鈕
                "SubmitButton": "開始查詢",
            }

            log.info(f"提交: {from_code}→{to_code} {date_val} {travel_time}({time_form_value}) 驗證碼={captcha_text}")

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

            if "BookingS2" in result_url or "確認車次" in result_html or "TrainQueryDataViewPanel" in result_html:
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
                if "驗證碼" in error_msg or "security" in error_msg.lower() or "captcha" in error_msg.lower():
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
    """
    從 HTML 取得 BookingS1Form 的 action URL
    Playwright codegen 確認: form#BookingS1Form
    action 為動態 Wicket URL，含 jsessionid 和 IFormSubmitListener
    """
    patterns = [
        r'<form[^>]*id="BookingS1Form"[^>]*action="([^"]+)"',
        r'<form[^>]*action="([^"]*BookingS1Form[^"]*)"',
        r'action="(/IMINT/[^"]*IFormSubmitListener[^"]*)"',
        r'action="([^"]*IFormSubmitListener-BookingS1Form[^"]*)"',
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return m.group(1).replace("&amp;", "&")
    return None


def _extract_captcha_url(html: str, page_url: str) -> str | None:
    """
    從 HTML 取得驗證碼圖片 URL
    Playwright codegen 確認: img#BookingS1Form_homeCaptcha_captchaImage
    """
    patterns = [
        # 新版 ID（由 Playwright codegen 確認）
        r'<img[^>]*id="BookingS1Form_homeCaptcha_captchaImage"[^>]*src="([^"]+)"',
        # 舊版 ID（向下相容）
        r'<img[^>]*id="BookingS1Form_homeCaptcha_passCode"[^>]*src="([^"]+)"',
        # 通用 pattern
        r'<img[^>]*src="([^"]*captcha[^"]*)"',
        r'<img[^>]*src="([^"]*passCode[^"]*IResourceListener[^"]*)"',
        r'src="([^"]*homeCaptcha:passCode[^"]*)"',
        r'src="([^"]*homeCaptcha[^"]*captchaImage[^"]*)"',
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
        # 新增常見錯誤提示 pattern
        r'<span[^>]*class="[^"]*error-text[^"]*"[^>]*>(.*?)</span>',
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
    # 也找 name 在 type 前面的
    for m in re.finditer(
        r'<input[^>]*name="([^"]*)"[^>]*type="hidden"[^>]*value="([^"]*)"',
        html, re.IGNORECASE
    ):
        if m.group(1) not in fields:
            fields[m.group(1)] = m.group(2)
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
