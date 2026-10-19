# Bringing a PR branch up to date: merge or rebase (`branchUpdateStrategy`)

**Rule**: before a role brings a PR branch up to date with the default branch,
it asks `loom-daemon branch-update strategy`. The answer is `merge` or
`rebase`:

| Answer | Update | Push |
|---|---|---|
| `merge` | `git merge --no-ff -m "Merge origin/<default> into <branch> (<role>: <why>)" origin/<default>`; resolve conflicts, `git add`, `git commit --no-edit` | plain `git push origin HEAD:<branch>`. A rejected push means someone else pushed: fetch, merge their head in (or stand down), push again. **Never force.** Never amend or rebase a commit already on the remote. |
| `rebase` | `git rebase origin/<default>` | `--force-with-lease=<branch>:<sha>` pinned before the rewrite ([push-lease-pinning.md](push-lease-pinning.md)) |

Filed as issue #10771.

## Why

In a repository that forbids rebase, a rebase + force-push of a PR branch:

- deletes merge commits other agents already pushed, together with the record
  of how their conflicts were resolved (#9282);
- gives every commit a new SHA, so anything that cites a specific head (a
  reviewer's or ratification key's sign-off, an evidence record) now points at
  a commit the PR no longer contains;
- makes the author's next ordinary push fail as non-fast-forward.

A pinned `--force-with-lease` does not prevent any of this: the lease is
accurate, and the rewrite is still a rewrite. The 2026-10-11 case on #10771
leased against "the unchanged dispatch-time head", which was true, and still
flattened three merge commits and orphaned a two-key ratification head.

## How the answer is decided

First match wins:

1. `LOOM_BRANCH_UPDATE_STRATEGY` environment variable: `merge`, `rebase` or `auto`.
2. `branchUpdateStrategy` in `.loom/config.json` (or any tier of the effective
   config) of the primary checkout: `merge`, `rebase` or `auto`.
3. `auto`, the default: the forge's merge settings. `rebase` **only** when the
   repository is known to allow rebase merges (`allow_rebase_merge: true`).
   Everything else is `merge`: rebase merges disabled, the flag hidden from
   this credential, the settings unreadable, a Gitea forge (not probed
   natively yet), no resolvable `owner/repo`.

A value that is set but is not one of the three words is `merge`, not a fall
through to the next tier.

```json
{ "branchUpdateStrategy": "merge" }
```

A forge answer is cached for ten minutes in the git common dir, so one role
run's several calls cost one API read. Unknown answers are never cached.
`--no-cache` forces a fresh read; a cached `rebase` can outlive an operator
disabling rebase merges by up to those ten minutes.

## Contract

`loom-daemon branch-update strategy [--repo-dir DIR] [--repo OWNER/NAME] [--no-cache] [--quiet]`
always exits 0 and prints exactly `merge` or `rebase` on stdout, with the
reason on stderr. Callers write:

```bash
BRANCH_UPDATE=$(loom-daemon branch-update strategy) || BRANCH_UPDATE=merge
```

so a `loom-daemon` predating the subcommand also lands on `merge`.

## Who asks

- Doctor: the conflict recipes and the step 9 pre-push update.
- Judge: the automated DIRTY / BEHIND update and the simple-conflict path.
- Builder: the pre-push sync (`builder-pr.md`) and worktree conflict handling.
- `rebase-stacked-children.sh`: in merge mode a stale child is reported, never
  rewritten (merge-based de-stacking is #9808).
- `reconcile-stack.sh`: in merge mode the child is retargeted to the default
  branch and not rewritten. The parent's commits reached the default branch by
  merge commit, so the retarget alone is correct.
- The daemon's orphaned-stacked-child pass (#12760,
  `claim_reconciliation::orphan_stack`): an open PR whose base is a Loom
  branch whose PR was **closed without merging**. In rebase mode it replays
  only the child's own commits (`<closed parent head>..<child head>`) onto the
  freshly fetched default-branch tip in a scratch worktree, checks the result
  with `forge rebase-guard`, pushes with `--force-with-lease` pinned to the
  head it started from, and then retargets the PR to the default branch; PRs
  stacked on the child are replayed onto its new head the same way. It only
  rewrites a Loom branch in this repository whose head contains the parent
  head (`merge-base --is-ancestor`); anything else is flagged. In merge mode it
  neither rewrites **nor retargets**: the closed parent's commits never reached
  the default branch, so a retarget alone would carry them into the PR. The
  child gets `loom:merge-conflict` and one comment instead. That comment does
  not hand out a force-push recipe: it says the fix conflicts with this policy
  and points to #12767 for the merge-mode remedy. Details:
  [daemon-reference.md](daemon-reference.md) → "Orphaned stacked children".

`loom-daemon/tests/branch_update_cli.rs` runs the Doctor's own push block in a
merge-only fixture whose origin refuses non-fast-forward pushes, and scans the
role prompts so an unguarded `git rebase origin/...` or an unpinned force push
cannot come back.
