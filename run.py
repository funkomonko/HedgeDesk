"""
run.py — Terminal entrypoint for HedgeDesk.
Supports real Ctrl+C kill and automatic SQLite checkpoint resume when re-run!
Usage:
  python run.py                         # Runs sample_invoices.csv (resumes if previously killed mid-run)
  python run.py path/to/invoices.csv    # Runs custom CSV (resumes if interrupted)
"""
import hashlib
import json
import sys
from pathlib import Path

from book import load_invoices
from engine import run_pipeline
from sources import BASE_DIR


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else str(BASE_DIR / "sample_invoices.csv")
    rows = load_invoices(csv_path)
    run_id = "cli-" + hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:10]
    print(f"Starting HedgeDesk run_id={run_id} ({len(rows)} rows from {Path(csv_path).name})...")
    print("Press Ctrl+C at any time to simulate Kill-on-Stage, then run `python run.py` again to resume!\n")
    res = run_pipeline(run_id=run_id, invoices_input=rows)
    for line in res["step_logs"]:
        print("  *", line)
    print("\nSummary:", json.dumps(res["summary"], indent=2))
    print("\nOutbox Items:", len(res["outbox"]))
    for o in res["outbox"][:8]:
        print(f"  - [{o['state']}] {o['kind']} | {o['invoice_id']} | key={o['idem_key']} | ref={o.get('external_ref')}")


if __name__ == "__main__":
    main()
