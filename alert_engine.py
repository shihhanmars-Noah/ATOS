# alert_engine.py

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

# 同一類別警報 5 分鐘內不重複發送，避免洗版
ALERT_COOLDOWN = 300  
_last_alert_time = {}

def can_send_alert(alert_key: str, cooldown: int = ALERT_COOLDOWN) -> bool:
    now = time.time()
    last = _last_alert_time.get(alert_key, 0)
    if now - last > cooldown:
        _last_alert_time[alert_key] = now
        return True
    return False

def send_telegram_alert(message: str, alert_key: str, cooldown: int = ALERT_COOLDOWN) -> bool:
    """發送 Telegram 警報，含冷卻機制"""
    if not can_send_alert(alert_key, cooldown):
        return False

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        res = requests.post(url, data={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
        return res.status_code == 200
    except:
        return False

# --- 警報文字構建器 ---

def build_header(event_mode: str) -> str:
    """根據模式顯示標頭，提醒風險權限"""
    icons = {
        "NORMAL_MODE": "✅ [常規環境]",
        "SETTLEMENT_MODE": "⚠️ [週結算：部位減半]",
        "HIGH_GAMMA_MODE": "🔥 [月結算：嚴禁追價]",
        "EVENT_DEFENSE": "🚨 [事件防禦：禁止開倉]"
    }
    return icons.get(event_mode, "🔍 [環境重估中]")

def build_trap_alert(data: dict) -> str:
    """紅色警報：Trap 成立 (背景+行為共振)"""
    t_type = data['trap']
    header = build_header(data['event_mode'])
    
    msg = f"🔴 **ATOS 紅色戰術警報：{t_type}**\n{header}\n---\n"
    if t_type == "LONG_TRAP":
        msg += "🎯 誘多失敗：大戶利用散戶追多單進行反向收割。\n"
    else:
        msg += "🎯 誘空失敗：大戶利用散戶停損單進行軋空拉抬。\n"
    
    msg += f"\n現價：`{data['price']}` | 中軸：`{data['flip']}`"
    msg += f"\n散戶比：`{data['retail_ratio']}%` | 掃單位：`{data['sweep_level']}`"
    msg += f"\n\n👉 **核心指令**：不要對抗大戶！已有反向單應立即撤退，等待 5分K 確認慣性後換手。"
    return msg

def build_flip_invalid_alert(data: dict) -> str:
    """狀態警報：Flip 失效 (物理層損毀)"""
    header = build_header(data['event_mode'])
    msg = f"🛡️ **風控預警：物理協議失效**\n{header}\n---\n"
    msg += f"5分K 確認{'跌破' if '多方' in data['regime'] else '站回'} 中軸分界點。\n"
    msg += f"\n收盤價：`{data['price']}` | 中軸：`{data['flip']}`"
    msg += f"\n\n👉 **核心指令**：原慣性已變，多/空方協議終止。**全面轉為防守**，重新標定重心。"
    return msg

# --- 主調度邏輯 ---

def handle_market_alerts(context: dict):
    """
    處理所有警報邏輯
    context 範例: {
        "price": 41380, "flip": 41324, "regime": "🟢 多方",
        "behavior": {"sweep": "BULLISH_SWEEP", "trap": "NONE", ...},
        "event_mode": "SETTLEMENT_MODE", "is_flip_broken": False
    }
    """
    behavior = context.get("behavior", {})
    
    # 1. 優先順序最高：紅色 Trap 警報 (行為 + 籌碼)
    if behavior.get("trap") in ["LONG_TRAP", "SHORT_TRAP"]:
        msg = build_trap_alert({
            "trap": behavior["trap"],
            "price": context["price"],
            "flip": context["flip"],
            "retail_ratio": behavior.get("retail_ratio", 0),
            "sweep_level": behavior.get("sweep_level"),
            "event_mode": context["event_mode"]
        })
        send_telegram_alert(msg, alert_key=behavior["trap"])
        return

    # 2. 次高：Flip 失效警報 (物理層)
    if context.get("is_flip_broken"):
        msg = build_flip_invalid_alert(context)
        send_telegram_alert(msg, alert_key="FLIP_INVALID")
        return

    # 3. 行為層：Sweep 觀察 (橘色)
    sweep = behavior.get("sweep")
    if sweep in ["BEARISH_SWEEP", "BULLISH_SWEEP"]:
        header = build_header(context["event_mode"])
        msg = f"🟠 **ATOS 行為觀察：掃單偵測**\n{header}\n---\n"
        msg += f"類型：`{sweep}` | 位置：`{behavior.get('sweep_level')}`"
        msg += f"\n\n👉 **行動建議**：大戶正在掃蕩流動性，**禁止盲目追價**，等待 5分K 方向定調。"
        send_telegram_alert(msg, alert_key=f"SWEEP_{sweep}")
def build_behavior_alert(
    behavior: dict,
    price: float,
    flip: float,
    retail_ratio: float,
) -> tuple[str | None, str | None]:
    """
    相容 monitor_engine.py 的函式。
    將 behavior 結果轉成 message + alert_key。
    """

    trap = behavior.get("trap")
    sweep = behavior.get("sweep")
    sweep_level = behavior.get("sweep_level")
    retail_bias = behavior.get("retail_bias")

    if trap in ["LONG_TRAP", "SHORT_TRAP"]:
        msg = build_trap_alert({
            "trap": trap,
            "price": price,
            "flip": flip,
            "retail_ratio": retail_ratio,
            "sweep_level": sweep_level,
            "event_mode": behavior.get("event_mode", "NORMAL_MODE"),
        })
        return msg, trap

    if sweep in ["BEARISH_SWEEP", "BULLISH_SWEEP"]:
        msg = (
            "🟠 **ATOS 行為觀察：掃單偵測**\n"
            "---\n"
            f"類型：`{sweep}` | 位置：`{sweep_level}`\n\n"
            "👉 **行動建議**：大戶正在掃蕩流動性，禁止盲目追價，等待 5分K 方向定調。"
        )
        return msg, f"SWEEP_{sweep}"

    if retail_bias in ["RETAIL_LONG_CROWDED", "RETAIL_SHORT_CROWDED"]:
        msg = (
            "🟡 **ATOS 觀察警報：籌碼背景擁擠**\n"
            "---\n"
            f"散戶背景：`{retail_bias}`\n"
            f"說明：{behavior.get('retail_desc')}\n\n"
            "👉 目前只是背景警示，尚未形成進場或反向訊號。"
        )
        return msg, f"BIAS_{retail_bias}"

    return None, None