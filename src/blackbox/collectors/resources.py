"""Sample CPU, memory and disk usage and flag spikes against a rolling baseline.

CPU and memory spikes need to be both high in absolute terms (above a floor)
*and* well above the recent baseline, so a machine that always runs hot does not
alarm constantly. Disk usage is threshold-only: a disk that fills slowly should
still alert. Samples taken during a spike are kept out of the baseline.
"""

from __future__ import annotations

import importlib
import os
import shutil
import statistics
from collections import deque
from collections.abc import Sequence
from typing import Any, Protocol

from ..model import KIND_RESOURCE_RECOVERED, KIND_RESOURCE_SPIKE, EventDraft
from ..store import Store
from .base import Observation

MIN_BASELINE_SAMPLES = 5
RECOVERY_MARGIN = 5.0


class SamplerUnavailable(RuntimeError):
    """No supported source of CPU/memory figures on this platform."""


class Sampler(Protocol):
    def sample(self) -> dict[str, float]:
        """Return available metrics (percent): ``cpu`` and/or ``mem``."""
        ...


class PsutilSampler:
    def __init__(self) -> None:
        self._psutil: Any = importlib.import_module("psutil")
        self._psutil.cpu_percent(interval=None)  # prime: first call is meaningless

    def sample(self) -> dict[str, float]:
        return {
            "cpu": float(self._psutil.cpu_percent(interval=None)),
            "mem": float(self._psutil.virtual_memory().percent),
        }


class ProcSampler:
    """Linux fallback using /proc, with no third-party dependency."""

    def __init__(self, proc: str = "/proc") -> None:
        self._proc = proc
        self._last: tuple[int, int] | None = None
        if not os.path.exists(os.path.join(proc, "stat")):
            raise SamplerUnavailable("no /proc/stat on this platform")

    def sample(self) -> dict[str, float]:
        metrics: dict[str, float] = {}
        with open(os.path.join(self._proc, "stat"), encoding="ascii") as handle:
            fields = [int(x) for x in handle.readline().split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        total = sum(fields)
        if self._last is not None:
            d_total, d_idle = total - self._last[0], idle - self._last[1]
            if d_total > 0:
                metrics["cpu"] = max(0.0, min(100.0, 100.0 * (1 - d_idle / d_total)))
        self._last = (total, idle)
        meminfo: dict[str, int] = {}
        with open(os.path.join(self._proc, "meminfo"), encoding="ascii") as handle:
            for line in handle:
                name, _, rest = line.partition(":")
                meminfo[name] = int(rest.split()[0])
        if meminfo.get("MemTotal"):
            available = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
            metrics["mem"] = 100.0 * (1 - available / meminfo["MemTotal"])
        return metrics


def default_sampler() -> Sampler:
    try:
        return PsutilSampler()
    except ImportError:
        return ProcSampler()


class ResourcesCollector:
    name = "resources"

    def __init__(
        self,
        *,
        cpu_floor: float = 85.0,
        mem_floor: float = 90.0,
        disk_floor: float = 90.0,
        disk_paths: Sequence[str] = ("/",),
        window: int = 30,
        sigma: float = 3.0,
        min_jump: float = 15.0,
        sampler: Sampler | None = None,
    ) -> None:
        self.floors = {"cpu": cpu_floor, "mem": mem_floor}
        self.disk_floor = disk_floor
        self.disk_paths = list(disk_paths)
        self.sigma = sigma
        self.min_jump = min_jump
        self._window = window
        self._sampler = sampler
        self._history: dict[str, deque[float]] = {}
        self._spiking: set[str] = set()

    def collect(self, store: Store, now_us: int) -> Observation:
        if self._sampler is None:
            self._sampler = default_sampler()  # may raise SamplerUnavailable
        obs = Observation()
        for metric, value in self._sampler.sample().items():
            self._evaluate(obs, metric, value, self.floors[metric], use_baseline=True)
        for path in self.disk_paths:
            try:
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            percent = 100.0 * usage.used / usage.total if usage.total else 0.0
            self._evaluate(obs, f"disk:{path}", percent, self.disk_floor, use_baseline=False)
        return obs

    def _evaluate(
        self, obs: Observation, metric: str, value: float, floor: float, *, use_baseline: bool
    ) -> None:
        history = self._history.setdefault(metric, deque(maxlen=self._window))
        mean = std = None
        if use_baseline and len(history) >= MIN_BASELINE_SAMPLES:
            mean, std = statistics.fmean(history), statistics.pstdev(history)
        spike = value >= floor and (
            mean is None or std is None or value >= mean + max(self.sigma * std, self.min_jump)
        )
        if spike and metric not in self._spiking:
            self._spiking.add(metric)
            obs.drafts.append(
                EventDraft(
                    KIND_RESOURCE_SPIKE,
                    metric,
                    "warning",
                    payload={
                        "value": round(value, 1),
                        "floor": floor,
                        "baseline_mean": None if mean is None else round(mean, 1),
                        "baseline_std": None if std is None else round(std, 1),
                        "baseline_samples": len(history),
                    },
                )
            )
        elif not spike and metric in self._spiking and value < floor - RECOVERY_MARGIN:
            self._spiking.discard(metric)
            obs.drafts.append(
                EventDraft(
                    KIND_RESOURCE_RECOVERED, metric, "info", payload={"value": round(value, 1)}
                )
            )
        if metric not in self._spiking:
            history.append(value)
