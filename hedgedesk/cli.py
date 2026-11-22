import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict

from .backtester import run_fx_backtest
from .config import DEFAULT_POLICY
from .db import StateDB
from .policy_engine import SimulatedCrashError, run_hedgedesk_pipeline
from .test_cases import TEST_CASES


def run_automated_suite(db: StateDB) -> bool:
    """Runs all standard, hard, crash-recovery, and idempotency tests against live/cached APIs."""
    print("=" * 72)
    print("HEDGEDESK VERIFICATION & HARD-CASE SUITE (Post-Sept 2026 Rules)")
    print("=" * 72)

    all_passed = True

    def check(label: str, cond: bool, detail: str = ""):
        nonlocal all_passed
        tag = "PASS" if cond else "FAIL"
        print(f"  [{tag}] {label}" + (f" -> {detail}" if detail else ""))
        if not cond:
            all_passed = False

    # 1. Standard Cold-Chain + Domestic Break-Even
    tag_id = uuid.uuid4().hex[:6]
    r1 = run_hedgedesk_pipeline(
        run_id=f"test-std-{tag_id}",
        request_payload=TEST_CASES["standard_cold_chain"],
        db=db,
    )
    check("Live FX spot retrieved & dated", r1["fx_market"]["status"] == "VERIFIED", f"USD/INR={r1['fx_market'].get('spot_usd_inr')} ({r1['fx_market'].get('spot_date')})")
    check("Foreign suppliers verified via Crustdata", all(v["status"] == "VERIFIED" for v in r1["supplier_verifications"].values()))
    check("Exposure book hedged > $0", r1["exposure_book"]["total_hedged_usd_equiv"] > 0, f"${r1['exposure_book']['total_hedged_usd_equiv']:,.2f} hedged")
    dom1 = r1["domestic_comparisons"]["INV-2026-101"]
    check("Indian manufacturers found & break-even computed", len(dom1.get("makers", [])) > 0 and dom1["domestic_breakeven_unit_inr"] > 0, f"{len(dom1.get('makers', []))} makers, break-even <= INR {dom1['domestic_breakeven_unit_inr']}")
    check("Private domestic unit price labeled CANNOT_VERIFY", "CANNOT_VERIFY" in dom1["catalogue_price_verdict"])

    # 2. Hard Case: Duplicate Invoice Detection
    r2 = run_hedgedesk_pipeline(
        run_id=f"test-dup-{tag_id}",
        request_payload=TEST_CASES["hard_duplicate_invoice"],
        db=db,
    )
    decs2 = {d["invoice_id"]: d for d in r2["hedge_decisions"]}
    check("First invoice recommended for hedge", decs2["INV-2026-884"]["decision"] == "HEDGE_RECOMMENDED")
    check("Duplicate invoice held (0 over-hedge)", decs2["INV-2026-884-REISSUE"]["decision"] == "HOLD_DUPLICATE" and decs2["INV-2026-884-REISSUE"]["hedged_amount_fx"] == 0.0)

    # 3. Hard Case: Lookalike + Cannot Verify Ghost Supplier
    r3 = run_hedgedesk_pipeline(
        run_id=f"test-lookalike-{tag_id}",
        request_payload=TEST_CASES["hard_lookalike_unverifiable"],
        db=db,
    )
    vers3 = r3["supplier_verifications"]
    decs3 = {d["invoice_id"]: d for d in r3["hedge_decisions"]}
    check("Lookalike name-only 'Carrier Global' flagged", vers3["INV-2026-901"]["status"] in {"LOOKALIKE_SUSPECT", "CANNOT_VERIFY"}, vers3["INV-2026-901"].get("reason", "")[:80])
    check("Ghost shell supplier returns CANNOT_VERIFY", vers3["INV-2026-902"]["status"] == "CANNOT_VERIFY")
    check("Zero unauthorized forward booked on unverified lines", decs3["INV-2026-901"]["hedged_amount_fx"] == 0.0 and decs3["INV-2026-902"]["hedged_amount_fx"] == 0.0)

    # 4. Hard Case: Forward Roll + Stale/Down FX Gate
    r4 = run_hedgedesk_pipeline(
        run_id=f"test-roll-{tag_id}",
        request_payload=TEST_CASES["hard_roll_and_stale_fx"],
        db=db,
    )
    dec4 = r4["hedge_decisions"][0]
    check("Forward roll detected & swap cost computed", dec4["decision"] == "EXECUTE_FORWARD_ROLL" and dec4["roll_cost_inr"] > 0, f"roll_cost=INR {dec4['roll_cost_inr']:,.2f}")

    stale_payload = dict(TEST_CASES["hard_roll_and_stale_fx"])
    stale_payload["simulate_rate_source_down"] = True
    r4b = run_hedgedesk_pipeline(
        run_id=f"test-downfx-{tag_id}",
        request_payload=stale_payload,
        db=db,
    )
    check("Offline rate source cleanly holds with CANNOT_VERIFY_FX_HOLD", r4b["hedge_decisions"][0]["decision"] == "CANNOT_VERIFY_FX_HOLD")

    # 5. Kill-on-Stage Crash Recovery + Zero-Duplicate Idempotent Execution
    kill_run_id = f"test-kill-{uuid.uuid4().hex[:6]}"
    crashed = False
    try:
        run_hedgedesk_pipeline(
            run_id=kill_run_id,
            request_payload=TEST_CASES["standard_cold_chain"],
            db=db,
            simulate_kill_at_step=3,
        )
    except SimulatedCrashError:
        crashed = True
    check("Kill-on-Stage intentionally halted at Step 3", crashed and db.get_run(kill_run_id)["status"] == "PAUSED_KILLED")

    resumed = run_hedgedesk_pipeline(
        run_id=kill_run_id,
        request_payload=TEST_CASES["standard_cold_chain"],
        db=db,
        simulate_kill_at_step=0,
    )
    check("Resumed seamlessly without duplicating Steps 1-3", "Restored from checkpoint" in resumed["step_logs"][0])

    pending_act = next((a for a in resumed["actions"] if a["status"] == "PENDING_APPROVAL"), None)
    check("Pending action registered behind Human Sign-Off Gate", pending_act is not None)
    if pending_act:
        exec1 = db.execute_approved_action(pending_act["idempotency_key"], approver="CFO-TEST")
        exec2 = db.execute_approved_action(pending_act["idempotency_key"], approver="CFO-TEST-RETRY")
        check("Idempotent execution executes exactly once on retry", (not exec1["already_executed"]) and exec2["already_executed"] and exec1["execution_ref"] == exec2["execution_ref"], f"ref={exec1['execution_ref']}")

    # 6. 18-Month Backtest
    bt = run_fx_backtest(db=db, policy=DEFAULT_POLICY)
    check("18-month real USD/INR backtest computed", bt["rolling_windows_tested"] >= 15, bt["headline"][:100])

    print("=" * 72)
    print(f"SUITE RESULT: {'ALL TESTS PASSED' if all_passed else 'SOME TESTS FAILED'}")
    print("=" * 72)
    return all_passed


