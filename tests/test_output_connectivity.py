"""Tests for output connectivity verification (Issue #2264).

Verifies that verify_output_connectivity() correctly detects connected and
disconnected nets by re-parsing S-expression output.
"""

from kicad_tools.router.io import verify_output_connectivity
from kicad_tools.router.primitives import Layer, Pad, Segment, Via


def _pad(x: float, y: float, net: int, ref: str = "U1", pin: str = "1") -> Pad:
    """Create a minimal Pad for testing."""
    return Pad(
        x=x, y=y, width=0.5, height=0.5,
        net=net, net_name=f"NET{net}", ref=ref, pin=pin,
    )


def _seg_sexp(x1: float, y1: float, x2: float, y2: float, net: int) -> str:
    """Generate a segment S-expression string."""
    return (
        f'(segment (start {x1:.4f} {y1:.4f}) (end {x2:.4f} {y2:.4f}) '
        f'(width 0.2) (layer "F.Cu") (net {net}) (uuid "test-uuid"))'
    )


def _via_sexp(x: float, y: float, net: int) -> str:
    """Generate a via S-expression string."""
    return (
        f'(via (at {x:.4f} {y:.4f}) (size 0.6) (drill 0.3) '
        f'(layers "F.Cu" "B.Cu") (net {net}) (uuid "test-uuid"))'
    )


def _wrap_pcb(fragments: str) -> str:
    """Wrap S-expression fragments in a minimal PCB structure."""
    return f"(kicad_pcb (version 20221018)\n  {fragments}\n)"


