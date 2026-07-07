"""CLI tests for ``kct check --net-class-map <path>``.

Issue #2684 / Epic #2556 Phase 2.5c-cli.

Verifies the new flag is wired into ``check_cmd.py`` and threaded into
the ``DRCChecker`` constructor so the diff-pair routing-continuity and
length-skew rules can fire on routed boards.

Coverage:

1. **Graceful degradation** (AC #3): the existing CLI behaviour without
   ``--net-class-map`` is unchanged -- both diff-pair rules continue to
   report 0 checks and exit cleanly.
2. **Flag accepted**: a syntactically valid sidecar is accepted, parsed,
   and threaded into ``DRCChecker`` (the rules' ``rules_checked_by_rule``
   counter moves off zero, confirming the threading actually happened).
3. **Error paths**: missing file, malformed JSON, and structurally
   invalid map all return exit code 1 with a clear stderr message.
4. **Drift-prevention** (AC: idempotent round-trip): the same map
   serialized -> loaded -> threaded yields identical engaged-pair /
   threshold context as the in-memory map would.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Minimal valid PCB with the USB_D+/USB_D- pair as routed parallel
# segments.  The diff-pair detector (suffix inference) recognizes the
# names; the engagement helper consults the net-class map to decide
# whether they're engaged.
MINIMAL_DIFFPAIR_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "USB_D+")
  (net 2 "USB_D-")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start 110 120) (end 140 120) (width 0.2) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000020"))
  (segment (start 110 120.275) (end 140 120.275) (width 0.2) (layer "F.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000021"))
)
"""


@pytest.fixture
def diffpair_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "diffpair.kicad_pcb"
    p.write_text(MINIMAL_DIFFPAIR_PCB)
    return p


@pytest.fixture
def usb_net_class_map_sidecar(tmp_path: Path) -> Path:
    """Sidecar JSON with a HighSpeed entry for the USB pair.

    Mirrors what ``boards/03-usb-joystick/generate_design.py`` writes:
    the ``coupled_routing=True`` + ``diffpair_partner`` annotations are
    the producer-side knobs that allow the engagement helper to engage
    the pair.
    """
    sidecar = {
        "USB_D+": {
            "name": "HighSpeed",
            "coupled_routing": True,
            "diffpair_partner": "USB_D-",
            "intra_pair_clearance": 0.075,
            "skew_tolerance_mm": 3.0,
        },
        "USB_D-": {
            "name": "HighSpeed",
            "coupled_routing": True,
            "diffpair_partner": "USB_D+",
            "intra_pair_clearance": 0.075,
            "skew_tolerance_mm": 3.0,
        },
    }
    p = tmp_path / "net_class_map.json"
    p.write_text(json.dumps(sidecar, indent=2))
    return p


