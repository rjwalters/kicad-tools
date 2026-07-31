"""Tests for output connectivity verification (Issue #2264).

Verifies that verify_output_connectivity() correctly detects connected and
disconnected nets by re-parsing S-expression output.
"""

from kicad_tools.router.io import verify_output_connectivity
from kicad_tools.router.primitives import Pad


def _pad(x: float, y: float, net: int, ref: str = "U1", pin: str = "1") -> Pad:
    """Create a minimal Pad for testing."""
    return Pad(
        x=x,
        y=y,
        width=0.5,
        height=0.5,
        net=net,
        net_name=f"NET{net}",
        ref=ref,
        pin=pin,
    )


def _seg_sexp(x1: float, y1: float, x2: float, y2: float, net: int) -> str:
    """Generate a segment S-expression string."""
    return (
        f"(segment (start {x1:.4f} {y1:.4f}) (end {x2:.4f} {y2:.4f}) "
        f'(width 0.2) (layer "F.Cu") (net {net}) (uuid "test-uuid"))'
    )


def _via_sexp(x: float, y: float, net: int) -> str:
    """Generate a via S-expression string."""
    return (
        f"(via (at {x:.4f} {y:.4f}) (size 0.6) (drill 0.3) "
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
        segments = "\n".join(
            [
                _seg_sexp(0.0, 0.0, 5.0, 0.0, 1),
                _seg_sexp(5.0, 0.0, 10.0, 0.0, 1),
            ]
        )
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
        segments = "\n".join(
            [
                _seg_sexp(0.0, 0.0, 5.0, 0.0, 1),
                # Net 2 has no segments
            ]
        )
        pcb = _wrap_pcb(segments)
        net1_pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U1", "2")]
        net2_pads = [_pad(0.0, 5.0, 2, "U1", "3"), _pad(5.0, 5.0, 2, "U1", "4")]

        result = verify_output_connectivity(pcb, {1: net1_pads, 2: net2_pads})

        assert result[1]["connected"] is True
        assert result[2]["connected"] is False

    def test_via_connects_segments(self):
        """A via at the junction of two segments connects them."""
        segments = "\n".join(
            [
                _seg_sexp(0.0, 0.0, 5.0, 0.0, 1),
                _seg_sexp(5.0, 0.0, 10.0, 0.0, 1),
                _via_sexp(5.0, 0.0, 1),
            ]
        )
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

        result = verify_output_connectivity(pcb, {1: pads}, net_names={1: "SPI_CLK"})

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


# ---------------------------------------------------------------------------
# KiCad-10 name-only net dialect + linear-time scan (Issue #4476)
# ---------------------------------------------------------------------------


def _named_seg_sexp(x1: float, y1: float, x2: float, y2: float, net_name: str) -> str:
    """A segment in the KiCad-10 name-only dialect: ``(net "NAME")``."""
    return (
        f"(segment\n\t\t(start {x1:.4f} {y1:.4f})\n\t\t(end {x2:.4f} {y2:.4f})\n"
        f'\t\t(width 0.2)\n\t\t(layer "F.Cu")\n\t\t(uuid "u")\n\t\t(net "{net_name}")\n\t)'
    )


def _named_via_sexp(x: float, y: float, net_name: str) -> str:
    return (
        f"(via\n\t\t(at {x:.4f} {y:.4f})\n\t\t(size 0.6)\n\t\t(drill 0.3)\n"
        f'\t\t(layers "F.Cu" "B.Cu")\n\t\t(uuid "u")\n\t\t(net "{net_name}")\n\t)'
    )


def _wrap_named_pcb(fragments: str, *, net_table: dict[int, str]) -> str:
    """Wrap fragments in a PCB whose header declares the numeric net table."""
    table = "\n  ".join(f'(net {nid} "{name}")' for nid, name in sorted(net_table.items()))
    return f"(kicad_pcb (version 20241229)\n  {table}\n  {fragments}\n)"


class TestNameOnlyNetDialect:
    """Issue #4476: verification must read the ``(net "NAME")`` dialect.

    ``route_cmd._board_uses_name_only_dialect`` (#4416) re-emits route copper
    in whatever dialect the input board uses, and board-05 IS a name-only
    board (its zones are written that way).  The pre-#4476 numeric-only
    pattern matched nothing on such a board, so the post-save verification
    silently saw zero copper -- and, because its lazy gaps then scanned to
    end-of-file per ``(segment`` token, took minutes to say so.
    """

    def test_named_segment_connects_a_net(self):
        pcb = _wrap_named_pcb(_named_seg_sexp(0.0, 0.0, 5.0, 0.0, "NET1"), net_table={1: "NET1"})
        pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U1", "2")]

        result = verify_output_connectivity(pcb, {1: pads})

        assert result[1]["connected"] is True
        assert result[1]["connected_pads"] == 2

    def test_named_via_bridges_two_segments(self):
        frags = "\n  ".join(
            [
                _named_seg_sexp(0.0, 0.0, 5.0, 0.0, "NET1"),
                _named_via_sexp(5.0, 0.0, "NET1"),
                _named_seg_sexp(5.0, 0.0, 10.0, 0.0, "NET1"),
            ]
        )
        pcb = _wrap_named_pcb(frags, net_table={1: "NET1"})
        pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(10.0, 0.0, 1, "U2", "1")]

        result = verify_output_connectivity(pcb, {1: pads})

        assert result[1]["connected"] is True

    def test_named_segment_on_wrong_net_does_not_help(self):
        pcb = _wrap_named_pcb(
            _named_seg_sexp(0.0, 0.0, 5.0, 0.0, "NET2"), net_table={1: "NET1", 2: "NET2"}
        )
        pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U1", "2")]

        result = verify_output_connectivity(pcb, {1: pads})

        assert result[1]["connected"] is False
        assert result[1]["connected_pads"] == 0

    def test_unknown_net_name_is_ignored_not_crashed(self):
        """A name absent from the header table is skipped, never an exception."""
        pcb = _wrap_named_pcb(_named_seg_sexp(0.0, 0.0, 5.0, 0.0, "GHOST"), net_table={1: "NET1"})
        pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(5.0, 0.0, 1, "U1", "2")]

        result = verify_output_connectivity(pcb, {1: pads})

        assert result[1]["connected"] is False

    def test_scan_is_linear_on_a_large_unmatched_board(self):
        """Regression guard for the >4-minute board-05 stall.

        700 name-only segments in a 500 KB board: the pre-#4476 pattern made
        each ``(segment`` token scan to end-of-file hunting for a numeric
        ``(net N)``.  The block-scoped scan is linear, so this completes in
        well under a second.
        """
        import time

        frags = "\n  ".join(
            _named_seg_sexp(float(i), 0.0, float(i) + 1.0, 0.0, "NET1") for i in range(700)
        )
        # Pad the board out with unrelated text so a runaway scan is expensive.
        filler = "\n".join(
            f'  (gr_line (start {i} 0) (end {i} 1) (layer "Edge.Cuts"))' for i in range(3000)
        )
        pcb = _wrap_named_pcb(frags + "\n" + filler, net_table={1: "NET1"})
        assert len(pcb) > 200_000

        pads = [_pad(0.0, 0.0, 1, "U1", "1"), _pad(700.0, 0.0, 1, "U2", "1")]
        started = time.monotonic()
        verify_output_connectivity(pcb, {1: pads})
        assert time.monotonic() - started < 5.0
