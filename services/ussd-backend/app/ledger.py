"""
SQLite ledger for Vuka. All reads/writes of sender_phone/recipient_phone go
through crypto.py -- never construct raw SQL against those columns with a
plaintext phone number; use the *_hash blind-index columns for lookups.
"""
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

from . import config, crypto

os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id                    TEXT PRIMARY KEY,
                created_at            REAL NOT NULL,
                updated_at            REAL NOT NULL,
                session_id            TEXT,
                transaction_type      TEXT NOT NULL,   -- 'convert_transact' | 'merchant_payment'
                corridor              TEXT,             -- UGANDA | TANZANIA | RWANDA | GHANA | NULL for merchant
                sender_phone          TEXT NOT NULL,    -- encrypted (or plaintext if encryption disabled)
                sender_phone_hash     TEXT NOT NULL,    -- blind index for lookups
                recipient_phone       TEXT,
                recipient_phone_hash  TEXT,
                recipient_name        TEXT,
                send_amount           REAL NOT NULL,
                send_currency         TEXT NOT NULL,
                payout_amount         REAL,             -- QUOTE LOCKED at creation time -- never recomputed at dispatch
                payout_currency       TEXT,
                fee_amount            REAL,             -- combined (Vuka + partner) fee, in the ORIGIN currency
                vuka_fee              REAL,             -- Vuka's own cut of fee_amount, origin currency
                partner_fee           REAL,             -- settlement partner's cut of fee_amount, origin currency
                fx_rate               REAL,
                rate_lock_expires_at  REAL,             -- created_at + VUKA_RATE_LOCK_SECONDS
                due_diligence_tier    TEXT,             -- low | standard | enhanced (convert_transact only)
                status                TEXT NOT NULL,    -- see STATUS_* constants below
                collection_status     TEXT,             -- pending | confirmed | failed
                collection_reference  TEXT,
                collection_simulated  INTEGER,
                collection_method     TEXT,             -- daraja | onafriq | simulated
                mpesa_receipt         TEXT,
                payout_status         TEXT,              -- pending | dispatched | confirmed | failed
                payout_reference      TEXT,
                payout_simulated      INTEGER,
                payout_method         TEXT,              -- thunes | bank_partner | simulated
                otp_verified          INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_tx_sender_hash ON transactions (sender_phone_hash);
            CREATE INDEX IF NOT EXISTS idx_tx_created_at ON transactions (created_at);
            CREATE INDEX IF NOT EXISTS idx_tx_status ON transactions (status);

            CREATE TABLE IF NOT EXISTS float_pools (
                corridor      TEXT PRIMARY KEY,
                currency      TEXT NOT NULL,
                balance       REAL NOT NULL DEFAULT 0,
                low_threshold REAL NOT NULL DEFAULT 0,
                updated_at    REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS otp_log (
                id          TEXT PRIMARY KEY,
                transaction_id TEXT NOT NULL,
                code_hash   TEXT NOT NULL,
                created_at  REAL NOT NULL,
                expires_at  REAL NOT NULL,
                verified_at REAL,
                attempts    INTEGER DEFAULT 0,
                FOREIGN KEY (transaction_id) REFERENCES transactions (id)
            );

            CREATE TABLE IF NOT EXISTS compliance_flags (
                id              TEXT PRIMARY KEY,
                transaction_id  TEXT NOT NULL,
                rule            TEXT NOT NULL,     -- velocity | large_amount | local_watchlist | bank_partner_screen
                severity        TEXT NOT NULL,     -- medium | high
                note            TEXT NOT NULL,
                simulated       INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'open',  -- open | cleared | escalated
                created_at      REAL NOT NULL,
                resolved_at     REAL,
                resolved_by     TEXT,
                FOREIGN KEY (transaction_id) REFERENCES transactions (id)
            );

            CREATE INDEX IF NOT EXISTS idx_flags_status ON compliance_flags (status);
            CREATE INDEX IF NOT EXISTS idx_flags_tx ON compliance_flags (transaction_id);

            CREATE TABLE IF NOT EXISTS ai_risk_scores (
                id             TEXT PRIMARY KEY,
                transaction_id TEXT NOT NULL,
                score          REAL NOT NULL,   -- 0.0 (low risk) - 1.0 (high risk)
                label          TEXT NOT NULL,   -- low | medium | high
                rationale      TEXT,
                simulated      INTEGER NOT NULL DEFAULT 0,
                created_at     REAL NOT NULL,
                FOREIGN KEY (transaction_id) REFERENCES transactions (id)
            );
            CREATE INDEX IF NOT EXISTS idx_risk_tx ON ai_risk_scores (transaction_id);

            CREATE TABLE IF NOT EXISTS ussd_sessions (
                session_id  TEXT PRIMARY KEY,
                state_json  TEXT NOT NULL,
                updated_at  REAL NOT NULL
            );
            """
        )
        # Seed sizes are proportional to 2023 World Bank diaspora remittance inflows
        # (Kenya $4.2B, Uganda $1.4B, Tanzania $0.7B, Rwanda $0.5B, Ghana $4.6B) --
        # realistic RELATIVE corridor sizing for a demo treasury budget, not literal
        # balance-sheet size. Kenya is a full corridor now (Vuka is multi-origin,
        # not a Kenya-only hub), so it's seeded the same way as the other four
        # rather than as a separate informational-only row. XOF is seeded as a
        # placeholder for the expansion belt (never paid out from directly yet).
        _SEED_WEIGHTS = {"KENYA": 4.2, "UGANDA": 1.4, "TANZANIA": 0.7, "RWANDA": 0.5, "GHANA": 4.6}
        _SEED_UNIT = 5_000_000  # demo-scale base unit, in each corridor's local currency
        for corridor, meta in config.CORRIDORS.items():
            seed_balance = _SEED_WEIGHTS.get(corridor, 1.0) * _SEED_UNIT
            conn.execute(
                """INSERT OR IGNORE INTO float_pools (corridor, currency, balance, low_threshold, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (corridor, meta["currency"], seed_balance, seed_balance * 0.1, time.time()),
            )
        conn.execute(
            """INSERT OR IGNORE INTO float_pools (corridor, currency, balance, low_threshold, updated_at)
               VALUES ('XOF_EXPANSION', 'XOF', 0, 0, ?)""",
            (time.time(),),
        )


# Status constants -- kept as plain strings across the app for readability in logs/dashboard.
STATUS_INITIATED = "initiated"
STATUS_AWAITING_OTP = "awaiting_otp"
STATUS_AWAITING_COLLECTION = "awaiting_collection"
STATUS_COLLECTED = "collected"
STATUS_DISPATCHING_PAYOUT = "dispatching_payout"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
STATUS_REFUNDED = "refunded"
STATUS_COMPLIANCE_HOLD = "compliance_hold"


def new_transaction(
    session_id: str,
    transaction_type: str,
    sender_phone: str,
    send_amount: float,
    send_currency: str,
    corridor: str = None,
    recipient_phone: str = None,
    recipient_name: str = None,
    quote: dict = None,
    due_diligence_tier: str = None,
) -> str:
    """If `quote` is provided (convert_transact only, from fx.get_quote_for_recipient),
    it is the single source of truth for every money-related field on this row:
    payout_amount/currency (locked = exactly what the sender typed, the recipient-
    gets-full-amount guarantee), send_amount/currency (derived from the quote's
    sender_debit_kes -- the actual KES amount collected, NOT the caller's
    send_amount/send_currency args, which are ignored when a quote is present so
    there's no way for those to silently drift from the locked quote), and the
    vuka_fee/partner_fee/fx_rate breakdown.

    When quote is None (merchant_payment -- no cross-border leg, no gross-up),
    send_amount/send_currency are used as passed, and all quote-derived fields
    are NULL.

    complete_transfer() must reuse these exact locked values later, never
    recompute a fresh quote at dispatch time -- that's what makes the rate-lock
    window meaningful."""
    tx_id = str(uuid.uuid4())
    now = time.time()
    rate_lock_expires_at = (now + config.VUKA_RATE_LOCK_SECONDS) if quote else None

    if quote:
        stored_send_amount = quote["sender_debit"]
        stored_send_currency = quote["origin_currency"]
    else:
        stored_send_amount = send_amount
        stored_send_currency = send_currency

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO transactions (
                id, created_at, updated_at, session_id, transaction_type, corridor,
                sender_phone, sender_phone_hash, recipient_phone, recipient_phone_hash,
                recipient_name, send_amount, send_currency, payout_amount, payout_currency,
                fee_amount, vuka_fee, partner_fee, fx_rate, rate_lock_expires_at,
                due_diligence_tier, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tx_id,
                now,
                now,
                session_id,
                transaction_type,
                corridor,
                crypto.encrypt_phone(sender_phone),
                crypto.phone_blind_index(sender_phone),
                crypto.encrypt_phone(recipient_phone) if recipient_phone else None,
                crypto.phone_blind_index(recipient_phone) if recipient_phone else None,
                recipient_name,
                stored_send_amount,
                stored_send_currency,
                quote["payout_amount"] if quote else None,
                quote["payout_currency"] if quote else None,
                quote["fee"] if quote else None,
                quote["vuka_fee"] if quote else None,
                quote["partner_fee"] if quote else None,
                quote["vuka_rate"] if quote else None,
                rate_lock_expires_at,
                due_diligence_tier,
                STATUS_INITIATED,
            ),
        )
    return tx_id


