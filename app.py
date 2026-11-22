import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from actions import approve, execute, get_outbox, reject
from backtest import metrics, replay
from book import load_invoices
from engine import run_pipeline
from sources import (
    BASE_DIR,
    POLICY_YAML_PATH,
    db_conn,
    get_all_steps,
    get_crustdata_key,
    get_run_cost_summary,
    load_policy_yaml,
    log_cost,
)

app = FastAPI(title="HedgeDesk — AI Treasury Analyst & Domestic Sourcing Desk", version="2.0.0")
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(Exception)
async def _unhandled(request, exc: Exception):
    # Any crash comes back as JSON the UI can show, instead of a bare 500 page.
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


class RunReq(BaseModel):
    mode: str = "bearings_demo"  # bearings_demo | custom_rows | parse_text
    invoices: Optional[List[Dict[str, Any]]] = None
    raw_text: Optional[str] = None
    simulate_fx_down: bool = False
    simulate_fx_stale: bool = False
    simulate_fx_nonsense: bool = False
    simulate_fx_disagreement: bool = False
    simulate_402: bool = False
    cost_limit_credits: Optional[float] = None  # None -> policy.yaml run_cost_limit_credits


class OutboxActionReq(BaseModel):
    approver: str = "CFO / Finance Controller"
    second_approver: Optional[str] = None


class PolicyPatchReq(BaseModel):
    k: Optional[float] = None
    bank_spread_bps: Optional[float] = None
    default_duty_pct: Optional[float] = None
    run_cost_limit_credits: Optional[float] = None
    dual_approval_threshold_inr: Optional[float] = None


def _deterministic_run_id(inv_input: Any, flags: Dict[str, Any]) -> str:
    rows = load_invoices(inv_input) if not isinstance(inv_input, list) else inv_input
    # The policy is part of the run's identity: edit policy.yaml (or the drawer) and the same
    # invoices get a new run that re-decides, instead of replaying the old checkpoint. Crustdata
    # replies come from cache, so the re-run costs nothing; outbox keys stop double payments across runs.
    policy_sig = hashlib.sha256(json.dumps(load_policy_yaml(), sort_keys=True, default=str).encode()).hexdigest()[:10]
    sig_obj = {"rows": rows, "flags": flags, "policy": policy_sig}
    digest = hashlib.sha256(json.dumps(sig_obj, sort_keys=True).encode("utf-8")).hexdigest()[:10]
    return f"hd-{digest}"


@app.get("/")
def home():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/health")
def health():
    import httpx
    credits = None
    try:
        r = httpx.get(
            "https://api.crustdata.com/account/credits",
            headers={"authorization": f"Bearer {get_crustdata_key()}", "x-api-version": "2025-11-01"},
            timeout=8.0,
        )
        # Logged under its own run id so per-run job cost totals are unaffected.
        log_cost("health_check", f"/account/credits (HTTP {r.status_code})", 0.0, 0.0)
        if r.status_code == 200:
            credits = r.json().get("account", {}).get("credits")
    except Exception:
        log_cost("health_check", "/account/credits (network error)", 0.0, 0.0)
    return {
        "status": "ok",
        "policy": load_policy_yaml(),
        "crustdata_credits": credits,
    }


@app.get("/api/policy")
def get_policy():
    return load_policy_yaml()


