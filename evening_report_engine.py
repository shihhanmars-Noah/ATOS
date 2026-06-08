# evening_report_engine.py

import json
from datetime import datetime, date

from persistent_state import load_state
from messenger import send_to_telegram
from alert_log_engine import summarize_today_alerts, build_alert_log_text

try:
    from chip_data_engine import build_chip_context as _build_chip_ctx
except Exception:
    _build_chip_ctx = None


# --------------------------------------------------
# Basic Helpers
# --------------------------------------------------

def format_price(value):
    if value is None:
        return "N/A"

    try:
        return round(float(value), 1)
    except Exception:
        return value


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def normalize_date_str(value):
    """
    將各種日期格式轉成 YYYY-MM-DD。
    """

    if value is None:
        return None

    try:
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d")

        if isinstance(value, date):
            return value.strftime("%Y-%m-%d")

        text = str(value).strip()

        if not text:
            return None

        # 若是 '2026-05-15 13:40:00'
        if len(text) >= 10:
            return text[:10]

        return text

    except Exception:
        return None


def is_today(value) -> bool:
    return normalize_date_str(value) == today_str()


def get_first_existing(state: dict, keys: list, default=None):
    for key in keys:
        value = state.get(key)

        if value is not None:
            return value

    return default


def is_valid_value(value) -> bool:
    if value is None:
        return False

    if isinstance(value, str):
        if value.strip() == "":
            return False

        if value.strip().upper() in ["N/A", "NONE", "NULL", "UNKNOWN"]:
            return False

    return True


def is_valid_number(value) -> bool:
    try:
        if value is None:
            return False

        float(value)
        return True

    except Exception:
        return False


# --------------------------------------------------
# Data Readiness Gate
# --------------------------------------------------

def get_day_session_context(state: dict) -> dict:
    """
    從 state 整理日盤價格資料。

    注意：
    - 晚盤報告要檢討「日盤」，所以優先讀 day_session_* 欄位。
    - 避免 15:00 後夜盤 / 盤後 snapshot 覆蓋 state["price"] / state["latest_k_time"]。
    - 關鍵價位優先使用 preopen_plan["key_levels"]，用早上原始劇本做複盤。
    """

    levels = state.get("levels", {}) or {}

    preopen_plan = state.get("preopen_plan", {}) or {}
    preopen_key_levels = {}

    if isinstance(preopen_plan, dict):
        preopen_key_levels = preopen_plan.get("key_levels", {}) or {}

    # 日盤專用欄位優先，避免被夜盤 / 盤後資料覆蓋
    price = (
        state.get("day_session_close")
        or state.get("day_close")
        or state.get("session_close")
        or state.get("price")
    )

    latest_k_time = (
        state.get("day_session_last_k_time")
        or state.get("day_last_k_time")
        or state.get("session_last_k_time")
        or state.get("latest_k_time")
    )

    # 關鍵價位優先讀盤前劇本，因為晚盤要檢討「早上給出的價位是否有效」
    flip = (
        preopen_key_levels.get("flip")
        or preopen_key_levels.get("Flip")
        or state.get("flip")
        or levels.get("flip")
        or levels.get("Flip")
    )

    pivot = (
        preopen_key_levels.get("pivot")
        or preopen_key_levels.get("Pivot")
        or preopen_key_levels.get("PIVOT")
        or state.get("pivot")
        or levels.get("pivot")
        or levels.get("Pivot")
        or levels.get("PIVOT")
    )

    r1 = (
        preopen_key_levels.get("r1")
        or preopen_key_levels.get("R1")
        or state.get("r1")
        or state.get("R1")
        or levels.get("R1")
        or levels.get("r1")
    )

    s1 = (
        preopen_key_levels.get("s1")
        or preopen_key_levels.get("S1")
        or state.get("s1")
        or state.get("S1")
        or levels.get("S1")
        or levels.get("s1")
    )

    today_high = get_first_existing(
        state,
        [
            "day_session_high",
            "today_high",
            "day_high",
            "session_high",
            "high",
        ],
    )

    today_low = get_first_existing(
        state,
        [
            "day_session_low",
            "today_low",
            "day_low",
            "session_low",
            "low",
        ],
    )

    tick_source = (
        state.get("day_session_source")
        or state.get("tick_source")
        or "FINMIND_TXF_SNAPSHOT_5MIN"
    )

    data_delay = (
        state.get("day_session_data_delay_minutes")
        or state.get("data_delay_minutes")
        or "N/A"
    )

    return {
        "price": price,
        "flip": flip,
        "pivot": pivot,
        "r1": r1,
        "s1": s1,
        "today_high": today_high,
        "today_low": today_low,
        "latest_k_time": latest_k_time,
        "tick_source": tick_source,
        "data_delay": data_delay,
    }