def update_transaction(tx_id: str, **fields):
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE transactions SET {cols} WHERE id = ?", (*fields.values(), tx_id))


def get_transaction_by_pending_collection_reference(reference: str) -> dict:
    """Used by the Daraja callback handler for authenticity: Safaricom's STK
    result callback carries no signature, so the only way to trust it is to
    verify the CheckoutRequestID matches a transaction THIS APP already parked
    in pending collection state. Deliberately scoped to collection_status='pending'
    -- a reference matching an already-resolved transaction is not a valid replay
    target and returns None, same as a reference that doesn't exist at all."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM transactions
               WHERE collection_reference = ? AND collection_status = 'pending'""",
            (reference,),
        ).fetchone()
    if not row:
        return None
    tx = dict(row)
    tx["sender_phone"] = crypto.decrypt_phone(tx["sender_phone"])
    if tx.get("recipient_phone"):
        tx["recipient_phone"] = crypto.decrypt_phone(tx["recipient_phone"])
    return tx


def get_transaction(tx_id: str, decrypt: bool = True) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if not row:
        return None
    tx = dict(row)
    if decrypt:
        tx["sender_phone"] = crypto.decrypt_phone(tx["sender_phone"])
        if tx.get("recipient_phone"):
            tx["recipient_phone"] = crypto.decrypt_phone(tx["recipient_phone"])
    return tx


