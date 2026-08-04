# Blame-Issue Attribution

Reference detail for `.loom/scripts/blame-issue.sh` (#4338, codecast evaluation
borrow item 1 — [`docs/research/codecast-evaluation.md`](https://github.com/rjwalters/loom/blob/main/docs/research/codecast-evaluation.md)).

## Why

codecast's `cast blame` answers "which agent session wrote this line" by joining
`git blame` against a transcript session database. Loom does not have — or need —
a transcript database for this: every Builder/Doctor commit is created inside a
labeled issue's worktree, the PR is squash-merged with a `(#PR)` subject suffix
(this repo's merge style, see CLAUDE.md "Merging PRs"), and the PR body carries
`Closes #N`. That is already an in-band, durable join key from a line of code
back to the issue that produced it — `blame-issue.sh` is a small **read-only**
reporting wrapper that walks it for a human.

## Usage

```bash
./.loom/scripts/blame-issue.sh <path>                  # whole-file blame, one row per hunk
./.loom/scripts/blame-issue.sh <path> -L 10,40          # restrict to a line range (git-blame -L syntax)
./.loom/scripts/blame-issue.sh --pattern STRING <path>  # `git log --follow -S<pattern>` mode instead of blame
./.loom/scripts/blame-issue.sh --no-role <path>         # skip the role lookup (fewer gh calls, faster)
./.loom/scripts/blame-issue.sh --format json <path>     # newline-delimited JSON instead of the table
./.loom/scripts/blame-issue.sh --help
```

Output is a tab-separated table, one row per contiguous hunk (or per matching
commit in `--pattern` mode):

```
PATH  LINES  COMMIT  PR  ISSUES  ROLE  SUBJECT
```

## Resolution chain

1. `git blame --porcelain` (or `git log --follow -S<pattern>` in pattern mode)
   finds the commit that last touched each line/hunk.
2. The commit resolves to a PR number **offline first**: a squash-merge commit
   subject's trailing `(#1234)` is a reliable join key with no network call. If a
   commit has no such suffix (a direct-to-main commit, or a non-squash merge),
   it falls back to the GitHub REST "commit → associated pulls" endpoint
   (`gh api repos/{owner}/{repo}/commits/<sha>/pulls`).
3. The PR resolves to its closing issue number(s) via `closingIssuesReferences`
   (GitHub's own computed field from `gh pr view --json closingIssuesReferences`),
   falling back to regexing `Closes/Fixes/Resolves/Part of #N` out of the PR body
   when that field is empty.
4. **Role is best-effort.** A squash commit collapses every commit in the PR
   (Builder's original push, any Doctor fix-up pushes) into one commit on
   `main`, so per-line Builder-vs-Doctor attribution below the PR level is not
   recoverable. `blame-issue.sh` instead reports at the PR level: if
   `loom:changes-requested` was **ever** applied to the PR (per its label
   timeline, `gh api repos/{owner}/{repo}/issues/<PR>/timeline`), the PR went
   through at least one Doctor cycle — reported as `builder+doctor` (mixed).
   Otherwise `builder`. `unknown` when the PR can't be resolved at all, or when
   `--no-role`/no `gh` auth skips the lookup.

## Guardrails

Read-only: only `git blame`/`git log` (local) and `gh api`/`gh pr view` **GET**
calls. No forge writes, no daemon involvement, no new state on disk — nothing to
clean up, nothing to gitignore.

## Tests

`defaults/scripts/tests/test-blame-issue.sh` exercises the resolution chain
against a throwaway synthetic git fixture with a stubbed `gh` binary on `PATH`
(no network access needed) — mirrors the `test-check-main-freshness.sh` /
`test-disk-headroom.sh` fixture-repo pattern.
