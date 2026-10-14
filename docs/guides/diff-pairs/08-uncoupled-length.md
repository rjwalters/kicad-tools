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
| What is coupled? | The projected overlap of same-layer segments that are parallel (anti-parallel too) to within about 0.01°. 0.02° is already uncoupled. |
| Distance limit? | None, unless a `diff_pair_gap (max ..)` is present. kct emits only `(min ..)`, so two parallel runs 10 mm apart are coupled. |
| Via barrels? | Not counted. Vias that sit side by side cost nothing; offset vias cost only the track copper left without a partner. |

### Known divergences

* **KiCad's figure is larger than kct's on real copper.** KiCad does not
  couple two parallel runs when a nearer non-parallel neighbour intervenes;
  kct couples any parallel overlap. On the committed boards KiCad reports
  10.405 / 0.964 mm for board 03's `USB_D` / `USB_MCU_D` and 1.541 mm for each
  board 06 LVDS pair, against kct's 8.130 / 0.849 / 1.216 mm. kct's figure is a
  lower bound, so a pair that sits within ~3 mm of its limit can pass kct and
  fail kicad-cli. `test_known_divergence_kct_never_exceeds_kicad_on_committed_copper`
  pins the gap. Every committed pair is far below its limit in both engines.
* **Arcs** are not measured by kct.
* **Meanders:** KiCad's `diff_pair_uncoupled` is known to misreport on them. No
  committed board has one; the first fixture belongs to the length-tuning phase
  (#6447).
* **Per-terminal figure:** the strict per-terminal allowance with the courtyard
  ignored is a routing target, not yet a check.

### Pairs KiCad does not recognise by name

KiCad pairs nets only by its `MatchDpSuffix` rule (`+`/`-` or `P`/`N`). For
`X_POS`/`X_NEG` the `.kicad_dru` carries the net-name-scoped `skew` rule
(without `within_diff_pairs`; it still reports `|L_p - L_n|`) and `via_count`
rule, so both engines check them. `diff_pair_gap` and `diff_pair_uncoupled`
are ignored by KiCad for such pairs whatever their condition says, so no rule
is written and `kct check` reports one `info` line per pair saying the limit
is kct-only.

A pair KiCad recognises but the router did not couple gets no pair rules, so
kicad-cli never checks a pair kct does not.

## Native rules in the `.kicad_dru`

`kct route` writes one `diff_pair_gap (min ..)` rule per coupled pair KiCad
recognises by name into a managed `.kicad_dru` block, at the gap it routed
(guide 02, "The gap the router lays"). Byte-identical across re-routes.

The same block carries, per pair and at the limits `kct check` resolves
(same resolver, same severity): `diff_pair_uncoupled (max ..)`,
`skew (max ..) (within_diff_pairs)` and `via_count (max ..)`, scoped by
`A.inDiffPair('<base>')`. A limit that is an `uncited` protocol value is
written with `(severity warning)`; KiCad accepts only `error`, `warning`,
`ignore` and `exclusion`, so continuity has no native rule.
