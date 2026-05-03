"""Regression tests for ``--power-nets`` + ``edge_clearance`` interaction.

Issue #2463: When the user specifies ``--power-nets`` explicitly on the
``route`` command, the resulting power zones must be inset from the board
outline by the configured ``edge_clearance`` (e.g. 0.3mm for the JLCPCB
manufacturer profile).

Prior to the fix, ``route_cmd.py`` instantiated ``ZoneGenerator.from_pcb``
without the ``edge_clearance`` kwarg in the user-explicit path (the
auto-pour path was already correct), causing zones to extend to the board
edge and producing 5+ ``edge_clearance_zone`` DRC errors.

These tests guard both the *unit* contract (that the kwarg is forwarded)
and the *integration* contract (that resulting zone polygons are inset).
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixture: minimal 2-pad rectangular PCB with a GND net.
# Outline 30x25mm anchored at (100, 100).
# ---------------------------------------------------------------------------

_PCB_TEMPLATE = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "VIN")
  (footprint "TestLib:Pad" (layer "F.Cu") (at 110 110)
    (pad "1" smd roundrect (at 0 0) (size 1.0 1.3)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 1 "GND"))
  )
  (footprint "TestLib:Pad" (layer "F.Cu") (at 120 115)
    (pad "1" smd roundrect (at 0 0) (size 1.0 1.3)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 2 "VIN"))
  )
  (gr_line (start 100 100) (end 130 100) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 130 100) (end 130 125) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 130 125) (end 100 125) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 100 125) (end 100 100) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
)
"""


def _write_pcb(tmp_path: Path) -> Path:
    pcb_path = tmp_path / "fixture.kicad_pcb"
    pcb_path.write_text(_PCB_TEMPLATE)
    return pcb_path


# ---------------------------------------------------------------------------
# Unit-level: ZoneGenerator.from_pcb is called with edge_clearance.
# ---------------------------------------------------------------------------


class TestEdgeClearanceForwardingUnit:
    """Unit-level guards on the ``--power-nets`` zone-creation call."""

    def test_from_pcb_called_with_edge_clearance_when_set(self, tmp_path: Path):
        """``args.edge_clearance`` must be forwarded to ``ZoneGenerator.from_pcb``.

        Mocks ``ZoneGenerator`` so the rest of route_cmd's main is never
        invoked; we only need to confirm the kwarg threading.
        """
        from kicad_tools.cli import route_cmd

        pcb_path = _write_pcb(tmp_path)
        out_path = tmp_path / "out.kicad_pcb"

        # Note: ZoneGenerator is imported lazily inside route_cmd.main(),
        # so we patch the source module instead of route_cmd.
        with patch("kicad_tools.zones.ZoneGenerator") as mock_zone_gen_cls:
            # The route command catches and warns on zone-gen exceptions.
            # We deliberately raise so the rest of main() short-circuits the
            # zone branch but continues; we can still assert the call.
            mock_zone_gen_cls.from_pcb.side_effect = RuntimeError("stop here")

            route_cmd.main(
                [
                    str(pcb_path),
                    "-o",
                    str(out_path),
                    "--strategy",
                    "negotiated",
                    "--layers",
                    "2",
                    "--backend",
                    "python",
                    "--power-nets",
                    "GND:B.Cu",
                    "--edge-clearance",
                    "0.3",
                    "--no-cache",
                    "--skip-drc",
                    "--quiet",
                ]
            )

            assert mock_zone_gen_cls.from_pcb.called, (
                "ZoneGenerator.from_pcb must be invoked when --power-nets is set"
            )
            kwargs = mock_zone_gen_cls.from_pcb.call_args.kwargs
            assert kwargs.get("edge_clearance") == pytest.approx(0.3), (
                "edge_clearance kwarg must equal args.edge_clearance "
                f"(got {kwargs!r})"
            )

    def test_from_pcb_called_with_edge_clearance_from_jlcpcb_profile(
        self, tmp_path: Path
    ):
        """JLCPCB profile auto-injects edge_clearance=0.3 into args.

        Reproduces the original bug: even without ``--edge-clearance``, the
        manufacturer profile sets ``args.edge_clearance`` to its
        ``min_edge_clearance``. The user-explicit ``--power-nets`` path
        must honour that value.
        """
        from kicad_tools.cli import route_cmd

        pcb_path = _write_pcb(tmp_path)
        out_path = tmp_path / "out.kicad_pcb"

        with patch("kicad_tools.zones.ZoneGenerator") as mock_zone_gen_cls:
            mock_zone_gen_cls.from_pcb.side_effect = RuntimeError("stop here")

            route_cmd.main(
                [
                    str(pcb_path),
                    "-o",
                    str(out_path),
                    "--strategy",
                    "negotiated",
                    "--layers",
                    "2",
                    "--backend",
                    "python",
                    "--power-nets",
                    "GND:B.Cu",
                    "--manufacturer",
                    "jlcpcb",
                    "--no-cache",
                    "--skip-drc",
                    "--quiet",
                ]
            )

            assert mock_zone_gen_cls.from_pcb.called
            kwargs = mock_zone_gen_cls.from_pcb.call_args.kwargs
            # JLCPCB profile sets min_edge_clearance to 0.3mm.
            assert kwargs.get("edge_clearance") == pytest.approx(0.3), (
                "edge_clearance from --manufacturer jlcpcb (0.3mm) must "
                f"flow to ZoneGenerator.from_pcb (got {kwargs!r})"
            )


