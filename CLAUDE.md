# ATOS 系統開發指南

## 系統簡介

ATOS（Automated Trading Operations System）是一套台指期 + 台股選股的自動化報告與盤中警報系統，透過 Telegram 發送報告和即時建議。

資料來源：FinMind API（唯一來源）
發送管道：Telegram Bot
語言：Python 3.12
AI 引擎：Gemini 2.5 Flash（報告生成）+ Claude API（盤中指令）

---

## 專案架構（現況）

```
資料層      chip_data_engine.py        籌碼快取（外資期貨/現貨/OI/技術/情緒評分）
            data_engine.py             期貨快照/5分K/tick快取
監控層      monitor_engine.py          每2分鐘驅動警報循環
警報層      alert_engine_v2.py         警報文字產生 + 雙層防洪（全域90s + 事件冷卻）
策略層      strategy_engine.py         ATR/Regime 判定
            strategy_filter_engine.py  訊號分級 A/B/C
            target_engine.py           進場計畫（Pivot/Put wall/Call wall）
            behavior_engine.py         Sweep/Trap 偵測
            risk_adjustment.py         大戶行為模型修正
報告層      preopen_report_engine.py   早盤期貨報告（8段決策工具格式）
            stock_report_engine.py     早盤選股報告
            evening_report_engine.py   晚盤複盤報告（5段 + AI fallback）
AI層        ai_report_engine.py        Gemini 報告生成 + 新聞主編審稿
            claude_advisor.py          Claude API 盤中即時指令
新聞層      news_engine.py             AI 主編模式：FinMind + Yahoo RSS
排程層      main_commander.py          統整所有排程
發送層      messenger.py               Telegram Bot 多頻道發送
輔助層      utils.py                   共用工具（format_price 等）
            persistent_state.py        state.json 讀寫
            api_rate_limiter.py        FinMind 500次/小時限速
            data_backfill.py           啟動時補抓缺漏歷史資料
            settlement_engine.py       結算日判斷
            calendar_engine.py         交易日行事曆
            holiday_engine.py          假日快取
            session_engine.py          交易時段判斷
            scenario_engine.py         盤前三劇本文字
            night_session_engine.py    夜盤收盤資料
            intraday_advice_engine.py  盤中建議文字
            transition_engine.py       開收盤過渡訊號
            data_readiness.py          晚盤資料就緒輪詢（is_chip_data_ready / wait_until_ready）
```

已刪除的廢棄檔案（請勿重建）：
- `alert_engine.py` → 已由 `alert_engine_v2.py` 取代
- `report_engine.py` / `report_scheduler.py` / `manual_report.py` → 已由各 `*_report_engine.py` 取代
- `ai_formatter.py` → 已由 `ai_report_engine.py` 取代
- `stock_recommendation_backtest.py` / `v2` / `inspect_stock_picks_cache.py` → 廢棄腳本

---

## 環境變數（.env）

```
FINMIND_TOKEN=...     FinMind API token
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...  多個用逗號分隔
GEMINI_API_KEY=...    Gemini AI 生成報告用
```

Claude API 金鑰不在 .env，由 Claude Code 環境自動注入（`ANTHROPIC_API_KEY`）。

---

## 每日排程時間表

| 時間 | 功能 |
|------|------|
| 05:03 / 08:20 | 更新夜盤收盤價 |
| 07:30 | 更新假日行事曆 |
| 07:31 | 更新結算日資訊 |
| 07:32 | 更新重大事件快取 |
| 08:00 | 更新 Active Pivot（flip） |
| 08:30 | 更新籌碼快取 |
| **08:35** | **早盤期貨報告（Telegram）** |
| **08:40** | **早盤選股報告（Telegram）** |
| 08:45 | 開盤鈴 + 冷靜期 |
| 每 2 分鐘 | 盤中監控循環 |
| 每 5 分鐘 | 新聞輪詢（AI 主編審稿） |
| 13:50 | 午盤籌碼更新 |
| 14:50 | 尾盤 Pivot 更新 |
| **15:00** | **晚盤期貨報告輪詢開始（資料就緒即發，死線 16:00）** |
| **15:30** | **晚盤選股報告輪詢開始（資料就緒即發，死線 18:00）** |

---

## 核心資料結構

