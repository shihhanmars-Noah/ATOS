# claude_advisor.py

import os
import re
import time
from datetime import datetime
from typing import Optional

from google import genai
from google.genai import types

from error_handler import safe_execute
from chip_data_engine import build_chip_context
from risk_adjustment import apply_big_player_adjustments

GEMINI_MODEL = "gemini-2.5-flash"
CONFIDENCE_THRESHOLD = 3
COOLDOWN_SECONDS = 600  # 10 分鐘，同一事件同一方向不重複發指令
GEMINI_RETRY_DELAY = 5  # 429 重試等待秒數

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
3. 【目標】必須引用輸入中已存在的價位（Call牆、Put牆、Call牆+500、Put牆-500、Pivot）
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
6. 信心分門檻（做多標準較嚴）：
   做多：信心分 < 4 → 【指令】填「觀察」，【進場條件】填「暫不進場」，【目標】和【停損】填「N/A」
   做空：信心分 < 3 → 同上
7. 輸出純文字，不用任何 Markdown 符號
8. 進場條件前提（依方向不同）：
   做多：（三條件同時成立）①價格突破 Call wall 且5分K收盤確認；②當根量 > 前5K均量 × 1.2（量能確認）；③外資當日淨增多單 > 2,000 口且近3日趨勢由空轉多
   做空：（條件一）價格跌破 Put wall 且5分K收盤確認，且當根量 > 前5K均量 × 1.2；（條件二）或回抽中軸 ±50 點，當根量 < 前3K均量 × 0.7 且收黑K（量縮轉弱）
   觀望：價格在 Put wall ～ Call wall 之間無明確訊號，或外資兩面建倉，或結算日前3天；若出現 Put wall 假跌破（前K低於 Put wall，當K收回上方），立即取消空單

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
    flip_val = ctx.get("flip") or ctx.get("mid_range")
    lines = [
        f"警報事件：{label}（{event}）",
        f"當前價格：{_p(ctx.get('price'))}",
        f"中軸 Flip/mid_range：{_p(flip_val)}",
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
    if ctx.get("prev_candle_close"):
        lines.append(f"前K收盤：{_p(ctx.get('prev_candle_close'))}")
    if ctx.get("current_open"):
        lines.append(f"當K開盤：{_p(ctx.get('current_open'))}")
    if ctx.get("current_volume") is not None:
        lines.append(f"當根成交量：{int(ctx.get('current_volume')):,}")
    if ctx.get("vol_5bar_avg") is not None:
        lines.append(f"前5K均量：{int(ctx.get('vol_5bar_avg')):,}")
    if ctx.get("vol_3bar_avg") is not None:
        lines.append(f"前3K均量：{int(ctx.get('vol_3bar_avg')):,}")
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
# 輔助條件判斷函式
# --------------------------------------------------

def _check_volume_confirmed(ctx: dict) -> bool:
    """當根成交量 > 前5根K棒平均量的 1.2倍"""
    vol = ctx.get("current_volume")
    avg = ctx.get("vol_5bar_avg")
    if vol is None or avg is None or float(avg) == 0:
        return False
    return float(vol) > float(avg) * 1.2


def _check_mid_range_short_condition(ctx: dict) -> bool:
    """中軸附近量縮轉弱：價格在 mid_range ±50 點內，量 < 前3K均量 × 0.7，且收黑K"""
    mid = ctx.get("mid_range") or ctx.get("flip")
    price = ctx.get("price")
    vol = ctx.get("current_volume")
    avg3 = ctx.get("vol_3bar_avg")
    open_price = ctx.get("current_open")
    if not all([mid, price, vol, avg3, open_price]):
        return False
    in_zone = abs(float(price) - float(mid)) <= 50
    vol_shrink = float(vol) < float(avg3) * 0.7
    bearish_k = float(price) < float(open_price)
    return in_zone and vol_shrink and bearish_k


def _check_foreign_turning_bull(chip_ctx: dict) -> bool:
    """外資方向轉多：當日淨增多單 > 2000 口，且近3日趨勢轉多 (chg_3d > 0)"""
    chg_1d = chip_ctx.get("foreign_net_chg_1d") or 0
    chg_3d = chip_ctx.get("foreign_net_chg_3d") or 0
    return float(chg_1d) > 2000 and float(chg_3d) > 0


def _check_put_wall_false_breakout(ctx: dict) -> Optional[str]:
    """Put wall 假跌破：前K收在 Put wall 下方，當K收回上方 → 護盤反彈警告"""
    put_wall = ctx.get("put_wall")
    prev_close = ctx.get("prev_candle_close")
    price = ctx.get("price")
    if not all([put_wall, prev_close, price]):
        return None
    if float(prev_close) < float(put_wall) < float(price):
        return (
            f"⚠️ Put wall 假跌破護盤：前K收 {_p(prev_close)} < Put wall {_p(put_wall)}，"
            f"當K已收回 {_p(price)}，空方力道不足，建議暫停空單"
        )
    return None


def _get_session_penalty(now: datetime) -> int:
    """低流動性時段信心分懲罰（-1）"""
    t = now.hour * 60 + now.minute
    if 11 * 60 + 30 <= t <= 13 * 60:  # 午盤低流動性
        return -1
    if 15 * 60 <= t <= 16 * 60:        # 夜盤開盤初期
        return -1
    return 0


def _get_days_to_settlement() -> int:
    """取得距下一個結算日的天數，失敗時回傳 99。"""
    try:
        from settlement_engine import get_days_to_settlement
        return get_days_to_settlement()
    except Exception:
        return 99


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

    # Put wall 假跌破：直接回傳警告，不進入 AI 流程
    false_breakout_warning = _check_put_wall_false_breakout(alert_context)
    if false_breakout_warning:
        print(f"⚠️ [claude_advisor] Put wall 假跌破偵測，略過 AI 指令")
        return false_breakout_warning

    if chip_ctx is None:
        chip_ctx = build_chip_context()

    # 結算日資訊（後續多處使用）
    days_to_settlement = _get_days_to_settlement()
    price = alert_context.get("price")
    put_wall = chip_ctx.get("put_wall") if chip_ctx else None

    # Pre-check：結算前3天跌破 Put wall → 大戶護盤機率高，直接回傳多方機會提示
    if put_wall and price:
        try:
            if float(price) < float(put_wall) and days_to_settlement <= 3:
                pw = _p(put_wall)
                print(f"⚠️ [claude_advisor] 結算前{days_to_settlement}天跌破Put wall，回傳護盤提示")
                return (
                    f"⚠️ 結算前{days_to_settlement}天跌破Put wall {pw}，"
                    f"大戶護盤機率高，注意反彈做多機會，"
                    f"目標 {_p(float(put_wall) + 200)}，停損 {_p(float(put_wall) - 100)}"
                )
        except Exception:
            pass

    # 輔助條件預計算
    vol_confirmed = _check_volume_confirmed(alert_context)
    mid_range_short = _check_mid_range_short_condition(alert_context)
    foreign_turning_bull = _check_foreign_turning_bull(chip_ctx) if chip_ctx else False

    conditions_text = (
        f"量能確認（當根 > 前5K均量×1.2）：{'是' if vol_confirmed else '否'}\n"
        f"中軸量縮轉弱（±50點+量縮+黑K）：{'成立' if mid_range_short else '不成立'}\n"
        f"外資方向轉多（chg_1d>2000且chg_3d>0）：{'成立' if foreign_turning_bull else '不成立'}"
    )

    now_dt = datetime.now()
    now = now_dt.strftime("%H:%M")
    alert_text = _build_alert_text(alert_context)
    chip_text = _build_chip_summary(chip_ctx) if chip_ctx else "籌碼資料不可用"

    user_prompt = (
        f"時間：{now}\n\n"
        f"=== 警報資訊 ===\n{alert_text}\n\n"
        f"=== 籌碼背景 ===\n{chip_text}\n\n"
        f"=== 條件預判 ===\n{conditions_text}\n\n"
        "請根據以上資訊，產生 ATOS 結構化指令。"
    )

    full_prompt = f"{_SYSTEM}\n\n{user_prompt}"

    client = genai.Client(api_key=api_key)
    _cfg = types.GenerateContentConfig(
        max_output_tokens=600,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    text = None
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=full_prompt,
                config=_cfg,
            )
            text = response.text.strip()
            break
        except Exception as e:
            if "429" in str(e) or "ResourceExhausted" in type(e).__name__:
                if attempt < 2:
                    print(f"⏳ [claude_advisor] Gemini 429，{GEMINI_RETRY_DELAY}秒後重試（第{attempt + 1}次）")
                    time.sleep(GEMINI_RETRY_DELAY)
                else:
                    print("⏳ [claude_advisor] Gemini 429，重試2次仍失敗，略過")
                    return None
            else:
                raise
    if text is None:
        return None

    confidence = _extract_confidence(text)
    direction = _extract_direction(text)

    # --------------------------------------------------
    # 信心分後處理調整（via risk_adjustment）
    # --------------------------------------------------
    _signal_type = "LONG" if direction == "做多" else ("SHORT" if direction == "做空" else "NEUTRAL")
    _call_wall = chip_ctx.get("call_wall") if chip_ctx else None

    risk_adj = apply_big_player_adjustments(
        current_price=float(price) if price else 0.0,
        foreign_net=chip_ctx.get("foreign_net", 0) if chip_ctx else 0,
        foreign_net_chg_1d=chip_ctx.get("foreign_net_chg_1d", 0) if chip_ctx else 0,
        foreign_cost_estimate=chip_ctx.get("foreign_cost_estimate") if chip_ctx else None,
        top5_net=chip_ctx.get("lt_top5_net") or 0 if chip_ctx else 0,
        top5_net_chg=chip_ctx.get("lt_top5_net_chg") or 0 if chip_ctx else 0,
        put_wall=float(put_wall) if put_wall else 0.0,
        call_wall=float(_call_wall) if _call_wall else 0.0,
        days_to_settlement=days_to_settlement,
        fear_greed=chip_ctx.get("fear_greed_index") or 0 if chip_ctx else 0,
        sentiment_score=chip_ctx.get("sentiment_score") or 0 if chip_ctx else 0,
        signal_time=now_dt,
        signal_type=_signal_type,
        volume_confirmed=vol_confirmed,
    )

    confidence_adj = risk_adj["confidence_adjustment"]
    extra_notes: list[str] = risk_adj["warnings"]
    big_player_note = risk_adj["big_player_interpretation"]

    adjusted_confidence = confidence + confidence_adj

    # 信心分門檻：做多 >= 4，做空 >= 3
    min_confidence = 4 if direction == "做多" else CONFIDENCE_THRESHOLD
    if adjusted_confidence < min_confidence:
        label = _EVENT_LABELS.get(event, event)
        adj_note = f"（原{confidence}，調整{confidence_adj:+d}）" if confidence_adj != 0 else ""
        print(f"⚪ [claude_advisor] {event} 調整後信心分 {adjusted_confidence}/5{adj_note}，發觀察提示")
        return f"觀察中，條件未成熟（{label}，信心分 {adjusted_confidence}/5{adj_note}）"

    # 有方向指令才做冷卻檢查
    if direction in ("做多", "做空"):
        if _is_on_cooldown(event, direction):
            remaining = _cooldown_remaining(event, direction)
            print(f"⏳ [claude_advisor] {event} {direction} 冷卻中（剩餘 {remaining}s），略過")
            return None
        _set_cooldown(event, direction)

    # 附加後處理備註到輸出文字
    if extra_notes:
        text += "\n" + "\n".join(extra_notes)
    if big_player_note:
        text += f"\n【大戶動向】{big_player_note}"

    print(f"✅ [claude_advisor] {event} 指令完成（信心分：{adjusted_confidence}/5，方向：{direction}）")
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
