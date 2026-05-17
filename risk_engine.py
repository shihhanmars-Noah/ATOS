# risk_engine.py

from calendar_engine import get_event_mode


def get_risk_protocol(event_mode: str | None = None) -> dict:
    """
    根據事件模式，產生風控協議。

    Returns:
        {
            "risk_multiplier": float,
            "allow": str,
            "forbidden": str,
            "description": str
        }
    """

    event_mode = event_mode or get_event_mode()

    protocols = {
        "MARKET_CLOSED": {
            "risk_multiplier": 0.0,
            "allow": "休市，不交易",
            "forbidden": "所有交易行為",
            "description": "🛑 市場休市：停止所有方向判斷與交易警報。",
        },

        "HIGH_GAMMA_MODE": {
            "risk_multiplier": 0.3,
            "allow": "優先價差策略／僅限確認後極短線",
            "forbidden": "尾盤追價、隔夜裸單、凹單、重倉方向單",
            "description": "🔥 月結算高 Gamma 模式：結算引力強，價格容易被磁吸與甩動。",
        },

        "SETTLEMENT_MODE": {
            "risk_multiplier": 0.5,
            "allow": "確認後小量參與／以短線為主",
            "forbidden": "開盤追價、盤末新開大波段部位、凹單",
            "description": "⚠️ 週結算模式：掃單與假突破機率提高，需降低槓桿。",
        },

        "NORMAL_MODE": {
            "risk_multiplier": 1.0,
            "allow": "正常執行 ATOS 標準協議",
            "forbidden": "過度交易、未確認就追價",
            "description": "✅ 一般交易日：依 Flip、ATR、籌碼與行為訊號正常運作。",
        },
    }

    return protocols.get(event_mode, protocols["NORMAL_MODE"])


def apply_risk_multiplier(base_size: float, event_mode: str | None = None) -> float:
    """
    根據事件模式調整部位大小。

    Args:
        base_size: 原始部位，例如 1.0R

    Returns:
        調整後部位
    """

    protocol = get_risk_protocol(event_mode)
    return round(base_size * protocol["risk_multiplier"], 2)


def is_trade_allowed(event_mode: str | None = None) -> bool:
    """
    是否允許交易。
    """

    protocol = get_risk_protocol(event_mode)
    return protocol["risk_multiplier"] > 0


def is_defensive_mode(event_mode: str | None = None) -> bool:
    """
    是否為防守模式。
    """

    protocol = get_risk_protocol(event_mode)
    return protocol["risk_multiplier"] < 1.0