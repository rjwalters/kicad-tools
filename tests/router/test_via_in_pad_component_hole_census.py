"""Issue #5201 (reopened): via-in-pad component-hole census correctness.

The owner reopened #5201 after independently reproducing a gap PR #5364
left unfixed: for the eligible 4-layer ``jlcpcb-tier1`` process, a
candidate via with ``nearest_other_hole_distance_mm=None`` returns
eligible (``via_geometry_eligible`` skips the check -- by design, see
``tests/router/test_via_in_pad_eligibility.py``), but the production
``EscapeRouter._try_in_pad_escape`` and ``drc_nudge._scan_and_repair_via_in_pad``
never threaded a real component-hole census at all, so they ALWAYS
passed ``None`` regardless of the board's actual PTH-hole geometry --
including cases where a real nearby hole should have refused the via.

This file exercises the FULL production decision
(``EscapeRouter._try_in_pad_escape``, not just the
``via_in_pad_eligibility`` helpers) against five component-hole-census
shapes on the same 4-layer ``jlcpcb-tier1`` SSOP fixture
``tests/test_escape_via_in_pad.py`` already establishes:

* Unknown census (``component_holes=None``, ALSO the constructor's
  default) -- REFUSE.
* A verified nearby invalid hole (0.3mm PTH drill at 0.4mm centre
  distance; edge-to-edge clearance 0.1mm < the process's 0.5mm floor)
  -- REFUSE.
* A through-hole pad with a missing/zero/unparseable/non-finite drill
  anywhere on the board -- REFUSE (fail closed; its true clearance
  cannot be proven, without crashing on malformed input).
* A verified EMPTY census (no other through-hole pads at all) --
  RETAIN a connected same-net in-pad escape.
* A verified FAR hole -- RETAIN.

Plus a regression for the follow-up Judge finding on this PR: a
physically DISTINCT through-hole pad that happens to share the
candidate SMD pad's ``(ref, pin)`` (a duplicate-numbered pad) must
still be scanned, not discarded by logical-key exclusion.

See ``tests/test_drc_nudge.py::TestViaInPadComponentHoleCensus`` for the
equivalent repair-sweep (``_scan_and_repair_via_in_pad``) coverage.
"""

from __future__ import annotations

from kicad_tools.router.escape import EscapeDirection, EscapeRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# ----------------------------------------------------------------------------
# Fixtures -- mirrors tests/test_escape_via_in_pad.py's 4-layer tier1 SSOP.
# ----------------------------------------------------------------------------


def _make_rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.05,
        manufacturer="jlcpcb-tier1",
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    # 4-layer signal-signal stack: In1.Cu is a SIGNAL layer, matching
    # ``jlcpcb-tier1``'s POFV process 4-layer floor and letting
    # ``_select_inner_escape_layer`` land the stub on it.
    return RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        origin_x=-10.0,
        origin_y=-10.0,
        layer_stack=LayerStack.four_layer_sig_sig_gnd_pwr(),
    )


def _make_pad() -> Pad:
    """A single fine-pitch SSOP pad large enough to host the standard via
    (0.35 x 1.45mm, same geometry as the chorus-test-derived SSOP-28
    fixture in ``tests/test_escape_via_in_pad.py``)."""
    return Pad(
        x=0.0,
        y=0.0,
        width=0.35,
        height=1.45,
        net=5,
        net_name="NET5",
        ref="U5",
        pin="7",
        layer=Layer.F_CU,
    )


def _make_router(component_holes) -> EscapeRouter:
    rules = _make_rules()
    grid = _make_grid(rules)
    return EscapeRouter(grid, rules, component_holes=component_holes)


def _rescue(component_holes):
    """Call the production decision directly: no ``package`` (isolates
    the census check from the unrelated foreign-pad clearance rescue),
    dead-centre via placement."""
    router = _make_router(component_holes)
    pad = _make_pad()
    return router._try_in_pad_escape(
        pad=pad,
        direction=EscapeDirection.NORTH,
        effective_clearance=0.2,
        escape_width=0.2,
    )


def _pth_hole(x: float, y: float, drill: float, *, ref: str = "H1", pin: str = "1") -> Pad:
    return Pad(
        x=x,
        y=y,
        width=drill,
        height=drill,
        net=0,
        net_name="",
        layer=Layer.F_CU,
        ref=ref,
        pin=pin,
        through_hole=True,
        drill=drill,
    )


