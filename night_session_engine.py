# night_session_engine.py

from datetime import datetime, time

from persistent_state import load_state, save_state

try:
    from data_engine import txf_tick_cache_to_dataframe
except Exception:
    txf_tick_cache_to_dataframe = None


def format_price(value):
    """
    價格格式化。
    """

    if value is None:
        return "N/A"

    try:
        return round(float(value), 1)
    except Exception:
        return value


def get_latest_txf_cache_price():
    """
    從 TXF snapshot cache 取得最近一筆價格。

    這不是即時抓 API，而是從 txf_tick_cache.json 讀取。
    """

    if txf_tick_cache_to_dataframe is None:
        return None

    df = txf_tick_cache_to_dataframe()

    if df is None or df.empty:
        return None

    df = df.sort_values("datetime")

    row = df.iloc[-1]

    return {
        "price": float(row["price"]),
        "datetime": row["datetime"],
        "source": "TXF_TICK_CACHE",
    }


def get_night_session_close_from_cache():
    """
    從 TXF cache 推估夜盤收盤價。

    邏輯：
    - 優先抓 04:55～05:05 區間最後一筆
    - 若沒有，抓最近一筆 00:00～05:05 的 TXF cache
    - 若仍沒有，回傳 None

    注意：
    - 這要求 ATOS 在夜盤期間有持續執行
    - 若電腦沒開，就只能 fallback 前一日收盤
    """

    if txf_tick_cache_to_dataframe is None:
        return None

    df = txf_tick_cache_to_dataframe()

    if df is None or df.empty:
        return None

    df = df.copy()
    df["datetime"] = df["datetime"].dt.tz_localize(None)

    now = datetime.now()
    today = now.date()

    df_today = df[df["datetime"].dt.date == today].copy()

    if df_today.empty:
        return None

    # 04:55～05:05 夜盤收盤附近
    close_window = df_today[
        (df_today["datetime"].dt.time >= time(4, 55))
        & (df_today["datetime"].dt.time <= time(5, 5))
    ].copy()

    if not close_window.empty:
        row = close_window.sort_values("datetime").iloc[-1]

        return {
            "night_close": float(row["price"]),
            "night_close_time": str(row["datetime"]),
            "night_close_source": "TXF_CACHE_04_55_05_05",
        }

    # 備援：抓 00:00～05:05 最後一筆
    night_window = df_today[
        (df_today["datetime"].dt.time >= time(0, 0))
        & (df_today["datetime"].dt.time <= time(5, 5))
    ].copy()

    if not night_window.empty:
        row = night_window.sort_values("datetime").iloc[-1]

        return {
            "night_close": float(row["price"]),
            "night_close_time": str(row["datetime"]),
            "night_close_source": "TXF_CACHE_00_00_05_05_FALLBACK",
        }

    return None


def update_night_close_state():
    """
    更新 atos_state.json 裡的 night_close。

    優先：
    - TXF cache 夜盤收盤
    備援：
    - 不動原本 state
    """

    state = load_state()

    result = get_night_session_close_from_cache()

    if not result:
        return {
            "updated": False,
            "reason": "找不到 TXF 夜盤 cache，night_close 不更新。",
            "state": state,
        }

    state["night_close"] = result["night_close"]
    state["night_close_time"] = result["night_close_time"]
    state["night_close_source"] = result["night_close_source"]

    save_state(state)

    return {
        "updated": True,
        "reason": "night_close 已更新。",
        "night_close": result["night_close"],
        "night_close_time": result["night_close_time"],
        "night_close_source": result["night_close_source"],
        "state": state,
    }


