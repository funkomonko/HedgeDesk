"""
policy.py — Pure, zero-I/O, deterministic FX hedging decision engine.
Implements exact mathematical policy from Context Doc Page 7:
  VaR_95 = 1.645 * sigma_daily * sqrt(h) * S * Q
  F = S * (1 + p * d/365),   cost% = p * d/365
  s = clip((risk% / cost% - 1) / (k - 1), 0, 1),   ratio = lo + s * (hi - lo)
Includes strict point-in-time assert to guarantee zero future data leakage in backtesting.
"""
import math
from typing import Any, Dict, List, Optional, Tuple


def var_95(spot: float, amount_usd: float, sigma_daily: float, trading_days_h: int) -> float:
    """95% parametric Value-at-Risk in INR over trading-day horizon h."""
    if spot <= 0 or amount_usd <= 0 or trading_days_h <= 0:
        return 0.0
    return 1.645 * sigma_daily * math.sqrt(float(trading_days_h)) * spot * amount_usd


def fwd_cost(
    spot: float,
    annual_prem_pct: float,
    calendar_days_d: int,
    spread_bps: float = 18.0,
) -> Tuple[float, float]:
    """
    Returns (forward_rate_inr, cost_pct_fraction).
    Formula from Context Doc Page 7: F = S * (1 + p * d/365), cost% = p * d/365 (+ bank spread).
    """
    if spot <= 0 or calendar_days_d <= 0:
        return spot, 0.0
    p = annual_prem_pct / 100.0
    spread = spread_bps / 10_000.0
    cost_frac = p * (float(calendar_days_d) / 365.0) + spread
    fwd_rate = spot * (1.0 + cost_frac)
    return round(fwd_rate, 4), cost_frac


def get_tenor_bounds(tenor_days: int, doc_type: str, policy_cfg: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], str]:
    """Map calendar days + confirmed/forecast doc_type to policy range [lo, hi] and clause name."""
    ranges = policy_cfg["tenor_ranges"]
    dtype = "forecast" if doc_type == "forecast" else "confirmed"
    if 0 < tenor_days <= 30:
        lo, hi = ranges["0_to_1_month"][dtype]
        return float(lo), float(hi), f"0_to_1_month.{dtype}"
    if 31 <= tenor_days <= 90:
        lo, hi = ranges["1_to_3_months"][dtype]
        return float(lo), float(hi), f"1_to_3_months.{dtype}"
    if 91 <= tenor_days <= 180:
        lo, hi = ranges["3_to_6_months"][dtype]
        return float(lo), float(hi), f"3_to_6_months.{dtype}"
    return None, None, "OUTSIDE_6M_TENOR"


def pick_forward_premium_for_tenor(premia_pct: Dict[str, float], tenor_days: int) -> float:
    p1 = float(premia_pct.get("1m", 2.15))
    p3 = float(premia_pct.get("3m", 2.45))
    p6 = float(premia_pct.get("6m", 2.75))
    if tenor_days <= 30:
        return p1
    if tenor_days <= 90:
        return round(p1 + (p3 - p1) * ((tenor_days - 30) / 60.0), 3)
    return round(p3 + (p6 - p3) * min(1.0, (tenor_days - 90) / 90.0), 3)


def hedge_ratio(
    risk_pct: float,
    cost_pct: float,
    lo: float,
    hi: float,
    k: float = 3.0,
    risk_flag: str = "low",
    doc_type: str = "confirmed",
) -> Tuple[float, float]:
    """
    Context Doc Page 7-8 decision rule:
      s = clip((risk% / cost% - 1) / (k - 1), 0, 1)
      ratio = lo + s * (hi - lo)
    Supplier risk_flag == 'high' -> confirmed hedged at bottom `lo`, forecast held at `0.0`.
    Returns (ratio, s).
    """
    if risk_flag == "high":
        return (lo, 0.0) if doc_type == "confirmed" else (0.0, 0.0)

    if cost_pct <= 1e-8:
        s = 1.0
    else:
        raw_s = ((risk_pct / cost_pct) - 1.0) / max(0.01, (k - 1.0))
        s = max(0.0, min(1.0, raw_s))
    ratio = lo + s * (hi - lo)
    return round(min(1.0, max(0.0, ratio)), 4), round(s, 4)


