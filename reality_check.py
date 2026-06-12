# reality_check.py

from datetime import datetime


class RealityCheckLayer:
    """
    ATOS 系統最高指揮官：現實核對層
    所有報告生成前強制執行，輸出 MarketState 元數據
    """

    def __init__(self, state: dict, chip_ctx: dict, night_data: dict):
        self.current_price = float(state.get('price', 0) or 0)
        self.call_wall = float(chip_ctx.get('call_wall', 0) or 0)
        self.put_wall = float(chip_ctx.get('put_wall', 0) or 0)
        self.atr_5d = float(chip_ctx.get('atr_5d', 1000) or 1000)
        self.night_chg_pct = float(night_data.get('night_chg_pct', 0) or 0) / 100
        self.night_high = float(night_data.get('night_high', 0) or 0)
        self.night_low = float(night_data.get('night_low', 0) or 0)
        self.night_close = float(night_data.get('night_close', 0) or 0)
        self.night_range = self.night_high - self.night_low
        self.prev_close = float(state.get('prev_close', 0) or 0)
        self.pivot = float(state.get('pivot', 0) or 0)
        self.foreign_net = int(chip_ctx.get('foreign_net', 0) or 0)
        self.foreign_net_chg = int(chip_ctx.get('foreign_net_chg_1d', 0) or 0)
        self.sentiment_score = int(chip_ctx.get('sentiment_score', 0) or 0)
        self.chip_date = (chip_ctx.get('source_dates') or {}).get('futures', '')
        self.today = datetime.now().strftime('%Y-%m-%d')

    def execute(self) -> dict:
        """
        執行現實核對，回傳 MarketState 元數據。
        這是所有報告生成的第一步。
        """

        # ── 檢查一：夜盤是否有極端衝擊 ──
        is_night_shock = (
            abs(self.night_chg_pct) >= 0.015 or
            (self.atr_5d > 0 and self.night_range > self.atr_5d * 0.8)
        )
        night_shock_reason = ""
        if abs(self.night_chg_pct) >= 0.015:
            night_shock_reason = f"夜盤漲跌{self.night_chg_pct*100:+.1f}%超過1.5%"
        elif self.atr_5d > 0 and self.night_range > self.atr_5d * 0.8:
            night_shock_reason = f"夜盤區間{self.night_range:.0f}點超過ATR的80%"

        # ── 檢查二：OI框架防護力 ──
        framework_width = self.call_wall - self.put_wall if self.call_wall > self.put_wall else 0
        atr_ratio = round(framework_width / self.atr_5d, 2) if self.atr_5d > 0 else 0
        is_oi_compressed = (
            framework_width > 0 and
            self.atr_5d > 0 and
            framework_width < self.atr_5d * 1.5
        )

        # ── 檢查三：現價位置 ──
        if framework_width > 0:
            position_pct = (self.current_price - self.put_wall) / framework_width * 100
        else:
            position_pct = 50.0

        is_near_call = (
            self.call_wall > 0 and
            (self.call_wall - self.current_price) < self.atr_5d * 0.3
        )
        is_near_put = (
            self.put_wall > 0 and
            (self.current_price - self.put_wall) < self.atr_5d * 0.3
        )
        is_above_call = self.call_wall > 0 and self.current_price > self.call_wall + 500
        is_below_put = self.put_wall > 0 and self.current_price < self.put_wall - 500

        # ── 檢查四：資料新鮮度 ──
        chip_is_stale = (self.chip_date != self.today)
        chip_staleness = "昨日資料" if chip_is_stale else "今日資料"

        if is_night_shock and chip_is_stale:
            oi_trust = f"⚠️ 信任度低（{chip_staleness}+夜盤大波動，OI牆隨時被貫穿）"
        elif chip_is_stale:
            oi_trust = f"⚠️ 信任度中（{chip_staleness}）"
        else:
            oi_trust = "✅ 信任度高（今日資料）"

        # ── 決定框架模式 ──
        if is_above_call or is_below_put:
            chosen_mode = "MODE_1_OI_INVALID"
            mode_reason = (
                f"現價{self.current_price:.0f}已{'高於Call wall' if is_above_call else '低於Put wall'}"
                f"500點以上，OI框架完全失效"
            )
        elif is_night_shock or is_oi_compressed:
            chosen_mode = "MODE_2_DYNAMIC_MIDZONE"
            reasons = []
            if is_night_shock:
                reasons.append(night_shock_reason)
            if is_oi_compressed:
                reasons.append(f"OI寬度{int(framework_width)}點 < ATR×1.5（{int(self.atr_5d*1.5)}點）")
            mode_reason = "，".join(reasons)
        else:
            chosen_mode = "MODE_3_OI_STANDARD"
            mode_reason = f"OI框架有效（寬度{int(framework_width)}點，ATR×1.5={int(self.atr_5d*1.5)}點）"

        # ── 產生 AI 約束指令 ──
        ai_constraints = self._generate_ai_constraints(
            chosen_mode, is_night_shock,
            is_near_call, is_near_put,
            is_above_call, is_below_put,
        )

        market_state = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "chosen_mode": chosen_mode,
            "mode_reason": mode_reason,
            "audit_metrics": {
                "is_night_shock": is_night_shock,
                "night_shock_reason": night_shock_reason,
                "is_oi_compressed": is_oi_compressed,
                "is_near_call_wall": is_near_call,
                "is_near_put_wall": is_near_put,
                "is_above_call_wall": is_above_call,
                "is_below_put_wall": is_below_put,
                "position_pct": round(position_pct, 1),
                "framework_width": framework_width,
                "atr_ratio": atr_ratio,
                "chip_is_stale": chip_is_stale,
            },
            "data_freshness": {
                "price": "✅ 即時現價",
                "oi_walls": oi_trust,
                "chip": f"{'⚠️' if chip_is_stale else '✅'} {chip_staleness}",
                "night_session": "✅ 夜盤資料（最新）" if self.night_close > 0 else "⚠️ 夜盤資料未取得",
            },
            "ai_constraints": ai_constraints,
            "key_levels": {
                "current_price": self.current_price,
                "call_wall": self.call_wall,
                "put_wall": self.put_wall,
                "pivot": self.pivot,
                "night_high": self.night_high,
                "night_low": self.night_low,
                "atr_5d": self.atr_5d,
            }
        }

        print(f"[RealityCheck] {chosen_mode} | {mode_reason}")

        return market_state

    def _generate_ai_constraints(
        self, mode, is_night_shock,
        is_near_call, is_near_put,
        is_above_call, is_below_put,
    ) -> str:
        constraints = []

        # 基礎約束：永遠適用
        constraints.append(
            f"【最高約束】你只能使用以下系統提供的點位，"
            f"絕對禁止自創數字："
            f"現價={self.current_price:.0f}，"
            f"Call wall={self.call_wall:.0f}，"
            f"Put wall={self.put_wall:.0f}，"
            f"Pivot={self.pivot:.0f}，"
            f"ATR={self.atr_5d:.0f}"
        )

        if mode == "MODE_1_OI_INVALID":
            constraints.append(
                f"【框架失效約束】OI牆已失效，"
                f"禁止使用Call wall/Put wall作為操作依據，"
                f"改用夜盤高低點（高={self.night_high:.0f}/低={self.night_low:.0f}）"
            )

        elif mode == "MODE_2_DYNAMIC_MIDZONE":
            constraints.append(
                f"【中繼戰場約束】高波動環境，"
                f"必須以Pivot（{self.pivot:.0f}）和夜盤高低點為核心，"
                f"禁止使用遠端OI牆作為日內操作目標"
            )

        if is_night_shock:
            direction = "大漲" if self.night_chg_pct > 0 else "大跌"
            constraints.append(
                f"【資料失真約束】夜盤{direction}{abs(self.night_chg_pct)*100:.1f}%，"
                f"昨日所有技術指標已失真，"
                f"禁止說「個股站在5MA上方」等基於昨日收盤的結論，"
                f"禁止說「跌破Put wall {self.put_wall:.0f}」這種遠端廢話"
            )

        if is_near_call:
            constraints.append(
                f"【高位約束】現價{self.current_price:.0f}距Call wall"
                f"{self.call_wall:.0f}不足ATR的30%，"
                f"必須分析高位量縮空的可能性，不得看空太遠"
            )

        if is_near_put:
            constraints.append(
                f"【低位約束】現價{self.current_price:.0f}距Put wall"
                f"{self.put_wall:.0f}不足ATR的30%，"
                f"必須分析Put wall守守能否成功，不得盲目追空"
            )

        if self.foreign_net_chg > 1000:
            constraints.append(
                f"【籌碼約束】外資今日回補{self.foreign_net_chg:+,}口，"
                f"不得說外資持續加空，事實相反"
            )
        elif self.foreign_net_chg < -1000:
            constraints.append(
                f"【籌碼約束】外資今日加碼空單{self.foreign_net_chg:,}口，"
                f"不得說外資可能回補，事實相反"
            )

        return "\n".join(constraints)


def get_market_state(state=None, chip_ctx=None, night_data=None) -> dict:
    """
    便捷函式：取得當前市場狀態。
    所有報告引擎呼叫此函式，不直接使用 RealityCheckLayer。
    """
    from persistent_state import load_state as _load_state
    from chip_data_engine import build_chip_context
    from night_session_engine import get_night_session_data

    if state is None:
        state = _load_state()
    if chip_ctx is None:
        chip_ctx = build_chip_context() or {}
    if night_data is None:
        night_data = get_night_session_data() or {}

    checker = RealityCheckLayer(state, chip_ctx, night_data)
    return checker.execute()
