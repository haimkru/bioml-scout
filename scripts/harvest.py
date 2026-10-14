#!/usr/bin/env python3
"""Build queue.csv: recent papers from the journals in CLAUDE.md, via Europe PMC.

Stdlib only. Every API response is cached under data/cache/europepmc/, so a
re-run is free and reproducible; pass --refresh to bypass the cache.

Usage:
    uv run --no-project --python 3.11 scripts/harvest.py --start 2026-07-01 --end 2026-10-01
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "cache" / "europepmc"
RAW_PATH = ROOT / "data" / "harvest_raw.jsonl"
QUEUE_PATH = ROOT / "queue.csv"
DROPPED_PATH = ROOT / "queue_dropped.csv"
RUN_LOG = ROOT / "data" / "runs.log"

EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PAGE_SIZE = 1000
MIN_INTERVAL_S = 0.5  # Europe PMC publishes no hard limit; stay well under any plausible one
QUEUE_COLUMNS = ["paper_id", "title", "journal", "doi", "pmcid", "pub_date",
                 "is_open_access", "data_url", "status", "priority"]

# Slug prefix per journal (paper_id = <abbr><year>-<doi tail>).
ABBR = {
    "Nucleic Acids Research": "nar",
    "Genome Biology": "gb",
    "Cell Genomics": "cellgen",
    "Nature Genetics": "natgen",
    "Nature": "nature",
    "Cell": "cell",
    "Science": "science",
    "American Journal of Human Genetics (AJHG)": "ajhg",
}
# Multidisciplinary journals whose non-biology papers must be dropped.
MULTIDISCIPLINARY = {"Nature", "Science", "Cell"}
# Journals with magazine-style front matter: here a record with no abstract is an essay,
# book review, News & Views or research highlight, never a research paper.
NO_ABSTRACT_MEANS_FRONT_MATTER = {"Nature", "Science", "Cell", "Nature Genetics"}

# --- article types ---------------------------------------------------------
# Not papers: no data to evaluate. Matched case-insensitively against pubTypeList.
NON_RESEARCH_TYPES = {
    "published erratum", "correction", "erratum", "retraction of publication",
    "retraction", "expression of concern", "editorial", "news", "comment",
    "letter", "biography", "obituary", "interview", "portrait", "retraction notice",
    "expression-of-concern", "introductory journal article", "addendum", "book review", "news & views",
    "news-and-views", "correspondence", "reply", "editor's choice", "in brief",
    "research highlight",
}

# Recurring Science/Nature front-matter columns that carry no type tag or abstract of their own.
FRONT_MATTER_TITLE_RE = re.compile(
    r"^(?:in other journals|in science journals|new products|this week in science|editors' choice)\.?$", re.I)

# --- dataset-paper detection (title + abstract) ------------------------------
DATASET_PATTERNS = {
    "database": r"\bdatabases?\b",
    "atlas": r"\batlas(?:es)?\b",
    "resource": r"\bresources?\b",
    "catalog": r"\bcatalog(?:ue)?s?\b",
    "compendium": r"\bcompendi(?:um|a)\b",
    "repository": r"\brepositor(?:y|ies)\b",
    "comprehensive collection": r"\ba comprehensive collection of\b",
}

# --- biology filter (multidisciplinary journals only) ------------------------
_NOT_BIO_CELL = "".join(f"(?<!{w} )" for w in
                        ("solar", "fuel", "unit", "memory", "battery", "coin", "pouch",
                         "tandem", "perovskite", "photovoltaic", "electrochemical", "cooling"))
_NOT_BIO_PLANT = "".join(f"(?<!{w} )" for w in
                         ("power", "nuclear", "chemical", "industrial", "treatment", "pilot",
                          "coal", "gas", "manufacturing", "processing", "desalination"))
# Terms that rarely appear outside biology. A Nature/Science/Cell paper needs at least one to stay.
# Deliberately excluded because they fire on physics/chemistry/social science: cell(s) (solar,
# tandem, electrochemical), behaviour, human(s) (and MeSH "Humans"), drug, biological, immune
# ("immune to backscattering"), synaptic (neuromorphic), heart ("at the heart of"), species.
BIO_TERMS = [
    r"\bsingle-cell\b", r"\bgen(?:e|es|etic|etics|omic|omics|ome|omes)\b", r"\bprotein", r"\bpeptide",
    r"\b(?:dna|rna|mrna|crispr)\b", r"\benzym", r"\bbiocatal", r"\bneurons?\b", r"\bneural circuit",
    r"\bbrain(?!-inspired)", r"\bmice\b", r"\bmouse\b", r"\brats?\b", r"\bzebrafish\b", r"\bdrosophila\b",
    r"\bpatients?\b", r"\bclinical\b", r"\bdiseases?\b", r"\bcancer", r"\btumou?r", r"\bmalaria",
    r"\bbacteri", r"\bmicrob(?!ubble|ead|all|eam)", r"\bvir(?:us|uses|al)\b", r"\bpathogen", r"\binfect",
    r"\btissues?\b", r"\borganisms?\b", r"\bmetabolis", r"(?<!artificial )\bphotosynth",
    _NOT_BIO_PLANT + r"\bplants?\b(?! (?:operator|efficiency|capacity))", r"\bcrops?\b",
    r"\b(?:tropical|boreal|temperate|rain) ?forests?\b", r"\bvegetation\b", r"\becosystems?\b",
    r"\bspecies richness\b", r"\bbiodiversity\b", r"\bfossil(?!\s+fuel)", r"\bhominin", r"\bdinosaur",
    r"\bmammal", r"\bprimate", r"\bvertebrate", r"\binsect", r"\bbirds?\b", r"\bfish(?:es)?\b",
    r"\banimals?\b", r"\bancient dna\b", r"\bmuscle", r"\bliver\b", r"\bkidney", r"\bstem cell",
    r"\bembryo", r"\bhormon", r"\bantibod", r"\bvaccin", r"\bmitochondri", r"\bchromatin",
    r"\bmicrobiome\b", r"\bimmune (?:cell|system|response)", r"\bT cells?\b", r"\bconifer",
    r"(?<!decision )(?<!random )(?<!search )\btrees?\b", r"\bforaminifer", r"\bplankt", r"\balgae\b",
    r"\bcorals?\b", r"\bdrug discovery\b", r"\bbiomedical\b", r"\bneuroprosthe", r"\bneuromodulation",
    r"\bpsychotherap", r"\bspinal cord\b",
]
NON_BIO_TERMS = [
    r"\bquantum\b", r"\bqubits?\b", r"\bphoton", r"\blasers?\b", r"\bsuperconduct", r"\bsemiconduct",
    r"\btopolog", r"\bmagnon", r"\bphonon", r"\bexciton", r"\bplasmon", r"\bgraphene\b", r"\bperovskite",
    r"\bgalax(?:y|ies)\b", r"\bexoplanet", r"\bstellar\b", r"\bblack holes?\b", r"\bneutron stars?\b",
    r"\bsupernova", r"\bcosmolog", r"\bdark matter\b", r"\bgravitational wave", r"\btelescope",
    r"\basteroid", r"\bcomet", r"\bplanet", r"\blunar\b", r"\bmoon\b", r"\bmars\b", r"\bjupiter\b",
    r"\bsolar wind\b", r"\bmagnetospher", r"\batmospher", r"\bclimate\b", r"\bsea level", r"\bice sheet",
    r"\bglacier", r"\bearthquake", r"\bseismic", r"\bvolcan", r"\bmantle\b", r"\btecton", r"\bmagma",
    r"\bbatter(?:y|ies)\b", r"\belectrolyte", r"\bcathode", r"\banode", r"\bcatalys", r"\bcatalyt",
    r"\bpolymer", r"\bmetal-organic\b", r"\bcrystal", r"\blattice\b", r"\balloy", r"\btransistor",
    r"\bspintronic", r"\bferroelectric", r"\bmagnetic order", r"\bfusion plasma\b", r"\bplasma physics\b",
    r"\batoms?\b", r"\bmolecules? (?:on|adsorbed)", r"\boptical\b", r"\bnanophotonic", r"\bmetasurface",
    r"\bthin films?\b", r"\bsolar cells?\b", r"\bphotovoltaic", r"\bhydrogen production\b", r"\bco2 reduction\b",
    r"\bearth's\b", r"\bpaleoclimate\b", r"\bmonsoon", r"\bprecipitation\b", r"\baerosol", r"\bozone\b",
    r"\beconom", r"\bmachine learning\b(?= model for materials)", r"\bmaterials?\b",
]
BIO_RE = [re.compile(p, re.I) for p in BIO_TERMS]
NON_BIO_RE = [re.compile(p, re.I) for p in NON_BIO_TERMS]
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Journal:
    name: str
    issns: list[str]
    oa_note: str


# --------------------------------------------------------------------------- io
def log(msg: str) -> None:
    print(msg, flush=True)
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} harvest: {msg}\n")


def read_journals(claude_md: Path) -> list[Journal]:
    """Parse the '| Journal | ISSN (print) | ISSN (online) | Open access? |' table."""
    journals: list[Journal] = []
    in_table = False
    for line in claude_md.read_text().splitlines():
        if line.startswith("| Journal |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set(cells[0]) <= {"-", ":"}:
                continue
            issns = [c for c in cells[1:3] if re.fullmatch(r"\d{4}-\d{3}[\dXx]", c)]
            journals.append(Journal(cells[0], issns, cells[3] if len(cells) > 3 else ""))
    if not journals:
        sys.exit(f"No journal table found in {claude_md}")
    return journals


_last_request = 0.0


def get_json(params: dict[str, str], refresh: bool) -> dict:
    """GET Europe PMC search with caching, rate limiting and exponential backoff."""
    global _last_request
    url = f"{EPMC_SEARCH}?{urllib.parse.urlencode(params)}"
    cache_file = CACHE_DIR / f"{hashlib.sha1(url.encode()).hexdigest()}.json"
    if cache_file.exists() and not refresh:
        return json.loads(cache_file.read_text())

    for attempt in range(5):
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "bioml-scout/0.1 (harvest)"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
            data = json.loads(body)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(body)
            return data
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            err = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            err = repr(e)
        backoff = 2 ** attempt
        log(f"  retry {attempt + 1}/5 after {err}; sleeping {backoff}s")
        time.sleep(backoff)
    raise RuntimeError(f"Europe PMC request failed after retries: {url}")


def search_issn(issn: str, start: str, end: str, refresh: bool) -> tuple[int, list[dict]]:
    """All records for one ISSN in the date window, following cursorMark pages."""
    query = f'ISSN:"{issn}" AND FIRST_PDATE:[{start} TO {end}]'
    cursor, results, hit_count = "*", [], 0
    while True:
        data = get_json({"query": query, "format": "json", "resultType": "core",
                         "pageSize": str(PAGE_SIZE), "cursorMark": cursor}, refresh)
        hit_count = int(data.get("hitCount", 0))
        page = data.get("resultList", {}).get("result", [])
        results.extend(page)
        nxt = data.get("nextCursorMark")
        if not page or not nxt or nxt == cursor or len(results) >= hit_count:
            break
        cursor = nxt
    return hit_count, results


# ------------------------------------------------------------------ normalise
def clean(text: str | None) -> str:
    """Strip tags/whitespace and characters that would break run_queue.sh's IFS=, parsing."""
    if not text:
        return ""
    text = html.unescape(TAG_RE.sub("", html.unescape(text)))  # EPMC titles carry escaped <sup>/<i>
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)  # 11,000 -> 11000
    return text.replace(",", ";").replace('"', "'")


