"""Crashfence: deterministic offline safety simulator for a simulated policy council.

The package intentionally has no network or service dependencies.  The only
filesystem state created by this package is a ledger directory explicitly
passed to :mod:`crashfence.worker` or to the unittest crash-recovery tests.
"""

from .model import apply_step, initial_state, simulate_scenario
from .tracecheck import check_trace, enumerate_histories

simulate = simulate_scenario
check = check_trace

__all__ = [
    "simulate_scenario",
    "simulate",
    "initial_state",
    "apply_step",
    "check_trace",
    "check",
    "enumerate_histories",
]

__version__ = "1.0.0"
