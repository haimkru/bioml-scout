#!/usr/bin/env python3
"""Aggregate evaluations/*.json into PROJECTS.md — one file, best projects first.

Also writes projects.csv so you can sort it in a spreadsheet.
No LLM here: scores come from the fixed rubric so they're comparable across papers.
"""
import json, glob, csv, sys
from pathlib import Path

rows, broken = [], []
for p in sorted(glob.glob("evaluations/*.json")):
    try:
        rows.append(json.loads(Path(p).read_text()))
    except Exception as e:
        broken.append((p, str(e)))

if not rows:
    sys.exit("No evaluations found. Run scripts/run_queue.sh first.")

EFFORT_LABEL = {1: "a day", 2: "a few days", 3: "1-2 weeks", 4: "a month", 5: "months"}


def killers(r):
    a = r.get("part_a_killers") or {}
    return [k.split("_", 1)[1] for k, v in a.items() if isinstance(v, dict) and v.get("pass") is False]


def effort(r):
    e = (r.get("effort") or {}).get("hours_to_feature_table")
    if e is None:
        return 3
    return 1 if e <= 8 else 2 if e <= 32 else 3 if e <= 80 else 4 if e <= 200 else 5


# Each part has a different maximum, so normalise to 0-1 before combining.
# Otherwise a cheap project with a weak question outranks a strong one.
MAXES = {"c_worth_asking": 10, "d_backbone_fit": 6, "g_integration": 8, "i_does_anyone_care": 8}


def roi(r):
    s = r.get("scores") or {}
    num = sum((s.get(k) or 0) / m for k, m in MAXES.items()) / len(MAXES)   # 0-1
    return round(10 * num / effort(r), 1)


# Verdict decides the tier; ROI only orders within a tier. A cheap project you've
# decided to park should never sit above one you've decided to do.
VERDICT_RANK = {"do_it": 0, "verify_first": 1, "park": 2, "reject": 3}


def tier(r):
    return VERDICT_RANK.get(r.get("verdict"), 2)


def opened(r):
    return bool((r.get("verified") or {}).get("opened_data_files"))


alive = sorted([r for r in rows if not killers(r)], key=lambda r: (tier(r), -roi(r)))
dead = [r for r in rows if killers(r)]
unverified = [r for r in alive if not opened(r)]

o = []
o.append("# Best projects\n")
o.append(f"*{len(rows)} papers evaluated - {len(alive)} passed the killer checks - {len(dead)} rejected*\n")
o.append("Grouped by verdict, then by ROI. Each score is normalised to its own maximum before combining,\n"
         "so ROI = mean(worth asking, backbone fit, integration, would anyone care) / effort. Range 0-10.\n")

if unverified:
    o.append(f"> **WARNING: {len(unverified)} of {len(alive)} candidates below were scored from the paper text "
             "only - nobody opened their data files.** Those scores are unreliable; verify before investing. "
             "Candidates marked YES had their actual data inspected.\n")

o.append("## Shortlist\n")
o.append("| # | Verdict | ROI | Data opened | Project | Effort | First thing to check |")
o.append("|---|---|---|---|---|---|---|")
for i, r in enumerate(alive[:30], 1):
    o.append("| {} | **{}** | {} | {} | {} | {} | {} |".format(
        i, (r.get("verdict") or "?").replace("_", " ").upper(), roi(r),
        "YES" if opened(r) else "no",
        (r.get("title") or "?")[:48],
        EFFORT_LABEL[effort(r)],
        ((r.get("effort") or {}).get("cheapest_killing_check") or "-")[:42]))

