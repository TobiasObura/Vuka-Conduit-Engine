"""
FX + fee calculation -- RECIPIENT-GETS-FULL-AMOUNT model, ANY ORIGIN -> ANY DESTINATION.

The sender enters the amount they want the RECIPIENT to receive, in the
recipient's currency (e.g. "1000" means the Ugandan supplier gets exactly
1000 UGX, regardless of whether the sender is dialing in from Kenya, Ghana,
or anywhere else Vuka covers). Vuka's and the settlement partner's fees are
added ON TOP of the origin-currency cost of acquiring that amount -- they are
never deducted from what the recipient receives.

Vuka is pan-African, not a Kenya-only hub: any of the 5 base corridors
(Kenya, Uganda, Tanzania, Rwanda, Ghana) can be the sender's origin or the
recipient's destination. Rates are anchored to KES internally (an
implementation detail -- _RATES_FROM_KES is just the table this was already
built from) but every quote is computed as a genuine cross-rate between
whatever origin and destination corridors are involved; nothing downstream
assumes KES is the sender's currency.

Fee split (see ARCHITECTURE.md's settled fee economics):
  - VUKA fee: live-tunable per corridor via the dashboard's margin sliders
    (get_margin_bps), default 1.5%.
  - PARTNER fee: the settlement partner's cut (Thunes/bank), covers their FX
    spread (0.2-1.0%) plus settlement margin, default 1.5%. Together these
    land around the ~3% total target -- still far below the 7-9% traditional
    bank-transfer range used for the comparison line shown to the sender.

This is intentionally a static/simulated rate table -- real corridor
intelligence (live rates) lives in ai_corridors.py and isn't part of this
rebuild pass. Every quote returned here is marked simulated=True so nothing
downstream mistakes it for a live quote once a real rate feed is wired in.
"""
from . import config, ledger

# 1 KES ~= this many units of the corridor's currency. Purely an internal
# anchor for computing cross-rates -- KES has no special status to any caller
# of get_quote_for_recipient(); it's just the currency this table happened to
# be built relative to.
_RATES_FROM_KES = {
    "KENYA": 1.0,
    "UGANDA": 28.4,     # 1 KES ~= 28.4 UGX
    "TANZANIA": 18.1,   # 1 KES ~= 18.1 TZS
    "RWANDA": 10.3,     # 1 KES ~= 10.3 RWF
    "GHANA": 0.11,      # 1 KES ~= 0.11 GHS
}

DEFAULT_VUKA_MARGIN_BPS = config.DEFAULT_VUKA_MARGIN_BPS
PARTNER_FEE_BPS = config.PARTNER_FEE_BPS
BANK_COMPARISON_FRACTION = config.BANK_COMPARISON_FRACTION


def get_margin_bps(corridor: str) -> float:
    """Live VUKA margin, tunable per-corridor from the treasury dashboard's
    sliders (writes to the shared `settings` table as margin_bps:<corridor>).
    The corridor here is the DESTINATION corridor -- Vuka's margin is set per
    payout market, same as before this became multi-origin. Falls back to
    DEFAULT_VUKA_MARGIN_BPS if no override has been set."""
    override = ledger.get_setting(f"margin_bps:{corridor}")
    if override is None:
        return DEFAULT_VUKA_MARGIN_BPS
    try:
        return float(override)
    except ValueError:
        return DEFAULT_VUKA_MARGIN_BPS


def _cross_rate(origin_corridor: str, destination_corridor: str) -> float:
    """1 unit of origin_corridor's currency = this many units of
    destination_corridor's currency."""
    origin_rate = _RATES_FROM_KES.get(origin_corridor)
    dest_rate = _RATES_FROM_KES.get(destination_corridor)
    if origin_rate is None:
        raise ValueError(f"Unknown origin corridor: {origin_corridor}")
    if dest_rate is None:
        raise ValueError(f"Unknown destination corridor: {destination_corridor}")
    # origin_rate and dest_rate are both "1 KES = X <currency>" -- so
    # 1 <origin currency> = (1 / origin_rate) KES = (dest_rate / origin_rate) <dest currency>.
    return dest_rate / origin_rate


def get_quote_for_recipient(origin_corridor: str, destination_corridor: str,
                              recipient_amount: float) -> dict:
    """recipient_amount is in the DESTINATION corridor's currency (what the
    sender typed, and exactly what the recipient will receive -- this value is
    never adjusted downward for fees). Returns the ORIGIN-currency amount the
    sender's wallet must be debited to cover the conversion plus both fees,
    along with a breakdown and a bank-comparison line for the confirmation
    screen. origin_corridor == destination_corridor is rejected -- Convert &
    Transact is a cross-border product; same-country transfers aren't its job."""
    if origin_corridor == destination_corridor:
        raise ValueError("origin and destination corridors must differ")

    rate = _cross_rate(origin_corridor, destination_corridor)  # 1 origin unit = `rate` dest units

    # Origin-currency amount needed to acquire recipient_amount at the true/mid rate, before fees.
    origin_equivalent = recipient_amount / rate

    vuka_bps = get_margin_bps(destination_corridor)
    vuka_fee = round(origin_equivalent * vuka_bps / 10_000, 2)
    partner_fee = round(origin_equivalent * PARTNER_FEE_BPS / 10_000, 2)
    total_fee = round(vuka_fee + partner_fee, 2)

    sender_debit = round(origin_equivalent + total_fee, 2)
    bank_debit = round(origin_equivalent * (1 + BANK_COMPARISON_FRACTION), 2)
    savings = round(bank_debit - sender_debit, 2)

    origin_currency = config.CORRIDORS[origin_corridor]["currency"]
    destination_currency = config.CORRIDORS[destination_corridor]["currency"]

    return {
        "origin_corridor": origin_corridor,
        "destination_corridor": destination_corridor,
        "payout_amount": recipient_amount,       # LOCKED -- exactly what was typed, never reduced
        "payout_currency": destination_currency,
        "origin_equivalent": round(origin_equivalent, 2),
        "origin_currency": origin_currency,
        "vuka_fee": vuka_fee,
        "partner_fee": partner_fee,
        "fee": total_fee,                        # combined fee, in ORIGIN currency
        "sender_debit": sender_debit,             # what actually gets collected from the sender
        "vuka_rate": rate,                        # the true/mid cross-rate used for this quote
        "bank_debit": bank_debit,
        "savings_vs_bank": savings,
        "simulated": True,
    }
