# ai_report_engine.py

import os
import time
from datetime import datetime
from typing import Optional

from google import genai
from google.genai import types

from error_handler import safe_execute
from chip_data_engine import build_chip_context

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_RETRY_DELAY = 5  # 429 重試等待秒數

_SYSTEM_BASE = """你是台指期和台股的專業分析師。
分析風格：客觀、精準、直接，不說廢話。
輸出語言：繁體中文。
策略框架：以 Put wall/Call wall 為核心觸發點，外資籌碼為方向判斷。

重要規則：
1. 只能使用輸入中明確提供的點位數字，絕對不能自創數字
2. 不得重複報告中已有的操作策略內容
3. 只說最重要的一個矛盾訊號，不超過指定行數
4. 若 C/P Ratio < 0.5：分析散戶大量追Put的軋空含義
5. 若外資現貨異動 > 300億：特別分析此訊號的意義"""

_api_stats: dict = {"calls": 0, "total_tokens": 0}


def get_api_stats() -> dict:
    return dict(_api_stats)


def _call_gemini_with_retry(client, prompt: str, max_output_tokens: Optional[int] = None) -> Optional[str]:
    """Call Gemini with up to 2 retries on 429; return None silently if all retries fail.
    thinking_budget=0 disables Gemini 2.5 thinking mode so tokens go to output."""
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_BASE,
        max_output_tokens=max_output_tokens,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt, config=config
            )
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                _api_stats["calls"] += 1
                _api_stats["total_tokens"] += getattr(response.usage_metadata, "total_token_count", 0) or 0
            return response.text.strip()
        except Exception as e:
            if "429" in str(e) or "ResourceExhausted" in type(e).__name__:
                if attempt < 2:
                    print(f"⏳ [ai_report_engine] Gemini 429，{GEMINI_RETRY_DELAY}秒後重試（第{attempt + 1}次）")
                    time.sleep(GEMINI_RETRY_DELAY)
                else:
                    print("⏳ [ai_report_engine] Gemini 429，重試2次仍失敗，靜默跳過")
                    return None
            else:
                raise
    return None

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

    # 散戶貪婪 + 籌碼偏空警示（runtime check）
    try:
        fg = float(ctx.get("fear_greed_index") or 0)
        ss = int(ctx.get("sentiment_score") or 0)
        if fg > 60 and ss <= -3:
            if not any("散戶貪婪" in str(w) for w in warnings):
                lines.append(f"警示：⚠️ 散戶貪婪({int(fg)})+外資偏空({ss:+d}分）：歷史上這個組合往往是短期高點特徵，做多需極度謹慎")
    except Exception:
        pass

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

    # 在 EVENING_FUTURES 加入外資方向語意說明，避免 AI 把回補錯誤描述為增加
    if report_type == "EVENING_FUTURES":
        chg_1d = chip_ctx.get("foreign_net_chg_1d") or 0
        try:
            chg_1d = int(chg_1d)
            if chg_1d > 0:
                user_prompt += (
                    f"\n\n重要：今日外資回補空單 {chg_1d:+,} 口（淨空部位減少），"
                    "請正確描述為回補而非增加，並說明此變化對後市的含義"
                )
            elif chg_1d < 0:
                user_prompt += (
                    f"\n\n重要：今日外資加碼空單 {chg_1d:+,} 口（淨空部位增加），"
                    "請在解讀中特別強調空方壓力加重的含義"
                )
        except Exception:
            pass

    full_prompt = f"{_SYSTEM}\n\n{user_prompt}"

    client = genai.Client(api_key=api_key)
    text = _call_gemini_with_retry(client, full_prompt, max_output_tokens=800)
    if text is None:
        return None
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
4. 使用繁體中文，300字以內"""


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
    text = _call_gemini_with_retry(client, full_prompt, max_output_tokens=300)
    if text is None:
        return None
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
    text = _call_gemini_with_retry(client, full_prompt)
    if text is None:
        return None
    print(f"✅ [ai_report_engine] 個股 {stock_id} 快評完成（{len(text)} 字）")
    return text


# --------------------------------------------------
# 盤前操作指引（PREOPEN_GUIDANCE）
# --------------------------------------------------

_PREOPEN_GUIDANCE_SYSTEM = """你是 ATOS 盤前操作指引引擎。

