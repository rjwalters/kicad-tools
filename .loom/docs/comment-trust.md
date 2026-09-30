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

## Where it is enforced

The predicate lives once, in `loom-daemon/src/comment_trust.rs`:

- **Rust readers** filter at fetch. `claim_reconciliation`'s comment fetch
  returns trusted bodies only, so the verdict backstop, the stale-verdict
  dedup, and the base-conflict "is this flag ours?" check never see an
  untrusted marker. `star_liveness::trust` delegates to the same predicate.
- **Shell readers** call `loom-daemon forge trusted-comments`, which reads a
  comment listing on stdin and prints the trusted subset in the same shape.
  `verdict-staleness-guard.sh` and `check-promotion-landed.sh` use it.
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
