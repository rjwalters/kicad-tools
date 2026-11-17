# Pinning `--force-with-lease` (shared-clone push safety)

**Rule**: in a Loom clone, never push with a bare `git push --force-with-lease`.
Always pin the expected value — `--force-with-lease=<branch>:<oid>` — to the
remote head the work being pushed is actually based on, captured **before** that
work rewrote the branch.

Filed as issue #9487 after a live data-loss near-miss on PR #9483.

This applies only where PR branches may be rebased at all. In a repository
whose merge settings bar rebase (the default answer of
`loom-daemon branch-update strategy`), there is no force push to pin: the
branch is updated by merging and pushed plain. See
[branch-update-strategy.md](branch-update-strategy.md) (#10771).

## Why the bare flag is unsafe here

`git push --force-with-lease` with no `=<ref>:<expect>` value compares the
remote head against the **local remote-tracking ref**
`refs/remotes/origin/<branch>`. That is not the remote; it is a local cache of
it — and in a Loom clone it is a cache **shared by every linked worktree**.
`.loom/worktrees/issue-N` are `git worktree` links: one object store, one set of
remote-tracking refs, across every concurrently running agent.

So a sibling agent that pushes and then fetches — or that merely fetches —
fast-forwards *your* lease value to the commit *they* just published. The lease
is then satisfied by construction, your push is **accepted**, and their commit
is deleted. There is no error, no rejection, and no conflict marker: the check
compared against a ref somebody else updated, not against truth.

This is the same class of hazard as `refs/stash` being one stack shared by every
linked worktree (#4821 / #5754) — shared mutable state that looks per-worktree.

## The incident (PR #9483, 2026-09-29)

Two Doctor agents were dispatched against the same PR after a Judge rejection.
The first fixed and pushed (`23ad41605`, 09:15Z) and Judge approved it
(`loom:pr`, 09:27Z). The second, working from a view taken before that push,
pushed with a bare `--force-with-lease` at 09:38Z — **and it succeeded**,
overwriting an already-approved commit. It also invalidated green CI and
regressed the PR's label state.

The two independently derived fixes happened to be byte-identical, so nothing
was lost that time. Had they differed, the first Doctor's approved work would
have been destroyed silently.

## The trap: do not "freshen" the pin

The intuitive repair — `git fetch origin <branch>` immediately before the push,
then pin to the just-fetched value — **reintroduces the bug**. A pin read after
the sibling pushed *is* the sibling's commit, so the lease passes and the
clobber proceeds, now laundered as a "fresh" value.

The pin must be the head the pushing work is **based on**:

- **Doctor**: `CLAIM_HEAD_SHA`, the PR's `headRefOid` captured at claim time
  (step 2), **before** `gh pr checkout` — never re-read from the forge after
  the fix work. Re-pin to a newer head **only** after deliberately rebasing
  onto it, and to `git rev-parse HEAD` after each successful push of its own
  (otherwise its second push in a session is rejected and looks foreign).
- **A rebase/publish script**: the branch's live remote head read **before** the
  rebase runs.

## The pin does NOT fail closed on its own

git does not check that the expected value is a commit you have. A **full**
40-hex pin to a commit this clone never fetched is **accepted**: if origin is at
that commit, the push succeeds and overwrites it. That is exactly the state a
late-read pin produces (e.g. `gh pr view --json headRefOid` after a sibling
pushed), so a pin taken at the wrong moment launders the clobber just like the
bare flag does. Only an *abbreviated* SHA git cannot resolve is refused
(`cannot parse expected object name`) — no recipe should rely on that.

So every pin is paired with an **incorporation check**, run **before** anything
is rewritten (a rebase or amend drops the old head from `HEAD`'s history):

```bash
git merge-base --is-ancestor "$PUSH_LEASE_SHA" HEAD || { echo "Pin not in HEAD: STOP." >&2; exit 1; }
```

or, in a script, `pin-flag --local-ref` (below), which performs the same check.
With both halves the pinned form fails closed: a sibling push after the pin is
rejected (`! [rejected] … (stale info)`), and a pin that is not yours stops the
run before it rewrites anything. A missing pin means **stop** — never fall
back to re-reading the head, which is the "freshen" trap above.

## The implementation: `loom-daemon push-lease pin-flag`

The pinning logic is a **daemon subcommand**, not shell — new executable logic
belongs in Rust ([shell-language-policy.md](https://github.com/rjwalters/loom/blob/main/.loom/docs/shell-language-policy.md)
— an absolute URL because that doc is not installed into consumer repos), and a script that
reaches it only has to find the binary, pass the arguments, and read the result
back (`loom-daemon/src/cli/push_lease.rs` is the authoritative reference):

```bash
# At the point the branch state is READ — before the rebase/amend.
PUSH_LEASE_ARG="$("$DAEMON_BIN" push-lease pin-flag \
    --remote origin --branch "$BRANCH" --local-ref "refs/heads/$BRANCH")" || exit 1
# … rebase / amend / commit …
git push "$PUSH_LEASE_ARG" origin "$BRANCH"
```

| Exit | Meaning |
|---|---|
| `0` | stdout is the pinned `--force-with-lease=<branch>:<oid>` argument. An **empty** oid when the remote does not have the branch yet: git reads that as "must not exist", the correct lease for a first push. |
| `3` | `[FETCH]` — the remote could not be queried, so there is no pin. The caller must refuse, **not** fall back to the bare flag. |
| `4` | `[LEASE-PIN]` — the remote head is not an ancestor of `--local-ref`, i.e. the remote holds commits this clone never incorporated. The pin would be accurate and the push would still delete them. |

There is deliberately no "could not tell, here is a bare flag" outcome: the only
safe fallback for a missing pin is not pushing. The bracketed tokens are the same
greppable prerequisite shape `loom-daemon reconcile-stack` uses.

`defaults/scripts/lib/push-lease-verify.sh` keeps the *other* half of lease
truthfulness: `push_landed_despite_rejection <remote> <branch> <sha>`, for a
*reported* rejection of an update that actually landed via the Git-LFS pre-push
hook race (#6695).

Pinned callers today: `defaults/scripts/reconcile-stack.sh`,
`defaults/scripts/rebase-stacked-children.sh`, and every push recipe in the
Doctor role prompt (§"Pin the lease").

## Regression coverage

- `defaults/scripts/tests/test-reconcile-stack.sh` scenario **D2** reproduces
  the incident with real git: a sibling pushes *and* fetches between the pin and
  the push, so the shared tracking ref agrees with the moved remote. The pinned
  lease is rejected and the sibling's commit survives. Reverting the push to the
  bare flag makes that scenario fail with exit 0 — the clobber — which is the
  negative control for the fix.
- Scenario **D** covers a commit that landed *before* the run: refused as a
  precondition failure, before the rebase mutates anything.
- `defaults/scripts/tests/test-rebase-stacked-children.sh` scenarios **(c)**,
  **(j)**, **(k)** assert the pinned push argument, that the pin is built before
  the rebase, and the two refusals (exit 3 / exit 4).
- `loom-daemon/src/cli/push_lease.rs`'s unit tests cover the subcommand itself
  against real git: the live tip is read from the remote (not the tracking
  ref), an unqueryable remote is an error rather than "absent", and a sibling's
  unfetched commit is correctly reported as not incorporated. They also pin
  down the git semantics the Doctor recipes depend on — a never-fetched
  full-SHA pin is **accepted** (so the ancestry check is load-bearing), and a
  second push needs a re-pin to the first push's `HEAD` — and assert the Doctor
  prompt's recipes keep the claim-time pin, the ancestry check and the re-pin.
