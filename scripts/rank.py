#!/usr/bin/env python3
"""Aggregate evaluations/*.json into PROJECTS.md — one file, best projects first.

Also writes projects.csv so you can sort it in a spreadsheet.
No LLM here: scores come from the fixed rubric so they're comparable across papers.
"""
import json, glob, csv, sys
from pathlib import Path

# ROI, effort, totals and pass marks are computed in scoring.py, shared with dashboard.py.
import scoring
from glossary import gloss
from scoring import EFFORT_LABEL, roi


# The fields of one paper are read together here, so each acronym is explained once per paper.
# The plain-language fields come first, so they carry the definitions.
FIRST = ("pitch", "plain_idea", "verdict_reason", "ml_idea", "honest_claim_h3")


def glossed(value, used):
    """Explain known acronyms in every string in an evaluation, however deeply nested."""
    if isinstance(value, str):
        return gloss(value, used)
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: (FIRST.index(k) if k in FIRST else len(FIRST), k))
        return {k: glossed(value[k], used) for k in keys}
    if isinstance(value, list):
        return [glossed(v, used) for v in value]
    return value


rows, broken = [], []
for p in sorted(glob.glob("evaluations/*.json")):
    try:
        rows.append(glossed(json.loads(Path(p).read_text()), set()))
    except (json.JSONDecodeError, OSError) as e:   # a real parse problem, not a bug in this script
        broken.append((p, str(e)))

if not rows:
    sys.exit("No evaluations found. Run scripts/run_queue.sh first.")


def killers(r):
    return [k.split("_", 1)[1] for k in scoring.killers(r)]


def effort(r):
    return scoring.effort(r)[0]


def totals_line(r):
    out = []
    for part, _, _, mx, need, _ in scoring.PARTS:
        v, _ = scoring.part_total(r, part)
        out.append(f"{part} {'?' if v is None else f'{v:g}'}/{mx}{'' if v is None or v >= need else ' (below pass mark)'}")
    return ", ".join(out)


def roi_line(r):
    b = scoring.breakdown(r)
    bumps = f" ({b['effort_base']} from the hours estimate, +1 each for: {'; '.join(b['effort_bumps'])})" if b["effort_bumps"] else ""
    lab = f" x known labels {b['label_factor']} ({b['label_text']})" if b["label"] is not None else ""
    edge = f", {round(b['edge_share'] * 100)}% earned back by our edge" if b["edge_share"] else ""
    return (f"payoff {b['impact']} x crowding {b['crowding']} ({b['field_label']}{edge}){lab} / effort {b['effort']} of 5{bumps} = {b['roi']}"
            + (f". Not recorded, so no penalty applied: {', '.join(b['effort_unknown'])}" if b["effort_unknown"] else ""))


# Verdict decides the tier; ROI only orders within a tier. A cheap project you've
# decided to park should never sit above one you've decided to do.
VERDICT_RANK = {"do_it": 0, "verify_first": 1, "park": 2, "reject": 3}


def tier(r):
    return VERDICT_RANK.get(r.get("verdict"), 2)


def pitch(r):
    p = r.get("pitch")
    return p if isinstance(p, dict) else {}


def plain(r):
    """The plain-language idea block; {} for evaluations written before it existed."""
    p = r.get("plain_idea")
    return p if isinstance(p, dict) else {}


def opened(r):
    return bool((r.get("verified") or {}).get("opened_data_files"))


alive = sorted([r for r in rows if not killers(r)], key=lambda r: (tier(r), -roi(r)))
dead = [r for r in rows if killers(r)]
unverified = [r for r in alive if not opened(r)]

o = []
o.append("# Best projects\n")
o.append(f"*{len(rows)} papers evaluated - {len(alive)} passed the killer checks - {len(dead)} rejected*\n")
o.append("Grouped by verdict, then by ROI (return on investment), range 0-10. ROI = payoff x crowding / effort.\n"
         "Payoff averages checklist parts C (worth asking), D (a pretrained model fits), G (joins to big datasets) and\n"
         "I (anyone cares). Crowding shrinks it when others have already modelled the task well. Effort (1-5) rises for\n"
         "data over 1 GB, needing a GPU (graphics card), straining a 16 GB laptop, messy data, or data nobody opened.\n"
         "See scripts/scoring.py.\n")

if unverified:
    o.append(f"> **WARNING: {len(unverified)} of {len(alive)} candidates below were scored from the paper text "
             "only - nobody opened their data files.** Those scores are unreliable; verify before investing. "
             "Candidates marked YES had their actual data inspected.\n")

o.append("## Shortlist\n")
o.append("| # | Verdict | ROI | Data opened | Project | The idea, in plain words | Effort | First thing to check |")
o.append("|---|---|---|---|---|---|---|---|")
for i, r in enumerate(alive[:30], 1):
    o.append("| {} | **{}** | {} | {} | {} | {} | {} | {} |".format(
        i, (r.get("verdict") or "?").replace("_", " ").upper(), roi(r),
        "YES" if opened(r) else "no",
        (r.get("title") or "?")[:48],
        (plain(r).get("one_liner") or "-").replace("|", "/")[:110],
        EFFORT_LABEL[effort(r)],
        ((r.get("effort") or {}).get("cheapest_killing_check") or "-")[:42]))

