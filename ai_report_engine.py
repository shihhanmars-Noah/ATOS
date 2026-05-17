# ai_report_engine.py

import os
from datetime import datetime
from typing import Optional

from google import genai

from error_handler import safe_execute
from chip_data_engine import build_chip_context

GEMINI_MODEL = "gemini-2.0-flash"

REPORT_TYPES = {"PREOPEN_FUTURES", "PREOPEN_STOCKS", "EVENING_FUTURES", "EVENING_STOCKS"}


# --------------------------------------------------
# 資料格式化
# --------------------------------------------------

def _v(val, fmt=None, default="N/A"):
    """安全格式化單一值。"""
    if val is None:
        return default
    try:
        return format(val, fmt) if fmt else str(val)
    except Exception:
        return str(val)


def _format_chip_text(ctx: dict) -> str:
    """將 chip_context dict 轉成 prompt 用的純文字結構。"""
    lines = []

    # 外資期貨
    fn = ctx.get("foreign_net", 0)
    lines.append(f"外資期貨淨部位：{fn:+,}口（{ctx.get('foreign_net_level', 'N/A')}）")
    lines.append(f"  多單：{ctx.get('foreign_long', 0):,} / 空單：{ctx.get('foreign_short', 0):,}")
    lines.append(
        f"  1日變化：{ctx.get('foreign_net_chg_1d', 0):+,} / "
        f"3日趨勢：{ctx.get('foreign_net_chg_3d', 0):+,}"
    )
    lines.append(f"  今日動作：{ctx.get('foreign_action', 'N/A')}")

    h7 = ctx.get("history_7d", [])
    if h7:
        lines.append(f"  7日淨部位歷史：{h7}")

    # 外資成本估算
    cost = ctx.get("foreign_cost_estimate")
    if cost:
        lines.append(f"外資空單估算成本：{cost}（{ctx.get('foreign_cost_note', '')}）")

    # 現貨法人
    lines.append(
        f"外資現貨：{ctx.get('spot_foreign_net_buy_bn', 0):+.1f}億"
        f"（5日累計：{ctx.get('spot_foreign_5d_sum_bn', 0):+.1f}億）"
    )
    lines.append(f"投信現貨：{ctx.get('spot_trust_net_buy_bn', 0):+.1f}億")
    lines.append(f"期現方向：{ctx.get('spot_vs_futures', 'N/A')}")

    # OI 框架
    lines.append(
        f"Call牆：{_v(ctx.get('call_wall'))}（OI {_v(ctx.get('call_wall_oi'))}）  "
        f"Put牆：{_v(ctx.get('put_wall'))}（OI {_v(ctx.get('put_wall_oi'))}）"
    )
    lines.append(
        f"C/P比：{_v(ctx.get('call_put_ratio'))} / "
        f"MaxPain：{_v(ctx.get('max_pain'))} / "
        f"價位位置：{_v(ctx.get('price_position_pct'))}%"
    )

    # 技術層
    lines.append(
        f"前日 H/L/C：{_v(ctx.get('prev_high'))} / "
        f"{_v(ctx.get('prev_low'))} / "
        f"{_v(ctx.get('prev_close'))}"
    )
    lines.append(
        f"Pivot：{_v(ctx.get('pivot'))} / "
        f"R1：{_v(ctx.get('r1'))} / "
        f"S1：{_v(ctx.get('s1'))}"
    )
    lines.append(
        f"中軸：{_v(ctx.get('mid_range'))} / "
        f"MA5：{_v(ctx.get('ma5'))} / "
        f"MA20：{_v(ctx.get('ma20'))}"
    )
    lines.append(f"ATR5：{_v(ctx.get('atr_5d'))} / ATR20：{_v(ctx.get('atr_20d'))}")

    # 情緒評分
    score = ctx.get("sentiment_score", 0)
    lines.append(f"情緒評分：{score:+d}（{ctx.get('sentiment_bias', 'N/A')}）")
    detail = ctx.get("sentiment_detail", {})
    if detail:
        lines.append(
            f"  S1外資期貨：{_v(detail.get('s1_futures'))} / "
            f"S2動作品質：{_v(detail.get('s2_action'))} / "
            f"S3現貨確認：{_v(detail.get('s3_spot'))} / "
            f"S4OI結構：{_v(detail.get('s4_oi'))} / "
            f"S5波動率：{_v(detail.get('s5_vol'))} / "
            f"S6大額交易人：{_v(detail.get('s6_large_traders'))}"
        )

    # 大額交易人
    lt_net = ctx.get("lt_top5_net")
    if lt_net is not None:
        lines.append(
            f"大額交易人Top5淨部位：{lt_net:+,}口"
            f"（多{_v(ctx.get('lt_top5_long_pct'))}% / 空{_v(ctx.get('lt_top5_short_pct'))}%）"
            f"  市場總OI：{_v(ctx.get('lt_market_oi'))}"
        )

    # 恐懼貪婪
    fg = ctx.get("fear_greed_index")
    if fg is not None:
        lines.append(f"CNN恐懼貪婪指數：{fg}（{ctx.get('fear_greed_emotion', 'N/A')}）")

    # 警示
    warnings = ctx.get("warnings", [])
    if warnings:
        lines.append("警示訊號：")
        for w in warnings:
            lines.append(f"  {w}")

    return "\n".join(lines)