@app.post("/api/policy")
def patch_policy(patch: PolicyPatchReq):
    cfg = load_policy_yaml()
    if patch.k is not None:
        cfg["decision_rule"]["k"] = patch.k
    if patch.bank_spread_bps is not None:
        cfg["cost_and_landed_assumptions"]["bank_spread_bps"] = patch.bank_spread_bps
    if patch.default_duty_pct is not None:
        cfg["cost_and_landed_assumptions"]["default_duty_pct"] = patch.default_duty_pct
    if patch.run_cost_limit_credits is not None:
        cfg["cost_and_landed_assumptions"]["run_cost_limit_credits"] = patch.run_cost_limit_credits
    if patch.dual_approval_threshold_inr is not None:
        cfg["hard_rules"]["dual_approval_threshold_inr"] = patch.dual_approval_threshold_inr
    with open(POLICY_YAML_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    return {"status": "updated", "policy": cfg}


@app.get("/api/latest-run")
def get_latest_run():
    """
    On restart after a real Ctrl+C kill, finds the most recent run in SQLite `steps`
    and resumes it automatically from the last committed checkpoint with zero duplicate calls.
    """
    with db_conn() as conn:
        row = conn.execute(
            "SELECT run_id, output_ref FROM steps WHERE step = '0_request' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    if not row:
        return {"has_run": False}
    run_id = row["run_id"]
    inv_input = json.loads(row["output_ref"]) if row["output_ref"] else str(BASE_DIR / "sample_invoices.csv")
    res = run_pipeline(run_id=run_id, invoices_input=inv_input)
    res["has_run"] = True
    return res


@app.post("/api/run")
def start_run(req: RunReq):
    if req.mode == "custom_rows" and req.invoices:
        inv_input = req.invoices
    elif req.mode == "parse_text" and req.raw_text:
        inv_input = [_parse_plain_text_invoice(req.raw_text)]
    else:
        inv_input = str(BASE_DIR / "sample_invoices.csv")

    flags = {
        "down": req.simulate_fx_down,
        "stale": req.simulate_fx_stale,
        "nonsense": req.simulate_fx_nonsense,
        "disagree": req.simulate_fx_disagreement,
        "c402": req.simulate_402,
    }
    run_id = _deterministic_run_id(inv_input, flags)
    return run_pipeline(
        run_id=run_id,
        invoices_input=inv_input,
        simulate_fx_down=req.simulate_fx_down,
        simulate_fx_stale=req.simulate_fx_stale,
        simulate_fx_nonsense=req.simulate_fx_nonsense,
        simulate_fx_disagreement=req.simulate_fx_disagreement,
        simulate_402=req.simulate_402,
        cost_limit_credits=req.cost_limit_credits,
    )


@app.post("/api/upload-csv")
async def upload_csv(file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8", errors="ignore")
    run_id = _deterministic_run_id(content, {"upload": True})
    return run_pipeline(run_id=run_id, invoices_input=content)


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str):
    with db_conn() as conn:
        row = conn.execute("SELECT output_ref FROM steps WHERE run_id = ? AND step = '0_request'", (run_id,)).fetchone()
    if row and row["output_ref"]:
        inv_input = json.loads(row["output_ref"])
    else:
        inv_input = str(BASE_DIR / "sample_invoices.csv")
    return run_pipeline(run_id=run_id, invoices_input=inv_input)


@app.post("/api/outbox/{idem_key}/approve-and-execute")
def approve_and_execute(idem_key: str, req: OutboxActionReq):
    try:
        approve(idem_key, approver=req.approver, second_approver=req.second_approver)
        return execute(idem_key)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))


class AskReq(BaseModel):
    question: str
    run_id: Optional[str] = None


@app.post("/api/ask")
def ask_question(req: AskReq):
    """
    A typed request that states an amount is a new invoice (the UI then runs it). Anything else is a
    question about the current batch, answered from that run's decisions and evidence (ask.py).
    """
    import ask
    amt, _ = _extract_amount(req.question)
    if amt:
        return {"kind": "invoice"}
    run_id = req.run_id
    if not run_id:
        with db_conn() as conn:
            row = conn.execute("SELECT run_id FROM steps WHERE step = '0_request' ORDER BY rowid DESC LIMIT 1").fetchone()
        if not row:
            return {"kind": "answer", "answered": False, "lines": [],
                    "text": "No batch loaded yet. Run the demo or upload a CSV, then ask about it."}
        run_id = row["run_id"]
    res = ask.answer(req.question, resume_run(run_id))
    res["kind"] = "answer"
    return res


class PurgeReq(BaseModel):
    supplier_domain: Optional[str] = None
    requested_by: str = ""


