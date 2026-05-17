# transition_engine.py

def check_session_transition(
    prev_close: float,
    open_price: float,
    flip: float,
    atr: float,
) -> dict:
    """
    夜盤收盤 → 日盤開盤的跨盤狀態檢查。

    用途：
    - 檢查是否大跳空
    - 檢查是否跨過 Flip
    - 避免日盤沿用夜盤舊協議
    """

    if prev_close is None:
        return {
            "transition": True,
            "mode": "⚠️ 無夜盤收盤資料",
            "action": "無法比對跨盤狀態，日盤開盤先觀察第一根 5 分 K。",
            "gap": None,
        }

    gap = open_price - prev_close

    # -----------------------------------
    # 大跳空：超過 0.5 ATR
    # -----------------------------------
    if atr and abs(gap) > 0.5 * atr:
        return {
            "transition": True,
            "mode": "⚠️ 跨盤大跳空重估",
            "action": "暫停夜盤舊協議，嚴禁開盤衝動進場，等待 08:50 第一根 5 分 K 確認。",
            "gap": round(gap, 1),
        }

    # -----------------------------------
    # 夜空轉日多
    # -----------------------------------
    if prev_close < flip and open_price > flip:
        return {
            "transition": True,
            "mode": "🟢 夜空轉日多",
            "action": "夜盤空方協議失效，日盤重新觀察 Flip 是否轉為支撐。",
            "gap": round(gap, 1),
        }

    # -----------------------------------
    # 夜多轉日空
    # -----------------------------------
    if prev_close > flip and open_price < flip:
        return {
            "transition": True,
            "mode": "🔴 夜多轉日空",
            "action": "夜盤多方協議失效，日盤進入防守，等待空方是否延續。",
            "gap": round(gap, 1),
        }

    # -----------------------------------
    # 狀態延續
    # -----------------------------------
    return {
        "transition": False,
        "mode": "✅ 跨盤狀態延續",
        "action": "可沿用原協議，但仍需等待 08:50 第一根 5 分 K 確認。",
        "gap": round(gap, 1),
    }


def build_transition_message(transition: dict) -> str:
    """
    產生日盤開盤跨盤檢查訊息。
    """

    gap_text = (
        "無資料"
        if transition.get("gap") is None
        else f"{transition['gap']} 點"
    )

    return (
        "🌅 **ATOS 日盤開盤狀態檢查**\n"
        "---\n"
        f"模式：{transition['mode']}\n"
        f"跳空：{gap_text}\n"
        f"動作：{transition['action']}"
    )