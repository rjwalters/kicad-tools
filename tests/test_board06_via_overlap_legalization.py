"""Historical routing repairs must use the current physical drill-overlap rule."""

import importlib.util
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.router.quantize import segment_angle_census
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.via_in_pad import ViaInPadRule

ROOT = Path(__file__).resolve().parents[1] / "boards/06-diffpair-test"


@pytest.fixture
def recipe(monkeypatch):
    for name in ("generate_pcb", "generate_schematic", "generate_design"):
        spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("trunk_width", [0.387, 0.272])
def test_long_connector_restores_impedance_width_after_pad_escape(recipe, trunk_width):
    legs = recipe._connector_profile([(113.1, 66.2), (113.1, 64.4)], 0.15, trunk_width)
    assert legs == [
        ((113.1, 66.2), (113.1, 65.45), 0.15),
        ((113.1, 65.45), (113.1, 64.4), trunk_width),
    ]


def test_connector_neckdown_budget_is_cumulative_across_bends(recipe):
    import math

    points = [(0, 0), (0.3, 0), (0.3, 0.3), (0.6, 0.3), (0.6, 0.6), (0.9, 0.6)]
    legs = recipe._connector_profile(points, 0.15, 0.387)
    narrow = sum(math.dist(a, b) for a, b, width in legs if width == 0.15)
    wide = sum(math.dist(a, b) for a, b, width in legs if width == 0.387)
    assert narrow == pytest.approx(0.75)
    assert wide == pytest.approx(0.75)
    assert legs[0][0] == points[0]
    assert legs[-1][1] == points[-1]
    assert all(left[1] == right[0] for left, right in zip(legs, legs[1:], strict=False))


def test_legalizer_repairs_partial_drill_overlaps_without_changing_fixture(tmp_path, recipe):
    fixture = ROOT / "regression-fixture/diffpair_test_routed.kicad_pcb"
    original = fixture.read_bytes()
    candidate = tmp_path / fixture.name
    shutil.copy2(fixture, candidate)
    checker = ViaInPadRule()
    rules = SimpleNamespace(via_in_pad_supported=False)
    before = PCB.load(candidate)
    assert len(checker.check(before, rules).violations) == 7
    assert recipe._legalize_signal_vias(candidate) == 7
    identities = re.findall(r'\(uuid "([^"]+)"\)', candidate.read_text())
    assert len(identities) == len(set(identities))
    after = PCB.load(candidate)
    assert not checker.check(after, rules).violations
    assert len(after.vias) == len(before.vias)
    assert not segment_angle_census(candidate)[1]
    assert fixture.read_bytes() == original
    assert [
        (f.reference, f.position, [(p.number, p.position, p.net_name) for p in f.pads])
        for f in after.footprints
    ] == [
        (f.reference, f.position, [(p.number, p.position, p.net_name) for p in f.pads])
        for f in before.footprints
    ]


