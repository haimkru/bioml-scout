#!/usr/bin/env python3
"""Compare two sets of evaluations of the same papers, e.g. Opus vs Sonnet.

    python3 scripts/compare_models.py <dir_a> <dir_b> [--names Opus Sonnet]

For each paper in both directories: verdict, failed Part A checks, part totals, ROI, whether the
data files were opened, and — from the agent transcripts — wall time, tool calls and tokens.
Agreement on the verdict and on which deal-breakers failed matters most: a cheaper model that
rejects good papers, or passes bad ones, costs more than it saves.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scoring as sc  # noqa: E402

PROJECTS = Path.home() / ".claude" / "projects"


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def transcripts() -> dict[tuple[str, str], dict]:
    """(paper_id, model family) -> wall minutes, tool calls, tokens. Takes the latest run of each."""
    out: dict[tuple[str, str], dict] = {}
    for f in glob.glob(str(PROJECTS / "*" / "**" / "*.jsonl"), recursive=True):
        try:
            ev = [json.loads(l) for l in open(f) if l.strip()]
        except (OSError, json.JSONDecodeError):
            continue
        first_user = next((e for e in ev if e.get("type") == "user"), None)
        if not first_user:
            continue
        m = re.search(r"Evaluate exactly one paper.*?paper_id: ([a-z0-9-]+)|paper_id: ([a-z0-9-]+)",
                      json.dumps(first_user.get("message", "")), re.S)
        if not m:
            continue
        pid = m.group(1) or m.group(2)
        models = [(e.get("message") or {}).get("model") or "" for e in ev if e.get("type") == "assistant"]
        model = next((x for x in models if x), "")
        fam = "sonnet" if "sonnet" in model else "opus" if "opus" in model else model or "?"
        usage = [(e.get("message") or {}).get("usage") or {} for e in ev if e.get("type") == "assistant"]
        calls = sum(1 for e in ev for c in (e.get("message") or {}).get("content") or []
                    if isinstance(c, dict) and c.get("type") == "tool_use")
        if calls == 0:
            continue  # an outer wrapper session or a trivial test, not an evaluation
        tok_in = sum(u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                     + u.get("cache_read_input_tokens", 0) for u in usage)
        tok_out = sum(u.get("output_tokens", 0) for u in usage)
        stamps = [ts(e["timestamp"]) for e in ev if e.get("timestamp")]
        if not stamps:
            continue
        rec = {"wall": (max(stamps) - min(stamps)) / 60, "calls": calls,
               "tok_in": tok_in, "tok_out": tok_out, "end": max(stamps), "model": model}
        key = (pid, fam)
        if key not in out or rec["end"] > out[key]["end"]:
            out[key] = rec
    return out


def summary(path: Path) -> dict:
    r = json.loads(path.read_text())
    b = sc.breakdown(r)
    verdict = "reject" if b["killers"] or r.get("verdict") == "reject" else r.get("verdict")
    return {"verdict": verdict, "killers": sorted(k.split("_")[0] for k in b["killers"]), "roi": b["roi"],
            "parts": {p["part"]: p["total"] for p in b["parts"]},
            "opened": bool((r.get("verified") or {}).get("opened_data_files")),
            "label": b["label"], "pitch": bool((r.get("pitch") or {}).get("headline"))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir_a")
    ap.add_argument("dir_b")
    ap.add_argument("--names", nargs=2, default=["Opus", "Sonnet"])
    a = ap.parse_args()
    A, B = Path(a.dir_a), Path(a.dir_b)
    na, nb = a.names
    ids = sorted({p.stem for p in B.glob("*.json")} & {p.stem for p in A.glob("*.json")})
    if not ids:
        print("No papers in both directories yet.")
        return 1
    tx = transcripts()
    fa, fb = na.lower(), nb.lower()

    same_verdict = same_killers = 0
    tot = {fa: [0, 0, 0.0, 0], fb: [0, 0, 0.0, 0]}  # tokens in, out, minutes, papers with transcripts
    for pid in ids:
        sa, sb = summary(A / f"{pid}.json"), summary(B / f"{pid}.json")
        ta, tb = tx.get((pid, fa)), tx.get((pid, fb))
        same_verdict += sa["verdict"] == sb["verdict"]
        same_killers += sa["killers"] == sb["killers"]
        mark = "  " if sa["verdict"] == sb["verdict"] else "≠ "
        print(f"{mark}{pid}")
        for name, s, t in ((na, sa, ta), (nb, sb, tb)):
            parts = " ".join(f"{k}{'' if v is None else f'{v:g}'}" for k, v in s["parts"].items())
            cost = (f"{t['wall']:4.1f} min, {t['calls']:>2} calls, {(t['tok_in'] + t['tok_out']) / 1e3:6.0f}k tok"
                    if t else "no transcript")
            print(f"    {name:<7}{s['verdict']:<13} ROI {s['roi']:>4}  killers {','.join(s['killers']) or '-':<14}"
                  f" opened={'Y' if s['opened'] else 'n'}  {parts:<34} {cost}")
            if t:
                k = name.lower()
                tot[k][0] += t["tok_in"]; tot[k][1] += t["tok_out"]; tot[k][2] += t["wall"]; tot[k][3] += 1
    n = len(ids)
    print(f"\nPapers compared: {n}")
    print(f"Same verdict:          {same_verdict}/{n}")
    print(f"Same failed checks:    {same_killers}/{n}")
    for k, name in ((fa, na), (fb, nb)):
        i, o, m, c = tot[k]
        if c:
            print(f"{name:<7} per paper:  {(i + o) / c / 1e3:6.0f}k tokens ({o / c / 1e3:.1f}k written), {m / c:4.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