class TestVerifyOutputConnectivity:
    """Tests for verify_output_connectivity()."""

    def test_fully_connected_two_pad_net(self):
        """A net with two pads connected by a segment reports connected."""
        pcb = _wrap_pcb(_seg_sexp(0.0, 0.0, 5.0, 0.0, 1))
        pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U1", "2")]

        result = verify_output_connectivity(pcb, {1: pads})

        assert result[1]["total_pads"] == 2
        assert result[1]["connected_pads"] == 2
        assert result[1]["connected"] is True
        assert result[1]["disconnected_pads"] == []

    def test_disconnected_two_pad_net(self):
        """Two pads with only a short escape stub are disconnected."""
        pcb = _wrap_pcb(_seg_sexp(0.0, 0.0, 1.0, 0.0, 1))
        pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(10.0, 0.0, 1, "U2", "3")]

        result = verify_output_connectivity(pcb, {1: pads})

        assert result[1]["total_pads"] == 2
        assert result[1]["connected"] is False
        assert "U2:3" in result[1]["disconnected_pads"]

    def test_no_segments_for_net(self):
        """A net with no segments in the output reports 0 connected pads."""
        pcb = _wrap_pcb("")
        pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U1", "2")]

        result = verify_output_connectivity(pcb, {1: pads})

        assert result[1]["total_pads"] == 2
        assert result[1]["connected_pads"] == 0
        assert result[1]["connected"] is False

    def test_single_pad_net_trivially_connected(self):
        """A single-pad net is always connected."""
        pcb = _wrap_pcb("")
        pads = [_pad(0.0, 0.0, 1, "U1", "1")]

        result = verify_output_connectivity(pcb, {1: pads})

        assert result[1]["total_pads"] == 1
        assert result[1]["connected"] is True

    def test_chain_of_segments(self):
        """Three pads connected by a chain of segments are all connected."""
        segments = "\n".join([
            _seg_sexp(0.0, 0.0, 5.0, 0.0, 1),
            _seg_sexp(5.0, 0.0, 10.0, 0.0, 1),
        ])
        pcb = _wrap_pcb(segments)
        pads = [
            _pad(0.0, 0.0, 1, "U1", "1"),
            _pad(5.0, 0.0, 1, "U1", "2"),
            _pad(10.0, 0.0, 1, "U1", "3"),
        ]

        result = verify_output_connectivity(pcb, {1: pads})

        assert result[1]["total_pads"] == 3
        assert result[1]["connected_pads"] == 3
        assert result[1]["connected"] is True

    def test_multiple_nets_independent(self):
        """Connectivity is validated per-net independently."""
        segments = "\n".join([
            _seg_sexp(0.0, 0.0, 5.0, 0.0, 1),
            # Net 2 has no segments
        ])
        pcb = _wrap_pcb(segments)
        net1_pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U1", "2")]
        net2_pads = [_pad(0.0, 5.0, 2, "U1", "3"), _pad(5.0, 5.0, 2, "U1", "4")]

        result = verify_output_connectivity(pcb, {1: net1_pads, 2: net2_pads})

        assert result[1]["connected"] is True
        assert result[2]["connected"] is False

    def test_via_connects_segments(self):
        """A via at the junction of two segments connects them."""
        segments = "\n".join([
            _seg_sexp(0.0, 0.0, 5.0, 0.0, 1),
            _seg_sexp(5.0, 0.0, 10.0, 0.0, 1),
            _via_sexp(5.0, 0.0, 1),
        ])
        pcb = _wrap_pcb(segments)
        pads = [
            _pad(0.0, 0.0, 1, "U1", "1"),
            _pad(10.0, 0.0, 1, "U1", "2"),
        ]

        result = verify_output_connectivity(pcb, {1: pads})

        assert result[1]["connected"] is True

    def test_net_name_in_report(self):
        """Net name is included in the report when provided."""
        pcb = _wrap_pcb(_seg_sexp(0.0, 0.0, 5.0, 0.0, 1))
        pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U1", "2")]

        result = verify_output_connectivity(
            pcb, {1: pads}, net_names={1: "SPI_CLK"}
        )

        assert result[1]["net_name"] == "SPI_CLK"

    def test_net_name_fallback(self):
        """Net name defaults to 'Net <id>' when not provided."""
        pcb = _wrap_pcb(_seg_sexp(0.0, 0.0, 5.0, 0.0, 1))
        pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U1", "2")]

        result = verify_output_connectivity(pcb, {1: pads})

        assert result[1]["net_name"] == "Net 1"

    def test_pad_near_segment_endpoint_linked(self):
        """A pad close to but not exactly at a segment endpoint is linked."""
        pcb = _wrap_pcb(_seg_sexp(0.0, 0.0, 5.0, 0.0, 1))
        pads = [_pad(0.005, 0.005, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U1", "2")]

        result = verify_output_connectivity(pcb, {1: pads})

        assert result[1]["connected_pads"] == 2
        assert result[1]["connected"] is True

    def test_empty_net_pads(self):
        """Empty net_pads dict returns empty result."""
        pcb = _wrap_pcb(_seg_sexp(0.0, 0.0, 5.0, 0.0, 1))

        result = verify_output_connectivity(pcb, {})

        assert result == {}

    def test_dropped_segment_detected(self):
        """Simulates a to_sexp() bug that drops a segment -- verification catches it."""
        # Net 1 has 3 pads, but only segment from pad1 to pad2 is in the output
        # (segment from pad2 to pad3 was "dropped" during serialization)
        pcb = _wrap_pcb(_seg_sexp(0.0, 0.0, 5.0, 0.0, 1))
        pads = [
            _pad(0.0, 0.0, 1, "U1", "1"),
            _pad(5.0, 0.0, 1, "U1", "2"),
            _pad(10.0, 0.0, 1, "U1", "3"),
        ]

        result = verify_output_connectivity(pcb, {1: pads})

        assert result[1]["connected"] is False
        assert result[1]["connected_pads"] == 2
        assert "U1:3" in result[1]["disconnected_pads"]

    def test_segment_on_wrong_net_detected(self):
        """A segment assigned to the wrong net does not help the correct net."""
        # Segment is on net 2 but pads are on net 1
        pcb = _wrap_pcb(_seg_sexp(0.0, 0.0, 5.0, 0.0, 2))
        pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U1", "2")]

        result = verify_output_connectivity(pcb, {1: pads})

        assert result[1]["connected"] is False
        assert result[1]["connected_pads"] == 0