def get_chip_context(state: dict) -> dict:
    """
    讀取當日法人 / 期貨籌碼狀態。

    建議未來資料引擎寫入任一組：
    - state["chip_ready"] = True
    - state["chip_date"] = "YYYY-MM-DD"
    - state["institutional_sentiment"] = "..."
    - state["foreign_futures_net"] = -51068

    或：
    - state["today_chip"] = {...}
    """

    chip_ready_flag = state.get("chip_ready")
    chip_date = get_first_existing(
        state,
        [
            "chip_date",
            "institutional_date",
            "futures_chip_date",
            "foreign_futures_date",
        ],
    )

    sentiment = (
        state.get("sentiment")
        or state.get("institutional_sentiment")
        or state.get("chip_sentiment")
    )

    foreign_futures_net = get_first_existing(
        state,
        [
            "foreign_futures_net",
            "foreign_net_futures",
            "foreign_future_oi",
            "foreign_futures_position",
        ],
    )

    today_chip = state.get("today_chip")

    ready = False

    if chip_ready_flag is True:
        ready = True

    if isinstance(today_chip, dict) and today_chip:
        ready = True

    if is_valid_value(sentiment) and chip_date and is_today(chip_date):
        ready = True

    if is_valid_number(foreign_futures_net) and chip_date and is_today(chip_date):
        ready = True

    return {
        "ready": ready,
        "date": normalize_date_str(chip_date),
        "sentiment": sentiment,
        "foreign_futures_net": foreign_futures_net,
        "raw": today_chip,
    }


def get_option_oi_context(state: dict) -> dict:
    """
    讀取當日選擇權 OI 狀態。

    建議未來資料引擎寫入任一組：
    - state["option_oi_ready"] = True
    - state["option_oi_date"] = "YYYY-MM-DD"
    - state["option_oi_summary"] = {...}

    或：
    - state["today_option_oi"] = {...}
    - state["oi_pressure_support"] = {...}
    """

    oi_ready_flag = state.get("option_oi_ready") or state.get("oi_ready")

    oi_date = get_first_existing(
        state,
        [
            "option_oi_date",
            "oi_date",
            "today_oi_date",
            "option_chain_date",
        ],
    )

    option_oi_summary = get_first_existing(
        state,
        [
            "option_oi_summary",
            "today_option_oi",
            "oi_summary",
            "option_oi",
            "oi_pressure_support",
        ],
    )

    max_call_oi = get_first_existing(
        state,
        [
            "max_call_oi",
            "call_wall",
            "call_pressure",
            "max_call_strike",
        ],
    )

    max_put_oi = get_first_existing(
        state,
        [
            "max_put_oi",
            "put_wall",
            "put_support",
            "max_put_strike",
        ],
    )

    # 優先從 chip_cache.json 讀取 call_wall / put_wall，確保晚盤數字與盤前一致
    try:
        with open('chip_cache.json') as f:
            _chip_cache = json.load(f)
        _oi = _chip_cache.get('option_oi', {})
        _cw = _oi.get('call_wall_strike')
        _pw = _oi.get('put_wall_strike')
        if _cw is not None:
            max_call_oi = _cw
        if _pw is not None:
            max_put_oi = _pw
    except Exception:
        pass

    ready = False

    if oi_ready_flag is True:
        ready = True

    if isinstance(option_oi_summary, dict) and option_oi_summary:
        if oi_date is None or is_today(oi_date):
            ready = True

    if is_valid_value(max_call_oi) and is_valid_value(max_put_oi):
        if oi_date is None or is_today(oi_date):
            ready = True

    # chip_cache 有值時直接視為 ready（它是今日籌碼的最新來源）
    if is_valid_value(max_call_oi) and is_valid_value(max_put_oi):
        ready = True

    return {
        "ready": ready,
        "date": normalize_date_str(oi_date),
        "summary": option_oi_summary,
        "max_call_oi": max_call_oi,
        "max_put_oi": max_put_oi,
    }


