#!/usr/bin/env bash
# Keep the evaluation queue running without anyone watching it.
#
# Restarts run_queue.sh whenever it stops — a usage limit, a laptop sleep, a crash — but only
# once the network is back, and never while it is still running. Also keeps the dashboard
# watcher, the cache prewarmer and the keep-awake alive.
#
# This script spawns no agents and uses no Claude quota: it is curl, pgrep and sleep. Only
# run_queue.sh starts `claude` processes, so a usage limit can never stop the supervisor
# itself — which is the point. When a limit does stop the run, the limit message states when
# it resets, and the supervisor waits for that exact time rather than guessing.
#
# Usage:
#   nohup ./scripts/supervise.sh > logs/supervise.out 2>&1 &     # 1 agent, 1x rest
#   nohup ./scripts/supervise.sh 1 1x 300 > logs/supervise.out 2>&1 &
#       $1 agents in parallel, $2 rest between papers, $3 seconds between checks
#
# Stop everything:
#   ./scripts/supervise.sh --stop
#
# Papers already evaluated are skipped on every restart, so nothing is ever redone.

set -uo pipefail
cd "$(dirname "$0")/.."

PIDFILE=logs/supervise.pid
mkdir -p logs

if [ "${1:-}" = --stop ]; then
  [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null && echo "supervisor stopped"
  ./scripts/run_queue.sh --drain
  rm -f "$PIDFILE"
  exit 0
fi

JOBS="${1:-1}"
PAUSE="${2:-1x}"
CHECK="${3:-300}"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "A supervisor is already running (pid $(cat "$PIDFILE")). Stop it with: ./scripts/supervise.sh --stop"
  exit 1
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

log() { echo "$(date '+%F %T') $*"; }

alive()   { [ -f logs/run_queue.pid ] && kill -0 "$(cat logs/run_queue.pid)" 2>/dev/null; }
online()  { curl -s -o /dev/null --max-time 10 https://api.anthropic.com; }

# Seconds until the reset time quoted in the newest limit log ("resets 6:20pm"), or empty if
# there is none. The message gives a wall-clock time in the machine's own timezone.
seconds_until_reset() {
  local newest when epoch now
  newest=$(ls -t logs/*.log.limit 2>/dev/null | head -1)
  [ -z "$newest" ] && return
  when=$(grep -o -i "resets [0-9]\{1,2\}:[0-9]\{2\}[ap]m" "$newest" | head -1 | awk '{print $2}')
  [ -z "$when" ] && return
  epoch=$(date -j -f "%I:%M%p" "$when" +%s 2>/dev/null) || return
  now=$(date +%s)
  [ "$epoch" -lt "$now" ] && epoch=$((epoch + 86400))   # already past today, so it means tomorrow
  echo $(( epoch - now + 60 ))                          # a minute of slack
}

log "supervisor up (pid $$): $JOBS agent(s), rest $PAUSE, checking every ${CHECK}s"

backoff=0
while true; do
  if alive; then
    backoff=0
    sleep "$CHECK"
    continue
  fi

  # Wait for the network rather than burning a restart on a failure we can predict.
  until online; do
    log "offline; waiting"
    sleep 60
  done

  # A run that dies within a minute means the limit has not reset. Prefer the reset time the
  # limit message itself quotes; fall back to 10, 20, 40 minutes (capped) if it gives none.
  if [ "$backoff" -gt 0 ]; then
    wait_for=$(seconds_until_reset)
    if [ -n "$wait_for" ] && [ "$wait_for" -gt 0 ] && [ "$wait_for" -lt 86400 ]; then
      log "usage limit resets at $(date -r "$(( $(date +%s) + wait_for ))" '+%H:%M'); sleeping until then"
      sleep "$wait_for"
    else
      log "waiting ${backoff}s before retrying (last run ended immediately)"
      sleep "$backoff"
    fi
  fi

  [ -f run.out ] && mv run.out "logs/run_$(date +%Y-%m-%d-%H%M).out"
  nohup ./scripts/run_queue.sh all all "$JOBS" "$PAUSE" > run.out 2>&1 &
  sleep 20

  if alive; then
    R=$(cat logs/run_queue.pid)
    log "started run_queue (pid $R): $(ls evaluations/*.json 2>/dev/null | wc -l | tr -d ' ') evaluations so far"
    pgrep -f "caffeinate -i -w" > /dev/null || nohup caffeinate -i -w "$R" > /dev/null 2>&1 &
    pgrep -f "dashboard.py --watch" > /dev/null || nohup python3 scripts/dashboard.py --watch 60 > logs/dashboard.out 2>&1 &
    pgrep -f prewarm.sh > /dev/null || nohup ./scripts/prewarm.sh 300 > logs/prewarm.out 2>&1 &
    backoff=0
  else
    backoff=$(( backoff == 0 ? 600 : backoff * 2 ))
    [ "$backoff" -gt 2400 ] && backoff=2400
    log "run_queue exited at once — $(tail -2 run.out | tr '\n' ' ' | cut -c1-160)"
  fi
  sleep "$CHECK"
done
