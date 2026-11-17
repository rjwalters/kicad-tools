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
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable

from kicad_tools.router.reporting import RouteAttemptResult

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
    render_d3 = getattr(args, "render_d3", None)
    if render_d3:
        try:
            out = render_d3_results(Path(render_d3))
        except (OSError, ValueError, KeyError) as exc:
            return _fail(json_mode, f"cannot render {render_d3}: {exc}")
        if json_mode:
            emit_json({"d3_report_path": str(out)})
        else:
            print(f"D3 report: {out}")
        return 0
    suite = getattr(args, "suite", None)
    if suite == "pcbworld":
        return _run_bench_pcbworld(args)
    if suite not in (None, "deeppcb"):
        return _fail(json_mode, f"unknown suite {suite!r} (expected 'deeppcb' or 'pcbworld')")
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
    else:
        boards = fetch_boards.default_boards(boards)

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
    route_fn: Callable[[list[str]], int | RouteAttemptResult] | None = None,
    opener: Any = None,
    protocol: str = "zero-touch",
    net_class_map_path: Path | None = None,
    diff_pairs: Sequence[tuple[str, str]] | None = None,
    prepared_input: Any = None,
    extra_route_args: Sequence[str] = (),
):
    """Fetch, normalize, route, and measure one board. Returns a ``BenchmarkReport``.

    ``protocol`` selects the report's tag (``"zero-touch"`` or ``"tuned"``,
    see :mod:`kicad_tools.benchmark.external`). ``net_class_map_path``, when
    given (issue #4943's ``--tuned`` protocol), is passed to ``kct route``
    as ``--net-class-map`` and engages ``--differential-pairs`` so the
    declared diff-pair classes are actually routed as coupled pairs rather
    than falling through to the default single-ended strategy.

    ``prepared_input`` (``--suite pcbworld``, issue #6331) is a prepared
    ``guide`` board (``PreparedBoard``): fetch and ``normalize_board`` are
    skipped -- the board is already stripped and ripping it up again could
    alter the task -- and the board is verified to hold no tracks or vias.

    ``route_fn`` may return a legacy integer or ``RouteAttemptResult`` with
    explicit placement metadata for this invocation, including no-output
    failures. Metadata is never recovered from old artifacts or global state.
    """
    from kicad_tools.benchmark.external import collect_report

    route: Callable[[list[str]], int | RouteAttemptResult]
    if route_fn is not None:
        route = route_fn
    else:
        from kicad_tools.cli.route_cmd import main_with_result as _route_main

        route = _route_main

    baseline = None
    if prepared_input is not None:
        normalized_path = output_dir / "prepared" / f"{spec.slug}.kicad_pcb"
        normalized_path.parent.mkdir(parents=True, exist_ok=True)
        if verbose:
            print("  guide board: skipping normalize (no rip-up), verifying no copper ...")
        _assert_no_copper(prepared_input.guide_pcb)
        shutil.copyfile(prepared_input.guide_pcb, normalized_path)
        if prepared_input.guide_pro is not None:
            shutil.copyfile(prepared_input.guide_pro, normalized_path.with_suffix(".kicad_pro"))
    else:
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

    # Named per protocol AND slug (issue #5280): a tuned and a zero-touch run
    # against the same board slug must never share -- and thus never
    # overwrite or fall back to reading -- each other's routed output.
    routed_path = output_dir / "routed" / f"{spec.slug}.{protocol}.kicad_pcb"
    routed_path.parent.mkdir(parents=True, exist_ok=True)
    # Per-attempt output ownership (issue #5280): this attempt is the only
    # writer of `routed_path` for the rest of this call. If a prior run
    # (crashed, reused output dir, ...) left a file here, its mere existence
    # must never be mistaken for THIS attempt's output -- so it is removed
    # before the router is even invoked, and existence is checked only
    # after the call below returns.
    if routed_path.exists():
        routed_path.unlink()

    route_argv = [str(normalized_path), "-o", str(routed_path), "--seed", str(seed)]
    if net_class_map_path is not None:
        # Issue #4943 ("tuned" protocol): apply the declared per-board
        # netclass/diff-pair sidecar, and enable --differential-pairs so
        # any coupled_routing=True classes in it (e.g. STRF's USB pair)
        # actually engage the coupled pathfinder rather than falling
        # through to single-ended routing.
        route_argv += ["--net-class-map", str(net_class_map_path), "--differential-pairs"]
    # Declared, recorded router options (e.g. ``--timeout`` for a bounded
    # PCBWorld run, issue #6332) -- part of the attempt's resume key.
    route_argv += list(extra_route_args)

    if verbose:
        print(f"  routing ({protocol}, seed={seed}) ...")

    wall_clock_s: float | None = None
    route_rc: int | None
    route_exc: Exception | None = None
    placement_disposition = None
    # Measure the whole attempt, including failures, only with a native backend.
    start = time.perf_counter() if backend.timing_valid else None
    try:
        result = route(route_argv)
        if isinstance(result, RouteAttemptResult):
            route_rc = result.exit_code
            placement_disposition = result.placement_disposition
        else:
            route_rc = result
    except Exception as exc:  # defensive: a router crash must not abort the whole run
        route_rc = None
        route_exc = exc
        router_note = f"router raised {type(exc).__name__}: {exc}"
    else:
        router_note = f"router exit code: {route_rc}"
    finally:
        if start is not None:
            wall_clock_s = time.perf_counter() - start

    from kicad_tools.cli.route_deadline import TIMEOUT_EXIT
    from kicad_tools.router.crosstail_advisory import GATE_EXIT_CODE

    # These are explicit router contracts. Generic nonzero exit/output absence
    # says nothing about whether search started, so never infer a preflight stop.
    route_timed_out = route_rc == TIMEOUT_EXIT or isinstance(
        route_exc, (TimeoutError, subprocess.TimeoutExpired)
    )
    route_stopped_before_routing = route_rc == GATE_EXIT_CODE

    # Evidence of THIS attempt's output -- correct regardless of whether the
    # path was previously bare or previously occupied, because it was
    # unconditionally cleared above before `route()` ran.
    output_exists = routed_path.exists()
    measured_path = routed_path if output_exists else normalized_path

    notes = [
        f"seed={seed}",
        router_note,
        (
            f"human baseline (pre-rip-up): {baseline.segments} segments, "
            f"{baseline.vias} vias, {baseline.trace_length_mm:.1f}mm, "
            f"{baseline.unrouted_pads} unrouted pads"
            if baseline is not None
            else "PCBWorld guide board: used as prepared (no normalize / rip-up)"
        ),
    ]
    if not output_exists:
        notes.append(
            "router produced no output file for this attempt -- measuring "
            "the pre-route (ripped-up) input as fallback; the completion% "
            "below reflects pre-existing/trivial connectivity, NOT new "
            "routing progress (see route_outcome / pre_route_completion "
            "in the JSON report)"
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
        # Prepared corpus boards keep their stable source/split/board ID
        # (``pcbworld/d3a/<NNNN_name>``); files on disk use the safe slug.
        board_id=prepared_input.board_id if prepared_input is not None else spec.slug,
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
        route_exit_code=route_rc,
        placement_disposition=placement_disposition,
        route_output_exists=output_exists,
        route_exception=route_exc,
        route_timed_out=route_timed_out,
        route_stopped_before_routing=route_stopped_before_routing,
        pre_route_path=normalized_path,
    )


