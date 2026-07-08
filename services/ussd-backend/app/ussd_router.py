"""
Depth-based USSD state machine.

Africa's Talking sends the FULL accumulated `text` on every request
(e.g. "1*2*0712345678"), not just the newest keystroke. We keep server-side
session state keyed by session_id so we only ever need to look at the last
'*'-delimited segment to know what the user just typed, and drive the menu
tree from an explicit `stage` field rather than re-deriving position from
text depth (robust to users going back/re-entering).

complete_transfer() is the shared choke point for both the synchronous
(simulated, resolves immediately) and asynchronous (real provider callback
arrives later via a webhook) paths -- it holds the float-reserve and
payout-dispatch logic so both paths behave identically once collection is
confirmed.
"""
import json
import logging
import time

from . import config, fx, ledger, network_profiles

logger = logging.getLogger("vuka.ussd_router")

# Sessions are persisted to the ussd_sessions table (via ledger.py) rather than
# kept in a process-local dict. This means an in-flight USSD session survives
# a backend restart/redeploy and works correctly with multiple Flask workers
# or processes sharing the same SQLite ledger -- a real requirement once this
# runs with more than one worker in production, not just a nice-to-have.

SESSION_TTL_SECONDS = 180

# Stages
STAGE_LANGUAGE = "language"
STAGE_MAIN_MENU = "main_menu"
STAGE_CORRIDOR = "corridor"
STAGE_RECIPIENT_PHONE = "recipient_phone"
STAGE_RECIPIENT_NAME = "recipient_name"
STAGE_AMOUNT = "amount"
STAGE_CONFIRM = "confirm"
STAGE_OTP = "otp"
STAGE_DONE = "done"

STRINGS = {
    "EN": {
        "language_prompt": (
            "Welcome to Vuka\n1. English\n2. Francais\n3. Kiswahili\n"
            "4. Hausa\n5. Twi\n6. Kinyarwanda"
        ),
        "main_menu": "Vuka Menu\n1. Convert & Transact\n2. Speed Dial\n3. Market Rates\n4. Merchant Payment",
        "ask_recipient_phone": "Enter recipient phone number:",
        "ask_recipient_name": "Enter recipient name:",
        "invalid": "Invalid input. Please try again.",
        "ask_otp": "An OTP has been sent via SMS. Enter it to confirm:",
        "otp_invalid": "Incorrect OTP. Please try again.",
        "hold": "Your transaction is under review. You will be notified once cleared.",
        "session_expired": "Session expired. Please dial again.",
    },
    "FR": {
        "language_prompt": (
            "Bienvenue chez Vuka\n1. English\n2. Francais\n3. Kiswahili\n"
            "4. Hausa\n5. Twi\n6. Kinyarwanda"
        ),
        "main_menu": "Menu Vuka\n1. Convertir et Transferer\n2. Numero Rapide\n3. Taux du Marche\n4. Paiement Marchand",
        "ask_recipient_phone": "Entrez le numero du destinataire:",
        "ask_recipient_name": "Entrez le nom du destinataire:",
        "invalid": "Entree invalide. Veuillez reessayer.",
        "ask_otp": "Un code OTP a ete envoye par SMS. Entrez-le pour confirmer:",
        "otp_invalid": "Code OTP incorrect. Veuillez reessayer.",
        "hold": "Votre transaction est en cours de verification.",
        "session_expired": "Session expiree. Veuillez recomposer.",
    },
}

# Maps the digit typed at the language menu to ai_language.py's language codes.
LANGUAGE_MENU_ORDER = ["EN", "FR", "SW", "HA", "TW", "RW"]


def _new_session():
    return {
        "stage": STAGE_LANGUAGE,
        "language": "EN",
        "origin_corridor": None,
        "transaction_type": None,
        "corridor": None,
        "corridor_menu_options": None,
        "recipient_phone": None,
        "recipient_name": None,
        "amount": None,
        "quote": None,
        "tx_id": None,
        "otp_attempts": 0,
        "last_seen": time.time(),
    }


def _get_session(session_id: str) -> dict:
    raw = ledger.get_session_state(session_id)
    if raw:
        try:
            sess = json.loads(raw)
            if (time.time() - sess.get("last_seen", 0)) <= SESSION_TTL_SECONDS:
                sess["last_seen"] = time.time()
                return sess
        except (json.JSONDecodeError, TypeError):
            logger.warning("ussd_router: corrupt session state for %s, resetting", session_id)
    return _new_session()


def _save_session(session_id: str, sess: dict):
    ledger.set_session_state(session_id, json.dumps(sess))


