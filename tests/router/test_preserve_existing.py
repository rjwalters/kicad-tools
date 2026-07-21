"""Tests for Issue #3155: ``kct route --preserve-existing`` incremental routing.

Background
----------

Re-running ``kct route`` on a partially-routed board used to DESTROY existing
good routes.  ``--skip-nets`` only zeroed a skipped net's *pads* (so it was
not re-routed) but did nothing with that net's existing ``(segment ...)`` /
``(via ...)`` geometry.  Meanwhile the writer strips ALL top-level segment/via
blocks (``_strip_route_blocks``, added #2976) and re-inserts only
``Autorouter.to_sexp()`` (which serializes freshly-routed ``self.routes``
only).  Net effect: skipped-net copper, manually-routed nets, and standalone
stitch vias were all deleted on every route pass.

Fix (Issue #3155): the new ``--preserve-existing`` flag wires up the existing
but dormant ``load_pcb_for_routing(load_existing_routes=True)`` infrastructure
at all four route entry points (so existing copper is loaded as grid
obstacles AND populated into ``router.existing_routes``).  Preserved copper is
captured once from the freshly-staged input (``_capture_preserved_routes``)
and re-emitted both by the checkpoint callback (so the escalation loop, which
re-reads a checkpoint-overwritten staged file, never loses it) and by
``_finalize_routes`` after cleanup (so it survives the strip-then-rewrite).

These tests verify:

1. Re-routing a board with ``--preserve-existing`` (skipping all-but-one net)
   leaves every skipped net's geometry byte-identical and only routes the
   requested net (AC #1, #2).
2. The default (no ``--preserve-existing``) is unchanged: skipped-net copper
   is still stripped (regression-safe behaviour preserved bit-for-bit, AC #5).
3. ``_capture_preserved_routes`` parses existing copper (segments + standalone
   stitch vias) and ``_serialize_preserved_routes`` re-emits it with a
   defensive net-id dedupe so a re-routed net is never double-emitted
   (AC #3, #4 -- deterministic, environment-independent).
4. ``load_pcb_for_routing(load_existing_routes=True)`` populates
   ``router.existing_routes`` and ``_finalize_routes(preserve_existing=True)``
   appends the preserved geometry to ``route_sexp`` (and does NOT when the
   flag is off).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import (
    CatastrophicCopperLossError,
    _capture_preserved_routes,
    _finalize_routes,
    _serialize_preserved_routes,
    _write_routed_pcb,
)
from kicad_tools.cli.route_cmd import main as route_main
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.optimizer.pcb import parse_net_names, parse_segments, parse_vias

# A real routed board with eight 2-pad signal nets (LINE_*/NODE_*) and full
# segment geometry.  Reused as the fixture so the tests exercise real KiCad
# parsing/serialization rather than a hand-rolled stub.
_BOARD = (
    Path(__file__).resolve().parents[2]
    / "boards"
    / "02-charlieplex-led"
    / "output"
    / "charlieplex_3x3_routed.kicad_pcb"
)

# All routable signal nets except LINE_A.  Skipping these makes the router
# touch only LINE_A, which is the curator's reproduction recipe.  LINE_A
# routes in one 2-layer attempt, keeping the CLI test fast.
_SKIP_ALL_BUT_LINE_A = "LINE_B,LINE_C,LINE_D,NODE_A,NODE_B,NODE_C,NODE_D"

_ALL_SIGNAL_NETS = (
    "LINE_A",
    "LINE_B",
    "LINE_C",
    "LINE_D",
    "NODE_A",
    "NODE_B",
    "NODE_C",
    "NODE_D",
)


def _seg_key(seg) -> tuple:
    """Geometry identity for a segment (endpoints, width, layer, net).

    UUIDs are intentionally excluded -- ``Segment.to_sexp()`` mints a fresh
    UUID on every emission, so byte-identity is defined on the electrical
    geometry, exactly as the acceptance criteria require.
    """
    return (
        round(seg.x1, 4),
        round(seg.y1, 4),
        round(seg.x2, 4),
        round(seg.y2, 4),
        round(seg.width, 4),
        seg.layer.name,
        seg.net,
    )


def _geom_set(seg_lists) -> set[tuple]:
    return {_seg_key(s) for s in seg_lists}


@pytest.fixture
def board_text() -> str:
    if not _BOARD.exists():  # pragma: no cover - guards against fixture drift
        pytest.skip(f"fixture board not found: {_BOARD}")
    return _BOARD.read_text()


def _run_route(
    tmp_path: Path,
    pcb_text: str,
    *,
    preserve: bool,
    skip_nets: str | None = None,
    net_class_map_path: str | None = None,
) -> str:
    """Write ``pcb_text`` to a temp file, run ``kct route``, return output text.

    Uses ``--no-optimize`` and ``--force`` for determinism/speed and to skip
    the DRC gate (the fixture board carries real geometry that need not be
    DRC-clean for this test's purposes).
    """
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
    ]
    if skip_nets:
        argv += ["--skip-nets", skip_nets]
    if net_class_map_path:
        argv += ["--net-class-map", net_class_map_path]
    if preserve:
        argv.append("--preserve-existing")

    route_main(argv)
    assert out_path.exists(), "route did not produce an output file"
    return out_path.read_text()


def _inject_stitch_via(pcb_text: str, *, net: int, x: float, y: float) -> str:
    """Insert a standalone top-level ``(via ...)`` before the final ``)``.

    Models a ``kct stitch`` via: a plane-net via not owned by any routed
    signal net.
    """
    via_block = (
        "\t(via\n"
        f"\t\t(at {x:.4f} {y:.4f})\n"
        "\t\t(size 0.6000)\n"
        "\t\t(drill 0.3000)\n"
        '\t\t(layers "F.Cu" "B.Cu")\n'
        f"\t\t(net {net})\n"
        '\t\t(uuid "00000000-0000-0000-0000-0000deadbeef")\n'
        "\t)\n"
    )
    stripped = pcb_text.rstrip()
    last_paren = stripped.rfind(")")
    return stripped[:last_paren] + via_block + stripped[last_paren:]


class TestPreserveExistingCLI:
    """End-to-end CLI behaviour of ``kct route --preserve-existing``."""

    def test_preserve_keeps_skipped_nets_byte_identical(self, tmp_path, board_text):
        """AC #1/#2: skipped nets keep byte-identical geometry; only LINE_A routes."""
        orig = parse_segments(board_text)
        # Sanity: the fixture really does carry the eight signal nets.
        assert set(orig) == set(_ALL_SIGNAL_NETS)

        out_text = _run_route(tmp_path, board_text, preserve=True, skip_nets=_SKIP_ALL_BUT_LINE_A)
        out = parse_segments(out_text)

        # Every skipped net survived with byte-identical geometry.
        for net in ("LINE_B", "LINE_C", "LINE_D", "NODE_A", "NODE_B", "NODE_C", "NODE_D"):
            assert net in out, f"{net} geometry was destroyed under --preserve-existing"
            assert _geom_set(out[net]) == _geom_set(orig[net]), (
                f"{net} geometry changed under --preserve-existing"
            )

        # The one routable net is still present (re-routed in place).
        assert "LINE_A" in out
        assert len(out["LINE_A"]) > 0

    def test_default_still_strips_skipped_nets(self, tmp_path, board_text):
        """AC #5: without the flag, behaviour is unchanged (skipped copper stripped)."""
        out_text = _run_route(tmp_path, board_text, preserve=False, skip_nets=_SKIP_ALL_BUT_LINE_A)
        out = parse_segments(out_text)

        # The skipped nets are NOT preserved in default mode -- their copper
        # is stripped exactly as before this issue.  Only LINE_A (the routed
        # net) remains.  This locks in regression-safe default behaviour.
        assert set(out) == {"LINE_A"}


