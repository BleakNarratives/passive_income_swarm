#!/bin/bash
# Monitor system load and adjust OpenClaw priority
# Use renice on background processes for priority tasking
PID=$(pgrep -f "openclaw gateway")
if [ ! -z "$PID" ]; then
    renice -n -5 -p $PID
    echo "[Tue Apr 28 19:05:59 CDT 2026] Efficiency hook: Gateway priority boosted." >> ~/passive_income_swarm/swarm.log
fi