def pub_types(rec: dict) -> list[str]:
    return [t.lower() for t in (rec.get("pubTypeList") or {}).get("pubType", [])]


def plain(text: str | None) -> str:
    return html.unescape(TAG_RE.sub(" ", html.unescape(text or "")))


def text_blob(rec: dict) -> str:
    kws = (rec.get("keywordList") or {}).get("keyword", [])
    mesh = [m.get("descriptorName", "") for m in (rec.get("meshHeadingList") or {}).get("meshHeading", [])]
    return " ".join([plain(rec.get("title")), plain(rec.get("abstractText")),
                     " ".join(kws), " ".join(mesh)])


def dedupe_key(rec: dict) -> str:
    if rec.get("doi"):
        return "doi:" + rec["doi"].lower()
    if rec.get("pmid"):
        return "pmid:" + rec["pmid"]
    return f"{rec.get('source')}:{rec.get('id')}"


def slug(abbr: str, rec: dict) -> str:
    """Stable id from the DOI (pages change when online-first papers get an issue)."""
    year = (rec.get("firstPublicationDate") or rec.get("pubYear") or "0000")[:4]
    doi = (rec.get("doi") or "").lower()
    if doi:
        tail = doi.split("/", 1)[-1]
        cell_press = re.match(r"^j\.[a-z]+\.(\d{4})\.(.+)$", tail)  # j.ajhg.2026.06.002
        if cell_press:  # the DOI carries its own year; use it so the id never repeats or drifts
            year, tail = cell_press.groups()
        tail = re.sub(r"^(?:nar/|science\.|s4158[68]-0\d\d-|s13059-0\d\d-)", "", tail)
    else:
        tail = rec.get("pmid") or rec.get("pmcid") or rec.get("id") or "unknown"
    tail = re.sub(r"[^a-z0-9]+", "-", tail.lower()).strip("-")
    return f"{abbr}{year}-{tail}"


