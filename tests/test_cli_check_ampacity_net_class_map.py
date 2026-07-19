"""Regression tests for ``kct check --net-class-map`` ampacity enforcement.

Issue #4321 (SAFETY): ``kct check`` reaches ampacity via
``DRCChecker.check_ampacity`` -> ``derive_ampacity_specs(net_class_map)``;
``AmpacityRule.check`` matches segments by ``segment.net_name in self.specs``.
``route`` resolves the map's user keys onto board net names before use, but
``check`` historically did NOT -- it passed the raw map straight to
``DRCChecker``.  A hand-authored ``--net-class-map`` (bare keys, or keys
lacking KiCad's hierarchical ``/`` sheet prefix) therefore matched zero
segments -> 0 errors -> a silent false PASS on a dangerously under-width
high-current trace.

These tests cover all three fix tiers:

* **Tier 1** -- ``check --net-class-map`` resolves keys onto board net names
  and reports the expected non-zero ampacity error count (not 0).
* **Tier 2** -- the verdict is deterministic: identical geometry + map yields
  identical ampacity error counts regardless of ``.kicad_dru`` presence
  (``kct check`` never consults ``.kicad_dru``).
* **Tier 3** -- a declared ``target_ampacity`` that never matched a routed
  segment triggers a loud "declared but not evaluated" stderr warning; a
  declared target that DOES match routed segments does not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# A board net keyed with KiCad's hierarchical sheet prefix (``/FUSED_LINE``).
# The sidecar below keys the same net *bare* (``FUSED_LINE``), so without
# key resolution the ampacity rule matches ZERO segments.  Two 0.2 mm F.Cu
# segments on that net are both grossly under-width for 15 A (required
# external width at 1 oz is ~12.585 mm), so a correct check reports exactly
# 2 ampacity errors.
BOARD_WITH_HIERARCHICAL_NET = """(kicad_pcb
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
  (net 1 "/FUSED_LINE")
  (net 2 "GND")
  (gr_rect (start 100 100) (end 200 200)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start 110 120) (end 140 120) (width 0.2) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000001"))
  (segment (start 140 120) (end 170 120) (width 0.2) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000002"))
  (segment (start 110 160) (end 170 160) (width 0.5) (layer "F.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000003"))
)
"""

UNDER_WIDTH_SEGMENT_COUNT = 2


def _write_board(dir_path: Path, name: str = "board.kicad_pcb") -> Path:
    pcb_file = dir_path / name
    pcb_file.write_text(BOARD_WITH_HIERARCHICAL_NET)
    return pcb_file


def _write_net_class_map(dir_path: Path, entries: dict, name: str = "ncm.json") -> Path:
    """Write a hand-authored --net-class-map sidecar JSON file."""
    ncm_file = dir_path / name
    ncm_file.write_text(json.dumps(entries))
    return ncm_file


def _ampacity_errors(report_path: Path) -> list[dict]:
    """Return the ampacity error violations from a check JSON report."""
    data = json.loads(report_path.read_text())
    return [
        v
        for v in data["violations"]
        if v.get("rule_id") == "ampacity" and v.get("severity") == "error"
    ]


def _run_check(
    pcb: Path,
    ncm: Path,
    report: Path,
    *,
    extra: list[str] | None = None,
) -> int:
    from kicad_tools.cli.check_cmd import main

    argv = [
        str(pcb),
        "--mfr",
        "jlcpcb",
        "--net-class-map",
        str(ncm),
        "--output",
        str(report),
        "--format",
        "json",
    ]
    if extra:
        argv.extend(extra)
    return main(argv)


class TestTier1AppliesTargetAmpacity:
    """Tier 1: --net-class-map applies target_ampacity, sidecar-independent."""

    def test_bare_key_under_width_15A_net_is_flagged(self, tmp_path: Path, capsys):
        """A 0.2 mm / 15 A net (bare sidecar key) reports N ampacity errors, not 0.

        This is the exact false-PASS the issue describes: before the fix the
        bare ``FUSED_LINE`` key never matched the board's ``/FUSED_LINE``
        segments, so the rule reported 0 errors and the board PASSED despite
        two grossly under-width 15 A traces.
        """
        pcb = _write_board(tmp_path)
        ncm = _write_net_class_map(
            tmp_path, {"FUSED_LINE": {"name": "FUSED_LINE", "target_ampacity": 15.0}}
        )
        report = tmp_path / "report.json"

        _run_check(pcb, ncm, report)

        errors = _ampacity_errors(report)
        # Non-zero is the core regression assertion (was 0 -> false PASS).
        assert len(errors) > 0
        # Exactly one error per under-width segment on the net.
        assert len(errors) == UNDER_WIDTH_SEGMENT_COUNT
        # The required width must be the IPC-2221 external 15 A / 1 oz floor.
        for err in errors:
            assert err["required_value"] == pytest.approx(12.585, abs=0.01)
            assert err["actual_value"] == pytest.approx(0.2, abs=1e-6)

    def test_fully_qualified_key_also_matches(self, tmp_path: Path):
        """A fully-qualified ``/FUSED_LINE`` key resolves and flags the same segments."""
        pcb = _write_board(tmp_path)
        ncm = _write_net_class_map(
            tmp_path, {"/FUSED_LINE": {"name": "/FUSED_LINE", "target_ampacity": 15.0}}
        )
        report = tmp_path / "report.json"

        _run_check(pcb, ncm, report)

        assert len(_ampacity_errors(report)) == UNDER_WIDTH_SEGMENT_COUNT


class TestTier2DeterministicVerdict:
    """Tier 2: the ampacity verdict is independent of .kicad_dru presence."""

    def test_kicad_dru_presence_does_not_change_error_count(self, tmp_path: Path):
        """Identical geometry + map yields identical counts with/without a .kicad_dru.

        ``kct check`` never consults ``.kicad_dru`` (it is emitted by
        ``route`` for KiCad's own DRC engine), so a sibling ``.kicad_dru``
        carrying an ampacity ``track_width`` rule must not shift the verdict.
        """
        # Board A: a sibling .kicad_dru with an ampacity-style track_width rule.
        dir_a = tmp_path / "with_dru"
        dir_a.mkdir()
        pcb_a = _write_board(dir_a)
        (dir_a / "board.kicad_dru").write_text(
            '(version 1)\n(rule "ampacity /FUSED_LINE"\n'
            "  (condition \"A.NetName == '/FUSED_LINE'\")\n"
            "  (constraint track_width (min 12.585mm)))\n"
        )
        ncm_a = _write_net_class_map(
            dir_a, {"FUSED_LINE": {"name": "FUSED_LINE", "target_ampacity": 15.0}}
        )
        report_a = dir_a / "report.json"
        _run_check(pcb_a, ncm_a, report_a)

        # Board B: identical copper + map, no .kicad_dru at all.
        dir_b = tmp_path / "no_dru"
        dir_b.mkdir()
        pcb_b = _write_board(dir_b)
        ncm_b = _write_net_class_map(
            dir_b, {"FUSED_LINE": {"name": "FUSED_LINE", "target_ampacity": 15.0}}
        )
        report_b = dir_b / "report.json"
        _run_check(pcb_b, ncm_b, report_b)

        count_a = len(_ampacity_errors(report_a))
        count_b = len(_ampacity_errors(report_b))
        assert count_a == count_b == UNDER_WIDTH_SEGMENT_COUNT


class TestTier3FailLoudWhenUnevaluated:
    """Tier 3: warn when a declared target_ampacity was never evaluated."""

    _WARN_SUBSTR = "ampacity rule declared but not evaluated for net(s):"

    def test_nonexistent_net_triggers_warning(self, tmp_path: Path, capsys):
        """A target_ampacity for a net absent from the board warns loudly."""
        pcb = _write_board(tmp_path)
        ncm = _write_net_class_map(
            tmp_path,
            {"NO_SUCH_NET": {"name": "NO_SUCH_NET", "target_ampacity": 15.0}},
        )
        report = tmp_path / "report.json"

        _run_check(pcb, ncm, report)

        err = capsys.readouterr().err
        assert self._WARN_SUBSTR in err
        assert "NO_SUCH_NET" in err
        # It genuinely evaluated nothing, so there are no ampacity errors.
        assert _ampacity_errors(report) == []

    def test_matching_routed_net_does_not_warn(self, tmp_path: Path, capsys):
        """A target_ampacity that matches routed segments does NOT warn."""
        pcb = _write_board(tmp_path)
        ncm = _write_net_class_map(
            tmp_path, {"FUSED_LINE": {"name": "FUSED_LINE", "target_ampacity": 15.0}}
        )
        report = tmp_path / "report.json"

        _run_check(pcb, ncm, report)

        err = capsys.readouterr().err
        assert self._WARN_SUBSTR not in err

    def test_skip_ampacity_still_warns_for_declared_target(self, tmp_path: Path, capsys):
        """``--skip ampacity`` with a declared target still fires the warning."""
        pcb = _write_board(tmp_path)
        ncm = _write_net_class_map(
            tmp_path, {"FUSED_LINE": {"name": "FUSED_LINE", "target_ampacity": 15.0}}
        )
        report = tmp_path / "report.json"

        _run_check(pcb, ncm, report, extra=["--skip", "ampacity"])

        err = capsys.readouterr().err
        assert self._WARN_SUBSTR in err
        # The net resolves, so the warning names the board-resolved net.
        assert "/FUSED_LINE" in err
