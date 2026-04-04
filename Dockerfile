FROM python:3.10-slim

# 系統依賴（Playwright Chromium + TensorFlow）
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    libglib2.0-0 libnss3 libnspr4 libdbus-1-3 \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libxshmfence1 \
    fonts-liberation fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先安裝 Python 依賴
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安裝 Playwright + Chromium
RUN playwright install chromium
RUN playwright install-deps chromium

# 複製專案檔案
COPY . .

# 讓 start.sh 可執行
RUN chmod +x start.sh

# 暴露 HTTP 端口
EXPOSE 5000

# 用 gunicorn 執行 Flask
CMD ["bash", "start.sh"]
