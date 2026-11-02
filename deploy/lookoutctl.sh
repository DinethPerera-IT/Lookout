#!/usr/bin/env bash
# Lookout control: the everyday jobs in one command. Run from anywhere:
#
#   deploy/lookoutctl.sh status            services, demo containers, open incidents, last run
#   deploy/lookoutctl.sh start             start Lookout (timer, watcher, dashboard)
#   deploy/lookoutctl.sh stop              stop Lookout
#   deploy/lookoutctl.sh restart           restart Lookout
#   deploy/lookoutctl.sh run               one full check right now
#   deploy/lookoutctl.sh resolve           bring the demo containers back; their incidents resolve
#   deploy/lookoutctl.sh reset [-y]        back up, then wipe the dashboard history (asks first)
#   deploy/lookoutctl.sh test [oom|db|port|all]   live demo: crash, wait for detection + AI, then resolve
#   deploy/lookoutctl.sh latest            the latest incident: cause, time to detect, AI text
#   deploy/lookoutctl.sh logs              follow Lookout live (Ctrl+C to stop)
#
# Needs sudo for start/stop/restart/run/reset. Nothing here changes Lookout's code or config.
set -euo pipefail
cd "$(dirname "$0")/.."

DB=/var/lib/lookout/state.sqlite
API=http://127.0.0.1:8088
SERVICES=(lookout.timer lookout-watch.service lookout-dashboard.service)
DEMO=deploy/demo/demo.sh
AI_WAIT=240        # seconds to wait for an AI explanation (about 60 s on this CPU)

say() { printf '\033[1m%s\033[0m\n' "$*"; }

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

# Small helper: read the dashboard API with Python (no extra tools needed).
py_api() { python3 - "$API" "$@"; }

show_status() {
  say "Services"
  for s in "${SERVICES[@]}"; do printf '  %-28s %s\n' "$s" "$(systemctl is-active "$s" || true)"; done
  say "Demo containers"
  "$DEMO" status | sed 's/^/  /'
  say "Dashboard"
  py_api << 'EOF' || echo "  dashboard not reachable at 127.0.0.1:8088 (try: deploy/lookoutctl.sh start)"
import json, sys, urllib.request
s = json.load(urllib.request.urlopen(sys.argv[1] + "/api/status", timeout=5))
inc = json.load(urllib.request.urlopen(sys.argv[1] + "/api/incidents", timeout=5))["incidents"]
age = s["heartbeat"]["age_s"]
print(f"  open incidents: {s['open']['crit']} critical, {s['open']['warn']} warnings  ({len(inc)} in history)")
print(f"  last run: {'never' if age is None else f'{round(age)} s ago'}  ·  runs in 24 h: {s['runs_24h']}")
for i in inc:
    if i["status"] == "open":
        print(f"    - {i['severity']:4} {i['issue_key']}" + (f"  (cause: {i['cause']})" if i.get("cause") else ""))
EOF
}

show_latest() {
  py_api << 'EOF'
import json, sys, urllib.request
inc = json.load(urllib.request.urlopen(sys.argv[1] + "/api/incidents", timeout=5))["incidents"]
if not inc:
    sys.exit(print("No incidents recorded."))
d = json.load(urllib.request.urlopen(f"{sys.argv[1]}/api/incidents/{inc[0]['id']}", timeout=5))
ttd = d["time_to_detect_s"]
print(f"#{d['id']}  {d['status']}  {d['severity']}  {d['issue_key']}")
print(f"  cause:          {d['cause'] or '-'}")
print(f"  time to detect: {'-' if ttd is None else f'{ttd:.1f} s'}")
print(f"  AI:             {d['ai_status'] or 'not run'}")
if d.get("ai_explanation"):
    print(f"  AI says:        {d['ai_explanation']}")
EOF
}

run_once() {
  say "Running one full check (can take a minute or two if the AI has something to explain)..."
  sudo systemctl start lookout.service
  journalctl -u lookout.service -n 20 --no-pager -o cat | grep '^lookout:' | tail -2 | sed 's/^/  /'
}

resolve() {
  say "Bringing the demo containers back..."
  "$DEMO" reset > /dev/null
  if [[ "$(systemctl is-active lookout-watch.service || true)" == active ]]; then
    sleep 15   # the watcher sees the start events and runs a check by itself
  else
    run_once
  fi
  show_status
}

