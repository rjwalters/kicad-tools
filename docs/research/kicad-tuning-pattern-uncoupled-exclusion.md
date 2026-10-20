# Does a KiCad tuning pattern take the partner leg out of the uncoupled figure?

Issue #7632, the first step of operator ruling Q31 = 1 on #7521.
Dated 2026-10-20, against `origin/main` @ `0946e0ba2`.

## Provenance

kicad-tools is MIT-licensed, and this note rests on **black-box probes only**. A
synthetic board goes into `kicad-cli pcb drc`, and a
`diff_pair_uncoupled_length_too_long` `actual` figure comes out. **No KiCad source
was read in any form**: not upstream, not a local clone, and not the sources of an
installed package. When this note says what a figure "means", that is inference from
the figures, and it says so. The method is the one in
[`diffpair-primitive-pairing-spec.md`](diffpair-primitive-pairing-spec.md) (#7565).

## Answer

**No.** Emitting the bump as a tuning-pattern object, as kct does today (#7542),
does not take the partner copper beside the bump out of KiCad's figure. The bump's
own copper does leave the figure. The partner span beside it then has nothing to
pair with, so it counts in full: the figure is the bump's base-line length `B`,
whatever the bump's height.

**KiCad does exclude partner copper that is itself a tuning-pattern member.** This
works if the partner span beside the bump is cut out of its leg and listed as a
member, either of the bump's own pattern or of a separate pattern on the partner net.
The bump then adds 0 to the figure. So the effect Q31 = 1 asks for can be had, but
only by emitting the partner span as pattern copper. The #7591 review said Q16 rules
that out ("Do not route around this by labelling the partner span as a pattern").
Whether Q31 = 1 overrides that is the operator's call. The probes cannot settle it.

## Method (reproducible)

* Tool: `kicad-cli` 10.0.6, the release in CI's pinned image, run through
  `evidence_toolchain` (on a Mac: `KCT_KICAD_DOCKER=1`). Boards were checked one at
  a time, and a non-zero kicad-cli exit fails the probe (#6724).
* Boards: nets `D_P` (y = 100) and `D_N` (y = 100.45), both from x = 100 to 120,
  0.2 mm wide, so the nominal gap is 0.25 mm. Any bump is on N, starts at x = 108
  and pushes away from P by `h`. Every board is built with kct's own writers
  (`Segment.to_sexp`, `TuningPattern.to_sexp`) using deterministic uuids. They are
  committed as `tests/fixtures/tuning_pattern_partner_probes/<name>.kicad_pcb`, and
  the table that builds them is `KICAD` / `PROBES` in
  `tests/test_tuning_pattern_partner_exclusion_7632.py`.
* Rules: kct-style, `diff_pair_gap (min 0.2mm)` with `diff_pair_uncoupled (max 0.01mm)`
  on `A.inDiffPair('D_*')`, so the rule always fires. The figure is the pair's.
* Reproduce: `KCT_KICAD_DOCKER=1 uv run pytest tests/test_tuning_pattern_partner_exclusion_7632.py --no-cov`.
  `test_kct_matches` checks that kct's own measure (`kct check`) reads every figure
  below as well.
* Heights are 0.5x, 0.9x and 2x the gap (`h05`, `h09`, `h20`: 0.125, 0.225 and
  0.5 mm). The Q27 cap admits the first two. Flat-top widths are 0.4 mm (`short`,
  the tuner's loop width) and 4 mm (`long`).

## A/B. Square bump, the tuner's shape: plain copper vs tuning pattern

| Probe board (`a_square_*_plain` / `b_square_*_pattern`) | plain (mm) | pattern (mm) | Reading |
|---|---|---|---|
| `h05_short` | 0.65 | 0.4 | plain = B + 2h; pattern = B |
| `h05_long` | 4.25 | 4.0 | |
| `h09_short` | 0.85 | 0.4 | |
| `h09_long` | 4.45 | 4.0 | |
| `h20_short` | 1.4 | 0.4 | |
| `h20_long` | 5.0 | 4.0 | |

* As plain copper, the whole bump (two legs plus the top) is uncoupled. The top runs
  parallel to P but does not pair with it: N's own lead-in stands between them where
  their overlap starts, which is section B of the pairing spec. So P's span beside
  the top is uncoupled as well.
* As a pattern, **(a) the bump copper is excluded**: the `2h` and N's share go away.
  **(b) The partner copper beside it is not excluded**: the figure is exactly
  `B` = 0.4 or 4.0 at every height. The pattern swaps "bump + partner" for "partner
  alone", so the gain is only `2h`.
* Control: `ctl_straight` (two straight legs) reads 0.

## C. 45-degree (chamfered) bump at 0.9x

| Probe board | kicad-cli `actual` (mm) | Reading |
|---|---|---|
| `c_chamfer_h09_short_plain` | 0.6364 | only the two 45-degree legs (2 x 0.318); the top pairs with P |
| `c_chamfer_h09_long_plain` | 0.6364 | the same, independent of the top's length |
| `c_chamfer_h09_short_pattern` | 0.85 | P's span, B = 0.4 + 2h |
| `c_chamfer_h09_long_pattern` | 4.45 | P's span, B = 4 + 2h: 7x the plain figure |

A chamfered bump as **plain copper** is the cheapest shape measured that has no
partner membership. Its top pairs with P: the diagonal leg ends on the top's start,
so it covers the probe end (pairing-spec section B). The figure is only the legs,
about 2.83h, however long the bump. As a pattern the same bump reads B, which is
seven times as much on a 4 mm bump.

## D. The tuner's two-loop bump at 0.9x

| Probe board | kicad-cli `actual` (mm) | Reading |
|---|---|---|
| `d_two_loop_h09_plain` | 1.7 | 2 x (0.4 + 2h) |
| `d_two_loop_h09_pattern` | 0.8 | P beside the two loops (2 x 0.4); the on-line run between them still pairs |

## E. Partner straight vs jogging to follow the bump

P follows on the inside of N's long square bump at constant pitch. Its legs are
0.45 mm inside N's, and its top is 3.1 mm long against N's 4 mm.

| Probe board | kicad-cli `actual` (mm) | Reading |
|---|---|---|
| `e_follow_h09_plain` | 1.35 | N's legs (0.45) + N's top beyond P's shorter top (0.9) |
| `e_follow_h09_n_pattern` | 4.45 | N's bump out: P's whole jog (B + 2h) is beside nothing |
| `e_follow_h09_both_pattern` | 0.9 | both jogs are members: what is left is P's 2 x 0.45 straight run to its corners |
| `e_follow_h20_plain` | 1.8 | legs overlap 0.05 mm each and pair: 0.9 + 0.9 |
| `e_follow_h20_n_pattern` | 5.0 | P's whole jog, 4 + 2h |
| `e_follow_h20_both_pattern` | 0.9 | as at 0.9x |

A following partner does not help when only N's bump is a pattern. The figure is
worse than with a straight partner (4.45 vs 4.0), because more partner copper is
left beside nothing. With a straight partner (A/B) the figure is lower.

## F. Emission variants (square bump, 0.9x)

| Probe board | short (mm) | long (mm) | Reading |
|---|---|---|---|
| `f_*_split_plain` | 0.85 | 4.45 | P cut at the base-line ends, no pattern: same as A |
| `f_*_split_n_pattern` | 0.4 | 4.0 | P cut, N's pattern only: same as B |
| `f_*_no_coupled_baseline` | 0.4 | 4.0 | `base_line_coupled` omitted: same as B (the field has no effect) |
| `f_*_partner_span_same_pattern` | **0** | **0** | P's span is a member of N's pattern: excluded |
| `f_*_partner_span_own_pattern` | **0** | **0** | P's span is the only member of a second, `D_P` pattern: excluded |
| `f_*_mode_diff_pair_both` | **0** | **0** | `tuning_mode diff_pair` owning both: excluded |

* Cutting P on its own changes nothing. Neither `base_line_coupled` nor the tuning
  mode tells KiCad about the partner. Only membership does.
* The three member forms survive a `kicad-cli pcb upgrade` re-save with their
  members intact and still read 0
  (`test_partner_exclusion_survives_a_kicad_resave`).
* Membership does not take copper out of length. KiCad's `skew` figure is
  -0.45 mm (N 20.45, P 20.0) on `a_square_h09_long_plain`,
  `b_square_h09_long_pattern` and both `f_long_partner_span_*` boards
  (`test_members_still_count_in_skew`).

## G. Membership is what counts, not geometry

| Probe board | kicad-cli `actual` (mm) | Reading |
|---|---|---|
| `g_whole_partner_leg_member` | 16.0 | all of P is a member: N's 16 mm on-line copper is beside nothing |
| `g_partner_detour_plain` | 5.6569 | P detours 2 mm away from a straight N: its two 45-degree legs |
| `g_partner_detour_member` | 8.0 | the detour is a pattern on P: it is out, and N's 8 mm opposite it counts |

The rule the figures fit (inferred): a member track is neither uncoupled copper of its
net nor something the other net can pair with. So members on one net push the
uncoupled copper onto the other net, and the figure falls only where the two nets'
member copper lies exactly opposite. A member set that runs past its opposite leaves
the excess counted (`e_follow_*_both_pattern`, 0.9). One that covers a whole leg
uncouples the other leg (`g_whole_partner_leg_member`).

## If the operator accepts partner membership: what kct would emit

The emission is not implemented here; it belongs to #7591's rework.

1. **Cut the partner leg at the bump's base-line ends.** `skew_bump_pattern` already
   finds the two points as `base_line_coupled`. Split each partner segment that spans
   them, which on its own is neutral (`f_*_split_*`).
2. **Make the partner span a member.** The least novel form is the bump's own pattern
   (`f_*_partner_span_same_pattern`): one object per bump, tying the span to the bump
   it accompanies. The separate `D_P` pattern and `diff_pair` mode read the same.
   `Route.to_sexp` writes one pattern per route today, so cross-net members need the
   board-level writer to collect member uuids from both routes.
3. **Make the member sets exact opposites.** The partner members must be exactly the
   partner copper between those two points, and the tuned-leg members exactly the
   bump (`bump_members`, unchanged). Anything extra counts against the other net
   (group G). The probes only cover a straight partner across the span. A partner
   that bends, changes layer or carries a via inside the span has not been measured.
4. **No measure change.** kct's `measure_pair` already reads every probe as
   kicad-cli does (`test_kct_matches`), including the partner-member forms. The
   router's guard (`_pair_uncoupled_mm`) agrees with it (#7521).
5. **Side effects to cover in kct.** Member copper is out of KiCad's gap check (as
   measured in #7521, `tuning_pattern.py`). So with partner membership, KiCad would
   check the gap on neither leg across the bump, and kct's own Q27 excursion gate
   becomes the only check there. Membership leaves length and skew unchanged
   (`test_members_still_count_in_skew`).
6. **Not measured.** How pcbnew's GUI treats a cross-net or zero-amplitude pattern
   when a user edits or regenerates it. kicad-cli has no regenerate command, and
   `pcb upgrade` leaves the patterns alone.

With these changes, every compliant bump in a pair adds 0 to KiCad's uncoupled
figure, whatever its width. So the bump-shape part of the 18-of-20 rise on
06b/06rf/07rf (#7591 review, e.g. LVDS1 7.92 -> 11.80 mm) should disappear. The
board figures themselves are the re-measure step of Q31 and are not measured here.

## If it does not: other measured options

* **Chamfered plain copper** (group C) reads about 2.83h per bump, independent of
  width. At 0.9x gap that is 0.64 mm, against B (0.4 to 4+ mm) for a pattern. It
  needs no partner membership. But it is not a pattern, so KiCad cannot tell it from
  an accidental excursion, which is what Q16 set out to fix.
* Today's N-only pattern (group B) is cheaper than square plain copper by `2h`, and
  dearer than chamfered plain copper whenever B > 2.83h.
