"""Regression tests for issue #4983.

A PCB saved in KiCad 10's name-only net syntax -- inline pad references like
``(pad "1" smd rect ... (net "SIGNAL"))`` with no numeric net id, and
possibly no top-level ``(net N "NAME")`` declaration table at all -- made
``kct route`` report "SUCCESS" with a vacuous 0/0 net denominator, write an
unrouted output, and exit 0.

Root cause: ``router/io.py``'s pad-extraction paths (``load_pcb_for_routing``
and ``load_pads_for_analysis``) resolved net names to numeric net ids using a
name -> id map built ONLY from the numeric-plus-name dialect (top-level
``(net N "NAME")`` declarations).  On a name-only board with no such table,
that map was empty, so every name-only pad silently resolved to
``net_num=0`` -- the "no net" / obstacle sentinel -- and never entered
``router.nets``.  The CLI's ``--nets`` preflight (schema-level, dialect-aware)
still accepted the request, so the router reported "0/0 nets" as a false
success instead of routing or failing loudly.

These tests cover the acceptance criteria from the issue:

1. Both net dialects normalize identically before the routing graph is built
   (:class:`TestBuildNetNumberMap`, :class:`TestLoadPcbForRoutingDialectParity`,
   :class:`TestLoadPadsForAnalysisDialectParity`).
2. A requested net with two bound pads enters the routing denominator and is
   physically connected before success is reported
   (:class:`TestRouteCliNameOnlyDialect`).
3. Lost pad bindings fail loudly instead of vacuous exit-0 success
   (:class:`TestRejectLostRouteOnlyBindings`).
4. Numeric/name-only parity + negative tests for absent nets, verifying
   actual written copper (not just the printed progress counter)
   (:class:`TestRouteCliNameOnlyDialect`).
"""

from __future__ import annotations

import re
from pathlib import Path

from kicad_tools.cli.route_cmd import _reject_lost_route_only_bindings
from kicad_tools.cli.route_cmd import main as route_main
from kicad_tools.router.io import (
    _build_net_number_map,
    load_pads_for_analysis,
    load_pcb_for_routing,
    verify_output_connectivity,
)
from kicad_tools.router.primitives import Pad as PadObj

# ---------------------------------------------------------------------------
# Fixtures: the exact minimal repro from issue #4983 (name-only, no header
# net table at all) and its numeric-dialect positive control.
# ---------------------------------------------------------------------------

_NAME_ONLY_FIXTURE = """\
(kicad_pcb (version 20260206) (generator "pcbnew")
(general (thickness 1.6)) (paper "A4")
(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (5 "F.SilkS" user "F.Silkscreen") (1 "F.Mask" user) (3 "B.Mask" user) (13 "F.Paste" user) (25 "Edge.Cuts" user))
 (footprint "Test:Pad" (layer "F.Cu") (uuid "5ed2da21-2c5b-4dc0-92f1-d541550cca3c") (at 10 10)
(property "Reference" "J1" (at 0 -2) (layer "F.SilkS") (effects (font (size 1 1) (thickness .15))))
(attr smd) (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net "SIGNAL") (uuid "aa25d89c-8530-4928-9336-1ce04555242d")))(footprint "Test:Pad" (layer "F.Cu") (uuid "c71740f5-8a42-4d3f-a6d4-92591d06b536") (at 20 10)
(property "Reference" "J2" (at 0 -2) (layer "F.SilkS") (effects (font (size 1 1) (thickness .15))))
(attr smd) (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net "SIGNAL") (uuid "7271acd3-564c-461a-9ba2-473f671e3596")))
(gr_rect (start 0 0) (end 30 20) (stroke (width .05) (type solid)) (fill none) (layer "Edge.Cuts") (uuid "439c88ec-f215-4ad2-bff5-bc09ae692c4a")))
"""

