# target_engine.py

def calculate_trade_plan(state: dict) -> dict:
    """
    根據目前 Regime / Flip / ATR 產生：
    - 進場區
    - 停損
    - 停利 TP1 / TP2
    """

    regime = state.get("regime", "")
    flip = state.get("flip")
    atr = state.get("atr") or 100
    levels = state.get("levels", {}) or {}

    if not flip:
        return no_trade_plan("Flip 尚未建立，禁止交易")

    # 空方模式
    if "空方" in regime:
        entry_low = flip - 0.3 * atr
        entry_high = flip + 0.1 * atr
        stop_loss = flip + 0.5 * atr
        tp1 = levels.get("S1") or flip - atr
        tp2 = levels.get("S2") or flip - 1.5 * atr

        return {
            "mode": "SHORT",
            "entry_zone": f"{round(entry_low, 1)} ~ {round(entry_high, 1)}",
            "stop_loss": round(stop_loss, 1),
            "take_profit_1": round(tp1, 1),
            "take_profit_2": round(tp2, 1),
            "invalid_condition": "5分K收盤站回中軸上方",
            "desc": "空方模式：只做反彈放空，不抄底。",
        }

    # 多方模式
    if "多方" in regime:
        entry_low = flip - 0.1 * atr
        entry_high = flip + 0.3 * atr
        stop_loss = flip - 0.5 * atr
        tp1 = levels.get("R1") or flip + atr
        tp2 = levels.get("R2") or flip + 1.5 * atr

        return {
            "mode": "LONG",
            "entry_zone": f"{round(entry_low, 1)} ~ {round(entry_high, 1)}",
            "stop_loss": round(stop_loss, 1),
            "take_profit_1": round(tp1, 1),
            "take_profit_2": round(tp2, 1),
            "invalid_condition": "5分K收盤跌回 Flip 下方",
            "desc": "多方模式：只做拉回買進，不追高。",
        }

    return no_trade_plan("中性區間，沒有進場優勢")


def no_trade_plan(reason: str) -> dict:
    return {
        "mode": "NO_TRADE",
        "entry_zone": "不進場",
        "stop_loss": "無",
        "take_profit_1": "無",
        "take_profit_2": "無",
        "invalid_condition": reason,
        "desc": "目前不交易，等待下一個明確狀態。",
    }