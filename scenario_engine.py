# scenario_engine.py


def format_price(value):
    if value is None:
        return "N/A"

    try:
        return round(float(value), 1)
    except Exception:
        return value


def classify_intraday_scenario(
    price,
    flip,
    pivot,
    r1=None,
    s1=None,
):
    """
    判斷目前價格符合早盤哪一個劇本。

    劇本 A：
    站上中軸 → 偏多，但等回測

    劇本 B：
    跌破中軸 → 偏空，但等反彈

    劇本 C：
    卡在 Pivot / 中軸 中間 → 不做

    R1 / S1：
    額外標註壓力 / 支撐測試
    """

    if price is None or flip is None or pivot is None:
        return {
            "scenario": "UNKNOWN",
            "title": "⚪ 劇本無法判斷",
            "action": "等待",
            "summary": "價格或關鍵價不足，無法對應早盤劇本。",
        }

    price = float(price)
    flip = float(flip)
    pivot = float(pivot)

    lower = min(flip, pivot)
    upper = max(flip, pivot)

    # 接近 R1
    if r1 is not None:
        try:
            if abs(price - float(r1)) <= 50:
                return {
                    "scenario": "R1_TEST",
                    "title": "🟠 壓力測試劇本",
                    "action": "不追高，等突破後回測",
                    "summary": f"價格接近 R1 {format_price(r1)}，這裡是壓力測試，不是追高點。",
                }
        except Exception:
            pass

    # 接近 S1
    if s1 is not None:
        try:
            if abs(price - float(s1)) <= 50:
                return {
                    "scenario": "S1_TEST",
                    "title": "🟠 支撐測試劇本",
                    "action": "不追空，等跌破後反彈確認",
                    "summary": f"價格接近 S1 {format_price(s1)}，這裡是支撐測試，不是追空點。",
                }
        except Exception:
            pass

    if price > flip:
        return {
            "scenario": "A",
            "title": "🟢 劇本 A｜站上中軸",
            "action": "偏多觀察，但等回測",
            "summary": f"價格目前站在中軸 {format_price(flip)} 上方，符合早盤劇本 A。不要直接追高，等回測不破。",
        }

    if price < flip:
        return {
            "scenario": "B",
            "title": "🔴 劇本 B｜跌破中軸",
            "action": "偏空觀察，但等反彈",
            "summary": f"價格目前跌破中軸 {format_price(flip)}，符合早盤劇本 B。不要急跌追空，等反彈不過。",
        }

    if lower <= price <= upper:
        return {
            "scenario": "C",
            "title": "🟡 劇本 C｜中性洗盤區",
            "action": "不進場",
            "summary": f"價格位於 {format_price(lower)}～{format_price(upper)} 中間，符合早盤劇本 C，方向優勢不足。",
        }

    return {
        "scenario": "WAIT",
        "title": "⚪ 等待劇本",
        "action": "等待",
        "summary": "目前價格沒有明確對應劇本，等待下一個關鍵價。",
    }


def build_scenario_text(
    price,
    flip,
    pivot,
    r1=None,
    s1=None,
):
    """
    建立盤中訊息用劇本文字。
    """

    result = classify_intraday_scenario(
        price=price,
        flip=flip,
        pivot=pivot,
        r1=r1,
        s1=s1,
    )

    return (
        "📘 早盤劇本對應\n"
        f"目前劇本：{result['title']}\n"
        f"建議動作：{result['action']}\n\n"
        f"說明：\n"
        f"{result['summary']}"
    )