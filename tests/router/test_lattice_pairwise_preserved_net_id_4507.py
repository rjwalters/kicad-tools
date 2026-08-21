"""The #4602 lattice HV pairwise projection must resolve PRESERVED net ids (#4507).

Attribution sub-task (issue #4507, 2026-08-21 curator update): of the 4 genuine
softstart-rev-C T4 residuals attributed to a real search-time gap, 3 traced to
this exact defect, confirmed on the real board with a targeted diagnostic and
reproduced here minimally.

**The defect**: on a *filtered* pass (``--nets`` / ``--skip-nets`` / ``--region``
/ ``--complete`` -- every composition step), the board loader
(``router/io.py``) rewrites every NON-routable net's pads to ``net_num = 0`` so
they act as anonymous clearance obstacles: ``pad.net_name`` survives but
``pad.net`` collapses.  ``Autorouter._net_name_to_id()`` -- which backs
:meth:`Autorouter._lattice_pairwise_projection` (#4602) and the #4605 keepout
projection -- built its reverse name->id map from ``self.net_names`` and a
``pad.net``-keyed fallback, BOTH of which read the now-zeroed ``pad.net``.  So
every preserved net's name silently mapped to net id 0 -- the SAME sentinel
every other preserved net's name also collapsed to -- and
``LatticePairwise.required_by_pair`` (keyed by REAL net id pairs) never got an
entry any query against that net's TRUE id (the id its actual committed copper
carries) could find.  ``pairwise.required(moving_net, real_preserved_id)``
silently returned ``0.0``: the search-time HV avoidance #4602/#4507 built was
dormant for exactly this net, on every ``--complete`` run (which always has a
preserved/fixed net set).

This is the SAME defect class issue #4622 named and fixed for the net-class-map
sidecar merge (``_resolve_net_class_map_domains`` in ``route_cmd.py``, widened
with ``router.existing_routes``) -- ``_net_name_to_id`` never got the matching
fix.  ``existing_routes`` carries each preserved net's TRUE id straight from the
board's copper (never rewritten by the filtered-pass loader), so it is the
correct fallback source here too.

See ``docs/hv-pairwise-softstart-proof.md`` for the real-board numbers this
reproduces (``/GATE_BUS_POS(22) <-> /I_SENSE_OUT``: net id 0 -> 31, required
0.0 -> 1.6mm) and ``tests/router/test_lattice_fixed_clearance_4597.py`` for the
sibling per-route ``fixed_clearance`` lookup (a different mechanism -- keyed by
``route.net`` directly, so #4622-immune by construction -- that this test's
harness borrows conventions from).
"""

from __future__ import annotations

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table
from kicad_tools.router.primitives import Pad, Route, Segment

DRU = 0.2
# 150V vs 0V, IEC 60664-1 PD2/IIIa cross-domain creepage.
_REQUIRED_MM = 1.6


def _pad(x: float, y: float, net: int, *, ref: str, pin: str, net_name: str) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=1.0,
        height=1.0,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
        ref=ref,
        pin=pin,
    )


def _preserved_route(net: int, net_name: str) -> Route:
    return Route(
        net=net,
        net_name=net_name,
        segments=[
            Segment(
                x1=10.0,
                y1=5.0,
                x2=10.0,
                y2=25.0,
                width=0.5,
                layer=Layer.F_CU,
                net=net,
                net_name=net_name,
            )
        ],
        vias=[],
    )


def _router_with_preserved_net(*, zero_preserved_pad_net: bool) -> Autorouter:
    """A routable ``/MOVING`` net plus a preserved ``/FOREIGN`` net (#4507).

    ``zero_preserved_pad_net`` reproduces the exact filtered-pass loader
    behaviour: the preserved net's PAD carries ``net=0`` (the anonymous
    obstacle sentinel a ``--complete``/``--nets`` pass rewrites it to) while
    its ``net_name`` survives, and it is deliberately never registered in
    ``router.net_names`` (also never populated for a zeroed pad) -- exactly
    what a real ``load_pcb_for_routing`` filtered pass produces.  ``False``
    reproduces the pre-filter/never-filtered case (net_names has the real id)
    as the byte-identical control.
    """
    router = Autorouter(60, 50, strategy="lattice")

    moving_net = 1
    for i, (x, y) in enumerate(((5.0, 15.0), (50.0, 15.0))):
        pad = _pad(x, y, moving_net, ref=f"U{i + 1}", pin="1", net_name="/MOVING")
        router.pads[(pad.ref, pad.pin)] = pad
        router.all_pads.append(pad)
        router.nets.setdefault(pad.net, []).append((pad.ref, pad.pin))
        router.net_names[pad.net] = pad.net_name

    preserved_net = 31  # matches the real board's /I_SENSE_OUT net id
    pad_net = 0 if zero_preserved_pad_net else preserved_net
    foreign_pad = _pad(30.0, 30.0, pad_net, ref="U3", pin="1", net_name="/FOREIGN")
    router.pads[(foreign_pad.ref, foreign_pad.pin)] = foreign_pad
    router.all_pads.append(foreign_pad)
    if not zero_preserved_pad_net:
        router.net_names[preserved_net] = "/FOREIGN"

    router.existing_routes = [_preserved_route(preserved_net, "/FOREIGN")]

    router.rules.pairwise_clearance = build_pairwise_clearance_table(
        {"/MOVING": 150.0, "/FOREIGN": 0.0}, dru=DRU
    )
    return router, moving_net, preserved_net


def test_net_name_to_id_resolves_preserved_net_with_zeroed_pad() -> None:
    """``_net_name_to_id`` finds the TRUE id via ``existing_routes``, not 0."""
    router, _moving_net, preserved_net = _router_with_preserved_net(zero_preserved_pad_net=True)

    name_to_id = router._net_name_to_id()

    assert name_to_id["/FOREIGN"] == preserved_net


def test_net_name_to_id_never_overrides_a_genuinely_resolved_id() -> None:
    """A net whose pad carries its real id is untouched by the new fallback."""
    router, _moving_net, preserved_net = _router_with_preserved_net(zero_preserved_pad_net=False)

    name_to_id = router._net_name_to_id()

    assert name_to_id["/FOREIGN"] == preserved_net


def test_lattice_pairwise_projection_sees_preserved_net_requirement() -> None:
    """The #4602 projection must carry the pair keyed by the REAL preserved id.

    Before the fix this queried as ``required(moving_net, 0)`` -- absent from
    ``required_by_pair`` (or colliding with a DIFFERENT preserved net that also
    collapsed to 0) -- so the search-time HV avoidance was silently dormant for
    every pair naming a preserved net.
    """
    router, moving_net, preserved_net = _router_with_preserved_net(zero_preserved_pad_net=True)

    projection = router._lattice_pairwise_projection()

    assert projection is not None
    assert projection.required(moving_net, preserved_net) == _REQUIRED_MM
    # The stale, wrong id must not silently carry a (possibly garbled) value.
    assert projection.required(moving_net, 0) == 0.0


def test_lattice_pairwise_projection_dormant_without_preserved_fixup_regression() -> None:
    """Control: with the pad's real id (no filtered pass), the projection
    already worked -- pins the byte-identical case the fix must not disturb.
    """
    router, moving_net, preserved_net = _router_with_preserved_net(zero_preserved_pad_net=False)

    projection = router._lattice_pairwise_projection()

    assert projection is not None
    assert projection.required(moving_net, preserved_net) == _REQUIRED_MM
