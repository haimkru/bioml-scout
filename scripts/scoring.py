"""Scoring shared by rank.py and dashboard.py. The evaluator extracts facts and scores each
checklist item with a reason; this module computes every total, flag and ROI from them.

ROI = impact x field / effort, range 0-10.
  impact  10 x mean(C/10, D/6, G/8, I/8), the parts in CHECKLIST.md's ROI line.
  field   how crowded the task already is, from C2: 2 (little or no prior work) -> 1.0,
          1 (models exist, but closed / weak splits / small data: an opening) -> 0.8,
          0 (open, reproducible models with honest evaluation: served) -> 0.5.
  effort  1-5 from hours to a feature/label table, then +1 for each CLAUDE.md effort-up rule:
          working set over 1 GB or a GPU needed; laptop strain (peak RAM over 12 GB of the
          16 GB, or one-time embedding over 8 CPU-hours); model-readiness B under 8/12;
          data files never opened, so the counts are unverified. Capped at 5.
B, H, J and E don't enter ROI (CHECKLIST.md keeps them as pass marks); misses are flagged.
"""
from __future__ import annotations

import re

EFFORT_LABEL = {1: "a day", 2: "a few days", 3: "1-2 weeks", 4: "a month", 5: "months"}
FIELD_FACTOR = {2: 1.0, 1: 0.8, 0: 0.5}
FIELD_LABEL = {2: "little or no prior work", 1: "prior models exist, but leave an opening",
               0: "already well served by open, honestly tested models"}
RAM_LIMIT_GB, EMBED_LIMIT_H = 12, 8
# Labels already measured publicly for many other items: more training data, a ready-made
# external validation set, and known answers to check against. The user asked to prioritise these.
LABEL_FACTOR = {2: 1.25, 1: 1.1, 0: 1.0}
# A crowded field (C2 < 2) is penalised, but a concrete edge earns some of that penalty back:
# each "yes" to the four crowded-field questions restores a quarter of it, each "partly" an eighth.
EDGE_KEYS = ("more_or_better_data", "confidence_score", "models_beatable", "guaranteed_improvement")
EDGE_CREDIT = {"yes": 1.0, "partly": 0.5}
LABEL_TEXT = {2: "label already known publicly for many items", 1: "label measured elsewhere, but small or hard to join",
              0: "label exists only in this dataset"}

# (part, name, items, max, pass mark, key in the old `scores` block). Item names follow CHECKLIST.md.
PARTS = [
    ("B", "model-readiness", {"B1": "Table structure", "B2": "Parses first try", "B3": "Label consistency",
                              "B4": "Censored values", "B5": "Duplicates", "B6": "Documentation"},
     12, 8, "b_model_readiness"),
    ("C", "worth asking", {"C1": "Can it produce a useless answer?", "C2": "Existing work (state of the field)",
                           "C3": "Angle", "C4": "Current method slow or expensive?", "C5": "Baseline to compare against"},
     10, 6, "c_worth_asking"),
    ("D", "backbone fit", {"D1": "Open-weight backbone matches the data", "D2": "Freeze-and-head viable",
                           "D3": "Licence"}, 6, 4, "d_backbone_fit"),
    ("G", "integration", {"G1": "Shared identifiers", "G2": "Named large partner dataset",
                          "G3": "Join is clean", "G4": "Combination answers a bigger question"},
     8, 4, "g_integration"),
    ("H", "missing data", {"H1": "Negatives present", "H2": "Coverage is broad",
                           "H3": "Can state what the model claims", "H4": "Gaps obvious to a reader"},
     8, 4, "h_missing_data"),
    ("I", "anyone cares", {"I1": "Named user", "I2": "A result changes what they do",
                           "I3": "Metric matches the decision", "I4": "ML is the right tool"},
     8, 4, "i_does_anyone_care"),
    ("J", "finishability", {"J1": "Learn the ML I want", "J2": "Can tell sensible from nonsense",
                            "J3": "Minimum shareable version", "J4": "Time budget defined",
                            "J5": "Kill date and condition"}, 10, 6, "j_fit_finishability"),
]
E_ITEMS = {"E1": "Non-random split prevents leakage", "E2": "Enough groups to hold out",
           "E3": "Meaningful dummy baseline", "E4": "Noise floor estimable", "E5": "Positive control exists",
           "E6": "Negative controls runnable", "E7": "Second dataset for external validation",
           "E8": "A published number can be reproduced"}
