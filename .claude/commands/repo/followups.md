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
  `/repo:update-tools` step 1 does — sweep for their metadata files, then
  resolve each tool's local source clone **sidecar-first**, and derive a
  GitHub slug from that clone's `origin` remote.

  ```bash
  # a. Find installed-tool metadata (same sweep /repo:update-tools step 1 uses)
  find . -maxdepth 4 -name "install-metadata.json" \
    -not -path "*/node_modules/*" -not -path "*/.venv/*" 2>/dev/null
  ```

  A fixed list of known paths structurally cannot find a family member added
  after this doc was last updated (repo#165) — sweep for the file itself
  instead. `-maxdepth 4` reaches every known tool root (`.loom/`, `.anvil/`,
  `.kct/` at depth 2, and the two-levels-deeper `.claude/skills/*/` at depth 4).

  Resolve each tool's `source` clone path with the **sidecar → legacy inline →
  unknown** order that is normative in the [tool-package installer
  contract][contract] (requirement **C6**, which also covers the repo#96 signature
  below). Each step failing is "source unknown", not an error. Do not re-derive
  the order from what a given tool happens to do — read C6.

  [contract]: https://github.com/rjwalters/repo/blob/main/INSTALLER-CONTRACT.md

  **kicad-tools does not conform to C6.** `.kct/install-metadata.json` carries
  `kct_version` / `kct_commit` and no sidecar — it always records `source_mode`
  (`"path"` or `"git"`) and `source_ref` inline instead. When `source_mode` is
  `"path"`, `source_ref` **is** the local clone path — read its `origin`
  remote directly, skipping the sidecar ladder above. When `source_mode` is
  `"git"` (kicad-tools' default), there is no local clone at all — this
  degrades to "source unknown" exactly like a fresh clone, not an error, and
  the slug can be read straight off `source_ref` (a `<git-url>@<tag-or-rev>`
  string) without a `git -C <source> config` lookup.

  Then derive the slug from the resolved clone's remote:

  ```bash
  git -C <source> config --get remote.origin.url   # → owner/repo for gh --repo
  ```

  `install-metadata.json` (tracked) is JSON, and key names vary by tool
  (`version` vs `loom_version` / `anvil_version` / `kct_version`, etc.) — read
  whichever variant is present, same as `/repo:update-tools`. Neither the
  tracked metadata nor the sidecar stores an `owner/repo` slug directly for
  Loom / Anvil / Repo Skills — it is always derived from the source clone's
  `origin` remote; kicad-tools' `source_ref` in `"git"` mode is the one
  exception, since it already embeds the full GitHub URL to parse directly.

- **Unresolvable targets.** If a tool's source clone is unknown (no sidecar, no
  legacy field) there is no local remote to read — mark that follow-up
  **UNKNOWN** and surface it for the user to name a slug, per the safety rules.
  Likewise, if a candidate doesn't clearly belong to any discovered repo,
  surface it for a target decision rather than dropping it or guessing.

  **Signature check** (contract **C6**, "the repo#96 signature"): when
  `install-metadata.json` exists but neither a sidecar nor legacy inline fields
  do, that is also what a previously *tracked* sidecar leaves behind once it is
  untracked upstream. Still mark the target UNKNOWN — there is no path to read —
  but append C6's distinct suggestion (`"sidecar missing but
  install-metadata.json present — …"`) rather than treating it identically to a
  fresh clone, which gets no such suggestion. This is the same handling
  `/repo:update-tools` step 1 applies, and both follow C6 rather than each other.

Honor scope flags: `--here` keeps only this-repo targets; `--repo <tool>`
restricts to a single discovered tool.

### 3. Dedup against existing open issues

Before proposing to file, check each target repo for issues that already cover
the candidate so nothing is re-filed. Query the **REST search** endpoint, not
`gh issue list --search`:

```bash
gh api "search/issues?q=repo:<slug>+state:open+<key+terms>&per_page=30" \
  --jq '.items[] | "#\(.number) \(.title) \(.html_url)"'
```

**Pull requests are deliberately in scope.** `search/issues` returns both
issues **and pull requests** — the `issues` in the route name is GitHub's
issue-tracker sense of the word, and the step title uses it the same loose way.
This is intentional and is *not* narrowed with `+is:issue`: an open PR covering
a candidate is a **stronger** dedup signal than an open issue, because it means
the work is already in flight rather than merely proposed. Filtering PRs out
would discard exactly the signal that matters most for "don't re-file something
already being worked on". The cost of the wider result set is absorbed by
safety rule 2 — near-matches are always flagged to the user, never
auto-skipped or auto-filed-over — and `html_url` discloses which kind each
match is.

Search result items already carry `number`, `title`, and `html_url`, so this is
a straight replacement for the old `--json number,title,url` output shape — no
second-pass mapping needed. That parity covers the output **shape** only, not
the result **set**: the old `gh issue list --search` form returned issues only,
while this one is deliberately broader (see above).

Terms go into `q` as `+`-joined tokens; URL-encode anything that isn't
alphanumeric, and quote the whole URL so the shell leaves it alone.

**Why not `gh issue list --search`:** it goes through GitHub's GraphQL API,
whose rate-limit bucket is separate from REST's and is routinely exhausted on a
busy multi-agent host while the `core` budget sits nearly unused. `search/*` is
a third bucket again (30 requests/minute authenticated), so deduping here costs
nothing from the pool step 5 needs to actually file. Check live budgets with
`gh api rate_limit --jq .resources` if either step starts failing.

Classify each candidate against its target repo's open issues **and pull
requests** (the search returns both, per the note above):

- **New** — no match; propose to file.
- **Near-match** — a similar item exists; **flag it for the user** with the
  existing item's number/URL and let them choose: file anyway, skip, or
  comment on the existing one. Never silently file over it or silently drop it.
- **A near-match may itself be a pull request.** Check `html_url` for `/pull/`
  vs `/issues/` to tell which, and say so when flagging it — e.g. "near #99
  (PR, work already in flight)" alongside "near #217 (issue)". A PR match
  normally argues *more* strongly for skip-or-comment than an issue match does.
  Commenting works either way (`POST /issues/<n>/comments` accepts a PR
  number), but on a PR the comment lands in that PR's conversation rather than
  on a standalone issue, so confirm that's what the user wants.

### 4. Report the proposed set and confirm

Present the full proposal and get explicit approval before touching any repo:

```
FOLLOW-UPS FROM THIS SESSION
============================
| # | Target repo        | Title                              | Dedup                   |
|---|--------------------|------------------------------------|-------------------------|
| 1 | rjwalters/repo     | orphans check misses nested dirs   | NEW                     |
| 2 | rjwalters/loom     | worktree.sh fails on detached HEAD | near #217 (issue, flag) |
| 3 | rjwalters/repo     | followups dedup also matches PRs   | near #99 (PR, flag)     |
| 4 | rjwalters/anvil    | (docs gap) …                       | NEW                     |
| 5 | UNKNOWN            | kicad-tools DRC false positive     | ask — no slug           |
```

For each proposed issue show the target repo, title, a body preview (context /
repro / suggested acceptance criteria), and dedup status. Then confirm which to
file. **If `--dry-run` was passed, stop here — file nothing.**

The `Dedup` column carries step 3's classification: `NEW`, a flagged
near-match, or `ask` for an unresolved target. A flagged near-match may resolve
to **either an open issue or an open pull request** — step 3 searches both on
purpose — so name which kind it is (rows 2 and 3 above), since a PR match means
the work is already in flight and usually changes the user's choice.

### 5. File the approved issues

For each approved, non-UNKNOWN candidate, write the body to a scratch file and
POST it through REST. **Use a literal, spelled-out scratch path — never a
shell variable — as the `>` redirect target and the `--input` argument.** In a
Loom-managed repo the destructive-write guard denies a write whose target is
an unexpanded shell variable outright, because it cannot statically resolve
where the write lands and a variable-rooted path might resolve inside a repo
with live worktrees (#4921/#4178); a literal path sidesteps that ambiguity
entirely, so do not "clean this back up" into `$BODY` / `$PAYLOAD` variables
for readability. Prefer the session's own scratchpad directory when the
agent has one (it is both literal and guaranteed outside every repo);
otherwise spell out a `/tmp/...` path directly:

```bash
# 1. Write the issue body to a literal scratch path using your own
#    file-write capability — NOT a shell heredoc (see below). Content is the
#    usual shape:
#      ## Context
#      <where this came up in the session / repro>
#
#      ## Suggested acceptance criteria
#      - [ ] …

# 2. Build the create payload, then POST it (REST `core` pool, not GraphQL).
jq -n --arg t "<title>" --rawfile b /tmp/followup-body.md \
  '{title: $t, body: $b, labels: []}' > /tmp/followup-payload.json

gh api --method POST "repos/<slug>/issues" --input /tmp/followup-payload.json --jq '.html_url'
```

Two reasons this is the documented form rather than
`gh issue create --body "$(cat <<'EOF' … EOF)"`:

- **Rate-limit pool.** `gh issue create` is GraphQL-backed; `POST
  repos/…/issues` is REST. GraphQL exhausts first on a busy agent host, and
  filing is the step you least want to lose — it runs *after* the user has
  already approved the set.
- **The body never re-enters the shell.** A heredoc body is still shell input:
  a line containing `>=`, backticks, or `$(…)` gets tokenized by the shell and
  by command-matching guards, which can deny the call outright. `jq --rawfile`
  reads the file as one raw string and JSON-escapes it, so markdown checkboxes,
  headings, and code fences survive verbatim.

The payload's `labels` array is where labels would go if a variant ever needed
them — applied atomically with creation, no create-then-label round trip. Leave
it `[]` here, per the labeling note below.

Print the resulting issue URLs. For near-matches the user chose to comment on
instead of file, use the same REST shape (`gh issue comment` is GraphQL-backed
too) and the same literal-scratch-path rule as above — never a `$BODY` /
`$PAYLOAD` variable as the write target:

```bash
jq -n --rawfile b /tmp/followup-body.md '{body: $b}' > /tmp/followup-payload.json
gh api --method POST "repos/<slug>/issues/<n>/comments" \
  --input /tmp/followup-payload.json --jq '.html_url'
```

Leave UNKNOWN / skipped candidates unfiled and list them so nothing is silently
lost.

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
5. **Reach the forge over REST** — dedup via `gh api search/issues`, file via
   `gh api --method POST repos/<slug>/issues --input <payload>`, and pass issue
   bodies as files (`--rawfile` / `--input`), never as inline heredocs. The
   `gh issue list` / `gh issue create` forms are GraphQL-backed and fail on
   exactly the busy multi-agent repos this command is most useful in.
