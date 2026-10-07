"""Geometry-based finding keys for copper findings (Issue #6088).

``kct check`` used to name tracks and vias in a finding's items by UUID
(``Trace-1a2b3c4d``).  An unseeded ``kct route`` mints fresh ``uuid4()`` UUIDs
for every segment and via, so two routes with identical copper produced
different keys and a keyed waiver never carried over.  These tests emit the
same copper twice through the router's own serializer (fresh UUIDs each
time, exactly as two unseeded routes would) and check that keys, evidence
hashes and waivers carry over -- and that moving copper changes them.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from kicad_tools.cli import check_cmd
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Segment as RouterSegment
from kicad_tools.router.primitives import Via as RouterVia
from kicad_tools.router.primitives import segment_45_enforcement_disabled
from kicad_tools.schema.pcb import PCB, Arc, Segment, Via
from kicad_tools.validate.copper_refs import run_ref, segment_ref, via_ref

FIXTURE = Path(__file__).parent / "fixtures" / "projects"

_COPPER_TOKENS = ("Trace@", "Via@", "Arc@", "Run@")


# ---------------------------------------------------------------------------
# Descriptor unit tests
# ---------------------------------------------------------------------------


def _seg(start, end, *, width=0.25, layer="F.Cu", uuid="") -> Segment:
    return Segment(start=start, end=end, width=width, layer=layer, net_number=1, uuid=uuid)


def _via(pos, *, layers=("F.Cu", "B.Cu"), drill=0.3, size=0.6, uuid="") -> Via:
    return Via(position=pos, size=size, drill=drill, layers=list(layers), net_number=1, uuid=uuid)


class TestDescriptors:
    def test_segment_ignores_uuid(self):
        a = _seg((1.0, 2.0), (3.0, 2.0), uuid="aaaaaaaa-1")
        b = _seg((1.0, 2.0), (3.0, 2.0), uuid="bbbbbbbb-2")
        assert segment_ref(a) == segment_ref(b) == "Trace@F.Cu:w0.25:1/2~3/2"

    def test_segment_endpoint_order_is_canonical(self):
        assert segment_ref(_seg((3.0, 2.0), (1.0, 2.0))) == segment_ref(
            _seg((1.0, 2.0), (3.0, 2.0))
        )

    def test_segment_quantised_to_one_micron(self):
        base = segment_ref(_seg((1.0, 2.0), (3.0, 2.0)))
        # Float noise below the quantum keeps the key ...
        assert segment_ref(_seg((1.0000004, 2.0), (3.0, 1.9999996))) == base
        # ... a move of more than the quantum changes it.
        assert segment_ref(_seg((1.002, 2.0), (3.0, 2.0))) != base

    def test_half_micron_coordinates_survive_a_non_round_origin_move(self):
        # Sheet coordinates sitting exactly on a half-um boundary must keep
        # their key when only the board origin moves: PCB.load stores
        # ``sheet - origin`` and the key adds the origin back, which leaves
        # float noise that must not flip the rounding.
        for i in range(2000):
            sx, sy = 100.0005 + i * 0.001, 50.0015 + i * 0.002
            keys = set()
            for origin in ((0.0, 0.0), (108.5, 57.5), (12.3457, 98.7653), (0.1234, 0.0007)):
                rel_a = (sx - origin[0], sy - origin[1])
                rel_b = (sx + 10.0 - origin[0], sy - origin[1])
                keys.add(segment_ref(_seg(rel_a, rel_b), origin))
            assert len(keys) == 1, (sx, sy, keys)

    def test_segment_width_and_layer_discriminate(self):
        base = segment_ref(_seg((1.0, 2.0), (3.0, 2.0)))
        assert segment_ref(_seg((1.0, 2.0), (3.0, 2.0), width=0.3)) != base
        assert segment_ref(_seg((1.0, 2.0), (3.0, 2.0), layer="B.Cu")) != base

    def test_collinear_overlapping_segments_are_distinct(self):
        a = segment_ref(_seg((0.0, 0.0), (10.0, 0.0)))
        b = segment_ref(_seg((5.0, 0.0), (15.0, 0.0)))
        assert a != b

    def test_large_coordinates_keep_full_resolution(self):
        """``:g`` would truncate 1234.567 to 6 significant digits."""
        ref = segment_ref(_seg((1234.567, 2000.001), (1234.567, 2001.0)))
        assert ref == "Trace@F.Cu:w0.25:1234.567/2000.001~1234.567/2001"

    def test_negative_and_zero_coordinates(self):
        assert segment_ref(_seg((-0.0001, 0.0), (-1.5, 0.0))) == "Trace@F.Cu:w0.25:-1.5/0~0/0"

    def test_arc_includes_midpoint_and_is_reversal_stable(self):
        fwd = Arc(
            start=(0.0, 0.0), end=(2.0, 0.0), mid=(1.0, 1.0), width=0.2, layer="F.Cu", net_number=1
        )
        rev = Arc(
            start=(2.0, 0.0), end=(0.0, 0.0), mid=(1.0, 1.0), width=0.2, layer="F.Cu", net_number=1
        )
        other = Arc(
            start=(0.0, 0.0), end=(2.0, 0.0), mid=(1.0, -1.0), width=0.2, layer="F.Cu", net_number=1
        )
        assert segment_ref(fwd) == segment_ref(rev) == "Arc@F.Cu:w0.2:0/0~1/1~2/0"
        assert segment_ref(other) != segment_ref(fwd)

    def test_via_ignores_uuid_and_layer_order(self):
        a = _via((10.0, 20.0), uuid="aaaaaaaa")
        b = _via((10.0, 20.0), layers=("B.Cu", "F.Cu"), uuid="bbbbbbbb")
        assert via_ref(a) == via_ref(b) == "Via@10/20:F.Cu-B.Cu:d0.3/s0.6"

    def test_stacked_vias_with_different_spans_are_distinct(self):
        through = _via((10.0, 20.0))
        blind = _via((10.0, 20.0), layers=("F.Cu", "In1.Cu"))
        buried = _via((10.0, 20.0), layers=("In1.Cu", "In2.Cu"))
        assert len({via_ref(through), via_ref(blind), via_ref(buried)}) == 3
        assert via_ref(blind) == "Via@10/20:F.Cu-In1.Cu:d0.3/s0.6"

    def test_via_size_and_drill_discriminate(self):
        base = via_ref(_via((10.0, 20.0)))
        assert via_ref(_via((10.0, 20.0), size=0.7)) != base
        assert via_ref(_via((10.0, 20.0), drill=0.25)) != base

    def test_run_ref_is_direction_independent(self):
        assert run_ref("F.Cu", 0.2, (5.0, 1.0), (0.0, 1.0), 3) == run_ref(
            "F.Cu", 0.2, (0.0, 1.0), (5.0, 1.0), 3
        )
        assert run_ref("F.Cu", 0.2, (0.0, 1.0), (5.0, 1.0), 3) == "Run@F.Cu:w0.2:0/1~5/1:n3"

    def test_run_ref_digest_covers_interior_vertices(self):
        """Same end points and count, different bend: different key (review fix)."""
        ends = ((0.0, 0.0), (4.0, 0.0))
        bent_up = [(0.0, 0.0), (2.0, 1.0), (4.0, 0.0)]
        bent_down = [(0.0, 0.0), (2.0, -1.0), (4.0, 0.0)]
        up = run_ref("F.Cu", 0.2, *ends, 2, vertices=bent_up)
        assert up != run_ref("F.Cu", 0.2, *ends, 2, vertices=bent_down)
        assert up == run_ref("F.Cu", 0.2, *ends, 2, vertices=bent_up[::-1])
        # Moving the interior vertex by less than the 1 um quantum keeps the key.
        nudged = [(0.0, 0.0), (2.0, 1.0004), (4.0, 0.0)]
        assert up == run_ref("F.Cu", 0.2, *ends, 2, vertices=nudged)
        assert "," not in up and "|" not in up

    def test_descriptors_avoid_key_separators(self):
        for ref in (
            segment_ref(_seg((1.0, 2.0), (3.0, 2.0), layer="F,Cu|x")),
            via_ref(_via((1.0, 2.0))),
        ):
            assert "," not in ref and "|" not in ref


# ---------------------------------------------------------------------------
# Two "routes" of the same copper, end to end through ``kct check``
# ---------------------------------------------------------------------------

# Copper laid out in an empty strip of the fixture board (y = 40), using the
# fixture's nets: 1 VIN, 2 GND, 3 NET1.
#   A (VIN)  and B (GND): parallel, 0.05 mm apart       -> segment-segment
#   C (GND): collinear with and overlapping B, also next to A -> a distinct finding
#   V (NET1) and D (GND): via 0.075 mm from a trace     -> segment-via
_SEGMENTS = {
    "A": ((105.0, 40.0), (115.0, 40.0), 1, "VIN"),
    "B": ((105.0, 40.3), (115.0, 40.3), 2, "GND"),
    "C": ((110.0, 40.3), (120.0, 40.3), 2, "GND"),
    "D": ((130.5, 39.0), (130.5, 41.0), 2, "GND"),
}
_VIA = ((130.0, 40.0), 3, "NET1")


def _copper_sexp(moved: dict[str, tuple[float, float]] | None = None) -> str:
    """Serialize the test copper through the router's emitter (fresh UUIDs)."""
    moved = moved or {}
    parts = []
    with segment_45_enforcement_disabled():
        for name, (start, end, net, net_name) in _SEGMENTS.items():
            dx, dy = moved.get(name, (0.0, 0.0))
            seg = RouterSegment(
                x1=start[0] + dx,
                y1=start[1] + dy,
                x2=end[0] + dx,
                y2=end[1] + dy,
                width=0.25,
                layer=Layer.F_CU,
                net=net,
                net_name=net_name,
            )
            parts.append(seg.to_sexp())
    (vx, vy), vnet, vname = _VIA
    via = RouterVia(
        x=vx,
        y=vy,
        drill=0.3,
        diameter=0.6,
        layers=(Layer.F_CU, Layer.B_CU),
        net=vnet,
        net_name=vname,
    )
    parts.append(via.to_sexp())
    return "\n\t".join(parts)


