"""Via-in-pad as a tier-gated LAST-RESORT attach (Issue #4475, epic #4465 P3).

Issue #4284 made ``LatticePathfinder._via_ok`` admit a same-net via barrel on
an SMD pad whenever the fab tier supports it (``MfrLimits.via_in_pad_supported``),
but that gate is purely tier-based: the unified A* search may land a
via-in-pad site OPPORTUNISTICALLY even when an in-layer route or a
free-space (off-pad) via exists elsewhere, simply because it happened to lie
on the cheapest path.  Issue #4434's ask (external-tool parity) is that
via-in-pad should be reached only as a LAST RESORT on a genuinely walled pad.

This module exercises the new ``DesignRules.via_in_pad_last_resort`` flag:

1. Default OFF preserves the pre-#4475 opportunistic #4284 behavior
   byte-for-byte (a walled pad still closes via an opportunistic via-in-pad
   at a via-in-pad-capable tier, exactly as before this issue).
2. ON, an OPEN pad (an in-layer/free-space-via route exists) closes WITHOUT
   ever landing a via-in-pad -- staging forces via-in-pad OFF for the first
   search stage, so a successful first-stage route can never contain one.
3. ON, a WALLED pad (no in-layer/free-space-via escape exists) reaches for
   via-in-pad ONLY as the last resort, and only on a tier that supports it.
4. On a tier WITHOUT via-in-pad support, the walled pad is reported
   unroutable with the explicit ``"via-in-pad-tier-unsupported"`` reason
   (feeds Phase 4 reporting) instead of a generic ``"no-path"``, and no
   via-in-pad copper is ever emitted.
5. The existing other-net grown-rect veto still holds under the new
   staged ordering: a foreign-net pad is never a legal via site.
6. The emitted via-in-pad passes real ``kicad-cli pcb drc --refill-zones``
   clean on a via-in-pad-supported tier (jlcpcb-tier1) -- the cross-gate
   rule; ``kct check`` alone is insufficient.

Fixture geometry: two same-net SMD pads, ``A`` on F.Cu and ``B`` on B.Cu (so
the connection needs at least one layer change).  A "walled" variant rings
``A`` with four other-net SMD pads on F.Cu only, standing off far enough
that the ring's grown-rect via veto (``via_pad_grow``) does not reach A's
own center (so A's own pad-site via sites stay legal) but close enough that
the ring's agent-radius mask blocks every in-layer edge/node AND every
off-pad via site beyond A's own via-in-pad window -- so the ONLY legal via
site left for A's net is inside A's own pad (a via-in-pad, by construction).
B.Cu is left untouched (the ring pads are F.Cu-only SMD), so once a via
lands at A's location the rest of the path is a normal, unobstructed
B.Cu run to B.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.drc.geometric import run_geometric_drc
from kicad_tools.router.io import load_pads_for_analysis, merge_routes_into_pcb
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

_OUTLINE = [(-3.0, -3.0), (8.0, -3.0), (8.0, 3.0), (-3.0, 3.0)]

# Ring standoff (mm) from A's center.  Chosen so the ring's grown-rect via
# veto (``via_pad_grow`` = via_r + clearance - agent_radius = 0.25 at the
# default DesignRules geometry) does NOT reach A's center (0.7 - 0.25 = 0.45
# > 0), while its agent-radius node/edge mask (0.3) blocks every in-layer
# node/edge beyond A's own 0.5 mm via-in-pad window (0.7 - 0.3 = 0.4 < 0.5).
_WALL_STANDOFF = 0.7
_WALL_THICKNESS = 1.0
_WALL_LENGTH = 3.0


def _pad(x: float, y: float, w: float, h: float, net: int, *, ref: str, layer: Layer) -> Pad:
    return Pad(
        x=x, y=y, width=w, height=h, net=net, net_name=f"N{net}", layer=layer, ref=ref, pin="1"
    )


def _make_pads(*, walled: bool) -> tuple[list[Pad], Pad, Pad]:
    pad_a = _pad(0.0, 0.0, 0.3, 0.3, 1, ref="U1", layer=Layer.F_CU)
    pad_b = _pad(5.0, 0.0, 0.3, 0.3, 1, ref="U2", layer=Layer.B_CU)
    pads = [pad_a, pad_b]
    if walled:
        d, ww, hh = _WALL_STANDOFF, _WALL_THICKNESS, _WALL_LENGTH
        pads += [
            _pad(d + ww / 2.0, 0.0, ww, hh, 2, ref="WR", layer=Layer.F_CU),
            _pad(-(d + ww / 2.0), 0.0, ww, hh, 2, ref="WL", layer=Layer.F_CU),
            _pad(0.0, d + ww / 2.0, hh, ww, 2, ref="WT", layer=Layer.F_CU),
            _pad(0.0, -(d + ww / 2.0), hh, ww, 2, ref="WB", layer=Layer.F_CU),
        ]
    return pads, pad_a, pad_b


def _via_hits_pad(via: object, pad: Pad, via_radius: float) -> bool:
    return (
        abs(via.x - pad.x) <= pad.width / 2.0 + via_radius  # type: ignore[attr-defined]
        and abs(via.y - pad.y) <= pad.height / 2.0 + via_radius  # type: ignore[attr-defined]
    )


def _route_single(
    pads: list[Pad], pad_a: Pad, pad_b: Pad, rules: DesignRules
) -> tuple[object | None, str]:
    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=LayerStack.two_layer())
    committed = pf._fresh_committed()
    return pf._route_impl(pad_a, pad_b, None, committed=committed, history={}, present=0.0)


# ---------------------------------------------------------------------------
# 1. Default OFF preserves the pre-#4475 opportunistic behavior.
# ---------------------------------------------------------------------------


def test_default_off_preserves_legacy_opportunistic_via_in_pad() -> None:
    """``via_in_pad_last_resort`` defaults to False: the walled pad still
    closes via an OPPORTUNISTIC via-in-pad at a supporting tier, exactly the
    #4284 behavior this issue must not regress."""
    pads, pad_a, pad_b = _make_pads(walled=True)
    rules = DesignRules(manufacturer="jlcpcb-tier1")
    assert rules.via_in_pad_last_resort is False

    result, reason = _route_single(pads, pad_a, pad_b, rules)
    assert result is not None, f"expected a route, got decline: {reason}"
    via_r = rules.via_diameter / 2.0
    assert any(_via_hits_pad(v, pad_a, via_r) for v in result.route.vias)


