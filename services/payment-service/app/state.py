"""Mutable fault-injection state for Part E experiments.

All of it is process-local and defaults to "no fault", so a restart of the
container is always a full recovery. `/chaos/reset` is the panic button.
"""

import threading

LOCK = threading.Lock()

DEFAULT_CHAOS = {
    # Fraction of charges given an artificial delay (0.2 == every fifth request).
    "latency_probability": 0.0,
    # How long that delay is, in seconds.
    "latency_seconds": 0.5,
    # Fraction of charges answered with HTTP 503 instead of a decision.
    "failure_rate": 0.0,
    # Assignment 2: fail exactly this many upcoming charges, then stop. Lets the
    # caller demo show one retry deterministically instead of by chance.
    "fail_next": 0,
}

CHAOS = dict(DEFAULT_CHAOS)


def reset_chaos() -> dict:
    with LOCK:
        CHAOS.update(DEFAULT_CHAOS)
        return dict(CHAOS)


def claim_forced_failure() -> bool:
    """Use up one of the `fail_next` failures, if any are left."""
    with LOCK:
        if CHAOS["fail_next"] > 0:
            CHAOS["fail_next"] -= 1
            return True
        return False


def snapshot() -> dict:
    with LOCK:
        return dict(CHAOS)
