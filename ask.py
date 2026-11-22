"""
ask.py: answer plain-English questions about a batch that has already been run.

No model is involved. The question is matched to one or more fixed intents (hedge plan, holds,
local sourcing, approvals, cost, duplicates), optionally narrowed to one supplier or invoice named in
the question, and every line of the answer is built from the run's own decisions, outbox and
evidence IDs. A question that matches nothing gets "cannot answer" and the list of what can be asked,
never a guess.
"""
import re
from typing import Any, Dict, List

HEDGE_ACTIONS = {"HEDGE_FORWARD", "ROLL_EXISTING_FORWARD"}

INTENTS = [
    ("hedge", r"\b(hedg\w*|forwards?|cover|exposure|plan|var|risk)\b"),
    ("local", r"\b(local\w*|domestic|india\w*|source[sd]?|sourcing|makers?|manufactur\w*|rfq|substitut\w*)\b"),
    ("holds", r"\b(hold|held|block\w*|why not|cannot verify|can't verify|flag\w*|problem\w*|issues?|wrong)\b"),
    ("approvals", r"\b(approv\w*|pay|payments?|paid|execut\w*|outbox|pending|sign[- ]?off)\b"),
    ("cost", r"\b(cost of (the )?(run|job)|credits?|spend|spent|ledger|how much did)\b"),
    ("duplicates", r"\b(duplicat\w*|twice|double|same invoice)\b"),
]
HELP = ("I can answer about: the hedge plan, what is held and why, what could be sourced locally, "
        "what is waiting for approval, duplicates, and the run's cost. Add a supplier or invoice number to narrow it.")


def _fmt_usd(v: float) -> str:
    return f"${v:,.0f}"


def _inr(v: float) -> str:
    """Indian digit grouping: 13984710 -> 1,39,84,710."""
    n = int(round(abs(float(v))))
    s = str(n)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        s = head + "," + tail
    return ("-" if float(v) < 0 else "") + "₹" + s


def _fmt_inr(v: Any) -> str:
    return "cannot verify (no FX rate)" if v is None else _inr(v)


def _scope(question: str, run: Dict[str, Any]) -> List[str]:
    """Invoice IDs the question is about: those named directly or whose supplier is named; else all."""
    q = question.lower()
    invs = run.get("book", {}).get("invoices", [])
    hits = [i["invoice_id"] for i in invs if i["invoice_id"].lower() in q]
    if hits:
        return hits
    stop = {"ltd", "inc", "gmbh", "company", "co", "corp", "pvt", "limited", "the", "global", "and"}
    for i in invs:
        names = {str(i.get("supplier_raw") or ""), str((i.get("supplier") or {}).get("legal_name") or "")}
        toks = {t for n in names for t in re.findall(r"[a-z0-9]+", n.lower()) if len(t) > 2 and t not in stop}
        if toks and any(re.search(rf"\b{re.escape(t)}\b", q) for t in toks):
            hits.append(i["invoice_id"])
    return hits or [i["invoice_id"] for i in invs]