_NUMERIC_FIXTURE = """\
(kicad_pcb (version 20260206) (generator "pcbnew")
(general (thickness 1.6)) (paper "A4")
(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (5 "F.SilkS" user "F.Silkscreen") (1 "F.Mask" user) (3 "B.Mask" user) (13 "F.Paste" user) (25 "Edge.Cuts" user))
(net 0 "") (net 1 "SIGNAL")
 (footprint "Test:Pad" (layer "F.Cu") (uuid "5ed2da21-2c5b-4dc0-92f1-d541550cca3c") (at 10 10)
(property "Reference" "J1" (at 0 -2) (layer "F.SilkS") (effects (font (size 1 1) (thickness .15))))
(attr smd) (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIGNAL") (uuid "aa25d89c-8530-4928-9336-1ce04555242d")))(footprint "Test:Pad" (layer "F.Cu") (uuid "c71740f5-8a42-4d3f-a6d4-92591d06b536") (at 20 10)
(property "Reference" "J2" (at 0 -2) (layer "F.SilkS") (effects (font (size 1 1) (thickness .15))))
(attr smd) (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIGNAL") (uuid "7271acd3-564c-461a-9ba2-473f671e3596")))
(gr_rect (start 0 0) (end 30 20) (stroke (width .05) (type solid)) (fill none) (layer "Edge.Cuts") (uuid "439c88ec-f215-4ad2-bff5-bc09ae692c4a")))
"""

# 3-pad/2-net board (SIGNAL: 2 pads, LONELY: 1 pad) -- the exact shape of
# the Judge's PR #5121 repro for the single-pad ``--nets`` regression.
_SINGLE_PAD_NET_FIXTURE = """\
(kicad_pcb (version 20260206) (generator "pcbnew")
(general (thickness 1.6)) (paper "A4")
(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (5 "F.SilkS" user "F.Silkscreen") (1 "F.Mask" user) (3 "B.Mask" user) (13 "F.Paste" user) (25 "Edge.Cuts" user))
 (footprint "Test:Pad" (layer "F.Cu") (uuid "5ed2da21-2c5b-4dc0-92f1-d541550cca3c") (at 10 10)
(property "Reference" "J1" (at 0 -2) (layer "F.SilkS") (effects (font (size 1 1) (thickness .15))))
(attr smd) (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net "SIGNAL") (uuid "aa25d89c-8530-4928-9336-1ce04555242d")))(footprint "Test:Pad" (layer "F.Cu") (uuid "c71740f5-8a42-4d3f-a6d4-92591d06b536") (at 20 10)
(property "Reference" "J2" (at 0 -2) (layer "F.SilkS") (effects (font (size 1 1) (thickness .15))))
(attr smd) (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net "SIGNAL") (uuid "7271acd3-564c-461a-9ba2-473f671e3596")))
(footprint "Test:Pad" (layer "F.Cu") (uuid "d8e2f3a1-9a51-4d3f-a6d4-92591d06b537") (at 15 15)
(property "Reference" "J3" (at 0 -2) (layer "F.SilkS") (effects (font (size 1 1) (thickness .15))))
(attr smd) (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net "LONELY") (uuid "e9f3a4b2-1a62-4d3f-a6d4-92591d06b538")))
(gr_rect (start 0 0) (end 30 20) (stroke (width .05) (type solid)) (fill none) (layer "Edge.Cuts") (uuid "439c88ec-f215-4ad2-bff5-bc09ae692c4a")))
"""


# ---------------------------------------------------------------------------
# 1. Unit tests for the shared dialect-normalization helper.
# ---------------------------------------------------------------------------


class TestBuildNetNumberMap:
    def test_numeric_dialect_header_table_honored(self):
        text = '(kicad_pcb (net 0 "") (net 1 "SIGNAL") (net 2 "GND"))'
        assert _build_net_number_map(text) == {"SIGNAL": 1, "GND": 2}

    def test_name_only_dialect_no_header_table(self):
        """No numeric net id anywhere -- must synthesize a positive id."""
        text = '(kicad_pcb (pad "1" smd rect (net "SIGNAL")) (pad "1" smd rect (net "SIGNAL")))'
        net_map = _build_net_number_map(text)
        assert net_map.get("SIGNAL", 0) > 0

    def test_mixed_dialect_header_ids_take_priority(self):
        """A name with a numeric header id keeps it; a name-only net gets a
        synthesized id that does not collide with the header table."""
        text = (
            '(kicad_pcb (net 0 "") (net 1 "GND") '
            '(pad "1" smd rect (net 1 "GND")) '
            '(pad "1" smd rect (net "SIGNAL")))'
        )
        net_map = _build_net_number_map(text)
        assert net_map["GND"] == 1
        assert net_map["SIGNAL"] > 0
        assert net_map["SIGNAL"] != net_map["GND"]

    def test_empty_net_name_never_assigned_an_id(self):
        text = '(kicad_pcb (net 0 "") (pad "1" smd rect (net "")))'
        net_map = _build_net_number_map(text)
        assert "" not in net_map

    def test_synthesis_is_deterministic_first_seen_order(self):
        text = '(kicad_pcb (pad "1" smd rect (net "B")) (pad "2" smd rect (net "A")))'
        net_map = _build_net_number_map(text)
        assert net_map["B"] < net_map["A"]


