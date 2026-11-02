#!/usr/bin/env bash
# Lookout demo lab. Usage: deploy/demo/demo.sh start|crash-oom|crash-db|crash-port|reset|status|remove
#   start       build the image (from local node:22-alpine) and start all three, healthy
#   crash-oom   lookout-demo-oom allocates memory until the kernel OOM-kills it (exit 137)
#   crash-db    lookout-demo-db loses its database: connection refused x3, stack trace, exit 1
#   crash-port  lookout-demo-port tries to bind a port already in use: EADDRINUSE, exit 1
#   reset       recreate all three, healthy again
#   status      show the demo containers
#   remove      stop and delete the demo containers and network (Lookout will report them as gone)
set -euo pipefail
cd "$(dirname "$0")"
compose() { docker compose -f compose.yaml "$@"; }

show() { docker ps -a --filter label=lookout.demo=true --format 'table {{.Names}}\t{{.State}}\t{{.Status}}'; }

crash() {
  local name="lookout-demo-$1"
  [[ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" == "true" ]] \
    || { echo "$name isn't running: run '$0 reset' first" >&2; exit 1; }
  echo "$(date +%T) triggering failure in $name"
  docker kill -s USR1 "$name" >/dev/null
  for _ in $(seq 1 30); do
    [[ "$(docker inspect -f '{{.State.Running}}' "$name")" == "false" ]] && break
    sleep 1
  done
  docker inspect -f "$(date +%T) $name: {{.State.Status}}, exit {{.State.ExitCode}}, OOMKilled={{.State.OOMKilled}}" "$name"
}

case "${1:-}" in
  start)      compose up -d --build && show ;;
  crash-oom)  crash oom ;;
  crash-db)   crash db ;;
  crash-port) crash port ;;
  reset)      compose up -d --force-recreate && show ;;
  status)     show ;;
  remove)     compose down --remove-orphans ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
