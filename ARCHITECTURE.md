# Vuka — Architecture Reference

This document is the authoritative technical and operational reference for Vuka's
architecture decisions, gotchas, and deep-dive analysis of key partner integrations.
It is maintained alongside the codebase and updated whenever a non-obvious decision is made.

→ For a project overview, run/operate instructions, and stack summary see `replit.md`.

---

## Architecture decisions

### Python services in a pnpm monorepo
Python is not a natively supported artifact runtime in this pnpm monorepo, so Vuka's real
services live in plain `services/` directories run via plain (non-pnpm) Replit workflows,
not `createArtifact()`. To still get shared-proxy/HTTPS reachability (needed for Africa's
Talking and bank webhooks to call in from outside), two minimal `react-vite` artifacts were
created purely as Vite `server.proxy` shims forwarding to the Python ports — see
`replit.md` → Run & Operate. This is a workaround, not a real frontend.

All external integrations (Africa's Talking SMS, WhatsApp Business API, bank plugin
gateway) are wired for real credentials via env vars but explicitly fall back to logged
`[SIMULATION]` behavior when absent — never a silent fake success.

---

### Payout architecture — Thunes DGN as the primary gateway

Vuka's payout layer has two tiers (see `bank_adapters.py`):

**Tier 1 — ThunesPayoutAdapter (primary, all corridors)**
When `THUNES_API_KEY` + `THUNES_API_SECRET` + `THUNES_PAYER_ID` are set,
`BankAdapterFactory.get_adapter()` returns a single `ThunesPayoutAdapter` for every corridor.
It calls the Thunes Money Transfer API v2 directly — no intermediary bank plugin gateway.
One Thunes account covers Uganda (UGX/MTN MoMo), Tanzania (TZS/Vodacom M-Pesa),
Rwanda (RWF/MTN MoMo), Ghana (GHS/MTN MoMo), and any of Thunes' 130-country DGN expansion
corridors. `THUNES_SERVICE_ID_<CORRIDOR>` env vars (assigned by Thunes during onboarding)
identify the exact wallet-provider service per destination; until set, that corridor
simulates.

Payout is async: Thunes returns `CREATED` immediately; status updates arrive via
`POST /ussd/webhook/thunes-callback` as the transfer progresses to `EXECUTED` (success)
or `DECLINED`/`CANCELLED`/`ERROR` (failure). Float is restored and the sender is notified
on terminal failures.

The recipient always receives the exact `receive_amount` quoted at the USSD confirmation
screen (`destination.amount` is pinned). Thunes deducts the corresponding source amount
from Vuka's prefunded Thunes wallet at their live FX rate; any spread between Vuka's
internally-quoted rate and Thunes' execution rate is absorbed within the 0.5% gateway cost
allowance (`config.BANK_SETTLEMENT_BPS`). Thunes' published FX spread is 0.2%–1.0%.

**Ecobank delivery via Thunes — no bilateral Ecobank MOU required**
Ecobank Group signed as a Thunes DGN receiver in October 2025 and is live in Togo first,
expanding across 32 Ecobank countries progressively. Ecobank account holders in Vuka's
corridors are reachable as Thunes endpoints without Vuka holding separate Ecobank corporate
accounts or negotiating a bilateral Ecobank MOU.

**Tier 2 — per-corridor plugin gateway BankAdapter (fallback)**
`BANK_PARTNER_<CORRIDOR>_GATEWAY_URL` / `_SIGNING_SECRET` / `_NAME` wires a specific
corridor to an independent bank/MTO plugin gateway operating outside Vuka's trust boundary.
Used only when Thunes is not configured. Falls back to `SIMULATION` mode (logged loudly,
no real dispatch) when neither tier is active. Inbound callbacks identify themselves via
`X-Vuka-Bank-Partner` header so the correct per-corridor HMAC secret is used.

---

### Fee collection (Vuka's own margin)
`fee_amount` used to be only a calculated ledger field — no real collection ever happened,
so Vuka's margin was never retained as cash. The collection leg is now symmetric to the
payout leg: `mpesa_collection.trigger_collection()` initiates a Safaricom Daraja STK Push
for the sender's GROSS `send_amount` into a Vuka-controlled paybill/till; the destination
bank adapter only ever disburses the NET amount, so the fee is the residual left in the
collection account.

