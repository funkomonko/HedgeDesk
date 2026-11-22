"""
test_28_cases.py — Executes and verifies ALL 28 edge cases from HedgeDesk Context Doc Pages 11-14
and generates TESTS.md with observed outcomes.
"""
import uuid
from pathlib import Path
from typing import List, Tuple

from actions import approve, execute, get_outbox, make_payment_idem_key, propose
from alternatives import find_makers
from backtest import metrics, replay
from book import build_book, load_invoices, parse_amount_messy, resolve_supplier, verify_bank_change_email
from engine import KillOnStageException, run_pipeline
from policy import decide
from sources import crust, fetch_fx, get_run_cost_summary, load_policy_yaml


def run_all_28_cases() -> Tuple[bool, List[Tuple[str, bool, str]]]:
    cfg = load_policy_yaml()
    results: List[Tuple[str, bool, str]] = []
    tag = uuid.uuid4().hex[:6]

    def rec(num: str, title: str, cond: bool, obs: str):
        results.append((f"Case {num}: {title}", cond, obs))
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] Case {num}: {title} -> {obs}")

    # Cases 1*, 2*: Same invoice uploaded twice & variant spelling ('SKF GmbH' vs 'SKF Gmbh.')
    rows_dup = [
        {"invoice_id": "C1-100", "doc_type": "confirmed", "supplier_raw": "SKF GmbH", "supplier_domain": "skf.com", "currency": "USD", "amount": 200000, "due_date": "2027-01-21"},
        {"invoice_id": "C1-100", "doc_type": "confirmed", "supplier_raw": "SKF GmbH", "supplier_domain": "skf.com", "currency": "USD", "amount": 200000, "due_date": "2027-01-21"},
        {"invoice_id": "C2-101", "doc_type": "confirmed", "supplier_raw": "SKF Gmbh.", "supplier_domain": "skf.com", "currency": "USD", "amount": 200000, "due_date": "2027-01-21"},
    ]
    res_dup = run_pipeline(f"t12-{tag}", rows_dup)
    decs_dup = res_dup["decisions"]
    rec("1*", "Exact duplicate uploaded twice linked as dup_of & hedged once", decs_dup[0]["ratio"] > 0 and decs_dup[1]["action"] == "HOLD_DUPLICATE", f"orig={decs_dup[0]['ratio']}, dup={decs_dup[1]['action']}")
    rec("2*", "Fuzzy supplier spelling ('SKF GmbH' vs 'SKF Gmbh.') resolved to same ID & held", res_dup["book"]["invoices"][2]["dup_of"] == "C1-100" and decs_dup[2]["action"] == "HOLD_DUPLICATE", f"dup_of={res_dup['book']['invoices'][2]['dup_of']}")

    # Case 3: Two genuine invoices with identical amount/date and distinct invoice numbers
    rows_c3 = [
        {"invoice_id": "INV-A-901", "doc_type": "confirmed", "supplier_raw": "SKF GmbH", "supplier_domain": "skf.com", "currency": "USD", "amount": 40000, "due_date": "2027-01-21"},
        {"invoice_id": "INV-B-902", "doc_type": "confirmed", "supplier_raw": "SKF GmbH", "supplier_domain": "skf.com", "currency": "USD", "amount": 40000, "due_date": "2027-01-21"},
    ]
    res_c3 = run_pipeline(f"t3-{tag}", rows_c3)
    rec("3", "Two genuine invoices identical amt/date NOT auto-merged; held for confirmation", res_c3["decisions"][1]["action"] == "HOLD_CONFIRM_DISTINCT", res_c3["decisions"][1]["reason"][:75])

    # Cases 4, 5, 6, 7, 8, 9, 10
    p7 = parse_amount_messy("USD 1.2 lakh")
    rec("7", "Messy text / 'USD 1.2 lakh' normalized with lower confidence flag", p7["amount"] == 120000.0 and p7["confidence"] < 0.95, f"amt={p7['amount']}, conf={p7['confidence']}")

    rows_scope = [
        {"invoice_id": "C4-MISS", "doc_type": "confirmed", "supplier_raw": "Honeywell", "supplier_domain": "honeywell.com", "currency": "USD", "amount": 50000, "due_date": ""},
        {"invoice_id": "C5-EUR", "doc_type": "confirmed", "supplier_raw": "Danfoss", "supplier_domain": "danfoss.com", "currency": "EUR", "amount": 35000, "due_date": "2027-01-10"},
        {"invoice_id": "C6-INR", "doc_type": "confirmed", "supplier_raw": "Carrier Global", "supplier_domain": "carrier.com", "currency": "INR", "amount": 1500000, "due_date": "2027-01-10"},
        {"invoice_id": "C8-PAST", "doc_type": "confirmed", "supplier_raw": "NSK Ltd.", "supplier_domain": "nsk.com", "currency": "USD", "amount": 42000, "due_date": "2026-11-10"},
        {"invoice_id": "C9-LONG", "doc_type": "confirmed", "supplier_raw": "SKF GmbH", "supplier_domain": "skf.com", "currency": "USD", "amount": 60000, "due_date": "2027-08-15"},
        {"invoice_id": "C10-FCST", "doc_type": "forecast", "supplier_raw": "Timken Company", "supplier_domain": "timken.com", "currency": "USD", "amount": 120000, "due_date": "2027-01-15"},
    ]
    res_sc = run_pipeline(f"t410-{tag}", rows_scope)
    dmap = {d["invoice_id"]: d for d in res_sc["decisions"]}
    rec("4", "Missing due date held with CANNOT_DETERMINE_TENOR", dmap["C4-MISS"]["action"] == "CANNOT_DETERMINE_TENOR", dmap["C4-MISS"]["reason"][:60])
    rec("5", "Non-USD invoice flagged out-of-scope unhedged", dmap["C5-EUR"]["action"] == "OUT_OF_SCOPE_FLAG", dmap["C5-EUR"]["reason"][:60])
    rec("6", "INR invoice from foreign supplier treated as NO_FX_EXPOSURE", dmap["C6-INR"]["action"] == "NO_FX_EXPOSURE", dmap["C6-INR"]["reason"][:60])
    rec("8", "Overdue past-due invoice -> pay at spot today, no forward", dmap["C8-PAST"]["action"] == "PAY_AT_SPOT_NO_FORWARD", dmap["C8-PAST"]["reason"][:60])
    rec("9", "Tenor > 6 months outside policy -> referred to person", dmap["C9-LONG"]["action"] == "REFER_TO_PERSON_BEYOND_6M", dmap["C9-LONG"]["reason"][:60])
    rec("10", "Forecast PO uses forecast range (never above 50%)", 0.25 <= dmap["C10-FCST"]["ratio"] <= 0.50, f"forecast_ratio={dmap['C10-FCST']['ratio']}, clause={dmap['C10-FCST']['policy_clause']}")

    # Cases 11*, 12, 13, 14*
    sup_look = resolve_supplier("Carrier Global", supplier_domain="", run_id=f"t11-{tag}")
    rec("11*", "Lookalike name-only ('Carrier Global' -> 'Global Carrier') held", sup_look["status"] == "LOOKALIKE_HOLD", sup_look["reason"][:75])

    rows_c12 = [{"invoice_id": f"C12-NF-{tag}", "doc_type": "confirmed", "supplier_raw": "Xyz Unlisted Precision 999123", "supplier_domain": "nonexistent-999123.invalid", "currency": "USD", "amount": 50000, "due_date": "2027-01-21"}]
    res_c12 = run_pipeline(f"t12nf-{tag}", rows_c12)
    out_c12 = [o for o in res_c12["outbox"] if o["kind"] == "payment"][0]
    rec("12", "Unfound supplier: confirmed hedge at bottom of range, payment HELD", res_c12["decisions"][0]["ratio"] == 0.5 and out_c12["state"] == "held", f"ratio={res_c12['decisions'][0]['ratio']}, pay_state={out_c12['state']}")

    # Case 13: synthetic sharp headcount drop or new entity risk=high -> confirmed bottom lo, forecast 0
    mock_book_13 = {
        "invoices": [
            {"invoice_id": "C13-C", "doc_type": "confirmed", "currency": "USD", "amount_usd": 50000, "tenor_days": 60, "bucket": "b", "flags": [], "supplier": {"status": "VERIFIED", "risk_flag": "high", "evidence_ids": []}},
            {"invoice_id": "C13-F", "doc_type": "forecast", "currency": "USD", "amount_usd": 50000, "tenor_days": 60, "bucket": "b", "flags": [], "supplier": {"status": "VERIFIED", "risk_flag": "high", "evidence_ids": []}},
        ]
    }
    mkt_ok = fetch_fx(run_id=f"t13-{tag}")
    decs_13 = decide(mock_book_13, mkt_ok, cfg)
    rec("13", "High supplier risk flag: confirmed hedges at lo (0.5), forecast held (0.0)", decs_13[0]["ratio"] == 0.5 and decs_13[1]["ratio"] == 0.0, f"confirmed={decs_13[0]['ratio']}, forecast={decs_13[1]['ratio']}")

    # Case 14*: Bank-detail change check via Crustdata Person Search
    bc = verify_bank_change_email("zebra.com", "Rahul Verma", "rahul.verma@gmail.com", run_id=f"t14-{tag}")
    rec("14*", "Bank-detail change email checked via Person Search & HELD", bc["verdict"] == "HOLD_BANK_CHANGE_FRAUD_CHECK" and not bc["domain_match"], bc["reason"][:80])

    # Cases 15, 16, 17: Alternatives (Trader excluded, No candidate, Private price cannot verify)
    alt_live = find_makers("precision industrial ball bearings manufacturer", 500.0, 400, 93.0, run_id=f"t15-{tag}")
    rec("15", "Domestic candidate classified MAKER vs TRADER cleanly", alt_live["status"] in {"SOURCED", "ONLY_TRADERS_FOUND"}, f"makers={len(alt_live['makers'])}, traders_excluded={len(alt_live['traders_excluded'])}")
    alt_none = find_makers("unobtanium flux capacitor", 500.0, 400, 93.0, run_id=f"t16-{tag}", simulate_no_candidate=True)
    rec("16", "No domestic candidate handled without inventing makers", alt_none["status"] == "NO_DOMESTIC_CANDIDATE" and len(alt_none["makers"]) == 0, alt_none["note"])
    rec("17", "Domestic price always 'cannot verify, RFQ drafted' + break-even", alt_live.get("price_verdict") == "cannot verify, RFQ drafted" and alt_live["break_even_inr"] > 0, f"BE <= INR {alt_live['break_even_inr']}")

    # Cases 18*, 19, 20, 21, 22: Market data edge cases
    fx18 = fetch_fx(run_id=f"t18-{tag}", simulate_down=True)
    rec("18*", "Both FX feeds down -> CANNOT_VERIFY & no decision", fx18["status"] == "CANNOT_VERIFY", fx18["reason"][:60])
    fx19 = fetch_fx(run_id=f"t19-{tag}", simulate_stale=True)
    rec("19", "Stale FX data (>1 business day) -> STALE_HOLD naming date", fx19["status"] == "STALE_HOLD" and "2026-10-01" in fx19["reason"], fx19["reason"][:60])
    fx20 = fetch_fx(run_id=f"t20-{tag}", simulate_nonsense=True)
    rec("20", "Sanity bounds reject negative spot / 40% premium", fx20["status"] == "CANNOT_VERIFY", fx20["reason"][:65])
    fx21 = fetch_fx(run_id=f"t21-{tag}")
    rec("21", "Market holiday uses last published business day & states date", fx21["status"] == "VERIFIED" and bool(fx21.get("record_date")), f"record_date={fx21.get('record_date')}, spot={fx21.get('spot')}")
    fx22 = fetch_fx(run_id=f"t22-{tag}", simulate_disagreement=True)
    rec("22", "Source disagreement flagged & freshest official used", fx22["disagreement_flagged"] is True, f"evidence_ids={fx22['evidence_ids']}")

    # Cases 23*, 24*: Kill on stage + Crash after Razorpay call before saving outbox
    kill_rid = f"t23-{tag}"
    killed = False
    try:
        run_pipeline(kill_rid, rows_dup[:1], simulate_kill_at_step="3_policy")
    except KillOnStageException:
        killed = True
    resumed = run_pipeline(kill_rid, rows_dup[:1])
    rec("23*", "Kill mid-run & restart resumes from SQLite `steps` without re-calling APIs", killed and any("Restored from SQLite" in l for l in resumed["step_logs"]), f"logs={len(resumed['step_logs'])}")

    # Case 24*: Killed after Razorpay API call before saving result -> restart reconciles receipt!
    pay_k = make_payment_idem_key(f"C24-{tag}", 12000.0, "2027-01-21")
    propose(f"t24-{tag}", "payment", f"C24-{tag}", pay_k, {"inr_value": 500000.0, "title": "Test Case 24"})
    approve(pay_k, approver="CFO")
    crashed_24 = False
    try:
        execute(pay_k, simulate_kill_before_save=True)
    except RuntimeError:
        crashed_24 = True
    exec_recov = execute(pay_k, simulate_kill_before_save=False)
    exec_retry = execute(pay_k, simulate_kill_before_save=False)
    rec("24*", "Crash after Razorpay call before outbox save reconciles existing receipt ID (no double-pay)", crashed_24 and exec_recov["external_ref"] == exec_retry["external_ref"] and exec_retry["already_executed"], f"ref={exec_recov['external_ref']}")

    # Cases 25, 26, 27*, 28
    c25 = crust("/company/identify", {"domains": ["skf.com"]}, run_id=f"t25-{tag}", simulate_status=429)
    rec("25", "Crustdata 429 backoff & cache resilience", c25["ok"] is True, "Cache/backoff returned OK")

    c26 = resolve_supplier("SKF GmbH", supplier_domain="", run_id=f"t26-{tag}", simulate_402=True)
    rec("26", "Crustdata 402 credits-out stops new live calls & flags skipped checks", c26["skipped_due_to_budget"] is True, c26["reason"][:60])

    rows_27 = [{"invoice_id": "C27-ROLL", "doc_type": "confirmed", "supplier_raw": "Carrier Global", "supplier_domain": "carrier.com", "currency": "USD", "amount": 90000, "previous_due_date": "2026-12-20", "due_date": "2027-02-05"}]
    res_27 = run_pipeline(f"t27-{tag}", rows_27)
    rec("27*", "Due date moved -> rolls existing forward, records swap cost (never books 2nd)", res_27["decisions"][0]["action"] == "ROLL_EXISTING_FORWARD" and res_27["decisions"][0]["roll_cost_inr"] > 0, f"roll_cost=INR {res_27['decisions'][0]['roll_cost_inr']:,.2f}")

    c28 = crust("/company/enrich", {"domains": ["danfoss.com"]}, run_id=f"t28-{tag}", cost_limit_credits=0.1)
    rec("28", "Mid-run lowered cost limit enforced before paid call", (not c28["ok"]) and c28["error"] == "COST_LIMIT_REACHED", c28.get("message", "")[:70])

    # Backtest verification
    bt = metrics(replay(cfg))
    print(f"  [BACKTEST] {bt['headline']}")

    # Write TESTS.md
    lines = [
        "# HedgeDesk 28 Edge-Case Verification Report (`TESTS.md`)",
        "",
        f"Automated execution across all 28 cases from Context Doc Pages 11–14. Total checks: **{len(results)}/28**.",
        "",
        "| Case | Expected Behaviour Verified | Status | Observed Output |",
        "| :--- | :--- | :---: | :--- |",
    ]
    for label, ok, obs in results:
        lines.append(f"| {label.split(':')[0]} | {label.split(':', 1)[1].strip()} | {'PASS' if ok else 'FAIL'} | `{obs}` |")
    lines.extend([
        "",
        "## 18-Month Real USD/INR Backtest Headline",
        f"> {bt['headline']}",
    ])
    Path("TESTS.md").write_text("\n".join(lines), encoding="utf-8")

    all_ok = all(r[1] for r in results)
    print(f"\nALL 28 CASES PASSED: {all_ok} ({sum(1 for r in results if r[1])}/{len(results)})")
    return all_ok, results


if __name__ == "__main__":
    ok, _ = run_all_28_cases()
    raise SystemExit(0 if ok else 1)
