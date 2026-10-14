#!/usr/bin/env python3
"""Live dashboard of the evaluation run -> dashboard.html (open it in a browser).

    python3 scripts/dashboard.py              # write dashboard.html once
    python3 scripts/dashboard.py --watch 60   # rewrite every 60 s, plus a macOS notification
                                              # for each new do_it / verify_first candidate

Stdlib only. ROI uses the same formula as rank.py, so the dashboard and PROJECTS.md agree.
The page reloads itself every minute, but not while a report is open or you're interacting.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dashboard.html"
NOTIFIED = ROOT / "data" / "dashboard_notified.json"
GOOD = {"do_it", "verify_first"}
VERDICTS = {"do_it", "verify_first", "park", "reject"}

# --- scoring: shared with rank.py, so the dashboard and PROJECTS.md always agree --------
import scoring  # noqa: E402  (scripts/ is on sys.path when run as scripts/dashboard.py)
from glossary import gloss  # noqa: E402
from scoring import EFFORT_LABEL, killers, roi  # noqa: E402


def glossed(value):
    """Explain known acronyms in every string in an evaluation, however deeply nested."""
    if isinstance(value, str):
        return gloss(value)
    if isinstance(value, dict):
        return {k: glossed(v) for k, v in value.items()}
    if isinstance(value, list):
        return [glossed(v) for v in value]
    return value


def tier(r: dict) -> str:
    """Any failed Part A killer means rejected, whatever the verdict says (as in rank.py)."""
    if killers(r):
        return "reject"
    v = re.sub(r"[\s-]+", "_", str(r.get("verdict") or "").strip().lower())
    return v if v in VERDICTS else "park"  # rank.py also treats unknown verdicts as park


# --- run state ----------------------------------------------------------------
def queue_info() -> dict[str, dict]:
    path = ROOT / "queue.csv"
    if not path.exists():
        return {}
    with path.open() as fh:
        return {row["paper_id"]: row for row in csv.DictReader(fh)}


def run_state() -> dict:
    """In-flight and failed papers from run.out (written by run_queue.sh)."""
    path = ROOT / "run.out"
    state = {"started_at": None, "inflight": [], "failed": [], "runner_alive": False}
    pidfile = ROOT / "logs" / "run_queue.pid"
    if pidfile.exists():
        try:
            os.kill(int(pidfile.read_text().strip()), 0)
            state["runner_alive"] = True
        except (ValueError, OSError):
            pass
    if not path.exists():
        return state
    lines = path.read_text(errors="replace").splitlines()
    head = next((l for l in lines if re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \d+ papers", l)), None)
    if not head:
        return state
    day0 = datetime.strptime(head[:19], "%Y-%m-%d %H:%M:%S")
    state["started_at"] = day0.timestamp() * 1000

    def stamp(hms: str, prev: datetime) -> datetime:
        t = datetime.combine(prev.date(), datetime.strptime(hms, "%H:%M:%S").time())
        return t + timedelta(days=1) if t < prev - timedelta(hours=1) else t

    started: dict[str, tuple[str, float]] = {}
    prev = day0
    for line in lines:
        m = re.match(r"^(\d\d:\d\d:\d\d) \[(start|ok|FAILED)\]\s+(\S+)(?: — (.*))?$", line)
        if not m:
            continue
        prev = stamp(m.group(1), prev)
        kind, pid = m.group(2), m.group(3)
        if kind == "start":
            started[pid] = (m.group(4) or "", prev.timestamp() * 1000)
        else:
            started.pop(pid, None)
            if kind == "FAILED":
                state["failed"].append(pid)
    if state["runner_alive"]:
        state["inflight"] = [{"id": k, "title": v[0], "started": v[1]} for k, v in started.items()]
    return state


ML_WORDS = re.compile(r"\b(deep learning|machine learning|neural network|language model|foundation model|transformer|"
                      r"artificial intelligence|benchmark|predictor|prediction tool|web server|computational framework|"
                      r"embedding|generative|graph learning|virtual cell)\b", re.I)


def abstracts() -> dict[str, str]:
    path = ROOT / "data" / "harvest_raw.jsonl"
    out = {}
    if path.exists():
        for line in path.open():
            rec = json.loads(line).get("record", {})
            out[(rec.get("doi") or "").lower()] = (rec.get("title") or "", rec.get("abstractText") or "")
    return out


def paper_type(r: dict, title: str, abstract: str) -> str:
    """The evaluator's own call when present. Otherwise: an ML term in the title is enough on its
    own; in the abstract it takes two different ones, so a wet-lab paper that mentions "machine
    learning" once in passing isn't relabelled."""
    t = str(r.get("paper_type") or "").strip().lower()
    if t in ("experimental", "resource", "computational"):
        return t
    # "benchmark" alone is ambiguous in a title (a genome benchmark is a reference resource).
    if any(m.group(0).lower() != "benchmark" for m in ML_WORDS.finditer(title)):
        return "computational"
    return "computational" if len({m.group(0).lower() for m in ML_WORDS.finditer(abstract)}) >= 2 else "experimental or resource"


# A rejected paper needs only enough to explain its rejection; the full record (39 scored items,
# pitch, leakage plan) exists only for candidates anyway. Trimming rejects takes the page from
# ~13 MB to ~2 MB, which matters when it is published rather than opened from disk.
SLIM_KEEP = ("paper_id", "title", "journal", "doi", "data_url", "verdict", "verdict_reason",
             "part_a_killers", "verified", "plain_idea", "paper_type")


def slim_eval(r: dict) -> dict:
    return {k: v for k, v in r.items() if k in SLIM_KEEP}


def slim_why(w: dict) -> dict:
    """A reject has no scores to explain, so drop the per-item scaffolding but keep every key
    the page reads, with empty values it already guards against."""
    return {**w, "parts": [], "e_items": [], "misses": [], "effort_bumps": [],
            "effort_unknown": [], "crowded_case": {}, "label_info": {}, "edge_claimed": []}


def build_payload(slim: bool = False) -> tuple[dict, list[dict]]:
    queue = queue_info()
    texts = abstracts()
    points, bad = [], len(glob.glob(str(ROOT / "evaluations" / "*.json.bad")))
    for p in sorted(glob.glob(str(ROOT / "evaluations" / "*.json"))):
        try:
            r = json.loads(Path(p).read_text())
        except (json.JSONDecodeError, OSError):
            bad += 1
            continue
        pid = r.get("paper_id") or Path(p).stem
        q = queue.get(Path(p).stem, {})
        points.append({
            "id": Path(p).stem, "t": os.path.getmtime(p) * 1000, "roi": roi(r), "tier": tier(r),
            "opened": bool((r.get("verified") or {}).get("opened_data_files")),
            "killers": killers(r), "effort": EFFORT_LABEL[scoring.effort(r)[0]],
            "why": glossed(slim_why(scoring.breakdown(r)) if (slim and tier(r) == "reject")
                           else scoring.breakdown(r)),
            # queue.csv has the clean journal name; evaluators often write a full citation there
            "title": r.get("title") or q.get("title") or pid, "journal": q.get("journal") or r.get("journal") or "",
            "priority": q.get("priority") or "",
            "eval": glossed(slim_eval(r) if (slim and tier(r) == "reject") else r),
            "ptype": paper_type(r, *texts.get((q.get("doi") or "").lower(), (r.get("title") or "", ""))),
        })
    payload = {
        "generated": time.time() * 1000, "queue_total": len(queue),
        "queue_p1": sum(1 for q in queue.values() if q.get("priority") == "1"),
        "unparseable": bad, "run": run_state(), "points": points,
    }
    return payload, points


