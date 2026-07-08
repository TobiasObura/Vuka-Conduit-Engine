"""
Carrier-level collection routing, 4 tiers (see ARCHITECTURE.md):

  Tier 1 -- Safaricom Kenya -> Daraja direct. No aggregator margin, always
            cheapest, and the only real STK Push path (KES-only).
  Tier 2 -- All other carriers, if THUNES_MERCHANT_ID is configured -> Thunes
            Accept API (HOST_TO_HOST). Lets Vuka run single-provider (Thunes
            for both collection and payout) once both are configured.
  Tier 3 -- All other carriers, if Thunes Accept isn't configured but
            ONAFRIQ_API_KEY is -> Onafriq hub fallback.
  Tier 4 -- Neither configured -> per-currency simulation, clearly logged.

Routing is at the carrier level (network code), not just country level --
an unrecognized network code falls through to Tier 2/3's generic path rather
than blocking the transaction outright; the provider's own gateway will
reject it if genuinely unsupported.
"""
import logging

from . import config, mpesa_collection, network_profiles, onafriq_collection, thunes_collection

logger = logging.getLogger("vuka.collection_adapters")


def initiate_collection(tx_id: str, sender_phone: str, amount: float, currency: str,
                          network_code: str = None) -> dict:
    profile = network_profiles.lookup(network_code) if network_code else {"carrier": "unknown"}

    if profile.get("carrier") == network_profiles.SAFARICOM_CARRIER_NAME:
        return mpesa_collection.trigger_collection(tx_id, sender_phone, amount)

    if config.THUNES_ACCEPT_CONFIGURED:
        return thunes_collection.trigger_collection(
            tx_id, sender_phone, amount, currency, carrier=profile.get("carrier")
        )

    return onafriq_collection.trigger_collection(
        tx_id, sender_phone, amount, currency, carrier=profile.get("carrier")
    )