def is_database_issue(journal: str, rec: dict) -> bool:
    if journal != "Nucleic Acids Research":
        return False
    issue = ((rec.get("journalInfo") or {}).get("issue") or "").upper()
    pages = (rec.get("pageInfo") or "").upper()
    return issue.startswith("D") or bool(re.match(r"^D\d", pages))


def dataset_matches(rec: dict) -> tuple[list[str], list[str]]:
    title, abstract = plain(rec.get("title")), plain(rec.get("abstractText"))
    in_title = [k for k, p in DATASET_PATTERNS.items() if re.search(p, title, re.I)]
    in_abs = [k for k, p in DATASET_PATTERNS.items() if re.search(p, abstract, re.I)]
    return in_title, in_abs


def biology_counts(rec: dict) -> tuple[int, int]:
    """(biology terms, non-biology terms) matched in title + abstract + keywords + MeSH."""
    blob = text_blob(rec)
    return sum(1 for r in BIO_RE if r.search(blob)), sum(1 for r in NON_BIO_RE if r.search(blob))


def first_url(rec: dict) -> str:
    m = URL_RE.search(plain(rec.get("abstractText")))
    return clean(m.group(0).rstrip(".;:,")) if m else ""


# ----------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    today = date.today()
    ap.add_argument("--start", default=(today - timedelta(days=91)).isoformat())
    ap.add_argument("--end", default=today.isoformat())
    ap.add_argument("--refresh", action="store_true", help="bypass the response cache")
    args = ap.parse_args()

    journals = read_journals(ROOT / "CLAUDE.md")
    log(f"window {args.start} to {args.end}; {len(journals)} journals from CLAUDE.md")

    issn_hits: dict[str, int] = {}
    merged: dict[str, tuple[Journal, dict]] = {}
    found_per_journal: Counter[str] = Counter()
    for j in journals:
        keys: set[str] = set()
        for issn in j.issns:
            hits, recs = search_issn(issn, args.start, args.end, args.refresh)
            issn_hits[f"{j.name} {issn}"] = hits
            log(f"  {j.name:<45} ISSN {issn}: {hits} hits, {len(recs)} retrieved")
            for rec in recs:
                k = dedupe_key(rec)
                keys.add(k)
                merged.setdefault(k, (j, rec))
        found_per_journal[j.name] = len(keys)

    RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RAW_PATH.open("w") as fh:
        for j, rec in merged.values():
            fh.write(json.dumps({"journal": j.name, "record": rec}) + "\n")

    queue, dropped = [], []
    stats: Counter[str] = Counter()
    pattern_hits: Counter[str] = Counter()
    seen_ids: set[str] = set()
    for j, rec in merged.values():
        abbr = ABBR.get(j.name) or re.sub(r"[^a-z]", "", j.name.lower())[:8]
        pid = slug(abbr, rec)
        while pid in seen_ids:  # vanishingly rare; keep ids unique
            pid += "-x"
        seen_ids.add(pid)
        title = clean(rec.get("title"))
        types = pub_types(rec)

        reason = ""
        if FRONT_MATTER_TITLE_RE.match(title):
            reason = "not a paper: front-matter column"
        elif any(t in NON_RESEARCH_TYPES for t in types):
            reason = "not a paper: " + "/".join(t for t in types if t in NON_RESEARCH_TYPES)
        elif j.name in NO_ABSTRACT_MEANS_FRONT_MATTER and not (rec.get("abstractText") or "").strip():
            reason = "no abstract: front matter (essay; book review; News & Views; highlight)"
        elif j.name in MULTIDISCIPLINARY:
            n_bio, n_non = biology_counts(rec)
            if n_bio == 0:
                reason = f"non-biology (0 biology terms; {n_non} physics/chemistry/earth terms)"
        if reason:
            dropped.append({"paper_id": pid, "journal": j.name, "title": title,
                            "doi": rec.get("doi") or "", "pub_types": "; ".join(types), "reason": reason})
            continue

        in_title, in_abs = dataset_matches(rec)
        db_issue = is_database_issue(j.name, rec)
        is_dataset = bool(in_title or in_abs or db_issue)
        for k in set(in_title) | set(in_abs):
            pattern_hits[k] += 1
        if db_issue:
            pattern_hits["NAR Database Issue"] += 1
        if is_dataset:
            stats["dataset_title_or_dbissue" if (in_title or db_issue) else "dataset_abstract_only"] += 1

        queue.append({
            "paper_id": pid, "title": title, "journal": clean(j.name), "doi": clean(rec.get("doi")),
            "pmcid": clean(rec.get("pmcid")), "pub_date": clean(rec.get("firstPublicationDate")),
            "is_open_access": "Y" if rec.get("isOpenAccess") == "Y" else "N",
            "data_url": first_url(rec), "status": "pending", "priority": "1" if is_dataset else "2",
        })

    queue.sort(key=lambda r: (r["priority"], r["journal"], r["pub_date"], r["paper_id"]))
    for row in queue:  # run_queue.sh splits on commas: refuse to write a row that would break it
        assert all("," not in v and "\n" not in v and '"' not in v for v in row.values()), row
    with QUEUE_PATH.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=QUEUE_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(queue)
    with DROPPED_PATH.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["paper_id", "journal", "title", "doi", "pub_types", "reason"],
                           lineterminator="\n")
        w.writeheader()
        w.writerows(sorted(dropped, key=lambda r: (r["journal"], r["reason"], r["paper_id"])))

    print_summary(journals, issn_hits, found_per_journal, queue, dropped, stats, pattern_hits)


