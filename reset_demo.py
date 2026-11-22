"""reset_demo.py — Clears execution runs/outbox while preserving the Crustdata API cache so live demos run fast and spend zero extra credits."""
from sources import db_conn

with db_conn() as conn:
    conn.execute("DELETE FROM invoices")
    conn.execute("DELETE FROM decisions")
    conn.execute("DELETE FROM outbox")
    conn.execute("DELETE FROM steps")
    conn.execute("DELETE FROM evidence")
    conn.execute("DELETE FROM cost_ledger")
print("Cleared demo run tables (API cache preserved for instant replay).")
