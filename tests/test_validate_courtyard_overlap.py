"""Tests for the courtyard-overlap DRC rule and waiver loading (Issue #4137).

Uses in-memory synthetic ``PCB(SExp(...))`` fixtures constructed directly
(mirroring ``tests/test_validate_silkscreen.py``) -- no dependency on any
board file or the chorus fixture (local-only, not in CI).
"""

from __future__ import annotations

import json

import pytest

from kicad_tools.cli import check_cmd
from kicad_tools.schema.pcb import (
    PCB,
    Footprint,
    FootprintGraphic,
)
from kicad_tools.sexp import SExp
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.courtyard import (
    COURTYARD_RULE_ID,
    COURTYARD_UNRESOLVED_RULE_ID,
    COURTYARD_UNUSED_WAIVER_RULE_ID,
    CourtyardOverlapRule,
)
from kicad_tools.validate.rules.courtyard_waivers import (
    CourtyardWaivers,
    courtyard_waivers_from_dict,
    discover_courtyard_waivers_sidecar,
    load_courtyard_waivers,
)

pytest.importorskip("shapely")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _empty_pcb() -> PCB:
    return PCB(SExp(name="kicad_pcb"))


def _crtyd_rect(
    *,
    start: tuple[float, float],
    end: tuple[float, float],
    layer: str = "F.CrtYd",
) -> FootprintGraphic:
    return FootprintGraphic(
        graphic_type="rect",
        layer=layer,
        stroke_width=0.05,
        start=start,
        end=end,
    )


def _crtyd_lines(
    corners: list[tuple[float, float]],
    *,
    layer: str = "F.CrtYd",
) -> list[FootprintGraphic]:
    """Build a closed loop of fp_line segments from ordered corner points."""
    graphics: list[FootprintGraphic] = []
    n = len(corners)
    for i in range(n):
        graphics.append(
            FootprintGraphic(
                graphic_type="line",
                layer=layer,
                stroke_width=0.05,
                start=corners[i],
                end=corners[(i + 1) % n],
            )
        )
    return graphics


def _crtyd_poly(
    points: list[tuple[float, float]],
    *,
    layer: str = "F.CrtYd",
) -> FootprintGraphic:
    return FootprintGraphic(
        graphic_type="poly",
        layer=layer,
        stroke_width=0.05,
        points=points,
    )


def _make_footprint(
    *,
    reference: str,
    position: tuple[float, float] = (0.0, 0.0),
    rotation: float = 0.0,
    layer: str = "F.Cu",
    graphics: list[FootprintGraphic] | None = None,
) -> Footprint:
    return Footprint(
        name="TestFP",
        layer=layer,
        position=position,
        rotation=rotation,
        reference=reference,
        value="TEST",
        pads=[],
        texts=[],
        graphics=graphics or [],
    )


def _pcb_with(*footprints: Footprint) -> PCB:
    pcb = _empty_pcb()
    for fp in footprints:
        pcb._footprints.append(fp)
    return pcb


def _run(pcb: PCB, waivers: CourtyardWaivers | None = None):
    return CourtyardOverlapRule(waivers=waivers).check(pcb)


# ---------------------------------------------------------------------------
# Core overlap detection
# ---------------------------------------------------------------------------


