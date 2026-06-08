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
        "previous_high": high,
        "previous_low": low,
        "previous_close": close_price,
        "previous_volume": volume,
        "sentiment": sentiment,
        "sentiment_info": sentiment_info,
        "chip_ctx": chip_ctx,
        "bias": bias,
        "scenarios": scenarios,
        "night_context_text": night_context_text,
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
    closer = _closer_wall(ref_price, call_wall, put_wall) if ref_price else "N/A"
    pos_pct_str = f"{price_position_pct:.1f}" if price_position_pct is not None else "N/A"
    score_str = f"{int(sentiment_score):+d}" if sentiment_score is not None else "N/A"
    spot_dir = _spot_dir(spot_val)
    pain_dir = _pain_direction(max_pain, ref_price)

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

    lines = []

    lines.append(f"🛡️ ATOS 盤前 {today} {now_time}")
    lines.append("")

    # ━━ 今天的結構 ━━
    lines.append("━━ 今天的結構 ━━")
    lines.append(f"外資 {fn_level} {fn:+,}口，現貨{spot_dir} {abs(spot_val):.1f}億")
    lines.append(f"大戶框架：Put wall {_fp(put_wall)} ~ Call wall {_fp(call_wall)}（寬度{width}點）")
    lines.append(f"現價 {_fp(ref_price)}，位於框架 {pos_pct_str}%（接近{closer}）")
    lines.append(f"Max Pain {_fp(max_pain)}，大戶希望{pain_dir}結算")
    lines.append(f"→ {incentive}")
    lines.append("")

    # ━━ 最可能的場景 ━━
    lines.append("━━ 最可能的場景 ━━")
    lines.append(scenario)
    lines.append("")

    # ━━ 今天等什麼 ━━
    lines.append("━━ 今天等什麼 ━━")
    lines.append(f"等一（空方）：反彈到 Call wall {_fp(call_wall)} 附近量縮 → 空方機會")
    lines.append(f"等二（空方）：跌破 Put wall {_fp(put_wall)} 且量能確認 → 追空加速")
    lines.append(f"等三（多方）：突破 Call wall {_fp(call_wall)} 且量能確認 → 追多（機率低，需外資同步回補）")
    lines.append("")

    # ━━ 進場怎麼做 ━━
    lines.append("━━ 進場怎麼做 ━━")
    lines.append(f"空方（等一）：")
    lines.append(f"  進場：Call wall 附近5分K量縮確認轉弱")
    lines.append(f"  停損：Call wall 上方100點（{cw_stop}）（一口計=NT$20,000，請依帳戶規模調控）")
    lines.append(f"  目標：Pivot {_fp(active_pivot)}{pivot_note} → Put wall {_fp(put_wall)}")
    lines.append("")
    lines.append(f"空方（等二）：")
    lines.append(f"  進場：跌破{_fp(put_wall)}且下一根5分K確認")
    lines.append(f"  （若破線太快現價離Put wall已超過50點，放棄追單，改等反彈測試不破進場）")
    lines.append(f"  停損：{pw_stop}（一口計=NT$20,000）")
    lines.append(f"  目標：{pw_t1} → {pw_t2}")
    lines.append("")
    lines.append(f"多方（等三）：")
    lines.append(f"  進場：突破{_fp(call_wall)}且下一根5分K確認+量能放大")
    lines.append(f"  停損：{cw_stop_short}（一口計=NT$20,000）")
    lines.append(f"  目標：{cw_t1}")
    lines.append("")

    # ━━ 今天完全不做 ━━
    lines.append("━━ 今天完全不做 ━━")
    lines.append("✗ 開盤第一根追單")
    lines.append(f"✗ 在 Pivot {_fp(active_pivot)}{pivot_note} 附近無方向硬猜")
    lines.append("✗ 外資空單持續增加時追多")
    lines.append("✗ 低流動性時段（11:30-13:00）的突破訊號")
    lines.append("")

    # ━━ 關鍵價位 ━━
    lines.append("━━ 關鍵價位 ━━")
    lines.append(f"Call wall：{_fp(call_wall)} | Pivot：{_fp(active_pivot)} | Put wall：{_fp(put_wall)}")
    lines.append("")

    # ━━ 籌碼數據 ━━
    lines.append("━━ 籌碼數據 ━━")
    lines.append(f"外資期貨：{fn_level} {fn:+,}口 | 1日變動 {fn_1d:+,}")
    lines.append(f"現貨外資：{spot_dir} {abs(spot_val):.1f}億 | 5日累計 {spot_5d:+.1f}億")
    lines.append(f"C/P比：{call_put_ratio or 'N/A'} | Max Pain：{_fp(max_pain)}")
    lines.append(f"情緒：{score_str} {sentiment_bias} | Fear&Greed：{fear_greed} {fear_greed_emo}")
    for w in warnings:
        lines.append(str(w))
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
