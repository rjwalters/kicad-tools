"""Rip-up normalization for external benchmark boards (Epic #4932 / issue #4933).

Strips existing copper (tracks + vias) from a fetched, human-routed
``.kicad_pcb`` while preserving footprint placement, net assignments,
netclasses, zones, and the board outline -- producing a route-ready input
for the benchmark harness (#4934's metrics module runs against this
output). The pre-rip-up ("human baseline") routing stats -- segment/via
counts, trace length, unrouted pads -- are captured via
:meth:`PCB.routing_status` and persisted as a JSON sidecar next to the
normalized output, so the human's original routing can also be reported
alongside the router's.

Also handles legacy (KiCad 5/6-era) board formats that this repo's parser
cannot read directly: on a load failure, shells out to
``kicad-cli pcb upgrade`` (discovered via
:func:`kicad_tools.cli.runner.find_kicad_cli`) and retries the load once.

Usage:
    uv run python benchmarks/external/normalize.py
    uv run python benchmarks/external/normalize.py --board strf
    uv run python benchmarks/external/normalize.py --input foo.kicad_pcb --output bar.kicad_pcb
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Make the sibling fetch_boards.py importable regardless of how this module
# itself was loaded (as a script, or via importlib in the test suite) --
# benchmarks/ is not a package (pyproject.toml's pythonpath is ["src"]
# only), so a bare `import fetch_boards` is not otherwise reliable.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_boards  # type: ignore[import-not-found]  # noqa: E402

from kicad_tools.schema.pcb import PCB  # noqa: E402


class NormalizeError(RuntimeError):
    """Raised when a board cannot be normalized (unreadable + unupgradeable)."""


@dataclass(frozen=True)
class BaselineStats:
    """Pre-rip-up routing stats captured from the human-routed original.

    Mirrors the subset of :meth:`PCB.routing_status` fields relevant to a
    "human baseline" report -- the raw ``nets_with_traces``/``unrouted_pads``
    collections from ``routing_status()`` are reduced to counts here since
    the sidecar is a JSON-serializable summary, not a full ratsnest dump.
    """

    segments: int
    vias: int
    trace_length_mm: float
    nets_with_traces: int
    unrouted_pads: int

    def to_dict(self) -> dict:
        return asdict(self)


def load_with_upgrade(path: Path, *, kicad_cli: Path | None = None) -> PCB:
    """Load a ``.kicad_pcb``, upgrading via ``kicad-cli`` if the direct parse fails.

    Tries :meth:`PCB.load` first. If that raises, resolves ``kicad-cli``
    (via :func:`kicad_tools.cli.runner.find_kicad_cli` unless ``kicad_cli``
    is given explicitly), runs ``kicad-cli pcb upgrade <path>`` in place to
    rewrite the board in the current KiCad file format, then retries the
    load once.

    Raises:
        NormalizeError: if the initial parse fails AND either ``kicad-cli``
            cannot be found, the upgrade subprocess itself fails, or the
            retried load still fails.
    """
    try:
        return PCB.load(path)
    except Exception as first_error:
        cli = kicad_cli
        if cli is None:
            from kicad_tools.cli.runner import find_kicad_cli

            cli = find_kicad_cli()
        if cli is None:
            raise NormalizeError(
                f"{path}: failed to parse ({first_error!r}) and kicad-cli was not "
                "found on PATH or in known install locations -- install KiCad 8+ "
                "(see README.md 'Fresh worktree checklist') or upgrade the "
                "board's file format manually before retrying."
            ) from first_error

        try:
            result = subprocess.run(
                [str(cli), "pcb", "upgrade", str(path)],
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise NormalizeError(
                f"{path}: failed to parse ({first_error!r}) and running "
                f"'{cli} pcb upgrade' failed to start: {exc}"
            ) from exc

        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise NormalizeError(
                f"{path}: failed to parse ({first_error!r}); 'kicad-cli pcb "
                f"upgrade' also failed (exit {result.returncode}): {detail}"
            ) from first_error

        try:
            return PCB.load(path)
        except Exception as second_error:
            raise NormalizeError(
                f"{path}: still failed to parse after 'kicad-cli pcb upgrade' "
                f"reported success: {second_error!r}"
            ) from second_error


def capture_baseline(pcb: PCB) -> BaselineStats:
    """Snapshot ``pcb.routing_status()`` into a JSON-serializable summary."""
    status = pcb.routing_status()
    return BaselineStats(
        segments=status["segments"],
        vias=status["vias"],
        trace_length_mm=status["trace_length_mm"],
        nets_with_traces=len(status["nets_with_traces"]),
        unrouted_pads=len(status["unrouted_pads"]),
    )


def rip_up(pcb: PCB) -> dict[str, int]:
    """Remove all existing tracks and vias from ``pcb`` in place.

    Footprints, pads, nets, netclasses, zones, and the board outline are
    left untouched -- only ``(segment ...)`` and ``(via ...)`` nodes are
    removed, via :meth:`PCB.remove_segments` / :meth:`PCB.remove_vias`.

    Returns:
        ``{"segments": n_removed, "vias": n_removed}``.
    """
    removed_segments = pcb.remove_segments(list(pcb.segments))
    removed_vias = pcb.remove_vias(list(pcb.vias))
    return {"segments": removed_segments, "vias": removed_vias}


def normalize_board(
    source_path: Path,
    output_path: Path,
    *,
    baseline_path: Path | None = None,
    kicad_cli: Path | None = None,
) -> BaselineStats:
    """Load, capture the human baseline, rip up copper, and save the result.

    Writes a JSON baseline sidecar to ``baseline_path`` (default:
    ``<output_path stem>.baseline.json`` next to ``output_path``) recording
    the pre-rip-up :meth:`PCB.routing_status` snapshot.

    Returns:
        The captured :class:`BaselineStats`.
    """
    pcb = load_with_upgrade(source_path, kicad_cli=kicad_cli)
    baseline = capture_baseline(pcb)

    rip_up(pcb)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pcb.save(output_path)

    if baseline_path is None:
        baseline_path = output_path.with_name(output_path.stem + ".baseline.json")
    baseline_path.write_text(json.dumps(baseline.to_dict(), indent=2) + "\n")

    return baseline


def _resolve_source_path(spec: fetch_boards.BoardSpec, cache_dir: Path) -> Path:
    """Where fetch_boards.fetch_board() would have written this board."""
    return Path(cache_dir / spec.slug / Path(spec.board_path).name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--board",
        action="append",
        dest="boards",
        metavar="SLUG",
        help="Normalize only this board slug (repeatable); default: all boards in the manifest",
    )
    parser.add_argument("--manifest", type=Path, default=fetch_boards.DEFAULT_MANIFEST_PATH)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Dir boards were fetched into by fetch_boards.py",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Where to write normalized boards + baseline sidecars (default: <cache-dir>/normalized)",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Normalize a single explicit file instead of the manifest",
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="Output path when --input is given"
    )
    args = parser.parse_args(argv)

    if args.input is not None:
        if args.output is None:
            parser.error("--output is required when --input is given")
        try:
            baseline = normalize_board(args.input, args.output)
        except NormalizeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"{args.output} (baseline: {baseline.segments} segments, {baseline.vias} vias)")
        return 0

    cache_dir = fetch_boards.resolve_cache_dir(args.cache_dir)
    output_dir = args.output_dir or (cache_dir / "normalized")

    boards = fetch_boards.load_manifest(args.manifest)
    if args.boards:
        missing = set(args.boards) - boards.keys()
        if missing:
            print(f"error: unknown board slug(s): {sorted(missing)}", file=sys.stderr)
            return 1
        boards = {slug: boards[slug] for slug in args.boards}

    exit_code = 0
    for slug, spec in boards.items():
        source_path = _resolve_source_path(spec, cache_dir)
        if not source_path.exists():
            print(
                f"error: {slug}: {source_path} not found -- run fetch_boards.py first",
                file=sys.stderr,
            )
            exit_code = 1
            continue

        output_path = output_dir / f"{slug}.kicad_pcb"
        try:
            baseline = normalize_board(source_path, output_path)
        except NormalizeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        print(
            f"{slug}: {output_path} (baseline: {baseline.segments} segments, {baseline.vias} vias)"
        )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
