"""
Rafiki (NALA) payout adapter -- a REDUNDANT FALLBACK rail, not a replacement
for Thunes DGN. Thunes stays primary; this only gets called when a live
Thunes dispatch attempt actually fails at runtime (timeout, 5xx, network
error) -- see bank_adapters.dispatch_payout()'s retry logic.

This is a genuinely different pattern from the existing Thunes-vs-bank-
partner-fallback choice in bank_adapters.py, which picks a rail once based
on what's CONFIGURED. This module exists for the case where Thunes IS
configured and normally works, but a specific dispatch attempt fails --
redundancy against transient provider outages, not a permanent alternative.

HONEST GAP: NALA has not published a public developer API reference as of
this writing -- what's documented publicly is the product's existence and
value proposition (direct bank/mobile money integration across NALA's
network, built specifically for payout reliability), not a technical
endpoint contract. The request/response shape below is Vuka's own
best-guess placeholder, modeled on the same quote-then-dispatch pattern
Thunes uses -- it WILL need confirming/adjusting against NALA's actual API
docs once a commercial relationship and technical docs access exist. Never
treat this as a verified integration until that happens.

Falls back to a clearly-logged simulation when not configured -- same
posture as every other external adapter in this codebase.
"""
import logging

import requests

from . import config

logger = logging.getLogger("vuka.rafiki")

_BASE_URL = "https://api.rafiki.nala.money/v1"  # PLACEHOLDER -- confirm against NALA's real docs


def _auth_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.RAFIKI_API_KEY}",
        "X-Rafiki-Secret": config.RAFIKI_SECRET,
    }


def dispatch_payout(tx_id: str, corridor: str, recipient_phone: str, recipient_name: str,
                     payout_amount: float, payout_currency: str) -> dict:
    if not config.RAFIKI_CONFIGURED:
        logger.info(
            "rafiki: SIMULATED fallback payout tx=%s corridor=%s amount=%s %s to %s",
            tx_id, corridor, payout_amount, payout_currency, recipient_phone,
        )
        return {
            "status": "confirmed",
            "method": "rafiki_simulated",
            "reference": f"SIM-RAFIKI-{tx_id[:8]}",
            "simulated": True,
        }

    try:
        resp = requests.post(
            f"{_BASE_URL}/payouts",
            headers=_auth_headers(),
            json={
                "external_id": tx_id,
                "corridor": corridor,
                "beneficiary": {"msisdn": recipient_phone, "name": recipient_name},
                "amount": payout_amount,
                "currency": payout_currency,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "status": "pending",
            "method": "rafiki",
            "reference": data.get("id") or data.get("reference"),
            "simulated": False,
        }
    except requests.RequestException:
        logger.exception("rafiki: fallback payout ALSO failed for tx %s -- both rails down", tx_id)
        return {"status": "failed", "method": "rafiki", "reference": None, "simulated": False}
