---
description: Build the queue of papers to evaluate from the journals in CLAUDE.md
---

Build `queue.csv`, the list of papers to evaluate. Do not evaluate anything yet.

1. Read the journal list in `CLAUDE.md`.
2. For each journal, query the Europe PMC REST API by ISSN for the date range: $ARGUMENTS (default: last 3 months). Paginate. Use a script, not one-off fetches.
3. Write `queue.csv` with columns: `paper_id, title, journal, doi, pmcid, pub_date, is_open_access, data_url, status, priority`.
   - `status` starts as `pending`.
   - `paper_id` is a stable slug, e.g. `nar2026-d1469`.
4. **Flag dataset papers as `priority=1`.** Detect them by title and abstract patterns: "database", "atlas", "resource", "catalog", "compendium", "repository", "a comprehensive collection of", plus anything in a journal's Database Issue. Everything else is `priority=2`.
5. Drop clearly non-biology papers (Nature, Science and Cell also publish physics and chemistry).
6. Print a summary: papers per journal, how many found, how many dataset papers, how many open access.

Then stop and show me the summary before anything is evaluated.
