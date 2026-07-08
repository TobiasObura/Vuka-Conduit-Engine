"""
Encryption at rest for sender/recipient phone numbers.

- Fernet (AES-128-CBC + HMAC) for the stored value, key derived from SESSION_SECRET.
- A separate deterministic HMAC-SHA256 "blind index" lets us do equality lookups
  (e.g. the velocity compliance rule) WITHOUT decrypting rows in bulk.
- If SESSION_SECRET is unset, encryption is loudly disabled -- logged once at
  import time -- and phone numbers are stored in plaintext. This is never a
  silent downgrade.
- Old rows written before this feature existed are plaintext. decrypt_phone()
  passes through anything that isn't a valid Fernet token so old and new rows
  coexist without a backfill migration.
"""
import base64
import hashlib
import hmac
import logging

from cryptography.fernet import Fernet, InvalidToken

from . import config

logger = logging.getLogger("vuka.crypto")

_fernet = None
_hmac_key = None

if config.ENCRYPTION_ENABLED:
    # Derive a 32-byte urlsafe-base64 Fernet key from SESSION_SECRET.
    digest = hashlib.sha256(config.SESSION_SECRET.encode("utf-8")).digest()
    _fernet = Fernet(base64.urlsafe_b64encode(digest))
    # Separate, domain-separated key for the blind index HMAC so it's not the
    # same key material as the Fernet key.
    _hmac_key = hashlib.sha256(b"vuka-blind-index:" + config.SESSION_SECRET.encode("utf-8")).digest()
    logger.info("crypto: encryption at rest ENABLED (SESSION_SECRET present)")
else:
    logger.warning(
        "crypto: encryption at rest DISABLED -- SESSION_SECRET is not set. "
        "Phone numbers will be stored in plaintext. Set SESSION_SECRET to enable."
    )


def encrypt_phone(phone: str) -> str:
    """Encrypt a phone number for storage. Passes through unchanged if encryption is disabled."""
    if not phone:
        return phone
    if not config.ENCRYPTION_ENABLED:
        return phone
    return _fernet.encrypt(phone.encode("utf-8")).decode("utf-8")


def decrypt_phone(value: str) -> str:
    """Decrypt a stored phone value. Passes through unchanged if it isn't a valid Fernet token
    (covers both 'encryption disabled' and 'row predates encryption' cases)."""
    if not value:
        return value
    if not config.ENCRYPTION_ENABLED:
        return value
    try:
        return _fernet.decrypt(value.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return value


def phone_blind_index(phone: str) -> str:
    """Deterministic HMAC-SHA256 of a phone number, for equality lookups without decrypting.
    Falls back to a plain sha256 (no secret) if encryption is disabled -- still deterministic
    for lookups, just not keyed. Never used as the storage format, only as an index column."""
    if not phone:
        return phone
    key = _hmac_key if _hmac_key else b"vuka-unkeyed-index"
    return hmac.new(key, phone.encode("utf-8"), hashlib.sha256).hexdigest()
