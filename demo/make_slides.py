"""Builds the static slides used in the demo recording (demo/slides/*.html)."""
from pathlib import Path

OUT = Path(__file__).parent / "slides"
OUT.mkdir(exist_ok=True)

CSS = """
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{font-family:Inter,-apple-system,Segoe UI,sans-serif;background:#f5f5f3;color:#111110;display:flex;flex-direction:column;padding:64px 88px;-webkit-font-smoothing:antialiased}
.top{display:flex;align-items:center;gap:14px;margin-bottom:36px}.top img{height:46px;mix-blend-mode:multiply}
.top span{font-weight:700;font-size:20px;letter-spacing:-.01em}.top em{font-style:normal;color:#77736d;font-size:14px;margin-left:auto;font-family:'JetBrains Mono',monospace}
h1{font-size:46px;letter-spacing:-.03em;margin:0 0 14px;line-height:1.1}h2{font-size:34px;letter-spacing:-.025em;margin:0 0 26px}
p.lead{font-size:21px;color:#3f3d3a;max-width:1000px;line-height:1.5;margin:0}
.mono{font-family:'JetBrains Mono',ui-monospace,monospace}
.grid{display:grid;gap:14px}.g2{grid-template-columns:1fr 1fr}.g3{grid-template-columns:repeat(3,1fr)}
.card{background:#fff;border:1px solid #e7e5e2;border-radius:14px;padding:18px 20px}
.card h3{margin:0 0 6px;font-size:16px}.card p{margin:0;color:#3f3d3a;font-size:14.5px;line-height:1.5}
.k{font-size:30px;font-weight:600;font-family:'JetBrains Mono',monospace;letter-spacing:-.03em}
.lbl{font-size:13px;color:#77736d;margin-bottom:6px}
.flow{display:flex;align-items:stretch;gap:10px;margin-bottom:16px}
.node{flex:1;background:#fff;border:1.5px solid #111110;border-radius:12px;padding:12px 14px}
.node b{display:block;font-size:14px}.node small{display:block;color:#77736d;font-size:12px;margin-top:4px;font-family:'JetBrains Mono',monospace}
.arrow{align-self:center;font-size:22px;color:#77736d}
.band{background:#111110;color:#fff;border-radius:12px;padding:14px 18px;font-size:14px;margin-bottom:14px}
.band b{font-weight:600}.muted{color:#77736d}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e7e5e2;border-radius:12px;overflow:hidden;font-size:14px}
td,th{padding:11px 14px;border-bottom:1px solid #e7e5e2;text-align:left;vertical-align:top}th{font-size:12px;color:#77736d;background:#fafaf9}
td:first-child{font-weight:600;width:26%}tr:last-child td{border:none}
.tag{display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid #fbdba7;background:#fff7e8;color:#b45309;font-family:'JetBrains Mono',monospace}
"""
HEAD = ('<!doctype html><html><head><meta charset="utf-8"><link href="https://fonts.googleapis.com/css2?'
        'family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">'
        f'<style>{CSS}</style></head><body>'
        '<div class="top"><img src="../logo-mark.jpg"><span>HedgeDesk</span><em>{n}</em></div>')

slides = {}

slides["1_title"] = """
<div style="margin:auto 0">
<h1>An FX desk for Indian importers</h1>
<p class="lead">HedgeDesk reads a company's open dollar invoices, decides how much of each to lock in with a bank forward inside the board's own policy, checks each supplier, looks for Indian manufacturers of the same part, and queues every payment and booking for a person to approve.</p>
<p class="lead mono" style="margin-top:28px;font-size:15px;color:#77736d">ContextCon 05 · BITS Pilani · Buying and paying track</p>
</div>"""