Real collections are async — the USSD session ends immediately with "awaiting approval"
messaging, and `POST /ussd/webhook/mpesa-callback` (Safaricom's unsigned STK result
callback) is what actually confirms the debit and triggers `complete_transfer()` (float
reserve + payout dispatch), never the USSD request itself. Only fully-simulated collections
(no Daraja creds, or a non-KES sender since STK Push only settles KES) still settle
synchronously in the same USSD request, preserving the pre-existing demo UX.

Because Safaricom callbacks carry no signature, authenticity comes from only acting on a
`CheckoutRequestID` matching a transaction this app itself put into `pending` collection
state — an idempotent state-machine check, not a shared secret.

The Paybill/till used for real Daraja collection must be dedicated to Vuka — using an
unrelated business's existing paybill would show that business's name on the sender's STK
prompt and likely violates Safaricom merchant terms/AML compliance. Never configure
`DARAJA_*` with a third-party business's production paybill; sandbox testing with any
shortcode is fine.

---

### Collection provider abstraction (Daraja → Thunes Accept → Onafriq)
`collection_adapters.py` implements an Adapter + Factory pattern symmetric to
`bank_adapters.py` on the payout side. Routing is at the **carrier level** (network code),
not just country level:

| Tier | Network code / condition | Carrier | Route |
|---|---|---|---|
| 1 | `63902` / `99999` (sandbox) | Safaricom Kenya | Daraja direct — no aggregator margin, always cheapest |
| 2 | `THUNES_MERCHANT_ID` set | All other carriers (MTN, Airtel, Vodacom, …) | Thunes Accept API — HOST_TO_HOST USSD push |
| 3 | `ONAFRIQ_API_KEY` set | All other carriers | Onafriq hub — fallback when Thunes collection not configured |
| 4 | Neither | — | Per-currency simulation stubs with named [SIMULATION] logs |

**Thunes Accept API (HOST_TO_HOST mode)** — `POST /v1/payment/payment-orders` triggers a
USSD push or STK prompt directly on the sender's feature phone. No smartphone or internet
required on the sender's end. This is functionally identical to Onafriq's
`send_instructions: true` but through Thunes' DGN. When configured alongside
`ThunesPayoutAdapter`, Vuka operates on a **single Thunes account for both legs** — one
compliance review, one commercial contract, one API integration.

Credentials needed for Thunes collection (all from Thunes Portal):
- `THUNES_API_KEY`, `THUNES_API_SECRET` — shared with Money Transfer API
- `THUNES_MERCHANT_ID` — the Accept API merchant identifier (distinct from `THUNES_PAYER_ID`)

Collection webhook: `POST /ussd/webhook/thunes-collection-callback`. Thunes POSTs status
updates as the payment-order progresses. Terminal states: `Settled` → confirm collection
and dispatch payout; `Failed`/`Declined`/`Cancelled`/`Expired` → fail collection. The
intermediate `Authorized` state (USSD prompt sent, awaiting sender approval) is logged only.

**Onafriq hub** remains as Tier 3 fallback and is still worth configuring for corridors
where Thunes Accept is not yet commercially available. Onafriq does not have a native
reversal API — rate-expired refunds require a manual disbursement via the Onafriq dashboard.
Negotiate a reversal SLA in the Onafriq commercial contract.

Adding a new originating country to either Tier 2 or Tier 3 costs zero code changes — just
a row in `network_profiles.py` and the relevant API key already set.

---

### Funded settlement via Daraja B2B Express
Rather than maintaining a large pre-funded omnibus float at a bank partner, Vuka optionally
uses the Daraja B2B Express API to top up a payout corridor's settlement account on demand
from the Vuka paybill balance. `mpesa_collection.fund_settlement()` triggers a B2B transfer
to `BANK_SETTLEMENT_SHORTCODE` immediately after a collection is confirmed, before
`complete_transfer()` dispatches the payout.

With Thunes as the payout gateway, the settlement destination changes: rather than funding
a per-country Ecobank bank account, `BANK_SETTLEMENT_SHORTCODE` should reference Vuka's
Thunes pre-funding mechanism. In practice Thunes wallets are funded via international wire
to Thunes; the B2B leg funds a local collection account, not a country-level disbursement
account. Discuss the preferred local funding flow with Thunes during commercial onboarding.

---

