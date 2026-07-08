"""
Async fraud/risk scoring. Runs AFTER the compliance hold/no-hold decision
(compliance.py already made a real, synchronous, rule-based hold/no-hold call
on the USSD critical path) -- this is a slower, advisory second opinion that
never blocks the sender's session. It's fired in a background thread from
ussd_router right after OTP verification and simply writes a score + label +
rationale to ai_risk_scores for the dashboard/ops team to see later; nothing
downstream currently gates on it.

No PII is sent to Gemini: the prompt only ever contains transaction_type,
corridor, amount, velocity_count, and hour_of_day -- never phone numbers or
names.
"""
import logging
import threading

from . import ai_gemini, config, ledger

logger = logging.getLogger("vuka.ai_risk")


def score_transaction_async(tx_id: str, transaction_type: str, corridor: str, amount: float,
                              velocity_count: int, hour_of_day: int):
    """Fire-and-forget. Never raises into the caller."""
    thread = threading.Thread(
        target=_score_and_record,
        args=(tx_id, transaction_type, corridor, amount, velocity_count, hour_of_day),
        daemon=True,
    )
    thread.start()


def _score_and_record(tx_id, transaction_type, corridor, amount, velocity_count, hour_of_day):
    try:
        score, label, rationale, simulated = _score(
            transaction_type, corridor, amount, velocity_count, hour_of_day
        )
        ledger.record_risk_score(tx_id, score, label, rationale, simulated=simulated)
    except Exception:
        logger.exception("ai_risk: scoring failed for tx %s", tx_id)


def _score(transaction_type, corridor, amount, velocity_count, hour_of_day) -> tuple:
    if not ai_gemini.SIMULATION_MODE_GEMINI:
        try:
            return _score_via_gemini(transaction_type, corridor, amount, velocity_count, hour_of_day)
        except ai_gemini.GeminiError:
            logger.warning("ai_risk: Gemini call failed, falling back to heuristic scorer")

    return _score_heuristic(transaction_type, corridor, amount, velocity_count, hour_of_day)


def _score_via_gemini(transaction_type, corridor, amount, velocity_count, hour_of_day) -> tuple:
    import json

    prompt = (
        "You are a fraud-risk scoring assistant for a remittance app. Given ONLY these "
        "non-identifying transaction features, return a JSON object with keys "
        "'score' (float 0.0-1.0, higher = riskier), 'label' (one of 'low','medium','high'), "
        "and 'rationale' (one short sentence). Do not ask for or assume any personal "
        "identifying information.\n\n"
        f"transaction_type: {transaction_type}\n"
        f"corridor: {corridor}\n"
        f"amount_kes: {amount}\n"
        f"sender_transactions_last_hour: {velocity_count}\n"
        f"hour_of_day: {hour_of_day}\n"
    )
    raw = ai_gemini.generate_content(prompt, response_mime_type="application/json")
    data = json.loads(raw)
    score = float(data["score"])
    label = data["label"]
    rationale = data.get("rationale", "")
    return score, label, rationale, False


def _score_heuristic(transaction_type, corridor, amount, velocity_count, hour_of_day) -> tuple:
    """Simple, transparent, clearly-simulated heuristic used when Gemini isn't
    configured or fails. Not a substitute for real ML-based scoring -- see
    COMPLIANCE_SCREENING_OVERVIEW.md's honest-gaps section."""
    score = 0.0
    reasons = []

    if velocity_count >= config.COMPLIANCE_VELOCITY_THRESHOLD:
        score += 0.35
        reasons.append(f"{velocity_count} recent transactions")

    if corridor and corridor in config.CORRIDORS:
        soft_cap = config.CORRIDORS[corridor]["soft_cap"]
        # `amount` is the recipient-currency amount for convert_transact (see
        # fx.get_quote_for_recipient), matching soft_cap's currency correctly.
        if amount >= soft_cap * config.COMPLIANCE_LARGE_AMOUNT_FRACTION:
            score += 0.35
            reasons.append("amount near corridor soft cap")

    if hour_of_day < 5 or hour_of_day > 23:
        score += 0.15
        reasons.append("unusual hour")

    score = min(score, 1.0)
    label = "high" if score >= 0.6 else "medium" if score >= 0.3 else "low"
    rationale = "SIMULATED heuristic score. " + (
        "Factors: " + ", ".join(reasons) if reasons else "No risk factors triggered."
    )
    return score, label, rationale, True