# ---------------------------------------------------------------------------
# 2. load_pcb_for_routing dialect parity (the router's own entry point).
# ---------------------------------------------------------------------------


class TestLoadPcbForRoutingDialectParity:
    def test_name_only_pads_enter_routing_graph(self, tmp_path: Path):
        """Before the fix: net_num=0 for both pads, router.nets stayed empty."""
        pcb_path = tmp_path / "named.kicad_pcb"
        pcb_path.write_text(_NAME_ONLY_FIXTURE)

        router, net_map = load_pcb_for_routing(str(pcb_path))

        assert "SIGNAL" in net_map
        net_id = net_map["SIGNAL"]
        assert net_id > 0
        assert net_id in router.nets
        assert len(router.nets[net_id]) == 2
        assert router.net_names.get(net_id) == "SIGNAL"
        assert {ref for ref, _pin in router.nets[net_id]} == {"J1", "J2"}

    def test_numeric_and_name_only_produce_equivalent_graphs(self, tmp_path: Path):
        named_path = tmp_path / "named.kicad_pcb"
        named_path.write_text(_NAME_ONLY_FIXTURE)
        numeric_path = tmp_path / "numeric.kicad_pcb"
        numeric_path.write_text(_NUMERIC_FIXTURE)

        named_router, named_net_map = load_pcb_for_routing(str(named_path))
        numeric_router, numeric_net_map = load_pcb_for_routing(str(numeric_path))

        # Both dialects must resolve the requested net to a real, 2-pad,
        # routable net -- not silently collapse to net 0.
        named_id = named_net_map["SIGNAL"]
        numeric_id = numeric_net_map["SIGNAL"]
        assert len(named_router.nets[named_id]) == len(numeric_router.nets[numeric_id]) == 2

        # Same pad geometry regardless of dialect.
        named_pads = {k: (p.x, p.y) for k, p in named_router.pads.items()}
        numeric_pads = {k: (p.x, p.y) for k, p in numeric_router.pads.items()}
        assert named_pads == numeric_pads


# ---------------------------------------------------------------------------
# 3. load_pads_for_analysis dialect parity (preflight / fine-pitch analysis).
# ---------------------------------------------------------------------------


class TestLoadPadsForAnalysisDialectParity:
    def test_name_only_pads_carry_correct_net(self, tmp_path: Path):
        pcb_path = tmp_path / "named.kicad_pcb"
        pcb_path.write_text(_NAME_ONLY_FIXTURE)

        pads = load_pads_for_analysis(str(pcb_path))
        assert len(pads) == 2
        for pad in pads:
            assert pad.net_name == "SIGNAL"
            assert pad.net > 0
        # Both pads share the same net id.
        assert len({pad.net for pad in pads}) == 1

    def test_numeric_and_name_only_agree_on_net_name(self, tmp_path: Path):
        named_path = tmp_path / "named.kicad_pcb"
        named_path.write_text(_NAME_ONLY_FIXTURE)
        numeric_path = tmp_path / "numeric.kicad_pcb"
        numeric_path.write_text(_NUMERIC_FIXTURE)

        named_pads = load_pads_for_analysis(str(named_path))
        numeric_pads = load_pads_for_analysis(str(numeric_path))

        assert {p.net_name for p in named_pads} == {p.net_name for p in numeric_pads} == {"SIGNAL"}
        assert {p.net for p in named_pads} == {p.net for p in numeric_pads}