class TestCourtyardOverlap:
    def test_overlapping_rects_error(self):
        """Two overlapping F.CrtYd rects (no waiver) -> one error violation."""
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=[_crtyd_rect(start=(-1.0, -1.0), end=(1.0, 1.0))],
        )
        b = _make_footprint(
            reference="C1",
            position=(1.0, 0.0),  # overlaps U1 in x [0,1]
            graphics=[_crtyd_rect(start=(-1.0, -1.0), end=(1.0, 1.0))],
        )
        results = _run(_pcb_with(a, b))
        overlaps = results.filter_by_rule(COURTYARD_RULE_ID)
        assert len(overlaps) == 1
        v = overlaps[0]
        assert v.severity == "error"
        assert not v.is_waived
        assert set(v.items) == {"U1", "C1"}
        assert results.error_count == 1

    def test_non_overlapping_no_violation(self):
        """Well-separated courtyards -> no violation."""
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=[_crtyd_rect(start=(-1.0, -1.0), end=(1.0, 1.0))],
        )
        b = _make_footprint(
            reference="C1",
            position=(10.0, 0.0),
            graphics=[_crtyd_rect(start=(-1.0, -1.0), end=(1.0, 1.0))],
        )
        results = _run(_pcb_with(a, b))
        assert results.filter_by_rule(COURTYARD_RULE_ID) == []

    def test_touching_zero_area_no_violation(self):
        """Exactly-touching (zero-area intersection) courtyards -> no error."""
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=[_crtyd_rect(start=(-1.0, -1.0), end=(1.0, 1.0))],
        )
        b = _make_footprint(
            reference="C1",
            position=(2.0, 0.0),  # left edge at x=1 == U1 right edge
            graphics=[_crtyd_rect(start=(-1.0, -1.0), end=(1.0, 1.0))],
        )
        results = _run(_pcb_with(a, b))
        assert results.filter_by_rule(COURTYARD_RULE_ID) == []

    def test_opposite_sides_no_violation(self):
        """F.CrtYd vs B.CrtYd at the same x/y never conflict."""
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            layer="F.Cu",
            graphics=[_crtyd_rect(start=(-1.0, -1.0), end=(1.0, 1.0), layer="F.CrtYd")],
        )
        b = _make_footprint(
            reference="U2",
            position=(0.0, 0.0),
            layer="B.Cu",
            graphics=[_crtyd_rect(start=(-1.0, -1.0), end=(1.0, 1.0), layer="B.CrtYd")],
        )
        results = _run(_pcb_with(a, b))
        assert results.filter_by_rule(COURTYARD_RULE_ID) == []

    def test_line_chain_courtyard_overlap(self):
        """A courtyard built from fp_line segments still detects overlap."""
        square = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=_crtyd_lines(square),
        )
        b = _make_footprint(
            reference="C1",
            position=(1.0, 0.0),
            graphics=_crtyd_lines(square),
        )
        results = _run(_pcb_with(a, b))
        overlaps = results.filter_by_rule(COURTYARD_RULE_ID)
        assert len(overlaps) == 1
        assert overlaps[0].severity == "error"

    def test_poly_courtyard_overlap(self):
        """A courtyard drawn as fp_poly is resolved and overlap detected."""
        diamond = [(0.0, -1.5), (1.5, 0.0), (0.0, 1.5), (-1.5, 0.0)]
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=[_crtyd_poly(diamond)],
        )
        b = _make_footprint(
            reference="C1",
            position=(1.0, 0.0),
            graphics=[_crtyd_poly(diamond)],
        )
        results = _run(_pcb_with(a, b))
        assert len(results.filter_by_rule(COURTYARD_RULE_ID)) == 1

    def test_rotated_footprint_transform(self):
        """A rotated footprint's courtyard is transformed to board space.

        U1 is a 4x1 rect; rotating C1 90deg turns its 4x1 rect into a 1x4
        footprint that reaches across U1.  Without honoring the rotation the
        two would not overlap.
        """
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=[_crtyd_rect(start=(-2.0, -0.5), end=(2.0, 0.5))],
        )
        b = _make_footprint(
            reference="C1",
            position=(0.0, 1.5),
            rotation=90.0,
            graphics=[_crtyd_rect(start=(-2.0, -0.5), end=(2.0, 0.5))],
        )
        results = _run(_pcb_with(a, b))
        assert len(results.filter_by_rule(COURTYARD_RULE_ID)) == 1

    def test_unresolved_fp_arc_courtyard_info(self):
        """A courtyard with only unsupported geometry -> info finding."""
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=[
                FootprintGraphic(
                    graphic_type="arc",
                    layer="F.CrtYd",
                    stroke_width=0.05,
                    start=(-1.0, 0.0),
                    end=(1.0, 0.0),
                )
            ],
        )
        results = _run(_pcb_with(a))
        infos = results.filter_by_rule(COURTYARD_UNRESOLVED_RULE_ID)
        assert len(infos) == 1
        assert infos[0].severity == "info"
        assert "U1" in infos[0].items

    def test_no_courtyard_geometry_degrades(self):
        """Boards with no courtyard geometry at all -> clean, no crash."""
        a = _make_footprint(reference="U1", graphics=[])
        b = _make_footprint(reference="C1", graphics=[])
        results = _run(_pcb_with(a, b))
        assert results.error_count == 0
        assert results.filter_by_rule(COURTYARD_RULE_ID) == []
        assert results.filter_by_rule(COURTYARD_UNRESOLVED_RULE_ID) == []

    def test_three_way_overlap_only_one_waived(self):
        """A-B waived; B-C and A-C still fail independently."""
        # Three overlapping rects at x=0,1,2 all with width 2 -> A-B, B-C, A-C
        # all overlap.
        a = _make_footprint(
            reference="U1",
            position=(0.0, 0.0),
            graphics=[_crtyd_rect(start=(-1.5, -1.0), end=(1.5, 1.0))],
        )
        b = _make_footprint(
            reference="U2",
            position=(1.0, 0.0),
            graphics=[_crtyd_rect(start=(-1.5, -1.0), end=(1.5, 1.0))],
        )
        c = _make_footprint(
            reference="U3",
            position=(2.0, 0.0),
            graphics=[_crtyd_rect(start=(-1.5, -1.0), end=(1.5, 1.0))],
        )
        waivers = courtyard_waivers_from_dict(
            {
                "version": 1,
                "waivers": [
                    {
                        "rule": "courtyards_overlap",
                        "refs": ["U1", "U2"],
                        "reason": "ok",
                        "issue": "x#1",
                    }
                ],
            }
        )
        results = _run(_pcb_with(a, b, c), waivers=waivers)
        overlaps = results.filter_by_rule(COURTYARD_RULE_ID)
        assert len(overlaps) == 3
        waived = [v for v in overlaps if v.is_waived]
        errors = [v for v in overlaps if v.is_error]
        assert len(waived) == 1
        assert set(waived[0].items) == {"U1", "U2"}
        # B-C and A-C remain blocking.
        assert len(errors) == 2
        assert results.error_count == 2


