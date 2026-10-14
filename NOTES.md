# Notes

Design choices made where the spec left room. Simpler option picked each time.

## Harvest (`scripts/harvest.py` → `queue.csv`)

- **Stdlib only, no pyproject yet.** Run with `uv run --no-project --python 3.11 scripts/harvest.py --start YYYY-MM-DD --end YYYY-MM-DD`. Responses cached in `data/cache/europepmc/` (`--refresh` bypasses). 0.5 s between requests, exponential backoff on 429/5xx.
- **Date field is `FIRST_PDATE`** (first publication, usually the online date), inclusive on both ends.
- **ISSNs:** Europe PMC's `ISSN:` field matches print and online ISSN alike. Both ISSNs of every journal return identical hit counts, so the per-ISSN queries are unioned and deduplicated by DOI.
- **`paper_id` comes from the DOI, not from page numbers.** Online-first papers get pages later, so a page-based id would change between harvests and orphan `evaluations/<id>.json`. Examples: `nar2026-gkag372`, `gb2026-04191-y`, `nature2026-10621-5`, `science2026-aeb2672`. Cell Press DOIs carry their own year (`j.ajhg.2026.06.002` → `ajhg2026-06-002`).
- **No commas, double quotes or CR in any field.** `run_queue.sh` reads rows with `IFS=,`, so commas in titles become `;` (digit separators are removed: `11,000` → `11000`). Line endings are `\n`, not csv's default `\r\n`, which would have left `priority` as `1\r` and broken the priority filter.
- **`data_url`** is the first URL in the abstract (usually the database home page for resource papers), otherwise empty. Not resolved further: that's the evaluator's/`resolve_data`'s job.
- **`is_open_access`** is Europe PMC's `isOpenAccess` flag (an OA licence). A paper can have a `pmcid`, i.e. readable full text, without being flagged OA (author manuscripts).
- **Dropped before queueing** (all listed with reasons in `queue_dropped.csv`):
  - non-research types: errata, corrections, retraction notices, expressions of concern, editorials, news, comments, letters, interviews, plus Science's untagged "In Other Journals" / "In Science Journals" / "New Products" columns;
  - records with no abstract in Nature, Science, Cell, Nature Genetics. These are News & Views, Research Briefings and essays. Their parent research papers are queued separately with abstracts.
  - Nature/Science/Cell papers with no term from `BIO_TERMS`. Words that fire on physics/chemistry/social science (cell(s), behaviour, human(s), drug, biological, immune, synaptic, heart, species) are deliberately not in the list. Every drop was reviewed by hand once (2026-07-01..10-01 run).
- **"historical article" is not a drop reason.** MEDLINE applies it to some ancient-DNA and history-of-research papers that are real research.
- **Reviews are kept** (priority 2). They're biology papers, but rarely carry their own data.
- **Dataset detection (`priority=1`)** uses exactly the requested patterns on title and abstract, plus NAR Database Issue (issue `D*` / pages `D*`). Abstract-only matches are about half noise ("finite resources", "biobank resources", papers that *use* an atlas). Tighten here first if priority 1 is too broad.
- **No LLM yes/no for unclear biology calls.** Ollama isn't installed yet. Keyword rules only.

## Scoring (`scripts/scoring.py`, shared by `rank.py` and `dashboard.py`)

- **ROI = impact × field ÷ effort**, range 0–10. This replaces CHECKLIST.md's `(C + D + G + I) ÷ effort` (2026-10-05):
  - **impact** = 10 × mean(C/10, D/6, G/8, I/8), the same parts as before;
  - **field** comes from C2, the state of the field: no prior work ×1.0; models exist but there's an opening (closed, weak splits, small data) ×0.8; already served ×0.5. Before this, C2 was only 2 of C's 10 points, about 5% of the score.
  - **effort** starts at 1–5 from hours to a feature table, then gets +1 for each CLAUDE.md effort-up rule: working set over 1 GB or the baseline needs a GPU; model-readiness B under 8/12; data files never opened. Capped at 5.