class TestNetClassMapFlagWiring:
    """The ``--net-class-map`` flag is wired through the CLI."""

    def test_flag_absent_diffpair_rules_no_op(self, diffpair_pcb: Path, capsys):
        """Without --net-class-map both diff-pair rules check 0 (AC #3)."""
        from kicad_tools.cli.check_cmd import main

        # Issue #3750: tmp PCB has no schematic, so the meta rollup is
        # INCOMPLETE; ``--allow-incomplete`` preserves the rule-only
        # assertion (rules no-op -> no violations -> exit 0).
        result = main(
            [
                str(diffpair_pcb),
                "--format",
                "json",
                "--only",
                "diffpair_routing_continuity,diffpair_length_skew",
                "--allow-incomplete",
            ]
        )
        # No errors -> exit 0 (rules degrade to no-op, no spurious violations).
        assert result == 0
        captured = capsys.readouterr()
        # JSON should be on stdout.  The presence of the json structure
        # is enough -- we use the rules_checked_by_rule field as the
        # graceful-degradation contract.
        data = json.loads(captured.out)
        by_rule = data["summary"]["rules_checked_by_rule"]
        # Neither key need exist when the rule never invoked the counter;
        # ``.get(..., 0)`` covers both shapes.
        assert by_rule.get("diffpair_routing_continuity", 0) == 0
        assert by_rule.get("diffpair_length_skew", 0) == 0

    def test_flag_present_diffpair_rules_fire(
        self,
        diffpair_pcb: Path,
        usb_net_class_map_sidecar: Path,
        capsys,
    ):
        """With --net-class-map at least one diff-pair rule runs (AC #1, #2).

        The minimal PCB has USB_D+/USB_D- as routed parallel segments.
        With the sidecar declaring ``coupled_routing=True`` and a
        ``diffpair_partner`` on each side, the engagement helper engages
        the pair and the rules run their per-pair check -- moving the
        ``rules_checked_by_rule`` counter off zero for at least one of
        them.
        """
        from kicad_tools.cli.check_cmd import main

        result = main(
            [
                str(diffpair_pcb),
                "--format",
                "json",
                "--net-class-map",
                str(usb_net_class_map_sidecar),
                "--only",
                "diffpair_routing_continuity,diffpair_length_skew",
            ]
        )
        captured = capsys.readouterr()
        # Either rules pass (exit 0) or they fire violations (exit 2 in
        # strict mode).  Without --strict, warnings/infos do NOT bump
        # exit; only errors do.  The minimal fixture's geometry is too
        # synthetic to predict which severity fires, so we assert on
        # the per-rule counter rather than the exit code.
        assert result in (0, 2)
        data = json.loads(captured.out)
        by_rule = data["summary"]["rules_checked_by_rule"]
        # AC #1 + #2: at least ONE of the two diff-pair rule counters
        # is now positive (i.e., the rule's check() loop entered).  The
        # exact split depends on which of {skew_data, engaged_pairs} the
        # synthetic fixture yields -- both come from the same producer
        # helpers, so positivity on either confirms the threading.
        total_dp = by_rule.get("diffpair_routing_continuity", 0) + by_rule.get(
            "diffpair_length_skew", 0
        )
        assert total_dp >= 1, (
            "Expected at least one diff-pair rule to run with sidecar, "
            f"but rules_checked_by_rule={by_rule}"
        )


class TestNetClassMapErrorPaths:
    """Error paths return exit code 1 with a clear stderr message."""

    def test_missing_file_returns_1(self, diffpair_pcb: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        result = main(
            [
                str(diffpair_pcb),
                "--net-class-map",
                "/does/not/exist/net_class_map.json",
            ]
        )
        assert result == 1
        captured = capsys.readouterr()
        assert "net-class-map" in captured.err
        assert "not found" in captured.err

    def test_malformed_json_returns_1(self, diffpair_pcb: Path, tmp_path: Path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("not { valid json")
        from kicad_tools.cli.check_cmd import main

        result = main([str(diffpair_pcb), "--net-class-map", str(bad)])
        assert result == 1
        captured = capsys.readouterr()
        assert "JSON" in captured.err or "parsing" in captured.err

    def test_invalid_structure_returns_1(self, diffpair_pcb: Path, tmp_path: Path, capsys):
        """A dict-of-non-dicts or a dict-without-name returns 1."""
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"USB_D+": {"priority": 1}}))
        from kicad_tools.cli.check_cmd import main

        result = main([str(diffpair_pcb), "--net-class-map", str(bad)])
        assert result == 1
        captured = capsys.readouterr()
        assert "net-class-map" in captured.err or "invalid" in captured.err.lower()


