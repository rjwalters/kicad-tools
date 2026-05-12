# 04 — Cascade Safety (Why the Tuner Gives Up)

The match-group tuner inserts serpentines until every member is within
`length_match_tolerance_mm` of the reference — **bounded** by three
constants in `src/kicad_tools/router/match_group_tuning.py`:

| Constant | Value | Source line |
|---|---|---|
| `MAX_INSERTS_PER_GROUP_MEMBER_SMALL=3` | per-member budget for groups with `N <= 4` | `match_group_tuning.py:153` |
| `MAX_INSERTS_PER_GROUP_MEMBER_LARGE=2` | per-member budget for groups with `N > 4` | `match_group_tuning.py:161` |
| `MAX_TOTAL_INSERTS_PER_GROUP=16` | absolute cumulative ceiling across the group | `match_group_tuning.py:169` |

## Why the bounds exist

Each trombone insertion adds cumulative perturbation:

- A 10-net DDR byte at the LARGE budget can produce up to 20 inserts;
  the 16 ceiling caps the worst case at roughly 1.6 inserts/member.
- Dense boards with multiple groups produce 2D packing collisions
  (group A's serpentine intrudes on group B's keep-out). The bounds
  bound the radius of perturbation per group.
- Without the ceiling, a single hard-to-converge member can starve
  every other group on the board.

The MIPI/HDMI lane case (guide 03) is doubly affected: symmetric
inserts count as **two** against the per-member budget — a 4-lane MIPI
group with N=4 (LARGE not yet triggered) still hits the SMALL cap
after just 1–2 symmetric inserts per lane.

## When the tuner gives up: `reason` field

`TuneResult.reason` reports why a member could not reach tolerance.
Defined at `match_group_tuning.py:608-722`:

| Reason | Meaning |
|---|---|
| `"tuned"` | success — within tolerance |
| `"exceeded_max_inserts"` | per-member budget exhausted before reaching tolerance |
| `"cascade_budget_exhausted"` | group-level ceiling (`MAX_TOTAL_INSERTS_PER_GROUP`) hit |
| `"post_insertion_drc_violation"` | candidate serpentine would violate intra-group or neighbour clearance; rolled back |
| `"no_suitable_segment"` | member has no segment long enough to host any trombone amplitude |
| `"not_length_critical"` | engagement gate fired: `length_critical=False`, no change |
| `"unrouted"` | member was not in `routes_by_net` (never routed) |

## Remediation when the tuner gives up

1. **Loosen tolerance** — raise `length_match_tolerance_mm`. The
   "right" number is usually the protocol skew budget minus a margin
   for stackup tolerance; if you set it tighter than the routing
   density supports, the tuner will refuse to converge.
2. **Reduce N** — split a DDR data byte into upper/lower nibbles
   (DQ0-3 and DQ4-7 as two groups). Halves the cumulative perturbation
   budget pressure.
3. **Loosen clearance** — `post_insertion_drc_violation` means the
   serpentine collides with a neighbour. Raise `clearance` on the
   class or reduce trace width.
4. **Improve placement** — `no_suitable_segment` means the routed
   path has no straight run long enough for a trombone. Move pins
   or relocate the IC to add headroom.
5. **Accept the violation** — for non-safety-critical buses, the
   `match_group_length_skew` DRC rule (guide 06) can be filtered out
   with `--skip=match_group_length_skew` once you've documented the
   waiver.

## See also

- [02-reference-selection.md](02-reference-selection.md) — the
  pace-car policy refuses to perturb the reference, which can push a
  member into `exceeded_max_inserts`.
- [06-drc-rule.md](06-drc-rule.md) — `match_group_length_skew` fires
  on whatever residual skew remains after the tuner gives up.