slides["2_problem"] = """
<h2>Who pays for this today</h2>
<div class="grid g3" style="margin-bottom:18px">
 <div class="card"><div class="lbl">Typical order</div><div class="k">$200,000</div><p>Bearings from Germany, due in 60 days</p></div>
 <div class="card"><div class="lbl">If the rupee falls 2%</div><div class="k">+₹3.7 L</div><p>Extra cost on that one order</p></div>
 <div class="card"><div class="lbl">Our price</div><div class="k">₹24,999/mo</div><p>An advisory retainer is about ₹45,000 a cycle</p></div>
</div>
<div class="grid g2">
 <div class="card"><h3>The buyer</h3><p>CFO or finance head of a mid-size importer with regular USD payables. Large firms have treasury desks; mid-size firms leave the exposure open or take the bank RM's suggestion, and the RM earns on the forward spread.</p></div>
 <div class="card"><h3>The job we replace</h3><p>Treasury analyst or FX advisor: size the hedge per invoice, check the counterparty, prepare the bank request and payment, and keep a record of why.</p></div>
</div>"""

slides["3_arch"] = """
<h2>Architecture</h2>
<div class="flow">
 <div class="node"><b>Invoices</b><small>CSV · typed request</small></div><div class="arrow">→</div>
 <div class="node"><b>1 · Book</b><small>book.py</small><small>parse, KYB, dedupe</small></div><div class="arrow">→</div>
 <div class="node"><b>2 · Market</b><small>sources.fetch_fx</small><small>spot, 60d vol, bounds</small></div><div class="arrow">→</div>
 <div class="node"><b>3 · Policy</b><small>policy.py · no I/O</small><small>VaR, cost, ratio</small></div><div class="arrow">→</div>
 <div class="node"><b>4 · Alternatives</b><small>alternatives.py</small><small>makers, break-even</small></div><div class="arrow">→</div>
 <div class="node"><b>5 · Outbox</b><small>actions.py</small><small>forward, payment, RFQ</small></div>
</div>
<div class="band"><b>Checkpoint after every step.</b> Each step writes its output to the SQLite <span class="mono">steps</span> table before the next one starts. A restart with the same input skips finished steps.</div>
<div class="grid g3">
 <div class="card"><h3>sources.py: the only door out</h3><p>Crustdata (identify, enrich, person search, company search) and ECB rates via Frankfurter. Response cache keyed by request hash, budget check before each paid call, backoff on 429/5xx, hard stop on 402.</p></div>
 <div class="card"><h3>Ledgers</h3><p><span class="mono">evidence</span>: source, field, value, record date for every fact.<br><span class="mono">cost_ledger</span>: one row per call, including failures and cache hits.</p></div>
 <div class="card"><h3>Approval</h3><p>Nothing in the outbox runs until a person clicks Approve. Above ₹1 Cr it needs a second approver. Execute checks the stored state and the payment receipt before paying.</p></div>
</div>"""