### Rate lock window
The exchange rate quoted to the sender is locked for `VUKA_RATE_LOCK_SECONDS` (default 300 s)
from the time the transaction is created. `complete_transfer()` checks the lock before
dispatching the payout; if the lock has expired it sets `rate_expired` and triggers a
refund rather than settling at a stale rate. This is specifically designed to handle the
gap between async collection confirmation and payout dispatch — the window is deliberately
short so Vuka's FX exposure is bounded.

---

### AML/KYC compliance
Vuka's conduit model deliberately pushes formal AML/KYC obligations onto the licensed bank
partner underneath it, but a bank partner's due diligence will ask whether the originating
app has *any* risk posture — "none" was flagged internally as a deal-breaker.

`compliance.py` runs a risk-based, tiered set of rules on every Convert & Transact /
Merchant Payment transaction: a velocity check, amount-based tiering, local watchlist
screening (`VUKA_LOCAL_WATCHLIST`, comma-separated phone numbers — explicitly NOT a real
sanctions/PEP list), and bank-partner delegated screening. Every rule that fires persists
a row in `compliance_flags`; a `high`-severity hit blocks the transaction with a
`compliance_hold` status before OTP/collection ever runs.

An MLRO is designated via `VUKA_MLRO_NAME`/`VUKA_MLRO_EMAIL` (`config.MLRO_DESIGNATED`
reflects whether both are set); the dashboard's Compliance & AML/KYC tab is the review
workflow — MLRO status banner, open-flag list with Clear/Escalate actions, an "Escalated
flags (SAR-ready export)" panel that generates a downloadable draft SAR-style document per
escalated flag (`db.generate_sar_document()` — an export convenience, not a real FIU filing
integration), and a full audit-trail expander.

---

### Tiered risk-based due diligence (SDD/CDD/EDD)
`compliance._determine_tier()` classifies every Convert & Transact transaction as
LOW/STANDARD/ENHANCED relative to the corridor's soft cap (`VUKA_SDD_LOW_TIER_FRACTION`,
default 0.05, vs. the existing `VUKA_LARGE_AMOUNT_FRACTION_OF_CAP`, default 0.8) — mirrors
real risk-based CDD/SDD/EDD practice (FATF Recommendation 10).

- **LOW** (Simplified Due Diligence): only the watchlist check — velocity/amount-tiering
  and bank-partner screening are skipped for genuinely small transfers.
- **STANDARD**: velocity/amount/watchlist synchronously; bank-partner screening (when
  opted-in) runs in a background thread so the sender isn't held on a partner's HTTP
  round-trip. `complete_transfer()` calls `ledger.has_open_high_severity_flag(tx_id)` right
  before payout dispatch to catch a late-arriving async hold.
- **ENHANCED** (amount ≥ large-amount threshold): everything synchronously including
  bank-partner screening — large transfers are exactly the case a bank partner wants
  screened before funds move, not after.

---

### Bank-partner delegated compliance screening
Rather than requiring a paid vendor (ComplyAdvantage etc.), `bank_adapters.screen_with_bank_partner()`
can ask a corridor's own bank/MTO partner to run its own real screening. This is strictly
opt-in per corridor via `BANK_PARTNER_<CORRIDOR>_COMPLIANCE_ENABLED=true` and only turns on
once a specific partner has confirmed they expose `POST {gateway_url}/compliance-screen`.

Unconfigured/unavailable/failed calls always return `"available": False` — never a fake
"cleared". The screening payload includes the recipient's self-reported full name for
name-based matching where a partner supports it.

---

### Recipient name collection
Convert & Transact (not Merchant Payment) collects the recipient's self-reported full name
via USSD, stored in `transactions.recipient_name` (plaintext) and passed to bank-partner
screening. Explicitly labeled "self-reported, not independently verified" everywhere it
surfaces (bank-partner payload, SAR export) — USSD has no way to verify a typed name
against an ID.

---

### Encryption at rest
`sender_phone`/`recipient_phone` in the `transactions` table are Fernet-encrypted
(`crypto.py`, key derived via SHA-256 from `SESSION_SECRET`). Because encrypted values
aren't queryable, matching columns keep a deterministic HMAC "blind index"
(`sender_phone_hash`/`recipient_phone_hash`, also keyed from `SESSION_SECRET`) so lookups
work without decrypting every row. Every `ledger.py` read path decrypts via
`crypto.decrypt_transaction_row()` before returning a transaction dict — never read those
columns with raw SQL. The dashboard has its own decrypt-only mirror, `crypto_utils.py`.

Rows written before this change remain plaintext; decryption gracefully passes through
non-Fernet values rather than erroring, so old and new rows coexist safely — there was no
retroactive backfill.

