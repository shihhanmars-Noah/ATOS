# claude_advisor.py

import os
import re
import time
from datetime import datetime
from typing import Optional

from google import genai

from error_handler import safe_execute
from chip_data_engine import build_chip_context

GEMINI_MODEL = "gemini-2.0-flash"
CONFIDENCE_THRESHOLD = 3
COOLDOWN_SECONDS = 600  # 10 分鐘，同一事件同一方向不重複發指令

_EVENT_LABELS = {
    "LONG_TRAP":              "多方陷阱（假突破）",
    "SHORT_TRAP":             "空方陷阱（軋空）",
    "FLIP_BREAK":             "跌破中軸 Flip",
    "FLIP_RECOVER":           "站回中軸 Flip",
    "FLIP_INVALID":           "中軸方向失效",
    "BEARISH_SWEEP":          "上方掃單",
    "BULLISH_SWEEP":          "下方掃單",
    "SWEEP":                  "掃單警報",
    "R1_TOUCH":               "接近壓力 R1",
    "S1_TOUCH":               "接近支撐 S1",
    "LONG_CONFIRM_V3":        "多方確認訊號",
    "SHORT_RETEST_FAIL_V3":   "空方回測失敗",
}

_SYSTEM = """你是 ATOS 盤中即時指令引擎。

當警報觸發時，根據事件類型、當前價位與籌碼背景，產生結構化操作指令。

規則：
1. 輸出嚴格使用下列格式，每行一個欄位，不增不減
2. 所有點位數字只能來自輸入提供的關鍵價位，不得自行發明
3. 【目標】必須引用輸入中已存在的價位（R1、S1、Call牆、Put牆、Pivot）
4. 【停損】動態決定，優先順序如下：
   a. 首選：前K棒的反方向極值（做多用「前K低點」，做空用「前K高點」）
   b. 無前K資料時：用「當前價格 ± ATR × 1.5」計算，ATR 取輸入中的 ATR 值
   c. 只填計算出的具體數字，不填說明文字
5. 信心分評分基準（1-5）：
   1 = 事件孤立，籌碼無共鳴
   2 = 弱共鳴，方向模糊
   3 = 事件有籌碼支撐，但條件不完整
   4 = 事件 + 籌碼 + 技術三者共振
   5 = 完全共振，多重確認
6. 信心分 < 3：【指令】填「觀察」，【進場條件】填「暫不進場」，【目標】和【停損】填「N/A」
7. 輸出純文字，不用任何 Markdown 符號

輸出格式（固定七行）：
【指令】做多 / 做空 / 觀察
【進場條件】（描述進場觸發條件，或「暫不進場」）
【目標】（填已存在的價位，或 N/A）
【停損】（填動態計算的具體點位，或 N/A）
【信心分】X/5
【根據】（支撐本指令的籌碼與技術依據，逗號分隔）
【注意】（風險提示，無則填「無」）"""


# --------------------------------------------------
# 冷卻快取（session 內有效）
# --------------------------------------------------

_cooldown_cache: dict[tuple, float] = {}


def _is_on_cooldown(event: str, direction: str) -> bool:
    return time.time() - _cooldown_cache.get((event, direction), 0) < COOLDOWN_SECONDS


def _set_cooldown(event: str, direction: str):
    _cooldown_cache[(event, direction)] = time.time()


def _cooldown_remaining(event: str, direction: str) -> int:
    elapsed = time.time() - _cooldown_cache.get((event, direction), 0)
    return max(0, int(COOLDOWN_SECONDS - elapsed))


# --------------------------------------------------
# 格式化工具
# --------------------------------------------------

def _p(v) -> str:
    """價格格式化，去掉多餘小數點。"""
    if v is None:
        return "N/A"
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(round(f, 1))
    except Exception:
        return "N/A"


def _build_alert_text(ctx: dict) -> str:
    event = str(ctx.get("event", "UNKNOWN")).upper()
    label = _EVENT_LABELS.get(event, event)
    lines = [
        f"警報事件：{label}（{event}）",
        f"當前價格：{_p(ctx.get('price'))}",
        f"中軸 Flip：{_p(ctx.get('flip'))}",
        f"Pivot：{_p(ctx.get('pivot'))}",
        f"R1：{_p(ctx.get('r1'))} / S1：{_p(ctx.get('s1'))}",
        f"法人情緒：{ctx.get('sentiment', 'N/A')}",
        f"行為型態：{ctx.get('behavior', 'N/A')}",
    ]
    if ctx.get("stop"):
        lines.append(f"規則引擎停損：{_p(ctx.get('stop'))}")
    if ctx.get("target"):
        lines.append(f"規則引擎目標：{_p(ctx.get('target'))}")
    if ctx.get("trap"):
        lines.append(f"陷阱型態：{ctx.get('trap')}")
    if ctx.get("sweep"):
        lines.append(f"掃單型態：{ctx.get('sweep')}")
    # 動態停損所需資料
    if ctx.get("atr"):
        lines.append(f"ATR（近5日）：{_p(ctx.get('atr'))}")
    if ctx.get("prev_candle_high"):
        lines.append(f"前K高點：{_p(ctx.get('prev_candle_high'))}")
    if ctx.get("prev_candle_low"):
        lines.append(f"前K低點：{_p(ctx.get('prev_candle_low'))}")
    return "\n".join(lines)


