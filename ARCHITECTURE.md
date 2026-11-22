# HedgeDesk Architecture (`ARCHITECTURE.md`)

> One cached source layer feeds every decision; one outbox executes it.

```mermaid
flowchart TD
    IN["Inputs: Invoices / POs (sample_invoices.csv or plain-English ask)"] --> BOOK

    subgraph CORE["Deterministic Pipeline + SQLite WAL Checkpoints (hedgedesk.db)"]
        BOOK["book.py\nload_invoices(), dedupe(), resolve_supplier(), verify_bank_change_email(), build_book()"] --> CP1[("steps: 1_book")]
        CP1 --> MKT["sources.py\nfetch_fx() + sanity bounds + staleness gate"]
        MKT --> CP2[("steps: 2_market")]
        CP2 --> POL["policy.py (Pure Functions, No I/O)\nvar_95(), fwd_cost(), hedge_ratio(), decide()"]
        POL --> CP3[("steps: 3_policy")]
        CP3 --> ALT["alternatives.py\nfind_makers(), maker vs trader check, landed_cost(), break_even(), RFQ drafts"]
        ALT --> CP4[("steps: 4_alternatives")]
        CP4 --> ACT["actions.py\npropose() into outbox with SHA-256 idempotency keys"]
        ACT --> CP5[("steps: 5_outbox")]
    end

    SRC["sources.py\ncrust() cached by request_hash, 429/5xx backoff, 402 & cost limit gate"]
    BOOK <--> SRC
    MKT <--> SRC
    ALT <--> SRC
    BT["backtest.py\nreplay() & metrics() on 2025-2026 USD/INR history\nwith point-in-time assert"] <--> SRC
    BT --> POL

    SRC <--> CRUST["Crustdata API v2025-11-01\nIdentify, Enrich, Search, Person Search"]
    SRC <--> FX["Official FX Sources\nECB / Frankfurter + FBIL Premia"]

    SRC --> LEDGERS[("SQLite hedgedesk.db\ncache | evidence | cost_ledger | invoices | decisions | outbox | steps")]

    CP5 --> UI["FastAPI + Web Terminal (app.py / static/index.html)"]
    UI -- "Human Approve (+ Dual Maker-Checker > ₹1 Cr)" --> EXEC["actions.py: approve() -> execute()\nChecks outbox state + Razorpay receipt lookup before payout"]
    EXEC --> OUT["Outputs:\n1. Bank FX Forward Request\n2. Razorpay Test Payment ID\n3. Team-Inbox Domestic RFQs"]
```

## Module Map (Matches Context Doc Specification)

| File | Responsibility |
| :--- | :--- |
| `policy.yaml` | Board-approved tenor ranges (`0-1m`, `1-3m`, `3-6m` × `confirmed`/`forecast`), `k=3.0`, maker-checker threshold, sanity bounds, cost ceiling. |
| `sources.py` | Single external gateway (`crust()`, `fetch_fx()`, `evidence()`), SQLite `hedgedesk.db` 7-table schema, SHA-256 cache, `429`/`402` handling. |
| `book.py` | Parses CSV/messy text (`"USD 1.2 lakh"`), resolves suppliers via Crustdata, checks bank-detail change emails via `Person Search`, links duplicates via `dup_of`. |
| `policy.py` | Zero-I/O pure math (`var_95`, `fwd_cost`, `hedge_ratio`, `decide`) with point-in-time `assert`. |
| `alternatives.py` | Finds Indian manufacturers via `Company Search`, filters `TRADER` vs `MAKER`, computes break-even `₹/unit`, marks prices `"cannot verify, RFQ drafted"`. |
| `actions.py` | `drafted` -> `approved` -> `sent`/`paid` outbox state machine, dual approval gate, Razorpay test receipt lookup (`Case 24*`). |
| `backtest.py` | Point-in-time replay on 18 months of daily USD/INR data (`2025-04-30` to `2026-11-20`) vs 3 baselines. |
| `engine.py` | Crash-resilient orchestrator reading/writing SQLite `steps` checkpoints. |
| `app.py` & `static/index.html` | Interactive web console, CSV upload, `policy.yaml` live editor, Kill-on-Stage simulator, and Approval Queue. |
