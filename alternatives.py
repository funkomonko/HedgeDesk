"""
alternatives.py — Domestic import-substitution comparison using Crustdata /company/search.
Implements:
  - find_makers() with Maker vs Trader check (Case 15)
  - No domestic candidate handled cleanly (Case 16)
  - Private domestic price always marked 'cannot verify, RFQ drafted' + break-even (Case 17)
  - landed_cost() & break_even()
"""
from typing import Any, Dict, List, Optional

from sources import add_evidence, crust, load_policy_yaml, today_str


def landed_cost(
    unit_price_usd: float,
    effective_fx_rate: float,
    duty_pct: float = 7.5,
    freight_pct: float = 3.2,
) -> float:
    """Compute hedged landed import cost per unit in INR."""
    mult = 1.0 + (float(duty_pct) + float(freight_pct)) / 100.0
    return round(float(unit_price_usd) * float(effective_fx_rate) * mult, 2)


def break_even(landed_inr: float, gst_working_cap_adv_pct: float = 1.5) -> float:
    """Domestic unit price threshold below which buying Indian beats hedged import."""
    return round(float(landed_inr) * (1.0 - float(gst_working_cap_adv_pct) / 100.0), 2)


def classify_maker_vs_trader(company: Dict[str, Any]) -> Dict[str, Any]:
    """
    Case 15: Distinguish true Indian manufacturers from pure trading / import-export middlemen
    using Crustdata description, industry, and headcount signals.
    """
    bi = company.get("basic_info") or {}
    hc = company.get("headcount") or {}
    desc = (bi.get("description") or "").lower()
    name = (bi.get("name") or "").lower()
    headcount = int(hc.get("total") or 0)

    maker_signals = [
        "manufactur", "plant", "factory", "iso ", "oem", "precision",
        "engineering", "industrial", "bearings", "refrigeration", "automation",
        "forging", "machining", "production",
    ]
    trader_signals = [
        "wholesaler", "distributor", "trading", "traders", "import and export",
        "importer", "stockist", "dealer", "broker", "expo", "exhibition",
    ]
    maker_hits = sum(1 for kw in maker_signals if kw in desc or kw in name)
    trader_hits = sum(1 for kw in trader_signals if kw in desc or kw in name)

    is_event = any(w in desc for w in ["trade show", "exhibition", "expo ", "conference"])
    if is_event:
        kind = "EVENT_EXCLUDED"
        evidence_note = "Excluded: industry expo/association website, not a factory."
    elif (trader_hits >= 1 and maker_hits == 0) or (headcount < 40 and trader_hits > 0):
        kind = "TRADER"
        evidence_note = f"Classified TRADER (not maker): trader keywords={trader_hits}, maker keywords={maker_hits}, headcount={headcount}. Excluded from break-even unless human includes."
    else:
        kind = "MAKER"
        evidence_note = f"Verified MAKER: {headcount} employees, maker signals={maker_hits}, active Indian manufacturer."

    return {"kind": kind, "evidence_note": evidence_note, "headcount": headcount}


