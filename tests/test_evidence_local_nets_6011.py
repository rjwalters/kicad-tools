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


def _pad(
    number: str,
    net: str,
    at=(0.0, 0.0),
    size=(0.5, 0.5),
    shape: str = "rect",
    rotation: float = 0.0,
):
    from kicad_tools.schema.pcb import Pad

    return Pad(
        number=number,
        type="smd",
        shape=shape,
        position=at,
        size=size,
        layers=["F.Cu"],
        net_number=1,
        net_name=net,
        rotation=rotation,
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


# The finding used by ``_finding()``; every geometry test measures from here.
FX, FY = 10.5, 20.0
ANGLES = (0.0, 15.0, 30.0, 45.0, 60.0, 90.0, 135.0, 200.0, 330.0)


def _included(pad_fp, v: DRCViolation | None = None) -> bool:
    """Does adding ``pad_fp`` change the finding's hash?"""
    v = v or _finding()
    return _hash(_board([pad_fp]), v) != _hash(_board(), v)


class TestPadOutlineDistance:
    """The radius is measured to the pad's copper outline, not its centre.

    Fleet-judge review of PR #6040: ``max(w, h) / 2`` under-reaches a
    rectangle's corners, so a pad whose corner sat inside the radius was
    dropped and adding it left the hash unchanged.
    """

    def test_judge_repro_rect_corner_inside_radius(self):
        # 1x1 mm pad at (13.1, 22.6): nearest corner (12.6, 22.1) is
        # sqrt(2.1^2 + 2.1^2) = 2.97 mm from the finding at (10.5, 20).
        pad = _fp("J9", (13.1, 22.6), [_pad("1", "GND", size=(1, 1))])
        base = _hash(_board())
        with_pad = _hash(_board([pad]))
        assert with_pad != base
        # Removing it again restores the original hash.
        assert _hash(_board()) == base
        payload = evidence_payload(_finding(), EvidenceContext(_board([pad])))
        assert [row[0] for row in payload["nets"]["GND"]].count("J9.1") == 1

    def test_judge_repro_just_outside_is_excluded(self):
        # Same pad nudged so its corner sits at 3.04 mm.
        pad = _fp("J9", (13.15, 22.65), [_pad("1", "GND", size=(1, 1))])
        assert not _included(pad)

    @pytest.mark.parametrize("angle", ANGLES)
    @pytest.mark.parametrize("fp_rotation", [0.0, 90.0])
    def test_rotated_rect_corner(self, angle, fp_rotation):
        """A rotated 1.6x0.6 pad whose nearest corner is 2.95 / 3.05 mm away."""
        from kicad_tools.core.geometry import rotate_pad_offset

        w, h = 1.6, 0.6
        # Board offset of the pad's (+w/2, +h/2) corner under its absolute
        # rotation, using the KiCad convention the evidence code relies on.
        cx, cy = rotate_pad_offset(w / 2, h / 2, angle)
        norm = (cx * cx + cy * cy) ** 0.5
        ux, uy = cx / norm, cy / norm
        # Pointing the corner straight at the finding makes that corner the
        # nearest copper point (the finding lies in the corner's normal cone).
        # The old ``max(w, h) / 2`` reach would put it at d + 0.054 mm.
        for d, expected in ((2.95, True), (3.05, False)):
            px = FX - ux * d - cx
            py = FY - uy * d - cy
            # Footprint origin elsewhere, so the footprint rotation actually
            # moves the pad: place the footprint so the pad lands on (px, py).
            ox, oy = rotate_pad_offset(1.0, 0.0, fp_rotation)
            fp = _fp(
                "J9",
                (px - ox, py - oy),
                [_pad("1", "GND", at=(1.0, 0.0), size=(w, h), rotation=angle)],
                rotation=fp_rotation,
            )
            assert _included(fp) is expected, (angle, fp_rotation, d)

    @pytest.mark.parametrize("angle", [30.0, 60.0])
    def test_rotation_sign_matters(self, angle):
        """Mirroring the angle moves the corner away: the sign is honoured."""
        from kicad_tools.core.geometry import rotate_pad_offset

        w, h = 1.6, 0.6
        cx, cy = rotate_pad_offset(w / 2, h / 2, angle)
        norm = (cx * cx + cy * cy) ** 0.5
        px = FX - cx / norm * 2.95 - cx
        py = FY - cy / norm * 2.95 - cy
        right = _fp("J9", (px, py), [_pad("1", "GND", size=(w, h), rotation=angle)])
        mirrored = _fp("J9", (px, py), [_pad("1", "GND", size=(w, h), rotation=-angle)])
        assert _included(right)
        assert not _included(mirrored)

    @pytest.mark.parametrize("angle", [0.0, 37.0, 90.0])
    def test_circular_pad(self, angle):
        # Diameter 1.2 mm; the edge is at centre distance - 0.6.
        for d, expected in ((2.95, True), (3.05, False)):
            fp = _fp(
                "J9",
                (FX + d + 0.6, FY),
                [_pad("1", "GND", size=(1.2, 1.2), shape="circle", rotation=angle)],
            )
            assert _included(fp) is expected, (angle, d)

    @pytest.mark.parametrize("angle", ANGLES)
    def test_oval_pad(self, angle):
        """2.0x0.6 stadium: the finding faces a cap diagonally."""
        import math

        from kicad_tools.core.geometry import rotate_pad_offset

        w, h = 2.0, 0.6
        r = h / 2
        # Cap centre at local (+0.7, 0); approach it from local 45 degrees.
        kx, ky = rotate_pad_offset(w / 2 - r, 0.0, angle)
        dx, dy = rotate_pad_offset(math.sqrt(0.5), math.sqrt(0.5), angle)
        for d, expected in ((2.95, True), (3.05, False)):
            # finding = pad + cap + dir * (r + d)  =>  pad = finding - ...
            px = FX - kx - dx * (r + d)
            py = FY - ky - dy * (r + d)
            fp = _fp(
                "J9",
                (px, py),
                [_pad("1", "GND", size=(w, h), shape="oval", rotation=angle)],
            )
            # A rectangle envelope would reach the corner at ~2.86 mm and
            # include the 3.05 mm case; the stadium outline must not.
            assert _included(fp) is expected, (angle, d)

    def test_outline_distance_matches_shapely(self):
        """Exact distance agrees with Shapely for many shapes, angles and bboxes."""
        shapely = pytest.importorskip("shapely")
        import random

        from shapely.affinity import rotate, translate
        from shapely.geometry import Point, box

        from kicad_tools.validate.evidence import _pad_bbox_distance, _pad_shape

        rng = random.Random(6011)
        for _ in range(400):
            shape = rng.choice(["rect", "circle", "oval", "roundrect"])
            w, h = rng.uniform(0.2, 3.0), rng.uniform(0.2, 3.0)
            if shape == "circle":
                h = w
            angle = rng.uniform(-360, 360)
            pad = _pad("1", "GND", size=(w, h), shape=shape, rotation=angle)
            x, y = rng.uniform(-6, 6), rng.uniform(-6, 6)
            bx0, by0 = rng.uniform(-3, 3), rng.uniform(-3, 3)
            if rng.random() < 0.4:
                bbox = (bx0, by0, bx0, by0)
            else:
                bbox = (bx0, by0, bx0 + rng.uniform(0, 3), by0 + rng.uniform(0, 3))
            if shape == "circle":
                poly = Point(0, 0).buffer(w / 2, quad_segs=256)
            elif shape == "oval":
                r = min(w, h) / 2
                core = box(-(w / 2 - r), -(h / 2 - r), w / 2 - r, h / 2 - r)
                poly = core.buffer(r, quad_segs=256)
            else:
                poly = box(-w / 2, -h / 2, w / 2, h / 2)
            # KiCad negates the stored angle relative to Shapely's CCW rotate.
            poly = translate(rotate(poly, -angle, origin=(0, 0)), x, y)
            target = shapely.geometry.box(*bbox) if bbox[0] != bbox[2] else Point(bbox[:2])
            expected = poly.distance(target)
            got = _pad_bbox_distance(x, y, _pad_shape(pad), bbox)
            # roundrect is modelled as its enclosing rectangle (conservative).
            assert got == pytest.approx(expected, abs=2e-4), (shape, w, h, angle, x, y, bbox)


class TestExactRuleRadius:
    """Issue #6041: bare clearance-style rule ids get the documented 3 mm."""

    @pytest.mark.parametrize(
        "rule_id", ["clearance", "dimensions", "width_consistency", "mask_to_copper"]
    )
    def test_bare_rule_ids_use_3mm(self, rule_id):
        assert net_evidence_radius(rule_id) == 3.0
        # A pad 4 mm away is outside 3 mm, so it must not change the hash.
        pad = _fp("C9", (14.5, 20.0), [_pad("1", "GND", size=(0.0, 0.0))])
        v = _finding(rule_id)
        assert _hash(_board([pad]), v) == _hash(_board(), v)

    def test_exact_match_does_not_leak_to_lookalikes(self):
        # Exact entries only: an unrelated id sharing the stem keeps the default.
        assert net_evidence_radius("clearances_summary") == 5.0
        assert net_evidence_radius("dimensionsx") == 5.0


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
