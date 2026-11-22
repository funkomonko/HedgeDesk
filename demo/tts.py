"""Generates one narration clip per line (cached) and writes their durations to out/audio/durations.json."""
import asyncio, json, subprocess, time
from pathlib import Path
import edge_tts

DEMO = Path(__file__).resolve().parent
AUD = DEMO / "out" / "audio"; AUD.mkdir(parents=True, exist_ok=True)
VOICE = "en-US-AndrewNeural"

def dur(p): return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)]))

async def _tts(text, path): await edge_tts.Communicate(text, VOICE).save(str(path))

def main():
    lines = json.loads((DEMO / "narration.json").read_text())
    durs = []
    for i, (_, label, text) in enumerate(lines):
        path = AUD / f"line_{i:02d}.mp3"
        tries = 0
        while not (path.exists() and path.stat().st_size > 1000):
            tries += 1
            try:
                asyncio.run(_tts(text, path))
            except Exception as e:
                print(f"  {label}: retry {tries} ({type(e).__name__})", flush=True)
                time.sleep(min(4 * tries, 30))
            if tries > 25: raise SystemExit(f"gave up on {label}")
        durs.append(dur(path))
        print(f"{i:02d} {label:28} {durs[-1]:5.1f}s", flush=True)
        time.sleep(1.5)
    (AUD / "durations.json").write_text(json.dumps(durs))
    print("total speech", round(sum(durs), 1), "s")

if __name__ == "__main__":
    main()
