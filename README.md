# HedgeDesk

HedgeDesk does the job of an FX advisor for an Indian importer. It looks at the dollar invoices a company has to pay, decides how much of each one to lock in with a bank forward, checks whether an Indian manufacturer could supply the same part for less, and queues the bank request and the payment for a person to approve.

Built at ContextCon 05, BITS Pilani (Buying and paying track, with some Supply chain).

## The job, and who pays for it today

A mid-size importer orders bearings from Germany for $200,000, payable in 60 days. If the rupee falls 2% in that time, the order costs ₹3.7 lakh more than planned. Big companies have treasury desks and board-approved hedging policies for this. Mid-size ones usually leave the exposure open, or go with whatever their bank's relationship manager suggests (and the RM earns on the forward spread).

The people who do this job today are an in-house treasury analyst, an FX advisory firm on retainer, or the bank RM. The CFO or finance head pays for it out of the finance budget.

**Our buyer:** the CFO of a mid-size Indian importer with regular USD payables. In the event, that's the client we were given, BharatPrecision Components & 3PL Imports.

**Price:** ₹24,999 a month per company. A typical advisory retainer is around ₹45,000 a cycle.

**Cost per job:** every outbound call is logged in the `cost_ledger` table with its credits and rupee cost, including failed calls, retries and cache hits. A re-run from cache costs nothing. A cold run of the 9-invoice demo book uses roughly 15–17 credits, about ₹80.

## Input

- A list of open invoices and purchase orders, as a CSV (see [`sample_invoices.csv`](sample_invoices.csv)) or a plain-English request typed into the web app, e.g. *"We owe SKF GmbH (skf.com) USD 200,000 for 400 bearings due 2027-01-21"*. The parser reads amounts written as "$200k", "200,000 USD", "₹1.2 crore" or "Rs 50 lakh", "500 bearings at $400 each", and dates like "21 Jan 2027" or "21/01/2027". It doesn't guess: an invoice is held, with the reason, if it has no amount, no supplier or no currency, if it gives two different amounts, if the amount is negative (reads as a credit note), or if the date is ambiguous (03/04/2027). If the quantity is missing, the per-unit break-even is skipped. CSV headers such as "Vendor", "Qty" or "Due Date" are mapped to our column names.
- A question about the loaded batch, typed into the same box, e.g. *"Plan hedges for this month's payables and tell me if any should be sourced locally"*. If the text states no amount it is treated as a question and answered from the current run's decisions, with evidence IDs on each line (`ask.py`). It covers the hedge plan, holds and reasons, local sourcing, approvals, duplicates and run cost; anything else gets "cannot answer" and a list of what can be asked.
- The company's hedging policy in [`policy.yaml`](policy.yaml): allowed hedge ranges by tenor, VaR confidence, the approval threshold, freshness and sanity limits, and the per-run limits (25 credits, 120 seconds). The policy is part of each run's ID, so an edited policy re-decides the same invoices instead of replaying the old result.

## Finished result

For each invoice, one of two things:

1. **A hedge decision inside the policy range.** It shows the risk, the cost, the policy clause it follows and the evidence IDs behind every number, plus a drafted forward booking and payment waiting for approval.
2. **A hold with a reason**, such as: duplicate invoice, lookalike supplier, supplier not found, bank-detail change request, missing or unreadable due date, missing amount or currency, negative amount, wrong currency, tenor over 6 months, or market data stale or missing.

For the larger invoices it also returns a list of Indian manufacturers (found on Crustdata, with traders filtered out), the landed cost of importing the part, the domestic price that would beat it, and a draft RFQ. We don't know any domestic supplier's actual price, so the output always says "cannot verify, RFQ drafted" rather than guessing one.

Payments, forward bookings and RFQs only go out after a person clicks Approve. Anything above ₹1 crore needs two approvers. A person can also reject any action that hasn't executed, with their name and a reason; the rejection is kept on the row, and re-running the same invoices doesn't bring the action back. Payments against a forecast PO are held until the supplier's invoice arrives.

## Pass mark

A run passes if:

- every invoice ends up either hedged within its policy range, or held with a stated reason;
- no invoice is hedged or paid twice, including after the process is killed and restarted;
- no rate, price or company fact appears without a source, and anything we can't source says "cannot verify".

