"""Values computed at scrape time: process health and worker state."""

import os
import resource
import sys
import threading
import time
from collections import Counter as TallyCounter
from collections.abc import Callable, Iterable

from svn_obskit.worker.lifecycle import Worker, WorkerState

try:
    from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
except ImportError:  # pragma: no cover
    CounterMetricFamily = GaugeMetricFamily = None  # type: ignore[assignment]

_STARTED = time.time()
_AVAILABLE_STATES = {WorkerState.IDLE, WorkerState.BUSY}


def _resident_bytes() -> float:
    try:  # current RSS on Linux
        with open("/proc/self/statm") as f:
            return float(int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, IndexError):
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # peak, bytes on macOS and KB on Linux
        return float(peak if sys.platform == "darwin" else peak * 1024)


def _open_fds() -> float | None:
    for path in ("/proc/self/fd", "/dev/fd"):
        try:
            return float(len(os.listdir(path)))
        except OSError:
            continue
    return None


class RuntimeCollector:
    """Process and worker gauges. `workers` and `heartbeat_age` are read on every scrape."""

    def __init__(
        self,
        workers: Callable[[], Iterable[Worker]],
        heartbeat_age: Callable[[], float | None],
    ) -> None:
        self._workers = workers
        self._heartbeat_age = heartbeat_age

    def collect(self):
        usage = resource.getrusage(resource.RUSAGE_SELF)
        yield GaugeMetricFamily("obskit_process_uptime_seconds", "Seconds since the SDK was loaded.",
                                value=time.time() - _STARTED)  # fmt: skip
        cpu = CounterMetricFamily("obskit_process_cpu_seconds", "CPU time used by the process.")
        cpu.add_metric([], usage.ru_utime + usage.ru_stime)
        yield cpu
        yield GaugeMetricFamily(
            "obskit_process_resident_memory_bytes", "Resident memory.", value=_resident_bytes()
        )
        yield GaugeMetricFamily(
            "obskit_process_threads", "Active threads.", value=float(threading.active_count())
        )
        fds = _open_fds()
        if fds is not None:
            yield GaugeMetricFamily("obskit_process_open_fds", "Open file descriptors.", value=fds)

        workers = list(self._workers())
        by_state = TallyCounter((w.worker_type, w.state.value) for w in workers)
        states = GaugeMetricFamily(
            "obskit_workers", "Workers by type and state.", labels=["worker_type", "state"]
        )
        for (wtype, state), n in sorted(by_state.items()):
            states.add_metric([wtype, state], n)
        yield states
        available = GaugeMetricFamily(
            "obskit_workers_available", "Workers able to take work.", labels=["worker_type"]
        )
        for wtype in sorted({w.worker_type for w in workers}):
            available.add_metric(
                [wtype], sum(1 for w in workers if w.worker_type == wtype and w.state in _AVAILABLE_STATES)
            )
        yield available
        age = self._heartbeat_age()
        if age is not None:
            yield GaugeMetricFamily(
                "obskit_worker_heartbeat_age_seconds", "Seconds since the last accepted heartbeat.", value=age
            )
