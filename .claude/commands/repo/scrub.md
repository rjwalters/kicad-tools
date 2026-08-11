---
name: "scrub"
description: "Scan a repo's public surface for sensitive identifiers — code, history, issues, PRs and forks — and report what can and cannot actually be removed"
domain: repo
type: command
user-invocable: true
---

# /repo:scrub — Public-Surface Scrub

Answer one question the other hygiene commands don't: **is there anything here
that should not be public?**

That gap matters most where agents author content continuously. An agent
writing an issue, a runbook, or an incident write-up has no instinct for which
identifiers are sensitive, and those artifacts are exactly the genre where
account IDs, resource IDs, internal hostnames, and email addresses accumulate.
None of it is a credential; all of it is useful to an attacker. The drift is
slow and invisible, which is why it needs a check on a routine cadence rather
than a person remembering to look.

**This command reports. It never edits.** Same posture as [[orphans]]: name the
file, commit, issue, or PR and the class, and stop. Deleting an issue or
rewriting history is irreversible and is an operator decision — and, as the
removability section below explains, usually a less complete fix than the
operator assumes.

## Usage

```
/repo:scrub                      # This repo, default verbosity — credentials and live-at-HEAD findings
/repo:scrub --deep               # Add history-only and network-topology classes
/repo:scrub --owner <owner>      # Every public repo for an owner/org, enumerated from the forge
/repo:scrub --forks              # Also sweep the fork network (delegates to repo-scrub-forks.sh)
/repo:scrub --json               # Machine-readable findings
```

`/repo:all` runs the **default form only** — this repo, quiet verbosity, no
forge enumeration and no fork sweep. The wide forms cost API calls and minutes,
and belong in a deliberate invocation.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Clean — no findings at or above the reporting threshold |
| `1` | Findings **at HEAD** |
| `2` | Could not check — no `gh`, not authenticated, API failure, scan aborted |

**History-only findings never set exit 1 on their own.** Nearly every repo with
any history has some, none of them fixable without a rewrite nobody is going to
perform, so failing on them means every repo fails forever and the signal is
worth nothing. Code `2` is *inconclusive* and must never be reported as clean.

## Scope — the full public surface

The working tree being clean proves nothing. Check all of it:

- **Tracked files at HEAD**
- **Commit history** — every reachable commit, including messages and trailers
- **Issue bodies and comments**
- **PR bodies and review comments**
- **The fork network** — under `--forks`; see Forks below

### Enumerate from the forge, never from a manifest

Under `--owner`, take the repo list from the forge API (`gh repo list <owner>
--visibility public`), not from any checked-in inventory. A manifest-driven
scan inherits the manifest's blind spots, and the repos missing from a manifest
are exactly the forgotten ones most likely to carry stale content. A real audit
scoped to a hand-maintained manifest covered **6 of 58** public repos, and the
two carrying the most sensitive content were absent from it — surfaced only by
accident, through an exact-phrase code search.

### Confirm visibility on every search hit

An **authenticated** code search returns private repos. Every hit must have its
repository visibility confirmed before it counts as a public disclosure.
Reporting private-repo content as a leak is how the tool stops being trusted.

### Scan vendored trees by default

Do **not** exclude `.loom/`, `.anvil/`, or similar copy-installed tool trees.
Excluding them sounds reasonable — "not our code" — and is precisely backwards:
a vendored tree is *copied into repos the source project's own audit never
covers*. It is the one directory guaranteed to propagate content sideways into
places nobody tracks. In one sweep, vendored trees carried a real address in
**12 public repos** the rest of the audit had already called clean, plus the
installing machine's absolute path in ~25.

If the volume is a problem, **rank findings there lower — never drop them.** And
if this command ever ships a default exclusion list (`node_modules/`, `vendor/`,
`third_party/`), justify each entry against this failure mode: "it isn't ours"
is not the same as "it can't leak ours."

Findings inside a vendored tree usually **cannot be fixed in that repo** — the
next reinstall overwrites the edit. Say so in the finding and point at the
upstream source rather than suggesting a local edit that will silently revert.

## Detection classes

| Class | Examples | Default verbosity |
|---|---|---|
| Credentials | `sk-ant-*`, `sk-proj-*`, `AKIA*`, JWTs, PEM blocks | Always shown — hard fail |
| Cloud resource IDs | bare 32-hex (Cloudflare account/zone), UUIDs, cloud account numbers | Shown at HEAD |
| Identity | email addresses, access-policy allow-lists | Shown at HEAD |
| Affiliated entities | employer/client/org names and their aliases | Shown at HEAD when substantive |
| Network topology | RFC1918 dotted **and** dashed-hostname forms, tailnet `100.64/10`, elastic IPs | `--deep` only |

### Credentials

