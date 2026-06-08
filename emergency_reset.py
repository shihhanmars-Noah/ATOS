"""
emergency_reset.py
------------------
緊急資料重置工具：清除 tick cache 並重置 state 關鍵欄位。
在懷疑數據污染時手動執行，讓系統明天重新抓取。

用法：
    python emergency_reset.py
"""

import json
import os
from datetime import datetime

# ── Step 1：清空 tick cache ──────────────────────────────────────────────────
TICK_CACHE_FILE = "txf_tick_cache.json"

try:
    with open(TICK_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f)
    print(f"✅ tick cache 已清空（{TICK_CACHE_FILE}）")
except Exception as e:
    print(f"⚠️ tick cache 清空失敗：{e}")

# ── Step 2：重置 state 關鍵欄位 ──────────────────────────────────────────────
try:
    from persistent_state import load_state, save_state

    state = load_state()

    RESET_KEYS = [
        "flip", "pivot", "r1", "s1",
        "prev_close", "prev_high", "prev_low",
        "mid_range", "call_wall", "put_wall",
        "day_session_close", "day_session_high", "day_session_low",
    ]

    for key in RESET_KEYS:
        state[key] = None

    state["emergency_reset_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    save_state(state)
    print(f"✅ state 關鍵欄位已重置：{', '.join(RESET_KEYS)}")

except Exception as e:
    print(f"⚠️ state 重置失敗：{e}")

# ── Step 3：顯示目前 state 快照價格供確認 ────────────────────────────────────
try:
    from persistent_state import load_state
    s = load_state()
    price    = s.get("price", "(無)")
    reset_at = s.get("emergency_reset_at", "(未記錄)")
    print(f"\n目前即時快照價格：{price}")
    print(f"重置時間：{reset_at}")
    print("\n請確認上方快照價格合理後，重新啟動 ATOS。")
except Exception as e:
    print(f"⚠️ 無法讀取 state：{e}")
