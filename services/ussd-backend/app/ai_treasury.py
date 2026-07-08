"""
Natural-language treasury assistant, called from the dashboard's AI tab.

build_privacy_safe_summary() is the boundary that matters here: it computes
only aggregate figures (counts, sums, corridor/currency breakdowns) directly
from the ledger -- it never selects sender_phone, recipient_phone, or
recipient_name, encrypted or not. That summary (not raw transaction rows) is
the only thing that ever gets sent to Gemini.
"""
import logging

from . import ai_gemini, config, ledger

logger = logging.getLogger("vuka.ai_treasury")

# Re-exported so callers (the dashboard) can check this without importing
# ai_gemini directly.
SIMULATION_MODE_GEMINI = ai_gemini.SIMULATION_MODE_GEMINI


def build_privacy_safe_summary() -> dict:
    with ledger.get_conn() as conn:
        total_tx = conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"]
        by_status = conn.execute(
            "SELECT status, COUNT(*) c FROM transactions GROUP BY status"
        ).fetchall()
        # payout_amount/payout_currency are fixed per DESTINATION corridor (e.g.
        # UGANDA always pays out in UGX regardless of which country the sender
        # dialed in from), so summing payout_amount per corridor is unambiguous.
        # send_amount, by contrast, is in the SENDER's origin currency, which now
        # varies transaction-to-transaction even within the same destination
        # corridor -- summing that per corridor would silently blend currencies.
        corridor_volumes = conn.execute(
            """SELECT corridor, payout_currency AS currency, COUNT(*) tx_count,
                      COALESCE(SUM(payout_amount),0) total_payout
               FROM transactions WHERE corridor IS NOT NULL GROUP BY corridor"""
        ).fetchall()
        revenue_by_currency = conn.execute(
            """SELECT send_currency AS currency,
                      COALESCE(SUM(CASE WHEN collection_simulated=0 AND payout_simulated=0
                                    THEN vuka_fee ELSE 0 END),0) AS confirmed,
                      COALESCE(SUM(CASE WHEN collection_simulated=1 OR payout_simulated=1
                                    THEN vuka_fee ELSE 0 END),0) AS simulated
               FROM transactions
               WHERE status='complete' AND send_currency IS NOT NULL
               GROUP BY send_currency"""
        ).fetchall()
        open_flags = conn.execute(
            "SELECT severity, COUNT(*) c FROM compliance_flags WHERE status='open' GROUP BY severity"
        ).fetchall()

    return {
        "total_transactions": total_tx,
        "by_status": {r["status"]: r["c"] for r in by_status},
        "corridor_volumes": [dict(r) for r in corridor_volumes],
        "float_pools": ledger.get_all_float_pools(),
        "revenue_by_currency": [dict(r) for r in revenue_by_currency],
        "open_compliance_flags_by_severity": {r["severity"]: r["c"] for r in open_flags},
    }


def ask(question: str) -> str:
    summary = build_privacy_safe_summary()

    if ai_gemini.SIMULATION_MODE_GEMINI:
        return (
            "Gemini isn't configured, so I can't answer in natural language yet -- "
            "here's the raw current summary instead:\n\n"
            f"- Total transactions: {summary['total_transactions']}\n"
            f"- By status: {summary['by_status']}\n"
            f"- Corridor volumes (destination-currency payout totals): {summary['corridor_volumes']}\n"
            f"- Float pools: {summary['float_pools']}\n"
            f"- Vuka's revenue by origin currency: {summary['revenue_by_currency']}\n"
            f"- Open compliance flags by severity: {summary['open_compliance_flags_by_severity']}"
        )

    import json

    prompt = (
        "You are a treasury assistant for a remittance company. Answer the user's question "
        "using ONLY the data in the JSON summary below -- do not invent figures that aren't "
        "present. If the summary doesn't contain what's needed to answer, say so plainly.\n\n"
        f"Summary:\n{json.dumps(summary, default=str)}\n\n"
        f"Question: {question}"
    )
    try:
        return ai_gemini.generate_content(prompt).strip()
    except ai_gemini.GeminiError:
        logger.exception("ai_treasury: Gemini call failed")
        return "I couldn't reach the AI service just now -- please try again shortly."