class TestDriftPrevention:
    """Round-trip / idempotence: the sidecar reproduces the in-memory map."""

    def test_in_memory_and_sidecar_yield_same_engagement(self, tmp_path: Path):
        """Drift-prevention (AC: in-memory map vs sidecar parity).

        Build an in-memory map.  Serialize + deserialize it via the
        sidecar format.  Confirm both produce the same
        ``derive_engagement_state`` result on a routed PCB.  This is the
        byte-for-byte equality contract referenced in the issue body.
        """
        # Build the in-memory map as the autorouter would: HIGH_SPEED
        # class for both halves of the pair, with diffpair_partner set.
        from dataclasses import replace as dc_replace

        from kicad_tools.router.rules import (
            NET_CLASS_HIGH_SPEED,
            net_class_map_from_dict,
            net_class_map_to_dict,
        )
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.diffpair_engagement import derive_engagement_state

        in_memory = {
            "USB_D+": dc_replace(NET_CLASS_HIGH_SPEED, diffpair_partner="USB_D-"),
            "USB_D-": dc_replace(NET_CLASS_HIGH_SPEED, diffpair_partner="USB_D+"),
        }

        # Round-trip via JSON sidecar.
        wire = json.dumps(net_class_map_to_dict(in_memory))
        from_sidecar = net_class_map_from_dict(json.loads(wire))

        # The maps themselves are byte-equivalent.
        assert in_memory == from_sidecar

        # And both produce identical engagement state on the same PCB.
        pcb_path = tmp_path / "pcb.kicad_pcb"
        pcb_path.write_text(MINIMAL_DIFFPAIR_PCB)
        pcb = PCB.load(pcb_path)

        engaged_a, thresholds_a = derive_engagement_state(pcb, in_memory)
        engaged_b, thresholds_b = derive_engagement_state(pcb, from_sidecar)
        assert engaged_a == engaged_b
        assert thresholds_a == thresholds_b


# =============================================================================
# Issue #3440: loud warning when skew rules are inactive (no sidecar)
# =============================================================================


