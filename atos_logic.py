# atos_logic.py

def analyze_atos_state(current_price: float, flip_level: float, atr: float = 55.0) -> dict:
    """
    動態狀態機：使用 ATR 決定中性區間 (No Trade Zone)
    """
    # 動態 Buffer 設定為 0.5 * ATR (最小不低於 30, 最大不高於 80)
    dynamic_buffer = max(30, min(80, atr * 0.5))
    diff = current_price - flip_level

    if diff > dynamic_buffer:
        return {
            "state": "🟢 多方模式",
            "action": "拉回買／順勢做多",
            "forbidden": "追空、逆勢放空、猜頭",
            "no_trade": False,
            "diff": round(diff, 1),
        }
    elif diff < -dynamic_buffer:
        return {
            "state": "🔴 空方模式",
            "action": "反彈空／禁止抄底",
            "forbidden": "抄底、逆勢做多",
            "no_trade": False,
            "diff": round(diff, 1),
        }
    else:
        return {
            "state": "🟡 中性模式",
            "action": "觀望／等待脫離緩衝區",
            "forbidden": "在 Flip 附近過度交易",
            "no_trade": True,
            "diff": round(diff, 1),
        }


def check_invalidation(
    five_min_close: float,
    flip_level: float,
    current_state: str
) -> dict:
    """
    5 分 K 收盤確認失效檢查。

    Args:
        five_min_close: 5分K收盤價
        flip_level: 多空分界點
        current_state: 當前狀態

    Returns:
        {
            "alert": bool,
            "message": str | None
        }
    """

    # -----------------------------------
    # 多方失效
    # -----------------------------------
    if current_state == "🟢 多方模式":

        if five_min_close < flip_level:

            return {
                "alert": True,
                "message": (
                    f"💥 多方結構失效\n"
                    f"5分K收盤：{five_min_close}\n"
                    f"跌破中軸：{flip_level}"
                )
            }

    # -----------------------------------
    # 空方失效
    # -----------------------------------
    elif current_state == "🔴 空方模式":

        if five_min_close > flip_level:

            return {
                "alert": True,
                "message": (
                    f"💥 空方結構失效\n"
                    f"5分K收盤：{five_min_close}\n"
                    f"站回中軸：{flip_level}"
                )
            }

    return {
        "alert": False,
        "message": None
    }


def get_no_trade_zone(flip_level: float, buffer: int = 50) -> tuple:
    """
    取得 No Trade Zone 範圍。

    Returns:
        (lower, upper)
    """

    return (
        flip_level - buffer,
        flip_level + buffer
    )


def is_price_in_no_trade_zone(
    current_price: float,
    flip_level: float,
    buffer: int = 50
) -> bool:
    """
    判斷目前價格是否位於中性區。
    """

    lower, upper = get_no_trade_zone(flip_level, buffer)

    return lower <= current_price <= upper