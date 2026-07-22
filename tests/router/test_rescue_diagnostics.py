"""Unit tests for the Phase-1 rescue diagnostics (issue #4469).

Covers the three diagnose-only reports:

* :func:`classify_rescue_failure` -- turning a rescue subprocess's captured
  output into a concrete failure reason (no more opaque ``FAILED``);
* :func:`grid_fidelity_report` -- flagging sub-cell-clearance pad pairs on the
  coarse ``--allow-unsafe-grid`` grid;
* :func:`format_stranding_report` -- rendering the reused stuck-net taxonomy.

The reason parser is exercised against the REAL ``kct route`` rescue output
format captured on board-05 (``Failed nets:`` block + escape stderr), so the
signatures stay pinned to what the router actually prints.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.router.rescue_diagnostics import (
    RescueFailureCategory,
    classify_rescue_failure,
    format_grid_fidelity_report,
    format_rescue_reason_table,
    format_stranding_report,
    grid_fidelity_report,
)

# ---------------------------------------------------------------------------
# classify_rescue_failure
# ---------------------------------------------------------------------------

# Real board-05 single-net rescue output (2026-07 capture): the net loses
# every corridor to preserved copper and the router prints blocked_path.
_REAL_STDOUT = """  Detailed routing: 0 nets routed
Failed nets:
  - Net 14 "ISENSE_A+": blocked_path (blocked_path)
  Routed: 0/1 nets (0%)
  Failure causes: {'blocked_path': 3}
  Partially connected nets (1):
    ISENSE_A+: 1/4 pads connected
"""
_REAL_STDERR = (
    "Escape routing for U10 (QFP, 32 pins, 0.80mm pitch): 0 pins escaped -- "
    "all escapes failed clearance validation.\n"
    "Net ISENSE_A+: C++ pathfinder gave up (post-route clearance validation "
    "failed; exhausted 5 resume attempts).\n"
)


def test_blocked_path_maps_to_non_rippable() -> None:
    r = classify_rescue_failure("ISENSE_A+", _REAL_STDOUT, _REAL_STDERR, output_produced=True)
    assert r.category is RescueFailureCategory.BLOCKED_BY_NON_RIPPABLE
    assert r.router_cause == "blocked_path"
    # Pad-connectivity ratio and escape note are surfaced for the net.
    assert r.pads_connected == "1/4"
    assert "0 pins escaped" in r.escape_note
    # The coarse-grid clearance-validation failure is flagged.
    assert r.grid_infidelity is True


def test_pin_access_maps_to_no_legal_escape() -> None:
    stdout = 'Failed nets:\n  - Net 3 "NRST": pin_access (pin_access)\n'
    r = classify_rescue_failure("NRST", stdout, "", output_produced=True)
    assert r.category is RescueFailureCategory.NO_LEGAL_ESCAPE
    assert r.router_cause == "pin_access"


def test_clearance_cause_maps_to_clearance_infidelity() -> None:
    stdout = 'Failed nets:\n  - Net 9 "SDA": clearance (clearance)\n'
    r = classify_rescue_failure("SDA", stdout, "", output_produced=True)
    assert r.category is RescueFailureCategory.CLEARANCE_INFIDELITY


def test_escape_signature_without_failed_block() -> None:
    """No per-net cause line, but stderr shows an escape failure."""
    stderr = (
        "Escape routing for U1 (QFN, 0.5mm pitch): 0 pins escaped -- no grid point reachable.\n"
    )
    r = classify_rescue_failure("PWM_CH", "", stderr, output_produced=True)
    assert r.category is RescueFailureCategory.NO_LEGAL_ESCAPE
    assert "0 pins escaped" in r.escape_note


def test_budget_signature() -> None:
    stdout = "Net PWM_CL: deadline reached before a full path was found\n"
    r = classify_rescue_failure("PWM_CL", stdout, "", output_produced=True)
    assert r.category is RescueFailureCategory.BUDGET_EXHAUSTED


def test_no_output_produced() -> None:
    r = classify_rescue_failure("ISENSE_C-", "", "", output_produced=False)
    assert r.category is RescueFailureCategory.NO_OUTPUT


def test_unknown_when_no_signature() -> None:
    r = classify_rescue_failure("FOO", "some unrelated log line\n", "", output_produced=True)
    assert r.category is RescueFailureCategory.UNKNOWN


def test_net_name_with_plus_suffix_is_regex_escaped() -> None:
    """A ``+``/``-`` net suffix must not corrupt the FailureCause regex."""
    stdout = 'Failed nets:\n  - Net 14 "ISENSE_A+": blocked_path (blocked_path)\n'
    # A sibling net must NOT steal ISENSE_A+'s cause line.
    r = classify_rescue_failure("ISENSE_A", stdout, "", output_produced=True)
    assert r.router_cause == ""  # exact-name match only; no false positive


def test_last_cause_wins_across_escalation_reprints() -> None:
    stdout = (
        'Failed nets:\n  - Net 1 "X": congestion (congestion)\n'
        'Failed nets:\n  - Net 1 "X": blocked_path (blocked_path)\n'
    )
    r = classify_rescue_failure("X", stdout, "", output_produced=True)
    assert r.router_cause == "blocked_path"


def test_format_rescue_reason_table_empty_is_blank() -> None:
    assert format_rescue_reason_table([]) == ""


def test_format_rescue_reason_table_lists_each_net() -> None:
    reasons = [
        classify_rescue_failure("ISENSE_A+", _REAL_STDOUT, _REAL_STDERR, output_produced=True),
        classify_rescue_failure("PWM_CH", "", "", output_produced=False),
    ]
    table = format_rescue_reason_table(reasons)
    assert "ISENSE_A+" in table
    assert "blocked_by_non_rippable_copper" in table
    assert "PWM_CH" in table
    assert "no_output" in table
    # No opaque legacy phrasing.
    assert "no output produced)" not in table.replace("no_output", "")


# ---------------------------------------------------------------------------
# grid_fidelity_report
# ---------------------------------------------------------------------------

_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
"""


