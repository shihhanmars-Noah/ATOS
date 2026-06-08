# performance_engine.py
#
# 週度策略績效計算與報告。
# 每週日 20:00 由 main_commander 觸發。
#
# 功能：
#   - 計算近 N 天的勝率 / 期望值 / 訊號類型分析
#   - 被洗出場偵測（停損後反向 100 點）
#   - Gemini 策略修正建議
#   - 自動寫入 CLAUDE.md 策略修正歷史

import os
from datetime import datetime, timedelta

from trade_logger import load_trade_log
from messenger import send_to_telegram


# --------------------------------------------------
# 績效計算
# --------------------------------------------------

def calculate_weekly_performance(days: int = 7) -> dict:
    """
    計算近 N 天已追蹤訊號的策略績效。

    Returns:
        含勝率/期望值/分類分析的 dict；total=0 時表示無數據。
    """
    logs = load_trade_log()
    cutoff = datetime.now() - timedelta(days=days)

    recent = [
        r for r in logs
        if r.get("outcome") and
        datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M:%S") >= cutoff
    ]

    if not recent:
        return {"total": 0, "message": "本週無已追蹤訊號"}

    total  = len(recent)
    wins   = [r for r in recent if "WIN" in (r["outcome"].get("result", ""))]
    losses = [r for r in recent if r["outcome"].get("result") == "STOP_LOSS"]

    win_rate = len(wins) / total * 100
    avg_win  = (
        sum(r["outcome"]["pnl_points"] for r in wins) / len(wins)
        if wins else 0
    )
    avg_loss = (
        sum(r["outcome"]["pnl_points"] for r in losses) / len(losses)
        if losses else 0
    )
    expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

    # 依訊號類型
    by_type: dict = {}
    for r in recent:
        t = r.get("signal_type", "UNKNOWN")
        if t not in by_type:
            by_type[t] = {"total": 0, "wins": 0}
        by_type[t]["total"] += 1
        if "WIN" in (r["outcome"].get("result", "")):
            by_type[t]["wins"] += 1

    # 依信心分
    by_confidence: dict = {}
    for r in recent:
        c = str(r.get("confidence", 0))
        if c not in by_confidence:
            by_confidence[c] = {"total": 0, "wins": 0}
        by_confidence[c]["total"] += 1
        if "WIN" in (r["outcome"].get("result", "")):
            by_confidence[c]["wins"] += 1

    # 依時段
    by_session: dict = {}
    for r in recent:
        s = r.get("session", "UNKNOWN")
        if s not in by_session:
            by_session[s] = {"total": 0, "wins": 0}
        by_session[s]["total"] += 1
        if "WIN" in (r["outcome"].get("result", "")):
            by_session[s]["wins"] += 1

    # 被洗出場偵測（停損後 60m 追蹤顯示反向超過 100 點）
    washed = []
    for r in losses:
        track_60m = r.get("tracked_at_60m")
        if not track_60m:
            continue
        stop       = float(r["stop_loss"])
        price_60m  = float(track_60m.get("price_at_track", stop))
        direction  = r.get("direction", "")
        if direction == "SHORT" and price_60m < stop - 100:
            washed.append(r)
        elif direction == "LONG" and price_60m > stop + 100:
            washed.append(r)

    return {
        "total":         total,
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(win_rate, 1),
        "avg_win":       round(avg_win, 1),
        "avg_loss":      round(avg_loss, 1),
        "expectancy":    round(expectancy, 1),
        "by_type":       by_type,
        "by_confidence": by_confidence,
        "by_session":    by_session,
        "washed_count":  len(washed),
        "recent":        recent,
    }


# --------------------------------------------------
# 報告文字
# --------------------------------------------------

