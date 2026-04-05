# 🚅 AutoTHSR (ATO)

高鐵自動訂票 + 時刻表查詢 — Telegram Bot 指令控制 + 管理員審核

## 功能

- 🔍 **高鐵時刻表查詢** — 即時查詢任意站點的高鐵班次
- 🤖 **Telegram Bot 控制** — 透過 Telegram 指令操作一切
- 🔐 **用戶認證審核** — 只有管理員核准的用戶才能使用
- ⏱️ **6 小時保活** — 收到訊息自動重置計時
- 🚅 **自動訂票** — CNN 驗證碼辨識 + Playwright 自動化

## Telegram 指令

| 指令 | 說明 |
|------|------|
| `/start` | 註冊 / 歡迎 |
| `/help` | 顯示所有指令 |
| `/timetable <出發站> <到達站> <日期> [時間]` | 查詢高鐵時刻表 |
| `/from <站名>` | 設定出發站 |
| `/to <站名>` | 設定到達站 |
| `/date <日期>` | 設定出發日期 |
| `/time <時間>` | 設定出發時間 |
| `/count <人數>` | 設定票數 |
| `/seat <偏好>` | 設定座位偏好 |
| `/id <身分證>` | 設定身分證字號 |
| `/phone <手機>` | 設定手機號碼 |
| `/book` | 開始訂票 |
| `/stop` | 停止訂票 |
| `/status` | 查看訂票狀態 |
| `/settings` | 查看目前設定 |
| `/stations` | 車站列表 |
| `/times` | 可選時段 |

## 部署到 Render

### 環境變數

| 變數 | 說明 |
|------|------|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token |
| `ADMIN_TELEGRAM_CHAT_ID` | 管理員 Telegram Chat ID |
| `RENDER_EXTERNAL_URL` | Render 外部 URL |
| `THSRC_ID` | 身分證字號（可選） |
| `THSRC_PHONE` | 手機號碼（可選） |

### 部署步驟

1. Push 到 GitHub
2. 在 Render 建立 Web Service，連結此 repo
3. 設定環境變數
4. Deploy

## 車站列表

南港、台北、板橋、桃園、新竹、苗栗、台中、彰化、雲林、嘉義、台南、左營