Hard fail, every time, at HEAD or in history. A credential in history is still
live until rotated — see Rotation below, which is the *better* remedy here
anyway.

### Cloud resource IDs — bare 32-hex is load-bearing and ambiguous

A bare 32-hex string is the shape of a Cloudflare account ID. It is also the
shape of an md5 checksum, which appears legitimately in docs, lockfiles, and
test fixtures. A naive rule produces enough false positives to get the whole
check disabled, which is worse than not having it.

So triage on **nearby-word context**, not the bare regex: words like `account`,
`zone`, `database_id`, `namespace` promote a match; `md5`, `checksum`, `sha`,
`digest`, `integrity` demote it. When context is genuinely absent, rank it low
and say the context was absent — don't guess confidently in either direction.

### Identity — placeholders are not findings, third parties outrank you

Exclude `test@`, `you@`, `user@`, `example.com`, `*.noreply.github.com`, and
obvious throwaways (`t@t.com`). Reporting these is pure noise.

Conversely, a **third-party** address — a real person outside the org — ranks
*above* the operator's own address, not below it. The operator publishing their
own email is a choice they already made; publishing someone else's is not
theirs to make.

### Affiliated-entity identifiers

Employer, client, and organization names — the class that is sensitive for
*this* repo and meaningless for the next one. It therefore needs per-repo
configuration (see Allowlist and configuration), with two requirements:

- **Alias expansion.** One entity appears as a slug, a domain, prose with
  spaces or punctuation, and a service hostname. Matching only the canonical
  spelling misses most real occurrences.
- **Substantive vs incidental.** A name in a package `authors` field or a
  historical commit trailer is incidental; the same name in a deployment
  runbook describing internal topology is substantive. Rank them apart. A flat
  list of every occurrence is a large delete-and-refile operation nobody will
  perform, so it gets skipped wholesale.

### Network topology

Two forms, both required — a dotted-quad regex alone misses AWS internal
hostnames entirely:

```
\b10\.\d+\.\d+\.\d+\b      \b172\.(1[6-9]|2\d|3[01])\.\d+\.\d+\b      \b192\.168\.\d+\.\d+\b
ip-10-0-1-5                ip-172-31-74-176                           (dashed-hostname form)
```

Plus tailnet `100.64.0.0/10` and elastic IPs. Low severity, high volume —
`--deep` only, and summarized as a count in the default report.

## Severity gates verbosity, not just ordering

This runs inside `/repo:all`, which is routine and mostly clean. **A check that
emits a wall of low-severity findings on every run gets skimmed, then ignored,
and it takes the rest of `/repo:all` down with it in credibility.**

This is not hypothetical. Across ~20 repos and ~5,150 commits, internal
hostnames in `Co-authored-by:` trailers alone accounted for **60+ commits in 8
repos**. Every one a correct finding. Every one unfixable without a history
rewrite nobody is going to do. Emitting them on every run is noise wearing a
security badge.

So the default report carries **credentials and live-at-HEAD findings only**.
History-only and topology findings collapse to a one-line count with a pointer
to `--deep`.

## HEAD vs history is the load-bearing distinction

Never merge these into one count. They are different findings with different
remedies:

- **Live at HEAD** — fixable by an ordinary commit. Sets exit code `1`.
- **History-only** — reachable only through history. Needs a rewrite, and the
  right answer is usually **rotate the value**, not rewrite the repo. Does not
  set exit `1` on its own.

## Removability — say what cannot be removed

Every finding carries a **removability class**. Without one, the report
implicitly promises a fix that may not exist, and an operator who force-pushes
a rewrite believing it cleaned the repo has a *less* accurate picture than
before they started.

| Surface | Class | Mechanism |
|---|---|---|
| Working tree | `removable` | Ordinary commit |
| Branch/tag history | `removable` | `git filter-repo` + force-push |
| Issue body/comment | `removable-by-deletion` | Only by deleting the issue — editing leaves the original in `userContentEdits`, publicly queryable via GraphQL |
| **PR body/comment** | `permanent` | No delete endpoint exists for pull requests; `gh pr` offers only `close` and `edit`, and editing preserves the original exactly as for issues |
| **`refs/pull/*`** | `permanent` | Server-side hidden refs. `git push --delete refs/pull/1/head` returns `deny updating a hidden ref`. They pin every pre-rewrite commit, so old content stays fetchable by SHA after a full history rewrite |
| **Forks** | `permanent` | See Forks below |
| Published registries | `permanent` | crates.io/PyPI versions are immutable; yanking does not remove metadata |

An unrecognized surface type is an **error, not a default** — fail loudly rather
than silently classifying something as `removable`.

### "Cleaned" is a forbidden word unless every surface was checked

After a history rewrite the correct summary is: *clone and grep are clean; PR
refs and any forks still serve the original.* Anything shorter misleads. On one
real repo a full rewrite left **461 `refs/pull/*` refs** pinning the old
commits — a `--mirror` clone still saw every one of them while a normal clone
saw none.

