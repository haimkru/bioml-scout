#!/usr/bin/env bash
# Evaluate pending papers in queue.csv, one fresh Claude context per paper, JOBS at a time.
# Resumable: papers with a valid evaluations/<id>.json are skipped, so just re-run after a crash.
#
# Usage:
#   ./scripts/run_queue.sh                # all pending, 3 at a time
#   ./scripts/run_queue.sh 10             # first 10 pending (start here)
#   ./scripts/run_queue.sh 10 1           # first 10 pending with priority 1 (dataset papers)
#   ./scripts/run_queue.sh 30 1 4         # first 30 priority-1 papers, 4 at a time
#   ./scripts/run_queue.sh all all 1      # everything, serially
#   ./scripts/run_queue.sh all all 1 5    # ... and rest 5 minutes after each paper
#   ./scripts/run_queue.sh all all 1 2x   # ... rest twice as long as each paper took
#
# The 4th argument paces usage: total use per paper is unchanged, but use per hour drops, so a
# long run stays under the rolling usage limit and leaves quota for other work. At ~5 min a paper,
# a 5-minute rest gives ~30 papers per 5-hour window instead of hitting the limit after ~43.
#
# Stops early if the Claude session limit is hit, instead of marking the rest of the
# queue failed. Re-run after the reset time it prints; finished papers are skipped.
#
# Stop it:
#   ./scripts/run_queue.sh --drain     # let in-flight papers finish, start nothing new
#   kill "$(cat logs/run_queue.pid)"   # stop now, killing the agents in flight
#
# To change how many run at once, drain and then start again with the new number.
#
# The pending list is snapshotted at start, so re-running the harvest mid-run is safe;
# the new queue.csv is picked up on the next run.

set -uo pipefail
cd "$(dirname "$0")/.."

if [ "${1:-}" = --drain ]; then
  # Papers in flight finish normally; evaluate_one returns 255 for anything new, so xargs stops.
  if [ -f logs/run_queue.pid ] && kill -0 "$(cat logs/run_queue.pid)" 2>/dev/null; then
    touch logs/run_queue.stop
    echo "Draining pid $(cat logs/run_queue.pid): in-flight papers will finish, nothing new will start."
    echo "Watch it wind down with: tail -f run.out"
  else
    echo "Nothing is running."
  fi
  exit 0
fi

LIMIT="${1:-all}";    [ "$LIMIT" = all ] && LIMIT=99999
PRIORITY="${2:-all}"; [ "$PRIORITY" = all ] && PRIORITY=""
JOBS="${3:-3}"
PAUSE="${4:-0}"
export FALLBACK_MODEL="${FALLBACK_MODEL:-claude-opus-5}"   # used only when the default model declines
# "2x" means rest twice as long as the paper took; a plain number means that many minutes.
case "$PAUSE" in
  *x) export PAUSE_FACTOR="${PAUSE%x}" PAUSE_SECONDS=0 ;;
  *)  export PAUSE_FACTOR=0 PAUSE_SECONDS=$(( PAUSE * 60 )) ;;
esac
PIDFILE=logs/run_queue.pid
STOPFILE=logs/run_queue.stop
mkdir -p evaluations logs
rm -f "$STOPFILE"
export STOPFILE

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "run_queue.sh is already running (pid $(cat "$PIDFILE")). Stop it first: kill $(cat "$PIDFILE")"
  exit 1
fi
echo $$ > "$PIDFILE"
PENDING="$(mktemp)"
RESULTS="$(mktemp)"
export RESULTS

kill_tree() {
  local child
  for child in $(pgrep -P "$1"); do kill_tree "$child"; done
  kill "$1" 2>/dev/null
}
stop() {
  trap - INT TERM
  echo "Stopping: killing in-flight agents..."
  for child in $(pgrep -P $$); do kill_tree "$child"; done
  exit 130
}
trap stop INT TERM
trap 'rm -f "$PIDFILE" "$PENDING" "$RESULTS"' EXIT

# An agent killed mid-write can leave a truncated JSON that would be skipped forever.
# Move unparseable files aside so those papers are retried.
python3 - <<'EOF'
import glob, json, os
for p in glob.glob("evaluations/*.json"):
    try:
        json.load(open(p))
    except Exception:
        os.replace(p, p + ".bad")
        print(f"unparseable, moved aside for retry: {p}")
EOF

# Snapshot the pending rows: header skipped, priority filter, already-evaluated skipped.
tail -n +2 queue.csv | while IFS= read -r line; do
  paper_id="${line%%,*}"
  priority="${line##*,}"
  [ -n "$PRIORITY" ] && [ "$priority" != "$PRIORITY" ] && continue
  [ -f "evaluations/${paper_id}.json" ] && continue
  printf '%s\n' "$line"
done | head -n "$LIMIT" > "$PENDING"

total=$(wc -l < "$PENDING" | tr -d ' ')
case "$PAUSE" in *x) rest_desc="rest ${PAUSE} the paper's own time" ;; *) rest_desc="${PAUSE} min rest" ;; esac
echo "$(date '+%F %T') $total papers to evaluate, $JOBS at a time, $rest_desc between papers (pid $$)"