# ---------------------------------------------------------------------------
# 2. ON + open pad: closes WITHOUT via-in-pad when a route exists.
# ---------------------------------------------------------------------------


def test_last_resort_closes_open_pad_without_via_in_pad() -> None:
    pads, pad_a, pad_b = _make_pads(walled=False)
    rules = DesignRules(manufacturer="jlcpcb-tier1", via_in_pad_last_resort=True)

    result, reason = _route_single(pads, pad_a, pad_b, rules)
    assert result is not None, f"expected a route, got decline: {reason}"
    via_r = rules.via_diameter / 2.0
    offenders = [
        (v.x, v.y)
        for v in result.route.vias
        if _via_hits_pad(v, pad_a, via_r) or _via_hits_pad(v, pad_b, via_r)
    ]
    assert not offenders, f"via-in-pad shipped when an open route existed: {offenders}"


# ---------------------------------------------------------------------------
# 3. ON + walled pad, supporting tier: reaches for via-in-pad as last resort.
# ---------------------------------------------------------------------------


def test_last_resort_reaches_via_in_pad_only_when_walled() -> None:
    pads, pad_a, pad_b = _make_pads(walled=True)
    rules = DesignRules(manufacturer="jlcpcb-tier1", via_in_pad_last_resort=True)

    result, reason = _route_single(pads, pad_a, pad_b, rules)
    assert result is not None, f"expected the walled pad to close via last-resort: {reason}"
    via_r = rules.via_diameter / 2.0
    assert any(_via_hits_pad(v, pad_a, via_r) for v in result.route.vias), (
        "walled pad closed without a via-in-pad -- last-resort stage never engaged"
    )


