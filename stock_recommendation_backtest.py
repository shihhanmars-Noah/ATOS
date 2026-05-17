# stock_recommendation_backtest.py

import os
import json
from pathlib import Path
from datetime import timedelta

import pandas as pd
from dotenv import load_dotenv
from FinMind.data import DataLoader

from error_handler import safe_execute

load_dotenv()


# --------------------------------------------------
# Config
# --------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent

# 你的個股推薦快取檔
STOCK_RECOMMENDATION_FILE = PROJECT_DIR / "stock_picks_cache.pkl"

BACKTEST_OUTPUT_FILE = PROJECT_DIR / "stock_recommendation_backtest_result.csv"
SUMMARY_OUTPUT_FILE = PROJECT_DIR / "stock_recommendation_backtest_summary.csv"

START_DATE = "2026-01-01"
END_DATE = "2026-05-15"

MAX_HOLDING_DAYS = 5

STOP_LOSS_PCT = -3.0
TAKE_PROFIT_PCT = 6.0

HIT_1D_THRESHOLD = 2.0
HIT_3D_THRESHOLD = 3.0
HIT_5D_THRESHOLD = 5.0

# 預設只回測 A / B，C 是剔除名單，不納入推薦勝率
BACKTEST_GRADES = ["A", "B"]


# --------------------------------------------------
# FinMind
# --------------------------------------------------

def get_finmind_token() -> str:
    token = os.getenv("FIN_TOKEN") or os.getenv("FINMIND_TOKEN")

    if not token:
        raise ValueError("FINMIND_TOKEN missing")

    return token


def get_finmind_api() -> DataLoader:
    api = DataLoader()
    api.login_by_token(get_finmind_token())
    return api


# --------------------------------------------------
# Helpers
# --------------------------------------------------

def normalize_stock_id(stock_id) -> str:
    """
    將股票代號標準化。
    """

    if stock_id is None:
        return ""

    stock_id = str(stock_id).strip()

    stock_id = stock_id.replace(".TW", "").replace(".TWO", "")

    return stock_id


def safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def pct_change(from_price, to_price) -> float:
    try:
        return round((float(to_price) - float(from_price)) / float(from_price) * 100, 2)
    except Exception:
        return 0.0


def calculate_max_losing_streak(df: pd.DataFrame) -> int:
    max_streak = 0
    current_streak = 0

    for pnl in df["trade_return_pct"]:
        if pnl < 0:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    return max_streak


# --------------------------------------------------
# Load Recommendations
# --------------------------------------------------

