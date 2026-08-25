"""``kct bench external`` -- zero-touch / tuned DeepPCB-comparable benchmarks.

Epic #4932 Phase 2, issues #4941 (zero-touch) and #4943 (tuned). Drives the
full pipeline: fetch a pinned third-party board
(:mod:`benchmarks.external.fetch_boards`), rip up its existing copper while
preserving placement/netclasses (issue #4933's ``normalize.py``), route it
with kicad-tools' router, then measure the result with the Phase 1 metrics
module (:mod:`kicad_tools.benchmark.external`, issue #4934) and write a
JSON + markdown report in the schema documented at
``docs/benchmark-external-report-schema.md``.

Two protocols, always reported SEPARATELY (never merged/averaged -- Epic
#4932's stated risk register):

* **zero-touch** (default) -- RULES AS SHIPPED, no netclass or diff-pair
  tuning, mirroring DeepPCB's own "download the board, upload, route"
  vs-Quilter methodology.
* **tuned** (``--tuned``) -- applies a declared per-board netclass/
  diff-pair config (:mod:`benchmarks.external.tuned_rules`) mirroring
  DeepPCB's own case-study setup. Currently defined for STRF only; other
  boards report a clear per-board error under ``--tuned`` rather than
  silently falling back to zero-touch rules.

Distinct from the existing ``kct benchmark`` command (:mod:`kicad_tools.
benchmark`, this repo's own in-tree routing regression-test suite) --
``bench`` runs the external, human-placed, third-party boards DeepPCB
publishes numbers for.

This is a repository-local development tool, not a feature available from
a standalone pip install of kicad-tools: it dynamically imports
``benchmarks/external/fetch_boards.py`` and ``normalize.py``, which live
outside the installed ``kicad_tools`` package by Phase 1's deliberate
design (license/repo-size hygiene -- see ``benchmarks/external/README.md``).
:func:`_find_repo_root` locates them relative to the installed package's
own file, which resolves correctly for the editable/dev-checkout install
this repository always uses (see ``CLAUDE.md`` "Fresh worktree checklist");
a report explains the limitation rather than crashing with an import
traceback when they cannot be found.

**Timing is gated on the C++ router backend, exactly as
``kct build-native --check`` reports it -- BEFORE any routing pass
starts, not merely dropped afterward.** Per this project's routing-
performance convention (see the top-level ``CLAUDE.md``) and Epic #4932's
stated risk register, a Python-fallback wall-clock number is invalid and
must never reach a published comparison. When the backend is unavailable
the routing pass still runs -- completion/via/wirelength/DRC metrics do
not depend on which backend routed the board -- but no stopwatch is ever
started around it, so no timing number is even produced to discard.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable

from ..format_options import emit_json, stdout_to_stderr_when, wants_json

__all__ = ["run_bench_command"]


class BenchExternalError(RuntimeError):
    """Raised when the harness cannot produce a report at all."""


def _find_repo_root() -> Path | None:
    """Locate the kicad-tools checkout containing ``benchmarks/external/``.

    Walks up from this installed module's own file location. Returns
    ``None`` (rather than raising) when no such checkout is found -- e.g. a
    standalone pip install of kicad-tools has no ``benchmarks/`` directory.
    """
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        marker = candidate / "benchmarks" / "external" / "fetch_boards.py"
        if marker.exists():
            return candidate
    return None


def _external_dir() -> Path:
    """Resolve ``benchmarks/external/`` and ensure it is importable.

    Shared by :func:`_load_external_modules` and :func:`_load_tuned_rules`
    -- both dynamically import a sibling module from this directory (issue
    #4933's deliberate outside-the-package placement, see the module
    docstring).
    """
    repo_root = _find_repo_root()
    if repo_root is None:
        raise BenchExternalError(
            "kct bench external requires a kicad-tools git checkout -- "
            "benchmarks/external/fetch_boards.py was not found relative to "
            "the installed kicad_tools package. This command is a "
            "repository-local development tool (see "
            "benchmarks/external/README.md), not a feature of a standalone "
            "pip install."
        )
    external_dir = repo_root / "benchmarks" / "external"
    if str(external_dir) not in sys.path:
        sys.path.insert(0, str(external_dir))
    return external_dir


def _load_external_modules() -> tuple[Any, Any]:
    """Import ``benchmarks/external/fetch_boards.py`` and ``normalize.py``.

    These live outside the installed ``kicad_tools`` package by Phase 1's
    deliberate design (issue #4933). Added to ``sys.path`` the same way
    ``normalize.py`` already imports its own sibling ``fetch_boards`` --
    both this module and ``normalize`` end up sharing one
    ``sys.modules["fetch_boards"]`` entry.
    """
    _external_dir()
    import fetch_boards  # type: ignore[import-not-found]
    import normalize  # type: ignore[import-not-found]

    return fetch_boards, normalize


def _load_tuned_rules() -> Any:
    """Import ``benchmarks/external/tuned_rules.py`` (issue #4943's ``--tuned`` config).

    Same outside-the-package placement and import mechanism as
    :func:`_load_external_modules`; kept separate because it is only
    needed when ``--tuned`` is requested.
    """
    _external_dir()
    import tuned_rules  # type: ignore[import-not-found]

    return tuned_rules


def run_bench_command(args) -> int:
    """Handle ``bench`` command and subcommands."""
    subcommand = getattr(args, "bench_command", None)
    if subcommand == "external":
        return _run_bench_external(args)
    print("Usage: kct bench <command>")
    print("Commands: external")
    return 1


def _run_bench_external(args) -> int:
    from kicad_tools.benchmark.external import (
        PROTOCOL_TUNED,
        PROTOCOL_ZERO_TOUCH,
        probe_backend,
        render_markdown,
    )

    json_mode = wants_json(args)
    tuned = getattr(args, "tuned", False)
    protocol = PROTOCOL_TUNED if tuned else PROTOCOL_ZERO_TOUCH

    try:
        fetch_boards, normalize = _load_external_modules()
        tuned_rules = _load_tuned_rules() if tuned else None
    except BenchExternalError as exc:
        return _fail(json_mode, str(exc))

    manifest_path = (
        Path(args.manifest)
        if getattr(args, "manifest", None)
        else fetch_boards.DEFAULT_MANIFEST_PATH
    )
    boards = fetch_boards.load_manifest(manifest_path)

    requested = getattr(args, "boards", None)
    if requested:
        missing = set(requested) - boards.keys()
        if missing:
            return _fail(json_mode, f"unknown board slug(s): {sorted(missing)}")
        boards = {slug: boards[slug] for slug in requested}

    cache_dir = fetch_boards.resolve_cache_dir(
        Path(args.cache_dir) if getattr(args, "cache_dir", None) else None
    )
    output_dir = (
        Path(args.output_dir) if getattr(args, "output_dir", None) else cache_dir / "results"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    seed = getattr(args, "seed", 42)
    manufacturer = getattr(args, "manufacturer", "jlcpcb")
    layers = getattr(args, "layers", 4)
    skip_fetch = getattr(args, "skip_fetch", False)
    run_kicad_cli = not getattr(args, "skip_kicad_cli_drc", False)
    kicad_cli_timeout = getattr(args, "kicad_cli_timeout", 300)
    verbose = getattr(args, "verbose", False) and not json_mode

    backend = probe_backend()
    with stdout_to_stderr_when(json_mode):
        if backend.timing_valid:
            print(
                f"C++ router backend: available (version {backend.version}) "
                "-- timing will be recorded."
            )
        else:
            reason = backend.unavailable_reason or f"active backend is {backend.backend!r}"
            print(
                "WARNING: C++ router backend unavailable "
                f"({reason}). Per this project's routing-performance "
                "convention (CLAUDE.md), wall-clock timing is REFUSED for "
                "this run -- routing still proceeds so completion/via/"
                "wirelength/DRC metrics are still measured, but no timing "
                "number will be recorded. Run `kct build-native` to enable "
                "timing.",
                file=sys.stderr,
            )

    reports = []
    errors: dict[str, str] = {}
    with stdout_to_stderr_when(json_mode):
        for slug, spec in boards.items():
            if verbose:
                print(f"== {slug} ({protocol}) ==")
            try:
                net_class_map_path = None
                diff_pairs = None
                if tuned:
                    assert tuned_rules is not None  # guaranteed by `if tuned` above
                    tuned_map = tuned_rules.build_tuned_net_class_map(slug)
                    if tuned_map is None:
                        raise BenchExternalError(
                            f"no tuned netclass/diff-pair config defined for "
                            f"board {slug!r} -- the --tuned protocol currently "
                            "covers STRF only (benchmarks/external/"
                            "tuned_rules.py, Epic #4932 issue #4943). Run "
                            "without --board to restrict to boards with a "
                            "defined tuned config, or add one to tuned_rules.py."
                        )
                    net_class_map_path = output_dir / "tuned" / f"{slug}.net_class_map.json"
                    net_class_map_path.parent.mkdir(parents=True, exist_ok=True)
                    net_class_map_path.write_text(
                        json.dumps(tuned_map, indent=2) + "\n", encoding="utf-8"
                    )
                    diff_pairs = tuned_rules.diff_pairs_for(slug)

                report = _run_one_board(
                    spec,
                    fetch_boards,
                    normalize,
                    cache_dir=cache_dir,
                    output_dir=output_dir,
                    seed=seed,
                    manufacturer=manufacturer,
                    layers=layers,
                    skip_fetch=skip_fetch,
                    run_kicad_cli=run_kicad_cli,
                    kicad_cli_timeout=kicad_cli_timeout,
                    backend=backend,
                    verbose=verbose,
                    protocol=protocol,
                    net_class_map_path=net_class_map_path,
                    diff_pairs=diff_pairs,
                )
            except BenchExternalError as exc:
                print(f"error: {slug}: {exc}", file=sys.stderr)
                errors[slug] = str(exc)
                continue

            reports.append(report)
            json_path = output_dir / f"{slug}.{report.protocol}.json"
            report.write_json(json_path)
            print(
                f"{slug}: {report.completion.completion_pct:.1f}% complete, "
                f"{report.copper.via_count} vias, "
                f"{report.copper.wirelength_mm:.1f}mm -> {json_path}"
            )

        # Named per protocol (issue #4943): the tuned report must never
        # overwrite -- or be conflated with -- the zero-touch report for the
        # same output dir.
        markdown_path = None
        if reports:
            markdown = render_markdown(
                reports, title=f"External autorouter benchmark results ({protocol})"
            )
            markdown_path = output_dir / f"report.{protocol}.md"
            markdown_path.write_text(markdown, encoding="utf-8")
            print()
            print(markdown)
            print(f"markdown report: {markdown_path}")

    if json_mode:
        emit_json(
            {
                "reports": [r.to_dict() for r in reports],
                "errors": errors,
                "output_dir": str(output_dir),
                "markdown_path": str(markdown_path) if markdown_path else None,
                "success": bool(reports) and not errors,
            }
        )

    if not reports:
        if not json_mode:
            print("error: no benchmark reports were produced.", file=sys.stderr)
        return 1

    return 1 if errors else 0


def _fail(json_mode: bool, message: str) -> int:
    if json_mode:
        emit_json({"error": message})
    else:
        print(f"error: {message}", file=sys.stderr)
    return 1


def _run_one_board(
    spec: Any,
    fetch_boards: Any,
    normalize: Any,
    *,
    cache_dir: Path,
    output_dir: Path,
    seed: int,
    manufacturer: str,
    layers: int,
    skip_fetch: bool,
    run_kicad_cli: bool,
    kicad_cli_timeout: int,
    backend: Any,
    verbose: bool,
    route_fn: Callable[[list[str]], int] | None = None,
    opener: Any = None,
    protocol: str = "zero-touch",
    net_class_map_path: Path | None = None,
    diff_pairs: Sequence[tuple[str, str]] | None = None,
):
    """Fetch, normalize, route, and measure one board. Returns a ``BenchmarkReport``.

    ``protocol`` selects the report's tag (``"zero-touch"`` or ``"tuned"``,
    see :mod:`kicad_tools.benchmark.external`). ``net_class_map_path``, when
    given (issue #4943's ``--tuned`` protocol), is passed to ``kct route``
    as ``--net-class-map`` and engages ``--differential-pairs`` so the
    declared diff-pair classes are actually routed as coupled pairs rather
    than falling through to the default single-ended strategy.
    """
    from kicad_tools.benchmark.external import collect_report

    route: Callable[[list[str]], int]
    if route_fn is not None:
        route = route_fn
    else:
        from kicad_tools.cli.route_cmd import main as _route_main

        route = _route_main

    source_path = cache_dir / spec.slug / Path(spec.board_path).name
    if skip_fetch:
        if not source_path.exists():
            raise BenchExternalError(
                f"--skip-fetch given but {source_path} does not exist -- "
                "fetch it first (drop --skip-fetch, or run "
                "benchmarks/external/fetch_boards.py directly)"
            )
    else:
        if verbose:
            print(f"  fetching {spec.name} @ {spec.commit[:12]} ...")
        fetch_kwargs = {"opener": opener} if opener is not None else {}
        try:
            source_path = fetch_boards.fetch_board(spec, cache_dir, **fetch_kwargs)
        except fetch_boards.FetchError as exc:
            raise BenchExternalError(str(exc)) from exc

    normalized_path = output_dir / "normalized" / f"{spec.slug}.kicad_pcb"
    if verbose:
        print("  normalizing (rip-up copper, capture human baseline) ...")
    try:
        baseline = normalize.normalize_board(source_path, normalized_path)
    except normalize.NormalizeError as exc:
        raise BenchExternalError(str(exc)) from exc

    routed_path = output_dir / "routed" / f"{spec.slug}.kicad_pcb"
    routed_path.parent.mkdir(parents=True, exist_ok=True)
    route_argv = [str(normalized_path), "-o", str(routed_path), "--seed", str(seed)]
    if net_class_map_path is not None:
        # Issue #4943 ("tuned" protocol): apply the declared per-board
        # netclass/diff-pair sidecar, and enable --differential-pairs so
        # any coupled_routing=True classes in it (e.g. STRF's USB pair)
        # actually engage the coupled pathfinder rather than falling
        # through to single-ended routing.
        route_argv += ["--net-class-map", str(net_class_map_path), "--differential-pairs"]

    if verbose:
        print(f"  routing ({protocol}, seed={seed}) ...")

    wall_clock_s: float | None = None
    route_rc: int | None
    try:
        if backend.timing_valid:
            # Gate BEFORE measuring (Epic #4932 risk register): the
            # stopwatch is only ever started when the C++ backend is live,
            # so a Python-fallback number is never even produced.
            start = time.perf_counter()
            route_rc = route(route_argv)
            wall_clock_s = time.perf_counter() - start
        else:
            route_rc = route(route_argv)
    except Exception as exc:  # defensive: a router crash must not abort the whole run
        route_rc = None
        wall_clock_s = None
        router_note = f"router raised {type(exc).__name__}: {exc}"
    else:
        router_note = f"router exit code: {route_rc}"

    measured_path = routed_path if routed_path.exists() else normalized_path

    notes = [
        f"seed={seed}",
        router_note,
        (
            f"human baseline (pre-rip-up): {baseline.segments} segments, "
            f"{baseline.vias} vias, {baseline.trace_length_mm:.1f}mm, "
            f"{baseline.unrouted_pads} unrouted pads"
        ),
    ]
    if not routed_path.exists():
        notes.append(
            "router produced no output file -- reporting the unrouted, "
            "ripped-up board (0% complete) rather than a stale artifact"
        )
    if spec.deep_pcb_reference:
        notes.append(f"DeepPCB published reference: {spec.deep_pcb_reference}")
    if net_class_map_path is not None:
        notes.append(
            f"tuned protocol: applied declared net-class-map "
            f"({net_class_map_path}) via --net-class-map --differential-pairs "
            "-- see benchmarks/external/tuned_rules.py for the source values "
            "and schema-mapping caveats"
        )

    return collect_report(
        measured_path,
        board_id=spec.slug,
        protocol=protocol,
        board_commit=spec.commit,
        board_source=spec.repo_url,
        wall_clock_s=wall_clock_s,
        diff_pairs=diff_pairs,
        manufacturer=manufacturer,
        layers=layers,
        run_kicad_cli=run_kicad_cli,
        kicad_cli_timeout=kicad_cli_timeout,
        notes=notes,
        backend=backend,
    )