[`test_28_cases.py`](test_28_cases.py) checks this against 28 edge cases (duplicates, lookalikes, bad and stale market data, crashes mid-payment, out-of-credit errors, moved due dates and so on). All 28 pass; the latest run is in [`TESTS.md`](TESTS.md). [`test_hardening.py`](test_hardening.py) adds 38 checks on input parsing, holds on missing or odd fields, the live staleness gate, the run time limit, the premium thresholds, rejecting an action, personal-data purge and answering questions.

## Data sources

| Source | What we use it for | Called from |
|---|---|---|
| Crustdata `/company/identify` | Match a supplier name or domain to one company record; flag weak or lookalike matches | `book.py` |
| Crustdata `/company/enrich` | Check the supplier is active; headcount trend and founding year for the risk flag | `book.py` |
| Crustdata `/person/search` | Check whether someone asking to change a supplier's bank details actually works there | `book.py` |
| Crustdata `/company/search` | Find Indian manufacturers of the same part (40+ employees) | `alternatives.py` |
| Frankfurter (ECB reference rates) `api.frankfurter.app` | Live USD/INR spot, and the daily history used for volatility and the backtest | `sources.py` |

All calls go through `sources.py`, which caches responses, retries on 429 and 5xx, stops cleanly on 402, checks the run's credit and time limits before each live call, and writes an evidence row (source, field, value, record date) for every fact. The credit check assumes the next call costs the most that endpoint has ever cost us, so one expensive call can't push a run over its limit.

**Measured cost per job.** A cold run of the 9-invoice demo with no cached replies, on 22 Nov 2026, made 25 Crustdata calls for 25.85 credits, ₹119.82 at $0.05 a credit, in 74.9 seconds. The per-call ledger is in [`evidence/cold_run_ledger_2026-11-22.csv`](evidence/cold_run_ledger_2026-11-22.csv). Enrich costs 2–4 credits and company search 2.15; identify is 0.2. Re-running from cache costs nothing. The default limit is 40 credits per run.

**Personal data.** The only personal data is on bank-detail change requests: the sender's name and email, and the Person Search reply used to check them. That reply is cut to name, role and employer before it is stored. `privacy.py` reports what is held and purges it per supplier domain or for everything (the Evidence tab has a button), leaving an audit row with counts only.

What isn't live yet:

- **Forward premia (1M/3M/6M).** These are modelled from the 2025–26 FBIL range, adjusted for current volatility. They are not fetched from FBIL. We couldn't get a parseable FBIL feed working in time. The evidence ledger labels them as modelled, and forward rates in decisions are marked indicative until the bank quotes. This is the biggest gap between the code and the plan.
- **Payments.** These go through a local stand-in that behaves like Razorpay test mode: it stores each payment against a receipt ID and looks it up before paying again. It doesn't call Razorpay's API yet.
- **Bank forward requests and RFQs.** These are drafted and marked sent, but they go nowhere outside the app (in the event, only our team inbox would be allowed).

## How it works

The architecture diagram is in [`ARCHITECTURE.md`](ARCHITECTURE.md).

A run goes through five steps. Each one saves its output to SQLite before the next one starts. If the process dies, re-running it with the same input skips every finished step, so nothing is fetched or proposed twice.

