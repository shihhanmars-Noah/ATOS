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

    # 中軸：前日高低中間值，取代舊 Flip
    mid_range = None
    if high is not None and low is not None:
        try:
            mid_range = round((float(high) + float(low)) / 2, 1)
        except Exception:
            pass

    chip_ctx = None
    if build_chip_context is not None:
        try:
            chip_ctx = build_chip_context()
        except Exception:
            pass

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
        flip=mid_range,
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

    # mid_range 取代 flip 作為中軸，同時保留 flip 欄位供其他模組向後相容
    state["mid_range"] = safe_float(mid_range)
    state["flip"] = safe_float(mid_range)
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

def build_preopen_sip_message(payload: dict | None = None) -> str:
    """建立新版簡潔盤前報告。"""

    if payload is None:
        payload = build_preopen_payload()

    today = payload.get("today")
    now_time = payload.get("now_time")

    r1 = payload.get("r1")
    s1 = payload.get("s1")
    pivot = payload.get("pivot")
    mid_range = payload.get("mid_range")
    high = payload.get("previous_high")
    low = payload.get("previous_low")
    close_price = payload.get("previous_close")
    volume = payload.get("previous_volume")

    bias = payload.get("bias", {})
    bias_label = bias.get("label", "N/A")
    night_context_text = payload.get("night_context_text", "") or ""
    chip_ctx = payload.get("chip_ctx") or {}

    # 籌碼欄位
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

    # 計算 Call wall - 500
    try:
        call_target = _fp(float(call_wall) - 500) if call_wall else "N/A"
    except Exception:
        call_target = "N/A"

    score_str = f"{int(sentiment_score):+d}" if sentiment_score is not None else "N/A"
    pos_pct_str = f"{price_position_pct:.1f}" if price_position_pct is not None else "N/A"

    # AI 指引（今天怎麼做 + AI判斷）
    today_guidance = ""
    ai_judgment = ""
    try:
        from ai_report_engine import generate_preopen_guidance
        result = generate_preopen_guidance(chip_ctx, bias_label)
        if isinstance(result, dict):
            today_guidance = result.get("today", "")
            ai_judgment = result.get("judgment", "")
    except Exception:
        pass

    lines = []

    # 標頭
    lines.append(f"🛡️ ATOS 盤前 {today} {now_time}")
    lines.append(f"方向：{bias_label}")
    lines.append("")

    # 今天怎麼做
    lines.append("今天怎麼做")
    if today_guidance:
        lines.append(today_guidance)
    else:
        lines.append(f"今天關鍵問題只有一個：{_fp(put_wall)} 守不守得住？")
        lines.append(f"外資極端空單 {fn:+,}口壓制，反彈到中軸 {_fp(mid_range)} 量縮是空方機會。")
        lines.append(f"跌破 {_fp(put_wall)} 才是今年最好的空方機會，不破就繼續震盪等待。")
    lines.append(f"開盤第一步：觀察開盤價位置，若開盤在中軸 {_fp(mid_range)} 下方，等第一根5分K收盤確認再決定方向，不搶第一根。")
    lines.append("")

    # 關鍵價位
    lines.append("關鍵價位")
    lines.append(f"Call wall：{_fp(call_wall)}（突破才追多）")
    lines.append(f"中軸：{_fp(mid_range)}（區間內參考）")
    lines.append(f"Pivot：{_fp(pivot)}")
    lines.append(f"Put wall：{_fp(put_wall)}（跌破才追空）")
    lines.append("")

    # 時段操作指引
    lines.append("時段操作指引")
    lines.append("08:45-09:30：觀察開盤缺口，不追第一根")
    lines.append(f"09:30-11:30：主力時段，反彈到中軸 {_fp(mid_range)} 量縮是空方進場點")
    lines.append("13:00-13:45：注意外資尾盤方向，觀察是否大量加倉")
    lines.append("")

    # 選擇權
    lines.append("選擇權")
    lines.append(f"Call wall {_fp(call_wall)} 壓頂，多單目標不超過 {call_target}")
    lines.append(f"Put wall {_fp(put_wall)} 支撐，跌破才加速")
    lines.append(f"Max Pain {_fp(max_pain)}，{_pain_label(max_pain, close_price)}")
    lines.append("")

    # 籌碼數據
    lines.append("籌碼數據")
    lines.append(f"外資期貨：{fn_level} {fn:+,}口｜1日 {fn_1d:+,}｜3日 {fn_3d:+,}")
    lines.append(f"現貨外資：{_spot_dir(spot_val)} {spot_val:+.1f}億｜5日累計 {spot_5d:+.1f}億")
    lines.append(
        f"Call wall：{_fp(call_wall)}（OI {call_wall_oi or 'N/A'}）"
        f"｜Put wall：{_fp(put_wall)}（OI {put_wall_oi or 'N/A'}）"
    )
    lines.append(
        f"C/P比：{call_put_ratio or 'N/A'}｜現價位置：{pos_pct_str}%（{_pos_label(price_position_pct)}）"
    )
    lines.append(f"情緒評分：{score_str} {sentiment_bias}｜Fear&Greed：{fear_greed} {fear_greed_emo}")
    for w in warnings:
        lines.append(str(w))
    try:
        fg_val = int(fear_greed) if fear_greed not in (None, "N/A") else 0
        s_score = int(sentiment_score) if sentiment_score is not None else 0
        if fg_val > 60 and s_score <= -3:
            if not any("散戶貪婪" in str(w) for w in warnings):
                lines.append(f"⚠️ 散戶貪婪({fg_val})+外資偏空({s_score:+d}分）：歷史上這個組合往往是短期高點特徵，做多需極度謹慎")
    except Exception:
        pass
    lines.append("")

    # 前日資料
    lines.append("前日資料")
    lines.append(f"H {_fp(high)} / L {_fp(low)} / C {_fp(close_price)}｜量 {volume or 'N/A'}")
    lines.append(f"R1：{_fp(r1)}｜Pivot：{_fp(pivot)}｜S1：{_fp(s1)}")

    # 夜盤背景（有資料才顯示）
    night_line = night_context_text.strip().split("\n")[0] if night_context_text.strip() else ""
    if night_line and "無夜盤" not in night_line and "N/A" not in night_line:
        lines.append("")
        lines.append(night_line)

    # AI判斷
    lines.append("")
    lines.append("AI判斷：")
    lines.append(ai_judgment if ai_judgment else "（AI 分析暫不可用）")
    lines.append("")

    # 今日禁止
    lines.append("今日禁止")
    lines.append("✗ 開盤第一根追單")
    lines.append(f"✗ 卡在 {_fp(put_wall)}～{_fp(call_wall)} 區間硬猜方向")
    lines.append(f"✗ 不等 Put wall {_fp(put_wall)} 跌破確認就追空")

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
