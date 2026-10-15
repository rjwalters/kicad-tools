# kct check vs kicad-cli: per-net length and pair skew (Issue #6725)

The acceptance bench for #6725 checks that `kct check` (`diffpair_length_skew`) and
kicad-cli 10.0.6 report the same per-net lengths and pair skews. It covers every
diff pair on boards 03, 06b, 06rf and 07rf (`diffpair_bench.py`, lattice, seed 42).

## Method

- Route each board on this branch with
  `scripts/research/diffpair_bench.py run --boards <b> --engines lattice --seeds 42 --timeout 3600 --extra=--no-cache`.
  The C++ backend was built (`kct build-native --check`: build 56).
- **kct**: `DiffPairLengthTracker.measure_net_from_pcb`, the function behind
  `kct check --net-class-map`'s skew rules.
- **kicad-cli**: Docker `kicad/kicad:10.0` (`kicad-cli version` = 10.0.6), one
  container at a time. Each routed board was copied next to an override
  `.kicad_dru` with one rule per pair:
  `(constraint length (max 0.001mm)) (constraint skew (max 0.00001mm))`.
  Every net then violates its length rule, and the reported `actual` value is
  kicad-cli's net length (printed to 0.1 µm). The 0.1 µm print resolution sets
  the floor of the "d" columns.
  Any run where kicad-cli exited non-zero or wrote an empty report would have
  been discarded. None did.
- **Verdicts**: kct compares its own skew against the pair's resolved limit.
  The kicad-cli verdict comes from a second Docker DRC on the board with its
  own router-written `.kicad_dru`. Every pair below carries a
  `kct diff pair skew - P/N` rule there.

## Result: every pair agrees within 0.1 µm

Lengths and skews are in mm. The d columns give kct minus kicad-cli, in µm.

| Board | Pair | kct P | kicad P | dP | kct N | kicad N | dN | kct skew | kicad skew | d skew | limit | kct | kicad-cli |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 03 | USB_D | 15.5810 | 15.5810 | +0.04 | 17.1449 | 17.1449 | -0.02 | 1.5638 | 1.5639 | -0.06 | 2.500 | pass | pass |
| 03 | USB_MCU_D | 4.2616 | 4.2617 | -0.10 | 4.4160 | 4.4161 | -0.08 | 0.1544 | 0.1544 | +0.02 | 2.500 | pass | pass |
| 06b | LVDS1 | 30.9491 | 30.9491 | -0.01 | 31.0391 | 31.0391 | -0.04 | 0.0900 | 0.0900 | -0.03 | 0.100 | pass | pass |
| 06b | LVDS2 | 32.0488 | 32.0488 | -0.04 | 31.9588 | 31.9588 | +0.04 | 0.0899 | 0.0900 | -0.08 | 0.100 | pass | pass |
| 06b | LVDS3 | 31.3860 | 31.3860 | +0.02 | 31.2961 | 31.2961 | -0.00 | 0.0899 | 0.0899 | +0.02 | 0.100 | pass | pass |
| 06b | LVDS4 | 31.9411 | 31.9411 | -0.03 | 31.8511 | 31.8511 | +0.04 | 0.0899 | 0.0900 | -0.08 | 0.100 | pass | pass |
| 06rf | MIPI_CLK | 23.6627 | 23.6627 | +0.04 | 23.3927 | 23.3927 | +0.04 | 0.2700 | 0.2700 | -0.00 | 0.300 | pass | pass |
| 06rf | MIPI_D0 | 20.5037 | 20.5037 | +0.04 | 20.2339 | 20.2339 | -0.04 | 0.2699 | 0.2698 | +0.08 | 0.300 | pass | pass |
| 06rf | PCIE_RX | 28.5755 | 28.5755 | -0.05 | 29.0254 | 29.0254 | -0.03 | 0.4499 | 0.4499 | +0.02 | 0.500 | pass | pass |
| 06rf | PCIE_TX | 27.3128 | 27.3128 | -0.00 | 26.3069 | 26.3069 | -0.00 | 1.0059 | 1.0059 | -0.00 | 0.500 | fail | fail |
| 06rf | USB2_D | 16.2003 | 16.2003 | -0.01 | 15.0572 | 15.0572 | +0.03 | 1.1431 | 1.1431 | -0.05 | 1.270 | pass | pass |
| 06rf | USB3_RX1 | 57.7499 | 57.7499 | -0.04 | 57.2924 | 57.2924 | +0.02 | 0.4574 | 0.4575 | -0.05 | 0.500 | pass | pass |
| 06rf | USB3_RX2 | 48.1532 | 48.1532 | +0.05 | 48.6032 | 48.6032 | -0.04 | 0.4499 | 0.4500 | -0.09 | 0.500 | pass | pass |
| 06rf | USB3_TX1 | 47.5764 | 47.5764 | +0.02 | 50.6947 | 50.6947 | +0.00 | 3.1183 | 3.1183 | -0.01 | 0.500 | fail | fail |
| 06rf | USB3_TX2 | 45.0229 | 45.0229 | -0.02 | 44.2775 | 44.2775 | +0.02 | 0.7454 | 0.7454 | -0.03 | 0.500 | fail | fail |
| 07rf | DQS | 38.6151 | 38.6151 | -0.04 | 38.6146 | 38.6146 | +0.04 | 0.0004 | 0.0005 | -0.09 | 0.050 | pass | pass |
| 07rf | MIPI_CLK | 25.4488 | 25.4488 | +0.02 | 25.4038 | 25.4038 | +0.04 | 0.0450 | 0.0450 | -0.03 | 0.050 | pass | pass |
| 07rf | MIPI_DAT0 | 25.4486 | 25.4486 | -0.05 | 25.4040 | 25.4040 | -0.04 | 0.0446 | 0.0446 | -0.00 | 0.050 | pass | pass |
| 07rf | MIPI_DAT1 | 25.4485 | 25.4485 | +0.04 | 25.4037 | 25.4037 | -0.04 | 0.0449 | 0.0448 | +0.08 | 0.050 | pass | pass |
| 07rf | TMDS_D0 | 39.5962 | 39.5962 | -0.00 | 38.6325 | 38.6325 | +0.03 | 0.9637 | 0.9637 | -0.04 | 0.075 | fail | fail |
| 07rf | TMDS_D1 | 39.7395 | 39.7395 | +0.04 | 38.4892 | 38.4892 | -0.02 | 1.2504 | 1.2503 | +0.06 | 0.075 | fail | fail |
| 07rf | TMDS_D2 | 39.1150 | 39.1150 | +0.05 | 39.1141 | 39.1141 | -0.05 | 0.0010 | 0.0009 | +0.10 | 0.075 | pass | pass |

