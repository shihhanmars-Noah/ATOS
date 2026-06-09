# settlement_engine.py

import json
import os
from datetime import datetime, date, timedelta

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


def get_next_settlement_date() -> date | None:
    """
    回傳下一個結算日的 date 物件；找不到時回傳 None。
    優先從快取讀取，快取為空時自動用 compute_settlement_dates() 計算。
    """
    today = date.today()
    cache = load_settlement_cache()

    all_dates = []
    for d_str in cache.get("monthly_settlement", []) + cache.get("weekly_settlement", []):
        try:
            d = date.fromisoformat(d_str)
            if d >= today:
                all_dates.append(d)
        except Exception:
            pass

    if not all_dates:
        # 快取為空，自動計算台指期月結算日（每月第三個星期三）
        for d_str in compute_settlement_dates(months_ahead=3):
            try:
                d = date.fromisoformat(d_str)
                if d >= today:
                    all_dates.append(d)
            except Exception:
                pass

    return min(all_dates) if all_dates else None


def get_days_to_settlement() -> int:
    """
    計算距下一個結算日的天數。

    Returns:
        距下一個結算日的天數（整數，當天算0）；找不到結算日時回傳 99
    """
    next_d = get_next_settlement_date()
    if next_d is None:
        return 99
    return (next_d - date.today()).days


def compute_settlement_dates(months_ahead: int = 3) -> list[str]:
    """
    自動計算台指期月結算日（每月第三個星期三）。
    回傳未來 months_ahead 個月的結算日，格式 YYYY-MM-DD。
    """
    results = []
    today = date.today()
    year, month = today.year, today.month

    for _ in range(months_ahead + 1):  # 多算一個月確保含當月
        # 找當月第一天
        first = date(year, month, 1)
        # 當月第一個星期三（weekday 2）
        days_to_wed = (2 - first.weekday()) % 7
        first_wed = first + timedelta(days=days_to_wed)
        # 第三個星期三
        third_wed = first_wed + timedelta(weeks=2)
        d_str = third_wed.strftime("%Y-%m-%d")
        if third_wed >= today:
            results.append(d_str)
        # 下一個月
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1

    return sorted(results)


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