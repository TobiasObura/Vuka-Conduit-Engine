import logging

from flask import Flask, jsonify, request

from app import config, ledger, ussd_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("vuka.main")

app = Flask(__name__)

with app.app_context():
    ledger.init_db()
    logger.info("Vuka USSD backend starting. Simulation status: %s", config.simulation_summary())


@app.route("/ussd/", methods=["POST"])
def ussd_webhook():
    session_id = request.form.get("sessionId", "")
    phone_number = request.form.get("phoneNumber", "")
    text = request.form.get("text", "")
    network_code = request.form.get("networkCode")

    response = ussd_router.handle_request(session_id, phone_number, text, network_code)
    return response, 200, {"Content-Type": "text/plain"}


# -----------------------------------------------------------------------------
# Payout callbacks
# -----------------------------------------------------------------------------
@app.route("/ussd/webhook/thunes-callback", methods=["POST"])
def thunes_callback():
    """Thunes DGN async payout result. Terminal states: EXECUTED (success) or
    DECLINED/CANCELLED/ERROR (failure) -- see ARCHITECTURE.md."""
    payload = request.get_json(silent=True) or {}
    tx_id = payload.get("external_id") or payload.get("transaction_id")
    thunes_status = payload.get("status")

    if not tx_id:
        return jsonify({"error": "missing external_id"}), 400

    tx = ledger.get_transaction(tx_id)
    if not tx:
        return jsonify({"error": "unknown transaction"}), 404

    if thunes_status == "EXECUTED":
        ledger.update_transaction(
            tx_id, payout_status="confirmed", status=ledger.STATUS_COMPLETE,
            payout_reference=payload.get("id") or payload.get("reference"),
        )
    elif thunes_status in ("DECLINED", "CANCELLED", "ERROR"):
        # Float was reserved at dispatch time (see ussd_router.complete_transfer) --
        # release it now that we know the payout didn't actually go through, and
        # surface the transaction as failed so it's visible for sender refund/retry.
        if tx.get("corridor") and tx.get("payout_amount"):
            ledger.adjust_float_pool(tx["corridor"], tx["payout_amount"])
        ledger.update_transaction(tx_id, payout_status="failed", status=ledger.STATUS_FAILED)
    else:
        logger.warning("thunes_callback: unrecognized status '%s' for tx %s", thunes_status, tx_id)

    return jsonify({"ok": True}), 200


@app.route("/ussd/webhook/bank-callback", methods=["POST"])
def bank_callback():
    """Per-corridor bank-partner fallback payout result (Tier 2, only used when
    Thunes isn't configured for that corridor). Kept as a separate route from
    thunes-callback since it's a structurally different, HMAC-signed partner --
    not a legacy alias."""
    payload = request.get_json(silent=True) or {}
    tx_id = payload.get("transaction_id") or payload.get("external_id")
    status = payload.get("status")

    if not tx_id:
        return jsonify({"error": "missing transaction_id"}), 400

    tx = ledger.get_transaction(tx_id)
    if not tx:
        return jsonify({"error": "unknown transaction"}), 404

    if status in ("success", "confirmed", "SUCCESSFUL"):
        ledger.update_transaction(
            tx_id, payout_status="confirmed", status=ledger.STATUS_COMPLETE, payout_reference=payload.get("reference")
        )
    elif status in ("failed", "FAILED"):
        if tx.get("corridor") and tx.get("payout_amount"):
            ledger.adjust_float_pool(tx["corridor"], tx["payout_amount"])
        ledger.update_transaction(tx_id, payout_status="failed", status=ledger.STATUS_FAILED)
    else:
        logger.warning("bank_callback: unrecognized status '%s' for tx %s", status, tx_id)

    return jsonify({"ok": True}), 200


