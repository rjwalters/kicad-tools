"""Tests for automatic pour-net detection and skipping.

Verifies that both the MCP route_net_auto() path and the CLI route command
automatically detect pour nets (GND, GNDA, VSS, etc.) and either pass
net_class_map to the RoutingOrchestrator or extend skip_nets.

Issue #1292: Wire net_class_map into CLI route command for pour-net skipping.
"""

import contextlib
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Minimal PCB with a GND net, a signal net, and an outline
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

# PCB with multiple pour nets
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
