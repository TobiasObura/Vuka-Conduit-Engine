# Vuka — AML/KYC & Compliance Screening: Current Implementation

Prepared for external review/diagnosis. Reflects the code as of 2026-07-06.

## 1. Context / model

Vuka is a USSD conduit for cross-border remittance (Uganda, Tanzania, Rwanda, Ghana), pivoting to a white-label
Remittance-as-a-Service (RaaS) offering for pan-African banks. Vuka's architecture deliberately pushes *formal*
AML/KYC/licensing obligations onto the licensed bank/MTO partner underneath each corridor — Vuka itself is not a
licensed money transmitter. However, a bank partner's own due-diligence process will still ask whether the
*originating* app has any risk posture at all; "none" was treated as a deal-breaker for partnership conversations.
This document describes exactly what has been built to give Vuka a real (if intentionally scoped) risk posture,
and where the honest gaps are.

## 2. What runs on every transaction

`services/ussd-backend/app/compliance.py` — `screen_transaction()` is called for every **Convert & Transact**
transaction (cross-border corridor) and every **Merchant Payment** transaction, before OTP/collection proceeds.
It runs up to four independent rules and persists every rule that fires as a row in the `compliance_flags` table
(full audit trail, nothing is discarded):

| Rule | What it checks | Severity | Real or simulated? |
|---|---|---|---|
| **Velocity** | Sender has ≥3 transactions (configurable) in a rolling 60-minute window (configurable) — classic structuring/smurfing signal | Medium (review, does not block) | Real — computed from Vuka's own ledger |
| **Large amount** | Transaction ≥80% (configurable) of that corridor's soft cap | Medium (review, does not block) | Real — computed from Vuka's own config |
| **Local watchlist** | Sender or recipient phone number matches a small human-curated list (`VUKA_LOCAL_WATCHLIST` env var, comma-separated phone numbers) | High (blocks transaction) | **Explicitly simulated.** This is NOT a real sanctions/PEP list (no OFAC/UN/EU list data). `compliance.py` hardcodes `SANCTIONS_SCREENING_SIMULATED = True` and every flag note says so. It is a stand-in that lets Vuka demonstrate *the mechanism* (screen → flag → hold → MLRO review) without paying for a real provider yet. |
| **Bank-partner delegated screening** (new) | Asks the destination corridor's own bank/MTO partner to run *its own* sanctions/PEP screening via an HMAC-signed `POST {gateway_url}/compliance-screen` call | High or Medium, depending on the partner's response | Real *if and only if* a specific bank partner has confirmed they expose this endpoint and it's been explicitly turned on for that corridor (see §3). Otherwise a safe no-op — never a fake pass. Only applies to Convert & Transact (Merchant Payment has no bank corridor to delegate to). |

A **high** severity flag sets the transaction to `compliance_hold` status and stops the flow before OTP/collection
— it is a real block, not a logged-only warning. A **medium** flag is recorded for review but does not stop the
transaction.

## 3. Bank-partner delegated screening — the new piece

Rather than requiring Vuka to sign up for and pay a separate sanctions/PEP vendor (e.g. ComplyAdvantage,
Refinitiv World-Check), `bank_adapters.screen_with_bank_partner()` lets a corridor's *own* bank partner run its
own real screening, since the bank is the licensed entity and typically already runs this for its own regulatory
obligations.

Guardrails (this is the part worth stress-testing):
- **Strictly opt-in, per corridor.** Controlled by `BANK_PARTNER_<CORRIDOR>_COMPLIANCE_ENABLED` (e.g.
  `BANK_PARTNER_UGANDA_COMPLIANCE_ENABLED=true`). Unset/false for every corridor by default.
- **A configured payout gateway does NOT imply screening is available.** The code explicitly does not infer
  "gateway URL + signing secret are set" ⇒ "they must also expose compliance screening." Both the gateway
  credentials *and* the explicit compliance flag must be present, or the call is skipped entirely.
- **Fail-closed, never fail-open.** If the corridor isn't configured, the partner hasn't opted in, or the HTTP
  call errors/times out, the function returns `{"available": False}` and the rule simply does not fire — it
  never returns a fabricated "cleared" result. Vuka's own local rules (velocity/amount/watchlist) still run
  regardless of this rule's outcome.
- **Same trust model as the existing payout leg.** Requests are HMAC-SHA256 signed with that corridor's own
  signing secret (`X-Vuka-Signature` header) and tagged with `X-Vuka-Bank-Partner`, mirroring the pattern already
  used for the live payout-dispatch and float-balance-sync calls to bank partners.
