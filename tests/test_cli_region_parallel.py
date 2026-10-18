"""Regression tests for the ``--region-parallel`` CLI plumbing (Issue #3054).

Phase 2 of #3045 wires four flags through the routing CLI so users can
opt into region-based parallel routing without dropping to the Python
API:

* ``--region-parallel`` (boolean, default ``False``)
* ``--partition-rows N`` (int, default ``2``)
* ``--partition-cols N`` (int, default ``2``)
* ``--max-parallel-workers N`` (int, default ``4``)

The underlying ``route_all_negotiated()`` implementation already
supports these parameters; this CLI layer only needs to plumb them
through the ``kct route`` parser (``route_cmd._route_parser`` -- since issue
#7017 ``kct route`` passes its argv straight to ``route_cmd.main``) and the
call sites in ``cli/route_cmd.py``.

These tests pin every layer so the flag never silently goes
missing — the same drift bug class addressed by ``#2620``, ``#2622``,
``#2793``, ``#2812``, ``#2817``, ``#2819``, ``#3033`` and ``#3062``.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers (mirrors the drift-test pattern from tests/test_cli_parser_drift.py
# so the test files stay structurally similar and easy to maintain together).
# ---------------------------------------------------------------------------


def _flags_from_parser(parser: argparse.ArgumentParser) -> set[str]:
    flags: set[str] = set()
    for action in parser._actions:
        for option_string in action.option_strings:
            if option_string.startswith("--"):
                flags.add(option_string)
    return flags


def _inner_route_parser_flags() -> set[str]:
    from kicad_tools.cli.route_cmd import main as route_main

    captured: dict[str, argparse.ArgumentParser] = {}
    real_parse_args = argparse.ArgumentParser.parse_args

    def fake_parse_args(self, *args, **kwargs):
        if getattr(self, "prog", "") == "kicad-tools route":
            captured["parser"] = self
            raise SystemExit(0)
        return real_parse_args(self, *args, **kwargs)

    with patch.object(argparse.ArgumentParser, "parse_args", fake_parse_args):
        with pytest.raises(SystemExit):
            route_main([])

    assert "parser" in captured, "failed to capture inner route parser"
    return _flags_from_parser(captured["parser"])


def _kct_route_parser_flags() -> set[str]:
    from kicad_tools.cli.parser import create_parser, resolve_passthrough

    # ``route`` is a passthrough stub on create_parser() (issue #7017);
    # resolve it to the parser ``kct route`` actually runs.
    main_parser = resolve_passthrough(create_parser())
    for action in main_parser._actions:
        choices = getattr(action, "choices", None)
        if choices and "route" in choices:
            return _flags_from_parser(choices["route"])
    raise AssertionError("could not find 'route' subparser on the kct parser")


def _kct_route_subparser() -> argparse.ArgumentParser:
    from kicad_tools.cli.parser import create_parser, resolve_passthrough

    # ``route`` is a passthrough stub on create_parser() (issue #7017);
    # resolve it to the parser ``kct route`` actually runs.
    main_parser = resolve_passthrough(create_parser())
    for action in main_parser._actions:
        choices = getattr(action, "choices", None)
        if choices and "route" in choices:
            return choices["route"]
    raise AssertionError("could not find 'route' subparser on the kct parser")


# ---------------------------------------------------------------------------
# Layer 1 — the ``kct route`` parser declares the flags with expected defaults
# ---------------------------------------------------------------------------


def test_kct_route_parser_declares_region_parallel_flags():
    """The four #3054 flags must be declared on the ``kct route`` parser.

    Without this, ``kct route --region-parallel`` fails with
    ``error: unrecognized arguments``.
    """
    outer = _kct_route_parser_flags()
    assert "--region-parallel" in outer, (
        "--region-parallel missing from the kct route parser (would regress #3054)"
    )
    assert "--partition-rows" in outer, (
        "--partition-rows missing from the kct route parser (would regress #3054)"
    )
    assert "--partition-cols" in outer, (
        "--partition-cols missing from the kct route parser (would regress #3054)"
    )
    assert "--max-parallel-workers" in outer, (
        "--max-parallel-workers missing from the kct route parser (would regress #3054)"
    )


def test_kct_route_parser_default_values():
    """Defaults preserve single-threaded routing bit-for-bit."""
    route_parser = _kct_route_subparser()
    args = route_parser.parse_args(["dummy.kicad_pcb"])
    assert args.region_parallel is False
    assert args.partition_rows == 2
    assert args.partition_cols == 2
    assert args.max_parallel_workers == 4


def test_kct_route_parser_accepts_region_parallel_with_overrides():
    """Setting the flag and partition values parses cleanly."""
    route_parser = _kct_route_subparser()
    args = route_parser.parse_args(
        [
            "dummy.kicad_pcb",
            "--region-parallel",
            "--partition-rows",
            "3",
            "--partition-cols",
            "4",
            "--max-parallel-workers",
            "8",
        ]
    )
    assert args.region_parallel is True
    assert args.partition_rows == 3
    assert args.partition_cols == 4
    assert args.max_parallel_workers == 8


# ---------------------------------------------------------------------------
# Layer 2 — route_cmd's parser declares the flags
# ---------------------------------------------------------------------------


def test_inner_parser_declares_region_parallel_flags():
    """``route_cmd.main`` itself (``python -m kicad_tools.cli.route_cmd``,
    the deadline worker) must accept the four flags too."""
    inner = _inner_route_parser_flags()
    assert "--region-parallel" in inner, (
        "--region-parallel missing from inner route_cmd.py parser (would regress #3054)"
    )
    assert "--partition-rows" in inner, "--partition-rows missing from inner parser"
    assert "--partition-cols" in inner, "--partition-cols missing from inner parser"
    assert "--max-parallel-workers" in inner, "--max-parallel-workers missing from inner parser"


# ---------------------------------------------------------------------------
# Layer 3 — ``kct route`` passes the flags through to route_cmd.main
# ---------------------------------------------------------------------------


def _run_kct_route_capture_argv(extra_argv: list[str]) -> list[str]:
    """Run ``kct route dummy.kicad_pcb <extra_argv>`` in-process and return the
    argv ``route_cmd.main`` receives (the router itself is stubbed out)."""
    from tests.route_cli import kct_route

    forwarded, _ = kct_route(["dummy.kicad_pcb", *extra_argv])
    return forwarded


def test_kct_route_omits_region_parallel_flags_by_default():
    """When the user does not pass the flags, ``kct route`` must not add them.

    This preserves the byte-for-byte invariant for the default code path:
    existing ``kct route board.kicad_pcb`` invocations build the exact
    same ``sub_argv`` they did before #3054 landed.
    """
    argv = _run_kct_route_capture_argv([])
    assert "--region-parallel" not in argv
    assert "--partition-rows" not in argv
    assert "--partition-cols" not in argv
    assert "--max-parallel-workers" not in argv


def test_kct_route_forwards_region_parallel_flag():
    """``--region-parallel`` (boolean) is forwarded to the inner parser."""
    argv = _run_kct_route_capture_argv(["--region-parallel"])
    assert "--region-parallel" in argv


def test_kct_route_forwards_partition_overrides_only_when_set():
    """Partition flags are only forwarded when the user overrides the
    defaults, matching the pattern used by ``--per-net-timeout`` etc.
    """
    argv = _run_kct_route_capture_argv(
        [
            "--region-parallel",
            "--partition-rows",
            "3",
            "--partition-cols",
            "4",
            "--max-parallel-workers",
            "8",
        ]
    )
    assert "--region-parallel" in argv
    # The flag/value pairs must appear adjacently in the forwarded argv.
    rows_idx = argv.index("--partition-rows")
    assert argv[rows_idx + 1] == "3"
    cols_idx = argv.index("--partition-cols")
    assert argv[cols_idx + 1] == "4"
    workers_idx = argv.index("--max-parallel-workers")
    assert argv[workers_idx + 1] == "8"


# ---------------------------------------------------------------------------
# Layer 4 — every ``route_all_negotiated`` call site forwards the kwargs
# ---------------------------------------------------------------------------


def test_all_route_all_negotiated_calls_forward_region_parallel():
    """Static check: every ``route_all_negotiated(`` call site in
    ``route_cmd.py`` must pass ``region_parallel=...`` as a kwarg.

    PR #3065 (seed plumbing) and #3058 (checkpoint_callback) both
    identified 7 distinct call sites; #3054 must reach all of them or
    the parallel speedup applies only to a subset of the routing paths
    (escalation, two-phase, adaptive, diff-pair pre-pass, etc.).

    This is a structural test rather than a behavioural one: it reads
    the source file and confirms every ``router.route_all_negotiated(``
    occurrence has a matching ``region_parallel=`` kwarg before its
    closing paren.  Cheaper and more robust than a full integration
    test that would need a real PCB.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src/kicad_tools/cli/route_cmd.py"
    text = src.read_text()

    # Find every call site.  Skip docstring/comment hits by checking
    # whether the line containing the match starts (after whitespace)
    # with ``#`` -- those are commentary, not real call sites.
    call_idx = 0
    forwarded = 0
    pos = 0
    while True:
        idx = text.find("router.route_all_negotiated(", pos)
        if idx == -1:
            break
        # Locate the start of the line for this hit.
        line_start = text.rfind("\n", 0, idx) + 1
        line_prefix = text[line_start:idx].lstrip()
        if line_prefix.startswith("#"):
            # Comment line (e.g. the module-level note at the top of
            # the file); skip without counting.
            pos = idx + 1
            continue
        call_idx += 1
        # Find the matching closing paren by tracking depth.
        depth = 0
        end = idx + len("router.route_all_negotiated(") - 1  # at the '('
        i = end
        while i < len(text):
            c = text[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        assert depth == 0, f"unbalanced parens around route_all_negotiated call near offset {idx}"
        body = text[idx:i]
        if "region_parallel=" in body:
            forwarded += 1
        pos = i + 1

    assert call_idx == 7, (
        f"Expected 7 ``router.route_all_negotiated(`` call sites in route_cmd.py "
        f"(per PR #3065 / #3058 audit), found {call_idx}.  If a new call site "
        f"was added intentionally, update this assertion."
    )
    assert forwarded == call_idx, (
        f"Only {forwarded}/{call_idx} ``route_all_negotiated`` call sites "
        f"forward ``region_parallel=``.  Every call site must forward the "
        f"flag or the --region-parallel speedup applies only to a subset of "
        f"routing paths (Issue #3054)."
    )


# ---------------------------------------------------------------------------
# Layer 5 — behavioural check: the kwarg actually reaches the router
# ---------------------------------------------------------------------------


def test_router_route_all_negotiated_signature_matches_cli_defaults():
    """Pin the bridge between CLI defaults and ``route_all_negotiated``
    parameter names + defaults.

    The CLI forwarding passes ``region_parallel=getattr(args,
    "region_parallel", False)`` etc. with hard-coded fallback defaults
    that must stay in lock-step with the router signature.  If the
    router renames a parameter, the ``getattr`` lookup silently falls
    back to the default and the user-supplied override is dropped --
    the same drift bug class as #2819.
    """
    import inspect

    # Import the router class lazily so we don't pay the heavy
    # router-module import cost for the lightweight parser tests above.
    from kicad_tools.router.core import Autorouter

    sig = inspect.signature(Autorouter.route_all_negotiated)
    assert "region_parallel" in sig.parameters, (
        "Autorouter.route_all_negotiated no longer accepts region_parallel "
        "kwarg -- #3054 plumbing will silently break"
    )
    assert "partition_rows" in sig.parameters
    assert "partition_cols" in sig.parameters
    assert "max_parallel_workers" in sig.parameters

    # Confirm defaults match what the CLI assumes (the ``getattr``
    # fallbacks in route_cmd.py rely on these specific values).
    assert sig.parameters["region_parallel"].default is False
    assert sig.parameters["partition_rows"].default == 2
    assert sig.parameters["partition_cols"].default == 2
    assert sig.parameters["max_parallel_workers"].default == 4
