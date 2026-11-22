"""
engine.py — Checkpoint-resumable HedgeDesk pipeline orchestrator connecting:
sources.py -> book.py -> policy.py -> alternatives.py -> actions.py -> backtest.py.
"""
import json
from typing import Any, Dict, List, Optional

from actions import (
    get_outbox,
    make_forward_idem_key,
    make_payment_idem_key,
    make_rfq_idem_key,
    propose,
)
from alternatives import find_makers
from book import build_book, load_invoices
from policy import decide
from sources import (
    db_conn,
    fetch_fx,
    get_all_steps,
    get_run_cost_summary,
    get_run_evidence,
    get_step,
    load_policy_yaml,
    save_step,
)


class KillOnStageException(RuntimeError):
    """Raised intentionally when simulate_kill_at_step triggers for crash-recovery testing."""


def run_pipeline(
    run_id: str,
    invoices_input: Any,
    simulate_kill_at_step: Optional[str] = None,
    simulate_fx_down: bool = False,
    simulate_fx_stale: bool = False,
    simulate_fx_nonsense: bool = False,
    simulate_fx_disagreement: bool = False,
    simulate_402: bool = False,
    cost_limit_credits: Optional[float] = None,
    as_of_date: Optional[str] = None,
) -> Dict[str, Any]:
    cfg = load_policy_yaml()
    policy_version = cfg["policy_version"]
    logs: List[str] = []

    raw_for_resume = load_invoices(invoices_input)
    save_step(run_id, "0_request", "completed", raw_for_resume)

    def check_kill(step_name: str):
        if simulate_kill_at_step == step_name:
            raise KillOnStageException(f"Process killed cleanly after step '{step_name}' on run_id='{run_id}'.")

    # STEP 1: Load & Build Exposure Book (Deduped + Crustdata Supplier Verification)
    s1 = get_step(run_id, "1_book")
    if s1 and s1.get("output_data"):
        book = s1["output_data"]
        logs.append("Step 1 (book.build_book): Restored from SQLite `steps` checkpoint.")
    else:
        raw = raw_for_resume
        book = build_book(
            raw,
            run_id=run_id,
            as_of_date=as_of_date,
            cost_limit_credits=cost_limit_credits,
            simulate_402=simulate_402,
        )
        save_step(run_id, "1_book", "completed", book)
        logs.append(f"Step 1 (book.build_book): Resolved & deduped {len(book['invoices'])} invoice lines.")
    check_kill("1_book")

    # STEP 2: Fetch Live Official FX Market Data (RBI/FBIL/ECB + Sanity & Freshness Gates)
    s2 = get_step(run_id, "2_market")
    if s2 and s2.get("output_data"):
        market = s2["output_data"]
        logs.append("Step 2 (sources.fetch_fx): Restored from SQLite `steps` checkpoint.")
    else:
        market = fetch_fx(
            as_of_date=as_of_date,
            run_id=run_id,
            simulate_down=simulate_fx_down,
            simulate_stale=simulate_fx_stale,
            simulate_nonsense=simulate_fx_nonsense,
            simulate_disagreement=simulate_fx_disagreement,
        )
        save_step(run_id, "2_market", "completed", market)
        logs.append(f"Step 2 (sources.fetch_fx): status={market['status']}, spot={market.get('spot')}.")
    check_kill("2_market")

    # STEP 3: Pure Deterministic Hedging Policy Decisions
    s3 = get_step(run_id, "3_policy")
    if s3 and s3.get("output_data") is not None:
        decisions = s3["output_data"]
        logs.append("Step 3 (policy.decide): Restored from SQLite `steps` checkpoint.")
    else:
        decisions = decide(book=book, market=market, policy_cfg=cfg, as_of_date=as_of_date)
        with db_conn() as conn:
            conn.execute("DELETE FROM decisions WHERE run_id = ?", (run_id,))
            for d in decisions:
                conn.execute(
                    """
                    INSERT INTO decisions (
                        run_id, bucket, invoice_id, ratio, var, fwd_cost, risk_flag,
                        policy_clause, evidence_ids, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        d["bucket"],
                        d["invoice_id"],
                        d["ratio"],
                        d["var_95_inr"],
                        d["fwd_cost_inr"],
                        d["risk_flag"],
                        d["policy_clause"],
                        json.dumps(d["evidence_ids"]),
                        json.dumps(d),
                    ),
                )
        save_step(run_id, "3_policy", "completed", decisions)
        logs.append(f"Step 3 (policy.decide): Computed {len(decisions)} policy decisions.")
    check_kill("3_policy")

    # STEP 4: Domestic Indian Manufacturers & Break-Even Price (`alternatives.py`)
    s4 = get_step(run_id, "4_alternatives")
    if s4 and s4.get("output_data") is not None:
        alternatives_map = s4["output_data"]
        logs.append("Step 4 (alternatives.find_makers): Restored from SQLite `steps` checkpoint.")
    else:
        alternatives_map: Dict[str, Any] = {}
        dec_by_id = {d["invoice_id"]: d for d in decisions}
        for inv in book["invoices"]:
            inv_id = inv["invoice_id"]
            dec = dec_by_id[inv_id]
            if dec["action"] not in {"HEDGE_FORWARD", "ROLL_EXISTING_FORWARD"}:
                alternatives_map[inv_id] = {"status": "SKIPPED", "makers": [], "traders_excluded": []}
                continue
            unit_usd = inv["amount_usd"] / max(1, inv["quantity_units"])
            eff_fx = round(
                dec["ratio"] * (dec["forward_rate"] or dec["spot_rate"])
                + (1.0 - dec["ratio"]) * dec["spot_rate"],
                4,
            )
            alt = find_makers(
                part_query=inv["domestic_query"],
                unit_price_usd=unit_usd,
                quantity_units=inv["quantity_units"],
                effective_fx_rate=eff_fx,
                duty_pct=inv["duty_pct"],
                freight_pct=inv["freight_pct"],
                due_date=inv["due_date"],
                part_description=inv["part_description"],
                run_id=run_id,
                cost_limit_credits=cost_limit_credits,
            )
            alternatives_map[inv_id] = alt
        save_step(run_id, "4_alternatives", "completed", alternatives_map)
        logs.append("Step 4 (alternatives.find_makers): Audited Indian makers vs traders & computed break-even INR.")
    check_kill("4_alternatives")

    # STEP 5: Propose Idempotent Actions into Outbox (`actions.py`)
    s5 = get_step(run_id, "5_outbox")
    if s5 and s5.get("output_data") is not None:
        logs.append("Step 5 (actions.propose): Restored from SQLite `steps` checkpoint.")
    else:
        dec_by_id = {d["invoice_id"]: d for d in decisions}
        for inv in book["invoices"]:
            inv_id = inv["invoice_id"]
            dec = dec_by_id[inv_id]
            sup = inv.get("supplier") or {}
            spot_val = float(market.get("spot") or 92.68)

            # Determine if action is cleanly actionable or HELD
            hold_reason = None
            if dec["action"] not in {"HEDGE_FORWARD", "ROLL_EXISTING_FORWARD", "PAY_AT_SPOT_NO_FORWARD"}:
                hold_reason = dec["reason"]
            elif "BANK_CHANGE_FRAUD_HOLD" in inv.get("flags", []):
                hold_reason = inv["bank_change_check"]["reason"]
            elif sup.get("status") == "CANNOT_VERIFY":
                hold_reason = "Supplier cannot verify on Crustdata; payment held for human sign-off."

            # 1. Forward booking / Roll proposal (only when ratio > 0 or held)
            if dec["ratio"] > 0 or hold_reason:
                fkey = make_forward_idem_key(inv_id, inv.get("tenor_days") or 0, dec["hedged_usd"], policy_version)
                propose(
                    run_id=run_id,
                    kind="forward",
                    invoice_id=inv_id,
                    idem_key=fkey,
                    payload={
                        "title": (
                            f"{'Roll' if dec['action']=='ROLL_EXISTING_FORWARD' else 'Book'} "
                            f"{int(round(dec['ratio']*100))}% Forward (${dec['hedged_usd']:,.2f} @ ₹{dec.get('forward_rate') or 'N/A'})"
                        ),
                        "supplier": sup.get("legal_name") or inv["supplier_raw"],
                        "usd_amount": dec["hedged_usd"],
                        "inr_value": round(dec["hedged_usd"] * (dec.get("forward_rate") or spot_val), 2),
                        "due_date": inv["due_date"],
                        "roll_cost_inr": dec.get("roll_cost_inr", 0.0),
                        "policy_clause": dec["policy_clause"],
                    },
                    held_reason=hold_reason if dec["action"] not in {"HEDGE_FORWARD", "ROLL_EXISTING_FORWARD"} else None,
                )

            # 2. Supplier Payment proposal (Razorpay test mode — masked bank metadata)
            pkey = make_payment_idem_key(inv_id, inv["amount_usd"], inv["due_date"] or "missing")
            pay_hold = hold_reason
            propose(
                run_id=run_id,
                kind="payment",
                invoice_id=inv_id,
                idem_key=pkey,
                payload={
                    "title": f"Razorpay Test Payout (${inv['amount_usd']:,.2f} to {sup.get('legal_name') or inv['supplier_raw']})",
                    "supplier": sup.get("legal_name") or inv["supplier_raw"],
                    "usd_amount": inv["amount_usd"],
                    "inr_value": round(inv["amount_usd"] * spot_val, 2),
                    "due_date": inv["due_date"],
                },
                held_reason=pay_hold,
            )

            # 3. Domestic RFQ proposal (to team inbox, top verified Maker)
            alt = alternatives_map.get(inv_id) or {}
            makers = alt.get("makers") or []
            if makers and dec["action"] in {"HEDGE_FORWARD", "ROLL_EXISTING_FORWARD"}:
                m0 = makers[0]
                rkey = make_rfq_idem_key(inv_id, m0.get("crustdata_company_id") or m0["name"])
                propose(
                    run_id=run_id,
                    kind="rfq",
                    invoice_id=inv_id,
                    idem_key=rkey,
                    payload={
                        "title": f"Team-Inbox RFQ Draft to {m0['name']} (Break-even <= ₹{alt['break_even_inr']:,.2f}/unit)",
                        "maker_name": m0["name"],
                        "inr_value": 0.0,
                        "rfq_draft": m0["rfq_draft"],
                    },
                )

        save_step(run_id, "5_outbox", "completed", {"outbox_count": len(get_outbox(run_id))})
        logs.append(f"Step 5 (actions.propose): Queued {len(get_outbox(run_id))} idempotent outbox items.")

    costs = get_run_cost_summary(run_id)
    ev = get_run_evidence(run_id)
    outbox = get_outbox(run_id)

    total_usd = sum(d["hedged_usd"] for d in decisions)
    total_var = sum(d["var_95_inr"] for d in decisions)
    total_fwd_cost = sum(d["fwd_cost_inr"] for d in decisions)
    sub_inr = float(cfg["cost_and_landed_assumptions"]["monthly_subscription_inr"])
    advisor_inr = float(cfg["cost_and_landed_assumptions"]["manual_advisor_fee_inr"])

    return {
        "run_id": run_id,
        "client_name": cfg["client_name"],
        "policy_version": policy_version,
        "steps": get_all_steps(run_id),
        "step_logs": logs,
        "book": book,
        "market": market,
        "decisions": decisions,
        "alternatives": alternatives_map,
        "outbox": outbox,
        "evidence": ev,
        "cost_ledger": costs,
        "summary": {
            "total_invoices": len(book["invoices"]),
            "total_hedged_usd": round(total_usd, 2),
            "total_var_95_inr": round(total_var, 2),
            "total_fwd_cost_inr": round(total_fwd_cost, 2),
            "measured_credits": costs["total_credits"],
            "measured_job_cost_inr": costs["total_inr_cost"],
            "subscription_price_inr": sub_inr,
            "manual_advisor_fee_inr": advisor_inr,
            "gross_margin_pct": round((1.0 - costs["total_inr_cost"] / max(1.0, sub_inr)) * 100.0, 2),
        },
    }
