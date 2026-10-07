"""Net-membership evidence is limited to pads near the finding (issue #6011)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from kicad_tools.validate import DRCResults, DRCViolation
from kicad_tools.validate.evidence import (
    EVIDENCE_HASH_PREFIX,
    EvidenceContext,
    compute_evidence_hash,
    evidence_payload,
    is_outdated_evidence_hash,
    net_evidence_radius,
)
from kicad_tools.validate.rules.waivers import (
    WAIVER_STALE_RULE_ID,
    apply_waivers,
    waivers_from_dict,
)

REPO = Path(__file__).resolve().parent.parent


def _pad(number: str, net: str, at=(0.0, 0.0), size=(0.5, 0.5)):
    from kicad_tools.schema.pcb import Pad

    return Pad(
        number=number,
        type="smd",
        shape="rect",
        position=at,
        size=size,
        layers=["F.Cu"],
        net_number=1,
        net_name=net,
    )


def _fp(ref: str, position, pads, rotation: float = 0.0):
    from kicad_tools.schema.pcb import Footprint

    return Footprint(
        name="TestFP",
        layer="F.Cu",
        position=position,
        rotation=rotation,
        reference=ref,
        value="X",
        pads=pads,
        texts=[],
        graphics=[],
    )


def _board(extra=()):
    """U1 at (10, 20) with a GND pad; C3 at (11, 20) with GND + VCC pads."""
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.sexp import SExp

    pcb = PCB(SExp(name="kicad_pcb"))
    pcb._footprints.append(_fp("U1", (10.0, 20.0), [_pad("1", "GND"), _pad("2", "VCC", (0, 1))]))
    pcb._footprints.append(_fp("C3", (11.0, 20.0), [_pad("1", "GND"), _pad("2", "VCC", (1, 0))]))
    for fp in extra:
        pcb._footprints.append(fp)
    return pcb


def _finding(rule_id: str = "clearance_pad_pad", **kw) -> DRCViolation:
    base = {
        "rule_id": rule_id,
        "severity": "error",
        "message": "too close",
        "location": (10.5, 20.0),
        "layer": "F.Cu",
        "actual_value": 0.1,
        "required_value": 0.2,
        "items": ("U1", "C3"),
        "nets": ("GND",),
    }
    base.update(kw)
    return DRCViolation(**base)


def _hash(pcb, v: DRCViolation | None = None) -> str:
    return compute_evidence_hash(v or _finding(), EvidenceContext(pcb))


class TestLocalNetEvidence:
    def test_far_gnd_pad_does_not_change_hash(self):
        base = _hash(_board())
        far = _fp("J9", (80.0, 70.0), [_pad("1", "GND")])
        assert _hash(_board([far])) == base

    def test_near_gnd_pad_changes_hash(self):
        base = _hash(_board())
        near = _fp("C9", (12.0, 21.0), [_pad("1", "GND")])
        assert _hash(_board([near])) != base

    def test_rect_pad_corner_within_radius_changes_hash(self):
        # Finding at (10.5, 20), radius 3 mm. Nearest corner of this 1x1 pad
        # is (12.6, 22.1): 2.97 mm away, inside the radius, though
        # center-distance minus max(w, h) / 2 would be 3.18 mm.
        base = _hash(_board())
        pad = _fp("J9", (13.1, 22.6), [_pad("1", "GND", size=(1, 1))])
        assert _hash(_board([pad])) != base

    @pytest.mark.parametrize(
        ("side", "offset"),
        [(1.0, 2.5), (1.5, 2.7), (2.0, 3.0)],
    )
    @pytest.mark.parametrize("rotation", [0.0, 30.0, 45.0, 90.0, 135.0, 270.0])
    def test_square_pad_corner_within_radius_changes_hash_at_any_rotation(
        self, side, offset, rotation
    ):
        # Pad centre sits at (offset, offset) from the finding, diagonally, so
        # only its near corner is inside the 3 mm radius: centre distance minus
        # max(w, h) / 2 would exclude it. The footprint is rotated about its own
        # origin, with the pad offset chosen so the pad centre lands on target.
        from kicad_tools.core.geometry import rotate_pad_offset

        target = (10.5 + offset, 20.0 + offset)
        ox, oy = rotate_pad_offset(1.0, 0.5, rotation)
        fp_pos = (target[0] - ox, target[1] - oy)
        base = _hash(_board())
        pad = _fp("J9", fp_pos, [_pad("1", "GND", at=(1.0, 0.5), size=(side, side))], rotation)
        assert _hash(_board([pad])) != base

    def test_moving_a_nearby_pad_changes_hash(self):
        a = _hash(_board([_fp("C9", (12.0, 21.0), [_pad("1", "GND")])]))
        b = _hash(_board([_fp("C9", (12.2, 21.0), [_pad("1", "GND")])]))
        assert a != b

    def test_pad_position_uses_footprint_rotation(self):
        # A pad at local (+50, 0): rotation 0 puts it far away, rotation 180
        # brings it back next to the finding.
        far = _fp("R1", (60.0, 20.0), [_pad("1", "GND", (50.0, 0.0))])
        base = _hash(_board([far]))
        assert base == _hash(_board())
        near = _fp("R1", (60.0, 20.0), [_pad("1", "GND", (50.0, 0.0))], rotation=180.0)
        assert _hash(_board([near])) != base

    def test_closest_locations_widen_the_neighbourhood(self):
        # The pad is > R from location but within R of a closest point.
        pad = _fp("C9", (30.0, 20.0), [_pad("1", "GND")])
        v = _finding(location=(10.5, 20.0), closest_locations=((10.5, 20.0), (28.0, 20.0)))
        assert _hash(_board([pad]), v) != _hash(_board(), v)
        v_point = _finding(location=(10.5, 20.0))
        assert _hash(_board([pad]), v_point) == _hash(_board(), v_point)

    def test_named_footprint_pads_still_change_hash(self):
        pcb = _board()
        base = _hash(pcb)
        pcb.footprints[0].pads.append(_pad("3", "GND", (40.0, 40.0)))
        assert _hash(pcb) != base

    def test_whole_net_rule_keeps_full_membership(self):
        assert net_evidence_radius("single_pad_net") is None
        assert net_evidence_radius("unconnected_items") is None
        v = _finding("ampacity")
        far = _fp("J9", (80.0, 70.0), [_pad("1", "GND")])
        assert _hash(_board([far]), v) != _hash(_board(), v)
        payload = evidence_payload(v, EvidenceContext(_board([far])))
        assert "J9.1" in payload["nets"]["GND"]
        assert "net_radius" not in payload

    def test_unlocated_finding_keeps_full_membership(self):
        v = _finding(location=None)
        far = _fp("J9", (80.0, 70.0), [_pad("1", "GND")])
        assert _hash(_board([far]), v) != _hash(_board(), v)

    def test_radius_is_per_rule(self):
        assert net_evidence_radius("clearance_pad_pad") == 3.0
        assert net_evidence_radius("courtyards_overlap") == 5.0
        # 4 mm away: outside a clearance finding's radius, inside the default.
        pad = _fp("C9", (14.5, 20.0), [_pad("1", "GND", size=(0.0, 0.0))])
        clr = _finding()
        assert _hash(_board([pad]), clr) == _hash(_board(), clr)
        other = _finding("courtyards_overlap")
        assert _hash(_board([pad]), other) != _hash(_board(), other)


class TestVersionedHash:
    def test_prefix_is_ev2(self):
        assert EVIDENCE_HASH_PREFIX == "ev2:"
        assert _hash(_board()).startswith("ev2:")
        assert is_outdated_evidence_hash("ev1:abcdef")
        assert not is_outdated_evidence_hash("ev2:abcdef")
        assert not is_outdated_evidence_hash(None)

    def _apply(self, recorded: str):
        v = replace(_finding(), evidence_hash=_hash(_board()))
        results = DRCResults(violations=[v])
        entry = {
            "key": v.key,
            "evidence_hash": recorded,
            "reason": "reviewed",
            "reviewer": "ee",
            "date": "2026-10-06",
        }
        apply_waivers(results, waivers_from_dict({"version": 3, "waivers": [entry]}))
        finding = next(f for f in results.violations if f.rule_id == v.rule_id)
        (stale,) = [f for f in results.violations if f.rule_id == WAIVER_STALE_RULE_ID]
        return finding, stale

    def test_old_version_waiver_reports_outdated_version(self):
        finding, stale = self._apply("ev1:0123456789abcdef")
        assert not finding.waived
        assert "outdated evidence version" in stale.message
        assert "ev1" in stale.message and "ev2" in stale.message
        assert finding.to_dict()["stale_waiver_cause"] == "outdated_evidence_version"

    def test_same_version_mismatch_reports_evidence_change(self):
        finding, stale = self._apply("ev2:0123456789abcdef")
        assert "outdated evidence version" not in stale.message
        assert "geometry or nets" in stale.message
        assert finding.to_dict()["stale_waiver_cause"] == "evidence_changed"


# ---------------------------------------------------------------------------
# Real boards: keys and hashes are deterministic across runs
# ---------------------------------------------------------------------------

BOARDS = [
    "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb",
    "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb",
    "boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb",
]


def _check_json(capsys, pcb: Path) -> dict:
    from kicad_tools.cli import check_cmd

    check_cmd.main([str(pcb), "--format", "json"])
    return json.loads(capsys.readouterr().out)


@pytest.mark.slow
@pytest.mark.parametrize("rel", BOARDS)
def test_real_board_keys_and_hashes_are_stable(rel, capsys):
    pcb = REPO / rel
    if not pcb.is_file():
        pytest.skip(f"{rel} not present")
    first = _check_json(capsys, pcb)
    second = _check_json(capsys, pcb)

    def ids(report):
        return sorted((v["key"], v["evidence_hash"]) for v in report["violations"])

    assert ids(first) == ids(second)
    assert first["violations"]
    assert all(v["evidence_hash"].startswith("ev2:") for v in first["violations"])