class TestInactiveSkewRuleWarning:
    """``kct check`` without ``--net-class-map`` degrades the skew rules to
    silent no-ops; Issue #3440 requires a LOUD stderr warning so a recipe
    that forgot the sidecar cannot sail through green.
    """

    def test_warning_on_stderr_without_sidecar(self, diffpair_pcb: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        main([str(diffpair_pcb), "--format", "summary"])
        captured = capsys.readouterr()
        assert "INACTIVE without --net-class-map" in captured.err
        assert "match_group_length_skew" in captured.err
        assert "diffpair_length_skew" in captured.err

    def test_no_warning_with_sidecar(
        self, diffpair_pcb: Path, usb_net_class_map_sidecar: Path, capsys
    ):
        from kicad_tools.cli.check_cmd import main

        main(
            [
                str(diffpair_pcb),
                "--format",
                "summary",
                "--net-class-map",
                str(usb_net_class_map_sidecar),
            ]
        )
        captured = capsys.readouterr()
        assert "INACTIVE without --net-class-map" not in captured.err

    def test_no_warning_when_skew_rules_not_selected(self, diffpair_pcb: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        main([str(diffpair_pcb), "--format", "summary", "--only", "clearance"])
        captured = capsys.readouterr()
        assert "INACTIVE without --net-class-map" not in captured.err


# ---------------------------------------------------------------------------
# Issue #3917: a match-group PCB whose group skew fires ONLY when the
# net-class-map sidecar is loaded.  Used to exercise sidecar auto-discovery
# (Defect 2), explicit-flag precedence (AC3), the malformed-sidecar
# fall-back edge case, and the AC6 "10 errors vanish" regression.
# ---------------------------------------------------------------------------
MATCHGROUP_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "DQ0")
  (net 2 "DQ1")
  (net 3 "DQ2")
  (net 4 "DQ3")
  (gr_rect (start 0 0) (end 200 200)
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (segment (start 10 10) (end 40 10) (width 0.2) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000001"))
  (segment (start 10 20) (end 40 20) (width 0.2) (layer "F.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000002"))
  (segment (start 10 30) (end 40 30) (width 0.2) (layer "F.Cu") (net 3)
    (uuid "00000000-0000-0000-0000-000000000003"))
  (segment (start 10 40) (end 100 40) (width 0.2) (layer "F.Cu") (net 4)
    (uuid "00000000-0000-0000-0000-000000000004"))
)
"""

# Equal-length variant: all four DQ nets are 30mm so the group skew is
# 0mm and the rule PASSES -- used to exercise the AC5 --verbose per-group
# INFO line on a clean board.
MATCHGROUP_PCB_EQUAL = MATCHGROUP_PCB.replace(
    '(segment (start 10 40) (end 100 40) (width 0.2) (layer "F.Cu") (net 4)',
    '(segment (start 10 40) (end 40 40) (width 0.2) (layer "F.Cu") (net 4)',
)

_DDR_ENTRY = {"name": "DDR", "length_match_group": "DDR_DATA"}
MATCHGROUP_SIDECAR = {
    "DQ0": _DDR_ENTRY,
    "DQ1": _DDR_ENTRY,
    "DQ2": _DDR_ENTRY,
    "DQ3": _DDR_ENTRY,
}


def _write_matchgroup_board(directory: Path, *, equal: bool = False) -> Path:
    pcb = directory / "ddr.kicad_pcb"
    pcb.write_text(MATCHGROUP_PCB_EQUAL if equal else MATCHGROUP_PCB)
    return pcb


def _write_matchgroup_sidecar(directory: Path) -> Path:
    scar = directory / "net_class_map.json"
    scar.write_text(json.dumps(MATCHGROUP_SIDECAR, indent=2))
    return scar


def _mg_error_count(json_out: str) -> int:
    data = json.loads(json_out)
    return sum(
        1
        for v in data["violations"]
        if v["rule_id"] == "match_group_length_skew" and v["severity"] == "error"
    )


class TestSidecarAutoLoad:
    """Issue #3917 Defect 2 / AC2, AC3: ``kct check`` auto-discovers the
    ``net_class_map.json`` sidecar written by ``kct route``."""

    def test_auto_load_when_sidecar_present(self, tmp_path: Path, capsys):
        """No ``--net-class-map`` flag, but a sibling sidecar exists ->
        auto-loaded with an INFO line, and the sidecar-gated rule fires."""
        from kicad_tools.cli.check_cmd import main

        pcb = _write_matchgroup_board(tmp_path)
        _write_matchgroup_sidecar(tmp_path)

        rc = main(
            [
                str(pcb),
                "--only",
                "match_group_length_skew",
                "--format",
                "json",
                "--allow-incomplete",
            ]
        )
        captured = capsys.readouterr()
        assert "auto-loaded net-class-map sidecar" in captured.err
        # AC2: the auto-loaded sidecar engaged the rule -> it fired.
        assert _mg_error_count(captured.out) >= 1
        # A fired blocking rule flips the exit code off 0.
        assert rc == 2

    def test_no_autoload_message_without_sidecar(self, tmp_path: Path, capsys):
        """No sibling sidecar -> no auto-load INFO line, rule stays a no-op."""
        from kicad_tools.cli.check_cmd import main

        pcb = _write_matchgroup_board(tmp_path)  # no sidecar written

        main(
            [
                str(pcb),
                "--only",
                "match_group_length_skew",
                "--format",
                "json",
                "--allow-incomplete",
            ]
        )
        captured = capsys.readouterr()
        assert "auto-loaded net-class-map sidecar" not in captured.err
        assert _mg_error_count(captured.out) == 0

    def test_explicit_flag_skips_autoprobe(self, tmp_path: Path, capsys):
        """AC3: an explicit ``--net-class-map`` wins and the auto-probe is
        skipped (no auto-load INFO line, no double-load)."""
        from kicad_tools.cli.check_cmd import main

        pcb = _write_matchgroup_board(tmp_path)
        scar = _write_matchgroup_sidecar(tmp_path)

        main(
            [
                str(pcb),
                "--only",
                "match_group_length_skew",
                "--format",
                "json",
                "--allow-incomplete",
                "--net-class-map",
                str(scar),
            ]
        )
        captured = capsys.readouterr()
        # Explicit path -> the auto-discovery INFO line must NOT appear.
        assert "auto-loaded net-class-map sidecar" not in captured.err
        # Rule still fires (explicit path loaded fine).
        assert _mg_error_count(captured.out) >= 1

    def test_malformed_auto_sidecar_falls_back(self, tmp_path: Path, capsys):
        """A malformed auto-discovered sidecar degrades gracefully: warn and
        fall back to no-sidecar behaviour, never a hard exit-1 crash."""
        from kicad_tools.cli.check_cmd import main

        pcb = _write_matchgroup_board(tmp_path)
        (tmp_path / "net_class_map.json").write_text("not { valid json")

        rc = main(
            [
                str(pcb),
                "--only",
                "match_group_length_skew",
                "--format",
                "json",
                "--allow-incomplete",
            ]
        )
        captured = capsys.readouterr()
        # Not a tool-level failure (exit 1 is reserved for those).
        assert rc != 1
        assert "malformed net-class-map sidecar" in captured.err
        # Fell back to no-sidecar -> rule is a no-op.
        assert _mg_error_count(captured.out) == 0


class TestSidecarWrittenByRoute:
    """Issue #3917 Defect 1: the route step persists the net-class map as a
    sidecar next to the routed PCB via ``_write_net_class_map_sidecar``."""

    def test_sidecar_written_and_round_trips(self, tmp_path: Path):
        from kicad_tools.cli.route_cmd import _write_net_class_map_sidecar
        from kicad_tools.router.rules import (
            NetClassRouting,
            net_class_map_from_dict,
            net_class_map_to_dict,
        )

        pcb_out = tmp_path / "board_routed.kicad_pcb"
        pcb_out.write_text("(kicad_pcb)")
        ncm = {
            "DQ0": NetClassRouting(name="DDR", length_match_group="DDR_DATA"),
            "DQ1": NetClassRouting(name="DDR", length_match_group="DDR_DATA"),
        }

        _write_net_class_map_sidecar(pcb_out, ncm, quiet=True)

        sidecar = tmp_path / "net_class_map.json"
        assert sidecar.is_file()
        # AC1: round-trips with no data loss.
        loaded = net_class_map_from_dict(json.loads(sidecar.read_text()))
        assert loaded.keys() == ncm.keys()
        assert loaded["DQ0"].length_match_group == "DDR_DATA"
        assert net_class_map_to_dict(loaded) == net_class_map_to_dict(ncm)

    def test_empty_map_writes_nothing(self, tmp_path: Path):
        """An empty / None map writes no sidecar (a misleading empty sidecar
        would trip the check-side probe)."""
        from kicad_tools.cli.route_cmd import _write_net_class_map_sidecar

        pcb_out = tmp_path / "board_routed.kicad_pcb"
        pcb_out.write_text("(kicad_pcb)")

        _write_net_class_map_sidecar(pcb_out, {}, quiet=True)
        _write_net_class_map_sidecar(pcb_out, None, quiet=True)

        assert not (tmp_path / "net_class_map.json").exists()

    def test_readonly_output_dir_is_non_fatal(self, tmp_path: Path, capsys):
        """A blocked write (read-only output dir) warns but does not raise --
        the route must not fail because the sidecar could not be persisted."""
        from kicad_tools.cli.route_cmd import _write_net_class_map_sidecar
        from kicad_tools.router.rules import NetClassRouting

        ro_dir = tmp_path / "ro"
        ro_dir.mkdir()
        pcb_out = ro_dir / "board_routed.kicad_pcb"
        pcb_out.write_text("(kicad_pcb)")
        ncm = {"DQ0": NetClassRouting(name="DDR", length_match_group="DDR_DATA")}

        import os

        os.chmod(ro_dir, 0o500)
        try:
            # Must not raise.
            _write_net_class_map_sidecar(pcb_out, ncm, quiet=False)
        finally:
            os.chmod(ro_dir, 0o700)
        captured = capsys.readouterr()
        # Either the write was blocked (warning emitted) or the platform
        # ignored the mode bits (running as root) and the sidecar exists.
        assert (
            "could not write net-class-map sidecar" in captured.out
            or (ro_dir / "net_class_map.json").exists()
        )


class TestInactiveSkewRuleWarningNonCLI:
    """Issue #3917 Defect 3 / AC4: the INACTIVE warning fires from a *direct*
    ``DRCChecker`` instantiation too, not only through the ``kct check`` CLI
    entry point."""

    def test_direct_checker_warns_on_inactive_rule(self, tmp_path: Path, capsys):
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.load(_write_matchgroup_board(tmp_path))
        checker = DRCChecker(pcb, net_class_map=None)  # default warn=True

        checker.check_match_group_length_skew()
        captured = capsys.readouterr()
        assert "INACTIVE" in captured.err
        assert "match_group_length_skew" in captured.err

    def test_warning_deduplicated_per_rule(self, tmp_path: Path, capsys):
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.load(_write_matchgroup_board(tmp_path))
        checker = DRCChecker(pcb, net_class_map=None)

        checker.check_match_group_length_skew()
        checker.check_match_group_length_skew()
        captured = capsys.readouterr()
        # One warning per rule per instance, even across repeated calls.
        assert captured.err.count("match_group_length_skew") == 1

    def test_warning_suppressible(self, tmp_path: Path, capsys):
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.load(_write_matchgroup_board(tmp_path))
        checker = DRCChecker(pcb, net_class_map=None, warn_on_inactive_skew_rules=False)

        checker.check_match_group_length_skew()
        captured = capsys.readouterr()
        assert "INACTIVE" not in captured.err


class TestVerboseMeasuredValues:
    """Issue #3917 AC5: ``--verbose`` surfaces per-group measured values even
    when the group passes (no violation)."""

    def test_passing_group_emits_info_under_verbose(self, tmp_path: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        pcb = _write_matchgroup_board(tmp_path, equal=True)
        _write_matchgroup_sidecar(tmp_path)

        main(
            [
                str(pcb),
                "--only",
                "match_group_length_skew",
                "--verbose",
                "--allow-incomplete",
            ]
        )
        captured = capsys.readouterr()
        # The passing group's measured skew appears as an advisory info line.
        assert "within tolerance" in captured.out

    def test_no_info_without_verbose(self, tmp_path: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        pcb = _write_matchgroup_board(tmp_path, equal=True)
        _write_matchgroup_sidecar(tmp_path)

        main(
            [
                str(pcb),
                "--only",
                "match_group_length_skew",
                "--allow-incomplete",
            ]
        )
        captured = capsys.readouterr()
        assert "within tolerance" not in captured.out


class TestMeasurementSummary:
    """Issue #3924 AC1/AC4/AC5: the default (non-``--verbose``) ``kct check``
    table surface prints a per-group / per-pair length-measurement summary
    for boards with a net-class-map sidecar, showing measured skew and
    tolerance for both passing and failing groups."""

    def test_failing_group_summary_default_verbosity(self, tmp_path: Path, capsys):
        """AC1: an over-skew group prints a MEASUREMENT SUMMARY row with the
        measured skew, tolerance, and a FAIL status -- without ``--verbose``."""
        from kicad_tools.cli.check_cmd import main

        pcb = _write_matchgroup_board(tmp_path)  # unequal -> over-skew
        _write_matchgroup_sidecar(tmp_path)

        main(
            [
                str(pcb),
                "--only",
                "match_group_length_skew",
                "--allow-incomplete",
            ]
        )
        out = capsys.readouterr().out
        assert "MEASUREMENT SUMMARY" in out
        # The DDR group's measured skew and tolerance appear with FAIL.
        summary = out.split("MEASUREMENT SUMMARY", 1)[1]
        assert "DDR" in summary
        assert "0.500" in summary  # default tolerance column
        assert "FAIL" in summary

    def test_passing_group_summary_default_verbosity(self, tmp_path: Path, capsys):
        """AC1: a passing (equal-length) group still appears in the summary
        with a ``pass`` status at default verbosity -- but the raw info line
        (``within tolerance``) is NOT shown outside ``--verbose`` (AC4)."""
        from kicad_tools.cli.check_cmd import main

        pcb = _write_matchgroup_board(tmp_path, equal=True)
        _write_matchgroup_sidecar(tmp_path)

        main(
            [
                str(pcb),
                "--only",
                "match_group_length_skew",
                "--allow-incomplete",
            ]
        )
        out = capsys.readouterr().out
        assert "MEASUREMENT SUMMARY" in out
        summary = out.split("MEASUREMENT SUMMARY", 1)[1]
        assert "DDR" in summary
        assert "pass" in summary
        # AC4: the advisory info-finding wording is reserved for --verbose.
        assert "within tolerance" not in out

    def test_verbose_still_shows_info_line_and_summary(self, tmp_path: Path, capsys):
        """AC4: ``--verbose`` keeps the per-group advisory info line AND the
        measurement summary table (no regression to PR #3948's AC5)."""
        from kicad_tools.cli.check_cmd import main

        pcb = _write_matchgroup_board(tmp_path, equal=True)
        _write_matchgroup_sidecar(tmp_path)

        main(
            [
                str(pcb),
                "--only",
                "match_group_length_skew",
                "--verbose",
                "--allow-incomplete",
            ]
        )
        out = capsys.readouterr().out
        assert "MEASUREMENT SUMMARY" in out
        assert "within tolerance" in out

    def test_no_summary_without_sidecar(self, tmp_path: Path, capsys):
        """AC5: no net-class-map sidecar -> no measurement findings -> no
        MEASUREMENT SUMMARY table (graceful no-op unchanged)."""
        from kicad_tools.cli.check_cmd import main

        pcb = _write_matchgroup_board(tmp_path)  # no sidecar written

        main(
            [
                str(pcb),
                "--only",
                "match_group_length_skew",
                "--allow-incomplete",
            ]
        )
        out = capsys.readouterr().out
        assert "MEASUREMENT SUMMARY" not in out


class TestTenVanishingErrorsRegression:
    """Issue #3917 AC6: reproduce the board-07 "10 errors vanish" case --
    without the sidecar the error count is strictly lower, and the delta is
    attributable to ``match_group_length_skew`` specifically."""

    def test_sidecar_delta_covers_match_group_skew(self, tmp_path: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        pcb = _write_matchgroup_board(tmp_path)

        # (1) Without the sidecar: the rule degrades to a no-op.
        rc_without = main(
            [
                str(pcb),
                "--only",
                "match_group_length_skew",
                "--format",
                "json",
                "--allow-incomplete",
            ]
        )
        without = capsys.readouterr()
        without_data = json.loads(without.out)
        without_errors = without_data["summary"]["errors"]
        assert _mg_error_count(without.out) == 0
        assert rc_without == 0
        # The recipe that forgot the sidecar is warned, not sailed through.
        assert "INACTIVE" in without.err

        # (2) With the sidecar (explicit): the rule fires.
        scar = _write_matchgroup_sidecar(tmp_path)
        main(
            [
                str(pcb),
                "--only",
                "match_group_length_skew",
                "--format",
                "json",
                "--allow-incomplete",
                "--net-class-map",
                str(scar),
            ]
        )
        with_ = capsys.readouterr()
        with_data = json.loads(with_.out)
        with_errors = with_data["summary"]["errors"]
        with_mg = _mg_error_count(with_.out)

        # AC6: strictly lower without the sidecar, and the whole delta is
        # attributable to match_group_length_skew.
        assert with_errors > without_errors
        assert with_mg >= 1
        assert with_errors - without_errors == with_mg
