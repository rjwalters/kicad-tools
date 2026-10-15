#!/usr/bin/env python3
"""Does ``kct route --seed`` change the routed geometry? (issue #6332).

PCBWorld's @5 protocol routes each board five times and keeps the rollout
with the largest potential gain; "deterministic baselines are run once, where
@5 and @1 coincide" (PCBWorld ``docs/METRICS.md``). Before spending 5x the
compute on a D3 split, this script measures whether ``kct route`` is such a
deterministic baseline under the active backend.

For each input board it routes the board once per seed, plus one *repeat* of
the first seed, strictly sequentially and in-process (no fan-out -- the
benchmark hosts are shared). Each routed board is reduced to a geometry
fingerprint: the sorted list of ``segment`` / ``arc`` / ``via`` items with
their coordinates, width, layers and net, with ``uuid`` / ``tstamp`` removed.
Comparing fingerprints separates two effects:

* ``seed_varies``  -- different seeds give different geometry;
* ``repeat_stable`` -- the same seed twice gives the same geometry (if not,
  the router is run-to-run nondeterministic, e.g. wall-clock budgets, and
  "variation" across seeds is not attributable to the seed).

Output is a compact JSON record (no boards, no routed artifacts) suitable for
committing under ``benchmarks/external/results/``. Usage::

    uv run python benchmarks/external/seed_variation.py \\
        --seeds 1 2 3 4 5 --timeout 300 -o seed_variation.json BOARD.kicad_pcb ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

_ITEM_RE = re.compile(r"^\s*\((segment|via|arc)\b", re.MULTILINE)
_VOLATILE_RE = re.compile(r"\((?:uuid|tstamp)\s+[^)]*\)")


def _balanced_item(text: str, start: int) -> str:
    """Return the balanced s-expression starting at ``text[start] == '('``."""
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


def geometry_items(pcb_text: str) -> list[str]:
    """Canonical, sorted copper items of a board (UUID/tstamp-free)."""
    items = []
    for m in _ITEM_RE.finditer(pcb_text):
        start = pcb_text.index("(", m.start())
        item = _VOLATILE_RE.sub("", _balanced_item(pcb_text, start))
        items.append(" ".join(item.split()))
    return sorted(items)


def geometry_fingerprint(pcb_path: Path) -> dict[str, Any]:
    items = geometry_items(pcb_path.read_text(encoding="utf-8", errors="replace"))
    digest = hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()
    return {
        "sha256": digest,
        "segments": sum(1 for i in items if i.startswith("(segment")),
        "arcs": sum(1 for i in items if i.startswith("(arc")),
        "vias": sum(1 for i in items if i.startswith("(via")),
    }


def _default_route(argv: list[str]) -> int:
    from kicad_tools.cli.route_cmd import main_with_result

    result = main_with_result(argv)
    return int(getattr(result, "exit_code", result) or 0)


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def measure(
    boards: Sequence[Path],
    seeds: Sequence[int],
    *,
    timeout: float | None,
    extra_args: Sequence[str] = (),
    route: Callable[[list[str]], int] = _default_route,
    work_dir: Path | None = None,
) -> dict[str, Any]:
    """Route each board once per seed (+ a repeat of ``seeds[0]``) and compare."""
    if not seeds:
        raise ValueError("at least one seed is required")
    plan = [(s, f"s{s}") for s in seeds] + [(seeds[0], f"s{seeds[0]}-repeat")]
    records = []
    with tempfile.TemporaryDirectory(prefix="kct-seedvar-", dir=work_dir) as tmp:
        for board in boards:
            runs = []
            for seed, label in plan:
                out = Path(tmp) / f"{board.stem}.{label}.kicad_pcb"
                argv = [str(board), "-o", str(out), "--seed", str(seed), *extra_args]
                if timeout is not None:
                    argv += ["--timeout", str(timeout)]
                t0 = time.perf_counter()
                try:
                    rc: int | None = route(argv)
                    err = None
                except Exception as exc:  # a crash is a recorded run, not an abort
                    rc, err = None, f"{type(exc).__name__}: {exc}"
                elapsed = time.perf_counter() - t0
                fp = geometry_fingerprint(out) if out.is_file() else None
                runs.append(
                    {
                        "label": label,
                        "seed": seed,
                        "exit_code": rc,
                        "error": err,
                        "wall_s": round(elapsed, 2),
                        "geometry": fp,
                    }
                )
            records.append(summarize_board(board.name, runs, seeds))
    return {
        "schema": "kct-seed-variation/1",
        "seeds": list(seeds),
        "route_extra_args": list(extra_args),
        "timeout_s": timeout,
        "boards": records,
        "verdict": overall_verdict(records),
    }


def summarize_board(name: str, runs: list[dict[str, Any]], seeds: Sequence[int]) -> dict[str, Any]:
    by_label = {r["label"]: r for r in runs}

    def digest(label: str) -> str | None:
        g = by_label[label]["geometry"]
        return g["sha256"] if g else None

    seed_digests = [digest(f"s{s}") for s in seeds]
    repeat = digest(f"s{seeds[0]}-repeat")
    complete = all(d is not None for d in seed_digests) and repeat is not None
    return {
        "board": name,
        "runs": runs,
        "distinct_geometries_across_seeds": len({d for d in seed_digests if d}),
        "seed_varies": (len(set(seed_digests)) > 1) if complete else None,
        "repeat_stable": (repeat == seed_digests[0]) if complete else None,
    }


def overall_verdict(records: list[dict[str, Any]]) -> str:
    """``deterministic`` / ``seed-dependent`` / ``nondeterministic`` / ``inconclusive``."""
    done = [r for r in records if r["seed_varies"] is not None]
    if not done or len(done) != len(records):
        return "inconclusive"
    if any(r["repeat_stable"] is False for r in done):
        return "nondeterministic"
    if any(r["seed_varies"] for r in done):
        return "seed-dependent"
    return "deterministic"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("boards", nargs="+", type=Path)
    ap.add_argument("--seeds", nargs="+", type=int, default=[1, 2, 3, 4, 5])
    ap.add_argument("--timeout", type=float, default=300.0, help="per-route --timeout (s)")
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument(
        "--route-arg",
        action="append",
        default=[],
        help="extra argument forwarded to kct route (repeatable)",
    )
    args = ap.parse_args(argv)

    from kicad_tools.benchmark.external import probe_backend

    backend = probe_backend()
    result = measure(args.boards, args.seeds, timeout=args.timeout, extra_args=args.route_arg)
    result["provenance"] = {
        "kicad_tools_commit": _git_sha(),
        "backend": backend.backend,
        "backend_version": backend.version,
        "native_backend": bool(backend.timing_valid),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for rec in result["boards"]:
        print(
            f"{rec['board']}: distinct={rec['distinct_geometries_across_seeds']} "
            f"seed_varies={rec['seed_varies']} repeat_stable={rec['repeat_stable']}"
        )
    print(f"verdict: {result['verdict']} -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