根據籌碼數據和方向判斷，輸出兩段文字，中間用「---」分隔：

段落1（1-2行）：白話說明今日大方向，最重要的注意事項
段落2（3-4行）：分析外資動向、OI框架對今日操作的具體影響

規則：
1. 不說「建議做多/做空/買進/賣出」，描述結構和條件
2. 純文字，不用 Markdown 符號
3. 使用繁體中文"""


@safe_execute
def generate_preopen_contradiction(
    chip_ctx: Optional[dict],
    active_pivot,
    r1,
    s1,
) -> Optional[str]:
    """
    產生盤前「AI 矛盾分析」段落。
    只說最重要的一個訊號矛盾，不超過4行，不重複策略內容。
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or not chip_ctx:
        return None

    fn = chip_ctx.get("foreign_net", 0)
    score = chip_ctx.get("sentiment_score", 0)
    cp = chip_ctx.get("call_put_ratio", "N/A")
    cw = chip_ctx.get("call_wall", "N/A")
    pw = chip_ctx.get("put_wall", "N/A")
    mp = chip_ctx.get("max_pain", "N/A")
    spot = chip_ctx.get("spot_foreign_net_buy_bn", 0)
    fg = chip_ctx.get("fear_greed_index", "N/A")

    known_levels = (
        f"已知點位（只能用這些數字）：\n"
        f"  Pivot={active_pivot}，R1={r1}，S1={s1}，"
        f"Call wall={cw}，Put wall={pw}，Max Pain={mp}"
    )

    user_prompt = (
        f"外資期貨：{fn:+,}口（{chip_ctx.get('foreign_net_level', 'N/A')}）\n"
        f"情緒評分：{score:+d}（{chip_ctx.get('sentiment_bias', 'N/A')}）\n"
        f"C/P比：{cp}，Max Pain：{mp}\n"
        f"現貨外資：{spot:+.1f}億，Fear&Greed：{fg}\n"
        f"{known_levels}\n\n"
        "請只說今日籌碼中最重要的一個訊號矛盾（例如：籌碼偏空但Fear&Greed偏高、"
        "Put wall OI異常大但外資繼續加空等）。\n"
        "不超過4行，不重複上方策略操作內容，不自創點位數字。"
    )

    client = genai.Client(api_key=api_key)
    text = _call_gemini_with_retry(client, user_prompt, max_output_tokens=300)
    if text is None:
        return None
    print(f"✅ [ai_report_engine] 矛盾分析完成（{len(text)} 字）")
    return text


