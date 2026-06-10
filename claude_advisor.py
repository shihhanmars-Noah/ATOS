# claude_advisor.py

import json
import math
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
from strategy_filter_engine import _get_current_session

GEMINI_MODEL = "gemini-2.5-flash"
CONFIDENCE_THRESHOLD = 3


def _is_valid_level(price: float, level, pct: float = 0.05) -> bool:
    """點位有效性驗證：與現價差距超過 pct 比例則視為過期。"""
    if level is None:
        return False
    try:
        lv = float(level)
        pv = float(price)
        if pv <= 0 or lv <= 0:
            return False
        return abs(lv - pv) / pv <= pct
    except Exception:
        return False
COOLDOWN_SECONDS = 600  # 10 分鐘，同一事件同一方向不重複發指令
GEMINI_RETRY_DELAY = 5  # 429 重試等待秒數

_EVENT_LABELS = {
    "LONG_TRAP":              "多方陷阱（假突破）",
    "SHORT_TRAP":             "空方陷阱（軋空）",
    "FLIP_BREAK":             "跌破中軸",
    "FLIP_RECOVER":           "站回中軸",
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
    """價格格式化：強制整數，台指期不顯示小數。"""
    if v is None:
        return "N/A"
    try:
        return str(int(round(float(v))))
    except Exception:
        return "N/A"


def _expired_p(v, expired_keys: set, key: str) -> str:
    """格式化點位，若在過期集合中則標注（過期）。"""
    if key in expired_keys:
        return "N/A（點位過期）"
    return _p(v)


def _build_alert_text(ctx: dict) -> str:
    event = str(ctx.get("event", "UNKNOWN")).upper()
    label = _EVENT_LABELS.get(event, event)
    flip_val = ctx.get("flip") or ctx.get("mid_range")
    _expired = ctx.get("_expired_levels", set())
    lines = [
        f"警報事件：{label}（{event}）",
        f"當前價格：{_p(ctx.get('price'))}",
        f"中軸/mid_range：{_expired_p(flip_val, _expired, 'flip')}",
        f"Pivot：{_expired_p(ctx.get('pivot'), _expired, 'pivot')}",
        f"R1：{_expired_p(ctx.get('r1'), _expired, 'r1')} / S1：{_expired_p(ctx.get('s1'), _expired, 's1')}",
        f"法人情緒：{ctx.get('sentiment', 'N/A')}",
        f"行為型態：{ctx.get('behavior', 'N/A')}",
    ]
    # 若所有關鍵點位均過期，加入警示
    if {'flip', 'pivot', 'r1', 's1'}.issubset(_expired):
        lines.append("⚠️ 所有技術點位已過期，請以當前價格 ± ATR 自行判斷支撐壓力")
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


def _calculate_targets(
    direction: str,
    current_price: float,
    pivot,
    put_wall,
    call_wall,
    s1,
    r1,
):
    """
    計算目標價，確保目標方向正確。
    空方目標必須低於現價；多方目標必須高於現價。
    回傳 (tp1, tp2)，均為 float。
    """
    try:
        cp = float(current_price)
        if direction == 'SHORT':
            candidates = []
            if put_wall is not None:
                v = float(put_wall)
                if v < cp:
                    candidates.append(v)
            if s1 is not None:
                v = float(s1)
                if v < cp:
                    candidates.append(v)
            if pivot is not None:
                v = float(pivot)
                if v < cp:
                    candidates.append(v)
            candidates = sorted(set(candidates), reverse=True)   # 近→遠
            if len(candidates) >= 2:
                return candidates[0], candidates[1]
            elif len(candidates) == 1:
                return candidates[0], candidates[0] - 500
            else:
                return cp - 300, cp - 600
        else:  # LONG
            candidates = []
            if call_wall is not None:
                v = float(call_wall)
                if v > cp:
                    candidates.append(v)
            if r1 is not None:
                v = float(r1)
                if v > cp:
                    candidates.append(v)
            if pivot is not None:
                v = float(pivot)
                if v > cp:
                    candidates.append(v)
            candidates = sorted(set(candidates))                  # 近→遠
            if len(candidates) >= 2:
                return candidates[0], candidates[1]
            elif len(candidates) == 1:
                return candidates[0], candidates[0] + 500
            else:
                return cp + 300, cp + 600
    except Exception:
        if direction == 'SHORT':
            return float(current_price) - 300, float(current_price) - 600
        else:
            return float(current_price) + 300, float(current_price) + 600


def _calculate_stop_loss_points(atr_5d: float, session: str) -> int:
    """
    依 ATR 和時段動態計算停損點數。

    高波動（ATR > 1000）：0.07 × ATR（約 70-100 點）
    一般波動（ATR 500-1000）：0.10 × ATR（約 50-100 點）
    低波動（ATR < 500）：0.12 × ATR，最少 50 點
    夜盤（NIGHT / NIGHT_LATE）：流動性差，停損再放大 20%
    上限 200 點，避免異常放大
    """
    if not atr_5d or atr_5d <= 0:
        base_sl = 100
    elif atr_5d > 1000:
        base_sl = int(atr_5d * 0.07)
    elif atr_5d > 500:
        base_sl = int(atr_5d * 0.10)
    else:
        base_sl = max(50, int(atr_5d * 0.12))

    if session in ('NIGHT', 'NIGHT_LATE'):
        base_sl = int(base_sl * 1.2)

    base_sl = min(base_sl, 200)
    try:
        print(f"[stop_loss] {base_sl}pt (ATR={atr_5d:.0f}, session={session})")
    except Exception:
        pass
    return base_sl


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


def _get_price_direction(
    price: float,
    pivot,
    put_wall,
    call_wall,
) -> str:
    """
    根據現價和關鍵價位動態判斷方向感。
    不依賴舊的訊號方向，只看現價相對大戶框架的位置。
    """
    try:
        price = float(price)

        # 已跌破 Put wall：強烈偏空
        if put_wall and price < float(put_wall):
            return 'STRONG_BEAR'

        # 已突破 Call wall：強烈偏多
        if call_wall and price > float(call_wall):
            return 'STRONG_BULL'

        # 在 Pivot 下方：偏空
        if pivot and price < float(pivot):
            # 距 Put wall < 5%：非常接近大戶防線
            if put_wall:
                distance_pct = (price - float(put_wall)) / float(put_wall)
                if distance_pct < 0.05:
                    return 'STRONG_BEAR'
            return 'BEAR'

        # 在 Pivot 上方：偏多
        if pivot and price > float(pivot):
            # 距 Call wall < 5%：非常接近大戶壓線
            if call_wall:
                distance_pct = (float(call_wall) - price) / float(call_wall)
                if distance_pct < 0.05:
                    return 'STRONG_BULL'
            return 'BULL'

        return 'NEUTRAL'

    except Exception:
        return 'NEUTRAL'


def _get_nearest_meaningful_level(
    current_price: float,
    direction: str,
    pivot,
    put_wall,
    call_wall,
    s1,
    r1,
    atr_5d: float = 1000,
) -> dict:
    """
    根據現價和方向，找出最近且有意義的觀察點位。
    過濾掉離現價超過 1.5×ATR 的點位（無實戰意義）。
    """
    max_distance = atr_5d * 1.5

    result = {
        'observe_level': None,
        'observe_text': '',
        'key_defense': '',
    }

    try:
        if direction in ('STRONG_BEAR', 'BEAR'):
            # 偏空：找下方最近的支撐
            candidates = []

            if put_wall:
                diff = current_price - float(put_wall)
                if diff <= 0:
                    # 已跌破 Put wall
                    result['observe_text'] = (
                        f"Put wall {int(put_wall)} 已跌破（{abs(diff):.0f} 點），"
                        f"觀察是否快速彈回（假跌破訊號）"
                    )
                    result['key_defense'] = (
                        f"下方支撐：S1 {int(s1) if s1 else 'N/A'}"
                    )
                    return result
                elif diff < max_distance:
                    candidates.append((diff, float(put_wall), f"Put wall {int(put_wall)}（大戶防線）"))

            if s1:
                diff = current_price - float(s1)
                if 0 < diff < max_distance:
                    candidates.append((diff, float(s1), f"S1 {int(s1)}"))

            if pivot:
                diff = current_price - float(pivot)
                if 0 < diff < max_distance:
                    candidates.append((diff, float(pivot), f"Pivot {int(pivot)}"))

            if candidates:
                candidates.sort(key=lambda x: x[0])  # 最近的優先
                nearest = candidates[0]
                result['observe_level'] = nearest[1]
                result['observe_text'] = f"5分K 在 {nearest[2]} 附近是否止跌反彈"
                result['key_defense'] = f"下方防線：{nearest[2]}"
            else:
                result['observe_text'] = "所有已知支撐均超出有效範圍，等待新框架建立"

        elif direction in ('STRONG_BULL', 'BULL'):
            # 偏多：找上方最近的壓力
            candidates = []

            if call_wall:
                diff = float(call_wall) - current_price
                if 0 < diff < max_distance:
                    candidates.append((diff, float(call_wall), f"Call wall {int(call_wall)}（大戶壓線）"))

            if r1:
                diff = float(r1) - current_price
                if 0 < diff < max_distance:
                    candidates.append((diff, float(r1), f"R1 {int(r1)}"))

            if pivot:
                diff = float(pivot) - current_price
                if 0 < diff < max_distance:
                    candidates.append((diff, float(pivot), f"Pivot {int(pivot)}"))

            if candidates:
                candidates.sort(key=lambda x: x[0])
                nearest = candidates[0]
                result['observe_level'] = nearest[1]
                result['observe_text'] = f"5分K 能否站穩 {nearest[2]} 之上"
                result['key_defense'] = f"上方壓力：{nearest[2]}"
            else:
                result['observe_text'] = "所有已知壓力均超出有效範圍，等待新框架建立"

        else:
            result['observe_text'] = "在 Pivot 附近震盪，觀察方向確認"

    except Exception as e:
        try:
            print(f"[observe] _get_nearest_meaningful_level failed: {e}")
        except Exception:
            pass
        result['observe_text'] = "點位計算失敗，等待確認"

    return result


def _build_observe_message(
    current_price: float,
    direction: str,
    confidence: int,
    key_levels: dict,
    session_name: str,
    chip_ctx: dict = None,
) -> str:
    """
    觀察模式訊息（OBSERVE 時段）。

    方向感由現價相對大戶框架動態判斷，不沿用舊訊號方向。
    觀察點位過濾超過 1.5×ATR 的無效點位。

    Args:
        current_price: 當前價格
        direction:     原始訊號方向（已不用於判斷，改由 _get_price_direction 覆蓋）
        confidence:    調整後信心分
        key_levels:    dict with pivot / r1 / s1 / call_wall / put_wall
        session_name:  時段名稱（DAY_OPEN / DAY_CLOSE / NIGHT / NIGHT_LATE）
        chip_ctx:      chip context（取 atr_5d）
    """
    _session_labels = {
        'DAY_OPEN':   '開盤冷靜期',
        'DAY_CLOSE':  '尾盤',
        'DAY_MAIN':   '主力時段（信心不足）',
        'NIGHT':      '夜盤',
        'NIGHT_LATE': '深夜盤',
    }
    label = _session_labels.get(session_name, '非主力時段')

    pivot     = key_levels.get('pivot')
    r1        = key_levels.get('r1')
    s1        = key_levels.get('s1')
    call_wall = key_levels.get('call_wall')
    put_wall  = key_levels.get('put_wall')

    # 動態判斷方向感（忽略傳入的 direction）
    actual_direction = _get_price_direction(current_price, pivot, put_wall, call_wall)

    _dir_label = {
        'STRONG_BEAR': '強烈偏空',
        'BEAR':        '偏空',
        'NEUTRAL':     '中性',
        'BULL':        '偏多',
        'STRONG_BULL': '強烈偏多',
    }
    direction_zh = _dir_label.get(actual_direction, '中性')

    # ATR 參考（用於過濾遠距離點位）
    atr_5d = 1000.0
    if chip_ctx:
        try:
            atr_5d = float(chip_ctx.get('atr_5d') or 1000)
        except Exception:
            pass

    # 最近有效觀察點位
    level_info = _get_nearest_meaningful_level(
        current_price=current_price,
        direction=actual_direction,
        pivot=pivot,
        put_wall=put_wall,
        call_wall=call_wall,
        s1=s1,
        r1=r1,
        atr_5d=atr_5d,
    )

    lines = [
        f"👁️ 觀察提示（{label}）",
        f"現價：{int(round(float(current_price)))}｜方向感：{direction_zh}｜信心：{confidence}/5",
    ]

    if level_info.get('key_defense'):
        lines.append(level_info['key_defense'])

    if level_info.get('observe_text'):
        lines.append(f"觀察：{level_info['observe_text']}")

    # 夜盤流動性警示
    if session_name in ('NIGHT', 'NIGHT_LATE'):
        lines.append("⚠️ 夜盤流動性不足，避免追單，等關鍵價確認後再行動")

    lines.append(f"（{label}不發進場指令）")

    return "\n".join(lines)


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

    # ── 時段檢查（SILENT = 完全靜默；OBSERVE = 僅觀察提示，不呼叫 AI）──
    _session = _get_current_session()
    if _session['mode'] == 'SILENT':
        try:
            print(f"[claude_advisor] SILENT session ({_session['name']}), skipping")
        except Exception:
            pass
        return None

    if _session['mode'] == 'OBSERVE':
        # chip_ctx 在 OBSERVE 分支提早 return，需在這裡確保已載入（ATR 過濾需要）
        if chip_ctx is None:
            chip_ctx = build_chip_context()

        # 從事件名稱推斷方向（不呼叫 Gemini，節省 API 用量）
        _obs_event = str(alert_context.get("event", "")).upper()
        _event_direction_map = {
            'LONG_CONFIRM_V3':    '做多',
            'BULLISH_SWEEP':      '做多',
            'FLIP_RECOVER':       '做多',
            'SHORT_TRAP':         '做多',   # 軋空，多方機會
            'SHORT_RETEST_FAIL_V3': '做空',
            'BEARISH_SWEEP':      '做空',
            'FLIP_BREAK':         '做空',
            'FLIP_INVALID':       '做空',
            'LONG_TRAP':          '做空',   # 假突破，空方機會
        }
        _inferred_dir = _event_direction_map.get(_obs_event, '觀察')
        _obs_price = alert_context.get("price") or 0

        # 嘗試從 chip_cache 取點位
        _obs_levels: dict = {}
        try:
            with open('chip_cache.json', encoding='utf-8') as _of:
                _occ = json.load(_of)
            _ot = _occ.get('tech_levels', {})
            _ooi = _occ.get('option_oi', {})
            _obs_levels = {
                'pivot':     _ot.get('pivot'),
                'r1':        _ot.get('r1'),
                's1':        _ot.get('s1'),
                'call_wall': _ooi.get('call_wall_strike'),
                'put_wall':  _ooi.get('put_wall_strike'),
            }
        except Exception:
            pass

        # 補入 alert_context 中已有的點位（chip_cache 讀取失敗時的保底）
        for _lk in ('pivot', 'r1', 's1', 'call_wall', 'put_wall'):
            if not _obs_levels.get(_lk):
                _obs_levels[_lk] = alert_context.get(_lk)

        try:
            print(f"[claude_advisor] OBSERVE session ({_session['name']}), event={_obs_event} dir={_inferred_dir}")
        except Exception:
            pass

        return _build_observe_message(
            current_price=float(_obs_price) if _obs_price else 0.0,
            direction=_inferred_dir,
            confidence=0,          # 未呼叫 AI，信心分不適用
            key_levels=_obs_levels,
            session_name=_session['name'],
            chip_ctx=chip_ctx,
        )

    event = str(alert_context.get("event", "")).upper()

    # Put wall 假跌破：直接回傳警告，不進入 AI 流程
    false_breakout_warning = _check_put_wall_false_breakout(alert_context)
    if false_breakout_warning:
        print(f"⚠️ [claude_advisor] Put wall 假跌破偵測，略過 AI 指令")
        return false_breakout_warning

    if chip_ctx is None:
        chip_ctx = build_chip_context()

    # 關鍵價位補救：若 state 傳來的 pivot/r1/s1 是 None 或 nan，從 chip_cache 補回
    def _is_missing(v):
        if v is None:
            return True
        try:
            return math.isnan(float(v))
        except Exception:
            return False

    if any(_is_missing(alert_context.get(k)) for k in ("pivot", "r1", "s1", "mid_range")):
        try:
            with open('chip_cache.json', encoding='utf-8') as _f:
                _cc = json.load(_f)
            _tech = _cc.get('tech_levels', {})
            alert_context = dict(alert_context)
            for _key, _cache_key in (('pivot', 'pivot'), ('r1', 'r1'), ('s1', 's1'), ('mid_range', 'mid_range')):
                if _is_missing(alert_context.get(_key)):
                    alert_context[_key] = _tech.get(_cache_key)
        except Exception:
            pass

    # ── 點位有效性驗證：與現價差距 >5% 視為過期資料 ──
    _expired_levels: set = set()
    _ctx_price = alert_context.get("price")
    if _ctx_price:
        try:
            _pv = float(_ctx_price)
            alert_context = dict(alert_context)
            for _lk in ('flip', 'mid_range', 'pivot', 'r1', 's1'):
                if not _is_valid_level(_pv, alert_context.get(_lk)):
                    _expired_levels.add(_lk)
                    alert_context[_lk] = None
        except Exception:
            pass
    alert_context['_expired_levels'] = _expired_levels

    # ── OI 框架有效性：超出 Call/Put wall 500點外則標記 ──
    _oi_framework_valid = True
    if _ctx_price and chip_ctx:
        try:
            _cw = chip_ctx.get('call_wall')
            _pw = chip_ctx.get('put_wall')
            if _cw and _pw:
                _pv = float(_ctx_price)
                if _pv > float(_cw) + 500 or _pv < float(_pw) - 500:
                    _oi_framework_valid = False
        except Exception:
            pass

    # ── 若 pivot/r1/s1 全部過期，從 chip_cache 重算替代框架 ──
    if {'pivot', 'r1', 's1'}.issubset(_expired_levels):
        try:
            with open('chip_cache.json', encoding='utf-8') as _f:
                _cc = json.load(_f)
            _tech = _cc.get('tech_levels', {})
            _ph = _tech.get('prev_high')
            _pl = _tech.get('prev_low')
            _pc = _tech.get('prev_close')
            if _ph and _pl and _pc:
                _npivot = round((_ph + _pl + _pc) / 3, 0)
                _nr1    = round(2 * _npivot - float(_pl), 0)
                _ns1    = round(2 * _npivot - float(_ph), 0)
                alert_context['pivot']    = _npivot
                alert_context['r1']       = _nr1
                alert_context['s1']       = _ns1
                alert_context['flip']     = _npivot
                alert_context['mid_range'] = _npivot
                # 從過期集合移除（現在有有效替代值了）
                _expired_levels.discard('pivot')
                _expired_levels.discard('r1')
                _expired_levels.discard('s1')
                _expired_levels.discard('flip')
                _expired_levels.discard('mid_range')
                try:
                    print(f"[claude_advisor] All levels expired; rebuilt from chip_cache H/L/C: pivot={_npivot}")
                except Exception:
                    pass
        except Exception:
            pass

    # ── S1/R1 方向性驗證：若 S1 > 現價 或 R1 < 現價，改用 Put/Call wall 替代 ──
    _cp_v = alert_context.get("price")
    if _cp_v and chip_ctx:
        try:
            _pf_v = float(_cp_v)
            _pw_v = chip_ctx.get("put_wall")
            _cw_v = chip_ctx.get("call_wall")
            _s1_v = alert_context.get("s1")
            _r1_v = alert_context.get("r1")
            if _s1_v is not None:
                try:
                    if float(_s1_v) > _pf_v * 0.99 and _pw_v:   # S1 不應高於現價
                        alert_context = dict(alert_context)
                        alert_context["s1"] = _pw_v
                        try:
                            print(f"[claude_advisor] s1={_s1_v} > price*0.99, fallback to put_wall={_pw_v}")
                        except Exception:
                            pass
                except Exception:
                    pass
            if _r1_v is not None:
                try:
                    if float(_r1_v) < _pf_v * 1.01 and _cw_v:   # R1 不應低於現價
                        alert_context = dict(alert_context)
                        alert_context["r1"] = _cw_v
                        try:
                            print(f"[claude_advisor] r1={_r1_v} < price*1.01, fallback to call_wall={_cw_v}")
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            pass

    # 結算日資訊（後續多處使用）
    days_to_settlement = _get_days_to_settlement()
    price = alert_context.get("price")
    put_wall = chip_ctx.get("put_wall") if chip_ctx else None

    # 滑價防禦：跌破 Put wall 太快（現價距 Put wall > 50點）→ 等反彈再進場
    broke_put_wall = (
        event in ("FLIP_BREAK", "SHORT_RETEST_FAIL_V3") or
        (put_wall and price and float(price) < float(put_wall))
    )
    if broke_put_wall and put_wall and price:
        try:
            distance_from_put_wall = abs(float(price) - float(put_wall))
            if distance_from_put_wall > 50:
                print(f"⚠️ [claude_advisor] 跌破Put wall過快（距離{distance_from_put_wall:.0f}點），等反彈")
                return (
                    f"跌破Put wall過快，現價距Put wall已{distance_from_put_wall:.0f}點，"
                    f"放棄追空，等反彈測試{_p(put_wall)}不破再進場"
                )
        except Exception:
            pass

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

    # ── 動態停損計算（ATR × 係數，夜盤放大 20%）──
    _atr_5d = float(chip_ctx.get('atr_5d') or 1000) if chip_ctx else 1000.0
    _sl_pts = _calculate_stop_loss_points(_atr_5d, _session['name'])
    _sl_nt  = _sl_pts * 200
    _sl_pct = round(_sl_pts / _atr_5d * 100, 0) if _atr_5d > 0 else 0
    _sl_note = (
        f"建議停損：{_sl_pts}點"
        f"（ATR={_atr_5d:.0f}點的{_sl_pct:.0f}%，"
        f"一口計=NT${_sl_nt:,}）"
    )
    if _atr_5d > 1000:
        _position_note = f"⚠️ 高波動環境（ATR={_atr_5d:.0f}），建議縮減部位（最多1口）"
    elif _atr_5d > 700:
        _position_note = f"注意波動（ATR={_atr_5d:.0f}），建議半倉（1-2口）"
    else:
        _position_note = ""

    now_dt = datetime.now()
    now = now_dt.strftime("%H:%M")
    alert_text = _build_alert_text(alert_context)
    chip_text = _build_chip_summary(chip_ctx) if chip_ctx else "籌碼資料不可用"

    user_prompt = (
        f"時間：{now}\n\n"
        f"=== 警報資訊 ===\n{alert_text}\n\n"
        f"=== 籌碼背景 ===\n{chip_text}\n\n"
        f"=== 條件預判 ===\n{conditions_text}\n\n"
        f"=== 動態停損參考 ===\n{_sl_note}\n\n"
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

    # ── 時段分流：OBSERVE → 觀察提示；FULL → 正常指令流程 ──
    _key_levels = {
        'pivot':     alert_context.get('pivot'),
        'r1':        alert_context.get('r1'),
        's1':        alert_context.get('s1'),
        'call_wall': chip_ctx.get('call_wall') if chip_ctx else None,
        'put_wall':  chip_ctx.get('put_wall') if chip_ctx else None,
    }

    # FULL 模式：信心分門檻檢查（OBSERVE 模式已在函式頂部提前返回）
    min_confidence = 4 if direction == "做多" else CONFIDENCE_THRESHOLD
    if adjusted_confidence < min_confidence:
        label = _EVENT_LABELS.get(event, event)
        adj_note = f"（原{confidence}，調整{confidence_adj:+d}）" if confidence_adj != 0 else ""
        try:
            print(f"[claude_advisor] {event} adjusted confidence {adjusted_confidence}/5{adj_note}, observe only")
        except Exception:
            pass
        return _build_observe_message(
            current_price=float(price) if price else 0.0,
            direction=direction,
            confidence=adjusted_confidence,
            key_levels=_key_levels,
            session_name='DAY_MAIN',  # FULL 時段但信心不足
            chip_ctx=chip_ctx,
        )

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
    if _position_note:
        text += f"\n{_position_note}"

    # --------------------------------------------------
    # 記錄指令到 trade_logger（只記錄有方向的指令）
    # --------------------------------------------------
    if direction in ("做多", "做空"):
        try:
            from trade_logger import log_signal

            # 解析 AI 輸出的目標/停損數字
            def _parse_price_field(pattern: str, fallback: float) -> float:
                m = re.search(pattern, text)
                if m:
                    try:
                        return float(re.sub(r"[,，]", "", m.group(1).strip()))
                    except Exception:
                        pass
                return fallback

            sl_parsed  = _parse_price_field(r"【停損】\s*([\d,，.]+)", 0.0)
            t1_parsed  = _parse_price_field(r"【目標】\s*([\d,，.]+)", 0.0)
            # 第二目標：格式「目標1 → 目標2」或「目標1（...）目標2」
            t2_match = re.search(r"【目標】.*?[\d,，.]+\s*[→→]\s*([\d,，.]+)", text)
            t2_parsed = float(re.sub(r"[,，]", "", t2_match.group(1))) if t2_match else t1_parsed

            direction_en = "LONG" if direction == "做多" else "SHORT"

            # 目標價方向驗證：若 AI 目標方向錯誤，用計算值替換
            _cp = float(price) if price else 0.0
            if _cp > 0 and t1_parsed > 0:
                _dir_wrong = (
                    (direction_en == 'SHORT' and t1_parsed >= _cp) or
                    (direction_en == 'LONG'  and t1_parsed <= _cp)
                )
                if _dir_wrong:
                    try:
                        print(f"[targets] AI target {t1_parsed:.0f} wrong direction vs price {_cp:.0f}, recalculating")
                    except Exception:
                        pass
                    t1_parsed, t2_parsed = _calculate_targets(
                        direction_en, _cp,
                        alert_context.get('pivot'), chip_ctx.get('put_wall') if chip_ctx else None,
                        chip_ctx.get('call_wall') if chip_ctx else None,
                        alert_context.get('s1'), alert_context.get('r1'),
                    )

            trade_id = log_signal(
                direction=direction_en,
                entry_condition=re.search(r"【進場條件】\s*(.+)", text).group(1).strip()
                    if re.search(r"【進場條件】\s*(.+)", text) else "",
                entry_price_zone=float(price) if price else 0.0,
                stop_loss=sl_parsed,
                target_1=t1_parsed,
                target_2=t2_parsed,
                confidence=adjusted_confidence,
                signal_type=_signal_type,
                chip_ctx=chip_ctx or {},
                days_to_settlement=days_to_settlement,
            )
            # 把 trade_id 回寫到輸出文字，方便對應查詢（不干擾指令本身）
            text += f"\n（訊號ID：{trade_id}）"

        except Exception as _le:
            print(f"⚠️ [claude_advisor] trade_logger 記錄失敗：{_le}")

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
