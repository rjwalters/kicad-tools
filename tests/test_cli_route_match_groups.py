"""Tests for the ``--length-match-groups`` CLI flag (Epic #2661 Phase 3H).

Issue #2723.  This module covers the CLI surface around
:meth:`Autorouter.apply_match_group_tuning`:

- **AC2**: ``kct route --help`` lists ``--length-match-groups`` with a
  docstring referencing Epic #2661 Phase 3.
- **AC3**: The flag is defined in BOTH the standalone ``route_cmd.py``
  parser AND the unified ``cli/parser.py`` route subcommand.
- **AC4**: When no groups are detected the CLI does NOT crash -- the
  graceful short-circuit is exercised inline (the actual end-to-end
  invocation lives in unit tests for :meth:`apply_match_group_tuning`
  to keep the CI matrix fast).
- **Forwarding**: ``kct route`` hands the flag to ``route_cmd.main``
  verbatim (issue #7017) and never invents it.  (AC5's ordering check went
  with the old forwarding shim: pair tuning runs before group tuning by
  ``route_cmd``'s own code order, not by argv order.)
"""

from __future__ import annotations

import contextlib
import sys
from io import StringIO
from unittest.mock import patch

from tests.route_cli import kct_route, route_args

# =============================================================================
# AC2 + AC3: CLI flag is defined in both parsers
# =============================================================================


class TestFlagDefinedInBothParsers:
    """The flag must appear in both the standalone and unified parsers."""

    def test_length_match_groups_in_route_cmd_help(self):
        """``route_cmd.main(['--help'])`` lists ``--length-match-groups``."""
        from kicad_tools.cli.route_cmd import main as route_main

        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                route_main(["--help"])

        help_text = help_output.getvalue()
        assert "--length-match-groups" in help_text
        # AC2: docstring must reference Epic #2661 Phase 3.
        # argparse wraps long help text across lines, so collapse whitespace
        # before searching for the documented strings.
        collapsed = " ".join(help_text.split())
        assert "Epic #2661" in collapsed
        assert "Phase 3H" in collapsed

    def test_length_match_groups_in_unified_parser(self):
        """``kct route --length-match-groups`` is parseable via the unified parser."""

        args = route_args(["test.kicad_pcb", "--length-match-groups"])
        assert args.length_match_groups is True

    def test_length_match_groups_default_false(self):
        """When omitted, ``length_match_groups`` defaults to False."""

        args = route_args(["test.kicad_pcb"])
        assert args.length_match_groups is False


# =============================================================================
# Forwarding: kct route -> route_cmd.main
# =============================================================================


class TestForwarding:
    """``kct route`` hands the flag to the router verbatim (issue #7017)."""

    def test_length_match_groups_forwarded_when_set(self):
        forwarded, args = kct_route(["test.kicad_pcb", "--length-match-groups"])
        assert "--length-match-groups" in forwarded
        assert args.length_match_groups is True

    def test_length_match_groups_absent_when_unset(self):
        forwarded, args = kct_route(["test.kicad_pcb"])
        assert "--length-match-groups" not in forwarded
        assert args.length_match_groups is False

    def test_compatible_with_length_match_diffpairs(self):
        """Both flags can be set together; both reach the router."""
        forwarded, args = kct_route(
            [
                "test.kicad_pcb",
                "--differential-pairs",
                "--length-match-diffpairs",
                "--length-match-groups",
            ]
        )
        assert "--length-match-diffpairs" in forwarded
        assert "--length-match-groups" in forwarded
        assert args.length_match_diffpairs is True
        assert args.length_match_groups is True


# =============================================================================
# AC4: graceful no-op when no groups are detected
# =============================================================================


