#!/usr/bin/env bash
# Warm the HTTP cache for papers that haven't been evaluated yet, so an agent never pays the
# cold cost. No agents, no quota — just cached HTTP, which is why this can run alongside the queue.
#
# Usage:
#   ./scripts/prewarm.sh                 # the next 40 pending papers
#   ./scripts/prewarm.sh 200             # the next 200
#   ./scripts/prewarm.sh 200 1           # priority-1 papers only
#   nohup ./scripts/prewarm.sh 400 > logs/prewarm.out 2>&1 &
#
# Stays a few papers ahead of the evaluator. Already-cached papers return in ~0.1 s, so
# re-running is cheap and safe.

set -uo pipefail
cd "$(dirname "$0")/.."

LIMIT="${1:-40}"
PRIORITY="${2:-}"
mkdir -p logs data/cache/http

# Papers the evaluator has started but not finished: warming those duplicates its work and
# races it on the same URLs, so skip them.
INFLIGHT="$(mktemp)"
trap 'rm -f "$INFLIGHT"' EXIT
if [ -f run.out ]; then
  awk '/\[start\]/{seen[$3]=1} /\[(ok|FAILED|STOP)\]/{delete seen[$3]} END{for (k in seen) print k}' \
    run.out > "$INFLIGHT"
  [ -s "$INFLIGHT" ] && echo "skipping in flight: $(tr '\n' ' ' < "$INFLIGHT")"
fi

done_count=0
tail -n +2 queue.csv | while IFS= read -r line; do
  paper_id="${line%%,*}"
  priority="${line##*,}"
  [ -n "$PRIORITY" ] && [ "$priority" != "$PRIORITY" ] && continue
  [ -f "evaluations/${paper_id}.json" ] && continue
  grep -qxF "$paper_id" "$INFLIGHT" 2>/dev/null && continue
  [ "$done_count" -ge "$LIMIT" ] && break

  start=$(date +%s)
  # --json so nothing is printed but the structure; failures are expected and fine (paywalled papers).
  python3 scripts/paper_context.py "$paper_id" --json --quick > /dev/null 2>&1
  echo "$(date '+%T') warmed $paper_id in $(( $(date +%s) - start ))s"
  done_count=$((done_count + 1))
done

echo "$(date '+%F %T') prewarm finished. Cache: $(du -sh data/cache/http 2>/dev/null | cut -f1)"
