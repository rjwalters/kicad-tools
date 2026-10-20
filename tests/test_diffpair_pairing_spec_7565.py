"""Issue #7565: the black-box probes behind docs/research/diffpair-primitive-pairing-spec.md.

Every probe is a small two-net board (net ``T_P`` and ``T_N``, sometimes a third
net ``X``) committed under ``tests/fixtures/diffpair_pairing_probes/``.  Each one
records the ``diff_pair_uncoupled_length_too_long`` ``actual`` figure kicad-cli
10.0.6 reports for it (the maximum is set to 0.01 mm so the rule always fires),
read off the board alone: no KiCad source was consulted, in any form.

Two tests per probe:

* ``test_kicad_figure``: kicad-cli reports the figure in the table (skipped
  without kicad-cli), so the spec is reproducible;
* ``test_kct_matches``: kct's model reads the same (every probe but the ones in
  group H, which the spec lists as not modelled).

Regenerate the boards after editing the table::

    uv run python -m tests.test_diffpair_pairing_spec_7565 tests/fixtures/diffpair_pairing_probes
"""

from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path
from typing import NamedTuple

import pytest

from tests.test_diffpair_uncoupled_length_6443 import _kicad_cli, kct_uncoupled, kicad_uncoupled

PROBE_DIR = Path(__file__).resolve().parent / "fixtures" / "diffpair_pairing_probes"