ROI_PARTS = ("C", "D", "G", "I")


def killers(r: dict) -> list[str]:
    a = r.get("part_a_killers") or {}
    return [k for k, v in a.items() if isinstance(v, dict) and v.get("pass") is False]


def _items(r: dict) -> dict:
    items = r.get("score_items")
    return items if isinstance(items, dict) else {}


def _num(x) -> float | None:
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def part_total(r: dict, part: str) -> tuple[float | None, bool]:
    """(total, from_items). Sums the item scores when present, else the old per-part total."""
    _, _, names, _, _, old_key = next(p for p in PARTS if p[0] == part)
    scored = [_num((_items(r).get(k) or {}).get("score")) for k in names]
    if any(s is not None for s in scored):
        return sum(s or 0 for s in scored), True
    return _num((r.get("scores") or {}).get(old_key)), False


def c2(r: dict) -> tuple[int, str]:
    """(C2 score, where it came from). Older evaluations get it from backfill_fields.py."""
    s = _num((_items(r).get("C2") or {}).get("score"))
    if s is not None:
        return int(max(0, min(2, s))), "scored"
    s = _num((r.get("c2_backfill") or {}).get("score"))
    if s is not None:
        return int(max(0, min(2, s))), "backfilled from the C2 audit"
    models = [m for m in ((r.get("c2_audit") or {}).get("existing_models") or []) if isinstance(m, dict) and m.get("name")]
    return (1, "inferred: models listed, C2 not scored") if models else (2, "inferred: no models listed")


def working_set_gb(r: dict) -> float | None:
    """Stated by the evaluator (or backfilled). Free text isn't parsed: the largest size an
    evaluation mentions is usually raw data the project doesn't need."""
    return _num((r.get("effort") or {}).get("working_set_gb"))


def needs_gpu(r: dict) -> bool | None:
    v = (r.get("effort") or {}).get("needs_gpu")
    return v if isinstance(v, bool) else None


def laptop(r: dict) -> dict:
    e = r.get("effort") or {}
    return {k: _num(e.get(k)) for k in ("peak_ram_gb", "embedding_hours", "cv_run_minutes")} | {
        "working_set_gb": working_set_gb(r), "needs_gpu": needs_gpu(r), "note": e.get("compute_note") or ""}


def effort(r: dict) -> tuple[int, int, list[str]]:
    """(effort 1-5, base from hours, reasons for each +1)."""
    hrs = _num((r.get("effort") or {}).get("hours_to_feature_table"))
    base = 3 if hrs is None else 1 if hrs <= 8 else 2 if hrs <= 32 else 3 if hrs <= 80 else 4 if hrs <= 200 else 5
    bumps = []
    gb = working_set_gb(r)
    if needs_gpu(r):
        bumps.append("baseline needs a GPU")
    elif gb is not None and gb > 1:
        bumps.append(f"working set ~{gb:g} GB > 1 GB")
    lp = laptop(r)
    strain = []
    if lp["peak_ram_gb"] is not None and lp["peak_ram_gb"] > RAM_LIMIT_GB:
        strain.append(f"peak RAM ~{lp['peak_ram_gb']:g} GB of 16")
    if lp["embedding_hours"] is not None and lp["embedding_hours"] > EMBED_LIMIT_H:
        strain.append(f"embedding ~{lp['embedding_hours']:g} CPU-hours")
    if strain:
        bumps.append("laptop strain: " + ", ".join(strain))
    b, _ = part_total(r, "B")
    if b is not None and b < 8:
        bumps.append(f"model-readiness (B) {b:g}/12, under the pass mark of 8")
    if not (r.get("verified") or {}).get("opened_data_files"):
        bumps.append("data files never opened, so counts are unverified")
    return min(5, base + len(bumps)), base, bumps


def impact(r: dict) -> float:
    total = 0.0
    for part in ROI_PARTS:
        mx = next(p[3] for p in PARTS if p[0] == part)
        v, _ = part_total(r, part)
        total += (v or 0) / mx
    return round(10 * total / len(ROI_PARTS), 1)


