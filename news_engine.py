# news_engine.py
#
# 監控重大市場事件，來源：
#   1. FinMind TaiwanStockNews（主要台股個股新聞）
#   2. Yahoo Finance RSS（國際市場大事件）
#
# 每 5 分鐘輪詢一次（由 main_commander 排程）
# 發現重大關鍵字 → AI 快評 → Telegram 警報
# 相同標題 30 分鐘內不重複發送

import hashlib
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Optional

from error_handler import safe_execute
from messenger import send_to_telegram

# --------------------------------------------------
# 設定
# --------------------------------------------------

# 監控的主要個股（對大盤影響最大）
WATCH_STOCKS = ["2330", "2317", "2454", "2412", "2882"]

# 繁體中文關鍵字（發現任一即觸發）
KEYWORDS_ZH = [
    # 市場結構
    "暴跌", "崩盤", "熔斷", "停盤", "跌停", "漲停鎖死",
    # 政策與總經
    "緊急", "聯準會", "FOMC", "升息", "降息", "利率決定",
    "CPI", "PCE", "通膨", "衰退", "失業率",
    # 地緣政治
    "制裁", "關稅", "貿易戰", "台灣海峽", "軍事演習",
    # 重大企業
    "台積電", "外資大幅", "三大法人",
    # 天災
    "地震", "強震", "颱風警報",
]

# 英文關鍵字
KEYWORDS_EN = [
    # Market structure
    "crash", "circuit breaker", "trading halt", "emergency",
    # Fed / macro
    "FOMC", "rate hike", "rate cut", "interest rate decision",
    "inflation", "CPI", "PCE", "recession",
    # Geopolitics
    "Taiwan Strait", "sanction", "tariff", "trade war",
    # Companies
    "TSMC", "semiconductor",
]

# Yahoo Finance RSS 來源
YAHOO_RSS_URLS = [
    "https://finance.yahoo.com/rss/topstories",
]

NEWS_COOLDOWN = 1800   # 秒，同一則新聞 30 分鐘內不重複發送

# --------------------------------------------------
# 去重狀態（session 內有效）
# --------------------------------------------------

_last_sent: dict[str, float] = {}


def _news_hash(title: str) -> str:
    return hashlib.md5(title.strip().encode("utf-8")).hexdigest()[:12]


def _can_send(h: str) -> bool:
    return time.time() - _last_sent.get(h, 0) > NEWS_COOLDOWN


def _mark_sent(h: str):
    _last_sent[h] = time.time()


# --------------------------------------------------
# 關鍵字判斷
# --------------------------------------------------

def _is_major_event(title: str, content: str = "") -> bool:
    text = (title + " " + content).upper()
    for kw in KEYWORDS_ZH:
        if kw.upper() in text:
            return True
    for kw in KEYWORDS_EN:
        if kw.upper() in text:
            return True
    return False


# --------------------------------------------------
# 資料來源
# --------------------------------------------------

@safe_execute
def _fetch_finmind_news(days_back: int = 1) -> list[dict]:
    """從 FinMind TaiwanStockNews 抓取主要個股新聞。"""
    from FinMind.data import DataLoader

    token = os.getenv("FIN_TOKEN") or os.getenv("FINMIND_TOKEN")
    if not token:
        return []

    api = DataLoader()
    api.login_by_token(token)
    start = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    items = []
    for stock_id in WATCH_STOCKS:
        try:
            df = api.get_data(
                dataset="TaiwanStockNews",
                data_id=stock_id,
                start_date=start,
            )
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                items.append({
                    "source": "FinMind",
                    "stock_id": stock_id,
                    "title": str(row.get("title", "")),
                    "content": str(row.get("description", "") or row.get("content", "")),
                    "url": str(row.get("link", "") or row.get("url", "")),
                    "pub_time": str(row.get("date", "")),
                })
        except Exception:
            pass

    return items


@safe_execute
def _fetch_yahoo_rss() -> list[dict]:
    """從 Yahoo Finance RSS 抓取國際市場新聞。"""
    items = []
    headers = {"User-Agent": "Mozilla/5.0 ATOS-NewsEngine/1.0"}

    for url in YAHOO_RSS_URLS:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml_data = resp.read()

            root = ET.fromstring(xml_data)

            for item_el in root.findall(".//item"):
                def _text(tag):
                    el = item_el.find(tag)
                    return el.text.strip() if el is not None and el.text else ""

                items.append({
                    "source": "Yahoo Finance",
                    "stock_id": None,
                    "title": _text("title"),
                    "content": _text("description"),
                    "url": _text("link"),
                    "pub_time": _text("pubDate"),
                })
        except Exception:
            pass

    return items