def write_overlap_board(path, count=12, pad_size=2, via_dx=1.05):
    nodes = [
        "(kicad_pcb (version 20240108) (generator pcbnew)",
        '(general (thickness 1.6)) (layers (0 "F.Cu" signal) (31 "B.Cu" signal))',
        "(gr_rect (start 98.5 47.5) (end 198.5 127.5) "
        '(stroke (width .1) (type default)) (fill none) (layer "Edge.Cuts"))',
    ]
    for i in range(count):
        x, y = 110 + 15 * (i % 4), 60 + 18 * (i // 4)
        net = i + 1
        nodes.extend(
            [
                f'(net {net} "N{net}")',
                f'(footprint "test" (layer "F.Cu") (at {x} {y}) '
                f'(property "Reference" "R{net}") '
                f'(pad "1" smd rect (at 0 0) (size {pad_size} {pad_size}) (layers "F.Cu" "F.Mask") '
                f'(net {net} "N{net}")))',
                f"(via (at {x + via_dx} {y}) (size .6) (drill .25) "
                f'(layers "F.Cu" "B.Cu") (net {net}) '
                f'(uuid "00000000-0000-4000-8000-{net:012d}"))',
            ]
        )
    path.write_text("\n".join(nodes) + "\n)\n")


def test_legalizer_does_not_stop_after_eight_repairs(tmp_path, recipe):
    path = tmp_path / "many.kicad_pcb"
    write_overlap_board(path)
    checker, rules = ViaInPadRule(), SimpleNamespace(via_in_pad_supported=False)
    assert len(checker.check(PCB.load(path), rules).violations) == 12
    assert recipe._legalize_signal_vias(path) == 12
    assert not checker.check(PCB.load(path), rules).violations
    assert len(PCB.load(path).vias) == 12
    assert not segment_angle_census(path)[1]


def _via_pad_gap(path, pad_size=2):
    """Annulus-to-own-pad gap (mm) of the single via on a one-net board.

    ``write_overlap_board`` puts net 1's pad at (110, 60).
    """
    from shapely.geometry import Point, box

    text = path.read_text()
    vx, vy = map(float, re.search(r"\(via \(at ([\d.]+) ([\d.]+)\)", text).groups())
    size = float(re.search(r"\(via .*?\(size ([\d.]+)\)", text).group(1))
    half = pad_size / 2
    return (
        Point(vx, vy).buffer(size / 2).distance(box(110 - half, 60 - half, 110 + half, 60 + half))
    )


def test_relocation_walks_out_of_same_net_slit_band(tmp_path, recipe, monkeypatch):
    """Issue #9114: a relocated via must not open a (0, edge) same-net slit."""
    pad_edge, _trace_edge = recipe._same_net_band_edges()
    assert pad_edge > 0  # jlcpcb 4layer_1oz resolves a pad_spacing edge

    # Control: with the band disabled the ring seats the via at +0.9 mm east,
    # an annulus gap of ~0.05 mm to its own pad -- inside (0, edge).
    control = tmp_path / "control.kicad_pcb"
    write_overlap_board(control, count=1, pad_size=2, via_dx=0.45)
    monkeypatch.setattr(recipe, "_same_net_band_edges", lambda: (0.0, 0.0))
    assert recipe._legalize_signal_vias(control) == 1
    assert 0 < _via_pad_gap(control) < pad_edge

    monkeypatch.undo()
    path = tmp_path / "band.kicad_pcb"
    write_overlap_board(path, count=1, pad_size=2, via_dx=0.45)
    assert recipe._legalize_signal_vias(path) == 1
    gap = _via_pad_gap(path)
    assert gap <= 1e-6 or gap >= pad_edge
    assert (
        not ViaInPadRule()
        .check(PCB.load(path), SimpleNamespace(via_in_pad_supported=False))
        .violations
    )


def test_grid_escape_seat_is_graded_through_the_band(recipe):
    """Issue #9114: the same predicate rejects in-band seats, accepts both sides."""
    from shapely.geometry import LineString, Point

    pad = [(-1.0, -1.0, 1.0, 1.0)]
    dia = 0.5
    # Annulus edge 0.05 from the pad (in band) / touching / clear of the band.
    assert not recipe._same_net_band_seat_ok(1.3, 0, dia, [], pad, 0.15, 0.15)
    assert recipe._same_net_band_seat_ok(1.25, 0, dia, [], pad, 0.15, 0.15)
    assert recipe._same_net_band_seat_ok(1.5, 0, dia, [], pad, 0.15, 0.15)
    # Same-net segment / via copper grade on the trace edge.
    seg = LineString([(0, 1.65), (3, 1.65)]).buffer(0.075)
    assert not recipe._same_net_band_seat_ok(1.3, 2.0, dia, [seg], [], 0.15, 0.15)
    assert not recipe._same_net_band_seat_ok(
        0, 0, dia, [Point(0.65, 0).buffer(0.3)], [], 0.15, 0.15
    )
    # Edge 0 (profile without pad_spacing) leaves behavior unchanged.
    assert recipe._same_net_band_seat_ok(1.3, 0, dia, [seg], pad, 0.0, 0.0)


def write_drill_pair_slide_board(path):
    """Two same-net vias 0.25 mm drill-edge apart, joined by one F.Cu stub.

    The victim (``B``, ``(120.5, 70)``) has a single attached layer, so the
    staple collapse is skipped and the class-1 *slide* runs.  The first ring
    seat is ``+0.25 mm`` east, ``(120.75, 70)``; an own-net 0.5 mm SMD pad
    sits so that seat's 0.45 mm annulus is 0.1 mm from the pad edge -- inside
    the ``(0, 0.15)`` pad-population slit band (Issue #9127).
    """
    nodes = [
        "(kicad_pcb (version 20240108) (generator pcbnew)",
        '(general (thickness 1.6)) (layers (0 "F.Cu" signal) (31 "B.Cu" signal))',
        "(gr_rect (start 98.5 47.5) (end 198.5 127.5) "
        '(stroke (width .1) (type default)) (fill none) (layer "Edge.Cuts"))',
        '(net 1 "N1")',
        '(footprint "test" (layer "F.Cu") (at 121.325 70) '
        '(property "Reference" "R1") '
        '(pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu" "F.Mask") '
        '(net 1 "N1")))',
        '(via (at 120 70) (size .45) (drill .25) (layers "F.Cu" "B.Cu") (net 1) '
        '(uuid "00000000-0000-4000-8000-00000000000a"))',
        '(via (at 120.5 70) (size .45) (drill .25) (layers "F.Cu" "B.Cu") (net 1) '
        '(uuid "00000000-0000-4000-8000-00000000000b"))',
        "(segment (start 120 70) (end 120.5 70) (width .2) "
        '(layer "F.Cu") (net 1) (uuid "00000000-0000-4000-8000-00000000000c"))',
    ]
    path.write_text("\n".join(nodes) + "\n)\n")


def _slid_via_pad_gap(path):
    """Annulus gap (mm) from the slid victim via to the own-net pad."""
    from shapely.geometry import Point, box

    pad = box(121.075, 69.75, 121.575, 70.25)
    gaps = []
    for x, y, size in re.findall(
        r"\(via \(at ([\d.]+) ([\d.]+)\) \(size ([\d.]+)\)", path.read_text()
    ):
        if (float(x), float(y)) != (120.0, 70.0):
            gaps.append(Point(float(x), float(y)).buffer(float(size) / 2).distance(pad))
    assert len(gaps) == 1
    return gaps[0]


def test_drill_pair_slide_walks_out_of_same_net_slit_band(tmp_path, recipe, monkeypatch):
    """Issue #9127: the class-1 victim slide must not open a (0, edge) slit.

    Measured: with the band disabled the first ring seat (+0.25 mm east)
    lands 0.1 mm from the victim's own-net pad, inside ``(0, 0.15)``.  The
    slide keeps the victim's chain and only adds connectors, so the only
    exclusion the check needs is the victim via itself.
    """
    pad_edge, _trace_edge = recipe._same_net_band_edges()
    assert pad_edge > 0

    control = tmp_path / "control.kicad_pcb"
    write_drill_pair_slide_board(control)
    monkeypatch.setattr(recipe, "_same_net_band_edges", lambda: (0.0, 0.0))
    assert recipe._legalize_signal_vias(control) == 1
    assert 0 < _slid_via_pad_gap(control) < pad_edge

    monkeypatch.undo()
    path = tmp_path / "band.kicad_pcb"
    write_drill_pair_slide_board(path)
    assert recipe._legalize_signal_vias(path) == 1
    gap = _slid_via_pad_gap(path)
    assert gap <= 1e-6 or gap >= pad_edge
    # The drill pair itself is still repaired (>= 0.5 mm edge to edge).
    ((bx, by),) = [
        (float(x), float(y))
        for x, y in re.findall(r"\(via \(at ([\d.]+) ([\d.]+)\)", path.read_text())
        if (float(x), float(y)) != (120.0, 70.0)
    ]
    assert ((bx - 120.0) ** 2 + (by - 70.0) ** 2) ** 0.5 - 0.25 >= 0.5 - 1e-3


def test_legalizer_reports_overlap_without_a_legal_escape(tmp_path, recipe):
    path = tmp_path / "blocked.kicad_pcb"
    # A broad SMT land surrounds every candidate in the bounded search.
    write_overlap_board(path, count=1, pad_size=10)
    original = path.read_bytes()
    with pytest.raises(RuntimeError, match="1 unrepaired via-in-pad"):
        recipe._legalize_signal_vias(path)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "failure", ["split", "legalize", "refill", "pour", "clearance", "interrupt"]
)
def test_finalization_restores_copper_and_fills_on_every_failure(
    tmp_path, recipe, monkeypatch, failure
):
    path = tmp_path / "candidate.kicad_pcb"
    original = b"original copper and matching zone fills\n"
    path.write_bytes(original)
    calls = []

    def signature(candidate):
        calls.append("clearance")
        if failure == "clearance" and candidate.read_bytes() != original:
            return {("new foreign-copper violation",)}
        return set()

    def split(candidate, before):
        candidate.write_bytes(b"split copper with stale fills\n")
        if failure == "split":
            raise RuntimeError("split failed after writing")
        return 1

    def legalize(candidate):
        candidate.write_bytes(b"partially legalized copper with stale fills\n")
        if failure == "legalize":
            raise RuntimeError("1 unrepaired via-in-pad overlap")
        if failure == "interrupt":
            raise KeyboardInterrupt
        return 1

    def refill(*args, **kwargs):
        calls.append("refill")
        path.write_bytes(b"legalized copper and new fills\n")
        return SimpleNamespace(returncode=1 if failure == "refill" else 0)

    def audit(tag):
        calls.append("pour")
        return failure != "pour"

    monkeypatch.setattr(recipe, "_clearance_signature", signature)
    monkeypatch.setattr(recipe, "_repair_pour_connectivity", lambda p, nets: (0, 0))
    monkeypatch.setattr(recipe, "_split_offangle_chords", split)
    # Issue #7296: the pad-edge pass parses the board; these fixtures are bytes.
    monkeypatch.setattr(recipe, "_relocate_pad_edge_vias", lambda p: 0)
    monkeypatch.setattr(recipe, "_legalize_signal_vias", legalize)
    monkeypatch.setattr(recipe.subprocess, "run", refill)
    with pytest.raises(KeyboardInterrupt if failure == "interrupt" else RuntimeError):
        recipe._finalize_signal_copper(path, ["fill", str(path)], audit)
    assert path.read_bytes() == original
    assert calls.count("refill") <= 1  # rollback must not invoke a failing filler again
    if failure == "refill":
        assert "pour" not in calls  # stale/failed fills cannot pass the physical gate


