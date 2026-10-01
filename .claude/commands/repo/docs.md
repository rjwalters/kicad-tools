---
name: "docs"
description: "Check documentation against reality — content accuracy, README structure, and cross-references"
domain: repo
type: command
user-invocable: true
---

# /repo:docs — Documentation Check

The canonical documentation-health command. Verify that the repo's docs still
describe the repo that exists — not the one that existed three refactors ago.

This is the single entry point for docs work. It covers three layers:

1. **Content accuracy** — does the prose still match how things actually work?
   (unique to this command)
2. **README structure** — do file trees and directory listings match disk?
   (delegates to [[readme]])
3. **Cross-references** — do internal links and paths still resolve?
   (delegates to [[links]])

`/repo:readme` and `/repo:links` remain callable on their own when you only
want that one layer. Reach for `/repo:docs` when you want the whole picture.

## Usage

```
/repo:docs                     # Full repo — apply safe fixes, report as you go
/repo:docs docs/               # Scope to one subtree
/repo:docs README.md           # Check a single doc
/repo:docs --ask               # Review findings and confirm before applying
```

The optional path argument scopes every layer the same way, exactly as
[[readme]] and [[links]] scope on their own.

## What It Checks

### 1. Content Accuracy

This is the semantic layer that structural checks miss — prose that parses
fine and links that resolve fine, but describes behavior that has since
changed. Read the docs against the actual repo state and flag drift:

- **Feature / command tables** that list capabilities the repo no longer has,
  or omit ones it gained. For a repo with `.claude/commands/`, cross-check
  every documented command against the files that actually exist, in both
  directions.
- **CHANGELOG currency** — recent commits (features, breaking changes, renames)
  that landed with no corresponding CHANGELOG entry. Compare the top entry's
  date/version against `git log` since then.
- **Code examples & snippets** in docs that reference symbols, flags, file
  paths, or commands that no longer exist. Verify each referenced identifier
  resolves in the current tree.
- **Described workflows** — step-by-step instructions (install, build, usage)
  that name scripts, targets, or flags. Confirm they still exist and still take
  the arguments shown.
- **Version / count claims** — "supports 12 commands", "requires Node 18",
  hardcoded numbers that drift as the repo changes.

Content findings are judgment calls — when something looks stale but you can't
confirm it from the repo, flag it as a question rather than asserting it's
wrong.

### 2. README Structure (see [[readme]])

Run the full [[readme]] check: ASCII file-tree accuracy, directories missing a
README, and stale "gitignored"/"TODO" annotations. Fold its findings into this
report rather than emitting a separate one.

### 3. Cross-References (see [[links]])

Run the full [[links]] check: markdown links, CLAUDE.md path references,
skill/command wikilinks, and nested CLAUDE.md paths. Fold its findings in the
same way. Broken CLAUDE.md paths remain **critical** — they're the primary
navigation paths for agents.

That includes [[links]]' resolution rules, not just its finding list: where the
repo declares an install-template mapping in `.repo/link-roots.json`, fold in
the mapping table too and keep the "resolved via install mapping" annotation on
individual links. A consolidated report that drops it turns a wrong mapping into
a silent zero.

The same applies to [[links]]' sibling-repo base: where the repo declares
`.repo/link-siblings.json`, fold in its table and keep the
"resolved via sibling repo `<name>`" annotation and the
"sibling repo not present — unverifiable" annotation on individual links,
distinguishable at a glance from `MISSING`. Collapsing
either sibling-repo status back into `MISSING` in the consolidated report
reintroduces the machine-dependent false positive [[links]]' fourth base
exists to remove — a link that is merely unverifiable on this machine would
read as broken here even though [[links]] itself never calls it that.

## Output Format

One consolidated report, grouped by layer so it's clear which are mechanical
(structure, links) and which are judgment calls (content):

```
## Docs Check — docs/

### Content Accuracy (2 findings)
| Severity | Location | Issue |
|----------|----------|-------|
| warn | README.md:14 | Skills table omits /repo:docs (exists on disk) |
| info | CHANGELOG.md | No entry since v0.3.0; 4 feature commits since |

### README Structure (1 finding — via readme)
| warn | docs/analysis/ | No README, 8 files |

### Cross-References (1 finding — via links)
| critical | CLAUDE.md:42 | Link to docs/setup.md — MISSING |

### Summary
- 2 content (0 critical, 1 warn, 1 info)
- 1 structure (0 critical, 1 warn)
- 1 cross-reference (1 critical)
```

