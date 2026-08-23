#!/bin/bash
# Deploy generated tools to registry
cd ~/passive_income_swarm/dna_mutation/factory
# Sync any new tool subdirectories
for tool in */; do
  if [ -d "$tool" ]; then
    echo "Publishing $tool..."
    # npx clawhub publish "$tool" --no-input
  fi
done
