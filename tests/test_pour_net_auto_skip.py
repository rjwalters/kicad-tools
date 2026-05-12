"""Tests for automatic pour-net detection and skipping.

Verifies that both the MCP route_net_auto() path and the CLI route command
automatically detect pour nets (GND, GNDA, VSS, etc.) and either pass
net_class_map to the RoutingOrchestrator or extend skip_nets.

Issue #1292: Wire net_class_map into CLI route command for pour-net skipping.
Issue #1807: Only auto-skip power nets that have corresponding zones in PCB.
"""

import contextlib
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Minimal PCB with a GND net (with zone), a signal net, and an outline
POUR_NET_PCB = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0.05)
    (pcbplotparams (layerselection 0x0) (plot_on_all_layers_selection 0x0))
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "SPI_CLK")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 10 10)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "SPI_CLK"))
  )

  (footprint "R_0603"
    (layer "F.Cu")
    (at 30 10)
    (attr smd)
    (property "Reference" "R2")
    (property "Value" "4.7k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "SPI_CLK"))
  )

  (zone (net "GND") (layer "F.Cu") (tstamp "00000000-0000-0000-0000-000000000001"))
)
"""

# Minimal PCB with a GND net but NO zone (power net without zone)
POUR_NET_NO_ZONE_PCB = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0.05)
    (pcbplotparams (layerselection 0x0) (plot_on_all_layers_selection 0x0))
  )
  (net 0 "")
  (net 1 "+5V")
  (net 2 "SPI_CLK")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 10 10)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "+5V"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "SPI_CLK"))
  )
)
"""

# PCB with GND (has zone) and +5V (no zone) -- mixed scenario
MIXED_ZONE_PCB = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0.05)
    (pcbplotparams (layerselection 0x0) (plot_on_all_layers_selection 0x0))
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+5V")
  (net 3 "SPI_CLK")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 10 10)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SPI_CLK"))
  )

  (footprint "R_0603"
    (layer "F.Cu")
    (at 30 10)
    (attr smd)
    (property "Reference" "R2")
    (property "Value" "4.7k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "+5V"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SPI_CLK"))
  )

  (zone (net "GND") (layer "F.Cu") (tstamp "00000000-0000-0000-0000-000000000001"))
)
"""

# ---------------------------------------------------------------------------
# Traditional KiCad 7/8 format fixtures (net N) + (net_name "NAME")
# ---------------------------------------------------------------------------

# Traditional format: GND net with zone using (net 1) (net_name "GND")
POUR_NET_TRADITIONAL_PCB = """\
(kicad_pcb
  (version 20221018)
  (generator "pcbnew")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0.05)
    (pcbplotparams (layerselection 0x0) (plot_on_all_layers_selection 0x0))
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "SPI_CLK")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 10 10)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "SPI_CLK"))
  )

  (zone
    (net 1)
    (net_name "GND")
    (layer "F.Cu")
    (tstamp "00000000-0000-0000-0000-000000000010")
    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts (xy 0 0) (xy 50 0) (xy 50 40) (xy 0 40)))
  )
)
"""

# Traditional format: mixed -- GND has zone, +5V does not
MIXED_ZONE_TRADITIONAL_PCB = """\
(kicad_pcb
  (version 20221018)
  (generator "pcbnew")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0.05)
    (pcbplotparams (layerselection 0x0) (plot_on_all_layers_selection 0x0))
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+5V")
  (net 3 "SPI_CLK")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 10 10)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SPI_CLK"))
  )

  (footprint "R_0603"
    (layer "F.Cu")
    (at 30 10)
    (attr smd)
    (property "Reference" "R2")
    (property "Value" "4.7k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "+5V"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SPI_CLK"))
  )

  (zone
    (net 1)
    (net_name "GND")
    (layer "F.Cu")
    (tstamp "00000000-0000-0000-0000-000000000011")
    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts (xy 0 0) (xy 50 0) (xy 50 40) (xy 0 40)))
  )
)
"""

# Minimal PCB with no ground/power nets at all
NO_GROUND_PCB = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0.05)
    (pcbplotparams (layerselection 0x0) (plot_on_all_layers_selection 0x0))
  )
  (net 0 "")
  (net 1 "SPI_CLK")
  (net 2 "SPI_MOSI")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 10 10)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SPI_CLK"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "SPI_MOSI"))
  )
)
"""

