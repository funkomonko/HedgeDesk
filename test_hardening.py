"""
test_hardening.py: checks for input and safety gaps found after screening. Separate from
test_28_cases.py, which stays the report for the 28 context-doc cases.

Covers: typed-request parsing (currency after the number, written dates, per-unit prices,
two amounts), CSV robustness (quantity "1,200", "7.5%", blank currency, negative amount,
ambiguous dates, unknown doc type), live FX staleness gate, no guessed INR values when the
rate can't be verified, per-run time limit, the premium sensitivity on each hedge, rejecting an
action, and personal-data minimising and purge.
Run: .venv/bin/python test_hardening.py
"""
import uuid

import sources
from actions import approve, propose
from app import _parse_plain_text_invoice as parse_text
from book import load_invoices, normalize_date
from engine import run_pipeline
from policy import decide
from sources import business_days_missed, crust, fetch_fx, load_policy_yaml, start_run_clock

results = []


def check(name, cond, obs=""):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {obs}" if obs else ""))


tag = uuid.uuid4().hex[:6]
cfg = load_policy_yaml()

# ── Typed requests ────────────────────────────────────────────────────────────
r = parse_text("Pay Timken 200,000 USD due 21 Jan 2027 for 500 units")
check("currency after number + written date", r["amount_raw"] == "USD 200000.00" and r["due_date"] == "2027-01-21" and r["supplier_raw"] == "Timken")
r = parse_text("We owe Timken Company USD 1.2 million, due Jan 21, 2027")
check("'1.2 million' and 'Jan 21, 2027'", r["amount_raw"] == "USD 1200000.00" and r["due_date"] == "2027-01-21")
r = parse_text("owe NSK Ltd ₹1.2 crore due 2027-02-01")
check("₹ crore read as INR", r["amount_raw"] == "INR 12000000.00" and r["currency"] == "INR")
r = parse_text("owe Danfoss €35,000 due 15/02/2027")
check("€ amount, unambiguous DD/MM date", r["currency"] == "EUR" and r["due_date"] == "2027-02-15" and r["supplier_raw"] == "Danfoss")
r = parse_text("owe SKF GmbH $40,000 due 03/04/2027")
check("ambiguous 03/04/2027 not guessed", r["due_date"] == "03/04/2027")
r = parse_text("500 bearings at $400 each from SKF GmbH due 2027-01-21")
check("per-unit price x quantity", r["amount_raw"] == "USD 200000.00")
r = parse_text("500 units at $400 each, total $200,000 from SKF GmbH due 2027-01-21")
check("unit price ignored when a total is given", r["amount_raw"] == "USD 200000.00")
r = parse_text("owe SKF GmbH $200k and Timken $50k due 2027-01-21")
check("two different amounts -> blank with reason", r["amount_raw"] == "" and "more than one amount" in r["amount_note"])
r = parse_text("pay SKF GmbH 200k due 2027-01-21")
check("no currency -> blank", r["amount_raw"] == "" and r["currency"] == "")
r = parse_text("owe SKF GmbH $200x due 2027-01-21")
check("unknown unit -> blank", r["amount_raw"] == "")
r = parse_text("owe SKF GmbH -$50,000 due 2027-01-21")
check("'-$50,000' read as negative, supplier clean", r["amount_raw"] == "USD -50000.00" and r["supplier_raw"] == "SKF GmbH")

# ── CSV / row robustness ──────────────────────────────────────────────────────
check("date formats", normalize_date("21-Jan-2027") == "2027-01-21" and normalize_date("January 21st 2027") == "2027-01-21"
      and normalize_date("2027/01/21") == "2027-01-21" and normalize_date("31/02/2027") == "31/02/2027")
