# bioml-scout

A local tool that reads recent open-access biology papers from journals I choose, finds the data attached to or linked from each paper, inspects that data, and decides whether it's a good basis for an ML project. It outputs a ranked list of project ideas with reasons. The goal is only to *find* a good project for me to work on; this tool itself doesn't train any models.

## Journals and topic

Topic is **biology only**. Search these journals (ISSNs are print / online; query both):

| Journal | ISSN (print) | ISSN (online) | Open access? |
|---|---|---|---|
| Nucleic Acids Research | 0305-1048 | 1362-4962 | Fully OA |
| Genome Biology | 1474-7596 | 1474-760X | Fully OA |
| Cell Genomics | — | 2666-979X | Fully OA |
| Nature Genetics | 1061-4036 | 1546-1718 | Mostly paywalled |
| Nature | 0028-0836 | 1476-4687 | Mostly paywalled |
| Cell | 0092-8674 | 1097-4172 | Mostly paywalled |
| Science | 0036-8075 | 1095-9203 | Mostly paywalled |
| American Journal of Human Genetics (AJHG) | 0002-9297 | 1537-6605 | Partly open (many papers in PMC) |

Notes:
- Nature, Science, and Cell also publish physics, chemistry, astronomy, etc. The filter stage must drop non-biology papers (use subject keywords plus a quick LLM yes/no if unsure).
- Paywalled papers often have no full text in Europe PMC. For those, use the abstract, then find their data by linking the PMID/DOI to repositories (NCBI E-utilities `elink` from PubMed to GEO/SRA/BioProject, plus a DOI search on Zenodo/Figshare/Dryad). Repository metadata is open even when the paper isn't.
- NAR's yearly Database issue and Genome Biology's resource articles are dataset-heavy and especially useful.
- Verify the ISSNs return results in Europe PMC during Phase 1; tell me if any don't.

## Hard constraints

- Runs entirely on my laptop (assume 16 GB RAM, CPU only, maybe Apple Silicon). No paid APIs, no cloud services.
- The LLM runs locally through Ollama. Model name lives in `config/settings.yaml`, never hardcoded.
- Use open-access full text where available (Europe PMC / PubMed Central); otherwise abstract + repository metadata. No scraping publisher websites.
- Be polite to APIs: rate limit (NCBI: max 3 req/s without a key), cache every response, retry with backoff.
- Never download files larger than `max_file_mb` (default 50). For bigger files, record metadata only (name, size, format).
- Never download raw sequencing data (fastq, bam, cram, sra, raw mass-spec). Prefer processed files (count matrices, feature tables, csv/tsv/xlsx).

## Pipeline stages

Each stage reads from and writes to SQLite, so any stage can be rerun alone.

1. **fetch**: get recent papers for journals in `config/journals.yaml` via the Europe PMC REST API (full text when open access, abstract otherwise).
2. **filter**: cheap keyword filter to drop clearly irrelevant papers.
3. **resolve_data**: find datasets from the data availability statement, supplementary file list, accession numbers (Europe PMC Annotations API), and repository links (GEO, SRA, ArrayExpress, PRIDE, PDB, Zenodo, Figshare, Dryad).
4. **inspect**: for each dataset, get file names, sizes, and formats, plus headers and first rows of small tabular files. Produce a short "data card".
5. **extract** (LLM agent 1): fill a fixed JSON schema of facts about the paper and its data.
6. **score** (plain Python, no LLM): compute scores from extracted facts using `config/rubric.yaml`.
7. **skeptic** (LLM agent 2): argue why the top candidates would *not* work. Its red flags can lower the score.
8. **prior_work** (LLM agent 3): for good candidates, find existing ML/AI work, then decide our angle (see "Angles" below). Answer the full C2 audit from CHECKLIST.md for each: name the existing models, whether their weights/code are open, what they scored and on what split, what their authors named as unsolved, and whether we could run one as a baseline. A crowded field is only fatal when the existing models are open *and* already honestly evaluated.
   - The LLM proposes search queries.
   - Code runs them against Europe PMC, bioRxiv, arXiv, and Semantic Scholar.
   - The LLM reads the retrieved abstracts and picks the angle.
9. **integrate** (LLM agent 4): score Part G of CHECKLIST.md. Read the identifiers actually present in the data, name a specific large partner resource joinable on one of them (see the table in CHECKLIST.md), and state in one sentence the bigger question the combination answers. If that sentence can't be written, the join is decoration — score it 0. Also report expected join coverage and the one-to-many collapse rule.
10. **report**: write the final project list to `reports/` (see "Report format" below).

## Directory layout

```
bioml_scout/
  cli.py            # typer CLI, one command per stage
  db.py             # SQLite setup and helpers
  models.py         # pydantic models (Paper, Dataset, DataCard, Extraction, Score, SkepticReview)
  sources/          # europepmc.py, ncbi.py, repositories.py
  stages/           # fetch.py, filter.py, resolve_data.py, inspect.py, extract.py, score.py, skeptic.py, prior_work.py, integrate.py, report.py
  llm.py            # single wrapper around Ollama with schema validation + retries
prompts/            # extractor.md, skeptic.md, prior_work_queries.md, prior_work.md, integrator.md
config/             # settings.yaml, journals.yaml, rubric.yaml
data/               # cache + sqlite db (gitignored)
reports/
tests/
```

