# HedgeDesk 28 Edge-Case Verification Report (`TESTS.md`)

Automated execution across all 28 cases from Context Doc Pages 11–14. Total checks: **28/28**.

| Case | Expected Behaviour Verified | Status | Observed Output |
| :--- | :--- | :---: | :--- |
| Case 1* | Exact duplicate uploaded twice linked as dup_of & hedged once | PASS | `orig=0.75, dup=HOLD_DUPLICATE` |
| Case 2* | Fuzzy supplier spelling ('SKF GmbH' vs 'SKF Gmbh.') resolved to same ID & held | PASS | `dup_of=C1-100` |
| Case 3 | Two genuine invoices identical amt/date NOT auto-merged; held for confirmation | PASS | `Distinct invoice number 'INV-B-902' matches INV-A-901 on supplier/amount/da` |
| Case 7 | Messy text / 'USD 1.2 lakh' normalized with lower confidence flag | PASS | `amt=120000.0, conf=0.85` |
| Case 4 | Missing due date held with CANNOT_DETERMINE_TENOR | PASS | `Missing due date: cannot determine tenor; held and requestin` |
| Case 5 | Non-USD invoice flagged out-of-scope unhedged | PASS | `Currency EUR is outside today's USD-only policy scope; logge` |
| Case 6 | INR invoice from foreign supplier treated as NO_FX_EXPOSURE | PASS | `Invoice billed in INR from foreign vendor; zero USD/INR FX e` |
| Case 8 | Overdue past-due invoice -> pay at spot today, no forward | PASS | `Due date 2026-11-10 is in the past (-12d): spot exposure; re` |
| Case 9 | Tenor > 6 months outside policy -> referred to person | PASS | `Tenor 266d exceeds 6-month policy maximum (180d); referred t` |
| Case 10 | Forecast PO uses forecast range (never above 50%) | PASS | `forecast_ratio=0.5, clause=1_to_3_months.forecast` |
| Case 11* | Lookalike name-only ('Carrier Global' -> 'Global Carrier') held | PASS | `Lookalike supplier name: 'Carrier Global' resolved weakly to 'Global Carrie` |
| Case 12 | Unfound supplier: confirmed hedge at bottom of range, payment HELD | PASS | `ratio=0.5, pay_state=held` |
| Case 13 | High supplier risk flag: confirmed hedges at lo (0.5), forecast held (0.0) | PASS | `confirmed=0.5, forecast=0.0` |
| Case 14* | Bank-detail change email checked via Person Search & HELD | PASS | `Bank-detail change requested by 'Rahul Verma' (rahul.verma@gmail.com). Crustdata` |
| Case 15 | Domestic candidate classified MAKER vs TRADER cleanly | PASS | `makers=5, traders_excluded=0` |
| Case 16 | No domestic candidate handled without inventing makers | PASS | `No domestic candidate found; import decision stands; no invented alternatives.` |
| Case 17 | Domestic price always 'cannot verify, RFQ drafted' + break-even | PASS | `BE <= INR 50703.37` |
| Case 18* | Both FX feeds down -> CANNOT_VERIFY & no decision | PASS | `Both primary (FBIL/RBI) and secondary (ECB/Frankfurter) FX f` |
| Case 19 | Stale FX data (>1 business day) -> STALE_HOLD naming date | PASS | `Market data from 2026-10-01 is older than policy limit (1 bu` |
| Case 20 | Sanity bounds reject negative spot / 40% premium | PASS | `Sanity bounds rejected corrupted feed (spot=-5.0 outside [60.0, 1` |
| Case 21 | Market holiday uses last published business day & states date | PASS | `record_date=2026-11-20, spot=92.68` |
| Case 22 | Source disagreement flagged & freshest official used | PASS | `evidence_ids=[561, 562, 563]` |
| Case 23* | Kill mid-run & restart resumes from SQLite `steps` without re-calling APIs | PASS | `logs=5` |
| Case 24* | Crash after Razorpay call before outbox save reconciles existing receipt ID (no double-pay) | PASS | `ref=pay_rzptest_41f19b69df56` |
| Case 25 | Crustdata 429 backoff & cache resilience | PASS | `Cache/backoff returned OK` |
| Case 26 | Crustdata 402 credits-out stops new live calls & flags skipped checks | PASS | `HTTP 402: Crustdata credits exhausted; stopping live calls a` |
| Case 27* | Due date moved -> rolls existing forward, records swap cost (never books 2nd) | PASS | `roll_cost=INR 20,936.35` |
| Case 28 | Mid-run lowered cost limit enforced before paid call | PASS | `Run cost limit (0.10 cr) reached; skipped /company/enrich.` |

## 18-Month Real USD/INR Backtest Headline
> HedgeDesk cut the worst-month overrun by 58.7% vs never hedging, at 44.2 paise per dollar average cost, and was cheaper than always hedging 100% by 10.7 paise per dollar.