def _routed_board(dest: Path, moved: dict[str, tuple[float, float]] | None = None) -> Path:
    shutil.copytree(FIXTURE, dest)
    pcb = dest / "test_project.kicad_pcb"
    text = pcb.read_text().rstrip()
    assert text.endswith(")")
    pcb.write_text(text[:-1] + "\t" + _copper_sexp(moved) + "\n)\n")
    return pcb


def _uuids(pcb: Path) -> set[str]:
    board = PCB.load(str(pcb))
    return {item.uuid for item in [*board.segments, *board.vias] if item.uuid}


# Only the copper clearance rules matter here; skipping the rest keeps each
# ``kct check`` run to a few seconds.
_SCOPE = ("--only", "clearance", "--drc-only")


def _check(capsys, pcb: Path) -> list[dict]:
    check_cmd.main([str(pcb), *_SCOPE, "--format", "json"])
    return json.loads(capsys.readouterr().out)["violations"]


def _copper(findings: list[dict]) -> list[dict]:
    return [v for v in findings if any(t in v["key"] for t in _COPPER_TOKENS)]


@pytest.fixture
def two_routes(tmp_path) -> tuple[Path, Path]:
    return _routed_board(tmp_path / "run1"), _routed_board(tmp_path / "run2")


def test_identical_geometry_gives_identical_copper_keys(two_routes, capsys):
    """AC 1: identical copper under fresh UUIDs -> identical keys and hashes."""
    run1, run2 = two_routes
    # Only the fixture's own pre-existing copper keeps its UUIDs; every
    # router-emitted item got a fresh one, as in an unseeded re-route.
    fixture_uuids = _uuids(FIXTURE / "test_project.kicad_pcb")
    assert _uuids(run1) - fixture_uuids
    assert (_uuids(run1) & _uuids(run2)) == fixture_uuids

    first = _copper(_check(capsys, run1))
    second = _copper(_check(capsys, run2))
    rules = {v["rule_id"] for v in first}
    assert {"clearance_segment_segment", "clearance_segment_via"} <= rules
    assert len(first) >= 3

    def ident(findings):
        return sorted((v["key"], v["evidence_hash"]) for v in findings)

    assert ident(first) == ident(second)
    # Every copper finding has its own key (collinear B/C stay distinct).
    assert len({v["key"] for v in first}) == len(first)
    # No UUID leaks into a key.
    assert not any(re.search(r"\b(Trace|Via)-", v["key"]) for v in first)


