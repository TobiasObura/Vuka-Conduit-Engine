"""
Real sanctions/PEP screening vendor integration point.

This is the piece COMPLIANCE_SCREENING_OVERVIEW.md calls the single biggest
honest gap: the local watchlist rule in compliance.py is a manually curated
phone-number list, not a real OFAC/UN/EU consolidated list or PEP database,
and it has no name/DOB/nationality matching.

This module is the plug for a real vendor once one is contracted. It's
written against a generic REST contract close to how ComplyAdvantage and
Refinitiv World-Check One both shape their screening APIs (search by name,
get back matches with a score and list source) -- the exact field names will
need a one-time adjustment once a specific vendor is signed, but the flow
(screen by name -> matches above a score threshold => hold) doesn't change.

Guardrails, matching the same pattern as bank_adapters.screen_with_bank_partner():
  - Strictly opt-in: SANCTIONS_VENDOR_ENABLED must be explicitly true. A
    configured API key alone does not turn this on.
  - Fail-closed: any missing config, HTTP error, or timeout returns
    {"available": False} -- compliance.py treats that as "rule did not fire",
    NEVER as a fabricated clear. This is the same posture as every other
    external check in this codebase.
  - Real screening is name-based (plus DOB/nationality if supplied), which is
    what real sanctions/PEP databases require -- phone-number-only matching
    (the local watchlist) is not adequate for this and this module does not
    pretend otherwise.

Until SANCTIONS_VENDOR_ENABLED is true and credentials are set, this rule
simply does not fire -- exactly like the bank-partner delegated screening
before a partner is onboarded. It does NOT silently replace the local
watchlist; both can run side by side, and this one is additive.
"""
import logging
import os

import requests

logger = logging.getLogger("vuka.sanctions_screening")

SANCTIONS_VENDOR_ENABLED = os.environ.get("SANCTIONS_VENDOR_ENABLED", "").lower() in ("true", "1", "yes")
SANCTIONS_VENDOR_BASE_URL = os.environ.get("SANCTIONS_VENDOR_BASE_URL")
SANCTIONS_VENDOR_API_KEY = os.environ.get("SANCTIONS_VENDOR_API_KEY")
# Match score threshold (vendor-defined scale, commonly 0-100) above which a
# result is treated as a hit worth a human review. Conservative default --
# tune only after reviewing false-positive rates with the vendor.
SANCTIONS_VENDOR_MATCH_THRESHOLD = float(os.environ.get("SANCTIONS_VENDOR_MATCH_THRESHOLD", "85"))

CONFIGURED = bool(SANCTIONS_VENDOR_ENABLED and SANCTIONS_VENDOR_BASE_URL and SANCTIONS_VENDOR_API_KEY)


def screen_person(full_name: str, date_of_birth: str = None, nationality: str = None) -> dict:
    """Returns {"available": False} if not configured or the call fails/times out.
    Returns {"available": True, "hit": bool, "matches": [...], "reference": str} on success.
    NEVER returns hit=False as a substitute for "couldn't check" -- that distinction
    is exactly what "available" is for."""
    if not CONFIGURED:
        return {"available": False}

    try:
        resp = requests.post(
            f"{SANCTIONS_VENDOR_BASE_URL}/searches",
            headers={
                "Authorization": f"Bearer {SANCTIONS_VENDOR_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "search_term": full_name,
                "date_of_birth": date_of_birth,
                "nationality": nationality,
                "fuzziness": 0.6,
                "filters": {"types": ["sanction", "warning", "pep"]},
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        matches = [
            m for m in data.get("matches", [])
            if m.get("score", 0) >= SANCTIONS_VENDOR_MATCH_THRESHOLD
        ]
        return {
            "available": True,
            "hit": len(matches) > 0,
            "matches": matches,
            "reference": data.get("search_id") or data.get("id"),
        }
    except requests.RequestException:
        logger.warning("sanctions_screening: vendor call failed/unavailable -- rule will not fire")
        return {"available": False}
    except (ValueError, KeyError):
        logger.exception("sanctions_screening: unexpected vendor response shape")
        return {"available": False}
