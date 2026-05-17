# stock_report_policy_engine.py

"""
ATOS 個股報告策略分級模組

目的：
1. 根據回測結果調整個股報告語氣
2. A 級 = 優先交易候選
3. B 級 = 觀察名單，不主動進場
4. C 級 = 剔除
5. 不再採用 PULLBACK_MA / BREAK_PREV_HIGH 作為正式進場建議
"""


STOCK_STRATEGY_MODE = "DEFENSE_STOCK_MODE"

DEFAULT_ENTRY_MODE = "OPEN_NEXT_DAY"
DISABLED_ENTRY_MODES = ["PULLBACK_MA", "BREAK_PREV_HIGH"]

DEFAULT_STOP_LOSS_PCT = -3.0
DEFAULT_TAKE_PROFIT_PCT = 6.0
MAX_HOLDING_DAYS = 5


def safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def get_nested(data: dict, *keys, default=None):
    """
    安全讀取巢狀欄位。
    """

    current = data

    for key in keys:
        if not isinstance(current, dict):
            return default

        current = current.get(key)

        if current is None:
            return default

    return current


def evaluate_stock_report_policy(stock: dict, market: dict | None = None) -> dict:
    """
    根據目前回測結果，重新定義個股報告策略。

    回傳欄位：
    - report_group
    - trade_permission
    - entry_mode
    - command
    - risk_note
    """

    market = market or {}

    stock_id = stock.get("id") or stock.get("stock_id")
    grade = str(stock.get("grade", "")).upper()

    scores = stock.get("scores", {}) or {}
    tech = stock.get("tech", {}) or {}
    plan = stock.get("plan", {}) or {}

    total_score = safe_float(scores.get("total_score"))
    volume_score = safe_float(scores.get("volume_score"))
    trend_score = safe_float(scores.get("trend_score"))
    chip_score = safe_float(scores.get("chip_score"))
    market_score = safe_float(scores.get("market_score"))

    distance_to_ma5 = safe_float(tech.get("distance_to_ma5"))
    distance_to_ma10 = safe_float(tech.get("distance_to_ma10"))
    volume_ratio = safe_float(tech.get("volume_ratio"))
    close = safe_float(tech.get("close"))
    ma5 = safe_float(tech.get("ma5"))
    ma10 = safe_float(tech.get("ma10"))

    market_mode = (
        market.get("mode")
        or get_nested(stock, "market", "mode")
        or ""
    )

    market_label = (
        market.get("label")
        or get_nested(stock, "market", "label")
        or ""
    )

    base = {
        "strategy_mode": STOCK_STRATEGY_MODE,
        "stock_id": stock_id,
        "grade": grade,
        "entry_mode": DEFAULT_ENTRY_MODE,
        "disabled_entry_modes": DISABLED_ENTRY_MODES,
        "stop_loss_pct": DEFAULT_STOP_LOSS_PCT,
        "take_profit_pct": DEFAULT_TAKE_PROFIT_PCT,
        "max_holding_days": MAX_HOLDING_DAYS,
        "market_mode": market_mode,
        "market_label": market_label,
        "total_score": total_score,
        "volume_score": volume_score,
        "trend_score": trend_score,
        "chip_score": chip_score,
        "market_score": market_score,
        "distance_to_ma5": distance_to_ma5,
        "distance_to_ma10": distance_to_ma10,
        "volume_ratio": volume_ratio,
        "close": close,
        "ma5": ma5,
        "ma10": ma10,
        "observation_zone": plan.get("observation_zone"),
        "invalid": plan.get("invalid"),
    }

    # --------------------------------------------------
    # C 級：剔除
    # --------------------------------------------------
    if grade == "C":
        base.update({
            "report_group": "REJECTED",
            "trade_permission": "NO_TRADE",
            "title": "C級剔除",
            "command": "剔除，不列入交易名單。",
            "risk_note": "條件不足，禁止因單一籌碼或題材追價。",
            "report_tag": "❌ 剔除",
        })
        return base

    # --------------------------------------------------
    # B 級：觀察，不主動進場
    # --------------------------------------------------
    if grade == "B":
        reasons = []

        if total_score is not None and total_score < 70:
            reasons.append("總分未達 A 級門檻")

        if distance_to_ma5 is not None and distance_to_ma5 > 5:
            reasons.append("距離 5MA 偏遠，追價風險高")

        if market_mode == "NEUTRAL":
            reasons.append("大盤為中性 / 不可交易環境")

        if not reasons:
            reasons.append("條件尚可，但回測顯示 B 級直接交易期望值偏弱")

        base.update({
            "report_group": "WATCHLIST",
            "trade_permission": "WATCH_ONLY",
            "title": "B級觀察",
            "command": "只列觀察，不主動進場；除非後續升級為 A 級，否則不給買進語氣。",
            "risk_note": "B 級回測整體為負期望，禁止直接當成買進訊號。",
            "report_tag": "🟡 觀察",
            "policy_reasons": reasons,
        })
        return base

    # --------------------------------------------------
    # A 級：優先交易候選，但仍需防守
    # --------------------------------------------------
    if grade == "A":
        reasons = []

        reasons.append("A 級為目前回測中相對有效的分組")
        reasons.append("正式進場法只保留 OPEN_NEXT_DAY 觀察，不採用回測 MA 或突破高點進場")

        if market_mode == "NEUTRAL":
            reasons.append("但目前大盤為中性 / 不可交易環境，需降低部位或只觀察")

        if distance_to_ma5 is not None and distance_to_ma5 > 8:
            reasons.append("距離 5MA 偏遠，不可追高")

        base.update({
            "report_group": "PRIORITY",
            "trade_permission": "OPEN_OBSERVE",
            "title": "A級優先觀察",
            "command": "可列入隔日開盤優先觀察名單；只允許小部位、嚴格停損，不追高加碼。",
            "risk_note": "A 級有初步優勢，但樣本數仍不足，不可視為穩定勝率策略。",
            "report_tag": "🟢 優先",
            "policy_reasons": reasons,
        })

        # 台指期籌碼偏空時，A 級降為純觀察
        if market_mode == "BEAR_CHIP":
            base["risk_note"] = base["risk_note"] + "；當前台指期籌碼評分偏空，建議縮小部位或純觀察"
            base["trade_permission"] = "WATCH_ONLY"
            base["report_tag"] = "🟡 降級觀察（期貨偏空）"

        return base

    # --------------------------------------------------
    # 未知等級
    # --------------------------------------------------
    base.update({
        "report_group": "UNKNOWN",
        "trade_permission": "WATCH_ONLY",
        "title": "未知等級",
        "command": "等級不明，只觀察，不主動進場。",
        "risk_note": "資料欄位不完整，禁止交易。",
        "report_tag": "⚪ 未分類",
        "policy_reasons": ["grade 欄位不存在或無法辨識"],
    })

    return base


