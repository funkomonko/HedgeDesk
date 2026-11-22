# HedgeDesk study sheet

For the team only. Read this before screening and the finals. It covers what the system does, the maths, the demo data, every edge case, the numbers, and the questions we're likely to get, including the awkward ones.

Every number here comes from the code, from the `hedgedesk.db` demo run (`hd-64ed178f0e`), or from re-running `backtest.py` on 22 Nov 2026.

---

## 1. The pitch in 30 seconds

An Indian importer owes a foreign supplier dollars in 60 days. If the rupee weakens before then, the order costs more and the margin can be wiped out. Today they either leave it open and hope, pay an FX advisor, or take whatever their bank RM suggests (and the RM earns on the forward spread).

HedgeDesk does that advisor's job:

1. Reads the open invoices and POs.
2. Checks each supplier is real on Crustdata, and catches duplicates, lookalike names, and bank-detail change scams.
3. Gets the official USD/INR rate.
4. Decides what share of each invoice to lock in with a bank forward, using the company's own written policy (`policy.yaml`).
5. Finds Indian manufacturers of the same part and works out the price at which buying locally beats importing.
6. Queues the forward booking, the payment, and the RFQ for a person to approve. None of them can run twice, even after a crash.

**Track:** Buying and paying (main), plus Supply chain for the domestic sourcing part.

---

## 2. How we're scored (from the problem statement)

| Area | Judge | Pts | What they check | Our answer |
|---|---|---|---|---|
| Live run | CS | 20 | Unseen request → correct, sourced result | Plain-English box or CSV upload → full pipeline with evidence IDs |
| Robustness | CS | 15 | Kill on stage, restart, each action happens once | `steps` checkpoints + idempotency keys on the outbox |
| Design | CS | 15 | 5-min live fix + system design question | Policy lives in `policy.yaml`; core maths is pure functions |
| Ethics | Ethics | 25 | Personal data, approvals before harmful actions, explanations | Approval queue, maker-checker, deterministic explained decisions, minimal people data |
| Financial feasibility | Finance | 25 | Buyer, ₹ price, measured cost per job, would a firm pay | CFO of a mid-size importer, ₹24,999/month, cost ledger, backtest |

At screening a wrong answer hurts more than "cannot verify". When in doubt, the system should hold.

---

## 3. The pipeline, step by step

`engine.run_pipeline()` runs five steps. After each one, its output is saved to the SQLite `steps` table. On restart, a finished step is loaded from that table and **not re-run**, so no API calls are repeated and no actions are proposed twice.

```
0_request  → the raw invoice rows are saved, so /api/latest-run can resume them later
1_book     → book.build_book()       parse, check suppliers, check bank-change emails, dedupe, bucket
2_market   → sources.fetch_fx()      spot, 60-day volatility, forward premia, sanity and freshness gates
3_policy   → policy.decide()         one decision per invoice
4_alternatives → alternatives.find_makers()  Indian makers, landed cost, break-even, RFQ draft
5_outbox   → actions.propose()       forward, payment and RFQ go into the outbox as drafted or held
```

Then a person clicks **Approve & Execute** → `actions.approve()` → `actions.execute()`.

### Where everything lives

| File | One-line job | What to say if asked |
|---|---|---|
| `sources.py` | The only code that talks to the outside world. Creates the 7-table SQLite DB. | Each Crustdata call is cached by a SHA-256 hash of the request, writes a cost row and an evidence row, retries 429/5xx with backoff, stops on 402, and checks the run budget *before* each paid call. |
| `book.py` | Invoices → exposure book | Parses messy amounts ("USD 1.2 lakh"), checks suppliers with Identify + Enrich, checks bank-change senders with Person Search, and links duplicates with `dup_of`. Duplicates are linked, never deleted. |
| `policy.py` | The decision maths | No I/O at all. Same input gives the same output. Has the point-in-time `assert` used by the backtest. |
| `alternatives.py` | Buy-Indian check | Company Search for Indian firms with 40+ staff, keyword maker/trader filter, landed cost, break-even, RFQ draft. |
| `actions.py` | Outbox state machine | `drafted → approved → sent/paid`, or `held`. Deterministic idempotency keys. Two approvers needed above ₹1 Cr. |
| `engine.py` | Runs the 5 steps with checkpoints | |
| `backtest.py` | Replays the policy on real 2025–26 USD/INR data | Point-in-time; compared against never / always-100% / always-50%. |
| `app.py` + `static/index.html` | FastAPI server + one-page UI | 6 tabs, CSV upload, plain-English box, policy editor, simulation toggles. |
| `run.py` | CLI | Same pipeline; Ctrl+C then re-run to show the resume. |
| `test_28_cases.py` | Runs all 28 edge cases, rewrites `TESTS.md` | |
| `reset_demo.py` | Clears the run tables, keeps the API cache | |

