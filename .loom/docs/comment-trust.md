# Comment Trust: whose text counts as a control signal

Loom must be able to manage **any** repository, including public ones that
accept contributions from anyone. Everything an outsider can write (comments,
reviews, issue and PR bodies, fork branches) is therefore untrusted input. This
page states the rule for the narrow slice of that text Loom *acts on*: HTML
markers such as `<!-- loom:verdict-sha sha=… verdict=… -->` and control phrases
such as `Champion Review: APPROVED` (#9548).

For the companion rule about text an *agent* reads (prompt injection), see
[`untrusted-external-content.md`](untrusted-external-content.md).

## The rule

**Outsider text is content, not control.** A marker or phrase that changes
Loom's behaviour counts only when the forge says it was authored by a trusted
identity. A well-formed marker from anyone else is prose: it reads exactly as
if it were absent. It can neither vouch for a verdict nor invalidate one.

A comment or review is trusted when its author is:

| Author | Why |
|---|---|
| A repo insider by `author_association`: `OWNER`, `MEMBER`, `COLLABORATOR` | They can change labels anyway. `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, `FIRST_TIMER` and `NONE` never count: one merged fork PR makes anyone a contributor. |
| One of **this** fleet's GitHub Apps, matched exactly | The fleet's Apps appear as `NONE`/`CONTRIBUTOR`, so without this rule Loom could not read its own markers. The roster is [`forge_identity::FleetLogins`](github-authentication.md): the writer, every reader, `legacyLogins`, and the `loom-fleet-dispatch(-<digits>)` default family. The author must be *spelled* as an App (`x[bot]` from REST, `app/x` from GraphQL, or a `Bot` type): a user may register the bare slug, never `x[bot]`. |
| This daemon's own identity | Compared with the same account kind: the user `x` is never the App `x[bot]`. |
| A login in `forge.trustedCommenters` | An explicit allowlist, same account-kind rule (list `helper[bot]` to allow an App). |

**Another Loom installation's markers are not ours.** A foreign fleet emits
perfectly well-formed Loom markers; its Apps are not in this roster, so they
count for nothing here.

### Trap: an admin with private org membership reads as `CONTRIBUTOR`

`author_association` reflects *public* organization membership and explicit
collaborator records, not the live permission level. A repo admin (or org
owner) whose org membership is **private**, or an outside collaborator, is
reported as `CONTRIBUTOR`, so every verdict marker they post is dropped. The
verdict-staleness pass then falls back to an older trusted marker and clears a
fresh `loom:pr` (#9709). List such reviewers explicitly in
`forge.trustedCommenters` (see Configuration below). Trust is deliberately not
widened automatically; instead, when a newer marker was dropped this way, the
stale-clear notice (from either the daemon pass or
`verdict-staleness-guard.sh --clear`, both rendered by
`loom-daemon forge verdict-stale-notice`'s template) and the daemon log line
name the login, its `author_association`, and `forge.trustedCommenters`,
rather than claiming the head SHA moved.

## Where it is enforced

The predicate lives once, in `loom-daemon/src/comment_trust.rs`:

- **Rust readers** filter at fetch. `claim_reconciliation`'s comment fetch
  returns trusted bodies only, so the verdict backstop, the stale-verdict
  dedup, and the base-conflict "is this flag ours?" check never see an
  untrusted marker. `star_liveness::trust` delegates to the same predicate.
- **Shell readers** call `loom-daemon forge trusted-comments`, which reads a
  comment listing on stdin and prints the trusted subset in the same shape.
  `verdict-staleness-guard.sh` and `check-promotion-landed.sh` use it.
  `loom-daemon forge verdict-stale-notice --label L --marker-sha M --head-sha H`
  reads the *raw* listing on stdin only to name a dropped newer marker's
  author in the stale-clear notice (attribution, never evidence); without the
  verb, the guard posts a one-line notice carrying the same dedup marker.
  Empty or whitespace-only stdin exits 1 like any other non-listing: an
  empty listing is `[]`, so nothing at all means the fetch never happened.
- **The other Rust marker readers** (#9548, High slice) filter the same way,
  through `comment_trust::records`:

  | Reader | Marker | When the author is untrusted |
  |---|---|---|
  | Lease probes (claim reconciliation, orphan recovery, dispatch tie-break, mid-build watchdog) | `loom:lease` | Not a lease: it can neither hold a claim, win the tie-break, nor fence a cleanup. |
  | Claim reconciliation | `loom:claim-activity` / `loom:standdown` | Not claimant activity: it cannot keep a dead claim alive. |
  | Review-conflict pass | `loom:base-conflict flagged` | Not "ours", so a Judge's verdict is never undone. |
  | Quarantine reconciliation | `Auto-quarantined by loom-daemon (#3939)` | Not the daemon's quarantine; the marker must also *start* the comment. |
  | Dependency classification | `champion:proposal-escalated` / `dep-cycle` / `proposal-unescalated` | Absent. |
  | `premise-check` | `loom:premise-check … verdict=` | Absent (comments; a body counts only when its author is trusted). |
  | Required-check re-date | `loom:stale-check-redate` / its hold marker | Not attempt state. |
  | Mechanical-capability lane | body `loom:capability=` | No declaration: the park stays. |
  | Role-shard roster | `loom:roster` | Not a ring member. |
  | Open-linked-PR guard | a fork PR's `Closes #N` | Not a linked PR (a same-repo branch always counts). |

  Readers that used `gh … --json comments` now read the REST listing, whose
  author spelling can name an App. A REST comment listing that comes back
  empty or unparseable is a failed read, never "no comments".

  Body markers (`premise-check`, `loom:capability=`) are trusted by the
  **issue author**, because the forge does not say who last edited a body. A
  trusted insider's marker edited into an outsider-filed body is therefore
  ignored. That fails closed: post the record as a comment instead.
- **Structural tests** fail when a new Rust file handles a covered marker
  without being reviewed (`verdict_sha_readers_go_through_the_trust_filter`,
  `structure_tests::every_covered_marker_file_is_reviewed`), and when a
  reviewed file gains a comment fetch outside its filtered call sites
  (`structure_tests::every_comment_fetch_in_a_reviewed_file_is_a_filtered_call_site`).

When the filter cannot run (no `loom-daemon`, or one that predates the verb),
the answer degrades toward safety: the verdict guard treats every marker as
absent (`UNVERIFIABLE`, never `FRESH`), names the cause in `REASON`, and
suppresses `--anchor`; the promotion backstop exits 1 and reconciles nothing.
Merge paths treat an `UNVERIFIABLE` whose reason says the markers "could not be
authenticated" as a reason not to merge.

## `gh --json comments` cannot name an App

`gh issue view --json comments` (and `gh pr view`) reports an App author as the
bare slug, `{"login":"loom-fleet-dispatch"}`, indistinguishable from a user of
that name. A bare login is therefore treated as a user, and in that shape the
fleet's own comments pass only by association or allowlist. A reader that must
believe fleet-authored markers fetches the REST listing instead:

```bash
gh api "repos/{owner}/{repo}/issues/$N/comments" --paginate \
  | loom-daemon forge trusted-comments
```

## Configuration

```json
{ "forge": { "trustedCommenters": ["release-bot", "helper-app[bot]"] } }
```

Anything other than an array of logins is ignored with a warning: a malformed
value never widens trust.

## Shell and role-prompt readers

`loom-daemon forge trusted-comments --fetch N [--repo owner/name]` fetches
issue/PR N's REST listing itself and prints the trusted subset (exit 1 when it
cannot). `--with-body` puts the issue/PR first, so its body survives only when
its author is trusted; `--gh-shape` prints `gh --json comments` field names
(`author.login`, `authorAssociation`, `body`, `createdAt`) for splicing into a
`gh … --json` document. Readers and their direction when the filter is
unavailable:

| Reader | Markers / phrases | Filter unavailable |
|---|---|---|
| `claim-staleness.sh` | `loom:claim-activity`, `loom:standdown` | `unknown` (never stomp) |
| `sweep-lease-fence.sh` | `loom:lease`, `loom:lease-yield` | fails open, as on an unreadable listing |
| `sweep-lease-publish.sh` | same | publishes anyway, as on an unreadable listing |
| `sweep-lease-renew.sh` | same | exit 1, nothing patched |
| `classify-ac-verification.sh` | `loom:ac-verified` (comments; the PR body only when its author is trusted) | no evidence: the issue stays held |
| Champion merge precheck | hold markers, release phrases, new Judge reviews | skip the PR this pass |
| Champion criterion #5 | "real activity" comments | the raw read (it can only read as more active) |
| Critical-file hold | `champion:critical-file-*`, `hold-state` | the raw read (bookkeeping only; a FAIL never merges) |
| Champion epic | epic verdict / escalation markers | skip the epic this pass |
| Judge fast-track | `loom:conflict-only` | full evaluation |
| Curator AC-hold check | `champion:ac-hold` | treated as no hold |

Role prompts that read forge text carry either the full untrusted-content
block or a one-line pointer to this page; see
[`untrusted-external-content.md`](untrusted-external-content.md).

## Loom writes only to repos it manages

The same principle, in the other direction: an installation that can work on
any public repository must never act as a control plane on one it does not
manage. GitHub lets any account comment on any public issue, so a write aimed
at the wrong repository does not fail. Its label edits are refused, but its
comments, markers included, land.

The wrong repository comes from `gh`, not from any explicit choice. Without
`--repo`, and for `{owner}/{repo}` and `gh repo view`, `gh` resolves the base
repository from `GH_REPO`, then a `gh repo set-default` pin, then the remotes
ranked **`upstream` > `github` > `origin`**. A fork checkout therefore reads
and writes the upstream project.

A comment, label edit, merge or lease write to `OWNER/REPO` is allowed only
when all three hold:

1. **Resolved from the checkout, the target is its `origin`.** A checkout
   that `gh` resolves elsewhere is refused, not redirected, because its reads
   go there too. If `origin` is the repository Loom manages, pin it:
   `gh repo set-default OWNER/REPO`.
2. **The repository is managed:** the `origin` of a workspace in this
   daemon's registry, or of the Loom-installed checkout the call runs in.
3. **The credential has WRITE.** A user token needs `push`, `maintain` or
   `admin`; an App installation token needs the repository in its
   installation. Probed once per repository per hour
   (`LOOM_WRITE_SCOPE_TTL_SECS`); when a re-probe cannot answer, a WRITE
   verified in the last 24 hours still counts, and a definitive "no" never
   does. On Gitea the same rule probes `GET /api/v1/repos/{owner}/{repo}`'s
   `permissions` object for the authenticated user: `push` or `admin` is
   WRITE. The credential is the one the writes carry — `GITEA_TOKEN`, then
   `forge.gitea.token` in `.loom/config.json`, then `FORGE_TOKEN` (the
   documented generic fallback); `GITEA_URL` and `GITEA_USERNAME` beat their
   config keys the same way — and an instance that cannot be reached or
   authenticated is an unverifiable probe: refused, never guessed.

Anything unverifiable is a refusal. Reads are never gated.

- **Daemon:** `loom-daemon/src/write_scope.rs`. Claim reconciliation,
  quarantine reconciliation, star liveness, sweep dispatch and every
  scheduled role tick skip a refused workspace, logging the reason once. The
  `forge issue|pr` write passthroughs and `forge auto-merge` /
  `disable-auto-merge` vet their target first. The roster heartbeat checks its
  configured repository; `notify-cleared-blockers` and the stale-check redate
  take the repository `merge-pr.sh` already vetted; dependency classification
  resolves `origin` (never gh's preference) or takes its caller's explicit
  `--repo`. The structural test
  `write_scope::tests::daemon_write_paths_are_scoped` fails when a new daemon
  file writes to the forge without being reviewed into its list.
- **Shell:** `loom-daemon forge may-write [--repo OWNER/REPO]` prints the
  repository to name on the write (exit 0) or the reason (exit 1).
  `loom_write_repo` in `lib/forge-helpers.sh` wraps it. Every script that
  writes uses it and then passes `--repo` or `repos/OWNER/REPO` explicitly:
  `post-verdict.sh`, `verdict-staleness-guard.sh --clear/--anchor`,
  `merge-pr.sh`, `create-pr.sh`, `check-promotion-landed.sh --apply`,
  `check-main-clean.sh`, `claim-staleness.sh`, `classify-capacity-defer.sh`,
  `clean-stale-building-labels.sh`, `rebase-stacked-children.sh`,
  `reconcile-stack.sh`, `sync-labels.sh`, the lease publish/renew scripts,
  and the `forge-helpers.sh` comment, label, reopen, create and merge
  wrappers. `write_scope::tests::shell_write_paths_are_vetted` fails when a
  script under `defaults/scripts` writes without it.
- **Cross-repo writes** (`create-issue.sh --repo X`, `sync-labels.sh --repo
  X`) now need X to be the origin of a registered workspace. To manage X
  from here, register its checkout: `loom-daemon workspace add <path>`.
- **Without the verb** (no `loom-daemon`, or an older one), the permission
  check cannot run. The fallback allows a write only from a checkout whose
  one remote is `origin`, only to `origin`, and only with `GH_REPO` unset or
  equal, so it can never reach another project. A fork checkout therefore
  cannot write at all until the daemon is rolled.