Bake this into the emitted report string, not just the docs: a rewrite-only
report must **name the surfaces it did not check** rather than implying
totality.

### Rotation over removal, where the value is rotatable

For an exposed credential, account identifier, or resource ID, rotating is
cheaper *and more complete* than any rewrite — it invalidates every copy at
once, including forks and published registry versions that no rewrite can
reach. Recommend it first.

Rotation is a **recommendation attached to the value**, orthogonal to the
removability class. A finding can be `permanent` and still carry "rotate this."
Don't conflate the two axes.

## Forks

Search cannot see forks — GitHub's code search and repository search both
exclude them by default, and for code search this cannot be turned off. So any
tool built on search is structurally blind to exactly the copies it most needs
to find. A real sweep reported a repo clean while two public forks carried the
content.

Under `--forks`, delegate to `scripts/repo/repo-scrub-forks.sh` (installed to
`.claude/skills/repo/scripts/`), which walks the forks API recursively rather
than searching — a fork of a fork is a distinct copy and depth cannot be
assumed to be 1. Its findings are `permanent` by definition: outreach is the
only lever, and the report should never suggest a fix the operator cannot
perform on someone else's repository.

Note its `warn-before-private` subcommand: making a repo private **detaches**
one fork into a new network root and **re-parents** others beneath it, so the
fork list must be captured *before* any visibility change or it becomes
unqueryable afterwards — the opposite of what someone privatizing a repo for
cleanup intends.

## Verify from a fresh clone of origin

Never verify from a local or previously-verified copy, and **state which clone
type was used** — `--mirror` and normal clones legitimately disagree about what
exists. Two traps, both hit in one real session:

- A rewritten mirror was verified clean, the source then moved, and pushing the
  verified artifact would have silently dropped a later fix. **A verified
  artifact goes stale the moment the source moves.**
- Local tags do not update on `git fetch --prune` (they need `--force`), so
  verification against local refs produced entirely spurious failures — and
  would equally have hidden real ones.

## Allowlist and configuration

An allowlist is **mandatory, not optional.** Deliberate metadata — a package
`authors` field, a documented example address, a fixture checksum — is
intentional, and re-reporting it forever is precisely how the check gets muted
wholesale.

Read per-repo configuration from `.repo/scrub.toml` (same `.repo/` convention
as [[release]]'s policy file): confirmed-benign matches, and the
affiliated-entity names and aliases for this repo. Absent the file, the
affiliated-entity class simply does not run — it has nothing to match on, and
guessing at org names from the remote URL produces noise.

## Report

Group by class, then by severity, consistent with [[audit]]'s critical/warn/info
levels. Every finding carries: surface, location, class, **removability class**,
and recommended action.

```
REPO:SCRUB — rjwalters/repo
===========================
CRITICAL  none

WARN      2 findings at HEAD
  identity          docs/runbook.md:41        removable          third-party address — not yours to publish
  cloud-resource    .anvil/config.example:8   removable*         32-hex near "account_id" (context: promoted)
                    * vendored tree — a local edit reverts on reinstall; fix upstream in rjwalters/anvil

INFO      history-only, not fixable by commit (63) — rerun with --deep
          network topology (18) — rerun with --deep

Not checked: PR bodies/comments, fork network (--forks)
Exit 1 (findings at HEAD)
```

Every issue/PR finding must carry the **redaction-is-not-remediation** caveat:
GitHub retains `userContentEdits`, and on a public repo the pre-edit body is
readable via the public GraphQL API. Editing a leaked issue body leaves the
original one query away — the obvious fix is the wrong one.

## If a `--fix` mode is ever added

None exists today. Two constraints on any future one, both learned expensively:

- **Never scope replacements by file extension.** A real cleanup replaced a
  string across `*.md` and missed the identical string in a `.py` library file
  and seven times in a test — including a live `assertIn`, so the fixture and
  the assertion had to move together or the suite broke. It surfaced only by
  diffing against a history-rewritten copy, which operates on content globally.
  Replacement must be extension-agnostic.
- **Run the test suite afterwards.** See above — content replacement can break
  assertions that reference the replaced content.

## Principles

Same as every hygiene command, with one addition. **Report only** — never edit a
file, issue, PR, or history ([[orphans]]' posture, for the same reason: the
remedy is a judgment call with irreversible forms). **Don't be noisy** —
severity gates verbosity, because a check that cries wolf on every routine run
is a check nobody reads. **General by design** — anything repo-specific
(affiliated entities, allowlisted values) is read from the consumer repo's own
`.repo/scrub.toml`, never hardcoded. And **never claim more cleanliness than
was verified**: name the surfaces you did not check, every time.
