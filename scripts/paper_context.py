#!/usr/bin/env python3
"""Everything known about one paper before any judgement — in a single call.

    python3 scripts/paper_context.py <paper_id>            # summary + where the data is
    python3 scripts/paper_context.py <paper_id> --full     # include the whole full text
    python3 scripts/paper_context.py <paper_id> --json     # machine-readable

Why: timing 61 evaluations showed about 9 identical opening calls per paper (Europe PMC record,
full text, supplement listing, text-mined accessions, NCBI elink, Zenodo/figshare/Dryad search),
each one a separate turn whose cost is dominated by the model reading the result and deciding what
to do next. They're independent, so this runs them concurrently and returns one answer. The
Europe PMC record is already on disk from the harvest, so that part costs nothing.

Everything goes through scripts/fetch.py's cache, so re-running is free and nothing is fetched
twice. This gathers facts only; it makes no judgements and scores nothing.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import re
import sys
import urllib.parse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import fetch  # noqa: E402  the shared cache, rate limiting and size cap

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
LINK_DBS = ("gds", "sra", "bioproject", "biosample", "gap", "pmc")
# Europe PMC's supplementaryFiles endpoint averaged 71 s and twice ran past 500 s, so it gets
# one bounded attempt. --quick halves it for prewarming, where a miss costs nothing.
SUPPL_TIMEOUT = 120
# Accession-shaped strings worth pulling out of the data availability statement.
ACCESSION_RE = {
    "GEO": r"\bGSE\d{4,}\b", "SRA": r"\b(?:SRP|SRR|SRX|PRJNA|PRJEB|PRJDB)\d+\b",
    "ArrayExpress": r"\bE-[A-Z]{4}-\d+\b", "PRIDE": r"\bPXD\d{6}\b", "PDB": r"\b[0-9][A-Za-z0-9]{3}\b(?= *\(PDB)",
    "dbGaP": r"\bphs\d{6}\b", "EGA": r"\bEGA[SD]\d{11}\b", "BioStudies": r"\bS-[A-Z]{4}\d+\b",
    "Zenodo": r"zenodo\.org/record[s]?/\d+|10\.5281/zenodo\.\d+", "figshare": r"10\.6084/m9\.figshare\.\d+",
    "Dryad": r"10\.5061/dryad\.[a-z0-9]+", "CNGB": r"\bCNP\d{7}\b", "NGDC": r"\b(?:OMIX|HRA|PRJCA)\d+\b",
}
# Headings, not passing mentions: "...availability of data among antibiotics" is not the statement.
DAS_HEADINGS = (r"\b(data (and code |and materials )?availability( statement)?|code and data availability|"
                r"accession (codes|numbers)|data and software availability)\b")
METHODS_HEADINGS = r"\b(materials and methods|methods and materials|star\*?methods|experimental procedures)\b"


def get(url: str, timeout: float = 60, max_mb: float = 50) -> tuple[int, bytes, dict]:
    """Cached GET. Returns (status, body, meta); status 0 means the request failed."""
    body_path, meta_path = fetch.cache_paths(url)
    if body_path.exists() and meta_path.exists():
        return 200, body_path.read_bytes(), json.loads(meta_path.read_text())
    import subprocess
    try:
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "fetch.py"), url, "--quiet",
                              "--timeout", str(timeout), "--max-mb", str(max_mb)],
                             capture_output=True, text=False, timeout=timeout + 25)
    except subprocess.TimeoutExpired:   # the child is killed; belt and braces over its watchdog
        return 0, b"", {"error": f"fetch exceeded {timeout + 25:g}s and was killed", "url": url}
    if out.returncode != 0:
        try:
            return 0, b"", json.loads(out.stdout.decode(errors="replace") or "{}")
        except json.JSONDecodeError:
            return 0, b"", {"error": out.stdout.decode(errors="replace")[:300]}
    if body_path.exists() and meta_path.exists():
        return 200, body_path.read_bytes(), json.loads(meta_path.read_text())
    return 200, out.stdout, {}


def queue_row(paper_id: str) -> dict:
    path = ROOT / "queue.csv"
    if not path.exists():
        return {}
    with path.open() as fh:
        for row in csv.DictReader(fh):
            if row["paper_id"] == paper_id:
                return row
    return {}


def harvested(paper_id: str, doi: str) -> dict:
    """The Europe PMC record the harvest already saved — no network call."""
    path = ROOT / "data" / "harvest_raw.jsonl"
    if not path.exists():
        return {}
    want = (doi or "").lower()
    for line in path.open():
        rec = json.loads(line).get("record", {})
        if want and (rec.get("doi") or "").lower() == want:
            return rec
    return {}


def plain(xml: str) -> str:
    xml = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", xml, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml))


def sections(xml: str) -> dict[str, str]:
    """Pull the sections CLAUDE.md says to keep: data availability, methods, supplement captions."""
    out: dict[str, str] = {}
    text = plain(xml)
    # The real availability statement sits near the end, after the discussion, so take the last hit.
    hits = list(re.finditer(DAS_HEADINGS, text, re.I))
    if hits:
        out["data_availability"] = text[hits[-1].start():hits[-1].start() + 2500].strip()
    m = re.search(METHODS_HEADINGS, text, re.I)
    if m:
        out["methods"] = text[m.start():m.start() + 4000].strip()
    caps = [c.start() for c in re.finditer(r"\bsupplementary (information|material|tables?|figures?|data)\b", text, re.I)]
    if caps:
        out["supplementary_captions"] = text[caps[-1]:caps[-1] + 2000].strip()
    return out


def find_accessions(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for repo, rx in ACCESSION_RE.items():
        hits = sorted({m.group(0) for m in re.finditer(rx, text)})
        if hits:
            found[repo] = hits[:25]
    return found


# --- the independent probes, all run at once ---------------------------------
def probe_fulltext(pmcid: str) -> dict:
    if not pmcid:
        return {"available": False, "why": "no PMCID"}
    st, body, meta = get(f"{EPMC}/{pmcid}/fullTextXML", timeout=60)
    if st == 0 or not body:
        return {"available": False, "why": (meta or {}).get("error", "fullTextXML returned nothing")}
    xml = body.decode(errors="replace")
    if "<article" not in xml[:4000]:
        return {"available": False, "why": "no article element (usually not open access)"}
    return {"available": True, "chars": len(xml), "sections": sections(xml), "xml": xml}


def probe_supplement(pmcid: str) -> dict:
    """One attempt only: this endpoint averaged 71 s and twice ran past 500 s."""
    if not pmcid:
        return {"available": False, "why": "no PMCID"}
    url = f"{EPMC}/{pmcid}/supplementaryFiles"
    st, body, meta = get(url, timeout=SUPPL_TIMEOUT, max_mb=50)
    if st == 0 or not body:
        return {"available": False, "why": (meta or {}).get("skipped") or (meta or {}).get("error")
                or "not retrievable in 120 s", "url": url}
    path, _ = fetch.cache_paths(url)
    try:
        with zipfile.ZipFile(path) as z:
            files = [{"name": i.filename, "mb": round(i.file_size / 1e6, 3)} for i in z.infolist()]
    except zipfile.BadZipFile:
        return {"available": False, "why": "response is not a zip", "bytes": len(body), "url": url}
    return {"available": True, "zip_mb": round(len(body) / 1e6, 2), "files": files, "cached_zip": str(path)}


def probe_annotations(pmcid: str, pmid: str) -> dict:
    src, ident = ("PMC", pmcid) if pmcid else ("MED", pmid)
    if not ident:
        return {}
    url = ("https://www.ebi.ac.uk/europepmc/annotations_api/annotationsByArticleIds"
           f"?articleIds={src}%3A{ident.replace('PMC', '') if src == 'PMC' else ident}"
           "&type=Accession%20Numbers&format=JSON")
    st, body, _ = get(url, timeout=45)
    if st == 0 or not body:
        return {}
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {}
    out: dict[str, list[str]] = {}
    for art in data if isinstance(data, list) else []:
        for ann in art.get("annotations", []):
            tags = [t.get("name") for t in ann.get("tags", [])]
            out.setdefault(ann.get("type", "?"), [])
            for t in tags:
                if t and t not in out[ann["type"]]:
                    out[ann["type"]].append(t)
    return {k: v[:25] for k, v in out.items() if v}


def probe_elink(pmid: str) -> dict:
    """One link query per repository. NCBI's rate limiter serialises these anyway, but the
    HTTP waits overlap, which turns six 30 s worst cases into one."""
    if not pmid:
        return {}

    def one(db: str) -> tuple[str, list]:
        st, body, _ = get(f"{EUTILS}/elink.fcgi?dbfrom=pubmed&db={db}&id={pmid}&retmode=json", timeout=20)
        if st == 0 or not body:
            return db, []
        try:
            sets = json.loads(body).get("linksets", [])
        except json.JSONDecodeError:
            return db, []
        return db, [i for s in sets for d in s.get("linksetdbs", []) for i in d.get("links", [])]

    with cf.ThreadPoolExecutor(max_workers=len(LINK_DBS)) as ex:
        return {db: ids[:20] for db, ids in ex.map(one, LINK_DBS) if ids}


def probe_repo_search(doi: str, title: str) -> dict:
    out = {}
    if doi:
        for name, url in (("zenodo", f"https://zenodo.org/api/records?q=%22{urllib.parse.quote(doi)}%22&size=5"),
                          ("figshare", "https://api.figshare.com/v2/articles/search"),
                          ("dryad", f"https://datadryad.org/api/v2/search?q={urllib.parse.quote(doi)}")):
            if name == "figshare":
                continue  # POST-only; the agent can query it directly if the others come up empty
            st, body, _ = get(url, timeout=30)
            if st == 0 or not body:
                continue
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                continue
            if name == "zenodo":
                hits = [{"doi": h.get("doi"), "title": (h.get("metadata") or {}).get("title")}
                        for h in (data.get("hits", {}) or {}).get("hits", [])[:5]]
            else:
                hits = [{"doi": (h.get("_links") or {}).get("self", {}).get("href"), "title": h.get("title")}
                        for h in (data.get("_embedded", {}) or {}).get("stash:datasets", [])[:5]]
            if hits:
                out[name] = hits
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paper_id")
    ap.add_argument("--full", action="store_true", help="print the whole full text too")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quick", action="store_true", help="shorter timeouts; for prewarming")
    a = ap.parse_args()
    if a.quick:
        globals()["SUPPL_TIMEOUT"] = 60

    row = queue_row(a.paper_id)
    doi = row.get("doi", "")
    rec = harvested(a.paper_id, doi)
    pmcid = row.get("pmcid") or rec.get("pmcid") or ""
    pmid = rec.get("pmid") or ""
    title = row.get("title") or rec.get("title") or ""

    ex = cf.ThreadPoolExecutor(max_workers=5)
    jobs = {"fulltext": ex.submit(probe_fulltext, pmcid), "supplement": ex.submit(probe_supplement, pmcid),
            "annotations": ex.submit(probe_annotations, pmcid, pmid), "elink": ex.submit(probe_elink, pmid),
            "repositories": ex.submit(probe_repo_search, doi, title)}
    got = {}
    for k, f in jobs.items():
        try:
            got[k] = f.result(timeout=SUPPL_TIMEOUT + 60)
        except Exception as e:
            got[k] = {"error": repr(e), "note": "probe exceeded its deadline"}
    # Don't wait for a straggler at shutdown: its child process is already bounded, and a
    # `with` block here would re-introduce the hang this whole deadline chain exists to prevent.
    ex.shutdown(wait=False, cancel_futures=True)

    ft = got["fulltext"]
    # Accessions can appear anywhere in the paper, so search the whole text, not just the abstract.
    searched = " ".join([rec.get("abstractText") or "",
                         plain(ft["xml"]) if ft.get("xml") else json.dumps(ft.get("sections", {}))])
    out = {
        "paper_id": a.paper_id, "title": title, "journal": row.get("journal") or "", "doi": doi,
        "pmcid": pmcid, "pmid": pmid, "pub_date": row.get("pub_date") or rec.get("firstPublicationDate"),
        "priority": row.get("priority"),
        "europepmc_flags": {k: rec.get(k) for k in ("isOpenAccess", "inEPMC", "inPMC", "hasSuppl", "hasData",
                                                    "hasDbCrossReferences", "hasTMAccessionNumbers", "hasPDF")},
        "licence_hint": next((u.get("license") for u in (rec.get("fullTextUrlList") or {}).get("fullTextUrl", [])
                              if u.get("license")), None),
        "abstract": rec.get("abstractText"),
        "full_text": {k: v for k, v in ft.items() if k != "xml"},
        "accessions_in_text": find_accessions(searched),
        "text_mined_accessions": got["annotations"],
        "ncbi_links": got["elink"],
        "supplement": got["supplement"],
        "repository_search": got["repositories"],
    }
    if a.full and ft.get("xml"):
        out["full_text"]["text"] = plain(ft["xml"])

    if a.json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return 0

    print(json.dumps({k: v for k, v in out.items() if k not in ("abstract", "full_text")}, indent=1, ensure_ascii=False))
    print("\n--- abstract ---")
    print(out["abstract"] or "(none in Europe PMC)")
    if ft.get("available"):
        print(f"\n--- full text: {ft['chars']} chars, sections found: {list(ft.get('sections', {}))} ---")
        for name, body in (ft.get("sections") or {}).items():
            print(f"\n[{name}]\n{body[:1800]}")
        if a.full:
            print("\n--- whole full text ---")
            print(plain(ft["xml"]))
        else:
            print("\n(use --full for the whole text; it is cached, so that costs no network)")
    else:
        print(f"\n--- no full text: {ft.get('why')} ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
