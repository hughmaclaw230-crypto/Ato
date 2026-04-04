#!/usr/bin/env python3
"""
THSRC Sniper - Render 部署版
整合 CNN 驗證碼辨識 + Playwright + Telegram 通知
"""

import os
import io
import time
import asyncio
import logging
import requests
import numpy as np
from datetime import datetime
from PIL import Image
import tensorflow as tf
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─── 設定 ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# 從環境變數讀取（Render Secret 設定）
CONFIG = {
    "id_number":      os.environ["THSRC_ID"],           # 身分證字號
    "phone":          os.environ["THSRC_PHONE"],         # 手機號碼
    "from_station":   os.environ.get("FROM_STATION", "南港"),  # 出發站
    "to_station":     os.environ.get("TO_STATION",   "左營"),  # 到達站
    "travel_date":    os.environ["TRAVEL_DATE"],         # 格式: 2025/05/01
    "travel_time":    os.environ["TRAVEL_TIME"],         # 格式: 07:00
    "adult_count":    int(os.environ.get("ADULT_COUNT", "1")),
    "seat_type":      os.environ.get("SEAT_TYPE", "無座位偏好"),  # 無座位偏好/靠窗/靠走道
    "tg_token":       os.environ["TG_TOKEN"],
    "tg_chat_id":     os.environ["TG_CHAT_ID"],
    "max_retries":    int(os.environ.get("MAX_RETRIES", "30")),
    "retry_interval": float(os.environ.get("RETRY_INTERVAL", "3")),  # 秒
    "model_path":     os.environ.get("MODEL_PATH", "model/thsrc_cnn_model.hdf5"),
}

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
    "靠窗":       "1",
    "靠走道":     "2",
}

# CNN 字元對照（高鐵驗證碼為 0-9 + A-Z 共19種，排除易混淆）
CAPTCHA_CHARS = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"


# ─── CNN 模型載入 ─────────────────────────────────────────
def load_model():
    path = CONFIG["model_path"]
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到模型檔案: {path}，請確認已放置 thsrc_cnn_model.hdf5")
    log.info(f"載入 CNN 模型: {path}")
    model = tf.keras.models.load_model(path)
    log.info("模型載入完成")
    return model


def preprocess_captcha(img_bytes: bytes) -> np.ndarray:
    """對應 maxmilian/thsrc_captcha 的前處理邏輯"""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img = img.resize((140, 140), Image.LANCZOS)
    arr = np.array(img) / 255.0
    return np.expand_dims(arr, axis=0)  # (1, 140, 140, 3)


def decode_captcha(model, img_bytes: bytes) -> str:
    """CNN 推理，輸出4位驗證碼"""
    x = preprocess_captcha(img_bytes)
    preds = model.predict(x, verbose=0)  # 4個輸出，每個19類
    result = ""
    for pred in preds:
        idx = np.argmax(pred[0])
        result += CAPTCHA_CHARS[idx]
    log.info(f"驗證碼辨識結果: {result}")
    return result


