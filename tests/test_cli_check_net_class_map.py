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

        result = main(
            [
                str(diffpair_pcb),
                "--format",
                "json",
                "--only",
                "diffpair_routing_continuity,diffpair_length_skew",
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
