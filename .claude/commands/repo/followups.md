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
upstream targets it writes into *other people's* repos, usually public ones —
so this command is in the same "always confirm first" class as `release`,
`remote`, and `update-tools`, never the auto-apply behavior of the hygiene
commands. **Confirmation is the default and only mode; there is no `--ask` flag
because there is nothing to opt into.**

Mining a private session and publishing into a public repo is a visibility
boundary crossing, so every cross-repo candidate is scrubbed at authoring time
against [[scrub]]'s detection classes before it is ever proposed — see step 3b.

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

  **Self-target short-circuit: Repo Skills never needs the ladder.** One of the
  swept roots is `.claude/skills/repo/` — Repo Skills' own tool root (per the
  tool-root table in the [installer contract][contract]), the tool that *is*
  `/repo:followups` running right now. Unlike Loom / Anvil / kicad-tools, which
  are genuinely-external dependencies with no guarantee a local source clone
  exists anywhere on this machine, Repo Skills already writes its own repo slug
  into every copy of itself it installs: the installed
  `.claude/skills/repo/SKILL.md`'s own contract link and the `CLAUDE.md`
  install block (`install.sh:786`) both carry the literal
  `https://github.com/rjwalters/repo`. When the swept metadata path is
  `.claude/skills/repo/install-metadata.json`, resolve its target from that
  literal directly and skip the sidecar → legacy → unknown ladder below
  entirely — there is no source clone to look for, and this target is never
  surfaced to the operator as UNKNOWN:

  ```bash
  grep -oE 'github\.com/[^)/]+/[^)/]+' .claude/skills/repo/SKILL.md | head -1 \
    || echo "github.com/rjwalters/repo"   # fallback constant, mirrors install.sh:786
  ```

  This is intentionally **not fork-aware** — a fork of Repo Skills that renames
  itself without also editing its own copy of `SKILL.md` still resolves to
  `rjwalters/repo` — matching the same non-fork-aware literal `install.sh:786`
  already writes into every consumer's `CLAUDE.md`. That is a pre-existing
  limitation of how Repo Skills documents itself, not a new gap introduced by
  this short-circuit.

  Resolve every **other** discovered tool root's `source` clone path with the
  **sidecar → legacy inline → unknown** order that is normative in the
  [tool-package installer contract][contract] (requirement **C6**, which also
  covers the repo#96 signature below). Each step failing is "source unknown",
  not an error. Do not re-derive the order from what a given tool happens to
  do — read C6.

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

  **Loom's sidecar is named differently, but still conforms to C6's ladder.**
  Where C6 step 1 names the sidecar `<tool-root>/.install-local.json`, Loom's
  actual sidecar is the plain-text `.loom/loom-source-path` (a single path,
  not JSON) — check that file, not `.install-local.json`, when resolving
  Loom's source clone here. Full detail: `/repo:update-tools` step 1.

  Then derive the slug from the resolved clone's remote:

  ```bash
  git -C <source> config --get remote.origin.url   # → owner/repo for gh --repo
  ```

  `install-metadata.json` (tracked) is JSON, and key names vary by tool
  (`version` vs `loom_version` / `anvil_version` / `kct_version`, etc.) — read
  whichever variant is present, same as `/repo:update-tools`. Neither the
  tracked metadata nor the sidecar stores an `owner/repo` slug directly for
  Loom / Anvil — it is always derived from the source clone's `origin` remote.
  kicad-tools' `source_ref` in `"git"` mode and Repo Skills' self-target
  short-circuit above are the two exceptions, since each already has the full
  GitHub URL available to parse without a source clone.

- **Unresolvable targets.** If a tool's source clone is unknown (no sidecar, no
  legacy field) there is no local remote to read — mark that follow-up
  **UNKNOWN** and surface it for the user to name a slug, per the safety rules.
  Likewise, if a candidate doesn't clearly belong to any discovered repo,
  surface it for a target decision rather than dropping it or guessing. Repo
  Skills itself never reaches this branch — the self-target short-circuit above
  always resolves it before the ladder runs, so it is never surfaced to the
  operator as UNKNOWN.

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
  --jq '.items[] | "#\(.number) [\(if .pull_request then "PR" else "issue" end)] \(.title) \(.html_url)"'