# ---------------------------------------------------------------------------
# 4. verify_output_connectivity must resolve name-only output copper even
#    when the written file carries no header net table (issue #4983).
# ---------------------------------------------------------------------------


class TestVerifyOutputConnectivityNameOnlyNoHeaderTable:
    def test_name_only_segments_resolve_via_caller_net_names(self):
        # Output content with NO top-level (net N "NAME") table -- exactly
        # what the name-only writer path emits when the input had none.
        pcb_content = """(kicad_pcb
  (segment (start 0 0) (end 10 0) (width 0.25) (layer "F.Cu") (net "SIGNAL"))
)
"""
        pad_a = PadObj(x=0, y=0, width=1, height=1, net=1, net_name="SIGNAL", ref="J1", pin="1")
        pad_b = PadObj(x=10, y=0, width=1, height=1, net=1, net_name="SIGNAL", ref="J2", pin="1")

        report = verify_output_connectivity(
            pcb_content=pcb_content,
            net_pads={1: [pad_a, pad_b]},
            net_names={1: "SIGNAL"},
        )
        assert report[1]["connected"] is True
        assert report[1]["connected_pads"] == 2


# ---------------------------------------------------------------------------
# 5. Full CLI regression: the exact repro from the issue.
# ---------------------------------------------------------------------------


def _route_argv(pcb_path: Path, out_path: Path, nets: str = "SIGNAL") -> list[str]:
    return [
        str(pcb_path),
        "--nets",
        nets,
        "--preserve-existing",
        "--layers",
        "2",
        "--grid",
        ".1",
        # Force the Python backend for CI/host portability -- the fix is in
        # the pad/net parsing layer, upstream of backend selection, so
        # either backend exercises the same code path.
        "--backend",
        "python",
        "--no-placement-feedback",
        "--no-cache",
        # This is a copper/diagnostic regression, not a speed benchmark.
        # Leave room for supervised startup and post-routing checks under CI.
        "--timeout",
        "30",
        "--no-optimize",
        "-o",
        str(out_path),
    ]


def _segment_count(pcb_text: str) -> int:
    return len(re.findall(r"\(segment\b", pcb_text))


class TestRouteCliNameOnlyDialect:
    def test_name_only_board_routes_and_writes_copper(self, tmp_path: Path):
        """Issue #4983's exact repro: must route real copper, not 0/0."""
        pcb_path = tmp_path / "named.kicad_pcb"
        pcb_path.write_text(_NAME_ONLY_FIXTURE)
        out_path = tmp_path / "named-out.kicad_pcb"

        rc = route_main(_route_argv(pcb_path, out_path))

        assert rc == 0
        assert out_path.exists()
        out_text = out_path.read_text()

        # Acceptance criterion: verify ACTUAL written copper, not just the
        # progress counter the CLI prints.
        assert _segment_count(out_text) > 0

        # The two original name-only pads must still both be present and
        # the router must report them connected in the written output.
        report = verify_output_connectivity(
            pcb_content=out_text,
            net_pads={
                1: [
                    PadObj(
                        x=10, y=10, width=1, height=1, net=1, net_name="SIGNAL", ref="J1", pin="1"
                    ),
                    PadObj(
                        x=20, y=10, width=1, height=1, net=1, net_name="SIGNAL", ref="J2", pin="1"
                    ),
                ]
            },
            net_names={1: "SIGNAL"},
        )
        assert report[1]["connected"] is True

    def test_name_only_and_numeric_dialects_route_equivalently(self, tmp_path: Path):
        """Parity: both dialects must produce the same routing result for
        equivalent fixtures (issue #4983 acceptance criterion)."""
        named_path = tmp_path / "named.kicad_pcb"
        named_path.write_text(_NAME_ONLY_FIXTURE)
        named_out = tmp_path / "named-out.kicad_pcb"

        numeric_path = tmp_path / "numeric.kicad_pcb"
        numeric_path.write_text(_NUMERIC_FIXTURE)
        numeric_out = tmp_path / "numeric-out.kicad_pcb"

        named_rc = route_main(_route_argv(named_path, named_out))
        numeric_rc = route_main(_route_argv(numeric_path, numeric_out))

        assert named_rc == 0
        assert numeric_rc == 0

        named_segments = _segment_count(named_out.read_text())
        numeric_segments = _segment_count(numeric_out.read_text())
        assert named_segments > 0
        assert numeric_segments > 0
        assert named_segments == numeric_segments

    def test_genuinely_absent_net_fails_loudly(self, tmp_path: Path, capfd):
        """Negative test: a net name that does not exist on the board at
        all must abort with a non-zero exit and a clear error -- never a
        vacuous 0/0 SUCCESS."""
        pcb_path = tmp_path / "named.kicad_pcb"
        pcb_path.write_text(_NAME_ONLY_FIXTURE)
        out_path = tmp_path / "named-out.kicad_pcb"

        rc = route_main(_route_argv(pcb_path, out_path, nets="DOES_NOT_EXIST"))

        assert rc != 0
        err = capfd.readouterr().err
        assert "not present on the board" in err
        assert not out_path.exists()


