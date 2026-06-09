# data_engine.py

import os
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from FinMind.data import DataLoader

from error_handler import safe_execute
from persistent_state import load_state, save_state
load_dotenv()


# --------------------------------------------------
# Config
# --------------------------------------------------

FUGLE_BASE_URL = "https://api.fugle.tw/marketdata/v1.0/stock"
FINMIND_FUTURES_SNAPSHOT_URL = "https://api.finmindtrade.com/api/v4/taiwan_futures_snapshot"

PROJECT_DIR = Path(__file__).resolve().parent
TXF_TICK_CACHE_FILE = PROJECT_DIR / "txf_tick_cache.json"

DEFAULT_FUTURES_SYMBOL = "TXF"
BACKUP_INDEX_SYMBOL = "IX0001"
TXF_CACHE_KEEP_DAYS = 3

# 正式跑 main_commander.py 時建議 False，避免每 30 秒印整張候選合約表
DEBUG_SNAPSHOT_CANDIDATES = False

# snapshot 超過幾秒就視為非即時資料
SNAPSHOT_MAX_DELAY_SECONDS = 90
DAY_SESSION_START = "08:45"
DAY_SESSION_END = "13:45"
OPTION_OI_NEARBY_WINDOW_POINTS = 2000

# --------------------------------------------------
# FinMind API
# --------------------------------------------------

def get_finmind_token() -> str:
    token = os.getenv("FIN_TOKEN") or os.getenv("FINMIND_TOKEN")

    if not token:
        raise ValueError("FINMIND_TOKEN missing")

    return token


def get_finmind_api() -> DataLoader:
    """
    建立 FinMind SDK Client。

    注意：
    - 一般資料集可用 SDK
    - taiwan_futures_snapshot 使用 REST endpoint
    """

    token = get_finmind_token()

    api = DataLoader()
    api.login_by_token(token)

    return api


# --------------------------------------------------
# Common Helpers
# --------------------------------------------------

def is_valid_price(value) -> bool:
    try:
        return value is not None and float(value) > 0
    except Exception:
        return False


def safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value, default=0):
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def extract_nested_price(data: dict, path: list[str]):
    current = data

    try:
        for key in path:
            if not isinstance(current, dict):
                return None

            current = current.get(key)

        return current

    except Exception:
        return None


def parse_any_datetime(value) -> datetime:
    """
    支援：
    - FinMind date string
    - Fugle microseconds timestamp
    - Fugle milliseconds timestamp
    - seconds timestamp
    """

    if not value:
        return datetime.now()

    try:
        if isinstance(value, int) or str(value).isdigit():
            ts = int(value)

            if ts > 10_000_000_000_000:
                return datetime.fromtimestamp(ts / 1_000_000)

            if ts > 10_000_000_000:
                return datetime.fromtimestamp(ts / 1_000)

            return datetime.fromtimestamp(ts)

        dt = pd.to_datetime(value, errors="coerce")

        if pd.isna(dt):
            return datetime.now()

        if getattr(dt, "tzinfo", None) is not None:
            try:
                dt = dt.tz_convert("Asia/Taipei").tz_localize(None)
            except Exception:
                try:
                    dt = dt.tz_localize(None)
                except Exception:
                    pass

        return dt.to_pydatetime()

    except Exception:
        return datetime.now()


def parse_any_timestamp(value) -> str:
    dt = parse_any_datetime(value)
    return dt.strftime("%H:%M:%S")


def is_snapshot_fresh(
    raw_date,
    max_delay_seconds: int = SNAPSHOT_MAX_DELAY_SECONDS,
) -> tuple[bool, float]:
    """
    檢查 snapshot 是否足夠新。

    回傳：
    - is_fresh
    - delay_seconds
    """

    try:
        tick_dt = parse_any_datetime(raw_date)
        delay = (datetime.now() - tick_dt).total_seconds()

        return delay <= max_delay_seconds, round(delay, 1)

    except Exception:
        return False, 999999


# --------------------------------------------------
# Fugle Helper：只作為備援參考
# --------------------------------------------------

def get_fugle_api_key() -> str | None:
    api_key = os.getenv("FUGLE_API_KEY")

    if not api_key:
        print("⚠️ FUGLE_API_KEY 未設定")
        return None

    return api_key


def fugle_get(endpoint: str, params: dict | None = None) -> dict | None:
    """
    Fugle 目前只作為 IX0001 加權指數備援。
    不作為台指期交易訊號資料。
    """

    api_key = get_fugle_api_key()

    if not api_key:
        return None

    url = f"{FUGLE_BASE_URL}{endpoint}"

    try:
        res = requests.get(
            url,
            headers={"X-API-KEY": api_key},
            params=params or {},
            timeout=8,
        )

        if res.status_code != 200:
            print(f"⚠️ Fugle API error: {res.status_code} {res.text}")
            return None

        return res.json()

    except Exception as e:
        print(f"⚠️ Fugle request failed: {e}")
        return None


# --------------------------------------------------
# Retry Helper
# --------------------------------------------------

def _fetch_with_retry(fetch_func, max_retries: int = 2, delay: int = 3):
    """
    通用 retry wrapper。
    失敗最多重試 max_retries 次，每次間隔 delay 秒。
    """
    for attempt in range(max_retries + 1):
        try:
            result = fetch_func()
            if result is not None:
                return result
        except Exception as e:
            if attempt < max_retries:
                print(f"⚠️ 第{attempt + 1}次失敗，{delay}秒後重試：{e}")
                time.sleep(delay)
            else:
                print(f"⚠️ 重試{max_retries}次後仍失敗：{e}")
    return None


# --------------------------------------------------
# Fugle TXFR1 台指期備援
# --------------------------------------------------

def _get_fugle_futures_price() -> dict | None:
    """
    Fugle 台指期近月合約（TXFR1）備援報價。
    比 IX0001 加權指數更準確，直接是台指期價格。
    is_realtime = False（備援來源，不觸發交易）。
    """
    try:
        fugle_key = os.getenv("FUGLE_API_KEY")
        if not fugle_key:
            return None

        url = "https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/TXFR1"
        headers = {"X-API-KEY": fugle_key}
        r = requests.get(url, headers=headers, timeout=8)

        if r.status_code != 200:
            print(f"⚠️ Fugle TXFR1 HTTP error: {r.status_code}")
            return None

        data = r.json()
        price = (
            data.get("closePrice")
            or data.get("lastPrice")
            or data.get("price")
        )

        if not price:
            print(f"⚠️ Fugle TXFR1 無有效價格，raw={data}")
            return None

        price_f = float(price)
        print(f"✅ Fugle TXFR1 fallback：{price_f}")

        return {
            "price": round(price_f, 1),
            "source": "FUGLE_TXFR1_FALLBACK",
            "tick_source": "FUGLE_TXFR1_FALLBACK",
            "time": datetime.now().strftime("%H:%M:%S"),
            "is_realtime": False,
            "note": "Fugle TXFR1 台指期近月備援，不觸發交易。",
        }

    except Exception as e:
        print(f"⚠️ Fugle TXFR1 fallback 失敗：{e}")
        return None


