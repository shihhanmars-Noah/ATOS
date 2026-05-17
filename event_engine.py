# event_engine.py

import json
import os

from datetime import datetime, timedelta

EVENT_FILE = "event_cache.json"


def load_event_cache() -> dict:
    """
    載入重大事件快取。
    """

    if not os.path.exists(EVENT_FILE):
        return {
            "updated_at": None,
            "events": []
        }

    try:
        with open(EVENT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception as e:
        print(f"⚠️ event_cache.json 讀取失敗：{e}")

        return {
            "updated_at": None,
            "events": []
        }


def save_event_cache(data: dict) -> None:
    """
    儲存重大事件快取。
    """

    try:
        with open(EVENT_FILE, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:
        print(f"⚠️ event_cache.json 儲存失敗：{e}")


def update_event_cache(fetch_func) -> bool:
    """
    更新重大事件快取。

    fetch_func 必須回傳：
    [
        {
            "name": "US CPI",
            "time": "2026-05-14 20:30",
            "impact": "HIGH"
        }
    ]
    """

    try:
        events = fetch_func()

        if not isinstance(events, list):
            raise ValueError("fetch_func 必須回傳 list")

        payload = {
            "updated_at": datetime.now().isoformat(),
            "events": events
        }

        save_event_cache(payload)

        print("✅ event_cache.json 已更新")

        return True

    except Exception as e:
        print(f"⚠️ Event cache update failed: {e}")
        return False


def get_event_risk_mode(now=None) -> dict:
    """
    判斷目前是否進入事件防禦模式。

    模式：
    - EVENT_DEFENSE
    - EVENT_OBSERVATION
    - NORMAL

    Returns:
        {
            "mode": str,
            "event": str | None,
            "impact": str | None,
            "action": str
        }
    """

    now = now or datetime.now()

    cache = load_event_cache()

    for event in cache.get("events", []):

        try:
            event_time = datetime.strptime(
                event["time"],
                "%Y-%m-%d %H:%M"
            )

            before_window = event_time - timedelta(minutes=15)
            after_window = event_time + timedelta(minutes=15)

            # -----------------------------------
            # 事件公布前
            # -----------------------------------
            if before_window <= now < event_time:
                return {
                    "mode": "EVENT_DEFENSE",
                    "event": event["name"],
                    "impact": event.get("impact", "UNKNOWN"),
                    "action": "重大事件公布前，禁止新開方向單。",
                }

            # -----------------------------------
            # 事件公布後觀察
            # -----------------------------------
            if event_time <= now <= after_window:
                return {
                    "mode": "EVENT_OBSERVATION",
                    "event": event["name"],
                    "impact": event.get("impact", "UNKNOWN"),
                    "action": "事件公布後觀察期，禁止追第一波。",
                }

        except Exception as e:
            print(f"⚠️ Event parsing failed: {e}")

    return {
        "mode": "NORMAL",
        "event": None,
        "impact": None,
        "action": "無重大事件限制。",
    }


def is_event_defense_mode(now=None) -> bool:
    """
    是否為事件防禦模式。
    """

    return get_event_risk_mode(now)["mode"] == "EVENT_DEFENSE"


def is_event_observation_mode(now=None) -> bool:
    """
    是否為事件觀察模式。
    """

    return get_event_risk_mode(now)["mode"] == "EVENT_OBSERVATION"