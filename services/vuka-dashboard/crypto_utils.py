"""
Decrypt-only mirror of services/ussd-backend/app/crypto.py.

The dashboard is read-only with respect to sensitive data: it needs to
display phone numbers for compliance review, but must never be the thing
that writes an encrypted value. Keeping this as a separate, smaller module
(rather than importing the backend's crypto.py directly) makes that
boundary explicit and means the dashboard can be deployed/reasoned about
independently of the backend service.

Uses the same SESSION_SECRET -> Fernet-key derivation as the backend, so it
can decrypt what the backend encrypted. If SESSION_SECRET is unset, or a
value isn't a valid Fernet token (e.g. it predates encryption, or
encryption is disabled backend-side too), values are passed through
unchanged -- same graceful-coexistence behavior as the backend.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()

SESSION_SECRET = os.environ.get("SESSION_SECRET")

_fernet = None
if SESSION_SECRET:
    digest = hashlib.sha256(SESSION_SECRET.encode("utf-8")).digest()
    _fernet = Fernet(base64.urlsafe_b64encode(digest))


def decrypt_phone(value: str) -> str:
    if not value:
        return value
    if not _fernet:
        return value
    try:
        return _fernet.decrypt(value.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return value


def mask_phone(phone: str, keep_last: int = 3) -> str:
    """For display in list views -- full number only shown on demand (e.g. in
    the flag detail expander), not in the default table."""
    if not phone or len(phone) <= keep_last:
        return phone or ""
    return "*" * (len(phone) - keep_last) + phone[-keep_last:]