def decide(
    book: Dict[str, Any],
    market: Dict[str, Any],
    policy_cfg: Dict[str, Any],
    as_of_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Pure deterministic policy evaluator over every invoice in `book`.
    Enforces strict point-in-time assertion when `as_of_date` is supplied (no future leakage!).
    """
    if as_of_date and market.get("record_date"):
        assert str(market["record_date"])[:10] <= str(as_of_date)[:10], (
            f"Point-in-time leakage detected: market rate {market['record_date']} > as_of {as_of_date}"
        )

    market_ok = market.get("status") == "VERIFIED" and market.get("spot") is not None
    spot = float(market.get("spot") or 0.0)
    sigma_daily = float(market.get("sigma_daily") or 0.0022)
    premia_pct = market.get("premia_pct") or {}
    k_val = float(policy_cfg["decision_rule"]["k"])
    spread_bps = float(policy_cfg["cost_and_landed_assumptions"]["bank_spread_bps"])

    decisions: List[Dict[str, Any]] = []
    for inv in book.get("invoices", []):
        inv_id = inv["invoice_id"]
        sup = inv.get("supplier") or {}
        sup_status = sup.get("status", "CANNOT_VERIFY")
        risk_flag = sup.get("risk_flag", "high")
        ev_ids = list(set((sup.get("evidence_ids") or []) + (market.get("evidence_ids") or [])))
        if inv.get("bank_change_check", {}).get("evidence_id"):
            ev_ids.append(inv["bank_change_check"]["evidence_id"])

        # 1. Duplicate checks (Cases 1*, 2*, 3)
        if inv.get("dup_of") and inv.get("dedupe_status") != "CONFIRM_DISTINCT_INVOICE":
            decisions.append({
                "invoice_id": inv_id,
                "bucket": inv.get("bucket", "DUPLICATE"),
                "action": "HOLD_DUPLICATE",
                "ratio": 0.0,
                "hedged_usd": 0.0,
                "var_95_inr": 0.0,
                "risk_pct": 0.0,
                "cost_pct": 0.0,
                "s_factor": 0.0,
                "forward_rate": None,
                "fwd_cost_inr": 0.0,
                "roll_cost_inr": 0.0,
                "risk_flag": risk_flag,
                "policy_clause": "HARD_RULE.NO_OVER_HEDGING_DUPLICATE",
                "evidence_ids": ev_ids,
                "reason": f"Duplicate linked to {inv['dup_of']} ({inv.get('dedupe_status')}); hedged once on original.",
            })
            continue

        if inv.get("dedupe_status") == "CONFIRM_DISTINCT_INVOICE":
            decisions.append({
                "invoice_id": inv_id,
                "bucket": inv.get("bucket", "CONFIRM_DUP"),
                "action": "HOLD_CONFIRM_DISTINCT",
                "ratio": 0.0,
                "hedged_usd": 0.0,
                "var_95_inr": 0.0,
                "risk_pct": 0.0,
                "cost_pct": 0.0,
                "s_factor": 0.0,
                "forward_rate": None,
                "fwd_cost_inr": 0.0,
                "roll_cost_inr": 0.0,
                "risk_flag": "medium",
                "policy_clause": "HARD_RULE.CONFIRM_IDENTICAL_AMOUNT_DATE",
                "evidence_ids": ev_ids,
                "reason": f"Distinct invoice number '{inv_id}' matches {inv['dup_of']} on supplier/amount/date; held for human confirmation (not auto-merged).",
            })
            continue

        # 2. Scope & Date checks (Cases 4, 5, 6, 8, 9)
        flags = inv.get("flags", [])
        if "NO_FX_EXPOSURE_INR" in flags:
            decisions.append({
                "invoice_id": inv_id,
                "bucket": "INR_DOMESTIC",
                "action": "NO_FX_EXPOSURE",
                "ratio": 0.0,
                "hedged_usd": 0.0,
                "var_95_inr": 0.0,
                "risk_pct": 0.0,
                "cost_pct": 0.0,
                "s_factor": 0.0,
                "forward_rate": None,
                "fwd_cost_inr": 0.0,
                "roll_cost_inr": 0.0,
                "risk_flag": "low",
                "policy_clause": "SCOPE.INR_INVOICE",
                "evidence_ids": ev_ids,
                "reason": "Invoice billed in INR from foreign vendor; zero USD/INR FX exposure.",
            })
            continue

        if any(f.startswith("OUT_OF_SCOPE_CURRENCY_") for f in flags):
            decisions.append({
                "invoice_id": inv_id,
                "bucket": inv.get("bucket", "NON_USD"),
                "action": "OUT_OF_SCOPE_FLAG",
                "ratio": 0.0,
                "hedged_usd": 0.0,
                "var_95_inr": 0.0,
                "risk_pct": 0.0,
                "cost_pct": 0.0,
                "s_factor": 0.0,
                "forward_rate": None,
                "fwd_cost_inr": 0.0,
                "roll_cost_inr": 0.0,
                "risk_flag": "medium",
                "policy_clause": "SCOPE.USD_ONLY_TODAY",
                "evidence_ids": ev_ids,
                "reason": f"Currency {inv['currency']} is outside today's USD-only policy scope; logged as unhedged exposure, no automated decision.",
            })
            continue

        # Missing amount or supplier (e.g. a typed request that never stated them): hold, never guess.
        missing_hold = None
        if "MISSING_AMOUNT" in flags:
            missing_hold = ("HOLD_MISSING_AMOUNT", "HOLD.MISSING_AMOUNT",
                            "Cannot verify: no amount with a currency was given; held and requesting the invoice amount from finance team.")
        elif "MISSING_SUPPLIER" in flags:
            missing_hold = ("HOLD_MISSING_SUPPLIER", "HOLD.MISSING_SUPPLIER",
                            "Cannot verify: no supplier named; held and requesting the payee from finance team.")
        if missing_hold:
            decisions.append({
                "invoice_id": inv_id,
                "bucket": inv.get("bucket", "MISSING_FIELDS"),
                "action": missing_hold[0],
                "ratio": 0.0,
                "hedged_usd": 0.0,
                "var_95_inr": 0.0,
                "risk_pct": 0.0,
                "cost_pct": 0.0,
                "s_factor": 0.0,
                "forward_rate": None,
                "fwd_cost_inr": 0.0,
                "roll_cost_inr": 0.0,
                "risk_flag": "high",
                "policy_clause": missing_hold[1],
                "evidence_ids": ev_ids,
                "reason": missing_hold[2],
            })
            continue

        if "MISSING_DUE_DATE" in flags or "INVALID_DUE_DATE" in flags:
            decisions.append({
                "invoice_id": inv_id,
                "bucket": "MISSING_DATE",
                "action": "CANNOT_DETERMINE_TENOR",
                "ratio": 0.0,
                "hedged_usd": 0.0,
                "var_95_inr": 0.0,
                "risk_pct": 0.0,
                "cost_pct": 0.0,
                "s_factor": 0.0,
                "forward_rate": None,
                "fwd_cost_inr": 0.0,
                "roll_cost_inr": 0.0,
                "risk_flag": risk_flag,
                "policy_clause": "HOLD.CANNOT_DETERMINE_TENOR",
                "evidence_ids": ev_ids,
                "reason": "Missing due date: cannot determine tenor; held and requesting due date from finance team.",
            })
            continue

        if "OVERDUE_SPOT_EXPOSURE" in flags:
            decisions.append({
                "invoice_id": inv_id,
                "bucket": "OVERDUE_SPOT",
                "action": "PAY_AT_SPOT_NO_FORWARD",
                "ratio": 0.0,
                "hedged_usd": 0.0,
                "var_95_inr": 0.0,
                "risk_pct": 0.0,
                "cost_pct": 0.0,
                "s_factor": 0.0,
                "forward_rate": spot if market_ok else None,
                "fwd_cost_inr": 0.0,
                "roll_cost_inr": 0.0,
                "risk_flag": risk_flag,
                "policy_clause": "LIFECYCLE.OVERDUE_SPOT_ONLY",
                "evidence_ids": ev_ids,
                "reason": f"Due date {inv['due_date']} is in the past ({inv['tenor_days']}d): spot exposure; recommend paying at spot today, no forward.",
            })
            continue

        if "TENOR_BEYOND_6M" in flags:
            decisions.append({
                "invoice_id": inv_id,
                "bucket": inv.get("bucket", "BEYOND_6M"),
                "action": "REFER_TO_PERSON_BEYOND_6M",
                "ratio": 0.0,
                "hedged_usd": 0.0,
                "var_95_inr": 0.0,
                "risk_pct": 0.0,
                "cost_pct": 0.0,
                "s_factor": 0.0,
                "forward_rate": None,
                "fwd_cost_inr": 0.0,
                "roll_cost_inr": 0.0,
                "risk_flag": risk_flag,
                "policy_clause": "HARD_RULE.MAX_TENOR_180D",
                "evidence_ids": ev_ids,
                "reason": f"Tenor {inv['tenor_days']}d exceeds 6-month policy maximum (180d); referred to CFO.",
            })
            continue

        # 3. Lookalike supplier hold (Case 11*)
        if sup_status == "LOOKALIKE_HOLD":
            decisions.append({
                "invoice_id": inv_id,
                "bucket": inv.get("bucket", "LOOKALIKE"),
                "action": "HOLD_CANNOT_VERIFY_SUPPLIER",
                "ratio": 0.0,
                "hedged_usd": 0.0,
                "var_95_inr": 0.0,
                "risk_pct": 0.0,
                "cost_pct": 0.0,
                "s_factor": 0.0,
                "forward_rate": None,
                "fwd_cost_inr": 0.0,
                "roll_cost_inr": 0.0,
                "risk_flag": "high",
                "policy_clause": "KYB.LOOKALIKE_HOLD",
                "evidence_ids": ev_ids,
                "reason": f"Cannot verify supplier (lookalike match): {sup.get('reason')}",
            })
            continue

        # 4. Market Data Unavailable / Stale Gate (Cases 18*, 19, 20)
        if not market_ok:
            decisions.append({
                "invoice_id": inv_id,
                "bucket": inv.get("bucket", "FX_HOLD"),
                "action": "HOLD_CANNOT_VERIFY_MARKET_DATA",
                "ratio": 0.0,
                "hedged_usd": 0.0,
                "var_95_inr": 0.0,
                "risk_pct": 0.0,
                "cost_pct": 0.0,
                "s_factor": 0.0,
                "forward_rate": None,
                "fwd_cost_inr": 0.0,
                "roll_cost_inr": 0.0,
                "risk_flag": risk_flag,
                "policy_clause": "HARD_RULE.FRESH_MARKET_DATA_REQUIRED",
                "evidence_ids": ev_ids,
                "reason": market.get("reason", "Cannot verify official FX rate."),
            })
            continue

        # 5. Lifecycle: Cancelled or Reduced Invoice
        if inv.get("cancelled"):
            decisions.append({
                "invoice_id": inv_id,
                "bucket": inv.get("bucket", "CANCELLED"),
                "action": "PROPOSE_CANCEL_FORWARD",
                "ratio": 0.0,
                "hedged_usd": 0.0,
                "var_95_inr": 0.0,
                "risk_pct": 0.0,
                "cost_pct": 0.0,
                "s_factor": 0.0,
                "forward_rate": spot,
                "fwd_cost_inr": 0.0,
                "roll_cost_inr": 0.0,
                "risk_flag": risk_flag,
                "policy_clause": "LIFECYCLE.CANCEL_OR_REDUCE",
                "evidence_ids": ev_ids,
                "reason": f"Invoice cancelled: propose unwinding underlying forward at current spot ₹{spot}.",
            })
            continue

        # 6. Quantitative Policy Calculation (Pages 7-8)
        tenor_d = int(inv["tenor_days"])
        trading_h = max(1, round(tenor_d * (252.0 / 365.0)))
        lo, hi, clause = get_tenor_bounds(tenor_d, inv["doc_type"], policy_cfg)
        if lo is None:
            continue

        # Case 12: Supplier not found on Crustdata -> confirmed invoice still hedges at bottom of range `lo`, payment held!
        effective_risk_flag = risk_flag
        note_extra = []
        if sup_status == "CANNOT_VERIFY":
            effective_risk_flag = "high"
            note_extra.append("Supplier not found on Crustdata ('cannot verify'): confirmed hedge capped at bottom of range; payment HELD for person.")

        q_usd = float(inv["amount_usd"])
        v95 = var_95(spot, q_usd, sigma_daily, trading_h)
        notional_inr = spot * q_usd
        risk_pct = (v95 / notional_inr) if notional_inr > 0 else 0.0

        ann_prem_pct = pick_forward_premium_for_tenor(premia_pct, tenor_d)
        fwd_rate, cost_frac = fwd_cost(spot, ann_prem_pct, tenor_d, spread_bps=spread_bps)

        ratio, s_val = hedge_ratio(
            risk_pct=risk_pct,
            cost_pct=cost_frac,
            lo=lo,
            hi=hi,
            k=k_val,
            risk_flag=effective_risk_flag,
            doc_type=inv["doc_type"],
        )
        hedged_usd = round(q_usd * ratio, 2)
        fwd_cost_inr = round(hedged_usd * (fwd_rate - spot), 2)

        # Case 27*: Forward roll if due date moved
        roll_days = int(inv.get("roll_days") or 0)
        roll_cost_inr = 0.0
        action_name = "HEDGE_FORWARD"
        if roll_days > 0:
            action_name = "ROLL_EXISTING_FORWARD"
            roll_rate_pts = spot * (ann_prem_pct / 100.0) * (roll_days / 365.0)
            roll_cost_inr = round(hedged_usd * roll_rate_pts, 2)
            note_extra.append(f"Due date moved +{roll_days}d (from {inv['previous_due_date']}): rolling existing forward (swap cost ₹{roll_cost_inr:,.2f}), never booking a second one.")

        if "BANK_CHANGE_FRAUD_HOLD" in flags:
            note_extra.append(inv["bank_change_check"]["reason"])

        decisions.append({
            "invoice_id": inv_id,
            "bucket": inv["bucket"],
            "action": action_name,
            "range_lo": lo,
            "range_hi": hi,
            "ratio": ratio,
            "hedged_usd": hedged_usd,
            "spot_rate": spot,
            "forward_rate": fwd_rate,
            "ann_prem_pct": ann_prem_pct,
            "var_95_inr": round(v95, 2),
            "risk_pct": round(risk_pct * 100.0, 3),
            "cost_pct": round(cost_frac * 100.0, 3),
            "s_factor": s_val,
            "fwd_cost_inr": fwd_cost_inr,
            "roll_cost_inr": roll_cost_inr,
            "risk_flag": effective_risk_flag,
            "policy_clause": clause,
            "evidence_ids": ev_ids,
            "reason": (
                f"Range [{int(lo*100)}%, {int(hi*100)}%] ({clause}) | "
                f"risk%={risk_pct*100:.2f}% vs cost%={cost_frac*100:.2f}% -> s={s_val:.2f} -> hedge {int(round(ratio*100))}% "
                f"(${hedged_usd:,.2f} @ ₹{fwd_rate}, indicative: modelled premium, bank quote needed). " + " ".join(note_extra)
            ).strip(),
        })

    return decisions