def extract_recommendations_from_json_obj(obj) -> list:
    """
    相容不同格式的 stock_picks_cache。

    支援目前 ATOS stock_picks_cache.pkl：

    {
        "timestamp": datetime,
        "data": {
            "A": [...],
            "B": [...],
            "C": [...],
            "market": {...},
            "generated_at": "...",
            "is_cache": False
        }
    }

    預設只回測 A / B，不回測 C。
    """

    records = []

    if obj is None:
        return records

    # --------------------------------------------------
    # ATOS stock_picks_cache.pkl 格式
    # --------------------------------------------------
    if isinstance(obj, dict) and "data" in obj and isinstance(obj["data"], dict):
        data = obj["data"]

        generated_at = data.get("generated_at")
        cache_timestamp = obj.get("timestamp")

        for grade_key in BACKTEST_GRADES:
            picks = data.get(grade_key, [])

            if not isinstance(picks, list):
                continue

            for item in picks:
                if not isinstance(item, dict):
                    continue

                row = dict(item)

                # 股票代號欄位標準化：目前 cache 使用 id
                if "stock_id" not in row and "id" in row:
                    row["stock_id"] = row.get("id")

                # 日期欄位標準化
                if "date" not in row:
                    row["date"] = (
                        row.get("recommend_date")
                        or row.get("report_date")
                        or generated_at
                        or cache_timestamp
                    )

                # 等級欄位補上
                if "grade" not in row:
                    row["grade"] = grade_key

                # 股票名稱若沒有就留空
                if "stock_name" not in row:
                    row["stock_name"] = row.get("name", "")

                # 將一些常用分析欄位攤平成回測結果可追蹤欄位
                scores = row.get("scores", {})
                tech = row.get("tech", {})
                chip = row.get("chip", {})
                market = row.get("market", {})
                plan = row.get("plan", {})

                if isinstance(scores, dict):
                    row["total_score"] = scores.get("total_score")
                    row["chip_score"] = scores.get("chip_score")
                    row["trend_score"] = scores.get("trend_score")
                    row["volume_score"] = scores.get("volume_score")
                    row["market_score"] = scores.get("market_score")

                if isinstance(tech, dict):
                    row["signal_close"] = tech.get("close")
                    row["signal_ma5"] = tech.get("ma5")
                    row["signal_ma10"] = tech.get("ma10")
                    row["signal_ma20"] = tech.get("ma20")
                    row["signal_volume_ratio"] = tech.get("volume_ratio")
                    row["signal_distance_to_ma5"] = tech.get("distance_to_ma5")
                    row["signal_distance_to_ma10"] = tech.get("distance_to_ma10")

                if isinstance(chip, dict):
                    row["trust_net_buy"] = chip.get("trust_net_buy")
                    row["foreign_net_buy"] = chip.get("foreign_net_buy")
                    row["consecutive_trust_buy_days"] = chip.get("consecutive_trust_buy_days")

                if isinstance(market, dict):
                    row["market_mode"] = market.get("mode")
                    row["market_label"] = market.get("label")

                if isinstance(plan, dict):
                    row["observation_zone"] = plan.get("observation_zone")
                    row["invalid"] = plan.get("invalid")
                    row["command"] = plan.get("command")

                records.append(row)

        return records

    # --------------------------------------------------
    # 格式 1：list of dict
    # --------------------------------------------------
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                records.append(item)
        return records

    if not isinstance(obj, dict):
        return records

    # --------------------------------------------------
    # 格式 2：recommendations / picks / stocks
    # --------------------------------------------------
    for key in ["recommendations", "picks", "stocks", "selected_stocks"]:
        if key in obj and isinstance(obj[key], list):
            base_date = (
                obj.get("date")
                or obj.get("recommend_date")
                or obj.get("created_at")
                or obj.get("generated_at")
            )

            for item in obj[key]:
                if isinstance(item, dict):
                    row = dict(item)

                    if base_date and "date" not in row:
                        row["date"] = base_date

                    records.append(row)

            return records

    # --------------------------------------------------
    # 格式 3：日期作 key
    # --------------------------------------------------
    for key, value in obj.items():
        try:
            date_key = pd.to_datetime(key).strftime("%Y-%m-%d")
        except Exception:
            continue

        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    row = dict(item)
                    row["date"] = date_key
                    records.append(row)

        elif isinstance(value, dict):
            row = dict(value)
            row["date"] = date_key
            records.append(row)

    return records


