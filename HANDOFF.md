# HedgeDesk — Team Handoff Document

**Project:** HedgeDesk (ContextCon 05 · BITS Pilani — *Services, Replaced*)  
**One-liner:** *"HedgeDesk decides how much currency risk an importer should lock in, checks whether buying Indian is cheaper, and executes it with approval, with every number sourced and the policy backtested on real data."*  
**Status:** Fully built and verified (`28/28` edge cases passing in `test_28_cases.py`; FastAPI web terminal + CLI tested end-to-end).

---

## 1. File-to-Responsibility Map

### A. Active Core Modules (Root — Aligned 1:1 with `HedgeDesk Context Doc`)

| File | Role / What It Does | Key Functions / Endpoints |
| :--- | :--- | :--- |
| `policy.yaml` | Editable board-approved FX hedging policy & hard rules (tenor bands for confirmed vs. forecast, decision slope `k=3.0`, ₹1 Cr dual-approval threshold, 1-business-day staleness gate, sanity bounds, bank spread `18 bps`, duty/freight defaults, and credit cost ceiling). | Loaded dynamically by `sources.load_policy_yaml()` and live-editable via `/api/policy` for the 5-minute stage live fix. |
| `sources.py` | **Single gateway to all external APIs & SQLite (`hedgedesk.db`).** Initializes the 7-table SQLite WAL schema (`cache`, `evidence`, `cost_ledger`, `invoices`, `decisions`, `outbox`, `steps`), caches Crustdata calls by SHA-256 request hash, handles `429`/`5xx` backoff & `402` budget stops, fetches the USD/INR spot from ECB/Frankfurter, and models the 1M/3M/6M premia (the evidence ledger labels them as modelled). Sanity bounds apply; the staleness gate is tested but not yet enforced on the live path. | `crust()`, `fetch_fx()`, `evidence()`, `add_evidence()`, `save_step()`, `get_step()`, `get_run_cost_summary()` |
| `book.py` | **Invoice ingestion, deduplication & supplier KYB.** Parses CSV/JSON/messy text (normalizing `"USD 1.2 lakh"` & OCR typos), resolves foreign suppliers via Crustdata `/company/identify` + `/company/enrich` (detecting lookalikes & headcount-drop risk flags), checks bank-detail change emails via Crustdata `/person/search`, links exact/fuzzy duplicates via `dup_of`, and buckets exposures. | `load_invoices()`, `parse_amount_messy()`, `resolve_supplier()`, `verify_bank_change_email()`, `dedupe()`, `build_book()` |
| `policy.py` | **Pure, zero-I/O deterministic hedging math.** Computes 95% parametric FX VaR, forward strike & carry cost, linear risk-vs-cost hedge ratio inside policy bounds, forward roll swap costs on moved due dates, and lifecycle holds. Enforces a strict point-in-time `assert` so backtests cannot leak future rates. | `var_95()`, `fwd_cost()`, `hedge_ratio()`, `get_tenor_bounds()`, `decide()` |
| `alternatives.py` | **Domestic import-substitution engine.** Queries Crustdata `/company/search` for Indian manufacturers (`headcount >= 40`), classifies `MAKER` vs. excluded `TRADER`, computes hedged landed import unit cost (`₹`) and domestic break-even price (`<= ₹X/unit`), labels private prices `"cannot verify, RFQ drafted"`, and drafts team-inbox RFQ emails. | `find_makers()`, `classify_maker_vs_trader()`, `landed_cost()`, `break_even()` |
| `actions.py` | **Human approval queue & exactly-once outbox state machine** (`drafted` → `approved` → `sent`/`paid` or `held`). Computes deterministic SHA-256 idempotency keys, enforces 2-person maker-checker above `dual_approval_threshold_inr`, and reconciles Razorpay test-mode receipt IDs if killed after payout call before DB commit (`Case 24*`). | `propose()`, `approve()`, `execute()`, `get_outbox()`, `make_forward_idem_key()`, `make_payment_idem_key()` |
| `backtest.py` | **18-month point-in-time historical replay (`2025-04-30` to `2026-11-20`).** Replays monthly `$100,000` synthetic payables (rotating 30/60/90d tenors) through `policy.decide()` on real ECB/Frankfurter daily USD/INR data against Never Hedge, Always 100%, and Static 50%. | `replay()`, `metrics()` |
| `engine.py` | **Crash-resilient 5-step pipeline orchestrator.** Executes `1_book` → `2_market` → `3_policy` → `4_alternatives` → `5_outbox`, persisting each completed stage into SQLite `steps` so a real `Ctrl+C` kill resumes cleanly from the last checkpoint with zero duplicate calls. | `run_pipeline()` |
| `app.py` | **FastAPI web server & REST API.** Serves the web dashboard, runs deterministic `run_id` batches, auto-resumes the latest interrupted run on page load (`GET /api/latest-run`), accepts CSV uploads or plain-English asks, exposes live `policy.yaml` patching, and serves backtest/outbox/health endpoints. | `GET /`, `GET /api/health`, `GET /api/latest-run`, `POST /api/run`, `POST /api/upload-csv`, `POST /api/policy`, `POST /api/outbox/{key}/approve-and-execute`, `GET /api/backtest` |
| `run.py` | **Terminal CLI entrypoint.** Runs `sample_invoices.csv` (or any passed CSV path) using a deterministic `run_id` so pressing `Ctrl+C` mid-run and re-running `python run.py` demonstrates raw terminal crash recovery. | `main()` |

