"""Source shape survives real loading, worker reconstruction, and endpoint copies."""

import math
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kicad_tools.router import Autorouter, DesignRules
from kicad_tools.router.algorithms.evolutionary import _run_evolutionary_trial
from kicad_tools.router.core import _run_monte_carlo_trial
from kicad_tools.router.diffpair_routing import DiffPairRouter
from kicad_tools.router.io import _schema_pad_shape, load_pads_for_analysis, load_pcb_for_routing
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Segment
from kicad_tools.schema.pcb import PCB


def board_text(shape="rect", angle=45, size="4 1", extra=""):
    return f"""(kicad_pcb (version 20240108) (generator test)
      (net 0 "") (net 1 "SIGNAL")
      (gr_rect (start 0 0) (end 30 30) (layer "Edge.Cuts") (width 0.1))
      (footprint "Test:Pad" (layer "F.Cu") (at 10 10 30)
        (property "Reference" "J1" (at 0 0) (layer "F.SilkS"))
        (pad "1" smd {shape} (at 2 1 {angle}) (size {size})
          (layers "F.Cu") (net 1 "SIGNAL") {extra}))
    )"""


def load(path, loader):
    if loader == "analysis":
        return load_pads_for_analysis(path)[0]
    router, _ = load_pcb_for_routing(
        str(path),
        validate_drc=False,
        force_python=True,
        rules=DesignRules(grid_resolution=0.1),
        edge_clearance=0,
    )
    return router.pads[("J1", "1")]


@pytest.mark.parametrize("loader", ["analysis", "routing"])
@pytest.mark.parametrize(
    "shape,angle,size",
    [
        ("rect", 0, "2 2"),
        ("circle", 45, "2 2"),
        ("rect", 45, "4 1"),
        ("rect", -45, "4 1"),
        ("rect", 90, "4 1"),
        ("oval", 45, "4 1"),
        ("roundrect", 45, "2 2"),
    ],
)
def test_file_backed_shape_and_absolute_orientation(tmp_path, loader, shape, angle, size):
    path = tmp_path / "shape.kicad_pcb"
    path.write_text(board_text(shape, angle, size))
    pad = load(path, loader)
    assert pad.shape == shape
    # Footprint rotation applies once to the local offset; the pad angle is
    # already absolute and must not acquire another 30 degrees.
    assert (pad.x, pad.y) == pytest.approx((10 + math.sqrt(3) + 0.5, 10 + math.sqrt(3) / 2 - 1))
    assert pad.rotation == (0 if angle == 90 else angle % 360)
    dims = tuple(map(float, size.split()))
    assert (pad.width, pad.height) == (dims[::-1] if angle == 90 else dims)


UNSUPPORTED_COPPER = [
    (
        "custom",
        "(primitives (gr_poly (pts (xy -5 -5) (xy 5 -5) (xy 5 5)) (width 0) (fill yes)))",
    ),
    ("trapezoid", "(rect_delta 3 0)"),
    ("rect", '(padstack (mode custom) (layer "B.Cu" (shape rect) (size 8 8)))'),
]


@pytest.mark.parametrize("shape,extra", UNSUPPORTED_COPPER)
def test_unsupported_copper_rejected_by_the_analysis_loader(tmp_path, shape, extra):
    path = tmp_path / "unsupported.kicad_pcb"
    path.write_text(board_text(shape, size="1 1", extra=extra))
    with pytest.raises(ValueError, match="Unsupported routing geometry.*J1.1"):
        load(path, "analysis")


def test_padstack_copper_rejected_before_router_construction(tmp_path, monkeypatch):
    """Per-layer padstack copper is not derivable from the common pad block.

    Issue #5863 degrades a custom or trapezoid pad the routing loader cannot
    represent, but a padstack's actual per-layer copper is unknown here, so
    bounding it could under-estimate real metal -- it still refuses, and still
    before the router exists.
    """
    path = tmp_path / "padstack.kicad_pcb"
    path.write_text(board_text("rect", size="1 1", extra=UNSUPPORTED_COPPER[2][1]))
    constructor = Mock(side_effect=AssertionError("unsupported copper reached router"))
    monkeypatch.setattr("kicad_tools.router.io.Autorouter", constructor)
    with pytest.raises(ValueError, match="Unsupported routing geometry.*J1.1"):
        load(path, "routing")
    constructor.assert_not_called()