### The 7 tables in `hedgedesk.db`

`cache` (API responses by request hash, plus the simulated Razorpay receipts) · `evidence` (source URL, field, value, record date, retrieved-at) · `cost_ledger` (credits and ₹ per call) · `invoices` · `decisions` · `outbox` · `steps`.

---

## 4. The maths (policy.py), with the SKF invoice worked through

Inputs on 22 Nov 2026: spot **₹92.68** (ECB, published 20 Nov), 60-day daily volatility σ = **0.00283** (about 4.5% a year), invoice **$200,000**, due 21 Jan 2027 → **60 calendar days**, confirmed.

**Step 1: the policy range.** 60 days is in the 1–3 month band, confirmed → allowed hedge between **50% and 75%** (`policy.yaml`).

**Step 2: risk (95% VaR).**
`VaR = 1.645 × σ × √h × spot × amount`, where h is trading days = round(60 × 252/365) = 41.
= 1.645 × 0.00283 × 6.40 × 92.68 × 200,000 = **₹5.53 lakh**.
As a share of the invoice: **risk% = 2.98%**. In plain words: in a bad month (1 in 20), the rupee could move enough to cost about ₹5.5 lakh extra.

**Step 3: cost of the forward.**
The premium for 60 days is interpolated between the 1M and 3M premia → 2.52% a year.
`cost% = premium × days/365 + bank spread` = 2.52% × 60/365 + 0.18% = **0.595%**.
Forward rate = 92.68 × 1.00595 = **₹93.23**.

**Step 4: the decision rule.**
`s = clip((risk%/cost% − 1) / (k − 1), 0, 1)`, with k = 3.
risk/cost = 2.98 / 0.595 ≈ 5.0 → s = (5.0 − 1)/2 = 2.0 → clipped to **1.0**.
`ratio = lo + s × (hi − lo)` = 50% + 1.0 × 25% = **75%**.

What it means: **if the hedge costs less than a third of the risk it removes (k = 3), hedge as much as the policy allows. If the hedge costs as much as the risk, hedge the minimum. In between, scale linearly.**

**Result:** book a forward for $150,000 at ₹93.23. Carry cost ₹82,710.

**Supplier risk override:** if the supplier is flagged high-risk (company founded 2024 or later, headcount down 5% or more in 6 months, fewer than 15 staff, or not active), a confirmed invoice is hedged at the *bottom* of its range and a forecast PO is not hedged at all.

**Why this isn't speculation:** the forward premium mostly reflects the interest-rate gap between INR and USD. It isn't a bet on where the rupee goes. We never forecast the rupee. We buy certainty when it's cheap relative to the risk, which is the job a treasury desk does.

> ⚠️ **Know this before a judge spots it:** with today's volatility and premia, risk/cost is about 5, well above k = 3, so **s hits 1.0 on every invoice and in every backtest month.** In practice the policy currently means "hedge at the top of the allowed range". The k-slope would only matter if volatility fell or premia rose a lot.
> **Live-fix trick:** setting **k = 6** gives s ≈ 0.80, so SKF becomes **70%**, which is the number in our original demo script. (See section 10 for the catch about re-running.)

### Domestic break-even (alternatives.py)
- Unit import price = $200,000 / 400 = $500.
- Effective FX rate = 75% × 93.23 + 25% × 92.68 = 93.09.
- Landed cost = 500 × 93.09 × (1 + 7.5% duty + 3.2% freight) = **₹51,527 per unit**.
- Break-even = landed × (1 − 1.5% GST/working-capital advantage) = **₹50,754 per unit**.
- Domestic prices are always marked "cannot verify, RFQ drafted". We never make up a price. The RFQ says "quotes at or below ₹50,754/unit qualify".

### Forward roll (Carrier, due date moved)
Due date moved from 20 Dec 2026 to 5 Feb 2027 = **+47 days** (HANDOFF.md says +46; the code and DB say 47). We roll the existing forward and record the swap cost: $67,500 × 92.68 × 2.60% × 47/365 ≈ **₹20,936**. We never book a second forward.

