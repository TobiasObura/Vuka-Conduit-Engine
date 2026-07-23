"""
WhatsApp Business API low-float alerts. Called from the dashboard when a
corridor's float pool balance drops below its low_threshold. Falls back to a
logged simulation when WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID /
TREASURY_WHATSAPP_NUMBER aren't all set.
"""
import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("vuka.whatsapp_alerts")

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
TREASURY_WHATSAPP_NUMBER = os.environ.get("TREASURY_WHATSAPP_NUMBER")

CONFIGURED = bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID and TREASURY_WHATSAPP_NUMBER)


def send_low_float_alert(corridor: str, balance: float, currency: str, threshold: float) -> dict:
    message = (
        f"\u26a0\ufe0f Vuka float alert: {corridor} balance is {balance:,.2f} {currency}, "
        f"below the {threshold:,.2f} {currency} threshold. Top up soon."
    )

    if not CONFIGURED:
        logger.info("whatsapp_alerts: SIMULATED alert -> %s", message)
        return {"sent": False, "simulated": True, "message": message}

    try:
        resp = requests.post(
            f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_NUMBER_ID}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "to": TREASURY_WHATSAPP_NUMBER,
                "type": "text",
                "text": {"body": message},
            },
            timeout=10,
        )
        resp.raise_for_status()
        return {"sent": True, "simulated": False, "message": message}
    except requests.RequestException:
        logger.exception("whatsapp_alerts: send failed")
        return {"sent": False, "simulated": False, "error": True, "message": message}
