"""
Dashboard's own data-access layer -- deliberately separate from the backend's
app/ledger.py so the dashboard can be deployed/reasoned about as a
self-contained read-mostly service.

Two connection modes:
  - get_read_conn(): opened in SQLite URI read-only mode (mode=ro). All
    transaction/float-pool/audit queries go through this. This connection
    physically cannot write, even by accident.
  - get_write_conn(): a normal read-write connection, used ONLY for the two
    narrow, explicit mutations the dashboard is responsible for: resolving a
    compliance flag (Clear/Escalate) and setting a live margin override.
    Nothing else in this module writes.
"""
import csv
import io
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "VUKA_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "ussd-backend", "app", "data", "vuka.db"),
)


@contextmanager
def get_read_conn():
    uri = f"file:{os.path.abspath(DB_PATH)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_write_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def db_exists() -> bool:
    return os.path.exists(DB_PATH)


# ---------------------------------------------------------------------------
# Business stats
# ---------------------------------------------------------------------------
def get_summary_stats() -> dict:
    with get_read_conn() as conn:
        total_tx = conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"]
        completed = conn.execute(
            "SELECT COUNT(*) c FROM transactions WHERE status = 'complete'"
        ).fetchone()["c"]
        failed = conn.execute(
            "SELECT COUNT(*) c FROM transactions WHERE status = 'failed'"
        ).fetchone()["c"]
        held = conn.execute(
            "SELECT COUNT(*) c FROM transactions WHERE status = 'compliance_hold'"
        ).fetchone()["c"]
        # Revenue = Vuka's OWN cut (vuka_fee) only -- fee_amount is the combined
        # Vuka + settlement-partner fee and would overstate what Vuka actually earns.
        # Grouped by send_currency: fees are now in whatever the SENDER's origin
        # currency is (Vuka is multi-origin, not KES-only), so summing across
        # currencies into one number would silently blend KES + GHS + UGX etc.
        # as if they were the same unit -- never do that, report per-currency instead.
        revenue_by_currency = conn.execute(
            """SELECT send_currency AS currency,
                      COALESCE(SUM(CASE WHEN collection_simulated = 0 AND payout_simulated = 0
                                    THEN vuka_fee ELSE 0 END), 0) AS confirmed,
                      COALESCE(SUM(CASE WHEN collection_simulated = 1 OR payout_simulated = 1
                                    THEN vuka_fee ELSE 0 END), 0) AS simulated,
                      COALESCE(SUM(partner_fee), 0) AS partner_total
               FROM transactions
               WHERE status = 'complete' AND send_currency IS NOT NULL
               GROUP BY send_currency"""
        ).fetchall()
        pending_by_currency = conn.execute(
            """SELECT send_currency AS currency, COALESCE(SUM(vuka_fee), 0) AS pending
               FROM transactions
               WHERE status IN ('awaiting_collection', 'collected', 'dispatching_payout')
                 AND send_currency IS NOT NULL
               GROUP BY send_currency"""
        ).fetchall()
        gross_volume_by_currency = conn.execute(
            """SELECT send_currency AS currency, COALESCE(SUM(send_amount), 0) AS total
               FROM transactions
               WHERE status = 'complete' AND send_currency IS NOT NULL
               GROUP BY send_currency"""
        ).fetchall()

    return {
        "total_transactions": total_tx,
        "completed": completed,
        "failed": failed,
        "compliance_held": held,
        "revenue_by_currency": [dict(r) for r in revenue_by_currency],
        "pending_by_currency": [dict(r) for r in pending_by_currency],
        "gross_volume_by_currency": [dict(r) for r in gross_volume_by_currency],
    }


