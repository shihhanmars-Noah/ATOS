# calendar_engine.py

from datetime import datetime, date

from holiday_engine import is_holiday
from settlement_engine import get_settlement_type


def get_event_mode(target_date: str | None = None) -> str:
    """
    判斷今日事件模式。

    優先順序：
    1. 休市
    2. 月結算
    3. 週結算
    4. 一般交易日

    Args:
        target_date: YYYY-MM-DD

    Returns:
        MARKET_CLOSED
        HIGH_GAMMA_MODE
        SETTLEMENT_MODE
        NORMAL_MODE
    """

    target_date = target_date or date.today().strftime("%Y-%m-%d")

    if is_holiday(target_date):
        return "MARKET_CLOSED"

    settlement_type = get_settlement_type(target_date)

    if settlement_type == "MONTHLY_SETTLEMENT":
        return "HIGH_GAMMA_MODE"

    if settlement_type == "WEEKLY_SETTLEMENT":
        return "SETTLEMENT_MODE"

    return "NORMAL_MODE"


def is_market_closed_by_calendar(target_date: str | None = None) -> bool:
    """
    是否因行事曆休市。
    """

    return get_event_mode(target_date) == "MARKET_CLOSED"


def is_high_gamma_day(target_date: str | None = None) -> bool:
    """
    是否為月結算高 Gamma 模式。
    """

    return get_event_mode(target_date) == "HIGH_GAMMA_MODE"


def is_settlement_mode(target_date: str | None = None) -> bool:
    """
    是否為週結算模式。
    """

    return get_event_mode(target_date) == "SETTLEMENT_MODE"


def describe_event_mode(event_mode: str | None = None) -> dict:
    """
    將事件模式轉為報告用文字。
    """

    event_mode = event_mode or get_event_mode()

    descriptions = {
        "MARKET_CLOSED": {
            "title": "🛑 市場休市",
            "desc": "今日為休市日，停止所有交易監控與方向判斷。",
        },
        "HIGH_GAMMA_MODE": {
            "title": "🔥 月結算高 Gamma 模式",
            "desc": "月結算日，結算引力強，降低部位，禁止尾盤追價。",
        },
        "SETTLEMENT_MODE": {
            "title": "⚠️ 週結算模式",
            "desc": "週結算日，波動與掃單機率提高，僅做確認後交易。",
        },
        "NORMAL_MODE": {
            "title": "✅ 一般交易日",
            "desc": "無特殊結算或休市限制，依 ATOS 標準協議執行。",
        },
    }

    return descriptions.get(event_mode, descriptions["NORMAL_MODE"])