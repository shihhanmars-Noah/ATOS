# chip_data_engine.py  v2.0
#
# 架構：
#   資料層  → 四個 FinMind 資料集真正接通
#   計算層  → SentimentScore (-9~+9)、上下限三層框架
#   快取層  → chip_cache.json v2 新結構
#   報告層  → build_chip_context() 給 AI 報告使用
#
# 更新時間：
#   08:30  前一交易日資料初始化（早盤報告用）
#   13:50  當日盤後最新資料（晚盤報告用）
#   15:30  再次確認更新
#
# V1 靜態門檻，欄位保留 historical_percentile 供未來校準

import json
import os
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

from error_handler import safe_execute
from persistent_state import load_state, save_state

load_dotenv()

CHIP_FILE = "chip_cache.json"
FUTURES_SYMBOL = "TX"
OPTION_SYMBOL = "TXO"
SPOT_TAIEX_ID = "TAIEX"

# 近價 OI 視窗（點）
OI_NEARBY_WINDOW = 2000

# 每口市值計算用（台指期一口 = 指數 * 200 元）
FUTURES_MULTIPLIER = 200


# --------------------------------------------------
# FinMind 連線
# --------------------------------------------------

def _get_api():
    from FinMind.data import DataLoader
    token = os.getenv("FIN_TOKEN") or os.getenv("FINMIND_TOKEN")
    if not token:
        raise ValueError("FINMIND_TOKEN 未設定")
    api = DataLoader()
    api.login_by_token(token)
    return api


def _start_date(days: int = 30) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v) if v is not None else default
    except Exception:
        return default


def _safe_int(v, default=0) -> int:
    try:
        return int(v) if v is not None else default
    except Exception:
        return default


# --------------------------------------------------
# 資料層 A：台指期三大法人（期貨籌碼）
# --------------------------------------------------

@safe_execute
def fetch_futures_institutional() -> Optional[dict]:
    """
    抓取台指期三大法人未平倉資料（近30日）。

    回傳欄位：
      foreign_long, foreign_short, foreign_net
      foreign_net_chg_1d, foreign_net_chg_3d, foreign_net_chg_5d
      foreign_long_chg, foreign_short_chg
      foreign_action   加多減空 / 加空減多 / 兩面建倉 / 兩面減倉 / 其他
      foreign_net_level  極強多/強多/偏多/中性/偏空/強空/極強空
      foreign_notional_bn  淨部位估算金額（億元）
      dealer_net, dealer_net_chg
      history_7d  [由舊到新的7日 foreign_net 列表]
      source_date
    """

    api = _get_api()

    df = api.get_data(
        dataset="TaiwanFuturesInstitutionalInvestors",
        data_id=FUTURES_SYMBOL,
        start_date=_start_date(30),
    )

    if df is None or df.empty:
        print("⚠️ TaiwanFuturesInstitutionalInvestors 無資料")
        return None

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df = df.sort_values("date")

    # 找身份別欄位（不同 FinMind 版本欄位名稱可能不同）
    name_col = None
    for c in ["institutional_investors", "name", "identity_type"]:
        if c in df.columns:
            name_col = c
            break

    if name_col is None:
        print(f"⚠️ 找不到身份別欄位，現有欄位：{df.columns.tolist()}")
        return None

    # 標準化欄位
    long_col = _find_col(df, ["long_open_interest_balance_volume",
                               "long_open_interest_balance", "long_oi"])
    short_col = _find_col(df, ["short_open_interest_balance_volume",
                                "short_open_interest_balance", "short_oi"])

    if not long_col or not short_col:
        print(f"⚠️ 找不到多空未平倉欄位，現有欄位：{df.columns.tolist()}")
        return None

    df[long_col] = pd.to_numeric(df[long_col], errors="coerce").fillna(0)
    df[short_col] = pd.to_numeric(df[short_col], errors="coerce").fillna(0)
    df["net"] = df[long_col] - df[short_col]

    # 外資
    foreign_mask = df[name_col].astype(str).str.contains(
        "Foreign|外資|陸資", case=False, na=False
    )
    foreign_df = df[foreign_mask].copy()

    # 自營商
    dealer_mask = df[name_col].astype(str).str.contains(
        "Dealer|自營", case=False, na=False
    )
    dealer_df = df[dealer_mask].copy()

    if foreign_df.empty:
        print("⚠️ 找不到外資資料列")
        return None

    # 依日期聚合（同一天可能有多列）
    foreign_daily = foreign_df.groupby("date").agg(
        long_oi=(long_col, "sum"),
        short_oi=(short_col, "sum"),
        net=("net", "sum"),
    ).reset_index().sort_values("date")

    dealer_daily = dealer_df.groupby("date").agg(
        net=("net", "sum"),
    ).reset_index().sort_values("date") if not dealer_df.empty else pd.DataFrame()

    if foreign_daily.empty:
        return None

    # 最新一日
    latest = foreign_daily.iloc[-1]
    source_date = str(latest["date"].date())
    foreign_long = _safe_int(latest["long_oi"])
    foreign_short = _safe_int(latest["short_oi"])
    foreign_net = _safe_int(latest["net"])

    # 歷史淨部位序列（最近7日）
    history_7d = foreign_daily["net"].astype(int).tolist()[-7:]

    # 各期間淨增減
    def net_chg(n: int) -> int:
        rows = foreign_daily.tail(n + 1)
        if len(rows) < 2:
            return 0
        return _safe_int(rows.iloc[-1]["net"]) - _safe_int(rows.iloc[0]["net"])

    foreign_net_chg_1d = net_chg(1)
    foreign_net_chg_3d = net_chg(3)
    foreign_net_chg_5d = net_chg(5)

    # 今日多空單各自增減（與前日比）
    if len(foreign_daily) >= 2:
        prev = foreign_daily.iloc[-2]
        foreign_long_chg = foreign_long - _safe_int(prev["long_oi"])
        foreign_short_chg = foreign_short - _safe_int(prev["short_oi"])
    else:
        foreign_long_chg = 0
        foreign_short_chg = 0

    # 動作品質分類
    foreign_action = _classify_action(foreign_long_chg, foreign_short_chg)

    # 淨部位等級（V1 靜態門檻）
    foreign_net_level = _classify_net_level(foreign_net)

    # 淨部位估算金額（億元，用最新收盤估算）
    state = load_state()
    ref_price = _safe_float(state.get("day_session_close") or state.get("price"), 41000.0)
    foreign_notional_bn = round(abs(foreign_net) * ref_price * FUTURES_MULTIPLIER / 1e8, 1)

    # 自營商
    dealer_net = 0
    dealer_net_chg = 0
    if not dealer_daily.empty:
        dealer_net = _safe_int(dealer_daily.iloc[-1]["net"])
        if len(dealer_daily) >= 2:
            dealer_net_chg = dealer_net - _safe_int(dealer_daily.iloc[-2]["net"])

    result = {
        "source_date": source_date,
        "foreign_long": foreign_long,
        "foreign_short": foreign_short,
        "foreign_net": foreign_net,
        "foreign_net_chg_1d": foreign_net_chg_1d,
        "foreign_net_chg_3d": foreign_net_chg_3d,
        "foreign_net_chg_5d": foreign_net_chg_5d,
        "foreign_long_chg": foreign_long_chg,
        "foreign_short_chg": foreign_short_chg,
        "foreign_action": foreign_action,
        "foreign_net_level": foreign_net_level,
        "foreign_notional_bn": foreign_notional_bn,
        "dealer_net": dealer_net,
        "dealer_net_chg": dealer_net_chg,
        "history_7d": history_7d,
        # 保留供未來校準
        "historical_percentile": None,
    }

    print(
        f"✅ 期貨籌碼: date={source_date} "
        f"外資 long={foreign_long:,} short={foreign_short:,} net={foreign_net:,} "
        f"({foreign_net_level}) 1d_chg={foreign_net_chg_1d:+,} "
        f"action={foreign_action}"
    )

    return result


# --------------------------------------------------
# 資料層 B：現貨三大法人（大盤情緒）
# --------------------------------------------------

