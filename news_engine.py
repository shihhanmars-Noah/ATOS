# news_engine.py
#
# 監控重大市場事件，來源：
#   1. FinMind TaiwanStockNews（主要台股個股新聞）
#   2. Yahoo Finance RSS（國際市場大事件）
#
# 每 5 分鐘輪詢一次（由 main_commander 排程）
# 三級優先：Level 3 合併窗口 → Level 2 優先隊列 → Level 1 常規隊列
# 相同標題 30 分鐘內不重複發送；同關鍵字 Level 3 事件 24 小時冷卻

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

WATCH_STOCKS = ["2330", "2317", "2454", "2412", "2882"]

# Level 3：重大總經/地緣/市場結構事件 → 5分鐘時間窗口合併發送
LEVEL3_KEYWORDS = [
    # 中文
    '聯準會', 'CPI', 'PCE', '非農', '降息', '升息',
    '利率決議', '戰爭', '台海', '制裁', '熔斷', '崩盤',
    '緊急', '停牌', '暫停交易', '台灣海峽', '伊朗', '以色列', '地緣',
    # 英文
    'Fed', 'Federal Reserve', 'FOMC', 'rate cut', 'rate hike',
    'interest rate', 'nonfarm', 'Iran', 'Israel', 'sanctions',
    'circuit breaker', 'trading halt', 'Taiwan', 'war', 'missile',
    'chip ban', 'Trump', 'tariff',
]

# Level 2：重要但非緊急 → 優先隊列，下輪優先發
LEVEL2_KEYWORDS = [
    '外資', '法人', '台積電', '晶片', '關稅', '匯率',
    '三大法人', '期貨', '選擇權', '大跌', '暴跌',
    'TSMC', 'semiconductor',
]

# Yahoo Finance RSS 來源
YAHOO_RSS_URLS = [
    "https://finance.yahoo.com/rss/topstories",
]

NEWS_COOLDOWN             = 1800  # 秒，同一則新聞 30 分鐘內不重複發送（持久化）
NEWS_FRESH_WINDOW         = 30    # 分鐘，啟動時防炸版用（_is_fresh 沿用）
MAX_NEWS_AGE_MINUTES      = 60    # 分鐘，每輪輪詢時超過此時效的新聞視為舊聞
MAX_ALERTS_PER_POLL       = 2     # 每輪 Level 1/2 最多發 N 則
COLLECTION_WINDOW_MINUTES = 5     # Level 3 收集窗口（分鐘）
COOLDOWN_HOURS            = 24    # Level 3 同關鍵字冷卻（小時）

# --------------------------------------------------
# 三級優先狀態（記憶體）
# --------------------------------------------------

_event_collection: dict = {}   # key=window_key（5分鐘時間槽）, value={'news': [], 'window_start': datetime}
_sent_window_keys: set  = set()  # 已發送的窗口 key（session 內去重，每窗口只發一次）
_priority_queue:   list = []   # Level 2 待發隊列
_normal_queue:     list = []   # Level 1 待發隊列

# --------------------------------------------------
# 去重狀態（持久化 + 記憶體雙層）
# --------------------------------------------------

_last_sent:      dict[str, float] = {}
_sent_news_ids:  set              = set()   # session 內快速去重（fingerprint）


def _news_hash(title: str) -> str:
    return hashlib.md5(title.strip().encode("utf-8")).hexdigest()[:12]


def _load_persisted_sent() -> dict:
    try:
        from persistent_state import load_state
        return load_state().get("news_last_sent", {})
    except Exception:
        return {}


def _save_persisted_sent(h: str, ts: float) -> None:
    try:
        from persistent_state import load_state, save_state
        state = load_state()
        records = state.get("news_last_sent", {})
        records[h] = ts
        if len(records) > 200:
            oldest = sorted(records, key=lambda k: records[k])
            for old_key in oldest[:len(records) - 200]:
                del records[old_key]
        state["news_last_sent"] = records
        save_state(state)
    except Exception:
        pass


def _can_send(h: str) -> bool:
    now = time.time()
    if h not in _last_sent:
        persisted = _load_persisted_sent()
        if h in persisted:
            _last_sent[h] = persisted[h]
    return now - _last_sent.get(h, 0) > NEWS_COOLDOWN


def _mark_sent(h: str) -> None:
    ts = time.time()
    _last_sent[h] = ts
    _save_persisted_sent(h, ts)