evaluate_one() {
  [ -f "$STOPFILE" ] && return 255          # out of quota: stop without touching the rest
  local paper_id title journal doi pmcid pub_date is_oa data_url status priority
  IFS=, read -r paper_id title journal doi pmcid pub_date is_oa data_url status priority <<< "$1"
  local out="evaluations/${paper_id}.json" log="logs/${paper_id}.log" started=$(date +%s)
  echo "$(date '+%T') [start]  $paper_id — $title"

  # --print runs headless and exits. Each call is a FRESH context: no drift,
  # no context exhaustion, and one bad paper can't poison the rest.
  # The prompt goes in on stdin: --allowedTools takes a variable number of values
  # and would swallow a positional prompt as one more tool name.
  # --agent makes this session the evaluator itself. It used to be an outer session that loaded
  # the project context, started the evaluator as a subagent, waited, then re-checked the JSON:
  # two agent sessions per paper, one of which did nothing useful.
  local prompt="Evaluate exactly one paper and write ${out}.
paper_id: ${paper_id}
title: ${title}
doi: ${doi}
pmcid: ${pmcid}
journal: ${journal}
Follow CHECKLIST.md. Try to open the actual data files and record in 'verified' what you checked versus what you only read in the paper."
  printf '%s\n' "$prompt" \
    | claude --print --agent paper-evaluator --allowedTools "Read,Write,Bash,WebFetch,WebSearch" > "$log" 2>&1

  # The default model's safeguards sometimes refuse ordinary biology papers (a cancer-epitope
  # database was flagged). Only then, retry once on the fallback model; every other paper stays
  # on the default. The refusal is kept at the top of the log.
  if [ ! -f "$out" ] && grep -q "safeguards flagged" "$log" 2>/dev/null; then
    echo "$(date '+%T') [retry]  $paper_id — default model declined; retrying on $FALLBACK_MODEL"
    { echo "--- default model declined; retried on $FALLBACK_MODEL ---"; } >> "$log"
    printf '%s\n' "$prompt" \
      | claude --print --agent paper-evaluator --model "$FALLBACK_MODEL" \
          --allowedTools "Read,Write,Bash,WebFetch,WebSearch" >> "$log" 2>&1
    [ -f "$out" ] && echo "$paper_id" >> logs/fallback_model_papers.txt
  fi

  # A usage limit fails instantly and would otherwise mark every remaining paper failed.
  # Exit 255 so xargs stops launching new work; papers already in flight finish normally.
  if grep -qi "session limit\|usage limit\|rate limit\|quota" "$log" 2>/dev/null && [ ! -f "$out" ]; then
    rm -f "$log.limit"; cp "$log" "$log.limit"
    echo limit > "$STOPFILE"
    echo "$(date '+%T') [STOP]   $paper_id — $(tr -d '\n' < "$log" | head -c 200)"
    echo "         Stopping: nothing else will start. Re-run after the reset; finished papers are skipped."
    echo limit >> "$RESULTS"
    return 255
  fi

  if [ -f "$out" ] && python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$out" 2>/dev/null; then
    echo "$(date '+%T') [ok]     $paper_id"
    echo ok >> "$RESULTS"
  else
    [ -f "$out" ] && mv "$out" "$out.bad"
    echo "$(date '+%T') [FAILED] $paper_id — see $log"
    echo failed >> "$RESULTS"
  fi
  # Rest between papers: spreads usage over the limit window. Checked in short steps so a
  # --drain or a usage-limit stop isn't kept waiting for the whole pause.
  local rest="${PAUSE_SECONDS:-0}" waited=0
  if [ "${PAUSE_FACTOR:-0}" != 0 ]; then
    rest=$(awk -v t=$(( $(date +%s) - started )) -v f="$PAUSE_FACTOR" 'BEGIN{printf "%d", t*f}')
    # One pathological paper (67 min) shouldn't buy an hour of idling: cap the proportional rest.
    [ "$rest" -gt "${MAX_REST_SECONDS:-600}" ] && rest="${MAX_REST_SECONDS:-600}"
  fi
  [ "$rest" -gt 0 ] && echo "$(date '+%T') [rest]   $(( rest / 60 )) min before the next paper"
  # Rest by the wall clock, not by counting naps: a nap doesn't tick while the Mac sleeps, so a
  # 6-minute rest that started before the lid closed was still running hours later.
  local until_ts=$(( $(date +%s) + rest ))
  while [ "$(date +%s)" -lt "$until_ts" ] && [ ! -f "$STOPFILE" ]; do sleep 10; done
  sleep 2                                                   # be kind to the APIs
  return 0
}
export -f evaluate_one

# Run in the background and wait, so a TERM/INT trap fires immediately
# (bash defers traps until a foreground command finishes).
tr '\n' '\0' < "$PENDING" | xargs -0 -n 1 -P "$JOBS" bash -c 'evaluate_one "$1"' _ &
wait $!

echo
echo "$(date '+%F %T') done: $(grep -c '^ok' "$RESULTS") ok, $(grep -c '^failed' "$RESULTS") failed." \
     "$(ls evaluations/*.json 2>/dev/null | wc -l | tr -d ' ') evaluations on disk."
if [ -f "$STOPFILE" ]; then
  if [ -s "$STOPFILE" ]; then
    echo "STOPPED EARLY on a usage limit — the rest of the queue was not attempted."
    echo "Re-run the same command after the reset time above."
  else
    echo "DRAINED on request — the rest of the queue was not attempted. Re-run to continue."
  fi
  rm -f "$STOPFILE"
fi
echo "Now run: python3 scripts/rank.py  -> writes PROJECTS.md"
