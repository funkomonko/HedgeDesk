"""
book.py — Parse invoices/POs (CSV/JSON/messy text), normalize amounts (e.g., "USD 1.2 lakh"),
resolve suppliers via Crustdata Identify/Enrich, dedupe exact & fuzzy spelling matches,
verify bank-change requests via Crustdata Person Search, and bucket into confirmed vs forecast exposures.
Handles Edge Cases 1*, 2*, 3, 4, 5, 6, 7, 8, 9, 10, 11*, 12, 13, 14*.
"""
import csv
import io
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sources import (
    add_evidence,
    crust,
    db_conn,
    load_policy_yaml,
    today_str,
)


def parse_amount_messy(raw_val: Any) -> Dict[str, Any]:
    """
    Case 7: Handle messy text, OCR quirks, commas, and 'USD 1.2 lakh' style mixes.
    Returns {amount, currency_override, confidence, note}.
    """
    if isinstance(raw_val, (int, float)):
        return {"amount": float(raw_val), "currency_override": None, "confidence": 1.0, "note": ""}

    s = str(raw_val or "").strip()
    if not s:
        return {"amount": 0.0, "currency_override": None, "confidence": 0.0, "note": "Empty amount"}

    cleaned = re.sub(r"(?<=\d)[Oo](?=[\d,.])", "0", s)
    cleaned = re.sub(r"(?<=[\d,.])[Oo]", "0", cleaned)
    curr_match = re.search(r"\b(USD|INR|EUR|CNY|GBP|JPY)\b|\$", cleaned, re.IGNORECASE)
    ccy = None
    if curr_match:
        tok = curr_match.group(0).upper()
        ccy = "USD" if tok == "$" else tok

    mult = 1.0
    conf = 1.0
    note = ""
    lower = cleaned.lower()
    if "lakh" in lower or "lac" in lower:
        mult = 100_000.0
        conf = 0.85
        note = f"Normalized Indian lakh notation from '{s}'"
    elif "crore" in lower or "cr" in lower.split():
        mult = 10_000_000.0
        conf = 0.85
        note = f"Normalized crore notation from '{s}'"

    num_m = re.search(r"[\d,]+(?:\.\d+)?", cleaned)
    if not num_m:
        return {"amount": 0.0, "currency_override": ccy, "confidence": 0.0, "note": f"Could not parse '{s}'"}

    val = float(num_m.group(0).replace(",", "")) * mult
    if note:
        return {"amount": round(val, 2), "currency_override": ccy, "confidence": conf, "note": note}
    return {"amount": round(val, 2), "currency_override": ccy, "confidence": 1.0, "note": ""}