def build_option_oi_text(oi_ctx: dict) -> str:
    """
    將選擇權 OI summary 轉成晚報可讀格式。

    顯示邏輯：
    - 優先顯示現價附近有效 Call / Put OI
    - 若 data_engine 有寫入全市場最大 OI，額外顯示全市場極端位置
    - 避免直接印出 dict
    """

    summary = oi_ctx.get("summary")

    if not isinstance(summary, dict):
        return (
            f"Call 壓力：{oi_ctx.get('max_call_oi', 'N/A')}\n"
            f"Put 支撐：{oi_ctx.get('max_put_oi', 'N/A')}"
        )

    # --------------------------------------------------
    # 近價有效 OI：目前 data_engine.py 會把這組寫在 call_pressure / put_support
    # --------------------------------------------------

    nearby_call_pressure = (
        summary.get("call_pressure")
        or summary.get("nearby_call_pressure")
        or summary.get("near_call_strike")
        or summary.get("call_wall")
    )

    nearby_put_support = (
        summary.get("put_support")
        or summary.get("nearby_put_support")
        or summary.get("near_put_strike")
        or summary.get("put_wall")
    )

    nearby_call_oi = (
        summary.get("nearby_call_oi")
        or summary.get("near_call_oi")
        or summary.get("call_pressure_oi")
        or summary.get("max_call_oi")
    )

    nearby_put_oi = (
        summary.get("nearby_put_oi")
        or summary.get("near_put_oi")
        or summary.get("put_support_oi")
        or summary.get("max_put_oi")
    )

    # --------------------------------------------------
    # 全市場最大 OI：作為極端籌碼牆參考，不取代近價支撐壓力
    # --------------------------------------------------

    global_call_strike = (
        summary.get("global_max_call_strike")
        or summary.get("all_market_max_call_strike")
        or summary.get("absolute_max_call_strike")
    )

    global_put_strike = (
        summary.get("global_max_put_strike")
        or summary.get("all_market_max_put_strike")
        or summary.get("absolute_max_put_strike")
    )

    global_call_oi = (
        summary.get("global_max_call_oi")
        or summary.get("all_market_max_call_oi")
        or summary.get("absolute_max_call_oi")
    )

    global_put_oi = (
        summary.get("global_max_put_oi")
        or summary.get("all_market_max_put_oi")
        or summary.get("absolute_max_put_oi")
    )

    contract_date = summary.get("contract_date") or summary.get("contract")
    source = summary.get("source") or "N/A"

    reference_price = (
        summary.get("reference_price")
        or summary.get("near_price")
        or summary.get("ref_price")
    )

    nearby_window = (
        summary.get("nearby_window_points")
        or summary.get("near_window_points")
        or summary.get("window_points")
    )

    call_mode = summary.get("call_pressure_mode")
    put_mode = summary.get("put_support_mode")

    lines = [
        f"近價 Call 壓力：{format_price(nearby_call_pressure)}｜OI：{nearby_call_oi if nearby_call_oi is not None else 'N/A'}",
        f"近價 Put 支撐：{format_price(nearby_put_support)}｜OI：{nearby_put_oi if nearby_put_oi is not None else 'N/A'}",
    ]

    if reference_price is not None:
        lines.append(f"參考現價：{format_price(reference_price)}")

    if nearby_window is not None:
        lines.append(f"近價篩選：現價上下 {nearby_window} 點內")

    if call_mode or put_mode:
        lines.append(
            "近價模式："
            f"Call={call_mode or 'N/A'}｜Put={put_mode or 'N/A'}"
        )

    if global_call_strike is not None or global_put_strike is not None:
        lines.append("")
        lines.append("全市場最大 OI：")

        lines.append(
            f"全市場最大 Call：{format_price(global_call_strike)}｜OI："
            f"{global_call_oi if global_call_oi is not None else 'N/A'}"
        )

        lines.append(
            f"全市場最大 Put：{format_price(global_put_strike)}｜OI："
            f"{global_put_oi if global_put_oi is not None else 'N/A'}"
        )

    if contract_date:
        lines.append(f"合約月份：{contract_date}")

    lines.append(f"資料來源：{source}")

    return "\n".join(lines)


def get_preopen_plan_context(state: dict) -> dict:
    """
    讀取盤前劇本。

    建議未來盤前報告產生時寫入 state：
    - state["preopen_plan_ready"] = True
    - state["preopen_plan_date"] = "YYYY-MM-DD"
    - state["preopen_plan"] = {...}

    或至少：
    - state["morning_scenario"]
    - state["preopen_bias"]
    """

    ready_flag = state.get("preopen_plan_ready")

    plan_date = get_first_existing(
        state,
        [
            "preopen_plan_date",
            "morning_plan_date",
            "scenario_date",
            "preopen_report_date",
        ],
    )

    plan = get_first_existing(
        state,
        [
            "preopen_plan",
            "morning_plan",
            "morning_scenario",
            "preopen_scenario",
            "scenario",
        ],
    )

    preopen_bias = get_first_existing(
        state,
        [
            "preopen_bias",
            "morning_bias",
            "expected_trend",
            "day_bias",
        ],
    )

    ready = False

    if ready_flag is True:
        ready = True

    if isinstance(plan, dict) and plan:
        if plan_date is None or is_today(plan_date):
            ready = True

    if is_valid_value(preopen_bias):
        if plan_date is None or is_today(plan_date):
            ready = True

    return {
        "ready": ready,
        "date": normalize_date_str(plan_date),
        "plan": plan,
        "bias": preopen_bias,
    }


def get_alert_context(summary: dict) -> dict:
    """
    檢查盤中警報紀錄。

    注意：
    - 0 則警報不代表資料不足
    - 只要 alert_log_engine 可以正常回傳 summary，就視為已取得
    - 晚報第六段會顯示「今日盤中警報：0 則」
    """

    if not isinstance(summary, dict):
        return {
            "ready": False,
            "total": 0,
        }

    total = int(summary.get("total", 0) or 0)

    return {
        "ready": True,
        "total": total,
    }