## Rubric (summary; weights live in config/rubric.yaml)

**Strong signals:**
- **Negatives are present, not just successes**, and the data isn't one lab / one species / one population. Note what's systematically missing.
- **A named person or group would use the result**, and a result would change what they do.
- **A positive control exists** — some signal already known to be in the data that a working model must recover. Without one you can't tell a working pipeline from a broken one.
- **A second independent source exists for external validation** — another database, another organism, or simply a publication-year column allowing a temporal split. A year column is worth a lot for how little it costs.
- **Model-ready: high quality and usable with minimal preprocessing.** See the "Model-ready data" section below — this is the single most important criterion.
- Data is actually downloadable, not "available on request."
- Enough samples: hundreds at minimum, thousands is better.
- There's a clear label or outcome column a model could predict.
- Data is high quality: clear methods, consistent formats and units, low missingness, documented columns, deposited in a standard repository.
- The current method is slow or expensive (manual annotation, lab assays, simulations).
- **A specific pretrained backbone can be frozen and fitted with a new head on this data.** This is the highest-value signal: name the actual model, not just "transfer learning is possible". Candidates: ESM-2 (proteins, small checkpoints run on CPU), AlphaGenome (DNA; weights released Jan 2026, non-commercial, with the `alphagenome-ft` package for heads-only training), Nucleotide Transformer / DNABERT-2 / HyenaDNA (DNA), ChemBERTa / MolFormer (molecules), scGPT / Geneformer (single cells), RNA-FM (RNA). Note: AlphaFold is a structure predictor, not a freeze-and-head backbone — use its predicted structures as input features instead, and remember AlphaFold 3 weights are access-restricted. Diffusion generators (RFdiffusion, ProteinMPNN, molecular diffusion) need GPUs and far more data than these datasets have; don't propose fine-tuning them.
- The data has standard identifiers (UniProt, Ensembl/NCBI gene, PDB, taxonomy ID, ChEBI/PubChem) that make it joinable with other data.

**Red flags:**
- Tiny n.
- Labels that are subjective or missing.
- Data only available on request.
- Controlled-access data (dbGaP, EGA, UK Biobank, etc.) that needs an application. Common in human genetics (AJHG, Nature Genetics). Don't reject these; flag them as "controlled access" with the repository name, since access can take weeks.
- Only raw data that is too big for a laptop.
- The exact task is already heavily done with ML.

## Angles (what makes our project worth doing)

Every good candidate gets one or more angles, each with a one-sentence justification and links to the related ML papers found:

- **Novel**: little or no ML work exists on this task or data type in this subfield.
- **Better**: ML work exists, but it uses weak baselines, older methods, small or single datasets, no pretrained models, or poor evaluation, and we could clearly improve on it.
- **Diversity**: existing ML work covers a narrow slice (one species, one population/ancestry, one cell type, one tissue, one lab, one condition), and this dataset covers something underrepresented.
- **Unique application**: the data could be used for a different prediction task than the paper intended, or combined with other data in a way nobody has done.

If none apply, the project is marked "already well covered" and drops out of the main list.

## Report format

Write `reports/projects_<date>.md` plus a matching `.csv`. Rank sections by ROI. Start with a summary table (ROI rank, project, backbone to fine-tune, hardware, effort, impact, main risk), then say explicitly which order to do them in, then one section per good project with exactly these six numbered fields. **Keep each field to 2–4 sentences.** No long prose. **Use plain language:** avoid jargon, explain any technical term you can't avoid, and spell out every acronym the first time it appears in each field.

1. **Paper** — brief description of what the paper/resource is and why it exists.
2. **Link** — journal, volume/pages, DOI, database URL, licence.
3. **Data for ML** — what's actually in it: counts, labels/outcomes, formats, download size.
4. **Our application** — the ML task (input → output) and the one experiment that matters.
5. **How hard it would be** — **the honest one-sentence claim the model could make given what's missing from the data (H3); name the positive control and the external validation source, or say neither exists**; **how model-ready the data is and how much preprocessing stands between download and a feature/label table**, laptop feasibility, the main data-cleaning trap, and what to check first.
6. **Novelty / fine-tuning feasibility** — the existing-models audit (named models, open or closed, their scores and splits, what their authors said is unsolved), which angle applies, **which specific pretrained backbone could be frozen and fitted with a head** and on what hardware, and the integration play: a named large partner dataset, the join key, and the bigger question it unlocks. Flag licence restrictions.

End the report with a short "Rejected" list (one line each, with the reason) and a caveats note.

## ROI is the ranking criterion

Rank projects by **impact divided by effort**, not by impact alone. I want easy, high-payoff projects.