def _s(sess: dict, key: str) -> str:
    """Looks up a menu string in the sender's selected language. EN/FR resolve
    directly from STRINGS (no Gemini call needed); SW/HA/TW/RW route through
    ai_language.get_ussd_strings(), which either returns a real Gemini
    translation or -- if Gemini isn't configured/fails -- English with
    simulated=True (never a guessed translation, see ai_language.py). Local
    import: ai_language imports STRINGS from this module at load time, so a
    top-level import here would be circular."""
    from . import ai_language

    return ai_language.get_ussd_strings(sess["language"])["strings"][key]


def _last_input(text: str) -> str:
    if not text:
        return ""
    parts = text.split("*")
    return parts[-1].strip()


def _end(msg: str) -> tuple:
    return f"END {msg}", True


def _con(msg: str) -> tuple:
    return f"CON {msg}", False


def handle_request(session_id: str, phone_number: str, text: str, network_code: str = None) -> str:
    """Main entrypoint called by the Flask webhook. Returns the raw Africa's Talking
    response string (CON ... to continue, END ... to terminate the session)."""
    sess = _get_session(session_id)
    user_input = _last_input(text)

    try:
        response, done = _dispatch(sess, user_input, phone_number, network_code)
    except Exception:
        logger.exception("ussd_router: unhandled error in session %s", session_id)
        response, done = _end(_s(sess, "invalid"))

    if done:
        ledger.delete_session_state(session_id)
    else:
        _save_session(session_id, sess)
    return response