def _assert_no_copper(pcb_path: Path) -> None:
    """Guide boards must hold zero tracks/vias; anything else is a hard error."""
    text = pcb_path.read_text(encoding="utf-8", errors="replace")
    n = len(re.findall(r"^\s*\((?:segment|via|arc)\b", text, re.MULTILINE))
    if n:
        raise BenchExternalError(
            f"{pcb_path} already contains {n} track/via item(s); the pcbworld suite "
            "does not run normalize on guide boards and requires an unrouted input"
        )


def _load_pcbworld() -> Any:
    """Import ``benchmarks/external/pcbworld.py`` (same mechanism as the other siblings)."""
    _external_dir()
    import pcbworld  # type: ignore[import-not-found]

    return pcbworld


# --------------------------------------------------------------------------
# --suite pcbworld (issues #6331, #6332)
# --------------------------------------------------------------------------

#: Published D3-A comparators, labelled by source (issue #6332). Never
#: re-scored here -- that is #5943 / #6335.
D3A_COMPARATORS: tuple[dict[str, str], ...] = (
    {
        "router": "Freerouting",
        "cp": "0.80",
        "set": "D3-A, best-of-5",
        "source": "PCBWorld paper arXiv:2607.05915 Table 3 (denominator: full split vs fair-95 "
        "not verified)",
    },
    {
        "router": "KiCadRoutingTools",
        "cp": "0.74",
        "set": "D3-A, deterministic (@5 = @1)",
        "source": "PCBWorld paper arXiv:2607.05915 Table 3",
    },
    {
        "router": "TraceMaker",
        "cp": "0.99",
        "set": "'Small (99)', single 60 s run",
        "source": "punkfab.com/blog/tracemaker-router (third-party, self-reported, possibly "
        "tuned on these boards)",
    },
)