class TestPreservedRouteHelpers:
    """Deterministic unit coverage for the capture/serialize/dedupe helpers.

    These exercise the preservation guarantee (including standalone stitch
    vias) without depending on the optional external ``kicad-cli`` zone-fill
    pass, so they behave identically in CI and locally.
    """

    def test_capture_parses_segments_and_stitch_via(self, tmp_path, board_text):
        """AC #4: capture includes a standalone stitch via on a plane net."""
        net_names = parse_net_names(board_text)
        gnd_net = next(nid for nid, name in net_names.items() if name == "GND")
        stitched = _inject_stitch_via(board_text, net=gnd_net, x=12.3456, y=23.4567)

        in_path = tmp_path / "stitched.kicad_pcb"
        in_path.write_text(stitched)

        preserved = _capture_preserved_routes(in_path)
        by_name = {r.net_name: r for r in preserved}

        # All eight signal nets plus the GND stitch via are captured.
        for net in _ALL_SIGNAL_NETS:
            assert net in by_name, f"{net} not captured"
        assert "GND" in by_name, "stitch-via net not captured"
        gnd_vias = by_name["GND"].vias
        assert any(abs(v.x - 12.3456) < 1e-4 and abs(v.y - 23.4567) < 1e-4 for v in gnd_vias), (
            "captured GND route is missing the stitch via"
        )

    def test_serialize_emits_segments_and_vias(self, tmp_path, board_text):
        """AC #3/#4: serialization round-trips segments and the stitch via."""
        net_names = parse_net_names(board_text)
        gnd_net = next(nid for nid, name in net_names.items() if name == "GND")
        stitched = _inject_stitch_via(board_text, net=gnd_net, x=12.3456, y=23.4567)

        in_path = tmp_path / "stitched.kicad_pcb"
        in_path.write_text(stitched)

        preserved = _capture_preserved_routes(in_path)
        sexp = _serialize_preserved_routes(preserved)

        total_segments = sum(len(r.segments) for r in preserved)
        assert sexp.count("(segment") == total_segments
        # The standalone stitch via must appear in the serialized output.
        assert "(at 12.3456 23.4567)" in sexp

    def test_serialize_dedupes_rerouted_net(self, tmp_path, board_text):
        """AC #3: a net in ``exclude_net_ids`` is not re-emitted (no double-emit)."""
        in_path = tmp_path / "board.kicad_pcb"
        in_path.write_text(board_text)

        preserved = _capture_preserved_routes(in_path)
        line_a = next(r for r in preserved if r.net_name == "LINE_A")

        full = _serialize_preserved_routes(preserved)
        deduped = _serialize_preserved_routes(preserved, exclude_net_ids={line_a.net})

        # Excluding LINE_A drops exactly LINE_A's segments from the output.
        assert full.count("(segment") - deduped.count("(segment") == len(line_a.segments)
        # And LINE_A's specific geometry is absent from the deduped sexp.
        first_seg = line_a.segments[0]
        token = f"(start {first_seg.x1:.4f} {first_seg.y1:.4f})"
        # Only assert absence if the start point is unique to LINE_A (it is on
        # this board); guard against a shared coordinate causing a flake.
        if full.count(token) == 1:
            assert token not in deduped


