"""
Central config for Vuka's USSD backend.

Everything here is env-var driven so the app runs fully in simulation mode
with zero configuration, and individual real integrations switch on the
moment their required env vars are present. Nothing is ever "partially live"
silently -- each *_SIMULATED flag below is the single source of truth for
whether a given leg of the flow is real or simulated, and every adapter
should check it explicitly rather than re-deriving it from raw env lookups.
"""
import os

from dotenv import load_dotenv

load_dotenv()  # picks up a .env file in the working directory, if present

# ---------------------------------------------------------------------------
# Corridors
# ---------------------------------------------------------------------------
# soft_cap is in the corridor's own currency and is the ceiling used by the
# compliance "large amount" rule (>= 80% of soft_cap => medium severity flag).
#
# Vuka is pan-African: any of these 5 countries can be the SENDER's origin or
# the RECIPIENT's destination -- Kenya isn't a privileged hub, it's just where
# the product started. Origin is detected per-session from the sender's
# network code (see network_profiles.py); the destination menu offered to a
# sender is always "the other 4" relative to their detected origin.
CORRIDORS = {
    "KENYA": {
        "country": "Kenya",
        "currency": "KES",
        "soft_cap": 300_000,
        "thunes_service_id_env": "THUNES_SERVICE_ID_KENYA",
    },
    "UGANDA": {
        "country": "Uganda",
        "currency": "UGX",
        "soft_cap": 3_000_000,
        "thunes_service_id_env": "THUNES_SERVICE_ID_UGANDA",
    },
    "TANZANIA": {
        "country": "Tanzania",
        "currency": "TZS",
        "soft_cap": 2_500_000,
        "thunes_service_id_env": "THUNES_SERVICE_ID_TANZANIA",
    },
    "RWANDA": {
        "country": "Rwanda",
        "currency": "RWF",
        "soft_cap": 1_000_000,
        "thunes_service_id_env": "THUNES_SERVICE_ID_RWANDA",
    },
    "GHANA": {
        "country": "Ghana",
        "currency": "GHS",
        "soft_cap": 10_000,
        "thunes_service_id_env": "THUNES_SERVICE_ID_GHANA",
    },
}

# Fixed display order for menus -- the destination menu shown to any sender is
# this list with their own detected origin corridor removed (always leaves 4).
ALL_CORRIDORS_ORDER = ["KENYA", "UGANDA", "TANZANIA", "RWANDA", "GHANA"]

# ---------------------------------------------------------------------------
# Thunes DGN (primary payout, all corridors)
# ---------------------------------------------------------------------------
THUNES_API_KEY = os.environ.get("THUNES_API_KEY")
THUNES_API_SECRET = os.environ.get("THUNES_API_SECRET")
THUNES_PAYER_ID = os.environ.get("THUNES_PAYER_ID")
THUNES_ENVIRONMENT = os.environ.get("THUNES_ENVIRONMENT", "pre-production")
THUNES_CALLBACK_URL = os.environ.get("THUNES_CALLBACK_URL") or (
    f"https://{os.environ['REPLIT_DOMAINS'].split(',')[0]}/ussd/webhook/thunes-callback"
    if os.environ.get("REPLIT_DOMAINS")
    else None
)
THUNES_WEBHOOK_SECRET = os.environ.get("THUNES_WEBHOOK_SECRET")

THUNES_CONFIGURED = bool(THUNES_API_KEY and THUNES_API_SECRET and THUNES_PAYER_ID)
PAYOUT_SIMULATED = not THUNES_CONFIGURED  # per-corridor bank fallback can flip this per corridor; see bank_adapters

# --- Thunes Accept API (Tier 2 collection, HOST_TO_HOST USSD push) ---
# Distinct from THUNES_PAYER_ID -- this is the Accept API's own merchant identifier.
THUNES_MERCHANT_ID = os.environ.get("THUNES_MERCHANT_ID")
THUNES_COLLECTION_CALLBACK_URL = os.environ.get("THUNES_COLLECTION_CALLBACK_URL") or (
    f"https://{os.environ['REPLIT_DOMAINS'].split(',')[0]}/ussd/webhook/thunes-collection-callback"
    if os.environ.get("REPLIT_DOMAINS")
    else None
)
THUNES_ACCEPT_CONFIGURED = bool(THUNES_API_KEY and THUNES_API_SECRET and THUNES_MERCHANT_ID)

# --- Thunes x Circle USDC settlement (convert-on-payout) ---
THUNES_USDC_MODE = os.environ.get("THUNES_USDC_MODE", "").lower() in ("true", "1", "yes")
THUNES_ACCOUNT_ID = os.environ.get("THUNES_ACCOUNT_ID")