def write_html(payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.replace("__DATA__", data)
    tmp = OUT.with_suffix(".html.tmp")
    tmp.write_text(html)
    os.replace(tmp, OUT)  # atomic: the browser never loads a half-written page


def notify_new_good(points: list[dict], enabled: bool) -> None:
    good = {p["id"]: p for p in points if p["tier"] in GOOD}
    if not NOTIFIED.exists():  # first run: baseline, don't fire for what's already there
        NOTIFIED.parent.mkdir(parents=True, exist_ok=True)
        NOTIFIED.write_text(json.dumps(sorted(good)))
        return
    seen = set(json.loads(NOTIFIED.read_text()))
    for pid in sorted(set(good) - seen):
        p = good[pid]
        label = p["tier"].replace("_", " ")
        print(f"{time.strftime('%H:%M:%S')} NEW {label}: {pid} (ROI {p['roi']}) {p['title'][:70]}", flush=True)
        if enabled:
            subprocess.run(["osascript", "-e", "on run argv", "-e",
                            "display notification (item 1 of argv) with title (item 2 of argv)",
                            "-e", "end run", f"ROI {p['roi']} · {p['title'][:90]}", f"New candidate: {label}"],
                           check=False, capture_output=True)
        seen.add(pid)
    NOTIFIED.write_text(json.dumps(sorted(seen)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", type=int, metavar="SECONDS", help="regenerate every SECONDS until killed")
    ap.add_argument("--no-notify", action="store_true", help="no macOS notification for new candidates")
    ap.add_argument("--slim", action="store_true",
                    help="trim rejected papers to their rejection reason (much smaller page)")
    ap.add_argument("--out", help="write here instead of dashboard.html")
    args = ap.parse_args()
    global OUT
    if args.out:
        OUT = Path(args.out)
    while True:
        payload, points = build_payload(slim=args.slim)
        write_html(payload)
        notify_new_good(points, enabled=not args.no_notify)
        if not args.watch:
            n_good = sum(p["tier"] in GOOD for p in points)
            print(f"Wrote {OUT.name}: {len(points)} evaluations, {n_good} worth a look.")
            return
        time.sleep(args.watch)


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>bioml-scout</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface-1: #fcfcfb; --text-primary: #0b0b0b; --text-secondary: #52514e;
  --muted: #898781; --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --tier-do_it: #184f95; --tier-verify_first: #2a78d6; --tier-park: #86b6ef; --tier-reject: #c3c2b7;
  --meter-track: #cde2fb; --meter-fill: #2a78d6; --line: #2a78d6; --wash: rgba(42,120,214,0.10);
  --good: #0ca30c; --good-text: #006300; --critical: #d03b3b; --hover-wash: rgba(11,11,11,0.04);
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface-1: #1a1a19; --text-primary: #ffffff; --text-secondary: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --tier-do_it: #9ec5f4; --tier-verify_first: #3987e5; --tier-park: #1c5cab; --tier-reject: #52514e;
    --meter-track: #104281; --meter-fill: #3987e5; --line: #3987e5; --wash: rgba(57,135,229,0.14);
    --good: #0ca30c; --good-text: #0ca30c; --critical: #d03b3b; --hover-wash: rgba(255,255,255,0.05);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface-1: #1a1a19; --text-primary: #ffffff; --text-secondary: #c3c2b7;
  --muted: #898781; --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --tier-do_it: #9ec5f4; --tier-verify_first: #3987e5; --tier-park: #1c5cab; --tier-reject: #52514e;
  --meter-track: #104281; --meter-fill: #3987e5; --line: #3987e5; --wash: rgba(57,135,229,0.14);
  --good: #0ca30c; --good-text: #0ca30c; --critical: #d03b3b; --hover-wash: rgba(255,255,255,0.05);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--text-primary);
       font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1240px; margin: 0 auto; padding: 24px 24px 64px; }
header { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
h1 { font-size: 20px; font-weight: 600; margin: 0; }
h2 { font-size: 15px; font-weight: 600; margin: 0 0 2px; }
.sub { color: var(--text-secondary); font-size: 13px; }
.muted { color: var(--muted); }
button, select, input[type=search] {
  font: inherit; color: var(--text-primary); background: var(--surface-1);
  border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px; }
button { cursor: pointer; }
button:hover { background: var(--hover-wash); }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-top: 16px; }
/* KPI row */
.kpis { display: grid; grid-template-columns: 1.3fr repeat(5, 1fr); gap: 12px; margin-top: 16px; }
.tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.tile .label { color: var(--text-secondary); font-size: 13px; }
.tile .value { font-size: 24px; font-weight: 600; margin-top: 2px; }
.tile .note { color: var(--muted); font-size: 12px; }
.tile.hero .value { font-size: 52px; line-height: 1.05; }
.meter { height: 6px; border-radius: 3px; background: var(--meter-track); margin-top: 8px; overflow: hidden; }
.meter > span { display: block; height: 100%; background: var(--meter-fill); border-radius: 3px; }
.running { margin-top: 12px; color: var(--text-secondary); font-size: 13px; }
.running b { color: var(--text-primary); font-weight: 600; }
/* filters */
.filters select { max-width: 280px; }
.filters { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; margin-top: 20px; }
.filters label { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
.key { display: inline-block; width: 12px; height: 12px; border-radius: 50%; flex: none; }
.key.hollow { background: var(--surface-1) !important; box-shadow: inset 0 0 0 2px var(--tier-verify_first); }
.linekey { display: inline-block; width: 14px; height: 2px; border-radius: 1px; vertical-align: middle; margin-right: 6px; }
/* charts */
.chart { position: relative; }
svg { display: block; overflow: visible; }
.gridline { stroke: var(--grid); stroke-width: 1; shape-rendering: crispEdges; }
.baseline { stroke: var(--axis); stroke-width: 1; shape-rendering: crispEdges; }
.tick { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.axis-title { fill: var(--text-secondary); font-size: 12px; }
.strip-label { fill: var(--text-secondary); font-size: 12px; }
.strip { fill: var(--hover-wash); }
.pt { stroke: var(--surface-1); stroke-width: 2; cursor: pointer; }
.pt.t-do_it { fill: var(--tier-do_it); } .pt.t-verify_first { fill: var(--tier-verify_first); }
.pt.t-park { fill: var(--tier-park); } .pt.t-reject { fill: var(--tier-reject); }
.pt.hollow { fill: var(--surface-1); stroke-width: 2; }
.pt.hollow.t-do_it { stroke: var(--tier-do_it); } .pt.hollow.t-verify_first { stroke: var(--tier-verify_first); }
.pt.hollow.t-park { stroke: var(--tier-park); }
.pt.hot { stroke: var(--text-primary); stroke-width: 2; }
.pt:focus { outline: none; stroke: var(--text-primary); }
.dlabel { fill: var(--text-secondary); font-size: 12px; pointer-events: none; }
.cum-line { fill: none; stroke: var(--line); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.cum-area { fill: var(--wash); stroke: none; }
.cum-end { fill: var(--line); stroke: var(--surface-1); stroke-width: 2; }
.crosshair { stroke: var(--axis); stroke-width: 1; shape-rendering: crispEdges; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; color: var(--text-secondary); font-size: 13px; margin: 6px 0 4px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.tooltip { position: absolute; pointer-events: none; z-index: 5; max-width: 340px; padding: 10px 12px;
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.12); font-size: 13px; display: none; }
.tooltip .v { font-size: 16px; font-weight: 600; }
.tooltip .t { margin-top: 4px; font-weight: 500; }
.tooltip .m { color: var(--text-secondary); margin-top: 2px; }
.tooltip .r { color: var(--text-secondary); margin-top: 6px; }
.tooltip .idea { margin-top: 6px; }
.tooltip .pitchline { margin-top: 6px; font-weight: 600; }
.pitchbox { background: var(--wash); border-left: 3px solid var(--tier-verify_first); border-radius: 6px; padding: 10px 12px; margin-top: 10px; }
.pitchbox p { margin: 5px 0 !important; } .pitchbox .head { font-size: 15px; font-weight: 600; }
.pitch-row { color: var(--text-secondary); margin-top: 2px; font-style: italic; }
.tooltip .strip { margin-top: 6px; font-variant-numeric: tabular-nums; }
.tooltip { max-width: 420px !important; }
.itemrow { display: grid; grid-template-columns: 34px 36px 1fr; gap: 6px; font-size: 13px; margin: 2px 0 2px 8px; }
.itemrow .why { color: var(--text-secondary); }
.parthead { margin-top: 10px; }
.roiline { background: var(--page); border-radius: 6px; padding: 8px 10px; font-size: 13px; }
.plainbox { background: var(--wash); border-radius: 8px; padding: 10px 12px; margin-top: 12px; }
.plainbox p { margin: 4px 0 !important; } .plainbox b { font-weight: 600; }
.idea-row { color: var(--text-secondary); margin-top: 2px; }
.tooltip .h { color: var(--muted); margin-top: 6px; font-size: 12px; }
/* table */
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; font-weight: 600; color: var(--text-secondary); padding: 6px 8px; border-bottom: 1px solid var(--axis); white-space: nowrap; }
td { padding: 7px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
td.num { font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
tbody tr { cursor: pointer; }
tbody tr:hover, tbody tr:focus { background: var(--hover-wash); outline: none; }
tr.sel { background: var(--wash) !important; }
td.verdict { white-space: nowrap; }
.pill { display: inline-block; font-size: 11px; font-weight: 600; padding: 0 6px; border-radius: 9px;
        border: 1px solid var(--border); color: var(--text-primary); margin-right: 6px; }
.ok { color: var(--good-text); } .bad { color: var(--critical); }
.small { font-size: 12px; color: var(--muted); }
/* drawer */
#drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(560px, 100vw); overflow-y: auto; z-index: 10;
  background: var(--surface-1); border-left: 1px solid var(--border); box-shadow: -8px 0 24px rgba(0,0,0,0.12); padding: 20px 22px 48px; }
#drawer[hidden] { display: none; }
#drawer h2 { font-size: 18px; margin: 6px 0 4px; line-height: 1.3; }
#drawer h3 { font-size: 13px; font-weight: 600; margin: 18px 0 4px; color: var(--text-secondary); text-transform: none; }
#drawer p { margin: 2px 0; }
#drawer .close { position: sticky; top: 0; float: right; }
#drawer table td { padding: 4px 6px; }
.scorerow { display: grid; grid-template-columns: 215px 56px 1fr; gap: 8px; align-items: center; margin: 3px 0; }
.scorebar { height: 6px; border-radius: 3px; background: var(--meter-track); position: relative; }
.scorebar > span { position: absolute; left: 0; top: 0; bottom: 0; background: var(--meter-fill); border-radius: 3px; }
.scorebar > i { position: absolute; top: -3px; bottom: -3px; width: 2px; background: var(--text-secondary); }
a { color: var(--tier-verify_first); }
pre { white-space: pre-wrap; word-break: break-word; font-size: 12px; background: var(--page); padding: 10px; border-radius: 6px; }
@media (max-width: 900px) { .kpis { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>bioml-scout · paper evaluations</h1>
      <div class="sub" id="status"></div>
    </div>
    <div style="display:flex; gap:8px">
      <button id="markSeen" title="Clear the New badges">Mark all seen</button>
      <button id="refresh">Refresh</button>
      <button id="theme" title="Theme: auto / light / dark">Theme: auto</button>
    </div>
  </header>

  <section class="kpis" id="kpis"></section>
  <div class="running" id="running"></div>

  <div class="filters" id="filters" role="group" aria-label="Filters"></div>

  <section class="card">
    <h2>Every evaluation by finish time, scored by ROI (return on investment: payoff ÷ effort)</h2>
    <div class="sub">ROI = payoff × crowding ÷ effort, on a 0–10 scale (same as PROJECTS.md). Payoff comes from the checklist parts C (worth asking), D (pretrained model fits), G (joins to big datasets) and I (anyone cares). Crowding shrinks it when others have already modelled the task well. Effort rises for data over 1 GB, needing a GPU (graphics card), straining a 16 GB laptop, messy data, or data nobody opened. Hover a point to see the arithmetic; click it for the full report. Rejected papers sit in the strip below the axis.</div>
    <div class="legend" id="legend"></div>
    <div class="chart" id="scatterWrap"><svg id="scatter" role="img" aria-label="Scatter of evaluations over time by ROI"></svg><div class="tooltip" id="tip"></div></div>
  </section>

  <section class="card">
    <h2>Worth a look over time</h2>
    <div class="sub">Cumulative count of do it + verify first verdicts (within the current filters).</div>
    <div class="chart" id="cumWrap"><svg id="cum" role="img" aria-label="Cumulative candidates over time"></svg><div class="tooltip" id="tip2"></div></div>
  </section>

  <section class="card">
    <h2>All evaluations</h2>
    <div class="sub">Sorted by verdict tier, then ROI. Click a row (or press Enter) for the full report.</div>
    <div style="overflow-x:auto; margin-top:8px"><table id="table"></table></div>
  </section>
</main>
<aside id="drawer" hidden aria-label="Report"></aside>

<script type="application/json" id="data">__DATA__</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("data").textContent);
const TIERS = [
  { key: "do_it", label: "Do it", r: 7 },
  { key: "verify_first", label: "Verify first", r: 6 },
  { key: "park", label: "Park", r: 5 },
  { key: "reject", label: "Rejected", r: 4 },
];
const TIER = Object.fromEntries(TIERS.map((t, i) => [t.key, { ...t, rank: i }]));
const GOOD = new Set(["do_it", "verify_first"]);
const SCORE_PARTS = [
  ["b_model_readiness", "B · model-readiness", 12, 8], ["c_worth_asking", "C · worth asking", 10, 6],
  ["d_backbone_fit", "D · backbone fit", 6, 4], ["g_integration", "G · integration", 8, 4],
  ["h_missing_data", "H · missing data", 8, 4], ["i_does_anyone_care", "I · anyone cares", 8, 4],
  ["j_fit_finishability", "J · finishability", 10, 6],
];
const SVGNS = "http://www.w3.org/2000/svg";
const $ = (id) => document.getElementById(id);

// ---------- helpers (all untrusted text goes through textContent) ----------
function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") n.className = v; else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else n.setAttribute(k, v);
  }
  for (const k of kids) if (k != null) n.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return n;
}
function sv(tag, attrs) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
  return n;
}
const fmtTime = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const fmtDay = (ms) => new Date(ms).toLocaleDateString([], { month: "short", day: "numeric" });
const fmtWhen = (ms) => fmtDay(ms) + " " + fmtTime(ms);
const short = (s, n) => { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
const hash = (s) => { let h = 2166136261; for (const c of s) h = Math.imul(h ^ c.charCodeAt(0), 16777619); return (h >>> 0) / 4294967295; };
function links(text) {
  // Render URLs inside free text as links (http/https only), everything else as text.
  const frag = document.createDocumentFragment();
  const s = String(text || ""); let last = 0;
  for (const m of s.matchAll(/https?:\/\/[^\s<>"')\]]+/g)) {
    frag.append(document.createTextNode(s.slice(last, m.index)));
    const url = m[0].replace(/[.,;:]+$/, "");
    const a = el("a", { target: "_blank", rel: "noopener" }, url); a.href = url; frag.append(a);
    last = m.index + url.length;
  }
  frag.append(document.createTextNode(s.slice(last)));
  return frag;
}

// ---------- persisted UI state ----------
const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem("bioml_" + k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("bioml_" + k, JSON.stringify(v)); } catch {} },
};
const filters = Object.assign({ tiers: ["do_it", "verify_first", "park", "reject"], openedOnly: false, journal: "", q: "", ptype: "" }, store.get("filters", {}));
let seen = new Set(store.get("seen", []));
let theme = store.get("theme", "auto");
function applyTheme() {
  if (theme === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = theme;
  $("theme").textContent = "Theme: " + theme;
}
applyTheme();
$("theme").onclick = () => { theme = { auto: "light", light: "dark", dark: "auto" }[theme]; store.set("theme", theme); applyTheme(); renderAll(); };
$("refresh").onclick = () => reloadKeepingPlace();
$("markSeen").onclick = () => { DATA.points.forEach((p) => p.tier !== "reject" && seen.add(p.id)); store.set("seen", [...seen]); renderTable(); };

const isNew = (p) => p.tier !== "reject" && !seen.has(p.id);
function scoreStrip(p, cls) {
  // "B 7/12 ✗ · C 8/10 ✓ ..." with the pass mark judged per part.
  const box = el("div", { class: cls });
  p.why.parts.forEach((pt, i) => {
    const ok = pt.total == null ? null : pt.total >= pt.need;
    box.append(i ? " · " : "", el("span", { class: ok == null ? "muted" : ok ? "ok" : "bad",
      text: `${pt.part} ${pt.total == null ? "–" : pt.total}/${pt.max}${ok == null ? "" : ok ? " ✓" : " ✗"}` }));
  });
  return box;
}
function roiText(p) {
  const w = p.why;
  if (w.killers.length) return "Rejected: failed " + w.killers.map((k) => k.split("_")[0].toUpperCase()).join(", ");
  const bumps = w.effort_bumps.length ? ` (${w.effort_base} from the hours estimate, +1 each for: ${w.effort_bumps.join("; ")})` : " (from the hours estimate)";
  const unknown = w.effort_unknown.length ? `. Not recorded, so no penalty applied: ${w.effort_unknown.join(", ")}` : "";
  const src = w.c2_source === "scored" ? "" : w.c2_source.startsWith("backfilled") ? ", judged afterwards from the existing-models audit" : ", guessed: models listed but crowding not scored";
  const lab = w.label == null ? "" : ` × known labels ${w.label_factor} (${w.label_text})`;
  const edge = w.edge_share ? `, ${Math.round(w.edge_share * 100)}% of the penalty earned back by our edge` : "";
  return `ROI ${w.roi} = payoff ${w.impact} × crowding ${w.crowding} (${w.field_label}${src}${edge})${lab} ÷ effort ${w.effort} of 5${bumps}${unknown}`;
}
const pitch = (p) => (p.eval.pitch && typeof p.eval.pitch === "object") ? p.eval.pitch : {};
const plain = (p) => (p.eval.plain_idea && typeof p.eval.plain_idea === "object") ? p.eval.plain_idea : {};
function visible() {
  const q = filters.q.trim().toLowerCase();
  return DATA.points.filter((p) =>
    filters.tiers.includes(p.tier) && (!filters.openedOnly || p.opened) &&
    (!filters.journal || p.journal === filters.journal) &&
    (!filters.ptype || (filters.ptype === "computational" ? p.ptype === "computational" : p.ptype !== "computational")) &&
    (!q || [p.title, p.id, p.eval.ml_idea, p.eval.verdict_reason, ...Object.values(plain(p)), ...Object.values(pitch(p))].join(" ").toLowerCase().includes(q)));
}

// ---------- header, KPIs, running ----------
function renderHeader() {
  const run = DATA.run;
  const state = run.runner_alive ? "run in progress" : "runner not running";
  $("status").textContent = `Updated ${fmtWhen(DATA.generated)} · ${state} · reloads every minute unless a report is open`;
  const pts = DATA.points, n = pts.length;
  const count = (k) => pts.filter((p) => p.tier === k).length;
  const good = pts.filter((p) => GOOD.has(p.tier)).length;
  // The last hour, whatever the run history: restarts (drains, pacing changes, usage limits)
  // are frequent, and measuring from the latest one showed 0/h minutes after every restart.
  const since = DATA.generated - 3600e3;
  const hours = 1;
  const perHour = Math.round(pts.filter((p) => p.t >= since).length / hours);
  const remaining = Math.max(0, DATA.queue_total - n);
  const eta = perHour ? remaining / perHour : null;
  const unverifiedGood = pts.filter((p) => GOOD.has(p.tier) && !p.opened).length;
  const tiles = [
    { hero: true, label: "Worth a look", value: good, note: `do it ${count("do_it")} · verify first ${count("verify_first")}` + (unverifiedGood ? ` · ${unverifiedGood} not verified in files` : "") },
    { label: "Evaluated", value: n.toLocaleString(), note: `of ${DATA.queue_total.toLocaleString()} in queue`, meter: DATA.queue_total ? n / DATA.queue_total : 0 },
    { label: "Parked", value: count("park"), note: "passed Part A, weak case" },
    { label: "Rejected", value: count("reject"), note: n ? `${Math.round(100 * count("reject") / n)}% of evaluated` : "" },
    { label: "Pace", value: `${perHour}/h`, note: eta == null ? "no finishes recently" : `rest of queue: ~${eta < 48 ? eta.toFixed(1) + " h" : (eta / 24).toFixed(1) + " days"}` },
    { label: "Failed runs", value: run.failed.length + DATA.unparseable, note: run.failed.length ? "this run; see logs/<id>.log" : "none this run" },
  ];
  const k = $("kpis"); k.replaceChildren();
  for (const t of tiles) {
    const tile = el("div", { class: "tile" + (t.hero ? " hero" : "") }, el("div", { class: "label", text: t.label }), el("div", { class: "value", text: String(t.value) }), el("div", { class: "note", text: t.note }));
    if (t.meter != null) { const m = el("div", { class: "meter" }, el("span")); m.firstChild.style.width = (100 * Math.min(1, t.meter)).toFixed(1) + "%"; tile.append(m); }
    k.append(tile);
  }
  const r = $("running"); r.replaceChildren();
  if (run.inflight.length) {
    r.append(el("b", { text: `Running now (${run.inflight.length}): ` }));
    run.inflight.forEach((f, i) => {
      const mins = Math.max(0, Math.round((DATA.generated - f.started) / 60000));
      r.append((i ? " · " : ""), el("span", { title: f.title, text: `${f.id} (${mins} min)` }));
    });
  }
}

// ---------- filters ----------
function renderFilters() {
  const f = $("filters"); f.replaceChildren();
  for (const t of TIERS) {
    const cb = el("input", { type: "checkbox" }); cb.checked = filters.tiers.includes(t.key);
    cb.onchange = () => { filters.tiers = cb.checked ? [...new Set([...filters.tiers, t.key])] : filters.tiers.filter((x) => x !== t.key); saveFilters(); };
    const key = el("span", { class: "key" }); key.style.background = `var(--tier-${t.key})`;
    const n = DATA.points.filter((p) => p.tier === t.key).length;
    f.append(el("label", {}, cb, key, `${t.label} (${n})`));
  }
  const op = el("input", { type: "checkbox" }); op.checked = filters.openedOnly;
  op.onchange = () => { filters.openedOnly = op.checked; saveFilters(); };
  f.append(el("label", { title: "Only evaluations where the agent actually opened the data files" }, op, "Data files opened only"));
  const js = el("select", { "aria-label": "Journal" }, el("option", { value: "", text: "All journals" }));
  [...new Set(DATA.points.map((p) => p.journal))].filter(Boolean).sort().forEach((j) => js.append(el("option", { value: j, text: j })));
  js.value = filters.journal; js.onchange = () => { filters.journal = js.value; saveFilters(); };
  const q = el("input", { type: "search", placeholder: "Search title, idea, reason…", "aria-label": "Search" }); q.value = filters.q;
  q.oninput = () => { filters.q = q.value; saveFilters(); };
  const pt = el("select", { "aria-label": "Paper type" }, el("option", { value: "", text: "All paper types" }),
    el("option", { value: "computational", text: "Computational / ML papers" }),
    el("option", { value: "measured", text: "Experimental & resource papers" }));
  pt.value = filters.ptype; pt.onchange = () => { filters.ptype = pt.value; saveFilters(); };
  f.append(js, pt, q);
}
function saveFilters() { store.set("filters", filters); renderCharts(); renderTable(); }

// ---------- shared time scale ----------
function timeScale(pts, x0, x1) {
  const ts = pts.map((p) => p.t);
  let lo = Math.min(...ts, DATA.generated), hi = Math.max(...ts, DATA.generated);
  if (!isFinite(lo)) { lo = DATA.generated - 3600e3; hi = DATA.generated; }
  const pad = Math.max(60e3, (hi - lo) * 0.03); lo -= pad; hi += pad;
  const x = (t) => x0 + (t - lo) / (hi - lo) * (x1 - x0);
  const span = hi - lo, steps = [5, 10, 15, 30, 60, 120, 180, 360, 720, 1440].map((m) => m * 60e3);
  const step = steps.find((s) => span / s <= 8) || 1440 * 60e3;
  const ticks = []; const off = new Date().getTimezoneOffset() * 60e3;
  for (let t = Math.ceil((lo - off) / step) * step + off; t <= hi; t += step) ticks.push(t);
  return { x, lo, hi, ticks, step };
}
function drawTimeAxis(svg, sc, y, top) {
  let lastDay = "";
  for (const t of sc.ticks) {
    const xx = Math.round(sc.x(t)) + 0.5;
    svg.append(sv("line", { class: "gridline", x1: xx, x2: xx, y1: top, y2: y }));
    const day = fmtDay(t), label = day !== lastDay ? `${day} ${fmtTime(t)}` : fmtTime(t); lastDay = day;
    const tx = sv("text", { class: "tick", x: xx, y: y + 16, "text-anchor": "middle" }); tx.textContent = label; svg.append(tx);
  }
}

// ---------- scatter ----------
let hotId = null;
function renderScatter() {
  const wrap = $("scatterWrap"), svg = $("scatter"), tip = $("tip");
  const W = Math.max(480, wrap.clientWidth), m = { l: 72, r: 16, t: 12 }, plotH = 300, stripH = 44, axisH = 26;
  const H = m.t + plotH + stripH + axisH;
  svg.setAttribute("width", W); svg.setAttribute("height", H); svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.replaceChildren();
  const pts = visible(), sc = timeScale(pts, m.l, W - m.r);
  const yMax = 10, y = (v) => m.t + plotH - (Math.min(v, yMax) / yMax) * plotH;
  const base = m.t + plotH, stripTop = base + 6, stripBot = base + stripH - 6;
  for (let v = 0; v <= yMax; v += 2) {
    const yy = Math.round(y(v)) + 0.5;
    if (v > 0) svg.append(sv("line", { class: "gridline", x1: m.l, x2: W - m.r, y1: yy, y2: yy }));
    const tx = sv("text", { class: "tick", x: m.l - 8, y: yy + 4, "text-anchor": "end" }); tx.textContent = v; svg.append(tx);
  }
  svg.append(sv("rect", { class: "strip", x: m.l, y: stripTop, width: W - m.l - m.r, height: stripBot - stripTop, rx: 4 }));
  const sl = sv("text", { class: "strip-label", x: m.l - 8, y: (stripTop + stripBot) / 2 + 4, "text-anchor": "end" }); sl.textContent = "Rejected"; svg.append(sl);
  drawTimeAxis(svg, sc, base + stripH, m.t);
  svg.append(sv("line", { class: "baseline", x1: m.l, x2: W - m.r, y1: base + 0.5, y2: base + 0.5 }));
  const yt = sv("text", { class: "axis-title", x: 0, y: 0, transform: `translate(14 ${m.t + plotH / 2}) rotate(-90)`, "text-anchor": "middle" }); yt.textContent = "ROI (0–10)"; svg.append(yt);

  // Draw rejects first, then lower tiers, so the best sit on top.
  const order = [...pts].sort((a, b) => TIER[b.tier].rank - TIER[a.tier].rank || a.roi - b.roi);
  const placed = [];
  for (const p of order) {
    const cx = sc.x(p.t);
    const cy = p.tier === "reject" ? stripTop + 8 + hash(p.id) * (stripBot - stripTop - 16) : y(p.roi);
    const c = sv("circle", { cx, cy, r: TIER[p.tier].r, class: `pt t-${p.tier}` + (p.tier !== "reject" && !p.opened ? " hollow" : "") + (p.id === hotId ? " hot" : "") });
    c.dataset.id = p.id;
    if (p.tier !== "reject") {
      c.setAttribute("tabindex", "0"); c.setAttribute("aria-label", `${TIER[p.tier].label}, ROI ${p.roi}: ${p.title}`);
      c.addEventListener("focus", () => showTip(p, cx, cy)); c.addEventListener("blur", hideTip);
      c.addEventListener("keydown", (e) => { if (e.key === "Enter") openReport(p.id); });
    }
    svg.append(c); placed.push({ p, cx, cy });
  }
  // Direct labels: only the top three candidates by ROI.
  // Label the best few, but skip any label that would collide with one already placed —
  // overlapping labels are worse than none, and the tooltip carries the title anyway.
  const boxes = [];
  for (const d of placed.filter((x) => GOOD.has(x.p.tier)).sort((a, b) => b.p.roi - a.p.roi)) {
    if (boxes.length >= 3) break;
    const right = d.cx < W - 260;
    const text = short(d.p.title, 42);
    const w = text.length * 6.2 + 14, x0 = right ? d.cx + 12 : d.cx - 12 - w, y0 = d.cy - 7;
    if (boxes.some((b2) => x0 < b2.x1 && x0 + w > b2.x0 && y0 < b2.y1 && y0 + 14 > b2.y0)) continue;
    boxes.push({ x0, x1: x0 + w, y0, y1: y0 + 14 });
    const t = sv("text", { class: "dlabel", x: d.cx + (right ? 12 : -12), y: d.cy + 4, "text-anchor": right ? "start" : "end" });
    t.textContent = text; svg.append(t);
  }
  // Nearest-point hover: the pointer only has to be closest (within 24px), not on the dot.
  const nearest = (ev) => {
    const b = svg.getBoundingClientRect(), px = ev.clientX - b.left, py = ev.clientY - b.top;
    let best = null, bd = 24 * 24;
    for (const d of placed) { const dd = (d.cx - px) ** 2 + (d.cy - py) ** 2; if (dd <= bd) { bd = dd; best = d; } }
    return best;
  };
  svg.onpointermove = (ev) => {
    const d = nearest(ev);
    setHot(d ? d.p.id : null);
    if (d) { showTip(d.p, d.cx, d.cy); svg.style.cursor = "pointer"; } else { hideTip(); svg.style.cursor = "default"; }
  };
  svg.onpointerleave = () => { setHot(null); hideTip(); };
  svg.onclick = (ev) => { const d = nearest(ev); if (d) openReport(d.p.id); };

  function setHot(id) {
    if (id === hotId) return; hotId = id;
    svg.querySelectorAll(".pt.hot").forEach((n) => n.classList.remove("hot"));
    if (id) { const n = svg.querySelector(`.pt[data-id="${CSS.escape(id)}"]`); if (n) { n.classList.add("hot"); n.parentNode.append(n); } }
  }
  function showTip(p, cx, cy) {
    tip.replaceChildren(
      el("div", { class: "v", text: p.tier === "reject" ? "Rejected" + (p.killers.length ? " · failed " + p.killers.map((k) => k.split("_")[0].toUpperCase()).join(", ") : "") : `ROI ${p.roi}` }),
      el("div", { class: "m" }, lineKey(p.tier), `${TIER[p.tier].label} · ${p.opened ? "data files opened" : "paper text only"} · effort ${p.effort}`),
      el("div", { class: "t", text: short(p.title, 140) }),
      el("div", { class: "m", text: `${p.journal} · finished ${fmtWhen(p.t)}` }),
      pitch(p).headline ? el("div", { class: "pitchline", text: "“" + short(pitch(p).headline, 170) + "”" }) : null,
      plain(p).one_liner ? el("div", { class: "idea", text: "Idea: " + short(plain(p).one_liner, 200) }) : null,
      p.tier !== "reject" ? scoreStrip(p, "strip") : null,
      p.tier !== "reject" ? el("div", { class: "m", text: roiText(p) }) : null,
      p.tier !== "reject" && p.why.misses.length ? el("div", { class: "m", text: "Misses pass marks: " + p.why.misses.join("; ") }) : null,
      el("div", { class: "r", text: short(p.eval.verdict_reason, plain(p).one_liner ? 150 : 220) }),
      el("div", { class: "h", text: "Click for the full report" }));
    tip.style.display = "block";
    const w = tip.offsetWidth, h = tip.offsetHeight, W2 = wrap.clientWidth;
    let left = cx + 14, topY = cy - h / 2;
    if (left + w > W2) left = cx - 14 - w;
    tip.style.left = Math.max(0, left) + "px"; tip.style.top = Math.max(0, topY) + "px";
  }
  function hideTip() { tip.style.display = "none"; }
}
function lineKey(tier) { const k = el("span", { class: "linekey" }); k.style.background = `var(--tier-${tier})`; return k; }

function renderLegend() {
  const l = $("legend"); l.replaceChildren();
  for (const t of TIERS) {
    const k = el("span", { class: "key" }); k.style.background = `var(--tier-${t.key})`;
    l.append(el("span", {}, k, t.label));
  }
  l.append(el("span", {}, el("span", { class: "key hollow" }), "Hollow = scored from paper text; data files never opened"));
}

// ---------- cumulative ----------
function renderCum() {
  const wrap = $("cumWrap"), svg = $("cum"), tip = $("tip2");
  const W = Math.max(480, wrap.clientWidth), m = { l: 72, r: 40, t: 12 }, plotH = 120, axisH = 26, H = m.t + plotH + axisH;
  svg.setAttribute("width", W); svg.setAttribute("height", H); svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.replaceChildren();
  const pts = visible(), sc = timeScale(pts, m.l, W - m.r);
  const good = pts.filter((p) => GOOD.has(p.tier)).sort((a, b) => a.t - b.t);
  const maxN = Math.max(4, good.length), niceMax = Math.ceil(maxN / 2) * 2;
  const y = (v) => m.t + plotH - v / niceMax * plotH, base = m.t + plotH;
  const stepV = niceMax <= 10 ? 2 : Math.ceil(niceMax / 5);
  for (let v = 0; v <= niceMax; v += stepV) {
    const yy = Math.round(y(v)) + 0.5;
    if (v > 0) svg.append(sv("line", { class: "gridline", x1: m.l, x2: W - m.r, y1: yy, y2: yy }));
    const tx = sv("text", { class: "tick", x: m.l - 8, y: yy + 4, "text-anchor": "end" }); tx.textContent = v; svg.append(tx);
  }
  drawTimeAxis(svg, sc, base, m.t);
  svg.append(sv("line", { class: "baseline", x1: m.l, x2: W - m.r, y1: base + 0.5, y2: base + 0.5 }));
  const xStart = sc.x(sc.lo), xEnd = sc.x(Math.min(sc.hi, DATA.generated));
  let d = `M${xStart},${y(0)}`, n = 0;
  const steps = [];
  for (const p of good) { const xx = sc.x(p.t); d += ` L${xx},${y(n)}`; n += 1; d += ` L${xx},${y(n)}`; steps.push({ t: p.t, n, p }); }
  d += ` L${xEnd},${y(n)}`;
  svg.append(sv("path", { class: "cum-area", d: d + ` L${xEnd},${base} L${xStart},${base} Z` }));
  svg.append(sv("path", { class: "cum-line", d }));
  svg.append(sv("circle", { class: "cum-end", cx: xEnd, cy: y(n), r: 4 }));
  const endLabel = sv("text", { class: "dlabel", x: xEnd + 8, y: y(n) + 4 }); endLabel.textContent = n; svg.append(endLabel);
  const cross = sv("line", { class: "crosshair", y1: m.t, y2: base, x1: 0, x2: 0, visibility: "hidden" }); svg.append(cross);
  // Crosshair: snap to the nearest step in time.
  svg.onpointermove = (ev) => {
    const b = svg.getBoundingClientRect(), px = ev.clientX - b.left;
    if (!steps.length || px < m.l || px > W - m.r) { tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); return; }
    let best = steps[0]; for (const s of steps) if (Math.abs(sc.x(s.t) - px) < Math.abs(sc.x(best.t) - px)) best = s;
    const xx = Math.round(sc.x(best.t)) + 0.5; cross.setAttribute("x1", xx); cross.setAttribute("x2", xx); cross.setAttribute("visibility", "visible");
    tip.replaceChildren(el("div", { class: "v", text: `${best.n} worth a look` }), el("div", { class: "m", text: `by ${fmtWhen(best.t)}` }),
      el("div", { class: "t" }, lineKey(best.p.tier), short(best.p.title, 120)), el("div", { class: "h", text: "Click to open this one" }));
    tip.style.display = "block";
    const w = tip.offsetWidth; tip.style.left = (xx + 12 + w > W ? xx - 12 - w : xx + 12) + "px"; tip.style.top = "8px";
    svg.onclick = () => openReport(best.p.id);
  };
  svg.onpointerleave = () => { tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); };
}

function renderCharts() { renderLegend(); renderScatter(); renderCum(); }

// ---------- table ----------
function renderTable() {
  const t = $("table"); t.replaceChildren();
  const head = el("tr", {}, ...["Verdict", "ROI", "Data opened", "Paper", "Effort", "Failed checks / first check", "Finished"].map((h, i) => el("th", { class: i === 1 ? "num" : "", text: h })));
  t.append(el("thead", {}, head));
  const body = el("tbody");
  const rows = visible().sort((a, b) => TIER[a.tier].rank - TIER[b.tier].rank || b.roi - a.roi || b.t - a.t);
  for (const p of rows) {
    const failed = p.killers.map((k) => k.split("_")[0].toUpperCase()).join(", ");
    const first = (p.eval.effort || {}).cheapest_killing_check || "";
    const tr = el("tr", { tabindex: "0", "data-id": p.id, class: location.hash.slice(1) === p.id ? "sel" : "" },
      el("td", { class: "verdict" }, lineKey(p.tier), TIER[p.tier].label, isNew(p) ? el("div", {}, el("span", { class: "pill", text: "New" })) : null),
      el("td", { class: "num", text: p.tier === "reject" ? "–" : p.roi.toFixed(1) }),
      el("td", { class: p.opened ? "ok" : "muted", text: p.opened ? "✓ yes" : "– no" }),
      el("td", {}, el("div", { text: p.title }),
        plain(p).one_liner ? el("div", { class: "idea-row", text: plain(p).one_liner }) : null,
        pitch(p).headline ? el("div", { class: "pitch-row", text: "“" + pitch(p).headline + "”" }) : null,
        el("div", { class: "small", text: `${p.journal} · ${p.id} · ${p.ptype}` })),
      el("td", { text: p.tier === "reject" ? "–" : p.effort }),
      el("td", { text: failed ? "Failed " + failed : short(first, 120) }),
      el("td", { class: "num", text: fmtWhen(p.t) }));
    tr.onclick = () => openReport(p.id);
    tr.onkeydown = (e) => { if (e.key === "Enter") openReport(p.id); };
    body.append(tr);
  }
  if (!rows.length) body.append(el("tr", {}, el("td", { colspan: "7", class: "muted", text: "No evaluations match the filters." })));
  t.append(body);
}

// ---------- report drawer ----------
function openReport(id) {
  const p = DATA.points.find((x) => x.id === id); if (!p) return;
  if (p.tier !== "reject") { seen.add(id); store.set("seen", [...seen]); }
  history.replaceState(null, "", "#" + id);
  const r = p.eval, d = $("drawer"); d.replaceChildren();
  const sec = (title, ...kids) => { if (kids.every((k) => k == null || k === "")) return; d.append(el("h3", { text: title })); kids.forEach((k) => k != null && k !== "" && d.append(k instanceof Node ? k : el("p", {}, links(k)))); };
  d.append(el("button", { class: "close", onclick: closeReport, "aria-label": "Close report" }, "Close ✕"));
  d.append(el("div", { class: "small" }, lineKey(p.tier), `${TIER[p.tier].label} · ${p.tier === "reject" ? "rejected" : "ROI " + p.roi} · effort ${p.effort} · ${p.opened ? "data files opened" : "paper text only"}`));
  d.append(el("h2", { text: p.title }));
  const meta = el("p", { class: "small" }, `${p.journal} · ${p.id} · finished ${fmtWhen(p.t)}`);
  if (r.doi) { const a = el("a", { target: "_blank", rel: "noopener" }, "doi:" + r.doi); a.href = "https://doi.org/" + encodeURIComponent(r.doi).replace(/%2F/g, "/"); meta.append(" · ", a); }
  d.append(meta);
  const files = el("p", { class: "small" });
  for (const [label, href] of [["evaluation JSON", `evaluations/${p.id}.json`], ["agent log", `logs/${p.id}.log`]]) { const a = el("a", { target: "_blank" }, label); a.href = href; files.append(files.childNodes.length ? " · " : "", a); }
  d.append(files);

  const pch = pitch(p);
  if (pch.headline) {
    const box = el("div", { class: "pitchbox" }, el("p", { class: "head" }, links(pch.headline)));
    for (const [label, key] of [["What this data has that others don't: ", "what_makes_the_data_unusual"],
                                ["The result that would make it worth it: ", "the_win"],
                                ["What we learn even if it fails: ", "even_if_it_fails"],
                                ["Who would read it: ", "who_wants_it"]])
      if (pch[key]) box.append(el("p", {}, el("b", { text: label }), links(pch[key])));
    d.append(el("h3", { text: "The case for doing it" }), box);
  }
  const cc = (p.why || {}).crowded_case || {};
  if (Object.keys(cc).length) {
    const box = el("div", { class: "plainbox" });
    for (const [key, q] of [["more_or_better_data", "1. Much more data, or better data for a specific area?"],
                            ["confidence_score", "2. Can we give a confidence score the others don't?"],
                            ["models_beatable", "3. Are the existing models old or weak enough to beat?"],
                            ["guaranteed_improvement", "4. Is better performance near-certain from joinable new data?"]]) {
      const a = cc[key] || {}, ans = String(a.answer || "–").toLowerCase();
      box.append(el("p", {}, el("b", { text: q + " " }),
        el("span", { class: ans === "yes" ? "ok" : ans === "no" ? "bad" : "", text: ans.toUpperCase() + ". " }), links(a.why || "")));
    }
    d.append(el("h3", { text: "Other models exist — what's our edge?" }), box);
  }
  const le = (p.why || {}).label_info || {};
  if (le.sources || le.why) {
    const box = el("div", { class: "plainbox" });
    box.append(el("p", {}, el("b", { text: `${(p.why || {}).label_text} (score ${le.score ?? "?"}/2)` })));
    for (const [label, key] of [["Where else it's measured: ", "sources"], ["Roughly how many items: ", "approx_items"],
                                ["Joined on: ", "join_key"], ["What that makes possible: ", "why"]])
      if (le[key] != null && le[key] !== "") box.append(el("p", {}, el("b", { text: label }), links(String(le[key]))));
    d.append(el("h3", { text: "Is the label already known for many other things?" }), box);
  }
  const pi = plain(p), pb = el("div", { class: "plainbox" });
  if (pi.one_liner) {
    pb.append(el("p", {}, el("b", { text: pi.one_liner })));
    for (const [label, key] of [["Train on: ", "train_on"], ["To predict: ", "predict"], ["Why it's worth doing: ", "why_worthwhile"]])
      if (pi[key]) pb.append(el("p", {}, el("b", { text: label }), links(pi[key])));
  } else pb.append(el("p", { class: "muted", text: "No plain-language idea recorded (evaluated before the field existed; run scripts/backfill_fields.py). The technical version is below." }));
  d.append(el("h3", { text: "The idea in plain words" }), pb);
  sec("Verdict", r.verdict_reason);
  sec("The idea (technical)", r.ml_idea);
  sec("What it could honestly claim", r.honest_claim_h3);
  sec("Check this first (cheapest kill)", (r.effort || {}).cheapest_killing_check, (r.effort || {}).hours_to_feature_table != null ? `Hours to a feature/label table: ${(r.effort || {}).hours_to_feature_table}` : null);
  sec("Data", r.data_url, ((r.part_a_killers || {}).a5_enough_rows || {}).n_rows != null ? `Rows: ${Number(r.part_a_killers.a5_enough_rows.n_rows).toLocaleString()}` : null);

  const v = r.verified || {};
  sec("What was verified", el("p", {}, el("span", { class: v.opened_data_files ? "ok" : "bad", text: v.opened_data_files ? "✓ Opened the data files. " : "✗ Data files not opened. " }), links(v.what_i_actually_checked)),
      v.what_came_from_paper_text_only ? el("p", { class: "muted" }, "From paper text only: ", links(v.what_came_from_paper_text_only)) : null);

  const killersTbl = el("table");
  for (const [k, val] of Object.entries(r.part_a_killers || {})) {
    if (!val || typeof val !== "object") continue;
    const pass = val.pass, extra = [val.n_rows != null ? `n=${val.n_rows}` : "", val.working_set || ""].filter(Boolean).join(" · ");
    killersTbl.append(el("tr", {},
      el("td", { class: pass === false ? "bad" : pass === true ? "ok" : "muted", text: (pass === false ? "✗ " : pass === true ? "✓ " : "? ") + k.replace(/_/g, " ") }),
      el("td", {}, extra ? el("div", { class: "small", text: extra }) : null, links(val.note || ""))));
  }
  sec("Part A · deal-breakers (any ✗ rejects the paper)", killersTbl);

  const w = p.why;
  if (p.tier !== "reject") {
    sec("Why this ROI", el("div", { class: "roiline", text: roiText(p) }),
        w.misses.length ? el("p", { class: "bad", text: "✗ Misses pass marks: " + w.misses.join("; ") }) : el("p", { class: "ok", text: "✓ Meets every pass mark" }));
    const box = el("div");
    for (const pt of w.parts) {
      const val = pt.total, ok = val == null ? null : val >= pt.need;
      const bar = el("div", { class: "scorebar" }, el("span"), el("i"));
      bar.firstChild.style.width = val == null ? "0" : (100 * Math.min(1, val / pt.max)) + "%";
      bar.lastChild.style.left = (100 * pt.need / pt.max) + "%"; bar.title = `pass mark ${pt.need}/${pt.max}`;
      box.append(el("div", { class: "scorerow parthead" },
        el("span", {}, el("b", { text: `${pt.part} · ${pt.name}` }), pt.in_roi ? el("span", { class: "small", text: " · in ROI" }) : null),
        el("span", { class: ok == null ? "muted" : ok ? "ok" : "bad", text: val == null ? "–" : `${ok ? "✓" : "✗"} ${val}/${pt.max}` }), bar));
      if (pt.from_items) for (const it of pt.items)
        box.append(el("div", { class: "itemrow" }, el("span", { class: "small", text: it.id }),
          el("span", { text: it.score == null ? "–" : `${it.score}/2` }), el("span", { class: "why" }, el("span", { text: it.name + ". " }), links(it.why))));
    }
    if (!w.parts.some((pt) => pt.from_items))
      box.append(el("p", { class: "muted", text: "Item-by-item reasons weren't recorded for this paper (evaluated before they were added). Only part totals are available." }));
    sec("Scores, with the reason for each item (the tick on each bar is the pass mark)", box);
    const lp = w.laptop, fmt = (v, unit) => v == null ? "not recorded" : `${v} ${unit}`;
    const lap = el("div");
    for (const [label, val] of [["Data to load", fmt(lp.working_set_gb, "GB")], ["Peak memory (laptop has 16 GB)", fmt(lp.peak_ram_gb, "GB")],
        ["One-time embedding on CPU", fmt(lp.embedding_hours, "hours")], ["One cross-validation run", fmt(lp.cv_run_minutes, "minutes")],
        ["Needs a GPU (graphics card)", lp.needs_gpu == null ? "not recorded" : lp.needs_gpu ? "yes" : "no"]])
      lap.append(el("div", { class: "itemrow", style: "grid-template-columns: 230px 1fr" }, el("span", { text: label }), el("span", { class: val === "not recorded" ? "muted" : "", text: val })));
    if (lp.note) lap.append(el("p", { class: "small" }, links(lp.note)));
    sec("Runs on a laptop?", lap);
    if (w.e_items.some((e) => e.ok != null)) {
      const eb = el("div");
      for (const e of w.e_items) eb.append(el("div", { class: "itemrow" }, el("span", { class: "small", text: e.id }),
        el("span", { class: e.ok ? "ok" : e.ok === false ? "bad" : "muted", text: e.ok ? "✓" : e.ok === false ? "✗" : "–" }),
        el("span", { class: "why" }, el("span", { text: e.name + ". " }), links(e.why))));
      sec("E · evaluation is possible and honest", eb);
    }
  }
  const b = r.backbone || {};
  sec("Backbone", b.name ? `${b.name}${b.freeze_and_head_viable === false ? " (freeze-and-head NOT viable)" : ""} · ${b.hardware || "?"} · licence: ${b.licence || "?"}` : null);
  const models = ((r.c2_audit || {}).existing_models || []).filter((x) => x && x.name);
  if (models.length) {
    const ul = el("ul");
    for (const x of models) ul.append(el("li", {}, el("b", { text: x.name }), ` · weights ${x.open_weights === true ? "open" : x.open_weights === false ? "closed" : "?"}`,
      x.score_and_split ? el("div", { class: "small" }, links(x.score_and_split)) : null,
      x.authors_named_unsolved ? el("div", { class: "small" }, "Unsolved: ", links(x.authors_named_unsolved)) : null,
      x.runnable_as_baseline ? el("div", { class: "small ok", text: "runnable as our baseline" }) : null));
    sec("Who has already modelled this", ul);
  } else sec("Who has already modelled this", "Nobody found. That's weak evidence it's new: absence of search results isn't proof.");
  const ig = r.integration || {};
  sec("Integration", ig.partner_dataset ? `${ig.partner_dataset} · join on ${ig.join_key || "?"}` : null, ig.bigger_question_one_sentence);
  const c = r.controls || {};
  sec("Controls", c.positive_control ? "Positive: " + c.positive_control : null, c.negative_control ? "Negative: " + c.negative_control : null, c.external_validation ? "External validation: " + c.external_validation : null);
  const lk = r.leakage_plan;
  if (lk && typeof lk === "object") {
    const box = el("div", { class: "plainbox" });
    if (lk.verdict) box.append(el("p", {}, el("b", { text: lk.verdict })));
    const list = (label, xs) => { if (!xs || !xs.length) return; box.append(el("p", {}, el("b", { text: label })));
      const ul = el("ul"); [].concat(xs).forEach((x) => ul.append(el("li", {}, links(x)))); box.append(ul); };
    list("Ways it could leak:", lk.risks);
    if (lk.split) box.append(el("p", {}, el("b", { text: "The split that prevents it: " }), links(lk.split)));
    list("Checks that would catch it:", lk.checks);
    d.append(el("h3", { text: "Preventing data leakage" }), box);
  }
  const sc_ = r.success_criteria;
  if (sc_ && typeof sc_ === "object") {
    const box = el("div", { class: "plainbox" });
    for (const [label, key] of [["Deciding metric: ", "metric"], ["Baselines to beat: ", "baselines"], ["Counts as success: ", "success"],
                                ["Best possible (noise ceiling): ", "noise_ceiling"], ["Stop if: ", "kill"]])
      if (sc_[key] != null && sc_[key] !== "") box.append(el("p", {}, el("b", { text: label }), links(String(sc_[key]))));
    d.append(el("h3", { text: "What counts as success" }), box);
  }
  const pt = r.publication_target;
  if (pt && typeof pt === "object") {
    const box = el("div", { class: "plainbox" });
    for (const [label, key] of [["Ambitious: ", "ambitious"], ["Realistic: ", "realistic"], ["Fallback: ", "fallback"]]) {
      const v = pt[key] || {};
      if (v.journal) box.append(el("p", {}, el("b", { text: label + v.journal + ". " }), links(v.why || "")));
    }
    sec("Where to publish it", box);
  }
  const raw = el("details", {}, el("summary", { text: "Raw JSON" }), el("pre", { text: JSON.stringify(r, null, 2) }));
  d.append(el("h3", { text: "Everything" }), raw);
  d.hidden = false; d.scrollTop = 0; d.querySelector(".close").focus();
  document.querySelectorAll("tr.sel").forEach((n) => n.classList.remove("sel"));
  const row = document.querySelector(`tr[data-id="${CSS.escape(id)}"]`); if (row) { row.classList.add("sel"); row.querySelector(".pill")?.remove(); }
}
function closeReport() { $("drawer").hidden = true; history.replaceState(null, "", location.pathname); document.querySelectorAll("tr.sel").forEach((n) => n.classList.remove("sel")); }
addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("drawer").hidden) closeReport(); });