def _is_fresh(pub_time_str: str) -> bool:
    """判斷新聞是否在 NEWS_FRESH_WINDOW 分鐘以內發布。無法解析 → 視為新鮮。"""
    if not pub_time_str:
        return True
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S +0000"):
        try:
            dt = datetime.strptime(pub_time_str[:25], fmt[:len(pub_time_str[:25])])
            dt_naive = dt.replace(tzinfo=None)
            age_minutes = (datetime.now() - dt_naive).total_seconds() / 60
            return age_minutes <= NEWS_FRESH_WINDOW
        except Exception:
            continue
    return True


def _is_news_fresh(news: dict) -> bool:
    """
    判斷新聞是否在 MAX_NEWS_AGE_MINUTES（60分鐘）以內。
    嘗試 date / pub_time / publish_time / time 多個欄位。
    無法解析時間 → 視為舊聞（保守過濾，避免不知年代的新聞發出）。
    """
    time_str = (
        news.get("date") or
        news.get("pub_time") or
        news.get("publish_time") or
        news.get("time") or
        ""
    )

    if not time_str:
        print(f"⏸️ [news_engine] 無時間戳，略過：{news.get('title', '')[:40]}")
        return False

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S +0000",
    ):
        try:
            news_dt = datetime.strptime(str(time_str)[:25], fmt[:25])
            news_dt = news_dt.replace(tzinfo=None)
            elapsed_min = (datetime.now() - news_dt).total_seconds() / 60
            if elapsed_min > MAX_NEWS_AGE_MINUTES:
                print(
                    f"⏸️ [news_engine] 舊聞略過（{elapsed_min:.0f}分鐘前）："
                    f"{news.get('title', '')[:40]}"
                )
                return False
            return True
        except Exception:
            continue

    # 所有格式解析失敗
    print(f"⏸️ [news_engine] 時間格式無法解析，略過：{news.get('title', '')[:40]}")
    return False


def _get_news_fingerprint(news: dict) -> str:
    """產生新聞唯一識別碼（標題 + 日期前10碼 的 MD5 前12碼）。"""
    title = news.get("title", "")
    date  = str(
        news.get("date") or
        news.get("pub_time") or
        news.get("publish_time") or
        ""
    )[:10]
    return hashlib.md5(f"{title}{date}".encode("utf-8")).hexdigest()[:12]


