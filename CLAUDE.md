# ATOS 系統開發指南

## 系統簡介

ATOS（Automated Trading Operations System）是一套台指期 + 台股選股的自動化報告與盤中警報系統，透過 Telegram 發送報告和即時建議。

資料來源：FinMind API（唯一來源）
發送管道：Telegram Bot
語言：Python 3.12

---

## 專案架構

```
資料層      chip_data_engine.py   籌碼快取（外資期貨/現貨/OI/技術/情緒評分）
            data_engine.py        期貨快照/5分K/Pivot R1 S1
監控層      monitor_engine.py     每30秒跑一次，驅動警報
警報層      alert_engine_v2.py    警報文字產生
策略層      strategy_engine.py    ATR/Regime
            strategy_filter_engine.py  訊號分級 A/B/C
            behavior_engine.py    Sweep/Trap 偵測
報告層      preopen_report_engine.py   早盤期貨報告
            stock_report_engine.py     早盤選股報告
            evening_report_engine.py   晚盤複盤報告
AI層        ai_formatter.py       現有：Gemini 純排版（待升級）
            ai_report_engine.py   【待建】Claude API 報告產生器
            claude_advisor.py     【待建】盤中 AI 即時指令引擎
排程層      main_commander.py     統整排程
發送層      messenger.py          Telegram 發送
```

---

## 環境變數（.env）

```
FINMIND_TOKEN=...     FinMind API token
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...  多個用逗號分隔
GEMINI_API_KEY=...    現有 AI 格式化用
```

Claude API 金鑰不在 .env，由 Claude Code 環境自動注入（`ANTHROPIC_API_KEY`）。

---

## 核心資料結構

### chip_cache.json（v2，chip_data_engine.py 產生）

```json
{
  "meta": { "updated_at": "...", "source_dates": {...}, "version": "v2.0" },
  "futures_chip": {
    "foreign_net": -50963,
    "foreign_net_level": "極強空",
    "foreign_net_chg_1d": 0,
    "foreign_net_chg_3d": 1797,
    "foreign_action": "無明顯變化",
    "foreign_long": 20970,
    "foreign_short": 71933,
    "dealer_net": 734,
    "history_7d": [-54530, -54225, -53072, -52760, -51068, -50963, -50963]
  },
  "spot_chip": {
    "spot_foreign_net_buy_bn": -104.27,
    "spot_foreign_5d_sum_bn": -521.3,
    "spot_trust_net_buy_bn": -13.67
  },
  "option_oi": {
    "call_wall_strike": 42500,
    "put_wall_strike": 40000,
    "call_put_ratio": 0.645,
    "max_pain_strike": 40000,
    "price_position_pct": 39.7,
    "zone_width": 2500
  },
  "tech_levels": {
    "prev_high": 41223, "prev_low": 40448, "prev_close": 40511,
    "pivot": 40727.3, "r1": 41006.6, "s1": 40231.6,
    "mid_range": 40835.5,
    "atr_5d": 942.0, "atr_20d": 792.1,
    "ma5": 41191.2, "ma20": 40214.6
  },
  "fear_greed": { "fear_greed": 62, "fear_greed_emotion": "greed" },
  "large_traders": {
    "top5_net": 0, "top5_long_pct": 0, "top5_short_pct": 0,
    "market_oi": 0
  },
  "sentiment": {
    "total_score": -5,
    "bias_label": "偏空",
    "s1_futures_direction": -3,
    "s2_action_quality": 0,
    "s3_spot_confirm": -2,
    "s4_oi_structure": 0,
    "s5_volatility_risk": 0,
    "s6_large_traders": 0,
    "warnings": ["⚠️ 外資期貨淨部位達極端水平..."]
  }
}
```

### atos_state.json（monitor_engine 每30秒更新）

