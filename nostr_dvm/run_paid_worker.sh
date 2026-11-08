#!/usr/bin/env bash
# Launch the existing NIP-90 worker with fail-closed paid-mode protections.
set -eu
cd "$(dirname "$0")"
export DVM_PAID_ONLY=1
export DVM_MAX_INFLIGHT=1
exec "${PYTHON:-python3}" paid_mode_guard.py
