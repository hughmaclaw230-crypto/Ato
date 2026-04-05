#!/usr/bin/env python3
"""
THSRC Booking Engine — Playwright + CNN 驗證碼辨識
從 app.py 背景線程呼叫
"""

import os
import io
import time
import asyncio
import logging
from datetime import datetime

log = logging.getLogger(__name__)

# numpy / PIL / tensorflow / playwright 延遲載入
# 只在實際呼叫訂票時才 import，避免 requirements 精簡後 crash

THSRC_URL = "https://irs.thsrc.com.tw/IMINT/"

STATION_MAP = {
    "南港": "1", "台北": "2", "板橋": "3", "桃園": "4",
    "新竹": "5", "苗栗": "6", "台中": "7", "彰化": "8",
    "雲林": "9", "嘉義": "10", "台南": "11", "左營": "12",
}

TIME_MAP = {
    "06:00": "610N", "06:30": "612N", "07:00": "108N", "07:30": "110N",
    "08:00": "112N", "08:30": "114N", "09:00": "116N", "09:30": "118N",
    "10:00": "120N", "10:30": "122N", "11:00": "124N", "11:30": "126N",
    "12:00": "128N", "12:30": "130N", "13:00": "132N", "13:30": "134N",
    "14:00": "136N", "14:30": "138N", "15:00": "140N", "15:30": "142N",
    "16:00": "144N", "16:30": "146N", "17:00": "148N", "17:30": "150N",
    "18:00": "152N", "18:30": "154N", "19:00": "156N", "19:30": "158N",
    "20:00": "160N", "20:30": "162N", "21:00": "164N", "21:30": "166N",
    "22:00": "168N", "22:30": "170N",
}

SEAT_MAP = {
    "無座位偏好": "0",
    "靠窗": "1",
    "靠走道": "2",
}

CAPTCHA_CHARS = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"

# ─── CNN 模型 ─────────────────────────────────────────
_model = None


def get_model():
    global _model
    if _model is not None:
        return _model

    model_path = os.environ.get("MODEL_PATH", "model/thsrc_cnn_model.hdf5")
    if not os.path.exists(model_path):
        log.warning(f"找不到模型檔案: {model_path}")
        return None

    import tensorflow as tf
    log.info(f"載入 CNN 模型: {model_path}")
    _model = tf.keras.models.load_model(model_path)
    log.info("模型載入完成")
    return _model


def preprocess_captcha(img_bytes: bytes):
    import numpy as np
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img = img.resize((140, 140), Image.LANCZOS)
    arr = np.array(img) / 255.0
    return np.expand_dims(arr, axis=0)


def decode_captcha(model, img_bytes: bytes) -> str:
    import numpy as np
    x = preprocess_captcha(img_bytes)
    preds = model.predict(x, verbose=0)
    result = ""
    for pred in preds:
        idx = np.argmax(pred[0])
        result += CAPTCHA_CHARS[idx]
    log.info(f"驗證碼辨識結果: {result}")
    return result


# ─── 訂票主邏輯 ────────────────────────────────────────

def run_booking(config: dict, status: dict) -> dict:
    """同步入口，內部跑 asyncio。需要 numpy, Pillow, tensorflow, playwright 已安裝。"""
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as e:
        return {"success": False, "error": f"缺少訂票依賴套件: {e}"}

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_async_booking(config, status))
    finally:
        loop.close()


