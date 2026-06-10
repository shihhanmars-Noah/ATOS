# alert_engine_v2.py

import time

from messenger import send_to_telegram

try:
    from intraday_advice_engine import build_intraday_ai_advice_text
except Exception:
    build_intraday_ai_advice_text = None

try:
    from scenario_engine import build_scenario_text
except Exception:
    build_scenario_text = None

try:
    from strategy_filter_engine import (
        enrich_context_with_strategy_filter,
        build_strategy_filter_section,
    )
except Exception:
    enrich_context_with_strategy_filter = None
    build_strategy_filter_section = None

try:
    from alert_log_engine import record_alert
except Exception:
    record_alert = None

try:
    from claude_advisor import advise as claude_advise
except Exception:
    claude_advise = None


ALERT_COOLDOWN = {
    "LONG_TRAP": 1800,        # 30分鐘
    "SHORT_TRAP": 1800,       # 30分鐘
    "FLIP_INVALID": 1800,     # 30分鐘
    "SWEEP": 900,             # 15分鐘
    "BEARISH_SWEEP": 900,     # 15分鐘
    "BULLISH_SWEEP": 900,     # 15分鐘
    "FLIP_BREAK": 600,        # 10分鐘
    "FLIP_RECOVER": 600,      # 10分鐘
    "R1_TOUCH": 900,          # 15分鐘
    "S1_TOUCH": 900,          # 15分鐘
    "NEUTRAL_ZONE": 900,      # 15分鐘
    "LONG_CONFIRM_V3": 600,   # 10分鐘
    "SHORT_RETEST_FAIL_V3": 600,  # 10分鐘
}

# 全域最小發送間隔（秒）：任意兩則警報之間至少間隔此時間
# 避免多事件同時觸發連炸多則；單一事件仍受 ALERT_COOLDOWN 控制
GLOBAL_ALERT_MIN_INTERVAL = 90  # 1分30秒

_last_alert_time = {}        # {alert_key: timestamp}，記憶體層
_last_any_alert_time = 0.0   # 全域最後一次發送時間


# --------------------------------------------------
# Utility
# --------------------------------------------------

def format_price(value):
    """
    價格格式化。
    """

    if value is None:
        return "N/A"

    try:
        return round(float(value), 1)
    except Exception:
        return value


def normalize_event(event: str | None) -> str:
    """
    統一事件名稱。
    """

    if not event:
        return "UNKNOWN"

    event = str(event).upper()

    if event in ["BEARISH_SWEEP", "BULLISH_SWEEP"]:
        return "SWEEP"

    return event


def _load_persisted_alert_times() -> dict:
    """從 atos_state.json 讀取上次警報時間（跨重啟持久化）。"""
    try:
        from persistent_state import load_state
        state = load_state()
        return state.get("alert_last_sent", {})
    except Exception:
        return {}


def _save_persisted_alert_time(key: str, ts: float) -> None:
    """把警報時間寫回 atos_state.json，避免重啟後冷卻歸零。"""
    try:
        from persistent_state import load_state, save_state
        state = load_state()
        times = state.get("alert_last_sent", {})
        times[key] = ts
        # 只保留最近 50 筆，避免無限膨脹
        if len(times) > 50:
            oldest = sorted(times, key=lambda k: times[k])
            for old_key in oldest[: len(times) - 50]:
                del times[old_key]
        state["alert_last_sent"] = times
        save_state(state)
    except Exception:
        pass


