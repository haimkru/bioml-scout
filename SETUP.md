# How to set up the paper-evaluating agent

## The one thing that matters

**Never ask a single conversation to evaluate 182 papers.** It will run out of context around paper 15 and keep going anyway, producing confident garbage without telling you. Every arrangement below exists to avoid that.

The pattern: **one paper per fresh agent context, state on disk, resumable.**

## Layout

```
paper-eval/
  CLAUDE.md                        # journals, rules, pipeline  (from the kit)
  CHECKLIST.md                     # the rubric                 (from the kit)
  queue.csv                        # the papers to do
  .claude/
    agents/paper-evaluator.md      # subagent: one paper -> one JSON
    commands/harvest.md            # /harvest -> builds queue.csv
  scripts/
    run_queue.sh                   # loops Claude over the queue
    rank.py                        # aggregates JSON -> REPORT.md
  evaluations/<paper_id>.json      # one file per paper
  logs/<paper_id>.log
  PROJECTS.md                      # <-- THE OUTPUT: best projects, ranked
  projects.csv                     #     same thing, sortable in a spreadsheet
  EXAMPLE_PROJECTS.md              #     what PROJECTS.md looks like, filled in
```

## The output file

`PROJECTS.md` is the point of all this. Open `EXAMPLE_PROJECTS.md` to see the shape — it's a real run over the candidates from this project's history. It has:

- a **shortlist table** grouped by verdict (DO IT / VERIFY FIRST / PARK), then by ROI, with effort and the first thing to check for each;
- a **details block** for the top 10: the idea, the data link, row count, which backbone to freeze, what's already been done by others and what their authors said is unsolved, the controls, and the honest one-sentence claim;
- a **rejected table** with what each one failed on;
- a **warning banner** counting how many surviving candidates were scored from paper text alone without anyone opening the data.

Scores are normalised to their own maxima before combining, so a cheap project with a weak question can't outrank a strong one. Verdict sets the tier; ROI only orders within a tier.

## Setup

1. `mkdir paper-eval && cd paper-eval && git init`
2. Copy in `CLAUDE.md` and `CHECKLIST.md` from the evaluation kit.
3. Copy in the `.claude/` and `scripts/` folders from here.
4. `chmod +x scripts/run_queue.sh`
5. Open the folder in VS Code.

## Running it

**Step 1 — build the queue.** In the Claude Code panel:
```
/harvest 2026-07-01 to 2026-10-01
```
This writes `queue.csv` and stops. Dataset papers get `priority=1`. Check the summary before going on.

**Step 2 — test on ten papers.** In a terminal:
```bash
./scripts/run_queue.sh 10 1        # 10 papers, dataset papers only
python3 scripts/rank.py
```
Read `PROJECTS.md` and three or four of the raw JSON files. This is the step people skip and regret. You're checking whether the agent is actually opening data files or just paraphrasing abstracts.

**Step 3 — fix the prompt, not the output.** If the evaluations are shallow, edit `.claude/agents/paper-evaluator.md`. Delete the ten JSON files and re-run. Iterating here is cheap; iterating after 182 papers is not.

**Step 4 — run the rest.**
```bash
./scripts/run_queue.sh             # resumable; just re-run if it dies
python3 scripts/rank.py
```

## Why it's built this way

**`claude --print` instead of a long conversation.** Each invocation is a fresh context. No drift, no context exhaustion, and one weird paper can't poison the next fifty.

**A subagent rather than inline instructions.** The subagent definition is a contract: fixed tools, fixed output schema, one paper per call. It also can't wander off and start writing the ML project.

**One JSON file per paper as the state.** Resumability is free — the loop skips any paper with an existing file. Kill it, restart it, re-run one paper by deleting its file.

**Ranking in Python, not by the LLM.** Scores from a fixed rubric computed in code are comparable across papers. Scores from an LLM asked "rate this out of 10" are not.

**A `verified` block in the schema.** This is the honesty mechanism. It separates "I downloaded the file and counted rows" from "the abstract said 45,525". The ranking script counts how many surviving candidates were never actually verified and says so at the top of the report. Three candidates have already died on exactly this gap.

## Tuning it

- **Dataset papers first.** `./scripts/run_queue.sh 50 1` does only `priority=1`. Database and atlas papers have a far higher hit rate than research papers, because the data *is* the contribution.
- **Parallelism.** If you have the quota, run several at once with GNU parallel. Start serial until the prompt is right.
- **Cost control.** Make `/harvest`'s keyword filter stricter before spending LLM calls. Filtering is free; evaluating isn't.
- **Hooks.** If the agent keeps forgetting to open data files, add a hook in `.claude/settings.json` that rejects a JSON write when `opened_data_files` is false and no reason is given. Enforcement in code beats asking nicely in a prompt.

## What to expect

Of roughly 180 papers, expect maybe 20–40 to be dataset papers, maybe 5–10 to survive Part A, and 1–3 worth actually starting. That's a normal yield. The value is in the rejections being *fast* — thirty seconds of reading a JSON instead of three days of building against data that turns out not to exist.