@safe_execute
def fetch_spot_institutional() -> Optional[dict]:
    """
    抓取現貨三大法人買賣超（TAIEX 大盤）。

    回傳欄位：
      spot_foreign_net_buy_bn   外資買超（億元）
      spot_foreign_5d_sum_bn    近5日累計買超
      spot_trust_net_buy_bn     投信買超
      spot_source_date
    """

    api = _get_api()

    # TaiwanStockTotalInstitutionalInvestors：全市場三大法人合計，不需要 data_id
    # TaiwanStockInstitutionalInvestorsBuySell：需要個股代號，大盤 data_id 未公開
    # → 優先用 TaiwanStockTotalInstitutionalInvestors
    df = None
    dataset_used = None

    for dataset, kwargs in [
        ("TaiwanStockTotalInstitutionalInvestors", {}),
        ("TaiwanStockInstitutionalInvestorsBuySell", {"data_id": "TAIEX"}),
        ("TaiwanStockInstitutionalInvestorsBuySell", {"data_id": "IX0001"}),
    ]:
        try:
            df = api.get_data(
                dataset=dataset,
                start_date=_start_date(20),
                **kwargs,
            )
            if df is not None and not df.empty:
                dataset_used = dataset
                print(f"✅ 現貨法人資料抓取成功 ({dataset})")
                print(f"   欄位: {df.columns.tolist()}")
                break
            df = None
        except Exception as e:
            print(f"⚠️ {dataset} {kwargs} 失敗：{str(e)[:100]}")
            df = None

    if df is None or df.empty:
        print("⚠️ 現貨三大法人：所有嘗試均失敗")
        return None

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df = df.sort_values("date")

    # 欄位偵測：不同 dataset 結構不同，動態處理
    name_col = _find_col(df, ["institutional_investors", "name", "investor_type"])
    buy_col  = _find_col(df, ["buy", "buy_volume", "foreign_buy"])
    sell_col = _find_col(df, ["sell", "sell_volume", "foreign_sell"])
    net_col  = _find_col(df, ["net_buy", "foreign_net_buy", "net"])

    # 建立 "net" 欄位
    if buy_col and sell_col:
        df[buy_col]  = pd.to_numeric(df[buy_col],  errors="coerce").fillna(0)
        df[sell_col] = pd.to_numeric(df[sell_col], errors="coerce").fillna(0)
        df["net"] = df[buy_col] - df[sell_col]
    elif net_col:
        df[net_col] = pd.to_numeric(df[net_col], errors="coerce").fillna(0)
        df["net"] = df[net_col]
    else:
        print(f"⚠️ 現貨法人欄位無法解析：{df.columns.tolist()}")
        return None

    trust_df = pd.DataFrame()

    # 有身份別欄位：依外資/投信拆分
    if name_col and name_col in df.columns:
        foreign_mask = df[name_col].astype(str).str.contains(
            "Foreign|外資|陸資", case=False, na=False
        )
        trust_mask = df[name_col].astype(str).str.contains(
            "Investment|投信", case=False, na=False
        )
        foreign_df = (
            df[foreign_mask]
            .groupby("date").agg(net=("net", "sum"))
            .reset_index().sort_values("date")
        )
        trust_df = (
            df[trust_mask]
            .groupby("date").agg(net=("net", "sum"))
            .reset_index()
        )
    else:
        # TaiwanStockTotalInstitutionalInvestors 可能直接是合計，無身份別
        # 以全部合計當外資（外資是最大組成）
        print("⚠️ 現貨法人無身份別欄位，使用全市場合計")
        foreign_df = (
            df.groupby("date").agg(net=("net", "sum"))
            .reset_index().sort_values("date")
        )

    if foreign_df.empty:
        print("⚠️ 現貨外資資料空")
        return None

    latest_date = foreign_df.iloc[-1]["date"]
    source_date = str(latest_date.date())

    # 金額單位：FinMind TaiwanStockTotalInstitutionalInvestors 的 buy/sell 單位是「元」
    # 換算億元：除以 1e8
    # （注意：部分個股資料集用千元，但 TotalInstitutionalInvestors 用元）
    raw_net = _safe_float(foreign_df.iloc[-1]["net"])
    divisor = 1e8

    spot_foreign_net_buy_bn = round(raw_net / divisor, 2)
    spot_foreign_5d_sum_bn  = round(
        foreign_df.tail(5)["net"].sum() / divisor, 2
    )

    spot_trust_net_buy_bn = 0.0
    if not trust_df.empty and "date" in trust_df.columns:
        trust_latest = trust_df[trust_df["date"] == latest_date]
        if not trust_latest.empty:
            spot_trust_net_buy_bn = round(
                _safe_float(trust_latest.iloc[-1]["net"]) / divisor, 2
            )

    result = {
        "spot_source_date": source_date,
        "spot_foreign_net_buy_bn": spot_foreign_net_buy_bn,
        "spot_foreign_5d_sum_bn": spot_foreign_5d_sum_bn,
        "spot_trust_net_buy_bn": spot_trust_net_buy_bn,
    }

    print(
        f"✅ 現貨籌碼: date={source_date} "
        f"外資={spot_foreign_net_buy_bn:+.1f}億 "
        f"5日累計={spot_foreign_5d_sum_bn:+.1f}億 "
        f"投信={spot_trust_net_buy_bn:+.1f}億"
    )

    return result


# --------------------------------------------------
# 資料層 C：選擇權 OI（上下限結構）
# --------------------------------------------------