# --------------------------------------------------
# System prompt（所有報告類型共用）
# --------------------------------------------------

_SYSTEM = """你是 ATOS 系統的籌碼分析整合引擎。

規則：
1. 客觀整合籌碼資料，呈現市場結構現況
2. 指出訊號間的一致性與矛盾點
3. 不做主觀操作建議，不說「建議做多/做空」
4. 絕對不得修改規則引擎給出的任何點位數字（Pivot / R1 / S1 / Call牆 / Put牆等）
5. 輸出純文字，不使用 Markdown 符號（不用 * # - 等）
6. 使用繁體中文，語氣簡潔"""


# --------------------------------------------------
# 各報告類型的 prompt 模板
# --------------------------------------------------

_PROMPTS = {
    "PREOPEN_FUTURES": """\
根據以下籌碼資料，產生今日（{today}）早盤期貨報告。

輸出格式（依序，各段之間空一行）：
第一行固定輸出：【早盤期貨籌碼】{today}

外資期貨現況
  - 目前淨部位、方向強弱
  - 近3日趨勢是否持續或轉折
  - 今日動作解讀

OI框架
  - Call牆 / Put牆 構成的大戶操控區間
  - 目前價位在區間內的相對位置（price_position_pct 的意義）
  - C/P比與MaxPain的含義

技術關鍵價位（原封不動引用，不得修改數字）
  - Pivot / R1 / S1 / 中軸

情緒評分解讀
  - 總分與各分項的主要貢獻/拖累
  - 大額交易人與外資是否同向

今日情境摘要（150字以內）
  - 根據籌碼呈現今日市場結構重點
  - 不做操作建議

若有警示訊號，最後獨立一段列出

籌碼資料：
{chip_text}""",

    "PREOPEN_STOCKS": """\
根據以下籌碼資料，產生今日（{today}）早盤選股背景分析。

輸出格式（依序，各段之間空一行）：
第一行固定輸出：【早盤選股背景】{today}

大盤籌碼氛圍
  - 外資現貨今日與5日累計流向
  - 投信方向
  - 期現同向或背離

大戶框架對大盤的含義
  - Call牆 / Put牆 上下界
  - 目前價位位置（偏低端/中段/高端）

市場情緒
  - 恐懼貪婪指數（若有）
  - 情緒評分總分

選股情境（100字以內）
  - 根據目前籌碼方向，哪類結構的股票較有利
  - 不點名個股

若有警示訊號，最後獨立一段列出

籌碼資料：
{chip_text}""",

    "EVENING_FUTURES": """\
根據以下籌碼資料，產生今日（{today}）晚盤期貨複盤報告。

輸出格式（依序，各段之間空一行）：
第一行固定輸出：【晚盤期貨複盤】{today}

今日籌碼回顧
  - 外資期貨今日淨部位變化
  - 現貨法人同向或背離
  - 期現方向一致性

OI框架驗證
  - 今日收盤相對Call牆 / Put牆的位置
  - 區間是否有效（價格是否留在Put牆與Call牆之間）

情緒評分總結
  - 今日評分各分項解讀

明日觀察重點（100字以內）
  - 根據今日籌碼，明日需注意的結構變化
  - 不做預測，只列出需要觀察的條件

若有警示訊號，最後獨立一段列出

籌碼資料：
{chip_text}""",

    "EVENING_STOCKS": """\
根據以下籌碼資料，產生今日（{today}）晚盤選股複盤分析。

輸出格式（依序，各段之間空一行）：
第一行固定輸出：【晚盤選股複盤】{today}

今日法人現貨流向
  - 外資現貨買賣超金額
  - 投信方向
  - 5日累計趨勢

大盤OI結構影響
  - Call牆 / Put牆 框架是否有變化
  - 今日收盤位置

明日選股背景提示（100字以內）
  - 根據今日籌碼方向，明日選股的結構背景
  - 不點名個股

若有警示訊號，最後獨立一段列出

籌碼資料：
{chip_text}""",
}


