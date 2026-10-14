#!/usr/bin/env python3
"""Add fields that older evaluations don't have, extracted from their own text.

    python3 scripts/backfill_fields.py          # candidates only (not rejected)
    python3 scripts/backfill_fields.py --all    # rejects too

  plain_idea      the plain-language idea block
  pitch           the case for doing it (non-rejected papers only)
  effort.*        working_set_gb, needs_gpu, peak_ram_gb, embedding_hours, cv_run_minutes,
                  compute_note: the laptop-feasibility facts ROI uses (null when not stated)
  c2_backfill     state of the field, per the CHECKLIST.md C2 guide, when C2 wasn't scored

One short `claude --print` call per missing group. The model only returns the new fields, from
what is already in the evaluation; this script merges them in, so nothing else can change. The
file's modification time is kept, because the dashboard uses it as the finish time.
Safe to re-run: anything already present is skipped.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / ".claude" / "agents" / "paper-evaluator.md"
KEYS = ("one_liner", "train_on", "predict", "why_worthwhile")
PITCH_KEYS = ("headline", "what_makes_the_data_unusual", "the_win", "even_if_it_fails", "who_wants_it")
CONTEXT = ("title", "journal", "data_url", "ml_idea", "honest_claim_h3", "verdict", "verdict_reason",
           "part_a_killers", "controls", "integration", "backbone", "effort")


CHECKLIST = ROOT / "CHECKLIST.md"


def c2_guide() -> str:
    m = re.search(r"^Scoring guide:.*?(?=^\*\*C1 is the ROI question)", CHECKLIST.read_text(), re.S | re.M)
    if not m:
        sys.exit(f"No C2 scoring guide in {CHECKLIST}")
    return m.group(0).strip()


def section(heading: str) -> str:
    """A section of the evaluator prompt, so the backfill and the evaluator use one wording."""
    m = re.search(rf"^## {re.escape(heading)}.*?(?=^## )", AGENT.read_text(), re.S | re.M)
    if not m:
        sys.exit(f"No '## {heading}' section in {AGENT}")
    return m.group(0).strip()


def rejected(r: dict) -> bool:
    a = r.get("part_a_killers") or {}
    failed = any(isinstance(v, dict) and v.get("pass") is False for v in a.values())
    return failed or str(r.get("verdict") or "").strip().lower() == "reject"


def has_block(r: dict) -> bool:
    p = r.get("plain_idea")
    return isinstance(p, dict) and bool(str(p.get("one_liner") or "").strip())


def has_pitch(r: dict) -> bool:
    p = r.get("pitch")
    return isinstance(p, dict) and bool(str(p.get("headline") or "").strip())


def ask_pitch(r: dict, guide: str) -> dict | None:
    """The pitch argues for the project, so it gets the verified block and the audit as evidence."""
    context = {k: r.get(k) for k in ("title", "journal", "plain_idea", "ml_idea", "honest_claim_h3", "verdict",
                                     "verdict_reason", "verified", "part_a_killers", "controls", "integration",
                                     "backbone", "c2_audit", "effort", "data_url")}
    prompt = (f"{guide}\n\nBelow is a finished evaluation of one paper. Write its `pitch`, using only what this "
              "evaluation says; don't add facts or numbers. Where a claim rests on the paper's text rather than the "
              "files, say so in that sentence.\nReply with only a JSON object with exactly these keys: "
              f"{', '.join(PITCH_KEYS)}. No prose, no code fence.\n\n"
              + json.dumps(context, indent=1, ensure_ascii=False))
    obj = claude_json(prompt, lambda o: all(isinstance(o.get(k), str) and o[k].strip() for k in PITCH_KEYS))
    return {k: obj[k].strip() for k in PITCH_KEYS} if obj else None


def with_pitch(r: dict, block: dict) -> dict:
    """pitch goes just before plain_idea, so the report leads with the case for doing it."""
    out: dict = {}
    for k, v in r.items():
        if k == "plain_idea":
            out["pitch"] = block
        if k != "pitch":
            out[k] = v
    out.setdefault("pitch", block)
    return out


FACT_KEYS = ("working_set_gb", "needs_gpu", "peak_ram_gb", "embedding_hours", "cv_run_minutes", "compute_note")


def c2_scored(r: dict) -> bool:
    return isinstance(((r.get("score_items") or {}).get("C2") or {}).get("score"), int)


def needs_facts(r: dict) -> bool:
    e = r.get("effort") or {}
    return any(k not in e for k in FACT_KEYS) or not (c2_scored(r) or isinstance(r.get("c2_backfill"), dict))


def first_json_object(text: str) -> dict | None:
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                continue
    return None


def claude_json(prompt: str, valid) -> dict | None:
    for _ in range(2):  # retry once, then give up on this file
        try:
            out = subprocess.run(["claude", "--print"], input=prompt, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            continue
        obj = first_json_object(out.stdout)
        if obj is not None and valid(obj):
            return obj
    return None


def ask_facts(r: dict, guide: str) -> dict | None:
    context = {"ml_idea": r.get("ml_idea"), "plain_idea": r.get("plain_idea"), "backbone": r.get("backbone"),
               "a6_fits_laptop": (r.get("part_a_killers") or {}).get("a6_fits_laptop"),
               "effort": r.get("effort"), "c2_audit": r.get("c2_audit")}
    prompt = ("From the evaluation below, extract these facts, using only what it says. Every fact is about the "
              "main proposed task, not optional variants; use null when the evaluation doesn't say.\n"
              "- working_set_gb: size in GB of the data you'd actually load to build the feature/label table, "
              "after subsetting. Not raw data.\n"
              "- needs_gpu: whether the baseline needs a GPU.\n"
              "- peak_ram_gb: memory in GB at the heaviest step, with the streaming or subsetting it describes.\n"
              "- embedding_hours: one-time CPU hours to compute backbone embeddings.\n"
              "- cv_run_minutes: minutes for one full cross-validation run.\n"
              "- compute_note: one plain sentence on where these numbers come from and whether anything was timed.\n"
              "- c2: score existing work on this task with this guide, with a why of at most 20 words naming the "
              f"models that decide it:\n{guide}\n\n"
              'Reply with only JSON: {"working_set_gb": number or null, "needs_gpu": true/false/null, '
              '"peak_ram_gb": number or null, "embedding_hours": number or null, "cv_run_minutes": number or null, '
              '"compute_note": "...", "c2": {"score": 0/1/2, "why": "..."}}. No prose, no code fence.\n\n'
              + json.dumps(context, indent=1, ensure_ascii=False))

    def num_or_null(v) -> bool:
        return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0)

    def valid(o: dict) -> bool:
        gpu, c = o.get("needs_gpu"), o.get("c2")
        return (all(num_or_null(o.get(k)) for k in ("working_set_gb", "peak_ram_gb", "embedding_hours", "cv_run_minutes"))
                and isinstance(o.get("compute_note", ""), str)
                and (gpu is None or isinstance(gpu, bool)) and isinstance(c, dict)
                and c.get("score") in (0, 1, 2) and isinstance(c.get("why"), str) and c["why"].strip())
    return claude_json(prompt, valid)


def ask(r: dict, guide: str) -> dict | None:
    context = {k: r.get(k) for k in CONTEXT}
    prompt = (f"{guide}\n\nBelow is a finished evaluation of one paper. Write its `plain_idea` block using only "
              "what is in this evaluation. Don't add facts, numbers or claims that aren't in it; if the number "
              "of examples isn't stated, say \"unknown\".\nReply with only a JSON object with exactly these keys: "
              "one_liner, train_on, predict, why_worthwhile. No prose, no code fence.\n\n"
              + json.dumps(context, indent=1, ensure_ascii=False))
    obj = claude_json(prompt, lambda o: all(isinstance(o.get(k), str) and o[k].strip() for k in KEYS))
    return {k: obj[k].strip() for k in KEYS} if obj else None


def with_plain_idea(r: dict, block: dict) -> dict:
    """plain_idea goes just before ml_idea, so it reads first."""
    out: dict = {}
    for k, v in r.items():
        if k == "ml_idea":
            out["plain_idea"] = block
        if k != "plain_idea":
            out[k] = v
    out.setdefault("plain_idea", block)
    return out


def with_facts(r: dict, facts: dict) -> dict:
    out = dict(r)
    e = dict(out.get("effort") or {})
    for k in FACT_KEYS:
        e.setdefault(k, facts.get(k) if k != "compute_note" else (facts.get(k) or "").strip())
    out["effort"] = e
    if not c2_scored(out):
        out.setdefault("c2_backfill", {"score": facts["c2"]["score"], "why": facts["c2"]["why"].strip()})
    return out


def write(path: Path, out: dict) -> None:
    """Write atomically and keep the original mtime (the dashboard's finish time)."""
    st = path.stat()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    os.utime(path, (st.st_atime, st.st_mtime))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="also backfill rejected papers")
    args = ap.parse_args()
    guide, pitch_guide, c2g = section("The idea in plain words (`plain_idea`)"), section("The sales pitch (`pitch`)"), c2_guide()
    todo = []
    for p in sorted(glob.glob(str(ROOT / "evaluations" / "*.json"))):
        try:
            r = json.loads(Path(p).read_text())
        except (json.JSONDecodeError, OSError):
            continue
        needs_pitch = not rejected(r) and not has_pitch(r)
        if (args.all or not rejected(r)) and (not has_block(r) or needs_facts(r) or needs_pitch):
            todo.append(Path(p))
    print(f"{len(todo)} evaluations need fields.")
    failed = 0
    for path in todo:
        r = json.loads(path.read_text())
        notes = []
        if not has_block(r):
            block = ask(r, guide)
            if block:
                r = with_plain_idea(r, block)
                notes.append("plain_idea")
        if not rejected(r) and not has_pitch(r):
            block = ask_pitch(r, pitch_guide)
            if block:
                r = with_pitch(r, block)
                notes.append("pitch")
        if needs_facts(r):
            facts = ask_facts(r, c2g)
            if facts:
                r = with_facts(r, facts)
                notes.append(", ".join(f"{k}={facts.get(k)}" for k in FACT_KEYS[:5]) + f", C2={facts['c2']['score']}")
        if notes:
            write(path, r)
            print(f"  ok      {path.stem}: {'; '.join(notes)}")
        if not has_block(r) or needs_facts(r) or (not rejected(r) and not has_pitch(r)):
            failed += 1
            print(f"  FAILED  {path.stem} (something still missing)")
    print(f"Done: {len(todo) - failed} complete, {failed} incomplete.")


if __name__ == "__main__":
    main()
