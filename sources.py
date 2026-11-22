"""
sources.py — The single gateway to external APIs (Crustdata, RBI/FBIL/ECB FX).
Implements the exact 7-table SQLite schema (`hedgedesk.db`) from HedgeDesk Context Doc p.5:
cache, evidence, cost_ledger, invoices, decisions, outbox, steps.
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# On Vercel the deployment filesystem is read-only except /tmp, so the SQLite DB and the
# live-editable policy copy live there (per instance, reset on cold start). Locally nothing changes.
ON_VERCEL = bool(os.getenv("VERCEL"))
RUNTIME_DIR = Path("/tmp") if ON_VERCEL else BASE_DIR
POLICY_YAML_PATH = RUNTIME_DIR / "policy.yaml"
if ON_VERCEL and not POLICY_YAML_PATH.exists():
    import shutil
    shutil.copyfile(BASE_DIR / "policy.yaml", POLICY_YAML_PATH)
DB_FILE = RUNTIME_DIR / "hedgedesk.db"
CRUSTDATA_BASE = "https://api.crustdata.com"
CRUSTDATA_VERSION = "2025-11-01"

_DB_LOCK = threading.RLock()


def load_policy_yaml() -> Dict[str, Any]:
    with open(POLICY_YAML_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_crustdata_key() -> str:
    load_dotenv(BASE_DIR / ".env")
    return os.getenv("CRUSTDATA_API_KEY", "").strip()


@contextmanager
def db_conn(db_path: Path = DB_FILE):
    with _DB_LOCK:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db(db_path: Path = DB_FILE):
    """Create the exact 7 SQLite tables specified in Context Doc Page 5."""
    with db_conn(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cache (
                request_hash TEXT PRIMARY KEY,
                endpoint TEXT NOT NULL,
                response_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                source TEXT NOT NULL,
                field_path TEXT NOT NULL,
                value TEXT NOT NULL,
                record_date TEXT NOT NULL,
                retrieved_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cost_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                credits REAL DEFAULT 0.0,
                tokens INTEGER DEFAULT 0,
                inr_cost REAL DEFAULT 0.0,
                at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS invoices (
                invoice_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                supplier_raw TEXT NOT NULL,
                supplier_id TEXT,
                amount_usd REAL DEFAULT 0.0,
                due_date TEXT,
                doc_type TEXT DEFAULT 'confirmed',
                status TEXT NOT NULL,
                dup_of TEXT,
                raw_json TEXT,
                PRIMARY KEY (run_id, invoice_id)
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                bucket TEXT NOT NULL,
                invoice_id TEXT,
                ratio REAL NOT NULL,
                var REAL DEFAULT 0.0,
                fwd_cost REAL DEFAULT 0.0,
                risk_flag TEXT NOT NULL,
                policy_clause TEXT NOT NULL,
                evidence_ids TEXT NOT NULL,
                details_json TEXT
            );

            CREATE TABLE IF NOT EXISTS outbox (
                idem_key TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                invoice_id TEXT,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                state TEXT NOT NULL,
                approved_by TEXT,
                second_approver TEXT,
                executed_at TEXT,
                external_ref TEXT
            );

            CREATE TABLE IF NOT EXISTS steps (
                run_id TEXT NOT NULL,
                step TEXT NOT NULL,
                status TEXT NOT NULL,
                output_ref TEXT,
                PRIMARY KEY (run_id, step)
            );
            """
        )


init_db()


def add_evidence(
    run_id: str,
    source: str,
    field_path: str,
    value: Any,
    record_date: str,
    retrieved_at: Optional[str] = None,
    db_path: Path = DB_FILE,
) -> int:
    with db_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO evidence (run_id, source, field_path, value, record_date, retrieved_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, source, field_path, str(value), record_date, retrieved_at or utc_now()),
        )
        return int(cur.lastrowid)


def evidence(ev_id: int, db_path: Path = DB_FILE) -> Optional[Dict[str, Any]]:
    with db_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM evidence WHERE id = ?", (ev_id,)).fetchone()
        return dict(row) if row else None