@pytest.mark.parametrize("shape,extra", UNSUPPORTED_COPPER[:2])
def test_routing_loader_degrades_unsupported_copper_to_an_obstacle(tmp_path, shape, extra):
    """Issue #5863: unsupported copper costs its net, not the whole board.

    The pad is still never a routable terminal with a substituted shape (the
    #5229 invariant); it becomes obstacle copper that CONTAINS its true
    outline, and its net drops out of routing.
    """
    from shapely.affinity import rotate, translate
    from shapely.geometry import Point, Polygon

    from kicad_tools.router.io import load_pcb_for_routing

    path = tmp_path / "unsupported.kicad_pcb"
    path.write_text(board_text(shape, size="1 1", extra=extra))
    router, nets = load_pcb_for_routing(
        str(path),
        validate_drc=False,
        force_python=True,
        rules=DesignRules(grid_resolution=0.1),
        edge_clearance=0,
    )
    assert ("J1", "1") not in router.pads
    assert nets["SIGNAL"] not in router.nets
    assert router.unsupported_geometry_nets == frozenset({"SIGNAL"})
    fills = [f for f in router.grid.fixed_fills.fills if f.source_kind == "pad"]
    assert [f.source_object_id for f in fills] == ["J1.1"]
    # The pad sits at the footprint-rotated offset, at its own absolute angle.
    theta = math.radians(-30)
    px = 10 + 2 * math.cos(theta) - 1 * math.sin(theta)
    py = 10 + 2 * math.sin(theta) + 1 * math.cos(theta)

    def board_frame(local):
        return translate(rotate(local, -45, origin=(0, 0)), px, py)

    if shape == "custom":
        # Default (circle) anchor plus the authored triangle primitive.
        true_copper = board_frame(
            Polygon([(-5, -5), (5, -5), (5, 5)]).union(Point(0, 0).buffer(0.5))
        )
    else:
        # KiCad shifts each nominal corner by half the OTHER axis' rect_delta,
        # so a 1x1 pad with (rect_delta 3 0) reaches y = +/-2.
        true_copper = board_frame(Polygon([(-0.5, 2.0), (0.5, 1.0), (0.5, -1.0), (-0.5, -2.0)]))
    assert fills[0].geometry.buffer(1e-9).covers(true_copper)


@pytest.mark.parametrize("loader", ["analysis", "routing"])
@pytest.mark.parametrize("extra", ["", "(rect_delta 0 0)"])
def test_rectangular_trapezoid_routes_as_the_rect_it_is(tmp_path, loader, extra):
    """A zero (or absent) rect_delta leaves a plain rectangle (issue #5863).

    Every trapezoid pad on the pinned dataset-srj18 OCULINK / USB-C boards is
    one of these, and refusing them lost whole boards over copper that is
    exactly a rect. The reduction is exact, so the pad stays routable.
    """
    path = tmp_path / "trapezoid.kicad_pcb"
    path.write_text(board_text("trapezoid", 45, "4 1", extra=extra))
    pad = load(path, loader)
    assert pad.shape == "rect"
    assert (pad.width, pad.height) == (4.0, 1.0)
    assert pad.rotation == 45


@pytest.mark.parametrize("shape", ["rect", "circle", "oval", "roundrect"])
def test_schema_metadata_and_padstack_guard(tmp_path, shape):
    path = tmp_path / "schema.kicad_pcb"
    path.write_text(board_text(shape, size="2 2"))
    pcb = PCB.load(str(path))
    assert _schema_pad_shape(pcb.footprints[0].pads[0], "J1") == shape
    path.write_text(board_text(shape, extra="(padstack (mode custom))"))
    pcb = PCB.load(str(path))
    with pytest.raises(ValueError, match="layer-specific padstack"):
        _schema_pad_shape(pcb.footprints[0].pads[0], "J1")


@pytest.mark.parametrize("worker", [_run_monte_carlo_trial, _run_evolutionary_trial])
def test_worker_roundtrip_preserves_geometric_discrimination(monkeypatch, worker):
    router = Autorouter(20, 20, rules=DesignRules(grid_resolution=0.1), force_python=True)
    for ref, x, shape in [("J1", 5, "rect"), ("J2", 12, "circle")]:
        router.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": 10,
                    "width": 2,
                    "height": 2,
                    "rotation": 45,
                    "shape": shape,
                    "net": 1,
                    "net_name": "SIGNAL",
                }
            ],
        )
    config = router._serialize_for_parallel()
    config.update(
        trial_num=0, chrom_idx=0, seed=0, base_order=[], net_order=[], use_negotiated=False
    )
    restored = []

    def inspect_worker(actual, order):
        restored.append(actual)
        for ref, valid in [("J1", False), ("J2", True)]:
            pad = actual.pads[(ref, "1")]
            assert pad.shape == router.pads[(ref, "1")].shape
            assert pad.rotation == 45
            # At a diamond tip, a rotated 2x2 square reaches beyond a disc.
            segment = Segment(pad.x + 1.3, 9.99, pad.x + 1.3, 10.01, 0.02, Layer.F_CU, 2)
            assert actual.grid.validate_segment_clearance(segment, exclude_net=2)[0] is valid
        return []

    monkeypatch.setattr(Autorouter, "route_all", inspect_worker)
    monkeypatch.setattr(Autorouter, "_evaluate_solution", lambda self, routes: 0)
    worker(config)
    assert len(restored) == 1


