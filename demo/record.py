"""
Records the HedgeDesk demo (paced to the narration) against a real server on port 8001,
then lays the narration on top.
Run demo/tts.py first. Usage: .venv/bin/python demo/record.py
Output: demo/out/hedgedesk_demo_final.mp4 and demo/out/timeline.json
"""
import json, shutil, subprocess, time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"
OUT = DEMO / "out"; OUT.mkdir(exist_ok=True)
AUD = OUT / "audio"
PY = str(ROOT / ".venv/bin/python")
URL = "http://127.0.0.1:8001"
W, H = 1440, 900
LEAD, TAIL = 0.4, 0.9   # voice starts LEAD s into a section; section holds TAIL s after the line ends

# Recording-only overlays (not part of the app):
#  - alert() text mirrored into a toast, since native dialogs don't appear in recordings
#  - a caption chip naming each section
#  - a visible cursor with click ripple, since headless recordings have no pointer
INIT = """
window.alert = (msg) => {
  const t = document.createElement('div'); t.textContent = msg;
  t.style.cssText = 'position:fixed;right:24px;bottom:24px;z-index:9999;max-width:520px;background:#111110;color:#fff;padding:14px 18px;border-radius:12px;font:500 14px Inter,sans-serif;box-shadow:0 12px 32px rgba(0,0,0,.25);white-space:pre-wrap;opacity:0;transition:opacity .25s';
  document.body.appendChild(t); requestAnimationFrame(()=>t.style.opacity=1); setTimeout(() => t.remove(), 4600);
};
window.__cap = (txt) => {
  let c = document.getElementById('__cap');
  if (!c) { c = document.createElement('div'); c.id='__cap';
    c.style.cssText='position:fixed;left:50%;transform:translateX(-50%);bottom:22px;z-index:9998;background:rgba(17,17,16,.88);color:#fff;padding:8px 16px;border-radius:999px;font:600 13px Inter,sans-serif;letter-spacing:.01em;transition:opacity .3s';
    document.body.appendChild(c); }
  c.textContent = txt; c.style.opacity = txt ? 1 : 0;
};
document.addEventListener('DOMContentLoaded', () => {
  const cur = document.createElement('div');
  cur.style.cssText = 'position:fixed;left:-50px;top:-50px;width:18px;height:18px;border-radius:50%;background:rgba(17,17,16,.85);border:2px solid #fff;box-shadow:0 2px 8px rgba(0,0,0,.3);z-index:10000;pointer-events:none;transform:translate(-50%,-50%);transition:left .12s linear, top .12s linear';
  document.body.appendChild(cur);
  addEventListener('mousemove', e => { cur.style.left = e.clientX + 'px'; cur.style.top = e.clientY + 'px'; }, true);
  addEventListener('mousedown', e => {
    const r = document.createElement('div');
    r.style.cssText = `position:fixed;left:${e.clientX}px;top:${e.clientY}px;width:16px;height:16px;border-radius:50%;border:2px solid rgba(17,17,16,.7);z-index:9999;pointer-events:none;transform:translate(-50%,-50%) scale(1);transition:transform .45s ease-out, opacity .45s ease-out`;
    document.body.appendChild(r); requestAnimationFrame(()=>{ r.style.transform='translate(-50%,-50%) scale(3.2)'; r.style.opacity='0'; });
    setTimeout(()=>r.remove(), 500);
  }, true);
});
"""

def start_server():
    p = subprocess.Popen([str(ROOT / ".venv/bin/uvicorn"), "app:app", "--host", "127.0.0.1", "--port", "8001"],
                         cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import urllib.request
    for _ in range(80):
        try: urllib.request.urlopen(URL + "/api/policy", timeout=1); return p
        except Exception: time.sleep(0.25)
    raise SystemExit("server did not start")

def dur(p): return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)]))

