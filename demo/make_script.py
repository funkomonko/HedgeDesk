"""Writes demo/SCRIPT.md from narration.json and the recorded timeline."""
import json
from pathlib import Path

DEMO = Path(__file__).resolve().parent
lines = json.loads((DEMO / "narration.json").read_text())
tl = json.loads((DEMO / "out" / "timeline.json").read_text())

ON_SCREEN = [
    "Title slide",
    "Problem slide: $200,000 order, +₹3.7 L on a 2% move, ₹24,999/month",
    "Pipeline slide: five steps, checkpoint band",
    "Technical architecture diagram",
    "App opens empty; click Run Demo Story; KPIs fill in",
    "Exposure Book tab, scroll past DUPLICATE_LINKED, LOOKALIKE, bank-change flag",
    "Hedge Decisions tab, scroll through ranges and evidence IDs",
    "Domestic Alternatives tab, open a drafted RFQ",
    "Approvals tab, click Approve on the ₹1.39 Cr forward; refusal toast",
    "Type 'Director, Finance' as approver 2; approve forward and payment",
    "Server killed, page fails to load, server restarts, run resumes; outbox shows Executed Once",
    "Evidence & Cost Ledger tab",
    "18M ECB Backtest tab",
    "Open and close Edit Hedge Parameters; type the request with no amount; HOLD_MISSING_AMOUNT",
    "Tick 'FX Feeds Down', run again; every invoice held",
    "Design decisions slide",
    "What isn't live yet slide",
    "Title slide",
]

def ts(s): return f"{int(s // 60)}:{int(s % 60):02d}"

out = ["# HedgeDesk demo: narration script", "",
       f"Video: `demo/out/hedgedesk_demo_final.mp4`, about {ts(tl[-1][0] + 8)} long. "
       "Times are when each section starts in the video. Read at a normal pace; each section holds until its line is done.", ""]
for (start, _), (_, label, text), screen in zip(tl, lines, ON_SCREEN):
    out += [f"## {ts(start)} · {label}", "", f"*On screen:* {screen}", "", text, ""]
(DEMO / "SCRIPT.md").write_text("\n".join(out), encoding="utf-8")
print("wrote", DEMO / "SCRIPT.md")