## Interaction

By default, apply the safe, reversible fixes as you find them — correcting
stale listings, tables, and factual drift — and report each change (all
git-reversible). Run with `--ask` to review first: for each finding, offer
to **fix it**, **skip** it, or **show** the relevant section before deciding.

Content findings are judgment calls; when you can't confirm something is wrong
from the repo, raise it as a question rather than editing on a guess — even in
the default apply mode.

Never rewrite prose wholesale to "improve" it — the job is accuracy, not a
style pass. Fix the factual drift and leave the voice alone.

### Verify after write

Applying an edit is not proof it survived. A concurrent writer — another agent
working in the same clone, a background `git stash` or `git checkout --`, a
pre-commit hook, a Loom sweep quarantining the primary clone's working tree —
can revert a file between the moment you fix it and the moment you report it,
leaving this command claiming a fix that is no longer on disk.

So immediately after applying each fix, and **before counting it as applied**,
re-read the changed region of the file and confirm your specific edit is
present. `git diff -- <path>` / `git status --porcelain -- <path>` is a cheap
first pass, but only proves the path differs from HEAD — it cannot distinguish
your edit from someone else's, so it must not be the sole check when the file
may carry other uncommitted changes.

This check is **unconditional** — run it whether or not you have any reason to
suspect a concurrent writer. Detecting a daemon first would be racy (one can
start right after the check), and in a repo with no concurrent writer the check
always finds the edit still applied, so nothing about the reported output
changes.

If a fix is gone on re-check, report it on its own line as **reverted after
apply — needs re-run**. Do not silently re-apply it, and do not count it in the
fixed total — that total must only ever include edits confirmed still on disk.

### Loom-managed repo: land fixes where a sweep cannot take them

> **This is the canonical copy of this ladder.** [[gitignore]] and [[links]]
> point here instead of restating it — they run exactly this decision with their
> own command noun ("Gitignore fixes", "Link fixes") substituted into the
> warning and the report lines. Change the ladder here and it changes for all
> three; do not re-inline it anywhere else.

The check above catches an edit reverted *during* the run. It cannot catch the
likelier failure in a Loom-managed repo, which happens *after* it: a sweep runs
`check-main-clean.sh --quarantine`, which polices the **primary checkout's
working tree as a whole** — not the branch it happens to be on — and stashes
every uncommitted delta it finds so its worktrees get a clean base. Nothing is
destroyed (the stash is labelled `loom-quarantine: run=<sweep-id> issue=<N>`),
but the fixes this command reported as applied are off disk minutes later, and
nobody re-reads a summary that has already printed. Branching does not help —
the quarantine is branch-blind.

The related symptom, if you meet it: Loom's guard hooks deny writes into the
primary checkout while a managed worktree exists — `BLOCKED: ... resolves to the
main repository checkout ... but a Loom-managed worktree exists elsewhere`. When
that guard is disabled, or catches only the Bash-tool arm, the `Edit`/`Write`
fixes go in unblocked and are quarantined afterwards instead.

**Decide the destination before applying the first fix, and report it once, up
front.** Detecting afterwards means re-applying everything somewhere else — and
a pass that decides per-stage scatters one run's fixes across two destinations.
Choosing before acting is what makes the single up-front report honest: every
later stage of the same pass (including [[gitignore]]'s and [[links]]' fixes
when they run under [[all]]) lands wherever this decision put it.

```bash
root=$(git rev-parse --show-toplevel)
loom_managed=no
if [ -d "$root/.loom" ] && { [ -d "$root/.loom/worktrees" ] || pgrep -f loom-daemon >/dev/null 2>&1; }; then
  loom_managed=yes
fi
# Already inside a managed worktree? Then this is not the primary checkout,
# quarantine does not reach here, and nothing below applies.
[ -f "$root/.loom-managed" ] && loom_managed=no
```

`loom_managed=no` — every repo with no `.loom/` root, and any run from inside a
managed worktree — is the unchanged path: apply fixes in place, report them
exactly as before, and print none of the text below.

**If `loom_managed=yes`, choose a destination in this order:**

