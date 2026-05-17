# persistent_state.py

import json
import os
from datetime import datetime

STATE_FILE = "atos_state.json"


DEFAULT_STATE = {
    "updated_at": None,

    # 市場狀態
    "regime": "🟡 中性模式",
    "behavioral_regime": "NORMAL_AUCTION",

    # Flip 與風控
    "flip": 0,
    "kill_switch": None,
    "atr": 0,

    # 夜盤資料
    "night_close": None,

    # 最新 sweep
    "last_sweep_level": None,
    "last_sweep_type": None,

    # Alert 去重
    "last_alert_time": {},
    "last_alert_message": None,

    # Observation Mode
    "observation_mode": False,

    # Event / Settlement
    "event_mode": "NORMAL_MODE",

    # 交易狀態
    "allow_trade": True,
}


def load_state():
    """
    載入系統狀態。

    若檔案不存在或損毀：
    回傳預設狀態。
    """

    if not os.path.exists(STATE_FILE):
        return DEFAULT_STATE.copy()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        merged = DEFAULT_STATE.copy()
        merged.update(data)

        return merged

    except Exception as e:
        print(f"⚠️ 狀態檔損毀，使用預設值：{e}")
        return DEFAULT_STATE.copy()


def save_state(state_dict: dict):
    """
    儲存系統狀態。
    """

    try:
        state_dict["updated_at"] = datetime.now().isoformat()

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                state_dict,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:
        print(f"⚠️ 狀態儲存失敗：{e}")


def update_state(key, value):
    """
    更新單一狀態欄位。
    """

    state = load_state()
    state[key] = value
    save_state(state)


def reset_alert_state():
    """
    手動重置警報狀態。
    """

    state = load_state()

    state["last_alert_time"] = {}
    state["last_alert_message"] = None

    save_state(state)

    print("✅ Alert state reset completed.")