LEDGER_DIRNAME = "attempts"
RESULTS_FILENAME = "results.json"
D3_REPORT_FILENAME = "report.d3.md"


def _attempt_identity(
    *,
    board_id: str,
    rollout_idx: int,
    seed: int,
    input_sha256: str | None,
    tool_commit: str,
    corpus: Any,
    tools: Any,
    route_config: dict[str, Any],
) -> dict[str, Any]:
    """Everything that makes an attempt's result *current* (issue #6332 resume key).

    A ledger record is reused by ``--resume`` only when every field matches;
    anything else (new tool commit, re-prepared input, different seed,
    different router options) is stale and re-attempted.
    """
    return {
        "board_id": board_id,
        "rollout_idx": rollout_idx,
        "seed": seed,
        "input_sha256": input_sha256,
        "tool_commit": tool_commit,
        "pcbworld_commit": corpus.pcbworld_commit,
        "pcbench_commit": corpus.pcbench_commit,
        "kicad_version": tools.kicad_version,
        "route_config": route_config,
    }


def _ledger_path(ledger_dir: Path, slug: str, rollout_idx: int) -> Path:
    return ledger_dir / f"{slug}.r{rollout_idx}.json"


def _human_reference(reference_pcb: Path | None) -> tuple[float | None, int | None]:
    """Wirelength (mm) and via count of the routed guide board, or ``(None, None)``."""
    if reference_pcb is None or not Path(reference_pcb).is_file():
        return None, None
    try:
        from kicad_tools.benchmark.external.metrics import measure_copper

        copper = measure_copper(reference_pcb)
    except Exception:  # a reference we cannot read is "no reference", not a failure
        return None, None
    return round(copper.wirelength_mm, 3), copper.via_count


def _classify(status: str, outcome: str | None, pw: dict[str, Any], local_clean: Any) -> str | None:
    """Coarse failure class for the verdict's failure table (None = PCBWorld pass)."""
    from kicad_tools.benchmark.external.d3 import STATUS_ROUTE_NO_OUTPUT, STATUS_SCORED

    if status == STATUS_SCORED and pw.get("clean_pass") and outcome != "timeout":
        return None
    if outcome == "timeout":
        return "route_timeout"
    if status == STATUS_ROUTE_NO_OUTPUT:
        return f"route_no_output:{outcome or 'unknown'}"
    if status != STATUS_SCORED:
        return status
    rout = pw.get("routability")
    if rout is not None and rout < 1.0:
        return "incomplete_routing"
    if (pw.get("drv_errors_only_count") or 0) > 0:
        return "drc_errors"
    return "other"