B = 'fill="#fff" stroke="#111110" stroke-width="1.5" rx="10"'
G = 'fill="#fafaf9" stroke="#d6d3cf" stroke-width="1.2" rx="10"'
T = 'font-family="Inter" font-size="14" font-weight="600" fill="#111110"'
S = 'font-family="JetBrains Mono" font-size="11.5" fill="#57534e"'
L = 'stroke="#111110" stroke-width="1.4" fill="none" marker-end="url(#a)"'
D = 'stroke="#77736d" stroke-width="1.2" fill="none" stroke-dasharray="4 4" marker-end="url(#a2)"'
slides["4_tech"] = f"""
<h2 style="margin-bottom:14px">Technical architecture</h2>
<svg viewBox="0 0 1264 640" style="width:100%;height:auto">
<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#111110"/></marker>
<marker id="a2" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#77736d"/></marker></defs>

<!-- clients -->
<text x="0" y="16" {S}>CLIENTS</text>
<rect x="0" y="28" width="200" height="66" {B}/><text x="16" y="56" {T}>Browser</text><text x="16" y="78" {S}>static/index.html</text>
<rect x="0" y="110" width="200" height="66" {B}/><text x="16" y="138" {T}>CLI</text><text x="16" y="160" {S}>run.py · Ctrl+C safe</text>

<!-- api -->
<text x="250" y="16" {S}>API · FastAPI on uvicorn</text>
<rect x="250" y="28" width="300" height="236" {B}/><text x="266" y="56" {T}>app.py</text>
<text x="266" y="84" {S}>POST /api/run · /api/upload-csv</text>
<text x="266" y="106" {S}>GET  /api/latest-run  (resume)</text>
<text x="266" y="128" {S}>POST /api/outbox/{{key}}/</text>
<text x="266" y="146" {S}>     approve-and-execute</text>
<text x="266" y="168" {S}>GET/POST /api/policy</text>
<text x="266" y="190" {S}>GET  /api/backtest · /api/health</text>
<text x="266" y="222" {S}>run_id = sha256(invoices + flags)</text>
<text x="266" y="244" {S}>deterministic, so restarts resume</text>
<path d="M200 61 H246" {L}/><path d="M200 143 Q225 143 225 200 Q225 330 246 330" {L}/>

<!-- engine -->
<text x="420" y="300" {S}>PIPELINE · engine.run_pipeline()</text>
<rect x="250" y="312" width="720" height="104" {G}/>
<rect x="264" y="328" width="124" height="72" {B}/><text x="278" y="354" {T}>1 Book</text><text x="278" y="376" {S}>build_book()</text>
<rect x="404" y="328" width="124" height="72" {B}/><text x="418" y="354" {T}>2 Market</text><text x="418" y="376" {S}>fetch_fx()</text>
<rect x="544" y="328" width="124" height="72" {B}/><text x="558" y="354" {T}>3 Policy</text><text x="558" y="376" {S}>decide()</text><text x="558" y="392" {S}>pure, no I/O</text>
<rect x="684" y="328" width="124" height="72" {B}/><text x="698" y="354" {T}>4 Makers</text><text x="698" y="376" {S}>find_makers()</text>
<rect x="824" y="328" width="132" height="72" {B}/><text x="838" y="354" {T}>5 Outbox</text><text x="838" y="376" {S}>propose()</text>
<path d="M388 364 H400" {L}/><path d="M528 364 H540" {L}/><path d="M668 364 H680" {L}/><path d="M808 364 H820" {L}/>
<path d="M400 264 V308" {L}/>

<!-- policy yaml + backtest -->
<rect x="590" y="28" width="180" height="66" {B}/><text x="606" y="56" {T}>policy.yaml</text><text x="606" y="78" {S}>read on every run</text>
<path d="M550 168 Q570 168 570 70 Q570 61 586 61" {D}/>
<rect x="590" y="130" width="180" height="66" {B}/><text x="606" y="158" {T}>backtest.py</text><text x="606" y="180" {S}>18 mo real USD/INR</text>
<path d="M620 196 V324" {D}/>
<text x="630" y="262" {S}>calls the same decide()</text>

<!-- sources gateway -->
<rect x="250" y="468" width="440" height="150" {B}/><text x="266" y="496" {T}>sources.py</text><text x="360" y="496" {S}>the only code that calls out</text>
<text x="266" y="522" {S}>cache keyed by sha256(endpoint + body)</text>
<text x="266" y="544" {S}>budget check before each paid call</text>
<text x="266" y="566" {S}>429/5xx backoff · 402 hard stop</text>
<text x="266" y="588" {S}>writes evidence row per fact</text>
<text x="266" y="608" {S}>writes cost row per call</text>
<path d="M326 400 V464" {L}/><path d="M466 400 V464" {L}/><path d="M746 400 Q746 440 690 470" {L}/>

<!-- externals -->
<text x="740" y="456" {S}>EXTERNAL</text>
<rect x="740" y="468" width="230" height="84" {B}/><text x="756" y="496" {T}>Crustdata API</text><text x="756" y="518" {S}>identify · enrich</text><text x="756" y="538" {S}>person / company search</text>
<rect x="740" y="566" width="230" height="52" {B}/><text x="756" y="590" {T}>Frankfurter (ECB)</text><text x="756" y="608" {S}>daily USD/INR</text>
<path d="M690 510 H736" {L}/><path d="M690 592 H736" {L}/>

<!-- sqlite -->
<text x="1020" y="16" {S}>STATE · SQLite, WAL mode</text>
<rect x="1020" y="28" width="244" height="388" {B}/><text x="1036" y="56" {T}>hedgedesk.db</text>
<g {S}>
<text x="1036" y="92">steps</text><text x="1120" y="92" fill="#77736d">checkpoints</text>
<text x="1036" y="122">invoices</text><text x="1120" y="122" fill="#77736d">book rows</text>
<text x="1036" y="152">decisions</text><text x="1120" y="152" fill="#77736d">ratio, clause</text>
<text x="1036" y="182">outbox</text><text x="1120" y="182" fill="#77736d">idem_key, state</text>
<text x="1036" y="212">evidence</text><text x="1120" y="212" fill="#77736d">source, field</text>
<text x="1036" y="242">cost_ledger</text><text x="1120" y="242" fill="#77736d">credits, ₹</text>
<text x="1036" y="272">cache</text><text x="1120" y="272" fill="#77736d">API responses</text>
<text x="1036" y="330" fill="#77736d">one file, crash-safe</text><text x="1036" y="350" fill="#77736d">writes; single writer</text>
</g>
<path d="M970 364 H1016" {L}/>

<!-- execution -->
<text x="1020" y="456" {S}>EXECUTE · after a person approves</text>
<rect x="1020" y="468" width="244" height="150" {B}/><text x="1036" y="496" {T}>actions.py</text>
<text x="1036" y="522" {S}>approve(): 2nd approver &gt; ₹1 Cr</text>
<text x="1036" y="544" {S}>execute(): state check, then</text>
<text x="1036" y="566" {S}>payment (receipt lookup)</text>
<text x="1036" y="588" {S}>forward request draft</text>
<text x="1036" y="608" {S}>RFQ to team inbox</text>
<path d="M550 240 H995 V500 H1016" {D}/><text x="800" y="232" {S}>approve-and-execute</text>
</svg>"""