class TestTryInPadEscapeComponentHoleCensus:
    """The FIVE production-decision scenarios the owner specified."""

    def test_sanity_via_geometry_is_within_process_envelope(self):
        """Precondition sanity check: the standard via (0.3mm drill /
        0.6mm diameter -> 0.15mm annular ring) is within
        ``JLCPCB_TIER1_POFV_4L``'s drill/annular envelope on its own, so
        every REFUSE assertion below is attributable to the hole-census
        checks, not some other geometry mismatch."""
        route = _rescue(component_holes=[])
        assert route is not None
        assert route.via is not None
        assert route.via.in_pad is True
        assert route.via.drill == 0.3

    def test_unknown_census_refuses(self):
        """``component_holes=None`` (explicitly unknown/unavailable) must
        REFUSE -- fail closed rather than default to eligible."""
        route = _rescue(component_holes=None)
        assert route is None

    def test_nearby_invalid_hole_refuses(self):
        """A verified 0.3mm PTH hole at 0.4mm centre distance from the
        pad (edge-to-edge clearance 0.4 - 0.15 - 0.15 = 0.1mm, below the
        process's 0.5mm ``min_component_hole_distance_mm`` floor) must
        REFUSE."""
        near_hole = _pth_hole(0.4, 0.0, 0.3)
        route = _rescue(component_holes=[near_hole])
        assert route is None

    def test_unknown_pth_drill_refuses(self):
        """A through-hole pad with a missing/zero drill diameter is
        UNKNOWN, not proof the board has no nearby hole -- even placed
        far from the candidate pad, its own clearance cannot be
        computed, so the whole census fails closed."""
        unknown_hole = _pth_hole(50.0, 50.0, 0.0)
        route = _rescue(component_holes=[unknown_hole])
        assert route is None

    def test_verified_empty_census_retains_escape(self):
        """A verified EMPTY census (explicitly no other through-hole
        pads) is eligible-if-otherwise-qualifying -- RETAIN a connected
        same-net in-pad escape."""
        route = _rescue(component_holes=[])
        assert route is not None
        assert route.via is not None
        assert route.via.in_pad is True
        assert route.via.net == 5
        # Connected: the inner stub originates at the via position.
        assert route.segments[0].x1 == route.via_pos[0]
        assert route.segments[0].y1 == route.via_pos[1]

    def test_verified_far_hole_retains_escape(self):
        """A verified census whose only other through-hole pad is far
        away clears the process's component-hole-distance floor --
        RETAIN."""
        far_hole = _pth_hole(50.0, 50.0, 0.3)
        route = _rescue(component_holes=[far_hole])
        assert route is not None
        assert route.via is not None
        assert route.via.in_pad is True
        assert route.via.net == 5

    def test_default_component_holes_is_unknown_and_refuses(self):
        """The DEFAULT (``component_holes`` omitted entirely) is the
        UNKNOWN sentinel, per the acceptance criterion that missing
        caller context must never silently grant eligibility -- the
        constructor itself must fail closed.  A caller that has
        established the board genuinely has no other through-hole pads
        must say so explicitly with ``component_holes=()``."""
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)  # component_holes omitted -> None
        pad = _make_pad()
        route = router._try_in_pad_escape(
            pad=pad,
            direction=EscapeDirection.NORTH,
            effective_clearance=0.2,
            escape_width=0.2,
        )
        assert route is None

    def test_explicit_empty_component_holes_retains_escape(self):
        """An explicit ``component_holes=()`` at construction time is the
        verified-empty declaration and behaves identically to passing it
        per-call through ``_rescue``."""
        rules = _make_rules()
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules, component_holes=())
        pad = _make_pad()
        route = router._try_in_pad_escape(
            pad=pad,
            direction=EscapeDirection.NORTH,
            effective_clearance=0.2,
            escape_width=0.2,
        )
        assert route is not None
        assert route.via is not None
        assert route.via.in_pad is True

    def test_duplicate_ref_pin_hole_is_not_discarded(self):
        """A physically DISTINCT through-hole pad that happens to share
        the candidate SMD pad's ``(ref, pin)`` (a duplicate-numbered pad
        -- exactly the case ``all_pads``, not the lossy ``pads`` dict,
        exists to preserve) must still be scanned and still REFUSE when
        it is too close.  Excluding by logical ``(ref, pin)`` identity
        instead of object identity would silently discard it."""
        duplicate_identity_near_hole = _pth_hole(0.4, 0.0, 0.3, ref="U5", pin="7")
        route = _rescue(component_holes=[duplicate_identity_near_hole])
        assert route is None

    def test_nan_drill_refuses_without_silently_passing(self):
        """A through-hole pad with a ``NaN`` drill must not silently
        compare as "safely distant" (every ``NaN`` comparison is
        ``False``, so a naive ``<`` floor check never fires) -- it fails
        closed like any other unparseable geometry."""
        nan_hole = _pth_hole(0.4, 0.0, float("nan"))
        route = _rescue(component_holes=[nan_hole])
        assert route is None

    def test_unparseable_string_drill_refuses_without_crashing(self):
        """A non-numeric drill value must fail closed, not raise."""
        bad_hole = _pth_hole(0.4, 0.0, 0.3)
        bad_hole.drill = "bad"  # type: ignore[assignment]
        route = _rescue(component_holes=[bad_hole])
        assert route is None