### chip_cache.json（chip_data_engine.py 產生）

```json
{
  "meta": { "updated_at": "...", "source_dates": {...}, "version": "v2.0" },
  "futures_chip": {
    "foreign_net": -65501,
    "foreign_net_level": "極強空",
    "foreign_net_chg_1d": 3645,
    "foreign_net_chg_3d": 1797,
    "foreign_action": "回補",
    "foreign_long": 20970,
    "foreign_short": 86471,
    "dealer_net": 734,
    "history_7d": [-69146, -65501, ...]
  },
  "spot_chip": {
    "spot_foreign_net_buy_bn": -93.85,
    "spot_foreign_5d_sum_bn": -521.3,
    "spot_trust_net_buy_bn": -13.67
  },
  "option_oi": {
    "call_wall_strike": 41200,
    "put_wall_strike": 40000,
    "call_put_ratio": 0.49,
    "max_pain_strike": 40000,
    "price_position_pct": 39.7,
    "zone_width": 1200
  },
  "tech_levels": {
    "prev_high": 41802, "prev_low": 40990, "prev_close": 40992,
    "pivot": 41261.3, "r1": 41532.6, "s1": 40990.0,
    "atr_5d": 812.0, "atr_20d": 742.1,
    "ma5": 41191.2, "ma20": 40214.6
  },
  "fear_greed": { "fear_greed": 55, "fear_greed_emotion": "neutral" },
  "large_traders": {
    "top5_net": 0, "top5_long_pct": 0, "top5_short_pct": 0,
    "market_oi": 0
  },
  "foreign_cost_estimate": { "estimated_cost": 41191.2, "source": "ma5" },
  "sentiment": {
    "total_score": -5,
    "bias_label": "偏空",
    "s1_futures_direction": -3,
    "s2_action_quality": 0,
    "s3_spot_confirm": -2,
    "s4_oi_structure": 0,
    "s5_volatility_risk": 0,
    "s6_large_traders": 0,
    "warnings": ["⚠️ 外資期貨淨部位達極端水平"]
  }
}
```

### atos_state.json（monitor_engine 每2分鐘更新）

重要欄位：
- `flip`: **Active Pivot**（`(H+L+C)/3`，超出牆區間改用區間中點）
- `active_pivot`: 同 flip，明確命名版本
- `mid_range`: `(H+L)/2`，保留向後相容
- `r1`, `s1`, `pivot`: preopen 寫入後不被覆蓋（已修 Bug 1）
- `call_wall`, `put_wall`: 大戶牆，從 chip_cache 同步
- `sentiment_score`, `sentiment_bias`: 來自 chip_cache
- `alert_last_sent`: 各警報事件的最後發送時間（持久化冷卻）
- `evening_report_sent_date`: 今日晚盤報告已發日期（防重複）
- `news_last_sent`: 新聞發送記錄（持久化去重）

---

## 策略核心邏輯

### 三層大戶框架

```
層A【大戶牆 — 核心觸發點】
  Call wall = option_oi.call_wall_strike  大戶空頭防線
  Put wall  = option_oi.put_wall_strike   大戶多頭防線

  觸發條件：
  → 突破 Call wall（5分K收盤確認）：目標 Call wall+500，失效：跌回牆下
  → 跌破 Put wall（5分K收盤確認）：目標 Put wall-500→-1000，失效：收回牆上
  → 區間內（Put wall～Call wall）：大戶收割時間價值，不做方向單

層B【外資成本估算】
  粗估成本 = tech_levels.ma5（近5日均收）
  → 外資空單在 MA5 以下開始獲利了結

層C【日內技術參考】
  Active Pivot = (prev_high + prev_low + prev_close) / 3
  → 超出牆區間時改用 (call_wall + put_wall) / 2
  → 站上偏多，站下偏空
  → 反彈至 Pivot 附近量縮未過，空方進場機會
```

### Active Pivot 計算邏輯

```python
_sp = (H + L + C) / 3
if _sp >= call_wall or _sp <= put_wall:
    active_pivot = (call_wall + put_wall) / 2   # 改用區間中點
    pivot_note = "（前日大波動，改用區間中點）"
else:
    active_pivot = _sp
```

### 盤前三劇本