def answer(question: str, run: Dict[str, Any]) -> Dict[str, Any]:
    q = question.lower()
    intents = [name for name, pat in INTENTS if re.search(pat, q)]
    scope = _scope(question, run)
    named = len(scope) < len(run.get("book", {}).get("invoices", []))
    if not intents:
        if named:
            intents = ["hedge", "holds"]  # "what about SKF?" -> its decision and any hold
        else:
            return {"run_id": run.get("run_id"), "intents": [], "lines": [], "answered": False,
                    "text": f"Cannot answer that from this run. {HELP}"}

    decs = {d["invoice_id"]: d for d in run.get("decisions", []) if d["invoice_id"] in scope}
    invs = {i["invoice_id"]: i for i in run.get("book", {}).get("invoices", []) if i["invoice_id"] in scope}
    lines: List[Dict[str, Any]] = []

    def add(text: str, ev: List[int] = None):
        lines.append({"text": text, "evidence_ids": sorted(set(ev or []))[:6]})

    conf = int((run.get("summary") or {}).get("var_confidence_pct") or 95)
    if "hedge" in intents:
        hedged = [d for d in decs.values() if d["action"] in HEDGE_ACTIONS]
        tot = sum(d["hedged_usd"] for d in hedged)
        add(f"Hedge plan: {len(hedged)} of {len(decs)} invoices get a forward, {_fmt_usd(tot)} in total "
            f"(premiums are modelled, so rates are indicative until the bank quotes).")
        for d in hedged:
            add(f"{d['invoice_id']}: hedge {round(d['ratio'] * 100)}% ({_fmt_usd(d['hedged_usd'])}) at ₹{d['forward_rate']}, "
                f"clause {d['policy_clause']}, {conf}% VaR {_inr(d['var_95_inr'])}.", d.get("evidence_ids"))
    if "holds" in intents or "duplicates" in intents:
        held = [d for d in decs.values() if d["action"] not in HEDGE_ACTIONS]
        if "duplicates" in intents and "holds" not in intents:
            held = [d for d in held if "DUPLICATE" in d["action"] or "CONFIRM_DISTINCT" in d["action"]]
        only_dups = "duplicates" in intents and "holds" not in intents
        if held:
            add(f"{len(held)} {'possible duplicate(s)' if only_dups else 'invoice(s) not hedged'}:")
        else:
            add("No duplicates in scope." if only_dups else "Nothing in scope is held.")
        for d in held:
            add(f"{d['invoice_id']}: {d['action']}. {d['reason']}", d.get("evidence_ids"))
    if "local" in intents:
        alts = {k: v for k, v in (run.get("alternatives") or {}).items() if k in scope and v.get("status") != "SKIPPED"}
        if not alts:
            add("No invoice in scope was eligible for a domestic search (only hedged imports with a quantity are checked).")
        for inv_id, a in alts.items():
            if a.get("status") == "CANNOT_VERIFY_UNIT_PRICE":
                add(f"{inv_id}: cannot verify a per-unit break-even, quantity not given.")
                continue
            makers = [m["name"] for m in a.get("makers", [])]
            add(f"{inv_id}: buy locally only if an Indian maker quotes at or below {_inr(a.get('break_even_inr', 0))}/unit "
                f"(hedged landed import cost {_inr(a.get('landed_import_inr', 0))}/unit). Likely makers: "
                f"{', '.join(makers) if makers else 'none found'}. Their prices: cannot verify, RFQ drafted.",
                a.get("evidence_ids"))
    if "approvals" in intents:
        ob = [o for o in run.get("outbox", []) if o.get("invoice_id") in scope]
        by_state: Dict[str, int] = {}
        for o in ob:
            by_state[o["state"]] = by_state.get(o["state"], 0) + 1
        add("Outbox: " + ", ".join(f"{n} {s}" for s, n in sorted(by_state.items())) + ". Nothing is paid or sent without a person's approval.")
        for o in ob:
            if o["state"] == "drafted" and o["kind"] in {"payment", "forward"}:
                p = o.get("payload_obj") or {}
                add(f"Waiting for approval: {p.get('title', o['kind'])} ({_fmt_inr(p.get('inr_value'))}).")
    if "cost" in intents:
        c = run.get("cost_ledger") or {}
        s = run.get("summary") or {}
        add(f"This run: {c.get('call_count', 0)} logged calls, {c.get('total_credits', 0)} Crustdata credits, "
            f"₹{c.get('total_inr_cost', 0):.2f} measured cost, {s.get('run_time_s')}s of the {s.get('run_time_limit_s')}s limit.")

    return {"run_id": run.get("run_id"), "intents": intents, "scope": scope if named else "all",
            "lines": lines, "answered": True, "text": "\n".join(l["text"] for l in lines)}
