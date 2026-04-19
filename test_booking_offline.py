#!/usr/bin/env python3
"""
THSR 訂票引擎解析邏輯測試（離線版）
===================================
不需要網路連線，直接測試 HTML 解析和驗證碼預處理邏輯。
"""

import sys
import os
import io
import logging

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# ─── 模擬的 HTML（基於 Playwright codegen 實際擷取的頁面結構）───

MOCK_S1_HTML = """
<html>
<body>
<form id="BookingS1Form" action="/IMINT/;jsessionid=ABC123?wicket:interface=:0:BookingS1Form::IFormSubmitListener">
<input type="hidden" name="BookingS1Form:hf:0" value=""/>

<select id="BookingS1Form_selectStartStation" name="selectStartStation" class="uk-select out-station">
  <option value="" selected>請選擇</option>
  <option value="1">南港</option>
  <option value="2">台北</option>
  <option value="3">板橋</option>
  <option value="4">桃園</option>
  <option value="5">新竹</option>
  <option value="6">苗栗</option>
  <option value="7">台中</option>
  <option value="8">彰化</option>
  <option value="9">雲林</option>
  <option value="10">嘉義</option>
  <option value="11">台南</option>
  <option value="12">左營</option>
</select>

<select id="BookingS1Form_selectDestinationStation" name="selectDestinationStation" class="uk-select out-station">
  <option value="" selected>請選擇</option>
  <option value="1">南港</option>
  <option value="2">台北</option>
  <option value="3">板橋</option>
  <option value="4">桃園</option>
  <option value="5">新竹</option>
  <option value="6">苗栗</option>
  <option value="7">台中</option>
  <option value="8">彰化</option>
  <option value="9">雲林</option>
  <option value="10">嘉義</option>
  <option value="11">台南</option>
  <option value="12">左營</option>
</select>

<select name="trainCon:trainRadioGroup">
  <option value="0" selected>標準車廂對號座</option>
  <option value="1">商務車廂</option>
</select>

<select name="tripCon:typesoftrip">
  <option value="0" selected>單程</option>
  <option value="1">去回程</option>
</select>

<select name="seatCon:seatRadioGroup">
  <option value="0" selected>無座位偏好</option>
  <option value="1">靠窗</option>
  <option value="2">靠走道</option>
</select>

<input name="trainCon:trainDate" value="2026/04/10" class="uk-input"/>

<select name="trainCon:trainTime" class="uk-select out-time">
  <option value="1201A">00:00</option>
  <option value="0530A">05:30</option>
  <option value="0600A">06:00</option>
  <option value="0630A">06:30</option>
  <option value="0700A">07:00</option>
  <option value="0730A">07:30</option>
  <option value="0800A">08:00</option>
  <option value="0830A">08:30</option>
  <option value="0900A">09:00</option>
  <option value="0930A">09:30</option>
  <option value="1000A">10:00</option>
  <option value="1030A">10:30</option>
  <option value="1100A">11:00</option>
  <option value="1130A">11:30</option>
  <option value="1200N">12:00</option>
  <option value="1230A">12:30</option>
  <option value="0100P">13:00</option>
  <option value="0130P">13:30</option>
  <option value="0200P">14:00</option>
  <option value="0230P">14:30</option>
  <option value="0300P">15:00</option>
  <option value="0330P">15:30</option>
  <option value="0400P">16:00</option>
  <option value="0430P">16:30</option>
  <option value="0500P">17:00</option>
  <option value="0530P">17:30</option>
  <option value="0600P">18:00</option>
  <option value="0630P">18:30</option>
  <option value="0700P">19:00</option>
  <option value="0730P">19:30</option>
  <option value="0800P">20:00</option>
  <option value="0830P">20:30</option>
  <option value="0900P">21:00</option>
  <option value="0930P">21:30</option>
  <option value="1000P">22:00</option>
  <option value="1030P">22:30</option>
  <option value="1100P">23:00</option>
  <option value="1130P">23:30</option>
</select>

<select name="ticketCon:ticketCasualRecord:0:ticketCount">
  <option value="0">0</option><option value="1" selected>1</option>
  <option value="2">2</option><option value="3">3</option>
  <option value="4">4</option><option value="5">5</option>
  <option value="6">6</option><option value="7">7</option>
  <option value="8">8</option><option value="9">9</option>
  <option value="10">10</option>
</select>

<select name="ticketCon:ticketCasualRecord:1:ticketCount">
  <option value="0" selected>0</option>
</select>

<select name="ticketCon:ticketCasualRecord:2:ticketCount">
  <option value="0" selected>0</option>
</select>

<select name="ticketCon:ticketCasualRecord:3:ticketCount">
  <option value="0" selected>0</option>
</select>

<select name="ticketCon:ticketCasualRecord:4:ticketCount">
  <option value="0" selected>0</option>
</select>

<input name="trainCon:trainNumber" value=""/>

<img id="BookingS1Form_homeCaptcha_captchaImage"
     src="/IMINT/;jsessionid=ABC123?wicket:interface=:0:BookingS1Form:homeCaptcha:captchaImage:IResourceListener"/>
<input id="securityCode" name="homeCaptcha:securityCode" type="text"/>

<input id="SubmitButton" name="SubmitButton" type="submit" value="開始查詢"/>
</form>
</body>
</html>
"""

