# news_engine.py
#
# 監控重大市場事件，來源：
#   1. FinMind TaiwanStockNews（主要台股個股新聞）
#   2. Yahoo Finance RSS（國際市場大事件）
#
# 每 5 分鐘輪詢一次（由 main_commander 排程）
#
# 兩層過濾架構：
#   Tier 1：關鍵字粗篩（寬鬆，只為減少 Gemini 呼叫次數）
#   Tier 2：Gemini 批次精判（一次呼叫，max_output_tokens=20，極省）
#
# 通過兩層後進入 5 分鐘時間窗口合併發送：
#   同一窗口（5分鐘）的重大新聞合併為一則警報
#   每窗口只發一次（持久化去重，重啟不重發）

import hashlib
import json
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

# Tier 1：寬鬆關鍵字，只為減少 Gemini 呼叫
# 命中 → 送 Tier 2 AI 精判；未命中 → 直接略過
TIER1_KEYWORDS = [
    # 央行 / 貨幣政策
    'Fed', 'Federal Reserve', 'FOMC', 'Powell',
    'CPI', 'PCE', 'Nonfarm', 'Payroll',
    '非農', '聯準會', '降息', '升息', '利率',
    'rate cut', 'rate hike', 'interest rate',
    # 地緣政治
    'Iran', 'Israel', 'Taiwan', 'China', 'Russia', 'Ukraine',
    'war', 'missile', 'strike', 'sanctions', 'tariff', 'Trump',
    '台海', '制裁', '關稅', '戰爭', '衝突', '伊朗', '以色列',
    # 市場異常
    'crash', 'circuit breaker', 'halt', 'plunge', 'surge',
    '熔斷', '崩盤', '暴跌', '暴漲', '停牌', '緊急',
    # 科技 / 晶片（台股相關）
    'TSMC', 'Nvidia', 'chip ban', 'semiconductor', 'AI',
    '台積電', '晶片', '半導體',
    # 數字觸發（含百分比通常是重要數據）
    '%', 'billion', 'trillion',
]

# Yahoo Finance RSS 來源
YAHOO_RSS_URLS = [
    "https://finance.yahoo.com/rss/topstories",
]

NEWS_FRESH_WINDOW    = 30   # 分鐘，啟動時防炸版（_is_fresh）
MAX_NEWS_AGE_MINUTES = 120  # 分鐘，一般新聞有效期（120分鐘）
                             # 無時間戳的新聞：直接放行，交由 Gemini 判斷

# 財報/重要公司新聞：時效延長
EARNINGS_KEYWORDS = [
    'Sales', 'Revenue', 'Earnings', 'EPS', 'Profit',
    '營收', '獲利', '財報', '法說', 'Q1', 'Q2', 'Q3', 'Q4',
    'quarterly', 'annual',
]

IMPORTANT_COMPANIES = [
    'TSMC', 'Taiwan Semiconductor', '台積電',
    'Nvidia', 'NVDA', '輝達',
    'Apple', 'Microsoft', 'Fed', 'Federal Reserve',
]


def _get_max_age_minutes(title: str) -> int:
    """
    依新聞類型決定時效上限：
    - 央行/Fed 政策：720 分鐘（12 小時）
    - 重要公司財報：480 分鐘（8 小時）
    - 重要公司（非財報）：240 分鐘（4 小時）
    - 一般新聞：MAX_NEWS_AGE_MINUTES（120 分鐘）
    """
    title_lower = title.lower()

    fed_keywords = ['fed', 'federal reserve', 'fomc', 'rate decision', '央行', '升息', '降息']
    if any(kw in title_lower for kw in fed_keywords):
        return 720

    is_important = any(kw.lower() in title_lower for kw in IMPORTANT_COMPANIES)
    is_earnings  = any(kw.lower() in title_lower for kw in EARNINGS_KEYWORDS)

    if is_important and is_earnings:
        return 480
    if is_important:
        return 240

    return MAX_NEWS_AGE_MINUTES

COLLECTION_WINDOW_MINUTES = 5   # Level 3 收集窗口（分鐘）

# Yahoo Finance RSS 前置過濾：跳過明顯的分析文 / 非即時訊息
SKIP_PATTERNS = [
    "Is ",
    "A Good Stock",
    "Best Stock",
    "Analyst Report:",
    "Dear ",
    "Mark Your Calendar",
    "ETF Rally",
    " vs. ",
    "Should You Buy",
    "Here's Why",
    "Worth Buying",
    "Top Picks",
    "Stock of the Week",
    "Dividend Stock",
    "Buffett",            # 長線分析文
    "Warren Buffett",
]