o.append("\n---\n\n## Details\n")
for i, r in enumerate(alive[:10], 1):
    b = r.get("backbone") or {}
    ig = r.get("integration") or {}
    c = r.get("controls") or {}
    v = r.get("verified") or {}
    o.append(f"### {i}. {r.get('title')}\n")
    o.append(f"**ROI {roi(r)}** - effort: {EFFORT_LABEL[effort(r)]} - "
             f"verdict: **{(r.get('verdict') or '?').replace('_',' ')}** - {r.get('verdict_reason')}\n")
    o.append(f"- **Why this ROI:** {roi_line(r)}")
    misses = scoring.pass_mark_misses(r)
    o.append(f"- **Pass marks missed:** {'; '.join(misses) if misses else 'none'}")
    o.append(f"- **Paper:** {r.get('journal') or '?'} - {r.get('doi') or 'no DOI'}")
    o.append(f"- **Data:** {r.get('data_url') or 'not found'}")
    o.append(f"- **Rows:** {((r.get('part_a_killers') or {}).get('a5_enough_rows') or {}).get('n_rows') or 'unknown'}")
    cc = r.get("crowded_field_case")
    if isinstance(cc, dict) and cc:
        o.append("- **Other models exist — our edge:**")
        for key, q in [("more_or_better_data", "Much more data, or better for a specific area"),
                       ("confidence_score", "A confidence score the others don't give"),
                       ("models_beatable", "Existing models old or weak enough to beat"),
                       ("guaranteed_improvement", "Better performance near-certain from joinable new data")]:
            a_ = cc.get(key) or {}
            o.append(f"    - **{q}:** {str(a_.get('answer') or '?').upper()} — {a_.get('why') or ''}")
    pch = pitch(r)
    if pch.get("headline"):
        o.append(f"- **The case for doing it:** {pch['headline']}")
        for label, key in [("What this data has that others don't", "what_makes_the_data_unusual"),
                           ("The result that would make it worth it", "the_win"),
                           ("What we learn even if it fails", "even_if_it_fails"),
                           ("Who would read it", "who_wants_it")]:
            if pch.get(key):
                o.append(f"    - **{label}:** {pch[key]}")
    pi = plain(r)
    if pi.get("one_liner"):
        o.append(f"- **In plain words:** {pi.get('one_liner')}")
        o.append(f"    - **Train on:** {pi.get('train_on') or '?'}")
        o.append(f"    - **To predict:** {pi.get('predict') or '?'}")
        o.append(f"    - **Why it's worth doing:** {pi.get('why_worthwhile') or '?'}")
    else:
        o.append("- **In plain words:** not recorded (evaluated before the field existed; run scripts/backfill_fields.py)")
    o.append(f"- **The idea (technical):** {r.get('ml_idea')}")
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
    o.append(f"- **Scores:** {totals_line(r)}")
    o.append(f"- **Verified:** {'opened the data files - ' + (v.get('what_i_actually_checked') or '') if opened(r) else 'WARNING: paper text only; data never opened'}")
    sc_ = r.get("success_criteria")
    if isinstance(sc_, dict) and sc_:
        o.append(f"- **What counts as success:** {sc_.get('success') or '?'} (metric: {sc_.get('metric') or '?'}; "
                 f"baselines: {sc_.get('baselines') or '?'}). Stop if: {sc_.get('kill') or '?'}")
    lk = r.get("leakage_plan")
    if isinstance(lk, dict) and lk:
        o.append(f"- **Preventing data leakage:** {lk.get('verdict') or ''} Split: {lk.get('split') or '?'}")
        for x in (lk.get("risks") or [])[:4]:
            o.append(f"    - risk: {x}")
    pt = r.get("publication_target")
    if isinstance(pt, dict) and pt:
        o.append("- **Where to publish it:** " + "; ".join(
            f"{k} — {(pt.get(k) or {}).get('journal')}" for k in ("ambitious", "realistic", "fallback")
            if (pt.get(k) or {}).get("journal")))
        rv = (pt.get("realistic") or {}).get("why")
        if rv:
            o.append(f"    - **Why the realistic one:** {rv}")
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
    w.writerow(["roi", "data_opened", "title", "pitch_headline", "the_win", "even_if_it_fails",
                "idea_plain", "train_on", "predict", "why_worthwhile",
                "journal", "doi", "data_url", "n_rows", "effort",
                "verdict", "ml_idea", "backbone", "first_check",
                "B", "C", "D", "G", "H", "I", "J", "failed_killers"])
    for r in alive + dead:
        pi = plain(r)
        pch = pitch(r)
        w.writerow([roi(r), opened(r), r.get("title"), pch.get("headline"), pch.get("the_win"),
                    pch.get("even_if_it_fails"), pi.get("one_liner"), pi.get("train_on"),
                    pi.get("predict"), pi.get("why_worthwhile"), r.get("journal"), r.get("doi"),
                    r.get("data_url"),
                    ((r.get("part_a_killers") or {}).get("a5_enough_rows") or {}).get("n_rows"),
                    EFFORT_LABEL[effort(r)], r.get("verdict"), r.get("ml_idea"),
                    (r.get("backbone") or {}).get("name"),
                    (r.get("effort") or {}).get("cheapest_killing_check")]
                   + [scoring.part_total(r, part)[0] for part, *_ in scoring.PARTS]
                   + ["; ".join(killers(r))])

print(f"Wrote PROJECTS.md and projects.csv - {len(alive)} candidates, {len(dead)} rejected, "
      f"{len(unverified)} unverified.")