---

## 5. The demo book (`sample_invoices.csv`), row by row

| Row | What's in it | What HedgeDesk does | Why |
|---|---|---|---|
| INV-2026-401 SKF GmbH $200k, 60d | The main story | **Hedge 75%** ($150k @ ₹93.23); 5 Indian bearing makers found (Harsha Engineers, NRB, ARB, Austin, NIBL-Korta); break-even ₹50,754/unit; RFQ drafted | Normal case |
| INV-2026-401-DUP "SKF Gmbh." same amount and date | Fuzzy duplicate | **Held** as a duplicate of 401, not hedged | Same Crustdata company + amount + date + currency |
| INV-2026-402 "Schaeffler Global Lookalike Trading", no domain | Lookalike | **Held**, cannot verify supplier | Matched "Schaeffler" with confidence 0.60 (needs 0.82) |
| PO-2026-403 Timken "USD 1.2 lakh", forecast | Messy amount + forecast | Parsed to $120,000 (confidence 0.85), **hedge 50%** (top of forecast range 25–50%) | Forecasts get a lower range |
| INV-2026-404 Carrier $90k, due date moved +47d | Lifecycle | **Roll** the existing forward at 75%, swap cost ₹20,936 | Never two forwards for one invoice |
| INV-2026-405 Danfoss EUR 35k | Wrong currency | **Out of scope**, logged as unhedged | Policy is USD-only today |
| INV-2026-406 NSK $42k, due 10 Nov 2026 | Overdue | **Pay at spot**, no forward | A forward for a past date makes no sense |
| INV-2026-407 Honeywell $65k, no due date | Missing data | **Held**, can't work out the tenor | Asks finance for the date |
| INV-2026-408 Zebra $38k + bank-change email from rahul.verma@gmail.com | Fraud attempt | **Forward drafted at 75%, payment held** | Sender isn't on zebra.com; Person Search found no match |

**Totals:** hedged $306,000 · total VaR ₹12.57 lakh · forward carry ₹1.74 lakh.
**Outbox:** 21 items: forwards, payments and RFQs, some drafted and some held.

**Maker-checker in the demo:** the SKF forward is worth ₹1.40 Cr and the SKF payment ₹1.85 Cr. Both are above ₹1 Cr, so **Approve & Execute fails unless the second-approver field is filled in.** Show this on purpose. It's a good ethics moment.

---

## 6. The 28 edge cases: what each one tests and how

"Real" means it runs against live or cached Crustdata or the real FX data. "Simulated" means a flag forces that failure. Be ready to say which is which.

| # | Case | Result | How it's tested |
|---|---|---|---|
| 1* | Same invoice uploaded twice | Linked with `dup_of`, hedged once | Real |
| 2* | "SKF GmbH" vs "SKF Gmbh." | Same Crustdata ID → duplicate | Real (both rows have domain skf.com) |
| 3 | Two genuine invoices, same amount and date, different numbers | **Not** merged; held for a person | Real |
| 4 | Missing due date | Held, CANNOT_DETERMINE_TENOR | Real |
| 5 | EUR invoice | Out of scope, not hedged | Real |
| 6 | INR invoice from a foreign supplier | No FX exposure | Real |
| 7 | "USD 1.2 lakh" | 120,000, confidence 0.85 | Pure parsing |
| 8 | Overdue | Pay at spot | Real |
| 9 | Tenor over 6 months | Referred to the CFO | Real |
| 10 | Forecast PO | Uses the forecast range (max 50%) | Real |
| 11* | Lookalike name ("Carrier Global" with no domain) | LOOKALIKE_HOLD | Real Identify |
| 12 | Supplier not found | Hedge at bottom of range, payment held | Uses a `.invalid` domain, which the code treats as not found |
| 13 | High-risk supplier | Confirmed at bottom, forecast 0% | **Simulated** (hand-built supplier record) |
| 14* | Bank-detail change email | Held for a voice call-back | Real Person Search call (see section 11) |
| 15 | Maker vs trader | Classified | Real search (test passes if any result comes back) |
| 16 | No domestic maker | Says so; makes nothing up | **Simulated** flag |
| 17 | Domestic price | Always "cannot verify, RFQ drafted" + break-even | Real |
| 18* | Both FX feeds down | CANNOT_VERIFY, no decision | **Simulated** |
| 19 | Stale rate (more than 1 business day old) | STALE_HOLD, names the date | **Simulated** |
| 20 | Nonsense feed (negative spot, 40% premium) | Rejected by sanity bounds | **Simulated** |
| 21 | Market holiday | Uses last published day and says which | Real (ECB publishes 20 Nov for a 22 Nov run) |
| 22 | Sources disagree | Flagged, freshest official rate used | **Simulated** |
| 23* | Kill mid-run | Resumes from `steps`, no repeat calls | Real exception raised after step 3 |
| 24* | Crash after the payment call, before saving | Restart finds the existing receipt, no double pay | **Simulated** crash; Razorpay is a local mock (section 11) |
| 25 | Crustdata 429 | Backoff, then OK | **Simulated** first attempt |
| 26 | Crustdata 402 (out of credits) | Stops live calls, flags skipped checks | **Simulated** |
| 27* | Due date moved | Roll, swap cost, no second forward | Real |
| 28 | Cost limit lowered mid-run | Budget checked *before* the paid call | Real gate |

