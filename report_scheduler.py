# report_scheduler.py

import time

from chip_data_engine import (
    update_chip_cache,
    load_chip_cache,
)

from report_engine import generate_night_report
from messenger import send_to_telegram


def is_chip_data_ready(chip):

    if not chip:
        return False

    required = [
        "foreign_net",
        "retail_ratio",
        "call_wall",
        "put_support",
        "updated_at"
    ]

    for key in required:
        if key not in chip:
            return False

    if chip["updated_at"] is None:
        return False

    return True


def send_night_report_when_ready(max_wait_minutes=30):

    start_time = time.time()

    while True:

        elapsed = time.time() - start_time

        if elapsed > max_wait_minutes * 60:

            send_to_telegram(
                "⚠️ ATOS 夜盤報告取消：資料更新逾時。"
            )

            return False

        # 更新籌碼
        update_chip_cache()

        chip = load_chip_cache()

        # 資料完整才發送
        if is_chip_data_ready(chip):

            report = generate_night_report()

            send_to_telegram(report)

            return True

        # 每分鐘檢查一次
        time.sleep(60)