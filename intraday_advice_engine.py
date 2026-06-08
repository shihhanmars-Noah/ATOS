# intraday_advice_engine.py

from datetime import datetime

from utils import format_price


def build_option_stop_text(direction: str = "CALL") -> str:
    """
    建立選擇權停損文字。
    使用使用者既定規則：
    停損 = 進場成本減少 30%
    """

    direction_text = "Call" if direction.upper() == "CALL" else "Put"

    return (
        f"{direction_text} 停損規則：\n"
        "● 進場價 20 → 停損 14\n"
        "● 進場價 30 → 停損 21\n"
        "● 任何進場成本，一律用成本 -30% 作為停損"
    )


def build_intraday_ai_advice(
    price: float | None,
    flip: float | None,
    pivot: float | None,
    r1: float | None,
    s1: float | None,
    sentiment: str = "",
    behavior: str = "",
    trap: str | None = None,
    sweep: str | None = None,
    is_realtime: bool = False,
) -> dict:
    """
    建立盤中 AI 即時建議。

    注意：
    - 不新增點位
    - 不自行預測
    - 只根據已存在的規則點位與狀態產生白話建議
    """

    if price is None:
        return {
            "title": "⚪ 無即時資料",
            "action": "不進場",
            "summary": "目前沒有有效即時價格，ATOS 不做交易判斷。",
            "do_now": [
                "等待即時資料恢復",
                "不要用舊價格或昨收價進場",
                "等待下一次有效 snapshot",
            ],
            "option": "Call / Put 都不觀察。",
            "final": "資料不完整，現在禁止交易。",
        }

    if not is_realtime:
        return {
            "title": "⚪ 非即時資料",
            "action": "不進場",
            "summary": "目前價格來源不是真正台指期即時資料，不能作為進場依據。",
            "do_now": [
                "只允許觀察，不允許進場",
                "等待 FINMIND_FUTURES_SNAPSHOT 恢復",
                "不要用 Fugle IX0001 或 previousClose 做交易判斷",
            ],
            "option": "Call / Put 都不進。",
            "final": "非 realtime，禁止交易。",
        }

    if not flip or not pivot or not r1 or not s1:
        return {
            "title": "⚪ 關鍵價不足",
            "action": "不進場",
            "summary": "中軸 / Pivot / R1 / S1 尚未完整建立，盤中判斷不成立。",
            "do_now": [
                "等待關鍵價位重新建立",
                "不要只看現價進場",
                "等下一次盤前或資料更新",
            ],
            "option": "Call / Put 都不進。",
            "final": "戰場地圖不完整，禁止交易。",
        }

    price = float(price)
    flip = float(flip)
    pivot = float(pivot)
    r1 = float(r1)
    s1 = float(s1)

    distance_to_flip = price - flip
    distance_to_r1 = r1 - price
    distance_to_s1 = price - s1

    sentiment_text = str(sentiment)

    foreign_bearish = "強空" in sentiment_text or "淨空" in sentiment_text
    foreign_bullish = "強多" in sentiment_text or "淨多" in sentiment_text

    # --------------------------------------------------
    # Sweep / Trap 優先
    # --------------------------------------------------

    if sweep == "BEARISH_SWEEP":
        return {
            "title": "⚠️ 上方掃單 / 假突破風險",
            "action": "不追多，等待反轉確認",
            "summary": "價格可能出現上方掃單，現在不適合追 Call 或追多。",
            "do_now": [
                "觀察是否收回壓力區下方",
                "如果 5分K 留上影並跌回關鍵價下方，才觀察反打",
                "沒有明確轉弱前，不提前猜空",
            ],
            "option": (
                "Call：禁止追高。\n"
                "Put：等確認轉弱後才觀察。"
            ),
            "final": "現在重點不是追突破，而是確認是不是假突破。",
        }

    if sweep == "BULLISH_SWEEP":
        return {
            "title": "⚠️ 下方掃單 / 假跌破風險",
            "action": "不追空，等待收回確認",
            "summary": "價格可能出現下方掃單，現在不適合追 Put 或追空。",
            "do_now": [
                "觀察是否重新站回支撐或中軸",
                "如果 5分K 收回關鍵價上方，才觀察反打",
                "沒有明確轉強前，不提前猜多",
            ],
            "option": (
                "Put：禁止追低。\n"
                "Call：等確認收回後才觀察。"
            ),
            "final": "現在重點不是追跌，而是確認是不是假跌破。",
        }

    if trap == "LONG_TRAP":
        return {
            "title": "🚨 多方陷阱",
            "action": "取消追多，觀察反手空方機會",
            "summary": "目前狀態偏向多方假突破，追多風險提高。",
            "do_now": [
                "不要追 Call",
                "觀察是否跌回中軸 / Pivot 下方",
                "若反彈無法重新站回關鍵價，可觀察 Put",
            ],
            "option": (
                "Call：取消觀察。\n"
                "Put：等待反彈不過後觀察。"
            ),
            "final": "現在不是追多點，是檢查多方是否失敗。",
        }

    if trap == "SHORT_TRAP":
        return {
            "title": "🚨 空方陷阱",
            "action": "取消追空，觀察反手多方機會",
            "summary": "目前狀態偏向空方假跌破，追空風險提高。",
            "do_now": [
                "不要追 Put",
                "觀察是否重新站回中軸 / Pivot",
                "若回測不破，可觀察 Call",
            ],
            "option": (
                "Put：取消觀察。\n"
                "Call：等待回測不破後觀察。"
            ),
            "final": "現在不是追空點，是檢查空方是否失敗。",
        }

    # --------------------------------------------------
    # 接近 R1
    # --------------------------------------------------

    if 0 <= distance_to_r1 <= 50:
        return {
            "title": "🟠 接近上方壓力 R1",
            "action": "多單不追，觀察是否假突破",
            "summary": "價格已接近上方壓力，這裡追高的風險大於優勢。",
            "do_now": [
                f"觀察 {format_price(r1)} 附近是否突破有量",
                "如果突破後站不穩，容易形成假突破",
                "如果回測不破，再評估是否續強",
            ],
            "option": (
                "Call：不追高，只能等回測不破。\n"
                "Put：若突破失敗並跌回壓力下方，可觀察反打。"
            ),
            "final": "現在不是追高點，是壓力測試點。",
        }

    # --------------------------------------------------
    # 接近 S1
    # --------------------------------------------------

    if 0 <= distance_to_s1 <= 50:
        return {
            "title": "🟠 接近下方支撐 S1",
            "action": "空單不追，觀察是否假跌破",
            "summary": "價格已接近下方支撐，這裡追空容易被反彈洗掉。",
            "do_now": [
                f"觀察 {format_price(s1)} 附近是否跌破有延續",
                "如果跌破後收回，容易形成假跌破",
                "如果反彈不過支撐轉壓，再評估是否續弱",
            ],
            "option": (
                "Put：不追低，只能等反彈不過。\n"
                "Call：若跌破失敗並收回支撐，可觀察反打。"
            ),
            "final": "現在不是追空點，是支撐測試點。",
        }

    # --------------------------------------------------
    # 站上 Flip
    # --------------------------------------------------

    if price > flip + 20:
        if foreign_bearish:
            return {
                "title": "🟡 站上中軸，但籌碼偏空",
                "action": "等回測，不追多",
                "summary": "價格站上 Flip，但外資期貨偏空，這種位置容易出現拉高洗盤。",
                "do_now": [
                    f"等待回測 {format_price(flip)} 不破",
                    "至少等一根 5分K 確認",
                    "如果收回 Flip 下方，取消多方劇本",
                ],
                "option": (
                    "Call：只觀察回測不破後的機會。\n"
                    "Put：暫不追，等跌回 Flip 下方再觀察。"
                ),
                "final": "現在不是追多點，是等待回測確認點。",
            }

        return {
            "title": "🟢 站上中軸",
            "action": "偏多觀察，等回測確認",
            "summary": "價格站上多空分界，短線偏多，但仍不建議直接追高。",
            "do_now": [
                f"等待回測 {format_price(flip)} 不破",
                f"上方先看 {format_price(r1)} 壓力",
                "如果 5分K 收回中軸下方，取消多方劇本",
            ],
            "option": (
                "Call：回測不破後才觀察。\n"
                "Put：暫不觀察。"
            ),
            "final": "偏多，但要等回測，不追高。",
        }

    # --------------------------------------------------
    # 跌破 Flip
    # --------------------------------------------------

    if price < flip - 20:
        if foreign_bullish:
            return {
                "title": "🟡 跌破中軸，但籌碼偏多",
                "action": "等反彈確認，不追空",
                "summary": "價格跌破中軸，但外資期貨偏多，急跌後追空容易被反彈洗掉。",
                "do_now": [
                    f"等待反彈測試 {format_price(flip)}",
                    "如果反彈不過，再觀察空方延續",
                    "如果重新站回中軸，取消空方劇本",
                ],
                "option": (
                    "Put：只觀察反彈不過後的機會。\n"
                    "Call：暫不追，等重新站回中軸再觀察。"
                ),
                "final": "現在不是追空點，是等待反彈確認點。",
            }

        return {
            "title": "🔴 跌破中軸",
            "action": "偏空觀察，等反彈不過",
            "summary": "價格跌破多空分界，短線偏空，但仍不建議急跌後直接追空。",
            "do_now": [
                f"等待反彈測試 {format_price(flip)}",
                f"下方先看 {format_price(pivot)} / {format_price(s1)}",
                "如果 5分K 重新站回中軸，取消空方劇本",
            ],
            "option": (
                "Put：反彈不過後才觀察。\n"
                "Call：暫不觀察。"
            ),
            "final": "偏空，但要等反彈，不追低。",
        }

    # --------------------------------------------------
    # 中性區
    # --------------------------------------------------

    lower = min(flip, pivot)
    upper = max(flip, pivot)

    if lower <= price <= upper:
        return {
            "title": "🟡 中性洗盤區",
            "action": "不進場",
            "summary": "價格位於中軸 / Pivot 中間，方向優勢不足，容易來回洗盤。",
            "do_now": [
                f"等待突破 {format_price(upper)}",
                f"或跌破 {format_price(lower)}",
                "沒有脫離區間前，不做方向單",
            ],
            "option": (
                "Call：不進。\n"
                "Put：不進。"
            ),
            "final": "卡在中間，不做。",
        }

    # --------------------------------------------------
    # 預設
    # --------------------------------------------------

    return {
        "title": "⚪ 無明確優勢",
        "action": "等待",
        "summary": "目前價格沒有觸發明確劇本，等待下一個關鍵價位。",
        "do_now": [
            "不要急著進場",
            "等待靠近中軸 / Pivot / R1 / S1",
            "等 5分K 確認後再判斷",
        ],
        "option": (
            "Call：等待。\n"
            "Put：等待。"
        ),
        "final": "沒有優勢，不做。",
    }