def check_evening_report_readiness(state: dict, summary: dict) -> dict:
    """
    晚盤報告資料完整性檢查。

    資料不足：
    - 不發 Telegram
    - 只在終端機 print 缺少資料
    """

    missing = []

    day_ctx = get_day_session_context(state)
    chip_ctx = get_chip_context(state)
    oi_ctx = get_option_oi_context(state)
    preopen_ctx = get_preopen_plan_context(state)
    alert_ctx = get_alert_context(summary)

    # 1. 日盤價格 / 5分K
    if not is_valid_number(day_ctx.get("price")):
        missing.append("日盤收盤參考價")

    if not is_valid_value(day_ctx.get("latest_k_time")):
        missing.append("日盤最後 5分K 時間")

    # 2. 今日高低點
    if not is_valid_number(day_ctx.get("today_high")):
        missing.append("今日實際高點")

    if not is_valid_number(day_ctx.get("today_low")):
        missing.append("今日實際低點")

    # 3. 關鍵價位
    if not is_valid_number(day_ctx.get("flip")):
        missing.append("多空中軸")

    if not is_valid_number(day_ctx.get("pivot")):
        missing.append("Pivot 盤中重心")

    if not is_valid_number(day_ctx.get("r1")):
        missing.append("R1 上方壓力")

    if not is_valid_number(day_ctx.get("s1")):
        missing.append("S1 下方支撐")

    # 4. 當日籌碼
    if not chip_ctx.get("ready"):
        missing.append("當日法人 / 期貨籌碼")

    # 5. 當日選擇權 OI
    if not oi_ctx.get("ready"):
        missing.append("當日選擇權 OI")

    # 6. 盤前劇本
    if not preopen_ctx.get("ready"):
        missing.append("盤前劇本 / 盤前方向")

    # 7. 盤中警報紀錄
    if not alert_ctx.get("ready"):
        missing.append("今日盤中警報紀錄")

    return {
        "ready": len(missing) == 0,
        "missing": missing,
        "day": day_ctx,
        "chip": chip_ctx,
        "oi": oi_ctx,
        "preopen": preopen_ctx,
        "alert": alert_ctx,
    }


# --------------------------------------------------
# Review Logic
# --------------------------------------------------

def classify_day_result(day_ctx: dict) -> str:
    """
    根據日盤高低收與關鍵價位做簡單日盤型態判斷。
    """

    price = day_ctx.get("price")
    high = day_ctx.get("today_high")
    low = day_ctx.get("today_low")
    flip = day_ctx.get("flip")
    pivot = day_ctx.get("pivot")
    r1 = day_ctx.get("r1")
    s1 = day_ctx.get("s1")

    try:
        price = float(price)
        high = float(high)
        low = float(low)
        flip = float(flip)
        pivot = float(pivot)
        r1 = float(r1)
        s1 = float(s1)
    except Exception:
        return "日盤型態資料不足"

    if high >= r1 and price < flip:
        return "上攻壓力後轉弱，偏向開高走低 / 假突破結構"

    if low <= s1 and price > flip:
        return "下探支撐後收回，偏向開低走高 / 假跌破結構"

    if price < s1:
        return "收盤跌破 S1，日盤空方明顯主控"

    if price > r1:
        return "收盤站上 R1，日盤多方明顯主控"

    if price < flip:
        return "收盤低於中軸，日盤偏空"

    if price > flip:
        return "收盤高於中軸，日盤偏多"

    if low < pivot < high:
        return "價格圍繞 Pivot 震盪，日盤偏區間盤"

    return "日盤結構中性"


def evaluate_preopen_plan(preopen_ctx: dict, day_ctx: dict, chip_ctx: dict, oi_ctx: dict) -> str:
    """
    檢討盤前劇本是否被日盤結果驗證。

    這裡先做保守版文字判斷。
    後續若 preopen_plan 結構固定，可再做更精準的 score。
    """

    day_result = classify_day_result(day_ctx)
    bias = preopen_ctx.get("bias")
    plan = preopen_ctx.get("plan")

    lines = []

    lines.append("盤前劇本檢討：")

    if is_valid_value(bias):
        lines.append(f"● 盤前方向：{bias}")
    elif isinstance(plan, dict):
        plan_text = (
            plan.get("bias")
            or plan.get("direction")
            or plan.get("scenario")
            or plan.get("summary")
            or "已有盤前劇本，但未提供方向欄位"
        )
        lines.append(f"● 盤前劇本：{plan_text}")
    else:
        lines.append("● 盤前劇本：已取得，但格式未標準化")

    lines.append(f"● 日盤結果：{day_result}")

    chip_text = chip_ctx.get("sentiment")
    if is_valid_value(chip_text):
        lines.append(f"● 籌碼驗證：{chip_text}")
    elif is_valid_number(chip_ctx.get("foreign_futures_net")):
        lines.append(f"● 外資期貨淨部位：{chip_ctx.get('foreign_futures_net')}")
    else:
        lines.append("● 籌碼驗證：已取得當日籌碼，但摘要欄位未標準化")

    if isinstance(oi_ctx.get("summary"), dict):
        lines.append("● OI 驗證：已取得當日選擇權 OI，允許納入壓力支撐檢討")
    else:
        lines.append("● OI 驗證：已取得當日 OI 基礎欄位")

    return "\n".join(lines)