def _build_chip_summary(ctx: dict) -> str:
    fn = ctx.get("foreign_net") or 0
    spot = ctx.get("spot_foreign_net_buy_bn") or 0
    lt = ctx.get("lt_top5_net") or 0
    score = ctx.get("sentiment_score") or 0
    lines = [
        f"外資期貨：{fn:+,}口（{ctx.get('foreign_net_level', 'N/A')}）",
        f"外資現貨：{spot:+.1f}億",
        f"Call牆：{_p(ctx.get('call_wall'))} / Put牆：{_p(ctx.get('put_wall'))}",
        f"情緒評分：{score:+d}（{ctx.get('sentiment_bias', 'N/A')}）",
        f"大額交易人Top5淨：{lt:+,}口",
    ]
    fg = ctx.get("fear_greed_index")
    if fg is not None:
        lines.append(f"恐懼貪婪：{fg}（{ctx.get('fear_greed_emotion', 'N/A')}）")
    for w in ctx.get("warnings", []):
        lines.append(f"警示：{w}")
    return "\n".join(lines)


def _extract_confidence(text: str) -> int:
    m = re.search(r"【信心分】\s*(\d)", text)
    return int(m.group(1)) if m else 0


def _extract_direction(text: str) -> str:
    """從 AI 輸出中提取指令方向。"""
    m = re.search(r"【指令】\s*(.+)", text)
    if not m:
        return "觀察"
    raw = m.group(1).strip()
    if "做多" in raw:
        return "做多"
    if "做空" in raw:
        return "做空"
    return "觀察"


# --------------------------------------------------
# 主函式
# --------------------------------------------------

@safe_execute
def advise(alert_context: dict, chip_ctx: Optional[dict] = None) -> Optional[str]:
    """
    警報觸發後呼叫 Claude API，回傳結構化指令。

    Args:
        alert_context: monitor_engine.build_alert_context() 的輸出
                       可選欄位：atr, prev_candle_high, prev_candle_low（供動態停損用）
        chip_ctx:      build_chip_context() 的輸出；None 則自動載入

    Returns:
        指令文字；信心分 < 3 時回傳觀察提示；冷卻中或失敗回傳 None
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("⚠️ [claude_advisor] GEMINI_API_KEY 未設定")
        return None

    if not alert_context or not alert_context.get("event"):
        return None

    # 非即時資料不發指令
    if not alert_context.get("is_realtime", True):
        return None

    event = str(alert_context.get("event", "")).upper()

    if chip_ctx is None:
        chip_ctx = build_chip_context()

    now = datetime.now().strftime("%H:%M")
    alert_text = _build_alert_text(alert_context)
    chip_text = _build_chip_summary(chip_ctx) if chip_ctx else "籌碼資料不可用"

    user_prompt = (
        f"時間：{now}\n\n"
        f"=== 警報資訊 ===\n{alert_text}\n\n"
        f"=== 籌碼背景 ===\n{chip_text}\n\n"
        "請根據以上資訊，產生 ATOS 結構化指令。"
    )

    full_prompt = f"{_SYSTEM}\n\n{user_prompt}"

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=full_prompt,
    )

    text = response.text.strip()
    confidence = _extract_confidence(text)
    direction = _extract_direction(text)

    # 信心分不足：回傳簡短觀察提示，不發操作指令
    if confidence < CONFIDENCE_THRESHOLD:
        label = _EVENT_LABELS.get(event, event)
        print(f"⚪ [claude_advisor] {event} 信心分 {confidence}/5，發觀察提示")
        return f"觀察中，條件未成熟（{label}，信心分 {confidence}/5）"

    # 有方向指令才做冷卻檢查
    if direction in ("做多", "做空"):
        if _is_on_cooldown(event, direction):
            remaining = _cooldown_remaining(event, direction)
            print(f"⏳ [claude_advisor] {event} {direction} 冷卻中（剩餘 {remaining}s），略過")
            return None
        _set_cooldown(event, direction)

    print(f"✅ [claude_advisor] {event} 指令完成（信心分：{confidence}/5，方向：{direction}）")
    return text


# 向後相容別名
get_intraday_advice = advise


# --------------------------------------------------
# 手動測試
# --------------------------------------------------

if __name__ == "__main__":
    chip_ctx = build_chip_context()

    test_context = {
        "event": "SHORT_TRAP",
        "price": 41200,
        "flip": 40511,
        "pivot": 40727,
        "r1": 41007,
        "s1": 40232,
        "sentiment": "極強空",
        "behavior": "SHORT_TRAP",
        "trap": "SHORT_TRAP",
        "sweep": None,
        "is_realtime": True,
        "stop": 41350,
        "target": 40800,
        "atr": 420,
        "prev_candle_high": 41250,
        "prev_candle_low": 41150,
    }

    print("=== claude_advisor 測試 ===\n")
    print("警報：SHORT_TRAP @ 41200\n")
    result = advise(test_context, chip_ctx)
    if result:
        print(result)
    else:
        print("⚠️ 指令產生失敗")