MOCK_S2_HTML = """
<html>
<body>
<form id="BookingS2Form" action="/IMINT/;jsessionid=ABC123?wicket:interface=:0:BookingS2Form::IFormSubmitListener">
<table>
<tr>
  <td><input type="radio" name="TrainQueryDataViewPanel:TrainGroup" value="0_603_0800_0955"/></td>
  <td>603</td><td>08:00</td><td>09:55</td><td>1:55</td>
</tr>
<tr>
  <td><input type="radio" name="TrainQueryDataViewPanel:TrainGroup" value="1_605_0830_1025"/></td>
  <td>605</td><td>08:30</td><td>10:25</td><td>1:55</td>
</tr>
<tr>
  <td><input type="radio" name="TrainQueryDataViewPanel:TrainGroup" value="2_609_0900_1055"/></td>
  <td>609</td><td>09:00</td><td>10:55</td><td>1:55</td>
</tr>
</table>
<input type="submit" name="SubmitButton" value="確認車次"/>
</form>
</body>
</html>
"""


def test_form_action():
    """測試 form action 擷取"""
    from booking_engine import _extract_form_action
    action = _extract_form_action(MOCK_S1_HTML)
    assert action is not None, "Form action 擷取失敗"
    assert "IFormSubmitListener" in action
    assert "jsessionid" in action
    log.info(f"✅ Form Action: {action[:80]}")


def test_captcha_url():
    """測試驗證碼 URL 擷取"""
    from booking_engine import _extract_captcha_url
    url = _extract_captcha_url(MOCK_S1_HTML, "https://irs.thsrc.com.tw/IMINT/")
    assert url is not None, "驗證碼 URL 擷取失敗"
    assert "captchaImage" in url or "passCode" in url
    log.info(f"✅ Captcha URL: {url[:100]}")


def test_hidden_fields():
    """測試隱藏欄位擷取"""
    from booking_engine import _extract_form_fields
    fields = _extract_form_fields(MOCK_S1_HTML)
    assert "BookingS1Form:hf:0" in fields, "缺少 BookingS1Form:hf:0"
    log.info(f"✅ Hidden Fields: {len(fields)} 個 → {list(fields.keys())}")


def test_time_conversion():
    """測試時間格式轉換"""
    from booking_engine import _convert_time_to_form_value
    tests = [
        ("06:00", "0600A"),
        ("08:00", "0800A"),
        ("12:00", "1200N"),
        ("13:00", "0100P"),
        ("13:30", "0130P"),
        ("18:00", "0600P"),
        ("21:00", "0900P"),
        ("00:00", "1201A"),
    ]
    all_ok = True
    for time_str, expected in tests:
        result = _convert_time_to_form_value(time_str)
        ok = result == expected
        status = "✅" if ok else "❌"
        log.info(f"   {status} {time_str} → {result} (期望 {expected})")
        if not ok:
            all_ok = False
    assert all_ok, "時間轉換有誤"
    log.info("✅ 所有時間轉換正確")