1. **Commit them in a dedicated issue worktree**, when the run is attached to an
   issue number: `./.loom/scripts/worktree.sh <issue-number>`, apply the fixes in
   the worktree it prints, commit them there, and report the branch and path.
   For *this* arm always that helper, **never a bare `git worktree add`** — the
   helper is what writes the `.loom-managed` sentinel that authorizes later
   cleanup of an issue worktree. It takes a numeric issue number and nothing
   else, so this arm exists only when the pass has one; do not invent a number to
   unlock it. (Arm 2 below is the one deliberate exception, for the opposite
   reason: it has no issue number and therefore must not be sentinelled.)

2. **Commit them on a `chore/repo-hygiene-<date>` branch in a worktree off
   `origin/<default>`, then offer to push and open a PR** — when the run has no
   issue number **and** either the default branch is PR-protected or the operator
   explicitly asked for a branch + PR this run.

   This arm sits *above* arms 3 and 4 on purpose. A routine hygiene pass on a
   PR-protected repo has no issue number, usually sits on the default branch, and
   often has a dirty tree, so without it the run falls into arm 4 (fixes left at
   risk) or, on a clean tree, arm 3 — which on a protected default branch
   produces a commit nobody can push, recoverable only by cherry-picking it onto
   a fresh branch and resetting local `<default>` afterwards.

   **Detect protection before choosing**, mirroring — never sourcing —
   `.loom/scripts/land-resync-commit.sh`'s `default_branch_requires_pr()` (lines
   598–625), the same pattern [[update-tools]] step 3 uses: same
   fail-closed-on-error / fail-open-when-undetectable split, same
   `PROTECTION_SOURCE` side effect so the report can name *why* it is protected
   rather than only that it is.

   ```bash
   DEFAULT=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD | sed 's#^origin/##')
   DEFAULT=${DEFAULT:-main}

   # Try the rulesets API, fall back to legacy branch protection, and treat ANY
   # API failure or unparseable answer as protected — never assume unprotected
   # on ambiguity. {owner}/{repo} is gh's placeholder for *this* repo's own
   # `origin` remote (the repo being hygiene-checked), resolved from cwd.
   PROTECTION_SOURCE=""
   default_branch_requires_pr() {
     if [ "${FORCE_BRANCH:-0}" = "1" ]; then
       PROTECTION_SOURCE="operator asked for a branch + PR (detection skipped)"
       return 0
     fi
     # Fail OPEN in the two cases where detection cannot run *at all* — a Gitea
     # forge, or no `gh` on PATH. Those are exactly the cases the operator's
     # explicit ask above exists to cover; failing closed here would route every
     # non-GitHub repo down this arm whether or not it needs a PR.
     case "$(printf '%s' "${LOOM_FORGE_TYPE:-}" | tr '[:upper:]' '[:lower:]')" in
       gitea) return 1 ;;
     esac
     command -v gh >/dev/null 2>&1 || return 1
     # From here on the forge *was* asked, so an unhelpful answer fails CLOSED.
     local rule_types
     if ! rule_types="$(gh api "repos/{owner}/{repo}/rules/branches/$DEFAULT" --jq '.[].type' 2>/dev/null)"; then
       PROTECTION_SOURCE="rules API call failed — assumed protected (fail closed)"
       return 0
     fi
     if grep -qvE '^[A-Za-z0-9_]*$' <<< "$rule_types"; then
       PROTECTION_SOURCE="rules API answer unparseable — assumed protected (fail closed)"
       return 0
     fi
     if grep -qxE 'pull_request|required_status_checks' <<< "$rule_types"; then
       PROTECTION_SOURCE="ruleset rule(s): $(grep -xE 'pull_request|required_status_checks' <<< "$rule_types" | tr '\n' ' ' | sed 's/ $//')"
       return 0
     fi
     local legacy
     legacy="$(gh api "repos/{owner}/{repo}/branches/$DEFAULT/protection" \
       --jq '[.required_pull_request_reviews, .required_status_checks] | map(select(. != null)) | length' 2>/dev/null || true)"
     if [[ "$legacy" =~ ^[0-9]+$ && "$legacy" -gt 0 ]]; then
       PROTECTION_SOURCE="legacy branch protection (PR reviews / status checks required)"
       return 0
     fi
     return 1   # the forge definitively answered "no rules"
   }
   ```

   Then create the worktree with a **plain, explicit `git worktree add`** — this
   is the one arm where that is correct, because `./.loom/scripts/worktree.sh`
   takes a numeric issue number and this arm by definition has none:

   ```bash
   git fetch origin "$DEFAULT" --quiet
   HYGIENE_BRANCH="chore/repo-hygiene-$(date +%Y-%m-%d)"
   WT="$root/.loom/worktrees/repo-hygiene-$(date +%Y-%m-%d)"
   git worktree add -b "$HYGIENE_BRANCH" "$WT" "origin/$DEFAULT"
   # Apply every fix inside $WT, then:
   git -C "$WT" commit -m "chore: repo hygiene fixes ($(date +%Y-%m-%d))"
   ```

   **Do not write a `.loom-managed` sentinel into this worktree**, and do not
   reach for the helper in order to get one. The sentinel is not a general
   "Loom knows about this" marker — it is specifically the token that authorizes
   *cleanup-on-merge* for an **issue** worktree, whose contract is "the linked
   `loom:building` issue closes and its PR merges, therefore remove this
   directory and its branch." A hygiene worktree has no issue number, so there is
   no `loom:building` issue to close and that contract cannot be satisfied.
   Sentinelling it anyway hands `loom-clean` / the daemon's stale-worktree reaper
   a managed worktree it can never match to a merged PR — the shape they classify
   as orphaned or stale — so it can be reaped out from under an in-flight hygiene
   PR. Without the sentinel it is simply a user-provisioned worktree, which Loom
   never touches; remove it yourself with `git worktree remove` once the PR
   merges.

   **Pushing and opening the PR happen only on explicit operator confirmation**
   — never automatically. The commit is already safe on its own branch, so a
   declined offer loses nothing:

   ```bash
   git -C "$WT" push -u origin "$HYGIENE_BRANCH"
   gh pr create --head "$HYGIENE_BRANCH" --base "$DEFAULT" \
     --title "chore: repo hygiene fixes" \
     --body "Hygiene fixes from a /repo:* pass; $DEFAULT is PR-protected."
   ```

   Report the branch, the worktree path, `$PROTECTION_SOURCE` as the detector
   set it, and — once opened — the PR URL instead of a push reminder.