def _get_window_key(dt: datetime = None) -> str:
    """
    取當前時間所在的 5 分鐘時間槽，作為 Level 3 收集窗口的 key。
    例如 22:53 → '20260608_2250'，22:57 → '20260608_2255'。
    同一個 5 分鐘內的所有 Level 3 新聞進同一個窗口。
    """
    if dt is None:
        dt = datetime.now()
    floored_minute = (dt.minute // 5) * 5
    return dt.strftime(f"%Y%m%d_%H{floored_minute:02d}")


# --------------------------------------------------
# 三級分類工具
# --------------------------------------------------

def _get_news_level(title: str) -> int:
    """判斷新聞等級：3=重大事件 / 2=重要 / 1=一般"""
    t = title.lower()
    for kw in LEVEL3_KEYWORDS:
        if kw.lower() in t:
            return 3
    for kw in LEVEL2_KEYWORDS:
        if kw.lower() in t:
            return 2
    return 1


def _get_triggered_keyword(title: str) -> str:
    """回傳觸發 Level 3 的關鍵字（第一個命中）。"""
    t = title.lower()
    for kw in LEVEL3_KEYWORDS:
        if kw.lower() in t:
            return kw
    return ""


def _is_major_event(title: str, content: str = "") -> bool:
    """向後相容：任何等級 >= 2 都算重大事件。"""
    return _get_news_level(title + " " + content) >= 2


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
# Level 3 合併發送
# --------------------------------------------------

def _send_merged_event(window_key: str, news_list: list) -> None:
    """
    收集窗口結束後，合併該窗口內所有 Level 3 新聞為一則重大事件警報。
    同一窗口只發一次（由 _sent_window_keys 去重）。
    """
    count  = len(news_list)
    titles = '\n'.join([f"- {n['title']}" for n in news_list[:10]])

    prompt = (
        f"以下是本輪偵測到的 {count} 則重大新聞，請：\n"
        f"1. 判斷是否屬於同一事件或相關聯事件\n"
        f"2. 合併成一則重點摘要（不超過3行）\n"
        f"3. 說明對台指期的即時影響（多/空/中性）\n"
        f"4. 給出一個具體操作注意事項\n\n"
        f"新聞標題：\n{titles}\n\n"
        f"格式：\n"
        f"【事件判斷】同一事件/相關事件/無關聯\n"
        f"【摘要】...\n"
        f"【台指影響】多/空/中性，原因...\n"
        f"【操作注意】..."
    )

    commentary = _get_commentary_with_retry(prompt, max_tokens=350)

    if not commentary:
        commentary = (
            f"【事件判斷】偵測到 {count} 則重大新聞\n"
            f"【摘要】本輪出現多則重大市場事件，請留意相關風險\n"
            f"【台指影響】中性，等待方向確認\n"
            f"【操作注意】暫停追單，等待5分K方向確認後再進場"
        )

    now_str = datetime.now().strftime("%H:%M")
    msg = (
        f"🚨 重大事件警報（{count} 則新聞合併）\n"
        f"時間：{now_str}\n\n"
        f"{commentary}"
    )

    send_to_telegram(msg)
    print(f"✅ [news_engine] 重大事件合併發送：窗口 {window_key}（{count}則）")


def _get_commentary_with_retry(prompt: str, max_tokens: int = 300) -> Optional[str]:
    """呼叫 Gemini 生成快評，失敗時靜默回傳 None。"""
    try:
        from ai_report_engine import _call_gemini_with_retry
        return _call_gemini_with_retry(prompt, max_tokens=max_tokens)
    except Exception as e:
        print(f"⚠️ [news_engine] Gemini 快評失敗：{e}")
        return None


# --------------------------------------------------
# 警報訊息（Level 1/2）
# --------------------------------------------------

def _build_alert_message(item: dict, commentary: Optional[str]) -> str:
    now = datetime.now().strftime("%H:%M")
    source = item.get("source", "")
    stock_id = item.get("stock_id")
    title = item.get("title", "")
    url = item.get("url", "")
    ai_direction = item.get("ai_direction", "")
    ai_reason = item.get("ai_reason", "")

    lines = [f"【重大事件警報】{now}"]

    if ai_direction and ai_direction != "中性":
        lines.append(f"影響方向：{ai_direction}")

    lines.append(f"來源：{source}" + (f"（{stock_id}）" if stock_id else ""))
    lines.append(f"標題：{title}")

    if ai_reason:
        lines.append(f"AI 判斷：{ai_reason}")

    if commentary:
        lines.append(f"AI 快評：{commentary}")

    if url and url.startswith("http"):
        lines.append(f"連結：{url}")

    return "\n".join(lines)


# --------------------------------------------------
# 批次評估新聞重要性
# --------------------------------------------------

def batch_evaluate_news(news_list: list) -> list:
    """一次呼叫 Gemini 評估所有新聞重要性，回傳 importance >= 4 的項目。"""
    if not news_list:
        return []

    try:
        from ai_report_engine import batch_score_news
        scores = batch_score_news(news_list) or [0] * len(news_list)
    except Exception as e:
        print(f"⚠️ [news_engine] batch_score_news 失敗：{e}")
        return []

    important = []
    for i, news in enumerate(news_list):
        score = scores[i] if i < len(scores) else 0
        if score >= 4:
            important.append({**news, "importance": score})

    return important


# --------------------------------------------------
# 主函式
# --------------------------------------------------

def poll_news(chip_ctx: Optional[dict] = None) -> int:
    """
    輪詢所有新聞來源，三級處理：
    - Level 3：關鍵字命中 → 收集窗口（5分鐘）→ 合併發送 + 24小時冷卻
    - Level 2：優先隊列，每輪優先發
    - Level 1：常規隊列，每輪最多 MAX_ALERTS_PER_POLL 則

    Returns:
        本次發送的警報數
    """
    global _priority_queue, _normal_queue

    finmind_items: list = _fetch_finmind_news() or []
    yahoo_items:   list = _fetch_yahoo_rss() or []
    all_items = finmind_items + yahoo_items

    if not all_items:
        return 0

    # 過濾：時效 + fingerprint 去重 + 持久化冷卻
    candidates = []
    for item in all_items:
        title = item.get("title", "")
        if not title:
            continue

        # 1. 啟動防炸版：只看 NEWS_FRESH_WINDOW 分鐘內的新聞
        if not _is_fresh(item.get("pub_time", "") or item.get("date", "")):
            continue

        # 2. 時效過濾：超過 MAX_NEWS_AGE_MINUTES 的新聞不發警報
        if not _is_news_fresh(item):
            continue

        # 3. session 內 fingerprint 去重（快速；跨重啟由 _can_send 負責）
        fp = _get_news_fingerprint(item)
        if fp in _sent_news_ids:
            print(f"⏸️ [news_engine] 已發送過，略過：{title[:40]}")
            continue

        # 4. 持久化冷卻（30分鐘內不重複）
        if not _can_send(_news_hash(title)):
            continue

        candidates.append(item)

    if not candidates:
        _flush_expired_level3_windows()
        return 0

    # 批次 AI 評估重要性
    important_items = batch_evaluate_news(candidates)

    # 依等級分流
    for news in (important_items or []):
        title = news.get("title", "")
        level = _get_news_level(title)

        if level == 3:
            now        = datetime.now()
            window_key = _get_window_key(now)
            keyword    = _get_triggered_keyword(title)

            # 本窗口已發送過 → 略過（同一 5 分鐘時間槽只發一則合併摘要）
            if window_key in _sent_window_keys:
                print(f"⏸️ [news_engine] 窗口 {window_key} 已發送，略過：{title[:40]}")
                continue

            # 加入時間槽收集窗口
            if window_key not in _event_collection:
                _event_collection[window_key] = {
                    'news':         [],
                    'window_start': now,
                }
            _event_collection[window_key]['news'].append(news)
            print(
                f"📥 [news_engine] Level3 收集 [{keyword}] → 窗口 {window_key}"
                f"（已有 {len(_event_collection[window_key]['news'])} 則）"
            )

            # 窗口已滿（超過 5 分鐘）→ 立即合併發送
            window_age = (now - _event_collection[window_key]['window_start']).total_seconds() / 60
            if window_age >= COLLECTION_WINDOW_MINUTES:
                _send_merged_event(window_key, _event_collection[window_key]['news'])
                _sent_window_keys.add(window_key)
                del _event_collection[window_key]

        elif level == 2:
            _priority_queue.append(news)
            print(f"📋 [news_engine] Level2 加入優先隊列：{title[:50]}")

        else:
            _normal_queue.append(news)

    # 輪詢結束後，補掃過期的 Level 3 窗口
    _flush_expired_level3_windows()

    # 有重要新聞才載入 chip_ctx
    if chip_ctx is None and (_priority_queue or _normal_queue):
        try:
            from chip_data_engine import build_chip_context
            chip_ctx = build_chip_context()
        except Exception:
            chip_ctx = {}

    # 發送：優先隊列先 → 常規隊列
    sent = 0
    send_queue = _priority_queue[:] + _normal_queue[:]
    _priority_queue.clear()
    _normal_queue.clear()

    for item in send_queue:
        if sent >= MAX_ALERTS_PER_POLL:
            print(f"⏳ [news_engine] 本輪已發 {sent} 則（上限 {MAX_ALERTS_PER_POLL}），其餘留至下輪")
            # 剩餘放回 normal_queue，下輪繼續
            remaining = send_queue[send_queue.index(item):]
            _normal_queue.extend(remaining)
            break

        title = item.get("title", "")
        h = _news_hash(title)
        fp = _get_news_fingerprint(item)
        importance = item.get("importance", 4)
        print(f"🔴 [news_engine] 重要性 {importance}/5，發送警報：{title[:50]}")
        commentary = _get_commentary(item, chip_ctx or {})
        msg = _build_alert_message(item, commentary)
        if send_to_telegram(msg):
            _mark_sent(h)
            _sent_news_ids.add(fp)   # session 內 fingerprint 標記已發
            sent += 1
            print(f"✅ [news_engine] 警報已發送：{title[:50]}")

    return sent


def _flush_expired_level3_windows() -> None:
    """掃描並發送所有已過 5 分鐘收集窗口的 Level 3 事件。"""
    now = datetime.now()
    for wk in list(_event_collection.keys()):
        if wk in _sent_window_keys:
            del _event_collection[wk]
            continue
        window_age = (now - _event_collection[wk]['window_start']).total_seconds() / 60
        if window_age >= COLLECTION_WINDOW_MINUTES:
            _send_merged_event(wk, _event_collection[wk]['news'])
            _sent_window_keys.add(wk)
            del _event_collection[wk]


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
            return None
        print(f"⚠️ [news_engine] _get_commentary 失敗：{e}")
        return None


# --------------------------------------------------
# 手動測試
# --------------------------------------------------

if __name__ == "__main__":
    print("=== news_engine 手動測試 ===\n")

    print("--- 測試三級分類 ---")
    samples = [
        "Fed announces emergency rate cut",
        "聯準會 FOMC 決議：維持利率不變",
        "台積電法說會：毛利率優於預期",
        "外資大幅賣超三百億",
        "天氣晴，台股小幅震盪",
        "台海軍事演習進入第三天",
    ]
    for title in samples:
        level = _get_news_level(title)
        kw = _get_triggered_keyword(title) if level == 3 else ""
        print(f"  Level {level} {'[' + kw + ']' if kw else '':15} {title}")

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