def test_finalization_commits_only_after_successful_physical_gates(tmp_path, recipe, monkeypatch):
    path = tmp_path / "candidate.kicad_pcb"
    path.write_bytes(b"original")
    calls = []
    monkeypatch.setattr(recipe, "_clearance_signature", lambda p: {("existing",)})
    monkeypatch.setattr(recipe, "_split_offangle_chords", lambda p, before: 0)
    # Issue #7296: the pad-edge pass parses the board; these fixtures are bytes.
    monkeypatch.setattr(recipe, "_relocate_pad_edge_vias", lambda p: 0)

    def legalize(candidate):
        candidate.write_bytes(b"new copper")
        return 1

    def refill(*args, **kwargs):
        assert path.read_bytes() == b"new copper"
        path.write_bytes(b"new copper and matching fills")
        calls.append("refill")
        return SimpleNamespace(returncode=0)

    def audit(tag):
        assert path.read_bytes() == b"new copper and matching fills"
        calls.append("audit")
        return True

    monkeypatch.setattr(recipe, "_legalize_signal_vias", legalize)
    monkeypatch.setattr(recipe.subprocess, "run", refill)
    recipe._finalize_signal_copper(path, ["fill", str(path)], audit)
    assert path.read_bytes() == b"new copper and matching fills"
    assert calls == ["refill", "audit"]


