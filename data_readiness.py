# data_readiness.py
# 資料就緒判斷工具，供晚盤報告輪詢使用。

from datetime import datetime


def get_today_str() -> str:
    return datetime.now().strftime('%Y-%m-%d')


def is_futures_tick_ready(df) -> bool:
    """
    TaiwanFuturesTick 日盤收盤就緒判斷。
    確認今日日盤時段（13:30-13:45）有資料，避免夜盤 tick 干擾。
    """
    if df is None or df.empty:
        return False
    try:
        import pandas as pd
        today = get_today_str()
        time_col = 'Time' if 'Time' in df.columns else 'time'
        df = df.copy()
        df['_date_str'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
        df['_time_str'] = df[time_col].astype(str)

        day_close = df[
            (df['_date_str'] == today) &
            (df['_time_str'] >= '13:30:00') &
            (df['_time_str'] <= '13:45:00')
        ]
        return not day_close.empty
    except Exception as e:
        try:
            print(f"[data_readiness] is_futures_tick_ready failed: {e}")
        except Exception:
            pass
        return False


def is_chip_data_ready(df) -> bool:
    """
    通用籌碼資料就緒判斷。
    確認最新一筆資料的 date == 今日。
    """
    if df is None or df.empty:
        return False
    try:
        import pandas as pd
        today = get_today_str()
        latest_date = pd.to_datetime(df['date']).max()
        return str(latest_date)[:10] == today
    except Exception as e:
        try:
            print(f"[data_readiness] is_chip_data_ready failed: {e}")
        except Exception:
            pass
        return False


def wait_until_ready(
    check_func,
    label: str = "資料",
    poll_interval_seconds: int = 120,
    deadline_time: str = '16:00',
) -> bool:
    """
    輪詢等待資料就緒。

    回傳 True：資料就緒
    回傳 False：超過死線，強制繼續
    """
    import time

    today = get_today_str()
    start_time = datetime.now()
    deadline = datetime.strptime(f"{today} {deadline_time}", '%Y-%m-%d %H:%M')

    while datetime.now() < deadline:
        if check_func():
            elapsed = (datetime.now() - start_time).total_seconds() / 60
            try:
                print(f"[data_readiness] [{label}] ready (waited {elapsed:.0f} min)")
            except Exception:
                pass
            return True

        elapsed = (datetime.now() - start_time).total_seconds() / 60
        remaining = (deadline - datetime.now()).total_seconds() / 60
        try:
            print(
                f"[data_readiness] [{label}] not ready "
                f"(waited {elapsed:.0f} min, "
                f"{remaining:.0f} min to deadline, "
                f"retry in {poll_interval_seconds}s)"
            )
        except Exception:
            pass
        time.sleep(poll_interval_seconds)

    try:
        print(
            f"[data_readiness] [{label}] deadline {deadline_time} reached, "
            f"forcing send (data may use stale values)"
        )
    except Exception:
        pass
    return False