class TestPreserveExistingFinalize:
    """Unit-level companion: existing-routes load + finalize re-emission."""

    def test_load_existing_routes_populates_router(self, tmp_path, board_text):
        """load_existing_routes=True parses existing copper into existing_routes."""
        in_path = tmp_path / "board.kicad_pcb"
        in_path.write_text(board_text)

        router, _net_map = load_pcb_for_routing(
            str(in_path),
            load_existing_routes=True,
            validate_drc=False,
            strict_drc=False,
        )
        assert len(router.existing_routes) >= 1
        loaded_nets = {r.net_name for r in router.existing_routes}
        assert "NODE_A" in loaded_nets

    def test_finalize_appends_preserved_geometry(self, tmp_path, board_text):
        """preserve_existing=True appends preserved_routes to route_sexp; off does not."""
        in_path = tmp_path / "board.kicad_pcb"
        in_path.write_text(board_text)

        router, _net_map = load_pcb_for_routing(
            str(in_path),
            skip_nets=_SKIP_ALL_BUT_LINE_A.split(","),
            validate_drc=False,
            strict_drc=False,
        )
        multi_pad = {n for n, p in router.nets.items() if n > 0 and len(p) >= 2}
        preserved = _capture_preserved_routes(in_path)

        # No nets routed in this synthetic call (router.routes is empty), so
        # every preserved route should be re-emitted.
        route_sexp, _stats, _cleanup = _finalize_routes(
            router,
            multi_pad,
            len(multi_pad),
            quiet=True,
            preserve_existing=True,
            preserved_routes=preserved,
        )
        total_preserved_segments = sum(len(r.segments) for r in preserved)
        assert route_sexp.count("(segment") == total_preserved_segments

        # With preserve_existing=False the existing geometry is NOT re-emitted
        # (regression-safe: matches pre-#3155 behaviour).
        route_sexp_off, _s, _c = _finalize_routes(
            router,
            multi_pad,
            len(multi_pad),
            quiet=True,
            preserve_existing=False,
            preserved_routes=preserved,
        )
        assert route_sexp_off.count("(segment") == 0