# --------------------------------------------------
# 主函式
# --------------------------------------------------

@safe_execute
def generate_report(report_type: str, chip_ctx: Optional[dict] = None) -> Optional[str]:
    """
    呼叫 Claude API 產生籌碼整合報告。

    Args:
        report_type: PREOPEN_FUTURES / PREOPEN_STOCKS / EVENING_FUTURES / EVENING_STOCKS
        chip_ctx:    build_chip_context() 的輸出；若 None 則自動呼叫

    Returns:
        報告純文字，失敗回傳 None
    """
    if report_type not in REPORT_TYPES:
        print(f"⚠️ [ai_report_engine] 不支援的報告類型：{report_type}")
        return None

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("⚠️ [ai_report_engine] GEMINI_API_KEY 未設定")
        return None

    if chip_ctx is None:
        chip_ctx = build_chip_context()

    if not chip_ctx or chip_ctx.get("error"):
        print(f"⚠️ [ai_report_engine] chip_ctx 無效：{chip_ctx}")
        return None

    today = datetime.now().strftime("%Y-%m-%d")
    chip_text = _format_chip_text(chip_ctx)

    # 用 replace 避免 chip_text 內的 {} 干擾 .format()
    user_prompt = (
        _PROMPTS[report_type]
        .replace("{today}", today)
        .replace("{chip_text}", chip_text)
    )

    full_prompt = f"{_SYSTEM}\n\n{user_prompt}"

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=full_prompt,
    )

    text = response.text.strip()
    print(f"✅ [ai_report_engine] {report_type} 完成（{len(text)} 字）")
    return text


# --------------------------------------------------
# 即時事件報告（INTRADAY_EVENT）
# --------------------------------------------------

_EVENT_SYSTEM = """你是 ATOS 盤中事件快評引擎。

收到重大事件時，根據事件內容與當前籌碼背景，產生簡短的台指期影響分析。

規則：
1. 只說明方向影響，不做操作建議
2. 不修改任何籌碼數字與價位
3. 純文字，不用 Markdown
4. 使用繁體中文，100字以內"""


@safe_execute
def generate_event_report(event_desc: str, chip_ctx: Optional[dict] = None) -> Optional[str]:
    """
    重大事件即時分析。

    Args:
        event_desc: 事件描述文字（新聞標題 + 來源）
        chip_ctx:   build_chip_context() 輸出；None 則自動載入

    Returns:
        事件快評文字，失敗回傳 None
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("⚠️ [ai_report_engine] GEMINI_API_KEY 未設定")
        return None

    if chip_ctx is None:
        chip_ctx = build_chip_context()

    if not chip_ctx or chip_ctx.get("error"):
        return None

    score = chip_ctx.get("sentiment_score", 0)
    chip_summary = (
        f"外資期貨：{chip_ctx.get('foreign_net', 0):+,}口（{chip_ctx.get('foreign_net_level', 'N/A')}）\n"
        f"情緒評分：{score:+d}（{chip_ctx.get('sentiment_bias', 'N/A')}）\n"
        f"Call牆：{chip_ctx.get('call_wall', 'N/A')} / Put牆：{chip_ctx.get('put_wall', 'N/A')}"
    )

    user_prompt = (
        f"重大事件：\n{event_desc}\n\n"
        f"當前籌碼背景：\n{chip_summary}\n\n"
        "請分析此事件對台指期的短期影響方向（100字以內）："
    )

    full_prompt = f"{_EVENT_SYSTEM}\n\n{user_prompt}"

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=full_prompt,
    )

    text = response.text.strip()
    print(f"✅ [ai_report_engine] INTRADAY_EVENT 快評完成（{len(text)} 字）")
    return text


# --------------------------------------------------
# 個股快評（STOCK_COMMENTARY）
# --------------------------------------------------

_STOCK_COMMENTARY_SYSTEM = """你是 ATOS 個股籌碼解讀員。
根據個股技術指標和法人籌碼，用2-3行白話說明該股目前技術位置和籌碼狀況。
規則：
1. 不做操作建議，不說買進/賣出/做多/做空
2. 純文字，不用 Markdown 符號
3. 使用繁體中文
4. 100字以內"""


@safe_execute
def generate_stock_commentary(stock_item: dict, chip_ctx: Optional[dict] = None) -> Optional[str]:
    """
    為單一個股產生 2-3 行白話解讀。

    Args:
        stock_item: build_stock_watchlist() 輸出的單一個股 dict
        chip_ctx:   目前不使用，保留參數供日後擴充

    Returns:
        2-3 行白話解讀，失敗回傳 None
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    stock_id = stock_item.get("id") or stock_item.get("stock_id", "N/A")
    tech = stock_item.get("tech", {}) or {}
    chip = stock_item.get("chip", {}) or {}
    scores = stock_item.get("scores", {}) or {}

    user_prompt = (
        f"股票：{stock_id}\n"
        f"收盤：{tech.get('close', 'N/A')}｜5MA：{tech.get('ma5', 'N/A')}｜"
        f"10MA：{tech.get('ma10', 'N/A')}｜20MA：{tech.get('ma20', 'N/A')}\n"
        f"量比：{tech.get('volume_ratio', 'N/A')}｜上影比：{tech.get('upper_shadow_ratio', 'N/A')}\n"
        f"距5MA：{tech.get('distance_to_ma5', 'N/A')}%｜距10MA：{tech.get('distance_to_ma10', 'N/A')}%\n"
        f"投信淨買：{chip.get('trust_net_buy', 'N/A')}｜外資淨買：{chip.get('foreign_net_buy', 'N/A')}｜"
        f"投信連買：{chip.get('consecutive_trust_buy_days', 'N/A')} 天\n"
        f"總分：{scores.get('total_score', 'N/A')}"
        f"（籌碼{scores.get('chip_score', 'N/A')}｜趨勢{scores.get('trend_score', 'N/A')}｜量能{scores.get('volume_score', 'N/A')}）\n\n"
        "請用2-3行白話說明此股目前技術位置和籌碼狀況："
    )

    full_prompt = f"{_STOCK_COMMENTARY_SYSTEM}\n\n{user_prompt}"

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=full_prompt,
    )

    text = response.text.strip()
    print(f"✅ [ai_report_engine] 個股 {stock_id} 快評完成（{len(text)} 字）")
    return text


