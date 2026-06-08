# trade_logger.py
#
# 記錄 claude_advisor 發出的每一筆指令，並追蹤事後結果。
# 資料寫入 trade_log.json；每筆含 30m / 60m / close 三個追蹤點。

import json
import os
from datetime import datetime

TRADE_LOG_FILE = "trade_log.json"


# --------------------------------------------------
# 讀寫
# --------------------------------------------------

def load_trade_log() -> list:
    if not os.path.exists(TRADE_LOG_FILE):
        return []
    try:
        with open(TRADE_LOG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def save_trade_log(logs: list) -> None:
    try:
        with open(TRADE_LOG_FILE, 'w', encoding='utf-8') as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ [trade_logger] 儲存失敗：{e}")


# --------------------------------------------------
# 記錄訊號
# --------------------------------------------------

def log_signal(
    direction: str,
    entry_condition: str,
    entry_price_zone: float,
    stop_loss: float,
    target_1: float,
    target_2: float,
    confidence: int,
    signal_type: str,
    chip_ctx: dict = None,
    days_to_settlement: int = None,
) -> str:
    """
    記錄一筆 claude_advisor 發出的指令。

    Returns:
        trade_id（格式 YYYYmmdd_HHMMSS），供後續 track_outcome() 查詢。
    """
    chip_ctx = chip_ctx or {}
    now = datetime.now()
    trade_id = now.strftime("%Y%m%d_%H%M%S")

    record = {
        "id": trade_id,
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "direction": direction,
        "entry_condition": entry_condition,
        "entry_price_zone": entry_price_zone,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "confidence": confidence,
        "signal_type": signal_type,
        "session": _get_session(now),

        # 籌碼背景快照
        "sentiment_score":     chip_ctx.get("sentiment_score", 0),
        "foreign_net":         chip_ctx.get("foreign_net", 0),
        "foreign_net_chg_1d":  chip_ctx.get("foreign_net_chg_1d", 0),
        "call_wall":           chip_ctx.get("call_wall"),
        "put_wall":            chip_ctx.get("put_wall"),
        "atr_5d":              chip_ctx.get("atr_5d"),
        "fear_greed":          chip_ctx.get("fear_greed_index"),
        "days_to_settlement":  days_to_settlement,

        # 追蹤結果（待填）
        "outcome":           None,
        "tracked_at_30m":    None,
        "tracked_at_60m":    None,
        "tracked_at_close":  None,
    }

    logs = load_trade_log()
    logs.append(record)
    save_trade_log(logs)

    print(f"✅ [trade_logger] 記錄訊號 {trade_id}：{direction} {signal_type} 信心{confidence}/5")
    return trade_id


# --------------------------------------------------
# 追蹤結果
# --------------------------------------------------

def track_outcome(trade_id: str, current_price: float, track_point: str = "30m") -> None:
    """
    追蹤指定 trade_id 在某時間點的結果。

    track_point: '30m' | '60m' | 'close'
    """
    logs = load_trade_log()

    for record in logs:
        if record["id"] != trade_id:
            continue

        # 30m 追蹤只做一次
        if track_point == "30m" and record.get("tracked_at_30m") is not None:
            return

        direction  = record["direction"]
        entry      = float(record["entry_price_zone"])
        stop       = float(record["stop_loss"])
        t1         = float(record["target_1"])
        t2         = float(record["target_2"])
        current    = float(current_price)

        if direction == "SHORT":
            hit_t1   = current <= t1
            hit_t2   = current <= t2
            hit_stop = current >= stop
            pnl      = entry - current
        else:  # LONG
            hit_t1   = current >= t1
            hit_t2   = current >= t2
            hit_stop = current <= stop
            pnl      = current - entry

        if hit_stop:
            result = "STOP_LOSS"
        elif hit_t2:
            result = "WIN_T2"
        elif hit_t1:
            result = "WIN_T1"
        elif pnl > 0:
            result = "OPEN_PROFIT"
        else:
            result = "OPEN_LOSS"

        outcome = {
            "tracked_at":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "track_point":    track_point,
            "price_at_track": current,
            "hit_target_1":   hit_t1,
            "hit_target_2":   hit_t2,
            "hit_stop_loss":  hit_stop,
            "pnl_points":     round(pnl, 1),
            "result":         result,
        }

        record[f"tracked_at_{track_point}"] = outcome
        # 主要結果用 30m 追蹤（最能反映進場後即時表現）
        if track_point == "30m":
            record["outcome"] = outcome

        print(f"✅ [trade_logger] 追蹤 {trade_id} @{track_point}：{result} {pnl:+.0f}點")
        break

    save_trade_log(logs)


# --------------------------------------------------
# 查詢待追蹤
# --------------------------------------------------

def get_pending_tracks() -> list[dict]:
    """
    回傳所有已發出但尚未完整追蹤的訊號清單。

    每筆格式：{"id": "...", "track_point": "30m"|"60m"|"close"}
    """
    logs = load_trade_log()
    now  = datetime.now()
    pending = []

    for record in logs:
        try:
            signal_time  = datetime.strptime(record["datetime"], "%Y-%m-%d %H:%M:%S")
            elapsed_min  = (now - signal_time).total_seconds() / 60
            today_str    = now.strftime("%Y-%m-%d")
            now_minutes  = now.hour * 60 + now.minute

            if elapsed_min >= 30 and record.get("tracked_at_30m") is None:
                pending.append({"id": record["id"], "track_point": "30m"})

            if elapsed_min >= 60 and record.get("tracked_at_60m") is None:
                pending.append({"id": record["id"], "track_point": "60m"})

            # 收盤追蹤：13:45 之後、當天的訊號
            if now_minutes >= 13 * 60 + 45 and record["date"] == today_str:
                if record.get("tracked_at_close") is None:
                    pending.append({"id": record["id"], "track_point": "close"})

        except Exception:
            continue

    return pending


# --------------------------------------------------
# 工具
# --------------------------------------------------

def _get_session(now: datetime = None) -> str:
    if now is None:
        now = datetime.now()
    t = now.hour * 60 + now.minute
    if 8 * 60 + 45 <= t <= 13 * 60 + 45:
        return "DAY"
    elif 15 * 60 <= t <= 23 * 60 + 59:
        return "NIGHT_EARLY"
    else:
        return "NIGHT_LATE"


# --------------------------------------------------
# 手動測試
# --------------------------------------------------

if __name__ == "__main__":
    print("=== trade_logger 手動測試 ===\n")

    tid = log_signal(
        direction="SHORT",
        entry_condition="跌破Put wall測試",
        entry_price_zone=40000,
        stop_loss=40100,
        target_1=39500,
        target_2=39000,
        confidence=4,
        signal_type="PUT_WALL_BREAK",
        chip_ctx={"sentiment_score": -4, "foreign_net": -65501, "call_wall": 41200, "put_wall": 40000},
        days_to_settlement=5,
    )
    print(f"trade_id: {tid}\n")

    pending = get_pending_tracks()
    print(f"待追蹤：{pending}\n")

    track_outcome(tid, current_price=39800, track_point="30m")

    logs = load_trade_log()
    last = [r for r in logs if r["id"] == tid]
    if last:
        print(f"追蹤結果：{last[0]['outcome']}")
