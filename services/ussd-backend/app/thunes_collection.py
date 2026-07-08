"""
Thunes Accept API -- Tier 2 collection. HOST_TO_HOST mode triggers a USSD push
or STK-style prompt directly on the sender's feature phone, no smartphone or
internet required. Functionally equivalent to Onafriq's send_instructions
collection, but through Thunes' own DGN -- meaning when both this and
ThunesPayoutAdapter are configured, Vuka can run on a single Thunes account
for both legs of a non-Safaricom transaction.

Distinct credential from the payout side: THUNES_MERCHANT_ID (Accept API's own
merchant identifier) vs THUNES_PAYER_ID (Money Transfer API). Both draw on the
same THUNES_API_KEY/SECRET.

Falls back to a clearly-logged simulation when THUNES_MERCHANT_ID isn't set --
same posture as every other adapter in this codebase.
"""
import logging

import requests

from . import config

logger = logging.getLogger("vuka.thunes_collection")

_BASE_URLS = {
    "pre-production": "https://api.thunes.com/v1",
    "production": "https://api.thunes.com/v1",
}


def _base_url() -> str:
    return _BASE_URLS.get(config.THUNES_ENVIRONMENT, _BASE_URLS["pre-production"])


def _auth_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "X-Api-Key": config.THUNES_API_KEY,
        "X-Api-Secret": config.THUNES_API_SECRET,
    }


def trigger_collection(tx_id: str, phone_number: str, amount: float, currency: str,
                         carrier: str = None) -> dict:
    if not config.THUNES_ACCEPT_CONFIGURED:
        logger.info(
            "thunes_collection: SIMULATED collection tx=%s phone=%s amount=%s %s carrier=%s",
            tx_id, phone_number, amount, currency, carrier,
        )
        return {
            "status": "confirmed",
            "method": "simulated",
            "reference": f"SIM-THUNES-ACCEPT-{tx_id[:8]}",
            "simulated": True,
        }

    try:
        resp = requests.post(
            f"{_base_url()}/payment/payment-orders",
            headers=_auth_headers(),
            json={
                "merchant_id": config.THUNES_MERCHANT_ID,
                "external_id": tx_id,
                "mode": "HOST_TO_HOST",
                "payer": {"msisdn": phone_number, "carrier": carrier},
                "amount": amount,
                "currency": currency,
                "callback_url": config.THUNES_COLLECTION_CALLBACK_URL,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "status": "pending",  # resolved later via /ussd/webhook/thunes-collection-callback
            "method": "thunes_accept",
            "reference": data.get("id") or data.get("external_id"),
            "simulated": False,
        }
    except requests.RequestException:
        logger.exception("thunes_collection: payment-order request failed for tx %s", tx_id)
        return {"status": "failed", "method": "thunes_accept", "reference": None, "simulated": False}