@safe_execute
def fetch_option_oi(reference_price: Optional[float] = None) -> Optional[dict]:
    """
    抓取近月 TXO 選擇權所有履約價 OI。

    計算：
      call_wall_strike / call_wall_oi
      put_wall_strike  / put_wall_oi
      call_put_ratio   全市場 Call/Put OI 比
      max_pain_strike  Max Pain（每日盤後計算）
      nearby_call_oi   現價附近 Call OI 合計
      nearby_put_oi    現價附近 Put OI 合計
      price_position_pct  現價在 Put wall~Call wall 的位置%
      zone_width       Call wall - Put wall
    """

    api = _get_api()

    df = api.get_data(
        dataset="TaiwanOptionDaily",
        data_id=OPTION_SYMBOL,
        start_date=_start_date(10),
    )

    if df is None or df.empty:
        print("⚠️ TaiwanOptionDaily 無資料")
        return None

    df = df.copy()

    # 欄位偵測
    date_col = _find_col(df, ["date", "日期"])
    strike_col = _find_col(df, ["strike_price", "strike", "履約價",
                                 "delivery_price", "exercise_price"])
    type_col = _find_col(df, ["call_put", "option_type", "put_call",
                               "買賣權", "買賣權別"])
    oi_col = _find_col(df, ["open_interest", "open_interest_volume",
                             "open_interest_balance_volume", "未沖銷契約量"])
    contract_col = _find_col(df, ["contract_date", "到期月份",
                                   "到期月份(週別)", "delivery_month"])

    for col_name, col_val in [("date", date_col), ("strike", strike_col),
                               ("type", type_col), ("oi", oi_col)]:
        if not col_val:
            print(f"⚠️ TaiwanOptionDaily 缺少{col_name}欄位")
            return None

    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df[strike_col] = pd.to_numeric(df[strike_col], errors="coerce")
    df[oi_col] = pd.to_numeric(df[oi_col], errors="coerce")
    df["cp"] = df[type_col].apply(_normalize_option_type)

    df = df.dropna(subset=[date_col, strike_col, oi_col, "cp"])
    df = df[(df[strike_col] > 0) & (df[oi_col] > 0)]

    latest_date = df[date_col].max()
    df = df[df[date_col] == latest_date].copy()

    # 取近月合約（contract_date 最小的有效月份）
    if contract_col and contract_col in df.columns:
        valid_contracts = (
            df[contract_col].dropna().astype(str)
            .str.replace("/", "", regex=False)
            .sort_values().unique().tolist()
        )
        # 過濾掉週選（通常含 W）
        monthly = [c for c in valid_contracts if "W" not in c.upper()]
        if monthly:
            near_contract = monthly[0]
            df = df[df[contract_col].astype(str).str.replace("/", "") == near_contract].copy()

    call_df = df[df["cp"] == "CALL"].copy()
    put_df = df[df["cp"] == "PUT"].copy()

    if call_df.empty or put_df.empty:
        print("⚠️ 選擇權 Call/Put OI 資料不完整")
        return None

    # 若沒有傳入 reference_price，從 state 取
    if not reference_price:
        state = load_state()
        reference_price = _safe_float(
            state.get("day_session_close") or state.get("price"), 0.0
        )

    # ---- Call wall / Put wall（近價範圍）----
    if reference_price > 0:
        nearby_call = call_df[
            (call_df[strike_col] >= reference_price) &
            (call_df[strike_col] <= reference_price + OI_NEARBY_WINDOW)
        ]
        nearby_put = put_df[
            (put_df[strike_col] >= reference_price - OI_NEARBY_WINDOW) &
            (put_df[strike_col] <= reference_price)
        ]
    else:
        nearby_call = call_df
        nearby_put = put_df

    nearby_call = nearby_call if not nearby_call.empty else call_df
    nearby_put = nearby_put if not nearby_put.empty else put_df

    call_wall_row = nearby_call.sort_values(oi_col, ascending=False).iloc[0]
    put_wall_row = nearby_put.sort_values(oi_col, ascending=False).iloc[0]

    call_wall_strike = round(float(call_wall_row[strike_col]))
    call_wall_oi = int(call_wall_row[oi_col])
    put_wall_strike = round(float(put_wall_row[strike_col]))
    put_wall_oi = int(put_wall_row[oi_col])

    # ---- Call/Put Ratio ----
    total_call_oi = int(call_df[oi_col].sum())
    total_put_oi = int(put_df[oi_col].sum())
    call_put_ratio = round(total_call_oi / total_put_oi, 3) if total_put_oi > 0 else 1.0

    # ---- nearby 合計 OI ----
    nearby_call_oi = int(nearby_call[oi_col].sum()) if not nearby_call.empty else 0
    nearby_put_oi = int(nearby_put[oi_col].sum()) if not nearby_put.empty else 0

    # ---- Max Pain ----
    max_pain_strike = _calc_max_pain(call_df, put_df, strike_col, oi_col)

    # ---- 現價位置% ----
    zone_width = call_wall_strike - put_wall_strike
    price_position_pct = None
    if zone_width > 0 and reference_price > 0:
        price_position_pct = round(
            (reference_price - put_wall_strike) / zone_width * 100, 1
        )

    result = {
        "oi_source_date": str(latest_date.date()),
        "call_wall_strike": call_wall_strike,
        "call_wall_oi": call_wall_oi,
        "put_wall_strike": put_wall_strike,
        "put_wall_oi": put_wall_oi,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "call_put_ratio": call_put_ratio,
        "max_pain_strike": max_pain_strike,
        "nearby_call_oi": nearby_call_oi,
        "nearby_put_oi": nearby_put_oi,
        "price_position_pct": price_position_pct,
        "zone_width": zone_width,
        "current_price_ref": reference_price,
    }

    print(
        f"✅ 選擇權OI: date={latest_date.date()} "
        f"Call wall={call_wall_strike}({call_wall_oi}) "
        f"Put wall={put_wall_strike}({put_wall_oi}) "
        f"C/P ratio={call_put_ratio} "
        f"MaxPain={max_pain_strike} "
        f"位置={price_position_pct}%"
    )

    return result


# --------------------------------------------------
# 資料層 D：台指期日K（技術層）
# --------------------------------------------------

@safe_execute
def fetch_afterhours_institutional() -> Optional[dict]:
    """
    抓取夜盤三大法人成交資料（TaiwanFuturesInstitutionalInvestorsAfterHours）。
    回傳外資和自營商的夜盤淨買賣口數（成交量，非未平倉）。
    """
    api = _get_api()

    df = api.get_data(
        dataset="TaiwanFuturesInstitutionalInvestorsAfterHours",
        data_id="TX",
        start_date=_start_date(3),
    )

    if df is None or df.empty:
        print("⚠️ 夜盤法人資料無法取得")
        return None

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")

    latest_date = df["date"].max()
    df_latest = df[df["date"] == latest_date]

    name_col = _find_col(df_latest, ["institutional_investors", "name", "investor_type"])
    if not name_col:
        print("⚠️ 夜盤法人：找不到身份別欄位")
        return None

    foreign_mask = df_latest[name_col].astype(str).str.contains(
        "Foreign|外資", case=False, na=False
    )
    foreign_df = df_latest[foreign_mask]

    dealer_mask = df_latest[name_col].astype(str).str.contains(
        "Dealer|自營", case=False, na=False
    )
    dealer_df = df_latest[dealer_mask]

    foreign_long  = _safe_int(foreign_df["long_deal_volume"].sum())  if not foreign_df.empty else 0
    foreign_short = _safe_int(foreign_df["short_deal_volume"].sum()) if not foreign_df.empty else 0
    foreign_ah_net = foreign_long - foreign_short

    dealer_long  = _safe_int(dealer_df["long_deal_volume"].sum())  if not dealer_df.empty else 0
    dealer_short = _safe_int(dealer_df["short_deal_volume"].sum()) if not dealer_df.empty else 0
    dealer_ah_net = dealer_long - dealer_short

    result = {
        "ah_source_date":  str(latest_date.date()),
        "foreign_ah_long":  foreign_long,
        "foreign_ah_short": foreign_short,
        "foreign_ah_net":   foreign_ah_net,
        "dealer_ah_net":    dealer_ah_net,
    }

    print(
        f"✅ 夜盤法人: date={latest_date.date()} "
        f"外資淨{'+' if foreign_ah_net >= 0 else ''}{foreign_ah_net:,}口 "
        f"自營商淨{'+' if dealer_ah_net >= 0 else ''}{dealer_ah_net:,}口"
    )

    return result


@safe_execute
def fetch_tech_levels() -> Optional[dict]:
    """
    計算上下限框架的技術層（層C）。

    回傳：
      prev_high, prev_low, prev_close
      pivot, r1, s1, mid_range
      atr_5d, atr_20d
      ma5, ma20
      price_vs_ma5, price_vs_ma20
    """

    api = _get_api()

    df = api.get_data(
        dataset="TaiwanFuturesDaily",
        data_id=FUTURES_SYMBOL,
        start_date=_start_date(60),
    )

    if df is None or df.empty:
        print("⚠️ TaiwanFuturesDaily 無資料")
        return None

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["volume"] = pd.to_numeric(df.get("volume", pd.Series([0]*len(df))), errors="coerce").fillna(0)

    # 主力合約（最大量）
    if "contract_date" in df.columns:
        df["contract_date"] = df["contract_date"].astype(str)
        df = df[~df["contract_date"].str.contains("/", na=False)]

    for col in ["max", "min", "close"]:
        if col not in df.columns:
            print(f"⚠️ TaiwanFuturesDaily 缺少欄位 {col}")
            return None
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["date", "max", "min", "close"])
    df = df[(df["max"] > 0) & (df["min"] > 0) & (df["close"] > 0)]

    # 每日取最大量合約
    if "contract_date" in df.columns:
        df = (
            df.sort_values("volume", ascending=False)
            .groupby("date", as_index=False)
            .first()
        )

    df = df.sort_values("date").reset_index(drop=True)

    if len(df) < 2:
        print("⚠️ TaiwanFuturesDaily 資料不足")
        return None

    # 前一交易日
    prev = df.iloc[-1]
    prev_high = round(float(prev["max"]), 1)
    prev_low = round(float(prev["min"]), 1)
    prev_close = round(float(prev["close"]), 1)
    source_date = str(prev["date"].date())

    # 技術指標
    pivot = round((prev_high + prev_low + prev_close) / 3, 1)
    r1 = round(2 * pivot - prev_low, 1)
    s1 = round(2 * pivot - prev_high, 1)
    mid_range = round((prev_high + prev_low) / 2, 1)

    closes = df["close"].values
    highs = df["max"].values
    lows = df["min"].values

    atr_series = [highs[i] - lows[i] for i in range(len(df))]
    atr_5d = round(float(pd.Series(atr_series).tail(5).mean()), 1)
    atr_20d = round(float(pd.Series(atr_series).tail(20).mean()), 1)

    ma5 = round(float(pd.Series(closes).tail(5).mean()), 1)
    ma20 = round(float(pd.Series(closes).tail(20).mean()), 1)

    price_vs_ma5 = round(prev_close - ma5, 1)
    price_vs_ma20 = round(prev_close - ma20, 1)

    result = {
        "tech_source_date": source_date,
        "prev_high": prev_high,
        "prev_low": prev_low,
        "prev_close": prev_close,
        "pivot": pivot,
        "r1": r1,
        "s1": s1,
        "mid_range": mid_range,
        "atr_5d": atr_5d,
        "atr_20d": atr_20d,
        "ma5": ma5,
        "ma20": ma20,
        "price_vs_ma5": price_vs_ma5,
        "price_vs_ma20": price_vs_ma20,
    }

    print(
        f"✅ 技術層: date={source_date} "
        f"H={prev_high} L={prev_low} C={prev_close} "
        f"pivot={pivot} R1={r1} S1={s1} "
        f"mid={mid_range} ATR5={atr_5d}"
    )

    return result