def build_level_review(day_ctx: dict, call_wall=None, put_wall=None) -> str:
    """
    檢討 Call wall / Put wall / 中軸是否有效。
    """

    price = day_ctx.get("price")
    high = day_ctx.get("today_high")
    low = day_ctx.get("today_low")
    flip = day_ctx.get("flip")
    pivot = day_ctx.get("pivot")

    try:
        price = float(price)
        high = float(high)
        low = float(low)
        flip = float(flip)
        pivot = float(pivot)
    except Exception:
        return "價位驗證資料不足。"

    lines = []
    lines.append("價位驗證：")

    if call_wall is not None:
        try:
            cw = float(call_wall)
            if high >= cw:
                if price >= cw:
                    lines.append(f"● Call wall {format_price(cw)}：突破且收盤守住，空頭防線失守。")
                else:
                    lines.append(f"● Call wall {format_price(cw)}：盤中觸及後未守住，壓力仍有效。")
            else:
                lines.append(f"● Call wall {format_price(cw)}：日盤未觸及，壓力區間完整。")
        except Exception:
            pass

    if put_wall is not None:
        try:
            pw = float(put_wall)
            if low <= pw:
                if price <= pw:
                    lines.append(f"● Put wall {format_price(pw)}：跌破且收盤未收回，多頭防線失守。")
                else:
                    lines.append(f"● Put wall {format_price(pw)}：盤中跌破後收回，支撐有效。")
            else:
                lines.append(f"● Put wall {format_price(pw)}：日盤未跌破，支撐區間完整。")
        except Exception:
            pass

    if low <= flip <= high:
        if price < flip:
            lines.append(f"● 中軸 {format_price(flip)}：穿越後收低，區間內偏空。")
        else:
            lines.append(f"● 中軸 {format_price(flip)}：穿越後收高，區間內偏多。")
    else:
        if price < flip:
            lines.append(f"● 中軸 {format_price(flip)}：全日未站上，偏空。")
        else:
            lines.append(f"● 中軸 {format_price(flip)}：全日守穩，偏多。")

    if low <= pivot <= high:
        lines.append(f"● Pivot {format_price(pivot)}：落入日內波動區間，今日重心驗證價。")
    else:
        lines.append(f"● Pivot {format_price(pivot)}：未落入主要波動區間，參考性降低。")

    return "\n".join(lines)


def build_alert_review(summary: dict) -> str:
    """
    檢討今日盤中警報是否具有策略意義。
    """

    lines = []

    lines.append("警報驗證：")
    lines.append(f"● 今日盤中警報：{summary.get('total', 0)} 則")

    if summary.get("has_flip_invalid"):
        lines.append("● Flip Invalid：代表盤中原劇本曾被破壞，晚盤不能沿用單一方向。")

    if summary.get("has_long_trap"):
        lines.append("● Long Trap：多方假突破出現，追多風險提高。")

    if summary.get("has_short_trap"):
        lines.append("● Short Trap：空方假跌破出現，追空風險提高。")

    if summary.get("has_sweep"):
        lines.append("● Sweep：關鍵價附近有清洗行為，需等待 5分K 收盤確認。")

    if summary.get("has_flip_break") and not summary.get("has_flip_recover"):
        lines.append("● Flip Break 後未收復：偏空訊號較有效。")

    if summary.get("has_flip_recover") and not summary.get("has_flip_break"):
        lines.append("● Flip Recover 後未再跌破：偏多訊號較有效。")

    if (
        summary.get("has_flip_break")
        and summary.get("has_flip_recover")
    ):
        lines.append("● Flip Break / Recover 同日出現：代表區間震盪，不適合追方向。")

    return "\n".join(lines)


def build_evening_conclusion(
    summary: dict,
    state: dict,
    readiness: dict,
    call_wall=None,
    put_wall=None,
) -> str:
    """
    根據今日警報、Put wall / Call wall 狀態產生晚盤結論。
    """

    day_ctx = readiness["day"]
    chip_ctx = readiness["chip"]

    day_result = classify_day_result(day_ctx)
    chip_text = chip_ctx.get("sentiment") or ""

    price = day_ctx.get("price")
    flip = day_ctx.get("flip")
    today_low = day_ctx.get("today_low")
    today_high = day_ctx.get("today_high")

    cw_str = format_price(call_wall) if call_wall else "Call wall"
    pw_str = format_price(put_wall) if put_wall else "Put wall"

    # Put wall 跌破且未收回 → 最佳空方機會
    try:
        if put_wall and float(today_low) <= float(put_wall) and float(price) <= float(put_wall):
            return (
                f"日盤結果：{day_result}。\n"
                f"Put wall {pw_str} 跌破且收盤未收回，大戶多頭防線失守。"
                f"夜盤以反彈不過 {pw_str} 為空方延續訊號，不追空，等確認。"
            )
    except Exception:
        pass

    # Call wall 突破且守住 → 多方延伸
    try:
        if call_wall and float(today_high) >= float(call_wall) and float(price) >= float(call_wall):
            return (
                f"日盤結果：{day_result}。\n"
                f"Call wall {cw_str} 突破且守住，大戶空頭防線失守。"
                f"夜盤偏多觀察，但不追高，等回測 {cw_str} 不破再加倉。"
            )
    except Exception:
        pass

    if int(summary.get("total", 0) or 0) == 0:
        return (
            f"日盤結果：{day_result}。\n"
            f"未觸發主要警報，價格在 {pw_str}～{cw_str} 區間內震盪。"
            f"夜盤以中軸 {format_price(flip)} 作為區間內參考，無明確訊號不做方向單。"
        )

    if summary.get("has_long_trap"):
        return (
            f"日盤結果：{day_result}。\n"
            f"出現多方陷阱，Call wall {cw_str} 附近有假突破風險。"
            "夜盤若再拉高，等5分K確認守住再評估，不追第一波。"
        )

    if summary.get("has_short_trap"):
        return (
            f"日盤結果：{day_result}。\n"
            f"出現空方陷阱，Put wall {pw_str} 附近有假跌破風險。"
            "夜盤若再急跌，等5分K確認收低再評估，不追空。"
        )

    if "強空" in str(chip_text):
        try:
            if float(price) < float(flip):
                return (
                    f"日盤結果：{day_result}。\n"
                    f"籌碼偏空且收盤低於中軸，夜盤以反彈不過中軸 {format_price(flip)} 為空方機會。"
                    f"關鍵觀察：Put wall {pw_str} 是否跌破，跌破才是加速訊號。"
                )
        except Exception:
            pass

    return (
        f"日盤結果：{day_result}。\n"
        f"價格在 {pw_str}～{cw_str} 區間內，無明確方向訊號。"
        f"夜盤中軸 {format_price(flip)} 為區間參考，突破 {cw_str} 或跌破 {pw_str} 才做方向。"
    )