---

## Gotchas

- **Both workflows required.** Both the Python workflow and its proxy-shim artifact workflow
  must be running together, or the service will be unreachable from outside despite the
  Python process being healthy.

- **Float pool seeding.** All 4 payout currencies (UGX, TZS, RWF, GHS) plus KES and XOF
  are pre-seeded in `_SEED_FLOATS` on a fresh DB. Seed sizes are proportional to 2023 World
  Bank diaspora remittance inflows (Uganda $1.4B, Tanzania $0.7B, Rwanda $0.5B, Ghana $4.6B)
  — realistic *relative* corridor sizing for a demo treasury budget, not literal balance-sheet
  size. KES and XOF pools are unaffected since neither is ever paid out.

- **Per-corridor simulation flags.** `SIMULATION_MODE_BANK` (global) is only `True` when
  *every* corridor's bank partner is unconfigured. Always check
  `config.BANK_PARTNERS[<corridor_key>]["simulation"]` for per-corridor truth once any real
  partner is onboarded. Same pattern for collections: check each transaction's own
  `collection_simulated` column, not the global `SIMULATION_MODE_COLLECTION`.

- **Daraja callback URL.** `DARAJA_CALLBACK_URL` must be a publicly reachable HTTPS URL for
  Safaricom to call back into — it auto-derives from `REPLIT_DOMAINS`, but that only works
  once the app is actually reachable externally (see the proxy-shim gotcha above).

- **STK Push is KES-only.** Real STK Push only settles KES — a sender in any other currency
  always falls back to simulated collection regardless of `DARAJA_*` config.

- **Onafriq refunds are manual.** Onafriq has no native reversal API equivalent to Daraja's
  B2C. A rate-expired refund requires manually initiating a disbursement back to the sender
  via the Onafriq account dashboard. Negotiate a reversal SLA in the commercial contract.

- **Merchant IDs per MNO.** The Onafriq "one API key" simplicity is commercial, not
  regulatory. Individual merchant IDs are still required per mobile network operator per
  country, requested through your Onafriq account manager, and may have setup costs and
  approval queues. Vuka cannot go live in all corridors simultaneously just by setting
  `ONAFRIQ_API_KEY`.

- **Phone column encryption is forward-only.** `sender_phone`/`recipient_phone` are
  encrypted only in rows written after the encryption retrofit. Decryption on read gracefully
  passes through non-Fernet values — safe, but don't write new raw-SQL queries against
  those columns outside `ledger.py`/`crypto.py`/`crypto_utils.py`.

- **Sanctions screening is simulated.** `SANCTIONS_SCREENING_SIMULATED` in `compliance.py`
  is always `True` — the local watchlist is a stand-in for a real sanctions/PEP provider.
  Never present watchlist screening as equivalent to real sanctions screening in any
  external-facing (investor/bank-partner) material.

- **Compliance hold is a hard stop.** A `high`-severity compliance flag sets the transaction
  to `compliance_hold` and stops the flow before OTP/collection — it does not silently
  continue. If a transaction seems "stuck", check `ledger.get_open_compliance_flags()` / the
  dashboard's Compliance tab before assuming a different bug.

- **`BANK_PARTNER_<CORRIDOR>_COMPLIANCE_ENABLED=true` alone does nothing** if that
  corridor's gateway/signing secret aren't also configured. Both are required. Never flip
  this on without first confirming with that specific bank partner that `/compliance-screen`
  exists on their gateway.

- **STANDARD-tier background screening latency.** For STANDARD-tier transactions,
  bank-partner screening runs in a background thread — `screen_transaction()`'s returned
  `highest_severity` will NOT reflect a hold that arrives after the USSD response was sent.
  The backstop is `complete_transfer()`'s `ledger.has_open_high_severity_flag(tx_id)` check.

- **`recipient_name` is self-reported, never verified.** Never present it (dashboard, SAR
  export, bank-partner payload) as a verified identity. Merchant Payment transactions
  leave it blank.

- **SAR export is a draft aid, not a filing.** `db.generate_sar_document()` is a
  formatting convenience. Vuka has no direct regulator filing API. Never describe it as
  "SAR filing" to a bank partner or regulator.

---

## Onafriq collection frictions — what "one API key" actually means operationally

### Friction 1 — Merchant IDs are per-MNO, not per-country

