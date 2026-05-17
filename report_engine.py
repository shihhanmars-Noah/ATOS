from datetime import datetime
from target_engine import calculate_trade_plan
from calendar_engine import get_event_mode, describe_event_mode
from risk_engine import get_risk_protocol
from chip_data_engine import load_chip_cache, describe_chip_background
from persistent_state import load_state
from stock_report_engine import build_stock_watchlist
# 匯入數據接口
from data_engine import get_institutional_sentiment, get_dynamic_resistance_support

# --- 工具：安全字串處理 ---
def safe_text(text):
    """移除所有會干擾 Telegram Markdown 解析的符號"""
    if not text: return "N/A"
    # 移除底線、星號、反引號等
    return str(text).replace("_", " ").replace("*", "").replace("`", "")

# --- AI 點評邏輯 ---
def get_ai_stock_judgment(stock_id: str, regime: str) -> str:
    logic_map = {
        "2330": "權值靈魂。觀察 ADR 溢價動能，注意資金排擠效應。",
        "2317": "老牌火種。受 AI 題材帶動，法人回補穩定，可作情緒指標。",
        "3017": "熱能指標。散熱族群股性活潑，回測均線不破視為強勢。",
    }
    return logic_map.get(stock_id, "籌碼剛起步。觀察開盤 15 分鐘站穩力道。")

# --- 核心指令邏輯 ---
def build_core_instruction(state: dict, risk: dict, sentiment: str = "") -> str:
    regime = state.get("regime", "")
    behavior = state.get("behavioral_regime", "")
    
    # 1. 優先判定陷阱
    if behavior and "TRAP" in behavior and behavior != "NO_TRAP":
        return f"🔥 偵測到 {safe_text(behavior)}，大戶正在收割，嚴禁反向開倉！"
    
    # 2. 結合大戶情緒與物理方向
    if "多方" in regime:
        if "空" in sentiment:
            return "⚠️ 物理多方但【大戶留倉偏空】，小心高位震盪，不宜追價。"
        return "🟢 物理多方穩固，目前無陷阱。順勢操作，嚴禁猜頭。"
    
    if "空方" in regime:
        return "🔴 物理空方壓制。反彈無力即是壓力，觀察破底動能。"
        
    return "🟡 價格位於緩衝區，暫無優勢，不猜方向。"

# --- 08:00 開盤前哨報告 ---
def generate_morning_picks_report() -> str:
    state = load_state()
    risk = get_risk_protocol()
    picks = get_institutional_picks(top_n=3)
    sentiment = get_institutional_sentiment()
    levels = get_dynamic_resistance_support()
    
    safe_sentiment = safe_text(sentiment)
    
    pick_text = ""
    if picks:
        for i, p in enumerate(picks, 1):
            tag = " [快取]" if p.get("is_cache") else ""
            ai_comment = get_ai_stock_judgment(p['id'], state.get('regime', ''))
            pick_text += f"{i}. 📌 **{p['id']}**：買超 {p['net_buy']} 張{tag}\n"
            pick_text += f"   🤖 AI點評：{ai_comment}\n\n"
    else:
        pick_text = "目前無符合連買標的。\n\n"

    report = (
        "📈 **ATOS 開盤前哨 (AI支援)**\n"
        f"**環境**：{safe_text(state.get('regime'))} | **風險**：{risk['risk_multiplier']}x\n"
        f"**大戶底牌**：{safe_sentiment}\n"
        "--- \n\n"
        "**🚀 法人強勢標的分析**\n"
        f"{pick_text}"
        "**📍 戰場點位預判**\n"
        f"● **壓力 R1**：`{levels.get('R1', 0)}`\n"
        f"● **支撐 S1**：`{levels.get('S1', 0)}`\n"
        "--- \n\n"
        "**💡 指揮官指令**\n"
        f"> {build_core_instruction(state, risk, safe_sentiment)}"
    )
    return report

# --- 每日/即時戰術監控報告 ---
def build_daily_report(label="ATOS 每日戰術", state=None, chip=None):
    state = state or load_state()
    sentiment = get_institutional_sentiment()
    levels = get_dynamic_resistance_support()
    
    safe_sentiment = safe_text(sentiment)
    
    return (
        f"🛡️ **{label}**\n"
        f"**時間**：{datetime.now().strftime('%H:%M')} | **行為**：{safe_text(state.get('behavioral_regime'))}\n"
        f"**大戶情緒**：{safe_sentiment}\n"
        "--- \n\n"
        "**📍 實時戰場分佈**\n"
        f"● **現價**：`{state.get('price')}`\n"
        f"● **壓力 R1**：`{levels.get('R1', 0)}`\n"
        f"● **分界 Flip**：`{state.get('flip')}`\n"
        f"● **支撐 S1**：`{levels.get('S1', 0)}`\n\n"
        "**💡 指揮官指令**\n"
        f"> {build_core_instruction(state, get_risk_protocol(), safe_sentiment)}"
    )

# --- 夜盤戰報 ---
def generate_night_report():
    state = load_state()
    sentiment = get_institutional_sentiment()
    safe_sentiment = safe_text(sentiment)
    
    return (
        "🌙 **ATOS 夜盤戰報**\n"
        f"**現價**：`{state.get('price')}`\n"
        f"**物理方向**：{safe_text(state.get('regime'))}\n"
        f"**大戶底牌**：{safe_sentiment}\n"
        "--- \n"
        "💡 夜盤波動大，請守穩 Flip 分界點。"
    )

generate_night_tactical_brief = generate_night_report