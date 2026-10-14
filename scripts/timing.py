#!/usr/bin/env python3
"""Where does a paper evaluation's wall-clock time go? Reads the agent transcripts.

    python3 scripts/timing.py

Pairs each tool_use with its tool_result in ~/.claude/projects/<this repo>/*/subagents/*.jsonl
and reports per-paper wall time, time inside tools, and the slowest calls. Run it after a batch
to see whether a prompt change actually made evaluations faster.
"""
import json, glob, re, sys, collections
from datetime import datetime
from pathlib import Path

P = Path.home() / ".claude/projects/-Users-haim-k-Desktop-personal-projects-ml-folder-attempts-finding-best-ai-or-ml-projects"
def ts(s): return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

papers = []
for f in glob.glob(str(P / "*/subagents/*.jsonl")):
    events = []
    for line in open(f):
        try: events.append(json.loads(line))
        except Exception: pass
    if not events: continue
    blob = json.dumps(events[0].get("message", ""))
    m = re.search(r"paper_id[\"':\s]+([a-z0-9-]+)", blob)
    if not m: continue
    pid = m.group(1)
    uses, results = {}, {}
    for e in events:
        t = e.get("timestamp")
        for c in (e.get("message") or {}).get("content") or []:
            if not isinstance(c, dict): continue
            if c.get("type") == "tool_use":
                i = c.get("input") or {}
                detail = str(i.get("command") or i.get("url") or i.get("query") or i.get("file_path") or "")
                uses[c["id"]] = (c["name"], detail, ts(t))
            elif c.get("type") == "tool_result":
                results[c.get("tool_use_id")] = ts(t)
    if not uses: continue
    calls = [(n, d, results[i] - s) for i, (n, d, s) in uses.items() if i in results]
    start = min(s for _, _, s in uses.values())
    end = max(list(results.values()) + [ts(events[-1]["timestamp"])])
    papers.append({"paper": pid, "wall": end - start, "calls": calls,
                   "tool_time": sum(d for _, _, d in calls), "n": len(calls), "file": f})

papers.sort(key=lambda p: -p["wall"])
print(f"{len(papers)} evaluations with transcripts\n")
print(f"{'paper':<20}{'wall':>7}{'in tools':>10}{'thinking':>10}{'calls':>7}  slowest single call")
for p in papers[:14]:
    slow = max(p["calls"], key=lambda c: c[2]) if p["calls"] else ("-", "", 0)
    print(f"{p['paper'][:19]:<20}{p['wall']/60:>6.1f}m{p['tool_time']/60:>9.1f}m"
          f"{(p['wall']-p['tool_time'])/60:>9.1f}m{p['n']:>7}  {slow[0]} {slow[2]:.0f}s {slow[1][:46]}")

tot = collections.Counter(); cnt = collections.Counter(); slowest = []
for p in papers:
    for n, d, dur in p["calls"]:
        tot[n] += dur; cnt[n] += 1; slowest.append((dur, n, d, p["paper"]))
wall = sum(p["wall"] for p in papers)
print(f"\nTotal across {len(papers)} papers: {wall/3600:.1f} h wall, "
      f"{sum(tot.values())/3600:.1f} h in tools ({100*sum(tot.values())/wall:.0f}%), "
      f"{(wall-sum(tot.values()))/3600:.1f} h model thinking")
print(f"\n{'tool':<14}{'total':>9}{'calls':>7}{'mean':>8}{'share of tool time':>20}")
for n, t in tot.most_common():
    print(f"{n:<14}{t/60:>8.0f}m{cnt[n]:>7}{t/cnt[n]:>7.0f}s{100*t/sum(tot.values()):>18.0f}%")
print("\nSlowest individual calls:")
for dur, n, d, pid in sorted(slowest, reverse=True)[:18]:
    flat = re.sub(r"\s+", " ", d)[:92]
    print(f"  {dur:>6.0f}s {n:<10} {pid[:17]:<18} {flat}")