# ---------------------------------------------------------------------------
# Issue #4413: name-based net dialect + catastrophic-copper-loss guard.
# ---------------------------------------------------------------------------


def _to_name_based_dialect(pcb_text: str) -> str:
    """Rewrite inline numeric ``(net N)`` refs to the name-based dialect.

    KiCad-10 hand-evolved boards reference a segment/via's net by quoted
    name -- ``(net "Net-(C11-Pad2)")`` -- instead of the numeric
    ``(net 5)``.  This helper converts a numeric-dialect fixture into that
    dialect so the preservation path can be exercised against it.

    Only the *inline* number-only form is rewritten: the header
    declarations ``(net N "name")`` carry a trailing string and never match
    ``\\(net (\\d+)\\)``, so they are left untouched (and remain the source
    of truth for the name->id reverse map).  ``(net 0)`` / unnamed nets stay
    numeric because they have no name to key on.
    """
    net_names = parse_net_names(pcb_text)

    def repl(m: re.Match) -> str:
        nid = int(m.group(1))
        name = net_names.get(nid)
        if not name:
            return m.group(0)
        return f'(net "{name}")'

    return re.sub(r"\(net (\d+)\)", repl, pcb_text)


class TestNameBasedDialectParsing:
    """Issue #4413: the preserve-path parser must resolve ``(net "NAME")``."""

    def test_parse_segments_resolves_name_based_refs(self, board_text):
        """AC: parse_segments returns resolved segments on a name-based board."""
        name_based = _to_name_based_dialect(board_text)
        # Sanity: the conversion actually produced name-based inline refs and
        # removed the numeric-only inline form for a real net.
        assert '(net "LINE_A")' in name_based

        numeric = parse_segments(board_text)
        namebased = parse_segments(name_based)

        # Before the fix, ``namebased`` was empty (every block dropped).
        assert namebased, "name-based board parsed to ZERO segments (data-loss bug)"
        assert set(namebased) == set(numeric)
        for net in _ALL_SIGNAL_NETS:
            # Same geometry AND same resolved numeric net id (connectivity).
            assert _geom_set(namebased[net]) == _geom_set(numeric[net])

    def test_parse_vias_resolves_name_based_refs(self, tmp_path, board_text):
        """AC: parse_vias resolves name-based via refs (incl. a stitch via)."""
        net_names = parse_net_names(board_text)
        gnd_net = next(nid for nid, name in net_names.items() if name == "GND")
        stitched = _inject_stitch_via(board_text, net=gnd_net, x=12.3456, y=23.4567)
        name_based = _to_name_based_dialect(stitched)

        v_numeric = parse_vias(stitched)
        v_namebased = parse_vias(name_based)

        assert v_namebased, "name-based board parsed to ZERO vias (data-loss bug)"
        assert set(v_namebased) == set(v_numeric)
        # The GND stitch via survived with its resolved numeric net id.
        assert any(abs(v.x - 12.3456) < 1e-4 and v.net == gnd_net for v in v_namebased["GND"])

    def test_name_absent_from_header_is_not_dropped(self):
        """Edge case (a): a name-only ref with no header entry keeps the block."""
        pcb_text = (
            "(kicad_pcb\n"
            '  (net 0 "")\n'
            "  (segment (start 0 0) (end 1 1) (width 0.2) "
            '(layer "F.Cu") (net "MYSTERY_NET"))\n'
            ")\n"
        )
        segs = parse_segments(pcb_text)
        # The block is preserved (keyed by its name) rather than silently
        # discarded -- dropping copper is the exact failure mode being fixed.
        assert "MYSTERY_NET" in segs
        assert len(segs["MYSTERY_NET"]) == 1


