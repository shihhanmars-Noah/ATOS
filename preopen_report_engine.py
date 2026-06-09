# preopen_report_engine.py

from datetime import datetime

from night_session_engine import build_night_context_text
from error_handler import safe_execute
from messenger import send_to_telegram
from persistent_state import load_state, save_state

from data_engine import (
    get_dynamic_resistance_support,
    get_institutional_sentiment,
)

try:
    from chip_data_engine import build_chip_context
except Exception:
    build_chip_context = None

try:
    from ai_report_engine import generate_report as ai_generate_report
except Exception:
    ai_generate_report = None


# --------------------------------------------------
# Helpers
# --------------------------------------------------

def format_price(value):
    if value is None:
        return "N/A"
    try:
        return round(float(value), 1)
    except Exception:
        return value


def safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _v(val, default="N/A"):
    return default if val is None else val


# --------------------------------------------------
# Bias / Scenario Logic
# --------------------------------------------------

def parse_sentiment_bias(sentiment: str) -> dict:
    text = str(sentiment)

    if "強空" in text or "淨空" in text:
        return {
            "bias": "🔴 外資偏空",
            "risk_note": "外資期貨仍偏空，開高不追多；多單只做站穩後回測不破。",
            "score": -1,
        }

    if "強多" in text or "淨多" in text:
        return {
            "bias": "🟢 外資偏多",
            "risk_note": "外資期貨偏多，拉回不破關鍵價可偏多觀察，但仍不追高。",
            "score": 1,
        }

    return {
        "bias": "🟡 外資中性",
        "risk_note": "法人方向不明，今日以區間與關鍵價確認為主。",
        "score": 0,
    }


def build_preopen_bias(
    close_price: float,
    mid_range: float,
    pivot: float,
    sentiment_score: int,
) -> dict:
    """
    建立盤前方向判斷。使用前日收盤 vs 中軸(mid_range) 及法人方向。
    """

    if not close_price or not mid_range or not pivot:
        return {
            "label": "⚪ 資料不足",
            "summary": "關鍵價位不足，今日盤前不做方向預判，只能等待開盤後確認。",
            "style": "WAIT",
        }

    close_price = float(close_price)
    mid_range = float(mid_range)
    pivot = float(pivot)

    above_mid = close_price >= mid_range
    above_pivot = close_price >= pivot

    if above_mid and above_pivot and sentiment_score >= 0:
        return {
            "label": "🟢 震盪偏多",
            "summary": "前日收盤站在中軸與 Pivot 上方，若開盤守住中軸，可偏多觀察。",
            "style": "BULL",
        }

    if above_mid and sentiment_score < 0:
        return {
            "label": "🟡 震盪偏空防守",
            "summary": "前日收盤雖在中軸附近或上方，但外資期貨偏空，今天不可追多，先看中軸是否守住。",
            "style": "DEFENSIVE",
        }

    if not above_mid and sentiment_score <= 0:
        return {
            "label": "🔴 偏空觀察",
            "summary": "前日收盤落在中軸下方，且籌碼偏空，今天反彈不過中軸才看空。",
            "style": "BEAR",
        }

    if not above_pivot:
        return {
            "label": "🟡 中性偏弱",
            "summary": "前日收盤低於 Pivot，代表盤中重心偏弱，需等重新站回 Pivot / 中軸才能轉強。",
            "style": "WEAK",
        }

    return {
        "label": "🟡 中性震盪",
        "summary": "價格與籌碼沒有一致方向，今日先用中軸 / Pivot 做區間判斷，不提前猜方向。",
        "style": "NEUTRAL",
    }


def build_opening_scenarios(
    call_wall,
    put_wall,
    mid_range: float,
    pivot: float,
) -> dict:
    """建立開盤三劇本，以 Call wall / Put wall 為核心觸發點。"""

    cw = format_price(call_wall)
    pw = format_price(put_wall)
    mid_text = format_price(mid_range)
    pivot_text = format_price(pivot)

    try:
        call_target = format_price(float(call_wall) + 500) if call_wall else "N/A"
        put_target1 = format_price(float(put_wall) - 500) if put_wall else "N/A"
        put_target2 = format_price(float(put_wall) - 1000) if put_wall else "N/A"
        call_sell = format_price(float(call_wall) - 500) if call_wall else "N/A"
    except Exception:
        call_target = put_target1 = put_target2 = call_sell = "N/A"

    scenario_a = (
        f"劇本 A｜突破 Call wall {cw}\n"
        "觸發：5分K收盤確認站上\n"
        "判斷：大戶空頭防線失守，可能軋空\n"
        f"目標：{call_target}，無明顯壓力延伸\n"
        f"失效：跌回 {cw} 下方"
    )

    scenario_b = (
        f"劇本 B｜跌破 Put wall {pw}\n"
        "觸發①：5分K收盤確認站下 + 當根量 > 前5K均量 × 1.2\n"
        "觸發②：或回抽中軸 ±50 點，量 < 前3K均量 × 0.7 且收黑K\n"
        "外資確認：外資淨空方向維持（今日未明顯減空）\n"
        "判斷：大戶選擇權防線失守，加速下跌\n"
        f"目標：{put_target1} → {put_target2}\n"
        f"失效：收回 {pw} 上方；或前K低於 {pw}、當K收回的假跌破型態"
    )

    scenario_c = (
        f"劇本 C｜區間震盪 {pw}～{cw}\n"
        "判斷：大戶收割時間價值，不做方向單\n"
        f"中軸 {mid_text}：偏多站上方，偏空站下方\n"
        f"選擇權策略：賣出 Call {call_sell} 收時間價值\n"
        f"等待：突破 {cw} 或跌破 {pw}"
    )

    return {"A": scenario_a, "B": scenario_b, "C": scenario_c}


# --------------------------------------------------
# 籌碼背景段落
# --------------------------------------------------