- The worst per-net difference is 0.10 µm and the worst skew difference is 0.10 µm.
  Both are at kicad-cli's print resolution.
- **07rf DQS passes both checkers**: the skew is 0.0004 mm in kct and
  0.0005 mm in kicad-cli, against a 0.050 mm limit. kicad-cli reports no
  `skew_out_of_range` for DQS.
- The two checkers return the same verdict on every pair. The five failing
  pairs (06rf PCIE_TX, USB3_TX1, USB3_TX2; 07rf TMDS_D0, TMDS_D1) are tuning
  failures: the tuner logged `exceeded_max_inserts` or
  `post_insertion_drc_violation` for each. They are not measurement
  disagreements.
- The 07rf match-group single-ended members (DQ0..DQ7, DM0, A0..A7) were checked
  the same way and agree within 0.7 µm. The A-bus residual is consistent
  with KiCad approximating roundrect pad corners as polygons.

## What the bench found (fixed on this branch)

The first run used the #6729 model on `main`. It disagreed with kicad-cli on 5 of 44 nets:

| Net | kct - kicad-cli | Cause |
|---|---|---|
| 03 USB_D+ / USB_D- | -187 / -178 µm | track passes through a mid-net pad centre |
| 06b LVDS1_P / LVDS1_N | -41 / -103 µm | track passes through a mid-net pad centre |
| 06rf USB2_D+ | -104 µm | track passes through a mid-net pad centre |
| 06rf PCIE_TX- | -566 µm | lines end inside a via, off-centre (4-way junction) |

The bench exposed three rules that the model had wrong. They were pinned down
with black-box kicad-cli probes: small hand-written boards run through Docker
`kicad/kicad:10.0` DRC, reading each net's reported length. No KiCad source was
consulted, and `router/kicad_length.py` is written from these observations.

1. **Segments are grouped into runs before trimming.** A run is an unbranched
   chain on one layer. Two segment ends continue a run only when they are the
   only two track ends at that point, counting every layer, and share a layer.
   A track that runs *through* a pad centre is one run there and is not trimmed
   at that pad. Only a run *ending* exactly at a pad centre is trimmed. Where
   three or more ends meet, the result depends on file order: the run grown
   first stops at the junction, and the remaining ends there can still pair
   up into one run.
2. **Via trimming does not need the centre.** A run with an end anywhere
   inside the via copper is clipped at the via boundary and continued straight
   to the via centre. A run passing through a via is not trimmed. A via trims
   at most one end of a run (the run's first end when both lie in it), and a
   run that never leaves the copper is kept.
3. **A stub via has zero height.** The via span runs between the layers the
   via is connected on. A layer counts when a track ends inside the via copper
   on it, or when a surface pad on it overlaps the via copper. A track that
   only passes through the via counts for nothing, and so does a through-hole
   pad. A via connected on fewer than two layers gets zero height: 5.0000 mm
   for a 5 mm track ending in a via. It does not fall back to the declared
   span. The Route form keeps the declared span for such a via, because a
   router `Route` can be one fragment of a net.

## Known limitation

At a junction of three or more endpoints, kicad-cli's result depends on item
order. In every probe that order was file load order. The PCB form uses file
order, and it matches every bench net and all but two of 57 probes. The two
misses are runs that loop back through a junction inside a via. The tuners'
Route form uses router order, so at such junctions it can drift from the
written board's number by one track stub. This happened on 06rf `PCIE_TX-`,
where the tuner read 1.289 mm and `kct check` read 1.006 mm. Follow-up: #6778.
