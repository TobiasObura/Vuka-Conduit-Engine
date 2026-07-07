# Vuka Treasury & USSD Fintech Platform

Vuka is a cross-border USSD remittance platform: users on a feature phone dial a USSD code
to convert and send money to Uganda, Tanzania, Rwanda, or Ghana, with a Streamlit-based
treasury dashboard for the business to monitor margins, corridor volumes, and multi-currency
float pools.

→ For architecture decisions, operational gotchas, and the Ecobank × Onafriq deep dive,
  see **`ARCHITECTURE.md`**.

---

## Run & Operate

- The **Node/TypeScript** side of this monorepo (`artifacts/api-server`, `artifacts/mockup-sandbox`)
  is currently unused boilerplate — Vuka's real logic lives in the Python services below.
- **Python services** run as plain Replit workflows (not pnpm-managed):
  - `Vuka USSD Backend` — `cd services/ussd-backend && python3 main.py` (Flask, port 8000)
  - `Vuka Treasury Dashboard` — `cd services/vuka-dashboard && streamlit run app.py --server.port 8008 --server.headless true --server.address 0.0.0.0`
- **Proxy exposure**: two thin `react-vite` artifacts (`artifacts/ussd-backend`,
  `artifacts/vuka-dashboard`) exist solely so the shared Replit proxy can route
  external/HTTPS traffic to the Python services. Their Vite dev servers only
  `server.proxy`-forward `/ussd/*` → `localhost:8000` and `/dashboard/*` → `localhost:8008`.
  Do not add real frontend code to these — they are routing shims, not apps. Both the Python
  workflow AND its matching proxy-shim artifact workflow must be running for the service to
  be reachable via `localhost:80` / the public domain.