Starred cases are the ones the context doc marks as must-show.

---

## 7. Kill on stage: what to say and show

- **The pipeline:** every finished step is in `steps`. Kill it anywhere and re-run with the same input: the `run_id` is a hash of the input rows, so it's the same run, and finished steps print "Restored from SQLite `steps` checkpoint".
- **The web UI:** on page load it calls `GET /api/latest-run`, which finds the newest run and resumes it.
- **Actions:**
  - Payment key = hash(invoice_id, amount, due_date)
  - Forward key = hash(invoice, tenor, amount, policy_version)
  - RFQ key = hash(invoice, maker)

  `propose()` with a key that already exists returns the existing row. `execute()` on something already `sent` or `paid` returns the stored reference and does nothing.
- **The nasty window:** what if we crash after the payment provider accepted the payment but before we saved it? The payment is created with `receipt = idem_key`. On restart, we look it up by receipt and record the existing payment ID instead of paying again (Case 24).
- **CLI demo:** `python run.py`, press Ctrl+C partway through, run `python run.py` again.

---

## 8. Backtest: the numbers and what they mean

Real ECB USD/INR daily rates, 30 Apr 2025 → 20 Nov 2026 (₹84.53 → ₹92.68; the rupee fell 9.6%). 17 monthly windows: a $100k payable each month, tenors rotating 30/60/90 days. Budget rate = spot on the order date. Overrun = (what we actually paid per dollar − budget rate) × $100k.

| Strategy | Worst month overrun | Avg overrun | Avg hedging cost |
|---|---|---|---|
| Never hedge | ₹3.35 L | ₹1.27 L | 0 |
| Always 50% | ₹2.00 L | ₹0.91 L | 27.4 p/$ |
| **HedgeDesk** | **₹1.38 L** | **₹0.72 L** | **44.2 p/$** |
| Always 100% | ₹0.87 L | ₹0.55 L | 54.9 p/$ |

**Headline:** "HedgeDesk cut the worst-month overrun by 58.7% vs never hedging, at 44.2 paise per dollar average cost, and was cheaper than always hedging 100% by 10.7 paise per dollar."

**Say this before they ask:**
- **Always-100% beat us on risk.** Its worst month was smaller (₹0.87 L vs ₹1.38 L). In a period when the rupee fell steadily, hedging everything looks great after the fact. We sit between 50% and 100%: we take most of the protection for less cost, and we stay inside the board's ranges. The context doc says to admit it when a baseline wins, so admit it.
- "Cheaper than 100%" is partly automatic, because we hedge less than 100%.
- HedgeDesk hedged 100% on 30-day tenors and 75% on 60/90-day tenors in **every** month (s = 1 every time; see section 4).
- Limits: synthetic book, one currency pair, one 18-month period of rupee weakness, assumed 18 bps bank spread, and **modelled forward premia** (section 11).
- No look-ahead: `decide()` asserts that the market data date is on or before the decision date.

---

## 9. Money (for the Finance judge)