def label_elsewhere(r: dict) -> int | None:
    s = _num((r.get("label_elsewhere") or {}).get("score"))
    return None if s is None else int(max(0, min(2, s)))


def edge(r: dict) -> tuple[float, list[str]]:
    """(0-1 share of the crowding penalty earned back, which edges were claimed)."""
    case = r.get("crowded_field_case")
    if not isinstance(case, dict):
        return 0.0, []
    credit, claimed = 0.0, []
    for k in EDGE_KEYS:
        ans = str(((case.get(k) or {}).get("answer")) or "").strip().lower()
        if ans in EDGE_CREDIT:
            credit += EDGE_CREDIT[ans] / len(EDGE_KEYS)
            claimed.append(f"{k.replace('_', ' ')}: {ans}")
    return credit, claimed


def crowding(r: dict) -> float:
    c2_score, _ = c2(r)
    base = FIELD_FACTOR[c2_score]
    share, _ = edge(r)
    return round(base + (1 - base) * share, 3) if c2_score < 2 else base


def roi(r: dict) -> float:
    if killers(r):
        return 0.0
    c2_score, _ = c2(r)
    e, _, _ = effort(r)
    lab = label_elsewhere(r)
    return round(impact(r) * crowding(r) * LABEL_FACTOR.get(lab, 1.0) / e, 1)


def pass_mark_misses(r: dict) -> list[str]:
    misses = []
    for part, name, _, mx, need, _ in PARTS:
        v, _ = part_total(r, part)
        if v is None:
            misses.append(f"{part} ({name}) not scored")
        elif v < need:
            misses.append(f"{part} ({name}) {v:g}/{mx}, pass mark {need}")
    e = {k: (_items(r).get(k) or {}).get("ok") for k in E_ITEMS}
    if all(v is None for v in e.values()):
        misses.append("E (evaluation checks) not recorded")
    else:
        missing = [f"{k} ({E_ITEMS[k].lower()})" for k in ("E1", "E3", "E6") if e[k] is not True]
        if e["E5"] is not True and e["E8"] is not True:
            missing.append("E5 or E8 (a positive control, or a published number to reproduce)")
        if missing:
            misses.append("E (evaluation) lacks " + "; ".join(missing))
    return misses


def breakdown(r: dict) -> dict:
    """Everything needed to explain a score: parts with item reasons, and the ROI arithmetic."""
    parts = []
    for part, name, names, mx, need, _ in PARTS:
        total, from_items = part_total(r, part)
        items = [{"id": k, "name": n, "score": _num((_items(r).get(k) or {}).get("score")),
                  "why": (_items(r).get(k) or {}).get("why") or ""} for k, n in names.items()]
        parts.append({"part": part, "name": name, "total": total, "max": mx, "need": need,
                      "in_roi": part in ROI_PARTS, "from_items": from_items, "items": items})
    e_items = [{"id": k, "name": n, "ok": (_items(r).get(k) or {}).get("ok"),
                "why": (_items(r).get(k) or {}).get("why") or ""} for k, n in E_ITEMS.items()]
    c2_score, c2_src = c2(r)
    e, base, bumps = effort(r)
    lp = laptop(r)
    unknown = [n for n, v in (("working set", lp["working_set_gb"]), ("GPU need", lp["needs_gpu"]),
                              ("peak RAM", lp["peak_ram_gb"]), ("embedding time", lp["embedding_hours"])) if v is None]
    lab = label_elsewhere(r)
    share, claimed = edge(r)
    return {"crowding": crowding(r), "edge_share": round(share, 2), "edge_claimed": claimed,
            "crowded_case": r.get("crowded_field_case") or {}, "label": lab, "label_factor": LABEL_FACTOR.get(lab, 1.0),
            "label_text": LABEL_TEXT.get(lab, "not recorded"), "label_info": r.get("label_elsewhere") or {},
            "effort_unknown": unknown, "laptop": lp,"parts": parts, "e_items": e_items, "impact": impact(r), "c2": c2_score, "c2_source": c2_src,
            "field": FIELD_FACTOR[c2_score], "field_label": FIELD_LABEL[c2_score], "effort": e,
            "effort_base": base, "effort_bumps": bumps, "effort_label": EFFORT_LABEL[e], "roi": roi(r),
            "misses": pass_mark_misses(r), "killers": killers(r)}