# --------------------------------------------------
# 資料層 E：CNN 恐懼貪婪指數（市場情緒補充）
# --------------------------------------------------

@safe_execute
def fetch_fear_greed() -> Optional[dict]:
    """
    抓取 CNN Fear & Greed Index。
    Tier: Backer/Sponsor

    回傳：
      fear_greed        : 數值 0~100
      fear_greed_emotion: 文字（Extreme Fear / Fear / Neutral / Greed / Extreme Greed）
      source_date
    """

    api = _get_api()

    df = api.get_data(
        dataset="CnnFearGreedIndex",
        start_date=_start_date(5),
    )

    if df is None or df.empty:
        print("⚠️ CnnFearGreedIndex 無資料（可能需要 Backer 以上方案）")
        return None

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")

    latest = df.iloc[-1]
    result = {
        "fg_source_date": str(latest["date"].date()),
        "fear_greed": _safe_int(latest.get("fear_greed")),
        "fear_greed_emotion": str(latest.get("fear_greed_emotion", "")),
    }

    print(
        f"✅ Fear&Greed: date={result['fg_source_date']} "
        f"index={result['fear_greed']} ({result['fear_greed_emotion']})"
    )

    return result


# --------------------------------------------------
# 資料層 F：大額交易人未沖銷部位（比三大法人更精準）
# --------------------------------------------------

@safe_execute
def fetch_large_traders() -> Optional[dict]:
    """
    抓取台指期大額交易人未沖銷部位（Top5/Top10）。
    Tier: Backer/Sponsor
    dataset: TaiwanFuturesOpenInterestLargeTraders

    大額交易人是真正的主力，比三大法人更能反映市場方向。

    回傳：
      top5_buy_oi   : 前5大多方未沖銷
      top5_sell_oi  : 前5大空方未沖銷
      top5_net      : top5_buy - top5_sell
      top10_buy_oi
      top10_sell_oi
      top10_net
      market_oi     : 市場總未沖銷
      top5_long_pct : 前5大多方佔比%
      top5_short_pct: 前5大空方佔比%
      source_date
    """

    api = _get_api()

    df = api.get_data(
        dataset="TaiwanFuturesOpenInterestLargeTraders",
        data_id="TX",
        start_date=_start_date(10),
    )

    if df is None or df.empty:
        print("⚠️ TaiwanFuturesOpenInterestLargeTraders 無資料（可能需要 Backer 以上方案）")
        return None

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")

    # 保留完整歷史（供計算 top5_net_chg）
    full_df = df.copy()

    # 取最新一日
    latest_date = df["date"].max()
    df = df[df["date"] == latest_date]

    if df.empty:
        print("⚠️ 大額交易人：找不到有效資料列")
        return None

    # 印出結構讓除錯更容易（name 是商品名稱，contract_type 才是合約分類）
    print(f"   大額交易人欄位: {df.columns.tolist()}")
    if "name" in df.columns:
        print(f"   name 值: {df['name'].unique().tolist()}")
    if "contract_type" in df.columns:
        print(f"   contract_type 值: {df['contract_type'].unique().tolist()}")

    # 用 contract_type 選取最具代表性的列
    # 優先：全部月份（all_months / 全部）；其次：近月；fallback：market_open_interest 最大列
    row = None
    if "contract_type" in df.columns:
        all_mask = df["contract_type"].astype(str).str.contains(
            "全部|所有|all|total", case=False, na=False
        )
        if all_mask.any():
            row = df[all_mask].iloc[0]
        else:
            near_mask = df["contract_type"].astype(str).str.contains(
                "近月|near", case=False, na=False
            )
            if near_mask.any():
                row = df[near_mask].iloc[0]

    if row is None:
        if "market_open_interest" in df.columns:
            idx = df["market_open_interest"].apply(_safe_int).idxmax()
            row = df.loc[idx]
        else:
            row = df.iloc[0]

    # 欄位名稱偵測（文件確認：buy_top5_trader_open_interest 等）
    def _get_val(r, candidates):
        for c in candidates:
            if c in r.index and r[c] is not None:
                v = _safe_int(r[c])
                if v != 0:
                    return v
        return 0

    top5_buy  = _get_val(row, ["buy_top5_trader_open_interest",  "buy_top5",  "top5_buy"])
    top5_sell = _get_val(row, ["sell_top5_trader_open_interest", "sell_top5", "top5_sell"])
    top10_buy  = _get_val(row, ["buy_top10_trader_open_interest",  "buy_top10",  "top10_buy"])
    top10_sell = _get_val(row, ["sell_top10_trader_open_interest", "sell_top10", "top10_sell"])
    market_oi  = _get_val(row, ["market_open_interest", "market_oi", "total_open_interest"])

    top5_net  = top5_buy - top5_sell
    top10_net = top10_buy - top10_sell

    # 計算 top5_net 日變化（今日 vs 前一交易日）
    top5_net_chg = 0
    all_dates_sorted = sorted(full_df["date"].unique())
    if len(all_dates_sorted) >= 2:
        prev_date = all_dates_sorted[-2]
        prev_df = full_df[full_df["date"] == prev_date]
        prev_row = None
        if "contract_type" in prev_df.columns:
            all_mask_p = prev_df["contract_type"].astype(str).str.contains(
                "全部|所有|all|total", case=False, na=False
            )
            if all_mask_p.any():
                prev_row = prev_df[all_mask_p].iloc[0]
            else:
                near_mask_p = prev_df["contract_type"].astype(str).str.contains(
                    "近月|near", case=False, na=False
                )
                if near_mask_p.any():
                    prev_row = prev_df[near_mask_p].iloc[0]
        if prev_row is None and not prev_df.empty:
            if "market_open_interest" in prev_df.columns:
                idx_p = prev_df["market_open_interest"].apply(_safe_int).idxmax()
                prev_row = prev_df.loc[idx_p]
            else:
                prev_row = prev_df.iloc[0]
        if prev_row is not None:
            prev_top5_buy = _get_val(prev_row, ["buy_top5_trader_open_interest", "buy_top5", "top5_buy"])
            prev_top5_sell = _get_val(prev_row, ["sell_top5_trader_open_interest", "sell_top5", "top5_sell"])
            top5_net_chg = top5_net - (prev_top5_buy - prev_top5_sell)

    top5_long_pct  = round(top5_buy  / market_oi * 100, 1) if market_oi > 0 else 0
    top5_short_pct = round(top5_sell / market_oi * 100, 1) if market_oi > 0 else 0

    result = {
        "lt_source_date": str(latest_date.date()),
        "top5_buy_oi": top5_buy,
        "top5_sell_oi": top5_sell,
        "top5_net": top5_net,
        "top5_net_chg": top5_net_chg,
        "top10_buy_oi": top10_buy,
        "top10_sell_oi": top10_sell,
        "top10_net": top10_net,
        "market_oi": market_oi,
        "top5_long_pct": top5_long_pct,
        "top5_short_pct": top5_short_pct,
    }

    print(
        f"✅ 大額交易人: date={result['lt_source_date']} "
        f"Top5多={top5_buy:,} 空={top5_sell:,} 淨={top5_net:+,} "
        f"(多{top5_long_pct}%/空{top5_short_pct}%)"
    )

    return result



