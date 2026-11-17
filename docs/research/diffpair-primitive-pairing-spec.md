# Black-box spec: how kicad-cli pairs P/N primitives into coupled length

Issue #7565 (follow-up of #6554).  A behavioural specification of the
`diff_pair_uncoupled` figure kicad-cli reports, and the model
`src/kicad_tools/validate/rules/diffpair_uncoupled_length.py` implements from it.

## Provenance

kicad-tools is MIT-licensed.  This spec, and the code written from it, rest on
**probes only**: a synthetic board goes into `kicad-cli pcb drc`, a
`diff_pair_uncoupled_length_too_long` `actual` figure comes out.  **No KiCad
source was read, in any form** (not upstream, not a local clone, not the sources
of an installed package), and neither was the reverted commit `de9ee9195` or any
description of KiCad internals derived from it.  Where this doc says what a
figure "means" it is inference from the figures, and says so.

## Method (reproducible)

* Tool: `kicad-cli` 10.0.6, the release CI's pinned image carries (check with
  `uv run python -m kicad_tools.cli.kicad_toolchain --check`), used as a black box.
* Each probe is a board with net `T_P` and `T_N` (and `X` where a third net is
  named), committed as `tests/fixtures/diffpair_pairing_probes/<name>.kicad_pcb`.
  The table that builds them, with the figure each must read, is `PROBES` in
  `tests/test_diffpair_pairing_spec_7565.py`; regenerate the boards with
  `uv run python -m tests.test_diffpair_pairing_spec_7565 tests/fixtures/diffpair_pairing_probes`.
* Rule file: `(rule "u" (condition "A.inDiffPair('T_')") (constraint diff_pair_uncoupled (max 0.01mm)))`,
  so the rule always fires and the violation text carries `actual N mm`;
  `kicad-cli pcb drc --format json --severity-all`.  The figure is the pair's
  larger net, `max(uncoupled(P), uncoupled(N))` (settled in #6443).
* Reproduce every figure below with
  `uv run pytest tests/test_diffpair_pairing_spec_7565.py::test_kicad_figure --no-cov`
  (on a Mac use `KCT_KICAD_DOCKER=1`).  Coordinates are mm, `y` grows downward, so
  "top" is min y and "bottom" is max y.
* Probe groups are lettered as in the test table (A and D are prose-only here).

## Settled before this spec (Issue #6554)

Each claim cites a probe in `PINNED` of `tests/test_diffpair_uncoupled_length_6443.py`.

1. Two same-layer segments parallel to within ~0.01 degrees couple over the overlap
   of their projections (`angle_0_005_deg` reads 0, `angle_0_02_deg` reads 20;
   `anti_parallel` reads 0; `partial_overlap` reads 8).  No distance limit
   (`far_parallel`).
2. The overlaps of all pairs are summed, not unioned: two partners beside one
   stretch both count (`intervening_mid`: 21 mm of partner on a 20 mm trace reads 0).
3. Other copper standing on the line **across the pair where their overlap starts**
   stops the pairing (`intervening_at_start` reads 16, `intervening_mid` reads 0,
   `overlap_start_not_trace_start`).  "Starts" is the end that lies at the start of
   P as stored (`anchor_p_reversed_start_side`, `anchor_p_reversed_blocks`),
   whatever N's or the blocker's direction (`anchor_ignores_n_direction`,
   `anchor_ignores_intervener_direction`).  The blocker may be any net's track, a
   via or a pad (`intervener_third_net`, `intervener_via`, `foreign_pad_between`),
   at any distance (`intervening_far_from_p`).
4. A trace wholly inside a pad of its own net is not paired (`stub_buried_in_pad`
   reads 0.2; `stub_longer_than_pad` reads 0).

Left open, and closed below: *which* copper counts as "the same conductor" and so
does not block, and what "inside a pad" means on a pad's edge.

## A. The probe, as modelled

For a P segment `a` and an N segment `b` parallel on one layer, take the point `S`
on `a`'s centre line where their overlap starts (anchored at P's stored start,
point 3 above) and the point `E` on `b`'s centre line straight across from it.  The
pair couples over its whole overlap unless some other primitive on that layer
reaches the segment `S`-`E`.

## B. Copper on the probe: only an end of it can make it the same conductor

Rule (inferred from the figures): a primitive that reaches `S`-`E` does **not**
block when its copper **covers `S` or `E`**; otherwise it blocks, **even when it
touches `a` or `b` elsewhere**.  A track's copper is its segment grown by half its
width with round caps, and its edge counts.