```
劇本A（突破上限）：突破 Call wall → 目標 Call wall+500
劇本B（跌破下限）：跌破 Put wall → 目標 Put wall-500 → Put wall-1000
劇本C（區間震盪）：Put wall～Call wall 之間，Active Pivot 為中軸參考
```

### 進場計畫（target_engine，已對齊大戶牆）

| 模式 | 進場區 | 停損 | TP1 | TP2 |
|------|--------|------|-----|-----|
| 多方 | Pivot ~ Pivot+0.2ATR | Pivot-100 | Call wall | Call wall+500 |
| 空方 | Pivot-0.2ATR ~ Pivot | Pivot+100 | Put wall | Put wall-500 |

停損固定 100 點 = NT$20,000/口

### SentimentScore（-10 到 +10）

| 分項 | 範圍 | 說明 |
|------|------|------|
| S1 外資期貨方向 | -3~+3 | 淨部位 + 3日趨勢 |
| S2 今日動作品質 | -1~+1 | 加多減空 vs 加空減多 |
| S3 現貨同向確認 | -2~+2 | 現期方向一致加分 |
| S4 OI結構 | -2~+2 | price_position_pct + max_pain |
| S5 波動率風險 | -1~0 | ATR 異常放大扣分 |
| S6 大額交易人 | -1~+1 | 無資料時跳過 |

門檻：+6以上強多，+3~+5偏多，-2~+2中性，-3~-5偏空，-6以下強空

### Claude Advisor 輸出格式

```
【指令】做空
【進場條件】下一根 5 分 K 收在 41680 之下
【目標】41500（Put wall 附近）
【停損】41750（進場後100點 = NT$20,000）
【信心分】4/5
【根據】外資淨空 -65,501口 / 現貨賣超 93.8億 / Call wall 假突破壓制
【注意】接近結算，建議縮半倉
```

信心分門檻：做多 < 4 / 做空 < 3 → 不發指令，只發觀察提示
滑點防守：跌破 Put wall 但距離 > 50 點 → 放棄追空，等反彈

---

## 報告格式規範

### 早盤期貨報告（8段）

```
━━ 今天的結構 ━━
━━ 最可能的場景 ━━
━━ 今天等什麼 ━━
━━ 進場怎麼做 ━━
━━ 今天完全不做 ━━
━━ 關鍵價位 ━━
━━ 籌碼數據 ━━
━━ AI 矛盾分析 ━━
```

### 晚盤複盤報告（5段）

```
━━ 今天發生了什麼 ━━
  收盤/H/L | Call wall 狀態 | Put wall 狀態 | Pivot 狀態
  籌碼變化：外資期貨今日回補 +X口（A → B）  ← 箭頭格式
  現貨外資：賣超/買超 X億

━━ 夜盤怎麼做 ━━
  做多/做空（強）/做空（次）/觀望條件
  停損規則（含 NT$20,000 提示）

━━ 今日警報（N則）━━

━━ 籌碼驗證 ━━
  外資期貨：-65,501口（今日回補 +3,645口，前值 -69,146）  ← 逗號格式

━━ AI 夜盤解讀 ━━
  （AI 失敗時顯示系統自動摘要，不空白）
```

### 格式規範

- **所有台指期價位強制整數**（`int(round(float(v)))`），不顯示小數
- 籌碼變化兩種格式：
  - 「今天發生了什麼」→ 箭頭格式：`回補 +3,645口（-69,146 → -65,501）`
  - 「籌碼驗證」→ 逗號格式：`（今日回補 +3,645口，前值 -69,146）`
- Telegram 純文字，不用 Markdown

---

## 警報系統

### 事件類型與冷卻

| 事件 | 說明 | 冷卻 |
|------|------|------|
| LONG_TRAP / SHORT_TRAP | 多空陷阱 | 30分 |
| FLIP_INVALID | Pivot 失效 | 30分 |
| SWEEP / BEARISH_SWEEP / BULLISH_SWEEP | 掃單 | 15分 |
| R1_TOUCH / S1_TOUCH | 觸及壓撐 | 15分 |
| NEUTRAL_ZONE | 進入中性區 | 15分 |
| FLIP_BREAK / FLIP_RECOVER | Pivot 突破/站回 | 10分 |

### 防洪機制（雙層）