def _attempt_record(
    *,
    identity: dict[str, Any],
    report: Any,
    score: dict[str, Any] | None,
    human: tuple[float | None, int | None],
    status_override: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Compact, license-safe attempt record (no geometry, no board content)."""
    from kicad_tools.benchmark.external import ARTIFACT_SOURCE_ROUTER_OUTPUT
    from kicad_tools.benchmark.external.d3 import (
        ATTEMPT_SCHEMA,
        STATUS_ROUTE_NO_OUTPUT,
        STATUS_SCORED,
        STATUS_SCORER_UNAVAILABLE,
    )

    record: dict[str, Any] = {
        "schema": ATTEMPT_SCHEMA,
        "board_id": identity["board_id"],
        "rollout_idx": identity["rollout_idx"],
        "seed": identity["seed"],
        "identity": identity,
        "human_wirelength_mm": human[0],
        "human_vias": human[1],
    }
    if report is None:
        record.update(
            {
                "status": status_override,
                "failure_class": status_override,
                "reason": reason,
                "timing_valid": False,
            }
        )
        return record

    outcome = report.route_outcome.outcome if report.route_outcome else None
    produced = (
        report.route_outcome is not None
        and report.route_outcome.artifact_source == ARTIFACT_SOURCE_ROUTER_OUTPUT
    )
    pw: dict[str, Any] = {}
    if not produced:
        status = STATUS_ROUTE_NO_OUTPUT
    elif score and score.get("available") and score.get("clean_pass") is not None:
        status = STATUS_SCORED
        chosen = (score.get("rollouts") or [{}])[0]
        pw = {
            "clean_pass": score.get("clean_pass"),
            "routability": score.get("routability"),
            "drv_errors_only_count": score.get("drv_errors_only_count"),
            "potential_gain": score.get("potential_gain"),
            "final_potential": chosen.get("final_potential"),
            "wirelength_mm": chosen.get("wirelength_mm"),
            "via_count": chosen.get("via_count"),
        }
    else:
        status = STATUS_SCORER_UNAVAILABLE
        reason = (score or {}).get("unavailable_reason") or "PCBWorld produced no verdict"

    drc = report.kicad_cli_drc
    drc_violations = drc.violation_count if drc.ran else None
    complete = report.completion.connections_routed >= report.completion.connections_total
    if not produced:
        local_clean: bool | None = False
    elif drc_violations is None or not report.kct_check.ran:
        local_clean = None  # the stricter bar needs BOTH engines; unknown, not clean
    else:
        local_clean = bool(complete and report.kct_check.passed and drc_violations == 0)

    record.update(
        {
            "status": status,
            "route_outcome": outcome,
            "route_exit_code": report.route_outcome.exit_code if report.route_outcome else None,
            "reason": reason,
            "pcbworld": pw,
            "local": {
                "completion_pct": round(report.completion.completion_pct, 2),
                "connections_routed": report.completion.connections_routed,
                "connections_total": report.completion.connections_total,
                "kct_check_passed": report.kct_check.passed if report.kct_check.ran else None,
                "kicad_cli_drc_violations": drc_violations,
                "local_clean": local_clean,
            },
            "vias": report.copper.via_count if produced else None,
            "wirelength_mm": round(report.copper.wirelength_mm, 3) if produced else None,
            "wall_clock_s": (
                round(report.timing.wall_clock_s, 3)
                if report.timing.valid and report.timing.wall_clock_s is not None
                else None
            ),
            "timing_valid": bool(report.timing.valid),
            "backend": report.backend.backend,
            "backend_version": report.backend.version,
        }
    )
    record["failure_class"] = _classify(status, outcome, pw, local_clean)
    return record


def _host_characteristics() -> dict[str, Any]:
    import os
    import platform

    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }


def _write_d3_results(
    output_dir: Path,
    *,
    split: str,
    board_ids: list[str],
    subsets: dict[str, list[str]],
    records: list[dict[str, Any]],
    run: dict[str, Any],
    provenance: dict[str, Any],
) -> tuple[Path, Path]:
    from kicad_tools.benchmark.external import render_d3_markdown
    from kicad_tools.benchmark.external.d3 import RESULTS_SCHEMA

    results = {
        "schema": RESULTS_SCHEMA,
        "split": split,
        "run": run,
        "provenance": provenance,
        "board_ids": board_ids,
        "subsets": subsets,
        "comparators": list(D3A_COMPARATORS) if split == "d3a" else [],
        "attempts": sorted(records, key=lambda r: (r["board_id"], r["rollout_idx"])),
    }
    results_path = output_dir / RESULTS_FILENAME
    results_path.write_text(json.dumps(results, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    report_path = output_dir / D3_REPORT_FILENAME
    report_path.write_text(render_d3_markdown(results), encoding="utf-8")
    return results_path, report_path


def render_d3_results(results_path: Path) -> Path:
    """Re-render ``report.d3.md`` next to a committed ``results.json``."""
    from kicad_tools.benchmark.external import render_d3_markdown

    results = json.loads(Path(results_path).read_text(encoding="utf-8"))
    out = Path(results_path).with_name(D3_REPORT_FILENAME)
    out.write_text(render_d3_markdown(results), encoding="utf-8")
    return out


def _run_bench_pcbworld(args, *, runner: Any = None, opener: Any = None) -> int:
    """``kct bench external --suite pcbworld`` (issues #6331, #6332).

    Fetch pinned PCBWorld/PCBench into the benchmark cache, prepare the
    selected D3 boards with PCBWorld's own (external) prep, route the bare
    guide board with ``kct route`` (no normalize), measure with the local
    gates, then score with PCBWorld's own scorer. The two verdicts are
    recorded separately (``external_scores.pcbworld`` vs. ``kct_check`` /
    ``kicad_cli_drc``).

    Issue #6332 adds the publishable-run mechanics: ``--rollouts N`` (seeds
    ``seed .. seed+N-1``), ``--full-split`` with an ``--offset``/``--limit``
    window and ``--max-attempts`` for bounded invocations, and a per-attempt
    ledger that ``--resume`` reuses only when the attempt's identity (board,
    rollout, seed, input hash, tool commit, PCBWorld/PCBench pins, KiCad
    version, router options) still matches. Every manifest board ends up
    with a record per rollout -- a result or an explicit failure -- in
    ``results.json``, rendered to ``report.d3.md``.
    """
    import dataclasses

    from kicad_tools.benchmark.external import (
        PROTOCOL_ZERO_TOUCH,
        ExternalScores,
        PCBWorldScore,
        probe_backend,
        render_markdown,
    )
    from kicad_tools.benchmark.external.d3 import (
        STATUS_HARNESS_ERROR,
        STATUS_PREP_FAILED,
        STATUS_SCORED,
    )
    from kicad_tools.benchmark.external.metrics import _tool_commit

    json_mode = wants_json(args)
    if getattr(args, "tuned", False) or getattr(args, "boards", None):
        return _fail(
            json_mode, "--suite pcbworld does not support --tuned or --board (use --split/--limit)"
        )
    rollouts = int(getattr(args, "rollouts", None) or 1)
    offset = int(getattr(args, "offset", None) or 0)
    full_split = bool(getattr(args, "full_split", False))
    max_attempts = getattr(args, "max_attempts", None)
    resume = bool(getattr(args, "resume", False))
    deterministic = bool(getattr(args, "deterministic_baseline", False))
    route_timeout = getattr(args, "route_timeout", None)
    if rollouts < 1 or offset < 0 or (max_attempts is not None and max_attempts < 1):
        return _fail(json_mode, "--rollouts and --max-attempts must be >= 1, --offset >= 0")
    if deterministic and rollouts != 1:
        return _fail(
            json_mode,
            "--deterministic-baseline declares @5 = @1 (PCBWorld runs deterministic baselines "
            "once); it cannot be combined with --rollouts > 1",
        )

    try:
        fetch_boards, _normalize = _load_external_modules()
        pcbworld = _load_pcbworld()
    except BenchExternalError as exc:
        return _fail(json_mode, str(exc))

    run = runner if runner is not None else subprocess.run
    split = getattr(args, "split", None) or "d3a"
    limit = getattr(args, "limit", None)
    if limit is None and not full_split:
        limit = 3  # #6331's smoke default
    skip_fetch = getattr(args, "skip_fetch", False)

    try:
        corpus_path = (
            Path(args.manifest) if getattr(args, "manifest", None) else pcbworld.DEFAULT_CORPUS_PATH
        )
        corpus = pcbworld.load_corpus(corpus_path)
        tools = pcbworld.resolve_tools(corpus, runner=run)
        cache_dir = fetch_boards.resolve_cache_dir(
            Path(args.cache_dir) if getattr(args, "cache_dir", None) else None
        )
        fetch_kwargs = {"opener": opener} if opener is not None else {}
        # Names come from PCBWorld's own split files, so fetch PCBWorld first.
        pw_dir = pcbworld.fetch_pcbworld(corpus, cache_dir, skip_fetch=skip_fetch, **fetch_kwargs)
        all_names = pcbworld.select_boards(corpus, split, pw_dir, None)
        window_end = None if limit is None else offset + limit
        window = all_names[offset:window_end]
        manifest_names = all_names if full_split else window
        if not window:
            raise pcbworld.PCBWorldError(
                f"--offset {offset} selects no boards (split {split!r} has {len(all_names)})"
            )
        subsets: dict[str, list[str]] = {}
        for alias, (sub_split, _rel) in corpus.subsets.items():
            if sub_split == split and full_split:
                _, names = pcbworld.load_subset(corpus, alias, pw_dir)
                subsets[alias] = [f"pcbworld/{split}/{n}" for n in names if n in all_names]
        pb_dir = pcbworld.fetch_pcbench(
            corpus, cache_dir, names=window, skip_fetch=skip_fetch, **fetch_kwargs
        )
    except pcbworld.PCBWorldError as exc:
        return _fail(json_mode, str(exc))

    prep_failures: dict[str, str] = {}
    try:
        prepared = pcbworld.prepare_boards(
            corpus,
            tools,
            split=split,
            names=window,
            pcbworld_dir=pw_dir,
            pcbench_dir=pb_dir,
            work_dir=cache_dir / "pcbworld" / "work" / f"{split}-{offset}-{len(window)}",
            runner=run,
            failures=prep_failures,
        )
    except pcbworld.PCBWorldError as exc:
        # A whole prep step failed: every board of the window gets an explicit
        # failure record rather than vanishing from the denominator.
        prepared = []
        prep_failures = {f"pcbworld/{split}/{n}": str(exc) for n in window}

    output_dir = (
        Path(args.output_dir) if getattr(args, "output_dir", None) else cache_dir / "results"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger_dir = output_dir / LEDGER_DIRNAME
    if ledger_dir.exists() and not resume:
        shutil.rmtree(ledger_dir)  # a fresh run never inherits earlier attempts
    ledger_dir.mkdir(parents=True, exist_ok=True)

    base_seed = int(getattr(args, "seed", 42))
    seeds = [base_seed + i for i in range(rollouts)]
    verbose = getattr(args, "verbose", False) and not json_mode
    run_kicad_cli = not getattr(args, "skip_kicad_cli_drc", False)
    extra_route_args = ["--timeout", str(route_timeout)] if route_timeout else []
    manufacturer: str = getattr(args, "manufacturer", "jlcpcb")
    layers: int = getattr(args, "layers", 4)
    route_config: dict[str, Any] = {
        "protocol": PROTOCOL_ZERO_TOUCH,
        "manufacturer": manufacturer,
        "layers": layers,
        "route_args": extra_route_args,
        "kicad_cli_drc": run_kicad_cli,
    }
    tool_commit = _tool_commit()
    backend = probe_backend()
    if not backend.timing_valid:
        print(
            "WARNING: C++ router backend unavailable -- wall-clock timing is refused for this "
            "run (run `kct build-native`).",
            file=sys.stderr,
        )

    by_id = {b.board_id: b for b in prepared}

    def identity_for(board_id: str, idx: int, input_sha: str | None) -> dict[str, Any]:
        return _attempt_identity(
            board_id=board_id,
            rollout_idx=idx,
            seed=seeds[idx],
            input_sha256=input_sha,
            tool_commit=tool_commit,
            corpus=corpus,
            tools=tools,
            route_config=route_config,
        )

    # ---- load the ledger: keep only records whose identity is still current.
    manifest_ids = [f"pcbworld/{split}/{n}" for n in manifest_names]
    window_ids = {f"pcbworld/{split}/{n}" for n in window}
    current: dict[tuple[str, int], dict[str, Any]] = {}
    stale = 0
    for path in sorted(ledger_dir.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            key = (rec["board_id"], int(rec["rollout_idx"]))
            ident = rec["identity"]
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink()
            stale += 1
            continue
        valid = key[0] in manifest_ids and key[1] < rollouts
        if valid:
            board = by_id.get(key[0])
            # Window boards were just re-prepared: their input hash must match.
            # Out-of-window boards keep their recorded hash (validated when written).
            want_sha = board.input_sha256 if board is not None else ident.get("input_sha256")
            valid = ident == identity_for(key[0], key[1], want_sha)
        retry = (
            key[0] in window_ids
            and rec.get("status") != STATUS_SCORED
            and rec.get("status") != "route_no_output"
        )
        if not valid or retry:
            path.unlink()
            stale += 0 if (valid and retry) else 1
            continue
        current[key] = rec
    if stale:
        print(
            f"resume: discarded {stale} stale attempt record(s) (identity changed)",
            file=sys.stderr,
        )

    def store(rec: dict[str, Any]) -> None:
        board = by_id.get(rec["board_id"])
        slug = board.slug if board is not None else rec["board_id"].replace("/", "_")
        _ledger_path(ledger_dir, slug, rec["rollout_idx"]).write_text(
            json.dumps(rec, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        current[(rec["board_id"], rec["rollout_idx"])] = rec

    for board_id, reason in prep_failures.items():
        for idx in range(rollouts):
            if (board_id, idx) not in current:
                store(
                    _attempt_record(
                        identity=identity_for(board_id, idx, None),
                        report=None,
                        score=None,
                        human=(None, None),
                        status_override=STATUS_PREP_FAILED,
                        reason=reason,
                    )
                )

    humans = {b.board_id: _human_reference(b.reference_pcb) for b in prepared}
    budget = max_attempts
    final: list[Any] = []
    errors: dict[str, str] = {}
    budget_exhausted = False
    with stdout_to_stderr_when(json_mode):
        for idx in range(rollouts):
            seed = seeds[idx]
            rollout_dir = output_dir if rollouts == 1 else output_dir / f"rollout-{idx}"
            rollout_dir.mkdir(parents=True, exist_ok=True)
            reports: dict[str, Any] = {}
            for board in prepared:
                if (board.board_id, idx) in current:
                    continue  # resumed: identical identity, already measured
                if budget is not None and budget <= 0:
                    budget_exhausted = True
                    break
                if budget is not None:
                    budget -= 1
                spec = fetch_boards.BoardSpec(
                    slug=board.slug,
                    name=board.board_id,
                    repo_url=corpus.pcbench_repo_url,
                    vcs="github",
                    commit=corpus.pcbench_commit,
                    board_path=board.guide_pcb.name,
                    license=corpus.pcbench_license,
                )
                if verbose:
                    print(f"== {board.board_id} rollout {idx} (seed {seed}) ==")
                try:
                    reports[board.board_id] = _run_one_board(
                        spec,
                        fetch_boards,
                        _normalize,
                        cache_dir=cache_dir,
                        output_dir=rollout_dir,
                        seed=seed,
                        manufacturer=manufacturer,
                        layers=layers,
                        skip_fetch=True,
                        run_kicad_cli=run_kicad_cli,
                        kicad_cli_timeout=getattr(args, "kicad_cli_timeout", 300),
                        backend=backend,
                        verbose=verbose,
                        protocol=PROTOCOL_ZERO_TOUCH,
                        prepared_input=board,
                        extra_route_args=extra_route_args,
                    )
                except BenchExternalError as exc:
                    print(f"error: {board.board_id}: {exc}", file=sys.stderr)
                    errors[f"{board.board_id}#r{idx}"] = str(exc)
                    store(
                        _attempt_record(
                            identity=identity_for(board.board_id, idx, board.input_sha256),
                            report=None,
                            score=None,
                            human=humans[board.board_id],
                            status_override=STATUS_HARNESS_ERROR,
                            reason=str(exc),
                        )
                    )

            # PCBWorld's own verdict, one scorer invocation per rollout batch.
            scoring_inputs = {}
            for board_id in reports:
                board = by_id[board_id]
                routed = rollout_dir / "routed" / f"{board.slug}.{PROTOCOL_ZERO_TOUCH}.kicad_pcb"
                scoring_inputs[board_id] = (board, routed if routed.exists() else None)
            pw_scores: dict[str, dict[str, Any]] = {}
            if scoring_inputs:
                try:
                    pw_scores = pcbworld.score_boards(
                        corpus,
                        tools,
                        scoring_inputs,
                        pcbworld_dir=pw_dir,
                        scoring_dir=rollout_dir / "pcbworld-scoring",
                        seed=seed,
                        runner=run,
                    )
                except pcbworld.PCBWorldError as exc:
                    pw_scores = {
                        bid: {
                            "available": False,
                            "comparable": False,
                            "unavailable_reason": str(exc),
                            "comparable_reason": str(exc),
                        }
                        for bid in reports
                    }

            for board_id, report in reports.items():
                payload = pw_scores.get(board_id) or {
                    "available": False,
                    "comparable": False,
                    "unavailable_reason": "no PCBWorld score returned for this board",
                }
                score = PCBWorldScore.from_payload(payload)
                report = dataclasses.replace(report, external_scores=ExternalScores(pcbworld=score))
                final.append(report)
                json_path = rollout_dir / f"{by_id[board_id].slug}.{report.protocol}.json"
                report.write_json(json_path)
                store(
                    _attempt_record(
                        identity=identity_for(board_id, idx, by_id[board_id].input_sha256),
                        report=report,
                        score=payload,
                        human=humans[board_id],
                    )
                )
                cp = "unavailable" if score.clean_pass is None else str(score.clean_pass)
                print(
                    f"{board_id} r{idx}: local {report.completion.completion_pct:.1f}% complete; "
                    f"PCBWorld CP: {cp} -> {json_path}"
                )
            if budget_exhausted:
                break

        markdown_path = None
        if final:
            markdown = render_markdown(
                final, title=f"External autorouter benchmark results (pcbworld {split})"
            )
            markdown_path = output_dir / f"report.pcbworld.{split}.md"
            markdown_path.write_text(markdown, encoding="utf-8")
            if not full_split:
                print()
                print(markdown)

        results_path, d3_path = _write_d3_results(
            output_dir,
            split=split,
            board_ids=manifest_ids,
            subsets=subsets,
            records=[r for (bid, _i), r in current.items() if bid in set(manifest_ids)],
            run={
                "split": split,
                "rollouts": rollouts,
                "seeds": seeds,
                "deterministic": deterministic,
                "full_split": full_split,
                "route_config": route_config,
            },
            provenance={
                "kicad_tools_commit": tool_commit,
                "pcbworld_commit": corpus.pcbworld_commit,
                "pcbench_commit": corpus.pcbench_commit,
                "kicad_version": tools.kicad_version,
                "router_backend": backend.backend,
                "router_backend_version": backend.version,
                "router_build_version": backend.build_version,
                "timing_valid": backend.timing_valid,
                **{f"host_{k}": v for k, v in _host_characteristics().items()},
            },
        )
        if budget_exhausted:
            print(
                f"--max-attempts {max_attempts} reached; rerun with --resume to continue.",
                file=sys.stderr,
            )
        print(f"results: {results_path}\nD3 report: {d3_path}")

    if json_mode:
        emit_json(
            {
                "reports": [r.to_dict() for r in final],
                "errors": errors,
                "prep_failures": prep_failures,
                "output_dir": str(output_dir),
                "markdown_path": str(markdown_path) if markdown_path else None,
                "results_path": str(results_path),
                "d3_report_path": str(d3_path),
                "budget_exhausted": budget_exhausted,
                "success": bool(final) and not errors and not prep_failures,
            }
        )
    if not final and not current:
        return 1
    return 1 if (errors or prep_failures) else 0
