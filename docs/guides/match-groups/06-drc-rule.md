# 06 — `match_group_length_skew` DRC Rule

The DRC rule `match_group_length_skew` (Issue #2702, Epic #2661
Phase 2G) validates the routed lane-to-lane skew of every detected
match group. Source:
`src/kicad_tools/validate/rules/match_group_length_skew.py:153`.

## What it detects

For every group, computes `max_length - min_length` (or
`|L_member - L_reference|` when an explicit reference is set) and
compares against `length_match_tolerance_mm` on the group's net class.
Fires one violation per out-of-tolerance member.

```bash
kct check routed.kicad_pcb --rules=match_group_length_skew
```

```bash
# Bundled into the manufacturer profile
kct check routed.kicad_pcb --mfr jlcpcb
```

## Producer-side requirement (Phase 2.5G)

The rule is **a no-op unless skew data is available** — it cannot
re-derive group membership from the PCB alone. Two producer-side
paths feed the rule:

1. **In-process** — `MatchGroupTracker` populated during routing
   (`Autorouter.update_match_group_skew` at
   `src/kicad_tools/router/core.py:7350`). Used by the `kct route`
   pipeline.
2. **Standalone** — the `--net-class-map` JSON sidecar. The
   `derive_group_skew_data` helper at
   `src/kicad_tools/validate/match_group_skew.py:77` re-derives
   group membership and per-net lengths from the routed PCB +
   sidecar map.

Without either, `check_match_group_length_skew` returns zero
violations (a no-op). This is the Phase 2.5G short-circuit; do not
interpret "0 violations" as "no skew" if you haven't supplied the
sidecar.

## Rule ID

The public string is `match_group_length_skew` — exactly the
`ViolationType.MATCH_GROUP_LENGTH_SKEW` enum value at
`src/kicad_tools/drc/violation.py:157`. The enum is aliased
explicitly in the `from_string` map at
`src/kicad_tools/drc/violation.py:299`:

```python
ViolationType.from_string("match_group_length_skew") is ViolationType.MATCH_GROUP_LENGTH_SKEW
```

The alias entry is required (the fuzzy fallback in `from_string`
keys off the substring `"clearance"` and would otherwise drop a skew
rule_id through to `UNKNOWN`). The `#2521` precedent applies here
verbatim — the same gotcha that bit `diffpair_length_skew`. The
drift-prevention test at
`tests/test_validate_match_group_length_skew.py` guards the round-trip.

## Remediation when fired

The violation's `details` field reports the offending member, its
length, the reference length, and the excess. Three responses:

1. **Re-route with `--length-match-groups`** — the tuner inserts
   serpentines to converge (see guide 07).
2. **Loosen tolerance** — raise `length_match_tolerance_mm` on the
   class if the protocol budget allows.
3. **Loosen cascade safety** — see guide 04 for the
   `MAX_INSERTS_PER_GROUP_MEMBER_*` constants and the trade-offs.

## See also

- [04-cascade-safety.md](04-cascade-safety.md) — the rule fires on
  residual skew that the tuner could not eliminate.
- [07-cli-and-sidecar.md](07-cli-and-sidecar.md) — the standalone
  `--net-class-map` workflow.
