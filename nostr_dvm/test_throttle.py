#!/usr/bin/env python3
"""test_throttle.py — unit tests for the DVM inbound throttle.

Covers: per-pubkey sliding-window rate limit, window expiry, global
in-flight cap, per-pubkey concurrency cap, slot release.

Run:
    cd ~/passive_income_swarm/nostr_dvm
    ~/passive_income_swarm-env/bin/python test_throttle.py
"""
import sys
import time
from collections import deque

sys.path.insert(0, ".")

import dvm_worker as dw  # noqa: E402  (module import order after sys.path)


def reset():
    dw._req_history.clear()
    dw._pubkey_inflight.clear()
    dw._inflight = 0


def test_rate_window():
    reset()
    pk = "a" * 64
    assert dw._rate_limited(pk) is False          # 1st job allowed
    n = sum(1 for _ in range(dw.DVM_RATE_JOBS - 1) if not dw._rate_limited(pk))
    assert n == dw.DVM_RATE_JOBS - 1              # rest of the budget allowed
    assert dw._rate_limited(pk) is True           # over limit -> reject
    print(f"  PASS: test_rate_window ({dw.DVM_RATE_JOBS} jobs/{dw.DVM_RATE_WINDOW}s)")


def test_rate_window_expiry():
    reset()
    pk = "b" * 64
    for _ in range(dw.DVM_RATE_JOBS):
        dw._rate_limited(pk)
    assert dw._rate_limited(pk) is True
    # Rewind the window: oldest timestamp just outside the window -> allowed again
    dw._req_history[pk] = deque([time.time() - dw.DVM_RATE_WINDOW - 1])
    assert dw._rate_limited(pk) is False
    print("  PASS: test_rate_window_expiry")


def test_global_inflight_cap():
    reset()
    # Fill the global cap using distinct pubkeys (per-pubkey cap is 2, so
    # one pubkey can't fill it alone)
    keys = [f"{i:064d}" for i in range(dw.DVM_MAX_INFLIGHT)]
    for k in keys:
        assert dw._try_acquire_slot(k) is True
    # Global cap reached: even a fresh pubkey is rejected
    assert dw._try_acquire_slot("z" * 64) is False
    # Release frees a global slot
    dw._release_slot(keys[0])
    assert dw._try_acquire_slot("z" * 64) is True
    print(f"  PASS: test_global_inflight_cap ({dw.DVM_MAX_INFLIGHT} slots)")


def test_per_pubkey_concurrency_cap():
    reset()
    pk = "e" * 64
    for _ in range(dw.DVM_MAX_PER_PUBKEY):
        assert dw._try_acquire_slot(pk) is True
    # Per-pubkey cap: same pubkey rejected, different pubkey unaffected
    assert dw._try_acquire_slot(pk) is False
    assert dw._try_acquire_slot("f" * 64) is True
    # Releasing both returns the pubkey to full budget
    dw._release_slot(pk)
    dw._release_slot(pk)
    assert dw._try_acquire_slot(pk) is True
    print(f"  PASS: test_per_pubkey_concurrency_cap ({dw.DVM_MAX_PER_PUBKEY}/pubkey)")


def main():
    print("DVM inbound throttle tests")
    print("=" * 46)
    tests = [test_rate_window, test_rate_window_expiry,
             test_global_inflight_cap, test_per_pubkey_concurrency_cap]
    passed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {t.__name__} — {e!r}")
    print(f"\n{passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