# -----------------------------------------------------------------------------
# Collection callbacks
# -----------------------------------------------------------------------------
@app.route("/ussd/webhook/mpesa-callback", methods=["POST"])
def mpesa_callback():
    """Safaricom Daraja STK Push result.

    IMPORTANT: Safaricom's callback carries no signature. Per ARCHITECTURE.md,
    authenticity here comes ONLY from the CheckoutRequestID matching a
    transaction this app itself already parked in pending collection state --
    an idempotent state-machine check, not a shared secret. We deliberately do
    NOT trust any transaction/external_id field a caller could supply; the
    CheckoutRequestID is the only linkage, looked up server-side.
    """
    payload = request.get_json(silent=True) or {}
    try:
        stk_callback = payload["Body"]["stkCallback"]
    except (KeyError, TypeError):
        logger.warning("mpesa_callback: payload missing Body.stkCallback: %s", payload)
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200

    checkout_request_id = stk_callback.get("CheckoutRequestID")
    result_code = stk_callback.get("ResultCode")

    if not checkout_request_id:
        logger.warning("mpesa_callback: missing CheckoutRequestID")
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200

    tx = ledger.get_transaction_by_pending_collection_reference(checkout_request_id)
    if not tx:
        # Either a replay, an unknown ID, or a transaction that already resolved --
        # never act on it. Always return 200 so Safaricom doesn't retry indefinitely.
        logger.warning(
            "mpesa_callback: CheckoutRequestID %s does not match a pending transaction -- ignoring",
            checkout_request_id,
        )
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200

    tx_id = tx["id"]

    if result_code == 0:
        receipt = None
        for item in stk_callback.get("CallbackMetadata", {}).get("Item", []):
            if item.get("Name") == "MpesaReceiptNumber":
                receipt = item.get("Value")
        ledger.update_transaction(tx_id, collection_status="confirmed", mpesa_receipt=receipt)
        ussd_router.complete_transfer(tx_id)
    else:
        ledger.update_transaction(tx_id, collection_status="failed", status=ledger.STATUS_FAILED)

    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200


@app.route("/ussd/webhook/thunes-collection-callback", methods=["POST"])
def thunes_collection_callback():
    """Thunes Accept API (Tier 2 collection) status updates. Terminal states:
    Settled -> confirm + complete_transfer(); Failed/Declined/Cancelled/Expired
    -> fail collection. Authorized (USSD prompt sent, awaiting sender approval)
    is logged only, per ARCHITECTURE.md."""
    payload = request.get_json(silent=True) or {}
    tx_id = payload.get("external_id") or payload.get("transaction_id")
    status = payload.get("status")

    if not tx_id:
        logger.warning("thunes_collection_callback missing external_id: %s", payload)
        return jsonify({"ok": True}), 200

    tx = ledger.get_transaction(tx_id)
    if not tx:
        logger.warning("thunes_collection_callback for unknown transaction %s", tx_id)
        return jsonify({"ok": True}), 200

    if status == "Settled":
        ledger.update_transaction(tx_id, collection_status="confirmed")
        ussd_router.complete_transfer(tx_id)
    elif status in ("Failed", "Declined", "Cancelled", "Expired"):
        ledger.update_transaction(tx_id, collection_status="failed", status=ledger.STATUS_FAILED)
    elif status == "Authorized":
        logger.info("thunes_collection_callback: tx %s authorized, awaiting sender approval", tx_id)
    else:
        logger.warning("thunes_collection_callback: unrecognized status '%s' for tx %s", status, tx_id)

    return jsonify({"ok": True}), 200


@app.route("/ussd/webhook/onafriq-callback", methods=["POST"])
def onafriq_callback():
    """Onafriq hub collection result -- same state-machine pattern as Daraja,
    but Onafriq's callback does carry an external_id we set ourselves."""
    payload = request.get_json(silent=True) or {}
    _handle_generic_collection_callback(payload, source="onafriq")
    return jsonify({"ok": True}), 200


@app.route("/ussd/webhook/momo-callback", methods=["POST"])
def momo_callback():
    """MTN MoMo direct callback -- fallback path when not routed through Onafriq."""
    payload = request.get_json(silent=True) or {}
    _handle_generic_collection_callback(payload, source="momo_direct")
    return jsonify({"ok": True}), 200


def _handle_generic_collection_callback(payload: dict, source: str):
    """For collection providers whose callback DOES carry back the external_id
    we generated (Onafriq, MTN MoMo direct) -- unlike Daraja, which does not."""
    tx_id = payload.get("transaction_id") or payload.get("external_id")
    status = payload.get("status")

    if not tx_id:
        logger.warning("%s callback missing transaction_id: %s", source, payload)
        return

    tx = ledger.get_transaction(tx_id)
    if not tx:
        logger.warning("%s callback for unknown transaction %s", source, tx_id)
        return

    if status in ("success", "confirmed", "SUCCESSFUL", "COMPLETED"):
        ledger.update_transaction(
            tx_id,
            collection_status="confirmed",
            mpesa_receipt=payload.get("receipt") or payload.get("mpesa_receipt"),
        )
        ussd_router.complete_transfer(tx_id)
    elif status in ("failed", "FAILED", "CANCELLED"):
        ledger.update_transaction(tx_id, collection_status="failed", status=ledger.STATUS_FAILED)
    else:
        logger.warning("%s callback: unrecognized status '%s' for tx %s", source, status, tx_id)


@app.route("/ussd/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok", "simulation": config.simulation_summary()}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