class TestGracefulNoOp:
    """When no groups are detected the CLI must not crash."""

    def test_no_groups_path_handled_in_route_cmd(self):
        """The route_cmd post-routing block has the explicit
        ``if not detected_groups`` short-circuit so a synthetic-test board
        with no declared groups exits cleanly with no tuning attempted.

        Read the source directly -- a unit-test against the live CLI
        would have to spin up an Autorouter with a routed board, which
        is out of scope for this fast CI-friendly test.
        """
        import inspect

        from kicad_tools.cli import route_cmd

        src = inspect.getsource(route_cmd)
        # The literal short-circuit string is the contract.  Drift here
        # means the AC4 graceful-no-op contract is broken silently.
        assert "No match groups detected; nothing to tune." in src, (
            "route_cmd.py must emit a graceful 'no groups detected' "
            "message when detect_match_groups returns empty (AC4)."
        )

    def test_phase_label_matches_connectivity_invariant(self):
        """The connectivity invariant phase label is ``length_match_groups``."""
        import inspect

        from kicad_tools.cli import route_cmd

        src = inspect.getsource(route_cmd)
        # The phase label is consumed by _enforce_connectivity_invariant_or_exit
        # and surfaced to users on regression.  AC7 depends on it being
        # consistent with the diff-pair sibling's ``length_match_diffpairs``.
        assert 'phase="length_match_groups"' in src, (
            "route_cmd.py must use phase='length_match_groups' for the "
            "connectivity invariant call (AC7)."
        )


# =============================================================================
# AC1: Without the flag, no tuning runs (preserves backward compat)
# =============================================================================


class TestBackwardCompat:
    """Without ``--length-match-groups``, ``apply_match_group_tuning`` is
    NOT invoked (AC1).
    """

    def test_apply_match_group_tuning_not_invoked_when_flag_absent(self):
        """The route_cmd dispatch is gated on
        ``getattr(args, 'length_match_groups', False)``.  This test asserts
        the gate exists by reading the source -- the same pattern as the
        diff-pair sibling at AC1.  A live test would require a routed
        board, out of scope here.
        """
        import inspect

        from kicad_tools.cli import route_cmd

        src = inspect.getsource(route_cmd)
        # The gate must be present and exact.
        assert 'getattr(args, "length_match_groups", False)' in src, (
            "route_cmd.py must gate the match-group tuning dispatch on "
            "args.length_match_groups (AC1)."
        )


# =============================================================================
# Issue #3440: loud warning when --length-match-groups lacks a net-class map
# =============================================================================


class TestMissingNetClassMapWarning:
    """``--length-match-groups`` without ``--net-class-map`` silently no-ops
    (detection consults NetClassRouting declarations exclusively when suffix
    inference is off).  Issue #3440 requires a LOUD stderr warning so the
    skew tuner can never be silently inactive.
    """

    def test_route_cmd_warns_when_net_class_map_missing(self):
        import inspect

        from kicad_tools.cli import route_cmd

        src = inspect.getsource(route_cmd)
        assert "--length-match-groups is INACTIVE" in src, (
            "route_cmd.py must warn on stderr when --length-match-groups is "
            "passed without a net-class map (Issue #3440)."
        )
        # The warning must go to stderr (visible even under --quiet).
        warn_idx = src.index("--length-match-groups is INACTIVE")
        tail = src[warn_idx : warn_idx + 600]
        assert "sys.stderr" in tail, (
            "the --length-match-groups inactivity warning must print to "
            "sys.stderr so --quiet runs still surface it"
        )


# =============================================================================
# Issue #6968: the default (--auto-layers) path runs the tuner
# =============================================================================