# --------------------------------------------------
# AI Fallback（AI 無回應時的靜態摘要）
# --------------------------------------------------

def _build_ai_fallback(
    call_wall_status: str = "",
    put_wall_status: str = "",
    pivot_status: str = "",
    chip_ctx: dict | None = None,
    today_high=None,
    today_low=None,
    price=None,
) -> str:
    """
    Gemini 503/無回應時，依結構數據自動組出夜盤摘要。
    不做預測，只陳述已知結構與關鍵注意點。
    """
    ctx = chip_ctx or {}
    fn = ctx.get("foreign_net", 0) or 0
    fn_1d = ctx.get("foreign_net_chg_1d", 0) or 0
    spot_val = ctx.get("spot_foreign_net_buy_bn") or 0
    call_wall = ctx.get("call_wall")
    put_wall  = ctx.get("put_wall")
    cp_ratio  = ctx.get("call_put_ratio")

    parts = []

    # 假突破訊號
    if call_wall_status and "假突破" in call_wall_status:
        parts.append(f"Call wall {_fp(call_wall)} 今日出現假突破，上方賣壓仍重")

    # 現貨外資異動
    try:
        spot_f = float(spot_val)
        if spot_f < -200:
            parts.append(f"現貨外資大賣 {abs(spot_f):.1f} 億，機構資金持續撤離")
        elif spot_f < 0:
            parts.append(f"現貨外資賣超 {abs(spot_f):.1f} 億")
    except Exception:
        pass

    # 期貨籌碼
    try:
        if fn_1d > 0:
            parts.append(f"期貨外資今日小幅回補 {abs(int(fn_1d)):,} 口，淨部位仍空")
        elif fn_1d < 0:
            parts.append(f"期貨外資今日加碼空單 {abs(int(fn_1d)):,} 口")
    except Exception:
        pass

    # Pivot 位置
    if pivot_status and "站上" in pivot_status:
        parts.append("收盤雖守 Pivot，結構矛盾未解，夜盤需觀察方向確認")
    elif pivot_status and "跌破" in pivot_status:
        parts.append("收盤跌破 Pivot，空方佔優，夜盤反彈需謹慎")

    # C/P Ratio
    try:
        if cp_ratio and float(cp_ratio) < 0.5:
            parts.append(f"C/P Ratio {float(cp_ratio):.2f} 低於 0.5，留意潛在軋空震盪")
    except Exception:
        pass

    if not parts:
        return "今日結構數據不足，建議夜盤觀望為主，等日盤開盤確認方向。"

    return "（AI 服務暫時中斷，以下為系統自動摘要）\n" + "；".join(parts) + "。夜盤以觀察為主。"


# --------------------------------------------------
# Report Builder
# --------------------------------------------------

def _fp(v) -> str:
    """台指期價位格式化：強制整數，無小數點。"""
    if v is None:
        return "N/A"
    try:
        return str(int(round(float(v))))
    except Exception:
        return "N/A"


def _build_wall_status(today_high, today_low, price_f, wall_f, wall_type: str) -> str:
    """產生 Call/Put wall 狀態描述（假突破判斷）。"""
    try:
        h = float(today_high)
        l = float(today_low)
        p = float(price_f)
        w = float(wall_f)
        if wall_type == "call":
            if h > w and p < w:
                return f"Call wall {int(w)}：假突破，賣方護盤成功（空方訊號）"
            elif h > w and p >= w:
                return f"Call wall {int(w)}：有效突破，多方主控"
            else:
                return f"Call wall {int(w)}：未觸及"
        else:  # put
            if l < w and p > w:
                return f"Put wall {int(w)}：假跌破，買方護盤成功（多方訊號）"
            elif l < w and p <= w:
                return f"Put wall {int(w)}：有效跌破，空方主控"
            else:
                return f"Put wall {int(w)}：未觸及"
    except Exception:
        return "N/A"