def build_night_context(
    flip=None,
    pivot=None,
    previous_futures_close=None,
):
    """
    建立盤前報告用夜盤背景。

    回傳：
    {
        "has_night_data": bool,
        "night_close": float | None,
        "night_close_time": str | None,
        "night_close_source": str,
        "gap_vs_prev_close": float | None,
        "position_vs_flip": str,
        "position_vs_pivot": str,
        "summary": str,
    }
    """

    state = load_state()

    night_close = state.get("night_close")
    night_close_time = state.get("night_close_time")
    night_close_source = state.get("night_close_source", "STATE")

    if night_close is None:
        # 嘗試即時從 cache 補一次
        update_result = update_night_close_state()

        if update_result.get("updated"):
            night_close = update_result.get("night_close")
            night_close_time = update_result.get("night_close_time")
            night_close_source = update_result.get("night_close_source")

    if night_close is None:
        return {
            "has_night_data": False,
            "night_close": None,
            "night_close_time": None,
            "night_close_source": "NO_DATA",
            "gap_vs_prev_close": None,
            "position_vs_flip": "無夜盤資料",
            "position_vs_pivot": "無夜盤資料",
            "summary": "目前沒有夜盤收盤資料，日盤開盤先觀察第一根 5分K，不預設跳空方向。",
        }

    night_close = float(night_close)

    gap_vs_prev_close = None

    if previous_futures_close:
        try:
            gap_vs_prev_close = round(night_close - float(previous_futures_close), 1)
        except Exception:
            gap_vs_prev_close = None

    position_vs_flip = "N/A"
    position_vs_pivot = "N/A"

    if flip:
        try:
            if night_close > float(flip):
                position_vs_flip = "夜盤收在中軸上方，日盤偏多方防守"
            elif night_close < float(flip):
                position_vs_flip = "夜盤收在中軸下方，日盤偏空方防守"
            else:
                position_vs_flip = "夜盤收在中軸附近，日盤容易洗盤"
        except Exception:
            pass

    if pivot:
        try:
            if night_close > float(pivot):
                position_vs_pivot = "夜盤收在 Pivot 上方，短線重心偏強"
            elif night_close < float(pivot):
                position_vs_pivot = "夜盤收在 Pivot 下方，短線重心偏弱"
            else:
                position_vs_pivot = "夜盤收在 Pivot 附近，短線重心中性"
        except Exception:
            pass

    if gap_vs_prev_close is None:
        summary = (
            f"夜盤收盤 {format_price(night_close)}，"
            "但缺少前一交易日收盤基準，暫不判斷跳空幅度。"
        )
    else:
        if gap_vs_prev_close >= 150:
            summary = (
                f"夜盤收盤較前日收盤高 {gap_vs_prev_close} 點，"
                "日盤可能偏開高，開盤不追高，先看能否守住 Flip。"
            )
        elif gap_vs_prev_close <= -150:
            summary = (
                f"夜盤收盤較前日收盤低 {abs(gap_vs_prev_close)} 點，"
                "日盤可能偏開低，開盤不追空，先看是否跌破後有延續。"
            )
        else:
            summary = (
                f"夜盤收盤與前日收盤差距 {gap_vs_prev_close} 點，"
                "屬一般區間，日盤仍以中軸 / Pivot 判斷。"
            )

    return {
        "has_night_data": True,
        "night_close": night_close,
        "night_close_time": night_close_time,
        "night_close_source": night_close_source,
        "gap_vs_prev_close": gap_vs_prev_close,
        "position_vs_flip": position_vs_flip,
        "position_vs_pivot": position_vs_pivot,
        "summary": summary,
    }


def build_night_context_text(
    flip=None,
    pivot=None,
    previous_futures_close=None,
):
    """
    產生盤前報告用夜盤文字。
    """

    context = build_night_context(
        flip=flip,
        pivot=pivot,
        previous_futures_close=previous_futures_close,
    )

    if not context["has_night_data"]:
        return (
            "夜盤資料：⚠️ 無夜盤收盤資料\n"
            "夜盤結論：日盤開盤先觀察第一根 5分K，不預設跳空方向。"
        )

    gap = context.get("gap_vs_prev_close")

    gap_text = "N/A" if gap is None else f"{gap} 點"

    return (
        f"夜盤收盤：{format_price(context['night_close'])}\n"
        f"夜盤時間：{context['night_close_time']}\n"
        f"資料來源：{context['night_close_source']}\n"
        f"相對前日收盤：{gap_text}\n"
        f"相對中軸：{context['position_vs_flip']}\n"
        f"相對 Pivot：{context['position_vs_pivot']}\n\n"
        f"夜盤結論：{context['summary']}"
    )


if __name__ == "__main__":
    result = update_night_close_state()
    print(result)

    print()
    print(build_night_context_text())