3. **Commit them on the current branch**, when arm 2 did not fire and the
   working tree was otherwise clean at the start of the run — then the commit
   holds your fixes and none of the operator's. Report the branch and the short
   sha. Add one clause when the current branch is the default branch: the primary
   clone is now a commit ahead of its upstream until someone pushes it. (On a
   PR-protected default branch arm 2 has already taken this case, precisely so
   that unpushable commit is never created.)

4. **Leave them uncommitted and warn.** A dirty tree with no issue number and an
   unprotected default branch lands here. Do not stash the operator's unrelated
   edits to manufacture a clean commit — that is the same working-tree rewrite
   this section exists to avoid. Print the warning once, on its own line:

   ```
   Loom-managed repo: uncommitted doc fixes in the primary checkout can be quarantined by a sweep — commit or stash them now.
   ```

**Name the destination in the report, not just the count.** `2 fixed` does not
say whether the fixes will still exist tomorrow:

```
Docs: 2 fixed on feature/issue-448 (worktree .loom/worktrees/issue-448, a1b2c3d)
Docs: 2 fixed on chore/repo-hygiene-2026-09-30 (worktree .loom/worktrees/repo-hygiene-2026-09-30, a1b2c3d) — main protected: ruleset rule(s): pull_request; push + PR offered
Docs: 2 fixed, committed on main (a1b2c3d)
Docs: 2 fixed — uncommitted, at risk
```

If fixes did vanish this way they are recoverable rather than lost — send the
operator to the stash instead of re-applying blind:

```bash
git stash list | grep loom-quarantine
git stash show -p 'stash@{0}'     # replay with `git apply`
```

## Principles

Same as every hygiene command: **apply safe fixes, gate destructive ones**
(doc edits are reversible, so they apply by default; `--ask` to confirm
first); **general by design** (no assumptions about doc layout — read what's
there); **don't be noisy** (a slightly informal sentence isn't a finding; a
command that no longer exists is).