def mark_rate_expired_refund(tx_id: str):
    update_transaction(tx_id, status=STATUS_REFUNDED)


def count_recent_transactions_for_phone(sender_phone: str, window_minutes: int) -> int:
    """Used by the compliance velocity rule. Looks up by blind index -- never decrypts in bulk."""
    since = time.time() - (window_minutes * 60)
    phone_hash = crypto.phone_blind_index(sender_phone)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM transactions WHERE sender_phone_hash = ? AND created_at >= ?",
            (phone_hash, since),
        ).fetchone()
    return row["c"] if row else 0


def phone_on_local_watchlist(phone: str, watchlist: list) -> bool:
    return phone in watchlist


def lookup_known_recipient(sender_phone: str, recipient_phone: str, corridor: str = None) -> dict:
    """Vuka ID: looks up the most recent name THIS SENDER used for this exact
    recipient phone number in a past COMPLETED transaction, scoped to the same
    corridor (or NULL corridor for merchant payments -- a different context).
    Deliberately never looks across different senders' histories for the same
    phone number -- that would leak one sender's relationship/naming for a
    number to a totally different sender, a real privacy problem even though
    the phone number itself isn't secret. The returned name is still
    self-reported (by this same sender, previously) -- never independently
    verified against an ID, same caveat as a freshly-typed name."""
    sender_hash = crypto.phone_blind_index(sender_phone)
    recipient_hash = crypto.phone_blind_index(recipient_phone)
    with get_conn() as conn:
        if corridor:
            row = conn.execute(
                """SELECT recipient_name FROM transactions
                   WHERE sender_phone_hash = ? AND recipient_phone_hash = ?
                     AND corridor = ? AND status = 'complete' AND recipient_name IS NOT NULL
                   ORDER BY created_at DESC LIMIT 1""",
                (sender_hash, recipient_hash, corridor),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT recipient_name FROM transactions
                   WHERE sender_phone_hash = ? AND recipient_phone_hash = ?
                     AND corridor IS NULL AND status = 'complete' AND recipient_name IS NOT NULL
                   ORDER BY created_at DESC LIMIT 1""",
                (sender_hash, recipient_hash),
            ).fetchone()
    return {"recipient_name": row["recipient_name"]} if row else None


def record_compliance_flag(tx_id: str, rule: str, severity: str, note: str, simulated: bool = False) -> str:
    flag_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO compliance_flags (id, transaction_id, rule, severity, note, simulated, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'open', ?)""",
            (flag_id, tx_id, rule, severity, note, int(simulated), time.time()),
        )
    return flag_id