# ---------------------------------------------------------------------------
# Integration: run route_cmd.main and inspect the resulting PCB.
# ---------------------------------------------------------------------------


_POLY_PTS_RE = re.compile(
    r"\(polygon\s*\(pts((?:\s*\(xy\s+-?[\d.]+\s+-?[\d.]+\))+)"
)
_XY_RE = re.compile(r"\(xy\s+(-?[\d.]+)\s+(-?[\d.]+)\)")


def _extract_zone_polygons(text: str) -> list[list[tuple[float, float]]]:
    """Extract polygon pts from PCB text (zone boundaries)."""
    polys: list[list[tuple[float, float]]] = []
    for m in _POLY_PTS_RE.finditer(text):
        pts = [(float(x), float(y)) for x, y in _XY_RE.findall(m.group(1))]
        polys.append(pts)
    return polys


class TestEdgeClearanceIntegration:
    """End-to-end: run route_cmd.main and inspect zone polygon vertices."""

    def test_power_nets_zones_are_inset_with_jlcpcb_profile(
        self, tmp_path: Path
    ):
        """Zone polygons must be inset by ``edge_clearance`` from the outline.

        Board outline is the rectangle (100,100)-(130,125). With the
        JLCPCB profile (``edge_clearance=0.3``), zone boundaries must be
        at least 0.29mm from every edge.
        """
        from kicad_tools.cli import route_cmd

        pcb_path = _write_pcb(tmp_path)
        out_path = tmp_path / "routed.kicad_pcb"

        rc = route_cmd.main(
            [
                str(pcb_path),
                "-o",
                str(out_path),
                "--strategy",
                "negotiated",
                "--layers",
                "2",
                "--backend",
                "python",
                "--power-nets",
                "GND:B.Cu",
                "--manufacturer",
                "jlcpcb",
                "--no-cache",
                "--skip-drc",
                "--quiet",
            ]
        )

        # We don't care whether routing succeeds; the zone is generated
        # before routing completes. The output PCB is written even on
        # partial routing.
        assert rc in (0, 1, 2), f"unexpected exit code {rc}"
        # Find any output file the command produced.
        candidates = [
            out_path,
            tmp_path / "routed_partial.kicad_pcb",
        ]
        produced = next((p for p in candidates if p.exists()), None)
        assert produced is not None, (
            "route command must produce some output PCB (full or partial)"
        )

        text = produced.read_text()
        polys = _extract_zone_polygons(text)
        assert polys, "expected at least one zone polygon in output PCB"

        # Outline is rectangle (100,100)-(130,125); inset by 0.3 = 0.29 minimum.
        edge_clearance = 0.3
        epsilon = 0.01  # tolerance below the configured clearance
        min_inset = edge_clearance - epsilon
        for poly in polys:
            for x, y in poly:
                assert x >= 100.0 + min_inset, (
                    f"zone vertex X={x} too close to left edge x=100 "
                    f"(expected >= {100 + min_inset})"
                )
                assert x <= 130.0 - min_inset, (
                    f"zone vertex X={x} too close to right edge x=130 "
                    f"(expected <= {130 - min_inset})"
                )
                assert y >= 100.0 + min_inset, (
                    f"zone vertex Y={y} too close to top edge y=100 "
                    f"(expected >= {100 + min_inset})"
                )
                assert y <= 125.0 - min_inset, (
                    f"zone vertex Y={y} too close to bottom edge y=125 "
                    f"(expected <= {125 - min_inset})"
                )

    def test_power_nets_zones_at_outline_when_no_edge_clearance(
        self, tmp_path: Path
    ):
        """Without an edge clearance, zones should reach the board edge.

        Uses a manufacturer with no edge-clearance constraint by passing
        ``--edge-clearance 0`` explicitly, which should suppress inset.
        """
        from kicad_tools.cli import route_cmd

        pcb_path = _write_pcb(tmp_path)
        out_path = tmp_path / "routed.kicad_pcb"

        route_cmd.main(
            [
                str(pcb_path),
                "-o",
                str(out_path),
                "--strategy",
                "negotiated",
                "--layers",
                "2",
                "--backend",
                "python",
                "--power-nets",
                "GND:B.Cu",
                "--edge-clearance",
                "0",
                "--no-cache",
                "--skip-drc",
                "--quiet",
            ]
        )

        candidates = [
            out_path,
            tmp_path / "routed_partial.kicad_pcb",
        ]
        produced = next((p for p in candidates if p.exists()), None)
        assert produced is not None

        text = produced.read_text()
        polys = _extract_zone_polygons(text)
        assert polys, "expected at least one zone polygon in output PCB"

        # With edge_clearance=0, vertices should sit on the outline (no inset).
        # Allow a small tolerance for floating-point representation.
        all_xs = [x for poly in polys for x, _ in poly]
        all_ys = [y for poly in polys for _, y in poly]
        assert min(all_xs) == pytest.approx(100.0, abs=0.05)
        assert max(all_xs) == pytest.approx(130.0, abs=0.05)
        assert min(all_ys) == pytest.approx(100.0, abs=0.05)
        assert max(all_ys) == pytest.approx(125.0, abs=0.05)