def build_chip_section(chip_ctx: dict) -> str:
    """將 chip_context 格式化成第五段純文字。"""

    if not chip_ctx or chip_ctx.get("error"):
        return "籌碼資料暫無法取得。"

    fn = chip_ctx.get("foreign_net", 0)
    fn_level = chip_ctx.get("foreign_net_level", "N/A")
    fn_1d = chip_ctx.get("foreign_net_chg_1d", 0)
    fn_3d = chip_ctx.get("foreign_net_chg_3d", 0)
    spot = chip_ctx.get("spot_foreign_net_buy_bn", 0)
    spot_5d = chip_ctx.get("spot_foreign_5d_sum_bn", 0)
    trust = chip_ctx.get("spot_trust_net_buy_bn", 0)

    cw = chip_ctx.get("call_wall")
    cw_oi = chip_ctx.get("call_wall_oi")
    pw = chip_ctx.get("put_wall")
    pw_oi = chip_ctx.get("put_wall_oi")
    cp = chip_ctx.get("call_put_ratio")
    mp = chip_ctx.get("max_pain")
    pos = chip_ctx.get("price_position_pct")

    score = chip_ctx.get("sentiment_score", 0)
    bias = chip_ctx.get("sentiment_bias", "N/A")
    detail = chip_ctx.get("sentiment_detail", {}) or {}

    lt_net = chip_ctx.get("lt_top5_net")
    lt_long = chip_ctx.get("lt_top5_long_pct")
    lt_short = chip_ctx.get("lt_top5_short_pct")

    fg = chip_ctx.get("fear_greed_index")
    fg_emo = chip_ctx.get("fear_greed_emotion")

    lines = [
        f"外資期貨：{fn:+,}口（{fn_level}）",
        f"  1日變化：{fn_1d:+,}口 / 3日趨勢：{fn_3d:+,}口",
        f"外資現貨：{spot:+.1f}億（5日累計：{spot_5d:+.1f}億）",
        f"投信現貨：{trust:+.1f}億",
        "",
        f"OI框架：",
        f"  Call牆：{_v(cw)}（OI {_v(cw_oi)}）  Put牆：{_v(pw)}（OI {_v(pw_oi)}）",
        f"  C/P比：{_v(cp)} / MaxPain：{_v(mp)} / 位置：{_v(pos)}%",
        "",
        f"情緒評分：{score:+d}（{bias}）",
    ]

    if detail:
        lines.append(
            f"  S1外資：{_v(detail.get('s1_futures'))} / "
            f"S2動作：{_v(detail.get('s2_action'))} / "
            f"S3現貨：{_v(detail.get('s3_spot'))} / "
            f"S4OI：{_v(detail.get('s4_oi'))} / "
            f"S5波動：{_v(detail.get('s5_vol'))} / "
            f"S6大戶：{_v(detail.get('s6_large_traders'))}"
        )

    if lt_net is not None:
        lines.append(f"大額交易人Top5：{lt_net:+,}口（多{_v(lt_long)}% / 空{_v(lt_short)}%）")

    if fg is not None:
        lines.append(f"CNN Fear & Greed：{fg}（{_v(fg_emo)}）")

    warnings = chip_ctx.get("warnings", [])
    if warnings:
        lines.append("")
        for w in warnings:
            lines.append(f"⚠️ {w}")

    return "\n".join(lines)


# --------------------------------------------------
# Payload Builder
# --------------------------------------------------

def build_preopen_payload() -> dict:
    """建立盤前 SIP 所有資料。"""

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    now_time = now.strftime("%H:%M")

    levels = get_dynamic_resistance_support(futures_id="TX") or {}
    sentiment = get_institutional_sentiment()

    r1 = levels.get("R1", 0)
    s1 = levels.get("S1", 0)
    pivot = levels.get("pivot", None)
    source_date = levels.get("source_date", "N/A")
    contract_date = levels.get("contract_date", "N/A")
    days_to_settlement = levels.get("days_to_settlement", 99)
    high = levels.get("high", None)
    low = levels.get("low", None)
    close_price = levels.get("close", None)
    volume = levels.get("volume", None)

    # Active Pivot 計算（取代 mid_range 顯示邏輯）
    mid_range = None  # 保留供向後相容
    if high is not None and low is not None:
        try:
            mid_range = round((float(high) + float(low)) / 2, 1)
        except Exception:
            pass

    chip_ctx_tmp = None
    if build_chip_context is not None:
        try:
            chip_ctx_tmp = build_chip_context()
        except Exception:
            pass

    _cw_tmp = chip_ctx_tmp.get("call_wall") if chip_ctx_tmp else None
    _pw_tmp = chip_ctx_tmp.get("put_wall") if chip_ctx_tmp else None

    active_pivot = None
    pivot_note = ""
    if high is not None and low is not None and close_price is not None:
        try:
            _sp = (float(high) + float(low) + float(close_price)) / 3
            if _cw_tmp is not None and _pw_tmp is not None:
                _cw_f = float(_cw_tmp)
                _pw_f = float(_pw_tmp)
                if _sp >= _cw_f or _sp <= _pw_f:
                    active_pivot = round((_cw_f + _pw_f) / 2, 1)
                    pivot_note = "（前日大波動，改用區間中點）"
                else:
                    active_pivot = round(_sp, 1)
                    pivot_note = ""
            else:
                active_pivot = round(_sp, 1)
        except Exception:
            pass

    chip_ctx = chip_ctx_tmp  # 已在 active_pivot 計算時取得

    chip_sentiment_score = 0
    if chip_ctx:
        chip_sentiment_score = chip_ctx.get("sentiment_score", 0)

    sentiment_info = parse_sentiment_bias(sentiment)
    # 優先用 chip 情緒評分決定方向，fallback 用 sentiment_info 文字分數
    effective_score = chip_sentiment_score if chip_ctx else sentiment_info["score"]

    bias = build_preopen_bias(
        close_price=close_price,
        mid_range=mid_range,
        pivot=pivot,
        sentiment_score=effective_score,
    )

    chip_call_wall = chip_ctx.get("call_wall") if chip_ctx else None
    chip_put_wall = chip_ctx.get("put_wall") if chip_ctx else None

    scenarios = build_opening_scenarios(
        call_wall=chip_call_wall,
        put_wall=chip_put_wall,
        mid_range=mid_range,
        pivot=pivot,
    )

    night_context_text = build_night_context_text(
        flip=active_pivot,   # 改用 active_pivot（取代 mid_range）
        pivot=pivot,
        previous_futures_close=close_price,
    )

    # 夜盤高低收資料（從 TaiwanFuturesTick 計算）
    try:
        from night_session_engine import get_night_session_data
        night_data = get_night_session_data()
    except Exception:
        night_data = {}

    # 估算今日開盤前外資部位（日盤 + 夜盤）
    _fn_val  = (chip_ctx or {}).get("foreign_net", 0) or 0
    _ah_val  = (chip_ctx or {}).get("foreign_ah_net", 0) or 0
    try:
        estimated_foreign_net = int(_fn_val) + int(_ah_val)
    except Exception:
        estimated_foreign_net = 0

    return {
        "today": today,
        "now_time": now_time,
        "levels": levels,
        "r1": r1,
        "s1": s1,
        "pivot": pivot,
        "active_pivot": active_pivot,
        "pivot_note": pivot_note,
        "mid_range": mid_range,
        "source_date": source_date,
        "contract_date": contract_date,
        "days_to_settlement": days_to_settlement,
        "previous_high": high,
        "previous_low": low,
        "previous_close": close_price,
        "previous_volume": volume,
        "sentiment": sentiment,
        "sentiment_info": sentiment_info,
        "chip_ctx": chip_ctx,
        "bias": bias,
        "scenarios":            scenarios,
        "night_context_text":   night_context_text,
        "night_data":           night_data,
        "estimated_foreign_net": estimated_foreign_net,
    }