- **B, H, J and E stay out of ROI**, as in CHECKLIST.md. They're pass marks (B≥8, C≥6, D≥4, H≥4, I≥4, J≥6; E1, E3, E6 and E5 or E8), and misses are flagged in the dashboard and PROJECTS.md.
- **The evaluator scores every item** (`score_items`: B1–J5 at 0–2 and E1–E8 as true/false, each with a reason). Code computes the part totals. Papers started before 2026-10-05 20:05 only have part totals.
- **Working set and GPU are never parsed from free text.** The largest size an evaluation mentions is usually raw data the project doesn't need. Old evaluations get these facts, plus C2, from `scripts/backfill_fields.py`. Where a fact is missing, no penalty is applied and the report says so.
- **The weights (×0.8 and ×0.5, +1 per rule) are judgment calls.** They're constants at the top of `scoring.py`.

## Usage limits stop the run (2026-10-05)

The first full run hit the Claude session limit at 20:59 and then marked the remaining 998 papers
"failed" in under two hours — each `claude --print` exited in under a second with
"You've hit your session limit". No evaluations were lost (a paper with no JSON is simply still
pending), but `run.out` was useless and the failure looked like a bug in the evaluator.

`run_queue.sh` now greps each log for a session/usage/rate/quota message and, on a hit, writes
`logs/run_queue.stop` and returns 255. `xargs` stops launching new work on 255, so papers already
in flight finish and nothing else starts. The log that tripped it is kept as `logs/<id>.log.limit`.
Re-run the same command after the reset; finished papers are skipped.

**Concurrency is the knob:** `./scripts/run_queue.sh all all 2` for 2 agents. Each paper is a
full agent session, so 3 at once burns quota three times as fast. Tested at 3 (hit the limit) and
now running at 2.

## Plain language (2026-10-05)

The report is read by a person, not a parser, so the evaluator is told to avoid jargon, explain
any unavoidable term, and spell out every acronym the first time it appears **in each field**
(fields are read on their own in tooltips and table cells).

`scripts/glossary.py` is the safety net for text already on disk: a fixed table of ~45 recurring
terms, applied in code so no agent calls are needed. `rank.py` glosses once per paper (its fields
are read together); the dashboard glosses once per field. Terms a general reader already knows
(CPU, RAM, DNA, CSV) are deliberately absent. Add a term when you hit one you had to look up.

## Watchers hold stale code

Both long-running processes load their code once: `run_queue.sh` (bash reads the whole loop at
start) and `dashboard.py --watch`. After editing either, restart it or the change won't apply —
this has now caused two confusing sessions. The dashboard watcher is cheap to restart:
`pkill -f "dashboard.py --watch"; nohup python3 scripts/dashboard.py --watch 60 > logs/dashboard.out 2>&1 &`

## The pitch (2026-10-06)

Every non-rejected evaluation now carries a `pitch`: the case *for* doing the project. It leads
the report panel and PROJECTS.md, above the plain-language idea.

Five fields: `headline`, `what_makes_the_data_unusual`, `the_win` (the number worth putting in an
abstract), `even_if_it_fails`, `who_wants_it`. The guard rails matter more than the fields:

- **No adjective without a number** — "9,698 genomes, 0 duplicate isolates", not "a large, clean set".
- **Only facts from the `verified` block**, and the pitch must say when a claim rests on the paper's
  text rather than the opened files.
- **It may not contradict `verdict_reason` or `honest_claim_h3`.** It is the best *honest* case; if
  the strongest true pitch is weak, the evaluator writes the weak one.
- **`even_if_it_fails` may say "nothing."** That answer is the most useful one in the whole field,
  since CLAUDE.md ranks a project that can't produce a useless answer above one that must beat a
  strong baseline.

This stays within the "LLM extracts facts and critiques, code computes scores" rule: the pitch is
prose and feeds no score. ROI is unchanged by it.

## Changing concurrency mid-run

`./scripts/run_queue.sh --drain` lets the papers in flight finish and starts nothing new (it
writes the same stop file the usage-limit guard uses, so `xargs` stops launching). Wait for the
process to exit, then start again with the number of agents you want:
`nohup ./scripts/run_queue.sh all all 1 > run.out 2>&1 &`.

Don't `kill` to change concurrency — that takes down the agents mid-paper, and a long atlas paper
can be 25 minutes of quota. The end-of-run line distinguishes the two cases: "DRAINED on request"
versus "STOPPED EARLY on a usage limit".