def _build_pivot_status(price_f, active_pivot_f) -> str:
    try:
        if float(price_f) > float(active_pivot_f):
            return f"Pivot {int(active_pivot_f)}：收盤站上，偏多"
        else:
            return f"Pivot {int(active_pivot_f)}：收盤跌破，偏空"
    except Exception:
        return "N/A"


def build_evening_report_message(readiness: dict | None = None) -> str | None:
    """
    建立新版決策工具格式晚盤報告。
    """

    state = load_state()
    summary = summarize_today_alerts()

    if readiness is None:
        readiness = check_evening_report_readiness(state, summary)

    day_ctx = readiness.get("day") or get_day_session_context(state)

    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")

    price = day_ctx.get("price")
    active_pivot = state.get("active_pivot") or day_ctx.get("pivot")
    r1 = day_ctx.get("r1")
    s1 = day_ctx.get("s1")
    today_high = day_ctx.get("today_high")
    today_low = day_ctx.get("today_low")

    # 完整籌碼
    chip_ctx = {}
    if _build_chip_ctx is not None:
        try:
            chip_ctx = _build_chip_ctx() or {}
        except Exception:
            pass

    call_wall = chip_ctx.get("call_wall") or state.get("call_wall")
    put_wall  = chip_ctx.get("put_wall")  or state.get("put_wall")

    fn = chip_ctx.get("foreign_net", 0)
    fn_level = chip_ctx.get("foreign_net_level", "N/A")
    fn_1d = chip_ctx.get("foreign_net_chg_1d", 0)
    spot_val = chip_ctx.get("spot_foreign_net_buy_bn") or 0
    spot_dir = "買超" if float(spot_val) > 0 else "賣超"

    # 前期籌碼（回補還是加碼）
    # chg_arrow：箭頭格式，用於「今天發生了什麼」（單括號，清晰顯示軌跡）
    # chg_str  ：逗號格式，用於「籌碼驗證」（套在外層括號內，避免嵌套）
    try:
        prev_net = fn - int(fn_1d)
        if fn_1d > 0:
            chg_arrow = f"回補 +{abs(fn_1d):,}口（{prev_net:+,} → {fn:+,}）"
            chg_str   = f"回補 +{abs(fn_1d):,}口，前值 {prev_net:+,}"
        else:
            chg_arrow = f"加碼 -{abs(fn_1d):,}口（{prev_net:+,} → {fn:+,}）"
            chg_str   = f"加碼 -{abs(fn_1d):,}口，前值 {prev_net:+,}"
    except Exception:
        chg_arrow = f"變動 {fn_1d:+,}口"
        chg_str   = chg_arrow

    # 假突破判斷
    try:
        price_f = float(price)
    except Exception:
        price_f = 0

    if call_wall:
        call_wall_status = _build_wall_status(today_high, today_low, price_f, call_wall, "call")
    else:
        call_wall_status = "Call wall N/A"

    if put_wall:
        put_wall_status = _build_wall_status(today_high, today_low, price_f, put_wall, "put")
    else:
        put_wall_status = "Put wall N/A"

    # Call wall 是否出現假突破（影響 pivot_status 文字與做空次條件）
    call_wall_fakeout = False
    try:
        if float(today_high) > float(call_wall) and price_f < float(call_wall):
            call_wall_fakeout = True
    except Exception:
        pass

    # Pivot 狀態：假突破當日不寫「偏多」，改為「撐壓待夜盤驗證」
    if call_wall_fakeout:
        try:
            pivot_status = f"Pivot {int(float(active_pivot))}：收盤站上，但 Call wall 假突破壓頂，撐壓待夜盤驗證"
        except Exception:
            pivot_status = _build_pivot_status(price_f, active_pivot)
    else:
        pivot_status = _build_pivot_status(price_f, active_pivot)

    # 做空次條件：動態考慮假突破 + 今日高點上影線
    try:
        ap_f = float(active_pivot)
        cw_f = float(call_wall)
        hi_f = float(today_high)
        if price_f >= ap_f:
            if call_wall_fakeout and (hi_f - price_f) > 200:
                # 假突破 + 長上影 → 夜盤反彈到高點/Call wall是更好的放空點
                short_sub = (
                    f"1. 夜盤反彈至今日高點 {_fp(hi_f)} / Call wall {_fp(cw_f)} 附近量縮轉弱確認\n"
                    f"  2. 跌回 Pivot {_fp(ap_f)} 下方且回測無法站回確認"
                )
            else:
                short_sub = f"跌回 Pivot {_fp(ap_f)} 下方且無法站回確認"
        else:
            short_sub = f"反彈到今日高點 {_fp(hi_f)} 附近未過確認"
    except Exception:
        short_sub = "參考 Pivot 確認後操作"

    # 警報
    total_alerts = int(summary.get("total", 0) or 0)
    alert_text = build_alert_log_text() or ""

    # AI 夜盤解讀（傳入假突破與現貨大賣等關鍵事實）
    ai_text = ""
    try:
        from ai_report_engine import generate_evening_guidance
        day_result = classify_day_result(day_ctx)
        ai_text = generate_evening_guidance(
            day_result, chip_ctx, summary,
            call_wall_status=call_wall_status,
            put_wall_status=put_wall_status,
            pivot_status=pivot_status,
            today_high=today_high,
            today_low=today_low,
            price=price,
        ) or ""
    except Exception:
        pass

    lines = []
    lines.append(f"🌙 ATOS 晚盤 {date_str}")
    lines.append("")

    # ━━ 今天發生了什麼 ━━
    lines.append("━━ 今天發生了什麼 ━━")
    lines.append(f"收盤：{_fp(price)} | H {_fp(today_high)} / L {_fp(today_low)}")
    lines.append(call_wall_status)
    lines.append(put_wall_status)
    lines.append(pivot_status)
    lines.append(f"籌碼變化：外資期貨今日{chg_arrow}")
    lines.append(f"現貨外資：{spot_dir} {abs(spot_val):.1f}億")
    lines.append("")

    # ━━ 夜盤怎麼做 ━━
    lines.append("━━ 夜盤怎麼做 ━━")
    lines.append(f"做多條件：重新突破 Call wall {_fp(call_wall)} 且5分K確認")
    lines.append(f"做空條件（強）：跌破 Put wall {_fp(put_wall)} 且量能確認")
    lines.append("做空條件（次）：")
    for _sub_line in short_sub.split("\n"):
        lines.append(f"  {_sub_line}")
    lines.append(f"觀望條件：在{_fp(put_wall)}~{_fp(call_wall)}無明確方向")
    lines.append("")
    lines.append("停損規則：")
    lines.append("空方停損：進場後100點（一口計=NT$20,000，請依帳戶規模調控）")
    lines.append("多方停損：進場後100點（一口計=NT$20,000）")
    lines.append("")

    # ━━ 今日警報 ━━
    lines.append(f"━━ 今日警報（{total_alerts}則）━━")
    if total_alerts == 0 or not alert_text.strip():
        lines.append("今日盤中無關鍵事件觸發")
    else:
        for row in alert_text.strip().split("\n"):
            lines.append(row)
    lines.append("")

    # ━━ 籌碼驗證 ━━
    lines.append("━━ 籌碼驗證 ━━")
    lines.append(f"外資期貨：{fn:+,}口（今日{chg_str}）")  # chg_str 已無嵌套括號
    lines.append(f"現貨外資：{spot_dir} {abs(spot_val):.1f}億")
    lines.append("")

    # ━━ AI 夜盤解讀 ━━
    lines.append("━━ AI 夜盤解讀 ━━")
    if ai_text:
        lines.append(ai_text)
    else:
        # Fallback：AI 無回應時，依結構數據自動組出摘要
        lines.append(_build_ai_fallback(
            call_wall_status=call_wall_status,
            put_wall_status=put_wall_status,
            pivot_status=pivot_status,
            chip_ctx=chip_ctx,
            today_high=today_high,
            today_low=today_low,
            price=price,
        ))

    # API 使用統計
    try:
        from ai_report_engine import get_api_stats
        stats = get_api_stats()
        lines.append("")
        lines.append(f"今日 AI API：呼叫 {stats['calls']}次 | 使用 {stats['total_tokens']} tokens")
    except Exception:
        pass

    return "\n".join(lines)


