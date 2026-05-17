import os
import time
import logging
import requests
from functools import wraps
from dotenv import load_dotenv

load_dotenv()

def safe_execute(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            # 這裡我們換一個絕對獨一無二的識別頭
            error_msg = f"🛡️ [ATOS_FINAL_V3] 異常發生：{func.__name__} -> {str(e)}"
            print(error_msg)
            return None
    return wrapper