重要欄位：
- `flip`: 前日收盤（舊邏輯，待移除）
- `r1`, `s1`, `pivot`: **bug 已知** — 每30秒被 strategy_engine 的 ATR 值覆蓋
- `sentiment_score`, `sentiment_bias`: 來自 chip_cache
- `call_pressure`, `put_support`: 選擇權 OI
- `foreign_futures_net`: 外資淨部位

---

## 策略核心邏輯（已確認）

### 三層策略框架（Put wall / Call wall 為核心觸發點）

```
層A【月度大戶框架 — 核心觸發點】
  Call wall = option_oi.call_wall_strike（42500）大戶空頭防線
  Put wall  = option_oi.put_wall_strike（40000） 大戶多頭防線

  觸發條件：
  → 突破 Call wall（5分K收盤確認）：空頭防線失守，可能軋空
      目標：Call wall + 500，失效：跌回 Call wall 下方
      多方信心分門檻：>= 4
  → 跌破 Put wall（5分K收盤確認）：多頭防線失守，加速下跌
      目標：Put wall - 500 → Put wall - 1000，失效：收回 Put wall 上方
      空方信心分門檻：>= 3（空方背景下標準放寬）
  → 在 Put wall ～ Call wall 之間：大戶收割時間價值，不做方向單

層B【外資成本估算】
  粗估成本 = tech_levels.ma5（近5日均收）
  → 外資空單在這位置以下開始獲利了結

層C【日內技術參考】
  中軸 mid_range = (prev_high + prev_low) / 2
  → 在 Put wall ～ Call wall 區間內：站上中軸偏多，站下中軸偏空
  → 反彈至中軸附近量縮未過，空方進場機會
  Pivot / R1 / S1 保留為日內次要參考
```

### 盤前三劇本邏輯

```
劇本A（突破上限）：突破 Call wall → 目標 Call wall+500
劇本B（跌破下限）：跌破 Put wall → 目標 Put wall-500 → Put wall-1000
劇本C（區間震盪）：Put wall～Call wall 之間，中軸為區間內參考中點
```

### 時段操作指引

```
08:45-09:30：觀察開盤缺口，不追第一根
09:30-11:30：主力時段，反彈到中軸量縮是空方進場點
13:00-13:45：注意外資尾盤方向，觀察是否大量加倉
```

### SentimentScore（-10 到 +10）

| 分項 | 範圍 | 說明 |
|------|------|------|
| S1 外資期貨方向 | -3~+3 | 淨部位 + 3日趨勢 |
| S2 今日動作品質 | -1~+1 | 加多減空 vs 加空減多 |
| S3 現貨同向確認 | -2~+2 | 現期方向一致加分 |
| S4 OI結構 | -2~+2 | price_position_pct + max_pain |
| S5 波動率風險 | -1~0 | ATR 異常放大扣分 |
| S6 大額交易人 | -1~+1 | Backer+，無資料跳過 |

門檻：+6以上強多，+3~+5偏多，-2~+2中性，-3~-5偏空，-6以下強空

### AI 指令格式（claude_advisor.py 目標輸出）

```
【指令】做空
【進場條件】下一根 5 分 K 收在 41680 之下
【目標】41500（Put wall 附近）
【停損】41750（進場後 70 點）
【信心分】4/5
【根據】外資淨空 -50963 口 / 現貨賣超 104 億 / Call wall 42500 壓制
【注意】接近結算，建議縮半倉
```

信心分門檻：做多 < 4 / 做空 < 3 → 不發指令，只發觀察提示。
觀望條件：在 Put wall ～ Call wall 區間無明確訊號、外資兩面建倉、結算日前3天。

---

## 已知 Bug（待修）

### Bug 1：R1/S1 每30秒被覆蓋（最高優先）

**位置**：`strategy_engine.py` → `calculate_tactical_levels()`  
**問題**：用 `flip ± ATR` 計算 R1/S1，寫入 `atos_state.json`，覆蓋 `data_engine.py` 計算的真實 Pivot R1/S1  
**影響**：盤中 R1_TOUCH / S1_TOUCH 警報點位完全錯誤（差距 900+ 點）  
**修法**：`monitor_engine.py` 的 `update_state_from_snapshot()` 跳過 r1/s1 的覆寫，保留 preopen 寫入的真實值

