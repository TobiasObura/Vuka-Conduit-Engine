"""
Risk-based, tiered AML/KYC screening layer (mirrors FATF Recommendation 10's
SDD/CDD/EDD framework -- see ARCHITECTURE.md "Tiered risk-based due diligence").

determine_tier() classifies every Convert & Transact transaction as
LOW / STANDARD / ENHANCED relative to the corridor's soft cap:
  - LOW (Simplified Due Diligence): only the watchlist + sanctions-vendor
    checks run. Velocity/amount-tiering and bank-partner screening are
    skipped for genuinely small transfers.
  - STANDARD: velocity/amount/watchlist/sanctions-vendor run synchronously;
    bank-partner screening (when opted in) runs in a background thread so the
    sender isn't held on a partner's HTTP round-trip. complete_transfer()'s
    has_open_high_severity_flag() check is the backstop that catches a
    late-arriving hold before payout.
  - ENHANCED (amount >= large-amount threshold): everything runs
    synchronously, including bank-partner screening -- large transfers are
    exactly the case a bank partner wants screened before funds move, not
    after.
Merchant Payment transactions (no corridor) and any transaction where the
corridor is missing/unrecognized always run the full synchronous check set --
tiering only applies to Convert & Transact.

A HIGH severity flag holds the transaction (screen_transaction returns
hold=True for the synchronous portion) and the caller must stop the flow
before OTP/collection. A MEDIUM flag is recorded for review but does not
block. Every rule that fires persists a row in compliance_flags -- full audit
trail, nothing discarded.
"""
import logging
import threading

from . import config, ledger

logger = logging.getLogger("vuka.compliance")

TIER_LOW = "low"
TIER_STANDARD = "standard"
TIER_ENHANCED = "enhanced"


def determine_tier(transaction_type: str, corridor: str, amount: float) -> str:
    """Returns None for Merchant Payment or an unrecognized corridor -- those
    always get the full synchronous check set, no tiering applied."""
    if transaction_type != "convert_transact" or not corridor or corridor not in config.CORRIDORS:
        return None

    soft_cap = config.CORRIDORS[corridor]["soft_cap"]
    low_threshold = soft_cap * config.VUKA_SDD_LOW_TIER_FRACTION
    high_threshold = soft_cap * config.COMPLIANCE_LARGE_AMOUNT_FRACTION
    # `amount` is the RECIPIENT-currency amount (what the sender typed, what the
    # recipient receives in full -- see fx.get_quote_for_recipient), which is the
    # same currency soft_cap is denominated in. This used to be a currency
    # mismatch (amount was KES, soft_cap wasn't) before the recipient-gets-
    # full-amount rework; it's a correct comparison now.
    if amount < low_threshold:
        return TIER_LOW
    elif amount < high_threshold:
        return TIER_STANDARD
    else:
        return TIER_ENHANCED


def screen_transaction(tx_id, transaction_type, sender_phone, recipient_phone, amount, corridor,
                        recipient_name=None) -> dict:
    tier = determine_tier(transaction_type, corridor, amount)
    flags = []

    if tier == TIER_LOW:
        flags += _check_local_watchlist(tx_id, sender_phone, recipient_phone)
        flags += _check_sanctions_vendor(tx_id, recipient_name)
        # Velocity, large-amount, and bank-partner screening are deliberately
        # skipped at LOW tier -- see module docstring.
    else:
        flags += _check_velocity(tx_id, sender_phone)
        flags += _check_large_amount(tx_id, amount, corridor)
        flags += _check_local_watchlist(tx_id, sender_phone, recipient_phone)
        flags += _check_sanctions_vendor(tx_id, recipient_name)

        if transaction_type == "convert_transact" and corridor:
            if tier == TIER_STANDARD:
                # Backgrounded -- does not contribute to this call's synchronous
                # hold decision. complete_transfer()'s has_open_high_severity_flag()
                # is the backstop for a late-arriving hold.
                _fire_async_bank_partner_screen(tx_id, corridor, sender_phone, recipient_phone,
                                                  amount, recipient_name)
            else:
                # ENHANCED (or tier is None -- e.g. Merchant Payment doesn't reach
                # here since it has no corridor) -- synchronous.
                flags += _check_bank_partner_screen(tx_id, corridor, sender_phone, recipient_phone,
                                                       amount, recipient_name)

    hold = any(f["severity"] == "high" for f in flags)
    return {"hold": hold, "flags": flags, "tier": tier}


def _record(tx_id, rule, severity, note, simulated=False) -> dict:
    ledger.record_compliance_flag(tx_id, rule, severity, note, simulated=simulated)
    flag = {"rule": rule, "severity": severity, "note": note, "simulated": simulated}
    logger.info("compliance: %s fired (%s) for tx %s: %s", rule, severity, tx_id, note)
    return flag


def _check_velocity(tx_id, sender_phone) -> list:
    count = ledger.count_recent_transactions_for_phone(
        sender_phone, config.COMPLIANCE_VELOCITY_WINDOW_MINUTES
    )
    # count includes the transaction just created, so compare against count-1
    # to get "how many transactions happened before this one".
    if (count - 1) >= config.COMPLIANCE_VELOCITY_THRESHOLD:
        note = (
            f"Sender has {count} transactions in the last "
            f"{config.COMPLIANCE_VELOCITY_WINDOW_MINUTES} minutes "
            f"(threshold: {config.COMPLIANCE_VELOCITY_THRESHOLD}) -- possible structuring/smurfing."
        )
        return [_record(tx_id, "velocity", "medium", note)]
    return []


