# 02 — Clearance and Net Classes

A differential pair has **two** clearances: the gap between the two halves of
the pair (intra-pair) and the gap between the pair and everything else
(inter-pair).

## Two fields, one effective accessor

`NetClassRouting` (`src/kicad_tools/router/rules.py`) exposes both:

| Field | Meaning |
|---|---|
| `clearance` | Inter-net clearance, applied between this class's nets and everything else. |
| `intra_pair_clearance` | Within-pair clearance, applied only to the two halves of a declared pair. `None` → falls back to `clearance`. |

Read `intra_pair_clearance` via the accessor, never the field directly:

```python
from kicad_tools.router.rules import NetClassRouting

nc = NetClassRouting(name="USB", clearance=0.15, intra_pair_clearance=0.075)
assert nc.effective_intra_pair_clearance() == 0.075  # explicit override

nc2 = NetClassRouting(name="USB", clearance=0.15)
assert nc2.effective_intra_pair_clearance() == 0.15  # fallback to clearance
```

The `None` sentinel encodes "fall back to `clearance`" — it is NOT a literal
zero clearance. This preserves backward compatibility with pre-#2557 configs
that only set `clearance`.

## The gap the router lays

The class value is a request. The router lays a coupled pair at
`DesignRules.pair_gap(net_class)` (Issue #6443): the largest of the class
intra-pair gap, the manufacturer minimum clearance and the input project's
authored `rules.min_clearance`. KiCad holds a pair's own P and N to that
minimum too, so a gap below it fails as `diff_pair_gap_out_of_range` and
`clearance`. `kct route` prints one `Diff-pair gap:` line per class a floor
raised; the board minimum is never lowered to fit the pair.

The routed `.kicad_dru` then carries one `diff_pair_gap (min <gap>)` rule per
coupled pair, in a managed block, conditioned on the two net names. Only pairs
KiCad pairs by name (trailing `+`/`-` or `P`/`N`) get one: kicad-cli 10.0.6
ignores the rule for `X_POS`/`X_NEG` whatever its condition, so those gaps are
checked by kct alone.

## Opt-in coupled routing

A pair is detected (guide 01) but is **not** routed as a coupled pair unless
the net class opts in:

```python
NetClassRouting(
    name="HighSpeed",
    intra_pair_clearance=0.075,
    coupled_routing=True,  # opt-in to CoupledPathfinder
)
```

`coupled_routing` (in `src/kicad_tools/router/rules.py`) defaults to `False`.
When `False`, the pair's halves are still routed *as a pair* by the diff-pair
dispatch — the intra-pair clearance is honored at the pathfinder layer — but
the `CoupledPathfinder` geometric coupling is bypassed. Use this for hobby
boards where tight clearance suffices without forcing coupled geometry.

### Canonical opt-in: `NET_CLASS_HIGH_SPEED`

The pre-configured `NET_CLASS_HIGH_SPEED` in `src/kicad_tools/router/rules.py`
already has `coupled_routing=True` and `intra_pair_clearance=0.075`. Opt nets
into it via `Autorouter(..., high_speed_nets=[...])` instead of redeclaring the
class.

## Manufacturer profiles

The manufacturer profile (e.g. `--mfr jlcpcb`) supplies a *minimum* inter-pair
clearance. `intra_pair_clearance` is allowed to be **tighter** than that
floor — the validation rule `diffpair_clearance_intra` (guide 06) checks the
intra-pair gap against the per-class field, not against the manufacturer
floor.

## Via-in-pad coverage

Via-in-pad (BGA escape, fine-pitch SSOP) interacts with intra-pair clearance
when the via lands inside a coupled launch. See `ViaInPadRule` and Issue
#2637 for the full coverage matrix — not duplicated here.

## See also

- [03-impedance-and-sizing.md](03-impedance-and-sizing.md) — letting
  `target_diff_impedance` drive `intra_pair_clearance` automatically.
- [06-drc-rules.md](06-drc-rules.md) — `diffpair_clearance_intra` checks the
  routed gap against `effective_intra_pair_clearance()`.
