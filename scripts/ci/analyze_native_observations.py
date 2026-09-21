"""Attribute native workloads and audit the concurrency bound (Issue #5501).

Reads the retained output of ``scripts/ci/native_observer.py`` (and, when the
gate was logging, its own permit records) and reports, per observed group:

* which tests owned native processes, how many each launched and their sampled
  peak RSS -- the attribution PR #5503 collected but did not summarise;
* the time-weighted native concurrency histogram and its maximum;
* the cgroup memory limit, sampled ``memory.current``/``memory.peak`` maxima and
  the ``memory.events`` delta, including ``oom``/``oom_kill``.

With ``--max-concurrency N`` it exits non-zero when observed native concurrency
exceeded ``N``, when a cgroup OOM kill was recorded, or when the gate reported
it had to launch unbounded -- so a run cannot look green merely because it got
lucky. Sampled RSS is a lower bound and sums of process RSS are not cgroup
usage; the cgroup counters are reported separately and never derived from RSS.

``--simulate-bound N`` replays the observed launches through an N-slot queue,
preserving each worker's measured native durations and its measured think time
between them, and reports the wall time and the per-test wait a bound of ``N``
would have cost that run. That is how the shipped bound was selected against the
pre-fix baseline instead of guessed; it is a schedule model only and says
nothing about memory.
"""

from __future__ import annotations

import argparse
import heapq
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

NATIVE_CATEGORIES = ("kicad-cli", "kicad-python-fill")
GIB = float(1 << 30)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except ValueError:
                # A worker killed mid-write can leave one truncated record;
                # every earlier record is still durable history.
                continue
    return records


def collect_processes(directory: Path) -> dict[tuple[int, int], dict[str, Any]]:
    """Fold per-sample records into one entry per (pid, kernel start tick)."""
    processes: dict[tuple[int, int], dict[str, Any]] = {}
    for record in read_jsonl(directory / "processes.jsonl"):
        if record.get("event") != "process_sample":
            continue
        key = (record["pid"], record["start_ticks"])
        entry = processes.setdefault(
            key,
            {
                "category": record["category"],
                "first_ns": record["time_ns"],
                "last_ns": record["time_ns"],
                "peak_rss_bytes": 0,
                "owner": None,
                "worker": None,
            },
        )
        entry["last_ns"] = record["time_ns"]
        entry["category"] = record["category"]
        entry["peak_rss_bytes"] = max(
            entry["peak_rss_bytes"], record.get("sampled_peak_rss_bytes") or 0
        )
        # Ownership is the launch-phase marker inherited by the child; never
        # overwrite a known owner with a later sample that lost it.
        entry["owner"] = record.get("owner") or entry["owner"]
        entry["worker"] = record.get("worker") or entry["worker"]
    return processes


def concurrency_profile(intervals: list[tuple[int, int]]) -> dict[str, Any]:
    """Time-weighted histogram of simultaneous native processes."""
    events: list[tuple[int, int]] = []
    for first, last in intervals:
        events.append((first, 1))
        events.append((last, -1))
    events.sort()
    histogram: Counter[int] = Counter()
    live = previous = 0
    maximum = 0
    for time_ns, delta in events:
        if previous:
            histogram[live] += time_ns - previous
        previous = time_ns
        live += delta
        maximum = max(maximum, live)
    total = sum(histogram.values()) or 1
    return {
        "max_observed": maximum,
        "seconds_by_concurrency": {
            str(level): round(duration / 1e9, 3) for level, duration in sorted(histogram.items())
        },
        "fraction_by_concurrency": {
            str(level): round(duration / total, 6) for level, duration in sorted(histogram.items())
        },
    }


