"""
privacy.py: what personal data HedgeDesk holds, and purging it on request.

The only personal data in the system comes from bank-detail change requests: the sender's name and
email on the invoice row, and the slimmed Person Search reply (name, role, employer) used to check
them. purge() removes those values from every table, per supplier domain or for everything, and
records that a purge happened (counts only, no personal values).
"""
import json
from typing import Any, Dict, List, Optional, Set

from sources import add_evidence, db_conn, today_str, utc_now

REDACTED = "[personal data purged]"

# (table, text column, key column) pairs that can carry copies of invoice rows or evidence text
_TEXT_COLUMNS = [
    ("invoices", "raw_json", "rowid"),
    ("steps", "output_ref", "rowid"),
    ("decisions", "details_json", "rowid"),
    ("outbox", "payload", "rowid"),
    ("evidence", "value", "id"),
]


def _personal_values(supplier_domain: Optional[str]) -> Set[str]:
    vals: Set[str] = set()
    dom = (supplier_domain or "").lower().strip()
    with db_conn() as conn:
        for row in conn.execute("SELECT raw_json FROM invoices").fetchall():
            try:
                inv = json.loads(row["raw_json"])
            except Exception:
                continue
            inv_dom = str(inv.get("supplier_domain") or (inv.get("supplier") or {}).get("primary_domain") or "").lower()
            if dom and inv_dom != dom:
                continue
            for k in ("bank_change_sender", "bank_change_email"):
                v = str(inv.get(k) or "").strip()
                if v:
                    vals.add(v)
                    if k == "bank_change_sender":
                        vals.add(v.split("(")[0].strip())
    return {v for v in vals if len(v) >= 3}


def summary() -> Dict[str, Any]:
    """Counts of personal data held, without showing it."""
    with db_conn() as conn:
        people_cache = conn.execute("SELECT COUNT(*) FROM cache WHERE endpoint LIKE '/person/%'").fetchone()[0]
        people_ev = conn.execute("SELECT COUNT(*) FROM evidence WHERE source LIKE '%/person/%'").fetchone()[0]
    return {
        "bank_change_contacts_held": len(_personal_values(None)),
        "person_search_replies_cached": people_cache,
        "person_search_evidence_rows": people_ev,
        "fields_kept_from_person_search": ["name", "role", "employer"],
        "used_for": "Checking that a bank-detail change request comes from someone who works at the supplier.",
    }


def purge(supplier_domain: Optional[str] = None, requested_by: str = "") -> Dict[str, Any]:
    """Redact bank-change contacts (for one supplier domain, or all) and drop cached Person Search replies."""
    values = sorted(_personal_values(supplier_domain), key=len, reverse=True)
    changed: Dict[str, int] = {}
    with db_conn() as conn:
        for table, col, key in _TEXT_COLUMNS:
            n = 0
            for row in conn.execute(f"SELECT {key} AS k, {col} AS v FROM {table}").fetchall():
                text = row["v"]
                if not isinstance(text, str):
                    continue
                new = text
                for v in values:
                    new = new.replace(json.dumps(v)[1:-1], REDACTED).replace(v, REDACTED)
                if new != text:
                    conn.execute(f"UPDATE {table} SET {col} = ? WHERE {key} = ?", (new, row["k"]))
                    n += 1
            changed[table] = n
        # Cached Person Search replies are keyed by an opaque request hash, so they can't be matched to
        # one supplier reliably: drop them all. They hold only name/role/employer and are re-fetched if needed.
        changed["cache_person_search"] = conn.execute("DELETE FROM cache WHERE endpoint LIKE '/person/%'").rowcount
    ev_id = add_evidence(
        "privacy", "privacy.purge", "personal_data_purge",
        f"Purged {len(values)} personal values ({'domain ' + supplier_domain if supplier_domain else 'all suppliers'}) "
        f"on request of {requested_by or 'unspecified'}; rows changed: {json.dumps(changed)}",
        today_str(),
    )
    return {"purged_values": len(values), "rows_changed": changed, "at": utc_now(), "audit_evidence_id": ev_id}
