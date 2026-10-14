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

Issue #6554. KiCad pairs primitives; it does not couple every parallel
overlap. kct reproduces its figure (to 1 µm on every committed pair, and on
the probe set pinned in the test file) with a rule inferred from black-box
kicad-cli probes:

* Each parallel same-layer P/N segment pair couples over its overlap. The
  coupled lengths of all pairs are **summed**, not unioned: two partners beside
  the same stretch both count, so a net can read negative (kct clamps at 0).
* A pair does **not** couple (not even partly) when other copper stands between
  the two traces at the **start of their overlap**. The probe is the straight
  line across the gap there. A track of either net or of a third net, a via or
  a pad blocks it, at any distance; copper that overlaps either trace of the
  pair is the same conductor and does not. Copper that starts later on the
  overlap does not block: an N stub 3 to 8 mm along a 0 to 16 mm run leaves the
  run coupled, while a stub 0 to 5 mm leaves it entirely uncoupled.
* "Start" follows the **P trace's stored direction** (the end it was written
  from); N's and the intervening copper's direction are irrelevant. A P trace
  stored 20 → 0 anchors at its x = 20 end.
* A trace that lies wholly inside a pad of its own net (a stub from the pad
  centre that ends within the pad) is not paired.

On real boards this is what makes short stubs at the pads uncouple: board 06's
two stubs (0.125 mm and 0.2 mm) are buried in their pads, which is the whole of
KiCad's extra 0.325 mm over the old projection model.

### Known divergences

* **Third-net copper overlapping one trace of the pair** (a short) and a
  third-net pad lying over a trace are paired by KiCad in ways kct does not
  model. Neither occurs on a routed pair.
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