The commercial simplicity of "one Onafriq API key covers all carriers" is accurate for the
*technical* integration. The *regulatory and commercial* reality is different: Onafriq must
provision a **Merchant ID with each mobile network operator separately**, per country.

- MTN Uganda: one Merchant ID request + MNO approval queue
- Airtel Uganda: separate Merchant ID + approval queue
- Vodacom Tanzania, MTN Ghana, MTN Rwanda, … each separately

Each Merchant ID may have a setup fee, 2–8 week approval lead time, and MNO-specific
transaction limits. Plan for a phased rollout: activate the highest-volume carrier per
country first (MTN Uganda, Vodacom Tanzania, MTN Rwanda, MTN Ghana); secondary carriers
follow. When Onafriq provisions a new Merchant ID, no code changes are required — the hub
routes automatically.

### Friction 2 — Onafriq pricing is fully negotiated; validate unit economics before signing

Onafriq publishes no rate card. Pricing is bespoke per volume tier, market, and integration
scope. The Daraja direct path (Safaricom Kenya) has effectively no per-transaction API fee
to Vuka — Onafriq costs for all non-Safaricom corridors are the unknown that determines
whether Vuka's 2% fee model works. Before signing:

1. Get per-country, per-MNO quotes — not a blended rate
2. Model Vuka's margin at P50 and P90 transaction sizes per corridor
3. Negotiate a volume-based pricing ladder
4. Ask explicitly about the FX margin on cross-border collections (sometimes on top of the
   per-transaction fee)

### Friction 3 — Beyonic API vs. cross-border hub endpoint

Onafriq acquired Beyonic in 2020. The Beyonic API (`api.beyonic.com`) was built for
Uganda/Kenya domestic operations. The cross-border hub (`mfsafrica.beyonicpartners.com`) is
a separate endpoint requiring separate market activation. `ONAFRIQ_BASE_URL` correctly
defaults to the cross-border hub; during onboarding confirm which URL your account manager
provisions and override if different. Ghana, Tanzania, and West Africa XOF corridors often
require explicit activation on the cross-border endpoint.

### Friction 4 — Onafriq refunds are manual

Onafriq has no native reversal API equivalent to Daraja's B2C. A rate-expired refund
requires manually initiating a disbursement back to the sender via the Onafriq dashboard.
Negotiate a reversal SLA in the commercial contract.

---

## Thunes × Ecobank — strategic context for Vuka

### Why Thunes replaces Ecobank as Vuka's payout gateway

The original Vuka architecture assumed Ecobank as the payout gateway, based on Ecobank's
pan-African presence (34 countries) and its Onafriq partnership. Research revealed this was
the wrong frame:

1. **The Ecobank-Onafriq deal is the wrong direction.** Rapidtransfer routes mobile money
   *into* Ecobank accounts — the opposite of Vuka's payout direction (bank → recipient wallet).
   These are two different Ecobank products with separate contracts.

2. **Ecobank's API targets funded-account fintechs.** Access presupposes Vuka holds
   corporate accounts with parked liquidity in Uganda, Tanzania, Rwanda, and Ghana separately
   — each requiring local entity/KYC and central bank remittance licensing per country.

3. **Ecobank's 2025 primary partner is Thunes, not Onafriq.** Ecobank signed with Thunes in
   October 2025 to power "instant payments for the next billion users", going live in Togo
   first with progressive expansion across 32 countries. Ecobank is now a Thunes DGN receiver
   endpoint. Vuka gets Ecobank delivery *through Thunes* — without a bilateral Ecobank MOU.

### Thunes DGN coverage for Vuka's corridors

All four base corridors are live on Thunes today:

| Corridor | Thunes rail | Status |
|---|---|---|
| Uganda (UGX) | MTN MoMo Uganda | Active (Tanzania ↔ Uganda live 2026) |
| Tanzania (TZS) | Vodacom/M-Pesa Tanzania | Thunes partner since 2018 |
| Rwanda (RWF) | MTN MoMo Rwanda | Active |
| Ghana (GHS) | MTN MoMo Ghana + bank accounts | Active |

Expansion corridors (XOF belt, Nigeria, Cameroon) are all covered under Thunes' 50+
intra-Africa corridors.

### Thunes Accept API — USSD collection (confirmed live)

Research (July 2026) confirmed the Thunes Accept API v1 (`docs.thunes.com/accept/v1/`) is
a production-ready collection gateway with HOST_TO_HOST mode for all five target corridors:

