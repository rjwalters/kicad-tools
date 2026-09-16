"""Opt-in Linux process/cgroup observation; no concurrency or timeout policy.

python scripts/ci/native_observer.py --output NEW_DIR -- python -m pytest ...
Polling misses short-lived descendants; launch events from the pytest plugin
cover attempted direct launches, but are not evidence of successful execution.
Only allowlisted command categories are retained, never raw argv/environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path

TOKEN = "KCT_NATIVE_OBSERVER_TOKEN"
OUTPUT = "KCT_NATIVE_OBSERVER_OUTPUT"


def category(argv):
    """Classify without persisting user-controlled command text or paths."""
    words = [os.fsdecode(x) for x in argv] if isinstance(argv, (list, tuple)) else []
    executable = Path(words[0]).name if words else ""
    if executable == "kicad-cli":
        return "kicad-cli"
    if executable.startswith("python"):
        if any(Path(x).name == "_fixed_fill_worker.py" for x in words[1:]):
            return "kicad-python-fill"
        if "kicad_tools.cli" in words:
            return "python-kct"
        return "python"
    return "other"


def owner_id(value):
    """Parameter IDs may contain credentials; retain a stable digest only."""
    if not value:
        return None
    if "[" in value and "]" in value:
        start, end = value.index("["), value.rindex("]")
        digest = hashlib.sha256(value[start : end + 1].encode()).hexdigest()[:16]
        return value[:start] + "[redacted-" + digest + "]" + value[end + 1 :]
    return value


def append(path, event):
    # One writer per file; flush every record so worker death retains history.
    with path.open("a") as stream:
        stream.write(json.dumps({"time_ns": time.time_ns(), **event}) + "\n")


def read_process(directory, token):
    """PID + kernel start tick defeats PID reuse; environment is never retained."""
    first_stat = (directory / "stat").read_text()
    first_tick = first_stat[first_stat.rfind(")") + 2 :].split()[19]
    env = dict(
        item.split(b"=", 1)
        for item in (directory / "environ").read_bytes().split(b"\0")
        if b"=" in item
    )
    if env.get(TOKEN.encode()) != token.encode():
        return None
    stat = (directory / "stat").read_text()
    fields = stat[stat.rfind(")") + 2 :].split()
    argv = (directory / "cmdline").read_bytes().split(b"\0")[:-1]
    last_stat = (directory / "stat").read_text()
    last_tick = last_stat[last_stat.rfind(")") + 2 :].split()[19]
    if first_tick != fields[19] or last_tick != first_tick:
        raise OSError("PID changed while sampling")
    # PYTEST_CURRENT_TEST is pytest's inherited launch-phase marker. Never
    # infer an orphan's owner from its current/reused parent PID.
    owner = env.get(b"PYTEST_CURRENT_TEST", b"").decode(errors="replace")
    worker = env.get(b"PYTEST_XDIST_WORKER", b"").decode(errors="replace")
    return {
        "pid": int(directory.name),
        "start_ticks": int(fields[19]),
        "ppid": int(fields[1]),
        "rss_bytes": int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
        "category": category(argv),
        "owner": owner_id(owner),
        "worker": worker or None,
    }


def snapshot(proc, token):
    records, unreadable = [], 0
    for directory in proc.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            record = read_process(directory, token)
            if record:
                records.append(record)
        except (OSError, ValueError, IndexError):
            unreadable += 1
    return records, unreadable


def cgroup_directory(proc=Path("/proc")):
    """Resolve v2 mount/root; explicitly unsupported on v1/non-Linux."""
    try:
        membership = next(
            line[3:]
            for line in (proc / "self/cgroup").read_text().splitlines()
            if line.startswith("0::")
        )
        for line in (proc / "self/mountinfo").read_text().splitlines():
            before, after = line.split(" - ", 1)
            if after.split()[0] == "cgroup2":
                fields = before.split()
                root, mount = fields[3], fields[4]
                relative = Path(membership).relative_to(root)
                return Path(mount) / relative
    except (OSError, StopIteration, ValueError, IndexError):
        pass
    return None


def memory(directory):
    result = {"available": directory is not None}
    for name in ("memory.current", "memory.peak", "memory.max", "memory.events"):
        try:
            text = (directory / name).read_text().strip() if directory else None
            if text is None:
                raise OSError("unavailable")
            result[name] = (
                {k: int(v) for k, v in (line.split() for line in text.splitlines())}
                if name == "memory.events"
                else (text if text == "max" else int(text))
            )
        except (OSError, ValueError):
            result[name] = None
    return result


class Recorder:
    def __init__(self, output):
        self.output = output
        self.seen = {}
        self.live = set()

    def sample(self, records):
        live = set()
        for record in records:
            key = (record["pid"], record["start_ticks"])
            live.add(key)
            previous = self.seen.get(key)
            peak = max(record["rss_bytes"], previous["sampled_peak_rss_bytes"] if previous else 0)
            current = {**record, "sampled_peak_rss_bytes": peak}
            self.seen[key] = current
            if previous is None or current != previous:
                append(self.output, {"event": "process_sample", **current})
        for key in self.live - live:
            append(
                self.output,
                {
                    "event": "process_unobserved",
                    "pid": key[0],
                    "start_ticks": key[1],
                    "exit_code": None,
                },
            )
        self.live = live


def degraded_notice(stage):
    """Best effort, fixed vocabulary: exception text may contain secrets."""
    # A closed/full stderr must not become a second supervision failure.
    with suppress(Exception):
        print(
            f"native observer: {stage} unavailable; diagnostics are incomplete; "
            "preserving workload supervision and exit status",
            file=sys.stderr,
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not 0 < args.interval <= 10:
        parser.error("command and positive sampling interval <=10 required")
    if not Path("/proc/self/stat").exists():
        parser.error("Linux /proc is required; no workload launched")
    args.output.mkdir(parents=True, exist_ok=False)
    token = uuid.uuid4().hex
    env = dict(os.environ, **{TOKEN: token, OUTPUT: str(args.output.resolve())})
    env["PYTHONPATH"] = str(Path(__file__).parent) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTEST_PLUGINS"] = ",".join(
        filter(None, [env.get("PYTEST_PLUGINS"), "native_observer_pytest"])
    )
    cg = cgroup_directory()
    before = memory(cg)
    append(
        args.output / "observer.jsonl",
        {
            "event": "start",
            "interval_s": args.interval,
            "memory": before,
            "limitations": [
                "sampled RSS is a lower bound",
                "short-lived descendants may be missed",
                "explicit replacement environments can omit ownership",
                "process disappearance is not an exit status",
            ],
        },
    )
    recorder = Recorder(args.output / "processes.jsonl")
    child = subprocess.Popen(command, env=env)  # Inherit output, cwd and all workload timeouts.
    degraded = False
    try:
        while True:
            records, unreadable = snapshot(Path("/proc"), token)
            recorder.sample(records)
            append(
                args.output / "observer.jsonl",
                {
                    "event": "memory_sample",
                    "memory": memory(cg),
                    "unreadable_proc_entries": unreadable,
                },
            )
            if child.poll() is not None:
                break
            time.sleep(args.interval)
    except Exception:
        # Only diagnostic operations are inside this guard. Once launched,
        # the workload must remain supervised even if /proc or storage fails.
        degraded = True
        degraded_notice("sampling")
    status = child.wait()  # Authoritative status; no new deadline or cancellation.
    try:
        after = memory(cg)
        initial, final = before.get("memory.events"), after.get("memory.events")
        delta = (
            {k: final[k] - initial.get(k, 0) for k in final}
            if initial is not None and final is not None
            else None
        )
        append(
            args.output / "observer.jsonl",
            {
                "event": "finish",
                "exit_code": status,
                "memory": after,
                "memory_events_delta": delta,
                "diagnostics_degraded": degraded,
                "still_observed": [
                    {"pid": p, "start_ticks": tick} for p, tick in sorted(recorder.live)
                ],
            },
        )
    except Exception:
        degraded_notice("terminal record")
    return status if status >= 0 else 128 - status


if __name__ == "__main__":
    raise SystemExit(main())