def load_invoices(source: Any) -> List[Dict[str, Any]]:
    """Load invoices from CSV path, CSV string, or list of dicts."""
    rows: List[Dict[str, Any]] = []
    if isinstance(source, (str, Path)) and Path(str(source)).exists():
        with open(str(source), "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    elif isinstance(source, str):
        rows = list(csv.DictReader(io.StringIO(source)))
    elif isinstance(source, list):
        rows = [dict(r) for r in source]

    normalized: List[Dict[str, Any]] = []
    for idx, r in enumerate(rows):
        if "amount_usd" in r and "parse_confidence" in r:
            normalized.append(dict(r))
            continue
        inv_id = str(r.get("invoice_id") or f"INV-{idx+1}").strip()
        raw_amt = next((r[k] for k in ("amount_raw", "amount", "amount_usd") if r.get(k) not in (None, "")), 0)
        parsed_amt = parse_amount_messy(raw_amt)
        ccy = (parsed_amt["currency_override"] or str(r.get("currency") or "USD")).upper().strip()
        doc_type = str(r.get("doc_type") or "confirmed").lower().strip()
        if doc_type not in {"confirmed", "forecast"}:
            doc_type = "confirmed"

        normalized.append({
            "invoice_id": inv_id,
            "doc_type": doc_type,
            "supplier_raw": str(r.get("supplier_raw") or r.get("supplier_name") or "").strip(),
            "supplier_domain": str(r.get("supplier_domain") or "").strip().lower(),
            "currency": ccy,
            "amount_usd": parsed_amt["amount"],
            "parse_confidence": parsed_amt["confidence"],
            "parse_note": parsed_amt["note"],
            "quantity_units": max(1, int(float(r.get("quantity_units") or 1))),
            "part_description": str(r.get("part_description") or "industrial components").strip(),
            "domestic_query": str(r.get("domestic_query") or r.get("part_description") or "industrial manufacturer").strip(),
            "due_date": str(r.get("due_date") or "").strip(),
            "previous_due_date": str(r.get("previous_due_date") or "").strip(),
            "duty_pct": float(r.get("duty_pct") or 7.5),
            "freight_pct": float(r.get("freight_pct") or 3.2),
            "bank_change_sender": str(r.get("bank_change_sender") or "").strip(),
            "bank_change_email": str(r.get("bank_change_email") or "").strip(),
            "cancelled": str(r.get("cancelled") or "").lower() in {"true", "1", "yes"},
            "reduced_from_usd": float(r.get("reduced_from_usd") or 0.0) if r.get("reduced_from_usd") else None,
        })
    return normalized


def verify_bank_change_email(
    supplier_domain: str,
    sender_name: str,
    sender_email: str,
    run_id: str = "default",
) -> Dict[str, Any]:
    """
    Case 14*: Email asking to change a supplier's bank details -> hold payment & check sender
    against Crustdata /person/search at that supplier; never auto-pay to new details.
    """
    if not sender_name and not sender_email:
        return {"has_bank_change": False}

    domain_match = bool(supplier_domain and sender_email and sender_email.lower().endswith("@" + supplier_domain.lower()))
    search_body = {
        "filters": {
            "op": "and",
            "conditions": [
                {"field": "experience.employment_details.company_domains", "type": "in", "value": [supplier_domain or "unknown.invalid"]},
                {"field": "basic_profile.name", "type": "contains", "value": sender_name.split("(")[0].strip()},
            ],
        },
        "limit": 3,
    }
    res = crust("/person/search", search_body, run_id=run_id)
    profiles = ((res.get("data") or {}).get("profiles") or []) if res.get("ok") else []

    ev_id = add_evidence(
        run_id=run_id,
        source="https://api.crustdata.com/person/search",
        field_path="profiles[].basic_profile",
        value=f"Bank-change sender '{sender_name}' ({sender_email}): matched_profiles={len(profiles)}, domain_match={domain_match}",
        record_date=today_str(),
    )
    return {
        "has_bank_change": True,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "domain_match": domain_match,
        "crustdata_profiles_found": len(profiles),
        "verdict": "HOLD_BANK_CHANGE_FRAUD_CHECK",
        "reason": (
            f"Bank-detail change requested by '{sender_name}' ({sender_email}). "
            f"Crustdata Person Search found {len(profiles)} match(es) at {supplier_domain or 'unknown'}, domain_match={domain_match}. "
            f"Payment HELD for human voice-callback verification; never auto-paid to new account."
        ),
        "evidence_id": ev_id,
    }


def resolve_supplier(
    supplier_raw: str,
    supplier_domain: str = "",
    run_id: str = "default",
    cost_limit_credits: Optional[float] = None,
    simulate_402: bool = False,
) -> Dict[str, Any]:
    """
    Resolve supplier via Crustdata /company/identify + /company/enrich.
    Detects:
      - Exact match -> one canonical crustdata_company_id (Case 2*)
      - Lookalike / weak confidence -> 'LOOKALIKE_HOLD' (Case 11*)
      - Not found -> 'CANNOT_VERIFY' (Case 12)
      - Headcount dropping sharply / very new company -> risk_flag = 'high' (Case 13)
    """
    cfg = load_policy_yaml()["supplier_risk_rules"]
    min_conf = float(cfg["min_identify_confidence"])
    hc_drop_thresh = float(cfg["headcount_drop_high_risk_pct"])
    new_year_thresh = int(cfg["new_company_year_threshold"])

    ident_body: Dict[str, Any] = {"count": 3}
    if supplier_domain:
        ident_body["domains"] = [supplier_domain]
    else:
        ident_body["names"] = [supplier_raw]

    ident_res = crust(
        "/company/identify",
        ident_body,
        run_id=run_id,
        cost_limit_credits=cost_limit_credits,
        simulate_status=402 if simulate_402 else None,
    )
    if not ident_res.get("ok"):
        return {
            "status": "CANNOT_VERIFY",
            "supplier_id": None,
            "legal_name": supplier_raw,
            "primary_domain": supplier_domain,
            "risk_flag": "high",
            "reason": ident_res.get("message") or ident_res.get("error") or "Identify failed",
            "evidence_ids": [ident_res["evidence_id"]] if ident_res.get("evidence_id") else [],
            "skipped_due_to_budget": ident_res.get("status_code") == 402,
        }

    raw_list = ident_res.get("data") or []
    matches = raw_list[0].get("matches") if (isinstance(raw_list, list) and raw_list) else []
    if not matches:
        ev_id = add_evidence(run_id, "https://api.crustdata.com/company/identify", "matches", f"0 matches for '{supplier_raw}'", today_str())
        return {
            "status": "CANNOT_VERIFY",
            "supplier_id": None,
            "legal_name": supplier_raw,
            "primary_domain": supplier_domain,
            "risk_flag": "high",
            "reason": f"Supplier '{supplier_raw}' not found on Crustdata.",
            "evidence_ids": [ev_id],
        }

    top = matches[0]
    conf = float(top.get("confidence_score") or 0.0)
    cdata = top.get("company_data") or {}
    binfo = cdata.get("basic_info") or {}
    cid = str(cdata.get("crustdata_company_id") or binfo.get("crustdata_company_id") or "")
    matched_name = binfo.get("name") or supplier_raw
    matched_domain = binfo.get("primary_domain") or supplier_domain

    stop = {"ltd", "inc", "corp", "co", "pvt", "gmbh", "llc", "company", "global", "trading"}
    in_toks = {t for t in re.sub(r"[^a-z0-9 ]", "", supplier_raw.lower()).split() if len(t) > 1 and t not in stop}
    out_toks = {t for t in re.sub(r"[^a-z0-9 ]", "", matched_name.lower()).split() if len(t) > 1 and t not in stop}
    overlap = len(in_toks & out_toks) / max(1, len(in_toks))

    # Case 12: Completely unrelated fuzzy hit with bogus domain or zero token overlap -> CANNOT_VERIFY
    if supplier_domain and ".invalid" in supplier_domain:
        ev_id = add_evidence(run_id, "https://api.crustdata.com/company/identify", "matches", f"Unresolvable domain '{supplier_domain}'", today_str())
        return {
            "status": "CANNOT_VERIFY",
            "supplier_id": None,
            "legal_name": supplier_raw,
            "primary_domain": supplier_domain,
            "risk_flag": "high",
            "reason": f"Supplier '{supplier_raw}' not found on Crustdata.",
            "evidence_ids": [ev_id],
        }

    if not supplier_domain and (conf < min_conf or overlap < 0.5 or (len(matches) >= 2 and float(matches[1].get("confidence_score") or 0) >= conf - 0.05)):
        ev_id = add_evidence(
            run_id,
            "https://api.crustdata.com/company/identify",
            "confidence_score / matches",
            f"LOOKALIKE_HOLD: '{supplier_raw}' matched '{matched_name}' (conf={conf:.2f}, overlap={overlap:.2f})",
            today_str(),
        )
        return {
            "status": "LOOKALIKE_HOLD",
            "supplier_id": cid or None,
            "legal_name": matched_name,
            "primary_domain": matched_domain,
            "confidence": conf,
            "risk_flag": "high",
            "reason": f"Lookalike supplier name: '{supplier_raw}' resolved weakly to '{matched_name}' (conf={conf:.2f}, overlap={overlap:.2f}). Hedge and payment HELD.",
            "evidence_ids": [ev_id],
        }

    enrich_body: Dict[str, Any] = {"fields": ["basic_info", "headcount", "locations"]}
    if matched_domain:
        enrich_body["domains"] = [matched_domain]
    else:
        enrich_body["crustdata_company_ids"] = [int(cid)]

    enr_res = crust("/company/enrich", enrich_body, run_id=run_id, cost_limit_credits=cost_limit_credits, simulate_status=402 if simulate_402 else None)
    ev_ids = [ident_res["evidence_id"]] if ident_res.get("evidence_id") else []
    hc_total = None
    hc_growth_pct = None
    year_founded = binfo.get("year_founded")
    country = "Unknown"
    operating_status = (binfo.get("status") or "active").lower()

    if enr_res.get("ok"):
        if enr_res.get("evidence_id"):
            ev_ids.append(enr_res["evidence_id"])
        elist = enr_res.get("data") or []
        if isinstance(elist, list) and elist and (elist[0].get("matches") or []):
            ecd = elist[0]["matches"][0].get("company_data") or cdata
            eb = ecd.get("basic_info") or binfo
            ehc = ecd.get("headcount") or {}
            eloc = ecd.get("locations") or {}
            matched_name = eb.get("name") or matched_name
            matched_domain = eb.get("primary_domain") or matched_domain
            year_founded = eb.get("year_founded") or year_founded
            operating_status = (eb.get("status") or "active").lower()
            hc_total = ehc.get("total")
            country = eloc.get("country") or ehc.get("largest_headcount_country") or "Global"
            ts = ehc.get("timeseries") or []
            if len(ts) >= 26:
                try:
                    c_now = float(ts[-1]["employee_count"])
                    c_6m = float(ts[-26]["employee_count"])
                    if c_6m > 0:
                        hc_growth_pct = round(((c_now - c_6m) / c_6m) * 100.0, 1)
                except Exception:
                    pass

    risk_flag = "low"
    risk_reasons = []
    if year_founded and int(year_founded) >= new_year_thresh:
        risk_flag = "high"
        risk_reasons.append(f"Very new entity (founded {year_founded} >= {new_year_thresh})")
    if hc_growth_pct is not None and hc_growth_pct <= hc_drop_thresh:
        risk_flag = "high"
        risk_reasons.append(f"Headcount dropped sharply ({hc_growth_pct:+.1f}% over 6m)")
    if hc_total is not None and hc_total < int(cfg["min_verified_headcount"]):
        risk_flag = "high"
        risk_reasons.append(f"Small footprint ({hc_total} tracked employees)")
    if operating_status != "active":
        risk_flag = "high"
        risk_reasons.append(f"Status={operating_status}")

    ev_kyb = add_evidence(
        run_id,
        "https://api.crustdata.com/company/enrich",
        "basic_info & headcount",
        f"{matched_name} (ID={cid}, emp={hc_total}, 6m_trend={hc_growth_pct}%, founded={year_founded}, risk={risk_flag})",
        enr_res.get("fetched_at", today_str())[:10],
    )
    ev_ids.append(ev_kyb)

    return {
        "status": "VERIFIED",
        "supplier_id": cid,
        "legal_name": matched_name,
        "primary_domain": matched_domain,
        "country": country,
        "headcount": hc_total,
        "headcount_growth_6m_pct": hc_growth_pct,
        "year_founded": year_founded,
        "confidence": conf,
        "risk_flag": risk_flag,
        "reason": "; ".join(risk_reasons) if risk_reasons else f"Active verified supplier ({hc_total or 'N/A'} employees, risk={risk_flag})",
        "evidence_ids": ev_ids,
    }


def dedupe(resolved_invoices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Cases 1*, 2*, 3:
    - Same invoice_id or same supplier_id + amount + due_date with formatting variation -> linked as dup_of.
    - Two genuine invoices with identical amount + due_date from same supplier BUT distinct valid invoice numbers
      -> NOT auto-merged; flagged as 'CONFIRM_POSSIBLE_DUPLICATE' for a person (Case 3).
    """
    seen_exact_id: Dict[str, str] = {}
    seen_entity_amt_date: Dict[str, Dict[str, Any]] = {}

    for inv in resolved_invoices:
        inv_id = inv["invoice_id"]
        sup_key = inv.get("supplier_id") or re.sub(r"[^a-z0-9]", "", inv["supplier_raw"].lower())
        base_id_norm = re.sub(r"(-dup|-reissue|-copy|_dup)$", "", inv_id.lower())
        sig = f"{sup_key}|{inv['currency']}|{inv['amount_usd']:.2f}|{inv['due_date']}"

        inv["dup_of"] = None
        inv["dedupe_status"] = "UNIQUE"

        if inv_id.lower() in seen_exact_id:
            inv["dup_of"] = seen_exact_id[inv_id.lower()]
            inv["dedupe_status"] = "EXACT_DUPLICATE"
            continue

        if sig in seen_entity_amt_date:
            orig = seen_entity_amt_date[sig]
            orig_base = re.sub(r"(-dup|-reissue|-copy|_dup)$", "", orig["invoice_id"].lower())
            # Case 2*: Same supplier resolved to same Crustdata ID under different spelling/case/punctuation
            if base_id_norm == orig_base or (inv["supplier_raw"] != orig["supplier_raw"]):
                inv["dup_of"] = orig["invoice_id"]
                inv["dedupe_status"] = "FUZZY_OR_REUPLOAD_DUPLICATE"
            else:
                inv["dup_of"] = orig["invoice_id"]
                inv["dedupe_status"] = "CONFIRM_DISTINCT_INVOICE"
            continue

        seen_exact_id[inv_id.lower()] = inv_id
        seen_entity_amt_date[sig] = inv

    return resolved_invoices


def build_book(
    raw_invoices: List[Dict[str, Any]],
    run_id: str = "default",
    as_of_date: Optional[str] = None,
    cost_limit_credits: Optional[float] = None,
    simulate_402: bool = False,
) -> Dict[str, Any]:
    """Resolve suppliers, verify bank-change alerts, dedupe, validate currencies/dates, and bucket by due month."""
    ref_date_str = as_of_date or today_str()
    try:
        ref_date = datetime.strptime(ref_date_str[:10], "%Y-%m-%d").date()
    except Exception:
        ref_date = datetime.strptime(today_str(), "%Y-%m-%d").date()

    enriched_rows: List[Dict[str, Any]] = []
    for inv in raw_invoices:
        sup = resolve_supplier(
            inv["supplier_raw"],
            inv.get("supplier_domain", ""),
            run_id=run_id,
            cost_limit_credits=cost_limit_credits,
            simulate_402=simulate_402,
        )
        bank_chk = verify_bank_change_email(
            sup.get("primary_domain") or inv.get("supplier_domain", ""),
            inv.get("bank_change_sender", ""),
            inv.get("bank_change_email", ""),
            run_id=run_id,
        )

        ccy = inv["currency"]
        due_str = inv["due_date"]
        flags: List[str] = []
        tenor_days: Optional[int] = None
        roll_days = 0
        bucket = "UNASSIGNED"

        if ccy == "INR":
            flags.append("NO_FX_EXPOSURE_INR")
        elif ccy != "USD":
            flags.append(f"OUT_OF_SCOPE_CURRENCY_{ccy}")

        if not due_str:
            flags.append("MISSING_DUE_DATE")
        else:
            try:
                due_dt = datetime.strptime(due_str[:10], "%Y-%m-%d").date()
                tenor_days = (due_dt - ref_date).days
                bucket = f"{due_str[:7]}_{inv['doc_type']}"
                if tenor_days <= 0:
                    flags.append("OVERDUE_SPOT_EXPOSURE")
                elif tenor_days > 180:
                    flags.append("TENOR_BEYOND_6M")
                if inv.get("previous_due_date"):
                    prev_dt = datetime.strptime(inv["previous_due_date"][:10], "%Y-%m-%d").date()
                    roll_days = max(0, (due_dt - prev_dt).days)
                    if roll_days > 0:
                        flags.append(f"MOVED_DUE_DATE_ROLL_{roll_days}D")
            except Exception:
                flags.append("INVALID_DUE_DATE")

        if bank_chk.get("has_bank_change"):
            flags.append("BANK_CHANGE_FRAUD_HOLD")
        if inv.get("parse_confidence", 1.0) < 0.95:
            flags.append("OCR_NORMALIZED_CONFIRM")

        row = dict(inv)
        row.update({
            "supplier": sup,
            "supplier_id": sup.get("supplier_id"),
            "bank_change_check": bank_chk,
            "tenor_days": tenor_days,
            "roll_days": roll_days,
            "bucket": bucket,
            "flags": flags,
        })
        enriched_rows.append(row)

    deduped = dedupe(enriched_rows)

    with db_conn() as conn:
        for inv in deduped:
            status = "ACTIVE"
            if inv["dup_of"] and inv["dedupe_status"] != "CONFIRM_DISTINCT_INVOICE":
                status = "DUPLICATE_LINKED"
            elif inv["dedupe_status"] == "CONFIRM_DISTINCT_INVOICE":
                status = "CONFIRM_POSSIBLE_DUPLICATE"
            elif inv["supplier"]["status"] in {"LOOKALIKE_HOLD", "CANNOT_VERIFY"}:
                status = inv["supplier"]["status"]
            elif "MISSING_DUE_DATE" in inv["flags"]:
                status = "HOLD_MISSING_DATE"

            inv["book_status"] = status
            conn.execute(
                """
                INSERT OR REPLACE INTO invoices (
                    invoice_id, run_id, supplier_raw, supplier_id, amount_usd,
                    due_date, doc_type, status, dup_of, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inv["invoice_id"],
                    run_id,
                    inv["supplier_raw"],
                    inv["supplier_id"],
                    inv["amount_usd"],
                    inv["due_date"],
                    inv["doc_type"],
                    status,
                    inv["dup_of"],
                    json.dumps(inv),
                ),
            )

    buckets_summary: Dict[str, Dict[str, Any]] = {}
    for inv in deduped:
        if inv["dup_of"] or inv["currency"] != "USD" or inv["tenor_days"] is None:
            continue
        bkey = inv["bucket"]
        if bkey not in buckets_summary:
            buckets_summary[bkey] = {
                "bucket": bkey,
                "doc_type": inv["doc_type"],
                "total_usd": 0.0,
                "invoice_ids": [],
                "avg_tenor_days": inv["tenor_days"],
            }
        buckets_summary[bkey]["total_usd"] += inv["amount_usd"]
        buckets_summary[bkey]["invoice_ids"].append(inv["invoice_id"])

    return {
        "run_id": run_id,
        "as_of_date": ref_date_str,
        "invoices": deduped,
        "buckets": buckets_summary,
    }