### B. Frontend, Data Assets & Environment Files

| File | Role / What It Does |
| :--- | :--- |
| `static/index.html` | Single-page institutional dark-mode Treasury & Sourcing Web Terminal (6 tabs: Exposure Book, Hedge Decisions, Domestic Alternatives, Approvals & Outbox, 18M Backtest SVG chart, Evidence & Cost Ledger; CSV upload; plain-English input; live `policy.yaml` editor; auto-resume on reload). |
| `sample_invoices.csv` | Flagship 9-row demo dataset covering the `$200,000` SKF bearings story (`SKF GmbH` vs `SKF Gmbh.` fuzzy duplicate, lookalike supplier, `"USD 1.2 lakh"` forecast PO, Carrier `+46d` moved due date roll, out-of-scope EUR, overdue invoice, missing due date, and Zebra bank-detail change email). |
| `hedgedesk/usd_inr_ecb_history.json` | **Required data file (keep!).** Verified local snapshot of 401 real daily official ECB/Frankfurter USD/INR rates (`2025-04-30` to `2026-11-20`, `₹84.53` → `₹92.68`) used by `sources.py` and `backtest.py` so volatility and backtesting work even offline. |
| `requirements.txt` | Python package dependencies (`fastapi`, `uvicorn`, `httpx`, `pydantic`, `python-dotenv`, `PyYAML`, `python-multipart`). |
| `.env` | Local secrets file (**gitignored — NEVER commit**). Holds `CRUSTDATA_API_KEY`. |
| `.env.example` | Safe template showing required environment variables for fresh clones. |
| `.gitignore` | Excludes `.env`, `*.db`, `*.db-journal`, `__pycache__/`, logs, and cache files from Git. |
| `hedgedesk.db` | Live SQLite WAL database created at runtime containing the 7 tables (`cache`, `evidence`, `cost_ledger`, `invoices`, `decisions`, `outbox`, `steps`). Excluded from Git. |

### C. Testing, Utilities & Documentation

| File | Role / What It Does |
| :--- | :--- |
| `test_28_cases.py` | Automated verification suite executing all 28 edge cases from Context Doc Pages 11–14 against live/cached Crustdata + FX feeds, printing pass/fail status and auto-generating `TESTS.md`. |
| `TESTS.md` | Generated verification report recording `28/28` PASS results and the measured 18-month backtest headline. |
| `reset_demo.py` | Clears run tables (`invoices`, `decisions`, `outbox`, `steps`, `evidence`, `cost_ledger`) while **preserving `cache`** so repeated live demos run fast and spend zero extra Crustdata credits. |
| `README.md` | Judge-facing project documentation (service replaced, buyer & ₹ pricing, inputs/outputs, data sources, approaches considered, backtest limits, setup & run instructions). |
| `ARCHITECTURE.md` | Mermaid architecture diagram + module map showing where data calls, retries, checkpoints, approvals, and cost/evidence ledgers sit. |
| `HANDOFF.md` | This team handoff document. |

### D. Reference PDFs & Legacy v1 Prototype Files (Do Not Wire Into Production)