@safe_execute
def generate_preopen_guidance(chip_ctx: Optional[dict], bias_label: str) -> Optional[dict]:
    """
    產生盤前「今天怎麼做」和「AI判斷」兩段內容。
    返回 {"today": str, "judgment": str}，失敗返回 None。
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    if not chip_ctx:
        return None

    fn = chip_ctx.get("foreign_net", 0)
    score = chip_ctx.get("sentiment_score", 0)

    user_prompt = (
        f"方向判斷：{bias_label}\n"
        f"外資期貨：{fn:+,}口（{chip_ctx.get('foreign_net_level', 'N/A')}）\n"
        f"情緒評分：{score:+d}（{chip_ctx.get('sentiment_bias', 'N/A')}）\n"
        f"Call牆：{chip_ctx.get('call_wall', 'N/A')}（OI {chip_ctx.get('call_wall_oi', 'N/A')}）\n"
        f"Put牆：{chip_ctx.get('put_wall', 'N/A')}（OI {chip_ctx.get('put_wall_oi', 'N/A')}）\n"
        f"現價位置：區間{chip_ctx.get('price_position_pct', 'N/A')}%\n"
        f"外資現貨：{chip_ctx.get('spot_foreign_net_buy_bn', 0):+.1f}億\n\n"
        "請產生盤前操作指引（兩段，中間用「---」分隔）："
    )

    full_prompt = f"{_PREOPEN_GUIDANCE_SYSTEM}\n\n{user_prompt}"

    client = genai.Client(api_key=api_key)
    text = _call_gemini_with_retry(client, full_prompt, max_output_tokens=800)
    if text is None:
        return None

    parts = text.split("---", 1)
    today = parts[0].strip() if parts else ""
    judgment = parts[1].strip() if len(parts) > 1 else text.strip()

    print(f"✅ [ai_report_engine] 盤前指引完成（{len(text)} 字）")
    return {"today": today, "judgment": judgment}


# --------------------------------------------------
# 晚盤解讀（EVENING_GUIDANCE）
# --------------------------------------------------

_EVENING_GUIDANCE_SYSTEM = """你是 ATOS 晚盤複盤分析引擎。

你的唯一任務：用4-5行點出今日籌碼與盤面中最重要的一個「矛盾訊號」或「結構特徵」，
讓交易員明白夜盤最需要警惕的陷阱是什麼。

絕對禁止事項：
1. 禁止重複報告中已有的做多/做空/觀望條件（Call wall、Put wall點位已在報告本文列明）
2. 禁止僅憑收盤位於Pivot上方就說「日盤偏多」——必須同時考慮假突破、現貨大賣等負面訊號
3. 禁止自創點位數字，只能引用輸入中的點位
4. 不說「建議做多/做空」，描述矛盾結構和陷阱
5. 純文字，不用 Markdown，使用繁體中文

判斷準則（優先順序）：
- 若 Call wall 出現假突破 → 這是上方主力防守極重的訊號，必須提及
- 若現貨外資賣超 > 200億 → 屬於重大異動，必須特別描述其含義
- 若期貨回補但現貨大賣 → 這是「現期背離」矛盾，是今日最大陷阱
- 若 C/P Ratio < 0.5 → 散戶過度追空，軋空風險"""


@safe_execute
def generate_evening_guidance(
    day_result: str,
    chip_ctx: Optional[dict],
    alert_summary: dict,
    call_wall_status: str = "",
    put_wall_status: str = "",
    pivot_status: str = "",
    today_high=None,
    today_low=None,
    price=None,
) -> Optional[str]:
    """
    產生晚盤 AI 矛盾解讀（4-5行）。
    接收假突破狀態、現貨大賣等關鍵事實，避免 AI 自行腦補錯誤結論。
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    alert_types = []
    if alert_summary.get("has_long_trap"):
        alert_types.append("多方陷阱")
    if alert_summary.get("has_short_trap"):
        alert_types.append("空方陷阱")
    if alert_summary.get("has_sweep"):
        alert_types.append("掃單")
    if alert_summary.get("has_flip_break"):
        alert_types.append("跌破Pivot")
    if alert_summary.get("has_flip_recover"):
        alert_types.append("站回Pivot")

    ctx = chip_ctx or {}
    fn = ctx.get("foreign_net", 0)
    fn_1d = ctx.get("foreign_net_chg_1d", 0)
    score = ctx.get("sentiment_score", 0)
    spot = ctx.get("spot_foreign_net_buy_bn", 0)
    cp = ctx.get("call_put_ratio", "N/A")

    # 現貨大賣警示
    spot_note = ""
    try:
        if abs(float(spot)) > 200:
            spot_note = f"（注意：現貨外資賣超 {abs(spot):.1f}億，屬重大異動）"
    except Exception:
        pass

    # 今日盤面事實摘要（不讓 AI 自行推斷，直接告訴它事實）
    facts = []
    if call_wall_status:
        facts.append(f"Call wall事實：{call_wall_status}")
    if put_wall_status:
        facts.append(f"Put wall事實：{put_wall_status}")
    if pivot_status:
        facts.append(f"Pivot事實：{pivot_status}")
    if today_high and price:
        try:
            shadow = float(today_high) - float(price)
            if shadow > 150:
                facts.append(f"今日高點{today_high}距收盤{price}差{shadow:.0f}點，留有長上影線")
        except Exception:
            pass

    user_prompt = (
        f"=== 今日盤面事實（必須以此為準，不得推翻）===\n"
        + "\n".join(facts) + "\n\n"
        f"=== 籌碼數據 ===\n"
        f"外資期貨：{fn:+,}口（今日{'+' if fn_1d > 0 else ''}{fn_1d:,}口）\n"
        f"現貨外資：{spot:+.1f}億{spot_note}\n"
        f"C/P比：{cp}｜情緒評分：{score:+d}（{ctx.get('sentiment_bias', 'N/A')}）\n"
        f"今日警報：{', '.join(alert_types) if alert_types else '無主要事件'}\n\n"
        "請只說今日籌碼與盤面中最重要的一個矛盾訊號或陷阱結構（4-5行，不重複報告已有的操作條件）："
    )

    full_prompt = f"{_EVENING_GUIDANCE_SYSTEM}\n\n{user_prompt}"

    client = genai.Client(api_key=api_key)
    text = _call_gemini_with_retry(client, full_prompt, max_output_tokens=400)
    if text is None:
        return None
    print(f"✅ [ai_report_engine] 晚盤解讀完成（{len(text)} 字）")
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
    text = _call_gemini_with_retry(client, full_prompt)
    if text is None:
        return None
    print(f"✅ [ai_report_engine] 市場快評完成（{len(text)} 字）")
    return text