rows = load_invoices([
    {"invoice_id": "Q1", "supplier_raw": "SKF GmbH", "currency": "USD", "amount": "200,000", "quantity_units": "1,200",
     "duty_pct": "7.5%", "freight_pct": "", "due_date": "21 Jan 2027", "doc_type": "Purchase Order"},
    {"invoice_id": "Q2", "supplier_raw": "SKF GmbH", "currency": "", "amount": "50000", "due_date": "2027-01-21"},
    {"invoice_id": "Q3", "supplier_raw": "SKF GmbH", "currency": "USD", "amount": "-50,000", "due_date": "2027-01-21"},
    {"invoice_id": "Q4", "supplier_raw": "SKF GmbH", "currency": "USD", "amount": "(12,000)", "due_date": "2027-01-21"},
    {"invoice_id": "Q5", "supplier_raw": "SKF GmbH", "currency": "USD", "amount": "9000", "quantity_units": "abc", "doc_type": "weird"},
])
q = {x["invoice_id"]: x for x in rows}
check("'1,200' qty and '7.5%' duty don't crash", q["Q1"]["quantity_units"] == 1200 and q["Q1"]["duty_pct"] == 7.5 and q["Q1"]["due_date"] == "2027-01-21")
check("'Purchase Order' doc type -> forecast", q["Q1"]["doc_type"] == "forecast")
check("blank currency not assumed USD", q["Q2"]["currency"] == "")
check("negative / bracketed amount kept negative", q["Q3"]["amount_usd"] == -50000 and q["Q4"]["amount_usd"] == -12000)
check("unreadable qty -> quantity_missing; unknown doc type -> forecast", q["Q5"]["quantity_missing"] and q["Q5"]["doc_type"] == "forecast")

hdr = load_invoices("Invoice No,Vendor,Website,Currency,Invoice Amount,Qty,Due Date,Type\n"
                    "A-1,SKF GmbH,skf.com,USD,\"200,000\",\"1,200\",21/01/2027,Invoice\n")[0]
check("CSV header aliases (Vendor, Qty, Due Date, Invoice Amount)", hdr["supplier_raw"] == "SKF GmbH" and hdr["supplier_domain"] == "skf.com"
      and hdr["amount_usd"] == 200000 and hdr["quantity_units"] == 1200 and hdr["due_date"] == "2027-01-21" and hdr["invoice_id"] == "A-1")

# Policy holds for these (pure, no network)
mkt = fetch_fx(run_id=f"h-mkt-{tag}")
def inv(i, **kw):
    base = {"invoice_id": i, "doc_type": "confirmed", "currency": "USD", "amount_usd": 50000, "tenor_days": 60,
            "bucket": "b", "flags": [], "supplier": {"status": "VERIFIED", "risk_flag": "low", "evidence_ids": []}}
    base.update(kw)
    return base
decs = {d["invoice_id"]: d for d in decide({"invoices": [
    inv("H1", currency="", flags=["MISSING_CURRENCY"]),
    inv("H2", amount_usd=-50000, flags=["NEGATIVE_AMOUNT"]),
    inv("H3", due_date="03/04/2027", tenor_days=None, flags=["INVALID_DUE_DATE"]),
    inv("H4"),
]}, mkt, cfg)}
check("missing currency held", decs["H1"]["action"] == "HOLD_MISSING_CURRENCY" and decs["H1"]["ratio"] == 0)
check("negative amount held", decs["H2"]["action"] == "HOLD_NEGATIVE_AMOUNT" and decs["H2"]["ratio"] == 0)
check("ambiguous date held with a reason naming it", decs["H3"]["action"] == "CANNOT_DETERMINE_TENOR" and "03/04/2027" in decs["H3"]["reason"])
d = decs["H4"]
check("premium sensitivity present and consistent", d["prem_top_of_range_below_pct"] is not None
      and d["prem_top_of_range_below_pct"] < d["prem_bottom_of_range_above_pct"]
      and ((d["ann_prem_pct"] <= d["prem_top_of_range_below_pct"]) == (d["ratio"] == d["range_hi"])),
      f"used={d['ann_prem_pct']} top<={d['prem_top_of_range_below_pct']} bottom>={d['prem_bottom_of_range_above_pct']} ratio={d['ratio']}")

