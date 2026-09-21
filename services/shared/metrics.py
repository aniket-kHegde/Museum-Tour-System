"""
services/shared/metrics.py

Lightweight, dependency-free latency instrumentation shared by all services.

Each request handler builds a `timings` dict (stage -> milliseconds) and a
`meta` dict (counts / flags / scores), attaches them to the response as
`_timings` / `_meta`, and the base subscriber echoes them back to the caller.
The benchmark driver (scripts/benchmark.py) reads these to decompose
end-to-end latency into per-stage contributions for the paper.

Usage:
    timings, meta = {}, {}
    with timed(timings, "stt_ms"):
        transcript = transcribe(...)
    meta["retrieval_scope"] = "scoped"
    response["_timings"] = timings
    response["_meta"] = meta
"""

import time
from contextlib import contextmanager


@contextmanager
def timed(timings: dict, name: str):
    """Record the wall-clock duration of the block into timings[name] (ms)."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = round((time.perf_counter() - t0) * 1000.0, 2)


def now() -> float:
    """Monotonic timestamp in seconds (for manual deltas)."""
    return time.perf_counter()
