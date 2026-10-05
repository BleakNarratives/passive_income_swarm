#!/bin/bash
# Run the swarm control-plane test suite. Stdlib unittest — no pytest needed.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -t . "$@"