# ---------------------------------------------------------------------------
# Waiver matching
# ---------------------------------------------------------------------------


class TestWaiverMatching:
    def _overlapping_pcb(self) -> PCB:
        a = _make_footprint(
            reference="C52",
            position=(0.0, 0.0),
            graphics=[_crtyd_rect(start=(-1.0, -1.0), end=(1.0, 1.0))],
        )
        b = _make_footprint(
            reference="U10",
            position=(1.0, 0.0),
            graphics=[_crtyd_rect(start=(-1.0, -1.0), end=(1.0, 1.0))],
        )
        return _pcb_with(a, b)

    def test_waived_pair_not_error(self):
        waivers = courtyard_waivers_from_dict(
            {
                "version": 1,
                "waivers": [
                    {
                        "rule": "courtyards_overlap",
                        "refs": ["C52", "U10"],
                        "reason": "EE-mandated tight decoupling",
                        "issue": "chorus#13",
                    }
                ],
            }
        )
        results = _run(self._overlapping_pcb(), waivers=waivers)
        overlaps = results.filter_by_rule(COURTYARD_RULE_ID)
        assert len(overlaps) == 1
        v = overlaps[0]
        assert v.is_waived
        assert v.waiver_reason == "EE-mandated tight decoupling"
        assert v.waiver_issue == "chorus#13"
        assert results.error_count == 0
        assert results.waived_count == 1
        assert results.passed is True  # gate passes

    def test_waiver_order_independent(self):
        """refs reversed vs iteration order still matches."""
        waivers = courtyard_waivers_from_dict(
            {
                "version": 1,
                "waivers": [
                    {
                        "rule": "courtyards_overlap",
                        "refs": ["U10", "C52"],  # reversed
                        "reason": "ok",
                        "issue": "x#1",
                    }
                ],
            }
        )
        results = _run(self._overlapping_pcb(), waivers=waivers)
        overlaps = results.filter_by_rule(COURTYARD_RULE_ID)
        assert len(overlaps) == 1
        assert overlaps[0].is_waived

    def test_unused_waiver_info(self):
        """A waiver naming an absent component -> info 'unused waiver'."""
        waivers = courtyard_waivers_from_dict(
            {
                "version": 1,
                "waivers": [
                    {
                        "rule": "courtyards_overlap",
                        "refs": ["C99", "U10"],  # C99 not on board
                        "reason": "stale",
                        "issue": "x#1",
                    }
                ],
            }
        )
        results = _run(self._overlapping_pcb(), waivers=waivers)
        unused = results.filter_by_rule(COURTYARD_UNUSED_WAIVER_RULE_ID)
        assert len(unused) == 1
        assert unused[0].severity == "info"
        # The real overlap (C52/U10) is NOT waived by this stale entry.
        overlaps = results.filter_by_rule(COURTYARD_RULE_ID)
        assert len(overlaps) == 1
        assert overlaps[0].is_error


