import os
import logging
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "8346103624:AAHxyb--C36GWhscHXp5fwWnPYPvSZyGKBQ")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7579338347")

logger = logging.getLogger(__name__)


def send_telegram(message: str) -> bool:
    """텔레그램 메시지 전송. 실패 시 False 반환 (예외 발생 안 함)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("텔레그램 토큰/채팅ID 미설정")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=5,
        )
        if resp.status_code != 200:
            logger.warning(f"텔레그램 전송 실패 status={resp.status_code} body={resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"텔레그램 전송 오류: {e}")
        return False