def _two_net_bus_board(tmp_path, *, length_critical: bool):
    """U1 -> U2 with D0 short and D1 long: a 2-member group with ~5mm skew."""
    import json

    from tests.test_auto_pour import _PCB_FOOTER, _PCB_HEADER

    def fp(ref: str, x: float, y: float, pads: list[tuple[float, float, int, str]]) -> str:
        out = (
            f'  (footprint "TestLib:TestPkg" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 -2) (layer "F.SilkS"))\n'
        )
        for i, (dx, dy, nid, name) in enumerate(pads):
            out += (
                f'    (pad "{i + 1}" smd roundrect (at {dx} {dy}) (size 1.0 1.0) '
                f'(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) '
                f'(net {nid} "{name}"))\n'
            )
        return out + "  )\n"

    pcb = _PCB_HEADER + '  (net 0 "")\n  (net 1 "D0")\n  (net 2 "D1")\n'
    pcb += fp("U1", 10, 10, [(0, 0, 1, "D0"), (0, 3, 2, "D1")])
    pcb += fp("U2", 35, 10, [(0, 0, 1, "D0"), (0, 15, 2, "D1")])
    pcb += _PCB_FOOTER
    pcb_path = tmp_path / "bus.kicad_pcb"
    pcb_path.write_text(pcb)
    nc = {
        "name": "BUS",
        "length_critical": length_critical,
        "length_match_group": "BUS",
        "length_match_tolerance_mm": 0.5,
    }
    ncm_path = tmp_path / "ncm.json"
    ncm_path.write_text(json.dumps({"D0": nc, "D1": nc}))
    return pcb_path, ncm_path


def _route(tmp_path, pcb_path, ncm_path, *extra: str) -> int:
    from kicad_tools.cli.route_cmd import main as route_main

    return route_main(
        [
            str(pcb_path),
            "-o",
            str(tmp_path / "out.kicad_pcb"),
            "--length-match-groups",
            "--net-class-map",
            str(ncm_path),
            "--skip-drc",
            *extra,
        ]
    )


class TestDefaultPathTunes:
    """``kct route --length-match-groups`` with no layer flags goes through
    ``route_with_layer_escalation``; before #6968 that path never called the
    tuner and printed no ``Length-Match Groups`` section."""

    def test_default_auto_layers_path_runs_tuner(self, tmp_path, capsys):
        pcb_path, ncm_path = _two_net_bus_board(tmp_path, length_critical=True)
        # Drive the real default path (no --no-auto-layers).
        assert _route(tmp_path, pcb_path, ncm_path) == 0
        out = capsys.readouterr().out
        assert "LAYER ESCALATION SUMMARY" in out, "default path must be layer escalation"
        assert out.count("--- Length-Match Groups") == 1
        assert "BUS: 2 members" in out
        assert "1 tuned" in out

    def test_single_attempt_path_tunes_exactly_once(self, tmp_path, capsys):
        pcb_path, ncm_path = _two_net_bus_board(tmp_path, length_critical=True)
        assert _route(tmp_path, pcb_path, ncm_path, "--no-auto-layers") == 0
        out = capsys.readouterr().out
        assert out.count("--- Length-Match Groups") == 1
        assert "1 tuned" in out


class TestAllGroupsGatedOffWarning:
    """Groups declared but every class ``length_critical=False``: warn on
    stderr (like #3440), do not engage -- the flag does not override an
    explicit per-class ``length_critical=False``."""

    def test_warns_on_stderr_even_when_quiet(self, tmp_path, capsys):
        pcb_path, ncm_path = _two_net_bus_board(tmp_path, length_critical=False)
        assert _route(tmp_path, pcb_path, ncm_path, "--quiet") == 0
        err = capsys.readouterr().err
        assert "--length-match-groups is INACTIVE" in err
        assert "length_critical=false" in err
        assert "BUS" in err

    def test_reports_measured_skew_but_does_not_tune(self, tmp_path, capsys):
        pcb_path, ncm_path = _two_net_bus_board(tmp_path, length_critical=False)
        assert _route(tmp_path, pcb_path, ncm_path) == 0
        out = capsys.readouterr().out
        assert "BUS: 2 members (0 tuned" in out
        line = next(ln for ln in out.splitlines() if ln.strip().startswith("skew:"))
        before, after = (
            float(v.strip().rstrip("m")) for v in line.split(":")[1].split("(")[0].split("->")
        )
        assert before == after > 0.5

    def test_no_warning_when_a_group_is_length_critical(self, tmp_path, capsys):
        pcb_path, ncm_path = _two_net_bus_board(tmp_path, length_critical=True)
        assert _route(tmp_path, pcb_path, ncm_path, "--quiet") == 0
        assert "INACTIVE" not in capsys.readouterr().err