Fixture: `P20` (T_P, (0,0)-(20,0)) and `N1` (T_N, (0,2)-(16,2)), both 0.2 wide, plus
one more trace.  The probe at the overlap start runs from (0,0) to (0,2).  With
nothing else the figure is 0.

| Probe board | kicad-cli `actual` (mm) | Reading |
|---|---|---|
| `b_parallel_edge_on_n_centreline` | 0 | N2 y=1.9 (edge at y=2.0, N1's centre line): same conductor, does not block |
| `b_parallel_edge_short_of_n_centreline` | 16 | N2 y=1.895 (edge at 1.995, 5 um short of N1's centre line): blocks |
| `b_wide_edge_on_n_centreline` | 0 | N2 0.4 wide at y=1.8: its edge reaches y=2.0, so it does not block |
| `b_narrow_edge_short` | 16 | N2 0.1 wide at y=1.9: edge at 1.95, short of the centre line, blocks |
| `b_diagonal_far_end_on_n` | 20 | a diagonal N2 whose far end lands on N1 still blocks at x=0 |
| `b_diagonal_cap_short` | 20 | diagonal N2 starting 0.15 from N1's centre line: its cap (0.1) does not reach it |
| `b_diagonal_cap_reaches` | 4 | diagonal N2 starting 0.05 from N1's centre line: its cap covers the probe end |
| `b_vertical_stub_ends_on_n` | 4 | a stub along the probe, ending on N1's centre line: covers the probe end |
| `b_vertical_stub_beside_probe` | 4 | a stub across the pair but not at the overlap start (x=5): no block |

A trace joined to `N1` at its far end (`b_diagonal_far_end_on_n`, and on a third
net `third_net_touching_partner` in the 6443 test) is an obstruction.  kct used to
call it the same conductor because it touched `N1` somewhere, and read 4 where
kicad-cli reads 20.  And the cut-off is the trace's own centre line, not its edge:
5 um short blocks (`b_parallel_edge_short_of_n_centreline`), reaching it does not.

## C. The same, at the P end

| Probe board | kicad-cli `actual` (mm) | Reading |
|---|---|---|
| `c_p_stub_edge_on_p_centreline` | 4 | a P stub whose edge reaches P's centre line covers the probe start: no block |
| `c_p_stub_edge_short_of_p_centreline` | 20 | a P stub 10 um further out blocks P/N1 (and is itself a short against P) |

The P stub in the second probe also overlaps P itself (same net), so what separates
the two is whether its copper reaches `S`.

## E. Pads follow the same rule, with edge ties

A pad on the probe blocks unless it contains `S` or `E`.  Rect pads, the point
placed on the middle of each edge of a 1 x 1 mm pad of the trace's own net:

| Probe board | kicad-cli `actual` (mm) | Reading |
|---|---|---|
| `e_p_start_on_min_x_edge` | 4 | pad spans x 0..1: the probe start on its left edge is inside |
| `e_p_start_on_max_x_edge` | 20 | pad spans x -1..0: the probe start on its right edge is outside |
| `e_p_start_on_min_y_edge` | 20 | pad spans y 0..1: the probe start on its top edge is outside |
| `e_p_start_on_max_y_edge` | 4 | pad spans y -1..0: the probe start on its bottom edge is inside |
| `e_p_start_inside` | 4 | the probe start strictly inside the pad |
| `e_n_end_on_min_x_edge` | 4 | the same four ties at the N end of the probe: left edge inside |
| `e_n_end_on_max_x_edge` | 20 | right edge outside |
| `e_n_end_on_min_y_edge` | 20 | top edge outside |
| `e_n_end_on_max_y_edge` | 4 | bottom edge inside |
| `e_third_net_pad_on_p_start_edge` | 20 | a third-net pad clear of both ends blocks wherever it stands between |

So on a rect pad a point is **inside on the left and bottom edge (min x, max y)**
and **outside on the right and top edge (max x, min y)**, at the P end and the N end
alike.  A crossing-number point-in-polygon test with the ray cast towards +x and the
y interval half-open as `(lo, hi]` gives exactly these figures; the model
implements that on the pad's corners in board axes (so a rotated pad has ties only
where an edge is axis-aligned).  That describes the figures; it is not a claim about
how kicad-cli computes them.

A pad of the pair's own net is no exception: `pad_top_edge` in the 6443 test (the P
trace along the top edge of its own rect pad) reads 20 for this reason.