def apply_stock_report_policy_to_picks(picks_data: dict) -> dict:
    """
    將策略分級套用到 stock_picks_cache 的 data 結構。

    輸入格式：
    {
        "A": [...],
        "B": [...],
        "C": [...],
        "market": {...}
    }

    回傳：
    {
        "priority": [...],
        "watchlist": [...],
        "rejected": [...],
        "market": {...},
        "strategy_mode": ...
    }
    """

    if not isinstance(picks_data, dict):
        return {
            "priority": [],
            "watchlist": [],
            "rejected": [],
            "market": {},
            "strategy_mode": STOCK_STRATEGY_MODE,
        }

    market = picks_data.get("market", {}) or {}

    priority = []
    watchlist = []
    rejected = []

    for grade_key in ["A", "B", "C"]:
        stocks = picks_data.get(grade_key, [])

        if not isinstance(stocks, list):
            continue

        for stock in stocks:
            if not isinstance(stock, dict):
                continue

            policy = evaluate_stock_report_policy(stock, market=market)

            enriched = dict(stock)
            enriched["report_policy"] = policy

            # 同步覆蓋 plan.command，讓原本報告顯示也變保守
            plan = enriched.get("plan")

            if not isinstance(plan, dict):
                plan = {}

            plan["entry_mode"] = policy.get("entry_mode")
            plan["trade_permission"] = policy.get("trade_permission")
            plan["command"] = policy.get("command")
            plan["risk_note"] = policy.get("risk_note")
            plan["strategy_mode"] = policy.get("strategy_mode")

            enriched["plan"] = plan

            group = policy.get("report_group")

            if group == "PRIORITY":
                priority.append(enriched)
            elif group == "WATCHLIST":
                watchlist.append(enriched)
            else:
                rejected.append(enriched)

    return {
        "priority": priority,
        "watchlist": watchlist,
        "rejected": rejected,
        "market": market,
        "strategy_mode": STOCK_STRATEGY_MODE,
    }


def format_policy_reasons(policy: dict) -> str:
    reasons = policy.get("policy_reasons", [])

    if not reasons:
        return "無"

    return "\n".join([f"　- {reason}" for reason in reasons])


