#!/bin/bash
while true; do
  # Find files in inbox
  files=(~/passive_income_swarm/control/inbox/*)
  if [ -e "${files[0]}" ]; then
    for cmd_file in "${files[@]}"; do
      echo "[Tue Apr 28 18:40:48 CDT 2026] Executing human command from $cmd_file" >> ~/passive_income_swarm/swarm.log
      bash "$cmd_file" >> ~/passive_income_swarm/control/outbox/last_result.log 2>&1
      mv "$cmd_file" ~/passive_income_swarm/control/pending/
    done
  fi
  sleep 5
done
