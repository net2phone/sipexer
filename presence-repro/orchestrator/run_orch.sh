#!/bin/bash
# Supervisor: keeps wave_orch.py running until $WAVES_DIR/orch/DONE exists (restarts it after a crash).
D="${WAVES_DIR:-/tmp/waves}/orch"; mkdir -p "$D"
HERE="$(cd "$(dirname "$0")" && pwd)"
echo $$ > "$D/supervisor.pid"
command -v caffeinate >/dev/null && caffeinate -dims -w $$ &
while [ ! -f "$D/DONE" ]; do
  python3 -u "$HERE/wave_orch.py" >> "$D/orch.log" 2>&1
  echo "$(date '+%H:%M:%S') SUPERVISOR orchestrator exited rc=$?, restarting in 20 s" >> "$D/orch.log"
  sleep 20
done
echo "$(date '+%H:%M:%S') SUPERVISOR done: $(cat "$D/DONE")" >> "$D/orch.log"