# 持久化狀態檔
NEWS_STATE_FILE = "news_engine_state.json"

# --------------------------------------------------
# 持久化狀態（去重 + 窗口記錄）
# --------------------------------------------------

_sent_news_ids:  set = set()   # 今日已發送的 fingerprint
_sent_window_keys: set = set() # 今日已發送的 window_key

# Level 3 收集窗口（記憶體）
_event_collection: dict = {}   # key=window_key, value={'news': [], 'window_start': datetime}


def _load_news_state() -> None:
    """載入今日的已發送記錄（重啟不重發）。"""
    global _sent_news_ids, _sent_window_keys
    try:
        with open(NEWS_STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
        today = datetime.now().strftime('%Y%m%d')
        _sent_news_ids   = set(state.get('sent_ids', []))
        _sent_window_keys = set(
            k for k in state.get('sent_windows', [])
            if k.startswith(today)
        )
        print(f"✅ [news_engine] 載入狀態：{len(_sent_news_ids)} 筆已發，{len(_sent_window_keys)} 個已發窗口")
    except FileNotFoundError:
        _sent_news_ids    = set()
        _sent_window_keys = set()
    except Exception as e:
        print(f"⚠️ [news_engine] 載入狀態失敗：{e}")
        _sent_news_ids    = set()
        _sent_window_keys = set()


def _save_news_state() -> None:
    """將今日的已發送記錄寫入磁碟。"""
    today = datetime.now().strftime('%Y%m%d')
    state = {
        'sent_ids':     list(_sent_news_ids),
        'sent_windows': [k for k in _sent_window_keys if k.startswith(today)],
        'updated_at':   datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    try:
        with open(NEWS_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ [news_engine] 儲存狀態失敗：{e}")


# 模組載入時自動初始化
_load_news_state()


# --------------------------------------------------
# 時效與去重工具
# --------------------------------------------------

def _news_hash(title: str) -> str:
    return hashlib.md5(title.strip().encode("utf-8")).hexdigest()[:12]


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


def _is_fresh(pub_time_str: str) -> bool:
    """啟動防炸版：只保留 NEWS_FRESH_WINDOW 分鐘內的新聞。無法解析 → 視為新鮮。"""
    if not pub_time_str:
        return True
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S +0000"):
        try:
            dt = datetime.strptime(pub_time_str[:25], fmt[:len(pub_time_str[:25])])
            dt_naive = dt.replace(tzinfo=None)
            return (datetime.now() - dt_naive).total_seconds() / 60 <= NEWS_FRESH_WINDOW
        except Exception:
            continue
    return True


def _is_news_fresh(news: dict) -> bool:
    """
    判斷新聞是否在動態時效內。

    時間欄位來源（依序嘗試）：
        date / publish_time / pub_time / time / published / pubDate / pub_date

    時效上限由 _get_max_age_minutes(title) 決定：
    - 央行/Fed 政策：720 分鐘（12 小時）
    - 重要公司財報：480 分鐘（8 小時）
    - 重要公司（非財報）：240 分鐘（4 小時）
    - 一般新聞：MAX_NEWS_AGE_MINUTES（120 分鐘）

    處理策略：
    - 無時間戳：放行，交由 Gemini Tier 2 判斷（RSS 常見情況）
    - 解析成功：超過時效上限則略過
    - 解析失敗：放行，交由 Gemini 判斷（不因格式問題誤殺）
    """
    import re as _re

    news_time_str = (
        news.get("date") or
        news.get("publish_time") or
        news.get("pub_time") or
        news.get("time") or
        news.get("published") or
        news.get("pubDate") or
        news.get("pub_date") or
        ""
    )

    title_short = news.get("title", "")[:40]

    if not news_time_str:
        # 無時間戳（RSS 常見）：放行，依賴 Gemini 過濾
        return True

    time_str = str(news_time_str).strip()

    # 前處理：移除時區偏移（+0800、-0500）和 GMT 字串
    clean_str = _re.sub(r"\s*[+-]\d{4}$", "", time_str).replace("GMT", "").strip()

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%a, %d %b %Y %H:%M:%S",   # RSS 標準（去除時區後）
        "%d %b %Y %H:%M:%S",
        "%d %b %Y %H:%M",
    ]

    news_dt = None
    for fmt in formats:
        try:
            news_dt = datetime.strptime(clean_str[:25], fmt).replace(tzinfo=None)
            break
        except Exception:
            continue

    # 若原始字串含時區資訊，嘗試帶時區解析後轉 UTC naive
    if news_dt is None:
        try:
            from datetime import timezone
            news_dt = datetime.strptime(time_str, "%a, %d %b %Y %H:%M:%S %z")
            news_dt = news_dt.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            pass

    if news_dt is None:
        # 格式無法解析：放行，交由 Gemini 判斷
        print(
            f"⚠️ [news_engine] 時間格式無法解析，視為新聞繼續處理："
            f"{time_str[:30]}"
        )
        return True

    elapsed_min = (datetime.now() - news_dt).total_seconds() / 60
    max_age = _get_max_age_minutes(news.get('title', ''))

    if elapsed_min > max_age:
        print(
            f"[news_engine] skip old news ({elapsed_min:.0f}min, limit {max_age}min): {title_short}"
        )
        return False

    return True


def _get_window_key(dt: datetime = None) -> str:
    """
    取當前時間所在的 5 分鐘時間槽。
    例如 22:53 → '20260608_2250'，22:57 → '20260608_2255'。
    """
    if dt is None:
        dt = datetime.now()
    floored = (dt.minute // 5) * 5
    return dt.strftime(f"%Y%m%d_%H{floored:02d}")


# --------------------------------------------------
# 前置過濾：排除分析文 / 非即時訊息
# --------------------------------------------------

def _is_actionable_news(title: str) -> bool:
    """
    排除 Yahoo Finance RSS 常見的分析文、評比文、非即時訊息。
    命中任意 SKIP_PATTERNS 即回傳 False（跳過）。
    """
    for pattern in SKIP_PATTERNS:
        if pattern in title:
            return False
    return True


# --------------------------------------------------
# Tier 1：關鍵字粗篩
# --------------------------------------------------

def _tier1_filter(title: str) -> bool:
    """Tier 1 寬鬆關鍵字過濾：命中任意關鍵字即通過。"""
    t = title.lower()
    return any(kw.lower() in t for kw in TIER1_KEYWORDS)


# --------------------------------------------------
# Tier 2：Gemini 批次精判
# --------------------------------------------------

def _tier2_ai_filter(news_list: list) -> list:
    """
    把 Tier 1 篩出的新聞標題批次送 Gemini 精判。
    問題：「哪些今天可能讓台指期波動超過100點？」
    回傳：Gemini 認為重要的新聞子集合。
    失敗時 fallback → 回傳全部（保守不丟棄）。
    """
    if not news_list:
        return []

    titles_text = '\n'.join([
        f"{i + 1}. {n['title']}"
        for i, n in enumerate(news_list)
    ])

    prompt = (
        f"你是台指期分析師。以下新聞標題中，哪些今天可能讓台指期波動超過100點？\n\n"
        f"{titles_text}\n\n"
        f"請只回答編號，用逗號分隔。沒有則回答「無」。\n"
        f"例如：1,3 或 無\n\n"
        f"不要解釋，只回答編號或「無」。"
    )

    try:
        import google.generativeai as genai
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        model = genai.GenerativeModel(
            "gemini-2.5-flash-preview-05-20",
            generation_config={"max_output_tokens": 20},
        )
        response = model.generate_content(prompt)
        result   = response.text.strip()

        print(f"🤖 [news_engine] Tier2 Gemini 回應：{result!r}")

        if not result or result == "無":
            return []

        indices = []
        for part in result.replace("，", ",").split(","):
            try:
                idx = int(part.strip()) - 1
                if 0 <= idx < len(news_list):
                    indices.append(idx)
            except Exception:
                continue

        return [news_list[i] for i in indices]

    except Exception as e:
        print(f"⚠️ [news_engine] Tier2 AI 判斷失敗（fallback 回傳全部）：{e}")
        return news_list   # 保守 fallback，不丟棄


# --------------------------------------------------
# 合併發送
# --------------------------------------------------

def _send_merged_event(window_key: str, news_list: list) -> None:
    """
    合併該窗口內所有重大新聞為一則警報，送 Gemini 生成摘要後發出。
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
            f"【摘要】本輪出現重大市場事件，請留意相關風險\n"
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


def _get_commentary_with_retry(prompt: str, max_tokens: int = 350) -> Optional[str]:
    """呼叫 Gemini 生成快評，失敗時靜默回傳 None。"""
    try:
        from ai_report_engine import _call_gemini_with_retry
        return _call_gemini_with_retry(prompt, max_tokens=max_tokens)
    except Exception as e:
        print(f"⚠️ [news_engine] Gemini 快評失敗：{e}")
        return None


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
                    "source":   "FinMind",
                    "stock_id": stock_id,
                    "title":    str(row.get("title", "")),
                    "content":  str(row.get("description", "") or row.get("content", "")),
                    "url":      str(row.get("link", "") or row.get("url", "")),
                    "pub_time": str(row.get("date", "")),
                })
        except Exception:
            pass

    return items


MAX_RSS_ITEMS = 20  # 每個 RSS 來源只取最新 N 篇，避免處理幾個月前的舊文章


@safe_execute
def _fetch_yahoo_rss() -> list[dict]:
    """從 Yahoo Finance RSS 抓取國際市場新聞。"""
    items   = []
    headers = {"User-Agent": "Mozilla/5.0 ATOS-NewsEngine/1.0"}

    for url in YAHOO_RSS_URLS:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml_data = resp.read()

            root = ET.fromstring(xml_data)

            raw_items = root.findall(".//item")
            # 只取最新 MAX_RSS_ITEMS 篇，RSS 通常已按時間降序排列
            raw_items = raw_items[:MAX_RSS_ITEMS]

            for item_el in raw_items:
                def _text(tag):
                    el = item_el.find(tag)
                    return el.text.strip() if el is not None and el.text else ""

                pub_time = _text("pubDate")

                # 24 小時硬過濾：有時間戳才過濾，無時間戳讓後續 Gemini 判斷
                if pub_time:
                    try:
                        from dateutil import parser as _dateutil_parser
                        from datetime import timezone as _tz
                        _dt = _dateutil_parser.parse(str(pub_time))
                        if _dt.tzinfo:
                            _dt = _dt.astimezone(_tz.utc).replace(tzinfo=None)
                        _elapsed_h = (datetime.now() - _dt).total_seconds() / 3600
                        if _elapsed_h > 24:
                            continue  # 超過 24 小時直接跳過
                    except Exception:
                        pass  # 解析失敗繼續處理

                items.append({
                    "source":   "Yahoo Finance",
                    "stock_id": None,
                    "title":    _text("title"),
                    "content":  _text("description"),
                    "url":      _text("link"),
                    "pub_time": pub_time,
                })
        except Exception:
            pass

    return items


# --------------------------------------------------
# 主函式
# --------------------------------------------------

def poll_news(chip_ctx: Optional[dict] = None) -> int:
    """
    輪詢所有新聞來源，兩層過濾後窗口合併發送。

    流程：
        抓取 → 時效過濾（60分鐘） → fingerprint 去重
        → Tier 1 關鍵字粗篩 → Tier 2 Gemini 精判（一次呼叫）
        → 5分鐘窗口合併發送（每窗口只發一次）

    Returns:
        本次發送的警報數（0 或 1）
    """
    finmind_items: list = _fetch_finmind_news() or []
    yahoo_items:   list = _fetch_yahoo_rss()    or []
    all_items = finmind_items + yahoo_items

    if not all_items:
        return 0

    # ① 啟動防炸版：只處理 NEWS_FRESH_WINDOW 分鐘內的新聞
    fresh_items = [
        item for item in all_items
        if item.get("title") and
        _is_fresh(item.get("pub_time", "") or item.get("date", ""))
    ]

    # ② 時效過濾：超過 MAX_NEWS_AGE_MINUTES 的新聞不處理
    fresh_items = [n for n in fresh_items if _is_news_fresh(n)]

    if not fresh_items:
        _flush_expired_windows()
        return 0

    # ②-B 分析文前置過濾（排除非即時訊息，主要針對 Yahoo RSS）
    fresh_items = [
        n for n in fresh_items
        if _is_actionable_news(n.get("title", ""))
    ]

    if not fresh_items:
        _flush_expired_windows()
        return 0

    # ③ fingerprint 去重（session + 持久化雙層）
    new_items = [
        n for n in fresh_items
        if _get_news_fingerprint(n) not in _sent_news_ids
    ]

    if not new_items:
        _flush_expired_windows()
        return 0

    # ④ Tier 1：關鍵字粗篩
    tier1_passed = [n for n in new_items if _tier1_filter(n["title"])]

    if not tier1_passed:
        _flush_expired_windows()
        return 0

    print(f"[news_engine] Tier1 通過 {len(tier1_passed)} 則，送 Gemini 精判...")

    # ⑤ Tier 2：Gemini 批次精判（一次呼叫）
    important_news = _tier2_ai_filter(tier1_passed)

    if not important_news:
        print("[news_engine] Gemini 判斷：本輪無重大影響新聞")
        _flush_expired_windows()
        return 0

    print(f"[news_engine] Gemini 確認 {len(important_news)} 則重大新聞")

    # ⑥ 取當前 5 分鐘窗口
    now        = datetime.now()
    window_key = _get_window_key(now)

    if window_key in _sent_window_keys:
        print(f"[news_engine] 窗口 {window_key} 已發送，略過")
        # 仍更新 fingerprint，防止下輪重複處理
        for n in important_news:
            _sent_news_ids.add(_get_news_fingerprint(n))
        _save_news_state()
        return 0

    # ⑦ 加入收集窗口（允許同一輪持續收集到窗口結束）
    if window_key not in _event_collection:
        _event_collection[window_key] = {
            'news':         [],
            'window_start': now,
        }
    for n in important_news:
        _event_collection[window_key]['news'].append(n)

    count = len(_event_collection[window_key]['news'])
    print(f"[news_engine] 窗口 {window_key} 已收集 {count} 則")

    # ⑧ 窗口已滿（超過 5 分鐘）→ 立即合併發送
    window_age = (now - _event_collection[window_key]['window_start']).total_seconds() / 60
    if window_age >= COLLECTION_WINDOW_MINUTES:
        _fire_window(window_key, important_news)
        return 1

    # 尚在收集窗口內，同時掃其他過期窗口
    _flush_expired_windows()
    return 0


def _fire_window(window_key: str, fallback_news: list) -> None:
    """發送指定窗口，並更新持久化狀態。"""
    news_list = _event_collection.get(window_key, {}).get('news') or fallback_news
    _send_merged_event(window_key, news_list)
    _sent_window_keys.add(window_key)
    for n in news_list:
        _sent_news_ids.add(_get_news_fingerprint(n))
    if window_key in _event_collection:
        del _event_collection[window_key]
    _save_news_state()


def _flush_expired_windows() -> None:
    """掃描並發送所有已過 5 分鐘收集窗口的事件。"""
    now = datetime.now()
    for wk in list(_event_collection.keys()):
        if wk in _sent_window_keys:
            del _event_collection[wk]
            continue
        window_age = (now - _event_collection[wk]['window_start']).total_seconds() / 60
        if window_age >= COLLECTION_WINDOW_MINUTES:
            _fire_window(wk, _event_collection[wk]['news'])


# --------------------------------------------------
# 向後相容
# --------------------------------------------------

def _is_major_event(title: str, content: str = "") -> bool:
    """向後相容：命中 Tier 1 關鍵字即視為重大事件。"""
    return _tier1_filter(title + " " + content)


def batch_evaluate_news(news_list: list) -> list:
    """向後相容入口，改用 Tier 2 邏輯。"""
    return _tier2_ai_filter(news_list)


# --------------------------------------------------
# 手動測試
# --------------------------------------------------

if __name__ == "__main__":
    print("=== news_engine 手動測試 ===\n")

    print("--- Tier 1 關鍵字過濾測試 ---")
    samples = [
        "Fed announces emergency rate cut",
        "Trump signs new chip ban executive order",
        "Iran missile strike on Israel bases",
        "Taiwan Strait tensions escalate sharply",
        "TSMC reports record profits",
        "聯準會 FOMC 決議降息一碼",
        "台海情勢緊張，外資大幅賣超",
        "外資賣超台積電三百億",
        "台股今日小漲，量能普通",
        "天氣晴，無重大事件",
    ]
    for title in samples:
        passed = _tier1_filter(title)
        print(f"  {'✅' if passed else '❌'} {title[:60]}")

    print()
    print("--- window_key 測試 ---")
    for minute in [0, 4, 5, 9, 53, 55, 59]:
        dt = datetime(2026, 6, 8, 22, minute)
        print(f"  22:{minute:02d} → {_get_window_key(dt)}")

    print()
    print("--- 持久化狀態 ---")
    print(f"  已發 fingerprint：{len(_sent_news_ids)} 筆")
    print(f"  已發窗口：{len(_sent_window_keys)} 個")

    print()
    print("--- Yahoo RSS 抓取 ---")
    yahoo_items = _fetch_yahoo_rss() or []
    print(f"  Yahoo RSS 筆數：{len(yahoo_items)}")
    if yahoo_items:
        print(f"  最新標題：{yahoo_items[0].get('title', '')[:60]}")

    print()
    print("--- FinMind 新聞抓取 ---")
    fm_items = _fetch_finmind_news() or []
    print(f"  FinMind 新聞筆數：{len(fm_items)}")
    if fm_items:
        print(f"  最新標題：{fm_items[0].get('title', '')[:60]}")
