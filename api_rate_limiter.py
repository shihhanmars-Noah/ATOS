# api_rate_limiter.py
# 全域 FinMind API 呼叫頻率控制
# 每小時上限 500 次，超過時自動等待至下一小時

import time
from collections import deque

_api_call_times: deque = deque()


def check_rate_limit():
    """
    呼叫 FinMind API 前必須先呼叫此函式。
    若本小時已達 500 次，自動等待至下一小時窗口。
    """
    now = time.time()

    # 清除 1 小時前的記錄
    while _api_call_times and _api_call_times[0] < now - 3600:
        _api_call_times.popleft()

    if len(_api_call_times) >= 500:
        sleep_seconds = 3600 - (now - _api_call_times[0])
        print(
            f"⚠️ API Rate Limit：本小時已用 {len(_api_call_times)} 次，"
            f"暫停 {sleep_seconds:.0f} 秒"
        )
        time.sleep(sleep_seconds + 10)

    _api_call_times.append(time.time())


def get_call_count() -> int:
    """回傳本小時已使用的 API 次數。"""
    now = time.time()
    while _api_call_times and _api_call_times[0] < now - 3600:
        _api_call_times.popleft()
    return len(_api_call_times)