# --------------------------------------------------
# 批次個股點評（A 級一次呼叫）
# --------------------------------------------------

@safe_execute
def generate_stock_commentaries_batch(stocks: list, chip_ctx: Optional[dict] = None) -> dict:
    """一次呼叫 Gemini 產生所有 A 級個股白話點評，回傳 {stock_id: comment} dict。"""
    if not stocks:
        return {}

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {}

    ctx = chip_ctx or {}
    sentiment_bias = ctx.get("sentiment_bias", "N/A")
    try:
        sentiment_score = int(ctx.get("sentiment_score", 0) or 0)
        score_str = f"{sentiment_score:+d}"
    except Exception:
        score_str = "N/A"
    foreign_net = ctx.get("foreign_net", 0) or 0

    stock_lines = []
    for s in stocks:
        tech = s.get("tech", {}) or {}
        chip = s.get("chip", {}) or {}
        stock_lines.append(
            f"{s.get('id', 'N/A')}：收{tech.get('close', 'N/A')} "
            f"5MA{tech.get('ma5', 'N/A')} "
            f"距MA{tech.get('distance_to_ma5', 'N/A')}% "
            f"量比{tech.get('volume_ratio', 'N/A')} "
            f"投信連買{chip.get('consecutive_trust_buy_days', 'N/A')}日"
        )

    prompt = (
        f"大盤情緒：{sentiment_bias}（{score_str}分）\n"
        f"外資期貨：{foreign_net:+,}口\n\n"
        f"以下每檔個股各給一行白話點評（格式：股號：點評內容）：\n"
        + "\n".join(stock_lines)
        + "\n\n要求：每檔一行，說明技術位置和主要風險，不超過30字。"
    )

    client = genai.Client(api_key=api_key)
    text = _call_gemini_with_retry(client, prompt, max_output_tokens=max(200, 80 * len(stocks)))
    if not text:
        return {}

    expected_ids = {str(s.get("id", "")) for s in stocks}
    commentaries: dict = {}
    for line in text.strip().split("\n"):
        if "：" in line:
            sid, comment = line.split("：", 1)
            sid = sid.strip()
            if sid in expected_ids:
                commentaries[sid] = comment.strip()

    print(f"✅ [ai_report_engine] 批次個股點評完成（{len(commentaries)}/{len(stocks)} 檔）")
    return commentaries


