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
            return {
                "weekly_settlement": [],
                "monthly_settlement": [],
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

            if levels.get("pivot"):
                self.state["pivot"] = levels.get("pivot")
                self.sentinel.state["pivot"] = levels.get("pivot")

            if levels.get("R1"):
                self.state["r1"] = levels.get("R1")
                self.sentinel.state["r1"] = levels.get("R1")

            if levels.get("S1"):
                self.state["s1"] = levels.get("S1")
                self.sentinel.state["s1"] = levels.get("S1")

            high = levels.get("high")
            low = levels.get("low")
            if high and low:
                mid_range = round((high + low) / 2, 1)
                self.state["mid_range"] = mid_range
                self.sentinel.state["mid_range"] = mid_range

        save_state(self.state)

        print(f"✅ prev_close 更新完成：{prev_close}")
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
        print("✅ [evening] 晚盤報告發送旗標已重置")

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

        # 夜盤收盤資料更新
        schedule.every().day.at("05:03").do(self.update_night_close)
        schedule.every().day.at("08:20").do(self.update_night_close)

        # 盤前資料更新
        schedule.every().day.at("08:00").do(self.refresh_flip)
        schedule.every().day.at("08:30").do(self.refresh_chip)

        # 盤前報告
        schedule.every().day.at("08:35").do(self.send_preopen_report)

        # 個股觀察報告
        schedule.every().day.at("08:40").do(self.send_stock_report)

        # 開盤提示
        schedule.every().day.at("08:45").do(self.send_opening_bell)

        # 盤後 / 夜盤前資料更新
        schedule.every().day.at("13:50").do(self.refresh_chip)
        schedule.every().day.at("14:50").do(self.refresh_flip)
        # 晚盤報告：14:55 重置旗標，每 2 分鐘輪詢資料就緒後發送
        schedule.every().day.at("14:55").do(self.reset_evening_sent)
        schedule.every(2).minutes.do(self.check_and_send_evening_report)

        # 晚盤選股複盤報告
        schedule.every().day.at("15:10").do(self.send_evening_stock_report)

        # 額外保險：15:30 再更新一次籌碼
        schedule.every().day.at("15:30").do(self.refresh_chip)

        # 重大新聞輪詢（每 5 分鐘）
        schedule.every(5).minutes.do(self.poll_news)

        # 週度績效報告（每週日 20:00）
        schedule.every().sunday.at("20:00").do(self.send_weekly_performance_report)

        print("✅ ATOS schedule setup completed")

    # --------------------------------------------------
    # Start
    # --------------------------------------------------

    def start(self):
        """
        啟動 ATOS Commander。
        """

        # 資料回補檢查
        if check_and_backfill is not None:
            try:
                check_and_backfill()
            except Exception as e:
                print(f"⚠️ data_backfill failed: {e}")

        # tick cache 啟動清理（移除 3 天前的資料）
        try:
            from data_engine import load_txf_tick_cache, clean_txf_tick_cache, save_txf_tick_cache
            rows = load_txf_tick_cache()
            cleaned = clean_txf_tick_cache(rows)
            if len(cleaned) < len(rows):
                save_txf_tick_cache(cleaned)
                print(f"🧹 tick cache 清理：{len(rows)} → {len(cleaned)} 筆")
        except Exception as e:
            print(f"⚠️ tick cache 清理失敗：{e}")

        self.send_startup_message()
        self.setup_schedule()

        print("🛡️ ATOS Commander running...")

        while True:
            try:
                schedule.run_pending()

                # 盤中監控主循環
                self.run_monitor_once()

                time.sleep(30)

            except KeyboardInterrupt:
                print("🛑 ATOS Commander stopped by user")
                break

            except Exception as e:
                print(f"💥 ATOS Commander loop error: {e}")
                time.sleep(10)


# --------------------------------------------------
# Entry
# --------------------------------------------------

if __name__ == "__main__":
    commander = AtosCommander()
    commander.start()