# --------------------------------------------------
# State Writer
# --------------------------------------------------

@safe_execute
def save_preopen_plan_to_state(payload: dict) -> bool:
    """將盤前劇本寫入 atos_state.json。"""

    if not isinstance(payload, dict):
        return False

    state = load_state()
    today = payload.get("today")

    r1 = payload.get("r1")
    s1 = payload.get("s1")
    pivot = payload.get("pivot")
    mid_range = payload.get("mid_range")

    active_pivot = payload.get("active_pivot") or payload.get("pivot")
    bias = payload.get("bias", {}) or {}
    sentiment_info = payload.get("sentiment_info", {}) or {}
    scenarios = payload.get("scenarios", {}) or {}
    chip_ctx = payload.get("chip_ctx") or {}

    preopen_plan = {
        "date": today,
        "source": "preopen_report_engine",
        "data_basis": "前一交易日 / 最新期貨日資料",
        "bias_label": bias.get("label"),
        "bias_summary": bias.get("summary"),
        "bias_style": bias.get("style"),
        "sentiment": payload.get("sentiment"),
        "sentiment_bias": sentiment_info.get("bias"),
        "sentiment_score": sentiment_info.get("score"),
        "sentiment_risk_note": sentiment_info.get("risk_note"),
        "main_scenario": bias.get("summary"),
        "strategy": (
            "以中軸(mid_range)為主控價。站穩中軸後回測不破才看多；"
            "跌破中軸後反彈不過才看空；卡在 Pivot / 中軸中間不做。"
        ),
        "key_levels": {
            "r1": safe_float(r1),
            "mid_range": safe_float(mid_range),
            "pivot": safe_float(pivot),
            "s1": safe_float(s1),
        },
        "previous_session": {
            "source_date": payload.get("source_date"),
            "contract_date": payload.get("contract_date"),
            "high": safe_float(payload.get("previous_high")),
            "low": safe_float(payload.get("previous_low")),
            "close": safe_float(payload.get("previous_close")),
            "volume": payload.get("previous_volume"),
        },
        "scenarios": {
            "A": scenarios.get("A"),
            "B": scenarios.get("B"),
            "C": scenarios.get("C"),
        },
        "rules": [
            "禁止開盤第一根追單",
            "禁止在 Pivot / 中軸中間硬猜",
            "禁止看到外資空就直接追空",
            "禁止看到開高就直接追多",
            "禁止沒有 5分K 確認就進場",
        ],
    }

    state["preopen_plan_ready"] = True
    state["preopen_plan_date"] = today
    state["preopen_bias"] = bias.get("label")
    state["preopen_plan"] = preopen_plan

    # active_pivot 寫入 flip（取代舊 mid_range 邏輯），向後相容保留 mid_range
    state["active_pivot"] = safe_float(active_pivot)
    state["flip"] = safe_float(active_pivot)   # flip 欄位改用 active_pivot
    state["mid_range"] = safe_float(mid_range)  # 保留向後相容
    state["pivot"] = safe_float(pivot)
    state["r1"] = safe_float(r1)
    state["s1"] = safe_float(s1)

    state["levels"] = {
        "R1": safe_float(r1),
        "pivot": safe_float(pivot),
        "S1": safe_float(s1),
        "source_date": payload.get("source_date"),
        "contract_date": payload.get("contract_date"),
        "high": safe_float(payload.get("previous_high")),
        "low": safe_float(payload.get("previous_low")),
        "close": safe_float(payload.get("previous_close")),
        "volume": payload.get("previous_volume"),
    }

    # 從 chip_ctx 寫入額外欄位
    if chip_ctx:
        state["call_wall"] = chip_ctx.get("call_wall")
        state["put_wall"] = chip_ctx.get("put_wall")
        state["sentiment_score"] = chip_ctx.get("sentiment_score")
        state["mid_range"] = chip_ctx.get("mid_range") or safe_float(mid_range)

    state["institutional_sentiment"] = payload.get("sentiment")
    state["sentiment"] = payload.get("sentiment")
    state["last_preopen_report_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    save_state(state)
    print("✅ preopen plan saved to atos_state.json")
    return True


# --------------------------------------------------
# Formatting Helpers（新版報告用）
# --------------------------------------------------

def _fp(v) -> str:
    """價格格式化：整數去小數點。"""
    if v is None:
        return "N/A"
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(round(f, 1))
    except Exception:
        return "N/A"


def _fmt_emo(emotion: str) -> str:
    table = {
        "extreme fear": "極度恐慌",
        "fear": "恐慌",
        "neutral": "中性",
        "greed": "貪婪",
        "extreme greed": "極度貪婪",
    }
    return table.get(str(emotion).lower().strip(), emotion)


def _pos_label(pct) -> str:
    if pct is None:
        return "N/A"
    pct = float(pct)
    if pct < 30:
        return "偏下方"
    if pct < 45:
        return "中段偏低"
    if pct <= 55:
        return "中段"
    if pct <= 70:
        return "中段偏高"
    return "偏上方"


def _pain_label(max_pain, current_price) -> str:
    if max_pain is None or current_price is None:
        return ""
    diff = float(max_pain) - float(current_price)
    if abs(diff) < 200:
        return "接近現價"
    return "大戶希望往下結算" if diff < 0 else "大戶希望往上結算"


def _spot_dir(val) -> str:
    if val is None:
        return ""
    return "買超" if float(val) > 0 else "賣超"


# --------------------------------------------------
# Message Builder
# --------------------------------------------------

def _closer_wall(price, call_wall, put_wall) -> str:
    """判斷現價較接近哪個 wall。"""
    try:
        d_call = abs(float(price) - float(call_wall))
        d_put  = abs(float(price) - float(put_wall))
        return "Call wall" if d_call < d_put else "Put wall"
    except Exception:
        return "N/A"


def _pain_direction(max_pain, current_price) -> str:
    if max_pain is None or current_price is None:
        return "N/A"
    try:
        return "往下" if float(max_pain) < float(current_price) else "往上"
    except Exception:
        return "N/A"


def _scenario_one_liner(sentiment_score, price_position_pct, call_wall, put_wall) -> str:
    """根據情緒評分和價位位置產生最可能場景一行描述。"""
    try:
        score = int(sentiment_score) if sentiment_score is not None else 0
        pct = float(price_position_pct) if price_position_pct is not None else 50.0
        cw = _fp(call_wall)
        pw = _fp(put_wall)

        if score <= -5:
            return f"籌碼極端偏空，最可能在 {pw}～{cw} 區間震盪壓縮，等跌破 {pw} 才有加速行情"
        if score <= -3:
            return f"外資偏空主導，反彈到 {cw} 附近量縮是主要空方機會，不破 {pw} 就繼續磨"
        if score >= 5:
            return f"籌碼偏多，站穩 {pw} 上方且外資持續回補時，突破 {cw} 機率上升"
        if score >= 3:
            return f"籌碼中性偏多，觀察能否突破 {cw}；破前以 Pivot 為多空分水嶺"
        return f"籌碼中性，在 {pw}～{cw} 區間大戶收割時間價值，無明確方向不做"
    except Exception:
        return "依籌碼與 5分K 確認再判斷方向"


def _incentive_one_liner(sentiment_score, call_wall, put_wall, fn) -> str:
    """短期誘因與限制一行描述。"""
    try:
        score = int(sentiment_score) if sentiment_score is not None else 0
        fn_i = int(fn) if fn is not None else 0
        cw = _fp(call_wall)
        pw = _fp(put_wall)
        if score <= -3 and fn_i < -30000:
            return f"外資極端空單 {fn_i:,}口壓制 {cw}，多方突破需外資同步回補才成立"
        if score >= 3:
            return f"籌碼偏多，Put wall {pw} 有大戶多頭保護，回測 {pw} 不破可偏多"
        return f"在 {pw}～{cw} 框架內大戶收割時間價值，無量能突破前不做方向"
    except Exception:
        return "以 Put wall / Call wall 為核心，等量能確認再進場"


def _build_oi_invalid_night_report(
    today: str,
    now_time: str,
    night_data: dict,
    estimated_foreign_net: int,
    chip_ctx: dict,
    call_wall,
    put_wall,
    days_to_settlement,
    atr_5d,
) -> str:
    """OI框架失效 + 夜盤資料可用時，使用夜盤框架的完整報告。"""

    # ── 夜盤資料 ──
    night_close   = float(night_data.get("night_close", 0))
    night_high    = float(night_data.get("night_high", 0))
    night_low     = float(night_data.get("night_low", 0))
    night_chg     = float(night_data.get("night_chg", 0))
    night_chg_pct = float(night_data.get("night_chg_pct", 0))
    vs_day_chg    = float(night_data.get("vs_day_chg", 0))
    day_close     = float(night_data.get("day_close", 0))
    night_date_str= night_data.get("data_date", "")

    # ── 籌碼 ──
    fn          = chip_ctx.get("foreign_net", 0) or 0
    fn_level    = chip_ctx.get("foreign_net_level", "N/A")
    spot_val    = chip_ctx.get("spot_foreign_net_buy_bn") or 0
    spot_5d     = chip_ctx.get("spot_foreign_5d_sum_bn") or 0
    spot_dir    = _spot_dir(spot_val)

    call_put_ratio   = chip_ctx.get("call_put_ratio")
    max_pain         = chip_ctx.get("max_pain")
    sentiment_score  = chip_ctx.get("sentiment_score", 0)
    sentiment_bias   = chip_ctx.get("sentiment_bias", "N/A")
    fear_greed       = chip_ctx.get("fear_greed_index", "N/A")
    fear_greed_emo   = _fmt_emo(chip_ctx.get("fear_greed_emotion", ""))
    warnings         = chip_ctx.get("warnings", [])
    chip_date        = (chip_ctx.get("source_dates") or {}).get("futures", "")

    # ── 夜盤法人 ──
    foreign_ah_net   = chip_ctx.get("foreign_ah_net", 0) or 0
    foreign_ah_long  = chip_ctx.get("foreign_ah_long", 0) or 0
    foreign_ah_short = chip_ctx.get("foreign_ah_short", 0) or 0
    dealer_ah_net    = chip_ctx.get("dealer_ah_net", 0) or 0
    ah_date          = chip_ctx.get("ah_source_date") or ""

    # ── ATR ──
    atr_5d_f = float(atr_5d) if atr_5d else 0
    atr_half  = int(round(atr_5d_f * 0.5)) if atr_5d_f else 0

    # ── 衍生 ──
    dtl = int(days_to_settlement) if days_to_settlement is not None else 99
    settle_note = "（結算週 ⚠️）" if dtl <= 7 else ""
    score_str = f"{int(sentiment_score):+d}" if sentiment_score is not None else "N/A"
    chip_date_disp = chip_date or datetime.now().strftime("%Y-%m-%d")

    # ── 特殊狀況描述 ──
    if night_chg > 300:
        situation = "昨夜盤大幅軋空"
    elif night_chg < -300:
        situation = "昨夜盤大幅下殺"
    else:
        situation = "昨夜盤OI框架失效"

    # ── 關鍵背景（靜態一句話）──
    if abs(night_chg) > 500:
        if night_chg > 0:
            bg = (
                f"夜盤軋空 {int(night_chg)} 點，外資估算仍為空方主導（{fn_level}），"
                f"短線空單回補不代表轉多，今日以觀察為主"
            )
        else:
            bg = (
                f"夜盤重挫 {int(abs(night_chg))} 點，外資估算空方延伸（{fn_level}），"
                f"今日開盤方向未定，先觀察第一根5分K"
            )
    elif abs(vs_day_chg) > 300:
        bg = (
            f"夜盤收盤較日盤{vs_day_chg:+.0f}點，昨日OI框架完全失效，"
            f"外資估算部位 {estimated_foreign_net:+,} 口，改用夜盤高低點框架"
        )
    else:
        bg = (
            f"OI框架失效，外資估算部位 {estimated_foreign_net:+,} 口（{fn_level}），"
            f"今日改用夜盤高低點作為操作依據"
        )

    # ── AI 矛盾分析（失敗時用靜態）──
    ai_analysis = ""
    try:
        from ai_report_engine import generate_preopen_contradiction
        _ai_ctx = dict(chip_ctx)
        _ai_ctx["_oi_invalid_note"] = (
            f"【OI框架失效】夜盤{night_chg:+.0f}點，外資估算{estimated_foreign_net:+,}口，"
            f"夜盤高{int(night_high)}/低{int(night_low)}/收{int(night_close)}，"
            f"C/P比{call_put_ratio}，結算{dtl}天"
        )
        ai_analysis = generate_preopen_contradiction(
            _ai_ctx, int(night_close), int(night_high), int(night_low)
        ) or ""
    except Exception:
        pass

    if not ai_analysis:
        if fn < -30000 and night_chg > 300:
            ai_analysis = (
                f"矛盾訊號：外資估算空單 {estimated_foreign_net:+,} 口（{fn_level}）"
                f"卻夜盤軋空 {int(night_chg)} 點。\n"
                f"解讀：短線空單被迫回補，不代表空頭方向逆轉。\n"
                f"C/P比 {call_put_ratio}，結算 {dtl} 天後，大戶選擇權部位仍可能壓制反彈高度。\n"
                f"今日觀察重點：開盤後能否守住夜盤低點 {int(night_low)}。"
            )
        elif night_chg < -300:
            ai_analysis = (
                f"OI框架失效後夜盤繼續下跌 {int(abs(night_chg))} 點。\n"
                f"外資估算部位 {estimated_foreign_net:+,} 口（{fn_level}），空方壓力持續。\n"
                f"C/P比 {call_put_ratio}，結算 {dtl} 天後。\n"
                f"今日不設定預設進場條件，等開盤方向確認後再行動。"
            )
        else:
            ai_analysis = (
                f"OI框架失效，夜盤波動 {night_chg:+.0f} 點，改用夜盤框架。\n"
                f"外資估算部位 {estimated_foreign_net:+,} 口，仍為{fn_level}。\n"
                f"C/P比 {call_put_ratio}，結算 {dtl} 天。\n"
                f"今日以夜盤高低點為操作依據，等14:30 OI更新再建立新框架。"
            )

    # ── 建立報告 ──
    L = []

    L.append(f"🛡️ ATOS 盤前 {today} {now_time}")
    L.append("")
    L.append(f"⚠️ 特殊狀況：{situation}")
    if day_close > 0:
        L.append(f"昨日日盤收盤：{int(day_close)}")
    L.append(
        f"今日夜盤收盤：{int(night_close)}"
        f"（{vs_day_chg:+.0f}點 {night_chg_pct:+.1f}%）"
    )
    L.append("昨日OI框架完全失效，今日改用夜盤框架操作")
    L.append("")

    # 今天的結構
    L.append("━━ 今天的結構 ━━")
    L.append(f"外資昨日空單：{fn:+,}口（{fn_level}）")
    if foreign_ah_net != 0:
        L.append(
            f"外資夜盤{'回補' if foreign_ah_net > 0 else '加碼'}："
            f"{foreign_ah_net:+,}口"
        )
    L.append(f"估算今日開盤前部位：約 {estimated_foreign_net:+,}口")
    L.append(f"現貨昨日{spot_dir}：{abs(spot_val):.1f}億（資料為昨日）")
    L.append(f"結算：{dtl}天後{settle_note}")
    L.append("")
    L.append("關鍵背景：")
    L.append(bg)
    L.append("")

    # 今日操作框架
    L.append("━━ 今日操作框架（夜盤框架）━━")
    L.append(f"上壓：夜盤高點 {int(night_high)}")
    L.append(f"參考中軸：夜盤收盤 {int(night_close)}")
    L.append(f"支撐：夜盤低點 {int(night_low)}")
    L.append("")

    # 今天等什麼
    L.append("━━ 今天等什麼 ━━")
    L.append(f"等一（多方）：站上夜盤高點 {int(night_high)} 且5分K量能確認")
    L.append(f"等二（空方）：跌破夜盤低點 {int(night_low)} 且5分K確認")
    L.append(f"等三（觀望）：在 {int(night_low)}～{int(night_high)} 之間震盪 → 不做")
    L.append("")

    # 進場怎麼做
    L.append("━━ 進場怎麼做 ━━")
    L.append("多方：")
    L.append(f"  進場：站上 {int(night_high)} 且5分K確認+量能放大")
    L.append(f"  停損：{int(night_high) - 100}（高點下方100點）（一口計=NT$20,000）")
    if atr_5d_f > 0:
        L.append(f"  （參考：ATR={int(atr_5d_f)}點，0.5×ATR={atr_half}點）")
    L.append(f"  目標一：{int(night_high) + 300}｜目標二：依動能延伸")
    L.append("")
    L.append("空方：")
    L.append(f"  進場：跌破 {int(night_low)} 且5分K確認")
    L.append("  （若破線太快現價離低點已超過50點，放棄追單）")
    L.append(f"  停損：{int(night_low) + 100}（低點上方100點）（一口計=NT$20,000）")
    L.append(f"  目標一：{int(night_low) - 300}｜目標二：{int(night_low) - 600}")
    L.append("")

    # 今天完全不做
    L.append("━━ 今天完全不做 ━━")
    L.append("✗ 開盤第一根追單")
    L.append(f"✗ 用昨日 Call wall {_fp(call_wall)} / Put wall {_fp(put_wall)} 操作")
    if night_chg > 300:
        L.append("✗ 軋空後直接追多，需等回測確認")
    if dtl <= 7:
        L.append("✗ 結算週前不做模糊訊號")
    L.append("")

    # 關鍵價位
    L.append("━━ 關鍵價位（夜盤框架）━━")
    L.append(f"上壓：夜盤高點 {int(night_high)}")
    L.append(f"參考：夜盤收盤 {int(night_close)}")
    L.append(f"支撐：夜盤低點 {int(night_low)}")
    L.append(f"⚠️ 昨日 Call wall {_fp(call_wall)} / Put wall {_fp(put_wall)} 失效，不使用")
    L.append("")

    # 籌碼數據
    L.append(f"━━ 籌碼數據（{chip_date_disp} 昨日，今日13:50後更新）━━")
    ah_dir_word = "回補" if foreign_ah_net >= 0 else "加碼"
    L.append(
        f"外資期貨：{fn_level} {fn:+,}口"
        f"｜夜盤{ah_dir_word} {foreign_ah_net:+,}口"
    )
    L.append(f"估算今日部位：約 {estimated_foreign_net:+,}口")
    L.append(f"現貨外資：{spot_dir} {abs(spot_val):.1f}億｜5日累計 {spot_5d:+.1f}億")
    L.append(f"C/P比：{call_put_ratio or 'N/A'}｜Max Pain：{_fp(max_pain)}")
    L.append(
        f"情緒：{score_str} {sentiment_bias}"
        f"｜Fear&Greed：{fear_greed} {fear_greed_emo}"
    )
    for w in warnings:
        L.append(str(w))
    L.append("")

    # 夜盤法人動向
    if ah_date:
        L.append("━━ 夜盤法人動向 ━━")
        if foreign_ah_net > 0:
            ah_dir = f"淨買 +{foreign_ah_net:,}口"
            ah_note = "回補，空單壓力略減"
        elif foreign_ah_net < 0:
            ah_dir = f"淨賣 {foreign_ah_net:,}口"
            ah_note = "加碼，空單壓力加重"
        else:
            ah_dir = "持平"
            ah_note = ""
        L.append(f"外資夜盤（{ah_date}）：{ah_dir}")
        L.append(f"（多{foreign_ah_long:,}/空{foreign_ah_short:,}）")
        if dealer_ah_net != 0:
            L.append(
                f"自營商夜盤：淨"
                f"{'+' if dealer_ah_net >= 0 else ''}{dealer_ah_net:,}口"
            )
        if ah_note:
            L.append(f"→ {ah_note}")
        L.append("")

    # AI 矛盾分析
    L.append("━━ AI 矛盾分析 ━━")
    L.append(ai_analysis)

    return "\n".join(L)


def build_preopen_sip_message(payload: dict | None = None) -> str:
    """建立新版決策工具格式盤前報告。"""

    if payload is None:
        payload = build_preopen_payload()

    today = payload.get("today")
    now_time = payload.get("now_time")

    r1 = payload.get("r1")
    s1 = payload.get("s1")
    pivot = payload.get("pivot")
    active_pivot = payload.get("active_pivot") or pivot
    pivot_note = payload.get("pivot_note", "")
    high = payload.get("previous_high")
    low = payload.get("previous_low")
    close_price = payload.get("previous_close")
    volume = payload.get("previous_volume")

    contract_date      = payload.get("contract_date", "N/A")
    days_to_settlement = payload.get("days_to_settlement", 99)

    # 夜盤資料
    night_data     = payload.get("night_data") or {}
    night_open     = night_data.get("night_open", 0)
    night_close    = night_data.get("night_close", 0)
    night_high     = night_data.get("night_high", 0)
    night_low      = night_data.get("night_low", 0)
    night_chg      = night_data.get("night_chg", 0)
    night_chg_pct  = night_data.get("night_chg_pct", 0)
    night_range_pt = night_data.get("night_range", 0)
    vs_day_chg     = night_data.get("vs_day_chg", 0)
    is_big_move    = night_data.get("is_big_move", False)
    night_date_str = night_data.get("data_date", "")

    chip_ctx = payload.get("chip_ctx") or {}

    fn = chip_ctx.get("foreign_net", 0)
    fn_level = chip_ctx.get("foreign_net_level", "N/A")
    fn_1d = chip_ctx.get("foreign_net_chg_1d", 0)
    fn_3d = chip_ctx.get("foreign_net_chg_3d", 0)
    spot_val = chip_ctx.get("spot_foreign_net_buy_bn") or 0
    spot_5d = chip_ctx.get("spot_foreign_5d_sum_bn") or 0

    call_wall = chip_ctx.get("call_wall")
    call_wall_oi = chip_ctx.get("call_wall_oi")
    put_wall = chip_ctx.get("put_wall")
    put_wall_oi = chip_ctx.get("put_wall_oi")
    call_put_ratio = chip_ctx.get("call_put_ratio")
    max_pain = chip_ctx.get("max_pain")
    price_position_pct = chip_ctx.get("price_position_pct")
    sentiment_score = chip_ctx.get("sentiment_score", 0)
    sentiment_bias = chip_ctx.get("sentiment_bias", "N/A")
    fear_greed = chip_ctx.get("fear_greed_index", "N/A")
    fear_greed_emo = _fmt_emo(chip_ctx.get("fear_greed_emotion", ""))
    warnings = chip_ctx.get("warnings", [])
    atr_5d = chip_ctx.get("atr_5d")

    # ATR 停損參考提示（附在每個停損說明後）
    if atr_5d and float(atr_5d) > 0:
        atr_half = round(float(atr_5d) * 0.5, 0)
        atr_note = (
            f"  （參考：今日ATR={int(atr_5d)}點，"
            f"0.5×ATR={int(atr_half)}點，"
            f"若市場波動放大可考慮動態停損）"
        )
    else:
        atr_note = ""

    # 衍生計算
    try:
        cw_f = float(call_wall)
        pw_f = float(put_wall)
        width = int(cw_f - pw_f)
        cw_stop = _fp(cw_f + 100)
        pw_stop = _fp(pw_f + 100)
        pw_t1   = _fp(pw_f - 500)
        pw_t2   = _fp(pw_f - 1000)
        cw_stop_short = _fp(cw_f - 100)
        cw_t1   = _fp(cw_f + 500)
    except Exception:
        width = "N/A"
        cw_stop = pw_stop = pw_t1 = pw_t2 = cw_stop_short = cw_t1 = "N/A"

    ref_price = close_price  # 盤前以前日收盤為現價參考
    pos_pct_str = f"{price_position_pct:.1f}" if price_position_pct is not None else "N/A"
    score_str = f"{int(sentiment_score):+d}" if sentiment_score is not None else "N/A"
    spot_dir = _spot_dir(spot_val)
    pain_dir = _pain_direction(max_pain, ref_price)

    # 現價與 Call wall / Put wall 位置關係
    try:
        price_f = float(ref_price) if ref_price else 0
        cw_f2   = float(call_wall) if call_wall else 0
        pw_f2   = float(put_wall)  if put_wall  else 0
    except Exception:
        price_f = cw_f2 = pw_f2 = 0

    if price_f > cw_f2 > 0:
        price_position = "above_call"
        position_label = f"已站上 Call wall {_fp(cw_f2)} 上方（+{int(price_f - cw_f2)}點）"
        scenario_wait = (
            f"等一（空方）：現價跌破 Call wall {_fp(cw_f2)} 且5分K確認 → 假突破確認，空方機會\n"
            f"等二（多方）：現價回測 Call wall {_fp(cw_f2)} 附近守穩不破 → 多方延續\n"
            f"等三（觀望）：外資極端空單未大幅回補前，站上 Call wall 後不主動追多"
        )
    elif pw_f2 > 0 and price_f < pw_f2:
        price_position = "below_put"
        position_label = f"已跌破 Put wall {_fp(pw_f2)} 下方（-{int(pw_f2 - price_f)}點）"
        scenario_wait = (
            f"等一（多方）：現價站回 Put wall {_fp(pw_f2)} 上方且5分K確認 → 假跌破，反彈機會\n"
            f"等二（空方）：反彈到 Put wall {_fp(pw_f2)} 附近量縮不過 → 空方延續\n"
            f"等三（觀望）：跌破後加速下行超過50點，放棄追空等反彈再看"
        )
    else:
        price_position = "in_range"
        pct = (price_f - pw_f2) / (cw_f2 - pw_f2) * 100 if cw_f2 > pw_f2 else 0
        if pct > 65:
            position_label = f"位於框架 {pct:.1f}%（偏上方，接近 Call wall）"
        elif pct < 35:
            position_label = f"位於框架 {pct:.1f}%（偏下方，接近 Put wall）"
        else:
            position_label = f"位於框架 {pct:.1f}%（區間中段）"
        scenario_wait = (
            f"等一（空方）：反彈到 Call wall {_fp(call_wall)} 附近量縮 → 空方機會\n"
            f"等二（空方）：跌破 Put wall {_fp(put_wall)} 且量能確認 → 追空加速\n"
            f"等三（多方）：突破 Call wall {_fp(call_wall)} 且量能確認 → 追多（機率低，需外資同步回補）"
        )

    incentive = _incentive_one_liner(sentiment_score, call_wall, put_wall, fn)
    scenario = _scenario_one_liner(sentiment_score, price_position_pct, call_wall, put_wall)

    # AI 矛盾分析（新版 prompt）
    ai_contradiction = ""
    try:
        from ai_report_engine import generate_preopen_contradiction
        ai_contradiction = generate_preopen_contradiction(chip_ctx, active_pivot, r1, s1) or ""
    except Exception:
        pass
    if not ai_contradiction:
        try:
            from ai_report_engine import generate_preopen_guidance
            result = generate_preopen_guidance(chip_ctx, sentiment_bias or "N/A")
            if isinstance(result, dict):
                ai_contradiction = result.get("judgment", "")
        except Exception:
            pass

    # ── OI 框架有效性檢查 ──────────────────────────────────────────────
    try:
        _state_snap = load_state()
        _snap_price  = float(_state_snap.get("price") or 0)
    except Exception:
        _snap_price = 0
    _cw_f = float(call_wall) if call_wall else 0
    _pw_f = float(put_wall)  if put_wall  else 0
    _oi_chip_date = (chip_ctx.get("source_dates") or {}).get("option_oi", "")
    _today_str    = datetime.now().strftime("%Y-%m-%d")

    oi_framework_valid = True
    oi_warning = ""

    if _oi_chip_date and _oi_chip_date != _today_str and _snap_price > 0:
        if _cw_f > 0 and _snap_price > _cw_f + 500:
            oi_framework_valid = False
            oi_warning = (
                f"🚨 OI框架失效：現價{_snap_price:.0f}已高於"
                f"Call wall {_cw_f:.0f}（+{_snap_price - _cw_f:.0f}點）\n"
                f"昨日框架不適用今日操作，等14:30 OI更新後再建立新框架\n"
                f"今日不使用Call wall/Put wall點位操作"
            )
        elif _pw_f > 0 and _snap_price < _pw_f - 500:
            oi_framework_valid = False
            oi_warning = (
                f"🚨 OI框架失效：現價{_snap_price:.0f}已低於"
                f"Put wall {_pw_f:.0f}（-{_pw_f - _snap_price:.0f}點）\n"
                f"昨日框架不適用今日操作，等14:30 OI更新後再建立新框架"
            )
    # ── OI 框架有效性檢查結束 ──────────────────────────────────────────

    # ── 早返回：OI失效 + 夜盤資料可用 → 使用全新夜盤框架報告 ──────────
    if not oi_framework_valid and night_high > 0:
        estimated_foreign_net = payload.get("estimated_foreign_net", 0) or 0
        return _build_oi_invalid_night_report(
            today=today,
            now_time=now_time,
            night_data=night_data,
            estimated_foreign_net=estimated_foreign_net,
            chip_ctx=chip_ctx,
            call_wall=call_wall,
            put_wall=put_wall,
            days_to_settlement=days_to_settlement,
            atr_5d=atr_5d,
        )
    # ── ──────────────────────────────────────────────────────────────

    lines = []

    lines.append(f"🛡️ ATOS 盤前 {today} {now_time}")
    if not oi_framework_valid:
        lines.append("")
        lines.append(oi_warning)
    lines.append("")

    # ━━ 今天的結構 ━━
    lines.append("━━ 今天的結構 ━━")
    lines.append(f"外資 {fn_level} {fn:+,}口，現貨{spot_dir} {abs(spot_val):.1f}億")
    lines.append(f"大戶框架：Put wall {_fp(put_wall)} ~ Call wall {_fp(call_wall)}（寬度{width}點）")
    lines.append(f"現價 {_fp(ref_price)}，{position_label}")
    _dtl = int(days_to_settlement) if days_to_settlement is not None else "?"
    lines.append(f"合約：{contract_date}（{_dtl}天後結算）")
    lines.append(f"Max Pain {_fp(max_pain)}，大戶希望{pain_dir}結算")
    lines.append(f"→ {incentive}")
    lines.append("")

    # ━━ 最可能的場景 ━━
    lines.append("━━ 最可能的場景 ━━")
    lines.append(scenario)
    lines.append("")

    # ━━ 今天等什麼 ━━
    lines.append("━━ 今天等什麼 ━━")
    if not oi_framework_valid and night_high > 0:
        # 有夜盤資料 → 改用夜盤框架
        _nh_str  = f"{night_high:.0f}"
        _nl_str  = f"{night_low:.0f}"
        _nc_str  = f"{night_close:.0f}"
        _nd_label = f"（{night_date_str}）" if night_date_str else ""
        lines.append(
            f"⚠️ 昨日OI框架失效，改用夜盤收盤框架{_nd_label}\n"
            f"夜盤區間：{_nl_str} ～ {_nh_str}（寬度{night_range_pt:.0f}點）\n"
            f"等一（多方）：站上夜盤高點 {_nh_str} 且5分K確認量能放大 → 追多\n"
            f"等二（空方）：跌破夜盤低點 {_nl_str} 且5分K確認 → 偏空\n"
            f"等三（觀望）：在 {_nl_str}～{_nh_str} 之間震盪 → 不做方向單\n"
            f"等四（換框架）：等14:30選擇權OI更新 → 晚盤報告給出新Call/Put wall"
        )
    elif not oi_framework_valid:
        # 無夜盤資料 → 純觀望指示
        lines.append(
            "⚠️ 昨日OI框架失效，今日無有效Call/Put wall\n"
            "等一：觀察開盤第一根5分K方向和量能\n"
            "等二：等13:50外資口數更新確認方向\n"
            "等三：等14:30選擇權OI更新建立新框架\n"
            "在新框架建立前，不做方向單"
        )
    else:
        lines.append(scenario_wait)
    lines.append("")

    # ━━ 進場怎麼做 ━━
    lines.append("━━ 進場怎麼做 ━━")
    if not oi_framework_valid and night_high > 0:
        _nh = int(night_high)
        _nl = int(night_low)
        _nc = int(night_close)
        lines.append(f"多方：站上夜盤高點 {_nh} 且5分K確認+量能")
        lines.append(f"  停損：夜盤低點下方100點（{_nl - 100}）（一口計=NT$20,000）")
        lines.append(f"  目標一：{_nh + 300}｜目標二：{_nh + 600}（無OI牆參考，依動能延伸）")
        lines.append("")
        lines.append(f"空方：跌破夜盤低點 {_nl} 且5分K確認")
        lines.append(f"  停損：夜盤高點上方100點（{_nh + 100}）（一口計=NT$20,000）")
        lines.append(f"  目標一：{_nl - 300}｜目標二：{_nl - 600}")
        lines.append("")
        lines.append("⚠️ 14:30後OI更新，晚盤報告給出新Call/Put wall點位")
        if atr_note:
            lines.append(atr_note)
    elif not oi_framework_valid:
        lines.append("今日OI框架失效期間，不設定進場條件")
        lines.append("14:30後OI更新，晚盤報告會給出新框架")
    else:
        lines.append(f"空方（等一）：")
        lines.append(f"  進場：Call wall 附近5分K量縮確認轉弱")
        lines.append(f"  停損：Call wall 上方100點（{cw_stop}）（一口計=NT$20,000，請依帳戶規模調控）")
        if atr_note:
            lines.append(atr_note)
        lines.append(f"  目標：Pivot {_fp(active_pivot)}{pivot_note} → Put wall {_fp(put_wall)}")
        lines.append("")
        lines.append(f"空方（等二）：")
        lines.append(f"  進場：跌破{_fp(put_wall)}且下一根5分K確認")
        lines.append(f"  （若破線太快現價離Put wall已超過50點，放棄追單，改等反彈測試不破進場）")
        lines.append(f"  停損：{pw_stop}（一口計=NT$20,000）")
        if atr_note:
            lines.append(atr_note)
        lines.append(f"  目標：{pw_t1} → {pw_t2}")
        lines.append("")
        lines.append(f"多方（等三）：")
        lines.append(f"  進場：突破{_fp(call_wall)}且下一根5分K確認+量能放大")
        lines.append(f"  停損：{cw_stop_short}（一口計=NT$20,000）")
        if atr_note:
            lines.append(atr_note)
        lines.append(f"  目標：{cw_t1}")
    lines.append("")

    # ━━ 今天完全不做 ━━
    lines.append("━━ 今天完全不做 ━━")
    lines.append("✗ 開盤第一根追單")
    lines.append(f"✗ 在 Pivot {_fp(active_pivot)}{pivot_note} 附近無方向硬猜")
    lines.append("✗ 外資空單持續增加時追多")
    lines.append("✗ 低流動性時段（11:30-13:00）的突破訊號")
    if not oi_framework_valid:
        lines.append("✗ 用昨日Call wall/Put wall點位操作")
    lines.append("")

    # ━━ 關鍵價位 ━━
    lines.append("━━ 關鍵價位 ━━")
    if not oi_framework_valid and night_high > 0:
        _nd_label2 = f"（{night_date_str}）" if night_date_str else ""
        lines.append(f"上壓：夜盤高點 {int(night_high)}{_nd_label2}")
        lines.append(f"參考：夜盤收盤 {int(night_close)}")
        lines.append(f"支撐：夜盤低點 {int(night_low)}")
        lines.append(f"⚠️ 昨日 Call wall {_fp(call_wall)} / Put wall {_fp(put_wall)} 失效，不使用")
    elif not oi_framework_valid:
        lines.append(f"Call wall：{_fp(call_wall)} | Pivot：{_fp(active_pivot)} | Put wall：{_fp(put_wall)}")
        lines.append("⚠️ 以上為昨日數值，市場已大幅跳空，今日不使用昨日 Call/Put wall 操作")
    else:
        lines.append(f"Call wall：{_fp(call_wall)} | Pivot：{_fp(active_pivot)} | Put wall：{_fp(put_wall)}")
    lines.append("")

    # ━━ 籌碼數據 ━━
    lines.append("━━ 籌碼數據 ━━")

    # 資料新鮮度警告
    chip_date = (chip_ctx.get("source_dates") or {}).get("futures")
    today_str = datetime.now().strftime("%Y-%m-%d")
    if chip_date and chip_date != today_str:
        stale_note = f"⚠️ 日盤籌碼資料為 {chip_date}（昨日），今日13:50後更新"
        try:
            _state = load_state()
            _prev_close = chip_ctx.get("prev_close") or 0
            _snap_price = float(_state.get("price") or 0)
            _night_move = abs(_snap_price - float(_prev_close)) if _prev_close and _snap_price else 0
            if _night_move > 500:
                stale_note += (
                    f"\n⚠️ 昨夜盤波動 {_night_move:.0f}點，"
                    f"今日OI框架（Call/Put wall）可能已失效，"
                    f"建議以夜盤高低點為參考，等14:30後OI更新"
                )
        except Exception:
            pass
        lines.append(stale_note)

    lines.append(f"外資期貨：{fn_level} {fn:+,}口 | 1日變動 {fn_1d:+,}")
    lines.append(f"現貨外資：{spot_dir} {abs(spot_val):.1f}億 | 5日累計 {spot_5d:+.1f}億")
    lines.append(f"C/P比：{call_put_ratio or 'N/A'} | Max Pain：{_fp(max_pain)}")
    lines.append(f"情緒：{score_str} {sentiment_bias} | Fear&Greed：{fear_greed} {fear_greed_emo}")
    for w in warnings:
        lines.append(str(w))
    lines.append("")

    # ━━ 夜盤法人動向 ━━
    foreign_ah_net   = chip_ctx.get("foreign_ah_net", 0)
    foreign_ah_long  = chip_ctx.get("foreign_ah_long", 0)
    foreign_ah_short = chip_ctx.get("foreign_ah_short", 0)
    dealer_ah_net    = chip_ctx.get("dealer_ah_net", 0)
    ah_date          = chip_ctx.get("ah_source_date") or ""

    if ah_date:
        lines.append("━━ 夜盤法人動向 ━━")

        if foreign_ah_net > 0:
            ah_direction = f"淨買 +{foreign_ah_net:,}口（多{foreign_ah_long:,}/空{foreign_ah_short:,}）"
            ah_note = "外資夜盤回補，空單壓力略減"
        elif foreign_ah_net < 0:
            ah_direction = f"淨賣 {foreign_ah_net:,}口（多{foreign_ah_long:,}/空{foreign_ah_short:,}）"
            ah_note = "外資夜盤加碼空單"
        else:
            ah_direction = f"無明顯方向（多{foreign_ah_long:,}/空{foreign_ah_short:,}）"
            ah_note = ""

        foreign_net_day = chip_ctx.get("foreign_net", 0)
        estimated_net = foreign_net_day + foreign_ah_net

        lines.append(f"外資夜盤（{ah_date}）：{ah_direction}")
        lines.append(
            f"估算今日開盤前部位：約 {estimated_net:+,}口"
            f"（日盤 {foreign_net_day:+,} + 夜盤 {foreign_ah_net:+,}）"
        )
        if dealer_ah_net != 0:
            lines.append(f"自營商夜盤：淨{'+' if dealer_ah_net >= 0 else ''}{dealer_ah_net:,}口")
        if ah_note:
            lines.append(f"→ {ah_note}")
        lines.append("")

    # ━━ AI 矛盾分析 ━━
    lines.append("━━ AI 矛盾分析 ━━")
    lines.append(ai_contradiction if ai_contradiction else "（AI 分析暫不可用）")

    return "\n".join(lines)


# --------------------------------------------------
# Sender
# --------------------------------------------------

@safe_execute
def send_preopen_sip_report():
    """發送盤前 SIP 作戰報告到 Telegram。"""

    payload = build_preopen_payload()
    save_preopen_plan_to_state(payload)
    msg = build_preopen_sip_message(payload)
    return send_to_telegram(msg)


# --------------------------------------------------
# Manual Test
# --------------------------------------------------

if __name__ == "__main__":
    send_preopen_sip_report()
