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

from actions import approve, execute, get_outbox
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


class RunReq(BaseModel):
    mode: str = "bearings_demo"  # bearings_demo | custom_rows | parse_text
    invoices: Optional[List[Dict[str, Any]]] = None
    raw_text: Optional[str] = None
    simulate_fx_down: bool = False
    simulate_fx_stale: bool = False
    simulate_fx_nonsense: bool = False
    simulate_fx_disagreement: bool = False
    simulate_402: bool = False
    cost_limit_credits: float = 25.0


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
    sig_obj = {"rows": rows, "flags": flags}
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
    amt_m = re.search(
        r"(\$|USD|EUR|INR)\s*([\d,]+(?:\.\d+)?)(?:\s*(lakh|lac|crore|cr|k|thousand|mn|million|bn|billion)\b)?([A-Za-z]?)",
        text, re.IGNORECASE,
    )
    raw_amt = ""
    if amt_m and not amt_m.group(4):
        scale = (amt_m.group(3) or "").lower()
        mult = {"k": 1e3, "thousand": 1e3, "mn": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}.get(scale)
        if mult:
            # Expand Western scale words here; parse_amount_messy only knows lakh/crore.
            num = float(amt_m.group(2).replace(",", "")) * mult
            raw_amt = f"{amt_m.group(1)} {num:.2f}"
        else:
            # Plain number, or lakh/lac/crore/cr which parse_amount_messy normalises.
            raw_amt = amt_m.group(0).strip()
    # A number followed by an unknown unit (e.g. "$200x") is left blank so the invoice is held.
    qty_m = re.search(r"(\d[\d,]*)\s*(?:units?|bearings?|compressors?|pcs|pieces)\b", text, re.IGNORECASE)
    qty = qty_m.group(1).replace(",", "") if qty_m else ""
    dom_m = re.search(r"\b([a-zA-Z0-9-]+\.(?:com|de|in|co|org|net))\b", text, re.IGNORECASE)
    dom = dom_m.group(1).lower() if dom_m else ""
    # Supplier name: capitalised words after owe/from/supplier/to, stopping before an amount,
    # a number, a bracketed domain, or a following clause ("for", "due", "on").
    sup_m = re.search(
        r"\b(?:owe|from|supplier|to)\s+([A-Z][A-Za-z0-9&.\- ]{1,40}?)"
        r"(?=\s*(?:\(|,|\$|\d|\bUSD\b|\bEUR\b|\bINR\b|\bfor\b|\bdue\b|\bon\b|$))",
        text,
    )
    sup = sup_m.group(1).strip() if sup_m else ""
    due_m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    due = due_m.group(1) if due_m else ""
    ccy = ""
    if raw_amt:
        tok = raw_amt.lower()
        ccy = "EUR" if "eur" in tok else ("INR" if "inr" in tok else "USD")
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
    }