def get_run_evidence(run_id: str, db_path: Path = DB_FILE) -> List[Dict[str, Any]]:
    with db_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM evidence WHERE run_id = ? ORDER BY id ASC", (run_id,)).fetchall()
        return [dict(r) for r in rows]


def get_run_cost_summary(run_id: str, db_path: Path = DB_FILE) -> Dict[str, Any]:
    with db_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM cost_ledger WHERE run_id = ? ORDER BY id ASC", (run_id,)).fetchall()
        items = [dict(r) for r in rows]
        total_credits = round(sum(float(x["credits"]) for x in items), 4)
        total_inr = round(sum(float(x["inr_cost"]) for x in items), 2)
        return {
            "items": items,
            "total_credits": total_credits,
            "total_inr_cost": total_inr,
            "call_count": len(items),
        }


def save_step(run_id: str, step: str, status: str, output_ref: Any = None, db_path: Path = DB_FILE):
    ref_str = json.dumps(output_ref) if not isinstance(output_ref, str) else output_ref
    with db_conn(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO steps (run_id, step, status, output_ref)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, step, status, ref_str),
        )


def get_step(run_id: str, step: str, db_path: Path = DB_FILE) -> Optional[Dict[str, Any]]:
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM steps WHERE run_id = ? AND step = ? AND status = 'completed'",
            (run_id, step),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["output_data"] = json.loads(d["output_ref"]) if d.get("output_ref") else None
        except Exception:
            d["output_data"] = d.get("output_ref")
        return d


def get_all_steps(run_id: str, db_path: Path = DB_FILE) -> List[Dict[str, Any]]:
    with db_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM steps WHERE run_id = ?", (run_id,)).fetchall()
        return [dict(r) for r in rows]


def log_cost(run_id: str, endpoint: str, credits: float = 0.0, inr_cost: float = 0.0, db_path: Path = DB_FILE) -> None:
    """Write one cost_ledger row. Every outbound call (success, failure or retry) and every cache hit gets one."""
    with db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO cost_ledger (run_id, endpoint, credits, tokens, inr_cost, at) VALUES (?, ?, ?, 0, ?, ?)",
            (run_id, endpoint, float(credits), float(inr_cost), utc_now()),
        )


# ── Per-run time limit ────────────────────────────────────────────────────────
# engine.run_pipeline starts a clock per run_id; crust() refuses new live calls once it runs out,
# the same way it refuses calls past the credit limit. Cached replies are still served, so a
# resumed or replayed run is never blocked. The clock lives in this process: after a kill and
# restart the resumed portion gets a fresh allowance.
_RUN_CLOCKS: Dict[str, Tuple[float, float]] = {}


def start_run_clock(run_id: str, limit_seconds: float) -> None:
    _RUN_CLOCKS[run_id] = (time.monotonic(), float(limit_seconds))


def run_time_left(run_id: str) -> Optional[float]:
    clk = _RUN_CLOCKS.get(run_id)
    if not clk:
        return None
    return clk[1] - (time.monotonic() - clk[0])


def run_clock_info(run_id: str) -> Dict[str, Any]:
    clk = _RUN_CLOCKS.get(run_id)
    if not clk:
        return {"elapsed_s": None, "limit_s": None}
    return {"elapsed_s": round(time.monotonic() - clk[0], 2), "limit_s": clk[1]}


FALLBACK_COST_FX = 92.68  # used for the ledger's INR column only if no USD/INR rate has ever been fetched


def cost_fx_rate(db_path: Path = DB_FILE) -> float:
    """USD/INR used to price Crustdata credits in INR: the latest ECB rate we fetched, else the stored history."""
    try:
        with db_conn(db_path) as conn:
            row = conn.execute(
                "SELECT response_json FROM cache WHERE endpoint LIKE 'https://api.frankfurter.app/latest%' "
                "ORDER BY fetched_at DESC LIMIT 1"
            ).fetchone()
        if row:
            return float(json.loads(row["response_json"])["rates"]["INR"])
        snap = BASE_DIR / "hedgedesk" / "usd_inr_ecb_history.json"
        if snap.exists():
            rates = json.loads(snap.read_text(encoding="utf-8")).get("rates", {})
            if rates:
                return float(rates[sorted(rates)[-1]]["INR"])
    except Exception:
        pass
    return FALLBACK_COST_FX


