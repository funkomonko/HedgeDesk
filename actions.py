"""
actions.py — Approval queue & exactly-once outbox state machine (`drafted`, `approved`, `sent`, `paid`, `held`).
Implements Context Doc Pages 6, 13-14:
  - Forward booking key = hash(invoice_id_or_bucket, tenor, amount, policy_version)
  - Payment key = hash(invoice_id, amount, due_date)
  - Maker-checker 2-person approval above configured INR threshold
  - Case 24*: Killed after Razorpay API call before DB save -> on restart, queries Razorpay
    receipt registry by idem_key and records the existing payment instead of paying again.
  - All outbound emails routed to internal team inbox (never external suppliers/banks).
"""
import hashlib
import json
from typing import Any, Dict, List, Optional

from sources import db_conn, load_policy_yaml, utc_now

# Simulated Razorpay Test-Mode Receipt Registry (persisted in SQLite `cache` as external provider memory
# so Case 24 crash-before-save can reconcile against external Razorpay order receipt!)
_RAZORPAY_PREFIX = "rzp_receipt_"


def make_forward_idem_key(invoice_id_or_bucket: str, tenor: Any, amount: float, policy_version: str) -> str:
    raw = f"FWD|{invoice_id_or_bucket}|{tenor}|{float(amount):.2f}|{policy_version}"
    return "fwd_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def make_payment_idem_key(invoice_id: str, amount: float, due_date: str) -> str:
    raw = f"PAY|{invoice_id}|{float(amount):.2f}|{due_date}"
    return "pay_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def make_rfq_idem_key(invoice_id: str, maker_id: Any) -> str:
    raw = f"RFQ|{invoice_id}|{maker_id}"
    return "rfq_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def propose(
    run_id: str,
    kind: str,
    invoice_id: str,
    idem_key: str,
    payload: Dict[str, Any],
    held_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Propose an irreversible action into `outbox` in `drafted` (or `held`) state."""
    state = "held" if held_reason else "drafted"
    full_payload = dict(payload)
    if held_reason:
        full_payload["held_reason"] = held_reason

    with db_conn() as conn:
        existing = conn.execute("SELECT * FROM outbox WHERE idem_key = ?", (idem_key,)).fetchone()
        if existing:
            return dict(existing)
        conn.execute(
            """
            INSERT INTO outbox (idem_key, run_id, invoice_id, kind, payload, state)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (idem_key, run_id, invoice_id, kind, json.dumps(full_payload), state),
        )
        row = conn.execute("SELECT * FROM outbox WHERE idem_key = ?", (idem_key,)).fetchone()
        return dict(row)


