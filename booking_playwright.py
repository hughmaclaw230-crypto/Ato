#!/usr/bin/env python3
"""
THSRC Booking Engine — Playwright 版本 (GitHub Actions 專用)
============================================================
使用真實 Chromium 瀏覽器自動化訂票，繞過高鐵反爬蟲偵測。
驗證碼辨識: CNN (ONNX) + ddddocr 雙引擎。

設計給 GitHub Actions 環境執行:
  - 所有參數透過環境變數傳入
  - 完成後透過 Telegram Bot API 通知結果

環境變數:
  THSR_FROM, THSR_TO, THSR_DATE, THSR_TIME, THSR_TRAIN_NO,
  THSR_ADULT_COUNT, THSR_SEAT_TYPE,
  THSR_ID, THSR_PHONE,
  THSR_RETRY_INTERVAL, THSR_MAX_RETRIES,
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import io
import re
import sys
import time
import json
import logging
import asyncio
import requests
import numpy as np
from PIL import Image
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── 設定 (從環境變數讀取) ────────────────────────────

CONFIG = {
    "from_station":   os.environ.get("THSR_FROM", "南港"),
    "to_station":     os.environ.get("THSR_TO", "左營"),
    "travel_date":    os.environ.get("THSR_DATE", ""),
    "travel_time":    os.environ.get("THSR_TIME", ""),
    "train_no":       os.environ.get("THSR_TRAIN_NO", ""),
    "adult_count":    int(os.environ.get("THSR_ADULT_COUNT", "1")),
    "seat_type":      os.environ.get("THSR_SEAT_TYPE", "無座位偏好"),
    "id_number":      os.environ.get("THSR_ID", ""),
    "phone":          os.environ.get("THSR_PHONE", ""),
    "retry_interval": float(os.environ.get("THSR_RETRY_INTERVAL", "5")),
    "max_retries":    int(os.environ.get("THSR_MAX_RETRIES", "720")),
}

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

THSRC_URL = "https://irs.thsrc.com.tw/IMINT/?locale=tw"

STATION_MAP = {
    "南港": "1", "台北": "2", "板橋": "3", "桃園": "4",
    "新竹": "5", "苗栗": "6", "台中": "7", "彰化": "8",
    "雲林": "9", "嘉義": "10", "台南": "11", "左營": "12",
}

STATION_MAP_REV = {v: k for k, v in STATION_MAP.items()}

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

# ─── Telegram 通知 ─────────────────────────────────────

def send_telegram(text: str):
    """送 Telegram 通知"""
    if not TG_TOKEN or not TG_CHAT_ID:
        log.warning("未設定 Telegram token/chat_id，跳過通知")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        if r.status_code != 200:
            log.warning(f"Telegram 發送失敗: {r.text}")
    except Exception as e:
        log.error(f"Telegram 發送錯誤: {e}")


# ─── 驗證碼辨識 (CNN + ddddocr) ───────────────────────

def decode_captcha(img_bytes: bytes) -> str:
    """雙引擎辨識驗證碼: CNN 優先 → ddddocr 備援"""
    # 嘗試 CNN
    try:
        from captcha_cnn import decode_captcha_cnn
        result = decode_captcha_cnn(img_bytes)
        if len(result) == 4:
            log.info(f"✅ CNN 驗證碼: '{result}'")
            return result
        log.warning(f"CNN 結果長度異常: '{result}'")
    except Exception as e:
        log.warning(f"CNN 辨識失敗: {e}")

    # 備援: ddddocr
    try:
        import ddddocr
        ocr = ddddocr.DdddOcr(show_ad=False)
        result = ocr.classification(img_bytes)
        cleaned = "".join(c for c in result.upper() if c.isalnum())[:4]
        log.info(f"📝 ddddocr 驗證碼: '{result}' → '{cleaned}'")
        return cleaned
    except Exception as e:
        log.error(f"ddddocr 辨識失敗: {e}")

    return ""


def _convert_time_to_form_value(time_str: str) -> str:
    """HH:MM → 高鐵表單時間值"""
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
            return f"{h-12:02d}{m:02d}P"
    except (ValueError, AttributeError):
        return "0800A"


# ─── Playwright 訂票主邏輯 ─────────────────────────────

async def run_booking():
    """
    使用 Playwright 自動化訂票:
    Step 1: 打開訂票頁面、填寫表單
    Step 2: 驗證碼辨識 + 提交
    Step 3: 選班次
    Step 4: 填個資
    Step 5: 取得結果
    """
    from playwright.async_api import async_playwright

    c = CONFIG
    from_code = STATION_MAP.get(c["from_station"])
    to_code = STATION_MAP.get(c["to_station"])
    if not from_code or not to_code:
        return {"success": False, "error": f"站名錯誤: {c['from_station']} → {c['to_station']}"}

    date_val = c["travel_date"].replace("-", "/")
    time_val = _convert_time_to_form_value(c["travel_time"])
    seat_code = SEAT_MAP.get(c.get("seat_type", "無座位偏好"), "0")
    max_retries = c["max_retries"]
    retry_interval = c["retry_interval"]
    captcha_pass_count = 0

    send_telegram(
        f"🚀 <b>GitHub Actions 訂票啟動</b>\n"
        f"─────────────────\n"
        f"🚉 {c['from_station']} → {c['to_station']}\n"
        f"📅 {date_val}　🕐 {c['travel_time']}\n"
        f"🔄 每 {retry_interval}s，最多 {max_retries} 次"
    )

    # 多個入口 URL，依序嘗試
    BOOKING_URLS = [
        "https://irs.thsrc.com.tw/IMINT/?locale=tw",
        "https://irs.thsrc.com.tw/IMINT/",
        "https://www.thsrc.com.tw/ArticleContent/a3b630bb-1066-4352-a1ef-58c7b4e8ef7c",
    ]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-web-security",
            ]
        )

        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            locale="zh-TW",
            timezone_id="Asia/Taipei",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            },
        )

        # 隱藏自動化特徵
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-TW', 'zh', 'en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
        """)

        page = await context.new_page()

        for attempt in range(1, max_retries + 1):
            log.info(f"── 第 {attempt}/{max_retries} 次嘗試 ──")

            try:
                # ═══ Step 1: 打開訂票頁面 (嘗試多個 URL) ═══
                page_loaded = False
                for url_idx, booking_url in enumerate(BOOKING_URLS):
                    try:
                        log.info(f"嘗試 URL {url_idx+1}/{len(BOOKING_URLS)}: {booking_url[:50]}...")
                        resp = await page.goto(booking_url, wait_until="domcontentloaded", timeout=60000)
                        if resp and resp.status < 400:
                            page_loaded = True
                            log.info(f"✅ 頁面載入成功 (HTTP {resp.status})")
                            break
                        else:
                            log.warning(f"HTTP {resp.status if resp else 'None'}")
                    except Exception as e:
                        log.warning(f"URL {url_idx+1} 失敗: {str(e)[:80]}")
                        continue

                if not page_loaded:
                    log.error("所有 URL 都無法連線")
                    await asyncio.sleep(retry_interval)
                    continue

                # 等待表單載入 (確認關鍵元素出現)
                try:
                    await page.wait_for_selector("#BookingS1Form", timeout=10000)
                except Exception:
                    log.warning("表單未載入，可能有彈窗或維護頁面")
                    # 嘗試點擊確認按鈕 (有時會有 cookie 同意彈窗)
                    try:
                        btn = page.locator("#btn-confirm")
                        if await btn.count() > 0:
                            await btn.click()
                            await page.wait_for_selector("#BookingS1Form", timeout=10000)
                    except Exception:
                        pass

                # ═══ Step 2: 填寫表單 ═══
                # 出發站
                await page.select_option(
                    "select#BookingS1Form_selectStartStation",
                    value=from_code
                )
                # 到達站
                await page.select_option(
                    "select#BookingS1Form_selectDestinationStation",
                    value=to_code
                )
                # 車廂類型: 標準車廂
                try:
                    await page.select_option(
                        "select#BookingS1Form_trainCon_trainRadioGroup",
                        value="0"
                    )
                except Exception:
                    pass

                # 行程類型: 單程
                try:
                    await page.select_option(
                        "select#BookingS1Form_tripCon_typesoftrip",
                        value="0"
                    )
                except Exception:
                    pass

                # 座位偏好
                try:
                    await page.select_option(
                        "select#BookingS1Form_seatCon_seatRadioGroup",
                        value=seat_code
                    )
                except Exception:
                    pass

                # 日期
                await page.fill("input[name='trainCon:trainDate']", date_val)

                # 時間
                await page.select_option(
                    "select[name='trainCon:trainTime']",
                    value=time_val
                )

                # 票數
                await page.select_option(
                    "select[name='ticketCon:ticketCasualRecord:0:ticketCount']",
                    value=str(c["adult_count"])
                )

                # ═══ Step 3: 驗證碼辨識 ═══
                # 取得驗證碼圖片
                captcha_img = page.locator(
                    "#BookingS1Form_homeCaptcha_captchaImage, "
                    "#BookingS1Form_homeCaptcha_passCode"
                )

                if await captcha_img.count() == 0:
                    log.warning("找不到驗證碼圖片元素")
                    await asyncio.sleep(retry_interval)
                    continue

                # 截圖驗證碼元素
                captcha_bytes = await captcha_img.screenshot()
                if not captcha_bytes or len(captcha_bytes) < 100:
                    log.warning("驗證碼截圖太小")
                    await asyncio.sleep(retry_interval)
                    continue

                # 辨識驗證碼
                captcha_text = decode_captcha(captcha_bytes)
                if len(captcha_text) != 4:
                    log.warning(f"驗證碼長度不對: '{captcha_text}'")
                    await asyncio.sleep(retry_interval)
                    continue

                # 填入驗證碼
                await page.fill("#securityCode", captcha_text)

                log.info(
                    f"提交: {c['from_station']}→{c['to_station']} "
                    f"{date_val} {c['travel_time']}({time_val}) "
                    f"驗證碼={captcha_text}"
                )

                # ═══ Step 4: 提交表單 ═══
                await page.click("#SubmitButton")

                # 等待頁面跳轉
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass

                await asyncio.sleep(1)  # 給頁面一點時間完整載入

                current_url = page.url
                page_content = await page.content()

                # ═══ Step 5: 判斷結果 ═══
                if "BookingS2" in current_url or "TrainQueryDataViewPanel" in page_content:
                    captcha_pass_count += 1
                    log.info(f"✅ 驗證碼通過！ (累計 {captcha_pass_count} 次) 進入選車次頁")

                    result = await _handle_train_selection(page, c)
                    if result and result.get("success"):
                        await browser.close()
                        return result
                    else:
                        log.warning("選車次失敗，重新嘗試")
                        await asyncio.sleep(retry_interval)
                        continue

                # 檢查錯誤
                error_text = await _get_error_text(page)
                if error_text:
                    log.warning(f"頁面錯誤: {error_text}")
                    if "驗證碼" in error_text:
                        log.info("→ 驗證碼錯誤，重試")
                    elif "過多" in error_text:
                        log.warning("→ 請求過多，等待")
                        await asyncio.sleep(retry_interval * 3)
                        continue
                else:
                    log.warning(f"未知狀態 (URL={current_url[:60]})")

            except Exception as e:
                log.error(f"例外: {e}")

            await asyncio.sleep(retry_interval)

        await browser.close()

    return {
        "success": False,
        "error": f"已嘗試 {max_retries} 次（驗證碼通過 {captcha_pass_count} 次）"
    }


