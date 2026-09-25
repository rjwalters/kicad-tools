---
name: loom-cargo-target-isolation
description: "**Load when**: you are about to run (or just ran) a local `cargo build`/`cargo test`/`cargo run` whose result will inform a verdict — a Judge approval or rejection, a Doctor \"the fix works\", or a Builder \"tests pass\" before opening a PR."
---
<!-- loom-managed-skill -->
<!-- GENERATED FILE — DO NOT EDIT DIRECTLY.
     Produced by `loom-daemon generate-agent-skills` from
     defaults/.claude/commands/loom/cargo-target-isolation.md (the same source Claude Code
     reads as /loom:cargo-target-isolation via .claude/commands/loom/). This is the
     cross-vendor skill-discovery surface (.agents/skills/<name>/SKILL.md)
     read natively by Codex, Kimi Code, Mistral Vibe, and Grok — see
     runtime-adapters.md §5. To change this file, edit the source above
     and re-run the generator; CI (`loom-daemon generate-agent-skills
     --check`) fails if this file is stale. -->

# Shared Cargo Target Dir: Isolation Recipe (#8457)

**Load when**: you are about to run (or just ran) a local `cargo build`/`cargo
test`/`cargo run` whose result will inform a verdict — a Judge approval or
rejection, a Doctor "the fix works", or a Builder "tests pass" before opening
a PR.

## Why this exists

A fleet host with one shared cargo target directory uplifts every workspace
binary to a single un-hashed path (e.g. `target/debug/loom-daemon`). Cargo
overwrites that path whenever another worktree builds concurrently. An
integration test that executes it can therefore observe **a different
worktree's binary**, silently. On 2026-09-20 this produced three real
false-verdict incidents in one day: a Judge saw 12 false failures from a
mid-run overwrite (an isolated re-run passed 194/194), a Doctor's
`cargo test --bin loom-daemon` reported results for a binary that, per
`strings`, contained none of that PR's code, and a deliberately
behavior-flipped local canary build landed at the shared path while Judges
concurrently tested the real PR.

**A shared-target-dir integration-test result is not verdict-bearing
evidence.** Treat a pass or a fail observed there as uninformative — not as
grounds to approve, reject, or declare a fix confirmed — until you have
reproduced it from an isolated build.

## The recipe: a private `CARGO_TARGET_DIR`

Run the build/test, and its cleanup, in a **single Bash tool call** (this
matters for the cleanup step below):

```bash
CARGO_TARGET_DIR="$(mktemp -d)"
export CARGO_TARGET_DIR
cargo test -p loom-daemon --test the_test_you_need   # or the scoped command you'd otherwise run
rm -rf "$CARGO_TARGET_DIR"
```

This is a full rebuild into a directory nobody else touches (3.5-11 GB,
several minutes) — worth it because a wrong verdict costs more than a rebuild.
Only the result of a run against a private `CARGO_TARGET_DIR` (or a repo's own
per-worktree target dir, once the structural fix in the parent issue lands) is
verdict-bearing.

## Cleanup depends on how you do it (`rmScope`)

The `rmScope` guard (`defaults/docs/guard-hooks.md` → "Repo-Scoped rm Guard")
is repo-scoped by default: it **denies** (a hard block, not an ask) an `rm -rf`
whose target cannot be proven to resolve inside the repo/worktree or a known
ephemeral temp root. Verified directly against the guard (not assumed) — two
shapes are safe, one is not:

- **Same-Bash-call cleanup (preferred, the recipe above)**: a bare
  `rm -rf "$NAME"` is allowed when the *same command* also contains exactly
  one assignment `NAME="$(mktemp -d)"` (or `mktemp` with no template/prefix)
  and nothing else reassigns `NAME` — the guard's
  `rm_scope_mktemp_same_command_safe()` fast path (#6520) proves the target is
  `/tmp`-or-`$TMPDIR`-rooted and skips the scope check entirely.
- **Cleanup in a later, separate Bash call, by literal path**: `mktemp -d`'s
  default output root (`/tmp`, `/var/tmp`, or `$TMPDIR`) is on the guard's
  built-in **ephemeral allowlist** — so `rm -rf /tmp/tmp.AbC123` (the actual
  printed path, not the `$VAR` reference) is allowed even from a later call
  with no same-command assignment. Print the path once
  (`echo "$CARGO_TARGET_DIR"`) so you have it verbatim if creation and cleanup
  end up split across turns.
- **Not safe**: a bare `rm -rf "$CARGO_TARGET_DIR"` in a call that does *not*
  also contain the mktemp assignment. The guard cannot resolve what a prior,
  separate call bound the variable to, and denies unconditionally
  (`rm-scope-unresolved-var`, "unexpanded shell variable ... fail closed").

Do not work around the guard to force an unresolved-variable cleanup through.
If you end up with a target dir you cannot remove by either safe shape above,
say so explicitly in your output — a stated, small `/tmp` leak is better than
a silent one.

## Not the same fix as `require-daemon-bin.sh` or a per-worktree target dir

Two adjacent, narrower mechanisms already exist and this recipe does not
replace either:

- `.loom/docs/verification-recipes.md` → "One shared build directory makes
  'the binary under test' ambiguous" and `tests/lib/require-daemon-bin.sh`
  protect **stub-driven shell suites** that reference a pinned binary by path
  — they resolve the freshest candidate and copy it to a private per-suite
  path before pinning it. That covers suites built on that harness; it does
  not cover a Judge/Doctor/Builder's own direct `cargo test`/`cargo build`
  invocation of `loom-daemon/tests/*.rs`, which is what this recipe is for.
- A genuine **per-worktree** `CARGO_TARGET_DIR` (so builds never share a
  target dir at all) is the structural fix and a separate, larger sub-issue
  of #8453 — this recipe is the stopgap until it lands.