def test_last_resort_off_and_on_agree_when_pad_is_open() -> None:
    """Sanity: the flag must not change the outcome for an ordinary
    (non-walled) connection -- it only changes WHICH via is picked when
    ties exist, never whether the connection completes."""
    pads, pad_a, pad_b = _make_pads(walled=False)
    for last_resort in (False, True):
        rules = DesignRules(manufacturer="jlcpcb-tier1", via_in_pad_last_resort=last_resort)
        result, reason = _route_single(pads, pad_a, pad_b, rules)
        assert result is not None, f"last_resort={last_resort}: {reason}"


# ---------------------------------------------------------------------------
# 4. Tier hard floor: no via-in-pad support -> explicit tier-limit reason.
# ---------------------------------------------------------------------------


def test_last_resort_reports_tier_limit_reason_when_unsupported() -> None:
    pads, pad_a, pad_b = _make_pads(walled=True)
    rules = DesignRules(manufacturer="jlcpcb", via_in_pad_last_resort=True)
    assert not LatticePathfinder(_OUTLINE, pads, rules)._via_in_pad_allowed

    result, reason = _route_single(pads, pad_a, pad_b, rules)
    assert result is None
    assert reason == "via-in-pad-tier-unsupported"


def test_last_resort_tier_limit_reason_surfaces_through_route_netset() -> None:
    """The same tier-limit reason lands in ``failure_reasons`` from the
    normal multi-net negotiation entry point, not just the single-route
    helper."""
    pads, pad_a, pad_b = _make_pads(walled=True)
    rules = DesignRules(manufacturer="jlcpcb", via_in_pad_last_resort=True)
    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=LayerStack.two_layer())
    routes, stats = pf.route_netset([((1, 0), pad_a, pad_b, None)], max_iterations=2)
    assert stats.routed == 0
    assert pf.failure_reasons == {(1, 0): "via-in-pad-tier-unsupported"}


def test_no_manufacturer_configured_never_reaches_via_in_pad() -> None:
    """No manufacturer configured is the conservative default (matches
    :attr:`LatticePathfinder._via_in_pad_allowed`): the walled pad declines
    with the tier-limit reason exactly as the unsupported-tier case."""
    pads, pad_a, pad_b = _make_pads(walled=True)
    rules = DesignRules(manufacturer=None, via_in_pad_last_resort=True)
    result, reason = _route_single(pads, pad_a, pad_b, rules)
    assert result is None
    assert reason == "via-in-pad-tier-unsupported"


# ---------------------------------------------------------------------------
# 5. Other-net pad site is never a legal via site under the new ordering.
# ---------------------------------------------------------------------------


def test_other_net_wall_pad_never_used_as_via_site() -> None:
    pads, pad_a, pad_b = _make_pads(walled=True)
    rules = DesignRules(manufacturer="jlcpcb-tier1", via_in_pad_last_resort=True)

    result, reason = _route_single(pads, pad_a, pad_b, rules)
    assert result is not None, reason
    via_r = rules.via_diameter / 2.0
    wall_pads = [p for p in pads if p.net == 2]
    offenders = [
        (v.x, v.y, wp.ref)
        for v in result.route.vias
        for wp in wall_pads
        if _via_hits_pad(v, wp, via_r)
    ]
    assert not offenders, f"other-net pad used as a via site: {offenders}"


def test_via_ok_override_forces_legality_independent_of_tier() -> None:
    """Unit-level check on the new ``via_in_pad_override`` parameter itself:
    ``False`` forces illegal, ``True`` forces legal, ``None`` preserves the
    pre-#4475 tier-only gate -- for the SAME-net window hit only; the
    unconditional other-net veto is untouched by the override."""
    pads, pad_a, _pad_b = _make_pads(walled=False)
    for manufacturer in (None, "jlcpcb", "jlcpcb-tier1"):
        rules = DesignRules(manufacturer=manufacturer)
        pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=LayerStack.two_layer())
        committed = pf._fresh_committed()
        lattice = pf.build()
        # The node at A's own center is a legal via-in-pad candidate site.
        key = min(
            lattice.nodes,
            key=lambda k: abs(lattice.node_point(k)[0]) + abs(lattice.node_point(k)[1]),
        )
        assert not pf._via_ok(key, pad_a.net, committed, via_in_pad_override=False)
        assert pf._via_ok(key, pad_a.net, committed, via_in_pad_override=True)
        assert pf._via_ok(key, pad_a.net, committed, via_in_pad_override=None) == (
            pf._via_in_pad_allowed
        )