def test_restarted_repair_allocator_preserves_existing_identities(recipe):
    recipe._reset_repair_uuid_counter()
    existing = [recipe._generate_uuid() for _ in range(4)]
    recipe._reset_repair_uuid_counter()
    recipe._reserve_repair_uuids("\n".join(f'(uuid "{identity}")' for identity in existing))
    resumed = [recipe._generate_uuid() for _ in range(4)]
    assert not set(existing) & set(resumed)
    assert len(set(resumed)) == 4
    recipe._reset_repair_uuid_counter()
    recipe._reserve_repair_uuids("\n".join(f'(uuid "{identity}")' for identity in existing))
    assert [recipe._generate_uuid() for _ in range(4)] == resumed


@pytest.mark.parametrize("refill_fails", [False, True])
def test_finalization_recovers_pour_cut_by_relocated_copper(
    tmp_path, recipe, monkeypatch, refill_fails
):
    path = tmp_path / "candidate.kicad_pcb"
    path.write_bytes(b"original")
    monkeypatch.setattr(recipe, "_clearance_signature", lambda p: set())
    monkeypatch.setattr(recipe, "_split_offangle_chords", lambda p, before: 0)
    # Issue #7296: the pad-edge pass parses the board; these fixtures are bytes.
    monkeypatch.setattr(recipe, "_relocate_pad_edge_vias", lambda p: 0)
    monkeypatch.setattr(recipe, "_legalize_signal_vias", lambda p: 1)
    calls = []

    def refill(*args, **kwargs):
        calls.append("refill")
        if path.read_bytes() == b"bridge":
            path.write_bytes(b"bridge and fills")
            return SimpleNamespace(returncode=1 if refill_fails else 0)
        path.write_bytes(b"legalized copper with disconnected pour")
        return SimpleNamespace(returncode=0)

    def repair(candidate, nets):
        assert candidate.read_bytes() == b"legalized copper with disconnected pour"
        assert nets == recipe.POUR_NETS
        candidate.write_bytes(b"bridge")
        calls.append("repair")
        return (0, 1)

    def audit(tag):
        return path.read_bytes() == b"bridge and fills"

    monkeypatch.setattr(recipe.subprocess, "run", refill)
    monkeypatch.setattr(recipe, "_repair_pour_connectivity", repair)
    if refill_fails:
        with pytest.raises(RuntimeError, match="pour re-fill failed"):
            recipe._finalize_signal_copper(path, ["fill"], audit)
        assert path.read_bytes() == b"original"
    else:
        recipe._finalize_signal_copper(path, ["fill"], audit)
        assert path.read_bytes() == b"bridge and fills"
    assert calls == ["refill", "repair", "refill"]
