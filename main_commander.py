# main_commander.py

import time
from datetime import datetime

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


load_dotenv()


# --------------------------------------------------
# Helper
# --------------------------------------------------

def format_price(value):
    if value is None:
        return "N/A"

    try:
        return round(float(value), 1)
    except Exception:
        return value


def build_tactical_card_message(state: dict) -> str:
    """
    建立即時戰術卡。

    注意：
    - 主價格應來自 FINMIND_FUTURES_SNAPSHOT
    - 若是 Fugle backup，代表不是台指期即時價，不應交易
    """

    price = state.get("price")
    flip = state.get("flip")
    levels = state.get("levels", {}) or state.get("daily_levels", {}) or {}

    r1 = (
        state.get("r1")
        or levels.get("R1")
    )

    s1 = (
        state.get("s1")
        or levels.get("S1")
    )

    pivot = (
        state.get("pivot")
        or levels.get("pivot")
    )

    sentiment = (
        state.get("sentiment")
        or state.get("institutional_sentiment")
        or "N/A"
    )

    tick_source = state.get("tick_source", "N/A")
    tick_time = state.get("tick_time", "N/A")
    is_realtime = state.get("is_realtime", False)

    if tick_source == "FINMIND_FUTURES_SNAPSHOT":
        price_label = "台指期即時價 TXF"
        realtime_text = "REALTIME"
        warning_text = ""
    else:
        price_label = "備援參考價"
        realtime_text = "NOT_REALTIME"
        warning_text = (
            "\n⚠️ 注意：目前不是 FinMind TXF 即時價，"
            "只允許觀察，不允許交易。\n"
        )

    if is_realtime and price and flip:
        try:
            if float(price) > float(flip):
                instruction = "🟢 價格站在 Flip 上方，偏多觀察，但不追高，等回測確認。"
            elif float(price) < float(flip):
                instruction = "🔴 價格在 Flip 下方，偏空觀察，但不追低，等反彈不過。"
            else:
                instruction = "🟡 價格貼近 Flip，中性洗盤區，不急著進場。"
        except Exception:
            instruction = "⚪ 價格判斷失敗，等待下一筆資料。"
    else:
        instruction = "⚠️ 價格來源非 realtime 或關鍵價不足，禁止交易，只允許觀察。"

    msg = (
        "🛡️ ATOS 即時戰術卡\n"
        f"時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

        "━━━━━━━━━━━━━━\n"
        "一、即時狀態\n"
        "━━━━━━━━━━━━━━\n\n"

        f"● {price_label}：{format_price(price)}\n"
        f"● 價格來源：{tick_source} / {tick_time}\n"
        f"● 即時狀態：{realtime_text}\n"
        f"{warning_text}\n"

        "━━━━━━━━━━━━━━\n"
        "二、戰場地圖\n"
        "━━━━━━━━━━━━━━\n\n"

        f"● 上方壓力 R1：{format_price(r1)}\n"
        f"● 多空分界 Flip：{format_price(flip)}\n"
        f"● 盤中重心 Pivot：{format_price(pivot)}\n"
        f"● 下方支撐 S1：{format_price(s1)}\n\n"

        "━━━━━━━━━━━━━━\n"
        "三、籌碼背景\n"
        "━━━━━━━━━━━━━━\n\n"

        f"{sentiment}\n\n"

        "━━━━━━━━━━━━━━\n"
        "四、指揮官指令\n"
        "━━━━━━━━━━━━━━\n\n"

        f"> {instruction}"
    )

    return msg


# --------------------------------------------------
# Main Commander
# --------------------------------------------------

class AtosCommander:
    def __init__(self):
        self.state = load_state()
        self.sentinel = AtosSentinel()

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
        更新市場分界 Flip，並同步儲存前一交易日主力期貨收盤資料。
        """

        old_flip = self.state.get("flip", 0)

        flip = get_flip_level(
            futures_id="TX",
            fallback=old_flip,
        )

        levels = get_dynamic_resistance_support(
            futures_id="TX",
        )

        if flip:
            self.state["flip"] = flip
            self.sentinel.state["flip"] = flip

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

        save_state(self.state)

        print(f"✅ Flip 更新完成：{flip}")
        return flip

    @safe_execute
    def refresh_chip(self):
        """
        更新法人期貨情緒。
        """

        sentiment = get_institutional_sentiment()

        self.state["sentiment"] = sentiment
        self.state["institutional_sentiment"] = sentiment

        self.sentinel.state["sentiment"] = sentiment
        self.sentinel.state["institutional_sentiment"] = sentiment

        save_state(self.state)

        print(f"✅ 籌碼情緒更新完成：{sentiment}")

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
        啟動訊息。
        """

        self.refresh_flip()
        self.refresh_realtime_price()
        self.refresh_chip()

        msg = build_tactical_card_message(self.state)

        send_to_telegram(
            "🛡️ ATOS Commander 啟動成功\n\n"
            f"{msg}"
        )

    @safe_execute
    def send_daily_status(self):
        """
        即時狀態戰術卡。
        """

        self.refresh_realtime_price()
        self.refresh_chip()

        msg = build_tactical_card_message(self.state)

        send_to_telegram(msg)

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
    def send_evening_report(self):
        """
        發送 ATOS 晚盤作戰報告。
        """

        if send_evening_report_func is None:
            send_to_telegram(
                "⚠️ 晚盤報告未啟用\n"
                "找不到 evening_report_engine.py 或 send_evening_report。"
            )
            return

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

    # --------------------------------------------------
    # Monitor
    # --------------------------------------------------

    @safe_execute
    def run_monitor_once(self):
        """
        執行一次盤中監控。
        """

        self.sentinel.monitor_loop()

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

        # 盤後 / 夜盤前資料更新
        schedule.every().day.at("13:50").do(self.refresh_chip)
        schedule.every().day.at("14:50").do(self.refresh_flip)
        schedule.every().day.at("15:05").do(self.send_evening_report)

        # 晚盤選股複盤報告
        schedule.every().day.at("15:10").do(self.send_evening_stock_report)

        # 額外保險：15:30 再更新一次籌碼
        schedule.every().day.at("15:30").do(self.refresh_chip)

        # 重大新聞輪詢（每 5 分鐘）
        schedule.every(5).minutes.do(self.poll_news)

        print("✅ ATOS schedule setup completed")

    # --------------------------------------------------
    # Start
    # --------------------------------------------------

    def start(self):
        """
        啟動 ATOS Commander。
        """

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