def build_performance_report(perf: dict) -> str:
    """組成週度績效報告純文字。"""
    if perf.get("total", 0) == 0:
        return "本週無已追蹤訊號，尚無績效數據。"

    now = datetime.now().strftime("%Y-%m-%d")

    lines = [
        f"ATOS 週度績效檢討 {now}",
        "",
        "本週訊號統計",
        f"發出指令：{perf['total']}次",
        f"勝率：{perf['win_rate']}%（{perf['wins']}勝{perf['losses']}負）",
        f"平均獲利：{perf['avg_win']}點  平均虧損：{perf['avg_loss']}點",
        f"期望值：{perf['expectancy']:+.1f}點/次",
        "",
        "訊號類型分析",
    ]

    for t, data in perf["by_type"].items():
        wr = data["wins"] / data["total"] * 100 if data["total"] > 0 else 0
        lines.append(f"{t}：{wr:.0f}% 勝率（{data['wins']}/{data['total']}）")

    lines.extend(["", "信心分分析"])
    for c in sorted(perf["by_confidence"].keys()):
        data = perf["by_confidence"][c]
        wr = data["wins"] / data["total"] * 100 if data["total"] > 0 else 0
        lines.append(f"信心分{c}：{wr:.0f}% 勝率（{data['wins']}/{data['total']}）")

    lines.extend(["", "時段分析"])
    session_names = {
        "DAY":         "日盤 09:30-13:45",
        "NIGHT_EARLY": "夜盤 15:00-23:59",
        "NIGHT_LATE":  "夜盤 00:00-08:45",
    }
    for s, data in perf["by_session"].items():
        wr = data["wins"] / data["total"] * 100 if data["total"] > 0 else 0
        name = session_names.get(s, s)
        lines.append(f"{name}：{wr:.0f}% 勝率（{data['wins']}/{data['total']}）")

    if perf["washed_count"] > 0:
        lines.extend([
            "",
            f"疑似被洗：{perf['washed_count']}次停損後反向走超過100點",
            "建議：考慮擴大停損或改用ATR動態停損",
        ])

    return "\n".join(lines)


# --------------------------------------------------
# AI 建議
# --------------------------------------------------

def generate_ai_suggestions(perf: dict, report_text: str) -> str:
    """送 Gemini 分析績效，取得策略修正建議。失敗時回傳靜態文字。"""
    try:
        import google.generativeai as genai
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        model = genai.GenerativeModel("gemini-2.5-flash-preview-05-20")

        prompt = (
            "你是台指期策略分析師。以下是一套自動交易系統本週的績效數據：\n\n"
            f"{report_text}\n\n"
            "請根據以上數據給出 3-5 條具體的策略修正建議。\n"
            "要求：\n"
            "1. 建議要具體可執行，不要說「需要更多數據」\n"
            "2. 聚焦在勝率最低的訊號類型和信心分\n"
            "3. 若有被洗現象，給出停損調整建議\n"
            "4. 每條建議不超過2行\n"
            "5. 格式：建議1：...  建議2：..."
        )

        response = model.generate_content(prompt)
        return response.text.strip()

    except Exception as e:
        print(f"⚠️ [performance_engine] Gemini 績效分析失敗：{e}")
        return "（AI 建議暫時無法取得）"


# --------------------------------------------------
# CLAUDE.md 策略修正歷史
# --------------------------------------------------

def update_claude_md_with_suggestions(suggestions: str) -> None:
    """把 Gemini 的策略建議插入 CLAUDE.md 的策略修正歷史章節。"""
    try:
        claude_md_path = "CLAUDE.md"
        if not os.path.exists(claude_md_path):
            return

        with open(claude_md_path, 'r', encoding='utf-8') as f:
            content = f.read()

        now = datetime.now().strftime("%Y-%m-%d")
        new_section = (
            f"\n### 策略修正紀錄 {now}\n\n"
            f"{suggestions}\n\n"
            "---\n"
        )

        if "## 策略修正歷史" in content:
            content = content.replace(
                "## 策略修正歷史",
                f"## 策略修正歷史\n{new_section}",
            )
        else:
            content += f"\n\n## 策略修正歷史\n{new_section}"

        with open(claude_md_path, 'w', encoding='utf-8') as f:
            f.write(content)

        print("✅ [performance_engine] CLAUDE.md 策略修正歷史已更新")

    except Exception as e:
        print(f"⚠️ [performance_engine] 更新 CLAUDE.md 失敗：{e}")


# --------------------------------------------------
# 主入口（由 main_commander 排程呼叫）
# --------------------------------------------------

def send_weekly_performance_report() -> None:
    """產生週度績效報告 + AI 建議，發送到 Telegram 並更新 CLAUDE.md。"""
    print("🔄 [performance_engine] 產生週度績效報告...")

    perf = calculate_weekly_performance(days=7)

    if perf.get("total", 0) == 0:
        print("⚠️ [performance_engine] 本週無訊號，不發報告")
        return

    report_text  = build_performance_report(perf)
    suggestions  = generate_ai_suggestions(perf, report_text)

    full_report = (
        f"{report_text}\n\n"
        f"━━ AI 修正建議 ━━\n"
        f"{suggestions}"
    )

    send_to_telegram(full_report)
    print("✅ [performance_engine] 週度績效報告已發送")

    update_claude_md_with_suggestions(suggestions)


# --------------------------------------------------
# 手動測試
# --------------------------------------------------

if __name__ == "__main__":
    print("=== performance_engine 手動測試 ===\n")
    perf = calculate_weekly_performance()
    print(build_performance_report(perf))
