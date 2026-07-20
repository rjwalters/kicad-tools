---
name: "followups"
description: "Capture follow-on work surfaced during this session and file it as issues — routed to this repo or the right upstream tool repo, always confirmed first"
domain: repo
type: command
user-invocable: true
---

# /repo:followups — File Session Follow-Ups

Mine the current working session for follow-on work that was surfaced but not
done — bugs found-but-not-fixed, deferred TODOs, documentation gaps, and
limitations discovered in an upstream tool while using it — then file each as an
issue in the right repo (this repo, or an upstream tool repo like Loom / Anvil /
Repo Skills / kicad-tools).

Unlike every other `/repo:*` command, which scans repo / git / filesystem
state, this one mines the **conversation**: the deferred work and discovered
bugs that only exist in the session's context. Filing is outward-facing — for
upstream targets it writes into *other people's* repos — so this command is in
the same "always confirm first" class as `release`, `remote`, and
`update-tools`, never the auto-apply behavior of the hygiene commands.
**Confirmation is the default and only mode; there is no `--ask` flag because
there is nothing to opt into.**

## Usage

```
/repo:followups                 # Review this session, propose issues, confirm, then file
/repo:followups --dry-run       # Propose only — show what would be filed, file nothing
/repo:followups --repo loom     # Restrict to follow-ups targeting one tool repo
/repo:followups --here          # Only this repo; skip all upstream tool repos
```

## Steps

### 1. Mine the session for candidates

Review the working session and collect concrete follow-on work in four
categories. Only include work that was actually surfaced — do not invent tasks.

- **Bugs found but not fixed** — something broke or misbehaved and was noted
  but left unaddressed (in this repo's code or in a tool being used).
- **Deferred TODOs** — "we should do X later", "out of scope for now",
  intentionally punted work.
- **Documentation gaps** — missing/stale/wrong docs noticed while working.
- **Upstream tool limitations** — a bug, missing feature, or rough edge in an
  installed tool (Loom, Anvil, Repo Skills, kicad-tools) hit while using it.

For each candidate capture: a one-line title, the context / where it came up in
the session (repro if it's a bug), and suggested acceptance criteria.

### 2. Build the target-routing table

Every candidate has to land in *some* repo. Build the routing table by reusing
`/repo:update-tools`' discovery — do **not** hardcode a repo list.

- **This repo** (`origin`): follow-ups about the Repo Skills commands
  themselves, or whatever code/docs live in the current repo.

  ```bash
  git config --get remote.origin.url    # → derive this repo's owner/repo slug
  ```

- **Upstream tool repos**: discover installed tools exactly as
  `/repo:update-tools` step 1 does — find their metadata files, then resolve
  each tool's local source clone **sidecar-first**, and derive a GitHub slug
  from that clone's `origin` remote.

  ```bash
  # a. Find installed-tool metadata (same locations update-tools checks)
  ls .loom/install-metadata.json .anvil/install-metadata.json 2>/dev/null
  ls .claude/skills/*/install-metadata.json 2>/dev/null
  ```

  Resolve each tool's `source` clone path in this order (each step failing is
  "source unknown", not an error):

  1. **Sidecar first.** For Repo Skills read
     `.claude/skills/repo/.install-local.json` (generally
     `.claude/skills/*/.install-local.json`); it holds `source` /
     `installed_at`. Loom uses the plain-text `.loom/loom-source-path` sidecar
     for the same purpose. A sidecar is gitignored, so it exists only on the
     machine that ran the install.
  2. **Legacy inline fallback.** Pre-split installs still embed `source` /
     `installed_at` directly in `install-metadata.json` — read from there if no
     sidecar exists.
  3. **Unknown → GitHub.** If neither yields a path, the local source clone is
     simply unknown (normal on a fresh clone elsewhere).

  Then derive the slug from the resolved clone's remote:

  ```bash
  git -C <source> config --get remote.origin.url   # → owner/repo for gh --repo
  ```

  `install-metadata.json` (tracked) is JSON, and key names vary by tool
  (`version` vs `loom_version` / `anvil_version`, etc.) — read whichever variant
  is present, same as `/repo:update-tools`. Neither the tracked metadata nor the
  sidecar stores an `owner/repo` slug directly; it is always derived from the
  source clone's `origin` remote.

- **Unresolvable targets.** If a tool's source clone is unknown (no sidecar, no
  legacy field) there is no local remote to read — mark that follow-up
  **UNKNOWN** and surface it for the user to name a slug, per the safety rules.
  Likewise, if a candidate doesn't clearly belong to any discovered repo,
  surface it for a target decision rather than dropping it or guessing.

Honor scope flags: `--here` keeps only this-repo targets; `--repo <tool>`
restricts to a single discovered tool.

### 3. Dedup against existing open issues

Before proposing to file, check each target repo for issues that already cover
the candidate so nothing is re-filed:

```bash
gh issue list --repo <slug> --state open --search "<key terms>" \
  --json number,title,url
```

Classify each candidate against its target repo's open issues:

- **New** — no match; propose to file.
- **Near-match** — a similar issue exists; **flag it for the user** with the
  existing issue's number/URL and let them choose: file anyway, skip, or
  comment on the existing one. Never silently file over it or silently drop it.

### 4. Report the proposed set and confirm

Present the full proposal and get explicit approval before touching any repo:

```
FOLLOW-UPS FROM THIS SESSION
============================
| # | Target repo        | Title                              | Dedup            |
|---|--------------------|------------------------------------|------------------|
| 1 | rjwalters/repo     | orphans check misses nested dirs   | NEW              |
| 2 | rjwalters/loom     | worktree.sh fails on detached HEAD | near #217 (flag) |
| 3 | rjwalters/anvil    | (docs gap) …                       | NEW              |
| 4 | UNKNOWN            | kicad-tools DRC false positive     | ask — no slug    |
```

For each proposed issue show the target repo, title, a body preview (context /
repro / suggested acceptance criteria), and dedup status. Then confirm which to
file. **If `--dry-run` was passed, stop here — file nothing.**

### 5. File the approved issues

For each approved, non-UNKNOWN candidate:

```bash
gh issue create --repo <slug> \
  --title "<title>" \
  --body "$(cat <<'EOF'
## Context
<where this came up in the session / repro>

## Suggested acceptance criteria
- [ ] …
EOF
)"
```

Print the resulting issue URLs. For near-matches the user chose to comment on
instead of file, use `gh issue comment --repo <slug> <n>`. Leave UNKNOWN /
skipped candidates unfiled and list them so nothing is silently lost.

Filed issues are triaged like any other afterward — this command does not apply
`loom:*` or other pipeline labels.

## Safety Rules

1. **Never file without confirmation** — present the full proposed set (target
   repo, title, body preview, dedup status) and file only what's approved.
2. **Dedup before filing** — check open issues in each target repo; show
   near-matches and let the user decide file / skip / comment-on-existing.
3. **Never guess a target repo** — unresolved or ambiguous targets are reported
   as UNKNOWN for the user to name, never filed to a guessed slug.
4. **`--dry-run` files nothing** — pure proposal mode for review.
