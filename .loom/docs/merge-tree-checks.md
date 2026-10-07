# Pre-merge tree checks (`merge.treeChecks`, #10026)

PR CI tests "base + PR". It never tests "base + PR + every sibling PR that
merged since", so two PRs that each touch a shared surface (two migrations
taking the same numeric prefix, an exhaustive switch or mirrored vocabulary a
sibling extended) are green alone and red together; the collision surfaces only
on the push-to-main run, one merge too late.

`merge-pr.sh` is the one sanctioned merge path and the only point that can see
the real merge tree, so a repo may declare cheap checks to run against it.

## Configure

In `.loom/config.json`:

```json
{
  "merge": {
    "treeChecks": ["scripts/check-migration-prefixes.sh", "npm run typecheck"],
    "treeChecksTimeoutSecs": 120
  }
}
```

- **Unset or `[]`: strict no-op.** No daemon call, no fetch, no worktree, no comment.
- Each entry runs through `sh -c`, in order, with the merge tree as cwd. The
  first failure stops the run. `treeChecksTimeoutSecs` (default 120) bounds each
  check; a timeout is a failure.
- The environment is scrubbed to `PATH`, `HOME`, `CI=1` and
  `LOOM_MERGE_TREE_CHECK=1`: no forge tokens reach a check. Commands come from
  the repo's own committed config, trusted like any other config.
- The command list is read from the **merging checkout's** config
  (`merge-pr.sh` passes its own `.loom/config.json` to the daemon), never from
  the PR: a PR cannot turn the gate off by editing its copy. But a check that
  runs a repo script runs **the merge tree's** copy of it, i.e. the PR's code,
  on the host doing the merge, with that user's `HOME`. That is the same trust
  a Judge gives a PR when it runs its tests locally; do not opt in a repo that
  takes PRs from authors you would not run code from.

## What happens

`loom-daemon merge-pr tree-checks` fetches the base branch and `refs/pull/<N>/head`,
verifies the fetched head is the head being merged, builds the merge tree
(`git merge-tree --write-tree`, base + PR head), extracts it into a temporary
directory (removed on every exit path) and runs the checks there. The primary
checkout and issue worktrees are never touched.

| Result | Behaviour |
|---|---|
| all pass | merge proceeds |
| a check fails | `merge-pr.sh` exits non-zero before the merge API is called, prints the check's real output, and posts a PR comment naming the failing check |
| tree cannot be built / check cannot run (fetch failure, conflict, head moved, missing or older `loom-daemon`, malformed config) | refused, fail closed |
| `--allow-red-tree` | for a **failing** check: warns and records an audit comment, then proceeds (like `--allow-unapproved`). It does not override a gate that cannot run |
| `--dry-run` | reports the would-block; posts no comment |

## What belongs in the gate

Only checks that **must see the merge tree** and **finish in seconds**: prefix
or uniqueness collisions, exhaustive-switch/vocabulary parity, a typecheck. The
full test suite stays on the push-to-main run; every merge pays this gate's
latency, and a slow gate stalls the queue.

## Dependencies must exist where `merge-pr.sh` runs

