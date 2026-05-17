import pandas as pd
from datetime import datetime, timedelta
from chip_data_engine import load_chip_cache

# --- 核心偵測函式 ---

def detect_liquidity_sweep(df_5min: pd.DataFrame, lookback: int = 20) -> dict:
    """
    偵測流動性掃蕩：
    - 向上假突破：BEARISH_SWEEP
    - 向下假跌破：BULLISH_SWEEP
    """
    if df_5min is None or df_5min.empty or len(df_5min) < lookback + 1:
        return {"sweep": None, "desc": "資料不足", "level": None}

    last_k = df_5min.iloc[-1]
    # 取得前一個區間的高低點
    swing_high = df_5min["high"].shift(1).rolling(lookback).max().iloc[-1]
    swing_low = df_5min["low"].shift(1).rolling(lookback).min().iloc[-1]

    # 向上假突破：高過前高但收盤跌回
    if last_k["high"] > swing_high and last_k["close"] < swing_high:
        return {
            "sweep": "BEARISH_SWEEP",
            "desc": "向上假突破失敗，疑似誘多。",
            "level": round(float(swing_high), 1),
        }

    # 向下假跌破：低過前低但收盤站回
    if last_k["low"] < swing_low and last_k["close"] > swing_low:
        return {
            "sweep": "BULLISH_SWEEP",
            "desc": "向下假跌破失敗，疑似誘空。",
            "level": round(float(swing_low), 1),
        }

    return {"sweep": None, "desc": "無掃單行為", "level": None}


def calculate_retail_bias(retail_ratio: float) -> dict:
    """判斷散戶部位背景"""
    if retail_ratio > 15:
        return {"bias": "RETAIL_LONG_CROWDED", "desc": "散戶偏多，具備多殺多火藥。"}
    if retail_ratio < -15:
        return {"bias": "RETAIL_SHORT_CROWDED", "desc": "散戶偏空，具備軋空火藥。"}
    return {"bias": "BALANCED", "desc": "散戶部位均衡。"}


def calculate_oi_pressure(current_price: float, call_wall: float | None, put_support: float | None) -> dict:
    """判斷 OI 壓力區位置"""
    if call_wall and current_price >= call_wall:
        return {"pressure": "CALL_WALL", "desc": "抵達買權壓力壁。"}
    if put_support and current_price <= put_support:
        return {"pressure": "PUT_SUPPORT", "desc": "抵達賣權支撐區。"}
    return {"pressure": "MID_RANGE", "desc": "位於壓力支撐中間。"}


def detect_trap(retail_bias: str, sweep_signal: str | None) -> dict:
    """判定陷阱共振"""
    if retail_bias == "RETAIL_LONG_CROWDED" and sweep_signal == "BEARISH_SWEEP":
        return {"trap": "LONG_TRAP", "desc": "多頭陷阱：散戶追多失敗，留意多殺多。"}
    if retail_bias == "RETAIL_SHORT_CROWDED" and sweep_signal == "BULLISH_SWEEP":
        return {"trap": "SHORT_TRAP", "desc": "空頭陷阱：散戶誘空失敗，留意軋空。"}
    return {"trap": "NO_TRAP", "desc": "尚未形成陷阱。"}


# --- 外部入口函式 ---

def analyze_behavioral_context(
    df_5min: pd.DataFrame,
    current_price: float,
    chip: dict | None = None,
) -> dict:
    """
    提供給 monitor_engine 的統一分析入口。
    """
    chip = chip or load_chip_cache()

    # 資料新鮮度檢查
    is_stale = False
    if chip.get("updated_at"):
        updated_time = datetime.fromisoformat(chip["updated_at"])
        if datetime.now() - updated_time > timedelta(hours=4):
            is_stale = True

    sweep = detect_liquidity_sweep(df_5min)
    retail = calculate_retail_bias(chip.get("retail_ratio", 0) if not is_stale else 0)
    oi = calculate_oi_pressure(current_price, chip.get("call_wall"), chip.get("put_support"))
    trap = detect_trap(retail["bias"], sweep["sweep"])

    return {
        "sweep": sweep["sweep"],
        "sweep_desc": sweep["desc"],
        "sweep_level": sweep["level"],
        "retail_bias": retail["bias"],
        "retail_desc": retail["desc"] if not is_stale else "⚠️ 籌碼資料過期",
        "oi_pressure": oi["pressure"],
        "oi_desc": oi["desc"],
        "trap": trap["trap"],
        "trap_desc": trap["desc"],
        "chip_updated_at": chip.get("updated_at"),
    }