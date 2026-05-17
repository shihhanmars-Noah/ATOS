# ai_formatter.py

import os
from dotenv import load_dotenv
from google import genai

from error_handler import safe_execute

load_dotenv()


@safe_execute
def format_report_with_ai(raw_report: str, style: str = "simple") -> str:
    """
    AI 只負責「翻譯與排版」。
    不負責策略判斷、不新增點位、不改變風控。

    Args:
        raw_report: report_engine 產出的原始報告
        style:
            simple = 簡單明瞭
            tactical = 指令式戰術報告

    Returns:
        格式化後的文字
    """

    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        raise ValueError("GEMINI_API_KEY 未設定")

    client = genai.Client(api_key=api_key)

    prompt = f"""
你是 ATOS 報告編譯官。

你的任務：
1. 只改寫語氣與排版。
2. 不准新增任何點位。
3. 不准改變多空判斷。
4. 不准加入原報告沒有的資訊。
5. 不准把觀察訊號寫成進場指令。
6. 保留所有數字、風控、允許與禁止條件。

風格：{style}

原始報告如下：
{raw_report}

請輸出一份簡單、明瞭、可盤中閱讀的 ATOS 報告。
"""

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
    )

    return response.text


def format_report_without_ai(raw_report: str) -> str:
    """
    AI 失敗時直接回傳原始報告。
    """

    return raw_report


def generate_final_report(raw_report: str, use_ai: bool = True) -> str:
    """
    產生最終報告。

    若 AI 失敗：
    回傳原始 report_engine 報告。
    """

    if not use_ai:
        return format_report_without_ai(raw_report)

    ai_report = format_report_with_ai(raw_report)

    if ai_report is None:
        return format_report_without_ai(raw_report)

    return ai_report