## Where the time goes per paper (measured 2026-10-06, 61 evaluations)

`python3 scripts/timing.py` reads the agent transcripts and reports this. Baseline:

- **13.5 h wall for 61 papers** — 11 min median, 47 min worst. **63% was model thinking, 37% tools.**
- Bash was 89% of tool time (2,009 calls, 8 s mean). WebSearch 9%, WebFetch 2%, Read/Write ~0.
- Inside Bash: downloads 35%, local parsing 31%, **waiting/polling loops 20%**, API calls 13%.
- **38 calls over 2 minutes accounted for 65% of all Bash time.** The tail is the problem, not the median.
- By endpoint: Europe PMC general 83 min, its `supplementaryFiles` 65 min over 55 calls (71 s mean,
  worst 500 s+), GEO/SRA/ENA 56 min, **virtualenv building 53 min over 106 calls**, figshare/Zenodo
  25 min, NCBI eutils 22 min.
- The same URL was fetched up to 12 times inside one evaluation — one 199 MB figshare file 8 times.

Fixes (in the evaluator's "Compute and network policy"):

1. **`python3`, never `python3 -I`.** Isolated mode hides the user site-packages, so agents
   concluded pandas was missing and built a venv per paper. pandas, numpy, scipy, openpyxl, h5py
   and requests are all already there. This alone was 53 min of the 265.
2. **`scripts/fetch.py` instead of bare `curl`** — caches every response under `data/cache/http/`
   (shared across papers), HEADs first so an oversized file is reported not transferred, rate-limits
   NCBI, and times out at 60 s. Measured: 1.4 s first call, 0.06 s cached.
3. **One 120 s attempt at `supplementaryFiles`,** then record "not retrievable" and move on.
4. **No backgrounded downloads with `until`/`sleep` polling** — 2 min a call, often waiting on
   something that had already failed.
5. **A stated budget:** about 25 tool calls and 15 minutes, then write the JSON with what you have
   and say in `verified` what you didn't reach. An honest partial evaluation beats a slow complete one.

Re-run `scripts/timing.py` after the next 20 papers to see whether this held.

## Cutting per-paper time without checking less (2026-10-06)

The first attempt capped evaluations at 15 minutes, which traded away thoroughness. That cap is
gone. The time now comes from doing the same checks in fewer turns, because **a turn costs far
more than the command inside it** — model thinking was 63% of wall-clock, and it is paid per turn.

- **`scripts/paper_context.py <paper_id>`** replaces the ~9 identical opening calls every
  evaluation made (measured: Europe PMC record 3.1/paper, repository search 1.5, fullTextXML 1.2,
  supplement 1.0, NCBI esearch/elink/efetch 1.8, annotations 0.3). One call, probes run
  concurrently, returns: abstract, full text with the data availability statement and methods
  pulled out, supplement listing with sizes, accessions found anywhere in the text, Europe PMC's
  text-mined accessions, NCBI links to GEO/SRA/BioProject/BioSample/dbGaP, and a Zenodo/Dryad DOI
  search. The Europe PMC record comes free from `data/harvest_raw.jsonl`, which the harvest already
  saved and agents were re-fetching.
- **`scripts/prewarm.sh N`** warms the cache for pending papers in the background. No agents, no
  quota — just HTTP — so it runs alongside the queue and the agent never pays the cold cost. It
  skips papers the evaluator has in flight (parsed from `run.out`); without that it duplicated the
  running paper and raced it on the same URLs.
- **Batching, not budgeting.** The evaluator is told to pack independent probes into one Bash call,
  issue independent tool calls in the same turn, and write a short script for anything over three
  steps. There is no cap on how much it verifies.

Two bugs this exposed, both worth remembering:

- **`urllib`'s `timeout` is per socket read, not a deadline.** A server that trickles bytes holds
  the request open indefinitely: one supplement download ran past 8 minutes under a "120 s"
  timeout. `fetch.py` now reads in 64 KB chunks against a wall-clock deadline. Same paper: 52 s.
- **Cache writes must be atomic.** With a prewarmer and an agent both fetching, a reader that
  checks "body and meta exist" could read a half-written body. Both are now written to a temporary
  file and `os.replace`d.
