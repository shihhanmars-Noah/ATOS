# target_engine.py
#
# 根據 Put wall / Call wall / Active Pivot 產生進場計畫。
# ATR 只用於計算停損距離，目標點位以大戶牆為準。


def calculate_trade_plan(state: dict) -> dict:
    """
    根據當前 Regime / Active Pivot / Call wall / Put wall / ATR 產生進場計畫。

    state 欄位：
        flip        : active_pivot（多空中軸）
        call_wall   : Call wall 壓力位
        put_wall    : Put wall 支撐位
        atr         : 當日 ATR（用於停損距離）
        regime      : 🟢多方模式 / 🔴空方模式 / 🟡中性模式
    """
    regime = state.get("regime", "")
    pivot  = state.get("flip")       # active_pivot 寫入 flip 欄位
    atr    = state.get("atr") or 100
    cw     = state.get("call_wall")
    pw     = state.get("put_wall")

    if not pivot:
        return _no_trade("Active Pivot 尚未建立，禁止交易")

    # 停損距離：固定 100 點（一口 NT$20,000）
    STOP = 100

    # --------------------------------------------------
    # 空方模式：反彈到 Pivot 附近做空
    # --------------------------------------------------
    if "空方" in regime:
        entry_high = int(round(float(pivot)))
        entry_low  = int(round(float(pivot) - 0.2 * atr))
        stop_loss  = int(round(float(pivot) + STOP))

        # 目標：Put wall > 有效跌破門檻 > 動態 S1
        if pw:
            tp1 = int(round(float(pw)))
            tp2 = int(round(float(pw) - 500))
        else:
            tp1 = int(round(float(pivot) - atr))
            tp2 = int(round(float(pivot) - 1.5 * atr))

        return {
            "mode": "SHORT",
            "entry_zone": f"{entry_low} ~ {entry_high}",
            "stop_loss": stop_loss,
            "take_profit_1": tp1,
            "take_profit_2": tp2,
            "invalid_condition": f"5分K收盤站回 Pivot {entry_high} 上方",
            "desc": f"空方模式：反彈到 Pivot 附近做空，目標 Put wall {tp1}。",
        }

    # --------------------------------------------------
    # 多方模式：拉回到 Pivot 附近做多
    # --------------------------------------------------
    if "多方" in regime:
        entry_low  = int(round(float(pivot)))
        entry_high = int(round(float(pivot) + 0.2 * atr))
        stop_loss  = int(round(float(pivot) - STOP))

        # 目標：Call wall > 有效突破門檻 > 動態 R1
        if cw:
            tp1 = int(round(float(cw)))
            tp2 = int(round(float(cw) + 500))
        else:
            tp1 = int(round(float(pivot) + atr))
            tp2 = int(round(float(pivot) + 1.5 * atr))

        return {
            "mode": "LONG",
            "entry_zone": f"{entry_low} ~ {entry_high}",
            "stop_loss": stop_loss,
            "take_profit_1": tp1,
            "take_profit_2": tp2,
            "invalid_condition": f"5分K收盤跌破 Pivot {entry_low} 下方",
            "desc": f"多方模式：拉回到 Pivot 附近買進，目標 Call wall {tp1}。",
        }

    return _no_trade("中性區間（Put wall～Call wall），無進場優勢")


def _no_trade(reason: str) -> dict:
    return {
        "mode": "NO_TRADE",
        "entry_zone": "不進場",
        "stop_loss": "無",
        "take_profit_1": "無",
        "take_profit_2": "無",
        "invalid_condition": reason,
        "desc": "目前不交易，等待下一個明確訊號。",
    }


# 向後相容
def no_trade_plan(reason: str) -> dict:
    return _no_trade(reason)
