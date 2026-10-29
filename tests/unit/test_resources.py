from __future__ import annotations

import shutil
from collections import namedtuple
from pathlib import Path

import pytest

from blackbox.collectors.resources import (
    ProcSampler,
    ResourcesCollector,
    SamplerUnavailable,
)
from blackbox.store import Store


class Scripted:
    def __init__(self, values: list[dict[str, float]]) -> None:
        self.values = values

    def sample(self) -> dict[str, float]:
        return self.values.pop(0)


def run(store: Store, collector: ResourcesCollector, clock, n: int) -> list[list[str]]:  # type: ignore[no-untyped-def]
    out = []
    for _ in range(n):
        obs = collector.collect(store, clock.advance(10))
        out.append([e.kind for e in store.commit(obs.drafts, ts_us=clock())])
    return out


def test_spike_above_floor_and_baseline_then_recovery(store: Store, clock) -> None:  # type: ignore[no-untyped-def]
    calm = [{"cpu": 20.0, "mem": 40.0}] * 6
    spike = [{"cpu": 97.0, "mem": 40.0}] * 2
    after = [{"cpu": 22.0, "mem": 40.0}]
    col = ResourcesCollector(sampler=Scripted([*calm, *spike, *after]), disk_paths=())
    kinds = run(store, col, clock, 9)
    assert kinds[:6] == [[]] * 6
    assert kinds[6] == ["resource.spike"]  # first spike sample only
    assert kinds[7] == []  # still spiking: no repeat
    assert kinds[8] == ["resource.recovered"]
    (spike_event,) = store.events(kinds=["resource.spike"])
    assert spike_event.subject == "cpu" and spike_event.payload["value"] == 97.0
    assert spike_event.payload["baseline_mean"] == 20.0


def test_always_hot_machine_does_not_alarm_forever(store: Store, clock) -> None:  # type: ignore[no-untyped-def]
    hot = [{"cpu": 88.0, "mem": 40.0}] * 12
    col = ResourcesCollector(sampler=Scripted(hot), disk_paths=())
    kinds = run(store, col, clock, 12)
    # before enough baseline exists, a floor breach is reported once; after, it is the baseline
    assert sum(k.count("resource.spike") for k in kinds) == 1


def test_below_floor_never_spikes_even_on_big_jump(store: Store, clock) -> None:  # type: ignore[no-untyped-def]
    values = [{"cpu": 5.0, "mem": 10.0}] * 6 + [{"cpu": 60.0, "mem": 10.0}]
    col = ResourcesCollector(sampler=Scripted(values), disk_paths=())
    assert sum(run(store, col, clock, 7), []) == []


def test_spike_samples_do_not_pollute_baseline(store: Store, clock) -> None:  # type: ignore[no-untyped-def]
    values = [{"cpu": 10.0, "mem": 10.0}] * 6 + [{"cpu": 99.0, "mem": 10.0}] * 5
    col = ResourcesCollector(sampler=Scripted(values), disk_paths=())
    run(store, col, clock, 11)
    assert max(col._history["cpu"]) == 10.0


def test_disk_is_threshold_only(
    store: Store,
    clock,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    usage = namedtuple("usage", "total used free")
    readings = [usage(100, 50, 50)] * 6 + [usage(100, 95, 5), usage(100, 95, 5), usage(100, 40, 60)]
    monkeypatch.setattr(shutil, "disk_usage", lambda path: readings.pop(0))
    col = ResourcesCollector(sampler=Scripted([{}] * 9), disk_paths=("/data",))
    kinds = run(store, col, clock, 9)
    assert kinds[6] == ["resource.spike"] and kinds[7] == [] and kinds[8] == ["resource.recovered"]
    assert store.events(kinds=["resource.spike"])[0].subject == "disk:/data"


def test_unreadable_disk_path_is_skipped(store: Store, clock) -> None:  # type: ignore[no-untyped-def]
    col = ResourcesCollector(sampler=Scripted([{}]), disk_paths=("/definitely/not/here",))
    assert run(store, col, clock, 1) == [[]]


def test_proc_sampler_reads_fake_proc(tmp_path: Path) -> None:
    proc = tmp_path
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\n")
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0\n")
    sampler = ProcSampler(str(proc))
    first = sampler.sample()
    assert "cpu" not in first and first["mem"] == pytest.approx(75.0)
    (proc / "stat").write_text("cpu  200 0 200 1000 0 0 0 0\n")  # +200 busy, +200 idle
    assert sampler.sample()["cpu"] == pytest.approx(50.0)


def test_proc_sampler_unavailable_off_linux(tmp_path: Path) -> None:
    with pytest.raises(SamplerUnavailable):
        ProcSampler(str(tmp_path / "nope"))


def test_default_sampler_works_here() -> None:
    from blackbox.collectors.resources import default_sampler

    sampler = default_sampler()
    sampler.sample()
    metrics = sampler.sample()
    assert 0.0 <= metrics["mem"] <= 100.0
