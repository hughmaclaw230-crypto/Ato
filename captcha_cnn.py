#!/usr/bin/env python3
"""
THSR Captcha CNN 推論模組
=======================
使用 ONNX Runtime 載入預訓練的 CNN 模型來辨識高鐵驗證碼。
不需要 TensorFlow，只需要 onnxruntime 和 numpy。

模型來源: gary9987/keras-TaiwanHighSpeedRail-captcha (94.5% 準確率)
字元集: 19 字元 (6數字 + 13英文字母)
"""

import os
import io
import logging
import numpy as np
from PIL import Image, ImageFilter

log = logging.getLogger(__name__)

# 高鐵驗證碼固定字元集（19 個字元）
CHAR_MAP = {
    0: '2', 1: '3', 2: '4', 3: '5', 4: '7', 5: '9',
    6: 'A', 7: 'C', 8: 'F', 9: 'H', 10: 'K', 11: 'M',
    12: 'N', 13: 'P', 14: 'Q', 15: 'R', 16: 'T', 17: 'Y', 18: 'Z'
}

# 模型期望的圖片尺寸
MODEL_INPUT_SIZE = (140, 48)  # (width, height)

# ONNX 模型路徑
MODEL_PATH = os.path.join(os.path.dirname(__file__), "captcha_model", "thsr_captcha.onnx")

_session = None


def get_cnn_session():
    """取得 ONNX Runtime 推論 session（延遲載入）"""
    global _session
    if _session is not None:
        return _session
    try:
        import onnxruntime as ort
        if not os.path.exists(MODEL_PATH):
            log.warning(f"CNN 模型不存在: {MODEL_PATH}")
            return None
        _session = ort.InferenceSession(MODEL_PATH, providers=['CPUExecutionProvider'])
        log.info(f"✅ CNN captcha 模型載入完成 ({os.path.getsize(MODEL_PATH) / 1024 / 1024:.1f} MB)")
        return _session
    except ImportError:
        log.warning("onnxruntime 未安裝，CNN 驗證碼辨識不可用")
        return None
    except Exception as e:
        log.error(f"CNN 模型載入失敗: {e}")
        return None


def preprocess_for_cnn(img_bytes: bytes) -> np.ndarray:
    """
    針對 CNN 模型的圖片預處理。
    參考 hyiche/THSR_Captcha_Recognition 的 test_predict 函數：
    1. 快速去噪 (fastNlMeansDenoisingColored 替代)
    2. 反轉二值化 (threshold 127)
    3. 灰階化
    4. 多項式回歸去弧線
    5. Resize 到 (140, 48)
    6. 正規化到 [0, 1] (灰階作為單通道)
    """
    img = Image.open(io.BytesIO(img_bytes))

    # Step 1: 去噪 — 使用中值濾波替代 cv2.fastNlMeansDenoisingColored
    img_denoised = img.filter(ImageFilter.MedianFilter(5))
    # 加強去噪
    img_denoised = img_denoised.filter(ImageFilter.SMOOTH_MORE)

    # Step 2: 反轉二值化
    arr = np.array(img_denoised)
    # 針對彩色圖片做灰階轉換
    if len(arr.shape) == 3:
        gray = np.mean(arr, axis=2)
    else:
        gray = arr.astype(float)

    # 反轉二值化: 低於 threshold → 255 (白), 高於 → 0 (黑)
    binary = np.where(gray < 127, 255, 0).astype(np.uint8)

    # Step 3: 多項式回歸去弧線
    binary = _remove_arc_for_cnn(binary)

    # Step 4: Resize 到模型期望尺寸 (140, 48)
    img_resized = Image.fromarray(binary)
    img_resized = img_resized.resize(MODEL_INPUT_SIZE, Image.BICUBIC)

    # Step 5: 轉為 CNN 輸入格式
    # 模型期望 (1, 48, 140, 3) — 需要 3 通道 RGB
    arr_resized = np.array(img_resized).astype(np.float32)
    # 灰階圖擴展到 3 通道
    if len(arr_resized.shape) == 2:
        arr_resized = np.stack([arr_resized] * 3, axis=-1)
    # 正規化到 [0, 1]
    arr_resized = arr_resized / 255.0
    # 增加 batch 維度
    return arr_resized[np.newaxis, :]  # (1, 48, 140, 3)


def _remove_arc_for_cnn(arr: np.ndarray) -> np.ndarray:
    """
    使用多項式回歸擬合並移除弧線。
    參數對齊 maxmilian/thsrc_captcha:
    - 左邊margin=14, 右邊margin=7, offset=4, degree=2
    """
    try:
        height, width = arr.shape
        # 只取左右邊緣的白色像素來擬合弧線
        # maxmilian/thsrc_captcha: img[:, 14:WIDTH - 7] = 0
        mask = arr.copy()
        mask[:, 14:width - 7] = 0

        ys, xs = np.where(mask == 255)
        if len(xs) < 3:
            return arr  # 找不到足夠的邊緣點

        # 將 y 坐標反轉（畫面座標 → 數學座標）
        Y = height - ys
        # 二次多項式擬合
        coeffs = np.polyfit(xs, Y, 2)
        poly = np.poly1d(coeffs)

        result = arr.copy()
        offset = 4  # maxmilian/thsrc_captcha: offset = 4
        for x in range(width):
            y_pred = height - int(round(poly(x)))
            y_start = max(0, y_pred - offset)
            y_end = min(height, y_pred + offset)
            # 弧線處黑白互換
            result[y_start:y_end, x] = 255 - result[y_start:y_end, x]

        return result
    except Exception:
        return arr


def decode_captcha_cnn(img_bytes: bytes) -> str:
    """
    使用 CNN 模型辨識驗證碼。
    返回 4 碼大寫英數字串。
    """
    session = get_cnn_session()
    if session is None:
        return ""

    try:
        # 預處理
        input_data = preprocess_for_cnn(img_bytes)

        # 推論
        input_name = session.get_inputs()[0].name
        output_names = [o.name for o in session.get_outputs()]
        results = session.run(output_names, {input_name: input_data})

        # 解碼 — 每個 digit 取 softmax 後 argmax
        captcha = ""
        for digit_output in results:
            # digit_output shape: (1, 19)
            probs = _softmax(digit_output[0])
            idx = int(np.argmax(probs))
            confidence = float(probs[idx])
            char = CHAR_MAP.get(idx, '?')
            captcha += char

        log.info(f"CNN 驗證碼辨識: '{captcha}'")
        return captcha

    except Exception as e:
        log.error(f"CNN 辨識失敗: {e}")
        return ""


def _softmax(x):
    """計算 softmax"""
    e_x = np.exp(x - np.max(x))
    return e_x / e_x.sum()
