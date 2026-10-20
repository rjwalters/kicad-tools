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
