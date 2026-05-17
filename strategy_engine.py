# strategy_engine.py

import pandas as pd

from error_handler import safe_execute
from atos_logic import analyze_atos_state


@safe_execute
def calculate_atr(df_history: pd.DataFrame, period: int = 14) -> float:
    """
    計算標準 ATR。

    df_history 必須包含：
    - high
    - low
    - close
    """

    if df_history is None or df_history.empty:
        raise ValueError("df_history is empty")

    required_cols = {"high", "low", "close"}
    if not required_cols.issubset(df_history.columns):
        raise ValueError(f"df_history 必須包含欄位：{required_cols}")

    prev_close = df_history["close"].shift(1)

    tr = pd.concat(
        [
            df_history["high"] - df_history["low"],
            (df_history["high"] - prev_close).abs(),
            (df_history["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.rolling(period).mean().iloc[-1]

    return round(float(atr), 1)


@safe_execute
def calculate_atos_regime(
    price: float,
    flip: float,
    df_history: pd.DataFrame,
    atr_period: int = 14,
) -> dict:
    """
    強化版 ATOS Regime 判定。

    使用：
    - Flip
    - ATR
    - Buffer
    - 5分K失效概念

    Returns:
        {
            "regime": str,
            "state": str,
            "action": str,
            "forbidden": str,
            "no_trade": bool,
            "diff": float,
            "atr": float,
            "kill_switch": float | None,
            "volatility_state": str
        }
    """

    atr = calculate_atr(df_history, atr_period)

    if atr is None:
        raise ValueError("ATR 計算失敗")

    # 使用 ATR 的 0.5 倍當動態 buffer
    dynamic_buffer = max(50, 0.5 * atr)
    diff = price - flip

    base_state = analyze_atos_state(price, flip)

    # -----------------------------------
    # Regime 判定
    # -----------------------------------
    if diff > dynamic_buffer:
        regime = "🟢 多方模式"
        kill_switch = flip
        action = "拉回買／順勢做多"
        forbidden = "追空、逆勢放空、猜頭"

    elif diff < -dynamic_buffer:
        regime = "🔴 空方模式"
        kill_switch = flip
        action = "反彈空／禁止抄底"
        forbidden = "抄底、逆勢做多、猜底"

    else:
        regime = "🟡 中性模式"
        kill_switch = None
        action = "等待突破或跌破確認"
        forbidden = "追價、猜方向、頻繁交易"

    # -----------------------------------
    # 波動狀態
    # -----------------------------------
    if atr < 60:
        volatility_state = "QUIET"
    elif atr < 150:
        volatility_state = "NORMAL"
    else:
        volatility_state = "HIGH_VOL"

    return {
        "regime": regime,
        "state": base_state["state"],
        "action": action,
        "forbidden": forbidden,
        "no_trade": kill_switch is None,
        "diff": round(diff, 1),
        "atr": atr,
        "dynamic_buffer": round(dynamic_buffer, 1),
        "kill_switch": kill_switch,
        "volatility_state": volatility_state,
    }


@safe_execute
def calculate_tactical_levels(price: float, flip: float, atr: float) -> dict:
    """
    產生戰術點位。

    R1 / S1 使用 ATR 動態調整。
    """

    r1 = flip + atr
    s1 = flip - atr

    r2 = flip + 1.5 * atr
    s2 = flip - 1.5 * atr

    return {
        "Flip": round(flip, 1),
        "R1": round(r1, 1),
        "S1": round(s1, 1),
        "R2": round(r2, 1),
        "S2": round(s2, 1),
    }


@safe_execute
def build_strategy_snapshot(
    price: float,
    flip: float,
    df_history: pd.DataFrame,
) -> dict:
    """
    統一產出策略快照。
    給 report_engine / monitor_engine 使用。
    """

    regime_data = calculate_atos_regime(
        price=price,
        flip=flip,
        df_history=df_history,
    )

    if regime_data is None:
        raise ValueError("regime_data 計算失敗")

    levels = calculate_tactical_levels(
        price=price,
        flip=flip,
        atr=regime_data["atr"],
    )

    return {
        "price": price,
        "flip": flip,
        "regime": regime_data["regime"],
        "action": regime_data["action"],
        "forbidden": regime_data["forbidden"],
        "no_trade": regime_data["no_trade"],
        "diff": regime_data["diff"],
        "atr": regime_data["atr"],
        "dynamic_buffer": regime_data["dynamic_buffer"],
        "kill_switch": regime_data["kill_switch"],
        "volatility_state": regime_data["volatility_state"],
        "levels": levels,
    }