# Declaring your required checks' inputs (`.loom/stale-check-inputs.json`)

`merge-pr.sh` refuses to merge on a green required check whose result may no
longer hold on the tree the PR will actually merge onto (the required-check
freshness guard, #8248/#8919). To decide that, it has to know **which files
each check reads**. Loom knows this for its own required checks. It knows
nothing about yours.

A required context the guard has no input set for is treated as **stale on any
base move**. In a busy repo, every merge then re-stales every other open PR,
each one needs a fresh CI run (a re-date push) before it can merge, and an
approved queue cannot drain. `2AMLogic/klayout-tools` hit this on 2026-09-30
with 20 approved PRs and a `Lint (ruff)` context (#9589).

To fix it, commit `.loom/stale-check-inputs.json` to your default branch.

## Format

```json
{
  "version": 1,
  "checks": {
    "Lint (ruff)": {
      "global": ["pyproject.toml", "ruff.toml", ".github/workflows/ci.yml"],
      "scanned": ["**/*.py"]
    },
    "Link Check": {
      "global": ["scripts/check-links.sh", ".github/workflows/ci.yml"],
      "coupled": ["**/*.md"],
      "removal_sensitive": true
    }
  }
}
```

Each key under `checks` is a required status-check context, spelled exactly as
branch protection or the ruleset names it. Each check has these fields:

| field | meaning |
|---|---|
| `global` (required, non-empty) | Files whose change can flip the verdict for **every** file: the check's own workflow, its scripts, its config, baselines and allowlists. |
| `scanned` | Files the check judges **one at a time**. A file's verdict depends only on its own content plus `global`, like a linter or formatter. |
| `coupled` | Files judged **together**: cross-file aggregates (a total over a set) and links (one file naming another). |
| `removal_sensitive` | `true` if deleting or renaming a path on one side can break the check while the other side touches `coupled`, as with link checkers. Defaults to `false`. |

Patterns are repo-relative, use `/`, and support only `*` (within one path
segment) and `**` (zero or more whole segments). `?`, `[…]`, `{a,b}` and `!`
are rejected, because the guard would match them literally, match nothing, and
silently narrow the check.

## How a declared check is judged

The guard applies the same predicate it uses for Loom's own checks. Here `D` is
what the base branch changed since the base the check tested, and `P` is the
PR's own diff. The check is stale if any of these holds:

- `D` touches `global` and `P` touches anything the check reads, or the other
  way round;
- both sides touch the **same** `scanned` file;
- both sides touch `coupled`;
- with `removal_sensitive`, one side deletes or renames a path while the other
  side touches `coupled`.

So an unrelated base move no longer re-stales your lint check, and neither do
edits to *different* `.py` files on the two sides. The `**/*.py` rule stays
per-file. Every entry is safe to **over**-populate: listing a path too broadly
only makes the guard refuse more often. Listing too little lets a stale green
through, so when in doubt, put a file in `global`.

## Fail-closed rules

- **No file, or a context not listed:** that context keeps today's "stale on
  any base move" behaviour. The guard never guesses a default, such as your
  workflow's `paths:` filter. Path filters are a CI optimisation, not a
  statement of what a check reads ([ci-principles](ci-principles.md) rules 3
  and 9).
- **Read from the base branch tip, not the PR.** A PR cannot narrow its own
  freshness check. The declaration file itself is an implicit `global` input
  of every declared check, so editing it on either side re-stales them all.
- **Any doubt rejects the whole file:** invalid JSON, an unknown field (typos
  like `scaned` are caught), a duplicated context, `version` other than `1`, an
  empty context name or `global`, a bad pattern, or any read error other than
  404. A rejected file behaves as if absent, and `merge-pr.sh` prints a
  `Warning:` naming the file and the reason.
- **Loom's built-in specs win.** A context Loom already describes (its own
  `Structural Checks`, `Daemon Checks`, …) is never re-read from your file.
- **`ci.yml` counts as a whole file** for a declared check. Loom narrows its
  own `ci.yml` edits to the job blocks they touch, but that mapping only knows
  Loom's jobs.

## Checking a declaration offline

`loom-daemon merge-pr stale-checks --from-stdin` accepts the declaration text
as an optional `stale_check_inputs` string next to `pr_files` and `base_moves`.
Paste a real PR's inputs to see the verdict before you commit the file.

Related: re-verifying deterministic ratchets on the merged tree instead of
demanding a new CI run is tracked in #9571. That handles real staleness; this
file removes false staleness.