def has_open_high_severity_flag(tx_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as c FROM compliance_flags
               WHERE transaction_id = ? AND severity = 'high' AND status = 'open'""",
            (tx_id,),
        ).fetchone()
    return bool(row["c"])


def get_open_compliance_flags() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM compliance_flags WHERE status = 'open' ORDER BY created_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_compliance_flag(flag_id: str, new_status: str, resolved_by: str):
    assert new_status in ("cleared", "escalated")
    with get_conn() as conn:
        conn.execute(
            """UPDATE compliance_flags SET status = ?, resolved_at = ?, resolved_by = ? WHERE id = ?""",
            (new_status, time.time(), resolved_by, flag_id),
        )


def adjust_float_pool(corridor: str, delta: float):
    with get_conn() as conn:
        conn.execute(
            """UPDATE float_pools SET balance = balance + ?, updated_at = ? WHERE corridor = ?""",
            (delta, time.time(), corridor),
        )


def get_float_pool(corridor: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM float_pools WHERE corridor = ?", (corridor,)).fetchone()
    return dict(row) if row else None


def get_all_float_pools() -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM float_pools").fetchall()
    return [dict(r) for r in rows]


def record_risk_score(tx_id: str, score: float, label: str, rationale: str, simulated: bool = False) -> str:
    score_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO ai_risk_scores (id, transaction_id, score, label, rationale, simulated, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (score_id, tx_id, score, label, rationale, int(simulated), time.time()),
        )
    return score_id


def get_latest_risk_score(tx_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM ai_risk_scores WHERE transaction_id = ? ORDER BY created_at DESC LIMIT 1""",
            (tx_id,),
        ).fetchone()
    return dict(row) if row else None


def get_session_state(session_id: str) -> str:
    """Returns the raw JSON string for a session, or None if not found."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT state_json FROM ussd_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    return row["state_json"] if row else None


def set_session_state(session_id: str, state_json: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ussd_sessions (session_id, state_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET state_json = excluded.state_json, "
            "updated_at = excluded.updated_at",
            (session_id, state_json, time.time()),
        )


def delete_session_state(session_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM ussd_sessions WHERE session_id = ?", (session_id,))


def purge_stale_sessions(older_than_seconds: int) -> int:
    """Housekeeping -- call periodically (e.g. from a cron-style workflow) so
    ussd_sessions doesn't grow unbounded from abandoned sessions."""
    cutoff = time.time() - older_than_seconds
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM ussd_sessions WHERE updated_at < ?", (cutoff,))
        return cur.rowcount


def get_setting(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
