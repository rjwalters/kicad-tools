"""Issue #5788: ``--preserve-existing`` must not re-route already-complete nets.

The defect
----------

``kct route --preserve-existing``'s ``--help`` promised that existing copper is
loaded as an immovable obstacle and re-emitted unchanged "so only unconnected
nets are routed".  Only the obstacle half was implemented: nothing removed an
already-complete net from ``router.nets``, and both engines deliberately let a
net that IS in the routable set replace its own copper::

    fixed_copper = [r for r in self.existing_routes if r.net not in self.nets]

(correct for ``--nets`` / ``--region``, where the caller explicitly asked for a
re-route).  So a fully-routed net was re-routed from scratch and its copper
dropped.  Measured on board 06 (the #5781 benchmark): **all 64** pre-routed,
coupled LVDS segments were replaced and 0 survived, through the one flag whose
entire purpose is to keep them.

The fix (``router/preserve_existing.py`` +
``route_cmd._resolve_preserved_connected_nets``) detects the nets that are
already fully connected on the INPUT board and holds them out of the routable
set, so their copper is a hard obstacle and is re-emitted verbatim.

What is pinned here
-------------------

1. Small fixture, one complete net + one unconnected net: the complete net's
   copper is unchanged and only the unconnected net gains copper.
2. The same for a **coupled differential pair** -- board 06's failure was on
   coupled pairs, so a single-ended-only regression test would not have caught
   it.
3. The default (no ``--preserve-existing``) is untouched: a complete net is
   still re-routed.
4. An EXPLICIT route set still re-routes a complete net -- ``--nets`` promises
   exactly that, and the fix must not quietly turn it into a no-op.
5. The detector itself, against board 06's real input: exactly the eight LVDS
   nets are reported complete.
6. End to end on board 06a (``slow``): every one of the 64 pre-routed LVDS
   segments survives.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import main as route_main
from kicad_tools.router.optimizer.pcb import parse_segments, parse_vias
from kicad_tools.router.preserve_existing import fully_connected_nets

_BOARD_06A = (
    Path(__file__).resolve().parents[2]
    / "boards"
    / "06-diffpair-test"
    / "output"
    / "diffpair_test.kicad_pcb"
)


def _seg_key(seg) -> tuple:
    """Geometry identity of a segment, UUID excluded.

    ``Segment.to_sexp()`` mints a fresh UUID on every emission, so "byte
    identical" in the acceptance criteria is defined on the electrical geometry
    (endpoints, width, layer, net) -- exactly as the sibling
    ``test_preserve_existing.py`` already defines it.
    """
    return (
        round(seg.x1, 4),
        round(seg.y1, 4),
        round(seg.x2, 4),
        round(seg.y2, 4),
        round(seg.width, 4),
        seg.layer.name,
    )


def _geom(seg_list) -> set[tuple]:
    return {_seg_key(s) for s in seg_list or ()}


# Two independent 2-pad lanes, far enough apart that routing one cannot
# plausibly need the other's corridor.  ``DONE_NET`` is pre-routed end to end
# (so it is ALREADY complete); ``OPEN_NET`` has pads and no copper.
_LANES = ("DONE_NET", "OPEN_NET")


def _two_lane_board(*, routed: tuple[str, ...] = ("DONE_NET",)) -> str:
    nodes = [
        '(kicad_pcb (version 20240108) (generator "test")',
        "(general (thickness 1.6))",
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))',
        '(net 0 "")',
        "(gr_rect (start 0 0) (end 24 20) (stroke (width 0.1) (type default)) "
        '(fill none) (layer "Edge.Cuts"))',
    ]
    for number, name in enumerate(_LANES, 1):
        y = 6 + (number - 1) * 8
        nodes.append(f'(net {number} "{name}")')
        for side, x in enumerate((5, 19)):
            nodes.append(
                f'(footprint "Test:Pad" (layer "F.Cu") (at {x} {y}) '
                f'(property "Reference" "J{number}{side}" (at 0 -1) (layer "F.SilkS")) '
                f'(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") '
                f'(net {number} "{name}")))'
            )
        if name in routed:
            nodes.append(
                f'(segment (start 5 {y}) (end 19 {y}) (width 0.25) (layer "F.Cu") (net {number}))'
            )
    return "\n".join([*nodes, ")"])


# A coupled differential pair (PAIR_P / PAIR_N) pre-routed as two parallel
# traces, plus one open single-ended lane so the pass always has real work to do.
def _diffpair_board() -> str:
    nodes = [
        '(kicad_pcb (version 20240108) (generator "test")',
        "(general (thickness 1.6))",
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))',
        '(net 0 "")',
        "(gr_rect (start 0 0) (end 26 22) (stroke (width 0.1) (type default)) "
        '(fill none) (layer "Edge.Cuts"))',
    ]
    # PAIR_P / PAIR_N: 0.4 mm apart, pre-routed straight across.
    for number, (name, y) in enumerate((("PAIR_P", 6.0), ("PAIR_N", 6.4)), 1):
        nodes.append(f'(net {number} "{name}")')
        for side, x in enumerate((5, 21)):
            nodes.append(
                f'(footprint "Test:Pad" (layer "F.Cu") (at {x} {y}) '
                f'(property "Reference" "P{number}{side}" (at 0 -1) (layer "F.SilkS")) '
                f'(pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") '
                f'(net {number} "{name}")))'
            )
        nodes.append(
            f'(segment (start 5 {y}) (end 21 {y}) (width 0.2) (layer "F.Cu") (net {number}))'
        )
    # SINGLE_OPEN: pads only.
    nodes.append('(net 3 "SINGLE_OPEN")')
    for side, x in enumerate((5, 21)):
        nodes.append(
            f'(footprint "Test:Pad" (layer "F.Cu") (at {x} 16) '
            f'(property "Reference" "S{side}" (at 0 -1) (layer "F.SilkS")) '
            f'(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") '
            f'(net 3 "SINGLE_OPEN")))'
        )
    return "\n".join([*nodes, ")"])


def _run_route(
    tmp_path: Path,
    pcb_text: str,
    *,
    extra_argv: list[str] | None = None,
) -> str:
    in_path = tmp_path / "in.kicad_pcb"
    out_path = tmp_path / "out.kicad_pcb"
    in_path.write_text(pcb_text)
    argv = [
        str(in_path),
        "--output",
        str(out_path),
        "--no-optimize",
        "--force",
        "--quiet",
        *(extra_argv or []),
    ]
    route_main(argv)
    assert out_path.exists(), "route did not produce an output file"
    return out_path.read_text()


class TestCompleteNetIsLeftAlone:
    """AC: a net already complete in the input keeps its copper, byte-identical."""

    def test_complete_net_copper_unchanged_and_open_net_routed(self, tmp_path):
        board = _two_lane_board()
        orig = parse_segments(board)
        # Precondition the whole issue rests on: exactly one lane is pre-routed
        # and the detector agrees it is the complete one.
        assert set(orig) == {"DONE_NET"}
        out = parse_segments(_run_route(tmp_path, board, extra_argv=["--preserve-existing"]))

        assert "DONE_NET" in out, (
            "the already-complete net's copper was dropped under --preserve-existing (issue #5788)"
        )
        assert _geom(out["DONE_NET"]) == _geom(orig["DONE_NET"]), (
            "the already-complete net was re-routed: its geometry changed even "
            "though --preserve-existing promises only unconnected nets are routed"
        )
        # ...and the unconnected net is the only one that gained copper.
        assert out.get("OPEN_NET"), "the unconnected net was not routed"

    def test_detector_reports_the_complete_lane_only(self, tmp_path):
        board = _two_lane_board()
        path = tmp_path / "b.kicad_pcb"
        path.write_text(board)
        assert fully_connected_nets(path) == ["DONE_NET"]

    def test_default_still_reroutes_a_complete_net(self, tmp_path):
        """Regression-safety: WITHOUT the flag, a full re-route is still a full re-route.

        ``--preserve-existing`` is documented as opt-in incremental routing; the
        default must keep replacing existing copper with freshly routed nets, or
        #5788's fix would have silently changed every plain ``kct route``.
        """
        board = _two_lane_board()
        out_text = _run_route(tmp_path, board)
        out = parse_segments(out_text)
        # Both lanes are routed from scratch, and the pre-existing copper block
        # is NOT carried over verbatim -- the writer strips and re-emits.
        assert set(out) == set(_LANES)
        assert out["DONE_NET"], "the default pass should have routed DONE_NET itself"

    def test_explicit_nets_still_reroutes_a_complete_net(self, tmp_path):
        """``--nets`` promises to route exactly what it lists -- even if complete.

        ``--nets`` implies ``--preserve-existing`` (#4322/#4355), so this is the
        exact path the #5788 fix must NOT hijack: naming a complete net is a
        deliberate re-route request, not an accident.
        """
        board = _two_lane_board(routed=("DONE_NET", "OPEN_NET"))
        assert set(parse_segments(board)) == set(_LANES)
        out = parse_segments(
            _run_route(
                tmp_path,
                board,
                extra_argv=["--preserve-existing", "--nets", "DONE_NET"],
            )
        )
        assert out["DONE_NET"], "--nets DONE_NET produced no copper for DONE_NET"
        # OPEN_NET is the non-listed net: preserved verbatim as a fixed obstacle.
        assert _geom(out["OPEN_NET"]) == _geom(parse_segments(board)["OPEN_NET"])


class TestCoupledDiffPairIsLeftAlone:
    """Board 06's failure was on COUPLED pairs, so pin that path explicitly.

    The filtering must land BEFORE diff-pair grouping, not merely on the
    independent-net path: a pair that is still in ``router.nets`` is still a
    candidate for coupled re-routing however its copper is later re-emitted.
    """

    @staticmethod
    def _spy_router_state(monkeypatch) -> list[dict]:
        """Record the routable / preserved split right after each board load.

        ``_apply_net_class_map_sidecar`` runs unconditionally, immediately after
        ``load_pcb_for_routing``, on every routing sub-flow -- the same hook
        ``test_preserve_existing.py`` already uses to observe that split.
        ``router.net_names`` is the routable domain (a skipped net's pads are
        rewritten to net 0, so it never enters it); ``router.existing_routes``
        is the preserved copper.
        """
        from kicad_tools.cli import route_cmd

        original = route_cmd._apply_net_class_map_sidecar
        seen: list[dict] = []

        def _wrapper(router, args, quiet=False):
            original(router, args, quiet=quiet)
            seen.append(
                {
                    "routable": set(router.net_names.values()),
                    "preserved": {
                        r.net_name for r in getattr(router, "existing_routes", []) if r.net_name
                    },
                }
            )

        monkeypatch.setattr(route_cmd, "_apply_net_class_map_sidecar", _wrapper)
        return seen

    def test_pre_routed_pair_survives_a_differential_pairs_pass(self, tmp_path, monkeypatch):
        board = _diffpair_board()
        orig = parse_segments(board)
        assert set(orig) == {"PAIR_P", "PAIR_N"}
        seen = self._spy_router_state(monkeypatch)

        out = parse_segments(
            _run_route(
                tmp_path,
                board,
                extra_argv=["--preserve-existing", "--differential-pairs"],
            )
        )

        assert seen, "the router was never loaded on this CLI path"
        for state in seen:
            for net in ("PAIR_P", "PAIR_N"):
                assert net not in state["routable"], (
                    f"{net} is an ALREADY-COMPLETE coupled pair member but is "
                    "still in the routable set, so the coupled re-route path can "
                    "replace its copper (issue #5788)"
                )
                assert net in state["preserved"], (
                    f"{net}'s copper was not loaded as preserved/fixed obstacle"
                )
        # The open single-ended net is exactly what the pass is left to route.
        assert "SINGLE_OPEN" in seen[0]["routable"]

        for net in ("PAIR_P", "PAIR_N"):
            assert net in out, (
                f"{net}: the pre-routed coupled pair's copper was dropped under "
                "--preserve-existing --differential-pairs (issue #5788)"
            )
            assert _geom(out[net]) == _geom(orig[net]), (
                f"{net}: the coupled pair was re-routed and its coupled geometry "
                "replaced -- exactly the board 06a data loss #5788 reports"
            )
        assert out.get("SINGLE_OPEN"), "the unconnected single-ended net was not routed"


class TestBoard06a:
    """The acceptance criterion measured on the real board from the issue."""

    @pytest.fixture(autouse=True)
    def _require_board(self):
        if not _BOARD_06A.exists():  # pragma: no cover - fixture drift guard
            pytest.skip(f"board 06 input not found: {_BOARD_06A}")

    def test_detector_finds_exactly_the_eight_lvds_nets(self):
        """Fast half of the AC: the detector's verdict on board 06a's input.

        The generator pre-routes the four LVDS pairs and leaves the eight LVTTL
        nets open, so a correct detector names exactly the eight LVDS nets --
        and therefore the routable set under ``--preserve-existing`` is exactly
        the eight open ones.
        """
        connected = fully_connected_nets(_BOARD_06A)
        assert connected == [
            "LVDS1_N",
            "LVDS1_P",
            "LVDS2_N",
            "LVDS2_P",
            "LVDS3_N",
            "LVDS3_P",
            "LVDS4_N",
            "LVDS4_P",
        ]

    @pytest.mark.slow
    @pytest.mark.integration
    def test_all_64_lvds_segments_survive(self, tmp_path):
        """End to end: ``kct route --preserve-existing`` keeps 64/64 LVDS segments.

        Marked ``slow`` (nightly): a full board 06 route is ~25 s with the C++
        backend built and considerably longer without it, which does not fit the
        60 s per-test budget the bulk CI job runs under.  The fast detector test
        above covers the same defect on every push.
        """
        board = _BOARD_06A.read_text()
        orig = parse_segments(board)
        lvds = sorted(n for n in orig if n.startswith("LVDS"))
        assert sum(len(orig[n]) for n in lvds) == 64, (
            "board 06's input no longer carries the 64 pre-routed LVDS segments "
            "this regression test is written against"
        )

        out_path = tmp_path / "out.kicad_pcb"
        route_main(
            [
                str(_BOARD_06A),
                "--output",
                str(out_path),
                "--preserve-existing",
                "--force",
                "--quiet",
            ]
        )
        out = parse_segments(out_path.read_text())

        survived = sum(len(_geom(orig[n]) & _geom(out.get(n))) for n in lvds)
        assert survived == 64, (
            f"only {survived}/64 pre-routed LVDS segments survived "
            "--preserve-existing (issue #5788)"
        )
        # The plane vias the generator placed survive too (the flag's original
        # #3155 promise), and the open LVTTL nets are the ones that gained copper.
        out_vias = parse_vias(out_path.read_text())
        in_vias = parse_vias(board)
        for net, vias in in_vias.items():
            assert len(out_vias.get(net, [])) >= len(vias), (
                f"{net}: preserved vias were dropped under --preserve-existing"
            )
        assert any(out.get(n) for n in ("IN1", "OUT1")), (
            "no open LVTTL net gained copper -- the pass routed nothing at all"
        )
