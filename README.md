# bioml-scout

Reads recent open-access biology papers, finds the data attached to them, opens that data, and
decides whether it is a good basis for a machine-learning project. It only *finds* projects; it
trains nothing itself.

**[▶ Open the dashboard](https://claude.ai/artifact/6vGy3Vuy2NuAbqdXVkscH7)** — every evaluation,
scored and browsable. A snapshot; the live copy is `dashboard.html`, rebuilt every minute while
a run is going.

[![dashboard](docs/dashboard.png)](https://claude.ai/artifact/6vGy3Vuy2NuAbqdXVkscH7)

## Where it has got to

As of 14 October 2026, with the run still going: 771 of 1,027 papers evaluated, from 8 journals
published between 1 July and 1 October 2026.

| verdict | papers | what it means |
|---|---|---|
| **do it** | 25 | passed every deal-breaker, with the data opened and checked |
| **verify first** | 35 | looks good, but one specific count needs confirming |
| park | 62 | passed the deal-breakers, weak case |
| rejected | 649 | failed a deal-breaker — usually the data needs an access application, or the label would have to be constructed |

The highest-scoring candidates are mostly protein-sequence tasks with a measured label, which is
what the rubric is built to surface: a frozen ESM-2 backbone plus a small head, on a laptop.

## How it works

Every paper is evaluated by its own fresh agent session, which is the whole design. A single long
conversation runs out of context after about fifteen papers and then keeps going, producing
confident nonsense without saying so. One paper per session, state on disk, resumable.

```
scripts/harvest.py      Europe PMC by ISSN and date  ->  queue.csv (1,027 papers)
scripts/prewarm.sh      fetches each paper's context ahead of the agent (no quota, just HTTP)
scripts/run_queue.sh    one `claude --agent paper-evaluator` per paper  ->  evaluations/<id>.json
scripts/rank.py         scores them in plain Python  ->  PROJECTS.md, projects.csv
scripts/dashboard.py    the browsable page            ->  dashboard.html
scripts/supervise.sh    restarts the run after a usage limit, a sleep or a crash
```

The agent itself is defined in [`.claude/agents/paper-evaluator.md`](.claude/agents/paper-evaluator.md):
its procedure, its output schema, and the rules that keep it honest. It must open the actual files
and record what it verified against what it only read in the abstract. Three candidates have
already died on exactly that gap.

**Code computes the scores, never the model.** The agent extracts facts and scores each checklist
item with a reason; [`scripts/scoring.py`](scripts/scoring.py) turns those into every total and
the ranking:

```
ROI = payoff × crowding × known-labels ÷ effort        (0–10)
```

- **payoff** — the checklist parts for whether the question is worth asking, whether a pretrained
  model fits, whether it joins to a big dataset, and whether anyone would act on the result.
- **crowding** — shrinks the score when others have already modelled the task well. A crowded
  field can earn part of that back by answering four questions about what our edge actually is.
- **known-labels** — raises it when the thing being predicted is already measured publicly for
  many other items, which gives more training data and a ready-made external test set.
- **effort** — rises for working sets over 1 GB, needing a GPU, straining a 16 GB laptop, messy
  data, or data nobody opened.

The rubric is [`CHECKLIST.md`](CHECKLIST.md); the project's own constraints and conventions are in
[`CLAUDE.md`](CLAUDE.md). [`NOTES.md`](NOTES.md) records every design decision and the bugs behind
them, including the ones that cost a day.

## Running it

```bash
# 1. build the queue (writes queue.csv, then stops)
uv run --no-project --python 3.11 scripts/harvest.py --start 2026-07-01 --end 2026-10-01

# 2. start evaluating, and keep it running unattended
nohup ./scripts/supervise.sh 1 1x 300 > logs/supervise.out 2>&1 &
#                            │ │   └─ seconds between health checks
#                            │ └───── rest after each paper, as a multiple of its own time
#                            └─────── agents in parallel

# 3. read the results
python3 scripts/rank.py                 # PROJECTS.md + projects.csv
python3 scripts/dashboard.py --watch 60 # live dashboard.html
open dashboard.html

./scripts/supervise.sh --stop           # finishes the paper in flight, then stops
```

Pacing is the knob that matters. One agent with a 1× rest works about half the time, which keeps
usage per hour low enough to stay under the rolling limit and leave room for other work. The
supervisor is plain bash and spawns no agents, so a usage limit cannot stop it; when one stops the
run, it reads the reset time out of the limit message and sleeps until exactly then.

## What is not in here

`data/` (5.6 GB of cached API responses and downloads) is excluded and is rebuilt by the scripts.
Some of what it caches is licensed material from publishers, including atlases under CC BY-NC-ND,
which may not be redistributed. `logs/`, `scratchpad/` and the 23 GB `attempted_projects/` tree are
excluded for the same reason or for size. `evaluations/` is here because it is this project's own
analysis.

Before making this repository public, note that `scripts/fetch.py` puts a contact address in its
User-Agent header, which is the polite convention for the NCBI and Europe PMC APIs but is a real
email address.

## Requirements

Python 3.11 (`uv` recommended, standard library only — no pip install needed for the pipeline),
the `claude` CLI, and a Mac or Linux machine. No GPU. The optional dashboard screenshotting uses
headless Chrome.