def test_waiver_from_run1_carries_over_to_run2(two_routes, capsys):
    """AC 2: a waiver recorded on run 1 suppresses the same finding on run 2."""
    run1, run2 = two_routes
    target = next(
        v for v in _copper(_check(capsys, run1)) if v["rule_id"] == "clearance_segment_via"
    )
    check_cmd.main(
        [
            str(run1),
            *_SCOPE,
            "--waive",
            target["key"],
            "--waive-reason",
            "reviewed",
            "--waive-reviewer",
            "pytest",
            "--format",
            "json",
        ]
    )
    capsys.readouterr()
    sidecar1 = run1.with_name("test_project.kct-waivers.json")
    assert sidecar1.is_file()
    shutil.copy(sidecar1, run2.with_name(sidecar1.name))

    after = _check(capsys, run2)
    waived = [v for v in after if v["key"] == target["key"]]
    assert waived and all(v["waived"] for v in waived)
    stale_or_unused = [v for v in after if v["rule_id"] in ("waiver_stale", "waiver_unused")]
    assert stale_or_unused == []


def test_moving_one_segment_changes_only_its_keys(tmp_path, capsys):
    """AC 3: move D past the quantum; only findings that name D change key."""
    base = _copper(_check(capsys, _routed_board(tmp_path / "base")))
    moved = _copper(_check(capsys, _routed_board(tmp_path / "moved", {"D": (0.01, 0.0)})))

    d_old = "Trace@F.Cu:w0.25:130.5/39~130.5/41"
    d_new = "Trace@F.Cu:w0.25:130.51/39~130.51/41"
    base_keys = {v["key"] for v in base}
    moved_keys = {v["key"] for v in moved}
    assert any(d_old in k for k in base_keys)
    assert any(d_new in k for k in moved_keys)
    assert {k for k in base_keys if d_old not in k} == {k for k in moved_keys if d_new not in k}


