"""Tests for the ``--complete`` completion pass (Issue #4471, epic #4465).

``kct route <board> --complete`` auto-detects the currently-unconnected signal
nets and routes ONLY those links, treating every other net's copper as a fixed
obstacle.  It is the first-class composition of pieces that already existed but
were unwired:

* detection -- ``partial_rescue.partially_connected_signal_nets`` (include_unrouted),
* net selection -- the #4322 ``--nets`` route-only machinery (invert into
  ``--skip-nets`` + imply ``--preserve-existing``),
* fixed-obstacle routing -- the lattice netset driver
  (``core.py:_negotiate_lattice_netset`` routes ONLY ``self.nets`` against
  ``fixed_copper = existing_routes not in self.nets``, #4355),
* the #4280 engine/strategy gate, which now carves out ``--complete`` (a
  route-only-listed-links contract, not the whole-netset negotiation the gate
  protects).

The unit tests exercise the three CLI-layer helpers in isolation; the
end-to-end test routes a deliberately-stranded link on a real board through the
lattice engine and asserts the other net's copper is preserved.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import (
    _apply_complete_mode_defaults,
    _resolve_complete_nets,
    _validate_route_engine_strategy,
)
from kicad_tools.cli.route_cmd import (
    main as route_main,
)


# ---------------------------------------------------------------------------
# Argparse defaults the helpers read off the namespace.
# ---------------------------------------------------------------------------
def _complete_args(**overrides) -> argparse.Namespace:
    base = {
        "complete": True,
        "route_engine": "grid",
        "strategy": "negotiated",
        "preserve_existing": False,
        "nets": None,
        "skip_nets": None,
        "manufacturer": "jlcpcb",
        "quiet": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class _FakeParser:
    """Minimal stand-in for ``parser.get_default`` used by the defaults helper."""

    @staticmethod
    def get_default(name: str):
        return {"route_engine": "grid", "strategy": "negotiated"}.get(name)


# ---------------------------------------------------------------------------
# _apply_complete_mode_defaults -- implications stamped BEFORE the #4280 gate
# ---------------------------------------------------------------------------
class TestApplyCompleteModeDefaults:
    def test_implies_preserve_existing_and_lattice(self, capsys):
        args = _complete_args()
        _apply_complete_mode_defaults(args, _FakeParser())
        assert args.preserve_existing is True
        assert args.route_engine == "lattice"
        assert "lattice engine" in capsys.readouterr().out

    def test_respects_explicit_engine_override(self, capsys):
        args = _complete_args(route_engine="mesh")
        _apply_complete_mode_defaults(args, _FakeParser())
        # An explicit non-default engine is respected -- not flipped to lattice.
        assert args.route_engine == "mesh"
        assert args.preserve_existing is True

    def test_noop_when_complete_absent(self):
        args = _complete_args(complete=False)
        _apply_complete_mode_defaults(args, _FakeParser())
        # No implication stamped -- plain ``kct route`` is byte-identical.
        assert args.preserve_existing is False
        assert args.route_engine == "grid"


# ---------------------------------------------------------------------------
# _resolve_complete_nets -- auto-detection + mutual exclusion + no-op
# ---------------------------------------------------------------------------
class TestResolveCompleteNets:
    def test_populates_nets_from_detection(self, monkeypatch, capsys):
        import kicad_tools.router.partial_rescue as pr

        monkeypatch.setattr(
            pr, "partially_connected_signal_nets", lambda *a, **k: ["/NET_A", "/NET_B"]
        )
        args = _complete_args()
        rc = _resolve_complete_nets(args, Path("dummy.kicad_pcb"))
        assert rc == 0
        assert args.nets == "/NET_A,/NET_B"
        assert getattr(args, "_complete_noop", False) is False
        out = capsys.readouterr().out
        assert "/NET_A" in out and "/NET_B" in out

    def test_empty_detection_is_noop(self, monkeypatch, capsys):
        import kicad_tools.router.partial_rescue as pr

        monkeypatch.setattr(pr, "partially_connected_signal_nets", lambda *a, **k: [])
        args = _complete_args()
        rc = _resolve_complete_nets(args, Path("dummy.kicad_pcb"))
        assert rc == 0
        assert args._complete_noop is True
        # No net set is fabricated -- a full re-route must NOT be triggered.
        assert args.nets is None
        assert "already fully connected" in capsys.readouterr().out

    def test_mutually_exclusive_with_nets(self, capsys):
        args = _complete_args(nets="/NET_A")
        rc = _resolve_complete_nets(args, Path("dummy.kicad_pcb"))
        assert rc == 2
        assert "mutually exclusive" in capsys.readouterr().err

    def test_mutually_exclusive_with_skip_nets(self, capsys):
        args = _complete_args(skip_nets="GND")
        rc = _resolve_complete_nets(args, Path("dummy.kicad_pcb"))
        assert rc == 2
        assert "mutually exclusive" in capsys.readouterr().err

    def test_noop_when_complete_absent(self, monkeypatch):
        import kicad_tools.router.partial_rescue as pr

        called = {"n": 0}

        def _spy(*a, **k):
            called["n"] += 1
            return []

        monkeypatch.setattr(pr, "partially_connected_signal_nets", _spy)
        args = _complete_args(complete=False)
        rc = _resolve_complete_nets(args, Path("dummy.kicad_pcb"))
        assert rc == 0
        # Detection is not even attempted without --complete.
        assert called["n"] == 0
        assert args.nets is None


# ---------------------------------------------------------------------------
# #4280 gate carve-out -- --complete permits lattice + coerces strategy
# ---------------------------------------------------------------------------
def _gate_args(**overrides) -> argparse.Namespace:
    base = {
        "route_engine": "grid",
        "strategy": "negotiated",
        "two_phase": False,
        "multi_resolution": False,
        "escape_routing": None,
        "no_escape_routing": False,
        "complete": False,
        "quiet": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class TestGateCompleteCarveOut:
    def test_complete_lattice_coerces_negotiated_to_basic(self, capsys):
        args = _gate_args(route_engine="lattice", strategy="negotiated", complete=True)
        rc = _validate_route_engine_strategy(args)
        assert rc == 0  # NOT rejected
        assert args.strategy == "basic"  # coerced
        assert "coercing --strategy negotiated" in capsys.readouterr().out

    def test_complete_lattice_basic_is_clean(self, capsys):
        args = _gate_args(route_engine="lattice", strategy="basic", complete=True)
        rc = _validate_route_engine_strategy(args)
        assert rc == 0
        assert args.strategy == "basic"

    def test_without_complete_negotiated_still_rejected(self, capsys):
        # The gate is unchanged for non-complete runs (#4280 still protects).
        args = _gate_args(route_engine="lattice", strategy="negotiated", complete=False)
        rc = _validate_route_engine_strategy(args)
        assert rc == 2
        assert "#4280" in capsys.readouterr().err

    def test_complete_does_not_relax_two_phase(self, capsys):
        # --two-phase genuinely bypasses the netset path -- still a hard
        # conflict even under --complete.
        args = _gate_args(route_engine="lattice", strategy="basic", complete=True, two_phase=True)
        rc = _validate_route_engine_strategy(args)
        assert rc == 2
        assert "--two-phase" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# End-to-end: one deliberately-stranded link on an otherwise-routed board.
# NET1 is fully connected (a pre-existing segment between its two pads); NET2
# is unrouted.  ``--complete`` must close NET2 on the lattice engine and leave
# NET1's copper untouched.
# ---------------------------------------------------------------------------
STRANDED_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (net 2 "NET2")
  (gr_rect (start 100 100) (end 130 120)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 108 105)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 120 105)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "NET2"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "NET2"))
  )
  (segment (start 107.49 105) (end 108.51 105) (width 0.2) (layer "F.Cu") (net 1))
)
"""