# ---------------------------------------------------------------------------
# 6. Real kicad-cli DRC on the emitted via-in-pad copper.
# ---------------------------------------------------------------------------

_HEADER = """(kicad_pcb
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
  (net 1 "NET1")
  (net 2 "WALL")
"""

_OUTLINE_RECT = """  (gr_rect (start 0.0 0.0) (end 20.0 20.0)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
"""


def _footprint(
    ref: str,
    x: float,
    y: float,
    w: float,
    h: float,
    net: int,
    net_name: str,
    layer: str,
    uuid_n: int,
) -> str:
    return f"""  (footprint "kct:Pad"
    (layer "{layer}")
    (uuid "00000000-0000-0000-0000-0000000000{uuid_n:02d}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "00000000-0000-0000-0000-0000000001{uuid_n:02d}"))
    (pad "1" smd rect (at 0 0) (size {w} {h})
      (layers "{layer}") (net {net} "{net_name}"))
  )
"""


def _walled_board_text() -> str:
    """A real ``.kicad_pcb`` mirroring the synthetic walled fixture above,
    for a genuine ``kicad-cli pcb drc --refill-zones`` roundtrip."""
    ax, ay = 10.0, 10.0
    bx, by = 10.0 + 5.0, 10.0
    d, ww, hh = _WALL_STANDOFF, _WALL_THICKNESS, _WALL_LENGTH
    fps = [
        _footprint("U1", ax, ay, 0.3, 0.3, 1, "NET1", "F.Cu", 10),
        _footprint("U2", bx, by, 0.3, 0.3, 1, "NET1", "B.Cu", 11),
        _footprint("WR", ax + d + ww / 2.0, ay, ww, hh, 2, "WALL", "F.Cu", 12),
        _footprint("WL", ax - d - ww / 2.0, ay, ww, hh, 2, "WALL", "F.Cu", 13),
        _footprint("WT", ax, ay + d + ww / 2.0, hh, ww, 2, "WALL", "F.Cu", 14),
        _footprint("WB", ax, ay - d - ww / 2.0, hh, ww, 2, "WALL", "F.Cu", 15),
    ]
    return _HEADER + _OUTLINE_RECT + "".join(fps) + ")\n"


def test_walled_via_in_pad_last_resort_is_drc_clean(tmp_path: Path) -> None:
    """The via-in-pad emitted as a last resort must pass real
    ``kicad-cli pcb drc --refill-zones`` on a via-in-pad-supported tier --
    ``kct check`` alone is insufficient (standing process rule)."""
    text = _walled_board_text()
    base_pcb = tmp_path / "base.kicad_pcb"
    base_pcb.write_text(text)
    base = run_geometric_drc(base_pcb)
    if not base.ran:
        pytest.skip(f"kicad-cli DRC unavailable: {base.reason}")
    assert base.error_count == 0

    pads = load_pads_for_analysis(text)
    net1_pads = [p for p in pads if p.net == 1]
    assert len(net1_pads) == 2
    pad_a, pad_b = net1_pads

    rules = DesignRules(manufacturer="jlcpcb-tier1", via_in_pad_last_resort=True)
    pf = LatticePathfinder.from_board(text, rules=rules)
    routes, stats = pf.route_netset([((1, 0), pad_a, pad_b, None)], max_iterations=4)
    assert stats.routed == 1, f"declines: {pf.failure_reasons}"

    route = next(iter(routes.values()))
    via_r = rules.via_diameter / 2.0
    assert any(_via_hits_pad(v, pad_a, via_r) for v in route.vias), (
        "fixture regression: expected the walled pad to require a via-in-pad"
    )

    sexp = "".join(r.to_sexp() for r in routes.values() if r.segments)
    out = tmp_path / "routed.kicad_pcb"
    out.write_text(merge_routes_into_pcb(text, sexp))
    res = run_geometric_drc(out)
    assert res.ran
    assert res.error_count == 0, f"via-in-pad copper is not DRC clean: {dict(res.by_type)}"