def test_waiver_goes_stale_or_unmatched_when_copper_moves(tmp_path, capsys):
    """AC 3: a waiver for D's finding does not cover D after it moves."""
    run1 = _routed_board(tmp_path / "run1")
    target = next(
        v for v in _copper(_check(capsys, run1)) if v["rule_id"] == "clearance_segment_via"
    )
    check_cmd.main(
        [
            str(run1),
            *_SCOPE,
            "--waive",
            target["key"],
            "--waive-reason",
            "r",
            "--waive-reviewer",
            "p",
        ]
    )
    capsys.readouterr()
    run2 = _routed_board(tmp_path / "run2", {"D": (0.01, 0.0)})
    shutil.copy(
        run1.with_name("test_project.kct-waivers.json"),
        run2.with_name("test_project.kct-waivers.json"),
    )
    after = _check(capsys, run2)
    moved_finding = [v for v in _copper(after) if v["rule_id"] == "clearance_segment_via"]
    assert moved_finding and not any(v["waived"] for v in moved_finding)


# ---------------------------------------------------------------------------
# Upgrade path: UUID-named copper waivers point at their replacement keys
# ---------------------------------------------------------------------------


def _unused_hint(capsys, pcb: Path, entry: dict) -> str:
    pcb.with_name("test_project.kct-waivers.json").write_text(
        json.dumps({"version": 3, "waivers": [entry]})
    )
    unused = [v for v in _check(capsys, pcb) if v["rule_id"] == "waiver_unused"]
    assert len(unused) == 1
    return unused[0]["message"]