1. **全域最小間隔 90 秒**：任意兩則警報強制間隔，防多事件同時爆發
2. **持久化冷卻**：寫入 `atos_state.json["alert_last_sent"]`，重啟不歸零

---

## 新聞引擎（AI 主編模式）

- **來源**：FinMind TaiwanStockNews（2330/2317/2454/2412/2882）+ Yahoo Finance RSS
- **篩選**：30分鐘內只取最新新聞（`NEWS_FRESH_WINDOW=30`），防啟動時炸版
- **AI 審查**：Gemini 判斷「今天會移動行情嗎？影響 > 100 點？訊息具體嗎？」
- **發送上限**：每5分鐘最多 2 則（`MAX_ALERTS_PER_POLL=2`）
- **持久化去重**：發送記錄寫入 `state["news_last_sent"]`

---

## 防護機制總覽

| 機制 | 位置 | 說明 |
|------|------|------|
| API Rate Limiter | api_rate_limiter.py | FinMind 500次/小時，自動等待 |
| Data Backfill | data_backfill.py | 啟動補抓缺漏籌碼歷史 |
| Tick Cache 清理 | main_commander.py 啟動 | 清除 3 天前 tick 資料 |
| 警報防洪（雙層） | alert_engine_v2.py | 全域90s + 事件冷卻，持久化 |
| 新聞防洪（三層） | news_engine.py | 30分鐘窗口 + 持久化去重 + 每輪上限2則 |
| 503 Retry | ai_report_engine.py | Gemini 過載等30秒重試3次 |
| AI Fallback | evening_report_engine.py | Gemini 失敗自動組靜態摘要 |
| 晚盤防重複 | evening_report_engine.py | 今日只發一次，記錄日期 |
| 滑點防守 | claude_advisor.py | 跌破 Put wall > 50點不追空 |
| 結算日提示 | main_commander.py | 結算當天自動發注意訊息 |
| R1/S1 不覆蓋 | monitor_engine.py | preopen 寫入的真實值不被 ATR 值覆蓋 |

---

## Gemini API 每日用量估算

| 功能 | 次數 |
|------|------|
| 早盤期貨報告（3個AI區塊） | 3次 |
| 早盤選股報告（批次點評） | 2次 |
| 晚盤複盤報告 | 1次 |
| 晚盤選股報告 | 1次 |
| 新聞批次審查 | 10~18次 |
| 重大事件快評 | 0~3次 |
| **每日合計** | **17~28次**（免費上限 1,500次） |

---

## 已知待確認項目

### chip_data_engine 大額交易人欄位（Bug 3）

**位置**：`chip_data_engine.py` → `fetch_large_traders()`
**問題**：Top5 數值全部是 0，需要確認 `TaiwanFuturesOpenInterestLargeTraders` 的實際 name 欄位值
**待確認**：在真實環境執行後貼出 `name 值: [...]` 的輸出，再對照修正欄位名稱

---

## 開發規範

1. **不動現有邏輯**：修改時只做最小範圍改動，不重構沒問題的部分
2. **向後相容**：新欄位加在現有欄位後面，不刪舊欄位
3. **safe_execute 裝飾器**：所有對外 API 呼叫的函式都加 `@safe_execute`
4. **失敗不影響主流程**：任何一個資料集取得失敗，用空值繼續，不中斷
5. **Telegram 純文字**：不用 Markdown，避免解析問題
6. **台指期價位整數化**：所有顯示價位用 `int(round(float(v)))`，不顯示小數
7. **共用工具**：format_price 等通用函式統一從 `utils.py` import，不各自定義

---

## 快速測試指令

```bash
# 測試籌碼引擎
python3 chip_data_engine.py

# 測試早盤報告
python3 -c "from preopen_report_engine import build_preopen_sip_message; print(build_preopen_sip_message())"

# 測試晚盤報告
python3 -c "from evening_report_engine import build_evening_report_message; print(build_evening_report_message())"

# 測試選股報告
python3 stock_report_engine.py

# 啟動主系統
python3 main_commander.py
```

## 啟動方式（Windows）

```
雙擊 啟動ATOS.bat
```

- 自動設定 PYTHONUTF8=1（解決 Windows emoji 編碼問題）
- 系統崩潰後自動 10 秒重啟
- 正常退出（errorlevel 0）不重啟
