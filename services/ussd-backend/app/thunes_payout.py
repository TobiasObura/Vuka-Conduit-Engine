"""
Thunes DGN payout adapter -- primary payout rail for all four corridors.

Real flow (when THUNES_API_KEY/SECRET/PAYER_ID are set): quote -> create
transaction -> confirm. Thunes resolves the transaction asynchronously and
notifies THUNES_CALLBACK_URL, which is why dispatch_payout() below returns
status='pending' on a successful transaction creation rather than
'confirmed' -- the webhook (main.py:bank_callback) is what ultimately marks
it confirmed or failed.

Falls back to a clearly-logged simulation when not configured -- never
silently pretends to be live.
"""
import logging

import requests

from . import config

logger = logging.getLogger("vuka.thunes")

_BASE_URLS = {
    "pre-production": "https://api.thunes.com/v3",
    "production": "https://api.thunes.com/v3",
}


def _base_url() -> str:
    return _BASE_URLS.get(config.THUNES_ENVIRONMENT, _BASE_URLS["pre-production"])


def _auth_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "X-Api-Key": config.THUNES_API_KEY,
        "X-Api-Secret": config.THUNES_API_SECRET,
    }


def dispatch_payout(tx_id: str, corridor: str, recipient_phone: str, recipient_name: str,
                     payout_amount: float, payout_currency: str) -> dict:
    if not config.THUNES_CONFIGURED:
        logger.info(
            "thunes: SIMULATED payout dispatch tx=%s corridor=%s amount=%s %s to %s",
            tx_id, corridor, payout_amount, payout_currency, recipient_phone,
        )
        return {
            "status": "confirmed",
            "method": "simulated",
            "reference": f"SIM-THUNES-{tx_id[:8]}",
            "simulated": True,
        }

    service_id_env = config.CORRIDORS[corridor]["thunes_service_id_env"]
    service_id = getattr(config, service_id_env, None) or __import__("os").environ.get(service_id_env)
    if not service_id:
        logger.error("thunes: THUNES_CONFIGURED but %s is unset for corridor %s", service_id_env, corridor)
        return {"status": "failed", "method": "thunes", "reference": None, "simulated": False,
                 "error": f"missing {service_id_env}"}

    try:
        quote = _create_quote(service_id, payout_amount, payout_currency)
        transaction = _create_transaction(quote, recipient_phone, recipient_name, tx_id)
        confirmation = _confirm_transaction(transaction["id"])
        return {
            "status": "pending",  # resolved later by THUNES_CALLBACK_URL webhook
            "method": "thunes",
            "reference": transaction.get("id"),
            "simulated": False,
            "raw": confirmation,
        }
    except requests.RequestException:
        logger.exception("thunes: payout dispatch failed for tx %s", tx_id)
        return {"status": "failed", "method": "thunes", "reference": None, "simulated": False}


def _create_quote(service_id: str, amount: float, currency: str) -> dict:
    resp = requests.post(
        f"{_base_url()}/quotes",
        headers=_auth_headers(),
        json={
            "payer_id": config.THUNES_PAYER_ID,
            "service_id": service_id,
            "amount": amount,
            "currency": currency,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _create_transaction(quote: dict, recipient_phone: str, recipient_name: str, external_id: str) -> dict:
    resp = requests.post(
        f"{_base_url()}/transactions",
        headers=_auth_headers(),
        json={
            "quote_id": quote.get("id"),
            "external_id": external_id,
            "beneficiary": {
                "msisdn": recipient_phone,
                "name": recipient_name,
            },
            "callback_url": config.THUNES_CALLBACK_URL,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _confirm_transaction(transaction_id: str) -> dict:
    resp = requests.post(
        f"{_base_url()}/transactions/{transaction_id}/confirm",
        headers=_auth_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_thunes_usdc_balance() -> dict:
    """Thunes x Circle USDC settlement balance (convert-on-payout model).
    Queries GET /v1/account/accounts/{THUNES_ACCOUNT_ID}/balances/available,
    activated by THUNES_USDC_MODE=true + THUNES_ACCOUNT_ID (assigned by Thunes
    Portal). See ARCHITECTURE.md's "Thunes USDC settlement" section -- until
    activated, the per-corridor float_pools rows remain the source of truth;
    this becomes relevant once Vuka moves to a single USDC treasury balance."""
    if not (config.THUNES_USDC_MODE and config.THUNES_ACCOUNT_ID and config.THUNES_CONFIGURED):
        return {"available": False, "balance": None, "simulated": True}

    try:
        resp = requests.get(
            f"{_base_url()}/account/accounts/{config.THUNES_ACCOUNT_ID}/balances/available",
            headers=_auth_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"available": True, "balance": data.get("balance"), "currency": "USDC", "simulated": False}
    except requests.RequestException:
        logger.exception("thunes: USDC balance fetch failed")
        return {"available": False, "balance": None, "simulated": False, "error": True}
