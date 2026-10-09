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

## The recipe: the `CARGO_TARGET_DIR` Loom gave you

Decide once, before your first verdict-bearing build:

1. **`$CARGO_TARGET_DIR` is set to a Loom-owned dir**: a path under
   `<repo>/.loom/targets/`, or the per-worktree dir named in your worktree's
   `.loom-cargo-target-dir`. Use it as given. Loom created it for this run
   and reclaims it (see Cleanup). Do not export another or delete it.
2. **Otherwise** (unset, a shared cache, an operator value, or you are one of
   several agents building concurrently in one session, which all inherit the
   same value): build into your worktree's own `target/`.

   ```bash
   CARGO_TARGET_DIR="$WORKTREE_ABS/target" cargo test -p loom-daemon --test the_test_you_need
   ```

   Nobody else builds there, and it is removed with the worktree.
3. **Never create one anywhere else.** No `mktemp -d`, nothing under `/tmp`,
   `$TMPDIR`, `~`, `~/.cache`, or `<repo>/.loom/target-*`. Nothing owns those
   paths, so a failed or interrupted run leaks the whole build: 85 GB of
   `.loom/target-*` on one fleet host and 45 GB of `/tmp/cargo-target-*` on
   another (#8370). The daemon's orphan sweep reclaims them only hours later.

A private dir means a full rebuild (3.5-11 GB, several minutes; sccache
softens it). That is worth it, because a wrong verdict costs more than a
rebuild. Only a result from one of the two dirs above is verdict-bearing.

## Cleanup

There is none for you to do. A case-1 dir is removed at run end only on a
role-runner tick (the daemon's periodic `/loom:<role>` dispatch), once the
harness and its process group have exited. For a daemon sweep spawn or a
manual `spawn-worker.sh` nothing waits on the run, so the daemon's orphan
sweep collects the dir 3 h or more after its owner exits. A case-2 dir goes
with the worktree. Do not background a build and end your run: it keeps the
dir until the sweep. If
you find a leftover dir that an earlier
run created under `/tmp` (from before this recipe), remove it by its literal
printed path (`rm -rf /tmp/cargo-target-123`). The `rmScope` guard
(`defaults/docs/guard-hooks.md`) allows `/tmp` and `$TMPDIR` literals and
denies an `rm -rf "$VAR"` it cannot resolve. Do not work around it.

## Not the same fix as `require-daemon-bin.sh` or a per-worktree target dir

Two adjacent, narrower mechanisms already exist and this recipe does not
replace either:

- `.loom/docs/verification-recipes.md` → "One shared build directory makes
  'the binary under test' ambiguous" and `tests/lib/require-daemon-bin.sh`
  protect **stub-driven shell suites** that reference a pinned binary by path
  — they pin a private copy of this checkout's own build (that recipe has the
  rule). That covers suites built on that harness; it does
  not cover a Judge/Doctor/Builder's own direct `cargo test`/`cargo build`
  invocation of `loom-daemon/tests/*.rs`, which is what this recipe is for.
- The structural fixes are the per-worktree dir a claim-owning sweep gets
  when the repo opts in (#8458) and the Loom-owned per-run dir every other
  role run gets (#8370). This recipe tells you which one you have and what
  to do when you have neither.
