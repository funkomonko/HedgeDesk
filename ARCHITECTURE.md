# HedgeDesk Architecture (`ARCHITECTURE.md`)

> One cached source layer feeds every decision; one outbox executes it.

```mermaid
flowchart TD
    IN["Inputs: Invoices / POs (CSV upload or typed request)"] --> ASK{"app.py /api/ask\nstates an amount?"}
    ASK -- "yes: new invoice" --> BOOK
    ASK -- "no: question" --> QA["ask.py\nfixed intents over the current run\n(hedge plan, holds, local sourcing, approvals, cost)"]

    subgraph CORE["Pipeline: every step saved to SQLite steps before the next (run_id = hash of rows + flags + policy.yaml)"]
        BOOK["book.py\nload_invoices() (header aliases, dates, holds on missing/negative/ambiguous),\nresolve_supplier(), verify_bank_change_email(), dedupe()"] --> CP1[("steps: 1_book")]
        CP1 --> MKT["sources.fetch_fx()\nsanity bounds + staleness gate (business days, ECB holidays)"]
        MKT --> CP2[("steps: 2_market")]
        CP2 --> POL["policy.py (pure, no I/O)\nvar_95(), fwd_cost(), hedge_ratio(), decide()\n+ premium at which each hedge changes"]
        POL --> CP3[("steps: 3_policy")]
        CP3 --> ALT["alternatives.py\nfind_makers(): likely maker / trader / needs review,\nlanded_cost(), break_even(), RFQ drafts"]
        ALT --> CP4[("steps: 4_alternatives")]
        CP4 --> ACT["actions.propose()\noutbox rows keyed by content hash"]
        ACT --> CP5[("steps: 5_outbox")]
    end

    SRC["sources.crust()\ncache by request hash, 429/5xx backoff,\ncredit limit + time limit checked before each live call,\nevery call (hit, miss, failure) in cost_ledger,\nPerson Search reply cut to name/role/employer"]
    BOOK <--> SRC
    ALT <--> SRC
    MKT <--> FXSRC["ECB / Frankfurter spot\n(fallback: stored history file)\nforward premia modelled, not fetched"]
    BT["backtest.py\nreplay() on 2025-2026 USD/INR,\npoint-in-time assert"] --> POL

    SRC <--> CRUST["Crustdata API v2025-11-01\nIdentify, Enrich, Company Search, Person Search"]
    SRC --> DB[("hedgedesk.db (WAL)\ncache | evidence | cost_ledger | invoices | decisions | outbox | steps")]

    CP5 --> UI["FastAPI + web console (app.py / static/index.html)"]
    UI -- "Approve (2 people above ₹1 Cr) / Reject with reason" --> EXEC["actions.approve() / reject() -> execute()\nchecks outbox state and payment receipt before paying"]
    EXEC --> OUT["Outputs:\n1. Forward booking request (drafted)\n2. Payment via test-mode stand-in, receipt = outbox key\n3. Team-inbox RFQs"]
    UI -- "purge on request" --> PRIV["privacy.py\nredact bank-change contacts in every table,\ndrop cached Person Search replies, audit row"]
```

## Kill and restart: why each action happens once

```mermaid
sequenceDiagram
    participant U as Person
    participant A as app.py
    participant E as engine.run_pipeline
    participant S as steps table
    participant O as outbox table
    participant P as Payment (test mode)

    U->>A: Run batch
    A->>E: run_id = hash(rows, flags, policy)
    E->>S: save 1_book, 2_market, 3_policy ...
    Note over E: kill -9 here (any step)
    U->>A: restart, page reloads
    A->>E: /api/latest-run: same run_id
    E->>S: completed steps restored, no API calls repeated
    E->>O: propose(): key = hash(invoice, amount, due date)
    O-->>E: row already exists, returned as is
    U->>A: Approve payment
    A->>O: state drafted -> approved
    A->>P: create payment with receipt = outbox key
    Note over A,P: kill -9 after payment, before saving "paid"
    U->>A: Approve again after restart
    A->>P: look up receipt = outbox key
    P-->>A: existing payment id (no second payment)
    A->>O: state -> paid, external_ref saved
```

The same key rule covers re-runs: running the same invoices again, or under an edited policy, proposes the same payment key, finds the existing row (drafted, paid or rejected) and leaves it alone.

## Module map

| File | Responsibility |
| :--- | :--- |
| `policy.yaml` | Tenor ranges (`0-1m`, `1-3m`, `3-6m` × confirmed/forecast), `k`, VaR confidence, dual-approval threshold, staleness limit, sanity bounds, per-run credit and time limits, default duty and freight. |
| `sources.py` | Only file that calls outside services: `crust()`, `fetch_fx()`, evidence and cost ledgers, SQLite schema, run clock, Person Search minimising. |
| `book.py` | Reads CSV, typed or messy amounts and dates, maps common headers, resolves suppliers on Crustdata, checks bank-detail change requests, links duplicates. |
| `policy.py` | Pure maths (`var_95`, `fwd_cost`, `hedge_ratio`, `decide`) with the point-in-time assert; every hold has its own reason and clause. |
| `alternatives.py` | Indian maker search, keyword maker/trader/needs-review split, break-even ₹/unit, prices always "cannot verify, RFQ drafted". |
| `actions.py` | Outbox state machine (`drafted` → `approved` → `sent`/`paid`, or `held` / `rejected`), dual approval, receipt lookup before paying. |
| `ask.py` | Answers questions about a finished run from its own decisions and evidence; unknown questions get "cannot answer". |
| `privacy.py` | Summary of personal data held, purge per supplier domain or all, with an audit record. |
| `backtest.py` | Point-in-time replay on daily USD/INR (`2025-04-30` to `2026-11-20`) against never / 50% / 100% hedging. |
| `engine.py` | Runs the five steps, saving each to `steps`; starts the per-run clock. |
| `app.py`, `static/index.html` | Web console: runs, CSV upload, questions, policy editor, simulation toggles, approval queue, evidence and cost ledgers. |
