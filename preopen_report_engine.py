# preopen_report_engine.py

from datetime import datetime

from night_session_engine import build_night_context_text
from error_handler import safe_execute
from messenger import send_to_telegram
from persistent_state import load_state, save_state

from data_engine import (
    get_flip_level,
    get_dynamic_resistance_support,
    get_institutional_sentiment,
)


# --------------------------------------------------
# Helpers
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


def safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


# --------------------------------------------------
# Bias / Scenario Logic
# --------------------------------------------------

def parse_sentiment_bias(sentiment: str) -> dict:
    """
    根據法人文字判斷籌碼偏向。
    """

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
    flip: float,
    pivot: float,
    sentiment_score: int,
) -> dict:
    """
    建立盤前方向判斷。

    使用：
    - 前日收盤 vs Flip
    - 前日收盤 vs Pivot
    - 法人期貨方向
    """

    if not close_price or not flip or not pivot:
        return {
            "label": "⚪ 資料不足",
            "summary": "關鍵價位不足，今日盤前不做方向預判，只能等待開盤後確認。",
            "style": "WAIT",
        }

    close_price = float(close_price)
    flip = float(flip)
    pivot = float(pivot)

    above_flip = close_price >= flip
    above_pivot = close_price >= pivot

    if above_flip and above_pivot and sentiment_score >= 0:
        return {
            "label": "🟢 震盪偏多",
            "summary": "前日收盤站在 Flip 與 Pivot 上方，若開盤守住 Flip，可偏多觀察。",
            "style": "BULL",
        }

    if above_flip and sentiment_score < 0:
        return {
            "label": "🟡 震盪偏空防守",
            "summary": "前日收盤雖在 Flip 附近或上方，但外資期貨偏空，今天不可追多，先看 Flip 是否守住。",
            "style": "DEFENSIVE",
        }

    if not above_flip and sentiment_score <= 0:
        return {
            "label": "🔴 偏空觀察",
            "summary": "前日收盤落在 Flip 下方，且籌碼偏空，今天反彈不過 Flip 才看空。",
            "style": "BEAR",
        }

    if not above_pivot:
        return {
            "label": "🟡 中性偏弱",
            "summary": "前日收盤低於 Pivot，代表盤中重心偏弱，今天需等重新站回 Pivot / Flip 才能轉強。",
            "style": "WEAK",
        }

    return {
        "label": "🟡 中性震盪",
        "summary": "價格與籌碼沒有一致方向，今日先用 Flip / Pivot 做區間判斷，不提前猜方向。",
        "style": "NEUTRAL",
    }


def build_opening_scenarios(
    flip: float,
    pivot: float,
    r1: float,
    s1: float,
) -> dict:
    """
    建立開盤三劇本。
    """

    flip_text = format_price(flip)
    pivot_text = format_price(pivot)
    r1_text = format_price(r1)
    s1_text = format_price(s1)

    scenario_a = (
        f"劇本 A｜開盤站上 {flip_text}\n"
        "判斷：偏多，但不追高\n"
        f"做法：等回測 {flip_text} 不破再看多\n"
        f"目標：{r1_text}\n"
        f"失效：5分K 收回 {flip_text} 下方"
    )

    scenario_b = (
        f"劇本 B｜開盤跌破 {flip_text}\n"
        "判斷：偏空\n"
        f"做法：等反彈不過 {flip_text} 再看空\n"
        f"目標：{pivot_text} → {s1_text}\n"
        f"失效：5分K 重新站回 {flip_text}"
    )

    scenario_c = (
        f"劇本 C｜開盤卡在 {pivot_text}～{flip_text}\n"
        "判斷：中性洗盤區\n"
        "做法：不進場\n"
        f"等待：突破 {flip_text} 或跌破 {pivot_text}"
    )

    return {
        "A": scenario_a,
        "B": scenario_b,
        "C": scenario_c,
    }


# --------------------------------------------------
# Payload Builder
# --------------------------------------------------

def build_preopen_payload() -> dict:
    """
    建立盤前 SIP 所有資料。

    這個 payload 同時用於：
    1. 組 Telegram 盤前報告
    2. 寫入 atos_state.json，供晚盤複盤檢討
    """

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

    flip = get_flip_level(
        futures_id="TX",
        fallback=close_price or 0,
    )

    sentiment_info = parse_sentiment_bias(sentiment)

    bias = build_preopen_bias(
        close_price=close_price,
        flip=flip,
        pivot=pivot,
        sentiment_score=sentiment_info["score"],
    )

    scenarios = build_opening_scenarios(
        flip=flip,
        pivot=pivot,
        r1=r1,
        s1=s1,
    )

    night_context_text = build_night_context_text(
        flip=flip,
        pivot=pivot,
        previous_futures_close=close_price,
    )

    payload = {
        "today": today,
        "now_time": now_time,

        "levels": levels,
        "r1": r1,
        "s1": s1,
        "pivot": pivot,
        "flip": flip,

        "source_date": source_date,
        "contract_date": contract_date,
        "previous_high": high,
        "previous_low": low,
        "previous_close": close_price,
        "previous_volume": volume,

        "sentiment": sentiment,
        "sentiment_info": sentiment_info,
        "bias": bias,
        "scenarios": scenarios,
        "night_context_text": night_context_text,
    }

    return payload


# --------------------------------------------------
# State Writer
# --------------------------------------------------