async def _handle_train_selection(page, config: dict) -> dict | None:
    """Step 2→3→4: 選班次 → 填個資 → 取得結果"""
    target_train_no = config.get("train_no", "")
    target_time = config.get("travel_time", "")

    # 等待班次列表
    try:
        await page.wait_for_selector(
            "input[name='TrainQueryDataViewPanel:TrainGroup']",
            timeout=10000
        )
    except Exception:
        log.error("找不到班次列表")
        return None

    # 取得所有班次 radio buttons
    radios = await page.query_selector_all(
        "input[name='TrainQueryDataViewPanel:TrainGroup']"
    )
    if not radios:
        log.error("沒有班次 radio")
        return None

    log.info(f"找到 {len(radios)} 個班次")

    # 選最佳班次
    selected = False

    # 優先: 匹配車次號碼
    if target_train_no:
        rows = await page.query_selector_all("table.table_tra tr")
        for row in rows:
            text = await row.inner_text()
            if target_train_no in text:
                radio = await row.query_selector(
                    "input[name='TrainQueryDataViewPanel:TrainGroup']"
                )
                if radio:
                    await radio.check()
                    log.info(f"✅ 選中目標班次 {target_train_no}")
                    selected = True
                    break

    # 沒找到就選第一個
    if not selected:
        await radios[0].check()
        log.info("選取第一個班次")

    # 點確認車次
    try:
        submit_btn = page.locator("#SubmitButton")
        if await submit_btn.count() > 0:
            await submit_btn.click()
        else:
            # 嘗試其他可能的按鈕
            await page.click("input[type='submit']")
    except Exception as e:
        log.error(f"點確認車次失敗: {e}")
        return None

    await page.wait_for_load_state("domcontentloaded", timeout=15000)
    await asyncio.sleep(1)

    current_url = page.url
    page_content = await page.content()

    # ═══ Step 3: 個資頁 (S3) ═══
    if "BookingS3" in current_url or "idNumber" in page_content:
        log.info("進入個資頁，填寫身分證和手機...")
        return await _handle_personal_info(page, config)

    # 可能直接到確認頁
    if "BookingS4" in current_url or "訂位代號" in page_content:
        log.info("直接到確認頁！")
        return await _extract_result(page)

    error = await _get_error_text(page)
    if error:
        log.error(f"S2 錯誤: {error}")
    return None