| Country | Currency | Mobile networks via Thunes Accept |
|---|---|---|
| Kenya | KES | M-Pesa STK Push |
| Uganda | UGX | MTN MoMo, Airtel Money |
| Tanzania | TZS | Vodacom/M-Pesa TZ, Airtel, Tigo Pesa |
| Rwanda | RWF | MTN MoMo, Airtel Money |
| Ghana | GHS | MTN MoMo, Vodafone Cash, AirtelTigo |

**Impact**: with Thunes Accept as Tier 2, Vuka can operate **fully single-provider**
(Thunes for both collection and payout) for all non-Safaricom senders. Onafriq remains
available as Tier 3 commercial fallback.

---

### Thunes USDC settlement — convert-on-payout (confirmed live)

Thunes × Circle partnership (announced October 2024, operational) eliminates nostro
pre-funding. The model:

1. **Fund in USDC** — Vuka holds one Thunes SmartX treasury account in USDC, funded via
   Circle APIs (instant, 24/7, no T+N bank delays). One pool finances all corridors.
2. **Convert-on-payout** — USDC is held until the moment of payout dispatch; Thunes
   converts to local currency at live FX rates and settles via DGN. This bounds FX
   exposure to the interval between collection confirmation and payout dispatch (which
   is already bounded by `VUKA_RATE_LOCK_SECONDS`).
3. **Compliance** — Thunes' Fortress Compliance platform + USDC reserves satisfy AML/KYC
   requirements; no additional layer needed.

Confirmed results:
- **Ghana**: T+2 → same-day settlement (measured)
- **Kenya, Tanzania**: named USDC priority markets
- **Rwanda, Uganda**: on the USDC expansion roadmap

**Impact on float pool model**: instead of maintaining pre-funded UGX/TZS/RWF/GHS balances
across four corridor accounts, Vuka holds a single USDC balance. The per-currency float
pools in the SQLite ledger become internal accounting (margin attribution by corridor) rather
than real settlement balances. The treasury dashboard needs a USDC balance panel (future
dashboard update) alongside the per-corridor accounting views.

The code hook is `fetch_thunes_usdc_balance()` in `bank_adapters.py` — queries
`GET /v1/account/accounts/{THUNES_ACCOUNT_ID}/balances/available`. Activated by setting
`THUNES_USDC_MODE=true` and `THUNES_ACCOUNT_ID` (assigned by Thunes Portal).

---

### Thunes commercial questions — remaining open item

Questions 2 (Accept API) and 3 (USDC settlement) from the pre-commercial checklist are
**answered** — both are live. The remaining commercial question for the first Thunes call:

1. Per-transaction API commission for payout to mobile wallets in Uganda, Tanzania, Rwanda,
   and Ghana — **separately**, not a blended rate. This is in addition to the FX spread
   (Thunes FX spread: 0.2–1.0%). The 0.5% `BANK_SETTLEMENT_BPS` gateway cost allowance
   must cover the combined FX spread + API commission.

### Thunes onboarding steps

| Step | Who | Blocker if skipped |
|---|---|---|
| Thunes Portal account + compliance review | Vuka | No API credentials issued |
| API key + secret + payer ID | Thunes → Vuka | `SIMULATION_MODE_THUNES` stays True (payout) |
| Merchant ID (Accept API) | Thunes → Vuka | `SIMULATION_MODE_THUNES_COLLECTION` stays True |
| Service IDs per corridor (4×) | Thunes → Vuka | Per-corridor payout stays simulated |
| Account ID (USDC treasury) | Thunes → Vuka | `fetch_thunes_usdc_balance()` stays simulated |
| `THUNES_CALLBACK_URL` reachable externally | Vuka (proxy shim running) | Async payout confirmations never arrive |
| `THUNES_COLLECTION_CALLBACK_URL` reachable | Vuka (proxy shim running) | Async collection confirmations never arrive |
| Thunes pre-funding: USDC via Circle, or wire | Vuka ↔ Thunes | Real payouts have no liquidity |
| Dedicated Daraja paybill/till (Safaricom KE) | Vuka ↔ Safaricom | STK prompt shows wrong business name |
| Onafriq account (optional Tier 3 fallback) | Vuka ↔ Onafriq | Non-Safaricom collection falls to simulation if Thunes Accept unavailable |

The code is ready for all of these. None require code changes — they are commercial,
regulatory, and account-provisioning steps.
