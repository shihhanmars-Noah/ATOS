# stock_report_engine.py

import os
import pickle
from datetime import datetime, timedelta

import pandas as pd

try:
    from stock_report_policy_engine import apply_stock_report_policy_to_picks
except Exception:
    apply_stock_report_policy_to_picks = None

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
# Report Formatting Helpers
# --------------------------------------------------

def _fmt_fear_greed(emotion: str) -> str:
    table = {
        "extreme fear": "極度恐慌",
        "fear": "恐慌",
        "neutral": "中性",
        "greed": "貪婪",
        "extreme greed": "極度貪婪",
    }
    return table.get(str(emotion).lower().strip(), emotion)


def _price_position_label(pct) -> str:
    if pct is None:
        return "N/A"
    pct = float(pct)
    if pct < 30:
        return "偏下方"
    if pct < 45:
        return "中段偏低"
    if pct <= 55:
        return "中段"
    if pct <= 70:
        return "中段偏高"
    return "偏上方"


def _max_pain_label(max_pain, current_price) -> str:
    if max_pain is None or current_price is None:
        return ""
    diff = float(max_pain) - float(current_price)
    if abs(diff) < 200:
        return "接近現價"
    return "大戶希望往下結算" if diff < 0 else "大戶希望往上結算"


def _spot_direction(val) -> str:
    if val is None:
        return ""
    return "買超" if float(val) > 0 else "賣超"


def _get_stock_ai_commentary(item: dict) -> str:
    try:
        from ai_report_engine import generate_stock_commentary
        return generate_stock_commentary(item) or ""
    except Exception:
        return ""


def _get_market_ai_commentary(chip_ctx: dict) -> str:
    try:
        from ai_report_engine import generate_chip_market_commentary
        return generate_chip_market_commentary(chip_ctx) or ""
    except Exception:
        return ""


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


def classify_stock(scores: dict, tech: dict, market: dict, sentiment_score: int = 0):
    """
    個股分級。
    """

    total = scores["total_score"]
    distance_to_ma5 = tech.get("distance_to_ma5")

    # 低於5MA 直接不列入觀察
    if not tech.get("above_ma5", True):
        return "C", "低於5MA，不列入觀察"

    # 大盤偏空時，不給 A 級
    if market["mode"] == "BEAR":
        if total >= 70:
            return "B", "大盤偏空，降級為觀察"
        return "C", "大盤偏空，剔除"

    # 台指期籌碼極端偏空：A 級自動降為 B，且加嚴門檻
    if market["mode"] == "BEAR_CHIP":
        _score = market.get("sentiment_score", sentiment_score)
        if tech.get("upper_shadow_ratio", 0) >= 0.5 and tech.get("volume_ratio", 0) >= 2:
            return "C", "爆量長上影，疑似上方賣壓"
        if distance_to_ma5 is not None and distance_to_ma5 > 8:
            return "C", "距5MA過遠，剔除"
        # 偏空環境 A 級加嚴（sentiment_score <= -3）
        if _score <= -3:
            consec = scores.get("chip_score", 0)  # 從 chip 計算出的連買天數需從 chip dict 拿
            # 直接用 market 傳入的 sentiment_score 判斷
            if tech.get("distance_to_ma5", 0) > 5:
                return "B", "偏空環境：距5MA過遠，降為觀察"
        if total >= 80:
            return "B", f"台指期籌碼偏空（{_score}分），降級觀察"
        if total >= 60:
            return "B", "B級：條件尚可，但需等待確認"
        return "C", "剔除：條件不足"

    # 爆量長上影，直接降級
    if tech.get("upper_shadow_ratio", 0) >= 0.5 and tech.get("volume_ratio", 0) >= 2:
        return "C", "爆量長上影，疑似上方賣壓"

    # 距離 5MA 太遠，直接剔除，不進 B 級
    if distance_to_ma5 is not None and distance_to_ma5 > 8:
        return "C", "距5MA過遠，剔除"

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
                sentiment_score=int(market.get("sentiment_score", 0) or 0),
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

def _stock_position_desc(item: dict) -> str:
    """A 級個股白話位置描述。"""
    tech = item.get("tech", {}) or {}
    close = tech.get("close", "N/A")
    ma5 = tech.get("ma5", "N/A")
    dist = tech.get("distance_to_ma5")
    vol_ratio = tech.get("volume_ratio")

    dist_str = f"{dist}%" if dist is not None else "N/A"

    if vol_ratio is None:
        vol_desc = "量能資料不足"
    elif vol_ratio > 2.5:
        vol_desc = "明顯放量"
    elif vol_ratio > 1.5:
        vol_desc = "溫和放量"
    elif vol_ratio < 0.8:
        vol_desc = "量縮"
    else:
        vol_desc = "量能正常"

    return (
        f"{close} 站在5MA {ma5} 上方 {dist_str}，{vol_desc}，"
        "注意大盤偏空環境下跌破5MA為出場訊號"
    )