A merge tree is a clean extract, so untracked dependency directories are absent.
If the checkout running `merge-pr.sh` has a `node_modules/`, it is symlinked into
the temp tree, which is what makes `npm run typecheck`-style steps work. So
`node_modules` (or whatever your checks need on `PATH`) **must already exist
wherever `merge-pr.sh` runs** (loom-ui#1042). A check that cannot find its tools
fails, and the merge is refused.

Because the gate is opt-in per repo, there is no daemon version floor for repos
that do not set `merge.treeChecks`; once set, a host whose `loom-daemon`
predates the `tree-checks` verb refuses merges until it is rolled.

## Stale cheap checks re-verified on the merge tree (#10388)

This is a separate mechanism from `merge.treeChecks`. It uses the same
merge-tree machinery to satisfy the required-check freshness guard (#8248)
without a CI round trip. It is **on by default** (#10465) for the
toolchain-free allowlisted checks (conflict markers first); a repository opts
out by setting the flag to `false`.

| Setting | Values | Default |
|---|---|---|
| env `LOOM_MERGE_REVERIFY_STALE_CHECKS` | `1`/`true`/`yes`/`on`, `0`/`false`/`no`/`off` | — (falls through) |
| config `merge.reverifyStaleChecks` | `true` / `false` | `true` |

Precedence is env > config > default. An unparseable value falls through to
the next tier. When the flag is off (explicitly), `stale-checks` behaves exactly as before:
no fetch, no temp dir, no extra output.

**Version floor (#10465).** The flag only has an effect on a merging host whose
`loom-daemon` is >= 0.19.741 (`_MP_REVERIFY_FLOOR` in `merge-pr.sh`). An older
binary ignores it and falls back to re-dates, so with the flag on (explicitly or by default) `merge-pr.sh`
prints one warning per invocation naming the host, the resolved daemon version
and the floor. The warning never changes the exit code; roll the host with
`cli/loom-daemon-update.sh --fetch`. `loom-daemon guards status` reports the
same condition as `REVERIFY-DAEMON-TOO-OLD`. An explicitly disabled flag is
silent. The feature stays fail-open: no `requires-daemon` floor is raised.

When it is on, `loom-daemon merge-pr stale-checks` finds required checks
stale, and **every** stale component of every stale context is on the cheap
allowlist (`local_eval::CHEAP_CHECKS`), it does the following:

- Builds the merge tree of the **base tip it judged** and the PR head. If the
  fetched base has moved since, the result is no-verdict. The fetch writes no
  `FETCH_HEAD`, and its private refs are deleted again. The primary clone's
  refs, index and worktrees are left as they were.
- Diffs that merge tree against the base tip (`git diff <base> <tree>`). If
  it changes `ci.yml` or any `*.sh`, the result is no-verdict. Only check code
  already on the base ever runs on the merging host. This diff comes from git
  objects, not the forge's file list, which GitHub caps at 3000 files without
  signalling truncation.
- Checks that tree out into a temporary git repository (objects borrowed via
  `alternates`, so `git ls-files` works as it does in CI). It is removed on
  every path.
- Runs each component's steps as read from **that tree's own `ci.yml`**, under
  the component's `# component:` marker. Each step runs as
  `bash --noprofile --norc -eo pipefail <file>`.

The allowlist covers toolchain-free gates that finish in seconds: Conflict
Marker Check, File Size Ratchet, Markdown Token Ratchet, Role Prompt Prefix
Ratchet, Docs/Defaults Parity Check, Doc Table-of-Contents Freshness, Vendored
Private-Reference Scrub, and Dangling Link Check. Dangling Link Check needs
`lychee` on `PATH` at **exactly** the version `ci.yml`'s install step pins
(`VER=`); any other version is no-verdict.

| Result | Behaviour |
|---|---|
| all pass | `LOOM-STALE-CHECKS-CLEAN`; a `LOOM-MERGE-TREE-REVERIFY … verdict=pass` line (base, head, tree, per-component result) on stderr, which is the merge log; a PR comment marked `<!-- loom:merge-tree-reverify base=… head=… tree=… -->` listing each step's command, exit code and duration; no re-date |
| a step fails | the #8248 refusal stands; the failure is printed and posted |
| no verdict | the refusal stands, and `--redate-stale-checks` runs as before; a `Warning:` on stderr names the reason, and no comment is posted |

No verdict covers:

- a merge tree that changes `ci.yml` or any `*.sh`;
- a stale non-allowlisted component (anything needing cargo), a time-rule or
  unknown verdict, or a repo-declared spec;
- a missing script or tool, or a pinned tool at another version;
- a step the reader cannot run faithfully: `uses:`, `env:`, `${{ }}`, a
  non-trivial `if:`, or a denied command (`curl`, `wget`, `sudo`, `gh`,
  `cargo`, `loom-daemon`, `npm`/`pnpm`/`node`, `pip`, anything under
  `target/`, …), checked on every read of the merge tree's `ci.yml`;
- a merge conflict, a moved base or head, or a timeout;
- a host-environment failure: exit 78 or 127, a signal, or a failing
  `--self-test` step. These are the host, not the PR, so they never post
  "Merge blocked".

Also:

- `merge-pr.sh --dry-run` posts no comment.
- `--from-stdin` re-verifies only when the payload carries `"reverify": true`
  (still subject to the opt-in). It posts no comment and prints the one it
  would post to stderr.
- An older `loom-daemon` simply never re-verifies. That is the pre-#10388
  behaviour, so there is no version floor.
- Re-date budget exhaustion is unchanged: it still escalates to
  `loom:operator` (#9590).
