# stock_report_engine.py

import os
import pickle
from datetime import datetime, timedelta

import pandas as pd

try:
    from stock_report_policy_engine import (
        apply_stock_report_policy_to_picks,
        build_stock_policy_report_section,
    )
except Exception:
    apply_stock_report_policy_to_picks = None
    build_stock_policy_report_section = None

from data_engine import get_finmind_api
from error_handler import safe_execute
from messenger import send_to_telegram
from persistent_state import load_state


CACHE_FILE = "stock_picks_cache.pkl"


# --------------------------------------------------
# Cache
# --------------------------------------------------

def save_to_cache(data):
    """
    將個股篩選資料存入 Pickle 檔案。
    """

    try:
        cache_data = {
            "timestamp": datetime.now(),
            "data": data,
        }

        with open(CACHE_FILE, "wb") as f:
            pickle.dump(cache_data, f)

        print("💾 [Cache] 個股篩選資料已快取。")

    except Exception as e:
        print(f"⚠️ [Cache] 儲存快取失敗: {e}")


def load_from_cache():
    """
    從 Pickle 檔案載入快取資料。
    """

    if not os.path.exists(CACHE_FILE):
        return None

    try:
        with open(CACHE_FILE, "rb") as f:
            cache_data = pickle.load(f)

        cache_age = datetime.now() - cache_data["timestamp"]
        data = cache_data["data"]

        for group_name in ["A", "B", "C"]:
            for item in data.get(group_name, []):
                item["is_cache"] = True
                item["cache_age_hours"] = round(cache_age.total_seconds() / 3600, 1)

        if cache_age > timedelta(hours=24):
            print("⚠️ [Cache] 個股快取資料已超過 24 小時。")

        return data

    except Exception as e:
        print(f"⚠️ [Cache] 讀取快取失敗: {e}")
        return None


# --------------------------------------------------
# Market Bias
# --------------------------------------------------

def get_market_bias_from_state():
    """
    從 ATOS state 判斷大盤環境。

    回傳模式：
    - BULL       現價站穩 mid_range +100 點以上
    - BEAR       現價跌破 mid_range -100 點以下
    - BEAR_CHIP  台指期情緒評分 ≤ -4（無論價位，強制降級）
    - NEUTRAL    其他
    - UNKNOWN    資料不足
    """

    try:
        state = load_state()

        price = state.get("price")
        mid_range = state.get("mid_range") or state.get("flip")
        allow_trade = state.get("allow_trade", True)
        sentiment_score = state.get("sentiment_score")

        if not allow_trade:
            return {
                "mode": "NEUTRAL",
                "label": "🟡 大盤不可交易 / 觀察",
                "score": 40,
            }

        # 台指期籌碼極端偏空 → 強制 BEAR_CHIP（優先於價格判斷）
        if sentiment_score is not None:
            try:
                s = int(sentiment_score)
                if s <= -4:
                    return {
                        "mode": "BEAR_CHIP",
                        "label": f"🔴 台指期籌碼偏空（評分 {s}）",
                        "score": 20,
                        "sentiment_score": s,
                    }
            except Exception:
                pass

        if price is None or mid_range is None or not mid_range:
            return {
                "mode": "UNKNOWN",
                "label": "⚪ 大盤資料不足",
                "score": 50,
            }

        price = float(price)
        mid_range = float(mid_range)

        if price > mid_range + 100:
            return {
                "mode": "BULL",
                "label": "🟢 大盤偏多",
                "score": 85,
            }

        if price < mid_range - 100:
            return {
                "mode": "BEAR",
                "label": "🔴 大盤偏空",
                "score": 25,
            }

        return {
            "mode": "NEUTRAL",
            "label": "🟡 大盤中性震盪",
            "score": 55,
        }

    except Exception as e:
        print(f"⚠️ get_market_bias_from_state failed: {e}")

        return {
            "mode": "UNKNOWN",
            "label": "⚪ 大盤狀態未知",
            "score": 50,
        }


# --------------------------------------------------
# Data Fetching
# --------------------------------------------------