## F. Buried stubs use the same inside test

A segment with both ends inside a pad of its own net is not paired (point 4 above),
and "inside" is the same test, ties included.  Pad 0.3 x 0.6 mm at (30,0), N a
vertical 2 mm trace beside it:

| Probe board | kicad-cli `actual` (mm) | Reading |
|---|---|---|
| `f_chain_no_pad` | 1.6 | four collinear P pieces beside a 2 mm N: 0.4 couples |
| `f_chain_pad` | 1.9 | with the pad the first two pieces (0..0.25, 0.25..0.3, ending on the pad's bottom edge) are buried |
| `f_two_pieces_pad` | 1.85 | 0..0.25 is buried, 0.25..0.4 leaves the pad and couples |
| `f_stub_ends_on_max_y_edge` | 2 | a stub ending exactly on the pad's bottom edge is buried: nothing couples |
| `f_stub_ends_on_min_y_edge` | 1.7 | a stub ending on the pad's top edge is not buried: it couples (2 - 0.3) |
| `f_stub_ends_on_min_x_edge` | 4 | a stub ending on the pad's left edge is buried |
| `f_stub_ends_on_max_x_edge` | 3.85 | a stub ending on the pad's right edge is not buried |

`f_chain_pad` is board 06's USB2_D+ at the J1 A6 pad in miniature: kct counted the
piece that ends on the pad edge as coupled, kicad-cli does not.

## Board 06's regression fixture

`boards/06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_pcb`: the four
pairs kct used to read low now agree with kicad-cli 10.0.6 to the four decimals the
test records (USB2_D 8.5727, USB3_RX1 49.5276, USB3_TX2 4.1385, MIPI_D0 22.4092 mm;
`test_kct_equals_kicad_on_board_06_regression_fixture`).  Bisecting USB3_TX2 (pad
`J1 B2` against the start of P's long diagonal, which sits on the pad's right edge:
group E) and USB2_D / MIPI_D0 (stubs ending on a pad edge: group F) found the two
rules above.

## Tuning-pattern and meander copper

The tuner's plain-copper trombones and square bumps, which kct read lower than
kicad-cli before this spec, now agree (`test_plain_trombone_agrees`,
`test_square_bump_agrees`): their legs join the trace they leave, the case of
section B.

## H. Measured, not modelled

kct does not model these; the table is for whoever does.  The probe boards are
checked on the kicad-cli side only, and a test fails if kct starts matching them
(move the probe to its group then).

| Probe board | kicad-cli `actual` (mm) | Reading |
|---|---|---|
| `h_n_stub_edge_on_p_centreline` | 4 | an N stub whose end lies on P's copper reads as P copper in kicad-cli (4.0), kct pairs it with P (0.0) |
| `h_n_stub_overlapping_p` | 20 | an N stub overlapping P without an end on it: P/N1 blocked and the short is not paired (kct pairs it: 16.0) |
| `h_third_net_pad_over_p_start` | 16 | a third-net pad covering P's start takes P's net away (kct 4.0) |
| `h_roundrect_p_start_on_max_x_edge` | 4 | a roundrect pad's right edge is inside (a rect pad's is outside); kct reads 20.0 |
| `h_roundrect_n_end_on_min_y_edge` | 4 | a roundrect pad's top edge at the N end is inside (a rect pad's is outside); kct reads 20.0 |

* **Other-net copper overlapping a pair trace** (a short) changes whose copper it is
  in kicad-cli's DRC output: an N stub with an end on P is listed as a `T_P` track,
  and a third-net pad over P's start lists P as an `X` track.  kct pairs by the net
  written in the file.
* **Non-rect pad edges** (roundrect, oval, circle): on a roundrect pad the right
  edge, and the top edge at the N end, are inside, where on a rect pad they are
  outside.  kct models every pad as its rectangle.
* **Arcs** are not measured (unchanged).

## What the code does (`diffpair_uncoupled_length.py`)

* `_blocked`: a track or via that reaches the probe is skipped only when
  `_track_contains` says it covers `S` or `E`; a pad only when `_pad_contains` does.
* `_pad_contains`: the tie rule of section E.
* `_buried`: both ends `_pad_contains`-inside a pad of the trace's own net.
* `uncoupled_intervals()` is still the complement of the coupled union, so
  `measure_per_terminal()` still sums to the pair figure (#6615).