def business_days_missed(record_date: str, today: str) -> int:
    """Weekdays strictly between the rate's date and today: published fixings we don't have.
    Friday's rate read on Monday morning = 0 missed; read on Wednesday = 2 missed (Mon, Tue)."""
    from datetime import date, timedelta
    try:
        d0 = date.fromisoformat(record_date[:10])
        d1 = date.fromisoformat(today[:10])
    except ValueError:
        return 999
    n, d = 0, d0 + timedelta(days=1)
    while d < d1:
        if d.weekday() < 5 and d not in _target_holidays(d.year):
            n += 1
        d += timedelta(days=1)
    return n


def _target_holidays(year: int):
    """Days the ECB publishes no reference rate (TARGET2 closing days): 1 Jan, Good Friday,
    Easter Monday, 1 May, 25 and 26 Dec."""
    from datetime import date, timedelta
    # Anonymous Gregorian algorithm for Easter Sunday
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    easter = date(year, month, day + 1)
    return {date(year, 1, 1), easter - timedelta(days=2), easter + timedelta(days=1),
            date(year, 5, 1), date(year, 12, 25), date(year, 12, 26)}


def _credits_from_headers(resp: Any) -> float:
    try:
        return float(resp.headers.get("x-credits-used") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _req_hash(endpoint: str, body: Dict[str, Any]) -> str:
    raw = f"{endpoint}:{json.dumps(body, sort_keys=True)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def crust(
    endpoint: str,
    body: Dict[str, Any],
    run_id: str = "default",
    cost_limit_credits: Optional[float] = None,
    simulate_status: Optional[int] = None,
    db_path: Path = DB_FILE,
) -> Dict[str, Any]:
    """
    Cached, rate-limit-resilient Crustdata caller (`https://api.crustdata.com`).
    Handles 429 (`x-ratelimit-reset`), 5xx backoff, 402 (credits exhausted -> finish with cache),
    and per-run cost limit (Case 25, 26, 28).
    """
    cfg = load_policy_yaml()
    limit_cr = float(
        cost_limit_credits
        if cost_limit_credits is not None
        else cfg["cost_and_landed_assumptions"]["run_cost_limit_credits"]
    )
    usd_per_cr = float(cfg["cost_and_landed_assumptions"]["usd_per_crustdata_credit"])
    rhash = _req_hash(endpoint, body)

    # 1. Check cache first (saves credits on retries & restarts — Case 23/25/26)
    if simulate_status is None:
        cached_payload = None
        with db_conn(db_path) as conn:
            row = conn.execute("SELECT * FROM cache WHERE request_hash = ?", (rhash,)).fetchone()
            if row:
                data = json.loads(row["response_json"])
                conn.execute(
                    "INSERT INTO cost_ledger (run_id, endpoint, credits, tokens, inr_cost, at) VALUES (?, ?, 0.0, 0, 0.0, ?)",
                    (run_id, f"{endpoint} (cache)", utc_now()),
                )
                cached_payload = (data, row["fetched_at"])
        if cached_payload is not None:
            data, fetched_at = cached_payload
            ev_id = add_evidence(
                run_id=run_id,
                source=f"{CRUSTDATA_BASE}{endpoint}",
                field_path="cached_response",
                value=f"cache_hit ({rhash[:10]})",
                record_date=fetched_at[:10],
                retrieved_at=fetched_at,
                db_path=db_path,
            )
            return {
                "ok": True,
                "data": data,
                "cache_hit": True,
                "credits": 0.0,
                "fetched_at": fetched_at,
                "evidence_id": ev_id,
            }

    # 2a. Enforce the run's time limit BEFORE a live call
    left = run_time_left(run_id)
    if left is not None and left <= 0:
        clk = run_clock_info(run_id)
        ev_id = add_evidence(
            run_id=run_id,
            source=f"{CRUSTDATA_BASE}{endpoint}",
            field_path="time_gate",
            value=f"TIME_LIMIT_REACHED ({clk['elapsed_s']:.1f}s/{clk['limit_s']:.0f}s)",
            record_date=today_str(),
            db_path=db_path,
        )
        return {
            "ok": False,
            "status_code": 402,
            "error": "TIME_LIMIT_REACHED",
            "message": f"Run time limit ({clk['limit_s']:.0f}s) reached; skipped {endpoint}, check marked cannot verify.",
            "evidence_id": ev_id,
        }

    # 2b. Enforce run cost limit BEFORE live call (Case 28)
    summary = get_run_cost_summary(run_id, db_path)
    est_credits = 2.0 if "enrich" in endpoint else (1.0 if "web" in endpoint else 0.2)
    if summary["total_credits"] + est_credits > limit_cr:
        ev_id = add_evidence(
            run_id=run_id,
            source=f"{CRUSTDATA_BASE}{endpoint}",
            field_path="budget_gate",
            value=f"COST_LIMIT_REACHED ({summary['total_credits']:.2f}/{limit_cr:.2f} cr)",
            record_date=today_str(),
            db_path=db_path,
        )
        return {
            "ok": False,
            "status_code": 402,
            "error": "COST_LIMIT_REACHED",
            "message": f"Run cost limit ({limit_cr:.2f} cr) reached; skipped {endpoint}.",
            "evidence_id": ev_id,
        }

    # 3. Simulated error handling tests (402, 429, 5xx)
    if simulate_status == 402:
        ev_id = add_evidence(
            run_id=run_id,
            source=f"{CRUSTDATA_BASE}{endpoint}",
            field_path="credits",
            value="402 Credits Exhausted — stopped live calls, continuing with cached data",
            record_date=today_str(),
            db_path=db_path,
        )
        return {
            "ok": False,
            "status_code": 402,
            "error": "CREDITS_EXHAUSTED",
            "message": "HTTP 402: Crustdata credits exhausted; stopping live calls and noting unverified checks.",
            "evidence_id": ev_id,
        }

    key = get_crustdata_key()
    headers = {
        "authorization": f"Bearer {key}",
        "x-api-version": CRUSTDATA_VERSION,
        "content-type": "application/json",
    }
    url = f"{CRUSTDATA_BASE}{endpoint}"
    backoff = 2.0
    fx_for_cost = cost_fx_rate(db_path)

    for attempt in range(4):
        try:
            if simulate_status == 429 and attempt == 0:
                time.sleep(0.05)
                continue
            left = run_time_left(run_id)
            if left is not None and left <= 0:
                return {"ok": False, "status_code": 402, "error": "TIME_LIMIT_REACHED",
                        "message": f"Run time limit reached while retrying {endpoint}; check marked cannot verify."}
            call_timeout = 30.0 if left is None else max(2.0, min(30.0, left))
            with httpx.Client(timeout=call_timeout) as client:
                resp = client.post(url, headers=headers, json=body)
            if resp.status_code >= 400:
                # Failed calls are still calls: record them (credits as charged by Crustdata, usually 0).
                fail_cr = _credits_from_headers(resp)
                log_cost(run_id, f"{endpoint} (HTTP {resp.status_code})", fail_cr, round(fail_cr * usd_per_cr * fx_for_cost, 2), db_path)
            if resp.status_code == 402:
                ev_id = add_evidence(
                    run_id=run_id,
                    source=url,
                    field_path="credits",
                    value="402 Credits Exhausted",
                    record_date=today_str(),
                    db_path=db_path,
                )
                return {"ok": False, "status_code": 402, "error": "CREDITS_EXHAUSTED", "evidence_id": ev_id}
            if resp.status_code == 429 or resp.status_code >= 500:
                reset_s = float(resp.headers.get("x-ratelimit-reset", backoff))
                if attempt < 3:
                    wait_s = min(max(reset_s, backoff), 12.0)
                    left = run_time_left(run_id)
                    if left is not None:
                        wait_s = min(wait_s, max(0.0, left))
                    time.sleep(wait_s)
                    backoff = min(backoff * 2.0, 15.0)
                    continue
                return {"ok": False, "status_code": resp.status_code, "error": f"HTTP {resp.status_code}"}
            if resp.status_code >= 400:
                return {"ok": False, "status_code": resp.status_code, "error": resp.text[:300]}

            data = resp.json()
            cr_used = float(resp.headers.get("x-credits-used", est_credits) or 0.0)
            req_id = resp.headers.get("x-request-id", "")
            inr_cost = round(cr_used * usd_per_cr * fx_for_cost, 2)
            now = utc_now()

            with db_conn(db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO cache (request_hash, endpoint, response_json, fetched_at) VALUES (?, ?, ?, ?)",
                    (rhash, endpoint, json.dumps(data), now),
                )
                conn.execute(
                    "INSERT INTO cost_ledger (run_id, endpoint, credits, tokens, inr_cost, at) VALUES (?, ?, ?, 0, ?, ?)",
                    (run_id, endpoint, cr_used, inr_cost, now),
                )

            ev_id = add_evidence(
                run_id=run_id,
                source=f"{url} (req_id={req_id})",
                field_path="response",
                value=f"credits={cr_used}",
                record_date=now[:10],
                retrieved_at=now,
                db_path=db_path,
            )
            return {
                "ok": True,
                "data": data,
                "cache_hit": False,
                "credits": cr_used,
                "request_id": req_id,
                "fetched_at": now,
                "evidence_id": ev_id,
            }
        except httpx.HTTPError as exc:
            log_cost(run_id, f"{endpoint} (network error: {type(exc).__name__})", 0.0, 0.0, db_path)
            if attempt == 3:
                return {"ok": False, "status_code": 0, "error": str(exc)}
            time.sleep(backoff)

    return {"ok": False, "status_code": 0, "error": "Max retries exhausted"}