- **Buyer:** CFO or finance head of a mid-size Indian importer (bearings, electronics, machinery, chemicals) with regular USD payables. Our client is a 3PL importer, "BharatPrecision Components & 3PL Imports".
- **Who does it today:** a junior treasury analyst, an FX advisory firm on retainer (we use ₹45,000 a cycle), or the bank RM, who is conflicted because they earn on the spread.
- **Our price:** ₹24,999 a month per company.
- **Cost per job:** every Crustdata call is written to `cost_ledger` (credits × $0.05 × ₹92.68). A re-run from cache costs ₹0. A cold run of the 9-row demo is roughly 15–17 credits by the code's own estimates (enrich ≈ 2 credits, other calls ≈ 0.2), which is **about ₹70–80**. If you can, do one cold run before the finals so you can quote a measured number, not an estimate.
- **Value:** in the backtest the worst month dropped from ₹3.35 L to ₹1.38 L per $100k payable. For an importer paying $1M a month, that's about ₹20 L less loss in the worst month, against a ₹25k subscription.
- **Gross margin:** above 99% on API costs. Hosting and support aren't counted, so say "API cost" rather than "all-in".

---

## 10. Live fix playbook (5 minutes)

Most likely requests, and where to change them:

| Request | Change |
|---|---|
| Change the hedge ranges / k / spread / duty / ₹1 Cr threshold / budget | `policy.yaml`, or the "Edit Hedge Parameters" drawer (k, spread, duty, dual threshold, budget) |
| New output field on a decision | Add it to the dict in `policy.decide()` (around line 389) and a column in `static/index.html` |
| New hold rule | Add a flag in `book.build_book()` and a branch in `policy.decide()` |
| New currency in scope | `currency_scope` in policy.yaml, plus the `ccy != "USD"` check in `book.py:429` (and FX data for that pair) |
| Lower cost limit | `run_cost_limit_credits` in policy.yaml, or the Cost Limit box in the UI |

> 🚨 **Gotcha:** the `run_id` is a hash of the invoice rows and simulation flags. **It does not include the policy.** If you change `policy.yaml` and press "Run Demo Story" again, you get the same run_id, step 3 is **restored from the checkpoint**, and **your change won't show.**
> **Fix during the demo:** run `python reset_demo.py` after changing the policy (the cache is kept, so the re-run is free and fast), then run again.
> **Better fix, if there's time before freeze:** add `policy_version` or a hash of `policy.yaml` to `_deterministic_run_id` in `app.py`.

---

## 11. Weak spots: know these before a judge does

Ordered by how much damage they could do.

1. **The forward premia are not fetched from FBIL/RBI.** `sources.fetch_fx()` uses fixed values (1M 2.15%, 3M 2.45%, 6M 2.75%) plus a small volatility adjustment, and **labels that evidence row with the FBIL URL.** The problem statement says "facts from model memory are not permitted" and every fact must link to the record it came from. A Finance or CS judge who clicks the FBIL evidence row will find nothing matching. **Honest answer:** "Spot and volatility are real ECB data. The premium curve is modelled from the 2025–26 FBIL range because we couldn't get a parseable FBIL feed in time. It's marked as an assumption in the README." The new README says exactly this. Better still, if there's time before freeze, change the evidence label to "modelled premium (assumption)" so the UI doesn't claim FBIL.
2. **Razorpay isn't actually called.** `actions._razorpay_test_lookup_or_create()` makes up a `pay_rzptest_…` ID and stores it in our own `cache` table. The rules say payments "run in a payment provider's test mode". **Honest answer:** "The outbox and receipt lookup are built the way Razorpay's receipt-based idempotency works. Wiring the real test-mode API is a one-function change." Don't say "Razorpay dashboard shows one payment".
3. **The bank forward reference `HDFC-FWD-…` is a placeholder.** No email or bank request is actually sent. Fine for a demo, but don't imply a bank was contacted.
4. **Person Search may not be returning results.** There are no `/person/search` responses in the cache, which suggests the call is failing, and the code then reports "found 0 matches". The outcome is still safe (the payment is always held on a bank-change request, whatever the search finds), but don't claim "we verified Rahul doesn't work at Zebra". Say "we held it, and the sender's Gmail address doesn't match zebra.com".
5. **The maker/trader classifier is a keyword list and defaults to MAKER.** The Zebra search returned "Sun Data Processing" and "Wayinfotech solutions" as makers. If asked: "It's a first filter; a person reviews the RFQ list before anything is sent."
6. **s = 1 everywhere** (section 4). The "dynamic" policy is really "top of range" in this market.
7. **A forecast PO also gets a payment proposal** (PO-2026-403). Paying against a PO with no invoice is odd. It sits in drafted and a person would reject it.
8. **₹ cost conversion uses a fixed 92.68** (`sources.py:366`), not the live spot.
9. **The staleness gate is only simulated.** In live mode, `fetch_fx()` never compares the rate's date with `max_market_data_age_business_days`. If Frankfurter is down it falls back to the local history file (last date 20 Nov 2026) without complaining. That's fine on demo day, but it would be silently stale a week later. In the Q&A about bad data, say "sanity bounds, plus a staleness hold we've tested but not yet wired into the live path".
10. **Tenors are measured from today's date**, so numbers drift day to day. They match the figures above only on 22 Nov 2026.
11. **There's no LLM in the system.** The context doc planned one for reading invoices. It's a strength for ethics ("no model makes any decision"), but don't call it "AI-powered".
12. **Repo hygiene:** the old v1 files under `hedgedesk/*.py` and `hedgedesk_state.db` are still there; `.env.example` points `HEDGEDESK_DB_PATH` at the v1 DB (the current code ignores it and always uses `hedgedesk.db`); the old README had `file:///C:/Users/Arjun Kumar/...` links that don't work for anyone else.