def format_stock_item(item: dict, index: int):
    """
    格式化單一個股。
    """

    chip = item["chip"]
    tech = item["tech"]
    scores = item["scores"]
    plan = item["plan"]

    cache_mark = "｜CACHE" if item.get("is_cache") else ""

    base = (
        f"{index}. {item['id']}｜總分 {scores['total_score']}｜{item['grade_reason']}{cache_mark}\n"
        f"   籌碼：投信淨買 {chip['trust_net_buy']}｜外資淨買 {chip['foreign_net_buy']}｜投信連買 {chip['consecutive_trust_buy_days']} 天\n"
        f"   技術：收盤 {tech['close']}｜5MA {tech['ma5']}｜10MA {tech['ma10']}｜20MA {tech['ma20']}\n"
        f"   量能：量比 {tech['volume_ratio']}｜上影比例 {tech['upper_shadow_ratio']}\n"
        f"   觀察區：{plan['observation_zone']}\n"
        f"   失效：{plan['invalid']}\n"
        f"   指令：{plan['command']}"
    )
    if item.get("grade") == "A":
        base += f"\n   白話：{_stock_position_desc(item)}"
    return base


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



@safe_execute
def send_stock_picks_report(
    candidate_limit: int = 30,
    top_a: int = 3,
    top_b: int = 5,
):
    """
    發送 ATOS 個股觀察報告（夜盤框架 + 結算週版本）。
    """

    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    today_str = date_str

    # ── 籌碼背景 ──
    try:
        from chip_data_engine import build_chip_context
        chip_ctx = build_chip_context() or {}
    except Exception:
        chip_ctx = {}

    source_dates     = chip_ctx.get("source_dates", {}) or {}
    chip_source_date = source_dates.get("futures") or source_dates.get("spot") or "N/A"
    oi_source_date   = source_dates.get("option_oi") or "N/A"

    # ── 夜盤資料 ──
    try:
        from night_session_engine import get_night_session_data
        night_data = get_night_session_data()
    except Exception:
        night_data = {}
    nd_close    = float(night_data.get("night_close", 0) or 0)
    nd_high     = float(night_data.get("night_high",  0) or 0)
    nd_low      = float(night_data.get("night_low",   0) or 0)
    nd_chg      = float(night_data.get("night_chg",   0) or 0)
    nd_chg_pct  = float(night_data.get("night_chg_pct", 0) or 0)
    nd_day_close= float(night_data.get("day_close",   0) or 0)
    nd_big_move = bool(night_data.get("is_big_move", False))

    # ── 法人籌碼 ──
    sentiment_score     = chip_ctx.get("sentiment_score", 0)
    bias_label          = chip_ctx.get("sentiment_bias", "N/A")
    fear_greed          = chip_ctx.get("fear_greed_index", "N/A")
    fear_greed_emotion  = _fmt_fear_greed(chip_ctx.get("fear_greed_emotion", ""))
    foreign_net         = chip_ctx.get("foreign_net", 0) or 0
    foreign_net_level   = chip_ctx.get("foreign_net_level", "N/A")
    foreign_ah_net      = chip_ctx.get("foreign_ah_net", 0) or 0
    spot_val            = chip_ctx.get("spot_foreign_net_buy_bn") or 0
    spot_5d             = chip_ctx.get("spot_foreign_5d_sum_bn") or 0
    spot_dir            = _spot_direction(spot_val)
    call_wall           = chip_ctx.get("call_wall", "N/A")
    put_wall            = chip_ctx.get("put_wall", "N/A")
    price_position_pct  = chip_ctx.get("price_position_pct")
    max_pain            = chip_ctx.get("max_pain")
    pivot               = chip_ctx.get("pivot")
    call_put_ratio      = chip_ctx.get("call_put_ratio")

    try:
        estimated_net = int(foreign_net) + int(foreign_ah_net)
    except Exception:
        estimated_net = foreign_net

    pos_label   = _price_position_label(price_position_pct)
    score_str   = f"{int(sentiment_score):+d}" if sentiment_score is not None else "N/A"

    # ── 結算天數 ──
    try:
        from settlement_engine import get_days_to_settlement
        days_settle = get_days_to_settlement()
        if days_settle is None:
            days_settle = 99
    except Exception:
        days_settle = 99
    try:
        days_settle = int(days_settle)
    except Exception:
        days_settle = 99

    # ── OI 框架有效性 ──
    oi_invalid = False
    try:
        _state_price = float(load_state().get("price") or 0)
        _cw = float(call_wall) if call_wall not in ("N/A", None, "") else 0
        _pw = float(put_wall)  if put_wall  not in ("N/A", None, "") else 0
        if oi_source_date != today_str and _state_price > 0:
            if _cw > 0 and _state_price > _cw + 500:
                oi_invalid = True
            elif _pw > 0 and _state_price < _pw - 500:
                oi_invalid = True
    except Exception:
        pass

    # ── 大盤判斷 ──
    market = get_market_bias_from_state()

    # ── 建立股票清單 ──
    result = build_stock_watchlist(
        candidate_limit=candidate_limit,
        top_a=top_a,
        top_b=top_b,
    )

    policy_result = result.get("policy_result")
    if isinstance(policy_result, dict):
        a_items = policy_result.get("priority", [])
        b_items = policy_result.get("watchlist", [])
    else:
        a_items = result.get("A", [])
        b_items = result.get("B", [])

    lines = []

    # ── 標頭 ──
    lines.append(f"📈 個股觀察 {date_str}")
    lines.append("")

    # ── 大盤環境 ──
    lines.append("━━ 大盤環境 ━━")
    if nd_close > 0:
        lines.append(f"台指夜盤：{int(nd_close)}")
        if nd_day_close > 0:
            lines.append(
                f"（昨日日盤 {int(nd_day_close)}，"
                f"夜盤 {nd_chg:+.0f}點 {nd_chg_pct:+.1f}%）"
            )
        lines.append(f"夜盤區間：{int(nd_low)} ～ {int(nd_high)}")
    else:
        lines.append("台指夜盤：資料取得中")

    if foreign_ah_net != 0:
        ah_dir_word = "買" if foreign_ah_net > 0 else "賣"
        lines.append(f"外資夜盤：淨{ah_dir_word} {foreign_ah_net:+,}口")
    lines.append(f"外資估算部位：約 {estimated_net:+,}口")
    lines.append(
        f"台指情緒：{bias_label}({score_str})"
        f"（昨日，今日13:50更新）"
    )
    lines.append(f"Fear&Greed：{fear_greed} {fear_greed_emotion}")
    lines.append(f"結算：{days_settle}天後")
    lines.append("")

    # ── 夜盤大波動警示 ──
    if nd_big_move and nd_close > 0:
        lines.append(f"⚠️ 夜盤大幅波動（{nd_chg_pct:+.1f}%）")
        lines.append("個股今日開盤跳空，昨日技術數據僅供參考")
        lines.append("所有個股進場點位需依今日開盤後實際點位重新評估")
        lines.append("不主動追高，等開盤第一根5分K確認方向後再決定")
        lines.append("")

    # ── 結算週警示 ──
    if days_settle <= 7:
        lines.append(f"⚠️ 結算週（{days_settle}天後結算）：個股受大盤換倉影響較大，降低操作頻率")
        lines.append("")

    # ── 偏空環境提示 ──
    try:
        _ss = int(sentiment_score) if sentiment_score is not None else 0
        if _ss <= -3:
            lines.append(f"⚠️ 市場偏空環境（情緒評分 {score_str}）")
            lines.append("以下為「大盤反彈時相對強勢股」預備清單")
            lines.append("現在不主動操作，等市場方向明朗後再使用")
            lines.append("")
    except Exception:
        pass

    # ── 大盤定位文字 ──
    if nd_big_move and nd_chg > 0:
        lines.append("軋空後開盤方向不確定，今日為觀察日，等確認後操作")
        lines.append("")
    elif nd_big_move and nd_chg < 0:
        lines.append("夜盤大跌後開盤風險高，今日以觀察為主")
        lines.append("")

    # ── A 級 — 批次 AI 點評 ──
    batch_commentaries: dict = {}
    if a_items:
        try:
            from ai_report_engine import generate_stock_commentaries_batch
            batch_commentaries = generate_stock_commentaries_batch(a_items, chip_ctx) or {}
        except Exception:
            pass

    # A 級標題
    try:
        _ss2 = int(sentiment_score) if sentiment_score is not None else 0
        if nd_big_move and nd_close > 0:
            lines.append("🟢 A級（方向確認後首選）")
            lines.append("（今日夜盤大幅波動，進場點位需依開盤後實際點位調整）")
        elif _ss2 <= -3:
            lines.append("🟢 A級（反彈首選）")
            lines.append("（偏空環境門檻：投信連買>=3天 + 距5MA<5% + 站在5MA上方）")
        else:
            lines.append("🟢 A級優先觀察")
    except Exception:
        lines.append("🟢 A級優先觀察")

    if not a_items:
        lines.append("今日無A級標的")
    else:
        for item in a_items:
            stock_id   = item.get("id") or item.get("stock_id", "N/A")
            tech       = item.get("tech", {}) or {}
            chip       = item.get("chip", {}) or {}
            close      = tech.get("close", "N/A")
            ma5        = tech.get("ma5", "N/A")
            consecutive= chip.get("consecutive_trust_buy_days", 0)
            vol_ratio  = tech.get("volume_ratio", "N/A")
            item_date  = chip.get("date", chip_source_date)
            lines.append(
                f"{stock_id}｜{close}｜5MA {ma5}"
                f"｜投信連買{consecutive}日｜量比{vol_ratio}"
            )
            lines.append(_stock_position_desc(item))
            commentary = batch_commentaries.get(str(stock_id), "")
            if commentary:
                lines.append(f"AI點評：{commentary}")
            lines.append(f"（技術數據：{item_date}，今日開盤後點位不同）")
    lines.append("")

    # ── B 級標題 ──
    try:
        _ss3 = int(sentiment_score) if sentiment_score is not None else 0
        if nd_big_move and nd_close > 0:
            lines.append("🟡 B級（次選觀察，今日跳空開盤請重新評估進場點位）")
        elif _ss3 <= -3:
            lines.append("🟡 B級（次選觀察，不建議主動進場）")
        else:
            lines.append("🟡 B級觀察（不主動進場）")
    except Exception:
        lines.append("🟡 B級觀察（不主動進場）")

    if not b_items:
        lines.append("今日無B級標的")
    else:
        for item in b_items:
            stock_id   = item.get("id") or item.get("stock_id", "N/A")
            tech       = item.get("tech", {}) or {}
            chip       = item.get("chip", {}) or {}
            close      = tech.get("close", "N/A")
            dist       = tech.get("distance_to_ma5")
            dist_str   = f"{dist:+.2f}%" if dist is not None else "N/A"
            position_word = "站在5MA上方" if tech.get("above_ma5") else "低於5MA下方"
            _vr = tech.get("volume_ratio")
            if _vr is None:
                vol_desc = "量能資料不足"
            elif _vr > 2.5:
                vol_desc = "明顯放量"
            elif _vr > 1.5:
                vol_desc = "溫和放量"
            elif _vr < 0.8:
                vol_desc = "量縮"
            else:
                vol_desc = "量能正常"
            item_date = chip.get("date", chip_source_date)
            lines.append(
                f"{stock_id}｜{close}｜距5MA {dist_str}，"
                f"{position_word}，{vol_desc}"
                f"（{item_date}）"
            )
    lines.append("")

    # ── 目前市場狀態 ──
    lines.append("━━ 目前市場狀態 ━━")
    lines.append(
        f"外資期貨：{foreign_net_level} {estimated_net:+,}口（估算含夜盤）"
    )
    lines.append(f"現貨外資：{spot_dir} {abs(spot_val):.1f}億（昨日）")

    if oi_invalid:
        lines.append(
            f"OI框架：昨日 Call wall {call_wall} / Put wall {put_wall} 失效"
        )
        lines.append("等14:30 OI更新後建立新框架")
    else:
        lines.append(f"Call wall：{call_wall}｜Put wall：{put_wall}")

    mp_label = "失效" if oi_invalid else _max_pain_label(max_pain, pivot)
    lines.append(f"Max Pain：{max_pain}（{mp_label}）")

    if max_pain is not None and pivot is not None and not oi_invalid:
        try:
            if float(max_pain) < float(pivot):
                lines.append(
                    f"⚠️ Max Pain {max_pain} 低於現價，大戶希望往下結算，個股多方需謹慎"
                )
        except Exception:
            pass

    lines.append(f"情緒總分：{score_str}｜{bias_label}")

    # ── AI 市場解讀 ──
    try:
        _ai_chip = dict(chip_ctx)
        # 注入夜盤背景讓 AI 有更好的上下文
        _ai_chip["_night_context"] = (
            f"夜盤{nd_chg:+.0f}點，外資估算{estimated_net:+,}口，"
            f"結算{days_settle}天，C/P比{call_put_ratio}"
        )
        if nd_big_move:
            _ai_chip["_analysis_request"] = (
                f"昨夜盤大幅波動{nd_chg:+.0f}點，請說明對個股操作的影響"
            )
        if foreign_ah_net != 0:
            _ai_chip["_ah_note"] = (
                f"外資夜盤已{'回補' if foreign_ah_net > 0 else '加碼'}"
                f"{foreign_ah_net:+,}口，估算部位{estimated_net:+,}口"
            )
        market_commentary = _get_market_ai_commentary(_ai_chip)
    except Exception:
        market_commentary = ""

    if market_commentary:
        lines.append("")
        lines.append("━━ AI 市場解讀 ━━")
        lines.append(market_commentary)

    send_to_telegram("\n".join(lines))


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