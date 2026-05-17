# messenger.py

import os
import requests
from dotenv import load_dotenv

# 載入 .env 中的 TOKEN 與 CHAT_ID
load_dotenv()


def get_telegram_targets() -> list[str]:
    """
    讀取 Telegram 目標 ID。

    .env 支援格式：
    TELEGRAM_CHAT_ID=6156445565

    也支援額外：
    TELEGRAM_GROUP_CHAT_ID=-5130274799
    """

    chat_id_raw = os.getenv("TELEGRAM_CHAT_ID", "")
    group_chat_id = os.getenv("TELEGRAM_GROUP_CHAT_ID", "")

    targets = []

    # TELEGRAM_CHAT_ID 支援逗號分隔多個 ID
    if chat_id_raw:
        for cid in chat_id_raw.split(","):
            cid = cid.strip()
            if cid:
                targets.append(cid)

    # 額外支援 GROUP_CHAT_ID，避免舊 .env 失效
    if group_chat_id:
        group_chat_id = group_chat_id.strip()
        if group_chat_id and group_chat_id not in targets:
            targets.append(group_chat_id)

    # 去重但保留順序
    unique_targets = []
    for cid in targets:
        if cid not in unique_targets:
            unique_targets.append(cid)

    return unique_targets


def send_single_message(chat_id: str, message: str) -> bool:
    """
    發送單一 Telegram 訊息。

    重點：
    - 不使用 Markdown
    - 純文字最穩定
    - 避免 * _ [ ] ` > 等符號造成 Telegram parse error
    """

    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token:
        print("❌ [Messenger] 錯誤：找不到 TELEGRAM_BOT_TOKEN")
        return False

    if not chat_id:
        print("❌ [Messenger] 錯誤：chat_id 為空")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": str(message),
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(
            url,
            data=payload,
            timeout=10,
        )

        if response.status_code == 200:
            print(f"✅ [Messenger] 訊息已成功發送至 ID: {chat_id}")
            return True

        print(
            f"❌ [Messenger] 發送至 ID {chat_id} 失敗："
            f"{response.status_code} {response.text}"
        )
        return False

    except Exception as e:
        print(f"💥 [Messenger] 發送至 ID {chat_id} 時發生異常：{e}")
        return False


def send_to_telegram(message: str) -> bool:
    """
    發送訊息到 Telegram。

    支援多重 ID。

    .env 範例：
    TELEGRAM_BOT_TOKEN=xxxxx
    TELEGRAM_CHAT_ID=6156445565,-5130274799

    注意：
    - 預設純文字
    - 不使用 Markdown
    - 不會再因 Markdown 格式錯誤而重發
    """

    if message is None:
        print("❌ [Messenger] 錯誤：message 為 None")
        return False

    targets = get_telegram_targets()

    if not targets:
        print("❌ [Messenger] 錯誤：找不到有效的 TELEGRAM_CHAT_ID")
        return False

    all_success = True

    for chat_id in targets:
        success = send_single_message(
            chat_id=chat_id,
            message=message,
        )

        if not success:
            all_success = False

    return all_success


# --------------------------------------------------
# Manual Test
# --------------------------------------------------

if __name__ == "__main__":
    send_to_telegram("✅ ATOS Messenger 測試成功：純文字模式正常。")