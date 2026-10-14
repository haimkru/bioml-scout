# Best projects

*5 papers evaluated - 2 passed the killer checks - 3 rejected*

Grouped by verdict, then by ROI. Each score is normalised to its own maximum before combining,
so ROI = mean(worth asking, backbone fit, integration, would anyone care) / effort. Range 0-10.

> **WARNING: 1 of 2 candidates below were scored from the paper text only - nobody opened their data files.** Those scores are unreliable; verify before investing. Candidates marked YES had their actual data inspected.

## Shortlist

| # | Verdict | ROI | Data opened | Project | Effort | First thing to check |
|---|---|---|---|---|---|---|
| 1 | **VERIFY FIRST** | 2.7 | no | Open Enzyme Database: kinetic parameters with as | 1-2 weeks | count rows with sequence+SMILES+value+unit |
| 2 | **PARK** | 5.4 | YES | nn4dms deep mutational scanning collection (5 pr | a day | how many public DMS sets publish raw read  |

---

## Details

### 1. Open Enzyme Database: kinetic parameters with assay conditions

**ROI 2.7** - effort: 1-2 weeks - verdict: **verify first** - Strong fit, but units/duplicates unknown and download unverified

- **Paper:** NAR 2026 - 10.1093/nar/gkaf1082
- **Data:** https://moleculemaker.org/research/alphasynthesis/
- **Rows:** 23309
- **The idea:** Predict log kcat from frozen ESM-2 enzyme embedding + substrate fingerprint; main result is the accuracy-vs-sequence-identity decay curve.
- **What it could honestly claim:** Predicts catalytic rate among enzymes someone chose to characterise, mostly well-studied ones with common substrates.
- **Backbone:** ESM-2 + ChemBERTa (both frozen) (laptop CPU, permissive)
- **Integration:** join AlphaFold DB + Meltome Atlas on `UniProt accession` -> Does structural or thermostability context predict catalytic rate better than sequence alone, especially for enzymes unlike anything in training?
- **Already done by others:**
    - CatPred - strong on published splits. Authors say unsolved: *generalization to enzyme sequences dissimilar to training* - runnable as our baseline
    - UniKP - strong, random-ish splits. Authors say unsolved: *condition dependence* - runnable as our baseline
- **Controls:** positive: predict MW from substrate SMILES; recover known rate spread across EC classes / negative: shuffle kcat labels, must collapse to per-substrate mean / external: train BRENDA / test SABIO-RK; temporal split if year column exists
- **Scores:** B7 C7 D6 G6 H5 I6 J8
- **Verified:** WARNING: paper text only; data never opened
- **START BY:** count rows with sequence+SMILES+value+units after dedupe

### 2. nn4dms deep mutational scanning collection (5 proteins)

**ROI 5.4** - effort: a day - verdict: **park** - Verified clean data but crowded; sequencing-based so excluded by preference

- **Paper:** PNAS 2021 - 10.1073/pnas.2104878118
- **Data:** https://github.com/gitter-lab/nn4dms
- **Rows:** 756090
- **The idea:** Quantify read-depth vs mutation-depth confound; re-run depth extrapolation with and without a read filter.
- **What it could honestly claim:** Predicts fitness among variants a library happened to sample at adequate read depth.
- **Backbone:** ESM-2 (frozen) (laptop CPU, permissive)
- **Integration:** join ProteinGym / MaveDB on `UniProt + position` -> Does read-depth-correlated label noise explain reported extrapolation limits across all public DMS benchmarks?
- **Already done by others:**
    - MULTI-evolve - benchmarked depth extrapolation across published DMS sets. Authors say unsolved: *when higher-order epistasis breaks low-order factorization* - runnable as our baseline
    - METL - position extrapolation on GB1/avGFP. Authors say unsolved: *small-training-set regimes* - runnable as our baseline
- **Controls:** positive: reproduce nn4dms published numbers / negative: shuffle labels / external: other MaveDB datasets
- **Scores:** B12 C3 D6 G4 H4 I3 J9
- **Verified:** opened the data files - downloaded all 5 TSVs, 756,090 rows, 31MB, parsed; checked read-count columns
- **START BY:** how many public DMS sets publish raw read counts at all

---

## Rejected

| Project | Failed on |
|---|---|
| Macrocycle-DB: macrocycle-based drug discovery database | label_is_a_column, units_stated |
| scCT-DB: paired pre/post treatment single-cell transcriptome | enough_rows, fits_laptop |
| Ribocentre-aptamer: structure-focused aptamer database | enough_rows |