def simulate_bound(native: list[dict[str, Any]], bound: int, top: int) -> dict[str, Any]:
    """Replay the observed launches through a ``bound``-slot FIFO queue.

    Each worker keeps its measured native durations *and* its measured think
    time between one native exiting and its next one starting, so the model
    only adds the queueing the bound would have forced. It is a schedule model:
    it predicts added wall time and per-test wait, never memory. Sampled first
    and last observations bound a process's lifetime from the inside, so the
    durations -- and therefore the predicted waits -- are lower bounds.
    """
    by_worker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in native:
        by_worker[entry["worker"] or "UNATTRIBUTED"].append(entry)
    workers: dict[str, dict[str, Any]] = {}
    for name, entries in by_worker.items():
        entries.sort(key=lambda item: item["first_ns"])
        think: list[float] = []
        previous: int | None = None
        for entry in entries:
            gap = 0.0 if previous is None else max((entry["first_ns"] - previous) / 1e9, 0.0)
            think.append(gap)
            previous = entry["last_ns"]
        workers[name] = {"entries": entries, "think": think, "index": 0}

    free = bound
    ready: list[tuple[float, int, str]] = []
    running: list[tuple[float, str]] = []
    pending: list[tuple[float, str]] = []
    sequence = 0
    for name, worker in workers.items():
        first = worker["entries"][0]["first_ns"] / 1e9 - worker["think"][0]
        heapq.heappush(ready, (first + worker["think"][0], sequence, name))
        sequence += 1
    waits: dict[str, float] = defaultdict(float)
    longest_wait = 0.0
    now = start = min(item[0] for item in ready) if ready else 0.0
    while ready or pending or running:
        options = []
        if ready:
            options.append(ready[0][0])
        if running:
            options.append(running[0][0])
        if pending and free > 0:
            options.append(now)
        if not options:
            break
        now = min(options)
        while running and running[0][0] <= now:
            finished, name = heapq.heappop(running)
            free += 1
            worker = workers[name]
            worker["index"] += 1
            if worker["index"] < len(worker["entries"]):
                heapq.heappush(ready, (finished + worker["think"][worker["index"]], sequence, name))
                sequence += 1
        while ready and ready[0][0] <= now:
            requested, _, name = heapq.heappop(ready)
            pending.append((requested, name))
        pending.sort()
        while pending and free > 0:
            requested, name = pending.pop(0)
            free -= 1
            worker = workers[name]
            entry = worker["entries"][worker["index"]]
            waited = now - requested
            waits[entry["owner"] or "UNATTRIBUTED"] += waited
            longest_wait = max(longest_wait, waited)
            duration = (entry["last_ns"] - entry["first_ns"]) / 1e9
            heapq.heappush(running, (now + duration, name))
    observed_span = (
        max(entry["last_ns"] for entry in native) - min(entry["first_ns"] for entry in native)
    ) / 1e9
    ranked = sorted(waits.items(), key=lambda item: -item[1])
    return {
        "bound": bound,
        "observed_native_span_seconds": round(observed_span, 3),
        "simulated_native_span_seconds": round(now - start, 3),
        "added_span_seconds": round((now - start) - observed_span, 3),
        "total_added_wait_seconds": round(sum(waits.values()), 3),
        "longest_single_wait_seconds": round(longest_wait, 3),
        "worst_tests_by_added_wait": [
            {"nodeid": nodeid, "added_wait_seconds": round(seconds, 3)}
            for nodeid, seconds in ranked[:top]
        ],
    }