def main():
    lines = json.loads((DEMO / "narration.json").read_text())
    durs = json.loads((AUD / "durations.json").read_text())
    subprocess.run([PY, "reset_demo.py"], cwd=ROOT, check=True)
    for port_pid in subprocess.run(["lsof", "-ti", "tcp:8001"], capture_output=True, text=True).stdout.split():
        subprocess.run(["kill", "-9", port_pid])
    srv = start_server()
    starts = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": W, "height": H}, record_video_dir=str(OUT / "raw"),
                            record_video_size={"width": W, "height": H})
        ctx.add_init_script(INIT)
        pg = ctx.new_page()
        T0 = time.time()
        wait = pg.wait_for_timeout

        def section(i):
            """Start narration section i, first holding until section i-1's line has finished."""
            if starts:
                prev = len(starts) - 1
                need = starts[prev] + LEAD + durs[prev] + TAIL
                left = need - (time.time() - T0)
                if left > 0: wait(int(left * 1000))
            starts.append(time.time() - T0)
            print(f"{starts[-1]:6.1f}s  {lines[i][1]}", flush=True)

        def cap(s): pg.evaluate("t => window.__cap && window.__cap(t)", s)
        def glide(x, y, steps=18): pg.mouse.move(x, y, steps=steps)
        def smooth(dy, steps=26, ms=40):
            for _ in range(steps): pg.mouse.wheel(0, dy / steps); wait(ms)
        def click(loc):
            loc.scroll_into_view_if_needed(); bb = loc.bounding_box()
            glide(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2); wait(180); loc.click(); wait(350)
        def tab(name): click(pg.get_by_role("button", name=name))
        def slide(name): pg.goto((DEMO / "slides" / f"{name}.html").as_uri()); wait(300)
        loaded = "document.getElementById('kCount').textContent.startsWith('9')"

        # 0-3 slides
        section(0); slide("1_title"); wait(2000)
        section(1); slide("2_problem"); wait(2000)
        section(2); slide("3_arch"); wait(2000)
        section(3); slide("4_tech"); wait(2000)

        # 4 run the demo
        section(4); pg.goto(URL); wait(1800); glide(700, 450, 1)
        click(pg.get_by_role("button", name="Run Demo Story"))
        pg.wait_for_function(loaded, timeout=60000)
        cap("9 invoices · checked on Crustdata · hedged inside policy"); wait(2500)

        # 5 exposure book
        section(5); cap("Exposure book: duplicate, lookalike, bank-detail change")
        glide(700, 600); smooth(560); wait(2200); smooth(520); wait(2500)

        # 6 hedge decisions
        section(6); smooth(-1080, steps=14); tab("2. Hedge Decisions")
        cap("Policy range · risk vs cost · evidence IDs"); glide(900, 600); smooth(400); wait(2500); smooth(420); wait(1500)

        # 7 domestic alternatives
        section(7); smooth(-820, steps=12); tab("3. Domestic Alternatives")
        cap("Indian makers · price: cannot verify, RFQ drafted"); smooth(300); wait(600)
        click(pg.locator("details summary").first); wait(2000)

        # 8 dual approval refused
        section(8); smooth(-300, steps=10); tab("4. Approvals & Outbox"); cap("Nothing runs until a person approves")
        smooth(300); wait(400)
        click(pg.get_by_role("button", name="Approve & Execute").first); wait(2500)

        # 9 second approver
        section(9); smooth(-300, steps=10)
        click(pg.locator("#secondApprover")); pg.keyboard.type("Director, Finance", delay=55); wait(400)
        smooth(300, steps=10)
        click(pg.get_by_role("button", name="Approve & Execute").first); wait(1500)
        click(pg.get_by_role("button", name="Approve & Execute").first); wait(1500)

        # 10 kill and restart
        section(10); cap("kill -9 the server process"); wait(600)
        srv.kill(); srv.wait(); wait(700)
        try: pg.reload(timeout=5000)
        except Exception: pass  # expected: connection refused
        wait(2600)
        srv = start_server()
        pg.goto(URL); pg.wait_for_function(loaded, timeout=60000)
        cap("Restarted · resumed from SQLite checkpoints"); wait(800)
        glide(700, 600); smooth(700); wait(2200); smooth(-700, steps=12)
        tab("4. Approvals & Outbox"); cap("Still executed once · no second payment"); smooth(300); wait(2000)

        # 11 evidence & cost
        section(11); smooth(-300, steps=10); tab("6. Evidence & Cost Ledger")
        cap("Every fact sourced · every call costed"); smooth(250); wait(1500)

        # 12 backtest
        section(12); smooth(-250, steps=10); tab("5. 18M ECB Backtest")
        cap("18 months of real USD/INR · same decide()"); smooth(250); wait(1500)

        # 13 policy + unseen request
        section(13); smooth(-250, steps=10); cap("Policy lives in policy.yaml")
        click(pg.get_by_role("button", name="Edit Hedge Parameters")); wait(2200)
        click(pg.get_by_role("button", name="Edit Hedge Parameters")); wait(300)
        cap("Unseen request with no amount → held, not guessed")
        click(pg.locator("#plainAsk")); pg.keyboard.type("We owe Timken for 300 bearings due 2027-02-15", delay=32)
        click(pg.get_by_role("button", name="Run Unseen Request")); wait(2200)
        tab("2. Hedge Decisions"); wait(1500)

        # 14 FX feed down
        section(14); pg.locator("#plainAsk").fill(""); cap("Simulate: FX feed down → every invoice held")
        click(pg.locator("#chkDown")); wait(300)
        click(pg.get_by_role("button", name="Run Demo Story")); wait(2200)
        tab("2. Hedge Decisions"); smooth(300); wait(1200)

        # 15-17 closing slides
        section(15); slide("5_decisions"); wait(1500)
        section(16); slide("6_limits"); wait(1500)
        section(17); slide("1_title"); wait(1000)
        # hold for the last line
        left = starts[-1] + LEAD + durs[-1] + 1.6 - (time.time() - T0)
        if left > 0: wait(int(left * 1000))
        vid = pg.video.path()
        ctx.close(); b.close()
    srv.kill()
    raw = OUT / "hedgedesk_demo_silent.webm"; shutil.move(vid, raw); shutil.rmtree(OUT / "raw", ignore_errors=True)
    (OUT / "timeline.json").write_text(json.dumps([[s, l[1]] for s, l in zip(starts, lines)], indent=1))

    # Mux: narration at section starts, gentle fade in/out on picture and sound.
    total = dur(raw)
    inputs, filt = [], []
    for k, s in enumerate(starts):
        inputs += ["-i", str(AUD / f"line_{k:02d}.mp3")]
        ms = int((s + LEAD) * 1000); filt.append(f"[{k+1}:a]adelay={ms}|{ms}[a{k}]")
    filt.append("".join(f"[a{k}]" for k in range(len(starts))) + f"amix=inputs={len(starts)}:normalize=0,"
                f"afade=t=out:st={total-1.2:.2f}:d=1.2[aout]")
    filt.append(f"[0:v]fade=t=in:st=0:d=0.6,fade=t=out:st={total-1.0:.2f}:d=1.0[vout]")
    final = OUT / "hedgedesk_demo_final.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), *inputs, "-filter_complex", ";".join(filt),
                    "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(final)], check=True)
    subprocess.run([PY, "reset_demo.py"], cwd=ROOT, check=True)
    print(f"done: {final}  ({dur(final):.1f}s)")

if __name__ == "__main__":
    main()