# --- Rafiki (NALA) -- REDUNDANT payout fallback, not a Thunes replacement ---
# Only invoked when a live Thunes dispatch attempt actually fails at runtime
# (timeout/5xx/network error) -- see bank_adapters.dispatch_payout(). NALA has
# not published a public developer API reference as of this writing; the
# request/response shape in rafiki_payout.py is Vuka's own placeholder and
# needs confirming against NALA's real docs once a commercial relationship
# and API access exist.
RAFIKI_API_KEY = os.environ.get("RAFIKI_API_KEY")
RAFIKI_SECRET = os.environ.get("RAFIKI_SECRET")
RAFIKI_CONFIGURED = bool(RAFIKI_API_KEY and RAFIKI_SECRET)

# ---------------------------------------------------------------------------
# Collection -- Onafriq hub (all non-Safaricom senders)
# ---------------------------------------------------------------------------
ONAFRIQ_API_KEY = os.environ.get("ONAFRIQ_API_KEY")
ONAFRIQ_BASE_URL = os.environ.get("ONAFRIQ_BASE_URL", "https://mfsafrica.beyonicpartners.com")
ONAFRIQ_CALLBACK_URL = os.environ.get("ONAFRIQ_CALLBACK_URL") or (
    f"https://{os.environ['REPLIT_DOMAINS'].split(',')[0]}/ussd/webhook/onafriq-callback"
    if os.environ.get("REPLIT_DOMAINS")
    else None
)
ONAFRIQ_CONFIGURED = bool(ONAFRIQ_API_KEY)
ONAFRIQ_SIMULATED = not ONAFRIQ_CONFIGURED

# ---------------------------------------------------------------------------
# Collection -- Safaricom Daraja (Safaricom Kenya senders only)
# ---------------------------------------------------------------------------
DARAJA_CONSUMER_KEY = os.environ.get("DARAJA_CONSUMER_KEY")
DARAJA_CONSUMER_SECRET = os.environ.get("DARAJA_CONSUMER_SECRET")
DARAJA_SHORTCODE = os.environ.get("DARAJA_SHORTCODE")
DARAJA_PASSKEY = os.environ.get("DARAJA_PASSKEY")
DARAJA_ENV = os.environ.get("DARAJA_ENV", "sandbox")
DARAJA_CALLBACK_URL = os.environ.get("DARAJA_CALLBACK_URL") or (
    f"https://{os.environ['REPLIT_DOMAINS'].split(',')[0]}/ussd/webhook/mpesa-callback"
    if os.environ.get("REPLIT_DOMAINS")
    else None
)
DARAJA_CONFIGURED = bool(
    DARAJA_CONSUMER_KEY and DARAJA_CONSUMER_SECRET and DARAJA_SHORTCODE and DARAJA_PASSKEY
)
DARAJA_SIMULATED = not DARAJA_CONFIGURED

# ---------------------------------------------------------------------------
# Africa's Talking SMS (OTP delivery)
# ---------------------------------------------------------------------------
AT_USERNAME = os.environ.get("AT_USERNAME")
AT_API_KEY = os.environ.get("AT_API_KEY")
SMS_CONFIGURED = bool(AT_USERNAME and AT_API_KEY)
SMS_SIMULATED = not SMS_CONFIGURED

# ---------------------------------------------------------------------------
# Encryption at rest
# ---------------------------------------------------------------------------
SESSION_SECRET = os.environ.get("SESSION_SECRET")
ENCRYPTION_ENABLED = bool(SESSION_SECRET)

# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------
SANCTIONS_SCREENING_SIMULATED = True  # see compliance.py -- always True; no real list is wired in

VUKA_MLRO_NAME = os.environ.get("VUKA_MLRO_NAME")
VUKA_MLRO_EMAIL = os.environ.get("VUKA_MLRO_EMAIL")
MLRO_DESIGNATED = bool(VUKA_MLRO_NAME and VUKA_MLRO_EMAIL)

VUKA_LOCAL_WATCHLIST = [
    p.strip() for p in os.environ.get("VUKA_LOCAL_WATCHLIST", "").split(",") if p.strip()
]

COMPLIANCE_VELOCITY_THRESHOLD = int(os.environ.get("COMPLIANCE_VELOCITY_THRESHOLD", "3"))
COMPLIANCE_VELOCITY_WINDOW_MINUTES = int(os.environ.get("COMPLIANCE_VELOCITY_WINDOW_MINUTES", "60"))
COMPLIANCE_LARGE_AMOUNT_FRACTION = float(os.environ.get("COMPLIANCE_LARGE_AMOUNT_FRACTION", "0.8"))

