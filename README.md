# 役男跨縣市代辦體檢名額監測

監測內政部「受理外縣市役男申請代辦體檢線上預約系統」
（`https://service.dca.moi.gov.tw/military-subPhyExam/...`），有場次出現名額就通知你。
程式只讀取場次列表，**不會自動送出申請，也不需要也不會填你的身分證字號等個資**；收到通知後請自己上官網申請。

## 安裝（macOS）

```bash
cd 役男代檢名額監測
python3 -m venv .venv && source .venv/bin/activate
pip install playwright
playwright install chromium
cp config.example.json config.json
```

## 設定 `config.json`

| 欄位 | 說明 |
|---|---|
| `exam_county` | 預設 `"全部"`：一次檢查所有縣市（自動排除戶籍地）。也可以填單一縣市 `"臺北市"` 或清單 `["臺北市", "新北市"]` |
| `household_county` | 你的戶籍地縣市（例：`臺南市`），程式會在第2步自動選 |
| `household_town` | 戶籍地鄉鎮市區（例：`東區`），空白就選第一個；只用來進到梯次頁，不影響名額 |
| `hospital_keywords` | 只看特定醫院，例 `["萬芳", "三軍"]`；空陣列＝全部 |
| `date_from` / `date_to` | 日期範圍，例 `"115/11/01"` 或 `"2026-11-01"`；空字串＝不限 |
| `interval_seconds` | 每一輪（所有縣市都檢查完一次）之間的間隔，預設 300 秒，最低 60 秒（請不要設太短，避免造成官網負擔） |
| `notify.desktop` | macOS 桌面通知（預設開） |
| `notify.telegram` | Telegram Bot 的 `bot_token` 和 `chat_id`，手機也收得到 |
| `notify.ntfy_topic` | 取一個不好猜的名字，手機裝 ntfy App 訂閱同名 topic 即可收推播（免註冊） |

> LINE Notify 已於 2025 年 3 月停止服務，所以改用 Telegram / ntfy。

## 執行

```bash
python3 monitor.py --test-notify     # 先確認通知收得到
python3 monitor.py --once --show     # 開瀏覽器視窗跑一次，看它有沒有走到場次頁
python3 monitor.py                   # 正式開始持續監測（Ctrl+C 停止）
```

官網系統沒開放時會顯示「系統訊息：未提供指定服務！」，這時程式只會檢查總入口，等到有縣市開放時會先通知你「有縣市開放申請了」，再接著檢查場次名額。

同一個場次只會通知一次；若名額又滿了、之後再釋出，會再通知一次。連續失敗 5 次也會通知你。

## 抓不到場次時

官網的頁面結構可能和程式預設的不同（例如要先按「我同意」之類的按鈕）。請執行：

```bash
python3 monitor.py --once --debug
```

`debug/` 資料夾會存下每一步（須知、戶籍地、梯次）的截圖和 HTML。歡迎到 GitHub 開 [issue](https://github.com/CYT-19/military-exam-slot-monitor/issues) 回報，並附上 `debug/` 裡的截圖；上傳前請先確認截圖中沒有身分證字號、姓名等個人資料。

## 判斷規則

- 含「額滿／已滿／截止／停止受理」或選項不能點 → 沒名額
- 「剩餘名額：N」→ N>0 有名額
- 「A/B」格式 → 視為「已報名/總名額」，A<B 有名額（若官網的意思相反，請告訴我）