@safe_execute
def load_recommendations(file_path: Path = STOCK_RECOMMENDATION_FILE) -> pd.DataFrame | None:
    """
    讀取個股推薦清單。
    """

    if not file_path.exists():
        print(f"⚠️ 找不到推薦檔案：{file_path}")
        print("請確認 stock_picks_cache.pkl 是否存在，或修改 STOCK_RECOMMENDATION_FILE")
        return None

    suffix = file_path.suffix.lower()
    records = []

    if suffix == ".json":
        with open(file_path, "r", encoding="utf-8") as f:
            obj = json.load(f)

        records = extract_recommendations_from_json_obj(obj)

    elif suffix in [".pkl", ".pickle"]:
        obj = pd.read_pickle(file_path)

        if isinstance(obj, pd.DataFrame):
            records = obj.to_dict("records")
        else:
            records = extract_recommendations_from_json_obj(obj)

    elif suffix in [".csv", ".txt"]:
        df = pd.read_csv(file_path)
        records = df.to_dict("records")

    elif suffix in [".xlsx", ".xls"]:
        df = pd.read_excel(file_path)
        records = df.to_dict("records")

    else:
        print(f"⚠️ 不支援的推薦檔案格式：{suffix}")
        return None

    if not records:
        print("⚠️ 推薦清單為空")
        return None

    df = pd.DataFrame(records)

    # 欄位相容：加入 id
    stock_id_col = None
    for col in ["stock_id", "stock_no", "ticker", "code", "symbol", "id", "股票代號", "代號"]:
        if col in df.columns:
            stock_id_col = col
            break

    if stock_id_col is None:
        print(f"⚠️ 找不到股票代號欄位，目前欄位：{list(df.columns)}")
        return None

    date_col = None
    for col in ["date", "recommend_date", "created_at", "report_date", "推薦日期", "日期"]:
        if col in df.columns:
            date_col = col
            break

    if date_col is None:
        print(f"⚠️ 找不到推薦日期欄位，目前欄位：{list(df.columns)}")
        return None

    name_col = None
    for col in ["stock_name", "name", "股票名稱", "名稱"]:
        if col in df.columns:
            name_col = col
            break

    df["recommend_date"] = pd.to_datetime(df[date_col], errors="coerce")
    df["stock_id"] = df[stock_id_col].apply(normalize_stock_id)

    if name_col:
        df["stock_name"] = df[name_col].astype(str)
    else:
        df["stock_name"] = ""

    if "grade" not in df.columns:
        df["grade"] = ""

    df = df.dropna(subset=["recommend_date"])
    df = df[df["stock_id"] != ""]

    if df.empty:
        print("⚠️ 推薦清單整理後為空")
        return None

    df["recommend_date"] = df["recommend_date"].dt.strftime("%Y-%m-%d")

    # 去重：同一天同一檔只保留一筆
    df = df.drop_duplicates(subset=["recommend_date", "stock_id"])

    # 限制期間
    df = df[
        (df["recommend_date"] >= START_DATE)
        & (df["recommend_date"] <= END_DATE)
    ].copy()

    if df.empty:
        print("⚠️ 指定期間內沒有推薦資料")
        return None

    print(f"✅ 推薦清單 loaded：{len(df)} 筆")
    show_cols = ["recommend_date", "stock_id", "stock_name", "grade"]
    show_cols = [c for c in show_cols if c in df.columns]
    print(df[show_cols].head(30).to_string(index=False))

    # 保留可用欄位，方便輸出分析
    keep_cols = [
        "recommend_date",
        "stock_id",
        "stock_name",
        "grade",
        "grade_reason",
        "total_score",
        "chip_score",
        "trend_score",
        "volume_score",
        "market_score",
        "signal_close",
        "signal_ma5",
        "signal_ma10",
        "signal_ma20",
        "signal_volume_ratio",
        "signal_distance_to_ma5",
        "signal_distance_to_ma10",
        "trust_net_buy",
        "foreign_net_buy",
        "consecutive_trust_buy_days",
        "market_mode",
        "market_label",
        "observation_zone",
        "invalid",
        "command",
    ]

    keep_cols = [c for c in keep_cols if c in df.columns]

    return df[keep_cols].copy()


# --------------------------------------------------
# Price Loading
# --------------------------------------------------

@safe_execute
def load_stock_price(stock_id: str, start_date: str, end_date: str) -> pd.DataFrame | None:
    """
    讀取台股日 K。
    """

    api = get_finmind_api()

    df = api.taiwan_stock_daily(
        stock_id=stock_id,
        start_date=start_date,
        end_date=end_date,
    )

    if df is None or df.empty:
        return None

    required_cols = ["date", "stock_id", "open", "max", "min", "close"]

    for col in required_cols:
        if col not in df.columns:
            print(f"⚠️ {stock_id} 缺少欄位：{col}")
            return None

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")

    for col in ["open", "max", "min", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["date", "open", "max", "min", "close"])

    df = df[
        (df["open"] > 0)
        & (df["max"] > 0)
        & (df["min"] > 0)
        & (df["close"] > 0)
    ].copy()

    if df.empty:
        return None

    df = df.sort_values("date").reset_index(drop=True)

    return df


# --------------------------------------------------
# Backtest Logic
# --------------------------------------------------