def _dispatch(sess: dict, user_input: str, phone_number: str, network_code: str) -> tuple:
    stage = sess["stage"]

    # First touch of a fresh session: text is empty, show the language prompt.
    if stage == STAGE_LANGUAGE and user_input == "":
        return _con(_s(sess, "language_prompt"))

    if stage == STAGE_LANGUAGE:
        idx = {"1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5}.get(user_input)
        if idx is None:
            return _con(_s(sess, "invalid") + "\n" + _s(sess, "language_prompt"))
        sess["language"] = LANGUAGE_MENU_ORDER[idx]
        # Detect the sender's origin corridor from their network code. Vuka is
        # pan-African, not a Kenya-only hub -- this determines both which
        # currency Merchant Payment charges in and which corridor is excluded
        # from the Convert & Transact destination menu (can't send to your own
        # country). Falls back to KENYA if the network code isn't recognized
        # (e.g. local testing without a real carrier) -- a safe, previously-
        # correct default rather than a hard failure.
        profile = network_profiles.lookup(network_code) if network_code else {}
        sess["origin_corridor"] = profile.get("corridor") or "KENYA"
        sess["stage"] = STAGE_MAIN_MENU
        return _con(_s(sess, "main_menu"))

    if stage == STAGE_MAIN_MENU:
        if user_input == "1":
            sess["transaction_type"] = "convert_transact"
            sess["stage"] = STAGE_CORRIDOR
            destinations = [c for c in config.ALL_CORRIDORS_ORDER if c != sess["origin_corridor"]]
            sess["corridor_menu_options"] = destinations
            menu_lines = "\n".join(
                f"{i+1}. {config.CORRIDORS[c]['country']}" for i, c in enumerate(destinations)
            )
            return _con(f"Send to:\n{menu_lines}")
        elif user_input == "4":
            sess["transaction_type"] = "merchant_payment"
            sess["stage"] = STAGE_RECIPIENT_PHONE
            return _con(_s(sess, "ask_recipient_phone"))
        elif user_input in ("2", "3"):
            # Speed Dial / Market Rates are read-only informational menus, not
            # part of this rebuild pass -- placeholder response.
            return _end("Feature coming soon.")
        else:
            return _con(_s(sess, "invalid") + "\n" + _s(sess, "main_menu"))

    if stage == STAGE_CORRIDOR:
        options = sess.get("corridor_menu_options") or []
        idx_map = {str(i + 1): c for i, c in enumerate(options)}
        chosen = idx_map.get(user_input)
        if chosen is None:
            menu_lines = "\n".join(f"{i+1}. {config.CORRIDORS[c]['country']}" for i, c in enumerate(options))
            return _con(_s(sess, "invalid") + f"\nSend to:\n{menu_lines}")
        sess["corridor"] = chosen
        sess["stage"] = STAGE_RECIPIENT_PHONE
        return _con(_s(sess, "ask_recipient_phone"))

    if stage == STAGE_RECIPIENT_PHONE:
        if not user_input.lstrip("+").isdigit() or len(user_input) < 9:
            return _con(_s(sess, "invalid") + "\n" + _s(sess, "ask_recipient_phone"))
        sess["recipient_phone"] = user_input
        sess["stage"] = STAGE_RECIPIENT_NAME
        return _con(_s(sess, "ask_recipient_name"))

    if stage == STAGE_RECIPIENT_NAME:
        if not user_input:
            return _con(_s(sess, "invalid") + "\n" + _s(sess, "ask_recipient_name"))
        sess["recipient_name"] = user_input
        sess["stage"] = STAGE_AMOUNT

        if sess["transaction_type"] == "convert_transact":
            currency = config.CORRIDORS[sess["corridor"]]["currency"]
            return _con(f"Enter amount in {currency} for {sess['recipient_name']} to receive:")
        # Merchant Payment: same-country, charged in the sender's own origin currency
        # (a Ghanaian sender paying a Ghanaian merchant pays in GHS, not KES).
        origin_currency = config.CORRIDORS[sess["origin_corridor"]]["currency"]
        return _con(f"Enter amount in {origin_currency} to pay {sess['recipient_name']}:")

    if stage == STAGE_AMOUNT:
        try:
            amount = float(user_input)
            assert amount > 0
        except (ValueError, AssertionError):
            if sess["transaction_type"] == "convert_transact":
                currency = config.CORRIDORS[sess["corridor"]]["currency"]
                retry_prompt = f"Enter amount in {currency} for {sess['recipient_name']} to receive:"
            else:
                origin_currency = config.CORRIDORS[sess["origin_corridor"]]["currency"]
                retry_prompt = f"Enter amount in {origin_currency} to pay {sess['recipient_name']}:"
            return _con(_s(sess, "invalid") + "\n" + retry_prompt)
        sess["amount"] = amount
        sess["stage"] = STAGE_CONFIRM

        if sess["transaction_type"] == "convert_transact":
            # `amount` here is in the RECIPIENT's currency -- what they'll receive,
            # in full, never reduced by fees. fx.get_quote_for_recipient() works out
            # what the sender's wallet actually gets debited, in THEIR origin
            # currency, fees added on top.
            quote = fx.get_quote_for_recipient(sess["origin_corridor"], sess["corridor"], amount)
            sess["quote"] = quote
            msg = (
                f"{sess['recipient_name']} receives: {quote['payout_amount']:,.2f} {quote['payout_currency']}\n"
                f"You pay: {quote['origin_currency']} {quote['sender_debit']:,.2f} (fees included)\n"
                f"A bank would charge you ~{quote['origin_currency']} {quote['bank_debit']:,.2f} for the same delivery\n"
                f"You save: ~{quote['origin_currency']} {quote['savings_vs_bank']:,.2f}\n"
                f"1. Confirm\n2. Cancel"
            )
        else:
            origin_currency = config.CORRIDORS[sess["origin_corridor"]]["currency"]
            msg = (
                f"Pay {origin_currency} {amount:,.0f} to {sess['recipient_name']}\n"
                f"1. Confirm\n2. Cancel"
            )
        return _con(msg)

    if stage == STAGE_CONFIRM:
        if user_input == "2":
            return _end("Transaction cancelled.")
        if user_input != "1":
            return _con(_s(sess, "invalid"))
        return _initiate_transaction(sess, phone_number, network_code)

    if stage == STAGE_OTP:
        return _verify_otp(sess, user_input, phone_number, network_code)

    # Fallback -- stale/unknown stage.
    return _end(_s(sess, "session_expired"))


def _initiate_transaction(sess: dict, phone_number: str, network_code: str) -> tuple:
    from . import compliance  # local import: avoids a circular import at module load time

    # sess["amount"] is in the RECIPIENT's currency for convert_transact (what the
    # sender typed, what the recipient will receive in full) and in KES for
    # merchant_payment. determine_tier() and screen_transaction()'s large-amount
    # check compare this directly against the corridor's soft cap -- which is
    # itself denominated in the destination currency, so this is now a correctly
    # currency-matched comparison for convert_transact (previously a known gap,
    # fixed as a side effect of the recipient-gets-full-amount rework).
    tier = compliance.determine_tier(sess["transaction_type"], sess.get("corridor"), sess["amount"])

    origin_currency = config.CORRIDORS[sess["origin_corridor"]]["currency"]
    tx_id = ledger.new_transaction(
        session_id=phone_number,
        transaction_type=sess["transaction_type"],
        sender_phone=phone_number,
        # Ignored by new_transaction when quote is provided (convert_transact) --
        # it derives the real send_amount from quote["sender_debit"]/quote["origin_currency"]
        # instead. Passed accurately here regardless, so nothing is misleading if read directly.
        send_amount=sess["amount"],
        send_currency=origin_currency,
        corridor=sess.get("corridor"),
        recipient_phone=sess.get("recipient_phone"),
        recipient_name=sess.get("recipient_name"),
        quote=sess.get("quote"),  # locks payout_amount/currency/fee/rate NOW -- see ledger.new_transaction
        due_diligence_tier=tier,
    )
    sess["tx_id"] = tx_id

    result = compliance.screen_transaction(
        tx_id=tx_id,
        transaction_type=sess["transaction_type"],
        sender_phone=phone_number,
        recipient_phone=sess.get("recipient_phone"),
        amount=sess["amount"],
        corridor=sess.get("corridor"),
        recipient_name=sess.get("recipient_name"),
    )

    if result["hold"]:
        ledger.update_transaction(tx_id, status=ledger.STATUS_COMPLIANCE_HOLD)
        return _end(_s(sess, "hold"))

    # Send OTP and move to verification stage.
    from . import otp

    otp.generate_and_send(tx_id, phone_number)
    sess["stage"] = STAGE_OTP
    ledger.update_transaction(tx_id, status=ledger.STATUS_AWAITING_OTP)
    return _con(_s(sess, "ask_otp"))


def _verify_otp(sess: dict, user_input: str, phone_number: str, network_code: str) -> tuple:
    from . import otp

    tx_id = sess["tx_id"]
    if not otp.verify(tx_id, user_input):
        sess["otp_attempts"] += 1
        if sess["otp_attempts"] >= 3:
            ledger.update_transaction(tx_id, status=ledger.STATUS_FAILED)
            return _end(_s(sess, "otp_invalid"))
        return _con(_s(sess, "otp_invalid"))

    ledger.update_transaction(tx_id, otp_verified=1, status=ledger.STATUS_AWAITING_COLLECTION)
    _fire_async_risk_score(sess, phone_number)
    return _dispatch_collection(sess, phone_number, network_code)


def _fire_async_risk_score(sess: dict, phone_number: str):
    """Advisory second opinion, fired after OTP verification -- never blocks
    the USSD session. See ai_risk.py for what it does and doesn't use."""
    try:
        from . import ai_risk
        import time as _time

        velocity_count = ledger.count_recent_transactions_for_phone(
            phone_number, config.COMPLIANCE_VELOCITY_WINDOW_MINUTES
        )
        ai_risk.score_transaction_async(
            tx_id=sess["tx_id"],
            transaction_type=sess["transaction_type"],
            corridor=sess.get("corridor"),
            amount=sess["amount"],
            velocity_count=velocity_count,
            hour_of_day=_time.localtime().tm_hour,
        )
    except Exception:
        logger.exception("ussd_router: failed to fire async risk score for tx %s", sess.get("tx_id"))


def _dispatch_collection(sess: dict, phone_number: str, network_code: str) -> tuple:
    from . import collection_adapters

    tx_id = sess["tx_id"]
    tx = ledger.get_transaction(tx_id)
    result = collection_adapters.initiate_collection(
        tx_id=tx_id,
        sender_phone=phone_number,
        amount=tx["send_amount"],
        currency=tx["send_currency"],
        network_code=network_code,
    )

    ledger.update_transaction(
        tx_id,
        collection_status=result["status"],
        collection_reference=result.get("reference"),
        collection_simulated=int(result.get("simulated", True)),
        collection_method=result.get("method"),
    )

    if result["status"] == "confirmed":
        # Simulated / instant-confirm path -- go straight to completing the transfer.
        complete_transfer(tx_id)
        return _end("Transaction complete. Funds are on their way.")

    if result["status"] == "failed":
        ledger.update_transaction(tx_id, status=ledger.STATUS_FAILED)
        return _end("Payment could not be collected. Please try again.")

    # 'pending' -- real STK push / Onafriq collection in flight, resolved later
    # by a webhook (see main.py) which calls complete_transfer().
    return _end("Please complete the payment prompt on your phone to finish this transfer.")


def complete_transfer(tx_id: str):
    """Shared choke point for both the sync (simulated) and async (webhook-driven)
    collection-confirmed paths. Holds the rate-lock check + float-reserve +
    payout-dispatch logic."""
    from . import bank_adapters

    tx = ledger.get_transaction(tx_id)
    if not tx:
        logger.error("complete_transfer: unknown transaction %s", tx_id)
        return

    if ledger.has_open_high_severity_flag(tx_id):
        # A high-severity compliance flag landed after collection but before payout
        # (e.g. an async bank-partner screen result) -- never dispatch payout in that case.
        ledger.update_transaction(tx_id, status=ledger.STATUS_COMPLIANCE_HOLD)
        logger.warning("complete_transfer: %s held at payout due to open high-severity flag", tx_id)
        return

    ledger.update_transaction(tx_id, status=ledger.STATUS_COLLECTED)

    if tx["transaction_type"] == "merchant_payment":
        # No cross-border corridor/payout leg -- collection alone completes it.
        ledger.update_transaction(tx_id, status=ledger.STATUS_COMPLETE)
        return

    corridor = tx["corridor"]

    # The quote was LOCKED into this row at transaction creation (see
    # ledger.new_transaction) -- never recompute fx.get_quote() here. Recomputing
    # would silently settle at a different rate if the live margin changed between
    # quote-display and now, which is exactly what the rate lock exists to prevent.
    if tx.get("rate_lock_expires_at") and time.time() > tx["rate_lock_expires_at"]:
        logger.warning(
            "complete_transfer: %s rate lock expired (%s > %s) -- refunding rather than "
            "settling at a stale rate", tx_id, time.time(), tx["rate_lock_expires_at"],
        )
        _refund_expired_rate_lock(tx)
        return

    payout_amount = tx["payout_amount"]
    payout_currency = tx["payout_currency"]

    if payout_amount is None:
        # Defensive: a convert_transact transaction should always have a locked
        # quote from creation. If it somehow doesn't, fail loudly rather than
        # guessing at a rate.
        logger.error("complete_transfer: %s has no locked quote -- failing rather than guessing", tx_id)
        ledger.update_transaction(tx_id, status=ledger.STATUS_FAILED)
        return

    ledger.update_transaction(tx_id, status=ledger.STATUS_DISPATCHING_PAYOUT)

    # Reserve float before dispatch so dashboards reflect the commitment immediately,
    # even though the payout call below may still fail (in which case we release it).
    ledger.adjust_float_pool(corridor, -payout_amount)

    payout_result = bank_adapters.dispatch_payout(
        tx_id=tx_id,
        corridor=corridor,
        recipient_phone=tx["recipient_phone"],
        recipient_name=tx["recipient_name"],
        payout_amount=payout_amount,
        payout_currency=payout_currency,
    )

    ledger.update_transaction(
        tx_id,
        payout_status=payout_result["status"],
        payout_reference=payout_result.get("reference"),
        payout_simulated=int(payout_result.get("simulated", True)),
        payout_method=payout_result.get("method"),
    )

    if payout_result["status"] == "failed":
        ledger.adjust_float_pool(corridor, payout_amount)  # release the reserve
        ledger.update_transaction(tx_id, status=ledger.STATUS_FAILED)
    elif payout_result["status"] in ("dispatched", "confirmed"):
        ledger.update_transaction(tx_id, status=ledger.STATUS_COMPLETE)
    # 'pending' payout (async bank rail) stays in STATUS_DISPATCHING_PAYOUT until
    # a payout webhook resolves it.


def _refund_expired_rate_lock(tx: dict):
    """Collection already succeeded (the sender's money was actually debited)
    but the locked rate expired before payout could dispatch -- settling now at
    a freshly recomputed rate would silently give the recipient a different
    amount than what the sender was shown, so this refunds the sender instead.

    Real refund capability depends on which collection rail was used:
      - Daraja: B2C reversal is a real, callable API (see mpesa_collection.refund_via_b2c) --
        dispatched here when the collection was real (not simulated).
      - Onafriq: no native reversal API (see ARCHITECTURE.md gotchas) -- this case
        is marked for MANUAL refund via the Onafriq dashboard, not silently dropped.
    """
    from . import mpesa_collection

    ledger.mark_rate_expired_refund(tx["id"])
    tx_id = tx["id"]

    if tx.get("collection_method") == "daraja" and not tx.get("collection_simulated"):
        result = mpesa_collection.refund_via_b2c(tx_id, tx["sender_phone"], tx["send_amount"])
        logger.info("_refund_expired_rate_lock: Daraja B2C refund result for %s: %s", tx_id, result)
    elif tx.get("collection_method") == "onafriq" and not tx.get("collection_simulated"):
        logger.error(
            "_refund_expired_rate_lock: tx %s needs a MANUAL refund via the Onafriq dashboard -- "
            "Onafriq has no reversal API. Sender phone: %s, amount: %s %s.",
            tx_id, tx["sender_phone"], tx["send_amount"], tx["send_currency"],
        )
    else:
        logger.info("_refund_expired_rate_lock: tx %s collection was simulated -- no real refund needed", tx_id)
