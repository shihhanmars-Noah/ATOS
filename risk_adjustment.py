# risk_adjustment.py
#
# 大戶行為模型調整器。
# 不覆蓋主訊號，只調整 confidence 和產生 warnings/risk_flags。

from datetime import datetime
from typing import Optional


def apply_big_player_adjustments(
    current_price: float,
    foreign_net: int,
    foreign_net_chg_1d: int,
    foreign_cost_estimate: Optional[float],
    top5_net: int,
    top5_net_chg: int,
    put_wall: float,
    call_wall: float,
    days_to_settlement: int,
    fear_greed: int,
    sentiment_score: int,
    signal_time: datetime,
    signal_type: str,         # 'LONG' / 'SHORT' / 'NEUTRAL'
    volume_confirmed: bool,
) -> dict:
    """
    大戶行為模型調整器。
    不覆蓋主訊號，只調整 confidence 和產生 warnings/risk_flags。

    回傳：
    {
        confidence_adjustment: int,   # 信心分調整值（正負整數）
        warnings: list,               # 警示訊息列表
        squeeze_risk: str,            # 'HIGH' / 'MEDIUM' / 'LOW'
        false_breakout_risk: str,     # 'HIGH' / 'MEDIUM' / 'LOW'
        settlement_pressure: str,     # 'HIGH' / 'MEDIUM' / 'LOW'
        big_player_interpretation: str # 一行白話解讀
    }
    """

    adjustment = 0
    warnings = []
    squeeze_risk = 'LOW'
    false_breakout_risk = 'LOW'
    settlement_pressure = 'LOW'

    # --- 修正一：時段過濾 ---
    hour = signal_time.hour
    minute = signal_time.minute
    time_val = hour * 60 + minute

    is_low_liquidity = (
        (11 * 60 + 30 <= time_val <= 13 * 60) or   # 午盤
        (15 * 60 <= time_val <= 16 * 60)             # 夜盤開盤
    )
    if is_low_liquidity:
        adjustment -= 1
        false_breakout_risk = 'HIGH'
        warnings.append("⚠️ 低流動性時段突破，假突破風險較高，需等待二次確認與量能延續")

    # --- 修正二：外資成本警示 ---
    if foreign_cost_estimate and current_price:
        below_cost = current_price < foreign_cost_estimate
        foreign_covering = foreign_net_chg_1d > 1000  # 外資今日回補空單

        if below_cost and signal_type == 'SHORT':
            adjustment -= 1
            warnings.append(
                f"⚠️ 外資空單已獲利（成本估算 {foreign_cost_estimate}，現價 {current_price}），"
                f"注意回補/軋空風險，空方信心分 -1"
            )
            if foreign_covering:
                adjustment -= 1
                squeeze_risk = 'HIGH'
                warnings.append(
                    f"⚠️ 外資空單獲利 + 今日開始回補（{foreign_net_chg_1d:+,}口），"
                    f"軋空風險升高，空方再降 -1"
                )
            else:
                squeeze_risk = 'MEDIUM'

    # --- 修正三：結算日邏輯 ---
    if days_to_settlement is not None:
        if days_to_settlement <= 3:
            settlement_pressure = 'HIGH'
            if signal_type == 'SHORT' and current_price <= put_wall + 100:
                # 結算前3天跌破 Put wall，大戶護盤機率高
                adjustment -= 2
                false_breakout_risk = 'HIGH'
                warnings.append(
                    f"⚠️ 結算前{days_to_settlement}天跌破 Put wall，"
                    f"賣方護盤誘因極高，假跌破機率大，"
                    f"若快速站回 {put_wall} 改為反彈做多機會"
                )
            elif signal_type == 'LONG' and current_price >= call_wall - 100:
                adjustment -= 2
                false_breakout_risk = 'HIGH'
                warnings.append(
                    f"⚠️ 結算前{days_to_settlement}天突破 Call wall，"
                    f"賣方壓制誘因極高，假突破機率大"
                )
        elif days_to_settlement <= 7:
            settlement_pressure = 'MEDIUM'
            adjustment -= 1
            warnings.append(
                f"⚠️ 結算前{days_to_settlement}天，"
                f"Put wall/Call wall 吸力增強，突破訊號信心分 -1"
            )

    # --- 修正四：散戶貪婪警示 ---
    if fear_greed and fear_greed > 60 and sentiment_score <= -3:
        if signal_type == 'LONG':
            adjustment -= 1
            warnings.append(
                f"⚠️ 散戶貪婪({fear_greed}) + 籌碼偏空({sentiment_score}分），"
                f"短線高點特徵，多方信心分 -1，追多需極度謹慎"
            )

    # --- 修正五：大額交易人軋空訊號 ---
    if top5_net is not None and top5_net_chg is not None:
        top5_adding_long = top5_net_chg > 500     # 大額交易人增加多單
        top5_reducing_long = top5_net_chg < -500
        foreign_covering = foreign_net_chg_1d > 1000   # 外資回補空單
        foreign_adding_short = foreign_net_chg_1d < -1000  # 外資加空

        if top5_adding_long and foreign_covering:
            # 大額多單增加 + 外資回補 = 軋空風險
            squeeze_risk = 'HIGH'
            if signal_type == 'SHORT':
                adjustment -= 1
                warnings.append(
                    f"⚠️ 軋空風險升高：大額交易人加多 + 外資回補空單同步，"
                    f"空方信心分 -1"
                )
        elif top5_reducing_long and foreign_adding_short:
            # 大額減多 + 外資加空 = 空方共振
            if signal_type == 'SHORT':
                adjustment += 1
                warnings.append(
                    f"✅ 空方共振：大額交易人減多 + 外資加空，"
                    f"空方信心分 +1"
                )
        elif top5_adding_long and foreign_adding_short:
            # 大額加多 + 外資加空 = 多空分歧
            warnings.append(
                f"⚠️ 多空分歧：大額交易人加多但外資同步加空，"
                f"可能為避險對作，方向不明"
            )

    # --- 綜合解讀 ---
    big_player_interpretation = _build_interpretation(
        squeeze_risk, false_breakout_risk, settlement_pressure,
        foreign_covering=(foreign_net_chg_1d > 1000),
        days_to_settlement=days_to_settlement,
    )

    return {
        'confidence_adjustment': max(-3, min(2, adjustment)),  # 限制在 -3 到 +2
        'warnings': warnings,
        'squeeze_risk': squeeze_risk,
        'false_breakout_risk': false_breakout_risk,
        'settlement_pressure': settlement_pressure,
        'big_player_interpretation': big_player_interpretation,
    }


def _build_interpretation(
    squeeze_risk: str,
    false_breakout_risk: str,
    settlement_pressure: str,
    foreign_covering: bool,
    days_to_settlement: int,
) -> str:
    """產生一行白話大戶行為解讀。"""

    if squeeze_risk == 'HIGH':
        return "軋空風險升高，外資和本土主力方向出現分歧，空方需謹慎"

    if false_breakout_risk == 'HIGH' and settlement_pressure == 'HIGH':
        return f"結算前{days_to_settlement}天護盤誘因強，突破訊號假動作機率高"

    if false_breakout_risk == 'HIGH':
        return "低流動性時段假突破機率高，建議等主力時段確認後再操作"

    if foreign_covering:
        return "外資開始回補空單，方向可能轉變，追空需降低部位"

    return "無特殊異常，依主訊號操作"