def _pad_fp(ref: str, x: float, y: float, net: int, name: str, size: str = "0.3 0.3") -> str:
    return (
        f'  (footprint "F" (layer "F.Cu") (at {x} {y})\n'
        f'    (property "Reference" "{ref}")\n'
        f'    (pad "1" smd rect (at 0 0) (size {size}) (layers "F.Cu") (net {net} "{name}"))\n'
        f"  )\n"
    )


def _write_grid_board(tmp_path: Path, body: str) -> Path:
    pcb = tmp_path / "grid.kicad_pcb"
    pcb.write_text(_HEADER + body + ")\n")
    return pcb


def test_grid_fidelity_flags_close_distinct_net_pair(tmp_path: Path) -> None:
    # Two 0.3mm pads 0.5mm apart (edge gap 0.2mm) on distinct nets, plus a far
    # pad that must NOT be flagged.
    body = (
        '  (net 1 "A")\n  (net 2 "B")\n  (net 3 "C")\n'
        + _pad_fp("P1", 10.0, 10.0, 1, "A")
        + _pad_fp("P2", 10.5, 10.0, 2, "B")
        + _pad_fp("P3", 30.0, 30.0, 3, "C")
    )
    pcb = _write_grid_board(tmp_path, body)
    rep = grid_fidelity_report(pcb, resolution=0.1, clearance=0.15)
    assert rep.unsafe_grid is True  # 0.1 > 0.15/2
    assert len(rep.sites) == 1
    site = rep.sites[0]
    assert {site.net_a, site.net_b} == {"A", "B"}
    # Directional box support: edge gap is 0.5 - 0.15 - 0.15 = 0.2mm (NOT the
    # bounding half-diagonal, which would spuriously report overlap).
    assert abs(site.edge_gap_mm - 0.2) < 1e-6


def test_grid_fidelity_ignores_same_net_and_far_pairs(tmp_path: Path) -> None:
    body = (
        '  (net 1 "A")\n'
        + _pad_fp("P1", 10.0, 10.0, 1, "A")
        + _pad_fp("P2", 10.4, 10.0, 1, "A")  # same net -- ignored
    )
    pcb = _write_grid_board(tmp_path, body)
    rep = grid_fidelity_report(pcb, resolution=0.1, clearance=0.15)
    assert rep.sites == []