def test_station_map():
    """測試車站對照表"""
    from booking_engine import STATION_MAP
    assert STATION_MAP["南港"] == "1"
    assert STATION_MAP["台北"] == "2"
    assert STATION_MAP["左營"] == "12"
    assert STATION_MAP["台中"] == "7"
    log.info(f"✅ 車站對照表: {len(STATION_MAP)} 站 → {list(STATION_MAP.keys())}")


def test_seat_map():
    """測試座位對照表"""
    from booking_engine import SEAT_MAP
    assert SEAT_MAP["無座位偏好"] == "0"
    assert SEAT_MAP["靠窗"] == "1"
    assert SEAT_MAP["靠走道"] == "2"
    log.info(f"✅ 座位對照表: {SEAT_MAP}")


def test_s2_parsing():
    """測試 S2 車次解析"""
    from booking_engine import _extract_s2_form_action, _parse_train_list, _find_best_train

    action = _extract_s2_form_action(MOCK_S2_HTML)
    assert action is not None
    log.info(f"✅ S2 Form Action: {action[:80]}")

    trains = _parse_train_list(MOCK_S2_HTML)
    assert len(trains) == 3, f"應有 3 個班次，但找到 {len(trains)}"
    log.info(f"✅ 班次列表: {len(trains)} 個")
    for t in trains:
        log.info(f"   車次={t.get('train_no', '?')} 出發={t.get('depart', '?')} 到達={t.get('arrive', '?')}")

    # 測試最佳班次選取
    best = _find_best_train(trains, "08:00", "")
    assert best is not None
    log.info(f"✅ 最佳班次 (08:00): {best}")

    best_exact = _find_best_train(trains, "08:30", "605")
    assert best_exact is not None
    log.info(f"✅ 指定班次 (605): {best_exact}")


def test_error_parsing():
    """測試錯誤訊息解析"""
    from booking_engine import _extract_error_message

    error_html = '<span class="feedbackPanelERROR">驗證碼輸入錯誤</span>'
    msg = _extract_error_message(error_html)
    assert msg is not None
    assert "驗證碼" in msg
    log.info(f"✅ 錯誤解析: {msg}")

    no_error_html = '<div>正常頁面</div>'
    msg2 = _extract_error_message(no_error_html)
    assert msg2 is None
    log.info("✅ 正常頁面無錯誤")


def test_captcha_cnn():
    """測試 CNN 模組載入"""
    try:
        from captcha_cnn import get_cnn_session, CHAR_MAP, MODEL_INPUT_SIZE
        session = get_cnn_session()
        if session is not None:
            log.info(f"✅ CNN 模型載入成功")
            log.info(f"   字元集: {len(CHAR_MAP)} 字元 → {''.join(CHAR_MAP.values())}")
            log.info(f"   輸入尺寸: {MODEL_INPUT_SIZE}")
        else:
            log.info("⚠️ CNN 模型不可用（模型檔不存在或 onnxruntime 未安裝）")
    except ImportError as e:
        log.info(f"⚠️ captcha_cnn 模組載入失敗: {e}")


def test_ddddocr():
    """測試 ddddocr 模組"""
    try:
        from booking_engine import get_ocr
        ocr = get_ocr()
        if ocr is not None:
            log.info("✅ ddddocr OCR 引擎可用")
        else:
            log.info("⚠️ ddddocr 不可用")
    except Exception as e:
        log.info(f"⚠️ ddddocr 載入失敗: {e}")


def test_preprocessing():
    """測試驗證碼圖片預處理（使用合成圖片）"""
    from booking_engine import preprocess_captcha_image
    import numpy as np
    from PIL import Image

    # 建立一個模擬的驗證碼圖片 (140x48)
    img = Image.new("RGB", (140, 48), (255, 255, 255))
    # 加入一些模擬的文字像素
    pixels = img.load()
    for x in range(30, 110):
        for y in range(15, 35):
            if (x + y) % 7 == 0:
                pixels[x, y] = (0, 0, 0)
    # 加入模擬弧線
    for x in range(140):
        y = int(10 + 5 * (x / 140) ** 2 * 30)
        if 0 <= y < 48:
            pixels[x, y] = (100, 100, 100)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img_bytes = buf.getvalue()

    # 測試預處理
    processed = preprocess_captcha_image(img_bytes)
    assert len(processed) > 0, "預處理結果為空"
    log.info(f"✅ 預處理完成: {len(img_bytes)} → {len(processed)} bytes")


