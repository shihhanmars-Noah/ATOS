# alert_log_engine.py

import json
from datetime import datetime, date
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
ALERT_LOG_FILE = PROJECT_DIR / "alert_log.json"

DAY_SESSION_START = "08:45:00"
DAY_SESSION_END = "13:45:00"


def parse_alert_datetime(row: dict) -> datetime | None:
    """
    將 alert row 的 datetime / date+time 轉成 datetime。
    """

    try:
        dt_text = row.get("datetime")

        if dt_text:
            return datetime.fromisoformat(str(dt_text))

        date_text = row.get("date")
        time_text = row.get("time")

        if date_text and time_text:
            return datetime.fromisoformat(f"{date_text} {time_text}")

    except Exception:
        return None

    return None


def is_day_session_alert(row: dict) -> bool:
    """
    判斷警報是否屬於日盤時段 08:45–13:45。

    晚盤日盤複盤只統計這個時段，避免 15:00 後夜盤 / 盤後警報混入。
    """

    dt = parse_alert_datetime(row)

    if dt is None:
        return False

    today_text = date.today().strftime("%Y-%m-%d")

    if dt.strftime("%Y-%m-%d") != today_text:
        return False

    t = dt.strftime("%H:%M:%S")

    return DAY_SESSION_START <= t <= DAY_SESSION_END


def load_alert_log() -> list[dict]:
    """
    讀取盤中警報紀錄。
    """

    if not ALERT_LOG_FILE.exists():
        return []

    try:
        with open(ALERT_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return data

        return []

    except Exception as e:
        print(f"⚠️ 讀取 alert_log.json 失敗：{e}")
        return []


def save_alert_log(rows: list[dict]) -> None:
    """
    儲存盤中警報紀錄。
    """

    try:
        with open(ALERT_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(
                rows,
                f,
                ensure_ascii=False,
                indent=2,
            )

    except Exception as e:
        print(f"⚠️ 儲存 alert_log.json 失敗：{e}")


def clean_alert_log(rows: list[dict], keep_days: int = 10) -> list[dict]:
    """
    清理警報紀錄，只保留最近 keep_days 天。
    """

    if not rows:
        return []

    today = date.today()
    cleaned = []

    for row in rows:
        try:
            row_date = datetime.fromisoformat(
                str(row.get("datetime"))
            ).date()

            if (today - row_date).days <= keep_days:
                cleaned.append(row)

        except Exception:
            continue

    return cleaned


def record_alert(context: dict, message: str | None = None) -> None:
    """
    紀錄一則盤中警報。

    context 來源：
    alert_engine_v2.send_human_alert(context)
    """

    if not isinstance(context, dict):
        return

    now = datetime.now()

    row = {
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),

        "event": context.get("event"),
        "price": context.get("price"),
        "flip": context.get("flip"),
        "pivot": context.get("pivot"),
        "r1": context.get("r1"),
        "s1": context.get("s1"),

        "sentiment": context.get("sentiment"),
        "behavior": context.get("behavior"),
        "trap": context.get("trap"),
        "sweep": context.get("sweep"),
        "is_realtime": context.get("is_realtime"),

        "stop": context.get("stop"),
        "target": context.get("target"),

        "tick_source": context.get("tick_source"),
        "tick_time": context.get("tick_time"),
        "latest_k_time": context.get("latest_k_time"),
        "data_delay_minutes": context.get("data_delay_minutes"),
    }

    if message:
        row["message_preview"] = str(message)[:500]

    rows = load_alert_log()
    rows.append(row)
    rows = clean_alert_log(rows)

    save_alert_log(rows)


def get_today_alerts() -> list[dict]:
    """
    取得今日所有警報。

    注意：這包含日盤與夜盤；晚盤日盤複盤請使用 get_today_day_session_alerts()。
    """

    today_str = date.today().strftime("%Y-%m-%d")
    rows = load_alert_log()

    return [
        row for row in rows
        if row.get("date") == today_str
    ]


def get_today_day_session_alerts() -> list[dict]:
    """
    取得今日日盤警報，只保留 08:45–13:45。

    用於晚盤「今日盤中警報複盤」，避免 15:00 後夜盤警報混入日盤檢討。
    """

    rows = load_alert_log()

    alerts = [
        row for row in rows
        if is_day_session_alert(row)
    ]

    alerts.sort(key=lambda row: str(row.get("datetime") or ""))

    return alerts


def summarize_today_alerts() -> dict:
    """
    統計今日警報。
    """

    alerts = get_today_day_session_alerts()

    summary = {
        "total": len(alerts),
        "events": {},
        "has_long_trap": False,
        "has_short_trap": False,
        "has_sweep": False,
        "has_flip_invalid": False,
        "has_flip_break": False,
        "has_flip_recover": False,
        "first_alert": alerts[0] if alerts else None,
        "last_alert": alerts[-1] if alerts else None,
        "alerts": alerts,
        "session_start": DAY_SESSION_START,
        "session_end": DAY_SESSION_END,
    }

    for alert in alerts:
        event = str(alert.get("event") or "UNKNOWN")

        summary["events"][event] = summary["events"].get(event, 0) + 1

        if event == "LONG_TRAP":
            summary["has_long_trap"] = True

        if event == "SHORT_TRAP":
            summary["has_short_trap"] = True

        if event in ["SWEEP", "BEARISH_SWEEP", "BULLISH_SWEEP"]:
            summary["has_sweep"] = True

        if event == "FLIP_INVALID":
            summary["has_flip_invalid"] = True

        if event == "FLIP_BREAK":
            summary["has_flip_break"] = True

        if event == "FLIP_RECOVER":
            summary["has_flip_recover"] = True

    return summary


def build_alert_log_text(max_items: int = 8) -> str:
    """
    建立晚盤報告用警報文字。
    """

    summary = summarize_today_alerts()
    alerts = summary["alerts"]

    if not alerts:
        return (
            f"今日盤中警報：0 則｜統計時段 {DAY_SESSION_START[:5]}–{DAY_SESSION_END[:5]}\n"
            "解讀：日盤時段沒有觸發 ATOS 主要警報；夜盤 / 盤後警報不列入日盤複盤。"
        )

    lines = []

    for idx, alert in enumerate(alerts[-max_items:], start=1):
        lines.append(
            f"{idx}. {alert.get('time')}｜"
            f"{alert.get('event')}｜"
            f"價位 {alert.get('price')}｜"
            f"中軸 {alert.get('flip')}"
        )

    event_text = ", ".join(
        [f"{k}: {v}" for k, v in summary["events"].items()]
    )

    return (
        f"今日盤中警報：{summary['total']} 則｜統計時段 {DAY_SESSION_START[:5]}–{DAY_SESSION_END[:5]}\n"
        f"事件統計：{event_text}\n\n"
        "最近警報：\n"
        + "\n".join(lines)
    )


if __name__ == "__main__":
    print(build_alert_log_text())