def get_corridor_volumes() -> list:
    """payout_amount/payout_currency are fixed per DESTINATION corridor (e.g.
    UGANDA always pays out in UGX regardless of the sender's origin country),
    so summing payout_amount per corridor is unambiguous. send_amount would be
    in the sender's origin currency, which now varies transaction-to-transaction
    -- summing that per corridor would silently blend different currencies."""
    with get_read_conn() as conn:
        rows = conn.execute(
            """SELECT corridor, payout_currency AS currency, COUNT(*) as tx_count,
                      COALESCE(SUM(payout_amount), 0) as total_payout
               FROM transactions
               WHERE corridor IS NOT NULL AND status = 'complete'
               GROUP BY corridor
               ORDER BY total_payout DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_revenue_timeseries(days: int = 30) -> list:
    """Grouped by day AND currency -- vuka_fee is in the sender's origin currency,
    which now varies, so blending across currencies into one 'confirmed'/'simulated'
    number per day would be the same mistake as the other revenue queries above."""
    since = time.time() - days * 86400
    with get_read_conn() as conn:
        rows = conn.execute(
            """SELECT
                 CAST(created_at / 86400 AS INTEGER) * 86400 as day_bucket,
                 send_currency as currency,
                 SUM(CASE WHEN status = 'complete' AND collection_simulated = 0 AND payout_simulated = 0
                          THEN vuka_fee ELSE 0 END) as confirmed,
                 SUM(CASE WHEN status = 'complete' AND (collection_simulated = 1 OR payout_simulated = 1)
                          THEN vuka_fee ELSE 0 END) as simulated
               FROM transactions
               WHERE created_at >= ? AND send_currency IS NOT NULL
               GROUP BY day_bucket, send_currency
               ORDER BY day_bucket""",
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_transactions(limit: int = 50) -> list:
    with get_read_conn() as conn:
        rows = conn.execute(
            """SELECT id, created_at, transaction_type, corridor, send_amount, send_currency,
                      payout_amount, payout_currency, status, collection_status, payout_status,
                      collection_simulated, payout_simulated, recipient_name
               FROM transactions ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Float pools
# ---------------------------------------------------------------------------
def get_float_pools() -> list:
    with get_read_conn() as conn:
        rows = conn.execute("SELECT * FROM float_pools ORDER BY corridor").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Compliance / AML-KYC
# ---------------------------------------------------------------------------
def get_open_flags() -> list:
    with get_read_conn() as conn:
        rows = conn.execute(
            """SELECT f.*, t.corridor, t.send_amount, t.send_currency, t.recipient_name,
                      t.sender_phone, t.recipient_phone
               FROM compliance_flags f
               JOIN transactions t ON t.id = f.transaction_id
               WHERE f.status = 'open'
               ORDER BY f.created_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_flags(limit: int = 500) -> list:
    with get_read_conn() as conn:
        rows = conn.execute(
            """SELECT f.*, t.corridor, t.send_amount, t.send_currency, t.recipient_name,
                      t.sender_phone, t.recipient_phone
               FROM compliance_flags f
               JOIN transactions t ON t.id = f.transaction_id
               ORDER BY f.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_flag(flag_id: str, new_status: str, resolved_by: str):
    """The dashboard's one narrow write path into compliance_flags. new_status
    must be 'cleared' or 'escalated' -- 'escalated' is the SAR-equivalent
    outcome kept on record here, not filed with any regulator."""
    assert new_status in ("cleared", "escalated")
    with get_write_conn() as conn:
        conn.execute(
            "UPDATE compliance_flags SET status = ?, resolved_at = ?, resolved_by = ? WHERE id = ?",
            (new_status, time.time(), resolved_by, flag_id),
        )


def export_flags_csv(flags: list) -> str:
    """SAR export -- produces a CSV of the given flags (typically escalated ones).
    This is a local export, not a submission to any Financial Reporting Centre."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["flag_id", "transaction_id", "rule", "severity", "status", "note", "simulated",
         "created_at", "resolved_at", "resolved_by", "corridor", "send_amount", "send_currency"]
    )
    for f in flags:
        writer.writerow(
            [
                f.get("id"), f.get("transaction_id"), f.get("rule"), f.get("severity"),
                f.get("status"), f.get("note"), f.get("simulated"), f.get("created_at"),
                f.get("resolved_at"), f.get("resolved_by"), f.get("corridor"),
                f.get("send_amount"), f.get("send_currency"),
            ]
        )
    return buf.getvalue()


def generate_sar_document(flag: dict) -> str:
    """Produces a draft SAR-style document for ONE escalated flag -- a formatting
    convenience for a human compliance officer to review/complete, NOT a
    submission to any Financial Reporting Centre or regulator. Vuka has no
    direct regulator filing API. Never describe this as "SAR filing" to a
    bank partner or regulator -- it's a draft aid."""
    from crypto_utils import decrypt_phone, mask_phone

    sender = mask_phone(decrypt_phone(flag.get("sender_phone") or ""))
    recipient = mask_phone(decrypt_phone(flag.get("recipient_phone") or "")) if flag.get("recipient_phone") else "N/A"
    recipient_name = flag.get("recipient_name")

    lines = [
        "=" * 70,
        "DRAFT SUSPICIOUS ACTIVITY REPORT (SAR) -- INTERNAL WORKING DOCUMENT",
        "NOT a regulator filing. Vuka has no direct FIU/regulator filing",
        "integration. This is a starting point for a human compliance officer.",
        "=" * 70,
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        f"Flag ID: {flag.get('id')}",
        f"Transaction ID: {flag.get('transaction_id')}",
        f"Rule triggered: {flag.get('rule')}",
        f"Severity: {flag.get('severity')}",
        f"Flag status: {flag.get('status')}",
        f"Resolved by: {flag.get('resolved_by') or 'N/A'}",
        "",
        "-- Transaction details --",
        f"Corridor: {flag.get('corridor') or 'N/A (merchant payment)'}",
        f"Amount: {flag.get('send_amount')} {flag.get('send_currency')}",
        f"Sender phone (masked): {sender}",
        f"Recipient phone (masked): {recipient}",
        f"Recipient name (SELF-REPORTED, NOT independently verified against any ID): "
        f"{recipient_name or 'N/A'}",
        "",
        "-- Rule detail --",
        flag.get("note", ""),
        "",
        "-- Compliance officer notes --",
        "[ fill in: narrative, supporting evidence, decision rationale ]",
        "",
        "=" * 70,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Settings / live margin tuning
# ---------------------------------------------------------------------------
def get_margin_bps(corridor: str, default: float = 150) -> float:
    with get_read_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (f"margin_bps:{corridor}",)
        ).fetchone()
    if row is None:
        return default
    try:
        return float(row["value"])
    except (ValueError, TypeError):
        return default


def set_margin_bps(corridor: str, bps: float):
    with get_write_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"margin_bps:{corridor}", str(bps)),
        )
