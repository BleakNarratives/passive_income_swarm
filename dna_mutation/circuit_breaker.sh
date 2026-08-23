#!/bin/bash
# Checks log size and resource usage before allowing new mutation
if [ $(wc -l < ~/passive_income_swarm/dna_mutation/logs/mutation.log) -gt 1000 ]; then
  echo "[Tue Apr 28 19:13:46 CDT 2026] Circuit breaker tripped: Mutation log too large. Halting." >> ~/passive_income_swarm/swarm.log
  exit 1
fi
