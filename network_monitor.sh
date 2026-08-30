#!/bin/bash
# Network-aware supervisor script
GATEWAY_PORT=8080
LOG_FILE="~/passive_income_swarm/swarm.log"

while true; do
  # Check for internet connectivity
  if ping -c 1 google.com &> /dev/null; then
    # Connection is up
    if ! pgrep -f "openclaw gateway" > /dev/null; then
      echo "[Tue Apr 28 18:36:19 CDT 2026] Network detected, starting OpenClaw gateway..." >> $LOG_FILE
      openclaw gateway --port $GATEWAY_PORT --force &
    fi
  else
    # Connection is down
    if pgrep -f "openclaw gateway" > /dev/null; then
      echo "[Tue Apr 28 18:36:19 CDT 2026] Network lost, stopping OpenClaw gateway..." >> $LOG_FILE
      pkill -f "openclaw gateway"
    fi
  fi
  sleep 30
done