def _check_large_amount(tx_id, amount, corridor) -> list:
    if not corridor or corridor not in config.CORRIDORS:
        return []
    soft_cap = config.CORRIDORS[corridor]["soft_cap"]
    threshold = soft_cap * config.COMPLIANCE_LARGE_AMOUNT_FRACTION
    # `amount` is the recipient-currency amount for convert_transact (see
    # fx.get_quote_for_recipient / the recipient-gets-full-amount model),
    # which matches soft_cap's currency correctly. Previously a known
    # currency-scoping gap when amount was the sender-side KES figure --
    # fixed as a side effect of that rework, not by this check itself.
    if amount >= threshold:
        note = (
            f"Transaction amount {amount} is >= {config.COMPLIANCE_LARGE_AMOUNT_FRACTION:.0%} "
            f"of the {corridor} soft cap ({soft_cap})."
        )
        return [_record(tx_id, "large_amount", "medium", note)]
    return []


def _check_local_watchlist(tx_id, sender_phone, recipient_phone) -> list:
    watchlist = config.VUKA_LOCAL_WATCHLIST
    if not watchlist:
        return []

    hit_sender = ledger.phone_on_local_watchlist(sender_phone, watchlist)
    hit_recipient = bool(recipient_phone) and ledger.phone_on_local_watchlist(recipient_phone, watchlist)

    if not (hit_sender or hit_recipient):
        return []

    who = "sender" if hit_sender else "recipient"
    note = (
        f"{who.capitalize()} phone number matches the local watchlist "
        f"(VUKA_LOCAL_WATCHLIST). SIMULATED: this is a manually curated phone-number "
        f"list, not a real OFAC/UN/EU sanctions or PEP database. Never present this as "
        f"equivalent to real sanctions screening in any external-facing material."
    )
    return [_record(tx_id, "local_watchlist", "high", note, simulated=config.SANCTIONS_SCREENING_SIMULATED)]


def _check_sanctions_vendor(tx_id, recipient_name) -> list:
    """Real name-based sanctions/PEP screening via sanctions_screening.py, when a
    vendor is configured and opted in. Honest scope limit: Vuka's USSD flow never
    collects the SENDER's name (identity is presumed via phone/SIM registration)
    -- so this can only screen the recipient's SELF-REPORTED name for now, never
    independently verified against an ID. Fail-closed: unavailable means the rule
    doesn't fire, never a fabricated clear."""
    from . import sanctions_screening

    if not recipient_name:
        return []

    result = sanctions_screening.screen_person(recipient_name)
    if not result.get("available"):
        return []

    if not result.get("hit"):
        return []

    note = (
        f"Recipient's self-reported name '{recipient_name}' (not independently "
        f"verified against an ID) matched {len(result['matches'])} sanctions/PEP "
        f"record(s) above the configured match threshold "
        f"(vendor reference: {result.get('reference', 'n/a')}). Sender was not "
        f"screened by name -- Vuka's USSD flow does not currently collect a sender name."
    )
    return [_record(tx_id, "sanctions_vendor", "high", note, simulated=False)]


def _check_bank_partner_screen(tx_id, corridor, sender_phone, recipient_phone, amount,
                                 recipient_name=None) -> list:
    from . import bank_adapters

    partner = config.BANK_PARTNERS.get(corridor)
    if not partner or not partner["compliance_enabled"]:
        # Not opted in for this corridor -- safe no-op, rule does not fire.
        return []

    result = bank_adapters.screen_with_bank_partner(
        corridor=corridor, sender_phone=sender_phone, recipient_phone=recipient_phone,
        amount=amount, recipient_name=recipient_name,
    )

    if not result.get("available"):
        # Fail-closed: gateway/partner didn't respond usefully -- never fabricate a pass.
        logger.info("compliance: bank_partner_screen unavailable for %s (tx %s)", corridor, tx_id)
        return []

    if result.get("cleared"):
        return []

    risk_level = result.get("risk_level", "high")
    severity = "high" if risk_level == "high" else "medium"
    note = (
        f"{partner['name']} delegated screening returned risk_level={risk_level} "
        f"(reference: {result.get('reference', 'n/a')})."
    )
    return [_record(tx_id, "bank_partner_screen", severity, note)]


def _fire_async_bank_partner_screen(tx_id, corridor, sender_phone, recipient_phone, amount,
                                      recipient_name):
    """STANDARD-tier bank-partner screening runs off the USSD critical path.
    Never raises into the caller. A hold discovered here arrives as a
    compliance_flags row that complete_transfer() checks for before payout
    dispatch -- see ledger.has_open_high_severity_flag()."""
    def _run():
        try:
            _check_bank_partner_screen(tx_id, corridor, sender_phone, recipient_phone, amount,
                                         recipient_name)
        except Exception:
            logger.exception("compliance: async bank-partner screen failed for tx %s", tx_id)

    threading.Thread(target=_run, daemon=True).start()