def test_legacy_uuid_keyed_waiver_lists_geometry_keys(tmp_path, capsys):
    pcb = _routed_board(tmp_path / "run")
    new = [
        v["key"] for v in _copper(_check(capsys, pcb)) if v["rule_id"] == "clearance_segment_via"
    ]
    assert len(new) == 1
    message = _unused_hint(
        capsys,
        pcb,
        {
            "key": "clearance_segment_via|Trace-7ebe780f,Via-3b0d0dc9|GND,NET1|F.Cu",
            "evidence_hash": "ev2:0123456789abcdef",
            "reason": "r",
            "reviewer": "ee",
            "date": "2026-10-06",
        },
    )
    assert repr(new[0]) in message
    assert "tracks and vias by their geometry" in message


def test_legacy_uuid_items_waiver_lists_geometry_keys(tmp_path, capsys):
    pcb = _routed_board(tmp_path / "run")
    findings = _copper(_check(capsys, pcb))
    target = next(v for v in findings if v["rule_id"] == "clearance_segment_via")
    message = _unused_hint(
        capsys,
        pcb,
        {
            "rule": "clearance_segment_via",
            "items": ["Trace-7ebe780f", "Via-3b0d0dc9"],
            "reason": "r",
            "issue": "x#1",
        },
    )
    assert repr(target["key"]) in message


def test_unused_geometry_keyed_waiver_gets_no_rewaive_hint(tmp_path, capsys):
    """A current-format key that matches nothing is not a legacy name."""
    pcb = _routed_board(tmp_path / "run")
    message = _unused_hint(
        capsys,
        pcb,
        {
            "key": (
                "clearance_segment_via|Trace@F.Cu:w0.25:1/1~2/1,"
                "Via@9/9:F.Cu-B.Cu:d0.3/s0.6|GND,NET1|F.Cu"
            ),
            "evidence_hash": "ev2:0123456789abcdef",
            "reason": "r",
            "reviewer": "ee",
            "date": "2026-10-06",
        },
    )
    assert "--waive" not in message


_RUN_KEY = "width_consistency|Run@F.Cu:w0.2:0/0~4/0:n2:h0123456789ab|NET1|F.Cu"


def test_legacy_uuid_run_items_waiver_matches_run_finding():
    from kicad_tools.validate.rules.waivers import Waiver, _rewaive_candidates
    from kicad_tools.validate.violations import DRCViolation

    finding = DRCViolation(
        rule_id="width_consistency",
        severity="warning",
        message="m",
        location=(1.0, 1.0),
        layer="F.Cu",
        items=("Run@F.Cu:w0.2:0/0~4/0:n2:h0123456789ab",),
        nets=("NET1",),
    )
    uuids = ("7ebe780f-1111-4222-8333-444455556666", "3b0d0dc9-1111-4222-8333-444455556666")
    entry = Waiver(
        rule="width_consistency",
        items=frozenset(uuids),
        nets=frozenset({"NET1"}),
        reason="r",
        issue="x#1",
    )
    assert _rewaive_candidates(entry, [finding]) == [finding.key]
    other = Waiver(**{**entry.__dict__, "nets": frozenset({"OTHER"})})
    assert _rewaive_candidates(other, [finding]) == []


def test_legacy_uuid_keyed_run_waiver_matches_run_key():
    from kicad_tools.validate.rules.waivers import _coarsen_key

    legacy = (
        "width_consistency|3b0d0dc9-1111-4222-8333-444455556666,"
        "7ebe780f-1111-4222-8333-444455556666|NET1|F.Cu"
    )
    assert _coarsen_key(legacy) == _coarsen_key(_RUN_KEY) == "width_consistency|Run|NET1|F.Cu"
