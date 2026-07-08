"""
Onafriq hub collection -- one integration covers MTN, Airtel, Vodacom, Orange,
Tigo, Moov, Wave, etc. Falls back to simulation when ONAFRIQ_API_KEY isn't set.
"""
import logging

import requests

from . import config

logger = logging.getLogger("vuka.onafriq")


def trigger_collection(tx_id: str, phone_number: str, amount: float, currency: str, carrier: str = None) -> dict:
    if not config.ONAFRIQ_CONFIGURED:
        logger.info(
            "onafriq: SIMULATED collection tx=%s phone=%s amount=%s %s carrier=%s",
            tx_id, phone_number, amount, currency, carrier,
        )
        return {
            "status": "confirmed",
            "method": "simulated",
            "reference": f"SIM-ONAFRIQ-{tx_id[:8]}",
            "simulated": True,
        }

    try:
        resp = requests.post(
            f"{config.ONAFRIQ_BASE_URL}/collections",
            headers={
                "Authorization": f"Bearer {config.ONAFRIQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "external_id": tx_id,
                "msisdn": phone_number,
                "amount": amount,
                "currency": currency,
                "carrier": carrier,
                "callback_url": config.ONAFRIQ_CALLBACK_URL,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "status": "pending",  # resolved later by ONAFRIQ_CALLBACK_URL webhook
            "method": "onafriq",
            "reference": data.get("reference") or data.get("id"),
            "simulated": False,
        }
    except requests.RequestException:
        logger.exception("onafriq: collection request failed for tx %s", tx_id)
        return {"status": "failed", "method": "onafriq", "reference": None, "simulated": False}
