# HedgeDesk demo: narration script

Video: `demo/out/hedgedesk_demo_final.mp4`, about 3:31 long. Times are when each section starts in the video. Read at a normal pace; each section holds until its line is done.

## 0:00 · Title

*On screen:* Title slide

This is HedgeDesk. It does the job of an FX advisor for an Indian company that pays its suppliers in dollars.

## 0:08 · Problem

*On screen:* Problem slide: $200,000 order, +₹3.7 L on a 2% move, ₹24,999/month

Say you owe a German supplier two hundred thousand dollars in sixty days. If the rupee slips two percent, that order costs three point seven lakh more. Mid-size importers rarely have anyone watching that.

## 0:21 · Pipeline

*On screen:* Pipeline slide: five steps, checkpoint band

Every run goes through five steps: build the book, get the market rate, apply the policy, look for Indian makers, and fill the outbox. Each step saves to SQLite before the next one starts.

## 0:35 · Technical architecture

*On screen:* Technical architecture diagram

The browser and the command line both call a FastAPI app. Every outside call goes through one module, sources dot py. It caches responses, checks the credit budget first, and writes a source row and a cost row for each call. Nothing executes until a person approves it.

## 0:54 · Run the demo

*On screen:* App opens empty; click Run Demo Story; KPIs fill in

Here's the app with a sample book of nine invoices. One click runs the whole pipeline on live Crustdata and ECB data.

## 1:03 · Exposure book

*On screen:* Exposure Book tab, scroll past DUPLICATE_LINKED, LOOKALIKE, bank-change flag

In the book, the second SKF invoice is caught as a duplicate, a lookalike supplier is held, and a request to change bank details is flagged for a phone check.

## 1:14 · Hedge decisions

*On screen:* Hedge Decisions tab, scroll through ranges and evidence IDs

Each decision shows the allowed range for that tenor, the risk against the cost of cover, and the evidence IDs behind every number. Holds say why in plain words.

## 1:25 · Domestic alternatives

*On screen:* Domestic Alternatives tab, open a drafted RFQ

For bigger orders it finds Indian manufacturers and works out the price that beats importing. We don't know their prices, so it drafts an RFQ instead of guessing.

## 1:36 · Dual approval

*On screen:* Approvals tab, click Approve on the ₹1.39 Cr forward; refusal toast

Approvals. This forward is over one crore, so one approver isn't enough, and it's refused.

## 1:43 · Approve

*On screen:* Type 'Director, Finance' as approver 2; approve forward and payment

Add a second approver, and the forward and the payment both go through.

## 1:52 · Kill and restart

*On screen:* Server killed, page fails to load, server restarts, run resumes; outbox shows Executed Once

Now we kill the server, hard. The page can't connect. We start it again, and the run comes back from its checkpoints without fetching anything twice. The outbox still shows both actions executed once. No second payment.

## 2:08 · Evidence and cost

*On screen:* Evidence & Cost Ledger tab

Every fact has a source and a record date. Every call, cache hits and failures included, has a cost row.

## 2:16 · Backtest

*On screen:* 18M ECB Backtest tab

The backtest replays eighteen months of real rupee rates through the same decide function. It cut the worst month's overrun by about fifty-nine percent compared with never hedging.

## 2:28 · Policy and unseen request

*On screen:* Open and close Edit Hedge Parameters; type the request with no amount; HOLD_MISSING_AMOUNT

The hedge policy is a YAML file the finance team can edit. Here's a request it hasn't seen, with no amount in it. Nothing gets filled in. The invoice is held, and the reason names what's missing.

## 2:41 · FX feed down

*On screen:* Tick 'FX Feeds Down', run again; every invoice held

If the FX feed goes down, there's no rate to trust, so every invoice is held and nothing is decided.

## 2:50 · Design decisions

*On screen:* Design decisions slide

A few design choices. The hedge rule is a pure function, so the backtest runs the same code as production. We kept language models out of the decision, because one invoice worded two ways got two answers. Actions are keyed by their contents, so a retry can't pay twice. And anything missing ends in a hold.

## 3:10 · What isn't live yet

*On screen:* What isn't live yet slide

What isn't live yet: forward premia are modelled, and labelled that way. Payments go through a local stand-in for Razorpay test mode. And the staleness check isn't on the live path yet.

## 3:23 · Close

*On screen:* Title slide

That's HedgeDesk. Thanks for watching.