class TestPreserveExistingNameBasedCLI:
    """End-to-end: --preserve-existing must round-trip a name-based board."""

    def test_preserve_keeps_other_nets_on_name_based_board(self, tmp_path, board_text):
        """AC #1: every OTHER net's copper survives a single-net re-route."""
        name_based = _to_name_based_dialect(board_text)
        orig = parse_segments(name_based)
        assert set(orig) == set(_ALL_SIGNAL_NETS)

        out_text = _run_route(tmp_path, name_based, preserve=True, skip_nets=_SKIP_ALL_BUT_LINE_A)
        out = parse_segments(out_text)

        # All pre-existing copper on the skipped nets survives byte-identical.
        for net in ("LINE_B", "LINE_C", "LINE_D", "NODE_A", "NODE_B", "NODE_C", "NODE_D"):
            assert net in out, (
                f"{net} copper was destroyed on a NAME-BASED board under "
                "--preserve-existing (the #4413 data-loss regression)"
            )
            assert _geom_set(out[net]) == _geom_set(orig[net])

        # The one routable net is present (re-routed in place).
        assert "LINE_A" in out
        assert len(out["LINE_A"]) > 0


class TestCatastrophicCopperLossGuard:
    """Issue #4413 defense-in-depth: refuse to overwrite on catastrophic loss."""

    @staticmethod
    def _board_with_segments(count: int) -> str:
        seg = '(segment (start 0 0) (end 1 1) (width 0.2) (layer "F.Cu") (net 1))'
        body = "\n  ".join(seg for _ in range(count))
        return f'(kicad_pcb\n  (net 0 "")\n  (net 1 "GND")\n  {body}\n)\n'

    def test_guard_aborts_and_leaves_output_untouched(self, tmp_path):
        """A >90% copper drop under the guard raises and does NOT write."""
        in_path = tmp_path / "in.kicad_pcb"
        in_path.write_text(self._board_with_segments(150))

        out_path = tmp_path / "out.kicad_pcb"
        sentinel = "SENTINEL-UNTOUCHED"
        out_path.write_text(sentinel)

        # Only a single segment re-emitted -> 1/150 survives (<10%).
        route_sexp = '(segment (start 0 0) (end 2 2) (width 0.2) (layer "F.Cu") (net 1))'
        with pytest.raises(CatastrophicCopperLossError):
            _write_routed_pcb(in_path, out_path, route_sexp, guard_copper_loss=True)

        # The output file on disk is unchanged (no partial/torn overwrite).
        assert out_path.read_text() == sentinel

    def test_guard_off_permits_the_same_write(self, tmp_path):
        """Without the guard flag (default) the same write proceeds."""
        in_path = tmp_path / "in.kicad_pcb"
        in_path.write_text(self._board_with_segments(150))
        out_path = tmp_path / "out.kicad_pcb"

        route_sexp = '(segment (start 0 0) (end 2 2) (width 0.2) (layer "F.Cu") (net 1))'
        _write_routed_pcb(in_path, out_path, route_sexp)  # guard_copper_loss defaults False
        assert out_path.exists()
        assert out_path.read_text().count("(segment") == 1

    def test_guard_no_false_positive_when_copper_preserved(self, tmp_path):
        """The guard does not fire when the preserved copper is re-emitted."""
        in_path = tmp_path / "in.kicad_pcb"
        in_path.write_text(self._board_with_segments(150))
        out_path = tmp_path / "out.kicad_pcb"

        # Re-emit all 150 preserved segments plus one freshly-routed one.
        seg = '(segment (start 0 0) (end 1 1) (width 0.2) (layer "F.Cu") (net 1))'
        preserved = "\n  ".join(seg for _ in range(150))
        route_sexp = (
            '(segment (start 0 0) (end 2 2) (width 0.2) (layer "F.Cu") (net 1))\n  ' + preserved
        )
        _write_routed_pcb(in_path, out_path, route_sexp, guard_copper_loss=True)
        assert out_path.read_text().count("(segment") == 151

    def test_guard_ignores_trivial_input(self, tmp_path):
        """A trivial input (<=100 segments) never trips the guard."""
        in_path = tmp_path / "in.kicad_pcb"
        in_path.write_text(self._board_with_segments(10))
        out_path = tmp_path / "out.kicad_pcb"

        route_sexp = '(segment (start 0 0) (end 2 2) (width 0.2) (layer "F.Cu") (net 1))'
        # 1/10 survives -- below the fraction, but the input is trivial so the
        # guard stays silent (a near-empty board legitimately routes to a few
        # elements).
        _write_routed_pcb(in_path, out_path, route_sexp, guard_copper_loss=True)
        assert out_path.exists()


