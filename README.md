# HedgeDesk — AI Treasury Analyst & Domestic Import-Substitution Desk

> **One line:** *"HedgeDesk decides how much currency risk an importer should lock in, checks whether buying Indian is cheaper, and executes it with approval, with every number sourced and the policy backtested on real data."*

---

## 1. The Service & Who Pays for It
- **Service Replaced:** The external FX treasury advisor, bank RM advisory desk, or in-house junior treasury analyst that mid-size Indian importers pay to manage USD/INR exposure on supplier payables.
- **Target Buyer:** CFO / Finance Head of mid-size Indian importers & 3PL importers (e.g., `BharatPrecision Components & 3PL Imports Pvt. Ltd.`).
- **Pricing & Unit Economics (₹):**
  - **Subscription Price:** `₹24,999 / month` (replacing `₹45,000+ / cycle` in manual FX advisory retainers).
  - **Measured Cost per Job:** Tracked live in SQLite `cost_ledger` (Crustdata credits converted to ₹). Cached replays cost `₹0.00`; cold runs cost well under `₹100`, giving `>99%` software gross margin.

## 2. Input, Finished Result & Pass Mark
- **Input:** Open purchase invoices and POs (`sample_invoices.csv` or uploaded CSV / JSON / plain-English ask) + `policy.yaml`.
- **Finished Result:**
  1. Deduped exposure book (`invoices` table, duplicates linked via `dup_of`, never deleted) with Crustdata supplier KYB & bank-change fraud checks.
  2. Quantitative hedge ratio per invoice/bucket (`policy.py`), with risk%, cost%, policy clause, and clickable evidence IDs.
  3. Verified Indian manufacturers (`MAKER` vs `TRADER` classified via `alternatives.py`), hedged landed import cost (`₹/unit`), domestic break-even threshold (`<= ₹X/unit`), `"cannot verify, RFQ drafted"`, and team-inbox RFQ drafts.
  4. Outbox instructions (`actions.py`) behind human approval (and 2-person maker-checker above `₹1 Crore`): Bank FX Forward booking, Razorpay Test Payment ID (`pay_rzptest_...`), and Team-Inbox RFQs.
  5. 18-month point-in-time historical backtest (`backtest.py`).
- **Pass Mark:** `28 / 28` automated edge cases passing in [TESTS.md](file:///c:/Users/Arjun%20Kumar/OneDrive%20-%20BIRLA%20INSTITUTE%20and%20SCIENCE/Desktop/CONTEXTCON/TESTS.md) — zero double-hedges, zero double-payments across crashes (`Case 23*`, `Case 24*`), zero invented prices or rates (`cannot verify` on misses).

## 3. Data Sources
- **Crustdata API (`https://api.crustdata.com`, `x-api-version: 2025-11-01`):**
  - `/company/identify`: Entity resolution, lookalike detection, canonical `crustdata_company_id`.
  - `/company/enrich`: Active status, founding year, 6-month headcount timeseries risk flag (`Case 13`).
  - `/company/search`: Indian manufacturer discovery (`locations.country in ["India"]`, `headcount.total => 40`).
  - `/person/search`: Bank-detail change sender verification (`Case 14*`).
- **Official FX Reference Data:**
  - **ECB / Frankfurter Daily USD/INR API** (`https://api.frankfurter.app`): Live spot reference + real daily historical series (`2025-04-30` to `2026-11-20`, ₹84.53 → ₹92.68).
  - **FBIL / RBI Interbank Forward Premia Curve:** 1M / 3M / 6M annualized forward premia.
- **Razorpay Test Mode (`actions.py`):** Idempotent receipt-checked payout order state machine (`Case 24*`).

## 4. How It's Built
See [ARCHITECTURE.md](file:///c:/Users/Arjun%20Kumar/OneDrive - BIRLA INSTITUTE OF TECHNOLOGY and SCIENCE/Desktop/CONTEXTCON/ARCHITECTURE.md) for the full Mermaid diagram and 7-table SQLite schema (`hedgedesk.db`).

## 5. Approaches Considered, What Worked & What Didn't
1. **LLM decides the hedge from the data (Rejected):** Non-reproducible—rewording an invoice changed outputs, and an LLM cannot be rigorously point-in-time backtested.
2. **Forecast USD/INR and hedge selectively (Rejected):** Unreliable and violates corporate treasury mandates by turning risk management into FX speculation.
3. **Rule-based layered policy with Risk vs. Cost inside board ranges (Chosen):** Pure deterministic functions (`policy.py`: `var_95`, `fwd_cost`, `hedge_ratio`, `decide`). Reproducible, auditable, maker-checker enforced, and backtestable with a strict point-in-time `assert`.
4. **Options and zero-cost collars (Future Scope):** Deferred because verified live implied-volatility option surfaces are not consistently exposed on free public endpoints.

### Backtest Results & Explicit Limits (`2025-04-30` to `2026-11-20`)
> **Measured Headline:** *HedgeDesk cut the worst-month overrun by 58.7% vs never hedging, at 44.2 paise per dollar average cost, and was cheaper than always hedging 100% by 10.7 paise per dollar.*

**Disclosed Limits:** Synthetic monthly USD 100,000 payables (30/60/90-day rotating tenors), single currency pair (USD/INR), 18-month rupee depreciation regime, assumed 18 bps bank spread, and user-supplied duty/freight percentages.

## 6. How to Run From a Fresh Clone

```bash
pip install -r requirements.txt
cp .env.example .env   # add your CRUSTDATA_API_KEY
```

### Run All 28 Automated Edge Cases (Generates `TESTS.md`)
```bash
python test_28_cases.py
```

### Launch the Web Terminal
```bash
uvicorn app:app --host 127.0.0.1 --port 8000
```
Open `http://127.0.0.1:8000`, click **"Run Demo Story (sample_invoices.csv)"**, and follow the 2-minute script in the Context Doc.

### Run CLI & Demonstrate Real `Ctrl+C` Kill-on-Stage
```bash
python run.py
# Press Ctrl+C mid-run to kill the process, then run again:
python run.py
```
On restart, both `run.py` and the Web Terminal (`GET /api/latest-run`) automatically resume from the last committed SQLite `steps` checkpoint with zero duplicate API calls or duplicate payouts.

### Reset Demo Runs (Preserves Crustdata Cache)
```bash
python reset_demo.py
```

## 7. What Is Left Out & Future Scope
- **Left out for 7-hour scope:** FX options/collars, EUR/CNY active hedging (flagged out-of-scope today), and live bank FIX API connectivity.
- **Future Scope:** Automated Tally/Zoho Books ERP sync, live RBI/FBIL MIFOR webhook ingestion, RazorpayX production payouts, and export receivable netting.
