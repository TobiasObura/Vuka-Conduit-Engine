"""
OTP generation, delivery (Africa's Talking SMS, or simulated), and verification.

The code itself is never stored in plaintext -- only its SHA-256 hash goes into
otp_log, matching the same "never store raw sensitive data" posture as crypto.py.
"""
import hashlib
import logging
import random
import time
import uuid

from . import config, ledger

logger = logging.getLogger("vuka.otp")

OTP_LENGTH = 5
OTP_TTL_SECONDS = 300
MAX_ATTEMPTS = 3


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def generate_and_send(tx_id: str, phone_number: str) -> str:
    code = "".join(random.choices("0123456789", k=OTP_LENGTH))
    now = time.time()

    with ledger.get_conn() as conn:
        conn.execute(
            """INSERT INTO otp_log (id, transaction_id, code_hash, created_at, expires_at, attempts)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (str(uuid.uuid4()), tx_id, _hash_code(code), now, now + OTP_TTL_SECONDS),
        )

    _send_sms(phone_number, code)
    return code


def _send_sms(phone_number: str, code: str):
    message = f"Your Vuka verification code is {code}. Valid for 5 minutes."
    if not config.SMS_CONFIGURED:
        logger.info("otp: SIMULATED SMS to %s: %s", phone_number, message)
        return

    import requests

    try:
        resp = requests.post(
            "https://api.africastalking.com/version1/messaging",
            headers={
                "apiKey": config.AT_API_KEY,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "username": config.AT_USERNAME,
                "to": phone_number,
                "message": message,
            },
            timeout=10,
        )
        resp.raise_for_status()
    except Exception:
        logger.exception("otp: Africa's Talking SMS send failed for %s", phone_number)


def verify(tx_id: str, submitted_code: str) -> bool:
    with ledger.get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM otp_log WHERE transaction_id = ? ORDER BY created_at DESC LIMIT 1""",
            (tx_id,),
        ).fetchone()

    if not row:
        return False
    if row["verified_at"]:
        return False
    if time.time() > row["expires_at"]:
        return False
    if row["attempts"] >= MAX_ATTEMPTS:
        return False

    with ledger.get_conn() as conn:
        conn.execute("UPDATE otp_log SET attempts = attempts + 1 WHERE id = ?", (row["id"],))

    if _hash_code(submitted_code) != row["code_hash"]:
        return False

    with ledger.get_conn() as conn:
        conn.execute("UPDATE otp_log SET verified_at = ? WHERE id = ?", (time.time(), row["id"]))
    return True
