# monitor_engine.py

import json
import math
from datetime import datetime, time

from error_handler import safe_execute
from session_engine import (
    is_market_open,
    is_opening_cooldown,
    is_no_trade_time,
    is_market_close_transition,
)
from persistent_state import load_state, save_state
from messenger import send_to_telegram
from data_engine import (
    get_realtime_tick,
    get_5min_history,
    is_five_min_close,
    get_latest_close_from_history,
)
from atos_logic import check_invalidation
from strategy_engine import build_strategy_snapshot
from behavior_engine import analyze_behavioral_context
from chip_data_engine import load_chip_cache
from transition_engine import check_session_transition, build_transition_message
from calendar_engine import get_event_mode
from risk_engine import get_risk_protocol
from event_engine import get_event_risk_mode
from target_engine import calculate_trade_plan
from alert_engine_v2 import send_human_alert


class AtosSentinel:
    def __init__(self):
        self.state = load_state()

    # --------------------------------------------------
    # Main Monitor Loop
    # --------------------------------------------------

    @safe_execute
    def monitor_loop(self):
        """
        ATOS 盤中主監控迴圈。

        核心邏輯：
        1. 檢查是否開盤
        2. 取得 TXF 即時價格
        3. 取得 TXF 5分K
        4. 建立策略快照
        5. 5分K 收盤時進行行為判斷與警報
        """

        if not is_market_open():
            return

        tick = get_realtime_tick()

        if not tick:
            print("⚠️ 無法取得即時價格，本輪不判斷")
            return

        current_price = tick["price"]

        event_mode = get_event_mode()
        event_risk = get_event_risk_mode()
        risk_protocol = get_risk_protocol(event_mode)

        self.state["event_mode"] = event_mode
        self.state["event_risk_mode"] = event_risk["mode"]
        self.state["tick_source"] = tick.get("source")
        self.state["tick_time"] = tick.get("time")
        self.state["is_realtime"] = tick.get("is_realtime", False)

        if event_mode == "MARKET_CLOSED":
            self.state["allow_trade"] = False
            save_state(self.state)
            return

        if is_opening_cooldown():
            self.handle_opening_cooldown(current_price)
            save_state(self.state)
            return

        if is_market_close_transition():
            self.state["allow_trade"] = False

        if is_no_trade_time():
            self.state["allow_trade"] = False

        if event_risk["mode"] in ["EVENT_DEFENSE", "EVENT_OBSERVATION"]:
            self.state["allow_trade"] = False

        # --------------------------------------------------
        # 真實 TXF 5 分 K 資料
        # --------------------------------------------------

        df_5min = get_5min_history()

        if df_5min is None or df_5min.empty:
            print("⚠️ 真實 TXF 5分K 無資料，本輪不判斷")
            self.state["allow_trade"] = False
            save_state(self.state)
            return

        latest_k_time = df_5min.iloc[-1]["datetime"]

        if isinstance(latest_k_time, str):
            latest_k_time = datetime.fromisoformat(latest_k_time)

        data_delay_minutes = (
            datetime.now() - latest_k_time
        ).total_seconds() / 60

        self.state["latest_k_time"] = str(latest_k_time)
        self.state["data_delay_minutes"] = round(data_delay_minutes, 1)

        if data_delay_minutes > 10:
            print(
                f"⚠️ 5分K 資料延遲 {round(data_delay_minutes, 1)} 分鐘，本輪不判斷"
            )
            self.state["allow_trade"] = False
            save_state(self.state)
            return

        # --------------------------------------------------
        # 建立策略快照
        # --------------------------------------------------

        snapshot = build_strategy_snapshot(
            price=current_price,
            flip=self.state.get("flip", 0),
            df_history=df_5min,
        )

        if snapshot is None:
            print("⚠️ 策略快照建立失敗，本輪不判斷")
            return

        self.update_state_from_snapshot(snapshot, risk_protocol)

        # 如果即時價格不是 realtime，只允許更新狀態，不允許交易
        if not tick.get("is_realtime", False):
            self.state["allow_trade"] = False

            forbidden = self.state.get("forbidden", [])

            if "即時價格來源非 REALTIME，禁止交易" not in forbidden:
                forbidden.append("即時價格來源非 REALTIME，禁止交易")

            self.state["forbidden"] = forbidden

        if is_five_min_close():
            self.handle_five_min_close(
                current_price=current_price,
                df_5min=df_5min,
                snapshot=snapshot,
            )

        save_state(self.state)

    # --------------------------------------------------
    # Opening Cooldown
    # --------------------------------------------------

    def handle_opening_cooldown(self, open_price: float):
        """
        日盤開盤冷靜期處理。

        優先使用：
        1. night_close：夜盤收盤價
        2. previous_futures_close：前一交易日期貨主力收盤
        3. daily_levels.close：日資料收盤價
        4. flip：最後備援
        """

        if self.state.get("observation_mode"):
            return

        prev_close = self.state.get("night_close")
        prev_close_source = "NIGHT_CLOSE"

        if not prev_close:
            daily_levels = self.state.get("daily_levels", {})
            state_levels = self.state.get("levels", {})

            prev_close = (
                self.state.get("previous_futures_close")
                or daily_levels.get("close")
                or state_levels.get("close")
                or self.state.get("flip")
            )

            prev_close_source = "FUTURES_DAILY_CLOSE_FALLBACK"

        if not prev_close:
            transition = {
                "mode": "NO_PREV_CLOSE",
                "gap": None,
                "action": "無法比對跨盤狀態，日盤開盤先觀察第一根 5 分 K。",
            }

        else:
            transition = check_session_transition(
                prev_close=prev_close,
                open_price=open_price,
                flip=self.state.get("flip", 0),
                atr=self.state.get("atr", 0),
            )

            transition["prev_close_source"] = prev_close_source
            transition["prev_close"] = prev_close

        msg = build_transition_message(transition)
        send_to_telegram(msg)

        self.state["observation_mode"] = True
        self.state["last_transition"] = transition

    # --------------------------------------------------
    # 5 Min Close Handler
    # --------------------------------------------------

    def handle_five_min_close(
        self,
        current_price: float,
        df_5min,
        snapshot: dict,
    ):
        """
        每根 5 分 K 收盤後執行。

        處理：
        1. 冷靜期結束
        2. Flip 失效
        3. Flip 站回 / 跌破
        4. R1 / S1 接近提醒
        5. Trap / Sweep 行為提醒
        6. 夜盤收盤紀錄
        """

        five_min_close = get_latest_close_from_history(df_5min)

        if five_min_close is None:
            return

        # --------------------------------------------------
        # 開盤冷靜期結束
        # --------------------------------------------------

        if self.state.get("observation_mode"):
            self.state["observation_mode"] = False

            send_to_telegram(
                "✅ ATOS 開盤冷靜期結束\n"
                "第一根 5 分 K 已收盤，恢復正常監控。"
            )

        trade_plan = calculate_trade_plan(self.state)

        # --------------------------------------------------
        # Flip 失效檢查：只在狀態首次失效時發一次
        # --------------------------------------------------

        invalidation = check_invalidation(
            five_min_close=five_min_close,
            flip_level=self.state.get("flip", 0),
            current_state=self.state.get("regime", "🟡 中性模式"),
        )

        if invalidation["alert"]:
            previous_flip_alert = self.state.get("previous_flip_alert", False)

            if not previous_flip_alert:
                send_human_alert(
                    self.build_alert_context(
                        event="FLIP_INVALID",
                        price=five_min_close,
                        snapshot=snapshot,
                        trade_plan=trade_plan,
                    )
                )

                self.state["previous_flip_alert"] = True

        else:
            self.state["previous_flip_alert"] = False

        # --------------------------------------------------
        # 關鍵價位提醒：Flip / R1 / S1
        # --------------------------------------------------

        self.handle_key_level_alerts(
            five_min_close=five_min_close,
            snapshot=snapshot,
            trade_plan=trade_plan,
        )

        # --------------------------------------------------
        # 行為分析
        # --------------------------------------------------

        chip = load_chip_cache()

        behavior = analyze_behavioral_context(
            df_5min=df_5min,
            current_price=current_price,
            chip=chip,
        )

        current_trap = behavior.get("trap")
        current_sweep = behavior.get("sweep")

        previous_trap = self.state.get("previous_trap")
        previous_sweep = self.state.get("previous_sweep")

        # --------------------------------------------------
        # LONG TRAP：只有狀態剛轉成 LONG_TRAP 才發
        # --------------------------------------------------

        if (
            current_trap == "LONG_TRAP"
            and previous_trap != "LONG_TRAP"
        ):
            send_human_alert(
                self.build_alert_context(
                    event="LONG_TRAP",
                    price=current_price,
                    snapshot=snapshot,
                    trade_plan=trade_plan,
                    behavior=behavior,
                    trap=current_trap,
                    sweep=current_sweep,
                )
            )

        # --------------------------------------------------
        # SHORT TRAP：只有狀態剛轉成 SHORT_TRAP 才發
        # --------------------------------------------------

        elif (
            current_trap == "SHORT_TRAP"
            and previous_trap != "SHORT_TRAP"
        ):
            send_human_alert(
                self.build_alert_context(
                    event="SHORT_TRAP",
                    price=current_price,
                    snapshot=snapshot,
                    trade_plan=trade_plan,
                    behavior=behavior,
                    trap=current_trap,
                    sweep=current_sweep,
                )
            )

        # --------------------------------------------------
        # SWEEP：只有 Sweep 類型變化才發
        # --------------------------------------------------

        if (
            current_sweep in ["BEARISH_SWEEP", "BULLISH_SWEEP"]
            and current_sweep != previous_sweep
        ):
            send_human_alert(
                self.build_alert_context(
                    event=current_sweep,
                    price=current_price,
                    snapshot=snapshot,
                    trade_plan=trade_plan,
                    behavior=behavior,
                    trap=current_trap,
                    sweep=current_sweep,
                )
            )

        # --------------------------------------------------
        # 更新狀態記憶
        # --------------------------------------------------

        self.state["previous_trap"] = current_trap
        self.state["previous_sweep"] = current_sweep

        self.state["behavioral_regime"] = current_trap or "NO_TRAP"
        self.state["last_sweep_type"] = current_sweep
        self.state["last_sweep_level"] = behavior.get("sweep_level")

        self.state["previous_5min_close"] = five_min_close

        # --------------------------------------------------
        # 夜盤收盤價紀錄
        # --------------------------------------------------

        now = datetime.now().time()

        if time(4, 55) <= now <= time(5, 0):
            self.state["night_close"] = five_min_close

    # --------------------------------------------------
    # Key Level Alerts
    # --------------------------------------------------

    def handle_key_level_alerts(
        self,
        five_min_close: float,
        snapshot: dict,
        trade_plan: dict,
    ):
        """
        關鍵價位提醒。

        事件：
        - FLIP_RECOVER：站回中軸
        - FLIP_BREAK：跌破中軸
        - R1_TOUCH：接近 R1
        - S1_TOUCH：接近 S1
        """

        flip = self.state.get("flip", 0)
        levels = self.get_levels(snapshot)

        r1 = levels.get("R1")
        s1 = levels.get("S1")

        previous_close = self.state.get("previous_5min_close")

        # 第一根沒有 previous_close，不做穿越判斷
        if previous_close is not None and flip:
            try:
                previous_close = float(previous_close)
                current_close = float(five_min_close)
                flip_value = float(flip)

                # 站回中軸
                if previous_close < flip_value <= current_close:
                    send_human_alert(
                        self.build_alert_context(
                            event="FLIP_RECOVER",
                            price=five_min_close,
                            snapshot=snapshot,
                            trade_plan=trade_plan,
                        )
                    )

                # 跌破中軸
                elif previous_close > flip_value >= current_close:
                    send_human_alert(
                        self.build_alert_context(
                            event="FLIP_BREAK",
                            price=five_min_close,
                            snapshot=snapshot,
                            trade_plan=trade_plan,
                        )
                    )

            except Exception:
                pass

        # 接近 R1
        if r1:
            try:
                if abs(float(five_min_close) - float(r1)) <= 50:
                    send_human_alert(
                        self.build_alert_context(
                            event="R1_TOUCH",
                            price=five_min_close,
                            snapshot=snapshot,
                            trade_plan=trade_plan,
                        )
                    )
            except Exception:
                pass

        # 接近 S1
        if s1:
            try:
                if abs(float(five_min_close) - float(s1)) <= 50:
                    send_human_alert(
                        self.build_alert_context(
                            event="S1_TOUCH",
                            price=five_min_close,
                            snapshot=snapshot,
                            trade_plan=trade_plan,
                        )
                    )
            except Exception:
                pass

    # --------------------------------------------------
    # Alert Context Builder
    # --------------------------------------------------

    def build_alert_context(
        self,
        event: str,
        price: float,
        snapshot: dict,
        trade_plan: dict,
        behavior: dict | None = None,
        trap: str | None = None,
        sweep: str | None = None,
    ) -> dict:
        """
        建立 send_human_alert 所需 context。

        這裡會補齊 AI 即時建議需要的欄位：
        - pivot
        - r1
        - s1
        - sentiment
        - behavior
        - trap
        - sweep
        - is_realtime
        """

        levels = self.get_levels(snapshot)

        _pivot = levels.get("pivot")
        _r1 = levels.get("R1")
        _s1 = levels.get("S1")

        def _is_missing(v):
            if v is None:
                return True
            try:
                return math.isnan(float(v))
            except Exception:
                return False

        if _is_missing(_pivot) or _is_missing(_r1) or _is_missing(_s1):
            try:
                with open('chip_cache.json') as _f:
                    _cc = json.load(_f)
                _tech = _cc.get('tech_levels', {})
                if _is_missing(_pivot):
                    _pivot = _tech.get('pivot')
                if _is_missing(_r1):
                    _r1 = _tech.get('r1')
                if _is_missing(_s1):
                    _s1 = _tech.get('s1')
            except Exception:
                pass

        return {
            "event": event,
            "price": price,
            "flip": self.state.get("flip", 0),

            "pivot": _pivot,
            "r1": _r1,
            "s1": _s1,

            "sentiment": self.get_sentiment(snapshot),
            "behavior": (
                behavior.get("regime")
                if isinstance(behavior, dict)
                else self.state.get("behavioral_regime", "NO_TRAP")
            ),

            "trap": trap,
            "sweep": sweep,

            "is_realtime": self.state.get("is_realtime", False),

            "stop": trade_plan.get("stop_loss"),
            "target": trade_plan.get("take_profit_1"),

            "tick_source": self.state.get("tick_source"),
            "tick_time": self.state.get("tick_time"),
            "latest_k_time": self.state.get("latest_k_time"),
            "data_delay_minutes": self.state.get("data_delay_minutes"),
        }

    # --------------------------------------------------
    # Snapshot / State Helpers
    # --------------------------------------------------

    def get_levels(self, snapshot: dict) -> dict:
        """
        取得 levels。

        優先順序：
        1. snapshot["levels"]
        2. state["levels"]
        3. state["daily_levels"]
        """

        if isinstance(snapshot, dict):
            levels = snapshot.get("levels")

            if isinstance(levels, dict):
                return levels

        levels = self.state.get("levels")

        if isinstance(levels, dict):
            return levels

        daily_levels = self.state.get("daily_levels")

        if isinstance(daily_levels, dict):
            return daily_levels

        return {}

    def get_sentiment(self, snapshot: dict) -> str:
        """
        取得法人 / 籌碼情緒文字。
        """

        if isinstance(snapshot, dict):
            for key in ["sentiment", "institutional_sentiment"]:
                value = snapshot.get(key)

                if value:
                    return value

        for key in ["sentiment", "institutional_sentiment"]:
            value = self.state.get(key)

            if value:
                return value

        return ""

    # --------------------------------------------------
    # State Update
    # --------------------------------------------------

    def update_state_from_snapshot(
        self,
        snapshot: dict,
        risk_protocol: dict,
    ):
        """
        用策略快照更新 state。
        """

        self.state["price"] = snapshot["price"]
        self.state["regime"] = snapshot["regime"]
        self.state["atr"] = snapshot["atr"]
        self.state["kill_switch"] = snapshot["kill_switch"]
        self.state["volatility_state"] = snapshot["volatility_state"]
        self.state["levels"] = snapshot["levels"]

        levels = snapshot.get("levels", {})

        if isinstance(levels, dict):
            if levels.get("close"):
                self.state["previous_futures_close"] = levels.get("close")

            if levels.get("pivot"):
                self.state["pivot"] = levels.get("pivot")

            # r1/s1 不在此覆寫，保留 preopen 寫入的真實 Pivot 值

        self.state["risk_multiplier"] = risk_protocol["risk_multiplier"]
        self.state["allow"] = risk_protocol["allow"]
        self.state["forbidden"] = risk_protocol["forbidden"]

        self.state["allow_trade"] = not snapshot["no_trade"]

        if risk_protocol["risk_multiplier"] <= 0:
            self.state["allow_trade"] = False