def build_stock_policy_report_section(policy_result: dict) -> str:
    """
    產生新版個股報告區塊。
    """

    priority = policy_result.get("priority", [])
    watchlist = policy_result.get("watchlist", [])
    rejected = policy_result.get("rejected", [])
    market = policy_result.get("market", {})

    lines = []

    lines.append("📌 個股策略模式：DEFENSE_STOCK_MODE")
    lines.append("--------------------------------------------------")
    lines.append("回測結論：")
    lines.append("● A級：優先觀察，但不可重倉")
    lines.append("● B級：只觀察，不主動進場")
    lines.append("● C級：剔除")
    lines.append("● 正式進場法暫只保留：隔日開盤觀察")
    lines.append("● 暫不採用：回測 5MA / 10MA、突破推薦日高點")
    lines.append("")

    if market:
        lines.append("大盤狀態：")
        lines.append(f"● {market.get('label', market.get('mode', 'N/A'))}")
        lines.append("")

    lines.append("🟢 A級：優先交易候選")
    lines.append("--------------------------------------------------")

    if not priority:
        lines.append("本次無 A 級優先候選。")
    else:
        for stock in priority:
            policy = stock.get("report_policy", {})
            stock_id = stock.get("id") or stock.get("stock_id")
            grade_reason = stock.get("grade_reason", "")
            scores = stock.get("scores", {}) or {}
            tech = stock.get("tech", {}) or {}
            plan = stock.get("plan", {}) or {}

            lines.append(f"{stock_id}｜{policy.get('report_tag', '🟢 優先')}")
            lines.append(f"分數：{scores.get('total_score', 'N/A')}｜量能：{scores.get('volume_score', 'N/A')}｜趨勢：{scores.get('trend_score', 'N/A')}")
            lines.append(f"收盤：{tech.get('close', 'N/A')}｜5MA：{tech.get('ma5', 'N/A')}｜10MA：{tech.get('ma10', 'N/A')}")
            lines.append(f"原因：{grade_reason}")
            lines.append(f"策略：{plan.get('command', policy.get('command'))}")
            lines.append(f"風控：{policy.get('risk_note')}")
            lines.append("")

    lines.append("🟡 B級：觀察名單")
    lines.append("--------------------------------------------------")

    if not watchlist:
        lines.append("本次無 B 級觀察名單。")
    else:
        for stock in watchlist:
            policy = stock.get("report_policy", {})
            stock_id = stock.get("id") or stock.get("stock_id")
            grade_reason = stock.get("grade_reason", "")
            scores = stock.get("scores", {}) or {}
            tech = stock.get("tech", {}) or {}

            lines.append(f"{stock_id}｜{policy.get('report_tag', '🟡 觀察')}")
            lines.append(f"分數：{scores.get('total_score', 'N/A')}｜量能：{scores.get('volume_score', 'N/A')}｜趨勢：{scores.get('trend_score', 'N/A')}")
            lines.append(f"收盤：{tech.get('close', 'N/A')}｜距5MA：{tech.get('distance_to_ma5', 'N/A')}%")
            lines.append(f"原因：{grade_reason}")
            lines.append(f"策略：{policy.get('command')}")
            lines.append("")

    lines.append("❌ C級：剔除")
    lines.append("--------------------------------------------------")

    if not rejected:
        lines.append("本次無 C 級剔除名單。")
    else:
        rejected_ids = [
            str(stock.get("id") or stock.get("stock_id"))
            for stock in rejected
        ]
        lines.append("、".join(rejected_ids))
        lines.append("策略：剔除，不列入交易。")

    return "\n".join(lines)


if __name__ == "__main__":
    test_stock = {
        "id": "3033",
        "grade": "A",
        "grade_reason": "A級：籌碼與技術同步，優先觀察",
        "scores": {
            "total_score": 83.5,
            "chip_score": 85,
            "trend_score": 100,
            "volume_score": 90,
            "market_score": 40,
        },
        "tech": {
            "close": 46.4,
            "ma5": 44.33,
            "ma10": 41.91,
            "distance_to_ma5": 4.67,
        },
        "market": {
            "mode": "NEUTRAL",
            "label": "🟡 大盤不可交易 / 觀察",
        },
        "plan": {
            "observation_zone": "44.33 ～ 41.91",
            "invalid": "跌破 10MA 41.91",
        },
    }

    result = evaluate_stock_report_policy(test_stock)
    print(result)