def _loaded_holes(tmp_path, kind="np_thru_hole", ref="", x=0.4, y=0):
    from kicad_tools.router.io import load_pcb_for_routing

    path = tmp_path / "holes.kicad_pcb"
    hole = (
        f'(footprint "Hole" (layer "F.Cu") (at {x} {y}) '
        f'(property "Reference" "{ref}" (at 0 0) (layer "F.SilkS")) '
        f'(pad "1" {kind} circle (at 0 0) (size .3 .3) '
        '(drill .3) (layers "*.Cu" "*.Mask")))'
        if kind
        else ""
    )
    path.write_text(
        "(kicad_pcb (version 20240108) (generator pcbnew) "
        "(general (thickness 1.6)) "
        '(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) '
        '(2 "In2.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user)) '
        '(net 0 "") '
        "(gr_rect (start -10 -10) (end 10 10) "
        '(stroke (width .1) (type default)) (fill none) (layer "Edge.Cuts")) ' + hole + ")"
    )
    router, _ = load_pcb_for_routing(str(path), rules=_make_rules(), force_python=True)
    return router


def test_loader_retains_named_and_anonymous_plated_and_unplated_holes(tmp_path):
    from kicad_tools.router.via_in_pad_eligibility import resolve_component_hole_context

    for kind in ("thru_hole", "np_thru_hole"):
        for ref in ("H1", "", "#H1"):
            router = _loaded_holes(tmp_path, kind, ref)
            census = router._escape._component_holes
            context = resolve_component_hole_context(0, 0, 0.3, all_pads=census)
            assert context.known
            assert abs(context.nearest_distance_mm - 0.1) < 1e-9
            assert _rescue(census) is None
            # Physical-hole records must not become plated routing copper.
            if kind == "np_thru_hole":
                assert all(not pad.through_hole for pad in router.all_pads)


def test_loader_keeps_far_and_empty_positive_controls(tmp_path):
    for kind in ("thru_hole", "np_thru_hole", ""):
        router = _loaded_holes(tmp_path, kind, "", x=5)
        assert _rescue(router._escape._component_holes) is not None


def test_loaded_census_observes_pads_added_after_escape_initialization(tmp_path):
    router = _loaded_holes(tmp_path, "", "", x=5)
    escape = router._escape
    assert _rescue(escape._component_holes) is not None
    router.add_component(
        "LATE",
        [
            {
                "number": "1",
                "x": 0.4,
                "y": 0,
                "width": 0.3,
                "height": 0.3,
                "through_hole": True,
                "drill": 0.3,
            }
        ],
    )
    assert router._escape is escape
    assert _rescue(escape._component_holes) is None


def test_hole_census_survives_both_worker_entry_points(tmp_path, monkeypatch):
    import pickle

    from kicad_tools.router.algorithms.evolutionary import _run_evolutionary_trial
    from kicad_tools.router.core import Autorouter, _run_monte_carlo_trial

    router = _loaded_holes(tmp_path)
    payload = pickle.loads(pickle.dumps(router._serialize_for_parallel()))
    payload.update(
        trial_num=0, seed=1, base_order=[], use_negotiated=False, chrom_idx=0, net_order=[]
    )
    seen = []

    def inspect_worker(worker, *_args, **_kwargs):
        seen.append(_rescue(worker._escape._component_holes) is None)
        return []

    monkeypatch.setattr(Autorouter, "route_all", inspect_worker)
    _run_monte_carlo_trial(payload)
    _run_evolutionary_trial(payload)
    assert seen == [True, True]

    # Old/incomplete worker payloads must not turn missing census into empty.
    del payload["component_holes"]
    seen.clear()
    _run_monte_carlo_trial(payload)
    _run_evolutionary_trial(payload)
    assert seen == [True, True]

    # Explicitly verified empty remains distinct and permits the same escape.
    payload["component_holes"] = []
    seen.clear()
    _run_monte_carlo_trial(payload)
    _run_evolutionary_trial(payload)
    assert seen == [False, False]