reset_data() {
  if [[ "${1:-}" != "-y" ]]; then
    read -r -p "Wipe the dashboard history (incidents, runs, issue list)? A backup is made first. [y/N] " ok
    [[ "$ok" == [yY]* ]] || { echo "Cancelled."; exit 0; }
  fi
  local backup="$DB.bak-$(date +%F-%H%M%S)"
  say "Stopping the timer and watcher..."
  sudo systemctl stop lookout.timer lookout-watch.service
  sudo cp -p "$DB" "$backup"
  echo "  backup: $backup"
  sudo python3 - "$DB" << 'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
for table in ("incidents", "runs", "issues"):
    print(f"  {table}: {c.execute(f'DELETE FROM {table}').rowcount} rows cleared")
c.commit()
c.close()
EOF
  "$DEMO" reset > /dev/null
  run_once
  sudo systemctl start lookout.timer lookout-watch.service
  show_status
  echo
  echo "Undo: sudo systemctl stop lookout.timer lookout-watch.service && sudo cp -p $backup $DB && sudo systemctl start lookout.timer lookout-watch.service"
}

# Wait until the dashboard shows a NEW open incident for this demo container, then for its AI text.
wait_for() {
  py_api "$1" "$2" "$AI_WAIT" << 'EOF'
import json, sys, time, urllib.request
api, key, since, ai_wait = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
get = lambda p: json.load(urllib.request.urlopen(api + p, timeout=5))
deadline, inc = time.time() + 60, None
while time.time() < deadline and inc is None:
    inc = next((i for i in get("/api/incidents")["incidents"]
                if i["issue_key"] == key and i["status"] == "open" and i["opened_at"] >= since - 1), None)
    time.sleep(1)
if inc is None:
    sys.exit(print("  no incident after 60 s: is the watcher running? (deploy/lookoutctl.sh status)"))
d = get(f"/api/incidents/{inc['id']}")
print(f"  detected in {d['time_to_detect_s']:.1f} s  ·  cause: {d['cause']}  ·  incident #{d['id']}")
print("  waiting for the AI explanation...", flush=True)
deadline = time.time() + ai_wait
while time.time() < deadline and d["ai_status"] == "pending":
    time.sleep(5)
    d = get(f"/api/incidents/{inc['id']}")
print(f"  AI: {d['ai_status']}" + (f"\n  AI says: {d['ai_explanation']}" if d.get("ai_explanation") else ""))
EOF
}

live_test() {
  local which="${1:-all}" kinds=()
  case "$which" in
    oom|db|port) kinds=("$which") ;;
    all) kinds=(oom db port) ;;
    *) usage ;;
  esac
  [[ "$(systemctl is-active lookout-watch.service || true)" == active ]] \
    || { echo "The watcher isn't running: deploy/lookoutctl.sh start"; exit 1; }
  say "Making sure the demo containers are healthy first..."
  "$DEMO" reset > /dev/null
  sleep 15
  for k in "${kinds[@]}"; do
    echo
    say "Crash: lookout-demo-$k"
    local t0
    t0=$(date +%s.%N)
    "$DEMO" "crash-$k" | sed 's/^/  /'
    wait_for "container_down:lookout-demo-$k" "$t0"
  done
  echo
  say "Recovery: resetting the demo lab (incidents should turn Resolved)..."
  "$DEMO" reset > /dev/null
  sleep 15
  show_status
}

case "${1:-}" in
  status)  show_status ;;
  start)   sudo systemctl start "${SERVICES[@]}"; show_status ;;
  stop)    sudo systemctl stop "${SERVICES[@]}"; echo "Lookout stopped (start again: deploy/lookoutctl.sh start)" ;;
  restart) sudo systemctl restart "${SERVICES[@]}"; show_status ;;
  run)     run_once ;;
  resolve) resolve ;;
  reset)   reset_data "${2:-}" ;;
  test)    live_test "${2:-all}" ;;
  latest)  show_latest ;;
  logs)    journalctl -fu lookout-watch -o cat | grep --line-buffered -v 'shadow report' ;;
  *)       usage ;;
esac