def main():
    parser = argparse.ArgumentParser(description="HedgeDesk Autonomous AI Treasury & Sourcing Analyst CLI")
    parser.add_argument("--case", choices=list(TEST_CASES.keys()), help="Run a built-in test case")
    parser.add_argument("--input", type=str, help="Path to custom JSON request file")
    parser.add_argument("--run-id", type=str, help="Explicit run ID (useful with --resume)")
    parser.add_argument("--simulate-kill-at-step", type=int, default=0, help="Kill process cleanly after step 1..6")
    parser.add_argument("--resume", type=str, help="Resume an interrupted run_id from SQLite checkpoint")
    parser.add_argument("--budget-credits", type=float, default=25.0, help="Max Crustdata credits for this run")
    parser.add_argument("--backtest", action="store_true", help="Run 18-month real USD/INR backtest")
    parser.add_argument("--run-tests", action="store_true", help="Run full verification & crash-recovery suite")
    parser.add_argument("--json", action="store_true", help="Print compact JSON result")
    args = parser.parse_args()

    db = StateDB()

    if args.run_tests:
        ok = run_automated_suite(db)
        sys.exit(0 if ok else 1)

    if args.backtest:
        bt = run_fx_backtest(db=db, policy=DEFAULT_POLICY)
        print(json.dumps(bt, indent=2))
        return

    if args.resume:
        run_row = db.get_run(args.resume)
        if not run_row:
            print(f"Error: run_id '{args.resume}' not found in SQLite DB.", file=sys.stderr)
            sys.exit(1)
        payload = json.loads(run_row["request_json"])
        res = run_hedgedesk_pipeline(
            run_id=args.resume,
            request_payload=payload,
            db=db,
            simulate_kill_at_step=0,
            budget_limit_credits=args.budget_credits,
        )
        print(json.dumps(res, indent=2) if args.json else json.dumps({
            "run_id": res["run_id"],
            "status": res["status"],
            "step_logs": res["step_logs"],
            "exposure_book": res["exposure_book"],
            "unit_economics_inr": res["unit_economics_inr"],
        }, indent=2))
        return

    payload: Dict[str, Any]
    if args.input:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    else:
        payload = TEST_CASES[args.case or "standard_cold_chain"]

    rid = args.run_id or f"hd-{uuid.uuid4().hex[:8]}"
    try:
        res = run_hedgedesk_pipeline(
            run_id=rid,
            request_payload=payload,
            db=db,
            simulate_kill_at_step=args.simulate_kill_at_step,
            budget_limit_credits=args.budget_credits,
        )
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Run ID: {res['run_id']} | Status: {res['status']}")
            print(f"USD/INR Spot: {res['fx_market'].get('spot_usd_inr')} (date {res['fx_market'].get('spot_date')})")
            print(f"Exposure Book: {json.dumps(res['exposure_book'], indent=2)}")
            print(f"Cost Ledger: {res['cost_ledger']['total_credits_billed']} credits (INR {res['cost_ledger']['total_cost_inr']})")
            print("Pending/Held Actions:")
            for a in res["actions"]:
                print(f"  - [{a['status']}] {a['action_type']} | {a['title']} | key={a['idempotency_key']}")
    except SimulatedCrashError as sce:
        print(f"\n[SIMULATED KILL-ON-STAGE] {sce}", file=sys.stderr)
        print(f"To resume from exact checkpoint: python -m hedgedesk.cli --resume {rid}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