@app.get("/api/privacy")
def privacy_summary():
    import privacy
    return privacy.summary()


@app.post("/api/privacy/purge")
def privacy_purge(req: PurgeReq):
    import privacy
    return privacy.purge(req.supplier_domain, req.requested_by)


class RejectReq(BaseModel):
    rejected_by: str
    reason: str


@app.post("/api/outbox/{idem_key}/reject")
def reject_action(idem_key: str, req: RejectReq):
    try:
        return reject(idem_key, req.rejected_by, req.reason)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))


@app.get("/api/outbox/{run_id}")
def list_outbox(run_id: str):
    return {"outbox": get_outbox(run_id)}


@app.get("/api/backtest")
def get_backtest():
    cfg = load_policy_yaml()
    return metrics(replay(cfg))


def _parse_plain_text_invoice(text: str) -> Dict[str, Any]:
    """
    Pull invoice fields out of a typed request. Anything the text does not state is left blank,
    never defaulted: the pipeline then holds the invoice (missing amount, supplier or due date)
    or skips the per-unit break-even (missing quantity) and says what is missing.
    """
    # A number followed by an unknown unit (e.g. "$200x") is left blank so the invoice is held.
    qty_m = re.search(r"(\d[\d,]*)\s*(?:units?|bearings?|compressors?|pcs|pieces)\b", text, re.IGNORECASE)
    qty = qty_m.group(1).replace(",", "") if qty_m else ""
    raw_amt, amount_note = _extract_amount(text, qty)
    dom_m = re.search(r"\b([a-zA-Z0-9-]+\.(?:com|de|in|co|org|net))\b", text, re.IGNORECASE)
    dom = dom_m.group(1).lower() if dom_m else ""
    # Supplier name: capitalised words after owe/from/supplier/to, stopping before an amount,
    # a number, a bracketed domain, or a following clause ("for", "due", "on").
    sup_m = re.search(
        r"\b(?i:owe|from|supplier|to|pay)\s+([A-Z][A-Za-z0-9&.\- ]{1,40}?)"
        r"(?=\s*(?:\(|,|-?\s*\$|-?\s*€|-?\s*₹|-?\s*\d|\bRs\b|\bUSD\b|\bEUR\b|\bINR\b|\bUS\$|\bfor\b|\bdue\b|\bon\b|\bby\b|$))",
        text,
    )
    sup = sup_m.group(1).strip() if sup_m else ""
    due = _extract_due_date(text)
    ccy = raw_amt.split(" ", 1)[0] if raw_amt else ""
    return {
        "invoice_id": f"INV-ASK-{hashlib.sha256(text.encode()).hexdigest()[:4].upper()}",
        "doc_type": "forecast" if re.search(r"\b(forecast|po|purchase order)\b", text, re.IGNORECASE) else "confirmed",
        "supplier_raw": sup,
        "supplier_domain": dom,
        "currency": ccy,
        "amount_raw": raw_amt,
        "quantity_units": qty,
        "part_description": text[:100],
        "domestic_query": text[:80],
        "due_date": due,
        "amount_note": amount_note,
    }


_CCY_PRE = r"(US\$|USD|EUR|€|INR|₹|Rs\.?|\$)"
_CCY_POST = r"(USD|EUR|INR|dollars?|euros?|rupees?)"
_NUM = r"(\d[\d,]*(?:\.\d+)?)"
_SCALES = {
    "k": 1e3, "thousand": 1e3, "mn": 1e6, "m": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9,
    "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "crore": 1e7, "crores": 1e7, "cr": 1e7,
}


def _ccy_code(tok: str) -> str:
    t = tok.lower().rstrip(".")
    if t in {"$", "us$", "usd", "dollar", "dollars"}:
        return "USD"
    if t in {"€", "eur", "euro", "euros"}:
        return "EUR"
    return "INR"  # inr, ₹, rs, rupee(s)


