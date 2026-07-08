"""
Safaricom Daraja integration: OAuth token, STK Push collection (Safaricom
senders only, no aggregator), and the optional B2B Express funded-settlement
leg. Falls back to simulation when Daraja isn't configured -- never silently
pretends a push was sent.
"""
import base64
import datetime
import logging

import requests

from . import config

logger = logging.getLogger("vuka.mpesa")

_BASE_URLS = {
    "sandbox": "https://sandbox.safaricom.co.ke",
    "production": "https://api.safaricom.co.ke",
}

_token_cache = {"token": None, "expires_at": 0}


def _base_url() -> str:
    return _BASE_URLS.get(config.DARAJA_ENV, _BASE_URLS["sandbox"])


def _get_access_token() -> str:
    import time

    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]

    credentials = base64.b64encode(
        f"{config.DARAJA_CONSUMER_KEY}:{config.DARAJA_CONSUMER_SECRET}".encode("utf-8")
    ).decode("utf-8")

    resp = requests.get(
        f"{_base_url()}/oauth/v1/generate?grant_type=client_credentials",
        headers={"Authorization": f"Basic {credentials}"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 3599)) - 30
    return _token_cache["token"]


def _password_and_timestamp() -> tuple:
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    raw = f"{config.DARAJA_SHORTCODE}{config.DARAJA_PASSKEY}{timestamp}"
    password = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
    return password, timestamp


def trigger_collection(tx_id: str, phone_number: str, amount: float) -> dict:
    """Initiates an STK Push prompt on the sender's phone. Returns immediately
    with status='pending' -- the actual result arrives async via
    main.py:mpesa_callback (POST /ussd/webhook/mpesa-callback)."""
    if not config.DARAJA_CONFIGURED:
        logger.info("mpesa: SIMULATED STK push tx=%s phone=%s amount=%s", tx_id, phone_number, amount)
        return {
            "status": "confirmed",
            "method": "simulated",
            "reference": f"SIM-MPESA-{tx_id[:8]}",
            "simulated": True,
        }

    try:
        token = _get_access_token()
        password, timestamp = _password_and_timestamp()
        resp = requests.post(
            f"{_base_url()}/mpesa/stkpush/v1/processrequest",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "BusinessShortCode": config.DARAJA_SHORTCODE,
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": int(amount),
                "PartyA": phone_number,
                "PartyB": config.DARAJA_SHORTCODE,
                "PhoneNumber": phone_number,
                "CallBackURL": config.DARAJA_CALLBACK_URL,
                "AccountReference": tx_id[:12],
                "TransactionDesc": "Vuka transfer",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "status": "pending",
            "method": "daraja",
            "reference": data.get("CheckoutRequestID"),
            "simulated": False,
        }
    except requests.RequestException:
        logger.exception("mpesa: STK push failed for tx %s", tx_id)
        return {"status": "failed", "method": "daraja", "reference": None, "simulated": False}


def refund_via_b2c(tx_id: str, phone_number: str, amount: float) -> dict:
    """Daraja B2C reversal -- used when a collected transaction can't proceed to
    payout (e.g. rate lock expired before dispatch) and the money needs to go
    back to the sender. Unlike Onafriq, this is a real, callable API rather than
    a manual dashboard operation -- see ARCHITECTURE.md gotchas."""
    import os

    initiator = os.environ.get("DARAJA_INITIATOR_NAME")
    security_credential = os.environ.get("DARAJA_SECURITY_CREDENTIAL")

    if not (config.DARAJA_CONFIGURED and initiator and security_credential):
        logger.info("mpesa: SIMULATED B2C refund of %s KES to %s for tx %s", amount, phone_number, tx_id)
        return {"status": "confirmed", "simulated": True, "reference": f"SIM-B2C-REFUND-{tx_id[:8]}"}

    try:
        token = _get_access_token()
        resp = requests.post(
            f"{_base_url()}/mpesa/b2c/v1/paymentrequest",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "InitiatorName": initiator,
                "SecurityCredential": security_credential,
                "CommandID": "BusinessPayment",
                "Amount": int(amount),
                "PartyA": config.DARAJA_SHORTCODE,
                "PartyB": phone_number,
                "Remarks": f"Vuka refund - rate lock expired - {tx_id[:12]}",
                "QueueTimeOutURL": config.DARAJA_CALLBACK_URL,
                "ResultURL": config.DARAJA_CALLBACK_URL,
                "Occasion": "VukaRefund",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return {"status": "pending", "simulated": False, "raw": resp.json()}
    except requests.RequestException:
        logger.exception("mpesa: B2C refund failed for tx %s -- needs manual follow-up", tx_id)
        return {"status": "failed", "simulated": False}


def fund_settlement(amount: float, currency: str = "KES") -> dict:
    """Daraja B2B Express: moves funds from the collection shortcode to the
    settlement/pre-funding account before a Thunes payout, if configured.
    Discuss the pre-funding mechanism with Thunes during onboarding before
    turning this on -- see replit.md."""
    import os

    initiator = os.environ.get("DARAJA_INITIATOR_NAME")
    security_credential = os.environ.get("DARAJA_SECURITY_CREDENTIAL")
    settlement_shortcode = os.environ.get("BANK_SETTLEMENT_SHORTCODE")

    if not (config.DARAJA_CONFIGURED and initiator and security_credential and settlement_shortcode):
        logger.info("mpesa: SIMULATED B2B settlement funding of %s %s", amount, currency)
        return {"status": "confirmed", "simulated": True, "reference": "SIM-B2B"}

    try:
        token = _get_access_token()
        resp = requests.post(
            f"{_base_url()}/mpesa/b2b/v1/paymentrequest",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "Initiator": initiator,
                "SecurityCredential": security_credential,
                "CommandID": "BusinessPayBill",
                "SenderIdentifierType": "4",
                "RecieverIdentifierType": "4",
                "Amount": int(amount),
                "PartyA": config.DARAJA_SHORTCODE,
                "PartyB": settlement_shortcode,
                "AccountReference": "VukaSettlement",
                "Remarks": "Vuka funded settlement",
                "QueueTimeOutURL": config.DARAJA_CALLBACK_URL,
                "ResultURL": config.DARAJA_CALLBACK_URL,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return {"status": "pending", "simulated": False, "raw": resp.json()}
    except requests.RequestException:
        logger.exception("mpesa: B2B settlement funding failed")
        return {"status": "failed", "simulated": False}