def _segment_nets(pcb_text: str) -> list[int]:
    import re

    return [int(m.group(1)) for m in re.finditer(r"\(segment.*?\(net (\d+)\)", pcb_text, re.S)]


@pytest.fixture
def stranded_board(tmp_path: Path) -> Path:
    board = tmp_path / "stranded.kicad_pcb"
    board.write_text(STRANDED_BOARD)
    return board


class TestCompleteEndToEnd:
    def test_closes_stranded_link_on_lattice_preserving_other_copper(
        self, stranded_board: Path, tmp_path: Path
    ):
        out = tmp_path / "stranded_out.kicad_pcb"
        rc = route_main([str(stranded_board), "-o", str(out), "--complete", "--backend", "cpp"])
        assert rc == 0, "completion pass should succeed"
        assert out.exists()

        seg_nets = _segment_nets(out.read_text())
        # NET1's pre-existing copper is preserved (fixed obstacle, not deleted).
        assert 1 in seg_nets, "NET1 copper must be preserved"
        # NET2 (the stranded link) is now routed -- new copper appeared.
        assert 2 in seg_nets, "NET2 (stranded) must be routed by --complete"

    def test_fully_connected_board_is_safe_noop(self, stranded_board: Path, tmp_path: Path):
        # First close NET2, producing a fully-connected board.
        routed = tmp_path / "routed.kicad_pcb"
        assert (
            route_main([str(stranded_board), "-o", str(routed), "--complete", "--backend", "cpp"])
            == 0
        )
        # A second --complete pass has nothing to route: byte-identical output.
        noop = tmp_path / "noop.kicad_pcb"
        rc = route_main([str(routed), "-o", str(noop), "--complete", "--backend", "cpp"])
        assert rc == 0
        assert noop.read_bytes() == routed.read_bytes(), "no-op must not mutate the board"

    def test_composes_with_explicit_route_engine_lattice(
        self, stranded_board: Path, tmp_path: Path, capsys
    ):
        # --complete + explicit --route-engine lattice must NOT trip the #4280
        # gate; the coerced strategy prints a notice rather than erroring.
        out = tmp_path / "compose_out.kicad_pcb"
        rc = route_main(
            [
                str(stranded_board),
                "-o",
                str(out),
                "--complete",
                "--route-engine",
                "lattice",
                "--backend",
                "cpp",
            ]
        )
        assert rc == 0
        assert 2 in _segment_nets(out.read_text())
