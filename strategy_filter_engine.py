# strategy_filter_engine.py

from datetime import datetime, time


STRATEGY_MODE = "DEFENSE"

GRADE_A = "A"
GRADE_B = "B"
GRADE_C = "C"

DEFAULT_STOP_POINTS = 30
DEFAULT_TARGET_POINTS = 80


def safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def format_price(value):
    try:
        return round(float(value), 1)
    except Exception:
        return "N/A"


def is_realtime_context(context: dict) -> bool:
    return bool(context.get("is_realtime", True))


def is_in_trade_window(context: dict) -> bool:
    """
    Defense Mode 先只允許 09:00–13:30 進入 A 級觀察。
    非此時段仍可提醒，但不給進場語氣。
    """

    raw_time = (
        context.get("tick_time")
        or context.get("latest_k_time")
        or context.get("datetime")
        or context.get("time")
    )

    if raw_time is None:
        return True

    try:
        if isinstance(raw_time, datetime):
            t = raw_time.time()
        else:
            raw_time = str(raw_time)

            if " " in raw_time:
                t = datetime.fromisoformat(raw_time).time()
            else:
                t = datetime.strptime(raw_time[:8], "%H:%M:%S").time()

        return time(9, 0) <= t <= time(13, 30)

    except Exception:
        return True


def is_neutral_zone(context: dict) -> bool:
    """
    中性區：
    價格位於 Flip 與 Pivot 之間，或同時接近 Flip / Pivot。
    """

    price = safe_float(context.get("price"))
    flip = safe_float(context.get("flip"))
    pivot = safe_float(context.get("pivot"))

    if price is None or flip is None or pivot is None:
        return False

    upper = max(flip, pivot)
    lower = min(flip, pivot)

    if lower <= price <= upper:
        return True

    # 同時靠近 Flip / Pivot，視為洗盤區
    if abs(price - flip) <= 30 and abs(price - pivot) <= 30:
        return True

    return False


def get_event_grade(event: str, context: dict) -> dict:
    """
    Defense Mode 分級邏輯。

    A：可觀察進場，但仍不是自動下單
    B：只提醒，不建議進場
    C：禁止交易
    """

    event = str(event or "UNKNOWN").upper()

    if event in ["BEARISH_SWEEP", "BULLISH_SWEEP"]:
        event = "SWEEP"

    reasons = []
    action = "WAIT"
    grade = GRADE_B

    # --------------------------------------------------
    # 硬性禁止條件
    # --------------------------------------------------
    if not is_realtime_context(context):
        return {
            "strategy_mode": STRATEGY_MODE,
            "signal_grade": GRADE_C,
            "allow_entry": False,
            "suggested_action": "NO_TRADE",
            "stop_points": DEFAULT_STOP_POINTS,
            "target_points": DEFAULT_TARGET_POINTS,
            "reasons": ["資料來源不是 realtime，禁止交易"],
        }

    if is_neutral_zone(context):
        return {
            "strategy_mode": STRATEGY_MODE,
            "signal_grade": GRADE_C,
            "allow_entry": False,
            "suggested_action": "NO_TRADE",
            "stop_points": DEFAULT_STOP_POINTS,
            "target_points": DEFAULT_TARGET_POINTS,
            "reasons": ["價格位於 Flip / Pivot 中性區，禁止交易"],
        }

    if not is_in_trade_window(context):
        return {
            "strategy_mode": STRATEGY_MODE,
            "signal_grade": GRADE_C,
            "allow_entry": False,
            "suggested_action": "NO_TRADE",
            "stop_points": DEFAULT_STOP_POINTS,
            "target_points": DEFAULT_TARGET_POINTS,
            "reasons": ["不在 Defense Mode 允許觀察時段 09:00–13:30"],
        }

    # --------------------------------------------------
    # 事件分級
    # --------------------------------------------------
    if event == "FLIP_BREAK":
        grade = GRADE_B
        action = "WATCH_ONLY"
        reasons.extend([
            "回測顯示跌破 Flip 直接追空期望值不佳",
            "此事件只代表結構轉弱，不等於可空",
            "等待反彈不過 Flip 或第二訊號確認",
        ])

    elif event == "FLIP_RECOVER":
        grade = GRADE_B
        action = "WATCH_ONLY"
        reasons.extend([
            "站回 Flip 代表多方結構改善",
            "但單一站回訊號尚未具備穩定優勢",
            "需等待 5 分 K 延續或回測不破",
        ])

    elif event == "R1_TOUCH":
        grade = GRADE_B
        action = "WATCH_ONLY"
        reasons.extend([
            "接近上方壓力不追多",
            "觀察是否假突破或突破後回測站穩",
        ])

    elif event == "S1_TOUCH":
        grade = GRADE_B
        action = "WATCH_ONLY"
        reasons.extend([
            "接近下方支撐不追空",
            "觀察是否假跌破或反彈不過後續弱",
        ])

    elif event == "FLIP_INVALID":
        grade = GRADE_C
        action = "NO_TRADE"
        reasons.extend([
            "原方向失效，先取消原策略",
            "等待新結構形成，不硬拗",
        ])

    elif event in ["LONG_TRAP", "SHORT_TRAP", "SWEEP"]:
        grade = GRADE_B
        action = "WATCH_ONLY"
        reasons.extend([
            "陷阱 / 掃單事件只做風險提醒",
            "等待下一根 5 分 K 收盤確認",
        ])

    elif event in ["LONG_CONFIRM_V3", "SHORT_RETEST_FAIL_V3"]:
        grade = "A-"
        action = "OBSERVE_ENTRY"
        reasons.extend([
            "符合 V3 觀察型條件",
            "目前僅允許微台小倉觀察，不可放大部位",
            "停損以 30 點為主，停利觀察 80–100 點",
        ])

    else:
        grade = GRADE_B
        action = "WATCH_ONLY"
        reasons.append("未知事件，僅提醒，不給進場建議")

    allow_entry = grade in ["A", "A-"]

    return {
        "strategy_mode": STRATEGY_MODE,
        "signal_grade": grade,
        "allow_entry": allow_entry,
        "suggested_action": action,
        "stop_points": DEFAULT_STOP_POINTS,
        "target_points": DEFAULT_TARGET_POINTS,
        "reasons": reasons,
    }