# ── FX staleness on the live path ─────────────────────────────────────────────
check("business days missed", business_days_missed("2026-11-20", "2026-11-23") == 0 and business_days_missed("2026-11-20", "2026-11-25") == 2)
# Thu 24 Dec -> Tue 29 Dec: 25th/26th are ECB holidays, so only Mon 28th is missed
check("ECB holidays not counted as missed fixings", business_days_missed("2026-12-24", "2026-12-29") == 1
      and business_days_missed("2027-03-25", "2027-03-30") == 0)  # Good Friday 26 Mar, Easter Monday 29 Mar 2027
from policy import var_z
check("VaR confidence configurable (95 -> 1.645, 99 -> 2.326)", var_z(95) == 1.645 and var_z(99) == 2.326)
usd_col = load_invoices([{"invoice_id": "U1", "supplier_raw": "SKF GmbH", "amount_usd": "50000", "due_date": "2027-01-21"}])[0]
check("amount_usd column -> currency USD", usd_col["currency"] == "USD")
real_today = sources.today_str
try:
    sources.today_str = lambda: "2026-12-15"  # cached ECB rate is from late Nov -> many business days old
    stale = fetch_fx(run_id=f"h-stale-{tag}")
finally:
    sources.today_str = real_today
    # drop the fx cache row keyed to the fake date so a real run on that day fetches fresh
    with sources.db_conn() as conn:
        conn.execute("DELETE FROM cache WHERE request_hash = ?", (sources._req_hash("fx_latest", {"date": "2026-12-15"}),))
check("old live/fallback rate -> STALE_HOLD", stale["status"] == "STALE_HOLD", stale.get("reason", "")[:90])

# ── No guessed INR value when FX is down ──────────────────────────────────────
res = run_pipeline(f"h-fxdown-{tag}", [{"invoice_id": f"OD-{tag}", "doc_type": "confirmed", "supplier_raw": "NSK Ltd.",
                                         "supplier_domain": "nsk.com", "currency": "USD", "amount": 42000, "due_date": "2026-11-10"}],
                   simulate_fx_down=True)
pays = [o for o in res["outbox"] if o["kind"] == "payment"]
check("FX down: overdue pay-at-spot payment held, INR value blank", pays and pays[0]["state"] == "held" and pays[0]["payload_obj"].get("inr_value") is None,
      pays[0]["payload_obj"].get("held_reason", "")[:80] if pays else "no payment")
k = f"h-noinr-{tag}"
propose(f"h-prop-{tag}", "payment", f"X-{tag}", k, {"title": "t", "inr_value": None})
try:
    approve(k, approver="CFO", second_approver="Director")
    check("approve refuses payment with no INR value", False)
except ValueError as e:
    check("approve refuses payment with no INR value", "no verified INR value" in str(e))

# ── Per-run time limit ────────────────────────────────────────────────────────
rid = f"h-time-{tag}"
start_run_clock(rid, 0)
c = crust("/company/identify", {"names": [f"Never Cached Co {tag}"], "count": 1}, run_id=rid)
check("time limit stops new live calls", (not c["ok"]) and c["error"] == "TIME_LIMIT_REACHED", c.get("message", ""))
c2 = crust("/company/identify", {"domains": ["skf.com"]}, run_id=rid)
check("cached replies still served after time limit", c2["ok"] and c2.get("cache_hit"))

# ── Reject, privacy ───────────────────────────────────────────────────────────
from actions import reject, execute
rk = f"h-rej-{tag}"
propose(f"h-rejrun-{tag}", "payment", f"R-{tag}", rk, {"title": "t", "inr_value": 1000.0})
rej = reject(rk, "CFO", "Supplier asked us to wait for a revised invoice")
blocked = False
try:
    approve(rk, approver="CFO")
except ValueError:
    blocked = True
again = propose(f"h-rejrun2-{tag}", "payment", f"R-{tag}", rk, {"title": "t", "inr_value": 1000.0})
check("reject records who/why, blocks approval, survives re-proposal", rej["state"] == "rejected" and blocked and again["state"] == "rejected")
try:
    reject(rk, "", "")
    check("reject needs name and reason", False)
except ValueError:
    check("reject needs name and reason", True)