def simulate_trade(
    recommendation_row: pd.Series,
    price_df: pd.DataFrame,
) -> dict | None:
    """
    推薦隔一交易日開盤進場。

    停損 / 停利邏輯：
    - 若同一天同時碰停損與停利，保守認定先停損。
    """

    stock_id = recommendation_row["stock_id"]
    stock_name = recommendation_row.get("stock_name", "")
    recommend_date = recommendation_row["recommend_date"]

    rec_dt = pd.to_datetime(recommend_date)

    future_df = price_df[price_df["date"] > rec_dt].copy()

    if future_df.empty:
        return None

    # 隔一交易日開盤進場
    entry_bar = future_df.iloc[0]
    entry_date = entry_bar["date"]
    entry_price = float(entry_bar["open"])

    holding_df = future_df.head(MAX_HOLDING_DAYS).copy()

    if holding_df.empty:
        return None

    stop_price = entry_price * (1 + STOP_LOSS_PCT / 100)
    target_price = entry_price * (1 + TAKE_PROFIT_PCT / 100)

    exit_date = None
    exit_price = None
    exit_reason = None
    holding_days = 0

    max_high = float(holding_df["max"].max())
    min_low = float(holding_df["min"].min())

    max_gain_pct = pct_change(entry_price, max_high)
    max_drawdown_pct = pct_change(entry_price, min_low)

    for _, bar in holding_df.iterrows():
        holding_days += 1

        high = float(bar["max"])
        low = float(bar["min"])

        # 保守：同一天同時碰停損與停利，先算停損
        if low <= stop_price:
            exit_date = bar["date"]
            exit_price = stop_price
            exit_reason = "STOP_LOSS"
            break

        if high >= target_price:
            exit_date = bar["date"]
            exit_price = target_price
            exit_reason = "TAKE_PROFIT"
            break

    if exit_date is None:
        last_bar = holding_df.iloc[-1]
        exit_date = last_bar["date"]
        exit_price = float(last_bar["close"])
        exit_reason = "TIME_EXIT"
        holding_days = len(holding_df)

    trade_return_pct = pct_change(entry_price, exit_price)

    # 命中率統計
    d1_df = holding_df.head(1)
    d3_df = holding_df.head(3)
    d5_df = holding_df.head(5)

    max_gain_1d = pct_change(entry_price, d1_df["max"].max()) if not d1_df.empty else 0
    max_gain_3d = pct_change(entry_price, d3_df["max"].max()) if not d3_df.empty else 0
    max_gain_5d = pct_change(entry_price, d5_df["max"].max()) if not d5_df.empty else 0

    hit_1d = max_gain_1d >= HIT_1D_THRESHOLD
    hit_3d = max_gain_3d >= HIT_3D_THRESHOLD
    hit_5d = max_gain_5d >= HIT_5D_THRESHOLD

    result = {
        "recommend_date": recommend_date,
        "stock_id": stock_id,
        "stock_name": stock_name,
        "grade": recommendation_row.get("grade", ""),
        "grade_reason": recommendation_row.get("grade_reason", ""),

        "entry_date": entry_date.strftime("%Y-%m-%d"),
        "entry_price": round(entry_price, 2),

        "exit_date": exit_date.strftime("%Y-%m-%d"),
        "exit_price": round(exit_price, 2),
        "exit_reason": exit_reason,
        "holding_days": holding_days,

        "trade_return_pct": round(trade_return_pct, 2),

        "max_gain_pct": round(max_gain_pct, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),

        "max_gain_1d_pct": round(max_gain_1d, 2),
        "max_gain_3d_pct": round(max_gain_3d, 2),
        "max_gain_5d_pct": round(max_gain_5d, 2),

        "hit_1d_2pct": hit_1d,
        "hit_3d_3pct": hit_3d,
        "hit_5d_5pct": hit_5d,

        "stop_loss_pct": STOP_LOSS_PCT,
        "take_profit_pct": TAKE_PROFIT_PCT,
        "max_holding_days": MAX_HOLDING_DAYS,
    }

    # 把推薦當下的分析欄位一起寫入結果
    extra_cols = [
        "total_score",
        "chip_score",
        "trend_score",
        "volume_score",
        "market_score",
        "signal_close",
        "signal_ma5",
        "signal_ma10",
        "signal_ma20",
        "signal_volume_ratio",
        "signal_distance_to_ma5",
        "signal_distance_to_ma10",
        "trust_net_buy",
        "foreign_net_buy",
        "consecutive_trust_buy_days",
        "market_mode",
        "market_label",
        "observation_zone",
        "invalid",
        "command",
    ]

    for col in extra_cols:
        if col in recommendation_row.index:
            result[col] = recommendation_row.get(col)

    return result


