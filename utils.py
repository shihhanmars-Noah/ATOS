# utils.py
# 全域共用工具函式，避免各模組重複定義

def format_price(value):
    """格式化價位：保留一位小數，None 回傳 'N/A'。"""
    if value is None:
        return "N/A"
    try:
        return round(float(value), 1)
    except Exception:
        return value


def format_price_int(value) -> str:
    """格式化台指期價位：強制整數，無小數點。用於報告顯示。"""
    if value is None:
        return "N/A"
    try:
        return str(int(round(float(value))))
    except Exception:
        return "N/A"


def safe_float(value, default=0.0) -> float:
    """安全轉換為 float，失敗回傳 default。"""
    try:
        return float(value)
    except Exception:
        return default


def safe_int(value, default=0) -> int:
    """安全轉換為 int，失敗回傳 default。"""
    try:
        return int(value)
    except Exception:
        return default