def can_send(event: str, key: str) -> bool:
    """
    冷卻時間控制，避免重複洗版。

    雙層保護：
    1. 全域最小間隔（GLOBAL_ALERT_MIN_INTERVAL）：任意兩則警報間距
       → 防止多事件同時觸發連炸
    2. 單事件冷卻（ALERT_COOLDOWN）：同一 key 的重複觸發間距
       → 防止同一訊號持續洗版
    3. 持久化：從 state.json 讀取上次時間，重啟後冷卻不歸零
    """
    global _last_any_alert_time

    now = time.time()

    # --- 層1：全域最小間隔 ---
    if now - _last_any_alert_time < GLOBAL_ALERT_MIN_INTERVAL:
        remaining = int(GLOBAL_ALERT_MIN_INTERVAL - (now - _last_any_alert_time))
        print(f"⏳ [alert] 全域冷卻中，還有 {remaining}s，跳過 {event}")
        return False

    # --- 層2：單事件冷卻（記憶體 + 持久化合併取最新） ---
    cooldown = ALERT_COOLDOWN.get(event, 900)
    mem_last = _last_alert_time.get(key, 0)

    # 首次或記憶體為空時從 state.json 補充
    if mem_last == 0:
        persisted = _load_persisted_alert_times()
        mem_last = persisted.get(key, 0)
        if mem_last:
            _last_alert_time[key] = mem_last  # 回填記憶體

    if now - mem_last < cooldown:
        remaining = int(cooldown - (now - mem_last))
        print(f"⏳ [alert] {event} 冷卻中，還有 {remaining}s")
        return False

    # --- 通過：更新記憶體 + 持久化 ---
    _last_alert_time[key] = now
    _last_any_alert_time = now
    _save_persisted_alert_time(key, now)
    return True