@pytest.mark.parametrize("shape", ["circle", "rect", "oval", "roundrect"])
def test_diffpair_virtual_pad_copies_shape(shape):
    router = Autorouter(20, 20, force_python=True)
    owner = SimpleNamespace(autorouter=router)
    original = Pad(5, 5, 2, 2, 1, "SIGNAL", ref="J1", pin="1", rotation=45, shape=shape)
    copied = DiffPairRouter._virtual_pad_at(owner, original, 8, 9, 0)
    assert (copied.x, copied.y) == (8, 9)
    assert (copied.shape, copied.rotation, copied.width, copied.height) == (shape, 45, 2, 2)


def _install_escape(monkeypatch, router, escape):
    monkeypatch.setattr(router._escape, "generate_escapes", lambda package: [escape])
    monkeypatch.setattr(
        router._escape, "generate_in_pad_rescues_only", lambda package, pin_filter: [escape]
    )
    monkeypatch.setattr(router._escape, "apply_escape_routes", lambda escapes: [])
    return SimpleNamespace(ref="J1", package_type=SimpleNamespace(name="TEST"))


@pytest.mark.parametrize("method", ["generate_escape_routes", "_apply_in_pad_escape_rescues"])
@pytest.mark.parametrize("shape", ["circle", "rect"])
def test_escape_virtual_pad_keeps_authored_shape_on_pad_copper(monkeypatch, method, shape):
    """An endpoint still standing on the pad's own copper keeps its geometry.

    Issue #5229 transport case: when the escape does not move the terminal
    off the pad, the authored shape/rotation/size describe real metal and
    must survive the copy.
    """
    router = Autorouter(20, 20, force_python=True)
    pad = Pad(5, 5, 2, 2, 1, "SIGNAL", ref="J1", pin="1", rotation=45, shape=shape)
    router.pads[("J1", "1")] = pad
    escape = SimpleNamespace(pad=pad, escape_point=(5, 5), escape_layer=Layer.F_CU)
    package = _install_escape(monkeypatch, router, escape)
    getattr(router, method)([package])
    copied = router._escape_pad_overrides[("J1", "1")]
    assert (copied.x, copied.y, copied.layer) == (5, 5, Layer.F_CU)
    assert (copied.shape, copied.rotation, copied.width, copied.height) == (shape, 45, 2, 2)


@pytest.mark.parametrize("method", ["generate_escape_routes", "_apply_in_pad_escape_rescues"])
@pytest.mark.parametrize("shape", ["circle", "rect"])
def test_escape_virtual_pad_sizes_moved_endpoint_to_conductor(monkeypatch, method, shape):
    """A moved endpoint is sized to the escape conductor, not the pad outline.

    Issue #5398: copying a 2 x 2 mm pad outline to an endpoint 3 mm away
    synthesized pad metal over bare board, and both A* backends waive
    blocked/clearance checks inside a terminal's metal area.
    """
    router = Autorouter(20, 20, force_python=True)
    pad = Pad(5, 5, 2, 2, 1, "SIGNAL", ref="J1", pin="1", rotation=45, shape=shape)
    router.pads[("J1", "1")] = pad
    stub = Segment(5, 5, 8, 9, 0.25, Layer.B_CU, 1, "SIGNAL")
    escape = SimpleNamespace(
        pad=pad,
        escape_point=(8, 9),
        escape_layer=Layer.B_CU,
        segments=[stub],
        via=None,
    )
    package = _install_escape(monkeypatch, router, escape)
    getattr(router, method)([package])
    copied = router._escape_pad_overrides[("J1", "1")]
    assert (copied.x, copied.y, copied.layer) == (8, 9, Layer.B_CU)
    assert (copied.shape, copied.rotation) == ("rect", 0.0)
    assert (copied.width, copied.height) == pytest.approx((0.25 / math.sqrt(2),) * 2)
    # Identity still resolves back to the physical pad.
    assert (copied.ref, copied.pin, copied.net) == ("J1", "1", 1)