- Required env for live (non-simulated) mode (none are currently set — app runs fully in
  simulation mode):
  - **Payout — Thunes DGN (primary, all corridors):**
    - `THUNES_API_KEY`, `THUNES_API_SECRET`, `THUNES_PAYER_ID` — issued by Thunes Portal
      after compliance review. Activates `ThunesPayoutAdapter` for all corridors.
      `THUNES_ENVIRONMENT` defaults to `pre-production`.
    - `THUNES_SERVICE_ID_UGANDA`, `_TANZANIA`, `_RWANDA`, `_GHANA` — wallet-provider service
      IDs assigned by Thunes per corridor during onboarding (e.g. MTN MoMo Uganda).
    - `THUNES_CALLBACK_URL` — auto-derives from `REPLIT_DOMAINS` if unset.
    - `THUNES_WEBHOOK_SECRET` — optional HMAC secret for verifying Thunes callbacks.
  - **Collection — Onafriq hub (all non-Safaricom senders):**
    - `ONAFRIQ_API_KEY` — activates real collections for MTN, Airtel, Vodacom, Orange, Tigo,
      Moov, Wave, etc. in one step. `ONAFRIQ_BASE_URL` defaults to
      `https://mfsafrica.beyonicpartners.com`; `ONAFRIQ_CALLBACK_URL` auto-derives.
  - **Collection — Safaricom Daraja (Safaricom Kenya senders only, no aggregator):**
    - `DARAJA_CONSUMER_KEY`, `DARAJA_CONSUMER_SECRET`, `DARAJA_SHORTCODE`, `DARAJA_PASSKEY` —
      Daraja STK Push. `DARAJA_ENV` defaults to `sandbox`; `DARAJA_CALLBACK_URL` auto-derives.
  - **Funded settlement (optional Daraja B2B leg):**
    - `BANK_SETTLEMENT_SHORTCODE`, `DARAJA_INITIATOR_NAME`, `DARAJA_SECURITY_CREDENTIAL` —
      Daraja B2B Express. Discuss the Thunes pre-funding mechanism with Thunes during
      onboarding before configuring this.
  - **Payout fallback — per-corridor plugin gateway (optional, only if Thunes not configured):**
    - `BANK_PARTNER_<CORRIDOR>_GATEWAY_URL` / `_SIGNING_SECRET` / `_NAME` — per-corridor
      bank plugin gateway. Not needed when Thunes is configured.
  - `AT_USERNAME`, `AT_API_KEY` — Africa's Talking SMS (OTP delivery).
  - `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `TREASURY_WHATSAPP_NUMBER` — WhatsApp
    Business API low-float alerts.
  - `VUKA_MLRO_NAME`, `VUKA_MLRO_EMAIL` — designates the Money Laundering Reporting Officer
    shown in the Compliance tab (unset by default; `config.MLRO_DESIGNATED` is `False` until
    both are set — no placeholder name is ever shown as if it were real).
  - `VUKA_LOCAL_WATCHLIST` — optional comma-separated phone numbers for local watchlist
    screening; empty by default.
  - `BANK_PARTNER_<CORRIDOR>_COMPLIANCE_ENABLED` — per-corridor opt-in (`true`/`1`/`yes`) to
    delegate sanctions/PEP screening to that corridor's bank partner; unset for all corridors
    by default.

---

## Stack

- Python 3.11, Flask (USSD backend), Streamlit + pandas + plotly (treasury dashboard),
  `cryptography` (Fernet encryption at rest)
- **Gemini AI** via Replit AI Integrations proxy (`AI_INTEGRATIONS_GEMINI_BASE_URL` +
  `AI_INTEGRATIONS_GEMINI_API_KEY`) — four modules: 6-language USSD translation, async
  fraud/risk scoring, 35-country corridor intelligence, NL treasury assistant. All four fall
  back to clearly-logged simulation mode when the env vars are absent.
- SQLite ledger at `services/ussd-backend/data/vuka.db`
  (transactions, float_pools, settings, otp_log, compliance_flags)
- pnpm/TypeScript monorepo scaffolding present for other artifacts, not used by Vuka's core

---

## Where things live

- `services/ussd-backend/main.py` — Flask entrypoint:
  - `POST /ussd/` — Africa's Talking USSD webhook
  - `POST /ussd/webhook/bank-callback` — async bank success/failure + auto-refund
  - `POST /ussd/webhook/mpesa-callback` — async Daraja STK Push result → confirms collection,
    dispatches payout (or B2B settlement first if configured)
  - `POST /ussd/webhook/onafriq-callback` — async Onafriq collection result → same
    state-machine pattern as Daraja
  - `POST /ussd/webhook/momo-callback` — async MTN MoMo direct callback (fallback path)
  - `GET /ussd/healthz`
- `services/ussd-backend/app/ussd_router.py` — depth-based USSD state machine; `complete_transfer()`
  holds float-reserve + payout-dispatch logic used by both sync (simulated) and async
  (real provider callback) paths
- `services/ussd-backend/app/collection_adapters.py` — carrier-level collection routing:
  Safaricom → Daraja direct; all others → Onafriq hub (or per-provider simulation stubs)
- `services/ussd-backend/app/network_profiles.py` — MCC-MNC → country/currency/carrier/limit
  lookup for all KE/UG/TZ/RW/GH carriers plus Ecobank expansion markets (XOF belt, Cameroon
  XAF, Nigeria NGN)
- `services/ussd-backend/app/mpesa_collection.py` — Safaricom Daraja OAuth + STK Push
  (`trigger_collection()`), B2B Express funded settlement (`fund_settlement()`)
- `services/ussd-backend/app/bank_adapters.py` — Adapter+Factory per corridor bank, HMAC-SHA256
  signed payloads
- `services/ussd-backend/app/ledger.py` — SQLite ledger. All reads/writes of
  `sender_phone`/`recipient_phone` go through `crypto.py` — never raw SQL. Key fields per
  transaction: `collection_status`, `collection_reference`, `collection_simulated`,
  `mpesa_receipt`, `recipient_name`. `has_open_high_severity_flag(tx_id)` guards payout
  dispatch.
- `services/ussd-backend/app/ai_gemini.py` — shared Gemini client; sets
  `SIMULATION_MODE_GEMINI`; proxy URL is
  `{AI_INTEGRATIONS_GEMINI_BASE_URL}/models/{model}:generateContent?key={key}` (no
  `/v1beta/` prefix — Replit proxy strips it)
- `services/ussd-backend/app/ai_language.py` — 6-language USSD (EN/FR/SW/HA/TW/RW)
- `services/ussd-backend/app/ai_risk.py` — async Gemini fraud/risk scorer; no PII sent to Gemini
- `services/ussd-backend/app/ai_corridors.py` — 35-country Ecobank corridor resolver; core
  corridors static, expansion corridors via Gemini; cached in-process
- `services/ussd-backend/app/ai_treasury.py` — NL treasury assistant; called from dashboard
  AI tab with a privacy-safe ledger summary (no phone numbers, no names)
- `services/ussd-backend/app/compliance.py` — risk-based, tiered AML/KYC screening layer;
  see `ARCHITECTURE.md`
- `services/ussd-backend/app/crypto.py` — Fernet encryption + HMAC blind-index for phone
  columns; see `ARCHITECTURE.md`
- `services/ussd-backend/app/config.py` — env-var simulation-mode flags; all simulation
  modes listed here
- `services/vuka-dashboard/app.py` / `db.py` — Streamlit dashboard (read-only ledger access,
  margin-tuning sliders, float pool monitoring, WhatsApp alert hooks, Compliance & AML/KYC
  tab with MLRO status / Clear / Escalate / SAR export)
- `services/vuka-dashboard/crypto_utils.py` — decrypt-only mirror of `crypto.py` for the
  dashboard
- `artifacts/ussd-backend/vite.config.ts`, `artifacts/vuka-dashboard/vite.config.ts` —
  proxy shims only

---

## Product

- **USSD flow**: language select (EN/FR) → main menu (Convert & Transact / Speed Dial /
  Market Rates / Merchant Payment) → corridor select → recipient phone + name → amount →
  confirmation with fee comparison vs. banks → OTP via SMS → fee collection (Daraja STK
  Push for Safaricom senders; Onafriq hub for all other carriers; simulated when unconfigured)
- **Treasury dashboard**: business stats, corridor volumes, live margin-tuning sliders,
  multi-currency float pool monitoring with WhatsApp alerts on low balance, Confirmed vs.
  Simulated/Pending revenue split, Compliance & AML/KYC tab

---

## User preferences

_None recorded yet._

---

## Pointers

- Architecture decisions, gotchas, and the Ecobank × Onafriq deep dive → `ARCHITECTURE.md`
- Workspace structure (pnpm monorepo conventions) → `pnpm-workspace` skill (does not apply
  to the Python services in `services/`)