def memory_summary(observer: list[dict[str, Any]]) -> dict[str, Any]:
    samples = [
        record["memory"]
        for record in observer
        if record.get("event") == "memory_sample" and (record.get("memory") or {}).get("available")
    ]
    finish = next((record for record in reversed(observer) if record.get("event") == "finish"), {})
    current = [sample["memory.current"] for sample in samples if sample.get("memory.current")]
    peak = [sample["memory.peak"] for sample in samples if sample.get("memory.peak")]
    limits = {sample.get("memory.max") for sample in samples}
    delta = finish.get("memory_events_delta") or {}
    return {
        "counters_available": bool(samples),
        "samples": len(samples),
        "memory_max": sorted(x for x in limits if isinstance(x, int))[-1:] or None,
        "memory_current_max_bytes": max(current, default=None),
        "memory_peak_max_bytes": max(peak, default=None),
        "memory_events_delta": delta or None,
        "oom_events": delta.get("oom"),
        "oom_kill_events": delta.get("oom_kill"),
        "limit_pressure_events": delta.get("max"),
        "diagnostics_degraded": finish.get("diagnostics_degraded"),
        "exit_code": finish.get("exit_code"),
    }


def gate_summary(directory: Path) -> dict[str, Any]:
    """Fold the gate's own permit records, when it was asked to log them."""
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("slots-*.jsonl")):
        records.extend(read_jsonl(path))
    if not records:
        return {"records": 0}
    waits = [r["waited_s"] for r in records if r.get("event") == "acquire"]
    waits.sort()
    unbounded = [r for r in records if r.get("event") == "unbounded"]
    # Issue #5572: queue time handed back to the waiting test's pytest-timeout
    # deadline. Summed per test so the report shows what the bound would
    # otherwise have charged to budgets that never covered it.
    credited: dict[str, float] = defaultdict(float)
    for record in records:
        seconds = record.get("credited_s") or 0.0
        if seconds:
            credited[str(record.get("owner"))] += seconds
    held: dict[int, int] = {}
    intervals: list[tuple[int, int]] = []
    for record in sorted(records, key=lambda r: r["time_ns"]):
        if record.get("event") == "acquire":
            held[record["pid"]] = record["time_ns"]
        elif record.get("event") == "release" and record["pid"] in held:
            intervals.append((held.pop(record["pid"]), record["time_ns"]))
    return {
        "records": len(records),
        "permits": len(waits),
        "wait_seconds_median": waits[len(waits) // 2] if waits else None,
        "wait_seconds_max": waits[-1] if waits else None,
        "unbounded_launches": len(unbounded),
        "unbounded_reasons": dict(Counter(r.get("reason") for r in unbounded)),
        "permit_concurrency": concurrency_profile(intervals) if intervals else None,
        "timeout_credit_seconds_total": round(sum(credited.values()), 6) or None,
        "timeout_credit_seconds_max_test": round(max(credited.values()), 6) if credited else None,
    }


def analyze_group(directory: Path, top: int, simulate: Sequence[int] = ()) -> dict[str, Any]:
    processes = collect_processes(directory)
    native = [entry for entry in processes.values() if entry["category"] in NATIVE_CATEGORIES]
    peaks = sorted(entry["peak_rss_bytes"] for entry in native)
    by_module_seconds: dict[str, float] = defaultdict(float)
    by_module_count: Counter[str] = Counter()
    by_test_count: Counter[str] = Counter()
    for entry in native:
        owner = entry["owner"] or "UNATTRIBUTED"
        module = owner.split("::")[0]
        by_module_seconds[module] += (entry["last_ns"] - entry["first_ns"]) / 1e9
        by_module_count[module] += 1
        by_test_count[owner] += 1
    return {
        "group": directory.name,
        "native_processes": len(native),
        "native_process_seconds": round(sum(by_module_seconds.values()), 3),
        "native_peak_rss_bytes": {
            "median": peaks[len(peaks) // 2] if peaks else None,
            "max": peaks[-1] if peaks else None,
        },
        "native_concurrency": concurrency_profile(
            [(entry["first_ns"], entry["last_ns"]) for entry in native]
        ),
        "categories": dict(Counter(entry["category"] for entry in processes.values())),
        "top_modules_by_native_seconds": [
            {
                "module": module,
                "native_seconds": round(seconds, 3),
                "native_processes": by_module_count[module],
            }
            for module, seconds in sorted(by_module_seconds.items(), key=lambda x: -x[1])[:top]
        ],
        "top_tests_by_native_processes": [
            {"nodeid": nodeid, "native_processes": count}
            for nodeid, count in by_test_count.most_common(top)
        ],
        "memory": memory_summary(read_jsonl(directory / "observer.jsonl")),
        "gate": gate_summary(directory),
        "simulated_bounds": [simulate_bound(native, bound, top) for bound in simulate if native],
    }


def verdict(groups: list[dict[str, Any]], max_concurrency: int | None) -> list[str]:
    problems = []
    for group in groups:
        name = group["group"]
        memory = group["memory"]
        if memory.get("oom_kill_events"):
            problems.append(f"{name}: {memory['oom_kill_events']} cgroup oom_kill event(s)")
        if memory.get("oom_events"):
            problems.append(f"{name}: {memory['oom_events']} cgroup oom event(s)")
        if group["gate"].get("unbounded_launches"):
            problems.append(
                f"{name}: gate launched {group['gate']['unbounded_launches']} native "
                f"process(es) unbounded ({group['gate']['unbounded_reasons']})"
            )
        if max_concurrency is not None:
            observed = group["native_concurrency"]["max_observed"]
            if observed > max_concurrency:
                problems.append(
                    f"{name}: observed native concurrency {observed} exceeds the "
                    f"configured bound {max_concurrency}"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="native-observer output root")
    parser.add_argument("--max-concurrency", type=int, default=None)
    parser.add_argument(
        "--simulate-bound",
        type=int,
        action="append",
        default=[],
        metavar="N",
        help="model what an N-slot bound would have cost this run (repeatable)",
    )
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--json", type=Path, default=None, help="write the full summary here")
    args = parser.parse_args(argv)
    if args.max_concurrency is not None and args.max_concurrency < 1:
        parser.error("--max-concurrency must be positive")
    if any(bound < 1 for bound in args.simulate_bound):
        parser.error("--simulate-bound must be positive")
    directories = sorted(
        path for path in args.root.iterdir() if path.is_dir() and (path / "observer.jsonl").exists()
    )
    if not directories:
        print(f"no observer output under {args.root}", file=sys.stderr)
        return 2
    groups = [analyze_group(directory, args.top, args.simulate_bound) for directory in directories]
    summary = {"groups": groups, "configured_max_concurrency": args.max_concurrency}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(summary, indent=2) + "\n")
    for group in groups:
        memory, concurrency = group["memory"], group["native_concurrency"]
        peak = group["native_peak_rss_bytes"]["max"]
        peak_text = f"{peak / GIB:.2f}GiB" if peak else "n/a"
        print(
            f"[{group['group']}] native={group['native_processes']} "
            f"process_seconds={group['native_process_seconds']} "
            f"max_concurrency={concurrency['max_observed']} "
            f"peak_native_rss={peak_text}"
        )
        print(
            f"    cgroup: current_max="
            f"{(memory['memory_current_max_bytes'] or 0) / GIB:.2f}GiB "
            f"peak_max={(memory['memory_peak_max_bytes'] or 0) / GIB:.2f}GiB "
            f"events={memory['memory_events_delta']} available={memory['counters_available']}"
        )
        for row in group["top_modules_by_native_seconds"][:5]:
            print(
                f"    {row['native_seconds']:8.1f}s {row['native_processes']:4d}  {row['module']}"
            )
        for model in group["simulated_bounds"]:
            worst = model["worst_tests_by_added_wait"][:1]
            print(
                f"    bound={model['bound']}: +{model['added_span_seconds']:.0f}s span, "
                f"longest single wait {model['longest_single_wait_seconds']:.1f}s, "
                f"worst test +"
                f"{worst[0]['added_wait_seconds'] if worst else 0.0:.1f}s "
                f"({worst[0]['nodeid'] if worst else 'n/a'})"
            )
    problems = verdict(groups, args.max_concurrency)
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