class TestRouteCliSinglePadOnlyNetsRequest:
    """Issue #5121 Judge follow-up: a --nets request naming ONLY single-pad
    net(s) is a pre-existing, intentionally-supported workflow (e.g.
    scripted per-net debug/escape routing over test-point nets) -- it must
    exit 0 with a graceful warning, not be treated as a #4983-style lost
    binding."""

    def test_single_pad_only_nets_request_succeeds_gracefully(self, tmp_path: Path, capfd):
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_SINGLE_PAD_NET_FIXTURE)
        out_path = tmp_path / "out.kicad_pcb"

        rc = route_main(_route_argv(pcb_path, out_path, nets="LONELY"))

        err = capfd.readouterr().err
        assert "fewer than 2 pads" in err
        assert "loader bug" not in err
        assert rc == 0
        assert out_path.exists()


# ---------------------------------------------------------------------------
# 6. Defense-in-depth: the "fail loudly on lost bindings" guard itself.
# ---------------------------------------------------------------------------


class TestRejectLostRouteOnlyBindings:
    def test_no_route_only_request_is_a_noop(self):
        args = type("Args", (), {"_route_only_nets": None})()
        assert _reject_lost_route_only_bindings(args, 0) is None
        assert _reject_lost_route_only_bindings(args, 5) is None

    def test_nonzero_denominator_is_trusted(self):
        args = type("Args", (), {"_route_only_nets": ["SIGNAL"]})()
        assert _reject_lost_route_only_bindings(args, 1) is None

    def test_zero_denominator_with_requested_nets_fails_loudly(self, capsys):
        args = type("Args", (), {"_route_only_nets": ["SIGNAL"]})()
        rc = _reject_lost_route_only_bindings(args, 0)
        assert rc is not None
        assert rc != 0
        err = capsys.readouterr().err
        assert "SIGNAL" in err
        assert "0 routable net" in err

    def test_zero_denominator_when_all_requested_nets_are_sub_two_pad_is_not_a_bug(self):
        """Issue #5121 Judge follow-up: --nets naming ONLY single-pad net(s)
        is a legitimate, preflight-warned outcome (see
        ``_resolve_route_only_nets``'s ``under_two`` reporting), not a lost
        binding -- must not be flagged."""
        args = type(
            "Args",
            (),
            {
                "_route_only_nets": ["LONELY"],
                "_route_only_nets_under_two": {"LONELY"},
            },
        )()
        assert _reject_lost_route_only_bindings(args, 0) is None

    def test_zero_denominator_with_mixed_sub_two_and_routable_nets_still_fails_loudly(self):
        """A genuine lost binding must still be caught even when SOME of the
        requested nets are sub-two-pad -- only an ALL-sub-two request is
        exempt."""
        args = type(
            "Args",
            (),
            {
                "_route_only_nets": ["LONELY", "SIGNAL"],
                "_route_only_nets_under_two": {"LONELY"},
            },
        )()
        rc = _reject_lost_route_only_bindings(args, 0)
        assert rc is not None
        assert rc != 0