def test_grid_fidelity_out_of_band_not_flagged(tmp_path: Path) -> None:
    # 0.3mm pads 1.0mm apart -> edge gap 0.7mm >> band 0.35mm.
    body = (
        '  (net 1 "A")\n  (net 2 "B")\n'
        + _pad_fp("P1", 10.0, 10.0, 1, "A")
        + _pad_fp("P2", 11.0, 10.0, 2, "B")
    )
    pcb = _write_grid_board(tmp_path, body)
    rep = grid_fidelity_report(pcb, resolution=0.1, clearance=0.15)
    assert rep.sites == []


def test_grid_fidelity_thin_pads_use_directional_support(tmp_path: Path) -> None:
    # Long thin pads side by side (short-axis facing): the bounding half-diagonal
    # would falsely report a big overlap; the directional support reports the
    # true small positive gap.
    body = (
        '  (net 1 "A")\n  (net 2 "B")\n'
        + _pad_fp("P1", 10.0, 10.0, 1, "A", size="0.2 1.5")
        + _pad_fp("P2", 10.4, 10.0, 2, "B", size="0.2 1.5")
    )
    pcb = _write_grid_board(tmp_path, body)
    rep = grid_fidelity_report(pcb, resolution=0.1, clearance=0.15)
    assert len(rep.sites) == 1
    # 0.4 - 0.1 - 0.1 = 0.2mm, positive (not a spurious diagonal overlap).
    assert abs(rep.sites[0].edge_gap_mm - 0.2) < 1e-6


def test_grid_fidelity_safe_grid_flag(tmp_path: Path) -> None:
    body = '  (net 1 "A")\n' + _pad_fp("P1", 10.0, 10.0, 1, "A")
    pcb = _write_grid_board(tmp_path, body)
    rep = grid_fidelity_report(pcb, resolution=0.05, clearance=0.15)
    assert rep.unsafe_grid is False  # 0.05 <= 0.15/2


def test_grid_fidelity_excluded_nets(tmp_path: Path) -> None:
    body = (
        '  (net 1 "A")\n  (net 2 "GND")\n'
        + _pad_fp("P1", 10.0, 10.0, 1, "A")
        + _pad_fp("P2", 10.5, 10.0, 2, "GND")
    )
    pcb = _write_grid_board(tmp_path, body)
    rep = grid_fidelity_report(
        pcb, resolution=0.1, clearance=0.15, excluded_nets=frozenset({"GND"})
    )
    assert rep.sites == []


def test_format_grid_fidelity_report_renders_unsafe(tmp_path: Path) -> None:
    body = (
        '  (net 1 "A")\n  (net 2 "B")\n'
        + _pad_fp("P1", 10.0, 10.0, 1, "A")
        + _pad_fp("P2", 10.5, 10.0, 2, "B")
    )
    pcb = _write_grid_board(tmp_path, body)
    rep = grid_fidelity_report(pcb, resolution=0.1, clearance=0.15)
    text = format_grid_fidelity_report(rep)
    assert "UNSAFE" in text
    assert "A / B" in text
    assert "sub-clearance sites flagged : 1" in text


# ---------------------------------------------------------------------------
# format_stranding_report (reuse of stuck-net taxonomy)
# ---------------------------------------------------------------------------


def test_stranding_report_fully_connected(tmp_path: Path) -> None:
    body = (
        '  (net 1 "A")\n'
        + _pad_fp("P1", 10.0, 10.0, 1, "A")
        + _pad_fp("P2", 10.5, 10.0, 1, "A")
        + '  (segment (start 10 10) (end 10.5 10) (width 0.2) (layer "F.Cu") (net 1))\n'
    )
    pcb = _write_grid_board(tmp_path, body)
    text = format_stranding_report(pcb)
    assert "stranding classification" in text
    # A trivially-connected board reports no stranded nets.
    assert "fully connected" in text or "stranded signal nets: 0" in text