Score effort *down* for: tabular data under ~100 MB, frozen-backbone-plus-head workflows that run on a laptop CPU, embeddings that can be cached once and reused, and clean labels needing little cleaning.

Score effort *up* for: anything needing a GPU, working sets over 1 GB, heavy data cleaning (censored values, cross-assay normalization, batch effects), or a project whose viability depends on a count nobody has verified.

Score impact *up* for: **transfer questions** that are informative whichever way they resolve ("does a model trained on A work on B?"), a published number that can be beaten directly, data nobody has modelled yet, and an angle from the Angles list.

Score impact *down* for: crowded subfields, and anything where the likely outcome is a marginal performance gain.

Prefer a project that cannot produce a useless answer over one that needs to beat a strong baseline to mean anything. For every project, state in field 5 the single cheapest check that would kill it (usually a count: how many matched pairs, how many patients per drug), and tell me to do that check first.

## Use the checklist

`CHECKLIST.md` in the repo root is the scoring rubric: Part A killers (verified in the files), then model-readiness, question value, backbone fit, evaluation and controls, integration potential, what's systematically missing from the data, whether anyone would act on the result, fit and finishability, and effort. Score every candidate against it and include the Part A/B/C/D scores and the ROI figure in the report.

## Model-ready data is the top criterion

A good project is one where the data is **high quality** and **ready to model more or less as-is**. Preprocessing is where projects die: it eats the time, it's where silent errors enter, and it's invisible in the paper that described the dataset.

Signs data is model-ready (score these up, heavily):
- One table, or a few tables with a clean primary key that actually joins.
- Labels already in a column — a measured outcome, in consistent units, with the units stated.
- Values on a common scale, or already normalized by the authors.
- Downloads as csv/tsv/parquet and parses on the first try.
- A row count that matches what the paper claims.
- No identifier resolution needed: sequences present rather than accession numbers, SMILES present rather than compound names.
- Duplicates either absent or trivially removable.

Signs data is NOT model-ready (score these down, heavily):
- **The thing being predicted has to be constructed** by joining, matching, or inferring across sources.
- Units missing, mixed, or implied. This is a silent killer: a milli/micro error shifts everything by three orders of magnitude and nothing crashes.
- Censored or qualitative values (">10 µM", "not determined", "inactive") that need a policy decision.
- Identifiers that need a second database to resolve, especially if that means thousands of lookups.
- Several row counts for the same resource across the paper, the API, and the download.
- Labels that come from different assays or labs and aren't comparable.
- The same measurement appearing in multiple sources with disagreeing values.

Rule of thumb: if getting from "downloaded" to "a table with features and a label" takes more than a day, the project is harder than it looks, and that cost must be stated explicitly rather than discovered later.

## Size filter

Prefer small, curated, tabular resources — chemistry, protein variants, curated measurement tables — which run comfortably on a laptop. Genomics-scale resources (whole-genome sequencing, raw single-cell, epigenomic atlases, anything in TB) are deprioritized: score them down on difficulty and state the working-set size in field 5. Include one only when the angle is unusually strong, and say so explicitly.

## LLM rules

- The LLM extracts facts and critiques. **Code computes scores.** Never ask the LLM for a single "is this good?" number.
- Every LLM call goes through `llm.py`, uses Ollama's JSON/schema output, and is validated with pydantic.
  - If validation fails, retry once.
  - If it fails again, mark the record `failed` and move on.
- If something isn't stated in the paper or visible in the data, the field is `null`. No guessing. Each extracted fact should include a short evidence snippet or file name.
- Prompts live in `prompts/*.md`, not inline in code.
- The LLM may only cite papers that the code actually retrieved. Code checks every cited ID against the retrieved set and drops any it can't match.
- Truncate long papers to the relevant sections: abstract, methods, data availability, supplementary captions.

## Conventions

- Python 3.11+, managed with `uv` (or venv + pip if uv is unavailable).
- Allowed dependencies: httpx, pydantic, typer, pyyaml, lxml, pandas, openpyxl, tenacity, rich, ollama, pytest. **Ask before adding anything else.**
- Type hints everywhere and small functions.
- Tests use saved fixture responses in `tests/fixtures/`. Tests never hit the network.
- Log progress with rich, and keep a run log in `data/runs.log`.

## Commands

```
uv run bioml-scout fetch --since 2026-01-01
uv run bioml-scout resolve-data
uv run bioml-scout inspect
uv run bioml-scout extract
uv run bioml-scout score
uv run bioml-scout skeptic --top 30
uv run bioml-scout prior-work --top 20
uv run bioml-scout report
uv run pytest
```

## How to work on this repo (for Claude)

- Work through `TODO.md` in order. Check items off as they're done. Don't skip ahead to later phases.
- Before marking a task done:
  - run the tests;
  - run the stage on a small real sample (5–10 papers);
  - show me the output.
- Keep changes small and commit after each finished task with a clear message.
- If an API behaves differently than expected, stop and tell me rather than working around it silently.
- When unsure about a design choice, pick the simpler option and note it in `NOTES.md`.
