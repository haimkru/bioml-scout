#!/usr/bin/env bash
# Evaluate every pending paper in queue.csv, one fresh Claude context each.
# Resumable: already-evaluated papers are skipped, so just re-run after a crash.
#
# Usage:
#   ./scripts/run_queue.sh            # all pending
#   ./scripts/run_queue.sh 10         # first 10 pending (start here)
#   ./scripts/run_queue.sh 10 1       # first 10 pending with priority 1 (dataset papers)

set -uo pipefail

LIMIT="${1:-99999}"
PRIORITY="${2:-}"
mkdir -p evaluations logs

count=0
while IFS=, read -r paper_id title journal doi pmcid pub_date is_oa data_url status priority; do
  [ "$paper_id" = "paper_id" ] && continue                 # header
  [ -f "evaluations/${paper_id}.json" ] && continue         # already done
  [ -n "$PRIORITY" ] && [ "$priority" != "$PRIORITY" ] && continue
  [ "$count" -ge "$LIMIT" ] && break

  echo "[$((count+1))] $paper_id — $title"

  # --print runs headless and exits. Each call is a FRESH context: no drift,
  # no context exhaustion, and one bad paper can't poison the rest.
  claude --print \
    --allowedTools Read,Write,Bash,WebFetch,WebSearch \
    "Use the paper-evaluator subagent to evaluate exactly one paper and write evaluations/${paper_id}.json.
     paper_id: ${paper_id}
     title: ${title}
     doi: ${doi}
     pmcid: ${pmcid}
     journal: ${journal}
     Follow CHECKLIST.md. Try to open the actual data files and record in 'verified' what you checked versus what you only read in the paper." \
    > "logs/${paper_id}.log" 2>&1

  if [ -f "evaluations/${paper_id}.json" ]; then
    echo "    ok"
  else
    echo "    FAILED — see logs/${paper_id}.log"
  fi

  count=$((count+1))
  sleep 2                                                   # be kind to the APIs
done < queue.csv

echo
echo "Evaluated $count papers. $(ls evaluations/*.json 2>/dev/null | wc -l) total on disk."
echo "Now run: python3 scripts/rank.py  -> writes PROJECTS.md"