# PCB with multiple pour nets (both with zones)
MULTI_POUR_PCB = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0.05)
    (pcbplotparams (layerselection 0x0) (plot_on_all_layers_selection 0x0))
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "GNDA")
  (net 3 "SPI_CLK")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 10 10)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SPI_CLK"))
  )

  (footprint "R_0603"
    (layer "F.Cu")
    (at 30 10)
    (attr smd)
    (property "Reference" "R2")
    (property "Value" "4.7k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GNDA"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SPI_CLK"))
  )

  (zone (net "GND") (layer "F.Cu") (tstamp "00000000-0000-0000-0000-000000000002"))
  (zone (net "GNDA") (layer "B.Cu") (tstamp "00000000-0000-0000-0000-000000000003"))
)
"""


# ---------------------------------------------------------------------------
# Helper to write PCB to temp file
# ---------------------------------------------------------------------------


@pytest.fixture
def pcb_with_gnd(tmp_path: Path) -> Path:
    """Write a PCB file with a GND pour net."""
    p = tmp_path / "board_gnd.kicad_pcb"
    p.write_text(POUR_NET_PCB)
    return p


@pytest.fixture
def pcb_no_ground(tmp_path: Path) -> Path:
    """Write a PCB file with no ground nets."""
    p = tmp_path / "board_sig.kicad_pcb"
    p.write_text(NO_GROUND_PCB)
    return p


@pytest.fixture
def pcb_multi_pour(tmp_path: Path) -> Path:
    """Write a PCB file with multiple pour nets."""
    p = tmp_path / "board_multi.kicad_pcb"
    p.write_text(MULTI_POUR_PCB)
    return p


@pytest.fixture
def pcb_pour_no_zone(tmp_path: Path) -> Path:
    """Write a PCB file with a power net (+5V) but no zone for it."""
    p = tmp_path / "board_no_zone.kicad_pcb"
    p.write_text(POUR_NET_NO_ZONE_PCB)
    return p


@pytest.fixture
def pcb_mixed_zone(tmp_path: Path) -> Path:
    """Write a PCB with GND (has zone) and +5V (no zone)."""
    p = tmp_path / "board_mixed.kicad_pcb"
    p.write_text(MIXED_ZONE_PCB)
    return p


@pytest.fixture
def pcb_traditional_gnd(tmp_path: Path) -> Path:
    """Write a PCB using traditional KiCad 7/8 format with GND zone."""
    p = tmp_path / "board_traditional_gnd.kicad_pcb"
    p.write_text(POUR_NET_TRADITIONAL_PCB)
    return p


@pytest.fixture
def pcb_traditional_mixed(tmp_path: Path) -> Path:
    """Write a PCB using traditional KiCad 7/8 format with mixed zones."""
    p = tmp_path / "board_traditional_mixed.kicad_pcb"
    p.write_text(MIXED_ZONE_TRADITIONAL_PCB)
    return p


# ===========================================================================
# Call site 1: route_net_auto() in routing.py (MCP path)
# ===========================================================================


class TestRouteNetAutoNetClassMap:
    """Verify route_net_auto() passes net_class_map to the orchestrator."""

    def test_gnd_net_skipped_via_net_class_map(self, pcb_with_gnd: Path) -> None:
        """GND net is detected as pour net and orchestrator returns success
        with a zone-fill warning instead of routing traces."""
        from kicad_tools.mcp.tools.routing import route_net_auto

        result = route_net_auto(
            pcb_path=str(pcb_with_gnd),
            net_name="GND",
        )

        assert isinstance(result, dict)
        assert result["success"] is True
        # The orchestrator should produce a zone-fill warning
        warnings = result.get("warnings", [])
        assert any("zone fill" in w.lower() for w in warnings), (
            f"Expected zone fill warning in {warnings}"
        )

    def test_signal_net_still_routed(self, pcb_with_gnd: Path) -> None:
        """SPI_CLK (non-pour net) should still be routed normally."""
        from kicad_tools.mcp.tools.routing import route_net_auto

        result = route_net_auto(
            pcb_path=str(pcb_with_gnd),
            net_name="SPI_CLK",
        )

        assert isinstance(result, dict)
        assert "success" in result
        # Signal nets should not get the zone-fill warning
        warnings = result.get("warnings", [])
        pour_warnings = [w for w in warnings if "zone fill" in w.lower()]
        assert len(pour_warnings) == 0

    def test_classification_failure_falls_back(self, pcb_with_gnd: Path) -> None:
        """If classify_and_apply_rules() raises, routing proceeds without
        net_class_map (no crash)."""
        from kicad_tools.mcp.tools.routing import route_net_auto

        with patch(
            "kicad_tools.router.net_class.classify_and_apply_rules",
            side_effect=RuntimeError("boom"),
        ):
            result = route_net_auto(
                pcb_path=str(pcb_with_gnd),
                net_name="GND",
            )

        assert isinstance(result, dict)
        assert "success" in result
        # Without net_class_map, GND is routed like a normal net
        # (no zone fill warning expected)


# ===========================================================================
# Call site 2: main() in route_cmd.py (CLI path)
# ===========================================================================


class TestCliAutoSkipPourNets:
    """Verify the CLI route command auto-populates skip_nets for pour nets.

    We mock load_pcb_for_routing to avoid heavy routing.  The auto-skip
    logic runs *before* load_pcb_for_routing is called, so the output we
    care about is already printed regardless of downstream mock behaviour.
    We use --quiet to suppress most output and --grid 0.1 to avoid the
    grid-vs-clearance validation gate.  Since downstream code may still
    fail when formatting mock attributes, we tolerate exceptions and
    only inspect captured stdout.
    """

    @staticmethod
    def _run_main_safe(argv: list[str], capsys) -> str:
        """Run main() capturing stdout, tolerating downstream failures."""
        from kicad_tools.cli.route_cmd import main

        with contextlib.suppress(SystemExit, Exception):
            main(argv)
        return capsys.readouterr().out

    def test_gnd_auto_added_to_skip_nets(self, pcb_with_gnd: Path, capsys) -> None:
        """GND is automatically added to skip_nets when no --skip-nets given."""
        with patch("kicad_tools.router.load_pcb_for_routing") as mock_load:
            mock_load.side_effect = SystemExit(0)  # stop immediately after call
            out = self._run_main_safe([str(pcb_with_gnd), "--grid", "0.1"], capsys)

        assert "Auto-skip:" in out
        assert "GND" in out
        assert "pour nets" in out

    def test_no_auto_skip_when_no_pour_nets(self, pcb_no_ground: Path, capsys) -> None:
        """Board with no pour nets prints no auto-skip message."""
        with patch("kicad_tools.router.load_pcb_for_routing") as mock_load:
            mock_load.side_effect = SystemExit(0)
            out = self._run_main_safe([str(pcb_no_ground), "--grid", "0.1"], capsys)

        assert "Auto-skip:" not in out

    def test_explicit_skip_nets_not_duplicated(self, pcb_with_gnd: Path, capsys) -> None:
        """If user supplies --skip-nets GND, GND is not listed twice."""
        with patch("kicad_tools.router.load_pcb_for_routing") as mock_load:
            mock_load.side_effect = SystemExit(0)
            out = self._run_main_safe(
                [str(pcb_with_gnd), "--skip-nets", "GND", "--grid", "0.1"],
                capsys,
            )

        # GND was already user-supplied, so auto-skip should not add it again
        assert "Auto-skip:" not in out

        # Verify skip_nets was passed correctly to load_pcb_for_routing
        assert mock_load.called, "load_pcb_for_routing was never called"
        call_kwargs = mock_load.call_args[1]
        skip_arg = call_kwargs.get("skip_nets", [])
        assert skip_arg.count("GND") == 1

    def test_multi_pour_nets_detected(self, pcb_multi_pour: Path, capsys) -> None:
        """Multiple pour nets (GND, GNDA) are both auto-detected."""
        with patch("kicad_tools.router.load_pcb_for_routing") as mock_load:
            mock_load.side_effect = SystemExit(0)
            out = self._run_main_safe([str(pcb_multi_pour), "--grid", "0.1"], capsys)

        assert "Auto-skip:" in out
        assert "GND" in out
        assert "GNDA" in out

    def test_classification_failure_no_crash(self, pcb_with_gnd: Path, capsys) -> None:
        """If classify_and_apply_rules raises, routing proceeds normally."""
        with (
            patch(
                "kicad_tools.router.net_class.classify_and_apply_rules",
                side_effect=RuntimeError("classification error"),
            ),
            patch("kicad_tools.router.load_pcb_for_routing") as mock_load,
        ):
            mock_load.side_effect = SystemExit(0)
            out = self._run_main_safe([str(pcb_with_gnd), "--grid", "0.1"], capsys)

        # No auto-skip since classification failed
        assert "Auto-skip:" not in out


# ===========================================================================
# Unit tests for classify_and_apply_rules pour-net detection
# ===========================================================================


class TestClassifyPourNets:
    """Verify the net classification correctly identifies pour nets."""

    def test_gnd_is_pour_net(self) -> None:
        """GND should be classified as a pour net."""
        from kicad_tools.router.net_class import classify_and_apply_rules

        net_names = {1: "GND", 2: "SPI_CLK"}
        result = classify_and_apply_rules(net_names)

        assert "GND" in result
        assert result["GND"].is_pour_net is True

    def test_signal_net_is_not_pour(self) -> None:
        """SPI_CLK should not be classified as a pour net."""
        from kicad_tools.router.net_class import classify_and_apply_rules

        net_names = {1: "GND", 2: "SPI_CLK"}
        result = classify_and_apply_rules(net_names)

        assert "SPI_CLK" in result
        assert result["SPI_CLK"].is_pour_net is False

    def test_gnda_is_pour_net(self) -> None:
        """GNDA (analog ground) should be classified as a pour net."""
        from kicad_tools.router.net_class import classify_and_apply_rules

        net_names = {1: "GNDA"}
        result = classify_and_apply_rules(net_names)

        assert "GNDA" in result
        assert result["GNDA"].is_pour_net is True

    def test_vss_is_pour_net(self) -> None:
        """VSS should be classified as a pour net."""
        from kicad_tools.router.net_class import classify_and_apply_rules

        net_names = {1: "VSS"}
        result = classify_and_apply_rules(net_names)

        assert "VSS" in result
        assert result["VSS"].is_pour_net is True

    def test_empty_net_names(self) -> None:
        """Empty net_names dict should return empty result."""
        from kicad_tools.router.net_class import classify_and_apply_rules

        result = classify_and_apply_rules({})
        assert result == {}

    def test_power_net_3v3_is_pour_net(self) -> None:
        """+3.3V should be classified as a pour net (power class)."""
        from kicad_tools.router.net_class import classify_and_apply_rules

        net_names = {1: "+3.3V", 2: "SPI_CLK"}
        result = classify_and_apply_rules(net_names)

        assert "+3.3V" in result
        assert result["+3.3V"].is_pour_net is True

    def test_suffix_gnd_is_pour_net(self) -> None:
        """Nets ending in _GND (e.g. SCAP_POS_GND) should be classified as pour nets."""
        from kicad_tools.router.net_class import classify_and_apply_rules

        net_names = {1: "SCAP_POS_GND", 2: "SCAP_NEG_GND", 3: "SPI_CLK"}
        result = classify_and_apply_rules(net_names)

        assert "SCAP_POS_GND" in result
        assert result["SCAP_POS_GND"].is_pour_net is True
        assert "SCAP_NEG_GND" in result
        assert result["SCAP_NEG_GND"].is_pour_net is True


# ===========================================================================
# Tests for _auto_skip_pour_nets helper function
# ===========================================================================


class TestAutoSkipPourNetsHelper:
    """Verify the _auto_skip_pour_nets helper function works correctly."""

    def test_auto_skip_detects_gnd(self, pcb_with_gnd: Path) -> None:
        """GND should be detected and added to skip_nets."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        auto_skipped, _no_zone = _auto_skip_pour_nets(pcb_with_gnd, skip_nets, quiet=True)

        assert "GND" in skip_nets
        assert "GND" in auto_skipped

    def test_auto_skip_does_not_duplicate(self, pcb_with_gnd: Path) -> None:
        """If GND is already in skip_nets, it should not be added again."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets = ["GND"]
        _auto_skip_pour_nets(pcb_with_gnd, skip_nets, quiet=True)

        assert skip_nets.count("GND") == 1

    def test_auto_skip_returns_empty_for_signal_only(self, pcb_no_ground: Path) -> None:
        """Board with no pour nets should return empty auto-skip list."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        auto_skipped, _no_zone = _auto_skip_pour_nets(pcb_no_ground, skip_nets, quiet=True)

        assert auto_skipped == []
        assert skip_nets == []

    def test_auto_skip_detects_multiple_pour_nets(self, pcb_multi_pour: Path) -> None:
        """Multiple pour nets (GND, GNDA) should both be detected."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        auto_skipped, _no_zone = _auto_skip_pour_nets(pcb_multi_pour, skip_nets, quiet=True)

        assert "GND" in skip_nets
        assert "GNDA" in skip_nets
        assert len(auto_skipped) == 2


# ===========================================================================
# Tests for zone-awareness in _auto_skip_pour_nets (Issue #1807)
# ===========================================================================


class TestAutoSkipZoneAwareness:
    """Verify _auto_skip_pour_nets only skips power nets that have zones."""

    def test_power_net_without_zone_is_not_skipped(self, pcb_pour_no_zone: Path) -> None:
        """+5V (power net) without a zone should NOT be added to skip_nets."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        auto_skipped, _no_zone = _auto_skip_pour_nets(pcb_pour_no_zone, skip_nets, quiet=True)

        assert "+5V" not in skip_nets
        assert auto_skipped == []

    def test_mixed_zone_gnd_skipped_5v_routed(self, pcb_mixed_zone: Path) -> None:
        """GND (has zone) is skipped; +5V (no zone) is routed."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        auto_skipped, _no_zone = _auto_skip_pour_nets(pcb_mixed_zone, skip_nets, quiet=True)

        assert "GND" in skip_nets
        assert "GND" in auto_skipped
        assert "+5V" not in skip_nets
        assert "+5V" not in auto_skipped

    def test_power_net_with_zone_still_skipped(self, pcb_with_gnd: Path) -> None:
        """GND with a zone in the PCB should still be auto-skipped."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        auto_skipped, _no_zone = _auto_skip_pour_nets(pcb_with_gnd, skip_nets, quiet=True)

        assert "GND" in skip_nets
        assert "GND" in auto_skipped

    def test_info_message_for_zoneless_power_nets(self, pcb_mixed_zone: Path, capsys) -> None:
        """With quiet=False, an info message lists power nets being routed."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        _auto_skip_pour_nets(pcb_mixed_zone, skip_nets, quiet=False)

        out = capsys.readouterr().out
        assert "Routing:" in out
        assert "+5V" in out
        assert "power nets without zones" in out

    def test_no_info_message_when_quiet(self, pcb_mixed_zone: Path, capsys) -> None:
        """With quiet=True, no informational message is printed."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        _auto_skip_pour_nets(pcb_mixed_zone, skip_nets, quiet=True)

        out = capsys.readouterr().out
        assert "Routing:" not in out

    def test_no_info_message_when_all_have_zones(self, pcb_with_gnd: Path, capsys) -> None:
        """When all power nets have zones, no 'Routing:' message is printed."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        _auto_skip_pour_nets(pcb_with_gnd, skip_nets, quiet=False)

        out = capsys.readouterr().out
        assert "Routing:" not in out


# ===========================================================================
# Tests for traditional KiCad 7/8 format zone detection (PR #1813 feedback)
# ===========================================================================


class TestAutoSkipZoneAwarenessTraditionalFormat:
    """Verify _auto_skip_pour_nets detects zones in traditional KiCad 7/8 format.

    Traditional format uses ``(net N)`` with a numeric ID and puts the
    human-readable name in a separate ``(net_name "...")`` node inside
    the zone definition, unlike KiCad 9 which uses ``(net "NAME")``.
    """

    def test_traditional_gnd_zone_detected(self, pcb_traditional_gnd: Path) -> None:
        """GND zone in traditional (net 1)(net_name "GND") format is detected."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        auto_skipped, _no_zone = _auto_skip_pour_nets(pcb_traditional_gnd, skip_nets, quiet=True)

        assert "GND" in skip_nets
        assert "GND" in auto_skipped

    def test_traditional_mixed_gnd_skipped_5v_routed(self, pcb_traditional_mixed: Path) -> None:
        """Traditional format: GND (has zone) is skipped; +5V (no zone) is routed."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        auto_skipped, _no_zone = _auto_skip_pour_nets(pcb_traditional_mixed, skip_nets, quiet=True)

        assert "GND" in skip_nets
        assert "GND" in auto_skipped
        assert "+5V" not in skip_nets
        assert "+5V" not in auto_skipped

    def test_traditional_info_message_for_zoneless_power_nets(
        self, pcb_traditional_mixed: Path, capsys
    ) -> None:
        """Traditional format: info message lists +5V as power net being routed."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        _auto_skip_pour_nets(pcb_traditional_mixed, skip_nets, quiet=False)

        out = capsys.readouterr().out
        assert "Routing:" in out
        assert "+5V" in out
        assert "power nets without zones" in out