def calculate_sentiment_score(
    futures_chip: dict,
    spot_chip: dict,
    option_oi: dict,
    tech: dict,
    large_traders: Optional[dict] = None,
    afterhours: Optional[dict] = None,
) -> dict:
    """
    六個分項加總得出 SentimentScore。

    S1  外資期貨方向          -3 ~ +3
    S2  今日動作品質          -1 ~ +1
    S3  現貨外資同向確認      -2 ~ +2
    S4  OI 結構分             -2 ~ +2
    S5  波動率風險            -1 ~  0
    S6  大額交易人方向（選）  -1 ~ +1  （需 Backer+，無資料則跳過）

    總分 -10 ~ +10（有 S6 時），-9 ~ +9（無 S6 時）
    """

    warnings = []

    # ---- S1：外資期貨方向 ----
    # 邏輯：
    #   淨部位的絕對量 + 近3日趨勢方向共同決定
    #   極端淨空（< -30000）即使短期在減少，持倉本身仍是強空訊號
    foreign_net = futures_chip.get("foreign_net", 0)
    chg_3d = futures_chip.get("foreign_net_chg_3d", 0)
    chg_1d = futures_chip.get("foreign_net_chg_1d", 0)

    if foreign_net > 15000 and chg_3d > 0:
        s1 = 3    # 強多且持續加碼
    elif foreign_net > 0 and chg_1d > 0:
        s1 = 2    # 淨多且今日仍在加多
    elif foreign_net > 0:
        s1 = 1    # 淨多但動能減弱
    elif -5000 <= foreign_net <= 5000:
        s1 = 0    # 中性
    elif -15000 <= foreign_net < -5000:
        # 輕度偏空：依3日趨勢決定 -1 或 -2
        s1 = -2 if chg_3d < 0 else -1
    elif -30000 <= foreign_net < -15000:
        # 強空：依3日趨勢決定 -2 或 -3
        s1 = -3 if chg_3d < 0 else -2
    else:
        # 極端空（< -30000）：持倉量本身即強空訊號
        # 即使3日在小幅減少空單，極端量仍給 -3
        s1 = -3

    # ---- S2：今日動作品質 ----
    action = futures_chip.get("foreign_action", "其他")
    if action == "加多減空":
        s2 = 1
    elif action == "加空減多":
        s2 = -1
    else:
        s2 = 0

    # ---- S2 夜盤修正：若外資夜盤大量回補/加碼，微調 S2 ----
    ah = afterhours or {}
    foreign_ah_net = ah.get("foreign_ah_net", 0)
    if foreign_ah_net > 3000:
        s2 = min(s2 + 1, 1)
        warnings.append(
            f"📌 外資夜盤回補 +{foreign_ah_net:,}口，"
            f"空方壓力可能減輕，注意日盤方向轉變"
        )
    elif foreign_ah_net < -3000:
        s2 = max(s2 - 1, -1)
        warnings.append(
            f"⚠️ 外資夜盤加碼空單 {foreign_ah_net:,}口，"
            f"空方壓力持續"
        )

    # ---- S3：現貨同向確認 ----
    spot_bn = spot_chip.get("spot_foreign_net_buy_bn", 0.0)
    # 判斷現/期方向
    futures_bullish = foreign_net > 0 or chg_1d > 1000
    futures_bearish = foreign_net < 0 or chg_1d < -1000

    if spot_bn > 50 and futures_bullish:
        s3 = 2
    elif spot_bn > 0:
        s3 = 1
    elif -10 <= spot_bn <= 10:
        s3 = 0
    elif spot_bn < -50 and futures_bearish:
        s3 = -2
    else:
        s3 = -1

    # 背離警示
    if (spot_bn > 30 and futures_bearish) or (spot_bn < -30 and futures_bullish):
        warnings.append(
            f"⚠️ 現/期方向背離：現貨外資{spot_bn:+.1f}億 vs 期貨{futures_chip.get('foreign_net_level')}"
        )

    # ---- S4：OI 結構分 ----
    # 邏輯：
    #   用 price_position_pct（現價在 Put wall~Call wall 的位置%）
    #   + max_pain 相對現價的方向，判斷大戶希望市場往哪走
    #   不用 pivot（技術指標），因為 OI 結構跟 pivot 是獨立的兩套邏輯
    put_wall = option_oi.get("put_wall_strike", 0)
    call_wall = option_oi.get("call_wall_strike", 0)
    max_pain = option_oi.get("max_pain_strike", 0)
    ref_price = option_oi.get("current_price_ref", 0)
    price_pct = option_oi.get("price_position_pct")  # 0~100，越高越接近Call wall
    cp_ratio = option_oi.get("call_put_ratio", 1.0)

    s4 = 0
    if ref_price > 0 and put_wall > 0 and call_wall > 0:
        zone_w = call_wall - put_wall

        # 條件1：接近 Put wall（下方支撐）且 Max Pain > 現價 → 大戶希望拉高，偏多
        if ref_price < put_wall + 200 and max_pain is not None and max_pain > ref_price:
            s4 = 2

        # 條件2：接近 Call wall（上方壓力）且 Max Pain < 現價 → 大戶希望壓低，偏空
        elif ref_price > call_wall - 200 and max_pain is not None and max_pain < ref_price:
            s4 = -2

        # 條件3：用 price_position_pct 判斷在區間的位置
        elif price_pct is not None:
            if price_pct < 35:
                # 在區間下半段：若 max_pain 在上方則有拉高動能
                s4 = 1 if (max_pain is not None and max_pain > ref_price) else 0
            elif price_pct > 65:
                # 在區間上半段：若 max_pain 在下方則有壓低動能
                s4 = -1 if (max_pain is not None and max_pain < ref_price) else 0
            else:
                s4 = 0  # 中段，方向不明

    # Call/Put Ratio 異常警示
    if cp_ratio > 2.0:
        warnings.append(f"⚠️ Call/Put Ratio={cp_ratio:.2f} 異常偏高，散戶大量追 Call（反指標）")
    elif cp_ratio < 0.5:
        warnings.append(f"⚠️ Call/Put Ratio={cp_ratio:.2f} 異常偏低，散戶大量追 Put（可能軋空）")

    # ---- S5：波動率風險（只扣分）----
    atr_5d = tech.get("atr_5d", 0)
    atr_20d = tech.get("atr_20d", 0)
    s5 = 0
    if atr_20d > 0 and atr_5d > atr_20d * 1.5:
        s5 = -1
        warnings.append(
            f"⚠️ ATR 異常放大：近5日均值={atr_5d} vs 近20日均值={atr_20d}（波動率 {atr_5d/atr_20d:.1f}x）"
        )

    # ---- S6：大額交易人方向（Backer+ 才有）----
    s6 = 0
    lt = large_traders or {}
    top5_net = lt.get("top5_net")
    top5_long_pct = lt.get("top5_long_pct", 0)
    top5_short_pct = lt.get("top5_short_pct", 0)

    if top5_net is not None and lt.get("market_oi", 0) > 0:
        # 大額交易人淨多 + 多方佔比高 → +1
        if top5_net > 0 and top5_long_pct > top5_short_pct:
            s6 = 1
        # 大額交易人淨空 + 空方佔比高 → -1
        elif top5_net < 0 and top5_short_pct > top5_long_pct:
            s6 = -1
        # 大額交易人與三大法人外資方向相反 → 警示
        foreign_net = futures_chip.get("foreign_net", 0)
        if (s6 > 0 and foreign_net < -5000) or (s6 < 0 and foreign_net > 5000):
            warnings.append(
                f"⚠️ 大額交易人({'+' if top5_net > 0 else ''}{top5_net:,}口) vs "
                f"外資期貨({foreign_net:+,}口) 方向相反，市場分歧"
            )

    # ---- 外資極端部位警示 ----
    if abs(foreign_net) > 30000:
        warnings.append(
            f"⚠️ 外資期貨淨部位達極端水平：{foreign_net:+,}口（V1靜態門檻，尚未用歷史百分位校準）"
        )

    # 兩面建倉警示
    long_chg = futures_chip.get("foreign_long_chg", 0)
    short_chg = futures_chip.get("foreign_short_chg", 0)
    if abs(long_chg) > 3000 and abs(short_chg) > 3000:
        warnings.append(
            f"⚠️ 外資兩面建倉：多單{long_chg:+,}口 + 空單{short_chg:+,}口，方向不明確"
        )

    total = s1 + s2 + s3 + s4 + s5 + s6
    total = max(-10, min(10, total))

    bias_label = _score_to_bias(total)

    # 散戶貪婪 + 籌碼偏空警示
    try:
        import json as _json
        with open("chip_cache.json", "r", encoding="utf-8") as _f:
            _cache = _json.load(_f)
        fg_index = int(_cache.get("fear_greed", {}).get("fear_greed", 0) or 0)
    except Exception:
        fg_index = 0

    if fg_index > 60 and total < -2:
        warnings.append(
            f"⚠️ 散戶貪婪指數({fg_index}) + 籌碼偏空({total}分）：短期高點特徵，做多極度謹慎"
        )

    return {
        "s1_futures_direction": s1,
        "s2_action_quality": s2,
        "s3_spot_confirm": s3,
        "s4_oi_structure": s4,
        "s5_volatility_risk": s5,
        "s6_large_traders": s6,
        "total_score": total,
        "bias_label": bias_label,
        "warnings": warnings,
    }