def test_form_data_construction():
    """測試完整表單資料建構"""
    from booking_engine import (
        STATION_MAP, SEAT_MAP,
        _extract_form_fields, _extract_form_action,
        _convert_time_to_form_value,
    )

    config = {
        "from_station": "南港",
        "to_station": "左營",
        "travel_date": "2026-04-15",
        "travel_time": "08:00",
        "adult_count": 2,
        "seat_type": "靠窗",
    }

    hidden_fields = _extract_form_fields(MOCK_S1_HTML)
    form_action = _extract_form_action(MOCK_S1_HTML)

    form_data = {
        **hidden_fields,
        "selectStartStation": STATION_MAP[config["from_station"]],
        "selectDestinationStation": STATION_MAP[config["to_station"]],
        "trainCon:trainRadioGroup": "0",
        "tripCon:typesoftrip": "0",
        "seatCon:seatRadioGroup": SEAT_MAP[config["seat_type"]],
        "trainCon:trainDate": config["travel_date"].replace("-", "/"),
        "trainCon:trainTime": _convert_time_to_form_value(config["travel_time"]),
        "trainCon:trainNumber": "",
        "ticketCon:ticketCasualRecord:0:ticketCount": str(config["adult_count"]),
        "ticketCon:ticketCasualRecord:1:ticketCount": "0",
        "ticketCon:ticketCasualRecord:2:ticketCount": "0",
        "ticketCon:ticketCasualRecord:3:ticketCount": "0",
        "ticketCon:ticketCasualRecord:4:ticketCount": "0",
        "homeCaptcha:securityCode": "A2B3",
        "SubmitButton": "開始查詢",
    }

    # 驗證關鍵欄位
    assert form_data["selectStartStation"] == "1", f"出發站應為 1, 實際 {form_data['selectStartStation']}"
    assert form_data["selectDestinationStation"] == "12", f"到達站應為 12"
    assert form_data["seatCon:seatRadioGroup"] == "1", f"座位應為 1 (靠窗)"
    assert form_data["trainCon:trainDate"] == "2026/04/15", f"日期應為 2026/04/15"
    assert form_data["trainCon:trainTime"] == "0800A", f"時間應為 0800A"
    assert form_data["ticketCon:ticketCasualRecord:0:ticketCount"] == "2"

    log.info("✅ 完整表單資料建構正確:")
    log.info(f"   南港(1)→左營(12) 2026/04/15 08:00(0800A)")
    log.info(f"   靠窗(1) 全票x2 驗證碼=A2B3")
    log.info(f"   共 {len(form_data)} 個欄位")


def main():
    log.info("🚄 THSR 訂票引擎離線測試 🚄")
    log.info("")

    tests = [
        ("Form Action 擷取", test_form_action),
        ("驗證碼 URL 擷取", test_captcha_url),
        ("Hidden Fields 擷取", test_hidden_fields),
        ("時間格式轉換", test_time_conversion),
        ("車站對照表", test_station_map),
        ("座位對照表", test_seat_map),
        ("S2 車次解析", test_s2_parsing),
        ("錯誤訊息解析", test_error_parsing),
        ("CNN 模組載入", test_captcha_cnn),
        ("ddddocr 引擎", test_ddddocr),
        ("驗證碼預處理", test_preprocessing),
        ("完整表單建構", test_form_data_construction),
    ]

    results = {}
    for name, fn in tests:
        log.info(f"\n{'─' * 50}")
        log.info(f"測試: {name}")
        log.info(f"{'─' * 50}")
        try:
            fn()
            results[name] = True
        except AssertionError as e:
            log.error(f"❌ {name}: {e}")
            results[name] = False
        except Exception as e:
            log.error(f"❌ {name}: {e}")
            results[name] = False

    # 總結
    log.info(f"\n{'=' * 50}")
    log.info("📊 測試總結")
    log.info(f"{'=' * 50}")
    for name, passed in results.items():
        log.info(f"   {'✅' if passed else '❌'} {name}")

    total = sum(1 for v in results.values() if v)
    log.info(f"\n   結果: {total}/{len(results)} 通過")

    return all(results.values())


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