---

## 12. Likely questions with short answers

**How is this different from asking ChatGPT?** The hedge is computed by fixed code from live data under the company's written policy. Same input, same answer, every time, and it can be backtested. No model picks a number.

**Why not forecast the rupee?** Forecasts are unreliable, and hedging on a forecast is speculation. Board policies forbid it.

**Why not options?** They have better payoffs, but pricing them needs implied-volatility data we can't source and verify for free. Future scope.

**100× volume?** The market data is the same for every client, so fetch it once a day and share it. The Crustdata cache is shared too. The policy engine is pure functions, so it scales out horizontally. Use one queue and outbox per company, and move SQLite to Postgres.

**A second client?** Add a company_id to every table and give each client its own `policy.yaml`. Market data and the supplier cache stay shared.

**What if the data is wrong?** Sanity bounds (spot must be ₹60–130, premium −2% to 15%). If they fail, we say "cannot verify" and make no decision. A 1-business-day freshness limit is in the policy and the tests, but not yet in the live path (see section 11, item 9).

**What personal data do you touch?** Only for the bank-change check: the requester's name and work email, compared with professional profiles at the supplier. We store name, employer, source and date, nothing more. Everything else is company data.

**What's irreversible, and who approves it?** Forward bookings, payments and RFQ emails. All wait for a person. Above ₹1 Cr, two people. Held items can't be approved at all until the hold is cleared. During the event, emails go only to our team inbox.

**Is this regulated advice?** No. It executes the customer's own written policy and drafts requests to their own bank. We don't sell products or take a spread.

**Fairness to suppliers?** A risk flag always cites the data behind it (for example, headcount −8% over 6 months). "Cannot verify" is never shown as "fraud", and every hold shows its reason to a person.

---

## 13. Words you should be able to define

- **Forward contract:** an agreement with the bank today to buy dollars on a future date at a fixed rate.
- **Forward premium:** how much more the forward rate is than spot, as an annual %. For USD/INR it's mostly the interest-rate gap between the two countries.
- **Hedge ratio:** the share of the invoice we lock in with a forward.
- **VaR 95%:** the loss you'd exceed only 1 time in 20, over the invoice's horizon.
- **CVaR:** the average of the worst 5–15% of outcomes.
- **Tenor:** days until payment.
- **Confirmed vs forecast:** an invoice we definitely owe vs a PO that may change. Forecasts get lower hedge ranges.
- **Roll:** moving an existing forward to a new date, paying the swap cost, instead of opening a second one.
- **Maker-checker:** one person proposes, a different person approves.
- **Idempotency key:** a fixed ID for an action, so doing it twice has the same effect as doing it once.
- **Point-in-time:** in the backtest, a decision only sees data dated on or before that day.
- **Landed cost:** price × FX + duty + freight, i.e. what the part actually costs once it arrives.
- **Break-even price:** the domestic price below which buying in India wins.

---

## 14. Commands

```powershell
pip install -r requirements.txt
python test_28_cases.py                         # all 28 cases, rewrites TESTS.md (makes live Crustdata calls for new run IDs)
python reset_demo.py                            # clear runs, keep the cache
python -m uvicorn app:app --host 127.0.0.1 --port 8000
python run.py                                   # CLI; Ctrl+C, then run again to resume
```

Note: `test_28_cases.py` uses a random tag in its run IDs, so some cases may make fresh paid Crustdata calls. Don't run it in a loop right before the finals.