# --------------------------------------------------
# 批次新聞市場影響評估（一次呼叫）
# --------------------------------------------------

_NEWS_TRIAGE_SYSTEM = """你是台指期盤中主編，負責判斷每則新聞是否需要立刻發出警報。

判斷標準（必須同時符合才算需要警報）：
1. 今天交易時段內會直接影響台指期方向（不是長期展望、不是業績回顧）
2. 影響幅度足以讓大盤單日波動超過 100 點以上
3. 訊息具體、有數字或決策（不是「市場觀望」「可能」「分析師預期」等模糊字眼）

以下類型不需要警報：
- 個股財報、法說會（除非是台積電且數字大幅偏離預期）
- 已知事件的例行更新（Fed 照預期升息、GDP 小幅修正）
- 分析師目標價調整、評等變更
- 技術分析文章、市場展望專欄
- 超過 2 小時的舊聞

需要警報的例子：
- 美國緊急宣布新關稅，且立刻生效
- Fed 超預期升息或降息（偏離市場預期 25bp 以上）
- 台積電法說會：毛利率大幅低於預期 3% 以上
- 地緣政治突發（軍事行動、制裁令生效）
- 台灣強震 5.5 級以上影響生產

回傳格式（每則一行，格式嚴格）：
編號|Y或N|多或空或中性|一句話說明

例如：
1|N|中性|例行財報，無驚喜
2|Y|空|Fed 超預期升息，直接衝擊風險資產
3|N|中性|分析師目標價調整，不影響今日行情"""


@safe_execute
def batch_score_news(news_list: list) -> list:
    """
    AI 逐則判斷新聞是否需要發警報。

    回傳與輸入同長度的分數列表：
    - 5 = 需要立刻發警報（Y）
    - 0 = 不需要（N）
    """
    if not news_list:
        return []

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return [0] * len(news_list)

    items = []
    for i, n in enumerate(news_list):
        title = n.get("title", "")
        pub = n.get("pub_time", "")[:16]
        stock = n.get("stock_id", "")
        line = f"{i + 1}. [{pub}]"
        if stock:
            line += f"（{stock}）"
        line += f" {title}"
        items.append(line)

    full_prompt = (
        _NEWS_TRIAGE_SYSTEM
        + "\n\n---\n以下新聞逐則判斷：\n"
        + "\n".join(items)
        + "\n\n請按格式回傳，每則一行："
    )

    client = genai.Client(api_key=api_key)
    text = _call_gemini_with_retry(
        client, full_prompt, max_output_tokens=max(60, len(news_list) * 30)
    )
    if not text:
        return [0] * len(news_list)

    import re
    scores = [0] * len(news_list)
    for line in text.strip().split("\n"):
        line = line.strip()
        # 解析 "編號|Y或N|方向|說明"
        parts = line.split("|")
        if len(parts) < 2:
            continue
        try:
            idx = int(re.sub(r"\D", "", parts[0])) - 1
            need_alert = parts[1].strip().upper() == "Y"
            direction = parts[2].strip() if len(parts) > 2 else ""
            reason = parts[3].strip() if len(parts) > 3 else ""
            if 0 <= idx < len(news_list):
                scores[idx] = 5 if need_alert else 0
                # 把 AI 的判斷理由和方向寫回 news_list 供警報訊息使用
                news_list[idx]["ai_direction"] = direction
                news_list[idx]["ai_reason"] = reason
                if need_alert:
                    print(f"  🔴 [{idx+1}] 需要警報｜{direction}｜{reason}")
                else:
                    print(f"  ⬜ [{idx+1}] 略過｜{reason[:30]}")
        except Exception:
            continue

    return scores


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
