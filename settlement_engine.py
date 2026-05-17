# settlement_engine.py

import json
import os
from datetime import datetime, date

SETTLEMENT_FILE = "settlement_cache.json"


def load_settlement_cache() -> dict:
    """
    載入結算日快取。
    """

    if not os.path.exists(SETTLEMENT_FILE):
        return {
            "updated_at": None,
            "weekly_settlement": [],
            "monthly_settlement": []
        }

    try:
        with open(SETTLEMENT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception as e:
        print(f"⚠️ settlement_cache.json 讀取失敗：{e}")
        return {
            "updated_at": None,
            "weekly_settlement": [],
            "monthly_settlement": []
        }


def save_settlement_cache(data: dict) -> None:
    """
    儲存結算日快取。
    """

    try:
        with open(SETTLEMENT_FILE, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:
        print(f"⚠️ settlement_cache.json 儲存失敗：{e}")


def get_settlement_type(target_date: str | None = None) -> str | None:
    """
    判斷指定日期是否為結算日。

    Args:
        target_date: YYYY-MM-DD

    Returns:
        MONTHLY_SETTLEMENT
        WEEKLY_SETTLEMENT
        None
    """

    target_date = target_date or date.today().strftime("%Y-%m-%d")

    cache = load_settlement_cache()

    monthly = cache.get("monthly_settlement", [])
    weekly = cache.get("weekly_settlement", [])

    if target_date in monthly:
        return "MONTHLY_SETTLEMENT"

    if target_date in weekly:
        return "WEEKLY_SETTLEMENT"

    return None


def is_settlement_day(target_date: str | None = None) -> bool:
    """
    是否為任何結算日。
    """

    return get_settlement_type(target_date) is not None


def update_settlement_cache(fetch_func) -> bool:
    """
    更新結算日快取。

    fetch_func 必須回傳：
    {
        "weekly_settlement": ["2026-05-20"],
        "monthly_settlement": ["2026-05-20"]
    }

    失敗時不覆蓋舊快取。
    """

    try:
        data = fetch_func()

        if not isinstance(data, dict):
            raise ValueError("fetch_func 必須回傳 dict")

        weekly = data.get("weekly_settlement", [])
        monthly = data.get("monthly_settlement", [])

        if not isinstance(weekly, list) or not isinstance(monthly, list):
            raise ValueError("weekly_settlement / monthly_settlement 必須為 list")

        payload = {
            "updated_at": datetime.now().isoformat(),
            "weekly_settlement": weekly,
            "monthly_settlement": monthly
        }

        save_settlement_cache(payload)

        print("✅ settlement_cache.json 已更新")
        return True

    except Exception as e:
        print(f"⚠️ Settlement cache update failed: {e}")
        return False