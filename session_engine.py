# session_engine.py

from datetime import datetime, time


DAY_SESSION_START = time(8, 45)
DAY_SESSION_END = time(13, 45)

NIGHT_SESSION_START = time(15, 0)
NIGHT_SESSION_END = time(5, 0)


def get_market_session(now=None):
    """
    判斷目前市場時段。

    Returns:
        DAY_SESSION
        NIGHT_SESSION
        CLOSED
    """

    now = now or datetime.now()

    current_time = now.time()
    weekday = now.weekday()  # Monday=0, Sunday=6

    # -----------------------------
    # 週末休市處理
    # -----------------------------

    # 星期六凌晨 05:00 後休市
    if weekday == 5 and current_time > NIGHT_SESSION_END:
        return "CLOSED"

    # 星期日全天休市
    if weekday == 6:
        return "CLOSED"

    # -----------------------------
    # 日盤
    # -----------------------------
    if DAY_SESSION_START <= current_time <= DAY_SESSION_END:
        return "DAY_SESSION"

    # -----------------------------
    # 夜盤（跨日）
    # -----------------------------
    if current_time >= NIGHT_SESSION_START or current_time <= NIGHT_SESSION_END:
        return "NIGHT_SESSION"

    return "CLOSED"


def is_market_open(now=None):
    """
    市場是否開盤。
    """

    return get_market_session(now) != "CLOSED"


def is_day_session(now=None):
    """
    是否為日盤。
    """

    return get_market_session(now) == "DAY_SESSION"


def is_night_session(now=None):
    """
    是否為夜盤。
    """

    return get_market_session(now) == "NIGHT_SESSION"


def is_opening_cooldown(now=None):
    """
    開盤冷靜期：
    08:45 ~ 08:50

    此期間：
    - 不發方向警報
    - 不觸發 trap
    - 只做觀察
    """

    now = now or datetime.now()

    current_time = now.time()

    return time(8, 45) <= current_time < time(8, 50)


def is_no_trade_time(now=None):
    """
    禁止交易時段：
    11:30 ~ 13:00
    """

    now = now or datetime.now()

    current_time = now.time()

    return time(11, 30) <= current_time <= time(13, 0)


def is_market_close_transition(now=None):
    """
    收盤過渡期。

    日盤：
    13:40 ~ 13:45

    夜盤：
    04:55 ~ 05:00

    此期間：
    - 不新開方向單
    - 只允許平倉
    """

    now = now or datetime.now()

    current_time = now.time()

    day_transition = (
        time(13, 40) <= current_time <= time(13, 45)
    )

    night_transition = (
        time(4, 55) <= current_time <= time(5, 0)
    )

    return day_transition or night_transition