| File | Role / What It Does |
| :--- | :--- |
| `1 · Builder Handbook · ContextCon 05 BITS Pilani.pdf` | Official ContextCon 05 event handbook, schedule, rules, and Crustdata API quickstart. |
| `ContextCon05_BITS_Problem_Statement.pdf` | Official problem statement defining the finals format (kill on stage, 5-min live fix, 4 judges: 2 CS + Ethics + Finance). |
| `HedgeDesk Context Doc (ContextCon 05).pdf` | Team specification doc defining the 7-module design, math formulas, 28 edge cases, and 2-minute demo script. |
| `hedgedesk/*.py` (`__init__.py`, `config.py`, `db.py`, `crustdata_client.py`, `fx_market.py`, `policy_engine.py`, `backtester.py`, `test_cases.py`, `cli.py`) & `hedgedesk_state.db` | **Superseded v1 prototype** created before `HedgeDesk Context Doc (ContextCon 05).pdf` was added. Not imported by `app.py` or `run.py`. **Keep `hedgedesk/usd_inr_ecb_history.json`**, and optionally remove the other v1 `.py` files and `hedgedesk_state.db` before final GitHub push for a spotless repo. |

---

## 2. Quick-Start Commands (Windows PowerShell)

```powershell
cd "C:\Users\Arjun Kumar\OneDrive - BIRLA INSTITUTE OF TECHNOLOGY and SCIENCE\Desktop\CONTEXTCON"
$py = "C:\Users\Arjun Kumar\.anaconda-desktop\micromamba\envs\assistant\python.exe"

# 1. Verify all 28 edge cases (regenerates TESTS.md)
& $py test_28_cases.py

# 2. Reset demo run tables (keeps Crustdata API cache warm)
& $py reset_demo.py

# 3. Launch the Web Terminal at http://127.0.0.1:8000
& $py -m uvicorn app:app --host 127.0.0.1 --port 8000

# 4. Or run via CLI (Ctrl+C mid-run, then run again to show checkpoint resume)
& $py run.py
```

---

## 3. Verified Headline Numbers (Ready for Demo & Slides)

- **Live USD/INR Reference Spot:** `₹92.68` (ECB/Frankfurter published business day `2026-11-20`, 60-day realized vol computed from real daily series).
- **Flagship Demo Book (`sample_invoices.csv`, 9 rows):**
  - Total Hedged Exposure: `$306,000.00` (across `INV-2026-401` `$200k` SKF bearings @ 75%, `PO-2026-403` `$120k` Timken forecast @ 50%, `INV-2026-404` `$90k` Carrier `+46d` forward roll @ 75%, `INV-2026-408` `$38k` Zebra @ 75% with payment held for bank-change fraud check).
  - Total 95% FX VaR: `₹12.57 Lakh` (`₹1,256,833.71`); Forward Carry Cost: `₹1.74 Lakh` (`₹174,445.20`).
  - Automatically Held/Flagged: `INV-2026-401-DUP` (`SKF Gmbh.` fuzzy duplicate), `INV-2026-402` (lookalike supplier), `INV-2026-405` (EUR out of scope), `INV-2026-406` (overdue spot), `INV-2026-407` (missing due date), `INV-2026-408` (bank-change email failed `Person Search` check).
- **18-Month Real USD/INR Backtest (`2025-04-30` to `2026-11-20`, `₹84.53` → `₹92.68`):**
  > *"HedgeDesk cut the worst-month overrun by 58.7% vs never hedging, at 44.2 paise per dollar average cost, and was cheaper than always hedging 100% by 10.7 paise per dollar."*
- **Unit Economics:** Subscription `₹24,999/month` vs. `₹45,000/cycle` manual FX advisor; marginal API cost tracked live in `cost_ledger` (`>99%` gross margin).

---

## 4. Pre-Freeze Checklist (Before 6:00 PM)

1. **Optional Cleanup Before Git Push:** Delete the unused v1 files (`hedgedesk/*.py`, `hedgedesk_state.db`) while **keeping `hedgedesk/usd_inr_ecb_history.json`** (and keeping `.env` and `hedgedesk.db` gitignored).
2. **GitHub Repo:** Initialize a public repo with commit history starting after 10:45 AM on 22 November 2026 and push all root modules, `static/index.html`, `policy.yaml`, `sample_invoices.csv`, `hedgedesk/usd_inr_ecb_history.json`, `README.md`, `ARCHITECTURE.md`, and `TESTS.md`.
3. **Architecture Diagram Link:** Export the Mermaid diagram in `ARCHITECTURE.md` via Mermaid Live (`mermaid.live`) or Excalidraw and confirm the share link opens in an incognito window.
4. **2-Minute Demo Video:** Record the 2-minute walkthrough on Loom / unlisted YouTube following Context Doc Page 20 (including live approval, manual `Ctrl+C` kill & restart showing zero duplicate payment, clickable evidence IDs, and backtest chart) and verify playback in a private window.
5. **Final Submission Form:** Submit before **6:00 PM** with every teammate's registration name, `@pilani.bits-pilani.ac.in` email, BITS ID, and GitHub username.