# ─── Telegram 通知 ────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{CONFIG['tg_token']}/sendMessage"
    payload = {
        "chat_id": CONFIG["tg_chat_id"],
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            log.info("Telegram 通知已送出")
        else:
            log.warning(f"Telegram 回應異常: {r.text}")
    except Exception as e:
        log.error(f"Telegram 發送失敗: {e}")


# ─── 主訂票邏輯 ───────────────────────────────────────────
async def book_ticket(model):
    from_code = STATION_MAP.get(CONFIG["from_station"])
    to_code   = STATION_MAP.get(CONFIG["to_station"])
    if not from_code or not to_code:
        raise ValueError(f"站名對照失敗: {CONFIG['from_station']} → {CONFIG['to_station']}")

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

        for attempt in range(1, CONFIG["max_retries"] + 1):
            log.info(f"── 第 {attempt} 次嘗試 ──")
            try:
                # 1. 載入首頁
                await page.goto(THSRC_URL, timeout=30000)
                await page.wait_for_load_state("networkidle", timeout=15000)

                # 2. 填寫出發/到達站
                await page.select_option("#BookingS1Form_startStation", from_code)
                await page.select_option("#BookingS1Form_endStation",   to_code)

                # 3. 填寫日期
                date_field = page.locator("#toStation-date, #BookingS1Form_toTrainDate, input[name='toTrainDate']").first
                await date_field.fill(CONFIG["travel_date"])

                # 4. 人數
                await page.select_option(
                    "#BookingS1Form_ticketPanel_rows_0_ticketType",
                    str(CONFIG["adult_count"])
                )

                # 5. 座位偏好
                seat_code = SEAT_MAP.get(CONFIG["seat_type"], "0")
                await page.select_option("#BookingS1Form_seatCon_seatRadio", seat_code)

                # 6. 抓驗證碼圖片
                captcha_img = page.locator("#BookingS1Form_homeCaptcha_passCode, img[id*='captcha']").first
                await captcha_img.wait_for(state="visible", timeout=5000)
                img_bytes = await captcha_img.screenshot()

                # 7. CNN 解碼
                captcha_text = decode_captcha(model, img_bytes)

                # 8. 填入驗證碼
                await page.fill("#BookingS1Form_homeCaptcha_passCode + input, input[name='homeCaptcha']", captcha_text)

                # 9. 送出
                await page.click("input[type='submit'], button[type='submit']")
                await page.wait_for_load_state("networkidle", timeout=15000)

                # 10. 判斷是否進到選車次頁
                if "BookingS2" in page.url or await page.locator(".result-table, #BookingS2Form").count() > 0:
                    log.info("✅ 驗證碼通過，進入選車次頁")
                    result = await select_train_and_confirm(page)
                    await browser.close()
                    return result
                else:
                    # 驗證碼錯誤或無票，繼續
                    err_text = await page.locator(".alert, .error-msg, #errorDiv").first.text_content() if await page.locator(".alert, .error-msg, #errorDiv").count() > 0 else "未知錯誤"
                    log.warning(f"未通過: {err_text.strip()[:80]}")

            except PlaywrightTimeout:
                log.warning("頁面逾時，重試中...")
            except Exception as e:
                log.error(f"例外: {e}")

            await asyncio.sleep(CONFIG["retry_interval"])

        await browser.close()
        return None


async def select_train_and_confirm(page) -> dict | None:
    """在選車次頁選取最接近目標時間的班次，並完成訂購"""
    target_time = CONFIG["travel_time"]
    log.info(f"目標時間: {target_time}，開始選取班次...")

    # 找到對應時間的 radio button
    # THSRC 選車次頁面：每一列有時間文字，找最近的
    rows = await page.locator("tr.result-row, .train-item, table tbody tr").all()
    best_row = None
    best_delta = float("inf")

    for row in rows:
        try:
            time_cell = await row.locator("td").nth(1).text_content()
            t = time_cell.strip()[:5]  # 取 HH:MM
            delta = abs(
                _time_to_minutes(t) - _time_to_minutes(target_time)
            )
            if delta < best_delta:
                best_delta = delta
                best_row = row
        except Exception:
            continue

    if best_row is None:
        log.error("找不到可選班次")
        return None

    # 點選該班次的 radio
    radio = best_row.locator("input[type='radio']").first
    await radio.check()
    log.info(f"已選取最近班次（差 {best_delta} 分鐘）")

    # 確認訂購
    await page.click("input[value*='確認'], input[type='submit']")
    await page.wait_for_load_state("networkidle", timeout=15000)

    # Step 3: 填入身分資料
    if "BookingS3" in page.url or await page.locator("#BookingS3Form, #idNumber").count() > 0:
        await page.fill("input[name='idNumber'], #idNumber", CONFIG["id_number"])
        await page.fill("input[name='mobilePhone'], #mobilePhone", CONFIG["phone"])
        await page.click("input[type='submit'], button[type='submit']")
        await page.wait_for_load_state("networkidle", timeout=15000)

    # 抓取結果頁資訊
    return await extract_booking_result(page)


async def extract_booking_result(page) -> dict | None:
    """解析訂票完成頁面，回傳票務資訊"""
    content = await page.content()
    info = {
        "url":      page.url,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 嘗試抓取訂位代號、車次、時間等
    for label, selectors in {
        "訂位代號": ["#ticketId, .ticket-id, td:has-text('訂位代號') + td"],
        "車次":     [".train-no, td:has-text('車次') + td"],
        "出發時間": [".depart-time, td:has-text('出發') + td"],
        "座位":     [".seat-no, td:has-text('座位') + td"],
    }.items():
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    info[label] = (await el.text_content()).strip()
                    break
            except Exception:
                pass

    # 如果找不到訂位代號，代表可能訂票失敗
    if "訂位代號" not in info:
        # 檢查是否有成功訊息
        if "BookingS4" in page.url or "完成" in content or "Success" in content:
            info["訂位代號"] = "（頁面上請自行確認）"
        else:
            log.warning("訂票結果頁面解析失敗，可能未完成訂票")
            return None

    log.info(f"訂票資訊: {info}")
    return info


def _time_to_minutes(t: str) -> int:
    """HH:MM → 分鐘數"""
    h, m = t.split(":")
    return int(h) * 60 + int(m)


# ─── 入口 ────────────────────────────────────────────────
def main():
    send_telegram(
        f"🚅 <b>THSRC Sniper 啟動</b>\n"
        f"路線：{CONFIG['from_station']} → {CONFIG['to_station']}\n"
        f"日期：{CONFIG['travel_date']}　時間：{CONFIG['travel_time']}\n"
        f"人數：{CONFIG['adult_count']} 人\n"
        f"─────────────────\n"
        f"最多嘗試 {CONFIG['max_retries']} 次，間隔 {CONFIG['retry_interval']}s"
    )

    model = load_model()

    result = asyncio.run(book_ticket(model))

    if result:
        msg = (
            f"✅ <b>訂票成功！</b>\n"
            f"─────────────────\n"
            f"路線：{CONFIG['from_station']} → {CONFIG['to_station']}\n"
            f"日期：{CONFIG['travel_date']}\n"
        )
        for k, v in result.items():
            if k not in ("url", "timestamp"):
                msg += f"{k}：{v}\n"
        msg += f"─────────────────\n時間：{result.get('timestamp', '')}"
        send_telegram(msg)
        log.info("完成！")
    else:
        msg = (
            f"❌ <b>訂票失敗</b>\n"
            f"已嘗試 {CONFIG['max_retries']} 次，仍未成功取票。\n"
            f"路線：{CONFIG['from_station']} → {CONFIG['to_station']}　{CONFIG['travel_date']}"
        )
        send_telegram(msg)
        log.error("所有重試均失敗")


if __name__ == "__main__":
    main()