# --------------------------------------------------
# 計算層：外資空單估算成本（層B）
# --------------------------------------------------

def estimate_foreign_cost(futures_chip: dict, tech: dict) -> dict:
    """
    用近5日加權平均收盤粗估外資多/空單的平均持倉成本。

    無法精確，僅作方向性參考。
    """

    ma5 = tech.get("ma5", 0)
    foreign_net = futures_chip.get("foreign_net", 0)

    if ma5 <= 0:
        return {"estimated_cost": None, "cost_note": "資料不足，無法估算"}

    direction = "空" if foreign_net < 0 else "多"
    notional = futures_chip.get("foreign_notional_bn", 0)

    return {
        "estimated_cost": ma5,
        "estimated_direction": direction,
        "estimated_notional_bn": notional,
        "cost_note": (
            f"外資{direction}單估算成本約 {ma5}（近5日均收）"
            f"，持倉規模約 NT${notional:.0f}億"
            f"（V1粗估，僅供參考）"
        ),
    }


# --------------------------------------------------
# 市場對照：指數報酬（供個股相對強弱計算用）
# --------------------------------------------------

@safe_execute
def fetch_index_returns() -> dict:
    """
    取得台灣加權指數近5日漲跌幅。
    供 stock_report_engine 計算個股相對強弱用。
    失敗時回傳空值，不影響主流程。
    """
    api = _get_api()
    start = _start_date(14)
    end = datetime.now().strftime("%Y-%m-%d")
    try:
        df = api.get_data(
            dataset="TaiwanStockPrice",
            data_id="Y9999",
            start_date=start,
            end_date=end,
        )
        if df is None or df.empty:
            return {"taiex_return_5d": None, "taiex_close": None}
        df = df.sort_values("date").tail(6)
        if len(df) < 2:
            return {"taiex_return_5d": None, "taiex_close": None}
        close_now = float(df.iloc[-1]["close"])
        close_5d  = float(df.iloc[0]["close"])
        return_5d = round((close_now - close_5d) / close_5d * 100, 2) if close_5d else 0.0
        try:
            print(f"[index_returns] TAIEX 5d: {return_5d:+.2f}%")
        except Exception:
            pass
        return {
            "taiex_return_5d": return_5d,
            "taiex_close": round(close_now, 0),
            "updated_at": datetime.now().isoformat(),
        }
    except Exception as e:
        try:
            print(f"[index_returns] failed: {e}")
        except Exception:
            pass
        return {"taiex_return_5d": None, "taiex_close": None}


# --------------------------------------------------
# 主函式：統整更新 chip_cache.json
# --------------------------------------------------