# ---------------------------------------------------------------------------
# Waiver-file schema validation
# ---------------------------------------------------------------------------


class TestWaiverValidation:
    def test_missing_version_rejected(self):
        with pytest.raises(ValueError, match="version"):
            courtyard_waivers_from_dict({"waivers": []})

    def test_unknown_version_rejected(self):
        with pytest.raises(ValueError, match="unsupported courtyard-waivers version"):
            courtyard_waivers_from_dict({"version": 99, "waivers": []})

    def test_bad_refs_count_rejected(self):
        with pytest.raises(ValueError, match="exactly 2"):
            courtyard_waivers_from_dict(
                {
                    "version": 1,
                    "waivers": [
                        {
                            "rule": "courtyards_overlap",
                            "refs": ["C1"],
                            "reason": "x",
                            "issue": "y",
                        }
                    ],
                }
            )

    def test_missing_reason_rejected(self):
        with pytest.raises(ValueError, match="reason"):
            courtyard_waivers_from_dict(
                {
                    "version": 1,
                    "waivers": [
                        {
                            "rule": "courtyards_overlap",
                            "refs": ["C1", "U1"],
                            "reason": "",
                            "issue": "y",
                        }
                    ],
                }
            )

    def test_missing_issue_rejected(self):
        with pytest.raises(ValueError, match="issue"):
            courtyard_waivers_from_dict(
                {
                    "version": 1,
                    "waivers": [
                        {
                            "rule": "courtyards_overlap",
                            "refs": ["C1", "U1"],
                            "reason": "x",
                        }
                    ],
                }
            )

    def test_unsupported_rule_rejected(self):
        with pytest.raises(ValueError, match="unsupported rule"):
            courtyard_waivers_from_dict(
                {
                    "version": 1,
                    "waivers": [
                        {
                            "rule": "clearance",
                            "refs": ["C1", "U1"],
                            "reason": "x",
                            "issue": "y",
                        }
                    ],
                }
            )

    def test_load_from_file(self, tmp_path):
        path = tmp_path / ".courtyard_waivers.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "waivers": [
                        {
                            "rule": "courtyards_overlap",
                            "refs": ["C52", "U10"],
                            "reason": "ok",
                            "issue": "x#1",
                        }
                    ],
                }
            )
        )
        waivers = load_courtyard_waivers(path)
        assert len(waivers) == 1
        assert waivers.match("U10", "C52") is not None

    def test_load_malformed_json_raises(self, tmp_path):
        path = tmp_path / ".courtyard_waivers.json"
        path.write_text("{not json")
        with pytest.raises(ValueError, match="parsing courtyard-waivers JSON"):
            load_courtyard_waivers(path)

    def test_discovery_probes_sidecar(self, tmp_path):
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")
        sidecar = tmp_path / ".courtyard_waivers.json"
        sidecar.write_text('{"version": 1, "waivers": []}')
        found = discover_courtyard_waivers_sidecar(pcb_path)
        assert found == sidecar

    def test_discovery_returns_none_when_absent(self, tmp_path):
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")
        assert discover_courtyard_waivers_sidecar(pcb_path) is None


# ---------------------------------------------------------------------------
# Checker + CLI integration
# ---------------------------------------------------------------------------