def print_summary(journals, issn_hits, found, queue, dropped, stats, pattern_hits) -> None:
    print("\n=== ISSN check (Europe PMC hitCount) ===")
    for k, v in issn_hits.items():
        print(f"  {k:<58} {v:>5}{'   <-- NO RESULTS' if v == 0 else ''}")

    drop_by = Counter((d["journal"], d["reason"].split(":")[0].split(" (")[0]) for d in dropped)
    print("\n=== Per journal ===")
    hdr = f"  {'journal':<45}{'found':>7}{'dropped':>9}{'queued':>8}{'dataset':>9}{'OA':>6}{'pmcid':>7}"
    print(hdr)
    tot = Counter()
    for j in journals:
        q = [r for r in queue if r["journal"] == clean(j.name)]
        row = {"found": found[j.name], "dropped": sum(1 for d in dropped if d["journal"] == j.name),
               "queued": len(q), "dataset": sum(r["priority"] == "1" for r in q),
               "OA": sum(r["is_open_access"] == "Y" for r in q), "pmcid": sum(bool(r["pmcid"]) for r in q)}
        tot.update(row)
        print(f"  {j.name:<45}" + "".join(f"{row[c]:>{w}}" for c, w in
                                          [("found", 7), ("dropped", 9), ("queued", 8), ("dataset", 9), ("OA", 6), ("pmcid", 7)]))
    print(f"  {'TOTAL':<45}" + "".join(f"{tot[c]:>{w}}" for c, w in
                                       [("found", 7), ("dropped", 9), ("queued", 8), ("dataset", 9), ("OA", 6), ("pmcid", 7)]))

    print("\n=== Dropped, by reason ===")
    for (jn, why), n in sorted(drop_by.items()):
        print(f"  {jn:<45} {why:<16} {n:>5}")

    print("\n=== Dataset papers (priority=1) ===")
    print(f"  matched in title or NAR Database Issue: {stats['dataset_title_or_dbissue']}")
    print(f"  matched in abstract only:               {stats['dataset_abstract_only']}")
    print("  papers matching each pattern: " + ", ".join(f"{k}={v}" for k, v in pattern_hits.most_common()))
    print(f"\nWrote {QUEUE_PATH.name} ({len(queue)} rows) and {DROPPED_PATH.name} ({len(dropped)} rows).")


if __name__ == "__main__":
    main()
