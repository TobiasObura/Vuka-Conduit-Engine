"""
Per-corridor bank partner integration: payout fallback (only used when Thunes
isn't configured) + delegated compliance screening (an independent opt-in,
usable even when Thunes IS the payout rail).

dispatch_payout() is the single entrypoint ussd_router.complete_transfer()
calls. It picks the rail in this order:
  1. Thunes DGN (primary, all corridors) -- if THUNES_CONFIGURED
  2. Per-corridor bank partner plugin gateway (fallback) -- if that corridor's
     BANK_PARTNER_<CORRIDOR>_GATEWAY_URL/_SIGNING_SECRET are set
  3. Simulation

screen_with_bank_partner() is unrelated to which payout rail is active -- it's
a separate opt-in (BANK_PARTNER_<CORRIDOR>_COMPLIANCE_ENABLED) checked by
compliance.py. A configured gateway does NOT imply screening is available;
both the gateway credentials and the explicit compliance flag must be set.
"""
import hashlib
import hmac
import json
import logging
import time

import requests

from . import config, thunes_payout

# ARCHITECTURE.md documents this hook as living in bank_adapters.py; the real
# implementation is in thunes_payout.py alongside the rest of the Thunes API
# client code, re-exported here so callers matching the doc still find it.
fetch_thunes_usdc_balance = thunes_payout.fetch_thunes_usdc_balance

logger = logging.getLogger("vuka.bank_adapters")


def _sign(signing_secret: str, body: dict) -> str:
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(signing_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def dispatch_payout(tx_id: str, corridor: str, recipient_phone: str, recipient_name: str,
                     payout_amount: float, payout_currency: str) -> dict:
    if config.THUNES_CONFIGURED:
        result = thunes_payout.dispatch_payout(
            tx_id, corridor, recipient_phone, recipient_name, payout_amount, payout_currency
        )
        if result["status"] == "failed" and config.RAFIKI_CONFIGURED:
            logger.warning(
                "bank_adapters: primary Thunes payout failed for tx %s -- retrying "
                "via Rafiki redundant rail", tx_id,
            )
            from . import rafiki_payout
            result = rafiki_payout.dispatch_payout(
                tx_id, corridor, recipient_phone, recipient_name, payout_amount, payout_currency
            )
            result["retried_from"] = "thunes"
        return result

    partner = config.BANK_PARTNERS.get(corridor)
    if partner and partner["payout_configured"]:
        return _dispatch_via_bank_partner(tx_id, corridor, partner, recipient_phone, recipient_name,
                                            payout_amount, payout_currency)

    logger.info(
        "bank_adapters: SIMULATED payout (no Thunes, no bank partner configured) "
        "tx=%s corridor=%s amount=%s %s", tx_id, corridor, payout_amount, payout_currency,
    )
    return {
        "status": "confirmed",
        "method": "simulated",
        "reference": f"SIM-BANK-{tx_id[:8]}",
        "simulated": True,
    }


def _dispatch_via_bank_partner(tx_id, corridor, partner, recipient_phone, recipient_name,
                                 payout_amount, payout_currency) -> dict:
    body = {
        "external_id": tx_id,
        "recipient_phone": recipient_phone,
        "recipient_name": recipient_name,
        "amount": payout_amount,
        "currency": payout_currency,
        "timestamp": int(time.time()),
    }
    signature = _sign(partner["signing_secret"], body)

    try:
        resp = requests.post(
            f"{partner['gateway_url']}/payout",
            headers={
                "Content-Type": "application/json",
                "X-Vuka-Signature": signature,
                "X-Vuka-Bank-Partner": corridor,
            },
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "status": data.get("status", "pending"),
            "method": "bank_partner",
            "reference": data.get("reference"),
            "simulated": False,
        }
    except requests.RequestException:
        logger.exception("bank_adapters: payout dispatch to %s failed for tx %s", partner["name"], tx_id)
        return {"status": "failed", "method": "bank_partner", "reference": None, "simulated": False}


def sync_float_balance(corridor: str) -> dict:
    """Query the bank partner's own float balance for reconciliation against
    Vuka's locally-tracked float_pools row. Same HMAC trust model as payout."""
    partner = config.BANK_PARTNERS.get(corridor)
    if not partner or not partner["payout_configured"]:
        return {"available": False}

    body = {"corridor": corridor, "timestamp": int(time.time())}
    signature = _sign(partner["signing_secret"], body)

    try:
        resp = requests.post(
            f"{partner['gateway_url']}/float-balance",
            headers={
                "Content-Type": "application/json",
                "X-Vuka-Signature": signature,
                "X-Vuka-Bank-Partner": corridor,
            },
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"available": True, "balance": data.get("balance"), "currency": data.get("currency")}
    except requests.RequestException:
        logger.exception("bank_adapters: float balance sync failed for %s", corridor)
        return {"available": False, "error": True}


def screen_with_bank_partner(corridor: str, sender_phone: str, recipient_phone: str, amount: float,
                               recipient_name: str = None) -> dict:
    """Ask the destination corridor's own bank/MTO partner to run its own sanctions/PEP
    screening. Fail-closed: any missing config, HTTP error, or timeout returns
    {"available": False} -- the caller (compliance.py) treats that as 'rule did not fire',
    never as a fabricated pass. Expected contract, once a partner confirms support:
    POST {gateway_url}/compliance-screen -> {"cleared": bool, "risk_level": str, "reference": str}

    recipient_name, if provided, is the sender's SELF-REPORTED entry from the USSD flow --
    not independently verified against any ID -- and is sent as-is so a partner with
    name-based matching can use it; the field is named accordingly in the payload."""
    partner = config.BANK_PARTNERS.get(corridor)
    if not partner or not partner["compliance_enabled"]:
        return {"available": False}

    body = {
        "sender_phone": sender_phone,
        "recipient_phone": recipient_phone,
        "recipient_name_self_reported": recipient_name,
        "amount": amount,
        "timestamp": int(time.time()),
    }
    signature = _sign(partner["signing_secret"], body)

    try:
        resp = requests.post(
            f"{partner['gateway_url']}/compliance-screen",
            headers={
                "Content-Type": "application/json",
                "X-Vuka-Signature": signature,
                "X-Vuka-Bank-Partner": corridor,
            },
            json=body,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "available": True,
            "cleared": bool(data.get("cleared")),
            "risk_level": data.get("risk_level", "high"),
            "reference": data.get("reference"),
        }
    except requests.RequestException:
        logger.warning("bank_adapters: delegated screening call to %s failed/unavailable", partner["name"])
        return {"available": False}