// ---------- auto-reload (paused while reading or interacting) ----------
let lastActivity = Date.now();
for (const ev of ["pointermove", "keydown", "wheel", "scroll", "input"]) addEventListener(ev, () => { lastActivity = Date.now(); }, { passive: true, capture: true });
function reloadKeepingPlace() { sessionStorage.setItem("bioml_scroll", String(scrollY)); location.reload(); }
setInterval(() => { if ($("drawer").hidden && Date.now() - lastActivity > 20000) reloadKeepingPlace(); }, 60000);
// Coming back to the tab (after sleep, or from another window) reloads at once if the data is
// stale — even with a report open, which reopens itself from the URL. Timers in background tabs
// are throttled, so the minute-by-minute reload alone can leave a tab hours behind.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && Date.now() - DATA.generated > 90e3) reloadKeepingPlace();
});
// Say plainly when the page is behind, so a stale view is never mistaken for a stalled run.
setInterval(() => {
  const mins = Math.round((Date.now() - DATA.generated) / 60e3);
  if (mins >= 2) $("status").textContent = `⚠ This view is ${mins} min old — press Refresh (or Cmd-R) for the latest. ` +
    ($("drawer").hidden ? "" : "Auto-reload is paused while a report is open.");
}, 30e3);

function renderAll() { renderHeader(); renderFilters(); renderCharts(); renderTable(); }
renderAll();
addEventListener("resize", () => renderCharts());
const sy = sessionStorage.getItem("bioml_scroll"); if (sy) { scrollTo(0, Number(sy)); sessionStorage.removeItem("bioml_scroll"); }
if (location.hash.length > 1) openReport(decodeURIComponent(location.hash.slice(1)));
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