# Tiered risk-based due diligence (SDD/CDD/EDD, mirrors FATF Recommendation 10).
# A convert_transact transaction is classified relative to its corridor's soft cap:
#   amount < VUKA_SDD_LOW_TIER_FRACTION * soft_cap                          -> low
#   VUKA_SDD_LOW_TIER_FRACTION*cap <= amount < COMPLIANCE_LARGE_AMOUNT_FRACTION*cap -> standard
#   amount >= COMPLIANCE_LARGE_AMOUNT_FRACTION * soft_cap                   -> enhanced
VUKA_SDD_LOW_TIER_FRACTION = float(os.environ.get("VUKA_SDD_LOW_TIER_FRACTION", "0.05"))

# --- Fee split (recipient-gets-full-amount model, see fx.py) ---
# Vuka's own margin defaults to this but is live-tunable per corridor via the
# dashboard's margin sliders (see fx.get_margin_bps / the settings table).
DEFAULT_VUKA_MARGIN_BPS = int(os.environ.get("DEFAULT_VUKA_MARGIN_BPS", "150"))  # 1.5%
# Settlement partner's cut -- covers their FX spread (0.2-1.0%) plus settlement
# margin. Not live-tunable from the dashboard (unlike Vuka's own margin) since
# it's set by commercial negotiation with the partner, not adjusted day-to-day.
PARTNER_FEE_BPS = int(os.environ.get("PARTNER_FEE_BPS", "150"))  # 1.5%
# What a traditional bank transfer would cost, for the comparison line shown
# to the sender -- midpoint of the 7-9% range referenced during fee-model design.
BANK_COMPARISON_FRACTION = float(os.environ.get("BANK_COMPARISON_FRACTION", "0.08"))  # 8%

# How long the FX rate quoted to the sender at confirmation stays valid.
# complete_transfer() checks this before dispatching payout; if expired it
# refunds rather than settling at a stale rate.
VUKA_RATE_LOCK_SECONDS = int(os.environ.get("VUKA_RATE_LOCK_SECONDS", "300"))

# Gateway cost allowance (Thunes FX spread 0.2-1.0% + their API commission)
# absorbed within Vuka's own margin rather than passed through as a surprise.
BANK_SETTLEMENT_BPS = int(os.environ.get("BANK_SETTLEMENT_BPS", "50"))  # 0.5%


# ---------------------------------------------------------------------------
# Per-corridor bank partner registry (payout fallback + delegated compliance)
# ---------------------------------------------------------------------------
def _build_bank_partners():
    partners = {}
    for corridor in CORRIDORS:
        gateway_url = os.environ.get(f"BANK_PARTNER_{corridor}_GATEWAY_URL")
        signing_secret = os.environ.get(f"BANK_PARTNER_{corridor}_SIGNING_SECRET")
        name = os.environ.get(f"BANK_PARTNER_{corridor}_NAME")
        compliance_flag = os.environ.get(f"BANK_PARTNER_{corridor}_COMPLIANCE_ENABLED", "").lower() in (
            "true",
            "1",
            "yes",
        )
        partners[corridor] = {
            "gateway_url": gateway_url,
            "signing_secret": signing_secret,
            "name": name or f"{corridor.title()} Bank Partner",
            # A configured gateway does NOT imply compliance screening is available.
            # Both the gateway credentials AND the explicit flag must be present.
            "payout_configured": bool(gateway_url and signing_secret),
            "compliance_enabled": bool(gateway_url and signing_secret and compliance_flag),
        }
    return partners


BANK_PARTNERS = _build_bank_partners()

# ---------------------------------------------------------------------------
# WhatsApp low-float alerts
# ---------------------------------------------------------------------------
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
TREASURY_WHATSAPP_NUMBER = os.environ.get("TREASURY_WHATSAPP_NUMBER")
WHATSAPP_CONFIGURED = bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID and TREASURY_WHATSAPP_NUMBER)

DB_PATH = os.environ.get("VUKA_DB_PATH", os.path.join(os.path.dirname(__file__), "data", "vuka.db"))


def simulation_summary() -> dict:
    """Single place to log/display what's real vs simulated at boot."""
    return {
        "payout_thunes": not PAYOUT_SIMULATED,
        "collection_onafriq": not ONAFRIQ_SIMULATED,
        "collection_daraja": not DARAJA_SIMULATED,
        "sms": not SMS_SIMULATED,
        "encryption_at_rest": ENCRYPTION_ENABLED,
        "mlro_designated": MLRO_DESIGNATED,
        "sanctions_screening_real": not SANCTIONS_SCREENING_SIMULATED,
        "bank_partner_compliance_by_corridor": {
            k: v["compliance_enabled"] for k, v in BANK_PARTNERS.items()
        },
    }
