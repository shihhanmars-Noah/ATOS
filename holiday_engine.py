# holiday_engine.py

import json
import os
from datetime import datetime, date

HOLIDAY_FILE = "holiday_cache.json"


def load_holiday_cache() -> dict:
    """
    載入國定假日快取。
    """

    if not os.path.exists(HOLIDAY_FILE):
        return {
            "updated_at": None,
            "holidays": []
        }

    try:
        with open(HOLIDAY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception as e:
        print(f"⚠️ holiday_cache.json 讀取失敗：{e}")
        return {
            "updated_at": None,
            "holidays": []
        }


def save_holiday_cache(data: dict) -> None:
    """
    儲存國定假日快取。
    """

    try:
        with open(HOLIDAY_FILE, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:
        print(f"⚠️ holiday_cache.json 儲存失敗：{e}")


def is_holiday(target_date: str | None = None) -> bool:
    """
    判斷是否為休市日。

    Args:
        target_date: YYYY-MM-DD

    Returns:
        True = 休市
        False = 非休市
    """

    target_date = target_date or date.today().strftime("%Y-%m-%d")

    cache = load_holiday_cache()
    holidays = cache.get("holidays", [])

    return target_date in holidays


def update_holiday_cache(fetch_func) -> bool:
    """
    更新國定假日快取。

    fetch_func 必須回傳：
        List[str]
        例如 ["2026-01-01", "2026-02-16"]

    失敗時不覆蓋舊快取。
    """

    try:
        holidays = fetch_func()

        if not isinstance(holidays, list):
            raise ValueError("fetch_func 必須回傳 list")

        data = {
            "updated_at": datetime.now().isoformat(),
            "holidays": holidays
        }

        save_holiday_cache(data)

        print("✅ holiday_cache.json 已更新")
        return True

    except Exception as e:
        print(f"⚠️ Holiday cache update failed: {e}")
        return False