### Bug 2：evening_report OI 換行

**位置**：`evening_report_engine.py` → `build_option_oi_text()`  
**問題**：`return "\\n".join(lines)` 用了 `\\n`（字面反斜線+n），Telegram 收到是連在一起的一行  
**修法**：改為 `return "\n".join(lines)`

### Bug 3：chip_data_engine 大額交易人欄位解析

**位置**：`chip_data_engine.py` → `fetch_large_traders()`  
**問題**：Top5 數值全部是 0，需要確認 `TaiwanFuturesOpenInterestLargeTraders` 的實際 name 欄位值  
**待確認**：在真實環境跑後貼出 `name 值: [...]` 的輸出

---

## 當前開發任務清單

按優先順序：

### 步驟2：修 data_engine.py（下一步）

- [ ] 修 Bug 1：`monitor_engine.py` 的 `update_state_from_snapshot`，不覆寫 r1/s1
- [ ] 在 `preopen_report_engine.py` 移除 Flip 相關邏輯
- [ ] 在 `preopen_report_engine.py` 加入 `mid_range`（`(H+L)/2`）作為新中軸
- [ ] 讓 `atos_state.json` 在盤前更新時寫入 `call_wall`, `put_wall`, `mid_range`, `sentiment_score`

### 步驟3：建 ai_report_engine.py

新建檔案，接收 `build_chip_context()` 輸出，用 Claude API 產生報告。

報告類型：
- `PREOPEN_FUTURES`：早盤期貨報告（08:35）
- `PREOPEN_STOCKS`：早盤選股報告（08:40）
- `EVENING_FUTURES`：晚盤期貨報告（15:05）
- `EVENING_STOCKS`：晚盤選股報告（15:10，待建）
- `INTRADAY_EVENT`：重大事件緊急報告（即時）

Claude API 呼叫方式（已在 artifacts 測試過）：
```python
import anthropic
client = anthropic.Anthropic()  # 自動讀取 ANTHROPIC_API_KEY
message = client.messages.create(
    model="claude-opus-4-5",
    max_tokens=1024,
    messages=[{"role": "user", "content": prompt}]
)
```

### 步驟4：建 claude_advisor.py

盤中警報觸發後呼叫 Claude API，輸出結構化指令。

輸入：`alert_context`（當前警報事件 + chip_context + state）  
輸出：指令格式（見上方「AI 指令格式」）

### 步驟5：建 news_engine.py

監控重大事件，來源：
- FinMind `TaiwanStockNews`（個股新聞）
- Yahoo Finance RSS（大盤相關）
- 定時輪詢，發現重大關鍵字時觸發緊急報告

### 步驟6：更新 main_commander.py

新增排程：
- `15:10` 晚盤選股報告
- `每5分鐘` news_engine 輪詢

---

## 開發規範

1. **不動現有邏輯**：修改時只做最小範圍改動，不重構沒問題的部分
2. **向後相容**：新欄位加在現有欄位後面，不刪舊欄位
3. **safe_execute 裝飾器**：所有對外 API 呼叫的函式都加 `@safe_execute`
4. **失敗不影響主流程**：任何一個資料集取得失敗，用空值繼續，不中斷
5. **Telegram 純文字**：不用 Markdown，避免解析問題

---

## 快速測試指令

```bash
# 測試籌碼引擎
python3 chip_data_engine.py

# 測試盤前報告
python3 -c "from preopen_report_engine import build_preopen_sip_message; print(build_preopen_sip_message())"

# 測試晚盤報告
python3 -c "from evening_report_engine import build_evening_report_message; print(build_evening_report_message())"

# 啟動主系統
python3 main_commander.py
```
