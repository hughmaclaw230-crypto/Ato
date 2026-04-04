# 🚅 THSRC Sniper v2.0 — LINE & Telegram 雙指令控制

高鐵自動訂票系統，透過 **LINE** 或 **Telegram** 傳送指令即可設定並觸發訂票。
使用 CNN 辨識驗證碼，成功後即時通知。

## 特色

- 🤖 **雙 Bot 控制** — LINE + Telegram 同時接收指令
- 💬 **中文指令** — LINE 支援中文指令（如「訂票」、「設定」）
- ⚙️ **即時設定** — 透過訊息修改出發站、日期、時間等
- 🏓 **6 小時保活** — 收到訊息自動重置 session，防止 Render 休眠
- 🔄 **背景訂票** — 訂票在背景執行，完成後自動通知

## 檔案結構

```
thsr-online/
├── app.py               # Flask 主服務（Webhook 入口）
├── wsgi.py              # Gunicorn WSGI 入口
├── booking_engine.py    # Playwright + CNN 訂票引擎
├── start.sh             # 啟動腳本
├── Dockerfile           # Docker 設定
├── render.yaml          # Render 服務設定
├── requirements.txt
├── main.py              # (舊版) 單次執行版本
└── model/
    └── thsrc_cnn_model.hdf5   # CNN 模型（自行下載）
```

---

## 指令一覽

### 共用指令（Telegram / LINE）

| 指令 | 中文（LINE） | 說明 |
|------|-------------|------|
| `/help` | 幫助 | 顯示說明 |
| `/settings` | 設定 | 查看目前設定 |
| `/from <站名>` | 出發站 <站名> | 設定出發站 |
| `/to <站名>` | 到達站 <站名> | 設定到達站 |
| `/date <日期>` | 日期 <日期> | 設定日期（例：2025/06/01）|
| `/time <時間>` | 時間 <時間> | 設定時間（例：07:30）|
| `/count <人數>` | 人數 <N> | 設定成人票數 |
| `/seat <偏好>` | 座位 <偏好> | 無座位偏好/靠窗/靠走道 |
| `/id <身分證>` | 身分證 <號碼> | 設定身分證字號 |
| `/phone <手機>` | 手機 <號碼> | 設定手機號碼 |
| `/book` | 訂票 / 搶票 | 開始訂票 |
| `/stop` | 停止 / 取消 | 停止訂票 |
| `/status` | 狀態 | 查看訂票進度 |
| `/stations` | 車站 | 車站列表 |
| `/times` | 時段 | 可選時段列表 |

---

## 部署步驟

### 1. 取得 CNN 模型

```bash
mkdir -p model
wget https://github.com/maxmilian/thsrc_captcha/raw/master/thsrc_cnn_model.hdf5 \
     -O model/thsrc_cnn_model.hdf5
```

### 2. 推到 GitHub

```bash
git init
git add .
git commit -m "THSRC Sniper v2.0"
git remote add origin https://github.com/你的帳號/thsr-online.git
git push -u origin main
```

### 3. 在 Render 建立 Web Service

1. 登入 [render.com](https://render.com)
2. **New → Web Service**
3. 連接 GitHub repo
4. Runtime 選 **Docker**
5. 名稱設為 `thsr-online`

### 4. 設定 Environment Variables

必填：

| 變數名稱 | 說明 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token |
| `ADMIN_TELEGRAM_CHAT_ID` | 管理員 Chat ID |
| `LINE_CHANNEL_SECRET` | LINE Channel Secret |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Channel Access Token |
| `RENDER_EXTERNAL_URL` | 如 `https://thsr-online.onrender.com` |

選填（可透過 Bot 指令修改）：

| 變數名稱 | 預設 | 說明 |
|---|---|---|
| `THSRC_ID` | - | 身分證字號 |
| `THSRC_PHONE` | - | 手機號碼 |
| `FROM_STATION` | 南港 | 出發站 |
| `TO_STATION` | 左營 | 到達站 |

### 5. 設定 LINE Webhook

部署成功後：
1. 到 [LINE Developers Console](https://developers.line.biz/)
2. 選擇你的 Messaging API Channel
3. **Webhook URL** 設為：`https://thsr-online.onrender.com/api/webhook/line`
4. 開啟 **Use webhook**

Telegram webhook 會在啟動時自動註冊。

---

## Session 保活機制

- 啟動後自動保持 **6 小時** 活躍狀態
- 每 5 分鐘 self-ping `/api/health` 防止 Render 休眠
- 收到任何 LINE 或 Telegram 訊息會**自動重置計時器**
- 6 小時後若無訊息，停止 keep-alive（Render 可能休眠）
- 再次傳送訊息會**喚醒服務**並重新開始 6 小時計時

---

## 取得 Bot 帳號

### Telegram Bot
1. 搜尋 **@BotFather**
2. 發送 `/newbot`，取得 Token
3. 搜尋 **@userinfobot** 取得你的 Chat ID

### LINE Messaging API
1. 到 [LINE Developers](https://developers.line.biz/)
2. 建立 Provider → 建立 Messaging API Channel
3. 取得 **Channel Secret** 和 **Channel Access Token**

---

## 注意事項

- 此工具僅供個人學習研究使用
- 請勿用於大量搶票，避免違反高鐵服務條款
- CNN 模型準確率約 97–99%，偶爾需重試幾次
- Render 免費方案每月 750 小時，搭配保活機制綽綽有餘