def find_makers(
    part_query: str,
    unit_price_usd: float,
    quantity_units: int,
    effective_fx_rate: float,
    duty_pct: float = 7.5,
    freight_pct: float = 3.2,
    due_date: str = "2027-01-21",
    part_description: str = "industrial part",
    run_id: str = "default",
    cost_limit_credits: Optional[float] = None,
    simulate_no_candidate: bool = False,
) -> Dict[str, Any]:
    """Search Crustdata for verified Indian makers and compute domestic break-even price."""
    cfg = load_policy_yaml()["cost_and_landed_assumptions"]
    landed = landed_cost(unit_price_usd, effective_fx_rate, duty_pct, freight_pct)
    be_price = break_even(landed, float(cfg["domestic_gst_working_cap_adv_pct"]))

    if simulate_no_candidate:
        ev_id = add_evidence(
            run_id, "https://api.crustdata.com/company/search", "companies",
            f"0 verified Indian makers found for '{part_query}'", today_str()
        )
        return {
            "status": "NO_DOMESTIC_CANDIDATE",
            "part_description": part_description,
            "landed_import_inr": landed,
            "break_even_inr": be_price,
            "makers": [],
            "traders_excluded": [],
            "note": "No domestic candidate found; import decision stands; no invented alternatives.",
            "evidence_ids": [ev_id],
        }

    body = {
        "search": {"query": f"{part_query} India", "mode": "hybrid"},
        "filters": {
            "op": "and",
            "conditions": [
                {"field": "locations.country", "type": "in", "value": ["India"]},
                {"field": "headcount.total", "type": "=>", "value": 40},
            ],
        },
        "limit": 5,
        "fields": ["crustdata_company_id", "basic_info", "headcount", "locations"],
    }
    res = crust("/company/search", body, run_id=run_id, cost_limit_credits=cost_limit_credits)
    if not res.get("ok"):
        return {
            "status": "CANNOT_VERIFY_DOMESTIC",
            "part_description": part_description,
            "landed_import_inr": landed,
            "break_even_inr": be_price,
            "makers": [],
            "traders_excluded": [],
            "note": res.get("message") or res.get("error") or "Domestic search unavailable",
            "evidence_ids": [res["evidence_id"]] if res.get("evidence_id") else [],
        }

    raw_cos = (res.get("data") or {}).get("companies") or []
    makers: List[Dict[str, Any]] = []
    traders: List[Dict[str, Any]] = []
    ev_ids: List[int] = [res["evidence_id"]] if res.get("evidence_id") else []

    for c in raw_cos:
        bi = c.get("basic_info") or {}
        loc = c.get("locations") or {}
        cls = classify_maker_vs_trader(c)
        if cls["kind"] == "EVENT_EXCLUDED":
            continue
        cid = c.get("crustdata_company_id") or bi.get("crustdata_company_id")
        item = {
            "crustdata_company_id": cid,
            "name": bi.get("name"),
            "primary_domain": bi.get("primary_domain"),
            "hq": loc.get("headquarters") or loc.get("city") or "India",
            "headcount": cls["headcount"],
            "classification": cls["kind"],
            "classification_evidence": cls["evidence_note"],
            "price_status": "cannot verify, RFQ drafted",
            "break_even_target_inr": be_price,
            "description": (bi.get("description") or "")[:220],
            "rfq_draft": (
                f"To: {bi.get('name')} Enterprise Sales (via Team Inbox — Test Mode)\n"
                f"Subject: RFQ for {quantity_units} units of {part_description} (Target <= INR {be_price:,.2f}/unit)\n\n"
                f"Dear {bi.get('name')} Team,\n"
                f"We are requesting a firm domestic INR unit quote for {quantity_units} units of {part_description} "
                f"for delivery before {due_date}. Our FX-hedged landed import benchmark is INR {landed:,.2f}/unit "
                f"(including {duty_pct}% duty + {freight_pct}% freight). Quotes <= INR {be_price:,.2f}/unit qualify for immediate PO.\n"
            ),
        }
        ev_id = add_evidence(
            run_id,
            f"https://api.crustdata.com/company/search (ID={cid})",
            "basic_info & headcount",
            f"{item['name']} | {cls['kind']} ({cls['headcount']} emp) | Price=cannot verify, RFQ drafted (BE <= ₹{be_price})",
            res.get("fetched_at", today_str())[:10],
        )
        item["evidence_id"] = ev_id
        ev_ids.append(ev_id)
        if cls["kind"] == "TRADER":
            traders.append(item)
        else:
            makers.append(item)

    if not makers and not traders:
        return {
            "status": "NO_DOMESTIC_CANDIDATE",
            "part_description": part_description,
            "landed_import_inr": landed,
            "break_even_inr": be_price,
            "makers": [],
            "traders_excluded": [],
            "note": "No domestic candidate found; import decision stands.",
            "evidence_ids": ev_ids,
        }

    return {
        "status": "SOURCED" if makers else "ONLY_TRADERS_FOUND",
        "part_description": part_description,
        "landed_import_inr": landed,
        "break_even_inr": be_price,
        "price_verdict": "cannot verify, RFQ drafted",
        "makers": makers,
        "traders_excluded": traders,
        "evidence_ids": ev_ids,
    }