_PER_UNIT = re.compile(r"^\s*(?:each|apiece|a piece|per\s+(?:unit|piece|pc|bearing|item)|/\s*(?:unit|pc|piece))\b", re.IGNORECASE)


def _extract_amount(text: str, qty: str = ""):
    """
    Find the amount and currency in a typed request. Accepts "$200,000", "USD 200k", "200,000 USD",
    "₹1.2 crore", "Rs 50 lakh", "2.5 million dollars". Returns ("USD 200000.00", note) or ("", note).
    Left blank (so the invoice is held) when: no currency is stated, the number has an unknown unit
    attached ("$200x"), or two different amounts appear and we can't tell which one is owed.
    """
    found = []  # (position, currency, value) or (position, None, None) for a malformed amount
    unit_prices = []  # "$400 each": a per-unit price, not the amount owed
    signed = re.compile(
        r"(?P<neg1>-\s*)?" + _CCY_PRE.replace("(", "(?P<ccy>", 1) + r"\s*(?P<neg2>-\s*)?"
        + _NUM.replace("(", "(?P<num>", 1) + r"(?:\s*(?P<unit>[A-Za-z]+))?",
        re.IGNORECASE,
    )
    for m in signed.finditer(text):
        sign = -1.0 if (m.group("neg1") or m.group("neg2")) else 1.0
        unit = (m.group("unit") or "").lower()
        attached = bool(m.group("unit")) and m.start("unit") == m.end("num")
        base = float(m.group("num").replace(",", ""))
        if unit in _SCALES:
            val, tail_at = base * _SCALES[unit], m.end("unit")
        elif attached:
            found.append((m.start(), None, None))
            continue
        else:
            val, tail_at = base, m.end("num")
        if _PER_UNIT.match(text[tail_at:]):
            unit_prices.append((_ccy_code(m.group("ccy")), val))
            continue
        found.append((m.start(), _ccy_code(m.group("ccy")), val * sign))
    for m in re.finditer(r"(?<![\d.,\-/])" + _NUM + r"\s*(?:([A-Za-z]+)\s+)?" + _CCY_POST + r"\b", text, re.IGNORECASE):
        unit = (m.group(2) or "").lower()
        if unit and unit not in _SCALES:
            continue
        val = float(m.group(1).replace(",", "")) * _SCALES.get(unit, 1.0)
        # skip if this number was already read with a leading currency symbol
        if any(abs(p - m.start()) <= 5 for p, _, _ in found):
            continue
        found.append((m.start(), _ccy_code(m.group(3)), val))

    if not found and len(unit_prices) == 1 and qty:
        # "500 bearings at $400 each" with no total: amount owed = quantity x unit price, both stated.
        ccy, unit_val = unit_prices[0]
        return f"{ccy} {unit_val * float(qty):.2f}", ""
    if not found:
        return "", "no amount with a currency was given"
    found.sort()
    if any(c is None for _, c, _ in found):
        return "", "amount has a unit we can't read (e.g. '$200x')"
    distinct = {(c, round(v, 2)) for _, c, v in found}
    if len(distinct) > 1:
        shown = ", ".join(f"{c} {v:,.0f}" for c, v in sorted(distinct))
        return "", f"more than one amount in the request ({shown}); can't tell which is owed"
    _, ccy, val = found[0]
    return f"{ccy} {val:.2f}", ""


def _extract_due_date(text: str) -> str:
    """ISO date, or a written one ("21 Jan 2027", "Jan 21, 2027", "21/01/2027" when unambiguous)."""
    from book import normalize_date

    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if m:
        return m.group(1)
    mon = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
    for pat in (
        r"\b(\d{1,2}(?:st|nd|rd|th)?[\s\-]+" + mon + r",?[\s\-]+\d{4})\b",
        r"\b(" + mon + r"[\s\-]+\d{1,2}(?:st|nd|rd|th)?,?[\s\-]+\d{4})\b",
        r"\b(\d{1,2}[/.]\d{1,2}[/.]\d{4})\b",
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return normalize_date(m.group(1))
    return ""