```

**A dedup query MUST carry the match type, not only the URL.** The
`[\(if .pull_request then "PR" else "issue" end)]` expression above is
load-bearing, not decoration: it is the only field that classifies each row
*as output*, without the reader having to notice `/pull/` inside a URL. If you
write your own `--jq` instead of using this one, it **must** emit the type as
well as `html_url` — a query that prints only `#number title` is not a valid
dedup query for this step, however convenient its output looks. This rule is
not hypothetical: a live run hand-rolled a `#number title` query, lost the
type, reported an open PR as a duplicate issue, and closed it (repo#515).

**Pull requests are deliberately in scope.** `search/issues` returns both
issues **and pull requests** — the `issues` in the route name is GitHub's
issue-tracker sense of the word, and the step title uses it the same loose way.
This is intentional and is *not* narrowed with `+is:issue`: an open PR covering
a candidate is a **stronger** dedup signal than an open issue, because it means
the work is already in flight rather than merely proposed. Filtering PRs out
would discard exactly the signal that matters most for "don't re-file something
already being worked on". The cost of the wider result set is absorbed by
safety rule 2 — near-matches are always flagged to the user, never
auto-skipped or auto-filed-over — and by the type expression above, which
states which kind each match is instead of leaving it to be inferred from
`html_url`.

Search result items already carry `number`, `title`, and `html_url` — plus a
`pull_request` object that is present only on pull requests, which is what the
type expression tests — so this is a straight replacement for the old
`--json number,title,url` output shape with no second-pass mapping needed.
That parity covers the output **shape** only, not the result **set**: the old
`gh issue list --search` form returned issues only, while this one is
deliberately broader (see above).

Terms go into `q` as `+`-joined tokens; URL-encode anything that isn't
alphanumeric, and quote the whole URL so the shell leaves it alone.

**Why not `gh issue list --search`:** it goes through GitHub's GraphQL API,
whose rate-limit bucket is separate from REST's and is routinely exhausted on a
busy multi-agent host while the `core` budget sits nearly unused. `search/*` is
a third bucket again (30 requests/minute authenticated), so deduping here costs
nothing from the pool step 5 needs to actually file. Check live budgets with
`gh api rate_limit --jq .resources` if either step starts failing.

Classify each candidate against its target repo's open issues **and pull
requests** (the search returns both, per the note above). **Every
classification names the match type** — there is no type-less near-match:

- **New** — no match; propose to file. Reported as `NEW`.
- **Near-match on an issue** — a similar *issue* exists; **flag it for the
  user** with the existing issue's number and URL and let them choose: file
  anyway, skip, or comment on the existing one. Reported as
  `near #N (issue)`. Never silently file over it or silently drop it.
- **Near-match on a pull request** — the match is a PR, so the work is already
  **in flight**. Reported as `in flight: #N (PR)` — never as a "duplicate",
  and never with the word *duplicate* attached to a PR number at all. A PR
  match argues *more* strongly for skip-or-comment than an issue match does.
  Commenting works either way (`POST /issues/<n>/comments` accepts a PR
  number), but on a PR the comment lands in that PR's conversation rather than
  on a standalone issue, so confirm that's what the user wants.

**Verify the type before you write a number into a row.** The search output's
type expression is the primary signal; when a number reaches this step from
anywhere else (an earlier turn, the operator, a hand-rolled query), re-derive
it from the forge before classifying:

```bash
gh api "repos/<slug>/issues/<n>" --jq '.pull_request != null'   # true => it is a PR
```

`gh issue view <n>` is **not** a type check: it accepts a PR number and none
of `title`/`state`/`createdAt` reveals which kind it is. Neither is
`gh issue close` / `gh issue comment` — both act on a PR number without
warning, and `gh issue close` prints `✓ Closed issue …` for a closed *pull
request* (repo#515).

**An implementation is not a duplicate.** If one item lists the other in its
`closingIssuesReferences`, the PR *implements* that issue — that is the
strongest possible "already in flight" signal and the weakest possible case
for treating either as redundant. Report it as `in flight`, never as a
duplicate of the thing it implements.

### 3b. Scrub cross-repo candidates before they are proposed

Candidates are mined from a **private working session**; every target that is
not this repo is someone else's repo, usually a **public** one. That is a
visibility boundary crossing, and it happens at *authoring* time — by the time
a body reaches the confirmation table in step 4 the sensitive value is already
written into it. In a live run (2026-08-11) the drafted upstream bodies carried
the consumer org/repo name, exact fleet sizes, and per-session operational
counts, and came out only because the operator happened to read them before
approving.

Filing is also the point of no return. Per [[scrub]]'s removability table an
issue body is `removable-by-deletion` — editing it leaves the original in
`userContentEdits`, publicly queryable — and a PR comment is `permanent`. There
is no post-filing redaction that actually removes anything, so the scrub has to
happen here, before the set is proposed.

**Scope: every candidate whose target repo is not this repo.** That is each
upstream target from step 2, plus any UNKNOWN once the user names a slug for
it. `--here` keeps only this-repo targets and therefore **skips this step
entirely** — filing into the repo the session is already working in crosses no
boundary. A `--repo <tool>` run is still cross-repo and is in scope.

Resolve each distinct target's visibility once, and carry it into step 4's
`Vis` column:

```bash
gh repo view <slug> --json visibility --jq '.visibility | ascii_downcase'
```

**Reuse [[scrub]]'s detection classes — do not restate them here.** Read every
candidate's title and body against the **Detection classes** table in [[scrub]]
— credentials, cloud resource IDs, identity, affiliated entities,
network topology — and apply that command's per-class triage as written there,
including the affiliated-entity names and aliases it reads from this repo's
`.repo/scrub.toml` (absent that file, that one class has nothing to match on,
and the rest still apply). This step deliberately adds no rules of its own to
those classes and keeps no second copy of them — two copies drift, and the
copy living in the consuming command is the one nobody updates. Two things
differ from a [[scrub]] run: the surface is **unpublished draft text** rather
than the repo, and a finding is **rewritten before the draft is shown**, not
merely reported.

Three classes are specific to session-mined text and exist only in a draft, so
[[scrub]]'s table does not cover them:

| Session-specific class | Examples | Rewrite as |
|---|---|---|
| Consumer identity | the consumer org/repo slug and its aliases, branch/worktree names, machine paths (`/Users/<name>/…`, `/home/<name>/…`), hostnames, account names | "a consumer repo"; a repo-relative path |
| Environment fingerprint | fleet/pool/agent counts, token-pool size, per-session operational counts, timings precise enough to identify the host | drop, or go qualitative — "repeatedly", "on a busy multi-agent host" |
| Session identifiers | session / run / sweep IDs, agent or terminal names, transcript paths, issue and PR numbers belonging to a private repo | drop |

**The bar follows the target's visibility**, which is why step 4 shows it:

- **public** — the full bar above. Every word is world-readable and permanent.
- **private** — a lower bar, not an exemption: the reader set is still not this
  session's. Credentials and third-party identity never travel; an environment
  fingerprint usually may.
- **unknown** — a visibility lookup that failed is treated as **public**. Fail
  closed; an unresolved target is never given the private bar by default.

**Preferred citation style for a public target: point at the value, never quote
it.** When a candidate genuinely has to reference something sensitive, cite it
by `path:line` in a file the *receiving* maintainer can open — their own repo's
tree — and describe the value's shape instead of reproducing it:

```
Prefer:  the fallback path is hardcoded at scripts/spawn.sh:212 rather than read from config
Avoid:   the fallback path is hardcoded as "/Users/<name>/work/<org>-infra/bin/spawn"
```

This is the same posture [[scrub]] takes with its own findings — report the
location and the class, never the value — and it keeps the body actionable:
the maintainer opens the line in their own checkout. When the value lives only
in the consumer's tree, which the maintainer cannot open, name the file's
**role** ("the consumer's installed metadata file") rather than its path.

A candidate that cannot be written at all without a sensitive value is **not
quietly dropped and not filed as drafted** — carry it into step 4 marked `HOLD
— needs redaction` and let the user decide, exactly as an UNKNOWN target is
surfaced rather than guessed at.

### 3c. Detect source-repo confidentiality

3b resolves how public each *target* is. This step checks the other side of
the same crossing: whether the *source* — the repo this session is running
in — has declared itself confidential or pre-disclosure. A repo can say so in
its own root `CLAUDE.md` with language like "confidential", "do not
disclose", "private", "before a provisional is filed", or "pre-disclosure" —
a firewall rule on outward writes in general, `/repo:followups` included. In
one real run the source repo's `CLAUDE.md` opened with exactly that kind of
line, and nothing in the flow surfaced the mismatch before filing.

**Runs once per session, not once per candidate.** Read this repo's root `CLAUDE.md`,
if one exists, and scan it case-insensitively for any of:

- "confidential"
- "do not disclose"
- "private" (the disclosure sense, in `CLAUDE.md` prose — distinct from a
  `Vis` value of `private` on a target, which 3b already handles)
- "before a provisional is filed"
- "pre-disclosure"

Also honor a structured opt-in in `.repo/scrub.toml` — the same per-repo
config file [[scrub]] already reads for its affiliated-entity allowlist (see
[[scrub]]'s "Allowlist and configuration") — if this repo sets a
confidentiality flag there (e.g. `source_confidential = true`), treat that as
authoritative and skip the keyword scan; a deliberate flag is a more reliable
signal than prose matching and should not need re-confirming every session.

`.repo/scrub.toml` also accepts a third, stricter value: the **string**
`source_confidential = "block"` (distinct from the boolean `true` above).
Where the boolean only strengthens step 4's warning, `"block"` changes step
4's default disposition for `public`/`unknown` rows — see step 4 below. This
is a separate, deliberate opt-in, not a stronger keyword match: the CLAUDE.md
keyword scan above never sets block mode on its own, no matter how many
confidentiality phrases it matches, because — as the next paragraph explains —
a keyword scan cannot tell a real firewall statement apart from an unrelated
use of the same word, which is a poor basis for something that changes what
gets filed rather than just what gets printed. Only an explicit
`.repo/scrub.toml` opt-in can select block mode. The default (no
`source_confidential` key) and the boolean `true` case are unaffected by this
paragraph and keep today's warn-only behavior exactly as described below.

**When in doubt, warn** — this follows the same fail-closed convention step 3b
already uses for an unresolved target visibility. A match on any phrase above
sets `source-looks-confidential = true` regardless of the surrounding context
— this step does not try to tell a real firewall statement apart from an
unrelated use of the same word. A false positive costs one extra line in step
4's preamble; a false negative is the exact leak this step exists to catch.
The one case that does **not** set the flag is a **missing** `CLAUDE.md` — no
file means no signal to read, not a signal to assume one way or the other.

This step reads no candidate bodies and re-derives no target visibility — it
produces exactly one repo-wide boolean (`source-looks-confidential`), plus a
second, independent repo-wide flag (`source-block-mode`, set only by the
`.repo/scrub.toml` `"block"` string above and never by the keyword scan), that
step 4 combines with 3b's already-resolved `Vis` column.

### 4. Report the proposed set and confirm

Present the full proposal and get explicit approval before touching any repo.

**If step 3c found `source-looks-confidential = true`, print a distinct,
louder warning immediately above the table whenever at least one row's `Vis`
is `public` or `unknown`** — `unknown` is already treated as public per 3b's
fail-closed rule, so the warning follows that same treatment rather than
requiring a literal `public` cell. This is additive to the `Vis` column, not
a replacement for it, since `Vis` alone reports the target's visibility and
says nothing about whether the *source* considers itself off-limits:

```
⚠️  SOURCE REPO LOOKS CONFIDENTIAL — filing to a PUBLIC repo below. This
    repo's CLAUDE.md carries a confidentiality/pre-disclosure signal (step
    3c); the body of any row marked `public` will be publicly visible once
    filed. Advisory only — review before approving, filing is not blocked.

FOLLOW-UPS FROM THIS SESSION
============================
| # | Target repo        | Vis     | Title                              | Dedup                    |
|---|--------------------|---------|------------------------------------|--------------------------|
| 1 | rjwalters/repo     | public  | orphans check misses nested dirs   | NEW                      |
| 2 | rjwalters/loom     | public  | worktree.sh fails on detached HEAD | near #217 (issue)        |
| 3 | rjwalters/repo     | public  | followups dedup also matches PRs   | in flight: #99 (PR)      |
| 4 | rjwalters/anvil    | public  | (docs gap) …                       | NEW                      |
| 5 | UNKNOWN            | unknown | kicad-tools DRC false positive     | ask — no slug            |
```

When 3c found no confidentiality signal, or every row's `Vis` is `private`
(the one case that is not treated as public), show the table alone with no
preamble warning — the ordinary confirm is sufficient.

**If step 3c set `source-block-mode = true` (the `.repo/scrub.toml`
`source_confidential = "block"` opt-in), replace the warning above with a
hold instead** — this is a different posture from the advisory warning, not a
louder version of it. Every row whose `Vis` is `public` or `unknown` is marked
`HOLD — source firewall` in the table by default, the same convention step 3b
already uses for `HOLD — needs redaction`, and that row is **not** filed by
the ordinary "approve the set" confirmation below — filing it requires the
operator to give a separate, explicit per-run override naming the held row(s)
after being told the hold exists, not a bare "yes" to the table as a whole:

```
🔒 SOURCE FIREWALL ACTIVE — this repo's .repo/scrub.toml sets
    source_confidential = "block". Every row below marked HOLD — source
    firewall is held by default; approving the set below does NOT file
    it. To file a held row anyway, say so explicitly for that row number
    after this warning — a plain "yes" to the table is not an override.

FOLLOW-UPS FROM THIS SESSION
============================
| # | Target repo        | Vis     | Title                              | Dedup                   | Hold                    |
|---|--------------------|---------|------------------------------------|--------------------------|-------------------------|
| 1 | rjwalters/repo     | public  | orphans check misses nested dirs   | NEW                     | HOLD — source firewall  |
| 2 | rjwalters/repo     | private | internal note about this repo      | NEW                     | —                       |
```

A row targeting this repo, or any row whose `Vis` is `private`, is never held
by block mode — only `public`/`unknown` rows are, the same fail-closed set the
advisory warning above already targets. `--dry-run` still stops before any
filing, block mode included, and does not by itself count as reviewing (let
alone overriding) a hold.

For each proposed issue show the target repo, its visibility, title, a body
preview (context / repro / suggested acceptance criteria), and dedup status.
Then confirm which to file. **If `--dry-run` was passed, stop here — file
nothing.**

The `Vis` column is step 3b's resolved visibility — `public`, `private`, or
`unknown` — and it is what tells the user (and anyone reading the transcript
later) which scrub bar each row was held to. Show the **scrubbed** body in the
preview, note which rows were rewritten, and mark any `HOLD — needs redaction`
row so an unscrubbable candidate is decided on rather than skimmed past. A row
targeting this repo still shows its visibility but carries no scrub obligation
(step 3b skips it) — say so rather than leaving the cell blank.

Like the rest of this command's confirmation gate, the step 3c warning is
**purely advisory** in the default case (no `.repo/scrub.toml`
`source_confidential` key) and in the boolean `source_confidential = true`
case — it never blocks filing and never auto-redacts a body; it exists only to
put the source/target mismatch in front of the user at the moment they decide,
the same "confirm, never auto-apply" posture the rest of the command already
holds. Block mode (`source_confidential = "block"`), described just above, is
the one deliberate exception: it is opt-in, structured, and never triggered by
the CLAUDE.md keyword scan on its own.

The `Dedup` column carries step 3's classification: `NEW`, a match, or `ask`
for an unresolved target. **A match cell always states the type** — step 3
searches open issues *and* open pull requests on purpose, so the cell is
`near #N (issue)` or `in flight: #N (PR)` (rows 2 and 3 above), never a bare
`near #N` and never the word "duplicate" against a PR number. A PR match means
the work is already in flight, which usually changes the user's choice; a
type-less cell is what let a live run propose closing an approved PR as a
duplicate issue (repo#515).

**This table proposes filings, not actions on existing items.** A match row
records what exists and hands the decision to the user; it is never a
"close #N" / "relabel #N" instruction, and approving the table never authorizes
one. See safety rule 9.

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
#
#      <!-- loom:requested-by login=<login> via=<session|agent> -->
#
#    The last line is the requested-by marker (see "Attribute the requester"
#    below) — the real one, on its own line, outside any code fence.

# 2. Verify the body carries exactly one effective requested-by marker outside
#    code fences; do not build the payload until this exits 0.
awk '/^[[:space:]]*(```|~~~)/ { f = !f; next } !f && /^[[:space:]]*<!-- loom:requested-by login=[^ ]+ via=(session|agent) -->[[:space:]]*$/ { n++ } END { exit (n == 1 ? 0 : 1) }' /tmp/followup-body.md

# 3. Build the create payload, then POST it (REST `core` pool, not GraphQL).
jq -n --arg t "<title>" --rawfile b /tmp/followup-body.md \
  '{title: $t, body: $b, labels: []}' > /tmp/followup-payload.json

gh api --method POST "repos/<slug>/issues" --input /tmp/followup-payload.json --jq '.html_url'
```

**Attribute the requester.** Every newly filed issue body MUST end with exactly
one requested-by marker, written into the scratch body before the payload is
built, for this-repo and upstream targets alike:

`<!-- loom:requested-by login=<login> via=<session|agent> -->`

A dashboard reading the filed issue otherwise sees only the token's login,
which on a shared host is usually the operator's even when an agent started the
follow-up on its own (repo#556). Resolve `login` and `via` from the
**invocation context**, once per run:

- **Operator session** — a human operator ran `/repo:followups` in this
  session: `login=<operator GitHub login> via=session`.
- **Agent-originated** — an autonomous agent ran it as part of its own work
  (a Loom role such as builder, doctor, or champion, with no operator turn
  asking for it): `login=<initiating role> via=agent`, e.g.
  `login=builder via=agent`. This holds even when the agent and the operator
  share the same token.

**The token's authenticated login alone does not establish the requester.**
`gh api user --jq .login` names whoever owns the token, not who asked; never
infer a human requester from a shared agent token, and never mark agent work
`via=session`. If a human requester is established but their GitHub login is
not known, ask for it before filing (the token's login may be offered as a
suggestion, not assumed) rather than inventing one. If an agent-originated run
cannot name its initiating role, leave its candidates unfiled and list them,
the same way an UNKNOWN target is handled. Attribution never bypasses step 4:
an unattended run still needs the same explicit prior authorization to file.

The marker is appended after step 3b's scrub and carries only the login and
`via` — no session, sweep, or host identifiers. Show it in step 4's body
preview so the operator approves the attribution along with the body. **Only
the real marker counts:** a marker inside a fenced example does not attribute
anything, and any requested-by line the drafted body already carries outside a
fence (quoted from the session, say) is removed so the appended one is the only
effective marker — step 2 of the recipe above fails on zero or on more than
one. Because `jq --rawfile` serializes the file verbatim, the marker reaches
the POSTed `body` unchanged. Comments posted on existing matches (below) are
outside this rule and carry no marker.

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

**Re-check the type immediately before posting**, even if step 3 already
classified it — the comment endpoint accepts a PR number silently, and on a PR
the comment lands in that PR's conversation:

```bash
gh api "repos/<slug>/issues/<n>" --jq '.pull_request != null'   # true => it is a PR
```

**Those two POSTs are the only writes this command makes.** Creating a new
issue, and — when the user explicitly picks it for a match — posting one
comment. `/repo:followups` **never closes, edits, relabels, reopens, assigns,
or otherwise changes the state of an existing issue or pull request**, and
there is no flag, mode, or approval phrasing that unlocks one: it files, or it
reports. If a candidate duplicates an existing **issue**, the row says so and
closing that issue is the user's call, made outside this command. If the match
is a **pull request**, there is nothing to close at all — the work is in
flight. `gh issue close` is not part of this command's vocabulary; a run that
reaches for it has left the command's rules, which is exactly how an approved
PR was closed as a "duplicate" in repo#515.

Leave UNKNOWN / skipped candidates unfiled and list them so nothing is silently
lost.

Filed issues are triaged like any other afterward — this command does not apply
`loom:*` or other pipeline labels.

## Safety Rules

1. **Never file without confirmation** — present the full proposed set (target
   repo, title, body preview, dedup status) and file only what's approved.
2. **Dedup before filing** — check open issues **and pull requests** in each
   target repo; show matches, typed, and let the user decide file / skip /
   comment-on-existing.
3. **Never guess a target repo** — unresolved or ambiguous targets are reported
   as UNKNOWN for the user to name, never filed to a guessed slug.
4. **`--dry-run` files nothing** — pure proposal mode for review.
5. **Reach the forge over REST** — dedup via `gh api search/issues`, file via
   `gh api --method POST repos/<slug>/issues --input <payload>`, and pass issue
   bodies as files (`--rawfile` / `--input`), never as inline heredocs. The
   `gh issue list` / `gh issue create` forms are GraphQL-backed and fail on
   exactly the busy multi-agent repos this command is most useful in.
6. **Scrub before proposing, not before filing** — every candidate targeting
   another repo is scrubbed at authoring time (step 3b) against [[scrub]]'s
   detection classes plus the session-specific ones, and a target whose
   visibility cannot be resolved is treated as public. An issue body is only
   `removable-by-deletion` and a PR comment is `permanent`, so there is no
   after-the-fact fix — the operator noticing in the confirmation table is a
   backstop, never the mechanism.
7. **Warn, never block, on a confidential source filing to a public target** —
   step 3c checks this repo's own `CLAUDE.md` (and any `.repo/scrub.toml`
   opt-in) for a confidentiality/pre-disclosure signal; when one is found and
   any row's `Vis` is `public` or `unknown`, step 4 shows a distinct warning
   above the table, additive to the `Vis` column. This is advisory only in the
   default case (no `.repo/scrub.toml` `source_confidential` key) and in the
   boolean `source_confidential = true` case, exactly like the rest of this
   command's confirm-first posture — it never blocks filing and never
   auto-redacts a body in those cases, and an unreadable or ambiguous
   `CLAUDE.md` fails closed to warning rather than silently skipping it.
8. **Hold, don't warn, when the source repo opts into block mode** —
   `.repo/scrub.toml` may set `source_confidential = "block"`, a separate
   structured opt-in from rule 7's boolean/default case. When set, step 4
   marks every `public`/`unknown` row `HOLD — source firewall` by default and
   does not file it under the ordinary set-level confirmation; filing a held
   row requires an explicit per-run operator override naming that row. The
   CLAUDE.md keyword scan alone never selects block mode — only the
   structured `.repo/scrub.toml` opt-in does.
9. **Never act on an existing issue or pull request** — this command **files,
   or it reports**. It never closes, edits, relabels, reopens, or assigns an
   existing item, and its only writes are creating a new issue and posting one
   comment on a match the user explicitly chose to comment on (step 5).
   Approving the step 4 table authorizes filings only; it is never consent to
   close or modify anything. A candidate that duplicates an existing **issue**
   is reported as such and the close is the user's, made outside this command.
   `gh issue close` has no place in this command at any step.
10. **Classify every match by type, and verify the type before using a
    number** — step 3's dedup query must emit the type inline
    (`[PR]`/`[issue]`), not just `html_url`, and every `Dedup` cell states it:
    `near #N (issue)` or `in flight: #N (PR)`. A pull-request match is **in
    flight**, never a "duplicate". Any number arriving from outside that query
    is re-checked with
    `gh api repos/<slug>/issues/<n> --jq '.pull_request != null'` before it is
    reported or commented on — `gh issue view` / `close` / `comment` all accept
    a PR number without saying so — and an item that lists the other in
    `closingIssuesReferences` is an implementation, not a duplicate.
11. **Attribute every filed issue to its requester** — each new issue body
    (this repo or upstream) ends with exactly one
    `<!-- loom:requested-by login=<login> via=<session|agent> -->` marker
    outside any code fence, resolved from the invocation context per step 5:
    the operator's GitHub login with `via=session`, or the initiating role with
    `via=agent`. The token's login alone never establishes the requester.
