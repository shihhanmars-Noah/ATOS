# main_commander.py

import json
import time
from datetime import datetime
from pathlib import Path

import schedule
from dotenv import load_dotenv

from error_handler import safe_execute
from persistent_state import load_state, save_state
from messenger import send_to_telegram

from data_engine import (
    get_realtime_tick,
    get_flip_level,
    get_dynamic_resistance_support,
    get_institutional_sentiment,
    backfill_day_session_ticks,
)

from monitor_engine import AtosSentinel


# --------------------------------------------------
# Optional Imports
# --------------------------------------------------

try:
    from preopen_report_engine import send_preopen_sip_report
except Exception:
    send_preopen_sip_report = None

try:
    from stock_report_engine import send_stock_picks_report
except Exception:
    send_stock_picks_report = None

try:
    from evening_report_engine import send_evening_report as send_evening_report_func
except Exception:
    send_evening_report_func = None

try:
    from night_session_engine import update_night_close_state
except Exception:
    update_night_close_state = None

try:
    from holiday_engine import update_holiday_cache
except Exception:
    update_holiday_cache = None

try:
    from settlement_engine import update_settlement_cache
except Exception:
    update_settlement_cache = None

try:
    from event_engine import update_event_cache
except Exception:
    update_event_cache = None

try:
    from news_engine import poll_news as news_engine_poll
except Exception:
    news_engine_poll = None

try:
    from ai_report_engine import generate_report as ai_generate_report
except Exception:
    ai_generate_report = None

try:
    from data_backfill import check_and_backfill
except Exception:
    check_and_backfill = None


load_dotenv()


# --------------------------------------------------
# Main Commander
# --------------------------------------------------

