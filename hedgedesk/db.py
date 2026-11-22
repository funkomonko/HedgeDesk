import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DB_PATH

_LOCK = threading.RLock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_idempotency_key(*parts: Any) -> str:
    raw = "|".join(str(p).strip().lower() for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class StateDB:
    """Durable SQLite store for crash recovery, API cache, evidence ledger, and idempotent actions."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self._init_schema()

    @contextmanager
    def connect(self):
        with _LOCK:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _init_schema(self):
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    case_name TEXT,
                    client_name TEXT,
                    status TEXT NOT NULL,
                    current_step INTEGER DEFAULT 0,
                    simulate_kill_at_step INTEGER DEFAULT 0,
                    budget_limit_credits REAL DEFAULT 25.0,
                    request_json TEXT NOT NULL,
                    policy_json TEXT,
                    result_json TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS step_checkpoints (
                    run_id TEXT NOT NULL,
                    step_num INTEGER NOT NULL,
                    step_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, step_num)
                );

                CREATE TABLE IF NOT EXISTS api_cache (
                    cache_key TEXT PRIMARY KEY,
                    endpoint TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    headers_json TEXT NOT NULL,
                    credits_used REAL DEFAULT 0.0,
                    request_id TEXT,
                    fetched_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cost_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    request_id TEXT,
                    credits_used REAL DEFAULT 0.0,
                    cost_inr REAL DEFAULT 0.0,
                    cache_hit INTEGER DEFAULT 0,
                    note TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evidence_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    claim TEXT NOT NULL,
                    value TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    field_path TEXT NOT NULL,
                    passage TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    request_id TEXT
                );

                CREATE TABLE IF NOT EXISTS idempotent_actions (
                    idempotency_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    invoice_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    counterparty TEXT NOT NULL,
                    amount_display TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    execution_ref TEXT,
                    approved_by TEXT,
                    created_at TEXT NOT NULL,
                    executed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS invoice_fingerprint_history (
                    fingerprint TEXT PRIMARY KEY,
                    first_run_id TEXT NOT NULL,
                    first_invoice_id TEXT NOT NULL,
                    supplier_name TEXT NOT NULL,
                    amount_usd REAL NOT NULL,
                    due_date TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    # --- Run & Checkpoint helpers ---
    def create_or_load_run(
        self,
        run_id: str,
        case_name: str,
        client_name: str,
        request_data: dict,
        policy_data: dict,
        simulate_kill_at_step: int = 0,
        budget_limit_credits: float = 25.0,
    ) -> dict:
        now = utc_now_iso()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is not None:
                conn.execute(
                    """
                    UPDATE runs
                    SET status = 'RUNNING', simulate_kill_at_step = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (simulate_kill_at_step, now, run_id),
                )
                return dict(conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, case_name, client_name, status, current_step,
                    simulate_kill_at_step, budget_limit_credits, request_json,
                    policy_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'RUNNING', 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    case_name,
                    client_name,
                    simulate_kill_at_step,
                    budget_limit_credits,
                    json.dumps(request_data),
                    json.dumps(policy_data),
                    now,
                    now,
                ),
            )
            return dict(conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())

    def get_run(self, run_id: str) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return dict(row) if row else None

    def list_runs(self, limit: int = 25) -> List[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT run_id, case_name, client_name, status, current_step, created_at, updated_at FROM runs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_run_status(
        self,
        run_id: str,
        status: str,
        current_step: Optional[int] = None,
        result_data: Optional[dict] = None,
        error_message: Optional[str] = None,
    ):
        now = utc_now_iso()
        with self.connect() as conn:
            fields = ["status = ?", "updated_at = ?"]
            vals: List[Any] = [status, now]
            if current_step is not None:
                fields.append("current_step = ?")
                vals.append(current_step)
            if result_data is not None:
                fields.append("result_json = ?")
                vals.append(json.dumps(result_data))
            if error_message is not None:
                fields.append("error_message = ?")
                vals.append(error_message)
            vals.append(run_id)
            conn.execute(f"UPDATE runs SET {', '.join(fields)} WHERE run_id = ?", vals)

    def get_checkpoint(self, run_id: str, step_num: int) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM step_checkpoints WHERE run_id = ? AND step_num = ? AND status = 'COMPLETED'",
                (run_id, step_num),
            ).fetchone()
            if not row:
                return None
            return json.loads(row["payload_json"])

    def save_checkpoint(self, run_id: str, step_num: int, step_name: str, payload: Any):
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO step_checkpoints (run_id, step_num, step_name, status, payload_json, completed_at)
                VALUES (?, ?, ?, 'COMPLETED', ?, ?)
                """,
                (run_id, step_num, step_name, json.dumps(payload), now),
            )
            conn.execute(
                "UPDATE runs SET current_step = ?, updated_at = ? WHERE run_id = ?",
                (step_num, now, run_id),
            )

    def get_all_checkpoints(self, run_id: str) -> List[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT step_num, step_name, status, completed_at FROM step_checkpoints WHERE run_id = ? ORDER BY step_num ASC",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # --- API Cache helpers ---
    def get_cached_api(self, cache_key: str) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM api_cache WHERE cache_key = ?", (cache_key,)).fetchone()
            if not row:
                return None
            return {
                "response": json.loads(row["response_json"]),
                "headers": json.loads(row["headers_json"]),
                "credits_used": row["credits_used"],
                "request_id": row["request_id"],
                "fetched_at": row["fetched_at"],
            }

    def set_cached_api(
        self,
        cache_key: str,
        endpoint: str,
        request_data: dict,
        response_data: Any,
        headers_data: dict,
        credits_used: float,
        request_id: str,
    ):
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO api_cache (
                    cache_key, endpoint, request_json, response_json,
                    headers_json, credits_used, request_id, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    endpoint,
                    json.dumps(request_data),
                    json.dumps(response_data),
                    json.dumps(headers_data),
                    credits_used,
                    request_id,
                    now,
                ),
            )

    # --- Cost Ledger helpers ---
    def log_cost(
        self,
        run_id: str,
        step_name: str,
        provider: str,
        endpoint: str,
        request_id: str,
        credits_used: float,
        cost_inr: float,
        cache_hit: bool = False,
        note: str = "",
    ):
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO cost_ledger (
                    run_id, step_name, provider, endpoint, request_id,
                    credits_used, cost_inr, cache_hit, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    step_name,
                    provider,
                    endpoint,
                    request_id,
                    round(float(credits_used), 4),
                    round(float(cost_inr), 4),
                    1 if cache_hit else 0,
                    note,
                    utc_now_iso(),
                ),
            )

    def get_run_costs(self, run_id: str) -> Dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cost_ledger WHERE run_id = ? ORDER BY id ASC", (run_id,)
            ).fetchall()
            items = [dict(r) for r in rows]
            total_credits = sum(r["credits_used"] for r in items if not r["cache_hit"])
            total_inr = sum(r["cost_inr"] for r in items if not r["cache_hit"])
            return {
                "items": items,
                "total_credits_billed": round(total_credits, 4),
                "total_cost_inr": round(total_inr, 2),
                "cache_hits": sum(1 for r in items if r["cache_hit"]),
                "live_calls": sum(1 for r in items if not r["cache_hit"]),
            }

    # --- Evidence Ledger helpers ---
    def clear_run_evidence(self, run_id: str):
        with self.connect() as conn:
            conn.execute("DELETE FROM evidence_ledger WHERE run_id = ?", (run_id,))

    def add_evidence(
        self,
        run_id: str,
        entity_id: str,
        claim: str,
        value: str,
        verdict: str,
        source_name: str,
        source_url: str,
        field_path: str,
        passage: str,
        retrieved_at: Optional[str] = None,
        request_id: str = "",
    ):
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO evidence_ledger (
                    run_id, entity_id, claim, value, verdict, source_name,
                    source_url, field_path, passage, retrieved_at, request_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    entity_id,
                    claim,
                    value,
                    verdict,
                    source_name,
                    source_url,
                    field_path,
                    passage,
                    retrieved_at or utc_now_iso(),
                    request_id,
                ),
            )

    def get_run_evidence(self, run_id: str) -> List[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evidence_ledger WHERE run_id = ? ORDER BY id ASC", (run_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # --- Idempotent Actions (Human Approval Gate & Double-Pay Protection) ---
    def register_action_idempotent(
        self,
        idempotency_key: str,
        run_id: str,
        invoice_id: str,
        action_type: str,
        status: str,
        title: str,
        counterparty: str,
        amount_display: str,
        payload: dict,
    ) -> dict:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM idempotent_actions WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                return dict(existing)
            now = utc_now_iso()
            conn.execute(
                """
                INSERT INTO idempotent_actions (
                    idempotency_key, run_id, invoice_id, action_type, status,
                    title, counterparty, amount_display, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    run_id,
                    invoice_id,
                    action_type,
                    status,
                    title,
                    counterparty,
                    amount_display,
                    json.dumps(payload),
                    now,
                ),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM idempotent_actions WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
            )

    def get_run_actions(self, run_id: str) -> List[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM idempotent_actions WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
            ).fetchall()
            res = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d.pop("payload_json"))
                res.append(d)
            return res

    def execute_approved_action(self, idempotency_key: str, approver: str) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM idempotent_actions WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown action idempotency_key: {idempotency_key}")
            if row["status"] == "APPROVED_EXECUTED":
                d = dict(row)
                d["already_executed"] = True
                d["payload"] = json.loads(d.pop("payload_json"))
                return d
            if row["status"] == "HELD_EXCEPTION":
                raise ValueError("Action is HELD due to exception/duplicate/cannot-verify and cannot be auto-executed.")

            now = utc_now_iso()
            prefix = {
                "BANK_FORWARD_BOOKING": "HDFC-FX-FWD",
                "SUPPLIER_PAYMENT": "RZP-TEST-PAY",
                "DOMESTIC_RFQ": "RFQ-SENT",
            }.get(row["action_type"], "ACT")
            exec_ref = f"{prefix}-{idempotency_key[:8].upper()}"

            conn.execute(
                """
                UPDATE idempotent_actions
                SET status = 'APPROVED_EXECUTED', execution_ref = ?, approved_by = ?, executed_at = ?
                WHERE idempotency_key = ?
                """,
                (exec_ref, approver, now, idempotency_key),
            )
            updated = dict(
                conn.execute(
                    "SELECT * FROM idempotent_actions WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
            )
            updated["already_executed"] = False
            updated["payload"] = json.loads(updated.pop("payload_json"))
            return updated