- **Expected contract**, once a partner confirms they support it:
  `POST {gateway_url}/compliance-screen` → `{"cleared": bool, "risk_level": "low"|"medium"|"high", "reference": str}`.

This was verified end-to-end with a mock bank endpoint: disabled → safe no-op; enabled + mock returns
`risk_level: "high"` → a `bank_partner_screen` flag is correctly created and the transaction is held.

## 4. Review / audit workflow (human-in-the-loop)

`services/vuka-dashboard/app.py` — Compliance & AML/KYC tab:
- **MLRO status banner.** A Money Laundering Reporting Officer is designated via `VUKA_MLRO_NAME` /
  `VUKA_MLRO_EMAIL` env vars. If either is unset, the dashboard shows an explicit warning that no MLRO is
  designated — it never shows a placeholder name as if it were real.
- **Open flags list** with Clear / Escalate actions per flag. "Escalate" is the SAR-equivalent (Suspicious
  Activity Report) outcome in this system and is kept on record, not actually filed with a regulator.
- **Full audit-trail expander** showing every flag ever raised (cleared, escalated, or still open), which rule
  fired, and the note explaining why.

## 5. Encryption at rest (adjacent control, also part of the compliance retrofit)

`services/ussd-backend/app/crypto.py` — `sender_phone` / `recipient_phone` columns in the `transactions` table
are encrypted at rest using Fernet (AES-128-CBC + HMAC), with the key derived from the existing `SESSION_SECRET`.
Because encrypted values can't be searched directly, a separate deterministic HMAC "blind index"
(`sender_phone_hash` / `recipient_phone_hash`) supports equality lookups (e.g. the velocity rule's phone-number
query) without ever decrypting rows in bulk. If `SESSION_SECRET` is unset, encryption is loudly disabled and
logged, never silently skipped. Rows written before this feature existed remain plaintext (no retroactive
backfill was performed) — decryption gracefully passes through non-Fernet values so old and new rows coexist.

## 6. Honest gaps — what this is NOT

- **No real sanctions/PEP list is integrated anywhere in the default path.** The "local watchlist" rule is a
  manually curated phone-number list, explicitly logged and labeled as simulated. There is no OFAC/UN/EU
  consolidated list, no PEP (politically exposed persons) database, and no fuzzy name-matching (screening here
  is phone-number-only, not name/DOB/nationality-based, which real sanctions screening typically requires).
- **The bank-partner delegated screening is a seam, not a live integration.** No real bank partner has been
  onboarded yet; `BANK_PARTNER_*_COMPLIANCE_ENABLED` is not set for any corridor. The `/compliance-screen`
  endpoint contract is Vuka's own proposed shape — it has not been confirmed or agreed with any actual bank.
  A bank could reasonably decline to expose this at all, in which case this rule permanently returns "not
  available" for that corridor and a real vendor becomes necessary.
- **No transaction monitoring / behavioral analytics beyond velocity + amount.** No device fingerprinting, no
  IP/geolocation checks, no cross-corridor pattern detection across a single sender, no ML-based anomaly scoring.
- **No formal KYC/identity verification step.** There is no ID document capture, no liveness/selfie check, no
  proof-of-address — USSD sessions are identified only by phone number (and, once fully live, verified only by
  OTP possession of that SIM). Identity verification is presumed to be handled upstream by the bank partner /
  telco SIM registration, not by Vuka.
- **No SAR filing integration.** "Escalate" in the dashboard is an internal status change, not a submission to a
  Financial Reporting Centre or any regulator's system.
- **No case-management / ticketing system** — clearing/escalating a flag is a two-button action on a Streamlit
  page, with no assignment, SLA tracking, or notification to a compliance team.
- **No independent rules-tuning validation.** Velocity thresholds (3 tx / 60 min) and the large-amount fraction
  (80% of soft cap) are business defaults set without regulatory or actuarial backing — they are tunable via env
  vars but not derived from any real risk model.

## 7. Where to look in code

- `services/ussd-backend/app/compliance.py` — rule engine
- `services/ussd-backend/app/bank_adapters.py` — `screen_with_bank_partner()`, payout dispatch, float-balance sync
- `services/ussd-backend/app/config.py` — `BANK_PARTNERS` registry, `_build_bank_partners()`
- `services/ussd-backend/app/crypto.py` — encryption at rest
- `services/ussd-backend/app/ledger.py` — `compliance_flags` table, `get_open_compliance_flags()`
- `services/vuka-dashboard/app.py` — Compliance & AML/KYC dashboard tab
- `replit.md` — "AML/KYC compliance" and "Bank-partner delegated compliance screening" sections (architecture
  rationale) and the "Gotchas" section (operational caveats/footguns)