# --------------------------------------------------
# 警報訊息
# --------------------------------------------------

def _build_alert_message(item: dict, commentary: Optional[str]) -> str:
    now = datetime.now().strftime("%H:%M")
    source = item.get("source", "")
    stock_id = item.get("stock_id")
    title = item.get("title", "")
    url = item.get("url", "")

    lines = [
        f"【重大事件警報】{now}",
        f"來源：{source}" + (f"（{stock_id}）" if stock_id else ""),
        f"標題：{title}",
    ]
    if commentary:
        lines.append(f"AI 快評：{commentary}")
    if url and url.startswith("http"):
        lines.append(f"連結：{url}")

    return "\n".join(lines)


# --------------------------------------------------
# 主函式
# --------------------------------------------------

def poll_news(chip_ctx: Optional[dict] = None) -> int:
    """
    輪詢所有新聞來源，對重大事件發送 Telegram 警報。

    Args:
        chip_ctx: build_chip_context() 輸出；None 則在有匹配時才載入

    Returns:
        本次發送的警報數
    """
    finmind_items: list = _fetch_finmind_news() or []
    yahoo_items: list = _fetch_yahoo_rss() or []
    all_items = finmind_items + yahoo_items

    if not all_items:
        return 0

    # 過濾出重大事件 + 去重
    candidates = []
    for item in all_items:
        title = item.get("title", "")
        if not title:
            continue
        if not _is_major_event(title, item.get("content", "")):
            continue
        h = _news_hash(title)
        if not _can_send(h):
            continue
        candidates.append((item, h))

    if not candidates:
        return 0

    # 有匹配才載入 chip_ctx 和 AI
    if chip_ctx is None:
        try:
            from chip_data_engine import build_chip_context
            chip_ctx = build_chip_context()
        except Exception:
            chip_ctx = {}

    sent = 0
    for item, h in candidates:
        title = item.get("title", "")
        print(f"🔴 [news_engine] 關鍵字命中，發送警報：{title[:50]}")
        commentary = _get_commentary(item, chip_ctx)
        msg = _build_alert_message(item, commentary)
        if send_to_telegram(msg):
            _mark_sent(h)
            sent += 1
            print(f"✅ [news_engine] 警報已發送：{title[:50]}")

    return sent


def _get_commentary(item: dict, chip_ctx: dict) -> Optional[str]:
    """呼叫 ai_report_engine 產生事件快評。"""
    from ai_report_engine import generate_event_report

    title = item.get("title", "")
    source = item.get("source", "")
    stock_id = item.get("stock_id")
    event_desc = f"標題：{title}\n來源：{source}" + (f"（{stock_id}）" if stock_id else "")

    try:
        return generate_event_report(event_desc, chip_ctx)
    except Exception as e:
        if "429" in str(e) or "ResourceExhausted" in type(e).__name__:
            return None  # 429 靜默處理，只發標題
        print(f"⚠️ [news_engine] _get_commentary 失敗：{e}")
        return None


# --------------------------------------------------
# 手動測試
# --------------------------------------------------

if __name__ == "__main__":
    print("=== news_engine 手動測試 ===\n")

    print("--- 測試關鍵字比對 ---")
    samples = [
        ("Fed announces emergency rate cut", ""),
        ("台積電法說會：毛利率優於預期", ""),
        ("天氣晴，台股小幅震盪", ""),
        ("FOMC 會議決議：維持利率不變", ""),
        ("強震規模 7.2 發生在台灣東部", ""),
    ]
    for title, content in samples:
        hit = _is_major_event(title, content)
        print(f"  {'✅' if hit else '❌'} {title}")

    print()
    print("--- 測試 Yahoo RSS 抓取 ---")
    yahoo_items = _fetch_yahoo_rss() or []
    print(f"  Yahoo RSS 筆數：{len(yahoo_items)}")
    if yahoo_items:
        print(f"  最新標題：{yahoo_items[0].get('title', '')[:60]}")

    print()
    print("--- 測試 FinMind 新聞抓取 ---")
    fm_items = _fetch_finmind_news() or []
    print(f"  FinMind 新聞筆數：{len(fm_items)}")
    if fm_items:
        print(f"  最新標題：{fm_items[0].get('title', '')[:60]}")

    print()
    print(f"整體 poll_news()（不實際發送 Telegram）：")
    # 測試模式：只跑關鍵字篩選，不發送
    all_items = (fm_items) + (yahoo_items)
    hits = [i for i in all_items if _is_major_event(i.get("title", ""), i.get("content", ""))]
    print(f"  符合關鍵字的新聞：{len(hits)} 筆")
    for h in hits[:3]:
        print(f"    - {h.get('title', '')[:60]}")