@safe_execute
def run_stock_recommendation_backtest() -> pd.DataFrame | None:
    recommendations = load_recommendations()

    if recommendations is None or recommendations.empty:
        print("⚠️ 無推薦清單，停止回測")
        return None

    # 為了讓推薦日後 5 日有資料，價格 end_date 往後多抓 20 天
    price_start = (
        pd.to_datetime(START_DATE) - timedelta(days=10)
    ).strftime("%Y-%m-%d")

    price_end = (
        pd.to_datetime(END_DATE) + timedelta(days=20)
    ).strftime("%Y-%m-%d")

    trades = []

    stock_ids = sorted(recommendations["stock_id"].unique().tolist())

    price_cache = {}

    print(f"📊 回測股票數：{len(stock_ids)}")

    for idx, stock_id in enumerate(stock_ids, start=1):
        print(f"📥 loading stock price {idx}/{len(stock_ids)}: {stock_id}")

        price_df = load_stock_price(
            stock_id=stock_id,
            start_date=price_start,
            end_date=price_end,
        )

        if price_df is None or price_df.empty:
            print(f"⚠️ {stock_id} 無價格資料，略過")
            continue

        price_cache[stock_id] = price_df

    for _, row in recommendations.iterrows():
        stock_id = row["stock_id"]
        price_df = price_cache.get(stock_id)

        if price_df is None or price_df.empty:
            continue

        trade = simulate_trade(
            recommendation_row=row,
            price_df=price_df,
        )

        if trade:
            trades.append(trade)

    if not trades:
        print("⚠️ 沒有產生任何回測交易")
        return None

    result = pd.DataFrame(trades)

    result.to_csv(
        BACKTEST_OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print(f"✅ 個股推薦回測完成：{len(result)} 筆")
    print(f"✅ 明細輸出：{BACKTEST_OUTPUT_FILE}")

    return result


# --------------------------------------------------
# Summary
# --------------------------------------------------

def summarize_result(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {}

    total = len(df)

    wins = df[df["trade_return_pct"] > 0]
    losses = df[df["trade_return_pct"] < 0]

    win_count = len(wins)
    loss_count = len(losses)

    win_rate = win_count / total * 100 if total else 0

    avg_win = wins["trade_return_pct"].mean() if not wins.empty else 0
    avg_loss = losses["trade_return_pct"].mean() if not losses.empty else 0
    avg_return = df["trade_return_pct"].mean()
    total_return = df["trade_return_pct"].sum()

    hit_1d = df["hit_1d_2pct"].mean() * 100
    hit_3d = df["hit_3d_3pct"].mean() * 100
    hit_5d = df["hit_5d_5pct"].mean() * 100

    avg_max_gain = df["max_gain_pct"].mean()
    avg_max_drawdown = df["max_drawdown_pct"].mean()

    max_losing_streak = calculate_max_losing_streak(df)

    exit_counts = df["exit_reason"].value_counts().to_dict()

    gross_profit = wins["trade_return_pct"].sum() if not wins.empty else 0
    gross_loss = abs(losses["trade_return_pct"].sum()) if not losses.empty else 0

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = 999 if gross_profit > 0 else 0

    summary = {
        "total_trades": total,
        "wins": win_count,
        "losses": loss_count,
        "win_rate_pct": round(win_rate, 2),

        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "avg_return_pct": round(avg_return, 2),
        "total_return_pct_sum": round(total_return, 2),

        "hit_1d_2pct_rate": round(hit_1d, 2),
        "hit_3d_3pct_rate": round(hit_3d, 2),
        "hit_5d_5pct_rate": round(hit_5d, 2),

        "avg_max_gain_pct": round(avg_max_gain, 2),
        "avg_max_drawdown_pct": round(avg_max_drawdown, 2),

        "max_losing_streak": max_losing_streak,
        "profit_factor": round(profit_factor, 3),

        "take_profit_count": int(exit_counts.get("TAKE_PROFIT", 0)),
        "stop_loss_count": int(exit_counts.get("STOP_LOSS", 0)),
        "time_exit_count": int(exit_counts.get("TIME_EXIT", 0)),

        "stop_loss_pct": STOP_LOSS_PCT,
        "take_profit_pct": TAKE_PROFIT_PCT,
        "max_holding_days": MAX_HOLDING_DAYS,
    }

    return summary


def print_summary(df: pd.DataFrame):
    summary = summarize_result(df)

    if not summary:
        print("⚠️ 無統計結果")
        return

    summary_df = pd.DataFrame([summary])
    summary_df.to_csv(
        SUMMARY_OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    print("\n==============================")
    print("📊 個股推薦報告回測統計")
    print("==============================")
    print(f"總推薦交易數：{summary['total_trades']}")
    print(f"勝利次數：{summary['wins']}")
    print(f"失敗次數：{summary['losses']}")
    print(f"交易勝率：{summary['win_rate_pct']}%")
    print(f"平均獲利：{summary['avg_win_pct']}%")
    print(f"平均虧損：{summary['avg_loss_pct']}%")
    print(f"平均每筆報酬：{summary['avg_return_pct']}%")
    print(f"Profit Factor：{summary['profit_factor']}")
    print(f"最大連敗：{summary['max_losing_streak']} 次")
    print("------------------------------")
    print(f"1日內最高 +2% 命中率：{summary['hit_1d_2pct_rate']}%")
    print(f"3日內最高 +3% 命中率：{summary['hit_3d_3pct_rate']}%")
    print(f"5日內最高 +5% 命中率：{summary['hit_5d_5pct_rate']}%")
    print("------------------------------")
    print(f"平均最大漲幅：{summary['avg_max_gain_pct']}%")
    print(f"平均最大回撤：{summary['avg_max_drawdown_pct']}%")
    print("------------------------------")
    print(f"停利次數：{summary['take_profit_count']}")
    print(f"停損次數：{summary['stop_loss_count']}")
    print(f"時間出場次數：{summary['time_exit_count']}")
    print("==============================\n")

    print(f"✅ 統計輸出：{SUMMARY_OUTPUT_FILE}")

    print("📌 依等級分類：")
    if "grade" in df.columns:
        by_grade = (
            df.groupby("grade")["trade_return_pct"]
            .agg(["count", "mean", "sum"])
            .reset_index()
        )
        print(by_grade.to_string(index=False))

    print("\n📌 依出場原因分類：")
    by_exit = (
        df.groupby("exit_reason")["trade_return_pct"]
        .agg(["count", "mean", "sum"])
        .reset_index()
    )
    print(by_exit.to_string(index=False))

    print("\n📌 表現前 10 名：")
    top10 = df.sort_values("trade_return_pct", ascending=False).head(10)
    print(
        top10[
            [
                "recommend_date",
                "stock_id",
                "stock_name",
                "grade",
                "entry_date",
                "entry_price",
                "exit_date",
                "exit_reason",
                "trade_return_pct",
                "max_gain_pct",
                "max_drawdown_pct",
            ]
        ].to_string(index=False)
    )

    print("\n📌 表現後 10 名：")
    bottom10 = df.sort_values("trade_return_pct", ascending=True).head(10)
    print(
        bottom10[
            [
                "recommend_date",
                "stock_id",
                "stock_name",
                "grade",
                "entry_date",
                "entry_price",
                "exit_date",
                "exit_reason",
                "trade_return_pct",
                "max_gain_pct",
                "max_drawdown_pct",
            ]
        ].to_string(index=False)
    )


# --------------------------------------------------
# Manual Run
# --------------------------------------------------

if __name__ == "__main__":
    print("🧪 ATOS Stock Recommendation Backtest Started")
    print(f"推薦檔案：{STOCK_RECOMMENDATION_FILE}")
    print(f"回測期間：{START_DATE} ~ {END_DATE}")
    print(f"回測等級：{BACKTEST_GRADES}")
    print("進場：推薦日隔一交易日開盤")
    print(f"停損：{STOP_LOSS_PCT}%")
    print(f"停利：{TAKE_PROFIT_PCT}%")
    print(f"最多持有：{MAX_HOLDING_DAYS} 個交易日")

    df_result = run_stock_recommendation_backtest()

    if df_result is not None and not df_result.empty:
        print_summary(df_result)