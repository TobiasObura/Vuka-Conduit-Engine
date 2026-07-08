# Vuka Treasury & USSD Fintech Platform

Vuka is a cross-border USSD remittance platform: users on a feature phone dial a USSD code
to convert and send money to Uganda, Tanzania, Rwanda, or Ghana, with a Streamlit-based
treasury dashboard for the business to monitor margins, corridor volumes, and multi-currency
float pools.

→ For architecture decisions and operational gotchas, see **`ARCHITECTURE.md`**.
  Payout runs through Thunes DGN as the primary rail for all four corridors,
  with Thunes Accept as Tier 2 collection and Onafriq hub as Tier 3 fallback --
  there is no Ecobank integration in this codebase (Ecobank is reachable
  *through* Thunes DGN as of their October 2025 tie-up).

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
- Required env for live (non-simulated) mode — see `.env.example` in each service directory
  for the full authoritative list with descriptions. Summary below.

---

## Stack

- Python 3.11, Flask (USSD backend), Streamlit + pandas + plotly (treasury dashboard),
  `cryptography` (Fernet encryption at rest)
- **Gemini AI** via Replit AI Integrations proxy (`AI_INTEGRATIONS_GEMINI_BASE_URL` +
  `AI_INTEGRATIONS_GEMINI_API_KEY`) — four modules: 6-language USSD translation, async
  fraud/risk scoring, corridor intelligence, NL treasury assistant. All four fall
  back to clearly-logged simulation mode when the env vars are absent.
- SQLite ledger at `services/ussd-backend/app/data/vuka.db`
  (transactions, float_pools, settings, otp_log, compliance_flags, ai_risk_scores, ussd_sessions).
  Transactions lock their FX quote at creation time (`VUKA_RATE_LOCK_SECONDS`) and carry a
  `due_diligence_tier` (low/standard/enhanced) that governs which compliance checks run
  synchronously vs. in the background -- see `ARCHITECTURE.md`.
- pnpm/TypeScript monorepo scaffolding present for other artifacts, not used by Vuka's core

---

## User preferences

_None recorded yet._

---

## Pointers

- Architecture decisions and gotchas → `ARCHITECTURE.md`
- Go-live readiness → `GO_LIVE_CHECKLIST.md`
- All required/optional env vars → `.env.example` in each service directory
- Workspace structure (pnpm monorepo conventions) → `pnpm-workspace` skill (does not apply
  to the Python services in `services/`)
