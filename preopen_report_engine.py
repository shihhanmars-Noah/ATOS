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
    mid_range: float,
    pivot: float,
    r1: float,
    s1: float,
) -> dict:
    """建立開盤三劇本，以中軸(mid_range)取代舊 Flip。"""

    mid_text = format_price(mid_range)
    pivot_text = format_price(pivot)
    r1_text = format_price(r1)
    s1_text = format_price(s1)

    scenario_a = (
        f"劇本 A｜開盤站上中軸 {mid_text}\n"
        "判斷：偏多，但不追高\n"
        f"做法：等回測 {mid_text} 不破再看多\n"
        f"目標：{r1_text}\n"
        f"失效：5分K 收回 {mid_text} 下方"
    )

    scenario_b = (
        f"劇本 B｜開盤跌破中軸 {mid_text}\n"
        "判斷：偏空\n"
        f"做法：等反彈不過 {mid_text} 再看空\n"
        f"目標：{pivot_text} → {s1_text}\n"
        f"失效：5分K 重新站回 {mid_text}"
    )

    scenario_c = (
        f"劇本 C｜開盤卡在 {pivot_text}～{mid_text}\n"
        "判斷：中性洗盤區\n"
        "做法：不進場\n"
        f"等待：突破 {mid_text} 或跌破 {pivot_text}"
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

    scenarios = build_opening_scenarios(
        mid_range=mid_range,
        pivot=pivot,
        r1=r1,
        s1=s1,
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
# Message Builder
# --------------------------------------------------

def build_preopen_sip_message(payload: dict | None = None) -> str:
    """建立盤前 SIP 作戰報告。"""

    if payload is None:
        payload = build_preopen_payload()

    today = payload.get("today")
    now_time = payload.get("now_time")

    r1 = payload.get("r1")
    s1 = payload.get("s1")
    pivot = payload.get("pivot")
    mid_range = payload.get("mid_range")

    source_date = payload.get("source_date")
    contract_date = payload.get("contract_date")
    high = payload.get("previous_high")
    low = payload.get("previous_low")
    close_price = payload.get("previous_close")
    volume = payload.get("previous_volume")

    bias = payload.get("bias", {})
    scenarios = payload.get("scenarios", {})
    night_context_text = payload.get("night_context_text", "")
    chip_ctx = payload.get("chip_ctx") or {}

    mid_text = format_price(mid_range)
    pivot_text = format_price(pivot)
    r1_text = format_price(r1)
    s1_text = format_price(s1)
    high_text = format_price(high)
    low_text = format_price(low)
    close_text = format_price(close_price)

    chip_section = build_chip_section(chip_ctx)

    msg = (
        "🛡️ ATOS 盤前 SIP 作戰報告\n"
        f"日期：{today}\n"
        f"時間：{now_time}\n"
        "資料基準：前一交易日 / 最新期貨日資料\n\n"

        "━━━━━━━━━━━━━━\n"
        "一、今日核心結論\n"
        "━━━━━━━━━━━━━━\n\n"

        f"方向判斷：{bias.get('label')}\n"
        f"主控價位：{mid_text}（中軸）\n"
        "今日重點：先看開盤能不能站穩中軸\n\n"

        "指揮官一句話：\n"
        f"> {bias.get('summary')}\n\n"

        "━━━━━━━━━━━━━━\n"
        "二、夜盤背景\n"
        "━━━━━━━━━━━━━━\n\n"

        f"{night_context_text}\n\n"

        "━━━━━━━━━━━━━━\n"
        "三、今日關鍵價位\n"
        "━━━━━━━━━━━━━━\n\n"

        f"上方壓力 R1：{r1_text}\n"
        f"多空分界 中軸：{mid_text}（前日高低均值）\n"
        f"盤中重心 Pivot：{pivot_text}\n"
        f"下方支撐 S1：{s1_text}\n\n"

        "簡化地圖：\n\n"
        f"{r1_text}  ← 上方壓力，不追高\n"
        f"{mid_text}  ← 多空分界（中軸）\n"
        f"{pivot_text}  ← 盤中重心\n"
        f"{s1_text}  ← 下方支撐\n\n"

        "━━━━━━━━━━━━━━\n"
        "四、前日資料基準\n"
        "━━━━━━━━━━━━━━\n\n"

        f"資料日：{source_date}\n"
        f"主力合約：{contract_date}\n"
        f"前日高點：{high_text}\n"
        f"前日低點：{low_text}\n"
        f"前日收盤：{close_text}\n"
        f"成交量：{volume if volume is not None else 'N/A'}\n\n"

        "━━━━━━━━━━━━━━\n"
        "五、籌碼背景\n"
        "━━━━━━━━━━━━━━\n\n"

        f"{chip_section}\n\n"

        "━━━━━━━━━━━━━━\n"
        "六、開盤三劇本\n"
        "━━━━━━━━━━━━━━\n\n"

        f"{scenarios.get('A')}\n\n"
        f"{scenarios.get('B')}\n\n"
        f"{scenarios.get('C')}\n\n"

        "━━━━━━━━━━━━━━\n"
        "七、第一節操作指令\n"
        "━━━━━━━━━━━━━━\n\n"

        "08:45–09:30：\n\n"
        "1. 不開盤直接追單\n"
        "2. 第一根 5分K 先觀察\n"
        "3. 價格站上中軸，再等回測\n"
        "4. 價格跌破 Pivot，再等反彈\n"
        "5. 沒有確認，就不做\n\n"

        "━━━━━━━━━━━━━━\n"
        "八、選擇權策略\n"
        "━━━━━━━━━━━━━━\n\n"

        "Call：\n"
        "只在站穩中軸後回測不破時觀察。\n"
        "不追高價 Call。\n\n"

        "Put：\n"
        "只在跌破中軸後反彈不過時觀察。\n"
        "不在急跌後追 Put。\n\n"

        "停損規則：\n"
        "進場價 20 → 停損 14\n"
        "進場價 30 → 停損 21\n\n"

        "━━━━━━━━━━━━━━\n"
        "九、今日禁止事項\n"
        "━━━━━━━━━━━━━━\n\n"

        "禁止開盤第一根追單\n"
        f"禁止在 {pivot_text}～{mid_text} 中間硬猜\n"
        "禁止看到外資空就直接追空\n"
        "禁止看到開高就直接追多\n"
        "禁止沒有 5分K 確認就進場\n\n"

        "━━━━━━━━━━━━━━\n"
        "十、最終指令\n"
        "━━━━━━━━━━━━━━\n\n"

        f"> 今天的主控價是 {mid_text}（中軸）。\n"
        "> 站穩它，回測不破才看多。\n"
        "> 跌破它，反彈不過才看空。\n"
        "> 卡在中間，不做。"
    )

    # 十一、AI 籌碼解讀
    ai_section = _build_ai_section(chip_ctx)
    if ai_section:
        msg += f"\n\n━━━━━━━━━━━━━━\n十一、AI 籌碼解讀\n━━━━━━━━━━━━━━\n\n{ai_section}"

    return msg


def _build_ai_section(chip_ctx: dict) -> str:
    """呼叫 ai_report_engine 產生 AI 籌碼解讀段落，失敗回傳空字串。"""
    if ai_generate_report is None:
        return ""
    try:
        text = ai_generate_report("PREOPEN_FUTURES", chip_ctx)
        if text:
            return text
    except Exception as e:
        print(f"⚠️ AI 籌碼解讀失敗：{e}")
    return ""


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