def fetch_institutional_data(api, lookback_days: int = 10):
    """
    抓全市場三大法人買賣資料。
    """

    start_date = (
        datetime.now() - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")

    df = api.get_data(
        dataset="TaiwanStockInstitutionalInvestorsBuySell",
        start_date=start_date,
    )

    if df is None or df.empty:
        raise ValueError("法人資料為空")

    required_cols = ["date", "stock_id", "name", "buy", "sell"]

    for col in required_cols:
        if col not in df.columns:
            raise ValueError(f"法人資料缺少欄位 {col}，目前欄位：{df.columns.tolist()}")

    df = df.copy()

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["stock_id"] = df["stock_id"].astype(str)
    df["name"] = df["name"].astype(str)
    df["buy"] = pd.to_numeric(df["buy"], errors="coerce")
    df["sell"] = pd.to_numeric(df["sell"], errors="coerce")

    df = df.dropna(subset=["date", "stock_id", "name", "buy", "sell"])

    if df.empty:
        raise ValueError("法人資料清理後為空")

    return df


def fetch_stock_price(api, stock_id: str, lookback_days: int = 60):
    """
    抓單一個股日K資料。
    """

    start_date = (
        datetime.now() - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")

    df = api.taiwan_stock_daily(
        stock_id=stock_id,
        start_date=start_date,
    )

    if df is None or df.empty:
        return None

    required_cols = [
        "date",
        "stock_id",
        "open",
        "max",
        "min",
        "close",
        "Trading_Volume",
    ]

    for col in required_cols:
        if col not in df.columns:
            print(f"⚠️ {stock_id} 股價資料缺少欄位 {col}")
            return None

    df = df.copy()

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["open"] = pd.to_numeric(df["open"], errors="coerce")
    df["max"] = pd.to_numeric(df["max"], errors="coerce")
    df["min"] = pd.to_numeric(df["min"], errors="coerce")
    df["Trading_Volume"] = pd.to_numeric(df["Trading_Volume"], errors="coerce")

    df = df.dropna(
        subset=[
            "date",
            "open",
            "max",
            "min",
            "close",
            "Trading_Volume",
        ]
    )

    if df.empty:
        return None

    return df.sort_values("date")


# --------------------------------------------------
# Institutional Scoring
# --------------------------------------------------

def build_chip_table(df_inst: pd.DataFrame):
    """
    建立個股法人籌碼表。

    指標：
    - 投信淨買
    - 外資淨買
    - 投信連買天數
    - 最新交易日
    """

    latest_date = df_inst["date"].max()
    latest_df = df_inst[df_inst["date"] == latest_date].copy()

    latest_df["net_buy"] = latest_df["buy"] - latest_df["sell"]

    trust_df = latest_df[
        latest_df["name"].str.contains(
            "Investment_Trust|投信",
            case=False,
            na=False,
        )
    ].copy()

    foreign_df = latest_df[
        latest_df["name"].str.contains(
            "Foreign|外資",
            case=False,
            na=False,
        )
    ].copy()

    if trust_df.empty:
        raise ValueError("找不到投信資料")

    trust_df = trust_df[trust_df["net_buy"] > 0].copy()

    if trust_df.empty:
        raise ValueError("最新交易日沒有投信買超標的")

    result = []

    for _, row in trust_df.iterrows():
        stock_id = str(row["stock_id"])
        trust_net_buy = int(row["net_buy"])

        foreign_row = foreign_df[foreign_df["stock_id"] == stock_id]

        if not foreign_row.empty:
            foreign_net_buy = int(foreign_row.iloc[0]["net_buy"])
        else:
            foreign_net_buy = 0

        consecutive_days = calculate_consecutive_trust_buy_days(
            df_inst=df_inst,
            stock_id=stock_id,
        )

        result.append({
            "stock_id": stock_id,
            "date": str(latest_date.date()),
            "trust_net_buy": trust_net_buy,
            "foreign_net_buy": foreign_net_buy,
            "consecutive_trust_buy_days": consecutive_days,
        })

    chip_df = pd.DataFrame(result)

    chip_df = chip_df.sort_values(
        ["trust_net_buy", "consecutive_trust_buy_days"],
        ascending=False,
    )

    return chip_df


def calculate_consecutive_trust_buy_days(df_inst: pd.DataFrame, stock_id: str):
    """
    計算投信連續買超天數。
    """

    df = df_inst[
        (df_inst["stock_id"] == stock_id)
        & (
            df_inst["name"].str.contains(
                "Investment_Trust|投信",
                case=False,
                na=False,
            )
        )
    ].copy()

    if df.empty:
        return 0

    df["net_buy"] = df["buy"] - df["sell"]
    df = df.sort_values("date", ascending=False)

    count = 0

    for _, row in df.iterrows():
        if row["net_buy"] > 0:
            count += 1
        else:
            break

    return count


# --------------------------------------------------
# Technical Scoring
# --------------------------------------------------

def calculate_technical_features(df_price: pd.DataFrame):
    """
    計算技術指標。
    """

    df = df_price.copy().sort_values("date")

    if len(df) < 20:
        return None

    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["vol5"] = df["Trading_Volume"].rolling(5).mean()
    df["vol20"] = df["Trading_Volume"].rolling(20).mean()

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    close = float(latest["close"])
    open_price = float(latest["open"])
    high = float(latest["max"])
    low = float(latest["min"])

    ma5 = float(latest["ma5"]) if pd.notna(latest["ma5"]) else None
    ma10 = float(latest["ma10"]) if pd.notna(latest["ma10"]) else None
    ma20 = float(latest["ma20"]) if pd.notna(latest["ma20"]) else None
    prev_ma20 = float(prev["ma20"]) if pd.notna(prev["ma20"]) else None

    volume = float(latest["Trading_Volume"])
    vol5 = float(latest["vol5"]) if pd.notna(latest["vol5"]) else None
    vol20 = float(latest["vol20"]) if pd.notna(latest["vol20"]) else None

    upper_shadow = high - max(open_price, close)
    candle_range = high - low if high > low else 1
    upper_shadow_ratio = upper_shadow / candle_range

    body = abs(close - open_price)
    body_ratio = body / candle_range

    distance_to_ma5 = None
    distance_to_ma10 = None

    if ma5 and ma5 > 0:
        distance_to_ma5 = round((close - ma5) / ma5 * 100, 2)

    if ma10 and ma10 > 0:
        distance_to_ma10 = round((close - ma10) / ma10 * 100, 2)

    volume_ratio = None

    if vol20 and vol20 > 0:
        volume_ratio = round(volume / vol20, 2)

    features = {
        "close": round(close, 2),
        "open": round(open_price, 2),
        "high": round(high, 2),
        "low": round(low, 2),
        "ma5": round(ma5, 2) if ma5 else None,
        "ma10": round(ma10, 2) if ma10 else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "volume": int(volume),
        "vol5": int(vol5) if vol5 else None,
        "vol20": int(vol20) if vol20 else None,
        "volume_ratio": volume_ratio,
        "distance_to_ma5": distance_to_ma5,
        "distance_to_ma10": distance_to_ma10,
        "upper_shadow_ratio": round(upper_shadow_ratio, 2),
        "body_ratio": round(body_ratio, 2),
        "above_ma5": bool(ma5 and close > ma5),
        "above_ma10": bool(ma10 and close > ma10),
        "above_ma20": bool(ma20 and close > ma20),
        "ma20_up": bool(ma20 and prev_ma20 and ma20 > prev_ma20),
        "red_k": bool(close > open_price),
        "black_k": bool(close < open_price),
    }

    return features


def calculate_scores(chip: dict, tech: dict, market: dict):
    """
    計算個股總分。

    總分：
    chip_score 40%
    trend_score 30%
    volume_score 15%
    market_score 15%
    """

    chip_score = 0

    trust_net_buy = chip.get("trust_net_buy", 0)
    foreign_net_buy = chip.get("foreign_net_buy", 0)
    consecutive_days = chip.get("consecutive_trust_buy_days", 0)

    if trust_net_buy > 0:
        chip_score += 35

    if trust_net_buy >= 1000:
        chip_score += 20
    elif trust_net_buy >= 500:
        chip_score += 15
    elif trust_net_buy >= 100:
        chip_score += 10

    if foreign_net_buy > 0:
        chip_score += 25

    if consecutive_days >= 5:
        chip_score += 20
    elif consecutive_days >= 3:
        chip_score += 15
    elif consecutive_days >= 2:
        chip_score += 10
    elif consecutive_days >= 1:
        chip_score += 5

    chip_score = min(chip_score, 100)

    trend_score = 0

    if tech.get("above_ma5"):
        trend_score += 25

    if tech.get("above_ma10"):
        trend_score += 25

    if tech.get("above_ma20"):
        trend_score += 20

    if tech.get("ma20_up"):
        trend_score += 20

    if tech.get("red_k"):
        trend_score += 10

    if tech.get("black_k"):
        trend_score -= 10

    if tech.get("upper_shadow_ratio", 0) >= 0.45:
        trend_score -= 25

    distance_to_ma5 = tech.get("distance_to_ma5")

    if distance_to_ma5 is not None:
        if distance_to_ma5 > 8:
            trend_score -= 25
        elif distance_to_ma5 > 5:
            trend_score -= 15
        elif 0 <= distance_to_ma5 <= 4:
            trend_score += 10

    trend_score = max(0, min(trend_score, 100))

    volume_score = 0

    volume_ratio = tech.get("volume_ratio")

    if volume_ratio is None:
        volume_score = 50
    else:
        if 1.2 <= volume_ratio <= 2.5:
            volume_score = 90
        elif 0.8 <= volume_ratio < 1.2:
            volume_score = 65
        elif 2.5 < volume_ratio <= 4:
            volume_score = 55
        elif volume_ratio > 4:
            volume_score = 30
        else:
            volume_score = 40

    market_score = market.get("score", 50)

    total_score = (
        chip_score * 0.40
        + trend_score * 0.30
        + volume_score * 0.15
        + market_score * 0.15
    )

    total_score = round(total_score, 1)

    return {
        "chip_score": round(chip_score, 1),
        "trend_score": round(trend_score, 1),
        "volume_score": round(volume_score, 1),
        "market_score": round(market_score, 1),
        "total_score": total_score,
    }


def classify_stock(scores: dict, tech: dict, market: dict):
    """
    個股分級。
    """

    total = scores["total_score"]
    distance_to_ma5 = tech.get("distance_to_ma5")

    # 大盤偏空時，不給 A 級
    if market["mode"] == "BEAR":
        if total >= 70:
            return "B", "大盤偏空，降級為觀察"
        return "C", "大盤偏空，剔除"

    # 台指期籌碼極端偏空：A 級自動降為 B
    if market["mode"] == "BEAR_CHIP":
        sentiment_score = market.get("sentiment_score", 0)
        if tech.get("upper_shadow_ratio", 0) >= 0.5 and tech.get("volume_ratio", 0) >= 2:
            return "C", "爆量長上影，疑似上方賣壓"
        if distance_to_ma5 is not None and distance_to_ma5 > 8:
            return "B", "漲幅偏離 5MA 過遠，只觀察不追"
        if total >= 80:
            return "B", f"台指期籌碼偏空（{sentiment_score}分），降級觀察"
        if total >= 60:
            return "B", "B級：條件尚可，但需等待確認"
        return "C", "剔除：條件不足"

    # 爆量長上影，直接降級
    if tech.get("upper_shadow_ratio", 0) >= 0.5 and tech.get("volume_ratio", 0) >= 2:
        return "C", "爆量長上影，疑似上方賣壓"

    # 距離 5MA 太遠，禁止追高
    if distance_to_ma5 is not None and distance_to_ma5 > 8:
        return "B", "漲幅偏離 5MA 過遠，只觀察不追"

    if total >= 80:
        return "A", "A級：籌碼與技術同步，優先觀察"

    if total >= 60:
        return "B", "B級：條件尚可，但需等待確認"

    return "C", "剔除：條件不足"


def build_trade_plan(item: dict):
    """
    建立個股觀察計畫。

    注意：
    V2 回測後，正式報告不再主張「回測 5MA / 10MA 進場」。
    目前只保留「隔日開盤觀察」作為回測中相對較佳的進場觀察模式。
    """

    tech = item["tech"]

    ma5 = tech.get("ma5")
    ma10 = tech.get("ma10")

    if ma5 and ma10:
        observation_zone = f"{ma5} ～ {ma10}"
        invalid = f"跌破 10MA {ma10}"
    elif ma5:
        observation_zone = f"5MA {ma5}"
        invalid = f"跌破 5MA {ma5}"
    else:
        observation_zone = "等待均線資料完整"
        invalid = "跌破短線支撐"

    if item["grade"] == "A":
        command = "優先觀察；隔日開盤列入觀察名單，小部位、嚴格停損，不追高加碼。"
    elif item["grade"] == "B":
        command = "只觀察，不主動進場；除非後續升級為 A 級，否則不給買進語氣。"
    else:
        command = "剔除，不列入交易。"

    return {
        "observation_zone": observation_zone,
        "invalid": invalid,
        "entry_mode": "OPEN_NEXT_DAY_OBSERVE",
        "disabled_entry_modes": ["PULLBACK_MA", "BREAK_PREV_HIGH"],
        "command": command,
    }


# --------------------------------------------------
# Main Selection Engine
# --------------------------------------------------

@safe_execute
def build_stock_watchlist(
    candidate_limit: int = 30,
    top_a: int = 3,
    top_b: int = 5,
):
    """
    建立 ATOS 個股觀察清單。
    """

    try:
        api = get_finmind_api()
        market = get_market_bias_from_state()

        df_inst = fetch_institutional_data(api)
        chip_df = build_chip_table(df_inst)

        chip_df = chip_df.head(candidate_limit)

        A_list = []
        B_list = []
        C_list = []

        for _, row in chip_df.iterrows():
            stock_id = str(row["stock_id"])

            df_price = fetch_stock_price(api, stock_id=stock_id)

            if df_price is None or df_price.empty:
                continue

            tech = calculate_technical_features(df_price)

            if tech is None:
                continue

            chip = {
                "stock_id": stock_id,
                "date": row["date"],
                "trust_net_buy": int(row["trust_net_buy"]),
                "foreign_net_buy": int(row["foreign_net_buy"]),
                "consecutive_trust_buy_days": int(row["consecutive_trust_buy_days"]),
            }

            scores = calculate_scores(
                chip=chip,
                tech=tech,
                market=market,
            )

            grade, grade_reason = classify_stock(
                scores=scores,
                tech=tech,
                market=market,
            )

            item = {
                "id": stock_id,
                "date": chip["date"],
                "grade": grade,
                "grade_reason": grade_reason,
                "chip": chip,
                "tech": tech,
                "scores": scores,
                "market": market,
                "is_cache": False,
                "cache_age_hours": 0,
            }

            item["plan"] = build_trade_plan(item)

            if grade == "A":
                A_list.append(item)
            elif grade == "B":
                B_list.append(item)
            else:
                C_list.append(item)

        A_list = sorted(
            A_list,
            key=lambda x: x["scores"]["total_score"],
            reverse=True,
        )[:top_a]

        B_list = sorted(
            B_list,
            key=lambda x: x["scores"]["total_score"],
            reverse=True,
        )[:top_b]

        C_list = sorted(
            C_list,
            key=lambda x: x["scores"]["total_score"],
            reverse=True,
        )[:5]

        result = {
            "A": A_list,
            "B": B_list,
            "C": C_list,
            "market": market,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_cache": False,
        }

        # 套用新版防守型個股報告策略
        if apply_stock_report_policy_to_picks is not None:
            try:
                policy_result = apply_stock_report_policy_to_picks(result)

                # 將 policy 覆蓋回 A/B/C 內的 plan.command
                result["policy_result"] = policy_result
                result["strategy_mode"] = "DEFENSE_STOCK_MODE"

            except Exception as e:
                print(f"⚠️ stock report policy failed: {e}")
                result["policy_result"] = None
                result["strategy_mode"] = "LEGACY_STOCK_MODE"
        else:
            result["policy_result"] = None
            result["strategy_mode"] = "LEGACY_STOCK_MODE"

        save_to_cache(result)

        return result

    except Exception as e:
        print(f"📡 [API Error] 個股篩選失敗: {e}")
        print("🛡️ [System] 嘗試讀取個股快取...")

        cached = load_from_cache()

        if cached:
            cached["is_cache"] = True

            if apply_stock_report_policy_to_picks is not None:
                try:
                    cached["policy_result"] = apply_stock_report_policy_to_picks(cached)
                    cached["strategy_mode"] = "DEFENSE_STOCK_MODE"
                except Exception as policy_error:
                    print(f"⚠️ cached stock report policy failed: {policy_error}")
                    cached["policy_result"] = None
                    cached["strategy_mode"] = "LEGACY_STOCK_MODE"

            return cached

        return {
            "A": [],
            "B": [],
            "C": [],
            "market": {
                "mode": "UNKNOWN",
                "label": "⚪ 大盤狀態未知",
                "score": 50,
            },
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_cache": True,
            "policy_result": None,
            "strategy_mode": "DEFENSE_STOCK_MODE",
        }


# --------------------------------------------------
# Message Formatting
# --------------------------------------------------

def format_stock_item(item: dict, index: int):
    """
    格式化單一個股。
    """

    chip = item["chip"]
    tech = item["tech"]
    scores = item["scores"]
    plan = item["plan"]

    cache_mark = "｜CACHE" if item.get("is_cache") else ""

    return (
        f"{index}. {item['id']}｜總分 {scores['total_score']}｜{item['grade_reason']}{cache_mark}\n"
        f"   籌碼：投信淨買 {chip['trust_net_buy']}｜外資淨買 {chip['foreign_net_buy']}｜投信連買 {chip['consecutive_trust_buy_days']} 天\n"
        f"   技術：收盤 {tech['close']}｜5MA {tech['ma5']}｜10MA {tech['ma10']}｜20MA {tech['ma20']}\n"
        f"   量能：量比 {tech['volume_ratio']}｜上影比例 {tech['upper_shadow_ratio']}\n"
        f"   觀察區：{plan['observation_zone']}\n"
        f"   失效：{plan['invalid']}\n"
        f"   指令：{plan['command']}"
    )


def build_legacy_stock_section(result: dict):
    """
    policy engine 不可用時的備援報告。
    """

    A_list = result.get("A", [])
    B_list = result.get("B", [])
    C_list = result.get("C", [])

    if A_list:
        a_text = "\n\n".join(
            format_stock_item(item, i)
            for i, item in enumerate(A_list, start=1)
        )
    else:
        a_text = "今日無 A 級標的。"

    if B_list:
        b_text = "\n\n".join(
            format_stock_item(item, i)
            for i, item in enumerate(B_list, start=1)
        )
    else:
        b_text = "今日無 B 級標的。"

    if C_list:
        c_text = "\n".join(
            f"{i}. {item['id']}｜{item['grade_reason']}｜總分 {item['scores']['total_score']}"
            for i, item in enumerate(C_list, start=1)
        )
    else:
        c_text = "無剔除清單。"

    return (
        "🟢 A級：優先觀察\n"
        f"{a_text}\n\n"
        "🟡 B級：觀察名單\n"
        f"{b_text}\n\n"
        "❌ C級：剔除\n"
        f"{c_text}"
    )


def _build_a_grade_ai_commentary(result: dict) -> str:
    """
    為 A 級個股產生 AI 白話快評（2-3 行）。
    ANTHROPIC_API_KEY 未設定或 AI 呼叫失敗時回傳空字串。
    """
    try:
        from ai_report_engine import generate_stock_commentary
    except Exception:
        return ""

    policy_result = result.get("policy_result")
    a_items = (
        policy_result.get("priority", [])
        if isinstance(policy_result, dict)
        else result.get("A", [])
    )

    if not a_items:
        return ""

    lines = ["AI 個股快評（A 級）"]
    for item in a_items[:3]:
        stock_id = item.get("id") or item.get("stock_id", "N/A")
        try:
            commentary = generate_stock_commentary(item)
            if commentary:
                lines.append(f"{stock_id}：{commentary}")
        except Exception:
            pass

    return "\n".join(lines) if len(lines) > 1 else ""


@safe_execute
def send_stock_picks_report(
    candidate_limit: int = 30,
    top_a: int = 3,
    top_b: int = 5,
):
    """
    發送 ATOS 個股觀察報告。
    """

    now = datetime.now().strftime("%H:%M")

    result = build_stock_watchlist(
        candidate_limit=candidate_limit,
        top_a=top_a,
        top_b=top_b,
    )

    market = result.get("market", {})
    source = "快取資料" if result.get("is_cache") else "FinMind 更新資料"

    policy_result = result.get("policy_result")

    if build_stock_policy_report_section is not None and policy_result is not None:
        try:
            stock_section = build_stock_policy_report_section(policy_result)
        except Exception as e:
            print(f"⚠️ build_stock_policy_report_section failed: {e}")
            stock_section = build_legacy_stock_section(result)
    else:
        stock_section = build_legacy_stock_section(result)

    ai_section = _build_a_grade_ai_commentary(result)

    if market.get("mode") == "BULL":
        market_command = "大盤偏多，A級可列優先觀察，但仍不可追高或重倉。"
    elif market.get("mode") == "BEAR":
        market_command = "大盤偏空，個股全部降級，只觀察不主動進場。"
    elif market.get("mode") == "BEAR_CHIP":
        market_command = "台指期籌碼偏空，A級已降為觀察，不主動進場。"
    elif market.get("mode") == "NEUTRAL":
        market_command = "大盤中性，僅保留A級優先觀察，B級不主動進場。"
    else:
        market_command = "大盤狀態不明，個股報告僅作觀察。"

    msg = (
        "📈 ATOS 個股觀察報告 v3｜Defense Stock Mode\n"
        f"時間：{now}\n"
        f"資料來源：{source}\n"
        f"產生時間：{result.get('generated_at')}\n"
        f"策略模式：{result.get('strategy_mode', 'DEFENSE_STOCK_MODE')}\n"
        "---\n\n"

        "🌐 大盤過濾\n"
        f"● 狀態：{market.get('label', 'N/A')}\n"
        f"● 指令：{market_command}\n\n"

        f"{stock_section}\n\n"

        + (f"{ai_section}\n\n" if ai_section else "")

        + "⚠️ 交易規則｜依 V2 回測修正\n"
        "● A級：優先觀察，不等於直接買進\n"
        "● B級：只觀察，不主動進場\n"
        "● C級：剔除，不列入交易\n"
        "● 正式進場法暫只保留：隔日開盤觀察\n"
        "● 暫不採用：回測 5MA / 10MA、突破推薦日高點\n"
        "● 不因法人買超直接追高\n"
        "● 大盤偏空或不可交易時，全部降級處理\n"
        "● 爆量長上影、距離 5MA 過遠，一律不追\n\n"

        "💡 指揮官結論\n"
        f"> {market_command}"
    )

    send_to_telegram(msg)


# --------------------------------------------------
# 舊版 report_engine.py 相容用
# --------------------------------------------------

@safe_execute
def get_institutional_picks(top_n: int = 3):
    """
    舊版 report_engine.py 相容用。

    將新版 build_stock_watchlist() 的 A / B 清單
    轉成舊版 get_institutional_picks() 格式。

    注意：
    回測後 B 級只作觀察，不主動進場。
    """

    result = build_stock_watchlist(
        candidate_limit=30,
        top_a=top_n,
        top_b=top_n,
    )

    picks = []

    # 優先只回傳 A 級
    for item in result.get("A", []):
        picks.append({
            "id": item.get("id"),
            "reason": item.get("grade_reason", "A級優先觀察"),
            "net_buy": item.get("chip", {}).get("trust_net_buy", 0),
            "score": item.get("scores", {}).get("total_score", 0),
            "grade": "A",
            "trade_permission": "OPEN_OBSERVE",
            "is_cache": item.get("is_cache", False),
        })

    # 若 A 不足，再補 B，但標記為 WATCH_ONLY
    if len(picks) < top_n:
        for item in result.get("B", []):
            picks.append({
                "id": item.get("id"),
                "reason": item.get("grade_reason", "B級觀察，不主動進場"),
                "net_buy": item.get("chip", {}).get("trust_net_buy", 0),
                "score": item.get("scores", {}).get("total_score", 0),
                "grade": "B",
                "trade_permission": "WATCH_ONLY",
                "is_cache": item.get("is_cache", False),
            })

            if len(picks) >= top_n:
                break

    return picks[:top_n]


# --------------------------------------------------
# Manual Test
# --------------------------------------------------

if __name__ == "__main__":
    send_stock_picks_report()