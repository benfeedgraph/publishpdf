"""How much of the machine one job may use.

`os.cpu_count()` reports the HOST's CPUs. Inside a container (Railway, Docker) that can
be 32+ while the container may use a few cores and a few GB, so sizing process pools by
it started dozens of PDF-rendering processes per job and the container was killed for
running out of memory — every job restarted, over and over.

Here the container's own limits (cgroup CPU quota and memory limit) are read, and shared
between the worker's job slots (JOB_CONCURRENCY). PROCESS_WORKERS overrides it.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# A process rendering or OCR-reading pages of a large PDF needs roughly this much memory.
MB_PER_PROCESS = 450


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


@lru_cache
def cpu_limit() -> float:
    """CPUs this container may use: its CPU quota, else the CPUs it is pinned to."""
    quota = None
    v2 = _read("/sys/fs/cgroup/cpu.max")                     # "max 100000" or "200000 100000"
    if v2:
        q, _, p = v2.partition(" ")
        if q != "max" and p:
            quota = int(q) / int(p)
    else:
        q, p = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"), _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if q and p and int(q) > 0:
            quota = int(q) / int(p)
    try:
        pinned = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        pinned = os.cpu_count() or 2
    return max(1.0, min(quota, pinned) if quota else pinned)


@lru_cache
def memory_limit_mb() -> int | None:
    """The container's memory limit, or None when there is none."""
    raw = _read("/sys/fs/cgroup/memory.max") or _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if not raw or raw == "max":
        return None
    n = int(raw)
    return None if n >= 1 << 60 else n // (1024 * 1024)       # cgroup v1 "unlimited" is huge


def process_workers(cap: int = 8) -> int:
    """Processes one job may start: its share of the container's CPUs and memory."""
    override = os.environ.get("PROCESS_WORKERS")
    if override and override.isdigit():
        return max(1, min(cap, int(override)))
    from app.config import get_settings
    slots = max(1, get_settings().job_concurrency)
    by_cpu = int(cpu_limit() // slots) or 1
    mem = memory_limit_mb()
    by_mem = max(1, mem // (slots * MB_PER_PROCESS)) if mem else by_cpu
    return max(1, min(cap, by_cpu, by_mem))