slides["5_decisions"] = """
<h2>Design decisions</h2>
<table>
<tr><th>Decision</th><th>Why</th></tr>
<tr><td>The hedge rule is a pure function</td><td><span class="mono">policy.decide()</span> takes the book, market snapshot and policy and returns decisions, with no network or database access. The backtest calls the same function, and an assert stops it from seeing a rate dated after the decision day.</td></tr>
<tr><td>No LLM in the decision path</td><td>We tried it: the same invoice worded two ways got two different ratios, and it can't be backtested. The rule hedges more when cover is cheap relative to VaR, always inside the board's range for that tenor.</td></tr>
<tr><td>Outbox with content-derived keys</td><td>Each action's key is a hash of what it does (invoice, amount, due date). Proposing it again returns the existing row. A crash between the payment call and the DB write is reconciled by looking up the receipt, so the payment happens once.</td></tr>
<tr><td>Checkpoint per step, run id from input</td><td>The run id is a hash of the invoices and flags, so a restart finds its own checkpoints. Nothing already fetched is fetched again and nothing already proposed is proposed again.</td></tr>
<tr><td>Fail closed</td><td>Missing amount, supplier or due date, a lookalike name, a bank-detail change, or FX data outside sanity bounds all end in a hold with the reason written out. No defaults.</td></tr>
<tr><td>SQLite in WAL mode</td><td>One file, no server to run, crash-safe writes. The limit is a single writer; for 100x volume or a second client we'd move to Postgres with a schema per client and keep one shared market-data cache.</td></tr>
</table>"""

slides["6_limits"] = """
<h2>What isn't live yet</h2>
<div class="grid g2">
 <div class="card"><h3>Forward premia are modelled <span class="tag">cannot verify</span></h3><p>1M/3M/6M premia come from the 2025–26 FBIL range plus a volatility adjustment. The evidence ledger says so, and forward rates are marked indicative until the bank quotes.</p></div>
 <div class="card"><h3>Payments use a local stand-in</h3><p>It behaves like Razorpay test mode, storing each payment against a receipt ID, but it doesn't call Razorpay's API yet. Bank requests and RFQs are drafted, not sent.</p></div>
 <div class="card"><h3>Staleness gate</h3><p>The one-business-day freshness hold is in the policy and the tests but isn't enforced on the live fallback path yet.</p></div>
 <div class="card"><h3>Backtest scope</h3><p>Real daily USD/INR, 30 Apr 2025 to 20 Nov 2026, but synthetic $100k payables and an assumed 18 bps spread. Hedging 100% did better on risk in a one-way market.</p></div>
</div>"""

for i, (name, body) in enumerate(slides.items(), 1):
    (OUT / f"{name}.html").write_text(HEAD.replace("{n}", f"{i} / {len(slides)}") + body + "</body></html>", encoding="utf-8")
print("wrote", len(slides), "slides to", OUT)
