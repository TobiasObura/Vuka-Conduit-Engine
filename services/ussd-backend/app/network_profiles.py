"""
MCC-MNC -> country/currency/carrier/corridor lookup.

Africa's Talking passes the sender's networkCode on every USSD request; this
maps that to a carrier name and, for KE, tells us to route collection via
Daraja instead of Onafriq. Only the core launch markets are filled in with
real MCC-MNC pairs; everything else safely falls back to "unknown carrier"
rather than guessing.
"""

# MCC-MNC -> profile. Values pulled from public GSMA network code listings.
NETWORK_PROFILES = {
    # Kenya
    "639-02": {"country": "Kenya", "currency": "KES", "carrier": "Safaricom", "corridor": "KENYA"},
    "639-03": {"country": "Kenya", "currency": "KES", "carrier": "Airtel Kenya", "corridor": "KENYA"},
    "639-07": {"country": "Kenya", "currency": "KES", "carrier": "Telkom Kenya", "corridor": "KENYA"},
    # Uganda
    "641-01": {"country": "Uganda", "currency": "UGX", "carrier": "MTN Uganda", "corridor": "UGANDA"},
    "641-10": {"country": "Uganda", "currency": "UGX", "carrier": "Airtel Uganda", "corridor": "UGANDA"},
    # Tanzania
    "640-04": {"country": "Tanzania", "currency": "TZS", "carrier": "Vodacom Tanzania", "corridor": "TANZANIA"},
    "640-02": {"country": "Tanzania", "currency": "TZS", "carrier": "Tigo Tanzania", "corridor": "TANZANIA"},
    "640-06": {"country": "Tanzania", "currency": "TZS", "carrier": "Airtel Tanzania", "corridor": "TANZANIA"},
    # Rwanda
    "635-10": {"country": "Rwanda", "currency": "RWF", "carrier": "MTN Rwanda", "corridor": "RWANDA"},
    "635-13": {"country": "Rwanda", "currency": "RWF", "carrier": "Airtel Rwanda", "corridor": "RWANDA"},
    # Ghana
    "620-01": {"country": "Ghana", "currency": "GHS", "carrier": "MTN Ghana", "corridor": "GHANA"},
    "620-02": {"country": "Ghana", "currency": "GHS", "carrier": "Vodafone Ghana", "corridor": "GHANA"},
    "620-06": {"country": "Ghana", "currency": "GHS", "carrier": "AirtelTigo Ghana", "corridor": "GHANA"},
}

SAFARICOM_CARRIER_NAME = "Safaricom"


def lookup(network_code: str) -> dict:
    """Returns a profile dict, or a safe 'unknown' fallback if the code isn't recognized."""
    profile = NETWORK_PROFILES.get(network_code)
    if profile:
        return profile
    return {"country": None, "currency": None, "carrier": "unknown", "corridor": None}


def is_safaricom(network_code: str) -> bool:
    return lookup(network_code).get("carrier") == SAFARICOM_CARRIER_NAME
