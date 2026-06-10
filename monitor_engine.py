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


def _is_valid_level(price: float, level, pct: float = 0.05) -> bool:
    """
    點位有效性驗證：與現價差距超過 pct 比例則視為過期。

    pct=0.05 = 5%，台指期 44000 點時約 2200 點門檻。
    """
    if level is None:
        return False
    try:
        lv = float(level)
        pv = float(price)
        if pv <= 0 or lv <= 0:
            return False
        return abs(lv - pv) / pv <= pct
    except Exception:
        return False


def _get_macro_context(state: dict, chip_ctx: dict) -> dict:
    """
    判斷當前宏觀市場位置，供警報過濾器使用。

    回傳欄位：
    - macro_bias:      宏觀偏向（BELOW_PUT_WALL / NEAR_PUT_WALL /
                       ABOVE_CALL_WALL / BELOW_PIVOT / NEUTRAL）
    - intraday_trend:  日內趨勢（STRONG_DOWN / DOWN / FLAT / UP / STRONG_UP）
    - day_change:      日內點數變化
    - day_change_pct:  日內漲跌幅（小數）
    - current_price / put_wall / call_wall
    """
    try:
        current_price = float(state.get('price', 0) or 0)
        put_wall  = float(state.get('put_wall', 0) or 0)
        call_wall = float(state.get('call_wall', 0) or 0)
        pivot     = float(state.get('pivot', 0) or 0)
        day_open  = float(state.get('day_session_open', current_price) or current_price)

        day_change     = current_price - day_open
        day_change_pct = day_change / day_open if day_open > 0 else 0.0

        if put_wall > 0 and current_price < put_wall:
            macro_bias = 'BELOW_PUT_WALL'
        elif put_wall > 0 and current_price < put_wall * 1.02:
            macro_bias = 'NEAR_PUT_WALL'
        elif call_wall > 0 and current_price > call_wall:
            macro_bias = 'ABOVE_CALL_WALL'
        elif pivot > 0 and current_price < pivot:
            macro_bias = 'BELOW_PIVOT'
        else:
            macro_bias = 'NEUTRAL'

        if day_change_pct < -0.03:
            intraday_trend = 'STRONG_DOWN'
        elif day_change_pct < -0.01:
            intraday_trend = 'DOWN'
        elif day_change_pct > 0.03:
            intraday_trend = 'STRONG_UP'
        elif day_change_pct > 0.01:
            intraday_trend = 'UP'
        else:
            intraday_trend = 'FLAT'

        return {
            'macro_bias':      macro_bias,
            'intraday_trend':  intraday_trend,
            'day_change':      day_change,
            'day_change_pct':  day_change_pct,
            'current_price':   current_price,
            'put_wall':        put_wall,
            'call_wall':       call_wall,
        }

    except Exception as e:
        try:
            print(f"[macro_ctx] failed: {e}")
        except Exception:
            pass
        return {'macro_bias': 'NEUTRAL', 'intraday_trend': 'FLAT',
                'day_change': 0, 'day_change_pct': 0,
                'current_price': 0, 'put_wall': 0, 'call_wall': 0}


def _should_suppress_alert(event_type: str, macro: dict) -> tuple:
    """
    判斷警報是否應被宏觀過濾器抑制。

    回傳 (should_suppress: bool, reason: str)
    """
    macro_bias     = macro.get('macro_bias', 'NEUTRAL')
    intraday_trend = macro.get('intraday_trend', 'FLAT')
    day_chg_pct    = macro.get('day_change_pct', 0.0)

    # 極端偏空環境：抑制多方警報
    if macro_bias in ('BELOW_PUT_WALL', 'NEAR_PUT_WALL') or intraday_trend == 'STRONG_DOWN':
        suppress_in_bear = {'BULLISH_SWEEP', 'LONG_TRAP', 'FLIP_RECOVER', 'R1_TOUCH'}
        if event_type in suppress_in_bear:
            return True, (
                f"宏觀過濾：{macro_bias}（日內{day_chg_pct:.1%}），"
                f"{event_type} 在偏空背景下無實戰意義"
            )

    # 極端偏多環境：抑制空方警報
    if macro_bias == 'ABOVE_CALL_WALL' or intraday_trend == 'STRONG_UP':
        suppress_in_bull = {'BEARISH_SWEEP', 'SHORT_TRAP', 'S1_TOUCH'}
        if event_type in suppress_in_bull:
            return True, (
                f"宏觀過濾：{macro_bias}（日內{day_chg_pct:.1%}），"
                f"{event_type} 在偏多背景下無實戰意義"
            )

    return False, ""