class AtosCommander:
    def __init__(self):
        self.state = load_state()
        self.sentinel = AtosSentinel()
        self._evening_sent = False  # 每日發送後設為 True，14:55 重置

    # --------------------------------------------------
    # Cache Updates
    # --------------------------------------------------

    @safe_execute
    def update_holiday(self):
        """
        更新假日快取。

        目前先使用空 fallback，避免排程失敗。
        """

        if update_holiday_cache is None:
            print("⚪ 假日快取：update_holiday_cache 不存在，略過")
            return

        def fetch_func():
            return []

        update_holiday_cache(fetch_func)
        print("✅ 假日快取已更新")

    @safe_execute
    def update_settlement(self):
        """
        更新結算日快取。

        目前先使用空 fallback，避免排程失敗。
        """

        if update_settlement_cache is None:
            print("⚪ 結算日快取：update_settlement_cache 不存在，略過")
            return

        def fetch_func():
            from settlement_engine import compute_settlement_dates
            monthly = compute_settlement_dates(months_ahead=3)
            return {
                "weekly_settlement": [],
                "monthly_settlement": monthly,
            }

        update_settlement_cache(fetch_func)
        print("✅ 結算日快取已更新")

    @safe_execute
    def update_events(self):
        """
        更新事件快取。

        目前尚未接正式事件資料源，先保留空事件，
        避免 setup_schedule 呼叫 self.update_events 時啟動失敗。
        """

        if update_event_cache is None:
            print("⚪ 事件快取：update_event_cache 不存在，略過")
            return

        def fetch_func():
            return []

        update_event_cache(fetch_func)
        print("✅ 事件快取已更新")

    @safe_execute
    def update_night_close(self):
        """
        更新夜盤收盤價。

        來源：
        - TXF snapshot cache
        """

        if update_night_close_state is None:
            print("⚪ 夜盤收盤：night_session_engine 尚未建立，略過")
            return

        result = update_night_close_state()

        if result.get("updated"):
            print(
                "✅ 夜盤收盤價已更新："
                f"{result.get('night_close')} / {result.get('night_close_time')}"
            )
        else:
            print(f"⚠️ 夜盤收盤價未更新：{result.get('reason')}")

    # --------------------------------------------------
    # Market Data Updates
    # --------------------------------------------------

    @safe_execute
    def refresh_night_session_close(self):
        """
        05:05 執行：抓取夜盤最終 H/L/C 與外資夜盤口數
        確保 08:00 的 Pivot 計算包含夜盤資訊
        """
        from night_session_engine import get_night_session_data

        night_data = get_night_session_data()

        if not night_data or not night_data.get('night_close'):
            try:
                print("⚠️ 夜盤收盤資料取得失敗，Pivot 將沿用昨日日盤資料")
            except Exception:
                pass
            return

        night_close = night_data['night_close']
        night_high  = night_data['night_high']
        night_low   = night_data['night_low']
        night_chg   = night_data.get('night_chg', 0)

        # 寫入 state
        state = load_state()
        state['night_session_close']     = night_close
        state['night_session_high']      = night_high
        state['night_session_low']       = night_low
        state['night_session_chg']       = night_chg
        state['night_session_updated_at'] = datetime.now().isoformat()
        save_state(state)

        try:
            print(
                f"✅ 夜盤收盤資料更新：收{night_close} "
                f"高{night_high} 低{night_low} "
                f"變動{float(night_chg):+.0f}點"
            )
        except Exception:
            pass

        # 同時更新夜盤外資口數
        try:
            from chip_data_engine import fetch_afterhours_institutional
            ah_data = fetch_afterhours_institutional()
            if ah_data:
                state = load_state()
                state['afterhours_foreign_net'] = ah_data.get('foreign_ah_net', 0)
                save_state(state)
                try:
                    print(f"✅ 夜盤外資口數更新：{ah_data.get('foreign_ah_net', 0):+,}口")
                except Exception:
                    pass
        except Exception as _e:
            try:
                print(f"⚠️ 夜盤外資口數更新失敗：{_e}")
            except Exception:
                pass

    @safe_execute
    def reset_daily_state(self):
        """
        06:00 執行：重置所有當日發送鎖與快取旗標
        確保新交易日從乾淨狀態開始
        """
        state = load_state()
        today = datetime.now().strftime('%Y-%m-%d')

        # 重置發送鎖（只重置昨日的，今日的保留）
        locks_to_reset = [
            'evening_report_sent_date',
            'evening_stock_report_sent_date',
            'preopen_report_sent_date',
            'stock_report_sent_date',
        ]

        for key in locks_to_reset:
            existing = state.get(key, '')
            if existing and existing != today:
                state[key] = None
                try:
                    print(f"[reset] {key}: {existing} -> None")
                except Exception:
                    pass

        # 重置日盤高低點
        state['day_session_high']  = None
        state['day_session_low']   = None
        state['day_session_close'] = None

        # 重置今日警報計數
        state['today_alert_count'] = 0

        save_state(state)
        try:
            print(f"✅ 新交易日狀態初始化完成（{today}）")
        except Exception:
            pass

    @safe_execute
    def refresh_flip(self):
        """
        更新前日收盤（prev_close）及 mid_range（前日高低中間值），
        並同步儲存 R1/S1/Pivot 等技術關鍵價位。
        """

        old_prev_close = self.state.get("prev_close") or self.state.get("flip", 0)

        prev_close = get_flip_level(
            futures_id="TX",
            fallback=old_prev_close,
        )

        levels = get_dynamic_resistance_support(
            futures_id="TX",
        )

        if prev_close:
            self.state["prev_close"] = prev_close
            self.sentinel.state["prev_close"] = prev_close

        if levels:
            self.state["daily_levels"] = levels
            self.state["levels"] = levels

            self.sentinel.state["daily_levels"] = levels
            self.sentinel.state["levels"] = levels

            if levels.get("close"):
                self.state["previous_futures_close"] = levels.get("close")
                self.sentinel.state["previous_futures_close"] = levels.get("close")

            if levels.get("R1"):
                self.state["r1"] = levels.get("R1")
                self.sentinel.state["r1"] = levels.get("R1")

            if levels.get("S1"):
                self.state["s1"] = levels.get("S1")
                self.sentinel.state["s1"] = levels.get("S1")

            high = levels.get("high")
            low  = levels.get("low")
            if high and low:
                mid_range = round((high + low) / 2, 1)
                self.state["mid_range"] = mid_range
                self.sentinel.state["mid_range"] = mid_range

            # ── Active Pivot：優先納入夜盤高低收 ──
            try:
                _state_now = load_state()
                night_high  = _state_now.get('night_session_high')
                night_low   = _state_now.get('night_session_low')
                night_close = _state_now.get('night_session_close')

                _prev_high  = float(levels.get('high',  0) or 0)
                _prev_low   = float(levels.get('low',   0) or 0)
                _prev_close = float(levels.get('close', 0) or 0)

                if night_high and night_low and night_close:
                    _combined_high  = max(_prev_high, float(night_high))
                    _combined_low   = min(_prev_low,  float(night_low))
                    _combined_close = float(night_close)   # 夜盤收盤為最新收盤
                    active_pivot = round(
                        (_combined_high + _combined_low + _combined_close) / 3, 1
                    )
                    pivot_source = "日盤+夜盤"
                    try:
                        print(
                            f"✅ Active Pivot（含夜盤）：{active_pivot} "
                            f"（日高{_prev_high}/夜高{night_high} "
                            f"日低{_prev_low}/夜低{night_low} "
                            f"夜收{night_close}）"
                        )
                    except Exception:
                        pass
                else:
                    if _prev_high and _prev_low and _prev_close:
                        active_pivot = round(
                            (_prev_high + _prev_low + _prev_close) / 3, 1
                        )
                    else:
                        active_pivot = levels.get("pivot")
                    pivot_source = "昨日日盤"
                    try:
                        print(f"⚠️ 無夜盤資料，Active Pivot 使用昨日日盤：{active_pivot}")
                    except Exception:
                        pass

                if active_pivot:
                    self.state["pivot"]        = active_pivot
                    self.state["flip"]         = active_pivot
                    self.state["pivot_source"] = pivot_source
                    self.sentinel.state["pivot"]        = active_pivot
                    self.sentinel.state["flip"]         = active_pivot
                    self.sentinel.state["pivot_source"] = pivot_source
            except Exception as _pe:
                # fallback：直接用 levels 內建 pivot
                if levels.get("pivot"):
                    self.state["pivot"] = levels.get("pivot")
                    self.sentinel.state["pivot"] = levels.get("pivot")
                try:
                    print(f"⚠️ Active Pivot 夜盤整合失敗，使用 levels pivot：{_pe}")
                except Exception:
                    pass

        save_state(self.state)

        try:
            print(f"✅ prev_close 更新完成：{prev_close}")
        except Exception:
            pass
        return prev_close

    @safe_execute
    def refresh_chip(self):
        """
        更新完整籌碼快取（chip_cache.json），供晚盤報告使用。
        結算日隔天提示轉倉影響。
        """
        # 結算日隔天提示
        try:
            from settlement_engine import get_settlement_type
            from datetime import date, timedelta
            yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
            if get_settlement_type(yesterday) is not None:
                print("⚠️ 結算日隔天，期貨淨部位含換月轉倉，變動量僅供參考")
        except Exception:
            pass
        try:
            from chip_data_engine import update_chip_cache, build_chip_context
            ok = update_chip_cache()
            if ok:
                chip_ctx = build_chip_context() or {}
                sentiment = chip_ctx.get("sentiment_bias") or chip_ctx.get("foreign_net_level") or "N/A"
                score = chip_ctx.get("sentiment_score", 0)
                self.state["sentiment"] = sentiment
                self.state["institutional_sentiment"] = sentiment
                self.state["sentiment_score"] = score
                self.sentinel.state["sentiment"] = sentiment
                self.sentinel.state["institutional_sentiment"] = sentiment
                self.sentinel.state["sentiment_score"] = score
                save_state(self.state)
                print(f"✅ 籌碼快取更新完成：{sentiment}（評分 {score:+d}）")
                return sentiment
            else:
                print("⚠️ update_chip_cache() 回傳 False，改用 get_institutional_sentiment() 補救")
        except Exception as e:
            print(f"⚠️ chip_data_engine 載入失敗：{e}，改用 get_institutional_sentiment()")

        # 降級：僅更新舊版法人情緒文字
        sentiment = get_institutional_sentiment()
        self.state["sentiment"] = sentiment
        self.state["institutional_sentiment"] = sentiment
        self.sentinel.state["sentiment"] = sentiment
        self.sentinel.state["institutional_sentiment"] = sentiment
        save_state(self.state)
        print(f"✅ 籌碼情緒（降級）更新完成：{sentiment}")
        return sentiment

    @safe_execute
    def refresh_realtime_price(self):
        """
        更新即時價格到 state。
        """

        tick = get_realtime_tick()

        if not tick:
            print("⚠️ 即時價格取得失敗")
            return None

        self.state["price"] = tick.get("price")
        self.state["tick_source"] = tick.get("source")
        self.state["tick_time"] = tick.get("time")
        self.state["is_realtime"] = tick.get("is_realtime", False)

        self.sentinel.state["price"] = tick.get("price")
        self.sentinel.state["tick_source"] = tick.get("source")
        self.sentinel.state["tick_time"] = tick.get("time")
        self.sentinel.state["is_realtime"] = tick.get("is_realtime", False)

        save_state(self.state)

        return tick

    # --------------------------------------------------
    # Reports
    # --------------------------------------------------

    @safe_execute
    def send_startup_message(self):
        """
        啟動訊息：更新關鍵資料後發送簡短通知。
        """
        self.refresh_flip()
        self.refresh_realtime_price()
        self.refresh_chip()

        send_to_telegram(
            f"🛡️ ATOS Commander 啟動\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

    @safe_execute
    def check_and_catchup(self):
        """
        系統啟動時執行：
        檢查今天哪些報告沒發，自動補發。
        """
        now = datetime.now()
        today = now.strftime('%Y-%m-%d')
        current_time = now.hour * 60 + now.minute
        state = load_state()

        # 只在交易日執行（週一=0 ~ 週五=4）
        if now.weekday() >= 5:
            try:
                print('非交易日，跳過補發檢查')
            except Exception:
                pass
            return

        open_time = now.strftime('%H:%M')
        try:
            print(f'補發檢查：現在 {open_time}，檢查今日未發報告...')
        except Exception:
            pass

        # 通知 Telegram 進入補發模式
        send_to_telegram(
            f"ATOS 補發模式啟動（{open_time}開機）\n"
            f"正在補發今日未發送的報告..."
        )

        catchup_results = []

        # ── 1. 早盤期貨報告（08:35 後 ~ 13:45 前）──
        preopen_sent = state.get('preopen_report_sent_date') == today
        if not preopen_sent and 8*60+35 <= current_time <= 13*60+45:
            try:
                print('補發：早盤期貨報告...')
            except Exception:
                pass
            try:
                from preopen_report_engine import (
                    send_preopen_sip_report as _send_pre,
                    build_preopen_payload,
                    save_preopen_plan_to_state,
                    build_preopen_sip_message,
                )
                _payload = build_preopen_payload()
                save_preopen_plan_to_state(_payload)
                _msg = build_preopen_sip_message(_payload, is_catchup=True)
                send_to_telegram(_msg)
                state['preopen_report_sent_date'] = today
                save_state(state)
                catchup_results.append('早盤期貨報告（補發）')
            except Exception as e:
                catchup_results.append(f'早盤期貨報告補發失敗：{e}')

        # ── 2. 早盤選股報告（08:40 後 ~ 13:45 前）──
        stock_sent = state.get('stock_report_sent_date') == today
        if not stock_sent and 8*60+40 <= current_time <= 13*60+45:
            try:
                print('補發：早盤選股報告...')
            except Exception:
                pass
            try:
                from stock_report_engine import send_stock_picks_report as _send_stock
                _send_stock(is_catchup=True)
                state['stock_report_sent_date'] = today
                save_state(state)
                catchup_results.append('早盤選股報告（補發）')
            except Exception as e:
                catchup_results.append(f'早盤選股報告補發失敗：{e}')

        # ── 3. 晚盤期貨報告（15:30 後，今日尚未發）──
        evening_sent = state.get('evening_report_sent_date') == today
        if not evening_sent and current_time >= 15*60+30:
            try:
                print('補發：晚盤期貨報告（立刻觸發輪詢）...')
            except Exception:
                pass
            try:
                self._trigger_evening_futures_report()
                catchup_results.append('晚盤期貨報告（補發）')
            except Exception as e:
                catchup_results.append(f'晚盤期貨報告補發失敗：{e}')

        # ── 4. 晚盤選股報告（16:30 後，今日尚未發）──
        evening_stock_sent = state.get('evening_stock_report_sent_date') == today
        if not evening_stock_sent and current_time >= 16*60+30:
            try:
                print('補發：晚盤選股報告（立刻觸發輪詢）...')
            except Exception:
                pass
            try:
                self._trigger_evening_stock_report()
                catchup_results.append('晚盤選股報告（補發）')
            except Exception as e:
                catchup_results.append(f'晚盤選股報告補發失敗：{e}')

        # ── 結果彙報 ──
        if catchup_results:
            try:
                print('補發結果：')
                for r in catchup_results:
                    print(f'  {r}')
            except Exception:
                pass
            send_to_telegram(
                "ATOS 補發完成\n"
                + "\n".join(catchup_results)
            )
        else:
            try:
                print('今日所有報告均已發送，無需補發')
            except Exception:
                pass

    @safe_execute
    def send_preopen_report(self):
        """
        發送盤前 SIP 作戰報告。
        """

        if send_preopen_sip_report is None:
            send_to_telegram(
                "⚠️ 盤前 SIP 報告未啟用\n"
                "找不到 preopen_report_engine.py 或 send_preopen_sip_report。"
            )
            return

        send_preopen_sip_report()

    @safe_execute
    def send_stock_report(self):
        """
        發送個股觀察報告。
        """

        if send_stock_picks_report is None:
            send_to_telegram(
                "⚠️ 個股觀察報告未啟用\n"
                "找不到 stock_report_engine.py 或 send_stock_picks_report。"
            )
            return

        send_stock_picks_report()

    @safe_execute
    def send_opening_bell(self):
        """
        08:45 開盤提示：即時台指價格 + 籌碼關鍵數字。
        """
        try:
            from chip_data_engine import build_chip_context
            chip_ctx = build_chip_context() or {}
        except Exception:
            chip_ctx = {}

        state = load_state()
        now_str = datetime.now().strftime("%H:%M")

        price = state.get("price") or "N/A"
        mid_range = state.get("mid_range") or chip_ctx.get("mid_range") or "N/A"
        bias_label = chip_ctx.get("sentiment_bias", "N/A")
        sentiment_score = chip_ctx.get("sentiment_score", 0)
        foreign_net = chip_ctx.get("foreign_net", 0)
        call_wall = chip_ctx.get("call_wall", "N/A")
        put_wall = chip_ctx.get("put_wall", "N/A")

        score_str = f"{int(sentiment_score):+d}" if sentiment_score is not None else "N/A"

        msg = (
            f"🔔 開盤 {now_str}\n"
            f"台指：{price}｜{mid_range}中軸\n"
            f"情緒：{bias_label}({score_str})｜外資：{foreign_net:+,}口\n"
            f"Call wall：{call_wall}｜Put wall：{put_wall}"
        )
        send_to_telegram(msg)

    @safe_execute
    def send_evening_report(self):
        """
        發送 ATOS 晚盤作戰報告（含結算日提示）。
        """

        if send_evening_report_func is None:
            send_to_telegram(
                "⚠️ 晚盤報告未啟用\n"
                "找不到 evening_report_engine.py 或 send_evening_report。"
            )
            return

        # 結算日提示
        try:
            from settlement_engine import is_settlement_day, get_days_to_settlement
            if is_settlement_day():
                send_to_telegram("⚠️ 今日為結算日，大戶框架結算後重置，夜盤OI數據將換月")
        except Exception:
            pass

        send_evening_report_func()

    @safe_execute
    def send_night_report_when_ready(self):
        """
        舊版夜盤報告接口。

        保留相容性：
        - 目前直接導向 send_evening_report()
        - 避免舊 schedule 或舊檔案呼叫失敗
        """

        self.send_evening_report()

    @safe_execute
    def send_evening_stock_report(self):
        """
        發送晚盤選股複盤報告（15:10）。
        使用 ai_report_engine 產生 EVENING_STOCKS 報告。
        """
        if ai_generate_report is None:
            print("⚠️ ai_report_engine 未載入，跳過晚盤選股報告")
            return

        report = ai_generate_report("EVENING_STOCKS")
        if report:
            send_to_telegram(report)

    @safe_execute
    def poll_news(self):
        """
        輪詢新聞來源，發現重大事件時發送 Telegram 警報。
        每 5 分鐘由 schedule 呼叫。
        """
        if news_engine_poll is None:
            return

        count = news_engine_poll()
        if count:
            print(f"📰 [news_engine] 本輪發送 {count} 則重大事件警報")

    @safe_execute
    def send_weekly_performance_report(self):
        """
        週度績效報告（每週日 20:00）。
        計算近7天訊號勝率、期望值、訊號類型分析，附 AI 修正建議。
        """
        try:
            from performance_engine import send_weekly_performance_report as _send_perf
            _send_perf()
        except Exception as e:
            print(f"⚠️ [commander] 週度績效報告失敗：{e}")

    # --------------------------------------------------
    # Evening Report — Data-Ready Polling
    # --------------------------------------------------

    def _is_evening_data_ready(self) -> tuple[bool, list[str]]:
        """
        檢查晚盤報告三個就緒條件。

        回傳 (全部就緒, 未就緒條件列表)
        """
        today = datetime.now().strftime("%Y-%m-%d")
        missing = []

        # 條件1: chip_cache.json 今日已更新
        try:
            chip_path = Path("chip_cache.json")
            if chip_path.exists():
                with open(chip_path, "r", encoding="utf-8") as f:
                    chip = json.load(f)
                updated_at = str(chip.get("meta", {}).get("updated_at", ""))
                if not updated_at.startswith(today):
                    label = updated_at[:10] if updated_at else "N/A"
                    missing.append(f"chip_cache 今日尚未更新（updated_at: {label}）")
            else:
                missing.append("chip_cache.json 不存在")
        except Exception as e:
            missing.append(f"chip_cache 讀取失敗：{e}")

        # 條件2: option_oi_ready = True
        state = load_state()
        if not state.get("option_oi_ready"):
            missing.append("option_oi_ready 未就緒")

        # 條件3: day_session_close 有值（日盤收盤資料已入）
        if not state.get("day_session_close"):
            missing.append("day_session_close 尚無值")

        return len(missing) == 0, missing

    @safe_execute
    def check_and_send_evening_report(self):
        """
        每 2 分鐘輪詢資料就緒狀態，確認後才發送晚盤報告。

        流程：
        - 15:00 前直接返回，不做任何事
        - 三個條件全滿足 → 發送報告
        - 16:30 後強制發送，並先發送資料警示
        - 發送完成後設 _evening_sent=True，後續呼叫直接返回
        """
        if self._evening_sent:
            return

        now = datetime.now()
        if now.hour < 15:
            return

        ready, missing = self._is_evening_data_ready()

        today = now.strftime("%Y-%m-%d")
        deadline = datetime.strptime(f"{today} 16:30", "%Y-%m-%d %H:%M")
        force = now >= deadline

        if not ready and not force:
            print(f"⏳ [evening] 等待資料就緒：{', '.join(missing)}")
            return

        # 強制發送時先送資料警示
        if force and not ready:
            send_to_telegram(
                "⚠️ 晚盤報告 - 部分資料尚未更新\n"
                + "\n".join(f"  - {w}" for w in missing)
            )
            print(f"⚠️ [evening] 16:30 強制發送（未就緒：{', '.join(missing)}）")

        # 發送晚盤報告
        if send_evening_report_func is None:
            send_to_telegram("⚠️ 晚盤報告未啟用：找不到 evening_report_engine")
        else:
            send_evening_report_func()

        self._evening_sent = True
        status = "就緒發送" if ready else "強制發送"
        print(f"✅ [evening] 晚盤報告已發送（{status}，{now.strftime('%H:%M')}）")

    def reset_evening_sent(self):
        """
        每日 14:55 重置晚盤發送旗標，讓當天輪詢可以正常觸發。
        """
        self._evening_sent = False
        try:
            print("[evening] evening sent flag reset")
        except Exception:
            pass

    # --------------------------------------------------
    # Evening Futures Report — Polling Trigger (15:00)
    # --------------------------------------------------

    def _trigger_evening_futures_report(self):
        """
        晚盤期貨報告：等外資期貨口數和選擇權OI就緒才發。
        強制死線：16:00
        在排程執行緒中阻塞直到就緒或超時。
        """
        from data_readiness import wait_until_ready, is_chip_data_ready
        from data_engine import get_finmind_api

        api = get_finmind_api()
        start_date = datetime.now().strftime('%Y-%m-%d')

        def check_ready():
            try:
                futures_df = api.get_data(
                    dataset='TaiwanFuturesInstitutionalInvestors',
                    data_id='TX',
                    start_date=start_date,
                )
                if not is_chip_data_ready(futures_df):
                    try:
                        print("[evening_futures] futures chip not ready yet")
                    except Exception:
                        pass
                    return False

                option_df = api.get_data(
                    dataset='TaiwanOptionDaily',
                    data_id='TXO',
                    start_date=start_date,
                )
                if not is_chip_data_ready(option_df):
                    try:
                        print("[evening_futures] option OI not ready yet")
                    except Exception:
                        pass
                    return False

                return True
            except Exception as e:
                try:
                    print(f"[evening_futures] readiness check error: {e}")
                except Exception:
                    pass
                return False

        is_ready = wait_until_ready(
            check_func=check_ready,
            label="evening_futures",
            poll_interval_seconds=120,
            deadline_time='16:00',
        )

        if not is_ready:
            try:
                print("[evening_futures] deadline reached, forcing chip cache update then send")
            except Exception:
                pass

        # 更新 chip_cache（就緒或超時都更新，確保最新資料）
        try:
            from chip_data_engine import update_chip_cache
            update_chip_cache()
        except Exception as _e:
            try:
                print(f"[evening_futures] chip cache update error: {_e}")
            except Exception:
                pass

        # 發送晚盤期貨報告
        self.send_evening_report()

    # --------------------------------------------------
    # Evening Stock Report — Polling Trigger (15:30)
    # --------------------------------------------------

    def _trigger_evening_stock_report(self):
        """
        晚盤選股報告：等個股法人買賣超和外資期貨口數就緒才發。
        強制死線：18:00
        在排程執行緒中阻塞直到就緒或超時。
        """
        from data_readiness import wait_until_ready, is_chip_data_ready
        from data_engine import get_finmind_api

        # 防重複發送
        state = load_state()
        today = datetime.now().strftime('%Y-%m-%d')
        if state.get('evening_stock_report_sent_date') == today:
            try:
                print("[evening_stock] already sent today, skip")
            except Exception:
                pass
            return

        api = get_finmind_api()
        start_date = today

        def check_ready():
            try:
                stock_chip_df = api.get_data(
                    dataset='TaiwanStockInstitutionalInvestorsBuySell',
                    start_date=start_date,
                )
                if not is_chip_data_ready(stock_chip_df):
                    try:
                        print("[evening_stock] stock chip data not ready yet")
                    except Exception:
                        pass
                    return False

                futures_df = api.get_data(
                    dataset='TaiwanFuturesInstitutionalInvestors',
                    data_id='TX',
                    start_date=start_date,
                )
                if not is_chip_data_ready(futures_df):
                    try:
                        print("[evening_stock] futures chip not ready yet")
                    except Exception:
                        pass
                    return False

                return True
            except Exception as e:
                try:
                    print(f"[evening_stock] readiness check error: {e}")
                except Exception:
                    pass
                return False

        is_ready = wait_until_ready(
            check_func=check_ready,
            label="evening_stock",
            poll_interval_seconds=120,
            deadline_time='18:00',
        )

        if not is_ready:
            try:
                print("[evening_stock] deadline reached, forcing send with possibly stale data")
            except Exception:
                pass

        # 清除舊快取，確保使用今日資料
        import os
        if os.path.exists('stock_picks_cache.pkl'):
            os.remove('stock_picks_cache.pkl')
            try:
                print("[evening_stock] cleared stale cache")
            except Exception:
                pass

        # 發送晚盤選股報告
        if send_stock_picks_report is not None:
            send_stock_picks_report()
        else:
            send_to_telegram("evening stock report: send_stock_picks_report not available")
            return

        # 記錄已發送
        state = load_state()
        state['evening_stock_report_sent_date'] = today
        save_state(state)
        try:
            print("[evening_stock] report sent and date recorded")
        except Exception:
            pass

    # --------------------------------------------------
    # Monitor
    # --------------------------------------------------

    @safe_execute
    def run_monitor_once(self):
        """
        執行一次盤中監控。
        """

        self.sentinel.monitor_loop()
        self.check_pending_tracks()

    @safe_execute
    def check_pending_tracks(self):
        """
        追蹤所有到期的 trade_logger 訊號（30m / 60m / close）。
        由 run_monitor_once 每輪呼叫，有待追蹤時才做。
        """
        try:
            from trade_logger import get_pending_tracks, track_outcome
        except Exception:
            return

        pending = get_pending_tracks()
        if not pending:
            return

        current_price = self.state.get("price")
        if not current_price:
            return

        for item in pending:
            try:
                track_outcome(
                    trade_id=item["id"],
                    current_price=float(current_price),
                    track_point=item["track_point"],
                )
            except Exception as e:
                print(f"⚠️ [commander] track_outcome 失敗：{e}")

    # --------------------------------------------------
    # Schedule
    # --------------------------------------------------

    def setup_schedule(self):
        """
        設定 ATOS 排程。
        """

        schedule.clear()

        # 基礎快取
        schedule.every().day.at("07:30").do(self.update_holiday)
        schedule.every().day.at("07:31").do(self.update_settlement)
        schedule.every().day.at("07:32").do(self.update_events)

        # 夜盤收盤資料更新（原始快取）
        schedule.every().day.at("05:03").do(self.update_night_close)
        schedule.every().day.at("08:20").do(self.update_night_close)

        # 05:05 夜盤收盤數據結算（H/L/C + 外資夜盤口數）
        schedule.every().day.at("05:05").do(self.refresh_night_session_close)

        # 06:00 新交易日狀態初始化（重置發送鎖 / 日盤高低點）
        schedule.every().day.at("06:00").do(self.reset_daily_state)

        # 盤前資料更新
        schedule.every().day.at("08:00").do(self.refresh_flip)   # 含夜盤 Pivot
        schedule.every().day.at("08:30").do(self.refresh_chip)

        # 盤前報告
        schedule.every().day.at("08:35").do(self.send_preopen_report)

        # 個股觀察報告
        schedule.every().day.at("08:40").do(self.send_stock_report)

        # 開盤提示
        schedule.every().day.at("08:45").do(self.send_opening_bell)

        # 盤後 / 夜盤前資料更新
        schedule.every().day.at("13:50").do(self.refresh_chip)
        # 14:47 先補齊今日日盤 5分K 快取缺口，再給 14:50 refresh_flip 使用
        schedule.every().day.at("14:47").do(backfill_day_session_ticks)
        schedule.every().day.at("14:50").do(self.refresh_flip)

        # 晚盤期貨報告：15:00 開始輪詢，就緒即發，死線 16:00
        schedule.every().day.at("15:00").do(self._trigger_evening_futures_report)

        # 晚盤選股報告：16:30 開始輪詢（法人資料通常 17:00 後才完整），死線 18:00
        schedule.every().day.at("16:30").do(self._trigger_evening_stock_report)

        # 重大新聞輪詢（每 5 分鐘）
        schedule.every(5).minutes.do(self.poll_news)

        # 週度績效報告（每週日 13:00）
        schedule.every().sunday.at("13:00").do(self.send_weekly_performance_report)

        try:
            print("ATOS schedule setup completed")
        except Exception:
            pass

    # --------------------------------------------------
    # Start
    # --------------------------------------------------

    def startup_validation(self) -> bool:
        """
        啟動時驗證關鍵資料是否合理。

        門檻放寬至 5%（取代固定 300 點），避免正常日內波動誤觸發。

        盤中啟動（08:45-13:45）：
          日K 是昨日收盤，即時快照是今日盤中價，差距大屬正常，
          只印警告，不拒絕啟動。

        盤外啟動：
          差距超過 5% 才視為真正的污染，發 Telegram 警報並拒絕啟動。

        levels is None：
          FinMind 無法取得資料時，不阻止啟動，讓系統繼續用快取跑。
        """
        try:
            from data_engine import get_dynamic_resistance_support
            levels = get_dynamic_resistance_support(futures_id="TX")

            if levels is None:
                print("⚠️ 無法取得期貨點位資料，使用快取繼續啟動")
                return True  # 不阻止啟動，讓系統繼續跑

            close    = levels.get("close", 0)
            snapshot = self.state.get("price", 0)

            if not snapshot or not close:
                print(
                    f"✅ 啟動驗證略過（快照或日K其中一項為空）："
                    f"收盤 {close}，快照 {snapshot if snapshot else '(無快照)'}"
                )
                return True

            diff      = abs(float(close) - float(snapshot))
            threshold = float(close) * 0.05  # 5%

            if diff > threshold:
                now          = datetime.now()
                now_minutes  = now.hour * 60 + now.minute
                is_intraday  = (8 * 60 + 45) <= now_minutes <= (13 * 60 + 45)

                if is_intraday:
                    # 盤中：日K昨日收盤 vs 今日即時快照差距大為正常，警告即可
                    print(
                        f"⚠️ 注意：日K收盤 {int(round(float(close)))} vs "
                        f"即時快照 {int(round(float(snapshot)))}，差距 {diff:.0f} 點"
                    )
                    print("⚠️ 盤中啟動，日K為昨日資料，差距屬正常，繼續啟動")
                    return True
                else:
                    # 盤外：差距超過 5% 才是真正的污染
                    msg = (
                        f"[STARTUP WARNING] ATOS\n"
                        f"close={int(round(float(close)))} snapshot={int(round(float(snapshot)))}\n"
                        f"diff={diff:.0f} threshold={threshold:.0f}\n"
                        f"Please verify contract month and restart"
                    )
                    try:
                        print(msg)
                    except Exception:
                        pass
                    try:
                        tg_msg = (
                            f"🚨 ATOS 啟動警報\n"
                            f"期貨日K收盤 {int(round(float(close)))} vs "
                            f"即時快照 {int(round(float(snapshot)))}\n"
                            f"差距 {diff:.0f} 點（門檻 {threshold:.0f} 點）\n"
                            f"請確認合約月份後重啟"
                        )
                        from messenger import send_to_telegram
                        send_to_telegram(tg_msg)
                    except Exception:
                        pass
                    return False

            print(
                f"✅ 啟動驗證通過：日K收盤 {close}，"
                f"快照 {snapshot}，差距 {diff:.0f} 點，"
                f"合約 {levels.get('contract_date', 'N/A')}，"
                f"{levels.get('days_to_settlement', '?')} 天後結算"
            )
            return True

        except Exception as e:
            print(f"⚠️ startup_validation 執行失敗（非致命）：{e}")
            return True  # 驗證本身失敗不阻斷啟動

    def start(self):
        """
        啟動 ATOS Commander。
        """
        try:
            print(f"ATOS Commander starting... {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception:
            pass

        # 步驟1：啟動驗證（日K vs 快照交叉比對）
        try:
            print("--- step1: startup validation")
        except Exception:
            pass
        if not self.startup_validation():
            try:
                print("startup validation failed, halting")
            except Exception:
                pass
            return

        # 步驟2：資料回補（缺漏交易日）
        try:
            print("--- step2: data backfill")
        except Exception:
            pass
        if check_and_backfill is not None:
            try:
                check_and_backfill()
            except Exception as e:
                try:
                    print(f"data_backfill failed: {e}")
                except Exception:
                    pass

        # 步驟3：tick cache 清理（移除 3 天前的資料）
        try:
            print("--- step3: tick cache cleanup")
            from data_engine import load_txf_tick_cache, clean_txf_tick_cache, save_txf_tick_cache
            rows = load_txf_tick_cache()
            cleaned = clean_txf_tick_cache(rows)
            if len(cleaned) < len(rows):
                save_txf_tick_cache(cleaned)
                print(f"tick cache: {len(rows)} -> {len(cleaned)} rows")
        except Exception as e:
            try:
                print(f"tick cache cleanup failed: {e}")
            except Exception:
                pass

        # 步驟4：更新基礎資料（flip + 籌碼）
        try:
            print("--- step4: refresh flip + chip")
        except Exception:
            pass
        self.refresh_flip()
        self.refresh_chip()

        # 步驟5：補發今日未發報告
        try:
            print("--- step5: catchup missed reports")
        except Exception:
            pass
        self.check_and_catchup()

        # 步驟6：設定排程 + 發送啟動訊息
        try:
            print("--- step6: setup schedule")
        except Exception:
            pass
        self.setup_schedule()
        self.refresh_realtime_price()
        send_to_telegram(
            f"ATOS Commander running\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        try:
            print("ATOS Commander running...")
        except Exception:
            pass

        # 步驟7：主循環
        while True:
            try:
                schedule.run_pending()

                # 盤中監控主循環
                self.run_monitor_once()

                time.sleep(30)

            except KeyboardInterrupt:
                try:
                    print("ATOS Commander stopped by user")
                except Exception:
                    pass
                break

            except Exception as e:
                try:
                    print(f"ATOS Commander loop error: {e}")
                except Exception:
                    pass
                time.sleep(10)


# --------------------------------------------------
# Entry
# --------------------------------------------------

if __name__ == "__main__":
    commander = AtosCommander()
    commander.start()