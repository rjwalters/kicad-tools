# Opt-in `kct route` flags: graduation and deprecation rule

Most advanced `kct route` features are off unless a flag is passed. An opt-in
flag that nobody passes costs maintenance (every tail must carry it) and
hides behaviour from users. This note says when a flag should graduate to a
default and when it should be deprecated. It records the rule and the current
inventory; it does not change any default or remove any flag (removal is the
operator's call).

## Rule

A flag is judged on three observable signals:

1. **Recipe use**: at least one `boards/*/generate_design.py` (or other board
   recipe) passes it.
2. **ON-test**: a test drives the flag through the CLI on the default
   (`--auto-layers`) entry path, not only a unit test of the underlying class.
   Flags that only act on the single-attempt path are covered by
   `tests/test_route_entry_path_goldens.py` and
   `tests/test_cli_route_single_tail_flags.py`.
3. **No regression in an A/B**: a fleet A/B (recipe boards, flag on vs off)
   shows no routed-quality regression.

| Verdict | Condition |
|---|---|
| **Graduation candidate** | Signals 1, 2 and 3 all hold. Propose the default flip in its own issue with the A/B attached. |
| **Keep opt-in** | Some signal holds but not all, or the flag is `EXPERIMENTAL` or newer than two releases. |
| **Deprecation candidate** | No recipe passes it and it has no CLI ON-test after two releases. Deprecate with a warning first; remove a release later. |

Diff-pair and pair-feature flags are frozen by
[#6448](https://github.com/rjwalters/kicad-tools/issues/6448) (unify the
diff-pair engines). They are listed here but are not graduated or deprecated
until #6448 decides; this note does not re-decide it.

## Inventory

Scanned on the tree at the time of writing (2026-10-21). "Boards" are
`boards/<board>/` recipe directories that mention the quoted `--flag`;
"scripts/CI" counts files under `boards/`, `scripts/` and `.github/`; "tests"
counts files under `tests/` mentioning the flag or its attribute name. Counts
are a plain text scan (a mention is not proof of an ON-test), so re-run it
before acting on a verdict:

```bash
grep -rl -- "--<flag>\b" boards scripts .github   # recipes and CI
grep -rlE -- "--<flag>\b|<flag_attr>" tests        # tests
```

| Flag | Boards | Scripts/CI | Tests | Status |
|---|---|---|---|---|
| `--deterministic-budget` | 01-07 | 17 | 39 | Graduation candidate |
| `--micro-via-in-pad-fallback` | 05, 07 | 6 | 12 | Graduation candidate |
| `--length-match-groups` | 07 | 6 | 13 | Graduation candidate |
| `--placement-delta-feedback` | 07 | 4 | 17 | Graduation candidate |
| `--placement-feedback` | 04, 05, 07 | 6 | 20 | Graduation candidate |
| `--escape-corridor-reservation` | 05 | 3 | 3 | Keep opt-in (thin test) |
| `--strict-layers` | 06, 07, 09 | 8 | 18 | Graduation candidate |
| `--strict-pad-clearance` | 07, 09 | 6 | 14 | Graduation candidate |
| `--strict-in-pad-clearance` | 09 | 4 | 6 | Keep opt-in |
| `--differential-pairs` | 03, 05, 07 | 10 | 49 | Frozen (#6448) |
| `--length-match-diffpairs` | none | 0 | 3 | Frozen (#6448) |
| `--targeted-ripup` | 07 | 2 | 9 | Keep opt-in |
| `--bundle-river-planner` | 07 | 2 | 4 | Frozen (#6448) |
| `--monotone-certificate-order` | none | 2 | 5 | Frozen (#6448) |
| `--cross-package-pair-corridor` | none | 0 | 3 | Frozen (#6448) |
| `--slack-corridor-widening` | none | 0 | 3 | Frozen (#6448) |
| `--tighten-diffpair-gap` | 07 | 2 | 1 | Keep opt-in (new; #7349 pending) |
| `--via-in-pad-last-resort` | none | 0 | 9 | Keep opt-in |
| `--stitch-power-planes` | none | 0 | 2 | Deprecation candidate if no recipe adopts it |
| `--progressive-clearance` | none | 0 | 3 | Deprecation candidate; single-attempt path only |
| `--bus-routing` | none | 0 | 2 | Deprecation candidate; single-attempt path only |
| `--nogood-cache` | none | 1 | 1 | Keep opt-in (EXPERIMENTAL) |
| `--fallback-bidirectional` | none | 0 | 8 | Keep opt-in (EXPERIMENTAL) |
| `--batch-routing` | none | 0 | 8 | Already a deprecated no-op |

`--progressive-clearance` and `--bus-routing` are honoured only by the
single-attempt path; the escalation ladders warn once on stderr that they are
ignored (`_warn_escalation_flags_ignored`).