# --------------------------------------------------
# 個股選股市場快評（CHIP_MARKET_COMMENTARY）
# --------------------------------------------------

_CHIP_MARKET_COMMENTARY_SYSTEM = """你是 ATOS 盤後籌碼解讀員。
根據目前台指期籌碼，用2-3行白話說明大戶動向和對個股操作的影響。
規則：
1. 聚焦外資期貨方向、OI框架和情緒評分對個股選股的含義
2. 不做操作建議，不說做多/做空/買進/賣出
3. 純文字，不用 Markdown 符號
4. 使用繁體中文，80字以內"""


@safe_execute
def generate_chip_market_commentary(chip_ctx: Optional[dict] = None) -> Optional[str]:
    """
    根據籌碼背景產生2-3行市場快評，供個股報告市場狀態區塊使用。
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    if chip_ctx is None:
        chip_ctx = build_chip_context()
    if not chip_ctx:
        return None

    fn = chip_ctx.get("foreign_net", 0)
    score = chip_ctx.get("sentiment_score", 0)
    user_prompt = (
        f"外資期貨：{fn:+,}口（{chip_ctx.get('foreign_net_level', 'N/A')}）\n"
        f"情緒評分：{score:+d}（{chip_ctx.get('sentiment_bias', 'N/A')}）\n"
        f"Call牆：{chip_ctx.get('call_wall', 'N/A')}｜Put牆：{chip_ctx.get('put_wall', 'N/A')}\n"
        f"現價位置：區間{chip_ctx.get('price_position_pct', 'N/A')}%\n"
        f"外資現貨：{chip_ctx.get('spot_foreign_net_buy_bn', 0):+.1f}億\n\n"
        "請用2-3行白話說明大戶動向，以及目前籌碼環境對個股操作的影響："
    )

    full_prompt = f"{_CHIP_MARKET_COMMENTARY_SYSTEM}\n\n{user_prompt}"

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(model=GEMINI_MODEL, contents=full_prompt)
    text = response.text.strip()
    print(f"✅ [ai_report_engine] 市場快評完成（{len(text)} 字）")
    return text


# --------------------------------------------------
# 手動測試
# --------------------------------------------------

if __name__ == "__main__":
    import sys

    report_type = sys.argv[1] if len(sys.argv) > 1 else "PREOPEN_FUTURES"

    print(f"\n=== ai_report_engine 測試：{report_type} ===\n")
    ctx = build_chip_context()
    print("--- chip_context 載入完成 ---")
    print(f"  情緒評分：{ctx.get('sentiment_score')} / 外資淨：{ctx.get('foreign_net')}\n")

    report = generate_report(report_type, ctx)
    if report:
        print(report)
    else:
        print("⚠️ 報告產生失敗")
