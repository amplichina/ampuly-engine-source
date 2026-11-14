"""Repeatable headless capacity baseline for the core runtime.

The modest ``ci`` profile is what pull-request tests run. ``heavy`` is opt-in
for the Windows reference host (limeng) and is not implied by a green CI run.
``soak`` is the hour-scale leak profile, also opt-in: it samples RSS, kernel
handles, file descriptors, and in-flight admissions, and fails on a leak slope
or an end-versus-start delta. ci and heavy ceilings are not the soak line.
Numbers are sampled from this process: RSS, kernel handle / file-descriptor
count, wall time, and how many admissions were actually in flight together.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

try:
    import resource
except ImportError:  # Windows has no resource module
    resource = None  # type: ignore[assignment]


# Console adapter modules. A headless baseline that imports these is not headless.
FORBIDDEN_UI_PREFIXES = (
    "ampuly.plugins.office_ui.services",
    "ampuly.plugins.office_ui.ws_handler",
    "ampuly.plugins.office_ui.api",
    "ampuly.plugins.office_ui.server",
    "ampuly.plugins.office_ui.chat_store",
    "ampuly.plugins.office_ui.agent_store",
    "ampuly.plugins.office_ui.event_adapter",
)


@dataclass(frozen=True)
class CapacityLimits:
    max_duration_s: float
    max_rss_growth_bytes: int
    max_rss_bytes: int
    max_peak_handles: int
    max_handle_delta: int


@dataclass(frozen=True)
class CapacityProfile:
    name: str
    concurrency: int
    iterations: int
    limits: dict[str, CapacityLimits]


def _limits(duration: float, growth_mib: int, absolute_mib: int, handles: int, delta: int) -> CapacityLimits:
    return CapacityLimits(
        duration,
        growth_mib * 1024 * 1024,
        absolute_mib * 1024 * 1024,
        handles,
        delta,
    )


# Linux ci ceilings sit above a fresh py3.12 sample from 2026-10-25:
# duration 0.265s, init 0.254s, workload 0.001s, rss 191.0->198.6 MiB
# (growth 7.5 MiB), fds 7->9 delta 1, peak_in_flight 4, operations 80,
# console UI modules loaded: none.
# Growth, not the absolute RSS, is the regression signal: a long pytest
# process is already large before this sample starts. The absolute cap only
# stops a runaway. Handle delta is the leak signal for the same reason.
# Windows kernel handles inside the GitHub fast-lane pytest process, py3.12.10,
# 2026-10-25: before 4602, peak 4614, after shutdown 4610, delta 8. RSS stayed
# at 516.6 MiB. Duration 2.017s, init 1.991s, workload 0.001s. The absolute
# handle cap sits above that host count; it is not the regression line.
# The absolute handle ceiling applies only when the sample starts below it
# (missing ``handles.before`` counts as 0, so older samples still take it).
# A loaded Windows desktop can already be above the cap before the sample:
# limeng ci on PR tip f3846e35 had before 14531, peak 14542, delta 8 against
# the 8192 cap. That ambient count is not a leak. Once the process starts at
# or above the cap, growth is the leak line: ``peak - before`` and the
# end-of-sample delta both have to stay within max_handle_delta (ci 48,
# heavy 64). A sample that starts under the cap and then crosses it still
# fails the absolute check. A held leak (14531 → 16000, delta far above 48)
# and a spike that is released before shutdown (peak - before above the
# delta cap) both still fail. RSS absolute/growth rules are unchanged.
_CI = {
    "linux": _limits(30.0, 256, 1200, 128, 16),
    "darwin": _limits(45.0, 320, 1200, 256, 24),
    "win32": _limits(60.0, 400, 1500, 8192, 48),
}
# First limeng heavy sample, 2026-10-25, commit 05825f1e, win32, fresh
# process. Workload was SELECT 1 x 1280 (6ms), not the persisted-row
# workload. Cleanup of ampuly.log was ignored (WinError 32) and the script
# still exited 0 under that wrapper: duration 2.101s (init 2.039 /
# workload 0.006 / shutdown 0.007), RSS 187.9->195.7 MiB peak 196.0,
# handles 259/285/288/285 delta 26, peak_in_flight 32, operations 1280,
# no console modules. The win32 absolute handle cap stays above that peak.
_HEAVY = {
    "linux": _limits(120.0, 400, 1500, 256, 24),
    "darwin": _limits(150.0, 480, 1500, 384, 32),
    "win32": _limits(180.0, 600, 1800, 12288, 64),
}

PROFILES = {
    "ci": CapacityProfile("ci", concurrency=4, iterations=20, limits=_CI),
    "heavy": CapacityProfile("heavy", concurrency=32, iterations=40, limits=_HEAVY),
}

# Soak is a separate leak line. It does not reuse or rewrite the ci/heavy
# ceilings above. The delta budget matches the ci RSS growth ceiling and the
# heavy handle delta for that platform. The slope budget spreads that same
# delta across the default hour, so a startup step inside the budget passes
# and a continuous climb through it fails. Landing exactly on the budget does
# not fail (the checks are strict ``>``).
SOAK_DEFAULT_SECONDS = 3600
SOAK_SAMPLE_EVERY_S = 60.0
SOAK_CONCURRENCY = 4


@dataclass(frozen=True)
class SoakLimits:
    max_rss_slope_bytes_per_s: float
    max_rss_delta_bytes: int
    max_handle_slope_per_s: float
    max_handle_delta: int
    max_fd_slope_per_s: float
    max_fd_delta: int
    max_in_flight_slope_per_s: float
    max_in_flight_delta: int


def _soak_limits(rss_delta_mib: int, handle_delta: int) -> SoakLimits:
    rss_delta = rss_delta_mib * 1024 * 1024
    return SoakLimits(
        rss_delta / SOAK_DEFAULT_SECONDS,
        rss_delta,
        handle_delta / SOAK_DEFAULT_SECONDS,
        handle_delta,
        handle_delta / SOAK_DEFAULT_SECONDS,
        handle_delta,
        # A steady in-flight oscillation is not a leak. A series that is still
        # climbing at the drained sample fails the delta of 0.
        0.05,
        0,
    )


SOAK_LIMITS = {
    "linux": _soak_limits(256, 24),
    "darwin": _soak_limits(320, 32),
    "win32": _soak_limits(400, 64),
}


def platform_key() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "win32"
    return sys.platform


def workload_kind(profile_name: str) -> str:
    """What one measured operation does.

    ``ci`` keeps a serialized ``SELECT 1`` so the pull-request sample stays
    small. ``heavy`` writes one row and reads it back on the engine store for
    every admission. The recorded limeng heavy numbers are the older
    SELECT 1 workload, not this persisted one.
    """
    if profile_name == "heavy":
        return "registry_admission_and_persisted_row"
    if profile_name == "soak":
        return "registry_admission_and_select1_soak"
    return "registry_admission_and_select1"


def profile_by_name(name: str) -> CapacityProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"unknown capacity profile: {name}") from exc


def rss_bytes() -> int:
    """Current resident set, or the platform peak when current is unavailable."""
    if sys.platform.startswith("linux"):
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        raise OSError("VmRSS missing from /proc/self/status")
    if sys.platform == "win32":
        return _windows_memory()
    # macOS ru_maxrss is the peak, in bytes. There is no stdlib current-RSS.
    if resource is None:
        raise OSError("RSS sampling needs resource or a Windows counter")
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def handle_count() -> int:
    """Open file descriptors, or Windows kernel handles for this process."""
    if sys.platform.startswith("linux"):
        return len(os.listdir("/proc/self/fd"))
    if sys.platform == "win32":
        return _windows_handle_count()
    return len(os.listdir("/dev/fd"))


def fd_count() -> int:
    """Open file descriptors.

    Windows has no POSIX fd table. The kernel handle count is the stand-in,
    and ``handle_kind`` on the sample says so.
    """
    if sys.platform.startswith("linux"):
        return len(os.listdir("/proc/self/fd"))
    if sys.platform == "darwin":
        return len(os.listdir("/dev/fd"))
    return handle_count()


def _windows_process() -> tuple[Any, Any]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # The current-process pseudo-handle is all-bits-one. The default ctypes
    # restype is a 32-bit int, which truncates it on 64-bit Windows.
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    return kernel32, kernel32.GetCurrentProcess()


def _windows_memory() -> int:
    import ctypes
    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = PROCESS_MEMORY_COUNTERS()
    size = ctypes.sizeof(counters)
    counters.cb = size
    kernel32, handle = _windows_process()
    # A ctypes field passed without argtypes is treated as a pointer, so the
    # size never arrives as a DWORD and the call returns false.
    getter = getattr(kernel32, "K32GetProcessMemoryInfo", None)
    if getter is None:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        getter = psapi.GetProcessMemoryInfo
    getter.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    getter.restype = wintypes.BOOL
    if not getter(handle, ctypes.byref(counters), size):
        err = ctypes.get_last_error()
        raise OSError(err, "GetProcessMemoryInfo failed")
    return int(counters.WorkingSetSize)


def _windows_handle_count() -> int:
    import ctypes
    from ctypes import wintypes

    kernel32, handle = _windows_process()
    count = wintypes.DWORD()
    kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    if not kernel32.GetProcessHandleCount(handle, ctypes.byref(count)):
        err = ctypes.get_last_error()
        raise OSError(err, "GetProcessHandleCount failed")
    return int(count.value)


def _note(peaks: dict[str, int]) -> None:
    peaks["rss"] = max(peaks["rss"], rss_bytes())
    peaks["handles"] = max(peaks["handles"], handle_count())


def _fresh_ui_modules(before: set[str]) -> list[str]:
    fresh = []
    for name in sys.modules:
        if name in before:
            continue
        for prefix in FORBIDDEN_UI_PREFIXES:
            if name == prefix or name.startswith(prefix + "."):
                fresh.append(name)
                break
    return sorted(fresh)


async def _sampler(stop: asyncio.Event, peaks: dict[str, int]) -> None:
    while not stop.is_set():
        _note(peaks)
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.05)
        except asyncio.TimeoutError:
            continue


async def measure(
    profile_name: str = "ci",
    *,
    ampuly_home: Any = None,
    soak_seconds: float | None = None,
    sample_every_s: float | None = None,
) -> dict[str, Any]:
    """Initialize one headless engine and run a fixed concurrent admission load.

    The engine home is a private directory. No console factory is constructed.
    ``soak`` keeps that engine up and samples RSS, handles, fds, and in-flight
    admissions until ``soak_seconds`` (default one hour).
    """
    if profile_name == "soak":
        return await _measure_soak(
            SOAK_DEFAULT_SECONDS if soak_seconds is None else float(soak_seconds),
            sample_every_s=SOAK_SAMPLE_EVERY_S if sample_every_s is None else float(sample_every_s),
            ampuly_home=ampuly_home,
        )
    import tempfile
    from pathlib import Path

    from ampuly.core.config import AmpulyConfig
    from ampuly.engine import AmpulyEngine

    profile = profile_by_name(profile_name)
    key = platform_key()
    if key not in profile.limits:
        raise RuntimeError(f"no capacity limits for platform {key}")
    limits = profile.limits[key]
    before_modules = set(sys.modules)
    started = time.perf_counter()
    peaks = {"rss": rss_bytes(), "handles": handle_count()}
    before = {"rss": peaks["rss"], "handles": peaks["handles"]}
    stop = asyncio.Event()
    sampler = asyncio.create_task(_sampler(stop, peaks))
    home_cm = None
    if ampuly_home is None:
        home_cm = tempfile.TemporaryDirectory(prefix="ampuly-capacity-")
        ampuly_home = Path(home_cm.name)
    else:
        ampuly_home = Path(ampuly_home)
    # Project folders default to <checkout>_workplace, which CI cannot create.
    # The baseline keeps that directory inside its private home.
    workplace = ampuly_home / "workplaces"
    workplace.mkdir(parents=True, exist_ok=True)
    previous_workplace = os.environ.get("AMPULY_WORKPLACE_ROOT")
    os.environ["AMPULY_WORKPLACE_ROOT"] = str(workplace)
    engine = AmpulyEngine(config=AmpulyConfig(), ampuly_home=ampuly_home, project_id="capacity-baseline")
    init_s = workload_s = shutdown_s = 0.0
    peak_in_flight = 0
    operations = 0
    try:
        init_mark = time.perf_counter()
        await engine.initialize()
        init_s = time.perf_counter() - init_mark
        _note(peaks)
        after_init = {"rss": rss_bytes(), "handles": handle_count()}
        registry = engine._active_task_run_registry
        project = str(engine.project_id or "capacity-baseline")
        db = engine.store._db
        db_lock = asyncio.Lock()
        flight = {"current": 0, "peak": 0}
        flight_lock = asyncio.Lock()
        barrier = asyncio.Barrier(profile.concurrency)
        persist = workload_kind(profile.name) == "registry_admission_and_persisted_row"
        if persist:
            async with db_lock:
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS capacity_admission ("
                    "id TEXT PRIMARY KEY, project_id TEXT NOT NULL, body TEXT NOT NULL)"
                )
                await db.commit()

        async def worker(index: int) -> None:
            nonlocal operations
            hold_id = f"cap-hold-{index}"
            token = registry.register(project, hold_id)
            held_flight = False
            released = False
            try:
                async with flight_lock:
                    flight["current"] += 1
                    flight["peak"] = max(flight["peak"], flight["current"])
                    held_flight = True
                await barrier.wait()
            except BaseException:
                barrier.abort()
                raise
            finally:
                if held_flight:
                    async with flight_lock:
                        flight["current"] -= 1
                released = registry.unregister(project, hold_id, token)
            if not released:
                raise RuntimeError("capacity admission was not released")
            for step in range(profile.iterations):
                task_id = f"cap-{index}-{step}"
                held = registry.register(project, task_id)
                try:
                    async with db_lock:
                        if persist:
                            await db.execute(
                                "INSERT INTO capacity_admission (id, project_id, body) "
                                "VALUES (?, ?, ?)",
                                (task_id, project, "capacity-admission"),
                            )
                            await db.commit()
                            async with db.execute(
                                "SELECT body FROM capacity_admission WHERE id = ?",
                                (task_id,),
                            ) as cursor:
                                row = await cursor.fetchone()
                            if row is None or row[0] != "capacity-admission":
                                raise RuntimeError("capacity admission row was not stored")
                        else:
                            async with db.execute("SELECT 1"):
                                pass
                finally:
                    if not registry.unregister(project, task_id, held):
                        raise RuntimeError("capacity admission was not released")
                operations += 1

        work_mark = time.perf_counter()
        await asyncio.gather(*(worker(index) for index in range(profile.concurrency)))
        workload_s = time.perf_counter() - work_mark
        peak_in_flight = flight["peak"]
        if registry.active_task_ids(project):
            raise RuntimeError("capacity workload left active admissions")
        _note(peaks)
    finally:
        shutdown_mark = time.perf_counter()
        try:
            await engine.shutdown()
        finally:
            shutdown_s = time.perf_counter() - shutdown_mark
            stop.set()
            await sampler
            if previous_workplace is None:
                os.environ.pop("AMPULY_WORKPLACE_ROOT", None)
            else:
                os.environ["AMPULY_WORKPLACE_ROOT"] = previous_workplace
            if home_cm is not None:
                home_cm.cleanup()
    after = {"rss": rss_bytes(), "handles": handle_count()}
    _note(peaks)
    duration_s = time.perf_counter() - started
    sample = {
        "profile": profile.name,
        "workload": workload_kind(profile.name),
        "platform": key,
        "python": sys.version.split()[0],
        "concurrency": profile.concurrency,
        "iterations": profile.iterations,
        "operations": operations,
        "peak_in_flight": peak_in_flight,
        "duration_s": round(duration_s, 3),
        "init_s": round(init_s, 3),
        "workload_s": round(workload_s, 3),
        "shutdown_s": round(shutdown_s, 3),
        "rss_bytes": {
            "before": before["rss"],
            "after_init": after_init["rss"],
            "peak": peaks["rss"],
            "after_shutdown": after["rss"],
        },
        "handles": {
            "before": before["handles"],
            "after_init": after_init["handles"],
            "peak": peaks["handles"],
            "after_shutdown": after["handles"],
            "delta": after["handles"] - before["handles"],
        },
        "limits": {
            "max_duration_s": limits.max_duration_s,
            "max_rss_growth_bytes": limits.max_rss_growth_bytes,
            "max_rss_bytes": limits.max_rss_bytes,
            "max_peak_handles": limits.max_peak_handles,
            "max_handle_delta": limits.max_handle_delta,
        },
        "ui_modules_loaded": _fresh_ui_modules(before_modules),
        "rss_kind": "current" if key != "darwin" else "peak_rusage",
        "handle_kind": "kernel_handles" if key == "win32" else "file_descriptors",
    }
    sample["violations"] = evaluate(sample)
    return sample


def evaluate(sample: dict[str, Any]) -> list[str]:
    """Return regression reasons. Empty means the sample is inside its ceiling."""
    limits = sample["limits"]
    reasons = []
    expected = int(sample["concurrency"]) * int(sample["iterations"])
    if int(sample["operations"]) != expected:
        reasons.append(f"operations {sample['operations']} != {expected}")
    if int(sample["peak_in_flight"]) < int(sample["concurrency"]):
        reasons.append(
            f"peak_in_flight {sample['peak_in_flight']} < concurrency {sample['concurrency']}"
        )
    if float(sample["duration_s"]) > float(limits["max_duration_s"]):
        reasons.append(f"duration {sample['duration_s']}s > {limits['max_duration_s']}s")
    rss_before = int(sample["rss_bytes"].get("before", 0))
    rss_peak = int(sample["rss_bytes"]["peak"])
    growth_limit = limits.get("max_rss_growth_bytes")
    if growth_limit is not None and rss_peak - rss_before > int(growth_limit):
        reasons.append(
            f"rss growth {rss_peak - rss_before} > {growth_limit}"
        )
    if rss_peak > int(limits["max_rss_bytes"]):
        reasons.append(f"peak rss {rss_peak} > {limits['max_rss_bytes']}")
    handles = sample["handles"]
    peak = int(handles["peak"])
    delta = int(handles["delta"])
    cap = int(limits["max_peak_handles"])
    delta_cap = int(limits["max_handle_delta"])
    # Absent before keeps the historical absolute check. A desktop that is
    # already above the cap is judged on growth, not on the ambient count.
    before = int(handles.get("before", 0))
    if before < cap:
        if peak > cap:
            reasons.append(f"peak handles {peak} > {cap}")
    else:
        growth = peak - before
        if growth > delta_cap:
            reasons.append(
                f"handle growth {growth} > {delta_cap} (ambient peak {peak} above cap {cap})"
            )
    if delta > delta_cap:
        reasons.append(f"handle delta {delta} > {delta_cap}")
    if sample.get("ui_modules_loaded"):
        reasons.append("console UI modules loaded: " + ", ".join(sample["ui_modules_loaded"]))
    return reasons


_SOAK_SERIES = (
    ("rss_bytes", "rss", "max_rss_slope_bytes_per_s", "max_rss_delta_bytes"),
    ("handles", "handle", "max_handle_slope_per_s", "max_handle_delta"),
    ("fd", "fd", "max_fd_slope_per_s", "max_fd_delta"),
    ("in_flight", "in_flight", "max_in_flight_slope_per_s", "max_in_flight_delta"),
)


def soak_in_flight(*, current: int, interval_peak: int, drained: bool) -> int:
    """In-flight value for one soak sample.

    Periodic samples report the peak since the previous sample. The drained
    end sample reports the current counter, so a run that released every
    admission ends where it started.
    """
    if drained:
        return int(current)
    return int(max(current, interval_peak))


def _slope(points: list[tuple[float, float]]) -> float:
    """Ordinary least-squares slope of y against t. Zero when time does not move."""
    count = len(points)
    if count < 2:
        return 0.0
    t_mean = sum(t for t, _ in points) / count
    y_mean = sum(y for _, y in points) / count
    numerator = 0.0
    denominator = 0.0
    for t, y in points:
        dt = t - t_mean
        numerator += dt * (y - y_mean)
        denominator += dt * dt
    if denominator == 0.0:
        return 0.0
    return numerator / denominator


def evaluate_soak(sample: dict[str, Any]) -> list[str]:
    """Leak reasons for a soak sample. Empty means slope and end delta are inside the line.

    ci/heavy samples are judged by :func:`evaluate`. This function does not read those ceilings.
    """
    points = list(sample.get("samples") or [])
    if len(points) < 2:
        return [f"soak samples {len(points)} < 2"]
    limits = sample["limits"]
    reasons = []
    for key, label, slope_key, delta_key in _SOAK_SERIES:
        series = [(float(point["t_s"]), float(point[key])) for point in points]
        slope = _slope(series)
        delta = series[-1][1] - series[0][1]
        slope_cap = float(limits[slope_key])
        delta_cap = float(limits[delta_key])
        if slope > slope_cap:
            reasons.append(f"{label} slope {slope} > {slope_cap}")
        if delta > delta_cap:
            reasons.append(f"{label} delta {delta} > {delta_cap}")
    loaded = sample.get("ui_modules_loaded") or []
    if loaded:
        reasons.append("console UI modules loaded: " + ", ".join(loaded))
    return reasons


def _soak_limit_payload(limits: SoakLimits) -> dict[str, float]:
    return {
        "max_rss_slope_bytes_per_s": limits.max_rss_slope_bytes_per_s,
        "max_rss_delta_bytes": limits.max_rss_delta_bytes,
        "max_handle_slope_per_s": limits.max_handle_slope_per_s,
        "max_handle_delta": limits.max_handle_delta,
        "max_fd_slope_per_s": limits.max_fd_slope_per_s,
        "max_fd_delta": limits.max_fd_delta,
        "max_in_flight_slope_per_s": limits.max_in_flight_slope_per_s,
        "max_in_flight_delta": limits.max_in_flight_delta,
    }


async def _measure_soak(
    soak_seconds: float,
    *,
    sample_every_s: float,
    ampuly_home: Any = None,
) -> dict[str, Any]:
    """Hold one headless engine for ``soak_seconds`` and sample the leak series.

    The workload is the ci admission (register, ``SELECT 1``, release) looped
    for the whole window. Rows are not accumulated, so database growth is not
    mistaken for a leak. The end sample is taken after the in-flight round
    drains and before shutdown.
    """
    if soak_seconds <= 0:
        raise ValueError("soak-seconds must be positive")
    if sample_every_s <= 0:
        raise ValueError("soak sample interval must be positive")
    import tempfile
    from pathlib import Path

    from ampuly.core.config import AmpulyConfig
    from ampuly.engine import AmpulyEngine

    key = platform_key()
    if key not in SOAK_LIMITS:
        raise RuntimeError(f"no capacity limits for platform {key}")
    limits = _soak_limit_payload(SOAK_LIMITS[key])
    before_modules = set(sys.modules)
    started = time.perf_counter()
    samples: list[dict[str, Any]] = []
    stop = asyncio.Event()
    home_cm = None
    if ampuly_home is None:
        home_cm = tempfile.TemporaryDirectory(prefix="ampuly-capacity-")
        ampuly_home = Path(home_cm.name)
    else:
        ampuly_home = Path(ampuly_home)
    workplace = ampuly_home / "workplaces"
    workplace.mkdir(parents=True, exist_ok=True)
    previous_workplace = os.environ.get("AMPULY_WORKPLACE_ROOT")
    os.environ["AMPULY_WORKPLACE_ROOT"] = str(workplace)
    engine = AmpulyEngine(config=AmpulyConfig(), ampuly_home=ampuly_home, project_id="capacity-baseline")
    operations = 0
    try:
        await engine.initialize()
        registry = engine._active_task_run_registry
        project = str(engine.project_id or "capacity-baseline")
        db = engine.store._db
        db_lock = asyncio.Lock()
        flight = {"current": 0, "interval_peak": 0}
        flight_lock = asyncio.Lock()

        def record(*, drained: bool = False) -> None:
            samples.append({
                "t_s": round(time.perf_counter() - started, 3),
                "rss_bytes": rss_bytes(),
                "handles": handle_count(),
                "fd": fd_count(),
                "in_flight": soak_in_flight(
                    current=int(flight["current"]),
                    interval_peak=int(flight["interval_peak"]),
                    drained=drained,
                ),
            })
            flight["interval_peak"] = int(flight["current"])

        async def sampler() -> None:
            while True:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=sample_every_s)
                    return
                except asyncio.TimeoutError:
                    record()

        async def admit(index: int, barrier: asyncio.Barrier) -> None:
            nonlocal operations
            hold_id = f"soak-hold-{index}"
            token = registry.register(project, hold_id)
            held_flight = False
            released = False
            try:
                async with flight_lock:
                    flight["current"] += 1
                    if flight["current"] > flight["interval_peak"]:
                        flight["interval_peak"] = flight["current"]
                    held_flight = True
                await barrier.wait()
            except BaseException:
                barrier.abort()
                raise
            finally:
                if held_flight:
                    async with flight_lock:
                        flight["current"] -= 1
                released = registry.unregister(project, hold_id, token)
            if not released:
                raise RuntimeError("capacity admission was not released")
            task_id = f"soak-{index}-{operations}"
            held = registry.register(project, task_id)
            try:
                async with db_lock:
                    async with db.execute("SELECT 1"):
                        pass
            finally:
                if not registry.unregister(project, task_id, held):
                    raise RuntimeError("capacity admission was not released")
            operations += 1

        async def rounds() -> None:
            while not stop.is_set():
                barrier = asyncio.Barrier(SOAK_CONCURRENCY)
                await asyncio.gather(*(admit(index, barrier) for index in range(SOAK_CONCURRENCY)))

        async def deadline() -> None:
            await asyncio.sleep(soak_seconds)
            stop.set()

        record()
        sampler_task = asyncio.create_task(sampler())
        deadline_task = asyncio.create_task(deadline())
        try:
            await rounds()
        finally:
            stop.set()
            if not deadline_task.done():
                deadline_task.cancel()
            try:
                await deadline_task
            except asyncio.CancelledError:
                pass
            await sampler_task
            record(drained=True)
        if registry.active_task_ids(project):
            raise RuntimeError("capacity workload left active admissions")
    finally:
        try:
            await engine.shutdown()
        finally:
            if previous_workplace is None:
                os.environ.pop("AMPULY_WORKPLACE_ROOT", None)
            else:
                os.environ["AMPULY_WORKPLACE_ROOT"] = previous_workplace
            if home_cm is not None:
                home_cm.cleanup()

    def summary(series_key: str) -> dict[str, float]:
        series = [(float(point["t_s"]), float(point[series_key])) for point in samples]
        start_value = series[0][1]
        end_value = series[-1][1]
        return {
            "start": start_value,
            "end": end_value,
            "delta": end_value - start_value,
            "slope_per_s": _slope(series),
        }

    sample = {
        "profile": "soak",
        "workload": workload_kind("soak"),
        "platform": key,
        "python": sys.version.split()[0],
        "soak_seconds": soak_seconds,
        "sample_every_s": sample_every_s,
        "concurrency": SOAK_CONCURRENCY,
        "operations": operations,
        "samples": samples,
        "rss_bytes": summary("rss_bytes"),
        "handles": summary("handles"),
        "fd": summary("fd"),
        "in_flight": summary("in_flight"),
        "limits": limits,
        "ui_modules_loaded": _fresh_ui_modules(before_modules),
        "rss_kind": "current" if key != "darwin" else "peak_rusage",
        "handle_kind": "kernel_handles" if key == "win32" else "file_descriptors",
        "fd_kind": "kernel_handles" if key == "win32" else "file_descriptors",
    }
    sample["in_flight"]["peak"] = max(int(point["in_flight"]) for point in samples)
    sample["violations"] = evaluate_soak(sample)
    return sample