def update_chip_cache(reference_price: Optional[float] = None) -> bool:
    """
    統整抓取四個資料集並計算所有指標，寫入 chip_cache.json。

    失敗時不覆蓋舊資料。
    """

    print("🔄 開始更新 chip_cache.json v2...")

    futures_chip  = fetch_futures_institutional()
    spot_chip     = fetch_spot_institutional()
    option_oi     = fetch_option_oi(reference_price)
    tech          = fetch_tech_levels()
    afterhours    = fetch_afterhours_institutional()
    fear_greed    = fetch_fear_greed()       # Backer+，失敗不影響主流程
    large_traders = fetch_large_traders()    # Backer+，失敗不影響主流程
    index_returns = fetch_index_returns()    # 大盤指數報酬，失敗不影響主流程

    # 任何一個主要資料集失敗都記錄但繼續
    if futures_chip is None:
        print("⚠️ 期貨籌碼取得失敗，使用空值")
        futures_chip = _empty_futures_chip()

    if spot_chip is None:
        print("⚠️ 現貨籌碼取得失敗，使用空值")
        spot_chip = _empty_spot_chip()

    if option_oi is None:
        print("⚠️ 選擇權OI取得失敗，使用空值")
        option_oi = _empty_option_oi()

    if tech is None:
        print("⚠️ 技術層取得失敗，使用空值")
        tech = _empty_tech()

    if afterhours is None:
        afterhours = {
            "ah_source_date":  None,
            "foreign_ah_long":  0,
            "foreign_ah_short": 0,
            "foreign_ah_net":   0,
            "dealer_ah_net":    0,
        }

    if fear_greed is None:
        fear_greed = {"fg_source_date": None, "fear_greed": None, "fear_greed_emotion": None}

    if large_traders is None:
        large_traders = {
            "lt_source_date": None,
            "top5_buy_oi": 0, "top5_sell_oi": 0, "top5_net": 0, "top5_net_chg": 0,
            "top10_buy_oi": 0, "top10_sell_oi": 0, "top10_net": 0,
            "market_oi": 0, "top5_long_pct": 0, "top5_short_pct": 0,
        }

    if index_returns is None:
        index_returns = {"taiex_return_5d": None, "taiex_close": None}

    # 計算 SentimentScore（加入大額交易人 + 夜盤資料）
    sentiment = calculate_sentiment_score(
        futures_chip, spot_chip, option_oi, tech, large_traders, afterhours
    )

    # 計算外資成本估算
    foreign_cost = estimate_foreign_cost(futures_chip, tech)

    # 現/期方向標記
    spot_vs_futures = _classify_spot_vs_futures(futures_chip, spot_chip)

    # 結算週警示
    settlement_warning = _check_settlement_warning()

    if settlement_warning:
        sentiment["warnings"].append(settlement_warning)

    cache = {
        "meta": {
            "updated_at": datetime.now().isoformat(),
            "source_dates": {
                "futures":    futures_chip.get("source_date"),
                "spot":       spot_chip.get("spot_source_date"),
                "option_oi":  option_oi.get("oi_source_date"),
                "tech":       tech.get("tech_source_date"),
                "afterhours": afterhours.get("ah_source_date"),
            },
            "version": "v2.0",
            "threshold_note": "V1靜態門檻，待歷史百分位校準後升級",
        },
        "futures_chip": {
            **futures_chip,
            "spot_vs_futures_direction": spot_vs_futures,
        },
        "spot_chip":           spot_chip,
        "option_oi":           option_oi,
        "tech_levels":         tech,
        "afterhours_chip":     afterhours,
        "fear_greed":          fear_greed,
        "large_traders":       large_traders,
        "foreign_cost_estimate": foreign_cost,
        "sentiment":           sentiment,
        "index_returns":       index_returns,
    }

    try:
        with open(CHIP_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

        print(
            f"✅ chip_cache.json v2 更新完成 | "
            f"情緒={sentiment['bias_label']}({sentiment['total_score']:+d}) | "
            f"外資={futures_chip.get('foreign_net',0):+,}口 | "
            f"Call wall={option_oi.get('call_wall_strike')} | "
            f"Put wall={option_oi.get('put_wall_strike')}"
        )

        # 同步更新 atos_state（保持向後相容）
        _sync_to_state(cache)

        return True

    except Exception as e:
        print(f"⚠️ chip_cache.json 寫入失敗：{e}")
        return False


# --------------------------------------------------
# 報告層：build_chip_context()
# --------------------------------------------------

def build_chip_context() -> dict:
    """
    從 chip_cache.json 讀取並整理成 AI 報告可直接使用的結構化資料。

    這個函式是報告引擎和 AI advisor 的唯一入口。
    """

    try:
        with open(CHIP_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        return {"error": "chip_cache.json 讀取失敗"}

    fc   = cache.get("futures_chip", {})
    sc   = cache.get("spot_chip", {})
    oi   = cache.get("option_oi", {})
    tech = cache.get("tech_levels", {})
    sent = cache.get("sentiment", {})
    cost = cache.get("foreign_cost_estimate", {})
    meta = cache.get("meta", {})
    ah   = cache.get("afterhours_chip", {})
    ir   = cache.get("index_returns", {})

    return {
        # 基本資訊
        "updated_at": meta.get("updated_at"),
        "source_dates": meta.get("source_dates", {}),

        # 外資期貨核心
        "foreign_net": fc.get("foreign_net", 0),
        "foreign_net_level": fc.get("foreign_net_level", "未知"),
        "foreign_net_chg_1d": fc.get("foreign_net_chg_1d", 0),
        "foreign_net_chg_3d": fc.get("foreign_net_chg_3d", 0),
        "foreign_action": fc.get("foreign_action", "未知"),
        "foreign_notional_bn": fc.get("foreign_notional_bn", 0),
        "foreign_long": fc.get("foreign_long", 0),
        "foreign_short": fc.get("foreign_short", 0),
        "history_7d": fc.get("history_7d", []),
        "dealer_net": fc.get("dealer_net", 0),
        "dealer_net_chg": fc.get("dealer_net_chg", 0),
        "spot_vs_futures": fc.get("spot_vs_futures_direction", "未知"),

        # 現貨
        "spot_foreign_net_buy_bn": sc.get("spot_foreign_net_buy_bn", 0),
        "spot_foreign_5d_sum_bn": sc.get("spot_foreign_5d_sum_bn", 0),
        "spot_trust_net_buy_bn": sc.get("spot_trust_net_buy_bn", 0),

        # 上下限框架（層A）
        "call_wall": oi.get("call_wall_strike"),
        "call_wall_oi": oi.get("call_wall_oi"),
        "put_wall": oi.get("put_wall_strike"),
        "put_wall_oi": oi.get("put_wall_oi"),
        "call_put_ratio": oi.get("call_put_ratio"),
        "max_pain": oi.get("max_pain_strike"),
        "zone_width": oi.get("zone_width"),
        "price_position_pct": oi.get("price_position_pct"),

        # 外資成本估算（層B）
        "foreign_cost_estimate": cost.get("estimated_cost"),
        "foreign_cost_note": cost.get("cost_note"),

        # 技術層（層C）
        "prev_high": tech.get("prev_high"),
        "prev_low": tech.get("prev_low"),
        "prev_close": tech.get("prev_close"),
        "pivot": tech.get("pivot"),
        "r1": tech.get("r1"),
        "s1": tech.get("s1"),
        "mid_range": tech.get("mid_range"),
        "atr_5d": tech.get("atr_5d"),
        "atr_20d": tech.get("atr_20d"),
        "ma5": tech.get("ma5"),
        "ma20": tech.get("ma20"),

        # 情緒評分
        "sentiment_score": sent.get("total_score", 0),
        "sentiment_bias": sent.get("bias_label", "中性"),
        "sentiment_detail": {
            "s1_futures": sent.get("s1_futures_direction"),
            "s2_action": sent.get("s2_action_quality"),
            "s3_spot": sent.get("s3_spot_confirm"),
            "s4_oi": sent.get("s4_oi_structure"),
            "s5_vol": sent.get("s5_volatility_risk"),
            "s6_large_traders": sent.get("s6_large_traders"),
        },
        "warnings": sent.get("warnings", []),

        # CNN 恐懼貪婪指數（Backer+）
        "fear_greed_index": cache.get("fear_greed", {}).get("fear_greed"),
        "fear_greed_emotion": cache.get("fear_greed", {}).get("fear_greed_emotion"),

        # 大額交易人（Backer+）
        "lt_top5_net":       cache.get("large_traders", {}).get("top5_net"),
        "lt_top5_net_chg":   cache.get("large_traders", {}).get("top5_net_chg", 0),
        "lt_top5_long_pct":  cache.get("large_traders", {}).get("top5_long_pct"),
        "lt_top5_short_pct": cache.get("large_traders", {}).get("top5_short_pct"),
        "lt_market_oi":      cache.get("large_traders", {}).get("market_oi"),

        # 夜盤法人（成交量方向，非未平倉）
        "foreign_ah_net":   ah.get("foreign_ah_net", 0),
        "foreign_ah_long":  ah.get("foreign_ah_long", 0),
        "foreign_ah_short": ah.get("foreign_ah_short", 0),
        "dealer_ah_net":    ah.get("dealer_ah_net", 0),
        "ah_source_date":   ah.get("ah_source_date"),

        # 大盤指數報酬（供個股相對強弱計算用）
        "taiex_return_5d":  ir.get("taiex_return_5d"),
        "taiex_close":      ir.get("taiex_close"),
    }


def load_chip_cache() -> dict:
    """
    載入 chip_cache.json。

    向後相容：回傳值在 v2 巢狀結構之外，
    額外注入舊版扁平欄位，讓 behavior_engine / monitor_engine 等
    舊模組繼續用 chip.get('call_wall') 也能拿到值。
    """
    try:
        with open(CHIP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}

    # v2 結構偵測
    if "meta" not in data:
        # 舊版 v1 cache，直接回傳
        return data

    # 注入向後相容的扁平欄位
    oi = data.get("option_oi", {})
    meta = data.get("meta", {})
    fc = data.get("futures_chip", {})
    sent = data.get("sentiment", {})

    data["updated_at"] = meta.get("updated_at")           # behavior_engine 用
    data["call_wall"] = oi.get("call_wall_strike")        # behavior_engine 用
    data["put_support"] = oi.get("put_wall_strike")       # behavior_engine 用
    data["retail_ratio"] = 0                               # 已不追蹤，固定 0
    data["foreign_net"] = fc.get("foreign_net", 0)        # 舊版欄位
    data["sentiment_score"] = sent.get("total_score", 0)  # 補充

    return data


def describe_chip_background(chip: dict | None = None) -> dict:
    """
    向後相容：給舊版 preopen_report_engine 使用。
    直接呼叫 build_chip_context()。
    """
    ctx = build_chip_context()
    foreign_net = ctx.get("foreign_net", 0)
    sentiment = ctx.get("sentiment_bias", "中性")

    return {
        "foreign_bias": f"外資期貨{ctx.get('foreign_net_level','未知')} {foreign_net:+,}口",
        "foreign_bias_detail": ctx.get("foreign_action", ""),
        "call_wall": ctx.get("call_wall"),
        "put_support": ctx.get("put_wall"),
        "sentiment_score": ctx.get("sentiment_score", 0),
        "sentiment_bias": sentiment,
        "warnings": ctx.get("warnings", []),
        "updated_at": ctx.get("updated_at"),
        # 舊版相容欄位
        "retail_bias": "（V2 不再追蹤散戶比）",
        "retail_desc": "",
    }


# --------------------------------------------------
# 工具函式
# --------------------------------------------------

def _find_col(df: pd.DataFrame, candidates: list) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _normalize_option_type(value) -> Optional[str]:
    text = str(value).strip().upper()
    if text in ("CALL", "C", "買權"):
        return "CALL"
    if text in ("PUT", "P", "賣權"):
        return "PUT"
    if "CALL" in text:
        return "CALL"
    if "PUT" in text:
        return "PUT"
    return None


def _classify_action(long_chg: int, short_chg: int) -> str:
    """依多空單增減分類外資今日動作。"""
    if long_chg > 0 and short_chg < 0:
        return "加多減空"
    if long_chg < 0 and short_chg > 0:
        return "加空減多"
    if long_chg > 0 and short_chg > 0:
        return "兩面建倉"
    if long_chg < 0 and short_chg < 0:
        return "兩面減倉"
    if long_chg > 0:
        return "加多"
    if long_chg < 0:
        return "減多"
    if short_chg > 0:
        return "加空"
    if short_chg < 0:
        return "減空"
    return "無明顯變化"


def _classify_net_level(net: int) -> str:
    """V1 靜態門檻（待歷史百分位校準後升級）。"""
    if net > 30000:
        return "極強多"
    if net > 15000:
        return "強多"
    if net > 5000:
        return "偏多"
    if net >= -5000:
        return "中性"
    if net >= -15000:
        return "偏空"
    if net >= -30000:
        return "強空"
    return "極強空"


def _score_to_bias(score: int) -> str:
    # 範圍 -10 ~ +10（有 S6 大額交易人時）
    if score >= 6:
        return "強多"
    if score >= 3:
        return "偏多"
    if score >= -2:
        return "中性"
    if score >= -5:
        return "偏空"
    return "強空"


def _calc_max_pain(
    call_df: pd.DataFrame,
    put_df: pd.DataFrame,
    strike_col: str,
    oi_col: str,
) -> Optional[int]:
    """
    計算 Max Pain 履約價。

    Max Pain = 讓所有選擇權買方損失最大（賣方獲益最大）的到期價格。

    做法：對每個履約價，計算若指數到期在此，
    所有 Call OI 和 Put OI 的合計虧損，取最小值對應的履約價。
    """
    try:
        all_strikes = sorted(
            set(call_df[strike_col].dropna().tolist() +
                put_df[strike_col].dropna().tolist())
        )

        if not all_strikes:
            return None

        call_map = dict(zip(call_df[strike_col], call_df[oi_col]))
        put_map = dict(zip(put_df[strike_col], put_df[oi_col]))

        min_pain = float("inf")
        max_pain_strike = None

        for expiry in all_strikes:
            # Call 買方損失（若到期低於履約價，損失全部）
            call_pain = sum(
                max(0, float(expiry) - float(s)) * float(oi)
                for s, oi in call_map.items()
            )
            # Put 買方損失（若到期高於履約價，損失全部）
            put_pain = sum(
                max(0, float(s) - float(expiry)) * float(oi)
                for s, oi in put_map.items()
            )
            total_pain = call_pain + put_pain
            if total_pain < min_pain:
                min_pain = total_pain
                max_pain_strike = int(expiry)

        return max_pain_strike

    except Exception as e:
        print(f"⚠️ Max Pain 計算失敗：{e}")
        return None


def _classify_spot_vs_futures(fc: dict, sc: dict) -> str:
    spot_bn = sc.get("spot_foreign_net_buy_bn", 0)
    foreign_net = fc.get("foreign_net", 0)
    if spot_bn > 10 and foreign_net > 0:
        return "同向多"
    if spot_bn < -10 and foreign_net < 0:
        return "同向空"
    if (spot_bn > 10 and foreign_net < 0) or (spot_bn < -10 and foreign_net > 0):
        return "背離"
    return "中性"


def _check_settlement_warning() -> Optional[str]:
    """若距結算日 ≤ 3 個交易日，發出警示。"""
    try:
        from settlement_engine import get_days_to_settlement
        days = get_days_to_settlement()
        if days is not None and 0 < days <= 3:
            return f"⚠️ 結算日前 {days} 個交易日：大戶可能製造假動作，建議降低倉位或嚴格篩選訊號"
    except Exception:
        pass
    return None


def _sync_to_state(cache: dict) -> None:
    """同步關鍵欄位到 atos_state.json（向後相容）。"""
    try:
        state = load_state()
        fc = cache.get("futures_chip", {})
        oi = cache.get("option_oi", {})
        sent = cache.get("sentiment", {})

        state["chip_ready"] = True
        state["chip_date"] = fc.get("source_date")
        state["foreign_futures_net"] = fc.get("foreign_net", 0)
        state["foreign_long_open_interest"] = fc.get("foreign_long", 0)
        state["foreign_short_open_interest"] = fc.get("foreign_short", 0)
        state["institutional_sentiment"] = (
            f"{sent.get('bias_label','?')}，"
            f"外資期貨淨{fc.get('foreign_net_level','?')} "
            f"{fc.get('foreign_net',0):+,}口 "
            f"(情緒分{sent.get('total_score',0):+d})"
        )
        state["sentiment"] = state["institutional_sentiment"]
        state["chip_source"] = "FinMind v2"
        state["last_chip_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # OI
        state["option_oi_ready"] = oi.get("oi_source_date") is not None
        state["option_oi_date"] = oi.get("oi_source_date")
        state["call_pressure"] = oi.get("call_wall_strike")
        state["put_support"] = oi.get("put_wall_strike")
        state["max_call_strike"] = oi.get("call_wall_strike")
        state["max_put_strike"] = oi.get("put_wall_strike")
        state["max_call_oi"] = oi.get("call_wall_oi")
        state["max_put_oi"] = oi.get("put_wall_oi")
        state["last_option_oi_update"] = state["last_chip_update"]

        # 情緒分同步
        state["sentiment_score"] = sent.get("total_score", 0)
        state["sentiment_bias"] = sent.get("bias_label", "中性")

        save_state(state)

    except Exception as e:
        print(f"⚠️ _sync_to_state 失敗：{e}")


# --------------------------------------------------
# 空值預設（任一資料集取得失敗時）
# --------------------------------------------------

def _empty_futures_chip() -> dict:
    return {
        "source_date": None,
        "foreign_long": 0, "foreign_short": 0, "foreign_net": 0,
        "foreign_net_chg_1d": 0, "foreign_net_chg_3d": 0, "foreign_net_chg_5d": 0,
        "foreign_long_chg": 0, "foreign_short_chg": 0,
        "foreign_action": "資料缺失",
        "foreign_net_level": "資料缺失",
        "foreign_notional_bn": 0,
        "dealer_net": 0, "dealer_net_chg": 0,
        "history_7d": [],
        "historical_percentile": None,
    }


def _empty_spot_chip() -> dict:
    return {
        "spot_source_date": None,
        "spot_foreign_net_buy_bn": 0,
        "spot_foreign_5d_sum_bn": 0,
        "spot_trust_net_buy_bn": 0,
    }


def _empty_option_oi() -> dict:
    return {
        "oi_source_date": None,
        "call_wall_strike": None, "call_wall_oi": None,
        "put_wall_strike": None, "put_wall_oi": None,
        "total_call_oi": 0, "total_put_oi": 0,
        "call_put_ratio": 1.0,
        "max_pain_strike": None,
        "nearby_call_oi": 0, "nearby_put_oi": 0,
        "price_position_pct": None,
        "zone_width": 0,
        "current_price_ref": 0,
    }


def _empty_tech() -> dict:
    return {
        "tech_source_date": None,
        "prev_high": 0, "prev_low": 0, "prev_close": 0,
        "pivot": 0, "r1": 0, "s1": 0, "mid_range": 0,
        "atr_5d": 0, "atr_20d": 0,
        "ma5": 0, "ma20": 0,
        "price_vs_ma5": 0, "price_vs_ma20": 0,
    }


# --------------------------------------------------
# CLI 測試入口
# --------------------------------------------------

if __name__ == "__main__":
    print("=== chip_data_engine v2 手動測試 ===\n")

    success = update_chip_cache()
    if success:
        print("\n--- build_chip_context() ---")
        ctx = build_chip_context()
        for k, v in ctx.items():
            if k not in ("history_7d", "sentiment_detail", "warnings"):
                print(f"  {k}: {v}")
        print(f"  history_7d: {ctx.get('history_7d')}")
        print(f"  warnings: {ctx.get('warnings')}")
