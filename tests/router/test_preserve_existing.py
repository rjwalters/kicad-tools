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

from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import (
    _capture_preserved_routes,
    _finalize_routes,
    _serialize_preserved_routes,
)
from kicad_tools.cli.route_cmd import main as route_main
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.optimizer.pcb import parse_net_names, parse_segments

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