def _load_real_fx_history(db_path: Path = DB_FILE, run_id: str = "fx_history") -> Dict[str, float]:
    """Load real daily ECB/Frankfurter USD/INR history (cached/local snapshot)."""
    snap_path = BASE_DIR / "hedgedesk" / "usd_inr_ecb_history.json"
    if snap_path.exists():
        raw = json.loads(snap_path.read_text(encoding="utf-8"))
        return {d: float(v["INR"]) for d, v in raw.get("rates", {}).items() if "INR" in v}

    url = "https://api.frankfurter.app/2025-04-30..2026-11-22?from=USD&to=INR"
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            r = client.get(url)
            log_cost(run_id, "fx_history_download (frankfurter)", 0.0, 0.0, db_path)
            r.raise_for_status()
            raw = r.json()
            snap_path.parent.mkdir(exist_ok=True)
            snap_path.write_text(json.dumps(raw), encoding="utf-8")
            return {d: float(v["INR"]) for d, v in raw.get("rates", {}).items() if "INR" in v}
    except Exception:
        log_cost(run_id, "fx_history_download (frankfurter, failed)", 0.0, 0.0, db_path)
        return {}


def fetch_fx(
    as_of_date: Optional[str] = None,
    run_id: str = "default",
    simulate_down: bool = False,
    simulate_stale: bool = False,
    simulate_nonsense: bool = False,
    simulate_disagreement: bool = False,
    db_path: Path = DB_FILE,
) -> Dict[str, Any]:
    """
    Fetch official USD/INR spot rate, 60-trading-day daily volatility sigma_daily,
    and 1M / 3M / 6M forward premia (FBIL / RBI / ECB).
    Enforces Cases 18*, 19, 20, 21, 22 (backoff, stale gate, sanity bounds, holiday, disagreement).
    """
    cfg = load_policy_yaml()
    bounds = cfg["hard_rules"]["sanity_bounds"]
    hist_map = _load_real_fx_history(db_path, run_id=run_id)

    # Case 18*: FBIL/RBI & fallback down -> cannot verify rate
    if simulate_down:
        ev_id = add_evidence(
            run_id, "SIMULATED (UI test toggle: FX feed down)", "usd_inr_spot", "CANNOT_VERIFY (simulated source timeout/down)", today_str(), db_path=db_path
        )
        return {
            "status": "CANNOT_VERIFY",
            "reason": "FX feeds unavailable (simulated): live ECB/Frankfurter rate and stored fallback both failed/timed out.",
            "spot": None,
            "evidence_ids": [ev_id],
        }

    # Case 20: Nonsense / layout broken (e.g., 40% forward premium or negative spot)
    if simulate_nonsense:
        bad_spot = -5.0
        bad_prem = 40.0
        ev_id = add_evidence(
            run_id, "SIMULATED (UI test toggle: corrupted feed)", "sanity_check", f"REJECTED spot={bad_spot}, prem={bad_prem}%", today_str(), db_path=db_path
        )
        return {
            "status": "CANNOT_VERIFY",
            "reason": f"Sanity bounds rejected corrupted feed (spot={bad_spot} outside [{bounds['min_spot_usd_inr']}, {bounds['max_spot_usd_inr']}], prem={bad_prem}% > {bounds['max_annual_fwd_prem_pct']}%).",
            "spot": None,
            "evidence_ids": [ev_id],
        }

    # Case 19: Stale market data (> 1 business day old)
    if simulate_stale:
        stale_date = "2026-10-01"
        ev_id = add_evidence(
            run_id, "SIMULATED (UI test toggle: stale feed)", "reference_rate_date", f"STALE ({stale_date})", stale_date, db_path=db_path
        )
        return {
            "status": "STALE_HOLD",
            "reason": f"Market data from {stale_date} is older than policy limit (1 business day). Holding decision.",
            "last_available_date": stale_date,
            "spot": 91.15,
            "evidence_ids": [ev_id],
        }

    # Point-in-time historical mode (used by backtest.py with strict date cutoff!)
    if as_of_date:
        valid_dates = sorted(d for d in hist_map.keys() if d <= as_of_date)
        if not valid_dates:
            return {"status": "CANNOT_VERIFY", "reason": f"No rate on or before {as_of_date}", "spot": None, "evidence_ids": []}
        rec_date = valid_dates[-1]
        spot = hist_map[rec_date]
        window_dates = valid_dates[-61:]
    else:
        # Live mode: hit Frankfurter/ECB latest (with holiday fallback to last published business day — Case 21)
        spot = None
        rec_date = None
        source_url = "https://api.frankfurter.app/latest?from=USD&to=INR"
        rhash = _req_hash("fx_latest", {"date": today_str()})
        with db_conn(db_path) as conn:
            row = conn.execute("SELECT * FROM cache WHERE request_hash = ?", (rhash,)).fetchone()
            if row:
                cached = json.loads(row["response_json"])
                spot = float(cached["rates"]["INR"])
                rec_date = cached["date"]
        if spot is not None:
            log_cost(run_id, "fx_official_reference (cache)", 0.0, 0.0, db_path)

        if spot is None:
            for attempt in range(2):
                try:
                    with httpx.Client(timeout=12.0, follow_redirects=True) as client:
                        r = client.get(source_url)
                        r.raise_for_status()
                        fdata = r.json()
                    spot = float(fdata["rates"]["INR"])
                    rec_date = fdata["date"]
                    with db_conn(db_path) as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO cache (request_hash, endpoint, response_json, fetched_at) VALUES (?, ?, ?, ?)",
                            (rhash, source_url, json.dumps(fdata), utc_now()),
                        )
                        conn.execute(
                            "INSERT INTO cost_ledger (run_id, endpoint, credits, tokens, inr_cost, at) VALUES (?, ?, 0.0, 0, 0.0, ?)",
                            (run_id, "fx_official_reference", utc_now()),
                        )
                    break
                except Exception:
                    log_cost(run_id, "fx_official_reference (failed)", 0.0, 0.0, db_path)
                    time.sleep(0.5)

        fx_source_used = source_url
        if spot is None and hist_map:
            # Market holiday or fallback to latest stored business day (Case 21)
            rec_date = sorted(hist_map.keys())[-1]
            spot = hist_map[rec_date]
            fx_source_used = "Stored ECB/Frankfurter history snapshot (hedgedesk/usd_inr_ecb_history.json; live feed failed)"

        if spot is None:
            return {"status": "CANNOT_VERIFY", "reason": "All FX feeds unreachable.", "spot": None, "evidence_ids": []}

        # Staleness gate on the live path (Case 19 for real data, not only the UI toggle): if the rate we
        # ended up with (live or the stored fallback) is older than policy allows, hold instead of deciding.
        max_age = int(cfg["hard_rules"].get("max_market_data_age_business_days", 1))
        missed = business_days_missed(rec_date, today_str())
        if missed > max_age:
            ev_id = add_evidence(
                run_id, "https://api.frankfurter.app/latest?from=USD&to=INR (or stored fallback)",
                "reference_rate_date", f"STALE ({rec_date}, {missed} business days missed)", rec_date, db_path=db_path,
            )
            return {
                "status": "STALE_HOLD",
                "reason": (f"Latest USD/INR rate we could get is from {rec_date} ({missed} business days old); "
                           f"policy allows {max_age}. Holding decisions until a fresh rate is available."),
                "last_available_date": rec_date,
                "spot": round(spot, 4),
                "evidence_ids": [ev_id],
            }

        valid_dates = sorted(d for d in hist_map.keys() if d <= rec_date)
        window_dates = valid_dates[-61:]

    # Sanity bounds validation (Case 20)
    if not (bounds["min_spot_usd_inr"] <= spot <= bounds["max_spot_usd_inr"]):
        return {"status": "CANNOT_VERIFY", "reason": f"Spot {spot} violated sanity bounds.", "spot": None, "evidence_ids": []}

    # Compute 60-trading-day realized daily log-return sigma (sigma_daily)
    import math, statistics
    vals = [hist_map[d] for d in window_dates] if len(window_dates) >= 5 else [spot]
    log_rets = [math.log(vals[i] / vals[i - 1]) for i in range(1, len(vals)) if vals[i - 1] > 0]
    sigma_daily = statistics.stdev(log_rets) if len(log_rets) >= 2 else 0.0022

    # 1M, 3M, 6M annualized forward premia (%) — MODELLED, not fetched. No live FBIL feed is integrated.
    # Base values sit inside the 2025-2026 FBIL range, nudged up for higher realised vol. Labelled as
    # modelled / cannot verify in the evidence ledger so no one reads them as an FBIL quote.
    vol_adj = max(0.0, (sigma_daily * math.sqrt(252) * 100.0 - 3.0) * 0.15)
    premia_pct = {
        "1m": round(2.15 + vol_adj, 3),
        "3m": round(2.45 + vol_adj, 3),
        "6m": round(2.75 + vol_adj, 3),
    }

    ev_ids = []
    if not as_of_date:
        ev1 = add_evidence(
            run_id,
            fx_source_used,
            "rates.INR",
            f"{spot} (published business day {rec_date})",
            rec_date,
            db_path=db_path,
        )
        ev2 = add_evidence(
            run_id,
            "MODELLED in sources.fetch_fx (no live FBIL feed integrated)",
            "forward_premia_annualized (modelled)",
            f"1M={premia_pct['1m']}% | 3M={premia_pct['3m']}% | 6M={premia_pct['6m']}% — cannot verify against FBIL; indicative only, bank quote needed",
            rec_date,
            db_path=db_path,
        )
        ev_ids.extend([ev1, ev2])

        # Case 22: Sources disagree beyond tolerance -> flag both, use fresher official one
        if simulate_disagreement:
            alt_spot = round(spot + 0.65, 4)
            ev3 = add_evidence(
                run_id,
                "SIMULATED (UI test toggle: source disagreement)",
                "rates.INR (Disagreement > 0.5%)",
                f"Simulated secondary={alt_spot} vs primary ECB/Frankfurter={spot}; used official reference {spot}",
                rec_date,
                db_path=db_path,
            )
            ev_ids.append(ev3)

    return {
        "status": "VERIFIED",
        "spot": round(spot, 4),
        "record_date": rec_date,
        "sigma_daily": sigma_daily,
        "ann_vol_pct": round(sigma_daily * (252 ** 0.5) * 100.0, 2),
        "premia_pct": premia_pct,
        "disagreement_flagged": simulate_disagreement,
        "holiday_note": f"Using published business day {rec_date}" if rec_date != today_str() and not as_of_date else None,
        "evidence_ids": ev_ids,
    }