def build_alert_key(context: dict) -> str:
    """
    建立警報去重 key。

    原則：
    - event + flip + price bucket
    - 避免每根 5分K 都發同一則
    """

    event = normalize_event(context.get("event"))
    flip = context.get("flip")
    price = context.get("price")

    try:
        price_bucket = int(float(price) // 50 * 50)
    except Exception:
        price_bucket = "NA"

    try:
        flip_key = int(float(flip))
    except Exception:
        flip_key = "NA"

    return f"{event}_{flip_key}_{price_bucket}"


def get_context_value(context: dict, *keys, default=None):
    """
    從 context 取多個可能欄位。
    """

    for key in keys:
        value = context.get(key)

        if value is not None:
            return value

    return default


# --------------------------------------------------
# AI Advice
# --------------------------------------------------

def build_ai_advice_section(context: dict) -> str:
    """
    建立盤中 AI 即時建議段落。

    注意：
    - AI 不新增點位
    - 只使用 context 已存在的價位與狀態
    """

    if build_intraday_ai_advice_text is None:
        return (
            "🤖 AI 即時建議\n"
            "狀態：建議模組尚未啟用\n"
            "現在動作：等待\n\n"
            "判斷原因：\n"
            "找不到 intraday_advice_engine.py，請確認檔案是否已建立。\n\n"
            "最終指令：\n"
            "> 建議模組未啟用前，不依此訊息進場。"
        )

    event = normalize_event(context.get("event"))

    price = get_context_value(context, "price", "current_price")
    flip = get_context_value(context, "flip")
    pivot = get_context_value(context, "pivot")
    r1 = get_context_value(context, "r1", "R1")
    s1 = get_context_value(context, "s1", "S1")

    # S1/R1 有效性驗證：若方向錯誤（S1 > price 或 R1 < price），改用 Put/Call wall 替代
    if price:
        try:
            _pf = float(price)
            # 嘗試從 context 或 chip_cache 取得 put_wall / call_wall
            _put_wall  = context.get("put_wall")
            _call_wall = context.get("call_wall")
            if not _put_wall or not _call_wall:
                try:
                    import json as _j
                    with open("chip_cache.json", encoding="utf-8") as _f:
                        _cc = _j.load(_f)
                    _oi = _cc.get("option_oi", {})
                    _put_wall  = _put_wall  or _oi.get("put_wall_strike")
                    _call_wall = _call_wall or _oi.get("call_wall_strike")
                except Exception:
                    pass
            if s1 is not None:
                try:
                    if float(s1) > _pf * 0.99:   # S1 不應高於現價
                        s1 = _put_wall or s1
                except Exception:
                    pass
            if r1 is not None:
                try:
                    if float(r1) < _pf * 1.01:   # R1 不應低於現價
                        r1 = _call_wall or r1
                except Exception:
                    pass
        except Exception:
            pass

    sentiment = get_context_value(
        context,
        "sentiment",
        "institutional_sentiment",
        default="",
    )

    behavior = get_context_value(
        context,
        "behavior",
        "behavioral_regime",
        default="",
    )

    trap = get_context_value(context, "trap", default=None)
    sweep = get_context_value(context, "sweep", default=None)
    is_realtime = bool(get_context_value(context, "is_realtime", default=True))

    # 如果 alert 事件本身就是 trap / sweep，就補進 advice engine
    if event == "LONG_TRAP":
        trap = "LONG_TRAP"

    elif event == "SHORT_TRAP":
        trap = "SHORT_TRAP"

    raw_event = str(context.get("event") or "").upper()

    if raw_event == "BEARISH_SWEEP":
        sweep = "BEARISH_SWEEP"

    elif raw_event == "BULLISH_SWEEP":
        sweep = "BULLISH_SWEEP"

    elif event == "SWEEP" and sweep is None:
        sweep = "BEARISH_SWEEP"

    try:
        return build_intraday_ai_advice_text(
            price=price,
            flip=flip,
            pivot=pivot,
            r1=r1,
            s1=s1,
            sentiment=sentiment,
            behavior=behavior,
            trap=trap,
            sweep=sweep,
            is_realtime=is_realtime,
        )

    except Exception as e:
        return (
            "🤖 AI 即時建議\n"
            "狀態：建議產生失敗\n"
            "現在動作：等待\n\n"
            "判斷原因：\n"
            f"AI 建議模組發生錯誤：{e}\n\n"
            "最終指令：\n"
            "> 建議產生失敗，本次不依此訊息進場。"
        )


def build_scenario_section(context: dict) -> str:
    """
    建立早盤劇本對應段落。
    """

    if build_scenario_text is None:
        return ""

    try:
        return build_scenario_text(
            price=context.get("price"),
            flip=context.get("flip"),
            pivot=context.get("pivot"),
            r1=context.get("r1"),
            s1=context.get("s1"),
        )

    except Exception as e:
        print(f"⚠️ 早盤劇本對應產生失敗：{e}")
        return ""


def build_strategy_filter_text(context: dict) -> str:
    """
    建立 Strategy Filter 區塊。
    """

    if build_strategy_filter_section is None:
        return ""

    try:
        return build_strategy_filter_section(context)

    except Exception as e:
        return (
            "🛡️ ATOS Strategy Filter\n"
            "------------------\n"
            f"狀態：分級模組產生失敗：{e}\n"
            "最終指令：\n"
            "> 分級失敗，本次不依此訊息進場。"
        )


# --------------------------------------------------
# Base Alert Messages
# --------------------------------------------------

def build_long_trap_message(context: dict) -> str:
    """
    多頭陷阱警報。
    """

    price = context.get("price")
    flip = context.get("flip")
    stop = context.get("stop")
    target = context.get("target")

    return (
        "🔴 多頭陷阱警報\n"
        "------------------\n\n"
        "市場假突破後轉弱。\n"
        "追多的人可能被套。\n\n"
        "盤中判斷：\n"
        "● 不要追多\n"
        "● 有多單先降風險\n"
        "● 等下一根 5分K 確認\n\n"
        f"📍 現價：{format_price(price)}\n"
        f"📍 中軸：{format_price(flip)}\n"
        f"🛑 防守點：{format_price(stop)}\n"
        f"🎯 觀察區：{format_price(target)}"
    )


def build_short_trap_message(context: dict) -> str:
    """
    空頭陷阱 / 軋空警報。
    """

    price = context.get("price")
    flip = context.get("flip")
    stop = context.get("stop")
    target = context.get("target")

    return (
        "🔥 空方陷阱 / 軋空警報\n"
        "------------------\n\n"
        "市場假跌破後拉回。\n"
        "空單可能被迫停損。\n\n"
        "盤中判斷：\n"
        "● 不要追空\n"
        "● 空單先降風險\n"
        "● 等拉回再觀察\n\n"
        f"📍 現價：{format_price(price)}\n"
        f"📍 中軸：{format_price(flip)}\n"
        f"🛑 防守點：{format_price(stop)}\n"
        f"🎯 觀察區：{format_price(target)}"
    )


def build_flip_invalid_message(context: dict) -> str:
    """
    Flip 方向失效警報。
    """

    price = context.get("price")
    flip = context.get("flip")
    pivot = get_context_value(context, "pivot")
    r1 = get_context_value(context, "r1", "R1")
    s1 = get_context_value(context, "s1", "S1")

    return (
        "⚠️ 原本方向失效\n"
        "------------------\n\n"
        "價格重新站回 / 跌破市場分界點。\n"
        "原本慣性可能已經結束。\n\n"
        "盤中判斷：\n"
        "● 原策略取消\n"
        "● 不要硬拗\n"
        "● 重新等待方向\n\n"
        f"📍 現價：{format_price(price)}\n"
        f"📍 中軸：{format_price(flip)}\n"
        f"📍 Pivot：{format_price(pivot)}\n"
        f"📍 R1：{format_price(r1)}\n"
        f"📍 S1：{format_price(s1)}"
    )


def build_sweep_message(context: dict) -> str:
    """
    掃單警報。
    """

    raw_event = str(context.get("event") or "").upper()
    price = context.get("price")
    flip = context.get("flip")
    pivot = get_context_value(context, "pivot")
    r1 = get_context_value(context, "r1", "R1")
    s1 = get_context_value(context, "s1", "S1")

    if raw_event == "BEARISH_SWEEP" or context.get("sweep") == "BEARISH_SWEEP":
        title = "🟠 上方掃單警報"
        description = (
            "市場快速掃過上方關鍵位置。\n"
            "可能正在清洗空單停損，也可能是假突破。"
        )
        action = (
            "● 不要追第一波突破\n"
            "● 等 5分K 收盤\n"
            "● 確認是否站穩壓力上方"
        )

    elif raw_event == "BULLISH_SWEEP" or context.get("sweep") == "BULLISH_SWEEP":
        title = "🟠 下方掃單警報"
        description = (
            "市場快速掃過下方關鍵位置。\n"
            "可能正在清洗多單停損，也可能是假跌破。"
        )
        action = (
            "● 不要追第一波急跌\n"
            "● 等 5分K 收盤\n"
            "● 確認是否重新收回支撐"
        )

    else:
        title = "🟠 掃單警報"
        description = (
            "市場剛剛快速掃過關鍵位置。\n"
            "可能正在清洗停損單。"
        )
        action = (
            "● 不要追第一波\n"
            "● 等 5分K 收盤\n"
            "● 確認真假突破"
        )

    return (
        f"{title}\n"
        "------------------\n\n"
        f"{description}\n\n"
        "盤中判斷：\n"
        f"{action}\n\n"
        f"📍 現價：{format_price(price)}\n"
        f"📍 中軸：{format_price(flip)}\n"
        f"📍 Pivot：{format_price(pivot)}\n"
        f"📍 R1：{format_price(r1)}\n"
        f"📍 S1：{format_price(s1)}"
    )


def build_flip_break_message(context: dict) -> str:
    """
    跌破 Flip 警報。
    """

    price = context.get("price")
    flip = context.get("flip")
    pivot = get_context_value(context, "pivot")
    s1 = get_context_value(context, "s1", "S1")

    return (
        "🔴 跌破中軸警報\n"
        "------------------\n\n"
        "價格跌破今日多空分界。\n"
        "短線偏空，但不代表可以急跌後追空。\n\n"
        "盤中判斷：\n"
        "● 等反彈測試中軸\n"
        "● 反彈不過才觀察空方延續\n"
        "● 重新站回中軸則空方劇本取消\n\n"
        f"📍 現價：{format_price(price)}\n"
        f"📍 中軸：{format_price(flip)}\n"
        f"📍 Pivot：{format_price(pivot)}\n"
        f"📍 S1：{format_price(s1)}"
    )


def build_flip_recover_message(context: dict) -> str:
    """
    站回 Flip 警報。
    """

    price = context.get("price")
    flip = context.get("flip")
    pivot = get_context_value(context, "pivot")
    r1 = get_context_value(context, "r1", "R1")

    return (
        "🟢 站回中軸警報\n"
        "------------------\n\n"
        "價格站回今日多空分界。\n"
        "短線轉強觀察，但不代表可以直接追高。\n\n"
        "盤中判斷：\n"
        "● 等回測中軸不破\n"
        "● 回測不破才觀察多方延續\n"
        "● 收回中軸下方則多方劇本取消\n\n"
        f"📍 現價：{format_price(price)}\n"
        f"📍 中軸：{format_price(flip)}\n"
        f"📍 Pivot：{format_price(pivot)}\n"
        f"📍 R1：{format_price(r1)}"
    )


def build_r1_touch_message(context: dict) -> str:
    """
    接近 R1 警報。
    """

    price = context.get("price")
    r1 = get_context_value(context, "r1", "R1")
    flip = context.get("flip")

    return (
        "🟠 接近上方壓力 R1\n"
        "------------------\n\n"
        "價格接近今日上方壓力。\n"
        "這裡不適合追高，重點是觀察是否突破後站穩。\n\n"
        "盤中判斷：\n"
        "● 多單不追\n"
        "● 觀察是否假突破\n"
        "● 突破後回測不破才看續強\n\n"
        f"📍 現價：{format_price(price)}\n"
        f"📍 R1：{format_price(r1)}\n"
        f"📍 中軸：{format_price(flip)}"
    )


def build_s1_touch_message(context: dict) -> str:
    """
    接近 S1 警報。
    """

    price = context.get("price")
    s1 = get_context_value(context, "s1", "S1")
    flip = context.get("flip")

    return (
        "🟠 接近下方支撐 S1\n"
        "------------------\n\n"
        "價格接近今日下方支撐。\n"
        "這裡不適合追空，重點是觀察是否跌破後延續。\n\n"
        "盤中判斷：\n"
        "● 空單不追\n"
        "● 觀察是否假跌破\n"
        "● 反彈不過才看續弱\n\n"
        f"📍 現價：{format_price(price)}\n"
        f"📍 S1：{format_price(s1)}\n"
        f"📍 中軸：{format_price(flip)}"
    )


def build_long_confirm_v3_message(context: dict) -> str:
    """
    V3 多方確認訊號。
    """

    price = context.get("price")
    flip = context.get("flip")
    pivot = get_context_value(context, "pivot")
    r1 = get_context_value(context, "r1", "R1")

    return (
        "🟢 V3 多方確認訊號\n"
        "------------------\n\n"
        "價格站回中軸後出現延續確認。\n"
        "此訊號目前只列為 A- 觀察，不代表自動進場。\n\n"
        "盤中判斷：\n"
        "● 僅允許微台小倉觀察\n"
        "● 不可追價放大部位\n"
        "● 停損以 30 點為主\n\n"
        f"📍 現價：{format_price(price)}\n"
        f"📍 中軸：{format_price(flip)}\n"
        f"📍 Pivot：{format_price(pivot)}\n"
        f"📍 R1：{format_price(r1)}"
    )


def build_short_retest_fail_v3_message(context: dict) -> str:
    """
    V3 空方回測失敗訊號。
    """

    price = context.get("price")
    flip = context.get("flip")
    pivot = get_context_value(context, "pivot")
    s1 = get_context_value(context, "s1", "S1")

    return (
        "🔴 V3 空方回測失敗訊號\n"
        "------------------\n\n"
        "價格跌破中軸後反彈無法收回。\n"
        "此訊號目前只列為 A- 觀察，不代表自動進場。\n\n"
        "盤中判斷：\n"
        "● 僅允許微台小倉觀察\n"
        "● 不可急跌追空\n"
        "● 停損以 30 點為主\n\n"
        f"📍 現價：{format_price(price)}\n"
        f"📍 中軸：{format_price(flip)}\n"
        f"📍 Pivot：{format_price(pivot)}\n"
        f"📍 S1：{format_price(s1)}"
    )


# --------------------------------------------------
# Main Alert Sender
# --------------------------------------------------

def build_base_alert_message(context: dict) -> str | None:
    """
    依事件建立主警報文字。
    """

    event = normalize_event(context.get("event"))

    if event == "LONG_TRAP":
        return build_long_trap_message(context)

    if event == "SHORT_TRAP":
        return build_short_trap_message(context)

    if event == "FLIP_INVALID":
        return build_flip_invalid_message(context)

    if event == "SWEEP":
        return build_sweep_message(context)

    if event == "FLIP_BREAK":
        return build_flip_break_message(context)

    if event == "FLIP_RECOVER":
        return build_flip_recover_message(context)

    if event == "R1_TOUCH":
        return build_r1_touch_message(context)

    if event == "S1_TOUCH":
        return build_s1_touch_message(context)

    if event == "LONG_CONFIRM_V3":
        return build_long_confirm_v3_message(context)

    if event == "SHORT_RETEST_FAIL_V3":
        return build_short_retest_fail_v3_message(context)

    return None


def send_human_alert(context: dict):
    """
    發送盤中人話警報 + 早盤劇本對應 + Strategy Filter + AI 即時建議 + 警報紀錄。
    """

    if context is None:
        return False

    context = dict(context)

    # 先做 Strategy Filter 分級，讓後續 message / alert_log 都能讀到分級欄位
    if enrich_context_with_strategy_filter is not None:
        try:
            context = enrich_context_with_strategy_filter(context)
        except Exception as e:
            print(f"⚠️ strategy filter context enrich failed: {e}")

    event = normalize_event(context.get("event"))
    alert_key = build_alert_key(context)

    if not can_send(event, alert_key):
        return False

    base_msg = build_base_alert_message(context)

    if not base_msg:
        return False

    scenario_text = build_scenario_section(context)
    strategy_filter_text = build_strategy_filter_text(context)
    ai_advice = build_ai_advice_section(context)

    sections = [base_msg]

    if scenario_text:
        sections.append(scenario_text)

    if strategy_filter_text:
        sections.append(strategy_filter_text)

    sections.append(ai_advice)

    if claude_advise is not None:
        try:
            claude_instruction = claude_advise(context)
            if claude_instruction:
                sections.append(claude_instruction)
        except Exception as e:
            print(f"⚠️ claude_advisor 失敗：{e}")

    msg = "\n\n====================\n\n".join(sections)

    success = send_to_telegram(msg)

    if success and record_alert is not None:
        try:
            record_alert(
                context=context,
                message=msg,
            )
        except Exception as e:
            print(f"⚠️ alert log 寫入失敗：{e}")

    return success


# --------------------------------------------------
# Manual Test
# --------------------------------------------------

if __name__ == "__main__":
    test_context = {
        "event": "FLIP_RECOVER",
        "price": 42380,
        "flip": 42359,
        "pivot": 42150,
        "r1": 42586,
        "s1": 41922,
        "sentiment": "🔴 強空，外資期貨淨空 -51068 口",
        "behavior": "NO_TRAP",
        "trap": None,
        "sweep": None,
        "is_realtime": True,
        "stop": 42320,
        "target": 42586,
        "tick_source": "FINMIND_FUTURES_SNAPSHOT",
        "tick_time": "09:15:00",
        "latest_k_time": "2026-05-15 09:15:00",
        "data_delay_minutes": 0.5,
    }

    print("manual test only; not sending alert")
    print(build_base_alert_message(test_context))