# --------------------------------------------------
# TXF Snapshot Cache
# --------------------------------------------------

def load_txf_tick_cache() -> list[dict]:
    if not TXF_TICK_CACHE_FILE.exists():
        return []

    try:
        with open(TXF_TICK_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return data

        return []

    except Exception as e:
        print(f"⚠️ 讀取 TXF tick cache 失敗：{e}")
        return []


def save_txf_tick_cache(rows: list[dict]) -> None:
    try:
        with open(TXF_TICK_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                rows,
                f,
                ensure_ascii=False,
                indent=2,
            )

    except Exception as e:
        print(f"⚠️ 儲存 TXF tick cache 失敗：{e}")


def clean_txf_tick_cache(rows: list[dict]) -> list[dict]:
    if not rows:
        return []

    cleaned = []
    cutoff = datetime.now() - timedelta(days=TXF_CACHE_KEEP_DAYS)
    seen_keys = set()

    for row in rows:
        try:
            dt = parse_any_datetime(row.get("datetime"))
            price = row.get("price")

            if dt < cutoff:
                continue

            if not is_valid_price(price):
                continue

            key = (
                row.get("symbol", DEFAULT_FUTURES_SYMBOL),
                row.get("futures_id", ""),
                dt.strftime("%Y-%m-%d %H:%M:%S"),
                str(price),
            )

            if key in seen_keys:
                continue

            seen_keys.add(key)

            cleaned.append({
                "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": row.get("symbol", DEFAULT_FUTURES_SYMBOL),
                "futures_id": row.get("futures_id"),
                "price": round(float(price), 1),
                "volume": safe_int(row.get("volume")),
                "total_volume": safe_int(row.get("total_volume")),
                "source": row.get("source", "FINMIND_FUTURES_SNAPSHOT"),
            })

        except Exception:
            continue

    cleaned.sort(key=lambda x: x["datetime"])

    return cleaned


def append_txf_tick_cache(tick: dict) -> None:
    """
    只有真正 realtime 的 TXF snapshot 才寫入 cache。
    非即時、Fugle 備援、無效價都不寫。
    """

    if not tick:
        return

    if tick.get("source") != "FINMIND_FUTURES_SNAPSHOT":
        return

    if not tick.get("is_realtime"):
        return

    price = tick.get("price")

    if not is_valid_price(price):
        return

    dt_raw = tick.get("raw_date") or tick.get("datetime") or datetime.now()
    dt = parse_any_datetime(dt_raw)

    row = {
        "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": tick.get("raw_symbol", DEFAULT_FUTURES_SYMBOL),
        "futures_id": tick.get("futures_id"),
        "price": round(float(price), 1),
        "volume": safe_int(tick.get("volume")),
        "total_volume": safe_int(tick.get("total_volume")),
        "source": "FINMIND_FUTURES_SNAPSHOT",
    }

    rows = load_txf_tick_cache()
    rows.append(row)
    rows = clean_txf_tick_cache(rows)
    save_txf_tick_cache(rows)


def txf_tick_cache_to_dataframe() -> pd.DataFrame | None:
    rows = load_txf_tick_cache()
    rows = clean_txf_tick_cache(rows)

    if not rows:
        return None

    df = pd.DataFrame(rows)

    if df.empty:
        return None

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")

    df["volume"] = pd.to_numeric(
        df.get("volume", 0),
        errors="coerce",
    ).fillna(0)

    df["total_volume"] = pd.to_numeric(
        df.get("total_volume", 0),
        errors="coerce",
    ).fillna(0)

    df = df.dropna(subset=["datetime", "price"])
    df = df[df["price"] > 0]

    if df.empty:
        return None

    return df.sort_values("datetime")


# --------------------------------------------------
# FinMind 台指期 Snapshot 即時報價：主力選擇
# --------------------------------------------------

def select_main_futures_snapshot_row(
    df: pd.DataFrame,
    data_id: str = DEFAULT_FUTURES_SYMBOL,
):
    """
    從 FinMind futures snapshot 回傳資料中選出主力台指期合約。

    原則：
    1. 優先選 futures_id 以 TXF 開頭
    2. 排除無效價格
    3. 以 total_volume 最大者作為主力
    4. total_volume 不足時，用 volume 最大者
    """

    if df is None or df.empty:
        return None

    df = df.copy()

    if "futures_id" not in df.columns:
        return df.iloc[-1]

    df["futures_id"] = df["futures_id"].astype(str)

    product_prefix = str(data_id).upper()

    df = df[
        df["futures_id"].str.upper().str.startswith(product_prefix)
    ].copy()

    if df.empty:
        print(f"⚠️ snapshot 找不到 {product_prefix} 開頭的 futures_id")
        return None

    for col in ["close", "buy_price", "sell_price", "average_price"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    def has_valid_row_price(row):
        close_price = row.get("close")
        buy_price = row.get("buy_price")
        sell_price = row.get("sell_price")
        average_price = row.get("average_price")

        if is_valid_price(close_price):
            return True

        if is_valid_price(buy_price) and is_valid_price(sell_price):
            return True

        if is_valid_price(average_price):
            return True

        return False

    df = df[df.apply(has_valid_row_price, axis=1)].copy()

    if df.empty:
        print("⚠️ snapshot TXF 系列沒有有效價格")
        return None

    if "total_volume" in df.columns:
        df["total_volume"] = pd.to_numeric(
            df["total_volume"],
            errors="coerce",
        ).fillna(0)
    else:
        df["total_volume"] = 0

    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(
            df["volume"],
            errors="coerce",
        ).fillna(0)
    else:
        df["volume"] = 0

    df = df.sort_values(
        ["total_volume", "volume"],
        ascending=False,
    )

    row = df.iloc[0]

    print(
        "✅ TXF 主力 snapshot selected: "
        f"{row.get('futures_id')} | "
        f"price={row.get('close')} | "
        f"volume={row.get('volume')} | "
        f"total_volume={row.get('total_volume')}"
    )

    return row


def print_snapshot_candidates(df: pd.DataFrame, data_id: str = DEFAULT_FUTURES_SYMBOL):
    """
    Debug 用：列出 snapshot 回傳的 TXF 候選合約。
    """

    try:
        if df is None or df.empty:
            print("⚠️ snapshot candidates empty")
            return

        view = df.copy()

        if "futures_id" in view.columns:
            view["futures_id"] = view["futures_id"].astype(str)
            view = view[
                view["futures_id"].str.upper().str.startswith(
                    str(data_id).upper()
                )
            ].copy()

        cols = [
            "futures_id",
            "date",
            "close",
            "buy_price",
            "sell_price",
            "volume",
            "total_volume",
        ]

        cols = [c for c in cols if c in view.columns]

        if not cols:
            print("⚠️ snapshot candidates 無可顯示欄位")
            return

        print("📋 TXF snapshot candidates:")
        print(view[cols].to_string(index=False))

    except Exception as e:
        print(f"⚠️ print_snapshot_candidates failed: {e}")


@safe_execute
def get_futures_snapshot_tick(
    data_id: str = DEFAULT_FUTURES_SYMBOL,
    write_cache: bool = True,
) -> dict | None:
    """
    使用 FinMind futures snapshot endpoint 取得台指期即時報價。

    正確 endpoint：
    https://api.finmindtrade.com/api/v4/taiwan_futures_snapshot

    注意：
    - 不是 /api/v4/data
    - 不帶 dataset 參數
    - token 用 Authorization: Bearer <token>
    """

    try:
        token = get_finmind_token()

        headers = {
            "Authorization": f"Bearer {token}",
        }

        params = {
            "data_id": data_id,
        }

        response = requests.get(
            FINMIND_FUTURES_SNAPSHOT_URL,
            headers=headers,
            params=params,
            timeout=15,
        )

        if response.status_code != 200:
            print(
                f"⚠️ FinMind futures snapshot HTTP error: "
                f"{response.status_code} {response.text}"
            )
            return None

        payload = response.json()

        if not payload.get("status"):
            print(f"⚠️ FinMind futures snapshot API error: {payload}")
            return None

        data = payload.get("data", [])

        if not data:
            print(f"⚠️ FinMind futures snapshot 無資料：data_id={data_id}")
            return None

        df = pd.DataFrame(data)

        if df is None or df.empty:
            print(f"⚠️ FinMind futures snapshot 無資料：data_id={data_id}")
            return None

        # 印出候選合約，確認是不是選到真正主力
        # Debug 用：確認是不是選到真正主力
        if DEBUG_SNAPSHOT_CANDIDATES:
            print_snapshot_candidates(
            df=df,
            data_id=data_id,
        )

        row = select_main_futures_snapshot_row(
            df=df,
            data_id=data_id,
        )

        if row is None:
            print(f"⚠️ FinMind futures snapshot 找不到可用主力合約：{data_id}")
            return None

        close_price = row.get("close")
        buy_price = row.get("buy_price")
        sell_price = row.get("sell_price")
        average_price = row.get("average_price")

        price = None
        price_source = None

        if is_valid_price(close_price):
            price = float(close_price)
            price_source = "close"

        elif is_valid_price(buy_price) and is_valid_price(sell_price):
            price = (float(buy_price) + float(sell_price)) / 2
            price_source = "bid_ask_mid"

        elif is_valid_price(average_price):
            price = float(average_price)
            price_source = "average_price"

        if not is_valid_price(price):
            print(f"⚠️ FinMind futures snapshot 無有效價格：{row.to_dict()}")
            return None

        raw_date = row.get("date")
        parsed_dt = parse_any_datetime(raw_date)

        is_fresh, delay_seconds = is_snapshot_fresh(
            raw_date=raw_date,
            max_delay_seconds=SNAPSHOT_MAX_DELAY_SECONDS,
        )

        tick = {
            "price": round(float(price), 1),
            "source": "FINMIND_FUTURES_SNAPSHOT",
            "price_source": price_source,
            "time": parsed_dt.strftime("%H:%M:%S"),
            "datetime": parsed_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "is_realtime": is_fresh,
            "delay_seconds": delay_seconds,

            "raw_symbol": data_id,
            "futures_id": row.get("futures_id", data_id),

            "open": safe_float(row.get("open")),
            "high": safe_float(row.get("high")),
            "low": safe_float(row.get("low")),
            "close": safe_float(row.get("close")),
            "buy_price": safe_float(row.get("buy_price")),
            "sell_price": safe_float(row.get("sell_price")),
            "volume": safe_int(row.get("volume")),
            "total_volume": safe_int(row.get("total_volume")),
            "change_price": safe_float(row.get("change_price")),
            "change_rate": safe_float(row.get("change_rate")),
            "tick_type": safe_int(row.get("TickType")),
            "raw_date": str(raw_date) if raw_date is not None else None,
        }

        if not is_fresh:
            print(
                f"⚠️ FinMind snapshot 資料延遲 {delay_seconds} 秒，"
                "標記為 NOT_REALTIME，禁止交易"
            )

        if write_cache:
            append_txf_tick_cache(tick)

        return tick

    except Exception as e:
        print(f"⚠️ FinMind futures snapshot failed: {e}")
        return None


# --------------------------------------------------
# Fugle IX0001 備援報價
# --------------------------------------------------

@safe_execute
def get_fugle_index_backup_tick(symbol: str = BACKUP_INDEX_SYMBOL) -> dict | None:
    """
    Fugle IX0001 只作為備援參考。
    is_realtime 永遠 False。
    """

    raw = fugle_get(f"/intraday/quote/{symbol}")

    if raw is None:
        return None

    data = raw.get("data", raw)

    is_trial = bool(data.get("isTrial", False))
    is_open = bool(data.get("isOpen", False))
    is_close = bool(data.get("isClose", False))
    is_continuous = bool(data.get("isContinuous", False))

    last_updated = (
        data.get("lastUpdated")
        or data.get("closeTime")
        or data.get("openTime")
    )

    readable_time = parse_any_timestamp(last_updated)

    base_payload = {
        "is_realtime": False,
        "raw_symbol": symbol,
        "note": "Fugle IX0001 是加權指數備援，不是台指期即時價，不觸發交易。",
        "is_trial": is_trial,
        "is_open": is_open,
        "is_close": is_close,
        "is_continuous": is_continuous,
    }

    last_price = data.get("lastPrice")

    if is_valid_price(last_price):
        return {
            **base_payload,
            "price": round(float(last_price), 1),
            "source": "FUGLE_IX0001_LAST_PRICE_BACKUP",
            "time": readable_time,
        }

    last_trade_price = extract_nested_price(data, ["lastTrade", "price"])
    last_trade_time = extract_nested_price(data, ["lastTrade", "time"])

    if is_valid_price(last_trade_price):
        return {
            **base_payload,
            "price": round(float(last_trade_price), 1),
            "source": "FUGLE_IX0001_LAST_TRADE_BACKUP",
            "time": parse_any_timestamp(last_trade_time),
        }

    close_price = data.get("closePrice")

    if is_valid_price(close_price):
        return {
            **base_payload,
            "price": round(float(close_price), 1),
            "source": "FUGLE_IX0001_CLOSE_PRICE_BACKUP",
            "time": readable_time,
        }

    avg_price = data.get("avgPrice") or data.get("averagePrice")

    if is_valid_price(avg_price):
        return {
            **base_payload,
            "price": round(float(avg_price), 1),
            "source": "FUGLE_IX0001_AVG_PRICE_BACKUP",
            "time": readable_time,
        }

    trial_price = extract_nested_price(data, ["lastTrial", "price"])
    trial_time = extract_nested_price(data, ["lastTrial", "time"])

    if is_valid_price(trial_price):
        print(
            f"⚠️ Fugle 目前為試撮 / 非正式成交價，price={trial_price}，"
            "標記為 NOT_REALTIME"
        )

        return {
            **base_payload,
            "price": round(float(trial_price), 1),
            "source": "FUGLE_IX0001_TRIAL_PRICE_BACKUP",
            "time": parse_any_timestamp(trial_time),
            "is_trial": True,
        }

    previous_close = data.get("previousClose")

    if is_valid_price(previous_close):
        print(
            f"⚠️ Fugle 目前無即時成交價，改用 previousClose={previous_close}，"
            "標記為 NOT_REALTIME"
        )

        return {
            **base_payload,
            "price": round(float(previous_close), 1),
            "source": "FUGLE_IX0001_PREVIOUS_CLOSE_BACKUP",
            "time": datetime.now().strftime("%H:%M:%S"),
        }

    print(f"⚠️ Fugle 回傳無有效價格，raw={raw}")

    return None


# --------------------------------------------------
# 即時價格感測器
# --------------------------------------------------

@safe_execute
def get_realtime_tick(symbol: str = DEFAULT_FUTURES_SYMBOL) -> dict | None:
    """
    即時報價，帶 retry 與多層 fallback。

    優先順序：
    1. FinMind REST taiwan_futures_snapshot（重試最多2次，間隔3秒）
    2. Fugle TXFR1 台指期近月（點位最接近台指期）
    3. Fugle IX0001 加權指數（最後手段，點位有差距）
    4. state 快取價格（5分鐘內有效）

    每次成功取得價格都寫入 state['tick_source'] 與 state['last_price_update']。
    """

    # ── 第一優先：FinMind，帶 retry ──────────────────────────────────
    futures_tick = _fetch_with_retry(
        lambda: get_futures_snapshot_tick(data_id=symbol, write_cache=True),
        max_retries=2,
        delay=3,
    )

    if futures_tick:
        _write_tick_source_to_state(
            futures_tick.get("tick_source") or futures_tick.get("source"),
        )
        return futures_tick

    # ── 第二優先：Fugle TXFR1 台指期近月 ────────────────────────────
    print("⚠️ FinMind futures snapshot 失敗（含重試），fallback Fugle TXFR1")
    txfr1_tick = _get_fugle_futures_price()

    if txfr1_tick:
        _write_tick_source_to_state("FUGLE_TXFR1_FALLBACK")
        return txfr1_tick

    # ── 第三優先：Fugle IX0001 加權指數（最後手段）──────────────────
    print("⚠️ Fugle TXFR1 失敗，最後手段 fallback Fugle IX0001")
    print("⚠️ 注意：IX0001 為加權指數，非台指期，點位可能有差距")
    print("⚠️ 警報觸發點位以台指期為準，本次 fallback 僅供參考")

    ix_tick = get_fugle_index_backup_tick(symbol=BACKUP_INDEX_SYMBOL)
    if ix_tick:
        ix_tick["tick_source"] = "FUGLE_IX0001_FALLBACK"
        _write_tick_source_to_state("FUGLE_IX0001_FALLBACK")
        return ix_tick

    # ── 第四優先：state 快取價格（5分鐘內有效）──────────────────────
    try:
        _state = load_state()
        cached_price = _state.get("price")
        cached_time  = _state.get("last_price_update")

        if cached_price and cached_time:
            elapsed = (
                datetime.now() - datetime.fromisoformat(cached_time)
            ).total_seconds()

            if elapsed < 300:
                print(
                    f"⚠️ 使用快取價格（{elapsed:.0f}秒前）：{cached_price}"
                )
                _write_tick_source_to_state("CACHE_FALLBACK")
                return {
                    "price": float(cached_price),
                    "source": "CACHE_FALLBACK",
                    "tick_source": "CACHE_FALLBACK",
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "is_realtime": False,
                    "note": f"快取價格，{elapsed:.0f}秒前更新，僅供參考。",
                }
    except Exception:
        pass

    print("🚨 所有價格來源失敗，無法取得即時報價")
    return None


def _write_tick_source_to_state(source: str | None) -> None:
    """在 state 寫入 tick_source 與 last_price_update，供後續模組識別資料來源。"""
    try:
        _state = load_state()
        _state["tick_source"] = source or "UNKNOWN"
        _state["last_price_update"] = datetime.now().isoformat()
        save_state(_state)
    except Exception:
        pass

def update_day_session_state_from_5min(df_5min: pd.DataFrame) -> bool:
    """
    從今日 TXF 5分K 計算日盤實際高低點，並寫入 atos_state.json。

    寫入欄位：
    - today_high
    - today_low
    - day_session_high
    - day_session_low
    - day_session_date
    - day_session_ready
    - latest_k_time
    - price
    """

    try:
        if df_5min is None or df_5min.empty:
            return False

        df = df_5min.copy()

        if "datetime" not in df.columns:
            return False

        required_cols = ["high", "low", "close"]

        for col in required_cols:
            if col not in df.columns:
                return False

        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df["high"] = pd.to_numeric(df["high"], errors="coerce")
        df["low"] = pd.to_numeric(df["low"], errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")

        df = df.dropna(subset=["datetime", "high", "low", "close"])

        if df.empty:
            return False

        today = datetime.now().date()

        df = df[df["datetime"].dt.date == today].copy()

        if df.empty:
            return False

        session_start = pd.to_datetime(f"{today} {DAY_SESSION_START}")
        session_end = pd.to_datetime(f"{today} {DAY_SESSION_END}")

        df_day = df[
            (df["datetime"] >= session_start)
            & (df["datetime"] <= session_end)
        ].copy()

        if df_day.empty:
            return False

        today_high = round(float(df_day["high"].max()), 1)
        today_low = round(float(df_day["low"].min()), 1)

        latest_row = df_day.sort_values("datetime").iloc[-1]

        latest_k_time = latest_row["datetime"].strftime("%Y-%m-%d %H:%M:%S")
        latest_close = round(float(latest_row["close"]), 1)

        state = load_state()

        state["today_high"] = today_high
        state["today_low"] = today_low

        state["day_session_high"] = today_high
        state["day_session_low"] = today_low
        state["day_session_date"] = str(today)
        state["day_session_ready"] = True

        # 日盤專用欄位：晚盤複盤必須讀這組，避免被夜盤覆蓋
        state["day_session_close"] = latest_close
        state["day_session_last_k_time"] = latest_k_time

        # 相容舊欄位：盤中 / 其他模組仍可使用
        state["latest_k_time"] = latest_k_time
        state["price"] = latest_close

        state["last_day_session_update"] = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        save_state(state)

        print(
            "✅ 日盤高低點已寫入 state："
            f"high={today_high}, low={today_low}, "
            f"latest_close={latest_close}, latest_k={latest_k_time}"
        )

        return True

    except Exception as e:
        print(f"⚠️ update_day_session_state_from_5min failed: {e}")
        return False
# --------------------------------------------------
# TXF Snapshot Cache → 5 分 K
# --------------------------------------------------

@safe_execute
def get_5min_history_from_futures_snapshot(
    symbol: str = DEFAULT_FUTURES_SYMBOL,
    min_bars: int = 1,
) -> pd.DataFrame | None:
    """
    使用 TXF snapshot cache 產生台指期 5分K。

    不 fallback IX0001。
    沒有 TXF 5分K 就停止交易判斷。

    成功產生 5分K 後，會同步寫入：
    - today_high
    - today_low
    - day_session_high
    - day_session_low
    - day_session_ready
    - latest_k_time
    - price
    """

    get_futures_snapshot_tick(
        data_id=symbol,
        write_cache=True,
    )

    df_tick = txf_tick_cache_to_dataframe()

    if df_tick is None or df_tick.empty:
        print("⚠️ TXF tick cache 無資料，無法建立 5分K")
        return None

    df_tick = df_tick[df_tick["symbol"].astype(str) == str(symbol)].copy()

    if df_tick.empty:
        print(f"⚠️ TXF tick cache 找不到商品：{symbol}")
        return None

    # --------------------------------------------------
    # 只使用目前 cache 中 total_volume 最大的主力合約
    # --------------------------------------------------

    if "futures_id" in df_tick.columns:
        valid_contract_df = df_tick.dropna(subset=["futures_id"]).copy()
    else:
        valid_contract_df = pd.DataFrame()

    if not valid_contract_df.empty:
        contract_volume = (
            valid_contract_df
            .groupby("futures_id")["total_volume"]
            .max()
            .sort_values(ascending=False)
        )

        main_contract = contract_volume.index[0]

        df_tick = valid_contract_df[
            valid_contract_df["futures_id"] == main_contract
        ].copy()

        print(f"✅ 5分K 使用主力合約：{main_contract}")

    else:
        print("⚠️ TXF tick cache 沒有有效 futures_id，停止建立 5分K")
        return None

    # --------------------------------------------------
    # 只取今日資料
    # --------------------------------------------------

    now = datetime.now()
    today = now.date()

    df_tick = df_tick[df_tick["datetime"].dt.date == today].copy()

    if df_tick.empty:
        print("⚠️ 今日 TXF tick cache 無資料，無法建立 5分K")
        return None

    # --------------------------------------------------
    # 只取已收盤 5分K
    # --------------------------------------------------

    closed_cutoff = now.replace(
        minute=(now.minute // 5) * 5,
        second=0,
        microsecond=0,
    )

    df_closed = df_tick[df_tick["datetime"] < closed_cutoff].copy()

    if df_closed.empty:
        print("⚠️ 尚無已收盤 TXF 5分K，等待第一根 5分K 完成")
        return None

    df_closed = df_closed.set_index("datetime").sort_index()

    # --------------------------------------------------
    # Resample 成 5分K
    # --------------------------------------------------

    price_ohlc = df_closed["price"].resample("5min").ohlc()
    volume = df_closed["volume"].resample("5min").sum()

    kbar = price_ohlc.join(volume.rename("volume"))
    kbar = kbar.dropna().reset_index()

    if kbar.empty:
        print("⚠️ TXF 5分K resample 後無資料")
        return None

    kbar = kbar[
        (kbar["open"] > 0)
        & (kbar["high"] > 0)
        & (kbar["low"] > 0)
        & (kbar["close"] > 0)
    ].copy()

    if kbar.empty:
        print("⚠️ TXF 5分K 清理後無有效資料")
        return None

    kbar["volume"] = pd.to_numeric(
        kbar["volume"],
        errors="coerce",
    ).fillna(0)

    kbar["source"] = "FINMIND_TXF_SNAPSHOT_5MIN"
    kbar["note"] = "TXF 台指期 snapshot cache 產生的 5分K，可用於交易判斷"

    result = kbar[
        [
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "source",
            "note",
        ]
    ].copy()

    if len(result) < min_bars:
        print(f"⚠️ TXF 5分K 數量不足：{len(result)} < {min_bars}")
        return None

    # --------------------------------------------------
    # 寫入日盤高低點到 atos_state.json
    # --------------------------------------------------

    update_day_session_state_from_5min(result)

    return result


@safe_execute
def get_5min_history(
    symbol: str = DEFAULT_FUTURES_SYMBOL,
    target_date: str | None = None,
) -> pd.DataFrame | None:
    """
    正式交易判斷只使用 TXF snapshot cache 產生的 5分K。
    不再使用 Fugle IX0001 或 TAIEX 5秒資料。
    """

    df_txf = get_5min_history_from_futures_snapshot(
        symbol=symbol,
        min_bars=1,
    )

    if df_txf is not None and not df_txf.empty:
        print("✅ 5分K source = FinMind TXF snapshot cache")
        return df_txf

    print("⚠️ 無 TXF 5分K，停止交易判斷，不使用 IX0001 代替")
    return None


# --------------------------------------------------
# Mock 5 分 K：測試用
# --------------------------------------------------

@safe_execute
def get_mock_5min_history() -> pd.DataFrame:
    now = datetime.now()
    rows = []
    base = 42050

    for i in range(40):
        dt = now - timedelta(minutes=5 * (40 - i))
        open_price = base + i * 3
        high = open_price + 30
        low = open_price - 25
        close = open_price + 10

        rows.append({
            "datetime": dt,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000 + i * 10,
            "source": "MOCK",
            "note": "測試資料，禁止實盤使用",
        })

    return pd.DataFrame(rows)


# --------------------------------------------------
# 5 分 K 收盤判定
# --------------------------------------------------

def is_five_min_close(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return now.minute % 5 == 0 and now.second >= 2


@safe_execute
def get_latest_close_from_history(df_5min: pd.DataFrame) -> float | None:
    if df_5min is None or df_5min.empty:
        return None

    if "close" not in df_5min.columns:
        return None

    return round(float(df_5min.iloc[-1]["close"]), 1)


# --------------------------------------------------
# Price & Contract Validation
# --------------------------------------------------

def validate_price_data(calculated_close: float, state: dict) -> bool:
    """
    驗證計算出的收盤價與即時快照價格是否合理。
    若誤差超過 300 點，視為數據污染。

    注意：盤中（09:00-13:45）日K close 是昨日收盤，
    即時快照是今日盤中價，兩者本來就可以差數百點，
    因此盤中時段不做驗證，只在盤前/盤後時段驗證。
    """
    snapshot_price = state.get("price")
    if not snapshot_price:
        return True  # 無快照無法驗證，放行
    try:
        # 盤中時段（09:00~13:45）跳過驗證
        now_h = datetime.now().hour
        now_m = datetime.now().minute
        now_minutes = now_h * 60 + now_m
        OPEN_MINUTES  = 9 * 60       # 09:00
        CLOSE_MINUTES = 13 * 60 + 45 # 13:45
        if OPEN_MINUTES <= now_minutes <= CLOSE_MINUTES:
            return True

        diff = abs(float(calculated_close) - float(snapshot_price))
        if diff > 300:
            print(
                f"🚨 數據污染警報：日K收盤 {calculated_close} vs "
                f"即時快照 {snapshot_price}，差距 {diff:.0f} 點，"
                f"停止使用此資料"
            )
            return False
        return True
    except Exception:
        return True


def validate_contract_month(contract_date: str) -> bool:
    """
    驗證合約月份是否為當前近月合約。
    contract_date 格式：YYYYMM（例如 202606）
    """
    try:
        now = datetime.now()
        current_ym = int(now.strftime("%Y%m"))
        contract_ym = int(str(contract_date)[:6])

        diff_months = contract_ym - current_ym

        if diff_months < 0:
            print(f"🚨 合約月份過期：{contract_date}，當前 {current_ym}")
            return False
        elif diff_months > 1:
            print(
                f"⚠️ 合約月份過遠：{contract_date}，當前 {current_ym}，"
                f"差 {diff_months} 個月"
            )
            return False
        return True
    except Exception as e:
        print(f"⚠️ 合約月份驗證失敗：{e}")
        return True  # 驗證失敗時放行，不中斷主流程


# --------------------------------------------------
# Active Contract Selection
# --------------------------------------------------

def get_active_contract(latest_df: "pd.DataFrame") -> str:
    """
    從最新交易日的 DataFrame 中選出正確的主力合約。

    規則：
    - 結算日前 7 天以上：選最大量合約（正常邏輯）
    - 結算日前 7 天以內：鎖定近月合約（最小月份代碼）
      避免換月轉倉期間量能轉移導致系統誤切到次月合約

    Args:
        latest_df: 已過濾至最新交易日的 DataFrame，必須含 contract_date / volume 欄位

    Returns:
        合約代碼字串，例如 '202606'；找不到時回傳空字串
    """
    try:
        from settlement_engine import get_days_to_settlement
        days_to_settlement = get_days_to_settlement()
    except Exception:
        days_to_settlement = 99

    if latest_df.empty:
        return ""

    # 依量排序，取最大量合約
    top_row = latest_df.sort_values("volume", ascending=False).iloc[0]
    top_contract = str(top_row["contract_date"])

    # 結算日前 7 天內 → 鎖近月，防誤切換
    if days_to_settlement <= 7:
        # 只看月合約（6位數字，不含週合約的 W 字元）
        monthly = [
            str(c) for c in latest_df["contract_date"].unique()
            if len(str(c)) == 6 and "W" not in str(c).upper()
        ]
        if monthly:
            near_month = sorted(monthly)[0]
            if near_month != top_contract:
                print(
                    f"⚠️ [data_engine] 結算前 {days_to_settlement} 天，"
                    f"鎖定近月合約 {near_month}（最大量是 {top_contract}）"
                )
            return near_month

    return top_contract


# --------------------------------------------------
# Flip Level
# --------------------------------------------------

@safe_execute
def get_flip_level(
    futures_id: str = "TX",
    lookback_days: int = 7,
    fallback: float = 0,
) -> float:
    try:
        api = get_finmind_api()

        start_date = (
            datetime.now() - timedelta(days=lookback_days)
        ).strftime("%Y-%m-%d")

        df = api.taiwan_futures_daily(
            futures_id=futures_id,
            start_date=start_date,
        )

        if df is None or df.empty:
            print("⚠️ get_flip_level：futures daily 無資料")
            return fallback

        required_cols = ["date", "contract_date", "close", "volume"]

        for col in required_cols:
            if col not in df.columns:
                print(f"⚠️ get_flip_level 缺少欄位 {col}，目前欄位：{df.columns.tolist()}")
                return fallback

        df = df.copy()
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["contract_date"] = df["contract_date"].astype(str)
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

        df = df.dropna(subset=["date", "contract_date", "close", "volume"])
        df = df[~df["contract_date"].str.contains("/", na=False)]
        df = df[(df["close"] > 0) & (df["volume"] > 0)]

        if df.empty:
            print("⚠️ get_flip_level：找不到有效主力合約資料")
            return fallback

        latest_date = df["date"].max()
        latest_df = df[df["date"] == latest_date].copy()

        if latest_df.empty:
            print("⚠️ get_flip_level：找不到最新交易日")
            return fallback

        contract = get_active_contract(latest_df)
        if not contract:
            print("⚠️ get_flip_level：get_active_contract 回傳空值")
            return fallback

        row = latest_df[latest_df["contract_date"] == contract].iloc[0]
        flip = float(row["close"])

        try:
            from settlement_engine import get_days_to_settlement
            days = get_days_to_settlement()
        except Exception:
            days = 99

        print(
            f"✅ prev_close: {round(flip, 1)} "
            f"| date={latest_date.date()} "
            f"| contract={contract}（{days}天後結算）"
            f"| volume={int(row['volume'])}"
        )

        return round(flip, 1)

    except Exception as e:
        print(f"⚠️ get_flip_level failed: {e}")
        return fallback


# --------------------------------------------------
# 法人情緒
# --------------------------------------------------

@safe_execute
def get_institutional_sentiment() -> str:
    """
    讀取外資期貨未平倉淨部位，回傳文字，並同步寫入 atos_state.json。

    寫入欄位：
    - chip_ready
    - chip_date
    - foreign_futures_net
    - institutional_sentiment
    - chip_source
    - last_chip_update
    """

    try:
        api = get_finmind_api()

        start_date = (
            datetime.now() - timedelta(days=10)
        ).strftime("%Y-%m-%d")

        df = api.get_data(
            dataset="TaiwanFuturesInstitutionalInvestors",
            data_id="TX",
            start_date=start_date,
        )

        if df is None or df.empty:
            return "⚪ 法人資料尚未更新"

        if "date" not in df.columns:
            return "⚪ 法人資料缺少 date 欄位"

        latest_date = df["date"].max()
        latest_df = df[df["date"] == latest_date].copy()

        if latest_df.empty:
            return "⚪ 法人資料尚未更新"

        if "institutional_investors" not in latest_df.columns:
            return f"⚪ 法人資料欄位異常：{df.columns.tolist()}"

        foreign = latest_df[
            latest_df["institutional_investors"].astype(str).str.contains(
                "Foreign|外資",
                case=False,
                na=False,
            )
        ]

        if foreign.empty:
            return "⚪ 找不到外資資料"

        row = foreign.iloc[-1]

        long_oi = float(row.get("long_open_interest_balance_volume", 0) or 0)
        short_oi = float(row.get("short_open_interest_balance_volume", 0) or 0)

        net_oi = int(long_oi - short_oi)

        if net_oi > 10000:
            sentiment_text = f"🟢 強多，外資期貨淨多 {net_oi} 口"
        elif net_oi < -10000:
            sentiment_text = f"🔴 強空，外資期貨淨空 {net_oi} 口"
        else:
            sentiment_text = f"🟡 中性，外資期貨淨部位 {net_oi} 口"

        # --------------------------------------------------
        # 寫入 atos_state.json
        # --------------------------------------------------

        state = load_state()

        state["chip_ready"] = True
        state["chip_date"] = str(latest_date)
        state["foreign_futures_net"] = net_oi
        state["institutional_sentiment"] = sentiment_text

        state["chip_source"] = "FinMind TaiwanFuturesInstitutionalInvestors"
        state["chip_data_id"] = "TX"
        state["foreign_long_open_interest"] = int(long_oi)
        state["foreign_short_open_interest"] = int(short_oi)

        state["last_chip_update"] = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        save_state(state)

        print(
            "✅ 法人 / 期貨籌碼已寫入 state："
            f"date={latest_date}, net={net_oi}, text={sentiment_text}"
        )

        return sentiment_text

    except Exception as e:
        return f"⚪ 法人情緒讀取失敗：{e}"



# --------------------------------------------------
# 選擇權 OI
# --------------------------------------------------

def normalize_option_type(value) -> str | None:
    """
    將選擇權買賣權欄位標準化成 CALL / PUT。
    """

    text = str(value).strip().upper()

    if text in ["CALL", "C", "買權"]:
        return "CALL"

    if text in ["PUT", "P", "賣權"]:
        return "PUT"

    if "CALL" in text or text == "買權":
        return "CALL"

    if "PUT" in text or text == "賣權":
        return "PUT"

    return None


def find_first_existing_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """
    從 DataFrame 找第一個存在的欄位名稱。
    """

    if df is None or df.empty:
        return None

    for col in candidates:
        if col in df.columns:
            return col

    return None


@safe_execute
def update_option_oi_state() -> dict | None:
    """
    從 FinMind TaiwanOptionDaily 讀取選擇權 OI，
    找出現價附近有效 Call 壓力 / Put 支撐，並寫入 atos_state.json。

    寫入欄位：
    - option_oi_ready
    - option_oi_date
    - option_oi_summary
    - max_call_strike
    - max_put_strike
    - call_pressure
    - put_support
    """

    try:
        api = get_finmind_api()

        start_date = (
            datetime.now() - timedelta(days=10)
        ).strftime("%Y-%m-%d")

        df = api.get_data(
            dataset="TaiwanOptionDaily",
            data_id="TXO",
            start_date=start_date,
        )

        if df is None or df.empty:
            print("⚠️ TaiwanOptionDaily 無資料")
            return None

        df = df.copy()

        print(f"📋 TaiwanOptionDaily columns: {df.columns.tolist()}")

        date_col = find_first_existing_column(
            df,
            ["date", "日期"],
        )

        strike_col = find_first_existing_column(
            df,
            [
                "strike_price",
                "strike",
                "履約價",
                "delivery_price",
                "exercise_price",
            ],
        )

        option_type_col = find_first_existing_column(
            df,
            [
                "call_put",
                "option_type",
                "put_call",
                "買賣權",
                "買賣權別",
            ],
        )

        oi_col = find_first_existing_column(
            df,
            [
                "open_interest",
                "open_interest_volume",
                "未沖銷契約量",
                "open_interest_balance_volume",
            ],
        )

        contract_col = find_first_existing_column(
            df,
            [
                "contract_date",
                "到期月份",
                "到期月份(週別)",
                "delivery_month",
            ],
        )

        if not date_col:
            print("⚠️ TaiwanOptionDaily 缺少日期欄位")
            return None

        if not strike_col:
            print("⚠️ TaiwanOptionDaily 缺少履約價欄位")
            return None

        if not option_type_col:
            print("⚠️ TaiwanOptionDaily 缺少買賣權欄位")
            return None

        if not oi_col:
            print("⚠️ TaiwanOptionDaily 缺少 OI / 未沖銷契約量欄位")
            return None

        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df[strike_col] = pd.to_numeric(df[strike_col], errors="coerce")
        df[oi_col] = pd.to_numeric(df[oi_col], errors="coerce")

        df["option_type_normalized"] = df[option_type_col].apply(
            normalize_option_type
        )

        df = df.dropna(
            subset=[
                date_col,
                strike_col,
                oi_col,
                "option_type_normalized",
            ]
        )

        df = df[
            (df[strike_col] > 0)
            & (df[oi_col] > 0)
        ].copy()

        if df.empty:
            print("⚠️ TaiwanOptionDaily 清理後無有效 OI 資料")
            return None

        latest_date = df[date_col].max()
        latest_df = df[df[date_col] == latest_date].copy()

        if latest_df.empty:
            print("⚠️ TaiwanOptionDaily 找不到最新日期資料")
            return None

        selected_contract = None

        if contract_col and contract_col in latest_df.columns:
            valid_contracts = (
                latest_df[contract_col]
                .dropna()
                .astype(str)
                .sort_values()
                .unique()
                .tolist()
            )

            if valid_contracts:
                selected_contract = valid_contracts[0]
                latest_df = latest_df[
                    latest_df[contract_col].astype(str) == selected_contract
                ].copy()

        call_df = latest_df[
            latest_df["option_type_normalized"] == "CALL"
        ].copy()

        put_df = latest_df[
            latest_df["option_type_normalized"] == "PUT"
        ].copy()

        if call_df.empty:
            print("⚠️ 找不到 Call OI 資料")
            return None

        if put_df.empty:
            print("⚠️ 找不到 Put OI 資料")
            return None

        state = load_state()

        reference_price = (
            safe_float(state.get("day_session_close"))
            or safe_float(state.get("price"))
        )

        # --------------------------------------------------
        # 全市場最大 OI：保留作為背景參考
        # --------------------------------------------------

        global_call_row = call_df.sort_values(oi_col, ascending=False).iloc[0]
        global_put_row = put_df.sort_values(oi_col, ascending=False).iloc[0]

        global_call_strike = round(float(global_call_row[strike_col]), 1)
        global_put_strike = round(float(global_put_row[strike_col]), 1)
        global_call_oi = int(global_call_row[oi_col])
        global_put_oi = int(global_put_row[oi_col])

        # --------------------------------------------------
        # 現價附近有效 OI：作為實戰壓力 / 支撐
        # --------------------------------------------------

        nearby_call_df = pd.DataFrame()
        nearby_put_df = pd.DataFrame()

        if reference_price and reference_price > 0:
            nearby_call_df = call_df[
                (call_df[strike_col] >= reference_price)
                & (call_df[strike_col] <= reference_price + OPTION_OI_NEARBY_WINDOW_POINTS)
            ].copy()

            nearby_put_df = put_df[
                (put_df[strike_col] <= reference_price)
                & (put_df[strike_col] >= reference_price - OPTION_OI_NEARBY_WINDOW_POINTS)
            ].copy()

        # 若現價附近沒有資料，才 fallback 全市場最大 OI，避免 gate 卡死
        if nearby_call_df.empty:
            nearby_call_df = call_df.copy()
            call_pressure_mode = "GLOBAL_MAX_FALLBACK"
        else:
            call_pressure_mode = "NEARBY_PRICE_RANGE"

        if nearby_put_df.empty:
            nearby_put_df = put_df.copy()
            put_support_mode = "GLOBAL_MAX_FALLBACK"
        else:
            put_support_mode = "NEARBY_PRICE_RANGE"

        call_row = nearby_call_df.sort_values(oi_col, ascending=False).iloc[0]
        put_row = nearby_put_df.sort_values(oi_col, ascending=False).iloc[0]

        max_call_strike = round(float(call_row[strike_col]), 1)
        max_put_strike = round(float(put_row[strike_col]), 1)

        max_call_oi = int(call_row[oi_col])
        max_put_oi = int(put_row[oi_col])

        option_oi_summary = {
            # 實戰用：現價附近有效壓力 / 支撐
            "call_pressure": max_call_strike,
            "put_support": max_put_strike,
            "max_call_strike": max_call_strike,
            "max_put_strike": max_put_strike,
            "max_call_oi": max_call_oi,
            "max_put_oi": max_put_oi,

            # 計算背景
            "reference_price": reference_price,
            "nearby_window_points": OPTION_OI_NEARBY_WINDOW_POINTS,
            "call_pressure_mode": call_pressure_mode,
            "put_support_mode": put_support_mode,

            # 全市場最大 OI：只作背景，不直接當作今晚壓力支撐
            "global_max_call_strike": global_call_strike,
            "global_max_put_strike": global_put_strike,
            "global_max_call_oi": global_call_oi,
            "global_max_put_oi": global_put_oi,

            "contract_date": selected_contract,
            "source": "FinMind TaiwanOptionDaily",
        }

        state["option_oi_ready"] = True
        state["option_oi_date"] = str(latest_date.date())
        state["option_oi_summary"] = option_oi_summary

        state["call_pressure"] = max_call_strike
        state["put_support"] = max_put_strike
        state["max_call_strike"] = max_call_strike
        state["max_put_strike"] = max_put_strike
        state["max_call_oi"] = max_call_oi
        state["max_put_oi"] = max_put_oi

        state["last_option_oi_update"] = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        save_state(state)

        print(
            "✅ 選擇權 OI 已寫入 state："
            f"date={latest_date.date()}, "
            f"contract={selected_contract}, "
            f"ref={reference_price}, "
            f"Call壓力={max_call_strike}({max_call_oi})[{call_pressure_mode}], "
            f"Put支撐={max_put_strike}({max_put_oi})[{put_support_mode}], "
            f"全市場Call={global_call_strike}({global_call_oi}), "
            f"全市場Put={global_put_strike}({global_put_oi})"
        )

        return option_oi_summary

    except Exception as e:
        print(f"⚠️ update_option_oi_state failed: {e}")
        return None


# --------------------------------------------------
# 動態支撐壓力
# --------------------------------------------------

@safe_execute
def get_dynamic_resistance_support(
    futures_id: str = "TX",
) -> dict:
    try:
        api = get_finmind_api()

        start_date = (
            datetime.now() - timedelta(days=14)
        ).strftime("%Y-%m-%d")

        df = api.taiwan_futures_daily(
            futures_id=futures_id,
            start_date=start_date,
        )

        if df is None or df.empty:
            print("⚠️ get_dynamic_resistance_support：futures daily 無資料")
            return {"R1": 0, "S1": 0, "pivot": None, "source_date": "N/A"}

        required_cols = ["date", "contract_date", "max", "min", "close", "volume"]

        for col in required_cols:
            if col not in df.columns:
                print(
                    f"⚠️ get_dynamic_resistance_support 缺少欄位 {col}，"
                    f"目前欄位：{df.columns.tolist()}"
                )
                return {"R1": 0, "S1": 0, "pivot": None, "source_date": "N/A"}

        df = df.copy()
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["contract_date"] = df["contract_date"].astype(str)

        df["max"] = pd.to_numeric(df["max"], errors="coerce")
        df["min"] = pd.to_numeric(df["min"], errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

        df = df.dropna(subset=["date", "contract_date", "max", "min", "close", "volume"])
        df = df[~df["contract_date"].str.contains("/", na=False)]

        df = df[
            (df["max"] > 0)
            & (df["min"] > 0)
            & (df["close"] > 0)
            & (df["volume"] > 0)
        ]

        if df.empty:
            print("⚠️ get_dynamic_resistance_support：找不到有效主力合約資料")
            return {"R1": 0, "S1": 0, "pivot": None, "source_date": "N/A"}

        latest_date = df["date"].max()
        latest_df = df[df["date"] == latest_date].copy()

        if latest_df.empty:
            print("⚠️ get_dynamic_resistance_support：找不到最新交易日")
            return {"R1": 0, "S1": 0, "pivot": None, "source_date": "N/A"}

        contract_date = get_active_contract(latest_df)
        if not contract_date:
            print("⚠️ get_dynamic_resistance_support：get_active_contract 回傳空值")
            return {"R1": 0, "S1": 0, "pivot": None, "source_date": "N/A"}

        # 合約月份驗證（當月或下月才合法）
        if not validate_contract_month(contract_date):
            print(f"🚨 get_dynamic_resistance_support：合約月份異常 {contract_date}，回傳 None")
            return None

        row = latest_df[latest_df["contract_date"] == contract_date].iloc[0]

        high   = float(row["max"])
        low    = float(row["min"])
        close  = float(row["close"])
        volume = int(row["volume"])

        pivot = (high + low + close) / 3
        r1    = 2 * pivot - low
        s1    = 2 * pivot - high

        try:
            from settlement_engine import get_days_to_settlement
            days_to_settlement = get_days_to_settlement()
        except Exception:
            days_to_settlement = 99

        # 即時價格交叉驗證（與 atos_state 快照比對）
        try:
            from persistent_state import load_state as _load_state
            _state = _load_state()
        except Exception:
            _state = {}
        if not validate_price_data(close, _state):
            print(f"🚨 get_dynamic_resistance_support：價格污染 close={close}，回傳 None")
            return None

        result = {
            "R1":                round(r1, 1),
            "S1":                round(s1, 1),
            "pivot":             round(pivot, 1),
            "source_date":       str(latest_date.date()),
            "contract_date":     contract_date,
            "days_to_settlement": days_to_settlement,
            "high":              round(high, 1),
            "low":               round(low, 1),
            "close":             round(close, 1),
            "volume":            volume,
        }

        print("✅ R/S calculated:", result)

        return result

    except Exception as e:
        print(f"⚠️ get_dynamic_resistance_support failed: {e}")
        return {"R1": 0, "S1": 0, "pivot": None, "source_date": "N/A"}


# --------------------------------------------------
# Manual Test
# --------------------------------------------------

if __name__ == "__main__":
    print("=== Test realtime tick ===")
    tick = get_realtime_tick()
    print(tick)

    print("\n=== Test futures snapshot directly ===")
    snapshot = get_futures_snapshot_tick(
        data_id=DEFAULT_FUTURES_SYMBOL,
        write_cache=True,
    )
    print(snapshot)

    print("\n=== Test TXF tick cache ===")
    cache_df = txf_tick_cache_to_dataframe()

    if cache_df is not None:
        print(cache_df.tail())
        print(cache_df.columns)
    else:
        print("No TXF tick cache")

    print("\n=== Test TXF 5min history ===")
    df = get_5min_history()

    if df is not None:
        print(df.tail())
        print(df.columns)
    else:
        print("No TXF 5min data")

    print("\n=== Test Flip ===")
    print(get_flip_level())

    print("\n=== Test R/S ===")
    print(get_dynamic_resistance_support())

    print("\n=== Test Institutional Sentiment ===")
    print(get_institutional_sentiment())

    print("\n=== Test Option OI ===")
    print(update_option_oi_state())