# ===========================================================================
# Tests for _is_pour_net / _filter_pour_nets with pour_nets_without_zones
# (Issue #1841)
# ===========================================================================


class TestFilterPourNetsWithoutZones:
    """Verify _filter_pour_nets respects _pour_nets_without_zones.

    Issue #1841: Pour nets without zones (e.g. GNDA on chorus-test-revA)
    were incorrectly re-filtered by _filter_pour_nets inside the autorouter
    even though _auto_skip_pour_nets had already classified them as
    'route as signal'.
    """

    def _make_autorouter(self):
        """Create a minimal Autorouter with GNDA and GND nets."""
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.net_class import classify_and_apply_rules

        router = Autorouter(width=50, height=40)
        net_names = {1: "GND", 2: "GNDA", 3: "SPI_CLK"}
        net_class_map = classify_and_apply_rules(net_names)
        router.net_class_map = net_class_map
        router.net_names = net_names
        router.nets = {1: [], 2: [], 3: []}
        return router

    def test_is_pour_net_true_by_default(self) -> None:
        """GNDA is a pour net by default (has is_pour_net=True in net class)."""
        router = self._make_autorouter()
        assert router._is_pour_net(2) is True  # GNDA

    def test_is_pour_net_false_when_in_without_zones(self) -> None:
        """GNDA returns False from _is_pour_net when marked as no-zone."""
        router = self._make_autorouter()
        router._pour_nets_without_zones = {"GNDA"}
        assert router._is_pour_net(2) is False  # GNDA
        # GND (not in the set) remains a pour net
        assert router._is_pour_net(1) is True  # GND

    def test_filter_pour_nets_keeps_no_zone_nets(self) -> None:
        """_filter_pour_nets should NOT remove GNDA when it has no zone."""
        router = self._make_autorouter()
        router._pour_nets_without_zones = {"GNDA"}

        net_order = [1, 2, 3]  # GND, GNDA, SPI_CLK
        filtered = router._filter_pour_nets(net_order)

        # GND (has is_pour_net, NOT in _pour_nets_without_zones) -> filtered out
        assert 1 not in filtered
        # GNDA (has is_pour_net, IS in _pour_nets_without_zones) -> kept
        assert 2 in filtered
        # SPI_CLK (not a pour net) -> kept
        assert 3 in filtered

    def test_filter_pour_nets_removes_all_when_no_override(self) -> None:
        """Without _pour_nets_without_zones set, all pour nets are filtered."""
        router = self._make_autorouter()
        # _pour_nets_without_zones defaults to empty set

        net_order = [1, 2, 3]  # GND, GNDA, SPI_CLK
        filtered = router._filter_pour_nets(net_order)

        assert 1 not in filtered  # GND filtered
        assert 2 not in filtered  # GNDA filtered
        assert 3 in filtered  # SPI_CLK kept

    def test_auto_skip_returns_no_zone_list(self, pcb_mixed_zone: Path) -> None:
        """_auto_skip_pour_nets returns (skipped, no_zone) where no_zone
        contains power nets without zones."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        auto_skipped, no_zone = _auto_skip_pour_nets(pcb_mixed_zone, skip_nets, quiet=True)

        assert "GND" in auto_skipped
        assert "+5V" in no_zone
        assert "+5V" not in auto_skipped


# ===========================================================================
# Tests for round-trip serialization of _pour_nets_without_zones (Issue #2454)
# ===========================================================================


class TestPourNetsWithoutZonesSerialization:
    """Verify _pour_nets_without_zones survives serialization for parallel workers.

    Issue #2454: _serialize_for_parallel() did not include
    _pour_nets_without_zones, so worker processes created via
    _run_monte_carlo_trial / _run_evolutionary_trial would see an empty
    set, causing _is_pour_net() to incorrectly return True for nets that
    should be routed as signals.
    """

    def _make_autorouter(self, pour_nets_without_zones=None):
        """Create a minimal Autorouter with GND and GNDA nets."""
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.net_class import classify_and_apply_rules

        router = Autorouter(width=50, height=40)
        net_names = {1: "GND", 2: "GNDA", 3: "SPI_CLK"}
        net_class_map = classify_and_apply_rules(net_names)
        router.net_class_map = net_class_map
        router.net_names = net_names
        router.nets = {1: [], 2: [], 3: []}
        if pour_nets_without_zones is not None:
            router._pour_nets_without_zones = pour_nets_without_zones
        return router

    def test_serialize_includes_pour_nets_without_zones(self) -> None:
        """_serialize_for_parallel() includes pour_nets_without_zones key."""
        router = self._make_autorouter(pour_nets_without_zones={"GND", "GNDA"})
        config = router._serialize_for_parallel()

        assert "pour_nets_without_zones" in config
        assert set(config["pour_nets_without_zones"]) == {"GND", "GNDA"}

    def test_serialize_empty_pour_nets_without_zones(self) -> None:
        """Empty _pour_nets_without_zones serializes as empty list."""
        router = self._make_autorouter()
        config = router._serialize_for_parallel()

        assert "pour_nets_without_zones" in config
        assert config["pour_nets_without_zones"] == []

    def test_roundtrip_pour_nets_without_zones(self) -> None:
        """Reconstructed router preserves _pour_nets_without_zones."""
        from kicad_tools.router.core import Autorouter

        router = self._make_autorouter(pour_nets_without_zones={"GND"})
        config = router._serialize_for_parallel()

        # Simulate worker reconstruction
        reconstructed = Autorouter(
            width=config["width"],
            height=config["height"],
            origin_x=config["origin_x"],
            origin_y=config["origin_y"],
        )
        reconstructed._pour_nets_without_zones = set(config.get("pour_nets_without_zones", []))
        reconstructed.net_names = {int(k): v for k, v in config["net_names"].items()}
        reconstructed.net_class_map = config.get("net_class_map")

        assert reconstructed._pour_nets_without_zones == {"GND"}

    def test_is_pour_net_correct_after_roundtrip(self) -> None:
        """_is_pour_net() returns correct results on a reconstructed router."""
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.net_class import classify_and_apply_rules

        router = self._make_autorouter(pour_nets_without_zones={"GNDA"})
        config = router._serialize_for_parallel()

        # Simulate worker reconstruction
        reconstructed = Autorouter(
            width=config["width"],
            height=config["height"],
            origin_x=config["origin_x"],
            origin_y=config["origin_y"],
        )
        reconstructed.net_names = {int(k): v for k, v in config["net_names"].items()}
        net_class_map = classify_and_apply_rules(reconstructed.net_names)
        reconstructed.net_class_map = net_class_map
        reconstructed.nets = {1: [], 2: [], 3: []}
        reconstructed._pour_nets_without_zones = set(config.get("pour_nets_without_zones", []))

        # GNDA is in _pour_nets_without_zones -> should NOT be treated as pour
        assert reconstructed._is_pour_net(2) is False
        # GND is NOT in _pour_nets_without_zones -> should still be pour
        assert reconstructed._is_pour_net(1) is True
        # SPI_CLK is a signal net -> not pour
        assert reconstructed._is_pour_net(3) is False

    def test_backward_compat_missing_key(self) -> None:
        """Config without pour_nets_without_zones key defaults to empty set."""
        config = {"pour_nets_without_zones": None}  # simulate missing
        result = set(config.get("pour_nets_without_zones") or [])
        assert result == set()

        # Also test truly missing key
        config2 = {}
        result2 = set(config2.get("pour_nets_without_zones", []))
        assert result2 == set()


# ===========================================================================
# Tests for ERC-marker net exclusion (#2592)
# ===========================================================================

# Minimal PCB with GND, +3.3V, PWR_FLAG and a signal -- mimics what KiCad's
# netlister produces when a schematic uses a PWR_FLAG to silence ERC.
PWR_FLAG_PCB = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0.05))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (net 3 "PWR_FLAG")
  (net 4 "SIG1")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu") (at 10 10) (attr smd)
    (property "Reference" "R1") (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "+3.3V"))
  )
  (footprint "R_0603"
    (layer "F.Cu") (at 30 10) (attr smd)
    (property "Reference" "R2") (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "PWR_FLAG"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 4 "SIG1"))
  )

  (zone (net 1) (net_name "GND") (layer "B.Cu") (hatch edge 0.5)
    (connect_pads (clearance 0.25))
    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts (xy 0 0) (xy 50 0) (xy 50 40) (xy 0 40))))
)
"""


