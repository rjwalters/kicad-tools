# 08 — Uncoupled Length and Via Count

Issue #6443. `diffpair_uncoupled_length` (guide 06) holds each coupled pair to
an uncoupled-length limit and a via count, in `kct check` and, natively, in
kicad-cli. Source: `src/kicad_tools/validate/rules/diffpair_uncoupled_length.py`.

Per net it measures the copper that runs beside no partner copper, and holds
the **larger of the two nets** to the pair's limit. It also holds each net's
via count to `max_vias_per_net` and warns when P and N have unequal via
counts.

The limit resolves as `max_uncoupled_total_mm` on the class (explicit, an
error), then the protocol's per-terminal allowance times the number of
**components with a pad on the net** (USB2: 6.35 mm, TI SPRAAR7J §3.3, so
board 03's `USB_D+`, with J1, R4 and U2, gets 19.05 mm). A limit that is an
`uncited` table value is a warning. The finding names its source.

```bash
kct check board.kicad_pcb --rules=diffpair_uncoupled_length
```

How KiCad measures, and so how kct does (pinned on kicad-cli 10.0.6 by
`tests/test_diffpair_uncoupled_length_6443.py`):

| Question | KiCad's answer |
|---|---|
| Per net or P + N summed? | Per net; the pair's figure is `max(uncoupled(P), uncoupled(N))`. |
| What is coupled? | The projected overlap of a P segment and an N segment on one layer that are parallel (anti-parallel too) to within about 0.01° (0.02° is already uncoupled), **unless copper stands between them where the overlap starts** (see "How pairs are formed"). |
| Distance limit? | None, unless a `diff_pair_gap (max ..)` is present. kct emits only `(min ..)`, so two parallel runs 10 mm apart are coupled. |
| Via barrels? | Not counted. Vias that sit side by side cost nothing; offset vias cost only the track copper left without a partner. |

### How pairs are formed

Issue #6554. KiCad pairs primitives; kct matches it (to 1 µm on every
committed pair and pinned probe) with a rule inferred from black-box probes:

* Each parallel same-layer P/N segment pair couples over its overlap, and the
  pairs are **summed**, not unioned (a net can read negative; kct clamps at 0).
* A pair does **not** couple at all when other copper (a track of any net, a
  via or a pad, at any distance) crosses the straight line between the traces
  at the **start of their overlap**; copper overlapping either trace does not
  block, nor does copper starting later (an N stub at 3 to 8 mm on a 0 to 16 mm
  run leaves it coupled; one at 0 to 5 mm uncouples it all).
* "Start" follows the **P trace's stored direction**: P stored 20 → 0 anchors
  at x = 20. N's and the blocker's direction are irrelevant.
* A trace lying wholly inside a pad of its own net is not paired; board 06's
  two buried stubs (0.125 and 0.2 mm) are KiCad's whole extra 0.325 mm.

### Known divergences

* **Third-net copper overlapping a pair trace** (a short) and a **third-net pad
  over a trace** are paired by KiCad in ways kct does not model; neither occurs
  on a routed pair. kct models every pad as its (rotated) rectangle.
* **Arcs** are not measured by kct.
* **Meanders:** KiCad's `diff_pair_uncoupled` is known to misreport on them; the
  first fixture belongs to the length-tuning phase (#6447).

### The per-terminal figure (kct-only)

Issue #6615. The rule also holds every **terminal** (a footprint with a pad on
the net) to `max_uncoupled_per_terminal_mm`, counting only uncoupled copper
**outside every terminal's gather-ignore region**. KiCad has no equivalent (no
`.kicad_dru` rule; the finding says `kct-only`). The total is unchanged.

* **Region**: the courtyard (`F.CrtYd` + `B.CrtYd`, every copper layer). Class
  field `gather_ignore`: `"courtyard"` (default, radius 0 mm), a number `r`
  (grown by `r` mm) or `"none"`. #6441/#6443 settle no radius, and TI SPRAAR7J
  §3.3 measures its allowance from the package, so a non-zero default would
  hide copper the source counts. **No courtyard** (or one that does not
  close): the pads' bounding box plus 0.25 mm (IPC-7351 courtyard excess),
  named `pad bounding box` in the finding.
* **Who pays**: the terminal nearest *along the copper* (endpoints,
  T-junctions and vias form the graph), so each end of a net is checked on its
  own; copper connected to no terminal goes to the nearest in a straight line.
  With `"none"` the per-terminal figures sum to the net's total.
* **Severity** follows the per-terminal limit's source (class or cited:
  `error`; `uncited`: `warning`). A net with no pads has no figure. Board 03's
  worst terminal is U2 on `USB_D+`, 1.959 mm against 6.35 mm.

### Pairs KiCad does not recognise by name

KiCad pairs nets only by its `MatchDpSuffix` rule (`+`/`-` or `P`/`N`). For
`X_POS`/`X_NEG` the `.kicad_dru` carries net-name-scoped `skew` (without
`within_diff_pairs`; it still reports `|L_p - L_n|`) and `via_count` rules.
KiCad ignores `diff_pair_gap` and `diff_pair_uncoupled` for such pairs, so none
is written and `kct check` reports one `info` line per pair: kct-only. A pair
the router left uncoupled gets no pair rules (kicad-cli checks none kct skips).

## Native rules in the `.kicad_dru`

`kct route` writes one `diff_pair_gap (min ..)` rule per coupled pair KiCad
recognises by name into a managed `.kicad_dru` block, at the gap it routed
(guide 02, "The gap the router lays"). Byte-identical across re-routes.

The same block carries, per pair and at the limits `kct check` resolves
(same resolver, same severity): `diff_pair_uncoupled (max ..)`,
`skew (max ..) (within_diff_pairs)` and `via_count (max ..)`, scoped by
`A.inDiffPair('<base>')`. An `uncited` protocol limit gets `(severity
warning)`; KiCad accepts only `error`, `warning`, `ignore` and `exclusion`;
continuity has no native rule.