def enrich_context_with_strategy_filter(context: dict) -> dict:
    """
    將分級結果寫回 context，方便 alert_log / evening_report 後續讀取。
    """

    context = dict(context)
    event = context.get("event")

    result = get_event_grade(event, context)

    context["strategy_mode"] = result["strategy_mode"]
    context["signal_grade"] = result["signal_grade"]
    context["allow_entry"] = result["allow_entry"]
    context["suggested_action"] = result["suggested_action"]
    context["strategy_filter_reasons"] = result["reasons"]
    context["stop_points"] = result["stop_points"]
    context["target_points"] = result["target_points"]

    return context


def build_strategy_filter_section(context: dict) -> str:
    """
    建立 Telegram 警報中的策略分級區塊。
    """

    grade = context.get("signal_grade", "B")
    allow_entry = bool(context.get("allow_entry", False))
    action = context.get("suggested_action", "WAIT")
    mode = context.get("strategy_mode", STRATEGY_MODE)
    reasons = context.get("strategy_filter_reasons", [])

    if allow_entry:
        status = "可觀察進場，但不可自動下單"
    elif grade == "C":
        status = "禁止交易"
    else:
        status = "只提醒，不建議進場"

    reason_text = "\n".join([f"● {r}" for r in reasons]) if reasons else "● 無"

    return (
        "🛡️ ATOS Strategy Filter\n"
        "------------------\n"
        f"模式：{mode}\n"
        f"訊號等級：{grade}\n"
        f"允許進場：{'是' if allow_entry else '否'}\n"
        f"建議動作：{action}\n"
        f"狀態：{status}\n\n"
        "判斷原因：\n"
        f"{reason_text}\n\n"
        "風控設定：\n"
        f"● 預設停損：{context.get('stop_points', DEFAULT_STOP_POINTS)} 點\n"
        f"● 觀察停利：{context.get('target_points', DEFAULT_TARGET_POINTS)} 點\n\n"
        "最終指令：\n"
        f"> {status}"
    )


if __name__ == "__main__":
    test_context = {
        "event": "FLIP_BREAK",
        "price": 41780,
        "flip": 42359,
        "pivot": 42149.7,
        "r1": 42586.3,
        "s1": 41922.3,
        "is_realtime": True,
        "tick_time": "10:30:00",
    }

    enriched = enrich_context_with_strategy_filter(test_context)
    print(build_strategy_filter_section(enriched))