class TestAutoSkipExcludesErcMarkers:
    """Verify ``_auto_skip_pour_nets`` does not list PWR_FLAG (#2592).

    ``classify_from_name`` matches ``PWR_FLAG`` against ``^PWR`` and so
    reports it as a pour-classified power net.  But because PWR_FLAG is
    purely an ERC annotation it will never have an associated zone, and
    listing it under ``Auto-skip: ... (pour nets — use zone fill)`` or
    ``Routing: ... (power nets without zones)`` would be misleading.
    """

    @pytest.fixture
    def pcb_with_pwr_flag(self, tmp_path: Path) -> Path:
        p = tmp_path / "board_pwr_flag.kicad_pcb"
        p.write_text(PWR_FLAG_PCB)
        return p

    def test_pwr_flag_not_in_auto_skip(self, pcb_with_pwr_flag: Path) -> None:
        """PWR_FLAG must not be added to ``skip_nets`` even if classified POWER."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        auto_skipped, no_zone = _auto_skip_pour_nets(pcb_with_pwr_flag, skip_nets, quiet=True)

        # GND has a zone -> auto-skipped.  +3.3V is a pour net without
        # zone -> reported in no_zone.  PWR_FLAG must appear in NEITHER.
        assert "GND" in auto_skipped
        assert "PWR_FLAG" not in auto_skipped
        assert "PWR_FLAG" not in skip_nets
        assert "PWR_FLAG" not in no_zone

    def test_pwr_flag_not_in_auto_skip_log(self, pcb_with_pwr_flag: Path, capsys) -> None:
        """The auto-skip / routing log lines must not mention PWR_FLAG."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        _auto_skip_pour_nets(pcb_with_pwr_flag, skip_nets, quiet=False)

        out = capsys.readouterr().out
        assert "PWR_FLAG" not in out