_HDR = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
\t(setup (pad_to_mask_clearance 0))
"""


def _uid(*parts: object) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "kicad-tools/7565/" + "/".join(map(str, parts))))


def seg(a, b, net, layer="F.Cu", width=0.2) -> str:
    return (
        f"\t(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {width}) "
        f'(layer "{layer}") (net "{net}") (uuid "{_uid("seg", a, b, net, layer, width)}"))\n'
    )


def via(at, net) -> str:
    return (
        f"\t(via (at {at[0]} {at[1]}) (size 0.6) (drill 0.3) "
        f'(layers "F.Cu" "B.Cu") (net "{net}") (uuid "{_uid("via", at, net)}"))\n'
    )


def pad(at, net, size, ref="R1", shape="rect") -> str:
    return (
        f'\t(footprint "X" (layer "F.Cu") (at {at[0]} {at[1]}) (uuid "{_uid("fp", at, net)}")\n'
        f'\t\t(property "Reference" "{ref}" (at 0 0) (layer "F.SilkS") '
        f'(uuid "{_uid("ref", at, net)}"))\n'
        f'\t\t(pad "1" smd {shape} (at 0 0) (size {size[0]} {size[1]}) '
        f'(layers "F.Cu" "F.Mask" "F.Paste") (net "{net}") '
        f'(uuid "{_uid("pad", at, net)}")))\n'
    )


def board_text(items: list[str]) -> str:
    edge = (
        "\t(gr_rect (start -5 -10) (end 60 20) (stroke (width 0.1) (type default)) "
        f'(fill no) (layer "Edge.Cuts") (uuid "{_uid("edge")}"))\n'
    )
    return _HDR + "".join(items) + edge + ")\n"


class Probe(NamedTuple):
    group: str
    items: list[str]
    kicad_mm: float
    what: str
    #: False for the probes the spec lists as not modelled (group H).
    modelled: bool = True


P20 = seg((0, 0), (20, 0), "T_P")
N16 = seg((0, 2), (16, 2), "T_N")
_BASE = [P20, N16]
_W = 0.275


def _chain(x: float, ys: list[float], net: str) -> list[str]:
    return [seg((x, a), (x, b), net, width=_W) for a, b in zip(ys, ys[1:], strict=False)]


#: A vertical N trace 2 mm long beside the stubs of groups F: it is the only
#: partner, so the stub copper that couples is read off N's figure (2 - coupled).
_NV = seg((31, 1), (31, -1), "T_N", width=_W)
#: Same for horizontal stubs: N 4 mm long at y = 1.
_NH = seg((28, 1), (32, 1), "T_N", width=_W)

PROBES: dict[str, Probe] = {}


def _add(name: str, group: str, items: list[str], kicad_mm: float, what: str, **kw) -> None:
    assert name not in PROBES, name
    PROBES[name] = Probe(group, items, kicad_mm, what, **kw)


# -- Group B: copper between the traces, and which copper is "the same conductor" ------
# P runs 0..20 at y=0, N1 0..16 at y=2 (both 0.2 wide), and a third trace T_N crosses
# the line from P to N1 where their overlap starts (x = 0).
_add(
    "b_parallel_edge_on_n_centreline",
    "B",
    [*_BASE, seg((0, 1.9), (5, 1.9), "T_N")],
    0.0,
    "N2 y=1.9 (edge at y=2.0, N1's centre line): same conductor, does not block",
)
_add(
    "b_parallel_edge_short_of_n_centreline",
    "B",
    [*_BASE, seg((0, 1.895), (5, 1.895), "T_N")],
    16.0,
    "N2 y=1.895 (edge at 1.995, 5 um short of N1's centre line): blocks",
)
_add(
    "b_wide_edge_on_n_centreline",
    "B",
    [*_BASE, seg((0, 1.8), (5, 1.8), "T_N", width=0.4)],
    0.0,
    "N2 0.4 wide at y=1.8: its edge reaches y=2.0, so it does not block",
)
_add(
    "b_narrow_edge_short",
    "B",
    [*_BASE, seg((0, 1.9), (5, 1.9), "T_N", width=0.1)],
    16.0,
    "N2 0.1 wide at y=1.9: edge at 1.95, short of the centre line, blocks",
)
_add(
    "b_diagonal_far_end_on_n",
    "B",
    [*_BASE, seg((0, 1.0), (3, 2.0), "T_N")],
    20.0,
    "a diagonal N2 whose far end lands on N1 still blocks at x=0",
)
_add(
    "b_diagonal_cap_short",
    "B",
    [*_BASE, seg((0, 1.85), (3, 2.0), "T_N")],
    20.0,
    "diagonal N2 starting 0.15 from N1's centre line: its cap (0.1) does not reach it",
)
_add(
    "b_diagonal_cap_reaches",
    "B",
    [*_BASE, seg((0, 1.95), (3, 2.0), "T_N")],
    4.0,
    "diagonal N2 starting 0.05 from N1's centre line: its cap covers the probe end",
)
_add(
    "b_vertical_stub_ends_on_n",
    "B",
    [*_BASE, seg((0, 1.0), (0, 2.0), "T_N")],
    4.0,
    "a stub along the probe, ending on N1's centre line: covers the probe end",
)
_add(
    "b_vertical_stub_beside_probe",
    "B",
    [*_BASE, seg((5, 1.0), (5, 2.0), "T_N")],
    4.0,
    "a stub across the pair but not at the overlap start (x=5): no block",
)

# -- Group C: the P end of the probe ------------------------------------------------------
_add(
    "c_p_stub_edge_on_p_centreline",
    "C",
    [*_BASE, seg((0, 0.1), (5, 0.1), "T_P")],
    4.0,
    "a P stub whose edge reaches P's centre line covers the probe start: no block",
)
_add(
    "c_p_stub_edge_short_of_p_centreline",
    "C",
    [*_BASE, seg((0, 0.11), (5, 0.11), "T_P")],
    20.0,
    "a P stub 10 um further out blocks P/N1 (and is itself a short against P)",
)

# -- Group E: pad edge ties.  A 1 x 1 mm rect pad of the trace's own net, the probe
# point (0,0) for P (or (0,2) for N1) on one edge of it ----------------------------------
_PAD = (1.0, 1.0)
_add(
    "e_p_start_on_min_x_edge",
    "E",
    [*_BASE, pad((0.5, 0), "T_P", _PAD)],
    4.0,
    "pad spans x 0..1: the probe start on its left edge is inside",
)
_add(
    "e_p_start_on_max_x_edge",
    "E",
    [*_BASE, pad((-0.5, 0), "T_P", _PAD)],
    20.0,
    "pad spans x -1..0: the probe start on its right edge is outside",
)
_add(
    "e_p_start_on_min_y_edge",
    "E",
    [*_BASE, pad((0, 0.5), "T_P", _PAD)],
    20.0,
    "pad spans y 0..1: the probe start on its top edge is outside",
)
_add(
    "e_p_start_on_max_y_edge",
    "E",
    [*_BASE, pad((0, -0.5), "T_P", _PAD)],
    4.0,
    "pad spans y -1..0: the probe start on its bottom edge is inside",
)
_add(
    "e_p_start_inside",
    "E",
    [*_BASE, pad((0.1, 0.1), "T_P", _PAD)],
    4.0,
    "the probe start strictly inside the pad",
)
_add(
    "e_n_end_on_min_x_edge",
    "E",
    [*_BASE, pad((0.5, 2), "T_N", _PAD)],
    4.0,
    "the same four ties at the N end of the probe: left edge inside",
)
_add(
    "e_n_end_on_max_x_edge", "E", [*_BASE, pad((-0.5, 2), "T_N", _PAD)], 20.0, "right edge outside"
)
_add("e_n_end_on_min_y_edge", "E", [*_BASE, pad((0, 2.5), "T_N", _PAD)], 20.0, "top edge outside")
_add("e_n_end_on_max_y_edge", "E", [*_BASE, pad((0, 1.5), "T_N", _PAD)], 4.0, "bottom edge inside")
_add(
    "e_third_net_pad_on_p_start_edge",
    "E",
    [*_BASE, pad((0, 0.6), "X", _PAD)],
    20.0,
    "a third-net pad clear of both ends blocks wherever it stands between",
)

# -- Group F: a stub "buried" in its own pad is not paired; the pad edge counts as inside
# by the same ties.  A 0.3 x 0.6 mm rect pad at (30,0) ------------------------------------
_P03 = (0.3, 0.6)
_add(
    "f_chain_no_pad",
    "F",
    [*_chain(30, [0, 0.25, 0.3, 0.35, 0.4], "T_P"), _NV],
    1.6,
    "four collinear P pieces beside a 2 mm N: 0.4 couples",
)
_add(
    "f_chain_pad",
    "F",
    [*_chain(30, [0, 0.25, 0.3, 0.35, 0.4], "T_P"), _NV, pad((30, 0), "T_P", _P03)],
    1.9,
    "with the pad the first two pieces (0..0.25, 0.25..0.3, ending on the pad's bottom edge) are buried",
)
_add(
    "f_two_pieces_pad",
    "F",
    [*_chain(30, [0, 0.25, 0.4], "T_P"), _NV, pad((30, 0), "T_P", _P03)],
    1.85,
    "0..0.25 is buried, 0.25..0.4 leaves the pad and couples",
)
_add(
    "f_stub_ends_on_max_y_edge",
    "F",
    [seg((30, 0), (30, 0.3), "T_P", width=_W), _NV, pad((30, 0), "T_P", _P03)],
    2.0,
    "a stub ending exactly on the pad's bottom edge is buried: nothing couples",
)
_add(
    "f_stub_ends_on_min_y_edge",
    "F",
    [seg((30, 0), (30, -0.3), "T_P", width=_W), _NV, pad((30, 0), "T_P", _P03)],
    1.7,
    "a stub ending on the pad's top edge is not buried: it couples (2 - 0.3)",
)
_add(
    "f_stub_ends_on_min_x_edge",
    "F",
    [seg((30, 0), (29.85, 0), "T_P", width=_W), _NH, pad((30, 0), "T_P", _P03)],
    4.0,
    "a stub ending on the pad's left edge is buried",
)
_add(
    "f_stub_ends_on_max_x_edge",
    "F",
    [seg((30, 0), (30.15, 0), "T_P", width=_W), _NH, pad((30, 0), "T_P", _P03)],
    3.85,
    "a stub ending on the pad's right edge is not buried",
)

# -- Group H: measured, and NOT modelled by kct (see the spec's section H) ----------------
_add(
    "h_n_stub_edge_on_p_centreline",
    "H",
    [*_BASE, seg((0, 0.1), (5, 0.1), "T_N")],
    4.0,
    "an N stub whose end lies on P's copper reads as P copper in kicad-cli (4.0), "
    "kct pairs it with P (0.0)",
    modelled=False,
)
_add(
    "h_n_stub_overlapping_p",
    "H",
    [*_BASE, seg((0, 0.11), (5, 0.11), "T_N")],
    20.0,
    "an N stub overlapping P without an end on it: P/N1 blocked and the short is "
    "not paired (kct pairs it: 16.0)",
    modelled=False,
)
_add(
    "h_third_net_pad_over_p_start",
    "H",
    [*_BASE, pad((0, 0.3), "X", _PAD)],
    16.0,
    "a third-net pad covering P's start takes P's net away (kct 4.0)",
    modelled=False,
)
_add(
    "h_roundrect_p_start_on_max_x_edge",
    "H",
    [*_BASE, pad((-0.5, 0), "T_P", _PAD, shape="roundrect")],
    4.0,
    "a roundrect pad's right edge is inside (a rect pad's is outside); kct reads 20.0",
    modelled=False,
)
_add(
    "h_roundrect_n_end_on_min_y_edge",
    "H",
    [*_BASE, pad((0, 2.5), "T_N", _PAD, shape="roundrect")],
    4.0,
    "a roundrect pad's top edge at the N end is inside (a rect pad's is outside); kct reads 20.0",
    modelled=False,
)


def write_probes(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, probe in PROBES.items():
        (directory / f"{name}.kicad_pcb").write_text(board_text(probe.items))


def _work_copy(name: str, tmp_path: Path) -> Path:
    src = PROBE_DIR / f"{name}.kicad_pcb"
    work = tmp_path / src.name
    shutil.copyfile(src, work)
    work.with_suffix(".kicad_pro").write_text(
        '{"board":{"design_settings":{"rules":{"min_clearance":0.0}}},"meta":{"version":3}}'
    )
    return work


@pytest.mark.parametrize("name", sorted(PROBES))
def test_committed_board_is_the_probe(name) -> None:
    """The committed board is exactly what the table builds (regenerate on edit)."""
    committed = (PROBE_DIR / f"{name}.kicad_pcb").read_text()
    assert committed == board_text(PROBES[name].items)


@pytest.mark.parametrize("name", sorted(PROBES))
def test_kicad_figure(name, tmp_path) -> None:
    """kicad-cli reads the recorded figure on the committed probe board."""
    work = _work_copy(name, tmp_path)
    assert kicad_uncoupled(_kicad_cli(), work) == pytest.approx(PROBES[name].kicad_mm, abs=1e-3)


@pytest.mark.parametrize("name", [n for n in sorted(PROBES) if PROBES[n].modelled])
def test_kct_matches(name, tmp_path) -> None:
    """kct's model reads the figure kicad-cli does on the committed probe board."""
    work = _work_copy(name, tmp_path)
    assert kct_uncoupled(work) == pytest.approx(PROBES[name].kicad_mm, abs=1e-3)


@pytest.mark.parametrize("name", [n for n in sorted(PROBES) if not PROBES[n].modelled])
def test_unmodelled_probe_still_differs(name, tmp_path) -> None:
    """Group H is documented as not modelled: if kct starts matching, move it to B-F."""
    work = _work_copy(name, tmp_path)
    assert kct_uncoupled(work) != pytest.approx(PROBES[name].kicad_mm, abs=1e-3)


if __name__ == "__main__":
    write_probes(Path(sys.argv[1]) if len(sys.argv) > 1 else PROBE_DIR)