async def _handle_personal_info(page, config: dict) -> dict | None:
    """Step 3: 填寫個人資料"""
    try:
        # 身分證
        id_input = page.locator("input[name='idNumber'], #idNumber")
        if await id_input.count() > 0:
            await id_input.fill(config["id_number"])

        # 手機
        phone_input = page.locator("input[name='mobilePhone'], #mobilePhone")
        if await phone_input.count() > 0:
            await phone_input.fill(config["phone"])

        # 同意條款
        agree_cb = page.locator("input[name='agree']")
        if await agree_cb.count() > 0:
            try:
                await agree_cb.check()
            except Exception:
                pass

        # 確認訂位
        await page.click("#SubmitButton")
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await asyncio.sleep(1)

        current_url = page.url
        if "BookingS4" in current_url or "訂位代號" in await page.content():
            log.info("✅ 進入訂位結果頁！")
            return await _extract_result(page)

        error = await _get_error_text(page)
        if error:
            log.error(f"S3 錯誤: {error}")
        return None

    except Exception as e:
        log.error(f"填個資失敗: {e}")
        return None


async def _extract_result(page) -> dict | None:
    """Step 4: 從結果頁擷取訂票資訊"""
    try:
        content = await page.content()
        info = {
            "success": True,
            "url": page.url,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        # 用多種方式嘗試擷取資訊
        # 訂位代號
        for selector in [".pnr-code", "[class*='pnr']"]:
            try:
                el = page.locator(selector)
                if await el.count() > 0:
                    info["訂位代號"] = (await el.inner_text()).strip()
                    break
            except Exception:
                pass

        if "訂位代號" not in info:
            # 嘗試 regex
            m = re.search(r'訂位代號.*?([A-Z0-9]{8,})', content, re.DOTALL)
            if m:
                info["訂位代號"] = m.group(1)
            else:
                info["訂位代號"] = "（請到高鐵網站確認）"

        # 嘗試截圖保存
        try:
            await page.screenshot(path="/tmp/booking_result.png", full_page=True)
            log.info("結果頁截圖已保存")
        except Exception:
            pass

        log.info(f"✅ 訂票結果: {info}")
        return info

    except Exception as e:
        log.error(f"擷取結果失敗: {e}")
        return {"success": True, "訂位代號": "（解析失敗，請到高鐵網站確認）"}


async def _get_error_text(page) -> str:
    """取得頁面錯誤訊息"""
    selectors = [
        ".feedbackPanelERROR",
        ".feedbackPanel li",
        "[class*='error']",
        ".alert-danger",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel)
            if await el.count() > 0:
                text = (await el.first.inner_text()).strip()
                if text:
                    return text[:200]
        except Exception:
            pass
    return ""


# ─── 主程式 ──────────────────────────────────────────

def main():
    c = CONFIG
    log.info("🚄 THSR Playwright 訂票引擎 (GitHub Actions)")
    log.info(f"  路線: {c['from_station']} → {c['to_station']}")
    log.info(f"  日期: {c['travel_date']}  時間: {c['travel_time']}")
    log.info(f"  班次: {c['train_no'] or '自動選取'}")
    log.info(f"  搜尋: 每 {c['retry_interval']}s，最多 {c['max_retries']} 次")

    # 檢查必填欄位
    missing = []
    if not c["travel_date"]:  missing.append("THSR_DATE")
    if not c["travel_time"]:  missing.append("THSR_TIME")
    if not c["id_number"]:    missing.append("THSR_ID")
    if not c["phone"]:        missing.append("THSR_PHONE")
    if missing:
        msg = f"❌ 缺少環境變數: {', '.join(missing)}"
        log.error(msg)
        send_telegram(msg)
        sys.exit(1)

    # 執行訂票
    result = asyncio.run(run_booking())

    # 發送結果通知
    if result.get("success"):
        booking_id = result.get("訂位代號", "—")
        msg = "\n".join([
            "🎉🎉🎉",
            "",
            "✅ <b>訂票成功！</b>",
            "═════════════════",
            "",
            f"🔖 訂位代號：<b><code>{booking_id}</code></b>",
            "",
            f"🚅 班次：<b>{c.get('train_no', '—')}</b>",
            f"🚉 路線：<b>{c['from_station']} → {c['to_station']}</b>",
            f"📅 日期：<b>{c['travel_date']}</b>",
            f"🕐 時間：<b>{c['travel_time']}</b>",
            f"👤 人數：<b>{c['adult_count']} 人</b>",
            "",
            "═════════════════",
            f"🕐 完成時間：{result.get('timestamp', '—')}",
            "",
            "⚠️ 請務必在時限內完成付款！",
            "🌐 https://irs.thsrc.com.tw/IMINT/",
        ])
        log.info("🎉 訂票成功！")
    else:
        msg = "\n".join([
            "❌ <b>訂票失敗</b>",
            "─────────────────",
            f"🚉 {c['from_station']} → {c['to_station']}",
            f"📅 {c['travel_date']}　🕐 {c['travel_time']}",
            f"❗ 原因：{result.get('error', '未知')}",
            "",
            "💡 可重新嘗試 /book",
        ])
        log.error(f"訂票失敗: {result.get('error')}")

    send_telegram(msg)

    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