@safe_execute
def save_preopen_plan_to_state(payload: dict) -> bool:
    """
    將盤前劇本寫入 atos_state.json。

    晚盤報告會檢查：
    - preopen_plan_ready
    - preopen_plan_date
    - preopen_bias
    - preopen_plan
    """

    if not isinstance(payload, dict):
        return False

    state = load_state()

    today = payload.get("today")

    r1 = payload.get("r1")
    s1 = payload.get("s1")
    pivot = payload.get("pivot")
    flip = payload.get("flip")

    bias = payload.get("bias", {}) or {}
    sentiment_info = payload.get("sentiment_info", {}) or {}
    scenarios = payload.get("scenarios", {}) or {}

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
            "以 Flip 為主控價。站穩 Flip 後回測不破才看多；"
            "跌破 Flip 後反彈不過才看空；卡在 Pivot / Flip 中間不做。"
        ),

        "key_levels": {
            "r1": safe_float(r1),
            "flip": safe_float(flip),
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
            "禁止在 Pivot / Flip 中間硬猜",
            "禁止看到外資空就直接追空",
            "禁止看到開高就直接追多",
            "禁止沒有 5分K 確認就進場",
        ],
    }

    state["preopen_plan_ready"] = True
    state["preopen_plan_date"] = today
    state["preopen_bias"] = bias.get("label")
    state["preopen_plan"] = preopen_plan

    # 同步保存 key levels，讓其他模組也可使用
    state["flip"] = safe_float(flip)
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

    # 同步保存法人方向，但 chip_ready 不在這裡打開
    # chip_ready 必須等當日籌碼資料真的更新後，由 chip_data_engine 寫入
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
    """
    建立盤前 SIP 作戰報告。
    """

    if payload is None:
        payload = build_preopen_payload()

    today = payload.get("today")
    now_time = payload.get("now_time")

    r1 = payload.get("r1")
    s1 = payload.get("s1")
    pivot = payload.get("pivot")
    flip = payload.get("flip")

    source_date = payload.get("source_date")
    contract_date = payload.get("contract_date")
    high = payload.get("previous_high")
    low = payload.get("previous_low")
    close_price = payload.get("previous_close")
    volume = payload.get("previous_volume")

    sentiment = payload.get("sentiment")
    sentiment_info = payload.get("sentiment_info", {})
    bias = payload.get("bias", {})
    scenarios = payload.get("scenarios", {})
    night_context_text = payload.get("night_context_text", "")

    flip_text = format_price(flip)
    pivot_text = format_price(pivot)
    r1_text = format_price(r1)
    s1_text = format_price(s1)
    high_text = format_price(high)
    low_text = format_price(low)
    close_text = format_price(close_price)

    msg = (
        "🛡️ ATOS 盤前 SIP 作戰報告\n"
        f"日期：{today}\n"
        f"時間：{now_time}\n"
        "資料基準：前一交易日 / 最新期貨日資料\n\n"

        "━━━━━━━━━━━━━━\n"
        "一、今日核心結論\n"
        "━━━━━━━━━━━━━━\n\n"

        f"方向判斷：{bias.get('label')}\n"
        f"主控價位：{flip_text}\n"
        "今日重點：先看開盤能不能站穩 Flip\n\n"

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
        f"多空分界 Flip：{flip_text}\n"
        f"盤中重心 Pivot：{pivot_text}\n"
        f"下方支撐 S1：{s1_text}\n\n"

        "簡化地圖：\n\n"
        f"{r1_text}  ← 上方壓力，不追高\n"
        f"{flip_text}  ← 今日多空分界\n"
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

        f"期貨法人：{sentiment}\n"
        f"籌碼解讀：{sentiment_info.get('bias')}\n\n"
        f"{sentiment_info.get('risk_note')}\n\n"

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
        "3. 價格站上 Flip，再等回測\n"
        "4. 價格跌破 Pivot，再等反彈\n"
        "5. 沒有確認，就不做\n\n"

        "━━━━━━━━━━━━━━\n"
        "八、選擇權策略\n"
        "━━━━━━━━━━━━━━\n\n"

        "Call：\n"
        "只在站穩 Flip 後回測不破時觀察。\n"
        "不追高價 Call。\n\n"

        "Put：\n"
        "只在跌破 Flip 後反彈不過時觀察。\n"
        "不在急跌後追 Put。\n\n"

        "停損規則：\n"
        "進場價 20 → 停損 14\n"
        "進場價 30 → 停損 21\n\n"

        "━━━━━━━━━━━━━━\n"
        "九、今日禁止事項\n"
        "━━━━━━━━━━━━━━\n\n"

        "禁止開盤第一根追單\n"
        f"禁止在 {pivot_text}～{flip_text} 中間硬猜\n"
        "禁止看到外資空就直接追空\n"
        "禁止看到開高就直接追多\n"
        "禁止沒有 5分K 確認就進場\n\n"

        "━━━━━━━━━━━━━━\n"
        "十、最終指令\n"
        "━━━━━━━━━━━━━━\n\n"

        f"> 今天的主控價是 {flip_text}。\n"
        "> 站穩它，回測不破才看多。\n"
        "> 跌破它，反彈不過才看空。\n"
        "> 卡在中間，不做。"
    )

    return msg


# --------------------------------------------------
# Sender
# --------------------------------------------------

@safe_execute
def send_preopen_sip_report():
    """
    發送盤前 SIP 作戰報告到 Telegram。

    流程：
    1. 建立 payload
    2. 寫入 preopen_plan 到 state
    3. 組訊息
    4. 發 Telegram
    """

    payload = build_preopen_payload()

    save_preopen_plan_to_state(payload)

    msg = build_preopen_sip_message(payload)

    return send_to_telegram(msg)


# --------------------------------------------------
# Manual Test
# --------------------------------------------------

if __name__ == "__main__":
    send_preopen_sip_report()