from sources import db_conn, minimize_personal_data, add_evidence
import json as _json, privacy
slim = minimize_personal_data("/person/search", {"profiles": [{"basic_profile": {"name": "A B", "emails": ["a@b.com"], "location": "x"},
                                                                 "experience": {"employment_details": {"current": [{"title": "AP lead", "company_name": "Zebra"}]}}}]})
check("Person Search reply cut to name/role/employer", list(slim["profiles"][0].keys()) == ["name", "role", "employer"] and "a@b.com" not in _json.dumps(slim))
pdom = f"purge-{tag}.invalid"
sender, email = f"Priya Test{tag}", f"priya.{tag}@gmail.com"
with db_conn() as conn:
    conn.execute("INSERT OR REPLACE INTO invoices (invoice_id, run_id, supplier_raw, supplier_id, amount_usd, due_date, doc_type, status, dup_of, raw_json) "
                 "VALUES (?, ?, 'X', NULL, 1, '2027-01-01', 'confirmed', 'ACTIVE', NULL, ?)",
                 (f"P-{tag}", f"h-purge-{tag}", _json.dumps({"supplier_domain": pdom, "bank_change_sender": sender, "bank_change_email": email})))
ev = add_evidence(f"h-purge-{tag}", "https://api.crustdata.com/person/search", "profiles", f"Bank-change sender '{sender}' ({email})", "2026-11-22")
out = privacy.purge(pdom, requested_by="test")
with db_conn() as conn:
    left = conn.execute("SELECT COUNT(*) FROM evidence WHERE value LIKE ?", (f"%{email}%",)).fetchone()[0] + \
           conn.execute("SELECT COUNT(*) FROM invoices WHERE raw_json LIKE ?", (f"%{sender}%",)).fetchone()[0]
check("purge removes a supplier's bank-change contact everywhere", out["purged_values"] >= 2 and left == 0, str(out["rows_changed"]))

# ── Questions about a batch (ask.py), on a hand-built run ─────────────────────
import ask
fake = {
    "run_id": "fake", "summary": {"var_confidence_pct": 95},
    "book": {"invoices": [{"invoice_id": "A1", "supplier_raw": "SKF GmbH", "supplier": {"legal_name": "SKF Group"}},
                          {"invoice_id": "A2", "supplier_raw": "Carrier Global", "supplier": {}}]},
    "decisions": [
        {"invoice_id": "A1", "action": "HEDGE_FORWARD", "ratio": 0.75, "hedged_usd": 150000, "forward_rate": 93.2,
         "policy_clause": "1_to_3_months.confirmed", "var_95_inr": 552796, "evidence_ids": [1, 2], "reason": "r"},
        {"invoice_id": "A2", "action": "HOLD_CANNOT_VERIFY_SUPPLIER", "ratio": 0, "hedged_usd": 0, "forward_rate": None,
         "policy_clause": "KYB.LOOKALIKE_HOLD", "var_95_inr": 0, "evidence_ids": [3], "reason": "Lookalike name."},
    ],
    "alternatives": {"A1": {"status": "SOURCED", "break_even_inr": 50754.34, "landed_import_inr": 51527.25,
                            "makers": [{"name": "ARB Bearings"}], "evidence_ids": [4]}},
    "outbox": [], "cost_ledger": {"call_count": 3, "total_credits": 0.4, "total_inr_cost": 1.85},
}
a = ask.answer("Plan hedges for this month's payables and tell me if any should be sourced locally", fake)
check("ask: hedge plan + local sourcing, with evidence IDs", a["intents"] == ["hedge", "local"] and "$150,000" in a["text"]
      and "₹50,754/unit" in a["text"] and any(l["evidence_ids"] for l in a["lines"]))
a = ask.answer("what about Carrier?", fake)
check("ask: narrows to a named supplier", a["scope"] == ["A2"] and "Lookalike" in a["text"])
a = ask.answer("what's the weather in Pune", fake)
check("ask: unknown question -> cannot answer, no guess", not a["answered"] and a["text"].startswith("Cannot answer"))

passed = sum(1 for _, ok in results if ok)
print(f"\nHARDENING CHECKS: {passed}/{len(results)} passed")
raise SystemExit(0 if passed == len(results) else 1)