async def _async_booking(config: dict, status: dict) -> dict:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

    model = get_model()
    if model is None:
        return {"success": False, "error": "CNN 模型未找到，請確認 model/thsrc_cnn_model.hdf5 已放置"}

    from_code = STATION_MAP.get(config["from_station"])
    to_code = STATION_MAP.get(config["to_station"])
    if not from_code or not to_code:
        return {"success": False, "error": f"站名對照失敗: {config['from_station']} → {config['to_station']}"}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        for attempt in range(1, config["max_retries"] + 1):
            # 檢查是否被使用者停止
            if not status.get("running", True):
                await browser.close()
                return {"success": False, "error": "使用者手動中止"}

            status["attempts"] = attempt
            log.info(f"── 第 {attempt} 次嘗試 ──")

            try:
                await page.goto(THSRC_URL, timeout=30000)
                await page.wait_for_load_state("networkidle", timeout=15000)

                await page.select_option("#BookingS1Form_startStation", from_code)
                await page.select_option("#BookingS1Form_endStation", to_code)

                date_field = page.locator(
                    "#toStation-date, #BookingS1Form_toTrainDate, input[name='toTrainDate']"
                ).first
                await date_field.fill(config["travel_date"])

                await page.select_option(
                    "#BookingS1Form_ticketPanel_rows_0_ticketType",
                    str(config["adult_count"]),
                )

                seat_code = SEAT_MAP.get(config["seat_type"], "0")
                await page.select_option("#BookingS1Form_seatCon_seatRadio", seat_code)

                captcha_img = page.locator(
                    "#BookingS1Form_homeCaptcha_passCode, img[id*='captcha']"
                ).first
                await captcha_img.wait_for(state="visible", timeout=5000)
                img_bytes = await captcha_img.screenshot()

                captcha_text = decode_captcha(model, img_bytes)

                await page.fill(
                    "#BookingS1Form_homeCaptcha_passCode + input, input[name='homeCaptcha']",
                    captcha_text,
                )

                await page.click("input[type='submit'], button[type='submit']")
                await page.wait_for_load_state("networkidle", timeout=15000)

                if "BookingS2" in page.url or await page.locator(".result-table, #BookingS2Form").count() > 0:
                    log.info("✅ 驗證碼通過，進入選車次頁")
                    result = await select_train_and_confirm(page, config)
                    await browser.close()
                    if result:
                        result["success"] = True
                        return result
                    return {"success": False, "error": "選車次或確認失敗"}
                else:
                    err_text = "未知錯誤"
                    if await page.locator(".alert, .error-msg, #errorDiv").count() > 0:
                        err_text = await page.locator(".alert, .error-msg, #errorDiv").first.text_content()
                    log.warning(f"未通過: {err_text.strip()[:80]}")

            except PlaywrightTimeout:
                log.warning("頁面逾時，重試中...")
            except Exception as e:
                log.error(f"例外: {e}")

            await asyncio.sleep(config["retry_interval"])

        await browser.close()
        return {"success": False, "error": f"已嘗試 {config['max_retries']} 次均失敗"}


async def select_train_and_confirm(page, config: dict) -> dict | None:
    target_time = config["travel_time"]
    log.info(f"目標時間: {target_time}，開始選取班次...")

    rows = await page.locator("tr.result-row, .train-item, table tbody tr").all()
    best_row = None
    best_delta = float("inf")

    for row in rows:
        try:
            time_cell = await row.locator("td").nth(1).text_content()
            t = time_cell.strip()[:5]
            delta = abs(_time_to_minutes(t) - _time_to_minutes(target_time))
            if delta < best_delta:
                best_delta = delta
                best_row = row
        except Exception:
            continue

    if best_row is None:
        log.error("找不到可選班次")
        return None

    radio = best_row.locator("input[type='radio']").first
    await radio.check()
    log.info(f"已選取最近班次（差 {best_delta} 分鐘）")

    await page.click("input[value*='確認'], input[type='submit']")
    await page.wait_for_load_state("networkidle", timeout=15000)

    if "BookingS3" in page.url or await page.locator("#BookingS3Form, #idNumber").count() > 0:
        await page.fill("input[name='idNumber'], #idNumber", config["id_number"])
        await page.fill("input[name='mobilePhone'], #mobilePhone", config["phone"])
        await page.click("input[type='submit'], button[type='submit']")
        await page.wait_for_load_state("networkidle", timeout=15000)

    return await extract_booking_result(page)


async def extract_booking_result(page) -> dict | None:
    content = await page.content()
    info = {
        "url": page.url,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    for label, selectors in {
        "訂位代號": ["#ticketId, .ticket-id, td:has-text('訂位代號') + td"],
        "車次": [".train-no, td:has-text('車次') + td"],
        "出發時間": [".depart-time, td:has-text('出發') + td"],
        "座位": [".seat-no, td:has-text('座位') + td"],
    }.items():
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    info[label] = (await el.text_content()).strip()
                    break
            except Exception:
                pass

    if "訂位代號" not in info:
        if "BookingS4" in page.url or "完成" in content or "Success" in content:
            info["訂位代號"] = "（頁面上請自行確認）"
        else:
            log.warning("訂票結果頁面解析失敗")
            return None

    log.info(f"訂票資訊: {info}")
    return info


def _time_to_minutes(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)