o.append("\n---\n\n## Details\n")
for i, r in enumerate(alive[:10], 1):
    s = r.get("scores") or {}
    b = r.get("backbone") or {}
    ig = r.get("integration") or {}
    c = r.get("controls") or {}
    v = r.get("verified") or {}
    o.append(f"### {i}. {r.get('title')}\n")
    o.append(f"**ROI {roi(r)}** - effort: {EFFORT_LABEL[effort(r)]} - "
             f"verdict: **{(r.get('verdict') or '?').replace('_',' ')}** - {r.get('verdict_reason')}\n")
    o.append(f"- **Paper:** {r.get('journal') or '?'} - {r.get('doi') or 'no DOI'}")
    o.append(f"- **Data:** {r.get('data_url') or 'not found'}")
    o.append(f"- **Rows:** {((r.get('part_a_killers') or {}).get('a5_enough_rows') or {}).get('n_rows') or 'unknown'}")
    o.append(f"- **The idea:** {r.get('ml_idea')}")
    o.append(f"- **What it could honestly claim:** {r.get('honest_claim_h3')}")
    o.append(f"- **Backbone:** {b.get('name') or '-'} ({b.get('hardware') or '?'}, {b.get('licence') or '?'})")
    if ig.get("bigger_question_one_sentence"):
        o.append(f"- **Integration:** join {ig.get('partner_dataset')} on `{ig.get('join_key')}` -> {ig.get('bigger_question_one_sentence')}")
    models = (r.get("c2_audit") or {}).get("existing_models") or []
    if models:
        o.append("- **Already done by others:**")
        for m in models:
            o.append(f"    - {m.get('name')} - {m.get('score_and_split')}. "
                     f"Authors say unsolved: *{m.get('authors_named_unsolved')}*"
                     f"{' - runnable as our baseline' if m.get('runnable_as_baseline') else ''}")
    else:
        o.append("- **Already done by others:** nothing found (weak evidence - likely novel at best)")
    o.append(f"- **Controls:** positive: {c.get('positive_control') or '?'} / negative: {c.get('negative_control') or '?'} / "
             f"external: {c.get('external_validation') or 'none found'}")
    o.append("- **Scores:** " + " ".join(
        f"{k[0].upper()}{s.get(k) if s.get(k) is not None else '?'}"
        for k in ("b_model_readiness", "c_worth_asking", "d_backbone_fit",
                  "g_integration", "h_missing_data", "i_does_anyone_care", "j_fit_finishability")))
    o.append(f"- **Verified:** {'opened the data files - ' + (v.get('what_i_actually_checked') or '') if opened(r) else 'WARNING: paper text only; data never opened'}")
    o.append(f"- **START BY:** {(r.get('effort') or {}).get('cheapest_killing_check') or '-'}\n")

o.append("---\n\n## Rejected\n")
o.append("| Project | Failed on |")
o.append("|---|---|")
for r in dead:
    o.append(f"| {(r.get('title') or '?')[:60]} | {', '.join(killers(r))} |")

if broken:
    o.append("\n## Unparseable evaluation files\n")
    o += [f"- {p}: {e}" for p, e in broken]

Path("PROJECTS.md").write_text("\n".join(o) + "\n")

with open("projects.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["roi", "data_opened", "title", "journal", "doi", "data_url", "n_rows", "effort",
                "verdict", "ml_idea", "backbone", "first_check",
                "B", "C", "D", "G", "H", "I", "J", "failed_killers"])
    for r in alive + dead:
        s = r.get("scores") or {}
        w.writerow([roi(r), opened(r), r.get("title"), r.get("journal"), r.get("doi"),
                    r.get("data_url"),
                    ((r.get("part_a_killers") or {}).get("a5_enough_rows") or {}).get("n_rows"),
                    EFFORT_LABEL[effort(r)], r.get("verdict"), r.get("ml_idea"),
                    (r.get("backbone") or {}).get("name"),
                    (r.get("effort") or {}).get("cheapest_killing_check")]
                   + [s.get(k) for k in ("b_model_readiness", "c_worth_asking", "d_backbone_fit",
                                         "g_integration", "h_missing_data", "i_does_anyone_care",
                                         "j_fit_finishability")]
                   + ["; ".join(killers(r))])

print(f"Wrote PROJECTS.md and projects.csv - {len(alive)} candidates, {len(dead)} rejected, "
      f"{len(unverified)} unverified.")