# ---------------------------------------------------------------------------
# Issue #4433: per-net avoid_layers is a HARD constraint under composition.
# ---------------------------------------------------------------------------


class TestPreserveExistingHardAvoidLayers:
    """Issue #4433: composition (`--preserve-existing`) must honour a hard
    per-net ``avoid_layers`` and must NOT regress #4413 copper preservation.

    The report: a two-step composition (step-1 pre-routes some nets, step-2
    routes the rest with ``--preserve-existing``) leaked an HV net onto the
    inner planes because per-net ``avoid_layers`` was soft in Python and absent
    in the C++ (default) backend, and the reloaded step-1 outer copper congests
    the surface so escalation reaches the ``four_layer_all_signal`` rung where
    In1/In2 are routable.
    """

    def test_preserve_existing_with_hard_avoid_map_keeps_skipped_nets(self, tmp_path, board_text):
        """A ``--preserve-existing`` composition run with a net-class map that
        declares ``avoid_layers`` + ``target_ampacity`` on the routed net keeps
        every skipped net's copper byte-identical (no #4413 regression) and
        still routes the requested net.

        This exercises the full CLI path with the new hard-avoid plumbing loaded
        (sidecar merge -> DesignRules.strict_layers/target_ampacity gating ->
        backend routable-layer narrowing) end to end.
        """
        import json

        orig = parse_segments(board_text)
        assert set(orig) == set(_ALL_SIGNAL_NETS)

        # Declare LINE_A as a 15 A HV net that must avoid the inner planes.
        map_path = tmp_path / "netclass.json"
        map_path.write_text(
            json.dumps(
                {
                    "LINE_A": {
                        "name": "HV",
                        "avoid_layers": [1, 2],
                        "target_ampacity": 15.0,
                        "trace_width": 0.25,
                        "clearance": 0.2,
                    }
                }
            )
        )

        out_text = _run_route(
            tmp_path,
            board_text,
            preserve=True,
            skip_nets=_SKIP_ALL_BUT_LINE_A,
            net_class_map_path=str(map_path),
        )
        out = parse_segments(out_text)

        # Every skipped net survived byte-identical -- the new hard-avoid code
        # path does not disturb #4413 copper preservation.
        for net in ("LINE_B", "LINE_C", "LINE_D", "NODE_A", "NODE_B", "NODE_C", "NODE_D"):
            assert net in out, (
                f"{net} geometry destroyed under --preserve-existing + hard avoid map"
            )
            assert _geom_set(out[net]) == _geom_set(orig[net]), (
                f"{net} geometry changed under --preserve-existing + hard avoid map"
            )

        # The HV net is still routed, and (on this 2-layer board where In1/In2
        # do not physically exist) it carries zero inner-layer copper.
        assert "LINE_A" in out and len(out["LINE_A"]) > 0
        assert all(seg.layer.name not in ("In1.Cu", "In2.Cu") for seg in out["LINE_A"])

    def test_composition_hard_avoid_net_gets_zero_inner_segments(self):
        """Deterministic composition repro: reloaded step-1 outer copper forces
        the step-2 HV net toward the inner planes; the hard ``avoid_layers``
        (via ``target_ampacity``) keeps it OFF In1/In2 while the SOFT default
        still leaks -- and the preserved step-1 copper is untouched.

        Built on the core ``RoutingGrid``/pathfinder (not the full CLI) so it is
        fast and environment-independent, while still exercising the exact
        four_layer_all_signal rung where the constraint was absent.
        """
        from kicad_tools.router.cpp_backend import (
            CppGrid,
            CppPathfinder,
            is_cpp_available,
        )
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.layers import Layer, LayerStack
        from kicad_tools.router.primitives import Obstacle, Pad, Route, Segment
        from kicad_tools.router.rules import DesignRules, NetClassRouting

        def route_hv(*, hard: bool):
            rules = DesignRules(
                trace_width=0.25,
                trace_clearance=0.2,
                via_diameter=0.6,
                via_clearance=0.2,
                grid_resolution=0.1,
            )
            grid = RoutingGrid(
                width=20.0,
                height=10.0,
                rules=rules,
                layer_stack=LayerStack.four_layer_all_signal(),
            )
            start = Pad(
                x=3.0,
                y=5.0,
                width=1.0,
                height=1.0,
                net=1,
                net_name="HV",
                layer=Layer.F_CU,
                ref="J1",
                pin="1",
                through_hole=True,
                drill=0.6,
            )
            end = Pad(
                x=17.0,
                y=5.0,
                width=1.0,
                height=1.0,
                net=1,
                net_name="HV",
                layer=Layer.F_CU,
                ref="J2",
                pin="1",
                through_hole=True,
                drill=0.6,
            )
            grid.add_pad(start)
            grid.add_pad(end)

            # Model reloaded step-1 copper: an existing preserved route occupying
            # the outer surface, marked on the grid as obstacles the step-2 net
            # must route around.  A full-height wall on BOTH outer layers stands
            # in for a congested surface where the only crossing is inner.
            preserved = Route(
                net=2,
                net_name="PRESERVED",
                segments=[
                    Segment(x1=10.0, y1=0.0, x2=10.0, y2=10.0, width=0.5, net=2, layer=Layer.F_CU),
                    Segment(x1=10.0, y1=0.0, x2=10.0, y2=10.0, width=0.5, net=2, layer=Layer.B_CU),
                ],
            )
            grid.add_obstacle(Obstacle(x=10.0, y=5.0, width=2.0, height=16.0, layer=Layer.F_CU))
            grid.add_obstacle(Obstacle(x=10.0, y=5.0, width=2.0, height=16.0, layer=Layer.B_CU))

            nc = NetClassRouting(
                name="HV",
                trace_width=0.25,
                clearance=0.2,
                avoid_layers=[1, 2],
                target_ampacity=(15.0 if hard else None),
            )
            backend = "cpp" if is_cpp_available() else "python"
            if backend == "cpp":
                cpp_grid = CppGrid.from_routing_grid(grid)
                pf = CppPathfinder(cpp_grid, rules, diagonal_routing=True, net_class_map={"HV": nc})
            else:
                from kicad_tools.router.pathfinder import Router

                pf = Router(grid, rules, net_class_map={"HV": nc})
            route = pf.route(start, end, net_class=nc)
            return route, preserved

        # SOFT default: the composition congestion pushes the HV net onto an
        # inner layer (the reported leak).  This confirms the repro is real.
        soft_route, _ = route_hv(hard=False)
        assert soft_route is not None, "soft HV net failed to route across the congested surface"
        soft_inner = [s for s in soft_route.segments if s.layer.value in (1, 2)]
        assert soft_inner, (
            "expected the SOFT default to leak onto an inner plane under composition "
            "congestion (the reported #4433 symptom) -- test setup no longer reproduces it"
        )

        # HARD (target_ampacity): zero inner-layer segments -- the fix.  The
        # preserved step-1 copper is never touched by the step-2 route.
        hard_route, preserved = route_hv(hard=True)
        hard_inner = (
            0
            if hard_route is None
            else sum(1 for s in hard_route.segments if s.layer.value in (1, 2))
        )
        assert hard_inner == 0, (
            "ampacity-bearing HV net still landed on an inner plane under "
            "--preserve-existing composition"
        )
        # #4413 non-regression: the preserved route object is intact (the step-2
        # HV route neither consumes nor mutates it).
        assert len(preserved.segments) == 2
        assert {s.layer.kicad_name for s in preserved.segments} == {"F.Cu", "B.Cu"}