# --------------------------------------------------
# Sender
# --------------------------------------------------

def send_evening_report() -> bool:
    """
    發送 ATOS 晚盤報告。

    規則：
    - 15:05 前不發（等期交所數據更新）
    - 今日已發過不重複發送
    - 資料不足：不發 Telegram，return False
    - 資料完整：發送報告，return True / False
    """
    from persistent_state import save_state

    # 時間鎖：15:05 前不發
    now = datetime.now()
    if now.hour < 15 or (now.hour == 15 and now.minute < 5):
        print("⚠️ 15:05前不發晚盤報告，等待期交所數據更新完成")
        return False

    # 防重複發送
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    if state.get("evening_report_sent_date") == today:
        print("⚠️ 今日晚盤報告已發送，不重複發送")
        return False

    summary = summarize_today_alerts()
    readiness = check_evening_report_readiness(state, summary)

    if not readiness["ready"]:
        print("⚠️ 晚盤報告未發送，資料不足：")
        for item in readiness["missing"]:
            print(f"   - {item}")
        return False

    msg = build_evening_report_message(readiness=readiness)

    if not msg:
        return False

    result = send_to_telegram(msg)
    if result:
        state["evening_report_sent_date"] = today
        save_state(state)

    return result


if __name__ == "__main__":
    message = build_evening_report_message()

    if message:
        print(message)
    else:
        print("⚠️ 晚盤報告資料不足，不輸出報告。")