class AtosSentinel:
    def __init__(self):
        self.state = load_state()

    # --------------------------------------------------
    # Level Refresh from Cache
    # --------------------------------------------------

    def _refresh_levels_from_cache(self, current_price: float):
        """
        每輪監控開始時，從 chip_cache.json 刷新技術點位。

        規則：
        - flip（Active Pivot）過期（>5%）→ 從 chip_cache 重算
        - pivot / r1 / s1 過期 → 從 chip_cache 補回
        - call_wall / put_wall 每輪都同步（選擇權鏈每日更新）
        """
        try:
            with open('chip_cache.json', encoding='utf-8') as _f:
                _cc = json.load(_f)
            _tech = _cc.get('tech_levels', {})
            _oi   = _cc.get('option_oi', {})

            # ── call_wall / put_wall 每輪同步 ──
            _new_call = _oi.get('call_wall_strike')
            _new_put  = _oi.get('put_wall_strike')
            if _new_call:
                self.state['call_wall'] = _new_call
            if _new_put:
                self.state['put_wall'] = _new_put

            # ── pivot / r1 / s1：只在過期時補回 ──
            for _key, _cache_key in (('pivot', 'pivot'), ('r1', 'r1'), ('s1', 's1')):
                _cur = self.state.get(_key)
                if not _is_valid_level(current_price, _cur, pct=0.05):
                    _fresh = _tech.get(_cache_key)
                    if _fresh is not None:
                        self.state[_key] = _fresh
                        try:
                            print(f"[Monitor] {_key} refreshed: {_cur} -> {_fresh}")
                        except Exception:
                            pass

            # ── flip（Active Pivot）：過期時從 chip_cache 重算 ──
            _cur_flip = self.state.get('flip')
            if not _is_valid_level(current_price, _cur_flip, pct=0.05):
                _ph = _tech.get('prev_high')
                _pl = _tech.get('prev_low')
                _pc = _tech.get('prev_close')
                if _ph and _pl and _pc:
                    _sp = (_ph + _pl + _pc) / 3
                    # Active Pivot 邏輯：超出牆區間改用區間中點
                    _cw = self.state.get('call_wall') or _oi.get('call_wall_strike')
                    _pw = self.state.get('put_wall') or _oi.get('put_wall_strike')
                    _active = _sp
                    if _cw and _pw:
                        try:
                            if _sp >= float(_cw) or _sp <= float(_pw):
                                _active = (float(_cw) + float(_pw)) / 2
                        except Exception:
                            pass
                    self.state['flip'] = round(_active, 1)
                    try:
                        print(f"[Monitor] flip refreshed: {_cur_flip} -> {self.state['flip']} (recalc from cache H/L/C)")
                    except Exception:
                        pass

        except Exception as _e:
            try:
                print(f"[Monitor] _refresh_levels_from_cache error: {_e}")
            except Exception:
                pass

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

        # ── 即時更新日盤高低點（盤中每 2 分鐘同步 tick 價）──
        try:
            from session_engine import is_day_session
            if is_day_session():
                _cp = float(current_price)
                _existing_high = float(self.state.get("day_session_high", 0) or 0)
                _existing_low  = float(self.state.get("day_session_low", 999999) or 999999)
                if _cp > _existing_high:
                    self.state["day_session_high"] = _cp
                if _cp < _existing_low or _existing_low == 0:
                    self.state["day_session_low"] = _cp
        except Exception:
            pass

        # 每輪開始主動刷新技術點位（防止 state 中的過期點位影響後續判斷）
        self._refresh_levels_from_cache(current_price)

        event_mode = get_event_mode()
        event_risk = get_event_risk_mode()
        risk_protocol = get_risk_protocol(event_mode)

        self.state["event_mode"] = event_mode
        self.state["event_risk_mode"] = event_risk["mode"]
        # tick_source 欄位優先（fallback 時標注為 FUGLE_IX0001_FALLBACK），
        # 其次用 source 欄位（FinMind 正常情況）
        self.state["tick_source"] = tick.get("tick_source") or tick.get("source")
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

            # forbidden 在 risk_engine 裡是字串，需確保這裡是 list
            forbidden = self.state.get("forbidden", [])
            if isinstance(forbidden, str):
                forbidden = [forbidden] if forbidden else []

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

        _flip_for_invalidation = self.state.get("flip", 0)
        if not _is_valid_level(float(five_min_close) if five_min_close else 0.0, _flip_for_invalidation):
            _flip_for_invalidation = 0  # 點位過期，停用 Flip 失效檢查

        invalidation = check_invalidation(
            five_min_close=five_min_close,
            flip_level=_flip_for_invalidation,
            current_state=self.state.get("regime", "🟡 中性模式"),
        )

        if invalidation["alert"]:
            previous_flip_alert = self.state.get("previous_flip_alert", False)

            if not previous_flip_alert:
                self._fire_alert(
                    "FLIP_INVALID",
                    self.build_alert_context(
                        event="FLIP_INVALID",
                        price=five_min_close,
                        snapshot=snapshot,
                        trade_plan=trade_plan,
                    ),
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
            self._fire_alert(
                "LONG_TRAP",
                self.build_alert_context(
                    event="LONG_TRAP",
                    price=current_price,
                    snapshot=snapshot,
                    trade_plan=trade_plan,
                    behavior=behavior,
                    trap=current_trap,
                    sweep=current_sweep,
                ),
            )

        # --------------------------------------------------
        # SHORT TRAP：只有狀態剛轉成 SHORT_TRAP 才發
        # --------------------------------------------------

        elif (
            current_trap == "SHORT_TRAP"
            and previous_trap != "SHORT_TRAP"
        ):
            self._fire_alert(
                "SHORT_TRAP",
                self.build_alert_context(
                    event="SHORT_TRAP",
                    price=current_price,
                    snapshot=snapshot,
                    trade_plan=trade_plan,
                    behavior=behavior,
                    trap=current_trap,
                    sweep=current_sweep,
                ),
            )

        # --------------------------------------------------
        # SWEEP：只有 Sweep 類型變化才發
        # --------------------------------------------------

        if (
            current_sweep in ["BEARISH_SWEEP", "BULLISH_SWEEP"]
            and current_sweep != previous_sweep
        ):
            self._fire_alert(
                current_sweep,
                self.build_alert_context(
                    event=current_sweep,
                    price=current_price,
                    snapshot=snapshot,
                    trade_plan=trade_plan,
                    behavior=behavior,
                    trap=current_trap,
                    sweep=current_sweep,
                ),
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
        # 驗證 flip 有效性
        _fp_f = float(five_min_close) if five_min_close else 0.0
        if not _is_valid_level(_fp_f, flip):
            flip = None

        levels = self.get_levels(snapshot)

        r1 = levels.get("R1")
        s1 = levels.get("S1")

        # 驗證 r1/s1 有效性
        if not _is_valid_level(_fp_f, r1):
            r1 = None
        if not _is_valid_level(_fp_f, s1):
            s1 = None

        previous_close = self.state.get("previous_5min_close")

        # 第一根沒有 previous_close，不做穿越判斷
        if previous_close is not None and flip:
            try:
                previous_close = float(previous_close)
                current_close = float(five_min_close)
                flip_value = float(flip)

                # 站回中軸
                if previous_close < flip_value <= current_close:
                    self._fire_alert(
                        "FLIP_RECOVER",
                        self.build_alert_context(
                            event="FLIP_RECOVER",
                            price=five_min_close,
                            snapshot=snapshot,
                            trade_plan=trade_plan,
                        ),
                    )

                # 跌破中軸
                elif previous_close > flip_value >= current_close:
                    self._fire_alert(
                        "FLIP_BREAK",
                        self.build_alert_context(
                            event="FLIP_BREAK",
                            price=five_min_close,
                            snapshot=snapshot,
                            trade_plan=trade_plan,
                        ),
                    )

            except Exception:
                pass

        # 接近 R1
        if r1:
            try:
                if abs(float(five_min_close) - float(r1)) <= 50:
                    self._fire_alert(
                        "R1_TOUCH",
                        self.build_alert_context(
                            event="R1_TOUCH",
                            price=five_min_close,
                            snapshot=snapshot,
                            trade_plan=trade_plan,
                        ),
                    )
            except Exception:
                pass

        # 接近 S1
        if s1:
            try:
                if abs(float(five_min_close) - float(s1)) <= 50:
                    self._fire_alert(
                        "S1_TOUCH",
                        self.build_alert_context(
                            event="S1_TOUCH",
                            price=five_min_close,
                            snapshot=snapshot,
                            trade_plan=trade_plan,
                        ),
                    )
            except Exception:
                pass

    # --------------------------------------------------
    # Alert Fire (with macro filter)
    # --------------------------------------------------

    def _fire_alert(self, event: str, context: dict) -> None:
        """
        統一警報發送入口：先跑宏觀過濾器，再決定是否呼叫 send_human_alert。

        若警報被抑制，印出原因並直接返回。
        若警報通過但宏觀背景特殊，在 context 中加入 macro_note 供訊息末尾顯示。
        """
        chip_ctx = {}
        try:
            chip_ctx = load_chip_cache() or {}
        except Exception:
            pass

        macro = _get_macro_context(self.state, chip_ctx)
        should_suppress, suppress_reason = _should_suppress_alert(event, macro)

        if should_suppress:
            try:
                print(f"[macro_filter] suppressed {event}: {suppress_reason}")
            except Exception:
                pass
            return

        # 宏觀背景特殊時，在 context 中加入提示（alert_engine_v2 會附加到訊息末）
        macro_note = ""
        macro_bias     = macro.get('macro_bias', 'NEUTRAL')
        day_chg_pct    = macro.get('day_change_pct', 0.0)
        if macro_bias == 'BELOW_PUT_WALL':
            macro_note = (
                f"\n⚠️ 宏觀背景：現價已跌破 Put wall {int(macro['put_wall'])}，"
                f"日內跌幅 {day_chg_pct:.1%}，請在大趨勢偏空背景下謹慎解讀此警報"
            )
        elif macro_bias == 'NEAR_PUT_WALL':
            macro_note = (
                f"\n⚠️ 宏觀背景：現價逼近 Put wall {int(macro['put_wall'])}，"
                f"日內跌幅 {day_chg_pct:.1%}，大趨勢偏空，請謹慎解讀"
            )
        elif macro_bias == 'ABOVE_CALL_WALL':
            macro_note = (
                f"\n⚠️ 宏觀背景：現價突破 Call wall {int(macro['call_wall'])}，"
                f"日內漲幅 {day_chg_pct:.1%}，大趨勢偏多，請謹慎解讀空方警報"
            )

        if macro_note:
            context = dict(context)
            context['macro_note'] = macro_note

        send_human_alert(context)

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
                with open('chip_cache.json', encoding='utf-8') as _f:
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

        # 過期驗證：與現價差距 >5% 視為過期資料，設為 None
        _price_f = float(price) if price else 0.0
        if _price_f > 0:
            if not _is_valid_level(_price_f, _pivot):
                _pivot = None
            if not _is_valid_level(_price_f, _r1):
                _r1 = None
            if not _is_valid_level(_price_f, _s1):
                _s1 = None

        _flip_val = self.state.get("flip", 0)
        if not _is_valid_level(_price_f, _flip_val):
            _flip_val = None

        return {
            "event": event,
            "price": price,
            "flip": _flip_val,

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