class TestCheckerIntegration:
    def _board_file(self, tmp_path, *, refs_positions):
        """Write a minimal .kicad_pcb with overlapping F.CrtYd footprints."""
        fps = []
        for ref, (x, y) in refs_positions:
            fps.append(
                f"""  (footprint "TestFP" (layer "F.Cu")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))
    (fp_rect (start -1 -1) (end 1 1) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))
  )"""
            )
        content = (
            "(kicad_pcb (version 20240108) (generator test)\n"
            '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n' + "\n".join(fps) + "\n)\n"
        )
        path = tmp_path / "board.kicad_pcb"
        path.write_text(content)
        return path

    def test_checker_flags_overlap(self, tmp_path):
        path = self._board_file(
            tmp_path, refs_positions=[("U1", (10.0, 10.0)), ("C1", (11.0, 10.0))]
        )
        pcb = PCB.load(path)
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_courtyard_overlap()
        assert results.error_count == 1

    def test_cli_waived_json_output(self, tmp_path, capsys):
        path = self._board_file(
            tmp_path, refs_positions=[("U1", (10.0, 10.0)), ("C1", (11.0, 10.0))]
        )
        waiver = tmp_path / ".courtyard_waivers.json"
        waiver.write_text(
            json.dumps(
                {
                    "version": 1,
                    "waivers": [
                        {
                            "rule": "courtyards_overlap",
                            "refs": ["U1", "C1"],
                            "reason": "intentional",
                            "issue": "x#1",
                        }
                    ],
                }
            )
        )
        rc = check_cmd.main(
            [
                str(path),
                "--only",
                "courtyard_overlap",
                "--format",
                "json",
                "--drc-only",
            ]
        )
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["summary"]["waived"] == 1
        assert data["summary"]["errors"] == 0
        assert data["summary"]["passed"] is True
        assert rc == 0
        waived_v = [v for v in data["violations"] if v.get("waived")]
        assert len(waived_v) == 1
        assert waived_v[0]["status"] == "waived"

    def test_cli_unwaived_overlap_fails(self, tmp_path, capsys):
        """A new, unwaived overlap still fails even alongside a waived pair."""
        path = self._board_file(
            tmp_path,
            refs_positions=[
                ("U1", (10.0, 10.0)),
                ("C1", (11.0, 10.0)),
                ("C2", (30.0, 30.0)),
                ("C3", (31.0, 30.0)),
            ],
        )
        waiver = tmp_path / ".courtyard_waivers.json"
        waiver.write_text(
            json.dumps(
                {
                    "version": 1,
                    "waivers": [
                        {
                            "rule": "courtyards_overlap",
                            "refs": ["U1", "C1"],
                            "reason": "intentional",
                            "issue": "x#1",
                        }
                    ],
                }
            )
        )
        rc = check_cmd.main(
            [str(path), "--only", "courtyard_overlap", "--format", "json", "--drc-only"]
        )
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["summary"]["waived"] == 1
        assert data["summary"]["errors"] == 1  # C2/C3 unwaived
        assert data["summary"]["passed"] is False
        assert rc == 2

    def test_cli_explicit_malformed_waiver_hard_error(self, tmp_path):
        path = self._board_file(
            tmp_path, refs_positions=[("U1", (10.0, 10.0)), ("C1", (11.0, 10.0))]
        )
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        rc = check_cmd.main(
            [str(path), "--only", "courtyard_overlap", "--courtyard-waivers", str(bad)]
        )
        assert rc == 1

    def test_cli_auto_malformed_waiver_degrades(self, tmp_path, capsys):
        path = self._board_file(
            tmp_path, refs_positions=[("U1", (10.0, 10.0)), ("C1", (11.0, 10.0))]
        )
        # Auto-discovered sidecar next to the board is malformed.
        (tmp_path / ".courtyard_waivers.json").write_text("{not json")
        rc = check_cmd.main(
            [str(path), "--only", "courtyard_overlap", "--format", "json", "--drc-only"]
        )
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # Degrades to zero waivers -> the overlap is a blocking error.
        assert data["summary"]["errors"] == 1
        assert rc == 2