# ===========================================================================
# Tests for declarative routing-intent opt-out via route_via (Issue #2772)
# ===========================================================================


# Minimal PCB with PHASE_A/B/C (HIGH_CURRENT_SIGNAL) plus a SIG_DATA signal,
# no zones for the phase nets.  Used to verify that a synthetic
# ``route_via="manual"`` classification causes the phases to be auto-skipped
# without a zone, while ``route_via="pathfinder"`` does NOT.
ROUTE_VIA_PHASE_PCB = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0.05)
    (pcbplotparams (layerselection 0x0) (plot_on_all_layers_selection 0x0))
  )
  (net 0 "")
  (net 1 "PHASE_A")
  (net 2 "PHASE_B")
  (net 3 "PHASE_C")
  (net 4 "SIG_DATA")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 10 10)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "PHASE_A"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 4 "SIG_DATA"))
  )

  (footprint "R_0603"
    (layer "F.Cu")
    (at 20 10)
    (attr smd)
    (property "Reference" "R2")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "PHASE_B"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 4 "SIG_DATA"))
  )

  (footprint "R_0603"
    (layer "F.Cu")
    (at 30 10)
    (attr smd)
    (property "Reference" "R3")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "PHASE_C"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 4 "SIG_DATA"))
  )
)
"""


@pytest.fixture
def pcb_phase_nets(tmp_path: Path) -> Path:
    """Write a PCB with PHASE_A/B/C and a SIG_DATA net (no zones)."""
    p = tmp_path / "board_phase.kicad_pcb"
    p.write_text(ROUTE_VIA_PHASE_PCB)
    return p


class TestRouteViaField:
    """Verify the new ``route_via`` field on :class:`NetClassRouting`."""

    def test_route_via_defaults_to_pathfinder(self) -> None:
        """New classes default to ``route_via="pathfinder"`` (no behaviour change)."""
        from kicad_tools.router.rules import NetClassRouting

        nc = NetClassRouting(name="Custom")
        assert nc.route_via == "pathfinder"

    def test_route_via_pour_serializes(self) -> None:
        """``route_via="pour"`` round-trips through ``to_dict``/``from_dict``."""
        from kicad_tools.router.rules import NetClassRouting

        original = NetClassRouting(name="PowerLike", route_via="pour")
        roundtripped = NetClassRouting.from_dict(original.to_dict())
        assert roundtripped.route_via == "pour"

    def test_route_via_manual_serializes(self) -> None:
        """``route_via="manual"`` round-trips through ``to_dict``/``from_dict``."""
        from kicad_tools.router.rules import NetClassRouting

        original = NetClassRouting(name="ManualPhase", route_via="manual")
        roundtripped = NetClassRouting.from_dict(original.to_dict())
        assert roundtripped.route_via == "manual"

    def test_from_dict_missing_route_via_defaults_to_pathfinder(self) -> None:
        """Legacy dicts without ``route_via`` deserialize to the default."""
        from kicad_tools.router.rules import NetClassRouting

        legacy_dict = {"name": "Legacy"}  # No route_via key
        nc = NetClassRouting.from_dict(legacy_dict)
        assert nc.route_via == "pathfinder"

    def test_net_class_power_routes_via_pour(self) -> None:
        """``NET_CLASS_POWER`` declares the new ``route_via="pour"`` intent."""
        from kicad_tools.router.rules import NET_CLASS_POWER

        assert NET_CLASS_POWER.route_via == "pour"
        # And keeps the legacy is_pour_net for backwards compatibility.
        assert NET_CLASS_POWER.is_pour_net is True

    def test_net_class_high_current_signal_stays_pathfinder(self) -> None:
        """``NET_CLASS_HIGH_CURRENT_SIGNAL`` is the regression baseline:
        PHASE_A/B/C must still be pathfinder-routed (NOT auto-skipped) so the
        pathfinder produces point-to-point wide traces rather than coupling
        switching noise via a pour.  See ``rules.py:902-921`` comment block.
        """
        from kicad_tools.router.rules import NET_CLASS_HIGH_CURRENT_SIGNAL

        assert NET_CLASS_HIGH_CURRENT_SIGNAL.route_via == "pathfinder"
        assert NET_CLASS_HIGH_CURRENT_SIGNAL.is_pour_net is False


class TestAutoSkipRouteViaManual:
    """Verify ``_auto_skip_pour_nets`` honours ``route_via="manual"``.

    Issue #2772: designers can declaratively opt OUT of the pathfinder
    by setting ``route_via="manual"`` on the relevant net class.  The
    nets are then unconditionally added to ``skip_nets`` (regardless of
    zone presence) and a distinct ``Manual:`` log line is emitted so the
    user is not confused into thinking the net was dropped.
    """

    def _patch_classifier_with(self, mapping):
        """Helper: return a patch context for classify_and_apply_rules.

        ``_auto_skip_pour_nets`` calls
        ``kicad_tools.router.net_class.classify_and_apply_rules`` but the
        symbol is imported locally inside the function, so we must patch
        the source module, not the route_cmd namespace.
        """
        return patch(
            "kicad_tools.router.net_class.classify_and_apply_rules",
            return_value=mapping,
        )

    def test_manual_net_is_auto_skipped_even_without_zone(self, pcb_phase_nets: Path) -> None:
        """PHASE_A declared ``route_via="manual"`` is skipped (no zone needed)."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets
        from kicad_tools.router.rules import NetClassRouting

        manual = NetClassRouting(name="ManualPhase", route_via="manual")
        signal = NetClassRouting(name="Signal", route_via="pathfinder")
        mapping = {
            "PHASE_A": manual,
            "PHASE_B": manual,
            "PHASE_C": manual,
            "SIG_DATA": signal,
        }

        skip_nets: list[str] = []
        with self._patch_classifier_with(mapping):
            auto_skipped, no_zone = _auto_skip_pour_nets(pcb_phase_nets, skip_nets, quiet=True)

        # PHASE_A/B/C are manual -> auto-skipped regardless of zone.
        assert "PHASE_A" in auto_skipped
        assert "PHASE_B" in auto_skipped
        assert "PHASE_C" in auto_skipped
        # SIG_DATA is pathfinder -> NOT skipped.
        assert "SIG_DATA" not in auto_skipped
        assert "SIG_DATA" not in skip_nets
        # Manual nets do not appear in ``no_zone`` -- the designer has
        # declared they are handled by hand, not by a missing zone.
        assert "PHASE_A" not in no_zone
        assert "PHASE_B" not in no_zone
        assert "PHASE_C" not in no_zone

    def test_manual_emits_distinct_log_line(self, pcb_phase_nets: Path, capsys) -> None:
        """The ``Manual:`` log line is emitted (distinct from ``Auto-skip:``)."""
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets
        from kicad_tools.router.rules import NetClassRouting

        manual = NetClassRouting(name="ManualPhase", route_via="manual")
        signal = NetClassRouting(name="Signal", route_via="pathfinder")
        mapping = {
            "PHASE_A": manual,
            "PHASE_B": manual,
            "PHASE_C": manual,
            "SIG_DATA": signal,
        }

        skip_nets: list[str] = []
        with self._patch_classifier_with(mapping):
            _auto_skip_pour_nets(pcb_phase_nets, skip_nets, quiet=False)

        out = capsys.readouterr().out
        assert "Manual:" in out
        assert "PHASE_A" in out
        assert "route_via=manual" in out

    def test_pathfinder_override_beats_is_pour_net(self, pcb_phase_nets: Path) -> None:
        """Explicit ``route_via="pathfinder"`` wins over legacy ``is_pour_net=True``.

        Acceptance criterion: a net with ``route_via="pathfinder"`` is NOT
        skipped even when ``is_pour_net=True`` -- the explicit declarative
        opt-IN overrides the legacy name-pattern inference.
        """
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets
        from kicad_tools.router.rules import NetClassRouting

        # Net class with the legacy pour flag set, but explicit pathfinder
        # routing intent -- the new field must take precedence.
        override = NetClassRouting(
            name="PourOverridePathfinder",
            is_pour_net=True,
            route_via="pathfinder",
        )
        mapping = {
            "PHASE_A": override,
            "PHASE_B": override,
            "PHASE_C": override,
            "SIG_DATA": NetClassRouting(name="Signal", route_via="pathfinder"),
        }

        skip_nets: list[str] = []
        with self._patch_classifier_with(mapping):
            auto_skipped, no_zone = _auto_skip_pour_nets(pcb_phase_nets, skip_nets, quiet=True)

        # Despite is_pour_net=True, route_via="pathfinder" wins.
        assert "PHASE_A" not in auto_skipped
        assert "PHASE_B" not in auto_skipped
        assert "PHASE_C" not in auto_skipped
        assert "PHASE_A" not in skip_nets

    def test_phase_nets_default_classifier_stay_pathfinder(self, pcb_phase_nets: Path) -> None:
        """Regression baseline: under the default classifier, PHASE_A/B/C
        are HIGH_CURRENT_SIGNAL with ``route_via="pathfinder"`` and must
        NOT be auto-skipped.  Without this guard, board 05 (BLDC motor
        controller) would regress -- the comment block at
        ``router/rules.py:902-921`` explicitly forbids pouring phase
        outputs because it couples switching noise into nearby traces.
        """
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        # NO mock -- use the real classifier.
        auto_skipped, no_zone = _auto_skip_pour_nets(pcb_phase_nets, skip_nets, quiet=True)

        # PHASE_A/B/C are HIGH_CURRENT_SIGNAL; default route_via is
        # "pathfinder"; must NOT be skipped.
        assert "PHASE_A" not in auto_skipped
        assert "PHASE_B" not in auto_skipped
        assert "PHASE_C" not in auto_skipped
        assert "PHASE_A" not in skip_nets
        # They also should NOT show up in the no_zone (pour-without-zone)
        # warning because their class does not declare is_pour_net.
        assert "PHASE_A" not in no_zone

    def test_pour_net_with_zone_still_skipped(self, pcb_with_gnd: Path) -> None:
        """No-regression: predefined ground class (route_via="pour") still
        auto-skips GND when a zone exists.  This verifies the
        ``NetClass.GROUND`` migration in ``net_class.py`` is wired up.
        """
        from kicad_tools.cli.route_cmd import _auto_skip_pour_nets

        skip_nets: list[str] = []
        auto_skipped, _no_zone = _auto_skip_pour_nets(pcb_with_gnd, skip_nets, quiet=True)

        assert "GND" in auto_skipped
        assert "GND" in skip_nets