def approve(
    idem_key: str,
    approver: str = "Finance Controller",
    second_approver: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Approve a drafted outbox item. Enforces dual maker-checker approval if INR notional
    exceeds `dual_approval_threshold_inr` in policy.yaml.
    """
    cfg = load_policy_yaml()["hard_rules"]
    dual_thresh_inr = float(cfg["dual_approval_threshold_inr"])

    with db_conn() as conn:
        row = conn.execute("SELECT * FROM outbox WHERE idem_key = ?", (idem_key,)).fetchone()
        if not row:
            raise ValueError(f"Outbox idem_key not found: {idem_key}")
        if row["state"] == "held":
            raise ValueError(f"Action {idem_key} is HELD and cannot be approved.")
        if row["state"] == "rejected":
            raise ValueError(f"Action {idem_key} was rejected and cannot be approved.")
        if row["state"] in {"sent", "paid"}:
            d = dict(row)
            d["already_executed"] = True
            return d

        payload = json.loads(row["payload"])
        if row["kind"] in {"payment", "forward"} and payload.get("inr_value") is None:
            raise ValueError(f"Action {idem_key} has no verified INR value, so the approval limit can't be checked. Not approved.")
        inr_val = float(payload.get("inr_value") or 0.0)
        if inr_val > dual_thresh_inr and not second_approver:
            raise ValueError(
                f"Maker-Checker required: INR {inr_val:,.0f} exceeds dual_approval_threshold_inr "
                f"(INR {dual_thresh_inr:,.0f}). Provide second_approver."
            )

        conn.execute(
            "UPDATE outbox SET state = 'approved', approved_by = ?, second_approver = ? WHERE idem_key = ?",
            (approver, second_approver, idem_key),
        )
        return dict(conn.execute("SELECT * FROM outbox WHERE idem_key = ?", (idem_key,)).fetchone())


def reject(idem_key: str, rejected_by: str, reason: str) -> Dict[str, Any]:
    """
    A person refuses a proposed action. Allowed until it has executed; the reason, who and when are
    kept on the row. Because outbox keys come from the action's content, re-running the same invoices
    finds this row again, so a rejected payment stays rejected instead of being re-proposed.
    """
    reason = (reason or "").strip()
    who = (rejected_by or "").strip()
    if not reason or not who:
        raise ValueError("A rejection needs the reviewer's name and a reason.")
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM outbox WHERE idem_key = ?", (idem_key,)).fetchone()
        if not row:
            raise ValueError(f"Outbox idem_key not found: {idem_key}")
        if row["state"] in {"sent", "paid"}:
            raise ValueError(f"Action {idem_key} already executed ({row['external_ref']}); it can't be rejected now.")
        if row["state"] == "rejected":
            d = dict(row)
            d["already_rejected"] = True
            return d
        payload = json.loads(row["payload"])
        payload.update({"rejected_by": who, "rejected_reason": reason, "rejected_at": utc_now(),
                        "state_before_rejection": row["state"]})
        conn.execute("UPDATE outbox SET state = 'rejected', payload = ? WHERE idem_key = ?",
                     (json.dumps(payload), idem_key))
        return dict(conn.execute("SELECT * FROM outbox WHERE idem_key = ?", (idem_key,)).fetchone())


def _razorpay_test_lookup_or_create(idem_key: str, amount_inr: float) -> str:
    """
    Simulates Razorpay Test Mode `/orders` with `receipt = idem_key`.
    Persisted in SQLite `cache` table under `rzp_receipt_<idem_key>` so if the process
    crashes AFTER calling Razorpay but BEFORE updating `outbox` (Case 24*), restarting
    and querying Razorpay by receipt returns the EXISTING payment ID instead of paying twice!
    """
    rhash = _RAZORPAY_PREFIX + idem_key
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM cache WHERE request_hash = ?", (rhash,)).fetchone()
        if row:
            data = json.loads(row["response_json"])
            return data["razorpay_payment_id"]

        pay_id = "pay_rzptest_" + hashlib.sha256(idem_key.encode()).hexdigest()[:12]
        order_rec = {
            "razorpay_payment_id": pay_id,
            "receipt": idem_key,
            "amount_paise": int(round(amount_inr * 100)),
            "currency": "INR",
            "mode": "test",
            "status": "authorized_test",
        }
        conn.execute(
            "INSERT INTO cache (request_hash, endpoint, response_json, fetched_at) VALUES (?, ?, ?, ?)",
            (rhash, "razorpay_test_api/v1/payments", json.dumps(order_rec), utc_now()),
        )
        return pay_id


def execute(idem_key: str, simulate_kill_before_save: bool = False) -> Dict[str, Any]:
    """
    Execute an approved outbox action exactly once.
    Checks outbox state first: if `sent` or `paid`, returns stored external_ref and does nothing.
    Supports Case 24*: if killed right after external payment API call before outbox commit,
    re-running `execute()` reconciles via Razorpay receipt ID and does NOT double-pay.
    """
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM outbox WHERE idem_key = ?", (idem_key,)).fetchone()
        if not row:
            raise ValueError(f"Outbox idem_key not found: {idem_key}")

        d = dict(row)
        if d["state"] in {"sent", "paid"}:
            d["already_executed"] = True
            return d
        if d["state"] == "held":
            raise ValueError("Cannot execute a HELD action.")
        if d["state"] == "rejected":
            raise ValueError("Cannot execute a rejected action.")
        if d["state"] != "approved":
            raise ValueError(f"Action {idem_key} must be approved first (current state={d['state']}).")

        payload = json.loads(d["payload"])
        kind = d["kind"]

    # External execution step (outside DB update to test Case 24 crash boundary)
    if kind == "payment":
        ext_ref = _razorpay_test_lookup_or_create(idem_key, float(payload.get("inr_value") or 1000.0))
        final_state = "paid"
    elif kind == "forward":
        ext_ref = f"HDFC-FWD-{idem_key[4:12].upper()}"
        final_state = "sent"
    else:
        # Outbound RFQ goes to internal team inbox during hackathon per rules (Page 17)
        ext_ref = f"TEAM-INBOX-RFQ-{idem_key[4:12].upper()}"
        final_state = "sent"

    if simulate_kill_before_save:
        raise RuntimeError(
            f"Simulated crash AFTER calling external provider (ref={ext_ref}) BEFORE saving outbox state! "
            f"Re-calling execute('{idem_key}') will reconcile via receipt and avoid double-pay."
        )

    now = utc_now()
    with db_conn() as conn:
        conn.execute(
            "UPDATE outbox SET state = ?, executed_at = ?, external_ref = ? WHERE idem_key = ?",
            (final_state, now, ext_ref, idem_key),
        )
        updated = dict(conn.execute("SELECT * FROM outbox WHERE idem_key = ?", (idem_key,)).fetchone())
        updated["already_executed"] = False
        return updated


def get_outbox(run_id: str) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute("SELECT * FROM outbox WHERE run_id = ? ORDER BY rowid ASC", (run_id,)).fetchall()
        res = []
        for r in rows:
            d = dict(r)
            try:
                d["payload_obj"] = json.loads(d["payload"])
            except Exception:
                d["payload_obj"] = {}
            res.append(d)
        return res