1. **Book** (`book.py`). Parse the invoices, including messy amounts like "USD 1.2 lakh". Resolve each supplier on Crustdata, check any bank-change requests, link duplicates to the original, and bucket by due month and confirmed vs forecast.
2. **Market** (`sources.py`). Get spot and 60-day volatility, and reject a spot outside the sanity bounds. If the live feed is down, fall back to the stored ECB history and say so. If the rate we end up with is more than one business day old (ECB holidays don't count), every invoice is held.
3. **Policy** (`policy.py`). For each invoice:
   - 95% VaR = 1.645 × daily vol × √(trading days) × spot × amount (confidence set in policy.yaml)
   - Forward cost = premium × days/365 + bank spread
   - Hedge more when cost is small relative to risk: `s = clip((risk/cost − 1)/(k − 1), 0, 1)`, then `ratio = low + s × (high − low)` within the policy range

   Because our forward premium is modelled, each hedge also shows the bank quote at which the decision changes: below one premium it goes to the top of the range, above another it drops to the bottom. A treasurer can compare a real bank quote against those two numbers.

   High-risk suppliers get the bottom of the range. Forecast POs get a lower range than confirmed invoices. This file does no I/O, so the same input always gives the same answer.
4. **Alternatives** (`alternatives.py`). Search for Indian makers, drop traders, then compute the landed import cost and the break-even domestic price.
5. **Outbox** (`actions.py`). Propose the forward, the payment and the RFQ, each with a key built from its contents (e.g. invoice + amount + due date). Proposing the same thing again returns the existing row. Executing something already paid returns the stored payment ID.

The web app (`app.py`, `static/index.html`) shows all of this in six tabs, with the evidence and cost ledgers, a policy editor, and toggles that simulate feed outages, stale data and out-of-credit errors.

## Approaches we considered

1. **Let an LLM decide the hedge.** Rejected. The same invoice worded differently gave different answers, and you can't backtest it honestly. There's no LLM anywhere in the decision path.
2. **Forecast the rupee and hedge on the forecast.** Rejected. FX forecasts are unreliable, and hedging on a view is speculation, which most board policies forbid.
3. **Fixed rules: hedge more when protection is cheap relative to risk, always inside board-set ranges.** This is what we built. It's reproducible, auditable, and it's how corporate treasury desks actually work.
4. **Options and collars.** Deferred. They need implied-volatility data we couldn't source and verify.

## Backtest

`backtest.py` replays the policy on real daily USD/INR rates from 30 April 2025 to 20 November 2026, a period in which the rupee fell from 84.53 to 92.68. Every month it creates a $100,000 payable (tenors rotating 30, 60 and 90 days). It compares HedgeDesk with never hedging, always hedging 50% and always hedging 100%. An assert in `policy.decide()` stops any decision from seeing a rate dated after the decision day.

| Strategy | Worst month overrun | Average overrun | Average hedging cost |
|---|---|---|---|
| Never hedge | ₹3.35 L | ₹1.27 L | 0 |
| Always 50% | ₹2.00 L | ₹0.91 L | 27.4 paise/$ |
| HedgeDesk | ₹1.38 L | ₹0.72 L | 44.2 paise/$ |
| Always 100% | ₹0.87 L | ₹0.55 L | 54.9 paise/$ |

HedgeDesk cut the worst-month overrun by 58.7% compared with never hedging, at 44.2 paise per dollar, which is 10.7 paise cheaper than hedging everything.

Hedging 100% did better on risk in this period. That's expected when the rupee only moved one way, but it's still true, so we're saying it. With current volatility, our rule ends up at the top of each policy range every month.

Limits:

- the payables are synthetic;
- it covers one currency pair and one 18-month stretch of rupee weakness;
- the bank spread (18 bps) is assumed;
- the forward premia are modelled, as noted above.

## Running it

Needs Python 3.11+ and a Crustdata API key.

```bash
pip install -r requirements.txt
cp .env.example .env        # then put your CRUSTDATA_API_KEY in .env
```

Web app, at https://hedge-desk-xi.vercel.app/:

```bash
uvicorn app:app --host 127.0.0.1 --port 8000
```

Click **Run Demo Story** to process `sample_invoices.csv`. If the server is killed and restarted, reloading the page resumes the last run from its checkpoint.

Command line:

```bash
python run.py                 # press Ctrl+C partway through, then run it again
python run.py my_invoices.csv
```

Edge-case suite (rewrites `TESTS.md`):

```bash
python test_28_cases.py
python test_hardening.py
```

Clear previous runs but keep the API cache, so demos stay fast and free:

```bash
python reset_demo.py
```

If you edit `policy.yaml`, run `reset_demo.py` before re-running the same invoices. Otherwise the run resumes from its saved policy step and the change won't show up.

## Left out, and what's next

Not built in the 7 hours: options and collars, hedging currencies other than USD (EUR and others are flagged and left unhedged), a live FBIL premium feed, real Razorpay test-mode calls, and sending anything to a bank.

Next: pull invoices from Tally or Zoho Books, net export receivables against payables, take duty rates from the tariff schedule instead of asking for them, and support more than one company with a shared market-data cache.

HedgeDesk runs the customer's own policy and drafts requests for their own bank. It isn't investment advice, and final say stays with the company's finance team.
