# data_backfill.py
# 系統啟動時自動檢查並補抓缺漏的歷史籌碼資料

from datetime import datetime, timedelta


def check_and_backfill():
    """
    系統啟動時自動檢查並補抓缺漏的歷史資料。

    邏輯：
    1. 讀取 atos_state.json 中最後一次成功更新的日期
    2. 計算距今的缺漏交易日
    3. 逐日回補期貨籌碼資料
    """
    try:
        from persistent_state import load_state
        from api_rate_limiter import check_rate_limit
    except Exception as e:
        print(f"⚠️ [backfill] 模組載入失敗：{e}")
        return

    state = load_state()
    last_update = state.get("last_chip_update_date")

    if not last_update:
        print("⚠️ [backfill] 無歷史更新記錄，跳過回補")
        return

    try:
        last_date = datetime.strptime(str(last_update)[:10], "%Y-%m-%d").date()
    except Exception:
        print(f"⚠️ [backfill] 日期解析失敗：{last_update}")
        return

    today = datetime.now().date()

    if (today - last_date).days <= 1:
        print("✅ [backfill] 資料連續，無需回補")
        return

    # 找出缺漏的交易日（排除週末，簡單過濾）
    missing_days = []
    current = last_date + timedelta(days=1)
    while current < today:
        if current.weekday() < 5:  # 週一到週五
            missing_days.append(current)
        current += timedelta(days=1)

    if not missing_days:
        print("✅ [backfill] 缺漏日期均為非交易日，無需回補")
        return

    print(f"🔄 [backfill] 偵測到 {len(missing_days)} 個缺漏交易日，開始回補...")

    try:
        from data_engine import get_finmind_api
        api = get_finmind_api()
    except Exception as e:
        print(f"⚠️ [backfill] 無法取得 API：{e}")
        return

    success = 0
    for d in missing_days:
        date_str = d.strftime("%Y-%m-%d")
        print(f"  [backfill] 回補 {date_str}...")
        try:
            check_rate_limit()
            df = api.get_data(
                dataset="TaiwanFuturesInstitutionalInvestors",
                data_id="TX",
                start_date=date_str,
                end_date=date_str,
            )
            if df is not None and not df.empty:
                print(f"  ✅ {date_str} 期貨籌碼回補完成（{len(df)} 筆）")
                success += 1
            else:
                print(f"  ⚠️ {date_str} 回補：資料為空（可能為非交易日）")
        except Exception as e:
            print(f"  ⚠️ {date_str} 回補失敗：{e}")

    print(f"✅ [backfill] 資料回補完成（{success}/{len(missing_days)} 日成功）")