def format_intraday_ai_advice(advice: dict) -> str:
    """
    將 AI 建議 dict 轉成 Telegram 文字。
    """

    do_now = advice.get("do_now", [])

    do_now_text = ""

    for idx, item in enumerate(do_now, start=1):
        do_now_text += f"{idx}. {item}\n"

    msg = (
        "🤖 AI 即時建議\n"
        f"狀態：{advice.get('title', 'N/A')}\n"
        f"現在動作：{advice.get('action', '等待')}\n\n"

        "判斷原因：\n"
        f"{advice.get('summary', 'N/A')}\n\n"

        "現在你應該做：\n"
        f"{do_now_text}\n"

        "選擇權觀察：\n"
        f"{advice.get('option', 'N/A')}\n\n"

        "最終指令：\n"
        f"> {advice.get('final', '等待')}"
    )

    return msg


def build_intraday_ai_advice_text(
    price: float | None,
    flip: float | None,
    pivot: float | None,
    r1: float | None,
    s1: float | None,
    sentiment: str = "",
    behavior: str = "",
    trap: str | None = None,
    sweep: str | None = None,
    is_realtime: bool = False,
) -> str:
    """
    對外使用的主函式。
    """

    advice = build_intraday_ai_advice(
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

    return format_intraday_ai_advice(advice)


# --------------------------------------------------
# Manual Test
# --------------------------------------------------

if __name__ == "__main__":
    text = build_intraday_ai_advice_text(
        price=42380,
        flip=42359,
        pivot=42150,
        r1=42586,
        s1=41922,
        sentiment="🔴 強空，外資期貨淨空 -51068 口",
        behavior="NO_TRAP",
        trap=None,
        sweep=None,
        is_realtime=True,
    )

    print(text)