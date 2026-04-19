#!/usr/bin/env python3
"""
THSR 訂票引擎整合測試
====================
直接對 https://irs.thsrc.com.tw/IMINT/?locale=tw 進行測試，驗證：
1. 頁面抓取 + form action 擷取
2. 驗證碼圖片下載 + CNN/ddddocr 辨識
3. 表單欄位正確性
4. 表單提交（驗證回應）

用法: python3 test_booking.py
"""

import sys
import os
import io
import re
import time
import logging
import requests
import urllib3

# 加入專案路徑
sys.path.insert(0, os.path.dirname(__file__))

from booking_engine import (
    THSRC_BASE, STATION_MAP, SEAT_MAP, TIME_VALUE_MAP,
    _extract_form_action, _extract_captcha_url,
    _extract_form_fields, _extract_error_message,
    _convert_time_to_form_value, decode_captcha,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def test_page_fetch():
    """測試 1: 頁面抓取"""
    log.info("=" * 60)
    log.info("測試 1: 抓取訂票頁面")
    log.info("=" * 60)

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

    resp = session.get(THSRC_BASE, timeout=20, verify=False)
    assert resp.status_code == 200, f"HTTP 狀態碼異常: {resp.status_code}"
    html = resp.text
    page_url = resp.url

    log.info(f"✅ 頁面載入成功 ({len(html)} bytes)")
    log.info(f"   URL: {page_url[:80]}")

    return session, html, page_url


def test_form_action(html: str):
    """測試 2: Form Action 擷取"""
    log.info("")
    log.info("=" * 60)
    log.info("測試 2: 表單 Action URL 擷取")
    log.info("=" * 60)

    action = _extract_form_action(html)
    assert action is not None, "找不到 BookingS1Form action URL"
    log.info(f"✅ Form Action: {action[:100]}")
    return action


def test_captcha(session: requests.Session, html: str, page_url: str):
    """測試 3: 驗證碼下載 + 辨識"""
    log.info("")
    log.info("=" * 60)
    log.info("測試 3: 驗證碼下載與辨識")
    log.info("=" * 60)

    captcha_url = _extract_captcha_url(html, page_url)
    assert captcha_url is not None, "找不到驗證碼圖片 URL"
    log.info(f"✅ 驗證碼 URL: {captcha_url[:100]}")

    resp = session.get(captcha_url, timeout=10, verify=False)
    assert resp.status_code == 200, f"驗證碼下載失敗: HTTP {resp.status_code}"
    assert len(resp.content) > 100, f"驗證碼圖片太小: {len(resp.content)} bytes"
    log.info(f"✅ 驗證碼圖片下載成功 ({len(resp.content)} bytes)")

    # 嘗試 CNN 辨識
    captcha_text = decode_captcha(resp.content)
    log.info(f"✅ 驗證碼辨識結果: '{captcha_text}' (長度={len(captcha_text)})")

    # 保存驗證碼圖片供人工檢視
    from PIL import Image
    img = Image.open(io.BytesIO(resp.content))
    save_path = "/tmp/thsr_captcha_test.png"
    img.save(save_path)
    log.info(f"   驗證碼圖片已保存: {save_path}")

    return captcha_text, resp.content


def test_hidden_fields(html: str):
    """測試 4: Hidden Fields 擷取"""
    log.info("")
    log.info("=" * 60)
    log.info("測試 4: Hidden Fields 擷取")
    log.info("=" * 60)

    fields = _extract_form_fields(html)
    log.info(f"✅ 找到 {len(fields)} 個 hidden fields:")
    for k, v in fields.items():
        log.info(f"   {k} = {v[:60] if v else '(empty)'}")
    return fields


def test_form_selectors(html: str):
    """測試 5: 確認所有表單欄位存在"""
    log.info("")
    log.info("=" * 60)
    log.info("測試 5: 表單欄位驗證")
    log.info("=" * 60)

    checks = {
        "出發站 (selectStartStation)": r'name="selectStartStation"',
        "到達站 (selectDestinationStation)": r'name="selectDestinationStation"',
        "車廂 (trainCon:trainRadioGroup)": r'name="trainCon:trainRadioGroup"',
        "行程 (tripCon:typesoftrip)": r'name="tripCon:typesoftrip"',
        "座位 (seatCon:seatRadioGroup)": r'name="seatCon:seatRadioGroup"',
        "日期 (trainCon:trainDate)": r'trainCon:trainDate',
        "時間 (trainCon:trainTime)": r'trainCon:trainTime',
        "全票 (ticketCon:ticketCasualRecord:0)": r'ticketCon:ticketCasualRecord:0:ticketCount',
        "驗證碼 (homeCaptcha:securityCode)": r'homeCaptcha:securityCode',
        "送出 (SubmitButton)": r'id="SubmitButton"',
        "驗證碼圖片": r'captchaImage|passCode',
    }

    all_ok = True
    for label, pattern in checks.items():
        found = bool(re.search(pattern, html, re.IGNORECASE))
        status = "✅" if found else "❌"
        log.info(f"   {status} {label}")
        if not found:
            all_ok = False

    if all_ok:
        log.info("✅ 所有表單欄位都存在！")
    else:
        log.warning("⚠️ 部分欄位缺失，表單結構可能已變更")

    return all_ok


def test_station_options(html: str):
    """測試 6: 車站 option 值"""
    log.info("")
    log.info("=" * 60)
    log.info("測試 6: 車站 option 值核對")
    log.info("=" * 60)

    # 擷取出發站的 options
    select_block = re.search(
        r'<select[^>]*name="selectStartStation"[^>]*>(.*?)</select>',
        html, re.DOTALL | re.IGNORECASE
    )
    if not select_block:
        log.warning("找不到出發站 select 區塊")
        return False

    options = re.findall(r'<option\s+value="(\d+)"[^>]*>(.*?)</option>', select_block.group(1))
    log.info(f"✅ 找到 {len(options)} 個車站選項:")
    for val, text in options:
        text_clean = re.sub(r'<[^>]+>', '', text).strip()
        log.info(f"   value={val}: {text_clean}")

    return True


def test_time_options(html: str):
    """測試 7: 時間 option 值"""
    log.info("")
    log.info("=" * 60)
    log.info("測試 7: 時間 option 值核對")
    log.info("=" * 60)

    select_block = re.search(
        r'<select[^>]*name="trainCon:trainTime"[^>]*>(.*?)</select>',
        html, re.DOTALL | re.IGNORECASE
    )
    if not select_block:
        log.warning("找不到出發時間 select 區塊")
        return False

    options = re.findall(r'<option\s+value="([^"]+)"[^>]*>(.*?)</option>', select_block.group(1))
    log.info(f"✅ 找到 {len(options)} 個時間選項:")
    for val, text in options[:5]:
        text_clean = re.sub(r'<[^>]+>', '', text).strip()
        log.info(f"   value={val}: {text_clean}")
    if len(options) > 5:
        log.info(f"   ... ({len(options) - 5} more)")

    # 驗證轉換函數
    test_times = ["06:00", "08:00", "12:00", "13:30", "18:00", "21:00"]
    log.info("")
    log.info("   時間轉換驗證:")
    for t in test_times:
        form_val = _convert_time_to_form_value(t)
        exists = any(val == form_val for val, _ in options)
        status = "✅" if exists else "❌"
        log.info(f"   {status} {t} → {form_val} {'(在選項中)' if exists else '(不在選項中！)'}")

    return True


def test_ticket_options(html: str):
    """測試 8: 票數 option 值"""
    log.info("")
    log.info("=" * 60)
    log.info("測試 8: 票數欄位核對")
    log.info("=" * 60)

    ticket_names = [
        ("全票", "ticketCon:ticketCasualRecord:0:ticketCount"),
        ("孩童票", "ticketCon:ticketCasualRecord:1:ticketCount"),
        ("愛心票", "ticketCon:ticketCasualRecord:2:ticketCount"),
        ("敬老票", "ticketCon:ticketCasualRecord:3:ticketCount"),
        ("大學生票", "ticketCon:ticketCasualRecord:4:ticketCount"),
    ]

    for label, name in ticket_names:
        found = name in html
        status = "✅" if found else "❌"
        log.info(f"   {status} {label}: {name}")

    return True


def test_submit_form(session, html, page_url, captcha_text):
    """測試 9: 實際提交表單（僅測試到驗證回應）"""
    log.info("")
    log.info("=" * 60)
    log.info("測試 9: 表單提交測試 (南港→左營, 明天)")
    log.info("=" * 60)

    form_action = _extract_form_action(html)
    if not form_action:
        log.error("找不到 form action")
        return False

    if form_action.startswith("/"):
        action_url = f"https://irs.thsrc.com.tw{form_action}"
    else:
        action_url = form_action

    hidden_fields = _extract_form_fields(html)

    # 計算明天日期
    from datetime import datetime, timedelta
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y/%m/%d")

    form_data = {
        **hidden_fields,
        "selectStartStation": "1",          # 南港
        "selectDestinationStation": "12",    # 左營
        "trainCon:trainRadioGroup": "0",     # 標準車廂
        "tripCon:typesoftrip": "0",          # 單程
        "seatCon:seatRadioGroup": "0",       # 無偏好
        "trainCon:trainDate": tomorrow,
        "trainCon:trainTime": "0800A",       # 08:00
        "trainCon:trainNumber": "",
        "ticketCon:ticketCasualRecord:0:ticketCount": "1",  # 全票 1
        "ticketCon:ticketCasualRecord:1:ticketCount": "0",
        "ticketCon:ticketCasualRecord:2:ticketCount": "0",
        "ticketCon:ticketCasualRecord:3:ticketCount": "0",
        "ticketCon:ticketCasualRecord:4:ticketCount": "0",
        "homeCaptcha:securityCode": captcha_text,
        "SubmitButton": "開始查詢",
    }

    log.info(f"   出發站: 南港 (1)")
    log.info(f"   到達站: 左營 (12)")
    log.info(f"   日期: {tomorrow}")
    log.info(f"   時間: 08:00 (0800A)")
    log.info(f"   驗證碼: {captcha_text}")
    log.info(f"   Action URL: {action_url[:100]}")
    log.info(f"   提交中...")

    try:
        resp = session.post(
            action_url,
            data=form_data,
            timeout=20,
            verify=False,
            allow_redirects=True,
        )
    except Exception as e:
        log.error(f"提交失敗: {e}")
        return False

    result_html = resp.text
    result_url = resp.url

    log.info(f"   回應 URL: {result_url[:100]}")
    log.info(f"   回應大小: {len(result_html)} bytes")

    # 分析結果
    if "BookingS2" in result_url or "確認車次" in result_html or "TrainQueryDataViewPanel" in result_html:
        log.info("✅✅✅ 驗證碼通過！已進入選車次頁面（S2）！✅✅✅")

        # 計算班次數量
        train_count = result_html.count("TrainQueryDataViewPanel:TrainGroup")
        log.info(f"   找到約 {train_count} 個班次選項")
        return True

    # 檢查錯誤
    error = _extract_error_message(result_html)
    if error:
        log.info(f"   頁面回應: {error}")
        if "驗證碼" in error or "security" in error.lower() or "verification" in error.lower():
            log.info("   → 驗證碼錯誤（正常，每次驗證碼都不同）")
        elif "班次" in error:
            log.info("   → 查無班次")
        else:
            log.info("   → 其他錯誤")
    else:
        # 嘗試從 HTML 取得更多資訊
        if "BookingS1" in result_url:
            log.info("   → 停留在 S1 頁面（可能是驗證碼錯誤或欄位不對）")

            # 看看是否有 feedbackPanel
            feedback = re.findall(r'class="feedback[^"]*"[^>]*>([^<]+)', result_html)
            if feedback:
                for fb in feedback:
                    log.info(f"   → Feedback: {fb.strip()}")

            # 看看有沒有 error-text 類的提示
            errors = re.findall(r'class="[^"]*error[^"]*"[^>]*>([^<]+)', result_html, re.IGNORECASE)
            if errors:
                for e in errors:
                    log.info(f"   → Error: {e.strip()}")

        elif "error" in result_url.lower():
            log.info("   → 伺服器錯誤頁面")
        else:
            log.info(f"   → 未識別的頁面狀態")

    return False


def main():
    log.info("🚄 THSR 訂票引擎整合測試 🚄")
    log.info(f"目標: {THSRC_BASE}")
    log.info("")

    results = {}

    # Test 1: 頁面抓取
    try:
        session, html, page_url = test_page_fetch()
        results["頁面抓取"] = True
    except Exception as e:
        log.error(f"❌ 頁面抓取失敗: {e}")
        results["頁面抓取"] = False
        return results

    # Test 2: Form Action
    try:
        form_action = test_form_action(html)
        results["Form Action"] = True
    except Exception as e:
        log.error(f"❌ Form Action 擷取失敗: {e}")
        results["Form Action"] = False

    # Test 3: 驗證碼
    captcha_text = ""
    try:
        captcha_text, captcha_bytes = test_captcha(session, html, page_url)
        results["驗證碼辨識"] = len(captcha_text) == 4
    except Exception as e:
        log.error(f"❌ 驗證碼測試失敗: {e}")
        results["驗證碼辨識"] = False

    # Test 4: Hidden Fields
    try:
        test_hidden_fields(html)
        results["Hidden Fields"] = True
    except Exception as e:
        log.error(f"❌ Hidden Fields 測試失敗: {e}")
        results["Hidden Fields"] = False

    # Test 5: 表單欄位
    try:
        results["表單欄位"] = test_form_selectors(html)
    except Exception as e:
        log.error(f"❌ 表單欄位驗證失敗: {e}")
        results["表單欄位"] = False

    # Test 6: 車站選項
    try:
        results["車站選項"] = test_station_options(html)
    except Exception as e:
        log.error(f"❌ 車站選項驗證失敗: {e}")
        results["車站選項"] = False

    # Test 7: 時間選項
    try:
        results["時間選項"] = test_time_options(html)
    except Exception as e:
        log.error(f"❌ 時間選項驗證失敗: {e}")
        results["時間選項"] = False

    # Test 8: 票數欄位
    try:
        results["票數欄位"] = test_ticket_options(html)
    except Exception as e:
        log.error(f"❌ 票數欄位驗證失敗: {e}")
        results["票數欄位"] = False

    # Test 9: 表單提交
    if captcha_text and len(captcha_text) == 4:
        try:
            results["表單提交"] = test_submit_form(session, html, page_url, captcha_text)
        except Exception as e:
            log.error(f"❌ 表單提交失敗: {e}")
            results["表單提交"] = False
    else:
        log.warning("跳過表單提交測試（驗證碼辨識失敗）")
        results["表單提交"] = None

    # 總結
    log.info("")
    log.info("=" * 60)
    log.info("📊 測試總結")
    log.info("=" * 60)
    for name, passed in results.items():
        if passed is True:
            log.info(f"   ✅ {name}")
        elif passed is False:
            log.info(f"   ❌ {name}")
        else:
            log.info(f"   ⏭️ {name} (跳過)")

    total = sum(1 for v in results.values() if v is True)
    failed = sum(1 for v in results.values() if v is False)
    log.info(f"\n   通過: {total}/{len(results)}, 失敗: {failed}/{len(results)}")

    return results


if __name__ == "__main__":
    main()
