#!/usr/bin/env python3
"""
THSR 驗證碼模型轉換腳本
=====================
在 Google Colab 上執行此腳本，將 gary9987 的 Keras HDF5 模型轉換為 ONNX 格式。

使用方式 (Colab):
  1. 上傳 cnn_model.hdf5 到 Colab
  2. 執行此腳本
  3. 下載產生的 thsr_captcha.onnx

本地也可以執行（需先 pip install tensorflow tf2onnx）
"""

import os
import sys

def install_deps():
    """安裝依賴（適用於 Colab）"""
    os.system("pip install tf2onnx onnxruntime -q")

def convert_hdf5_to_onnx(hdf5_path, onnx_path):
    """將 Keras HDF5 模型轉換為 ONNX"""
    import tensorflow as tf
    import tf2onnx
    import numpy as np

    print(f"載入模型: {hdf5_path}")
    model = tf.keras.models.load_model(hdf5_path)
    model.summary()

    # 取得輸入 spec
    input_shape = model.input_shape  # (None, 48, 140, 3)
    print(f"輸入形狀: {input_shape}")

    # 驗證模型可以推論
    dummy_input = np.random.rand(1, 48, 140, 3).astype(np.float32)
    outputs = model.predict(dummy_input)
    print(f"輸出數量: {len(outputs)}")
    for i, out in enumerate(outputs):
        print(f"  digit{i+1} 形狀: {out.shape}")

    # 轉換為 ONNX
    print(f"\n轉換為 ONNX: {onnx_path}")
    input_signature = [tf.TensorSpec(shape=(1, 48, 140, 3), dtype=tf.float32, name="input")]

    model_proto, _ = tf2onnx.convert.from_keras(
        model,
        input_signature=input_signature,
        output_path=onnx_path,
        opset=13
    )

    print(f"✅ ONNX 模型已儲存: {onnx_path}")
    print(f"   檔案大小: {os.path.getsize(onnx_path) / 1024 / 1024:.1f} MB")

    # 驗證 ONNX 模型
    verify_onnx(onnx_path, dummy_input, outputs)

def verify_onnx(onnx_path, dummy_input, keras_outputs):
    """驗證 ONNX 模型的輸出與 Keras 一致"""
    import onnxruntime as ort
    import numpy as np

    print("\n驗證 ONNX 模型...")
    session = ort.InferenceSession(onnx_path)

    # 取得輸入/輸出名稱
    input_name = session.get_inputs()[0].name
    output_names = [o.name for o in session.get_outputs()]
    print(f"ONNX 輸入名稱: {input_name}")
    print(f"ONNX 輸出名稱: {output_names}")

    # 推論
    results = session.run(output_names, {input_name: dummy_input})

    # 比較結果
    for i, (keras_out, onnx_out) in enumerate(zip(keras_outputs, results)):
        diff = np.abs(keras_out - onnx_out).max()
        print(f"  digit{i+1} 最大差異: {diff:.6f} {'✅' if diff < 0.001 else '❌'}")

    print("\n✅ ONNX 模型驗證通過！")

if __name__ == "__main__":
    # 預設路徑（可依實際情況修改）
    hdf5_path = sys.argv[1] if len(sys.argv) > 1 else "cnn_model.hdf5"
    onnx_path = sys.argv[2] if len(sys.argv) > 2 else "thsr_captcha.onnx"

    if not os.path.exists(hdf5_path):
        print(f"找不到模型檔: {hdf5_path}")
        print("請上傳 gary9987 的 cnn_model.hdf5 到同一目錄")
        sys.exit(1)

    convert_hdf5_to_onnx(hdf5_path, onnx_path)
