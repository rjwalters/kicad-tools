# Sweep

Process an explicit list of issues — **or an explicit/NL-described set of open PRs** — through the appropriate lifecycle from the current Claude session, no external daemon required. Runs sequentially by default, or in **parallel waves** of up to `N` builders when `--builders-per-wave N` is supplied (issue-set modes only). Supports `--dry-run` to preview the candidate plan without mutating anything.

> **Scope.** This skill accepts either an explicit list of issue numbers, a natural-language description of which issues to process, **or an explicit/NL-described list of open PRs** (Mode C, the "back half" of the lifecycle: Judge → Doctor → Merge per PR's current label). Runs the appropriate lifecycle in waves. Supports `--dry-run` to preview the plan without mutations. Other knobs sketched in #3298 are **deliberately deferred** — see "Limitations" below.
>
> If you need multi-account autonomous dispatch across many issues, use `/loom:loom` (it drives the `loom-daemon`). `/loom:sweep` is itself the single-issue lifecycle, and also covers the in-between case: "I have these N issues (or PRs), run them in this session, without spinning up a daemon."

## ⚠️ `--body @path` Does NOT Expand — It Posts the Literal String

If you (or a role you dispatch) post a comment via `gh issue comment` / `gh pr
comment` / `gh api ... comments` from a scratch file, `--body @path` (and `gh
api -f body=@path`) posts the literal string `@path`, not the file's contents.
**Full pitfall, incident citation, and fixes**:
[`comment-body-literal-path.md`](comment-body-literal-path.md).

## Arguments

**Arguments**: $ARGUMENTS

`$ARGUMENTS` is interpreted in one of **three modes** (A/B/C), chosen by inspection of the non-flag tokens and the presence of a `--prs` flag — plus a dedicated **build-everything sentinel** for the bare, sole token `all`. Before classifying, **strip all recognized flag tokens** (`--builders-per-wave N`, `--dry-run`, `--prs`, `--no-daemon`) from the token list — flags are honoured in their respective modes.

**`/loom:sweep all` (the build-everything sentinel).** When the non-flag token list is exactly `["all"]` (case-insensitive), `/loom:sweep` takes a dedicated, deterministic path that resolves the **entire open backlog** — every open issue, regardless of its current label — via a single fixed `gh issue list` query (no Mode B NL translation), then aggressively promotes and drives each toward a merged PR. This is the **fast/sloppy "just build everything" command**: uncurated issues get curated and promoted, stale `loom:building` claims are reclaimed, `loom:blocked` issues are probed for whether their blocker has cleared, `loom:epic` containers fan out to their `loom:epic-phase` children, and issues that already have an open PR are driven through Judge / Doctor → Merge. The only issues it skips outright are `loom:operator-only` (genuinely need a human — credentials, hardware, infra) and `loom:needs-capability` (blocked on a missing tool/agent capability, not an operator-by-right decision — #5817) — with one narrow, fail-closed, propose-only exception for a capability-matched `loom:operator-mechanical` item (#6893, see "Capability-aware `loom:operator-mechanical` lane"). A separate, **advisory-only** scan (`warn-operator-gated.sh`, #5137) additionally flags — but never skips — candidates whose **body text** declares operator-gating without carrying that label (e.g. "operator-gated", "operator authorization required", "paid GPU") or whose declared `Depends on #A`/`Requires #A` dependency is itself `loom:operator-only` or `loom:needs-capability`; the annotation is surfaced at the mandatory confirmation gate below for the operator to act on. See "Operator-gate advisory scan". The resolved set is handed to the same confirmation gate and wave machinery every other mode uses. `/loom:sweep all --prs` resolves the open **PR** set and drives Mode C. Only the bare, sole `all` token triggers this; `all open loom:issue items` and every other multi-token `all …` phrase still route to Mode B (or Mode C for PR phrases) exactly as before. See "Build-everything sentinel (`all`)", "Aggressive candidate taxonomy", and "Operator-gate advisory scan" under Validation rules.

**Mode selection summary** (full rules below):

| Trigger | Mode | Subject |
|---------|------|---------|
| Non-flag tokens == `["all"]` (case-insensitive, single token) | **Build-everything** (evaluated first — step 0) | Every open issue, aggressively promoted (or every open PR with `--prs`, via Mode C) |
| `--prs` flag present | **Mode C** (PR-set) | Open PRs, routed per their current label |
| No `--prs`, all non-flag tokens match `^#?\d+$` | **Mode A** (numeric issue list) | Issues, full lifecycle |
| No `--prs`, any non-flag token does not match `^#?\d+$` | **Mode B** (NL) | Issues (default) **or** PRs (if NL clearly indicates PRs — see Mode C NL triggers below) |

> **Build-everything sentinel (bare `all`).** The row above fires **only** when the non-flag token list is exactly `["all"]` (case-insensitive — `all`, `ALL`, `All`). Any multi-token phrase that merely begins with `all` (`all open loom:issue items`, `all merge-ready PRs`) has length > 1 and falls through to Mode B / Mode C **unchanged**. See "Build-everything sentinel" under Validation rules for the deterministic query and taxonomy.

### Mode A — Explicit numeric list (fast path, regression guard)

If **every** whitespace-separated non-flag token matches the regex `^#?\d+$` (a positive integer with an optional leading `#`), treat the arguments as today's explicit issue list. **No LLM interpretation, no extra `gh` calls.** This is the MVP behaviour and must remain bit-for-bit compatible — `/loom:sweep 123 456` and `/loom:sweep #123 #456` continue to work exactly as before.

### Mode B — Natural-language interpretation

Otherwise, treat `$ARGUMENTS` as an English description of which open issues to process. The orchestrator (Claude, this session) translates the description into one or more `gh issue list` invocations using the appropriate flags, surfaces the derived candidate set, awaits user confirmation, then proceeds with the rest of the lifecycle exactly as in Mode A.

**Issue candidate-resolution and PR candidate-resolution queries below run as `"$GH_READ" issue list …` / `"$GH_READ" pr list …`** — the short-TTL cached-read wrapper resolved once at sweep start (see "Cached forge reads (`gh-cached`)" under the Execution Model; it degrades to plain `gh` when absent). The translation guides are written with the bare `gh issue list` / `gh pr list` flag names because the flags are identical either way; only the leading binary changes.

**This is deliberately not a formal grammar.** There is no parser, no operator precedence, no fixed vocabulary. The orchestrator reads the description and picks reasonable `gh issue list` flags. The interpretation rules below are prose, not a spec.

**Translation guide — common NL fragments to `gh issue list` flags** (verified against `gh` v2):

| NL fragment | `gh issue list` flag(s) |
|-------------|------------------------|
| "labeled `loom:curated`" / "all `loom:curated` issues" | `--label loom:curated` |
| "filed by rjwalters" | `--author rjwalters` |
| "all my ..." / "my agent-filed ..." | `--author @me` (NOT `--assignee` — Loom files but does not self-assign) |
| "in the last week" / "from the last N days" | `--search "created:>=YYYY-MM-DD"` (compute the date) |
| "with 'docs' in the title" | `--search "docs in:title"` |
| "open" (always assumed) | `--state open` (the default) |
| "closed too" | `--state all` |

Combine flags as needed. Always pass `--state open` explicitly (default) unless the user asks for closed issues. Default to `--limit 100` rather than the `gh` default of `30` to avoid silent truncation (see edge case below).

**Mixed mode is supported.** `/loom:sweep #3310 #3312 and any other loom:issue with 'docs' in the title` should be interpreted as the union of `{3310, 3312}` and the `gh issue list --label loom:issue --search "docs in:title"` result. Because the tokens contain non-numeric words, this falls into Mode B and the orchestrator handles the union.

**Unknown-label guard.** Loom never invents labels (CLAUDE.md "Never create new GitHub labels" — that rule is about label *creation* via `gh label create`, which is separate from validating that a label the user already named actually exists on the repo). To validate label tokens in the user's description, query the **live repo label set** as the source of truth:

```bash
"$GH_READ" label list -R <repo> --limit 200 --json name --jq '.[].name'
```

Run this query **once at the start of Mode B label-token validation** and reuse the result for every subsequent token check within the same `/loom:sweep` invocation (at most one `gh label list` call per invocation, regardless of how many label tokens appear in the description). `$GH_READ` is the cached-read wrapper resolved in "Cached forge reads (`gh-cached`)" under the Execution Model — the repo's label set is near-static, so a cross-session cache hit here is free; the wrapper degrades to plain `gh` when absent (the offline fallback below is unchanged either way). Pass `--limit 200` explicitly (do not rely on `gh`'s default of 30, matching the explicit-limit convention used elsewhere in this skill for `gh issue list`). Scope the query to the repo currently being swept.

If a label token in the description is not in the repo's actual label set, **do not** silently fabricate a `--label <name>` filter — ask the user to clarify which existing label they meant, or supply explicit issue numbers.

**Offline fallback.** If `gh label list` fails (non-zero exit — network outage, auth failure, rate limit), fall back to consulting `.github/labels.yml` and log a warning to stderr (e.g., `warning: gh label list failed, falling back to .github/labels.yml (Loom-managed subset only)`). This keeps the skill functional in offline or restricted environments. Note that `.github/labels.yml` is only the Loom-managed subset, so the fallback may produce false "unknown-label" rejections for labels added via the GitHub UI, Dependabot, or other project conventions; this is the trade-off for offline operation. **When the failure is specifically a rate-limit rejection, this is the *last* rung, not the first** — try the REST label read described under "GraphQL-exhaustion fallback" below and drop to `.github/labels.yml` only if REST fails too, so a healthy REST budget still yields the complete live label set.

**GraphQL-exhaustion fallback (REST issue discovery, #4670).** `gh issue list`, `gh pr list`, and `gh label list` are **GraphQL**-backed; `gh api repos/{owner}/{repo}/…` is **REST**. The two draw on **independent quotas** — confirmed live during the #4526 incident and again in the `/loom:sweep` run that filed #4670, where GraphQL was exhausted while >4,500 REST core requests were still available. So a candidate-resolution failure that is *specifically* a rate-limit rejection must be re-issued over REST rather than aborting the sweep. The full degradation ladder for Mode B is **GraphQL → REST → (labels only) `.github/labels.yml`**.

1. **Detect exhaustion — reuse the existing signature table, do not derive a new one.** Capture the failing call's output with stderr merged (`2>&1`), lowercase it, and treat the failure as quota exhaustion **only** when it contains one of these five signatures. This is exactly the table `defaults/scripts/check-duplicate.sh`'s `is_rate_limit_error()` implements, itself mirrored from `loom-daemon/src/rate_limit_breaker.rs`'s `RATE_LIMIT_SIGNATURES` — the repo's tested ground truth for what `gh` actually prints:

   | Signature (match case-insensitively) | Seen as |
   |---|---|
   | `api rate limit exceeded` | REST: `HTTP 403: API rate limit exceeded for …` |
   | `api rate limit already exceeded` | GraphQL: `GraphQL: API rate limit already exceeded for user ID …` |
   | `secondary rate limit` | either transport, burst throttling |
   | `abuse detection mechanism` | either transport, burst throttling |
   | `was submitted too quickly` | either transport, burst throttling |

   The GraphQL and REST phrasings are **not** substrings of each other — the word `already` breaks the contiguous `api rate limit exceeded` match — which is why both are listed. Matching one substring is not enough.

   **Anything else is NOT exhaustion — keep today's fail-safe behavior.** An auth failure (`gh auth status` expired, missing `GH_TOKEN` scope), a DNS/network error, an HTTP 404 on a mistyped repo, a rejected flag: report the error verbatim, do **not** retry over REST, and EXIT without spawning any agent. A blind REST retry on an auth failure just fails again with a more confusing message and buries the real cause. Never infer exhaustion from a bare non-zero exit code.

   **The one carved-out exception: a GitHub App permission-scope 403 (#6074).** `403 … not accessible by integration` is a *distinct* signature from every rate-limit phrasing above and from a generic auth failure — it means the credential is a valid App installation token whose **cached permission set** is stale (minted before a grant propagated). Retrying it over REST is still pointless (same token, same 403), but retrying it with a **different credential** is not: `forge_gh_perm_safe` in `lib/forge-helpers.sh` force-mints a fresh installation token (bypassing the ~1h cache) and then falls back to a personal token. Every Loom write call site already routes through that ladder — `create-pr.sh`, `create-issue.sh`, and the `forge_gh_*_rl_safe` comment/label helpers — so this exception is **already handled below you**; do not hand-roll a second retry for it here, and do not widen `is_rate_limit_error`'s table to cover it. **Do not conflate an exhausted permission ladder with a confirmed credential fault** — see "Forge write failure diagnosis (#6425)" below for what you may and may not say about it in the operator-facing summary.

2. **Resolve the repository locally — never with another GraphQL call.** Do **not** call `gh repo view --json nameWithOwner` to learn owner/repo: that call is itself GraphQL-backed, so under GraphQL exhaustion it fails *before* any REST fallback is attempted (#4659 fixed this exact bug in `check-duplicate.sh`). Two supported forms, both free of API calls:
   - Preferred: write the endpoint with the literal `{owner}/{repo}` placeholder — `gh api "repos/{owner}/{repo}/issues?…"` — and let `gh` expand it locally from the git remote.
   - When the literal `owner/repo` string is needed (log lines, `-R` flags): parse `git remote get-url origin`, strip a trailing `.git` and/or `/`, and take the final two `/`- or `:`-delimited segments (works for both the SSH `git@host:owner/repo.git` and HTTPS `https://host/owner/repo` forms) — the same parse `check-duplicate.sh`'s `get_repo_nwo()` performs.

3. **Re-issue the query as a paginated REST listing.** Translate the Mode B flags you had derived:

   | `gh issue list` flag | REST equivalent on `repos/{owner}/{repo}/issues` |
   |---|---|
   | `--state open` (default) | `state=open` (`--state all` → `state=all`; `--state closed` → `state=closed`) |
   | `--label loom:issue` (repeatable) | `labels=loom:issue,loom:curated` — comma-separated is AND, matching repeated `--label` |
   | `--author rjwalters` | `creator=rjwalters` (`@me` → resolve the login first with `gh api user --jq .login`, itself REST) |
   | `--assignee X` | `assignee=X` |
   | `--limit 100` | `per_page=100` plus `--paginate`, then truncate client-side to the requested limit |
   | `--json number,title,labels,updatedAt` | already present in the REST payload — project with `--jq` (`updatedAt` is `updated_at`) |
   | `--search "…"` | **not expressible** on `/issues`; use `gh api "search/issues?q=repo:{owner}/{repo}+…"` (REST search, its own 30/min quota) or ask the operator to narrow to flags / explicit issue numbers |

   ```bash
   "$GH_READ" api --paginate "repos/{owner}/{repo}/issues?state=open&labels=loom:issue&per_page=100" \
     --jq '[.[] | select(.pull_request == null) | {number, title, labels: [.labels[].name], updatedAt: .updated_at}]'
   ```

   Candidate resolution is an observation read, so the REST retry routes through `$GH_READ` exactly like the GraphQL call it replaces (the wrapper caches `gh api` GETs too and degrades to plain `gh api` when absent). The **uncached carve-outs** listed under "Cached forge reads (`gh-cached`)" — claim arbitration, Mode C's C0 pre-flight, merge gating — stay on plain `gh api` if they need this fallback.

   **`/issues` returns pull requests too.** GitHub's REST issue endpoint includes PRs; drop them with `select(.pull_request == null)` (what `check-duplicate.sh` does) or Mode B will resolve PR numbers as issue candidates.

4. **Preserve every Mode B safeguard — this is a transport substitution, not a policy change.** Deduplicate the resulting numbers (preserve first-seen order) and union them with any explicit numeric tokens exactly as on the GraphQL path; keep the **explicit limit** (never rely on REST's default `per_page=30`, mirroring this skill's explicit-`--limit` convention); and apply the same edge-case rules — zero matches → print the resolved REST query and its empty result, then EXIT cleanly (edge case #1); results at the cap → warn that the set was truncated and ask the operator to narrow before proceeding (edge case #2). Because `--paginate` walks past the cap, the truncation point is now a client-side decision: state the cap you applied in that warning.

5. **Announce the degradation.** Log one warning to stderr (e.g. `warning: gh issue list rate-limited (GraphQL quota exhausted), falling back to REST via gh api`) and repeat it above the candidate set shown at the confirmation gate, so the operator knows the plan was resolved over the fallback path.

6. **Label validation degrades on the same ladder.** `gh label list` is GraphQL-backed, so under exhaustion the unknown-label guard goes `gh label list` → `"$GH_READ" api --paginate "repos/{owner}/{repo}/labels?per_page=100" --jq '.[].name'` (the live, complete label set — preferred) → `.github/labels.yml` (the "Offline fallback" above; Loom-managed subset only, so false "unknown-label" rejections are possible). Drop to the YAML only when the REST read also fails. **The unknown-label safety rule is unchanged at every rung**: never fabricate a `--label <name>` filter for a label you could not verify — ask the operator to clarify or supply explicit issue numbers.

**`GraphQL: Body is too long` is NOT part of this ladder — do not REST-fallback it (#6930).** This section's five-signature table is exclusively about **quota exhaustion**, where GraphQL and REST draw on independent budgets and re-issuing the *same* write over REST succeeds because the quota problem doesn't exist on that transport. A `Body is too long` rejection on `gh issue edit`/`gh pr edit --body`/`--body-file` is a **different, hard, permanent, size-based** guard (~262144 bytes / 256 KiB) that has nothing to do with rate limits — it fires the same way whether the GraphQL quota is full or empty, and it is not one of the five signatures above. Falling back to a REST `gh api -X PATCH .../issues/{n}` (or `/pulls/{n}`) on `body` "succeeds" only because REST doesn't enforce the same cap — but that PATCH is a blind read-modify-write that can silently lose a concurrent writer's edit with no error raised anywhere (observed in production, see full incident and remedy in `.loom/docs/graphql-body-size-cap.md`). The fix is to switch to posting the update as a **comment** instead of a body edit, not to retry the body edit over REST.

### Forge write failure diagnosis (#6425)

**Incident.** During a confirmed GitHub partial outage (2026-08-17: Issues API and Git ops degraded per githubstatus, the fleet's own `claim_reconciliation` logging `HTTP 503: No server is currently available`), two sweeps hit forge write failures and wrote a confident **credential** diagnosis into their operator-facing summaries — "this needs operator attention, not a retry … the GitHub App installation token lacking write permission," with an explicit "Action needed from you" line. Both were wrong: the first PR merged normally 17 minutes later with no permission change; the second repo's writes resumed once GitHub recovered. Nothing about the App installation had changed in either case — the same installation was merging PRs elsewhere the same day. A misattributed root cause with an "action needed from you" line is worse than a plain failure report: it converts a self-healing transient into a human task, and it erodes trust in the summaries that *are* right.

**Rule: never assert a permission/credential diagnosis without positive evidence.** Any time a `gh`/forge write fails (a merge via `merge-pr.sh`, a label flip, a comment, a PR/issue create) and you are about to write operator-facing prose explaining *why*, classify the failure text through `lib/forge-helpers.sh` before writing a word of diagnosis:

1. **`is_forge_transient_error <error text>`** — fires on an HTTP 5xx status, GitHub's own "No server is currently available" 503 wording, Bad Gateway / Service Unavailable / Gateway Timeout, or a connection reset/refusal. This is **always** forge-transient: log it as such and move on to the normal retry path (the next sweep pass, or `merge-pr.sh`'s own checkpoint-preserving retry — see "On merge failure" below). **Never** say "not a retry" or emit an operator action item for this signature — a forge 5xx burst during an incident is exactly the case to retry later.
2. **A `403 … not accessible by integration`** already routes through `forge_gh_perm_safe`'s three-rung credential-escalation ladder (#6074, above) at every Loom write call site. If that ladder is exhausted (all rungs still 403 or unavailable), the 403 is **still ambiguous** — GitHub can return an inconsistent 403 for a write during a backend incident even though the credential is fine. Do **not** default to a credential diagnosis. Instead, run the positive-evidence check:

   ```bash
   source ./.loom/scripts/lib/forge-helpers.sh
   forge_write_permission_confirmed "$WRITE_ERROR_TEXT"
   ```

   This is a cheap, side-effect-free probe (`gh api /rate_limit` on the same credential context — `gh api /user` is an equally valid sibling probe) run alongside the write failure: **read succeeds + write 403s (non-transient)** is positive evidence of a genuine scope gap — you may report a permission diagnosis, but you **must cite the check** ("confirmed: `gh api /rate_limit` succeeded on the same token while the write 403'd, persisting across a retry"). **Read also fails** is evidence of a broader outage or dead token, not a narrow scope gap — this is exactly the mistake the second incident summary made (it recorded `gh api /user` also 403'ing on the same token and still concluded "permissions"). `forge_write_permission_confirmed` returns non-zero for every case that is not confirmed, including "the check was never run."
3. **When uncertain** (the failure is forge-transient, or the permission ladder is exhausted and `forge_write_permission_confirmed` did not return 0) — the summary line is: `forge writes failing (possible GitHub incident) — will retry`. Do **not** emit a "needs operator attention" line, an "action needed from you" line, or any sentence asserting the fault is a credential/permission problem.

**Summary-outcome vocabulary.** Reuse the `rate-limited (...)` shape from "Summary Output" below, with a new `forge-transient:` reason prefix (sibling to `resumed:` / `downgraded:` / `unresumable:`, but for the forge-write axis rather than the Claude-account axis), for both the "will retry, no operator action" cases:
- Case 1 (an `is_forge_transient_error` signature): `rate-limited (forge-transient: <signature> — will retry)`.
- Case 2, unconfirmed (permission ladder exhausted, `forge_write_permission_confirmed` did not return 0): `rate-limited (forge-transient: permission fault not confirmed — will retry)`.

Reserve `blocked (…)` — and the "needs operator attention" / permission diagnosis it implies — for case 2, **confirmed** only, and always name the check that confirmed it in the log line, e.g. `PR #P merge failed: GitHub App permission fault confirmed (gh api /rate_limit succeeded, write 403'd, retried once, same result) — operator must check App installation scopes`. This is deliberately the mirror image of the `rate-limited` vs `blocked` split already documented below (`blocked` = the work/credential itself needs a human; `rate-limited` = an external system's fault, no operator action, retried automatically) — a forge outage is squarely in the `rate-limited` bucket. See "On merge failure" (both `merge-pr.sh` call sites below) for where this applies during the Merge phase specifically.

### Mode C — PR-set mode (back half of the lifecycle: Judge → Doctor → Merge)

When the user wants to drive a known set of open PRs through Judge / Doctor / Merge **without** spawning Curator or Builder, use Mode C. This is the symmetric counterpart to Mode A/B: same wave/dry-run/checkpoint machinery, different unit-of-work (PR instead of issue) and a different per-unit routing table.

**Mode C entry triggers** (any of these select Mode C):

1. **Explicit flag with explicit list**: the user passes `--prs` **and** every non-flag token matches `^#?\d+$`. Tokens are interpreted as **PR numbers** (not issue numbers). Example: `/loom:sweep --prs 100 101 102`.
2. **Explicit flag with NL description**: the user passes `--prs` **and** at least one non-flag token is non-numeric. The orchestrator translates the description into one or more `gh pr list` invocations (NOT `gh issue list`) — see the PR-side translation guide below. Example: `/loom:sweep --prs all open loom:pr`.
3. **NL trigger without `--prs`**: the user's description **clearly** indicates PRs ("PRs", "pull requests", "review-requested PRs", "all open `loom:pr`", "merge-ready PRs", etc.) — see the NL trigger list below. The orchestrator infers Mode C and proceeds as if `--prs` had been passed. If the description is ambiguous between issues and PRs, ask for clarification rather than guess.

**PR-side NL trigger phrases** (any of these in the description selects Mode C, even without `--prs`):

- `PRs`, `pull requests`, `pull request`
- `review-requested PRs`, `loom:review-requested`
- `changes-requested PRs`, `loom:changes-requested`
- `merge-ready PRs`, `loom:pr` (in a PR context)
- `all open loom:pr`
- `judge-pending PRs`, `judge-ready PRs`
- `pending review`

When uncertain whether the description means issues or PRs (e.g., `/loom:sweep all loom:review-requested` — the label only applies to PRs but the user did not say "PRs"), ask for clarification rather than infer.

**PR-side translation guide — common NL fragments to `gh pr list` flags** (verified against `gh` v2):

| NL fragment | `gh pr list` flag(s) |
|-------------|----------------------|
| "all `loom:review-requested` PRs" / "PRs awaiting Judge" | `--label loom:review-requested` |
| "all `loom:changes-requested` PRs" / "PRs needing Doctor" | `--label loom:changes-requested` |
| "all `loom:pr` PRs" / "merge-ready PRs" / "PRs approved for merge" | `--label loom:pr` |
| "filed by rjwalters" | `--author rjwalters` |
| "all my agent-filed PRs" | `--author @me` |
| "open" (always assumed) | `--state open` (the default) |
| "in the last week" / "from the last N days" | `--search "created:>=YYYY-MM-DD"` (compute the date) |

Combine flags as needed. Always pass `--state open` explicitly (Mode C operates exclusively on open PRs — closed/merged PRs are skipped). Default to `--limit 100` rather than the `gh` default of `30` to avoid silent truncation. The same **unknown-label guard** (one `gh label list` call per invocation, with `.github/labels.yml` offline fallback) applies to PR labels too — PR and issue labels are in the same repo-wide label set.

**GraphQL-exhaustion fallback (REST PR discovery, #4670).** `gh pr list` is GraphQL-backed and fails under the same quota exhaustion as `gh issue list`. Mode C uses the **identical ladder** documented in Mode B's "GraphQL-exhaustion fallback" — same five-signature detection table (`api rate limit exceeded` / `api rate limit already exceeded` / `secondary rate limit` / `abuse detection mechanism` / `was submitted too quickly`, matched case-insensitively against `2>&1` output), same rule that **any other failure — auth, network, 404 — is not exhaustion** and must fail safe with the error reported and no agents spawned, and the same local repo resolution (`gh api "repos/{owner}/{repo}/…"` placeholder expansion, or parsing `git remote get-url origin`; **never** `gh repo view --json nameWithOwner`, which is itself GraphQL-backed — #4659). Only the endpoint and the flag mapping differ:

| `gh pr list` flag | REST equivalent on `repos/{owner}/{repo}/pulls` |
|---|---|
| `--state open` (mandatory in Mode C) | `state=open` |
| `--limit 100` | `per_page=100` plus `--paginate`, truncated client-side to the requested limit |
| `--json number,title,labels` | `number`, `title`, and `labels[].name` are all in the list payload — project with `--jq` |
| `--label loom:review-requested` | **no `labels=` parameter on `/pulls`** — filter client-side on the payload's `labels[].name` (below), or list via `repos/{owner}/{repo}/issues?labels=…&state=open` and keep only entries where `.pull_request != null` |
| `--author rjwalters` / `@me` | no `creator=` parameter on `/pulls` either — filter client-side on `.user.login` (resolve `@me` with `gh api user --jq .login`) |
| `--search "…"` | not expressible; use `gh api "search/issues?q=repo:{owner}/{repo}+is:pr+…"` or ask the operator for explicit PR numbers |

```bash
"$GH_READ" api --paginate "repos/{owner}/{repo}/pulls?state=open&per_page=100" \
  --jq '[.[] | {number, title, labels: [.labels[].name]} | select(.labels | index("loom:review-requested"))]'
```

All Mode C safeguards carry over unchanged: deduplicate the resulting PR numbers (preserve first-seen order), keep the explicit limit (never REST's default `per_page=30`), apply the zero-match (print query + empty result, EXIT cleanly) and truncation-warning edge cases, **display the candidate set and await confirmation before spawning any agents**, and log the degradation to stderr (e.g. `warning: gh pr list rate-limited (GraphQL quota exhausted), falling back to REST via gh api`) plus above the confirmation-gate listing. The unknown-label guard degrades on the same GraphQL → REST (`repos/{owner}/{repo}/labels`) → `.github/labels.yml` ladder as Mode B. Note that only **candidate discovery** may be served from `$GH_READ`; Mode C's C0 per-PR pre-flight is a deliberately uncached live routing read (see "Cached forge reads (`gh-cached`)") — if it too hits GraphQL exhaustion, its REST equivalent is plain `gh api "repos/{owner}/{repo}/pulls/<N>"` (plus `repos/{owner}/{repo}/issues/<N>` for the label set), never a cached read.

**Mode C validation rules:**

- `--prs` strips from the token list before classification, exactly like `--builders-per-wave N`, `--dry-run`, and `--no-daemon`.
- Numeric tokens (after stripping `--prs`): same `^#?\d+$` regex as Mode A. Strip leading `#`, parse as positive integers, deduplicate (preserve first-seen order). Reject any token that fails to parse, with a clear error citing the offending token, and EXIT.
- NL tokens (after stripping `--prs`): translate to one or more `gh pr list` invocations per the guide above. Run the command, deduplicate the resulting PR list, and **display the candidate set to the user before spawning any agents**. Await confirmation. If the user declines, EXIT cleanly.
- **`--builders-per-wave N` is silently ignored in Mode C**. The Builder phase is skipped wholesale for PR-set mode; per-PR Judge is sequential within a wave (matching the existing issue-side wave policy). If the user passes both `--prs` and `--builders-per-wave N`, print a one-line note that the flag has no effect in Mode C and proceed without it. Mode C waves are size-1 by default — one PR settles fully (Judge → optional Doctor → optional Merge) before the next PR is touched. This may relax in a future issue; today it is size-1 because parallel Judge/Doctor across PRs is unbenchmarked and every wave member's Task result is read back into this same orchestrator session (context-window pressure). This is a **width** choice — it is **not** the #3289 rule, which is about nested (grandchild) dispatch depth, not wave width.
- Mixed Mode C and Mode A/B is **not** supported in this skill — if the user wants to sweep some issues and some PRs in one invocation, ask them to run two `/loom:sweep` calls (one for each mode). Implementing PR/issue mixing would require routing logic for the cross product of (issue-state × PR-state); cleanly out of scope.

### Edge cases (prose rules, applied in either mode but mostly relevant to Mode B)

1. **Zero matches.** Print the derived `gh issue list` command and its empty result, then EXIT cleanly. Do not spawn any agents and do not fall through to Mode A.
2. **More than the result cap.** `gh issue list` defaults to `--limit 30`; this skill should pass `--limit 100` explicitly. If results still hit the cap (100 candidates), print a warning that the result set was truncated and ask the user to narrow the description before proceeding. Do not silently process only the first 100.
3. **Out-of-band queries** (anything `gh issue list` cannot express by itself — body-content searches, file-touch queries like "issues touching `loom-daemon`", "issues without tests", repository-diff inspection). These require per-issue body or diff inspection, which is **out of scope for this skill**. Ask the user to clarify or supply explicit issue numbers. Do **not** attempt heuristic per-issue inspection here.
4. **Ambiguous time windows** ("recent", "lately", "this sprint"). Ask the user to specify a concrete date or duration rather than guessing. The translation table above only covers concrete forms ("last week", "last N days") which compute deterministically.

### Optional flags

- **`--builders-per-wave N`** — dispatch up to `N` builders in parallel per wave. When **omitted**, the wave size is `auto` — resolved at Stage -1 from the chosen backend and scratch-volume disk headroom (see "Resolve auto wave size"): the daemon detached-process path targets up to 10 concurrent sweeps, while the in-session subagent path core-scales within the #3289-safe `[3, 6]` band (`clamp(floor((cores-2)/4), 3, 6)`, #3693). When **present**, `N` must be an integer `>= 1` and the explicit value overrides auto entirely (operator wins). Honoured in Modes A and B (issue-side); **silently ignored in Mode C** (PR-set mode has no Builder phase — see Mode C validation rules above). Flag tokens are stripped before classification.
- **`--dry-run`** — print the planned candidate list (with wave grouping) and EXIT without performing any mutation. Recognized as a bare flag token (no value). May appear anywhere in `$ARGUMENTS`. Default is off. Honoured in **all three** modes — stripped before classification along with other flags. Mode C dry-run prints the PR-set plan (per-PR routing) instead of the issue-set plan.
- **`--prs`** — switch into Mode C (PR-set mode). Recognized as a bare flag token (no value). May appear anywhere in `$ARGUMENTS`. Default is off. When present, non-flag tokens are interpreted as **PR numbers** (numeric tokens) or as a **PR-list description** (NL tokens). When absent, an NL trigger phrase listed in the Mode C section can still select Mode C. See "Mode C" above for full semantics.
- **`--no-daemon`** — force in-process subagent dispatch even when the daemon is running with a multi-account token pool. Recognized as a bare flag token (no value). May appear anywhere in `$ARGUMENTS`. Default is off. When present, **Stage -1 (Backend detection) skips the `PROBE_DAEMON` step entirely** and the skill always falls through to the existing Mode A/B/C subagent dispatch path. Honoured in **all three** modes — stripped before classification along with other flags. Use this when you want the predictable single-process behaviour even though daemon dispatch is available (e.g., debugging, demoing the subagent path, or running under a token configuration that you don't want shared with daemon-spawned sweeps). See "Stage -1: Backend detection" below.
- **`--claim-owned <N>`** — daemon self-claim marker, embedded **inside the `-p` prompt string as part of this invocation's own `$ARGUMENTS`** (issue #3823, flag added #4111). It is NOT passed as a separate `claude` CLI argv token — `--claim-owned` is not a real `claude` flag, so a sibling arg would make `claude` exit 1 with `error: unknown option '--claim-owned'` before any session starts (#4120); `SweepRegistry::spawn_child` therefore bakes it into the prompt text (`-p "/loom:sweep N --claim-owned N"`), the only channel that reaches this skill's own argument parser. Emitted **only** by `SweepRegistry::dispatch` (`loom-daemon`) when it spawns a child for issue `N` — never by an operator, a GH Actions cron invocation, or `--auto-stack`/`--depends-on` chaining. Declares that the daemon flipped `loom:issue → loom:building` on `N` immediately before spawning THIS session, so the `loom:building` label this session is about to observe on `N` is its **own** claim, not a competing worker's. Consumed as the **mandatory, first-evaluated** pre-flight step in "1. Per-issue pre-flight" (see "Step 1a — daemon self-claim check" there) — a flag baked into the invocation's own text is in the model's context by construction, unlike an environment variable the model has no standing reason to introspect (the #4111 failure mode). Takes exactly one value, the issue number the daemon claimed on this child's behalf; recognized anywhere in `$ARGUMENTS` as `--claim-owned N` and stripped before mode classification like every other flag. The companion env var `LOOM_SWEEP_CLAIM_OWNED=<N>` (#3823) is still exported alongside this flag on every daemon dispatch — kept for backward compatibility (`spawn-claude.sh` logs it, and it is asserted by producer-side tests in `sweep_registry.rs` / `work_finder.rs` / `ipc.rs`) — but Step 1a treats either signal as sufficient on its own, so the flag does not depend on the env var reaching the session correctly (or vice versa). A hand-typed `/loom:sweep N --claim-owned N` is harmless (it just self-asserts ownership of a label this session itself is about to apply) but is not an intended usage — this flag is daemon-dispatch-only.
- **`--depends-on <parent>`** — stacked-PR mode (issue #3729, v1). Declares that this sweep's issue is stacked on the single parent issue `<parent>`: the Builder branches its worktree off `feature/issue-<parent>` (not the default branch) and opens its PR with `--base feature/issue-<parent>`, so the child's Curator→Builder→Judge can run **concurrently** with the parent's review. Takes **one value** (a positive integer parent issue number) — this is the sole, authoritative *operator-declared* dependency source (no `Depends on #A` body parsing unless `--auto-stack` is passed, see below). A single optional parent makes diamonds / multi-parent stacks unrepresentable. Recognized anywhere in `$ARGUMENTS` as `--depends-on N`; strip it (and its value) before classification and store `DEPENDS_ON=N`. Default **unset** — absent the flag, behavior is byte-for-byte unchanged (branches off the default branch as always). Intended for **daemon `dispatch_sweep`-only** use (`mcp__loom__dispatch_sweep` with `depends_on`); absent `--auto-stack`, the wave lifecycle does **not** auto-detect or auto-create stacks. See "Stacked dependency (auto-reconciliation on parent merge)" below. **Reconciliation after the parent squash-merges now fires automatically** from `merge-pr.sh` (#3747 v2 item 1) — a best-effort, live-forge-discovered pass that reconciles safe children and defers the ones whose issue is still `loom:building`; `./.loom/scripts/reconcile-stack.sh` remains available for manual/deferred runs.
- **`--auto-stack`** — opt-in auto-election of same-candidate-set stacking (issue #3759, v1). A bare flag (no value); default **off**. When present in Modes A/B (issue-set), the Stage 0 candidate survey additionally reads each candidate's issue `body` and detects **same-candidate-set** dependency edges declared in body text (`Depends on #A` / `Requires #A`) — see "Auto-stack detection and wave ordering (`--auto-stack`, #3759)". A detected edge is honored **only when `#A` is also a member of this sweep invocation's own deduplicated candidate list**; a `Depends on #A` naming an issue outside the set is left completely untouched (it flows through existing `loom:blocked` handling, unaffected). This generalizes the single-value `--depends-on` mechanics to a **per-issue** `DEPENDS_ON[N]` map: each child branches its worktree off `feature/issue-<parent>` and opens its PR with `--base feature/issue-<parent>`, exactly as a manually-dispatched `--depends-on` chain, and reconciliation on parent merge fires automatically (unchanged, #3747/#3752). **Absent the flag, behavior is byte-for-byte unchanged** (no body read, no edge detection, no wave reordering, no prompt). **No-op in Mode C** (PR-set mode has no Builder phase to stack — the flag is silently ignored, like `--builders-per-wave`). Scope is deliberately narrow: edges are **linear, single-parent** (no diamonds/multi-parent), **same-sweep only** (cross-`/loom:sweep` coordination is #3768's concern), and inferred from the **authoritative body-text signal only** — file-overlap-heuristic detection is explicitly out of scope **as a stacking-topology signal** (#3729 rejected file paths as a topology signal; the reactive #3647 in-wave overlap gate stays the backstop for accidental collisions). Note the distinction: file overlap *is* used elsewhere as a *scheduling* signal — "Overlap-aware wave partitioning" (#4161) reads the same `## Affected Files` data to decide **which wave** a candidate lands in (or to warn), which is a different use than creating a `--depends-on` edge. Overlap never produces a stacking edge here; the two uses must not be conflated. Recognized anywhere in `$ARGUMENTS`; strip it before classification and store `AUTO_STACK=true|false`.

### Validation rules

- Recognize `--dry-run`, `--prs`, `--no-daemon`, `--builders-per-wave N`, `--depends-on N`, `--auto-stack`, and `--claim-owned N` as flag tokens anywhere in `$ARGUMENTS`, strip them from the candidate list before validation, and store them as flags / parameters (`DRY_RUN=true|false`, `PRS_MODE=true|false`, `NO_DAEMON=true|false`, `BUILDERS_PER_WAVE=N`, `DEPENDS_ON=N|unset`, `AUTO_STACK=true|false`, `CLAIM_OWNED=N|unset`). When `--builders-per-wave` is **absent**, set the sentinel `BUILDERS_PER_WAVE=auto` (not `1`) — Stage -1 resolves the concrete wave size from the backend + disk headroom. An explicit integer is stored verbatim and overrides auto. `--depends-on N` consumes its following token as the parent issue number (a positive integer); reject a missing/non-numeric value with `Error: --depends-on requires a positive integer parent issue number` and EXIT. When absent, `DEPENDS_ON` is unset (no base override — default-branch behavior). `--auto-stack` is a bare flag (consumes no value); default `AUTO_STACK=false`. It applies to Modes A/B only — in Mode C it is silently ignored (no Builder phase to stack). `--auto-stack` and a single-issue `--depends-on N` may both be present: `--depends-on` seeds `DEPENDS_ON[N]` for its named issue and auto-stack detection fills in the rest of the map; a detected edge never overrides an explicit `--depends-on` for the same issue. `--claim-owned N` consumes its following token as the daemon-claimed issue number (a positive integer); a malformed or missing value is **fail-safe, not fatal** — treat `CLAIM_OWNED` as unset (do NOT EXIT, and do NOT trust a corrupt value) rather than rejecting the whole invocation, since a daemon-only flag with a bad value must never block or hard-fail the sweep it was meant to unblock. When absent, `CLAIM_OWNED` is unset and the "Step 1a — daemon self-claim check" in Per-issue pre-flight never applies (ordinary `loom:building` skip behavior for every issue, exactly as before this flag existed).
- At least one candidate (numeric token or NL description) must be supplied. If `$ARGUMENTS` (after stripping flag tokens) is empty, display:
  ```
  Usage: /loom:sweep <issue-number> [<issue-number> ...] [--builders-per-wave N] [--dry-run] [--no-daemon]
         /loom:sweep <natural-language description>     [--builders-per-wave N] [--dry-run] [--no-daemon]
         /loom:sweep all                                [--builders-per-wave N] [--dry-run] [--no-daemon]   # build everything (whole open backlog)
         /loom:sweep all --prs                          [--dry-run]                                         # every open PR (Mode C)
         /loom:sweep --prs <pr-number> [<pr-number> ...] [--dry-run]
         /loom:sweep --prs <natural-language PR description> [--dry-run]
         /loom:sweep <natural-language PR description>       [--dry-run]   # PR NL triggers select Mode C

  See #3298, #3384, #3454, and #3568 for the full design.
  ```
  and EXIT.
- **Mode-selection precedence** (apply in order):
  0. **Build-everything sentinel.** If the non-flag token list (after flag-stripping) is **exactly `["all"]`** — a single token, case-insensitive (`all`, `ALL`, `All`) — take the dedicated **build-everything** path (see "Build-everything sentinel (`all`)" below). This step is evaluated **before** every other step so the bare `all` token can never be swallowed by the Mode B NL classifier (step 4):
     - `--prs` **absent** → resolve the deterministic **entire open-issue** set (no label filter), set `SWEEP_ALL_AGGRESSIVE=true`, then hand off to the Mode A/B issue-set wave machinery (confirmation gate, Stage -1 backend detection, wave partition — all as today, but with the aggressive pre-flight overrides).
     - `--prs` **present** → resolve the deterministic **entire open-PR** set and drive the existing **Mode C** PR-set lifecycle (subagent path); C0 pre-flight filters non-actionable PRs.

     The guard is `lowercased(non_flag_tokens) == ["all"]` — length exactly 1. Any additional non-flag token (`all open loom:issue items`, `all my agent-filed ...`, `all merge-ready PRs`) has length > 1 and falls straight through to steps 1–4 unchanged. This is the backward-compatibility contract.
  1. If `--prs` is present, classify as **Mode C** (numeric → explicit PR list; NL → translated `gh pr list`).
  2. Else if any non-flag token does not match `^#?\d+$` AND the description contains a PR-side NL trigger phrase (see Mode C "PR-side NL trigger phrases"), classify as **Mode C** (NL-inferred).
  3. Else if every non-flag token matches `^#?\d+$`, classify as **Mode A** (numeric issue list).
  4. Else classify as **Mode B** (NL issue list).

  This ordering is deliberate: the bare `all` sentinel is intercepted first (else it would land in step 4), an explicit `--prs` flag is the next strongest signal, an unambiguous NL trigger is next, and the existing Mode A/B classifier (regression-guarded) handles everything else.

- **Build-everything sentinel (`all`)** — the deterministic path taken by step 0 above. This is the **fast/sloppy "promote and sweep everything" command**: it resolves the *entire* open backlog and aggressively drives each item toward a merged PR rather than filtering to a pre-curated subset.
  - **Trigger**: non-flag tokens exactly `["all"]` (case-insensitive). Flags (`--dry-run`, `--builders-per-wave N`, `--prs`, `--no-daemon`) are stripped first and compose normally, so `all --dry-run` and `all --builders-per-wave 2` still trigger the sentinel.
  - **Aggressive-mode flag**: resolving the candidate set via this sentinel sets the internal flag `SWEEP_ALL_AGGRESSIVE=true`, carried into the Wave Lifecycle. It **overrides the conservative pre-flight skip rules** (Wave Lifecycle step 1) with the recovery routing in the "Aggressive candidate taxonomy" table below. Mode A/B explicit-list and NL invocations never set this flag — their skip rules are unchanged.
  - **Candidate resolution (issues, `--prs` absent)** — one deterministic `gh issue list` call, **no label filter**, no LLM/NL translation:
    ```bash
    "$GH_READ" issue list --state open --limit 100 --json number,title,labels,updatedAt
    ```
    Every open issue is a candidate regardless of label — promotion, unblocking, stale-claim recovery, and epic fan-out happen per-issue per the "Aggressive candidate taxonomy" table below, not by pre-filtering the query (`updatedAt` feeds the staleness rule). `$GH_READ` is the cached-read wrapper (see "Cached forge reads (`gh-cached`)"); candidate resolution is a pure observation read — every claim decision is re-made from an **uncached** per-issue pre-flight read in Wave Lifecycle step 1, so a 30s-old listing cannot cause a stale claim. Pass `--limit 100` explicitly (never rely on gh's default of 30) and apply the existing **edge-case rules**: zero matches → print the resolved query + empty result and EXIT cleanly (edge case #1, do **not** fall through to any other mode); 100 candidates returned → warn about truncation and ask the operator to narrow (or deliberately raise `--limit`) before proceeding (edge case #2). If this call fails with a rate-limit signature, re-issue it over REST per Mode B's "GraphQL-exhaustion fallback" (`repos/{owner}/{repo}/issues?state=open&per_page=100`, PRs filtered out) — the sentinel's query is a `gh issue list` like any other.
  - **Orphaned-claim recovery pass (run once, AFTER the confirmation gate, before per-issue pre-flight)** — reclaim `loom:building` labels left behind by dead workers so stale claims don't mask buildable issues. Since Issue #6167, the same pass also reclaims stale PR-side `loom:reviewing` (Judge) / `loom:treating` (Doctor) claim overlays on any open PR — the "dead Judge/Doctor left a claim label behind" analogue of the issue-side reclaim, run proactively for the whole PR set rather than only when a Judge/Doctor happens to be dispatched for that specific PR (see "`loom:reviewing`/`loom:treating` are claim overlays" under Mode C's C0 pre-flight):
    ```bash
    ./.loom/scripts/recover-orphaned-shepherds.sh --recover
    ```
    - **Capability pre-probe (read-only, run at candidate resolution — BEFORE the confirmation gate).** The mutating `--recover` pass above runs only *after* the gate, but the operator needs to know *at* the gate whether it will actually reclaim anything, so probe the recovery path's availability during candidate resolution by invoking the script in its **default dry-run mode** (no `--recover` — it performs the same `loom_locate_daemon_bin` resolution and mutates nothing):
      ```bash
      ./.loom/scripts/recover-orphaned-shepherds.sh >/dev/null 2>orphan_probe.err; PROBE_RC=$?
      ```
      Route on the exit code and surface the outcome in the **candidate-set listing / confirmation-gate output** (same `⚠`-annotation channel as the "Operator-gate advisory scan"). The script documents its own exit codes in its header (#6392): `0` assessed clean, `1` this wrapper could not reach the subcommand at all (no/stale binary), `2` assessed successfully AND found orphans (dry-run report, not a failure), `3` the assessment itself errored out.
      - **`PROBE_RC == 0`** → the binary resolved; the `--recover` pass will run after the gate. **Emit no warning** — this is the common path and must stay noise-free (no spurious annotation on a healthy host).
      - **`PROBE_RC == 2`** → the binary resolved AND the assessment ran successfully — it just also found reclaimable claims, which the post-gate `--recover` pass below will handle. This is **not a failure**; it is the expected shape of a healthy pre-probe on a host that actually has stale claims to reclaim. **Emit no warning** here either — same noise-free rule as `PROBE_RC == 0`.
      - **`PROBE_RC != 0` AND `orphan_probe.err` contains `no loom-daemon binary could be resolved`** → surface this **operator-actionable** line at the top of the candidate listing (above the per-issue rows, like the overlap warning):
        > `⚠ orphan-claim recovery unavailable: no loom-daemon binary resolved — stale loom:building claims are NOT being actively reclaimed, only staleness-checked (updatedAt). Remedy: build/update loom-daemon (cargo build --release -p loom-daemon, or ./.loom/scripts/cli/loom-daemon-update.sh), then re-run.`
      - **`PROBE_RC != 0`, not `2`, for any other reason** (e.g. a stale binary that predates the `recover-orphans` subcommand, or an unexpected failure — genuinely `1` or `3` per the exit-code table above) → surface a **distinct** line that quotes the first stderr line so the operator sees the real cause and knows a different remedy applies:
        > `⚠ orphan-claim recovery pre-probe failed (exit <PROBE_RC>): "<first line of orphan_probe.err>" — stale loom:building claims will not be reclaimed this run; they fall through to the updatedAt staleness rule only.`
        This quote is only ever diagnostic since #6392: `recover-orphaned-shepherds.sh` suppresses the binary-resolution success trace on this path (it only prints under `--verbose`, which this probe never passes) so the first stderr line is always a genuine error, never the "resolved `<path>` via `$PATH`" resolution trace misread as a failure reason.
      The cases are **deliberately distinguished**: "no binary resolved" has a concrete build/install remedy, `2` is a clean bill of health for the probe itself (just with work for the post-gate pass to do), and any other non-zero exit is surfaced verbatim so the operator can diagnose it. This warning is **advisory only** — it never changes a planned action, never blocks the sweep, and never mutates a label; a stale `loom:building` claim still falls through to the `updatedAt` staleness rule in the taxonomy table.
    - **Post-gate `--recover` pass.** After the operator confirms, run the mutating `--recover` command above. Best-effort at this point: a non-zero exit is logged and ignored (never abort the sweep) — the operator was already warned at the gate by the pre-probe, so this pass does not re-prompt. Any issue still labeled `loom:building` after it is re-checked inline by the staleness rule in the taxonomy table.
    **Ordering is load-bearing**: the mutating `--recover` pass runs *only after* the operator confirms the resolved plan at the mandatory confirmation gate — never before (the read-only pre-probe is safe to run earlier because it mutates nothing). Both the pre-probe and the `--recover` pass are **skipped entirely under `--dry-run`** (the dry-run gate is read-only and EXITs before any mutation; the dry-run plan already states recovery is skipped). This preserves the file-wide "gate before mutation" invariant: nothing on disk or on the forge changes until the operator has confirmed (or `--dry-run` has printed and exited).
  - **Candidate resolution (PRs, `--prs` present)** — every open PR is *resolved*, handed to the Mode C PR-set lifecycle (subagent path):
    ```bash
    "$GH_READ" pr list --state open --limit 100 --json number,title,labels
    ```
    Mode C's C0 pre-flight already skips PRs with no actionable label, `loom:operator-only`, `loom:needs-capability`, or `loom:blocked` — with the one narrow, fail-closed carve-out for a capability-matched `loom:operator-mechanical` PR (#6893, see "Capability-aware `loom:operator-mechanical` lane") — and routes the rest by current label (Judge / Doctor → Judge / Merge) — so grabbing every open PR and letting C0 filter matches the "get every in-flight PR over the finish line" intent. **"Every open PR" describes the query's coverage, not the sweep's (#6218).** A PR opened outside the Builder path (e.g. `gh api --method POST .../pulls`, a human draft, a Dependabot bump) never picks up `loom:review-requested`, so it is resolved as a candidate here and then silently skipped by C0 for `no actionable label` — which is the correct call (an unlabeled PR may be deliberately outside the pipeline) but is easy to miss when the run reports "Total: 0 judged, N skipped" and exits 0. See "No-actionable-label PR advisory" immediately below for how this is surfaced *before* confirmation instead of only in the per-PR skip log. Same zero-match / truncation edge-case rules apply, and the same rate-limit fallback: on a rate-limit signature, re-issue over REST per Mode C's "GraphQL-exhaustion fallback" (`repos/{owner}/{repo}/pulls?state=open&per_page=100`).
  - **No-actionable-label PR advisory (#6218).** Because a PR resolved by the query above can still be invisible-skipped by C0, the sentinel checks each resolved PR's labels **before** the confirmation gate and, when ≥1 resolved PR carries none of `{loom:review-requested, loom:changes-requested, loom:pr}`, prints a summary block above the candidate listing naming every one, so the operator learns the touched set is smaller than the resolved set at the moment they are asked to confirm, not after the run exits:
    ```
    No-actionable-label PR advisory (#6218):
      PR #205 "Bump lodash from 4.17.20 to 4.17.21"   labels: (none)   — will be skipped (no actionable label)
      PR #206 "WIP: prototype new auth flow"          labels: (none)   — will be skipped (no actionable label)
    ADVISORY ONLY — neither PR above was labeled or dispatched because of this block. An
    unlabeled PR may be a human draft, a Dependabot bump, or deliberately outside the
    pipeline. Apply loom:review-requested by hand (gh pr edit <N> --add-label
    loom:review-requested) and re-run if you want the sweep to pick it up.
    ```
    **Advisory only** — this block never applies a label, never adds the PR to the wave plan, and never blocks the sweep (mirroring the "Operator-gate advisory scan"'s own advisory-only contract). **Zero matches ⇒ no block printed** — the plan is byte-for-byte identical to a run made before this advisory existed, matching the same "no block when clean" contract the `Overlap analysis` / `Detected stacking pairs` / `Operator-gate advisory` blocks already honor. `--dry-run` composes: this is a **read-only** label check with no mutation, so the same block also appears above the PR-set `--dry-run` plan, where each matching PR's row already independently carries `would skip (no actionable label)` per the PR-set output spec below — the summary block and the per-PR row are two views of the same signal, and neither replaces the other.
  - **Existing-PR routing (issues path)**: the sentinel adds **no** new PR-detection logic. Issues with an open linked PR are handed to the wave machinery, which routes an issue with one open linked PR to Judge (or Merge if the PR is already `loom:pr`) via the per-issue existing-PR probe (Wave Lifecycle step 1, #3359 + #3677 — the union of `closedByPullRequestsReferences` and timeline `cross-referenced` open-PR events that are phrase-confirmed as `Part of #N` / `Contributes to #N` (#6216), with the `state == OPEN` exclusion applied at the per-PR read (#6217), so a non-closing partial-increment PR is detected while a bare mention is not). This is the single source of truth for existing-PR routing and **takes precedence over the label routing** in the taxonomy table (an issue with an open PR is driven to merge, never rebuilt).
  - **Mandatory confirmation gate**: the sentinel path **always** displays the resolved candidate set (with the per-issue planned action from the taxonomy table) and awaits operator confirmation before spawning any agent — identical to Mode B/C's "display candidate set before spawning any agents" rule. A whole-backlog sweep must never auto-dispatch silently. Declining EXITs cleanly. **When the resolved plan has an unavoidable same-file overlap** (more overlapping candidates than waves to spread them across — see "Overlap-aware wave partitioning"), print the overlap warning **above** the candidate listing, naming the shared files and the specific candidates, so the operator can reorder or drop to `--builders-per-wave 1` before confirming. **When any candidate matched the "Operator-gate advisory scan"** (#5137, below), each matching candidate's line in this same listing carries its `⚠ body declares operator-gating: "<phrase>"` / `⚠ depends on #A, which is loom:operator-only` (or `loom:needs-capability`) annotation(s) — advisory only, never a routing change; zero matches leave the listing byte-for-byte unchanged. **When the orphaned-claim recovery pre-probe found no usable `loom-daemon` binary (or failed for any other reason)** — see "Orphaned-claim recovery pass" above — its `⚠ orphan-claim recovery unavailable …` / `⚠ orphan-claim recovery pre-probe failed …` line is printed **above** the candidate listing too, so the operator knows stale `loom:building` claims will not be actively reclaimed this run before confirming; a clean pre-probe (exit 0) adds nothing. **For `all --prs`, when any resolved PR carries none of `{loom:review-requested, loom:changes-requested, loom:pr}`** — see "No-actionable-label PR advisory" above — its summary block is printed above the candidate listing too, naming every such PR, so the operator can tell the touched set is smaller than the resolved set before confirming; zero matches leave the listing byte-for-byte unchanged.
  - **Flag composition**: `--dry-run` resolves the candidate set, prints the standard issue-set (or PR-set) dry-run plan with wave grouping + the aggressive per-issue actions, and EXITs with no mutation (the Stage-0 dry-run contract is backend-independent — the orphaned-claim recovery pass is skipped under `--dry-run`). `--builders-per-wave N` and `--no-daemon` compose with the wave / Stage -1 machinery exactly as for Mode A/B. Stage -1 backend detection is unchanged: after `all` resolves the issue set, the normal strict-AND daemon/pool probe decides daemon-dispatch vs subagent fallthrough; `all --prs` (Mode C) always routes to the subagent path per the existing Mode C short-circuit.

- **Aggressive candidate taxonomy** (the single source of truth for what `all` resolves and how each label class is routed — lives here beside the Mode B label logic so there is one definition). When `SWEEP_ALL_AGGRESSIVE=true`, **every** open issue is a candidate and is routed by its current label class:

  | Label class | Aggressive routing |
  |-------------|--------------------|
  | `loom:issue` | Build directly (already promoted). |
  | `loom:curated` | Promote to `loom:issue` (Approval gate, step 3) → build. |
  | Uncurated: none / `loom:triage` / `loom:curating` | Curate (step 2) → promote → build. |
  | Stale `loom:building` | Reclaim → build. "Stale" = no **open** linked PR **and** `updatedAt` older than `LOOM_STALE_BUILDING_HOURS` (default 2). "Open linked PR" here means the **union** probe (step 1, #3359 + #3677, phrase-confirmed since #6216) — `closedByPullRequestsReferences` **and** phrase-confirmed timeline `cross-referenced` open-PR events — so an in-flight non-closing `Part of #N` slice PR counts and blocks reclaim, but a PR that merely mentions the issue number in prose does not. Fresh `loom:building` (recently updated, or has an open PR) is genuinely in flight → route its open PR (if any) to Judge/Merge, else skip with `in flight (fresh loom:building)`. |
  | `loom:blocked` | Probe the blocker: if every `#N` it depends on (parsed from the blocker comment / issue body via `defaults/.claude/commands/loom/guide.md`'s `parse_dependencies` convention — tolerant of markdown emphasis/colon between the phrase and `#N`, e.g. `**Blocked by:** #1 (reason), #3 (reason)`, #4508) is CLOSED/MERGED, remove `loom:blocked` → build. If a dependency is still open → skip with `still blocked by #N`. If no dependency is parseable, first check the blocker text (comment / issue body) for hold/defer phrasing — case-insensitive match on instruction-shaped fragments `hold until`, `wait until`, `defer`, `not before`, `do not start` (not a bare substring match on `hold`/`wait` alone, to avoid false positives like "waiting on CI"). On a match → **do not** remove `loom:blocked` and **do not** build; skip with `explicit hold: "<quoted phrase>"`. Otherwise (truly empty/unparseable, no hold/defer phrasing) → remove `loom:blocked` and attempt anyway (fast/sloppy), unchanged. |
  | `loom:epic` | Fan out: build its open `loom:epic-phase` children (already in the candidate set). Skip the container with `expanded to #a #b …`. If it has **no** open phase children → skip with `needs decomposition (run Champion/Architect)` — a container is not directly buildable. |
  | `loom:epic-phase` | Build directly (a phase issue is a normal buildable unit). |
  | Has an **open** linked PR (any label) | Drive the existing PR through Judge / Doctor → Merge via the step-1 union probe (#3359 + #3677 — closing-keyword **and** phrase-confirmed non-closing `Part of #N` timeline references, #6216) — do not build a duplicate. Takes precedence over every row above. **On the daemon-dispatch path this row is reachable only as a `kind={"PrSet":[P]}` dispatch** — an issue-keyed `dispatch_sweep` for such a candidate is refused by the daemon's own open-PR guard (#4123) before any child exists, so the child pre-flight that would perform this routing never runs (#6593; see "The daemon-dispatch path"). |
  | `loom:abort` | Reclaim like a stale claim only if `updatedAt` is stale; otherwise skip with `abort flag set`. |
  | `loom:operator-only` **+ `loom:operator-mechanical`, every declared capability held** | **Propose (dry-run only)** — the one exception to the row below (#6893). The item's body declares `<!-- loom:capability=<name> -->` marker(s) and this worker holds **all** of them, so route it to the "Capability-aware `loom:operator-mechanical` lane" section below instead of skipping. Log `mechanical propose lane (holds: <caps>)`. **Never live execution** — the lane produces commands/a PR for the operator to approve. |
  | `loom:operator-only` **+ `loom:operator-mechanical`, a declared capability NOT held** | **Skip, but comment first** — post a capability request naming the missing capability (see the lane section below), then log `mechanical: missing capability <name>` and move on. This is the one skip row that leaves a forge comment: the point is to turn a silent stall into a named request. |
  | `loom:operator-only` (every other case — including `loom:operator-mechanical` with no marker, an unrecognized marker value, or any of the other three sub-kinds) | **Skip** — a hard exclusion, unchanged. Requires a human (credentials, hardware, infra); automation cannot complete it. Log `operator-only (human required)`. The two rows above are a **narrow, fail-closed** carve-out: anything that does not match them exactly lands here. |
  | `loom:needs-capability` | **Skip** — the same hard exclusion as `loom:operator-only`. Blocked on a missing tool/agent capability, not an operator-by-right decision (#5817); automation cannot complete it either way. **Unaffected by the capability lane** (#6885 non-goal): this label asserts the capability does not exist yet, which no worker can hold. Log `needs-capability (missing capability)`. |

  - Every recovery action (reclaim, unblock, promote, fan-out) only *removes* or *swaps among* labels that already exist on the repo — the sentinel invents no labels.
  - **PR variant (`--prs`)**: the candidate set is every open PR; C0 pre-flight routes `loom:review-requested` → Judge, `loom:changes-requested` → Doctor → Judge, `loom:pr` → Merge, and skips PRs with no actionable label, `loom:operator-only`, `loom:needs-capability`, or `loom:blocked` (except a capability-matched `loom:operator-mechanical` PR, #6893). **Resolved is not the same as touched (#6218)**: a PR with no actionable label is a member of the resolved candidate set but is never dispatched — see "No-actionable-label PR advisory" above for how that gap is surfaced at the confirmation gate.
  - **Body text alone never changes this table's routing** — the `loom:operator-only` and `loom:needs-capability` rows above are a hard skip only when that **label** is present. A candidate whose body merely *describes* operator-gating (without the label), or whose declared `Depends on #A`/`Requires #A` is itself `loom:operator-only` or `loom:needs-capability`, is routed exactly as its actual label class dictates, plus an advisory `⚠` annotation at the confirmation gate — see "Operator-gate advisory scan" immediately below "Overlap-aware wave partitioning".
- **Mode A** (every non-flag token matches `^#?\d+$`, `--prs` absent, no PR NL trigger):
  - Strip leading `#` from each token, parse as a positive integer.
  - Reject any token that fails to parse as a positive integer (after stripping). Display an error showing the offending token and EXIT.
  - Deduplicate the issue list (preserve first-seen order).
- **Mode B** (any non-flag token does not match `^#?\d+$`, `--prs` absent, no PR NL trigger):
  - Translate the description to `gh issue list` invocation(s) per the guide above.
  - Run the command, deduplicate, and **display the candidate set to the user before spawning any agents.** Await confirmation. If the user declines, EXIT cleanly. When the resolved candidate set has an **unavoidable** same-file overlap (see "Overlap-aware wave partitioning"), print the overlap warning above the candidate listing — naming the shared files and candidates — so the operator can reorder or drop to `--builders-per-wave 1` before confirming.
  - If the description is ambiguous, hits an out-of-band query, or references an unknown label, ask for clarification first — do not guess.
- **Mode C** (`--prs` flag present, OR PR-side NL trigger detected):
  - If every non-flag token matches `^#?\d+$`: strip leading `#`, parse as positive integers, deduplicate (preserve first-seen order). Reject any non-parseable token with a clear error and EXIT. Resolved list is **PR numbers**.
  - If any non-flag token does not match `^#?\d+$`: translate to `gh pr list` invocation(s) per the PR-side guide above. Run the command, deduplicate, and **display the candidate set to the user before spawning any agents.** Await confirmation. If the user declines, EXIT cleanly.
  - If the description is ambiguous between issues and PRs (e.g., `loom:review-requested` is PR-only but the description omits "PRs" / "pull requests"), ask the user to clarify before proceeding. Do not guess.
  - If `--builders-per-wave N` was supplied, print a one-line note that the flag has no effect in Mode C and proceed without it (Mode C waves are size-1; see Mode C section).
- **`--builders-per-wave N` validation:**
  - **Absent flag → `auto`.** When the operator did not pass `--builders-per-wave`, `BUILDERS_PER_WAVE=auto`; skip the integer validation below and resolve the concrete size at Stage -1 ("Resolve auto wave size"). The rules below apply **only** when an explicit value was passed — an explicit integer always overrides auto and is validated verbatim as before.
  - Parse `N` as an integer. Reject non-integer values with a clear error and EXIT.
  - Reject `N < 1` (including `0` and negative values) with: `Error: --builders-per-wave must be >= 1 (got: <N>)` and EXIT. Do **not** silently default to `1`.
  - If `N > 6`, print a warning and continue: `WARNING: --builders-per-wave=<N> is unvalidated. N<=6 is recommended; N>=7 may exhaust context or hit rate limits. Proceeding at your own risk.`
  - If `N` exceeds the number of candidates at any wave, **silently clamp** to the candidate count for that wave. Do not warn, do not stall.
- **`--no-daemon` validation:**
  - Bare flag, no value. If a value is supplied (`--no-daemon=true`, `--no-daemon something`), treat the `=value` form as an error and EXIT (`Error: --no-daemon takes no value`). The standalone-token form is the only accepted spelling.
  - Honoured in all three modes (A, B, C). The flag is a no-op in Mode C (Mode C is always subagent-side — see Stage -1's `DECIDE` precedence below) but is accepted without error so operators can pass it unconditionally from scripts.
  - When `NO_DAEMON=true`, Stage -1 short-circuits to the subagent path **before** issuing the daemon Ping probe. No daemon-state files are read or written, and no `mcp__loom__*` calls are made for backend probing.

**Wave-size guidance:**

| `N` | Status |
|-----|--------|
| `auto` | **Default** (flag omitted). Resolved at Stage -1 from the backend + scratch-volume disk headroom: daemon detached-process path targets up to 10; in-session subagent path **core-scales** in `[3, 6]` via `clamp(floor((cores-2)/4), 3, 6)` (#3693). Clamped by candidate count and disk, floor 1. See "Resolve auto wave size". |
| `1` | Fully sequential (MVP-compatible). Explicit override of `auto`. |
| `2` | **Recommended** explicit starting point for parallel subagent waves. |
| `3` | Tested and validated. The #3289-safe **floor** for the **subagent** path — the default auto-resolved target now scales with cores up to 6. |
| `4`–`6` | Evidence-supported for the **subagent** path on multi-core hosts (#3693); reached automatically by the core-scaled `auto` default. Operator discretion via explicit override. |
| `>= 7` | Unvalidated **for the subagent path**. Warns at parse time. Operator discretion. |

The subagent-path target is **soft** — there is no hard upper bound and the warning is the only guard. Its auto default core-scales within `[3, 6]` (floor 3 on small/shared hosts, ceiling 6 on big ones). The `[3, 6]` band is a **width** decision: at one level deep, width is bounded by the harness concurrency cap (`min(16, cores-2)`), not by #3289. The #3289 nested-dispatch stall is specifically about parallel *grandchildren* (`parent → nested /loom:sweep → builder`), which `/loom:sweep` never does — it dispatches builders directly, one level deep (see "CRITICAL: One level deep"). The ceiling stays 6 (not 8/10) because single-account rate-limit burn and orchestrator context-window pressure (every wave member's Task result is read back into the same session) both bind before #3289 does. High parallelism toward 10 is reached **only** via the daemon detached-process path (`mcp__loom__dispatch_sweep`), where each sweep is an isolated OS process — not a nested subagent. Never raise the subagent ceiling toward 10; route through the daemon instead.

## Examples

### Mode A — Explicit numeric list (fast path)

```bash
/loom:sweep 123                                    # Sequential lifecycle for issue 123
/loom:sweep 123 456 789                            # Sequential lifecycle for three issues
/loom:sweep #1083 #1080                            # Leading # is allowed
/loom:sweep 123 456 789 --builders-per-wave 2      # Two builders per wave (recommended)
/loom:sweep 1 2 3 4 5 6 --builders-per-wave 3      # Three builders per wave (validated)
/loom:sweep 1 2 --builders-per-wave 5              # Silently clamps to 2 (candidate count)
/loom:sweep 123 456 789 --dry-run                  # Print plan and EXIT without mutating
/loom:sweep 1 2 3 4 5 --dry-run --builders-per-wave 2  # Preview with wave grouping
/loom:sweep 123 456 --no-daemon                    # Force in-process subagent dispatch even when daemon is up (#3454)
```

### Build everything — the `all` sentinel

```bash
# Fast/sloppy "promote and sweep everything": resolves EVERY open issue and
# aggressively drives each toward a merged PR — curating uncurated issues,
# reclaiming stale loom:building claims, probing loom:blocked issues for a
# cleared blocker, fanning loom:epic containers out to their phase children,
# and driving any existing open PR through Judge / Doctor → Merge. Only
# loom:operator-only and loom:needs-capability issues are hard-skipped.
# Displays the resolved plan and awaits confirmation before dispatching.
/loom:sweep all

# Case-insensitive — ALL / All also trigger the sentinel
/loom:sweep ALL

# Preview the whole-backlog plan (per-issue action + wave grouping) without mutating
/loom:sweep all --dry-run

# Same aggressive set, two builders per wave
/loom:sweep all --builders-per-wave 2

# Every open PR, driven through Judge / Doctor → Merge per its current label (Mode C)
/loom:sweep all --prs
/loom:sweep all --prs --dry-run

# NOT the sentinel — >1 non-flag token, still routes to Mode B exactly as before
/loom:sweep all open loom:issue items
/loom:sweep all my agent-filed loom:issue items --builders-per-wave 2
```

### Mode B — Natural-language description

```bash
# Label filter — translates to: gh issue list --label loom:curated --state open --limit 100
/loom:sweep all loom:curated issues

# Compound label + author + time filter — translates to:
#   gh issue list --label loom:curated --author rjwalters \
#                 --search "created:>=2026-05-17" --state open --limit 100
/loom:sweep all loom:curated issues filed by rjwalters in the last week

# Title search on a label-filtered set — translates to:
#   gh issue list --label loom:issue --search "docs in:title" --state open --limit 100
/loom:sweep loom:issue items with 'docs' in the title

# "My" → --author @me (Loom files but does not self-assign):
/loom:sweep all my agent-filed loom:issue items --builders-per-wave 2

# Mixed mode — union of explicit numbers AND an NL-derived set:
/loom:sweep #3310 #3312 and any other loom:issue with 'docs' in the title

# Dry-run a NL-derived candidate set before committing to side effects:
/loom:sweep all loom:curated issues --dry-run
```

### Clarification triggers (Mode B asks before spawning)

```bash
# Ambiguous time window — asks "what duration do you mean?"
/loom:sweep recent loom:issue items

# Out-of-band query — gh issue list cannot inspect file paths in the diff
/loom:sweep issues labeled loom:issue except the ones touching loom-daemon

# Unknown label — 'bug' is not in the repo's label set (from `gh label list`); ask which label was meant
/loom:sweep all my agent-filed bugs that aren't blocked

# Pure nonsense — no derivable candidate set
/loom:sweep nonsense gibberish

# Ambiguous between Mode B (issues) and Mode C (PRs) — loom:review-requested
# is PR-only but the description does not say "PRs". Ask which was meant.
/loom:sweep all loom:review-requested
```

### Mode C — PR-set mode (explicit `--prs` flag)

```bash
# Explicit numeric PR list — each PR routed by its current label
# (review-requested → Judge, changes-requested → Doctor→Judge, loom:pr → Merge)
/loom:sweep --prs 100 101 102

# Leading # is allowed
/loom:sweep --prs #100 #101 #102

# Single PR — back-half-only handling (Judge → Doctor → Merge) for that PR
/loom:sweep --prs 100

# Dry-run a PR-set plan — prints per-PR action plan and EXITs without mutating
/loom:sweep --prs 100 101 102 --dry-run

# NL description with explicit flag — translates to: gh pr list --label loom:pr --state open --limit 100
/loom:sweep --prs all open loom:pr

# Compound filter — translates to:
#   gh pr list --label loom:review-requested --author @me --state open --limit 100
/loom:sweep --prs all my review-requested PRs
```

### Mode C — PR-set mode (NL trigger, no flag)

```bash
# "PRs" in the description selects Mode C even without --prs:
# translates to: gh pr list --label loom:pr --state open --limit 100
/loom:sweep all open loom:pr PRs

# "pull requests" also triggers Mode C:
/loom:sweep all loom:review-requested pull requests

# "merge-ready PRs" triggers Mode C:
/loom:sweep all merge-ready PRs
```

## Execution Model

`/loom:sweep` processes the candidate list in **waves**:

- **Mode A/B (issue-set)**: the candidate list is partitioned into waves of up to `N = --builders-per-wave` issues, where an omitted flag resolves to the Stage -1 auto wave size (see "Resolve auto wave size" — up to 10 on the daemon path, core-scaled within `[3, 6]` on the subagent path, disk-clamped). Issues are picked into waves in order. Within a wave, builders are dispatched in parallel; across waves, processing is sequential. Each wave fully settles (all builders → per-PR Judge → optional Doctor → merge) before the next wave starts.
- **Mode C (PR-set)**: the candidate list is processed in **size-1 waves** (one PR per wave). `--builders-per-wave` is ignored because there is no Builder phase. Each PR is routed per its current label (Judge / Doctor→Judge / Merge — see "PR-set Wave Lifecycle" below) and fully settles before the next PR is touched. Sequential per-PR processing is a **width** choice — parallel Judge/Doctor across PRs is unbenchmarked and every wave member's Task result is read back into this orchestrator session (context pressure) — and parallels the issue-side "per-PR Judge is sequential within a wave" policy. It is **not** the #3289 rule, which governs nested (grandchild) dispatch depth, not wave width.

> **CRITICAL — GitHub `mergeable`/`mergeStateStatus` is a base-branch-only check, NOT a sibling-PR conflict check.** GitHub evaluates every PR against the **base branch** independently; it has **no concept of the other PRs a sweep has in flight in the same wave**. So two sibling PRs that both edit the same file can each report `MERGEABLE` / `CLEAN` at the same instant, and the conflict only becomes visible **after the first one merges** — the second PR flips to `CONFLICTING` the moment its base moves. This repo's branch ruleset provides **no** server-side backstop either: it has no `required_status_checks` and no "require branches up to date before merging" rule, so `merge-pr.sh --auto` will merge a clean-but-stale sibling immediately without re-checking it against the new `main`. **Never treat a green `mergeable`/`mergeStateStatus` as evidence that a wave's PRs are mutually compatible.** Two defenses cover this gap: the **proactive** overlap-aware partitioning below (keep same-file candidates out of the same wave, or warn) and the **reactive** intra-wave overlap revalidation in Wave Lifecycle step 7 (re-check each about-to-merge PR against the just-merged `main`). See "Overlap-aware wave partitioning" and step 7.

### CRITICAL: Only Builders parallelize — issue-creating roles must be serialized (issue #3707)

**Waves parallelize Builders only.** The reason a wave can safely fan out `N` agents at once is that each Builder works in an isolated git worktree and produces **exactly one PR at the end** — no shared mutable forge state is touched mid-run, so two concurrent Builders never collide. `/loom:sweep` itself only ever dispatches Builders (plus per-issue Curator/Judge/Doctor, which run **sequentially within a wave**), so today's wave loop is safe by construction.

**Exception: the git stash stack (#4821).** `refs/stash` is shared across
*every* linked worktree of the repo, not per-worktree — so if two
concurrent Builders in a wave both use bare `git stash` for ad-hoc WIP
handling, one can pop or drop the other's entry (observed in production:
kicad-tools PRs #4524/#4526). This is the one piece of shared mutable state
the isolated-worktree argument above does not cover. Builders must use
`./.loom/scripts/worktree.sh snapshot <issue-number>` (a per-worktree patch
file) instead of `git stash` for WIP — see `defaults/roles/builder.md`.

**Never dispatch two or more issue-creating agents concurrently.** Agents that **create issues** — Architect proposals, Curator oversized-issue decomposition, Champion epic-phase creation — mutate the forge's **shared, server-assigned issue-number space** with no client-side coordination, transaction, or idempotency key. When two such agents run `gh issue create` bursts at the same time they **race on issue numbers and cross-contaminate bodies** (one epic's title paired with another's body), and any recovery/retry loop that PATCHes-by-title amplifies the damage by winning every write race against the other still-active filer. This is not hypothetical: it was observed 2026-07-21 on a 4-wide wave (1 builder + 3 architects) — 2 duplicate issues, 3 with mismatched title/body, and a corrupted roadmap comment, all needing manual reconciliation (#3707).

This rule targets roles whose **primary output is an issue-creation burst** — Architect, Curator-decomposition, Champion epic-phase — not any single `create-issue.sh` call anywhere in a wave: a Builder, Judge, or Doctor filing one follow-up issue for an out-of-scope discovery it hit mid-task is not the hazard this section guards against and remains permitted, and expected, even inside a parallel wave.

Concrete rules for anyone extending this skill or hand-driving a wave:

- **Do NOT construct a mixed wave** that places any issue-creating role (Architect / Curator-decomposition / Champion epic-phase) alongside Builders — or alongside another issue-creating agent. That exact `1 builder + 3 architects` shape is the footgun this section forbids.
- **Serialize issue-creating agents**: one must finish its entire `gh issue create` burst before the next starts. A recovery/retry loop must never run against a still-active concurrent filer. **"Serialize" here means awaited-to-completion, not merely dispatched-with-a-sync-flag** — see "Subagent dispatch is async-only" below (#3822).
- Parallel **Builders** remain safe and are the only role `/loom:sweep` fans out — this is unchanged.

Heavier mitigations (a per-wave issue-filing lock, an epic-scoped idempotency UUID + post-create reconciliation, or a serialized issue-filing sub-phase inside `/loom:sweep`) are **deferred, out-of-scope follow-ups** to this documentation guardrail — build them only if serialization-by-convention proves insufficient in practice (#3707).

### CRITICAL: Subagent dispatch is async-only — you MUST block explicitly (issue #3822)

**The harness may launch every Agent/Task subagent asynchronously regardless of the dispatch flags.** In particular, `run_in_background: false` is **not** a guarantee of synchronous return — it has been observed ignored, with the agent launched async anyway (2026-07-23, Claude Code harness). An orchestrator that trusts a sync-flag and proceeds immediately can start a downstream serialized phase before the upstream agent has finished — e.g. begin Judge before builders finish, or overlap two issue-creating agents (the exact #3707 race this skill forbids).

Therefore, at **every** dispatch site where this skill sequences one phase after another, the orchestrator **MUST explicitly await each subagent's completion** — via the context-safe recipe below (issue #6168: a single big blocking `TaskOutput` call is the wrong tool for this) — before advancing. Do not rely on any dispatch flag to enforce ordering. Concretely, this makes the skill's sequencing rules load-bearing on an explicit await, not on the harness:

- **Sequential Curator per issue** (step 2) — await each Curator before the next.
- **"Await all builders before Judge"** (step 4) — collect every builder's `TaskOutput` before any Judge dispatch.
- **Sequential per-PR Judge / Doctor within a wave** (steps 5–6) — await each PR's Judge (and its Doctor→Judge cycle) before the next PR's Judge.

**"Serialized" therefore means awaited-to-completion, not merely dispatched-with-a-sync-flag.** The #3707 rule above depends on this: serializing issue-creating agents is only safe if each is explicitly awaited to completion before the next is dispatched — a `run_in_background: false` that the harness ignores would silently overlap them.

**Sharper hazard in headless `claude -p` mode (issue #4257): ending your turn IS the kill signal.** Everything above frames the async-dispatch hazard as an *ordering* bug — the next phase starting too early. In headless `-p` mode there is a second, more severe consequence: **ending the orchestrator's turn terminates the `claude -p` process, and that process exit kills every still-running background child, full stop.** There is no "it'll finish in the background after I'm done talking" — once the orchestrator writes its final message and the turn ends, the process (and therefore every subagent it spawned) is gone. Concretely:

- **Never dispatch a role subagent (Curator / Builder / Judge / Doctor) with `run_in_background: true`** in a sweep. There is no safe way to "fire and forget" a role dispatch here — a headless sweep has no later turn in which to check on it.
- Because `run_in_background: false` is **not** honored as a synchronous-return guarantee either (see above), the only safe pattern in either case is: **write the orchestrator's final message only after every dispatched subagent's completion has been explicitly observed** — via the context-safe recipe below, one recipe for each. If you have not yet observed completion for a dispatched subagent, you MUST NOT end the turn.
- **The await itself must be context-safe, not a blind blocking `TaskOutput` (issue #6168).** A blocking `TaskOutput` on a still-running `local_agent` Task/Agent subagent is the wrong tool by the harness's own documentation, which warns against reading a background task's raw `.output` file — the full subagent conversation transcript (JSONL) — because it "will overflow your context window". That is exactly what has been observed live: a `TaskOutput(block=true, timeout=600000)` call on a still-running Builder subagent returned a multi-kilobyte raw JSONL transcript dump into the orchestrator's context on timeout, instead of a small status result. The context-safe recipe differs by session mode, and this skill cannot always tell interactive from headless apart (the `guard-background-subagents.sh` Stop hook itself now can, as of #6645 — it blocks only in headless mode and downgrades to a one-line advisory in an interactive session, so ending an interactive turn is no longer blocked):
  - **Interactive session** (a human driving Claude Code, or any invocation that is not `claude -p`): background agents keep running across turns even after the current turn ends, so just end the turn and let the harness's completion notification arrive on a later turn. Do not call a blocking `TaskOutput` here — it buys nothing over the notification and risks the JSONL-dump hazard above.
  - **Headless `claude -p`** (this skill's normal mode — no later turn exists, because ending the turn kills the process, see below): await in-turn with a **bounded, non-blocking poll loop** instead — call `TaskOutput` with `block: false` (or a short `timeout`), read only the result's `<status>` tag (`running` → sleep and poll again in the same turn; `completed`/`failed` → resolved), and never call `TaskOutput` with `block: true` and a long timeout. This is the same bounded in-turn poll-loop discipline already required below for backoff waits and long-running Bash operations.
- **Failure signature to match in forensics**: a sweep log whose final line is something like *"…in the background. I'll wait…"* immediately followed by process exit — the orchestrator believed a background task would keep running unsupervised, then ended its turn, killing it. This exact incident: `sweep-issue-4195.log`, PR #4243, where the backgrounded Judge was killed mid-review and left a stale `loom:reviewing` claim on the PR.
- **Never end a turn while a monitored background task — not just a subagent — is the only pending work** (issue #4366). This is the same kill signal, just triggered by a `Bash run_in_background` task, a `Monitor` wait, or any other "I'll check back on this later" narration instead of a role-subagent dispatch. A long-running operation (a cache/dependency download, a build, a CI wait) MUST be awaited **in-turn** via a bounded poll loop (repeatedly check status, sleeping between checks, inside the SAME turn) — never parked on a monitor and left for "a future turn" to pick back up, because in headless `claude -p` mode that future turn never arrives: the process has already exited.
- **Second failure signature to match in forensics** (issue #4366, observed 2026-07-28): a sweep log whose final line narrates something like *"Cache download is running in the background (monitored). I'll pick this back up once it completes or the fallback check fires."* immediately followed by a clean process exit (exit code 0) — indistinguishable from a legitimate self-skip by exit code alone, but with **zero lifecycle progress**: no checkpoint written, no PR opened, no phase advanced. The daemon reaper's no-progress backstop (`SweepExited.no_progress`) now catches and quarantines this shape after repeated occurrences, but the skill-level fix is to never produce it in the first place — poll in-turn instead of parking on the monitor.
- **A transport failure (529/Overloaded, connection reset, network error) is the SAME hazard, not an exception to it** (issue #4462, observed 2026-07-29). When a dispatched subagent (Curator / Builder / Judge / Doctor) dies to a transport error, the ONLY two safe responses in headless `-p` mode are: **(a) retry the dispatch inline, in the SAME turn** (re-invoke the subagent, optionally after a short in-turn `sleep`+poll if you want to space retries), or **(b) once inline retries are exhausted, exit NONZERO** so `claude-wrapper.sh` / the daemon retry machinery re-runs the sweep from its last checkpoint. **NEVER arm an end-of-turn backoff** — a `Monitor {command: "sleep 90 && …"}` / `ScheduleWakeup` wait followed by "I'll retry when the timer fires" narration and a turn end. In `-p` mode that "future turn" never arrives: the process exits at turn end, so the timer has no session to wake, and — because the exit code is **0** — the wrapper logs "completed successfully", the reaper sees a clean exit, and the issue is stranded in `loom:building` with no PR and no live sweep. **Backoff means a bounded in-turn sleep-and-retry loop, never an armed timer you end your turn on.** Third failure signature to match in forensics: a sweep log whose final lines are *"Backoff timer armed (90s). I'll retry the Builder dispatch when it fires."* immediately followed by a clean exit-0 — the exact #4462 incident (`sweep-issue-4426-1785358105`, two 529 kills then an armed `Monitor` backoff, ~35 min orphaned).
- **This prose guardrail is not the only line of defense.** A mechanical `Stop`-hook backstop (`defaults/hooks/guard-background-subagents.sh`, issue #4257, coverage extended to background Bash tasks by #4389 and to armed `Monitor`/`ScheduleWakeup` waits by #4462) blocks the turn from ending — once, per stop sequence — when it detects an unresolved dispatched Task subagent, an outstanding `run_in_background` Bash task, or an armed-but-unfired `Monitor`/`ScheduleWakeup` timer in the transcript. See `defaults/docs/guard-hooks.md`'s "Background Subagent Stop Guard" section for how it works and how to verify it is wired in a given repo.

### CRITICAL: One level deep — never spawn a nested orchestrator (`/loom:sweep`) as a subagent

`/loom:sweep` dispatches `loom-builder`, `loom-judge`, and `loom-doctor` subagents **directly from this orchestrator session** in a single tool-call block. This is **one level deep** and is empirically safe for `N` up to at least 3.

**Do NOT, under any circumstances, dispatch a nested orchestrator skill (`/loom:sweep`) as a subagent from `/loom:sweep`.** That would be two levels deep (parent Claude → `/loom:sweep` Task → builder/judge Task) and triggers the nested-dispatch stall hazard tracked in #3289 (stream-pump dies on parallel grandchildren). The wave loop in this skill is the architectural answer to that race — preserve it.

Concretely, when this skill says "dispatch builders for the wave", that means: in a single tool-call block, invoke `loom-builder` once per issue in the wave (e.g., three parallel `Task` calls if `N=3`). It does **not** mean invoke `/loom:sweep` three times.

If a future maintainer is tempted to "simplify" by replacing the wave-loop with parallel `/loom:sweep` calls: don't. Read #3289, then read this section again.

### Model selection for subagent dispatch (issue #3477, Phase 1)

Every role subagent dispatched by this skill (`loom-curator`, `loom-builder`, `loom-judge`, `loom-doctor`) gets its model resolved through a fixed precedence chain. Resolve once per role at dispatch time and pass the result via the Task tool's `model` parameter:

1. **Explicit dispatch param** — a model explicitly requested by the operator for this sweep (e.g., an operator instruction in the invoking prompt).
2. **Workspace override** — `.loom/config.json` → the `terminals[]` entry whose `roleConfig.roleFile` matches the role (e.g., `builder.md`) → its optional `roleConfig.model` field.
3. **Role default** — `.loom/roles/<role>.json` → `suggestedModel` (ships as an alias: `sonnet`, `opus`, or `haiku`).
4. **Session default** — if none of the above resolves (or resolves to an empty string), **omit the `model` parameter entirely** so the subagent inherits the parent session's model. Never pass `model: ""`.

**Logical-tier resolution before dispatch (issue #3982).** Every rung, tier, and arm in this skill names a *logical tier* by its CLI alias (`sonnet`, `opus`, `fable`) — but the bare `opus` alias still resolves to a **previous-generation** model on the wire (`claude-opus-4-8`), while `sonnet`/`fable` resolve to the current generation. So the shipped ladder `sonnet → sonnet@xhigh → opus → fable` would silently step *down* a generation at the `opus` rung. To fix this **without** scattering a pinned ID across the skill, resolve the chosen model through the single indirection point **immediately before** you pass it to the Task tool's `model` parameter (or to a spawned child's `--model`):

```bash
RESOLVED_MODEL="$(./.loom/scripts/resolve-model.sh "$LOGICAL_MODEL")"   # e.g. opus -> claude-opus-5
```

- Apply this to **every** resolved model on the dispatch path: the escalation-ladder rung, the No-Fable-Judge `opus` fallback, the `fable → opus` refusal fallback, and the experiment's Arm A (`assign-arm --resolve` does the same resolution for you — see "Model-cost experiment mode"). The Tier 2.5 complexity-tier resolution passes through `resolve-model.sh` **inside** `resolve-tier-model.sh`, so its output is already a concrete ID.
- `resolve-model.sh` maps only the stale logical tiers to concrete IDs (today `opus → claude-opus-5`); it **passes unknown aliases and pinned IDs (`claude-sonnet-4-6`) through unchanged**, and preserves the `@effort` suffix. So resolving is always safe — a tier-1/tier-2 operator pin, a `sonnet` rung, or a `sonnet@xhigh` rung all survive untouched.
- The mapping is configurable in `.loom/config.json` → `sweep.modelAliases` (an additive tier → ID object), so an operator can repoint a tier — or drop the pin once the CLI's own `opus` alias rolls to gen-5 — with no code change. Absent block ⇒ shipped default.
- The pinned ladder strings in this document (`sonnet → sonnet@xhigh → opus → fable`) stay written in **logical aliases** on purpose — the resolution happens at dispatch time, not in this prose.

**Pinned-ID degradation on Task-tool dispatch (issue #4282).** The resolution above yields a **concrete model ID** (`claude-opus-5`), but *which dispatch surface* carries it decides whether that ID is passable — exactly like the `@effort` degradation at "Effort passthrough vs. graceful degradation" below, and it composes with it:

- **Process-spawn / daemon path — the pinned ID IS passed through.** A spawned `/loom:sweep` child (`mcp__loom__dispatch_sweep`, or a direct `spawn-claude.sh --model <id>`) reaches the `claude` CLI, which accepts a pinned ID. #3982's guarantee holds unchanged here.
- **In-session Task tool — the pinned ID DEGRADES to its family alias.** This skill dispatches its per-role subagents (Curator/Builder/Judge/Doctor) through the **Task tool**, one level deep (see "CRITICAL: One level deep"), whose `model` parameter is an **alias-only enum** (`sonnet | opus | haiku | fable`) — a pinned ID like `claude-opus-5` is an invalid value there. So on this path, degrade the resolved ID back to its nearest Task-passable alias with `resolve-model.sh --task-alias` **immediately before** you pass it to the Task tool's `model` parameter, and if it changed, emit a **loud log line** noting the substitution and its generation cost:

  ```bash
  TASK_MODEL="$(./.loom/scripts/resolve-model.sh "$RESOLVED_MODEL" --task-alias)"   # claude-opus-5 -> opus
  ```

  e.g. `model resolution: pinned ID 'claude-opus-5' not passable on Task-tool dispatch — degraded to alias 'opus' (gen 5 → gen 4)`. `--task-alias` strips any `@effort` suffix too (the Task tool has no effort knob — same as the #3705 rule). **Exit 3** ⇒ no Task-passable alias (a non-Claude runtime ID, an unparseable value) ⇒ **omit the `model` parameter** so the subagent inherits the parent/agent-definition model; never dispatch a guessed alias, and never block a sweep on resolution.
- **Retirement (AC-style, like the `@effort` rule):** this degradation exists only because the CLI/harness `opus` alias still lags a generation. When it rolls to gen 5, drop the pin via `.loom/config.json` → `sweep.modelAliases: {"opus": "opus"}`: `resolve-model.sh` then returns the bare `opus` alias, `--task-alias` is the identity, and the degradation disappears with **no code or prose change**.

**Tier 2.5 — complexity marker (issues #3702, #4238, Builder dispatch only)**: between tier 2 and tier 3, at **Builder** dispatch, resolve the Builder's model from the Curator-emitted `<!-- loom:complexity=<tier> -->` marker (an HTML comment; values `mechanical` | `routine` | `complex`, absent ⇒ `routine`; see `curator.md`). The marker classifies the issue on one axis — *would a mistake be caught?* — into three cost-of-being-wrong strata, and the model for that stratum is resolved from `sweep.tierModels[<runtime>][<tier>]` (a runtime-neutral map of logical tiers), falling back to the active **`sweep.optimization` profile preset** (`cost` | `speed` | `balanced`, see below) when `tierModels` has no entry for that runtime/tier. **Resolve by command, not by judgement** — do not read a model out of the config yourself:

```bash
MODEL="$(./.loom/scripts/resolve-tier-model.sh <issue> <runtime>)"   # e.g. mechanical -> haiku, complex -> opus
```

- **Exit 0** ⇒ `$MODEL` is the resolved concrete ID (already passed through `resolve-model.sh`); pass it to the Task tool's `model` parameter (or export `LOOM_MODEL` / pass `--model "$MODEL"` to a spawned child). This **replaces** the tier-3 `suggestedModel` resolution for the Builder. On the Task-tool path this concrete ID degrades via `resolve-model.sh --task-alias` — see "Pinned-ID degradation on Task-tool dispatch" above.
- **Exit 3** ⇒ neither `sweep.tierModels` nor the optimization preset has an entry for the runtime/tier (the default — no such block ships in `defaults/config.json`, and the default `balanced` profile's preset is empty); **fall through to the tier-3 role default unchanged.** An unconfigured repo (or one with `sweep.optimization` unset/`"balanced"`) therefore dispatches **byte-for-byte identically to today**. Existing curated issues (which carry no marker) are unaffected.

**`sweep.optimization` — cost/speed policy switch (issue #4238 Phase B).** An operator-facing profile in `.loom/config.json` → `sweep.optimization`: `"cost"` | `"speed"` | `"balanced"` (default `"balanced"`), with env override `LOOM_SWEEP_OPTIMIZATION` (precedence **env > config > default**, the standard pattern used by `sweep.escalation` / `sweep.max_doctor_cycles`). It selects a **preset** over the `sweep.tierModels` map above rather than a fixed bump — see `resolve-tier-model.sh` / `resolve_optimization_profile` / `optimization_preset` in `loom-daemon/src/script_helpers/model_tiers.rs` for the implementation, and `defaults/docs/model-selection.md` for the full preset table. An explicit `sweep.tierModels[<runtime>][<tier>]` entry, if the operator has set one, still wins over the preset — the preset only fills tiers `tierModels` leaves unmapped. An invalid `sweep.optimization` value warns and falls back to `balanced`; it never fails dispatch.

Hard bounds, all enforced here (apply identically to both `sweep.tierModels` and the `sweep.optimization` preset — the profile is just an alternate source for the same tier-2.5 resolution, not a separate mechanism with separate rules):

> **Experiment-mode suppression (issue #3725).** When `sweep.modelExperiment` resolves to `experiment` (see "Model-cost experiment mode" below), the forced arm **overrides and SUPPRESSES this tier-2.5 resolution** for the Builder: the marker is still *read* (same grep) and used **only as the stratification key**, never as a model override (the experiment strata `complex` vs. the rest, so `mechanical` collapses with `routine` there). This is load-bearing — without it, a `complex`-marked issue on Arm B (sonnet-first) would silently jump models and confound the A/B. The tier map (and the `sweep.optimization` preset behind it) applies normally whenever the experiment is `off`/`observe`.

- **Never resolves to `fable`.** `resolve-tier-model.sh` refuses a tier map or optimization preset that names (or resolves to) `fable` and falls through instead. Fable is reached only via the escalation ladder (objective Judge-rejection evidence) or an explicit operator param, never on a Curator's speculation or an operator's cost/speed profile.
- **It is not a label** and creates no label — it lives only in the issue body.
- **Tier-1 and tier-2 pins still win.** The marker (and the optimization profile behind it) sits *strictly between* tiers 2 and 3: an explicit dispatch param (tier 1) or a `roleConfig.model` workspace pin (tier 2) overrides it, exactly as they override tier 3.
- The marker applies **only to the Builder path**. It never influences Curator, Judge, or Doctor resolution. (The fork's cost-of-being-wrong design additionally raises the Judge to match a `complex` Builder; that Judge-side change is deferred to a follow-up so this change stays byte-identical when the tier map is unconfigured.)
- Log the resolved model and reason per dispatch, e.g. `model: builder=haiku (complexity=mechanical)`.

**No-Fable-Judge hard invariant (issue #3702)**: **Judge model resolution can never resolve to `fable`, regardless of `sweep.escalation` contents or any marker.** The escalation ladder and the tier-2.5 marker apply only to the Curator-marker→Builder path and to the rejection-triggered Doctor — never to Judge. The Judge is the escalation sensor (see #3481); reviewing security-adjacent diffs is precisely Fable's refusal surface, and a refusing Judge would deadlock the control loop. If a resolved Judge model would ever be `fable` (alias or pinned ID), fall back to `opus` for the Judge dispatch and log the substitution. **Ordering matters on the Task-tool path**: apply this No-Fable fallback **first** (fall to `opus`), *then* run the resolved model through the Task-tool degradation (`resolve-model.sh --task-alias` — see "Pinned-ID degradation on Task-tool dispatch") — `task_alias_of` maps a fable-family ID to `fable` mechanically, so aliasing before the No-Fable fallback would defeat the invariant.

Rules:

- Aliases (`sonnet`/`opus`/`haiku`) and pinned IDs (`claude-sonnet-4-6`) are both valid at every tier. Shipped role JSONs use aliases; workspaces that need determinism pin exact IDs in `roleConfig.model`.
- A retry of the same role for the same issue (e.g., Builder re-dispatch after a mid-builder kill, or a second Judge pass after Doctor) **reuses the same resolved model**. Transport-level retries inside `claude-wrapper.sh` (token exhaustion, crashes, 5xx) likewise always keep the model — they are not quality signals and never trigger escalation.
- **Exception — Judge-rejection escalation (issue #3481, Phase 2)**: a Doctor dispatched *because of* a `loom:changes-requested` transition escalates one rung up the capability ladder. See "Model escalation on Judge rejection" below.
- Resolution failures are soft: if a role JSON is missing or unparseable, fall through to the next tier silently. Model selection must never block a sweep.
- The daemon path has its own equivalent: `mcp__loom__dispatch_sweep` accepts an optional `model` param which the daemon forwards to the spawned child as `claude --model <value>`. When delegating to the daemon (Stage -1 `use_daemon`), you MAY pass a resolved model; when omitted, the child inherits the spawning environment's default — the daemon emits no `--model` flag at all.

### Model escalation on Judge rejection (issue #3481, Phase 2)

When the Judge requests changes and this orchestrator dispatches a Doctor for the rejected PR — the Doctor phase at issue-side step 6 and at Mode C step C1b — the Doctor's model escalates one rung up a capability ladder instead of resolving through tiers 3/4 of the precedence chain.

**The ladder** lives in `.loom/config.json` under `sweep.escalation`:

```json
{
  "sweep": {
    "escalation": ["sonnet", "opus"]
  }
}
```

Three states:

| `sweep.escalation` value | Behavior |
|--------------------------|----------|
| Key absent | Default ladder `["sonnet", "opus"]` applies |
| `[]` or `false` | Escalation disabled — pure Phase 1 behavior; the rejection-triggered Doctor resolves through the unmodified precedence chain |
| Non-empty array | As configured; rungs accept aliases or pinned IDs, same as every other tier |

Rules:

1. **Trigger**: escalation fires **only** on a real Judge rejection — the `loom:changes-requested` transition that routes into the Doctor phase. First attempts of every role (Curator, Builder, the first Judge pass) always use the unmodified Phase 1 precedence chain. `ladder[0]` never overrides anything — it documents what attempt 1 is *expected* to run on, it is not applied.
2. **Precedence interaction**: the rejection-triggered Doctor resolves to `ladder[1]`, but only when its model would otherwise come from tier 3 (role `suggestedModel`) or tier 4 (session default). Tier 1 (explicit dispatch param) and tier 2 (`roleConfig.model` workspace pin) still win — pins are pins; operators who pinned want determinism.
3. **Composes with the cap, does not extend it**: escalation composes with the configurable Doctor→Judge cycle cap (`sweep.max_doctor_cycles`, default 1 — see "Doctor-cycle cap" below); it never raises the cap on its own. Consume the ladder generically as `ladder[min(attempt - 1, len - 1)]`: cycle 1 (attempt 2) resolves `ladder[1]`, cycle 2 (attempt 3) resolves `ladder[2]`, and so on. When the cap is at its default of 1, only `ladder[1]` is reached on the normal path (a configured third rung stays dormant); raising `max_doctor_cycles` above 1 — or granting the default-cap distinct-defect grace cycle — activates deeper rungs automatically, with no change here.
4. **Mode C inherits the rule** — C1b runs the identical Doctor phase under the identical cap, so the identical `ladder[1]` rule applies. No separate policy.
5. **Resume safety**: the escalation decision derives from the `loom:changes-requested` label/phase, **not** from a stored counter — so a sweep killed between Doctor dispatch and the follow-up Judge resumes correctly: re-entry routes back through the Doctor/Judge phases per the checkpoint skip rules, and any re-dispatched rejection-triggered Doctor escalates again. The optional `attempt` field on the sweep checkpoint (`sweep-checkpoint.sh write N doctor-done ... --attempt 2`) is forward-compat bookkeeping for a future cap raise; readers treat an absent field as attempt 1.
6. **The orchestrator decides, never the wrapper**: escalation is resolved here at Doctor-dispatch time. `claude-wrapper.sh` / `spawn-claude.sh` retries always keep their model (transport failures are not quality signals), and no wrapper change is involved.

### Effort-aware rung grammar, the `fable` rung, and refusal fallback (issue #3702)

This subsection extends the `sweep.escalation` ladder above with an optional richer rung grammar and a top `fable` rung. It is **fully additive and opt-in**: the shipped default ladder stays `["sonnet", "opus"]` with `max_doctor_cycles` `1`, and bare-alias configs parse and behave byte-for-byte as documented above. Nothing here changes default behaviour.

**Rung grammar — `model@effort`.** Each rung in `sweep.escalation` is either:

- a **bare alias** (or pinned ID) — `"opus"` resolves to `(model=opus, no effort override)`, exactly as today; or
- an **`alias@effort`** form — `"sonnet@xhigh"` resolves to `(model=sonnet, effort=xhigh)`. The part before `@` is the model (alias or pinned ID); the part after `@` is the effort level passed through to the dispatched role.

Escalating the cheaper dimension first (`sonnet → sonnet@xhigh → opus → fable`) retries at ~Sonnet cost before committing to Opus's higher output pricing. A rung with no `@` never carries an effort override, so existing arrays are unaffected.

**Effort passthrough vs. graceful degradation (issue #3705).** Whether an `alias@effort` rung's effort half actually reaches the dispatched role depends on **which dispatch surface** carries it — the two surfaces differ, and only one exposes a per-call effort knob:

- **`claude` CLI / process-spawn / daemon path — effort IS passed through.** The `claude` CLI exposes `--effort <level>`, and `spawn-claude.sh` threads it via a `LOOM_EFFORT` env → `--effort` passthrough (mirroring the `LOOM_MODEL` → `--model` plumbing; #3705). This is reachable whenever a whole `/loom:sweep` child is spawned as an OS process (`mcp__loom__dispatch_sweep` / a direct `spawn-claude.sh` invocation). It sets a **session-default** effort for that child, and `spawn-claude.sh` logs a structured `spawn-claude: effort=<level>` line for greppable per-run observability (model-parity, #3482). Note this is session-wide for the child, **not** per-rung.
- **In-session Task tool — effort DEGRADES to the bare model.** The sweep dispatches its per-role subagents (Builder/Judge/Doctor) through the **Task tool**, one level deep (see "CRITICAL: One level deep"), and the Task tool exposes **no** effort / reasoning-effort parameter alongside `model`. Because the escalation ladder's per-rung `@effort` is consumed at that per-role dispatch time, a resolved `alias@effort` rung on this path **resolves to the bare model** (the `@effort` suffix is dropped) and the orchestrator emits a **loud log line** noting the degradation, e.g. `escalation: effort plumbing unavailable on Task-tool dispatch — rung 'sonnet@xhigh' degraded to bare model 'sonnet'`. Never treat a malformed or empty effort (`sonnet@`, `sonnet@@x`) as an error — it falls back to bare-model dispatch with the same loud line; model resolution must never block a sweep.

The grammar ships either way so configs stay stable across environments: a `sonnet@xhigh` rung raises effort wherever the CLI/process path carries it, and degrades cleanly to bare `sonnet` on the Task-tool path — no config edit needed in either case, and if the Task tool later gains an effort parameter the same config activates the per-rung bump automatically.

**The `fable` rung.** `fable` (alias, or a pinned frontier-model ID where alias resolution is unavailable at a given tier — do not hard-code a specific ID into shipped defaults) is a valid **top** rung. Because the ladder is consumed as `ladder[min(attempt - 1, len - 1)]` under the Doctor-cycle cap, a `fable` rung placed at index ≥ 3 is only ever reached when `max_doctor_cycles ≥ 3` — i.e. it is **opt-in** and never appears on the shipped default ladder.

**Recommended opt-in deep-ladder recipe** (`.loom/config.json`) — pairs a 4-rung ladder with the cap raise required to reach its deeper rungs (see "Doctor-cycle cap" below):

```json
{
  "sweep": {
    "escalation": ["sonnet", "sonnet@xhigh", "opus", "fable"],
    "max_doctor_cycles": 3
  }
}
```

**Refusal-aware fallback for the `fable` rung.** Fable-class safety classifiers refuse some legitimate security-adjacent work (guard hooks, OAuth token handling, credential scanning) with `stop_reason: "refusal"` — which `classify_error` (`.loom/scripts/lib/classify-error.sh`) reports as `MODEL_REFUSAL`. On a `MODEL_REFUSAL` at a `fable` rung, the orchestrator **re-dispatches the same attempt one rung down** (`fable → opus`) **without consuming a Doctor cycle**. A refusal is a *routing error*, not a quality signal, so it must not eat the escalation / `max_doctor_cycles` budget: the `attempt` counter is unchanged, and the retried Doctor is still the same cycle `k`. This is distinct from a Judge rejection (which advances the attempt and escalates *up*). Only the `fable` rung has a rung below it to fall to; a `MODEL_REFUSAL` at a non-`fable` rung is handled by the normal error path.

**No-Fable-Judge invariant (restated).** Judge dispatch never resolves to `fable`, regardless of ladder contents — see the invariant under "Model selection for subagent dispatch". The ladder here governs only the rejection-triggered Doctor.

### Credit-exhaustion fallback — one rung down, any rung (issue #5687)

**The signature.** `You're out of usage credits` — which `classify_error` (`.loom/scripts/lib/classify-error.sh`) reports as **`MODEL_CREDITS_EXHAUSTED`**, a category of its own. Credits are scoped to a **model tier**, so this is *not* the account dying: the same account, on a cheaper model, is still fully usable. Observed 2026-08-08 on a wave-width-6 `/loom:sweep all` run — all six wave builders were dispatched on the session-default model and all six died within minutes of each other when that tier's credits ran out, with nothing in this document telling the orchestrator what to do about it.

**Why this is not `TOKEN_EXPIRED` / `TOKEN_EXHAUSTED`.** Those name the *account credential* dying (weekly/session/plan limit, expired OAuth token); the remedy is rotating to a different token in the pool, and on those signatures it is the only remedy. Credit exhaustion is one axis narrower, and the difference is decisive **on the in-session path specifically**: subagents dispatched through the Task tool share the orchestrator's own credential and have **no token pool to rotate through** — but their `model` parameter is chosen fresh at every dispatch. The remedy that does not exist here (rotate the account) and the one that does (drop a model rung) are exactly inverted relative to the wrapper/daemon path, which is why the two classes must not be conflated. (A forge GraphQL rate limit is a *third* axis — neither of these; see "Mid-phase-death recovery".)

**The response — same attempt, one rung down, no Doctor cycle consumed.** Structurally identical to the `MODEL_REFUSAL` handling above, keyed on a different signature and **not restricted to the `fable` rung**:

1. Resolve the cheaper rung **by command, not by judgement**:

   ```bash
   NEXT_MODEL="$(./.loom/scripts/resolve-model.sh "$CURRENT_MODEL" --downgrade)"   # opus -> sonnet
   ```

   `--downgrade` steps one rung down the Task-tool cost ladder `fable → opus → sonnet → haiku`, accepts a bare alias, a pinned ID, or an `@effort` form, and always emits a **Task-passable alias** — so it subsumes the `--task-alias` degradation and no second pass is needed. Its `fable → opus` hop is deliberately the same hop the refusal fallback above hard-codes. **Exit 3** ⇒ no cheaper rung; see terminal behavior below.
2. **Re-dispatch the same phase for the same issue at `$NEXT_MODEL`** — same attempt, same worktree, same claim, same wave. For a Builder this is exactly the "resume from builder start" path the Builder scope limit already prescribes (`worktree.sh` is idempotent, and the resumed builder decides for itself whether to commit, amend, or discard the partial diff its predecessor left).
3. **Do not consume a Doctor cycle and do not advance the `attempt` counter.** Credit exhaustion is an *availability* fault, not a quality signal: it must not eat the `max_doctor_cycles` budget, and it must never trigger the *upward* escalation ladder, which is reserved for real Judge rejections.
4. **Log the substitution loudly**, matching the refusal-fallback / Task-degradation log conventions:
   `credit exhaustion: issue #N builder killed (MODEL_CREDITS_EXHAUSTED) — re-dispatching same attempt at 'sonnet' (was 'opus'); no Doctor cycle consumed`

**Recover the whole wave, not one subagent.** Credit exhaustion is **correlated across a wave by construction** — every builder in a wave shares one account and, absent per-issue tier-2.5 overrides, one model. N simultaneous deaths is the *expected* observation, not a coincidence. So:

- Re-dispatch **every** affected member of the wave at its downgraded model **in a single tool-call block**, exactly as the wave was originally dispatched. Do not serialize the recovery — that turns one wave-width-6 outage into six sequential retries.
- Downgrade **per subagent, from that subagent's own resolved model**. A tier-2.5 `complex` builder on `opus` and a `mechanical` builder on `haiku` are not on the same rung; never pick one replacement model for the whole wave.
- Never re-dispatch a phase that already wrote a checkpoint, and never re-dispatch a member that exited cleanly.

**Terminal behavior — when there is no rung below.** If `resolve-model.sh --downgrade` exits 3 (the phase was already at `haiku`, or the model is not Claude-family), **stop downgrading**: there is no cheaper tier to fall to, and retrying the same tier dies the same way. Fall through to the ordinary "Mid-phase-death recovery" procedure below — re-verify forge state, complete only the missing steps — and if the phase still cannot be completed, record the issue as `rate-limited (unresumable: <phase> MODEL_CREDITS_EXHAUSTED mid-phase, no cheaper model rung available)` and advance. **One rung per kill, never a blind loop**: if the re-dispatch at the cheaper rung dies with the same signature, downgrade once more *from that rung*. The ladder is finite and terminates at `haiku`, but you must never re-dispatch the same phase at the same model on this signature.

**What this does not change.**

- **The No-Fable-Judge invariant holds trivially** — the ladder only descends, so a downgrade can never land on `fable`.
- **The daemon / process-spawn path is unaffected.** It has no per-dispatch model knob in flight, so `claude-wrapper.sh` treats `MODEL_CREDITS_EXHAUSTED` byte-identically to `TOKEN_EXHAUSTED` (rotate the account, mark it exhausted, same cooldown), and `tokens_pool::health` records the same `PlanExhausted` state. The distinct category buys a *name* for forensics and for this orchestrator's remedy choice, not a different pool policy — the pool has no per-model account state to support one.

### Spend-limit fallback — session-default first, any rung (issue #6518)

**The signature.** `"You've hit your monthly spend limit"` / `"hit your ... spend limit"` — the exact wording observed 2026-08-19 killing an in-session `model: opus` Builder mid-turn ("Agent terminated early due to an API error: You've hit your monthly spend limit · raise it at claude.ai/settings/usage"). `classify_error` (`.loom/scripts/lib/classify-error.sh`) already reports this as **`TOKEN_EXHAUSTED`** (added by issue #5631) — that classification is correct and unchanged for the **subprocess / daemon path**: `claude-wrapper.sh`'s `is_account_exhaustion` already rotates to a different pooled account on it, same as any other `TOKEN_EXHAUSTED`. Nothing in this section touches that path.

The gap is the same one #5687 (`MODEL_CREDITS_EXHAUSTED`) closed for credit exhaustion: **on the in-session Task-tool dispatch path there is no subprocess, so `classify_error` never runs at all.** The orchestrator must pattern-match the raw Task-tool failure text itself, exactly as it already does for the credit-exhaustion signature. This document names that signature `SPEND_LIMIT` purely as **this orchestrator's own reason-prefix label** for the phrase — it is not, and does not need to be, a `classify_error()` return value; the actual classifier still reports `TOKEN_EXHAUSTED` for this text everywhere it runs.

**Why the remedy differs from the credit-exhaustion fallback above.** `MODEL_CREDITS_EXHAUSTED` is *definitionally* scoped to one model tier — "You're out of usage credits" always means the same account is fine on a cheaper model, so walking `resolve-model.sh --downgrade` is always a safe first move. A monthly **spend** limit is not definitionally scoped that way: it reads like a dollar cap on the whole account, which a cheaper model would also eventually hit, not a per-tier ceiling. The 2026-08-19 incident is consistent with *either* reading (the killed dispatch carried an explicit `model: opus` override; a bare re-dispatch with no `model` param — inheriting the session default — succeeded), so **do not assume the per-tier reading and jump straight to a cost-ladder walk.** Apply the remedy the incident actually validated, and let the account-wide case reveal itself instead of being guessed at:

1. **If the killed dispatch carried an explicit `model` param** (a role `suggestedModel`, a `sweep.escalation` rung, or any other override above session default): **re-dispatch the exact same attempt with the `model` param OMITTED entirely** — not a rung-down alias, the parameter itself absent, so the subagent inherits the parent session's own model. This is the literal remedy the incident observed working, and it makes no assumption about whether the limit is per-tier or account-wide.
2. **If that session-default re-dispatch also dies with the same `"spend limit"` signature — or the killed dispatch had no explicit `model` param to omit in the first place (it was already running at session default)** — this is decisive evidence of the **account-wide** reading: no substitution of models help here, because there is no `model` left to change out from under it. **Stop substituting models.** Fall straight through to the ordinary "Mid-phase-death recovery" procedure below (re-verify forge state, complete only the missing steps), and if the phase still cannot be completed, record the issue as `rate-limited (unresumable: <phase> SPEND_LIMIT mid-phase, no session-default fallback available)` and advance.
3. **Same attempt, no Doctor cycle consumed, at any rung** — identical to the credit-exhaustion and refusal fallbacks: the `attempt` counter is unchanged, and this never triggers the *upward* escalation ladder (reserved for real Judge rejections). Applies whether the killed dispatch was a first-attempt role resolution or a `sweep.escalation`-ladder rung — a rejection-triggered Doctor escalated onto a spend-limited rung gets the same omit-the-model-param treatment before its cycle is considered consumed.
4. **Log the substitution loudly**, matching the existing conventions: `spend limit: issue #N builder killed (SPEND_LIMIT) — re-dispatching same attempt with model param omitted (was 'opus'); no Doctor cycle consumed`.

**Recover the whole wave, not one subagent.** Like credit exhaustion, a spend-limit kill can plausibly correlate across a wave (shared account); apply the same "re-dispatch every affected member in one tool-call block, never serialize" discipline described under "Credit-exhaustion fallback" above.

**What this does not change.**

- **The No-Fable-Judge invariant holds trivially** — omitting a `model` param can never resolve to `fable`.
- **The daemon / process-spawn path keeps its own remedy**, same as `MODEL_CREDITS_EXHAUSTED` — `claude-wrapper.sh` already treats this signature as `TOKEN_EXHAUSTED` (rotate the account, mark it exhausted) via the existing #5631 pattern. One daemon-side **attribution** bug did have to be fixed alongside this section: the reaper's own crash-signature table (`loom-daemon/src/sweep_registry/crash_signals.rs::exhaustion_signatures`) still carried the pre-#5631 fixed alternation (`hit your (limit|session limit|weekly limit)`), so a spend-capped detached child matched **no** signature there and the reaper charged the **issue's** insta-crash quarantine tally instead of marking the account — the same backwards attribution #4501 fixed for the per-model ceiling. That table is now back in lockstep with the shell classifier's bounded-filler shape. The account pool's *treatment* is unchanged either way (`rate-limited`, transient exhaustion).
- **No new `classify-error.sh` category.** Unlike `MODEL_REFUSAL`/`MODEL_CREDITS_EXHAUSTED`, this section does not need one: the subprocess path already classifies the phrase correctly as `TOKEN_EXHAUSTED`, and the in-session orchestrator keys off the raw phrase directly (same mechanism `MODEL_CREDITS_EXHAUSTED`'s own text match uses upstream of any classifier call). A distinct daemon-side category remains a legitimate, separate follow-up if crash-forensics telemetry ever wants to distinguish a spend cap from other `TOKEN_EXHAUSTED` causes — out of scope here.

### Why no pre-resolved chain (issue #5697)

Issue #5697 asked, before any code was written, whether the reactive one-rung-down remedy above should instead pre-resolve a *whole* fallback chain at dispatch time. **Closed as not worth building** — no `sweep.fallbackChain` config key, no dispatch-time chain data structure — for three reasons, each answering one of the issue's own questions:

1. **A "chain" is not an independent fact to configure — it is fully derivable.** `fable → opus → sonnet → haiku` is exactly what repeatedly applying `model_tiers::downgrade_task_alias` (`resolve-model.sh --downgrade`) produces, in order, every time. There is no second source of truth a `sweep.fallbackChain` key could usefully override without drifting out of sync with the ladder `downgrade_task_alias` already hard-codes — a config surface that only exists to duplicate a pure function is a liability, not a feature.
2. **The Task tool dispatch takes exactly one `model` param per call, and the orchestrator must re-dispatch on every kill regardless.** Pre-resolving rungs 2, 3, and 4 up front buys nothing when only rung 2 can ever be used on the *next* dispatch — the orchestrator has no way to hand the subagent a fallback list it could consult on its own mid-turn. Whether the rung was resolved a step early (at wave-dispatch time) or reactively (at kill time, which is what happens today) it is looked up exactly once per re-dispatch either way.
3. **`downgrade_task_alias` is already an O(1) deterministic table lookup**, not an expensive computation — there is no recomputation being saved by resolving it ahead of the kill instead of at the moment it is needed.
4. **The daemon/process-spawn path does not want this at all.** It has no per-dispatch model knob to pre-load a chain into (see "What this does not change" above) — it responds to `MODEL_CREDITS_EXHAUSTED` by rotating pool accounts (`tokens_pool::health`), never by downgrading models, so a pre-resolved chain would have no consumer on that path.

**Item 2 (daemon-path telemetry) — implemented, not closed.** Unlike the chain question above, tagging credit exhaustion distinctly in the daemon's per-sweep outcome telemetry *was* worth building: the gap was real (a `MODEL_CREDITS_EXHAUSTED` daemon-dispatched death was previously indistinguishable from a plan/quota `TOKEN_EXHAUSTED` death in the durable outcomes journal) and the fix is a pure reporting addition. The reaper's crash classifier (`sweep_registry::classify_account_exhaustion` / `classify_crash`) now recognizes the `MODEL_CREDITS_EXHAUSTED` prose ("You're out of usage credits") as its own `model-credits-exhausted` signature, distinct from `rate-limited` / `rate-limit-abort` / `model-limit`, and that classification is persisted on the durable `#4644` outcomes journal's `crash_classification` field (`account-exhausted:model-credits-exhausted`). The account pool's health policy (`tokens_pool::health`'s fused `TokenExhausted | ModelCreditsExhausted` match arm) is unchanged — this is a reporting-only distinction, not a new pool policy.

### Doctor-cycle cap (`sweep.max_doctor_cycles`, issue #3668)

The Doctor→Judge cycle cap bounds how many times a single PR can bounce between Judge and Doctor before it is blocked for human attention. It exists to stop Judge/Doctor disagreement loops and bound worst-case latency. The cap is configurable in `.loom/config.json` under `sweep.max_doctor_cycles`, read once at lifecycle-entry time the same way `sweep.escalation` is:

```json
{
  "sweep": {
    "max_doctor_cycles": 1
  }
}
```

Three states:

| `sweep.max_doctor_cycles` value | Behavior |
|---------------------------------|----------|
| Key absent | Default cap of **1** applies — one Doctor→Judge cycle per PR (the historical behavior) |
| Invalid (non-integer, or `< 1`) | Falls back to the default cap of **1** and logs a warning; a malformed config never blocks a sweep |
| Valid integer `>= 1` | Up to that many Doctor→Judge cycles per PR before the PR is blocked |

**Counting.** A "cycle" is one Doctor pass plus the re-Judge that evaluates it. The cap reuses the existing `attempt` checkpoint field: attempt 1 is the Builder's PR (or the PR as it enters Mode C); the Doctor dispatched after the first Judge rejection is attempt 2 (cycle 1), the Doctor after the second rejection is attempt 3 (cycle 2), and so on. Doctor cycle `k` is permitted while `k <= max_doctor_cycles` (equivalently `attempt <= max_doctor_cycles + 1`). When the cap is reached and Judge still requests changes, block the PR — add `loom:blocked`, leaving the Judge's `loom:changes-requested` in place (that label pair is what Champion's recovery pass below keys on) — log `PR #P blocked: doctor cycle exhausted after <k> Doctor→Judge round(s); human attention required`, and advance to the next candidate. The `attempt` value written on each Doctor cycle is `k + 1`; the checkpoint schema already accepts any positive integer, so no plumbing change is needed to reach attempt 3+.

**Escalation composes.** Because the ladder is consumed as `ladder[min(attempt - 1, len - 1)]`, raising the cap activates deeper rungs automatically (see "Model escalation on Judge rejection" point 3). The cap and the ladder are independent knobs.

**Distinct-defect exception (default cap only).** When `max_doctor_cycles` is at its **default of 1** and the *second* Judge rejection names a defect that is demonstrably **distinct** from the first rejection's defect — forward progress (the first fix worked and uncovered a genuinely new problem), not thrash (the same disagreement re-litigated) — the orchestrator MAY grant **exactly one** additional bounded Doctor→Judge cycle before blocking. This is a judgment call made by comparing the two Judge rejection comments:

- **Distinct defect** (e.g. rejection 1 = "duplicate ampacity rules"; rejection 2 = "root-only test-permission flaw uncovered after the dedup fix") → grant one grace cycle, and **emit a required log line** naming the distinction, matching the block-log convention so the grant is auditable:
  `PR #P: granted one extra Doctor cycle — second rejection is a distinct defect (<short reason>)`.
- **Same defect re-rejected, or ambiguous** → **block immediately** per the cap. The anti-thrash guarantee is unchanged for the thrash case.

Constraints that keep the exception from becoming an unbounded loop:

- It is **single-use per PR** — one grace cycle only. A *third* rejection after the grace cycle always blocks, even if it too looks distinct.
- It applies **only at the default cap** (`max_doctor_cycles == 1`). When an operator has already raised the cap above 1, the exception does **not** compose on top — the configured cap is the entire budget. (Layering a per-rejection grace cycle onto an operator-raised cap would reintroduce the indefinite-thrash risk the cap exists to prevent.)
- The distinction MUST be stated in the log line. An unlogged grace cycle is a bug.

**Champion-side counterpart (issue #4574).** Once a PR is blocked, Champion's Capped-PR Recovery Pass (`champion-pr-merge.md` → "Capped-PR Recovery Pass") reconsiders it — open PRs carrying `loom:blocked` + `loom:changes-requested` — and may grant a further bounded Doctor→Judge cycle by removing `loom:blocked`. It applies **this same forward-progress test**, just at a different decision point: periodically, post-mortem, with the PR's complete rejection history instead of the dying sweep's local context. The two do not compose into a double-grant (a PR reaches `loom:blocked` only after the in-sweep exception was consumed or was not applicable) and **neither imposes a numeric cap on the other** — the in-sweep exception stays single-use per PR, and Champion's repeat grants are bounded by re-applying the forward-progress test each round, not by a shared counter.

### Model-cost experiment mode (`sweep.modelExperiment` / `LOOM_MODEL_EXPERIMENT`, issue #3725)

> **Fallback note (issue #4809).** For a **daemon-dispatched** sweep (the
> normal `dispatch_sweep` / work-finder / epic-supervisor path), arm
> assignment and the forced Builder model are now resolved **natively in the
> daemon at dispatch time** (`sweep_registry::resolve_autonomous_dispatch_model`)
> — the daemon computes the SAME deterministic arm this section describes and
> passes the resolved model as the dispatch `--model`, so it wins the #4501
> default-pin precedence instead of being silently overridden by it. Per-sweep
> outcome telemetry (`sweep.outcome` records) is likewise attributed an `arm`
> automatically from the dispatched model, with no action from this skill.
> This is the fix for two compounding defects discovered in the 2026-07-31
> canary rollout: (1) the deterministic per-phase instructions below are
> **LLM-authored prose** and were observed to never execute in a headless
> `-p` child — no banner, no `assign-arm`/`record` calls, zero rows in
> `.loom/stats/sweep-model-stats.jsonl` across a full day of canary sweeps;
> and (2) even when they *would* execute, the #4501 dispatch model pin is
> tier-1 and structurally outranks a model only ever "forced" in prose. The
> instructions below remain a **best-effort fallback** for non-daemon
> contexts (a manual `/loom:sweep` run in an interactive session, or a
> Task-tool in-session dispatch that never passes through `dispatch_sweep`) —
> keep following them there — but they are no longer the primary data path
> for a daemon-dispatched canary.

This mode instruments a sweep to produce the balanced A/B evidence #3718 needs to decide the Builder `opus → sonnet` retune. **It is off by default and is byte-for-byte a no-op when unset** — every deterministic instruction below runs only when the mode resolves to `observe` or `experiment`. All the arithmetic (mode resolution, arm assignment, the durable append, the harvest) lives in `./.loom/scripts/sweep-experiment.sh` (a thin stub over `loom-daemon sweep-experiment`); this skill never computes a modulo by hand.

**Tri-state resolution (read once at lifecycle entry, same point as `sweep.escalation`).** Resolve `./.loom/scripts/sweep-experiment.sh resolve-mode` → one of `off` | `observe` | `experiment`. Precedence follows the **string-valued** guard pattern (`guards.rmScope` / `guards.forceScope`), not the boolean one:

- highest: `LOOM_MODEL_EXPERIMENT` env (`off`/`observe`/`experiment`) → then `.loom/config.json` → `sweep.modelExperiment` → default `off`.
- Unknown/malformed value → treated as `off` with a stderr warning; a bad value **never** aborts the sweep.

The three states:

| Mode | Behavior |
|------|----------|
| `off` | No instrumentation. Zero behavior change. No `.loom/stats/` file is created. |
| `observe` | Passive measurement. No model forcing, no arm. One JSONL record appended per phase (`arm` null). Safe to run anywhere. |
| `experiment` | Active A/B. Builder is forced to the assigned arm's model; records are tagged with the `arm`. **Canary-only** (see Guardrails). |

**Two arms map onto #3718's inequality.** `resolve-mode` in `experiment` picks a per-issue arm via `./.loom/scripts/sweep-experiment.sh assign-arm --issue N --complexity <routine|complex>` → prints `<arm> <model>`:

- **Arm A = opus-first** — Builder forced to `opus`; the normal escalation ladder still applies on Judge rejection. Resolve the printed `<model>` through `./.loom/scripts/resolve-model.sh` (or pass `--resolve` to `assign-arm`, which prints the already-resolved ID) before dispatch, so Arm A reaches **Opus 5** (`claude-opus-5`) on the wire rather than the stale gen-4 `opus` alias (issue #3982). Arm B's `sonnet` is unaffected (it passes through unchanged). **On in-session Task-tool dispatch** the pinned `claude-opus-5` is not passable and degrades to the `opus` alias via `resolve-model.sh --task-alias` — see "Pinned-ID degradation on Task-tool dispatch" (issue #4282); only the process-spawn/daemon path reaches Opus 5 on the wire.
- **Arm B = sonnet-first + escalate** — Builder forced to `sonnet`; on Judge rejection the Doctor escalates via the existing `sweep.escalation` ladder (#3481), exactly as documented in "Model escalation on Judge rejection". Arm B *is* the candidate policy #3718 is evaluating.

**Deterministic, resume-safe, stratified assignment.** The arm is a pure function of the issue number and the #3702 complexity stratum, so a killed-and-resumed sweep re-running the same issue **lands on the same arm**. The complexity marker is read once (the same grep at the tier-2.5 site) and serves two purposes: the **stratification key** (so both arms see a comparable difficulty mix) and — **only when the experiment is off/observe** — the tier-2.5 tier-map resolution. In `experiment` mode that resolution is suppressed (see the "Experiment-mode suppression" note under tier 2.5).

**Forced-arm precedence.** The forced arm slots into the Builder model-resolution chain **above tier 2.5 / tier 3** but **below tier 1 / tier 2 operator pins**: an explicit dispatch param (tier 1) or a `roleConfig.model` workspace pin (tier 2) still wins — a pinned canary is intentionally opted out of the experiment. The forced arm only ever replaces what tier 2.5 / tier 3 would have resolved for the Builder.

**Durable stats store.** Instrumentation appends one JSONL record per role phase invocation to `.loom/stats/sweep-model-stats.jsonl` (gitignored; survives the merge that deletes the transient checkpoint). Immediately after each phase's `sweep-checkpoint.sh write`, also run:

```bash
./.loom/scripts/sweep-experiment.sh record --mode <mode> --issue N --phase <curator|builder|judge|doctor|merge> \
  --role <role> --model <resolved-model> --arm <A|B|"" > --attempt <k> --complexity <routine|complex> \
  --verdict <pass|changes|""> --agent-id <agent-id> --stats-file .loom/stats/sweep-model-stats.jsonl
```

Each record carries the **HARD deterministic outcome-chain** (`arm`, `model`, `attempt`, `judge_verdict`, `cycle_count`, `complexity`) — which alone answers #3718's inequality (first-attempt Judge-pass rate + mean Doctor cycles × model price) — **plus the `agent-id` join key** for the role invocation (available in the Task-result metadata at dispatch/return time), which the harvest joins against #3726's transcript index to attribute exact cost.

**Token fidelity.** Live per-phase token capture is **not** available at the Task-result boundary; the exact input/output + cache split is recovered at **harvest** time by parsing each role subagent's `agent-<id>.jsonl` `usage` blocks (see below). Each record stamps a `token_fidelity` tag naming the source (`none` | `sweep-aggregate-log` | `transcript`). The deterministic outcome-chain is the load-bearing signal; exact cost just makes it precise.

**Guardrails (load-bearing).** `off` by default; `observe` is safe anywhere. `experiment` is **canary-only**: `resolve-mode` refuses to honor it on a non-canary target and **loudly downgrades to `observe`** unless the operator confirms a canary via an **uncommitted** signal — the `LOOM_MODEL_EXPERIMENT_CANARY=1` env var or the gitignored `.loom/CANARY` sentinel file. The committed `sweep.modelExperimentCanary` config flag is **no longer** an accepted confirmation (#3731): it would propagate with a copied config and fire experiment on production. A git-tracked `.loom/CANARY` is refused for the same reason. The `sweep.modelExperiment` *mode* may still live in committed config — it stays inert without the uncommitted confirmation. At lifecycle entry, print the loud banner naming the active mode, the canary confirmation source, and — in `experiment` — the arm assigned to the issue:

```bash
./.loom/scripts/sweep-experiment.sh banner --issue N --complexity <routine|complex>
```

**Harvest (exact per-role cost).** After a canary run, aggregate the store into the per-arm inequality inputs #3718 consumes — first-attempt Judge-pass rate, mean Doctor cycles, exact cache-aware cost per arm, and the merge-rate quality floor — via the reader alongside `agent-metrics.sh`:

```bash
./.loom/scripts/agent-metrics.sh --model-experiment --archive-dir "$LOOM_TRANSCRIPT_ARCHIVE"
# equivalently: ./.loom/scripts/sweep-experiment.sh harvest --archive-dir "$LOOM_TRANSCRIPT_ARCHIVE"
```

The harvest parses each joined `agent-<id>.jsonl` transcript's `usage` blocks (input/output + `cache_read_input_tokens`/`cache_creation_input_tokens`) and prices them with the same **cache-aware** per-model table as `loom-daemon`'s `resource_usage.rs`. Transcripts are located through #3726's `loom.transcript-index/v1` archive index (`--archive-dir` = `LOOM_TRANSCRIPT_ARCHIVE`); harvest should run periodically (cron) over a multi-day canary so usage is extracted into the compact stats store before `~/.claude/projects` is pruned.

> **Daemon detached-child path (honest finding, verified against on-disk transcripts).** The role-subagent transcripts of a daemon-dispatched `claude -p "/loom:sweep N"` child land under that child's own `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/<cwd-slug>/<child-session-uuid>/subagents/agent-<id>.jsonl` tree — the **durable** location, not the ephemeral `/tmp/.../tasks/` scratch — and each carries the full per-message `usage` (input/output + cache split) and `model`. Confirmed present on disk for real detached-child sessions. So they are archivable/harvestable via the same #3726 periodic sync. What the daemon reaper does **not** yet know is the child's session-uuid, so it cannot trigger a precise single-session archive on exit — the cron periodic sync is the backstop, exactly as for the completion hook (see "Session Transcript Archival").

### Other constraints

- **Do NOT write to `.loom/daemon-state.json`.** That file is owned by the standalone daemon. `/loom:sweep` runs independently and must not race with the daemon on shepherd-slot bookkeeping. Reading `daemon-state.json` for situational awareness is fine; writing is not.

### Cached forge reads (`gh-cached`, #4667)

Every concurrent sweep, Judge, and Champion on this host shares **one**
personal `gh` rate-limit budget (#4665), and they re-issue the same candidate
listings and candidate surveys independently. Route those repeated
**observation** reads through the short-TTL cache wrapper; keep every
**arbitration** and **merge-gating** read on plain `gh`.

Resolve the wrapper **once**, at sweep start (alongside Step 0a's run id):

```bash
# Degrades to plain `gh` when the wrapper is absent or its Python runtime is
# broken — the same probe merge-pr.sh uses. Nothing in this document depends on
# the cache existing; it is a budget optimization, never a correctness mechanism.
GH_READ="gh"
_ghc="$(git rev-parse --show-toplevel 2>/dev/null)/.loom/scripts/gh-cached"
if [[ -x "$_ghc" ]] && "$_ghc" --version >/dev/null 2>&1; then GH_READ="$_ghc"; fi
```

**Route through `$GH_READ` (cached, 30s TTL):**

| Call site | Where |
|---|---|
| Mode B / Mode C candidate resolution (`gh issue list` / `gh pr list` translations) | Validation rules, Examples |
| The `all` sentinel's whole-backlog `gh issue list --state open --limit 100` | Validation rules |
| The one-per-invocation `gh label list --limit 200` token validation | Validation rules |
| `--dry-run` Stage 0 per-candidate surveys (`gh issue view` / `gh pr view`) | Dry-run gate, Procedures |

**Keep on plain `gh` (deliberately uncached — do NOT wrap these):**

| Call site | Why it must be live |
|---|---|
| Per-issue pre-flight step 1 (`gh issue view N --json state,labels,closedByPullRequestsReferences`), the timeline existing-PR probe, and the follow-up `gh pr view` routing read | **Claim arbitration.** A 30s-stale `loom:building` / open-PR view is exactly the window a competing Builder's claim lands in — the failure mode is a duplicate builder on a claimed issue |
| Mode C's C0 per-PR pre-flight (`--json number,state,labels,closingIssuesReferences`) | Same: routes a PR to Judge/Doctor/Merge; must see another session's just-written verdict |
| Step 5's checkpoint-divergence recheck (`gh pr view <PR> --json labels`) | Its entire purpose is detecting that a concurrent process moved the PR on |
| Step 7's overlap probe (`gh pr view X --json files`) and `mergeStateStatus` recheck | **Merge gating** — the last read before an irreversible merge. A failed probe call must be observed as a failure (exit status), not silently coerced into an empty-and-therefore-disjoint result — see step 7's `git diff` local fallback (#6390) |
| The `--dry-run` "Verifying nothing mutates" before/after reads | **Differential check** — the identical command runs twice around the operation under test; a cache hit would return the "before" value and make the check vacuously pass |

**Writes stay literal `gh`.** Never wrap `gh issue edit` / `gh pr comment` in
`"$GH_READ"` — the destructive-command guard hooks pattern-match the literal
command text and a wrapped form slips past them. After a mutation this sweep
made, drop the cache instead: `"$GH_READ" --clear-cache` (a local `/tmp`
sweep, zero API cost) so a later cached read cannot return pre-write state.

Full policy, TTL/invalidation semantics, and manual verification steps:
`.loom/docs/gh-cached.md` (source: `defaults/docs/gh-cached.md`).

## Sweep Run Identity + Peer-`/loom:sweep` Detection (#3768)

Before **any** other stage — including Backend detection (Stage -1), the dry-run gate, and all wave lifecycles — establish a **stable identity for this sweep invocation** and probe for a concurrently-running peer `/loom:sweep`. This runs for **all modes (A, B, and C)** — it is *not* short-circuited by Mode C or `--no-daemon` (those only affect the Stage -1 backend probes below).

This section exists because `/loom:sweep` was originally hardened (#3373 checkpoints, #3648 baseline) assuming a single sweep instance per repo. Two concurrent `/loom:sweep` runs in the same repo (observed live 2026-07-22) collided on shared run-state: they shared the single fixed main-clean baseline path (one clobbered the other's pre-sweep snapshot), and their checkpoints were indistinguishable because `task_id` was `sweep-$$` — the PID of each Bash *subshell*, which varies *within* a single sweep across tool calls, not a stable per-invocation id.

### Step 0a: Generate the stable run id (once, at sweep start)

Run this **exactly once**, before anything else:

```bash
RUN_ID=$(./.loom/scripts/sweep-run-registry.sh new --pid "$PPID")
echo "sweep run id: $RUN_ID"
```

`sweep-run-registry.sh new` generates a portable (macOS/Linux, no `uuidgen`) run id combining a UTC timestamp + PID + random suffix (e.g. `sweep-20260722T231500Z-84213-a3f9c1`), and registers it under `.loom/sweep-run/<RUN_ID>.json` (gitignored) with a liveness PID for peer detection.

**`--pid "$PPID"` is load-bearing, not decoration (#4691).** `$PPID` expanded *here* — in the tool-call shell — is the long-lived orchestrator (`claude -p /loom:sweep …`) that spans the whole sweep. The tool-call shell itself is a fresh one-shot `<shell> -c …` process that is reaped the moment this Bash block returns, so recording *it* would mark this run dead within seconds of registration; the very next peer scan would then prune this run's registry entry and delete its `main-clean-baseline-<RUN_ID>.txt` mid-sweep (and, because the entry vanishes before the baseline is written, orphan that baseline forever). Passing `$PPID` explicitly also makes the recovery lookup below — which matches on `$PPID` — actually find this entry. `sweep-run-registry.sh` resolves the same orchestrator PID itself when `--pid` is omitted, so an older installed copy of this skill still behaves correctly.

**Treat the printed `RUN_ID` as a fixed literal for the entire rest of this sweep.** Thread it — as that literal string — into every `--task-id "$RUN_ID"` checkpoint write and into the main-clean baseline path below. Do **NOT** regenerate it per Bash tool call, and do **NOT** fall back to `sweep-$$` (that is the exact bug this fixes: `$$` is a fresh subshell PID on every tool call). If you ever lose track of the literal mid-sweep, recover it from the registry rather than minting a new one:

```bash
RUN_ID=$(./.loom/scripts/sweep-run-registry.sh list | awk -v p="$PPID" '$2==p {print $1; exit}')
```

At sweep completion (or abort), remove this run's registry entry. On the subagent path this is after the wave lifecycle settles and just before the Summary Output; **on the daemon path, "completion" means immediately after the last `mcp__loom__dispatch_sweep` call returns** (see "The daemon-dispatch path" below) — dispatch-and-exit is that path's entire job, so there is no later in-session point to defer this to:

```bash
./.loom/scripts/sweep-run-registry.sh cleanup "$RUN_ID"
```

`cleanup` removes **both** RUN_ID-keyed transients of this run: the registry entry `.loom/sweep-run/<RUN_ID>.json` and the main-clean baseline `.loom/sweep-checkpoint/main-clean-baseline-<RUN_ID>.txt` (#4450 — before that, baselines accumulated forever).

This is best-effort cleanup — a dead run's entry *and* baseline are also pruned automatically by any later sweep's peer scan (dead-PID liveness check), so a crash that skips cleanup never leaves a permanent false-positive. The bulk backstop for a run whose peer scan never happens is `loom-daemon clean`, which prunes baselines of non-live runs older than 48h plus checkpoints of closed issues.

Both pruners bias toward **keeping** a transient when liveness is ambiguous (#4691): a `kill(pid, 0)` that fails with `EPERM` means the process *exists* but is not signallable by the pruning caller, so only `ESRCH` ("no such process") authorizes deletion. A never-pruned baseline is a bounded, harmless leak; a baseline deleted under a live sweep silently disables the #3648 contamination-subtraction backstop for the rest of that run.

**Heartbeat refresh, at each wave boundary (#5896).** PID liveness alone cannot tell a genuinely live peer from a same-process zombie: a `/clear` inside this long-lived `claude -p /loom:sweep …` orchestrator does not end the OS process, so a same-process `/clear` + re-invoke leaves a registry entry whose PID stays alive forever even though nothing will ever drive its work again — this was observed live (a dead run's lane held 4 open PRs and 4 `loom:building` claims that stalled for hours before an operator manually confirmed the peer was defunct). The same "PID outlives the sweep" failure also happens under a *different* PID (#6595): a sweep interrupted inside an interactive session that stays open leaves an entry whose PID is alive for as long as that session is. The registry entry's `heartbeat` field starts equal to `timestamp` at registration and must be refreshed periodically so `peers` (Step 0b, below) can label **any** entry whose heartbeat has gone stale — same-PID or not — distinctly from one still actively driving a run. Refresh it at every wave boundary — the Wave Lifecycle's "advance to the next wave" point (step 8, after the post-wave integration gate settles), the nearest existing per-wave hook:

```bash
./.loom/scripts/sweep-run-registry.sh heartbeat "$RUN_ID"
```

This call is best-effort and non-fatal — if it fails (e.g. this run's own entry was already pruned by something else), do not stop the sweep over it; a missed refresh only means this run's own entry may itself be mislabeled stale (`stale-same-pid` or `stale-heartbeat`) by a peer's *next* scan, not that anything is corrupted. `SWEEP_RUN_HEARTBEAT_STALE_SECS` (default 900, 15 minutes) controls how long any entry may go without a refresh before `peers` calls it stale.

### Step 0b: Peer-`/loom:sweep` detection (loud, NON-BLOCKING)

Immediately after registering, probe for other **live** `/loom:sweep` runs in this repo and warn if any are found — never block, never auto-stop (mirroring the Daemon Coexistence contract):

```bash
PEERS=$(./.loom/scripts/sweep-run-registry.sh peers "$RUN_ID")
if [[ -n "$PEERS" ]]; then
  echo "$PEERS" | while read -r rid pid ts hb status; do
    case "$status" in
      stale-same-pid:*)
        age="${status#stale-same-pid:}"
        echo "⚠️  LIKELY-STALE SAME-PROCESS RUN (probably a cleared context, #5896):" >&2
        echo "       run $rid (pid $pid, started $ts, heartbeat stale ${age}) shares THIS" >&2
        echo "       orchestrator's own PID but has not refreshed its heartbeat — almost" >&2
        echo "       certainly a pre-/clear run whose conversation is gone, not a genuine" >&2
        echo "       concurrent sweep. Safe to investigate adopting its lane (verify its" >&2
        echo "       open PRs are frozen first) rather than deferring to it as a live peer." >&2
        ;;
      stale-heartbeat:*)
        age="${status#stale-heartbeat:}"
        echo "⚠️  LIKELY-STALE RUN (pid alive, but not sweeping, #6595):" >&2
        echo "       run $rid (pid $pid, started $ts, heartbeat stale ${age}) is registered" >&2
        echo "       under a still-running process that has not refreshed its heartbeat —" >&2
        echo "       most likely a sweep that was interrupted inside a session that stayed" >&2
        echo "       open, not a genuine concurrent sweep. Treat as situational awareness," >&2
        echo "       not as a peer to defer to (confirm before touching its lane)." >&2
        ;;
      *)
        echo "⚠️  ANOTHER /loom:sweep IS RUNNING IN THIS REPO:" >&2
        echo "       run $rid (pid $pid, started $ts)" >&2
        ;;
    esac
  done
  echo "   Two concurrent sweeps merge into a moving default branch unaware of" >&2
  echo "   each other. Per-issue loom:building claims still prevent double-builds," >&2
  echo "   and each sweep now keys its own main-clean baseline + checkpoints by its" >&2
  echo "   own RUN_ID, so they will not clobber each other's run-state — but you" >&2
  echo "   should be aware both are advancing main. Proceeding (non-blocking)." >&2
fi
```

The `peers` subcommand only reports runs whose recorded PID is still alive (`kill -0`); it prunes any dead-PID entry as a side effect, so a sweep killed with SIGKILL mid-run does not produce a false-positive warning forever. Empty output → no peer → the single-sweep case, no warning printed (byte-for-byte the prior behaviour). Among the entries it does report, `status` distinguishes the two genuinely-active cases (`live` — a different PID, heartbeat fresh; `live-same-pid` — this run's own PID, heartbeat fresh) from the two stale ones: `stale-same-pid:Nm` (the #5896 post-`/clear` zombie, same PID as this run) and `stale-heartbeat:Nm` (#6595 — a different PID that is still alive but has not refreshed in N minutes, typically a sweep interrupted inside a session that stayed open; that entry otherwise warned as a `live` peer for the session's whole lifetime, observed at ~8 hours). Neither stale entry is deleted — PID liveness is ambiguous, and per #4691 only confirmed PID death authorizes removing another run's state — they are only labeled, so a stale label is a strong hint, not proof: confirm before adopting or touching that run's lane. **Do not block, do not auto-stop a genuine peer, do not abort** — a `live`/`live-same-pid` peer sweep is legitimate; this remains situational awareness only for those. See "Coexistence (peer `/loom:sweep` and legacy daemon)" for how this relates to the legacy daemon-PID check.

## Stage -1: Backend detection (Phase D of #3449)

Before the dry-run gate and all wave lifecycles (but **after** Sweep Run Identity above), decide whether to **delegate dispatch to the in-process loom-daemon** or **fall through to the existing in-process subagent dispatch**. This stage is prose for the LLM running this skill; it does not run a separate binary. Implementation is small, side-effect-free probes followed by a single routing decision.

This stage exists because Phase A of epic #3449 (#3452) shipped `mcp__loom__dispatch_sweep`, an MCP tool that queues a sweep on the daemon's spawn queue and returns immediately. When the daemon is reachable **and** a multi-account token pool is configured, dispatching to the daemon means each sweep runs in its own detached process with its own rotated OAuth token — load is balanced across accounts, and the orchestrator session exits sub-2-second after dispatch. When either precondition is missing, today's Mode A/B/C subagent path is the right choice — it works on a solo token, it doesn't depend on a running daemon, and it is the verified behaviour for the v0.9.x line.

The contract is **strict AND between two preconditions**, with an explicit Mode C short-circuit and an explicit `--no-daemon` opt-out. There is **no implicit auto-start** of the daemon if the pool exists but the daemon is down; there is **no implicit "use daemon if reachable even without a pool"** branch. Either probe failing → subagent fallthrough.

### Decision tree (the contract)

```text
PROBE_MODE:
  If --prs flag present OR any PR-side NL trigger detected → Mode C (subagent always)

PROBE_DAEMON:
  Ping ~/.loom/loom-daemon.sock with 500ms timeout. Pong → reachable.

PROBE_POOL:
  Count *.token files in .loom/tokens/ OR ACCOUNT_KEY_* lines summed across the merged
  claude-monitor / .loom/accounts.env / legacy .env account sources. Pool exists if count >= 2.

DECIDE:
  if Mode C: use_subagent()
  elif --no-daemon: use_subagent()
  elif LOOM_SWEEP_CLAIM_OWNED is set or CLAIM_OWNED is set: use_subagent()   # daemon-owned child — skip re-probe entirely (#3829/#4111)
  elif PROBE_DAEMON AND PROBE_POOL: use_daemon()
  else: use_subagent()
```

The precedence is deliberate:

1. **Mode C → subagent** (always, regardless of daemon/pool state). This is a routing choice about *this skill's own* `--prs` invocation, **not** a statement about daemon capability: the daemon's dispatch surface accepts `kind={"PrSet":[<n1>,<n2>,…]}` as well as `kind={"Issue":N}` (#5342 — see `.loom/docs/daemon-reference.md` → `dispatch_sweep`), so PR-set dispatch **is** available on the daemon. (An earlier version of this line claimed PR-set dispatch was an explicit non-goal with no v0.10.0 roadmap slot; #5342 landed it, so that claim is retired.) An operator who typed `--prs` is asking for an in-session PR-set run they can watch, so Mode C still routes to the in-process subagent path, which supports Mode C end-to-end. Daemon-side `PrSet` dispatch is used on the daemon path for a *different* purpose — re-routing an issue candidate the daemon's open-PR guard refuses, see "The daemon-dispatch path" below.
2. **`--no-daemon` → subagent** (operator opt-out, after Mode C but before any probes). When this flag is present, do not even attempt the `PROBE_DAEMON` Ping — saves a 500ms ceiling and produces predictable behaviour for debug/demo/scripted runs.
3. **`LOOM_SWEEP_CLAIM_OWNED` set (or the equivalent `--claim-owned N` flag, #4111) → subagent** (daemon-owned child self-detection, #3829 — after `--no-daemon`, still **before** any probes). This env var — and, as of #4111, the positional `--claim-owned <N>` flag in this invocation's own `$ARGUMENTS` — is present **only** on a child that `loom-daemon` itself dispatched (`SweepRegistry::dispatch` → `spawn_child`, `sweep_registry.rs`), carrying the issue number the daemon already claimed on this child's behalf (the same marker/flag the "1. Per-issue pre-flight" Step 1a self-claim check consumes one stage later). A daemon-dispatched child is **by construction** running in the exact environment that makes `PROBE_DAEMON ∧ PROBE_POOL` true — a live daemon plus a multi-account pool, since that is *why* it was dispatched there — so without this rule it would always land on `use_daemon` and issue a **circular** MCP round-trip back into the very daemon that spawned it (`mcp__loom__list_sweeps`, or worse a self-re-dispatch of its own issue number). In headless `claude -p` mode there is no operator to interrupt a stuck tool call and Stage -1's "500ms timeout" is LLM-directed prose, not a mechanically-enforced transport guard, so that round-trip can hang the whole session idle before it ever reaches the Builder phase. The child is already the daemon's work — it must run the lifecycle **itself**, in-process, exactly like `--no-daemon`. This short-circuit removes the entire class of hang. Mirrors `--no-daemon`: do not even attempt the `PROBE_DAEMON` Ping.
4. **`PROBE_DAEMON ∧ PROBE_POOL → daemon`** (the only way to land on the daemon path). **Strict AND**: both probes must succeed. Either missing → fallthrough.
5. **Else → subagent** (the universal fallthrough, equivalent to v0.9.x behaviour).

### The three probes

#### PROBE_MODE — mode classification (already done)

Mode classification happens in the existing "Mode-selection precedence" rules above (Arguments → Validation rules). By the time Stage -1 runs, the skill knows whether it is in Mode A, B, or C. **If the mode is C, the decision is already made — go straight to the subagent path** (the "Stage 0: Dry-run gate" section below, then "PR-set Wave Lifecycle"). Do not run the daemon or pool probes for Mode C.

#### PROBE_DAEMON — is the loom-daemon reachable?

The daemon listens on `~/.loom/loom-daemon.sock` (a Unix-domain socket). A reachability probe is a cheap `mcp__loom__list_sweeps` invocation — the daemon answers with the current sweep list (which may be an empty array if the daemon is up but no sweeps are queued). Either a successful response **or** an empty-list response is a "pong" — the daemon is reachable.

Use a **500ms timeout** on this probe. The MCP layer accepts a timeout parameter; do not raise it. The 500ms ceiling covers two failure modes simultaneously:

- **No daemon running.** The Unix socket file does not exist, or the connection refused immediately. The MCP call returns an error in well under 500ms; treat as `PROBE_DAEMON = false`.
- **Stale socket.** The socket file exists but no process is listening (e.g., the daemon crashed without cleanup). The connection hangs until the OS times out — that's the 500ms guard. Timeout → treat as `PROBE_DAEMON = false`. **Do not retry, do not auto-clean the stale socket, do not auto-start the daemon.** Those behaviours belong in operator tools, not in this skill.

A successful response (any well-formed `EventStream`/sweep-list payload, including the empty case) → `PROBE_DAEMON = true`.

```text
PROBE_DAEMON pseudocode (LLM-directed):

  if NO_DAEMON or LOOM_SWEEP_CLAIM_OWNED is set or CLAIM_OWNED is set:
      PROBE_DAEMON = false   # short-circuit; do not even issue the call
                             # (LOOM_SWEEP_CLAIM_OWNED / --claim-owned: daemon-owned
                             #  child, #3829/#4111 — re-probing the spawning daemon
                             #  is circular)
  else:
      try:
          response = mcp__loom__list_sweeps(timeout_ms=500)
          PROBE_DAEMON = true        # any structured response = reachable
      except timeout, connection_error, no_such_tool:
          PROBE_DAEMON = false
```

The `no_such_tool` case covers older Loom installs without Phase A's MCP additions — treat as "daemon not reachable" and fall through. Do not try to detect the daemon by other means (no `ps` parsing, no PID file reads — the socket probe is the authoritative reachability test).

#### PROBE_POOL — does a multi-account token pool exist?

A pool exists if **either** of these is true (logical OR, both checked):

1. **Materialized pool**: `.loom/tokens/*.token` contains **two or more** files. The bootstrap step (`loom-daemon tokens bootstrap`) writes one `*.token` file per `ACCOUNT_KEY_*` triple in the merged account set; a count `>= 2` means at least two distinct accounts are available for rotation.
2. **Configured pool**: **two or more** `ACCOUNT_KEY_*` lines are declared across the **merged account sources** — the claude-monitor master (`${LOOM_CLAUDE_MONITOR_DIR:-$HOME/.claude-monitor}/accounts.env`), the repo-local file (`.loom/accounts.env`, falling back to the legacy `.env`), and — **only when `LOOM_ACCOUNTS_ENV` is set** — the opt-in home master at that path. This catches the case where the operator has configured multiple accounts (in the post-#3695/#3704 claude-monitor-first layout, not just the legacy `.env`) but hasn't yet run `loom-daemon tokens bootstrap` — the daemon's spawn-time selector can still pick a token, and the pool will be materialized on demand.

Both checks are cheap, local, and side-effect-free. The configured-pool count mirrors `bootstrap.py`'s source precedence but does **not** dedupe by email — a raw sum of `ACCOUNT_KEY_*` lines is an accepted approximation for this boolean `>= 2` gate (worst case a single account declared in two sources double-counts at the `== 1` vs `== 2` boundary, a false-positive toward daemon use that still requires `PROBE_DAEMON` to also be true):

```bash
TOKEN_FILE_COUNT=$(find .loom/tokens -maxdepth 1 -name '*.token' 2>/dev/null | wc -l | tr -d ' ')

# Repo-local (mirrors bootstrap.py: .loom/accounts.env if present, else legacy .env)
# NOTE: `grep -c` prints `0` AND exits non-zero on an existing-but-empty file, so a
# `|| echo 0` fallback would emit a two-line "0\n0" and abort the arithmetic below under
# bash 3.2. Use `|| true` + `${var:-0}` so an existing-empty source yields exactly `0`.
if [[ -f .loom/accounts.env ]]; then
  REPO_KEY_COUNT=$(grep -c '^ACCOUNT_KEY_' .loom/accounts.env 2>/dev/null || true); REPO_KEY_COUNT=${REPO_KEY_COUNT:-0}
else
  REPO_KEY_COUNT=$(grep -c '^ACCOUNT_KEY_' .env 2>/dev/null || true); REPO_KEY_COUNT=${REPO_KEY_COUNT:-0}
fi

# claude-monitor master (primary source per CLAUDE.md; LOOM_CLAUDE_MONITOR_DIR override)
MONITOR_DIR="${LOOM_CLAUDE_MONITOR_DIR:-$HOME/.claude-monitor}"
MONITOR_KEY_COUNT=$(grep -c '^ACCOUNT_KEY_' "$MONITOR_DIR/accounts.env" 2>/dev/null || true); MONITOR_KEY_COUNT=${MONITOR_KEY_COUNT:-0}

# Opt-in home master — only consulted when LOOM_ACCOUNTS_ENV is set and non-empty (per #3704)
HOME_KEY_COUNT=0
if [[ -n "${LOOM_ACCOUNTS_ENV:-}" ]]; then
  HOME_KEY_COUNT=$(grep -c '^ACCOUNT_KEY_' "$LOOM_ACCOUNTS_ENV" 2>/dev/null || true); HOME_KEY_COUNT=${HOME_KEY_COUNT:-0}
fi

ENV_KEY_COUNT=$(( REPO_KEY_COUNT + MONITOR_KEY_COUNT + HOME_KEY_COUNT ))
if (( TOKEN_FILE_COUNT >= 2 )) || (( ENV_KEY_COUNT >= 2 )); then
  PROBE_POOL=true
else
  PROBE_POOL=false
fi

# Discoverable signal: accounts configured but not yet bootstrapped. Only fires when
# the merged sources declare a pool (ENV_KEY_COUNT >= 2) yet .loom/tokens/ has < 2
# token files — NOT on every subagent fallthrough.
if (( ENV_KEY_COUNT >= 2 )) && (( TOKEN_FILE_COUNT < 2 )); then
  echo "Configured account pool detected but not bootstrapped — run 'loom-daemon tokens bootstrap' to materialize .loom/tokens/." >&2
fi
```

A single-token configuration (`TOKEN_FILE_COUNT == 1` and `ENV_KEY_COUNT <= 1`) is **not** a pool — the daemon dispatch path needs at least two accounts to make rotation meaningful, and a single-token operator gets no benefit from delegating to the daemon. Fall through to the subagent path in that case.

> **Why >= 2 and not >= 1?** A pool of one is not a pool — it is a single token, and rotation requires alternatives. The daemon's dispatch advantage (per-sweep token selection, weekly-quota recovery) only materializes once two-or-more accounts are configured. Single-token operators see no degradation in the subagent path; this preserves the existing solo-token experience.

### Resolve auto wave size (when `BUILDERS_PER_WAVE = auto`)

Run this **after `DECIDE` is known** (both probes done) and **before** taking the daemon-dispatch or subagent-fallthrough branch below. If `BUILDERS_PER_WAVE` is a concrete integer (the operator passed `--builders-per-wave N`), **skip this entire block** — the explicit value wins and flows into the wave-partition consumers unchanged. Mode C also never reaches this block: Mode C is size-1 and ignores `--builders-per-wave` (the `DECIDE` precedence already routed it to the subagent path).

The disk math lives in a small sourceable helper so it is deterministic and unit-tested (`defaults/scripts/lib/disk-headroom.sh`, tested by `defaults/scripts/tests/test-disk-headroom.sh`). The skill sources it and calls two functions; it does not do the arithmetic inline:

```bash
source ./.loom/scripts/lib/disk-headroom.sh
REPO_ROOT="$(git rev-parse --show-toplevel)"
# df's the RESOLVED worktree root (scratch volume), not the repo drive.
# Unknown != zero (#4164): loom_worktree_root_free_gb prints NOTHING and
# returns non-zero when it cannot actually measure free space (missing arg,
# unresolvable worktree root, a failing/malformed `df`) — capture the exit
# status here and DO NOT feed a fake "0" into loom_wave_size_from_disk below;
# that used to be indistinguishable from a genuinely full disk.
DISK_PROBE_OK=true
FREE_GB="$(loom_worktree_root_free_gb "$REPO_ROOT")" || DISK_PROBE_OK=false
```

Then resolve by branch (`CAND` = number of surviving candidate issues):

```bash
if [[ "$DECIDE" == use_daemon ]]; then
    # Detached-process path: each sweep is its own OS process with its own
    # rotated token. NOT nested subagents, so #3289 does not apply — scale to 10.
    MECH=daemon;   MECHANISM="daemon detached-process"
else  # use_subagent (no daemon, single-token pool, --no-daemon, daemon-owned child, or Mode C)
    # In-session Task subagents, one level deep. WIDTH is bounded by the harness
    # concurrency cap (min(16, cores-2)), NOT by #3289 (which is a nesting rule,
    # not a width rule). Core-scale the subagent target within [3, 6] via
    # loom_subagent_target_from_cores (#3693); an operator-set LOOM_SUBAGENT_WAVE_CAP
    # always wins (the `:=` only fills an unset/empty value).
    : "${LOOM_SUBAGENT_WAVE_CAP:=$(loom_subagent_target_from_cores "$(loom_detect_cores)")}"
    export LOOM_SUBAGENT_WAVE_CAP
    MECH=subagent; MECHANISM="in-session subagent"
fi
if [[ "$DISK_PROBE_OK" == true ]]; then
    # The helper prints two lines: size on line 1, reason token on line 2.
    # Capture both without `mapfile` (a bash-4.0+ builtin) so this works under
    # macOS's default /bin/bash 3.2: grab stdout once, then split by line.
    _WS_OUT="$(loom_wave_size_from_disk "$MECH" "$CAND" "$FREE_GB")"
    WAVE_SIZE="$(sed -n '1p' <<<"$_WS_OUT")"; REASON="$(sed -n '2p' <<<"$_WS_OUT")"
else
    # Unknown != zero (#4164): the disk probe failed, so SKIP the disk clamp
    # entirely rather than feeding a bogus 0 into loom_wave_size_from_disk
    # (which would floor the wave size to 1 and log reason "floor" —
    # indistinguishable from a genuinely full disk). Fall back to
    # K = min(target, CAND) with no disk term at all.
    if [[ "$MECH" == daemon ]]; then
        _TARGET="${LOOM_DAEMON_WAVE_TARGET:-10}"
    else
        _TARGET="$LOOM_SUBAGENT_WAVE_CAP"
    fi
    if (( CAND < _TARGET )); then
        WAVE_SIZE="$CAND"
    else
        WAVE_SIZE="$_TARGET"
    fi
    (( WAVE_SIZE < 1 )) && WAVE_SIZE=1
    REASON="unknown"
fi
```

`loom_wave_size_from_disk` prints two lines — the clamped size `K = min(target, floor(free_gb / LOOM_PER_WORKTREE_GB), CAND)` with a floor of 1 (never 0, even on a full disk) on line 1, and a machine reason token (`target` / `candidates` / `disk` / `floor`) on line 2. `LOOM_PER_WORKTREE_GB` defaults to a conservative 2 GB and is env-overridable for large-repo operators. The target is **10** for the daemon path; for the subagent path it is the **core-scaled** `clamp(floor((cores-2)/4), 3, 6)` (#3693) — resolved into `LOOM_SUBAGENT_WAVE_CAP` just above via `loom_subagent_target_from_cores` / `loom_detect_cores`, floor 3 on small/shared hosts, ceiling 6 on big ones — and an operator-set `LOOM_SUBAGENT_WAVE_CAP` env value always overrides it.

**When the disk probe fails** (`DISK_PROBE_OK=false`, #4164), skip `loom_wave_size_from_disk` entirely — its pure-integer contract is unchanged and is not the caller-side fallback policy's home — and resolve `WAVE_SIZE = min(target, CAND)` (floor 1) with the reason token `unknown`, so an unmeasurable probe can never masquerade as a measured `0`.

**Emit a one-line reason** so the operator understands any reduction. Map the reason token to a human sentence, adding the backend-specific context:

| `DECIDE` / reason | One-line log |
|-------------------|--------------|
| `use_daemon`, `target` | `wave size 10, mechanism=daemon: daemon + multi-account pool → detached-process path (target 10)` |
| `use_subagent`, `target`, daemon not reachable | `wave size K, mechanism=subagent: daemon not reachable → subagent path (core-scaled target K, floor 3, ceiling 6)` |
| `use_subagent`, `target`, no pool | `wave size K, mechanism=subagent: single-token pool → subagent path (core-scaled target K, floor 3, ceiling 6)` |
| any, `candidates` | `wave size K, mechanism=<m>: reduced to K (only K candidate issues)` |
| any, `disk` | `wave size K, mechanism=<m>: reduced to K (only <FREE_GB> GB free on <worktree-root>)` |
| any, `floor` | `wave size 1, mechanism=<m>: reduced to 1 (only <FREE_GB> GB free on <worktree-root>)` |
| any, `unknown` | `wave size K, mechanism=<m>: disk headroom unknown (probe failed on <worktree-root>) — disk clamp skipped` |

The resolved `WAVE_SIZE` replaces `--builders-per-wave` everywhere the wave-partition consumers below reference it. On the **daemon path** `WAVE_SIZE` is the concurrency **target** the operator should expect (and that `--dry-run` reports) — the daemon runs each candidate as an independent detached process, so it is not a hard in-session partition. On the **subagent path** `WAVE_SIZE` is the literal wave partition size feeding the `min(...)` dispatch expression in the Wave Lifecycle. In both cases, **never raise the subagent ceiling toward 10** — the subagent auto default core-scales within `[3, 6]` (#3693), and true high parallelism toward 10 is the daemon path's job. (This is a width ceiling; the #3289 "one level deep" nesting rule is a separate, unchanged constraint the daemon path exists to route around.)

### The daemon-dispatch path (when `DECIDE = use_daemon`)

When `DECIDE` lands on `use_daemon`, the skill **dispatches each candidate issue** to the daemon and **exits sub-2-second**. There is no in-session orchestration after dispatch — operators monitor with `mcp__loom__list_sweeps` (Phase A) or the richer Phase C tools once they land.

**Housekeeping still applies on this path, at these two fixed points.** Before the first `dispatch_sweep` call below, run the Host Sleep Readiness (#3350) and Main Branch Freshness (#3770) checks (their trigger is now backend-independent — see "before the first wave, or on the daemon path before the first dispatch" in each section below). They matter *more* here than on the subagent path: each of the N children this loop is about to spawn runs for minutes-to-hours as a detached process, and a stale local default branch or a mid-run host sleep affects all N of them at once, not just one session. After the last `dispatch_sweep` call returns, run Step 0a's `sweep-run-registry.sh cleanup "$RUN_ID"` (this run's registry entry and main-clean baseline are no longer needed once dispatch is done) and the Session Transcript Archival step (its own daemon-path note, below, explains what it captures on this path).

**Derive `WORKSPACE_ROOT` once, before dispatching, and pass it explicitly on every `dispatch_sweep` call below.** Omitting `workspace_root` routes through the daemon's workspace-registry resolution (#4299/PR #4322): on a host with multiple managed workspaces registered, it either returns a structured ambiguity error, or — the dangerous case — silently resolves to the daemon's seeded default workspace when that default happens to be registered, targeting the wrong repo with no warning. Always pin the target explicitly:

```bash
WORKSPACE_ROOT=$(git rev-parse --show-toplevel)
```

For each candidate issue `N` in the candidate set:

```text
mcp__loom__dispatch_sweep(kind={"Issue": N}, workspace_root=$WORKSPACE_ROOT)
```

**When `AUTO_STACK=true` and edge detection populated `DEPENDS_ON[N]` for candidate `N`** (see "Auto-stack detection and wave ordering"), forward the detected parent on the dispatch:

```text
mcp__loom__dispatch_sweep(kind={"Issue": N}, depends_on=<parent>, workspace_root=$WORKSPACE_ROOT)
```

This is purely "start populating a parameter that already exists" — the daemon and the `mcp__loom__dispatch_sweep` schema already accept `depends_on` (#3729/#3742), forwarding it to the child as `--depends-on <parent>`, so there is **no daemon-side code change**. Candidates with no detected edge dispatch exactly as today (no `depends_on` argument). To respect the parent-before-child topological ordering on the daemon path, dispatch the reordered candidate list in order (a parent stacked-before its child is dispatched first so its `feature/issue-<parent>` branch exists when the child's Builder resolves the base).

The daemon enqueues the sweep, returns a sweep ID, and the skill logs the dispatch (`Dispatched sweep <sweep-id> for issue #N to daemon`). The daemon's spawn-time logic picks an OAuth token from the rotation pool, detaches a `claude -p "/loom:sweep N"` child, and runs the sweep in that child's session — completely independent of this orchestrator session.

**Safe burst dispatch (Issue #6592).** Candidate bursts in the 10-15 range are the designed usage of this path (the daemon targets 10-way dispatch concurrency), and issuing the `dispatch_sweep` calls for a candidate set concurrently — in one tool block, rather than one-at-a-time — is intentional and supported: the daemon's IPC handler processes the guard-chain + spawn for each request under a brief lock hold and runs the multi-second account-selection poll **off** the shared registry mutex (`dispatch_sweep_nonblocking` in `loom-daemon/src/ipc.rs`), so a burst's acks no longer serialize behind each other and stay well under the client's 30s deadline. Two things to keep in mind regardless:

- **Always pass a stable `idempotency_key` per candidate issue** (e.g. derived from the issue number and run ID) on every `dispatch_sweep` call. If a client-side ack ever does time out anyway — a genuinely oversized burst, host distress, or a slow forge round trip — the request is far more likely to still be queued or executing than actually failed; the timeout error text says so explicitly and names the same key as the safe retry. Retrying with that same key returns the already-dispatched sweep (`was_new: false`) instead of spawning a duplicate.
- **A timed-out ack with no `idempotency_key` is not safe to blindly retry** — check `mcp__loom__list_sweeps` for a sweep that already started on that issue before re-dispatching, since a keyless retry has no dedup protection against a double-spawn.

**The skill does NOT subscribe to events.** Phase B's pub/sub bus is consumed by long-running monitors and the spawn loop, not by the skill itself. The skill is fire-and-forget: dispatch, log, exit.

**Mode C is excluded — by routing, not by daemon capability.** Mode C uses `--prs` (or NL triggers) and the `DECIDE` precedence sends it to the subagent path before this branch is evaluated, so if `PROBE_MODE` returned Mode C this branch is unreachable. That exclusion is a property of *this skill's* precedence table (rule 1 above), **not** a daemon limitation: the daemon does accept `kind={"PrSet":[…]}` (#5342). The paragraph immediately below is exactly where this path issues one.

**Candidates with an open linked PR CANNOT be dispatched issue-keyed on this path (#6593).** The daemon runs its own open-PR guard (#4123) *before* it spawns anything: `dispatch_sweep(kind={"Issue":N})` for an issue whose closes-graph shows an open linked PR `#P` is refused outright with

```text
refusing to dispatch issue #N: it already has an open linked PR #P (#4123 open-PR guard). …
To drive the existing PR forward instead of rebuilding, dispatch kind={"PrSet":[P]} (Mode C, #5342) …
```

This means **the aggressive taxonomy's "Has an open linked PR → drive it through Judge / Doctor → Merge, do not build a duplicate" row (see "Aggressive candidate taxonomy") is NOT reachable via issue-keyed dispatch here.** That routing normally happens in the spawned child's "1. Per-issue pre-flight" existing-PR probe — but on this path no child is ever spawned for such a candidate, so the pre-flight never runs. Dispatch the PR set instead:

```text
mcp__loom__dispatch_sweep(kind={"PrSet": [P]}, workspace_root=$WORKSPACE_ROOT)
```

Rules for this re-route:

- **Only the PR(s) actually linked to the refused candidate.** Build the `PrSet` from the PRs the taxonomy's step-1 union probe attributes to *that* issue — never widen it to unrelated open PRs, and never batch several candidates' PRs into one `PrSet` call (one refused candidate → one `PrSet` dispatch naming only its own PR(s)).
- **Pre-empt the refusal when the candidate set already told you.** The candidate resolution for `all` (and the confirmation-gate listing) already knows which candidates have an open linked PR — dispatch those as `kind={"PrSet":[…]}` in the first place rather than issuing a dispatch you know will be refused.
- **Ambiguity skips, as elsewhere.** If a candidate has more than one open linked PR, skip it with a log (same rule as the per-issue pre-flight's multi-PR ambiguity skip) rather than guessing which PR to drive.
- **A `PrSet` dispatch claims no issue.** It never flips `loom:building` and takes `.loom/locks/pr-<N>/` locks rather than `.loom/locks/issue-<N>/`, so it does not conflict with the issue's existing claim state. The daemon does **not** auto-convert a refused issue-keyed dispatch into a `PrSet` one — the two claim different things, so the re-dispatch is deliberate and explicit.

**Exit immediately after the last `mcp__loom__dispatch_sweep` returns and its housekeeping above (run-registry cleanup, transcript archival) completes.** Do **not** run the dry-run gate, the issue-side wave lifecycle, or any of the "0." through "8." stages below — those are subagent-path-only and would double-orchestrate. This exclusion does **not** cover the pre-/post-dispatch housekeeping named above: that is orchestrator-session bookkeeping (host-sleep, main-freshness, run-registry cleanup, transcript archival) that applies on both paths, not part of the subagent-only "0." through "8." lifecycle. The skill's job in the daemon path is dispatch (plus that housekeeping) and exit; the daemon-side child runs the full Curator → Builder → Judge → Doctor → Merge lifecycle in its own session.

**Dry-run interaction:** when `--dry-run` is passed alongside the daemon path, **the dry-run gate (Stage 0) still runs and the skill EXITs without dispatching**. Dry-run is a read-only contract independent of backend choice; it prints the candidate plan and exits without mutation regardless of whether the daemon would have been used. This is intentional — operators previewing a sweep should see the plan before any backend dispatches.

### The subagent fallthrough (when `DECIDE = use_subagent`)

Otherwise — `DECIDE` is `use_subagent` for **any** of the reasons above (Mode C, `--no-daemon`, `LOOM_SWEEP_CLAIM_OWNED` set / `--claim-owned N` passed (daemon-owned child, #3829/#4111), daemon unreachable, no pool, or any probe error) — **continue to "0. Dry-run gate" below and run the existing Mode A/B/C lifecycle in-process exactly as today**. This is the v0.9.x behaviour, unchanged. The skill prose from "0. Dry-run gate" onward is the canonical subagent path.

No behaviour change for solo-token operators: their `PROBE_POOL` returns `false`, the `DECIDE` lands on `use_subagent`, and the rest of the skill runs as it always has.

### Smoke tests (documented expectations)

These are the AC #3 and AC #4 contracts, written for the operator.

**Daemon-on + multi-account pool (AC #3):**

```bash
# Preconditions:
#   - loom-daemon is running (`pgrep loom-daemon` matches, ~/.loom/loom-daemon.sock exists)
#   - At least 2 accounts configured — in .loom/tokens/, or ACCOUNT_KEY_* lines across
#     the merged claude-monitor / .loom/accounts.env / legacy .env account sources

/loom:sweep 123 456

# Expected:
#   1. Stage -1 runs: PROBE_MODE=A, PROBE_DAEMON=true, PROBE_POOL=true.
#   2. DECIDE = use_daemon.
#   3. Skill calls mcp__loom__dispatch_sweep for issue 123 → logs sweep ID.
#   4. Skill calls mcp__loom__dispatch_sweep for issue 456 → logs sweep ID.
#   5. Skill exits in < 2 seconds.
#   6. Daemon runs the two sweeps independently in detached processes.
#   7. Operator monitors progress via mcp__loom__list_sweeps or Phase C tools.
```

**Daemon-off OR single-token (AC #4):**

```bash
# Preconditions:
#   - Either loom-daemon is not running, OR the merged account sources
#     (claude-monitor / .loom/accounts.env / legacy .env) have < 2 ACCOUNT_KEY_* lines total.

/loom:sweep 123 456

# Expected:
#   1. Stage -1 runs: PROBE_MODE=A, PROBE_DAEMON or PROBE_POOL is false.
#   2. DECIDE = use_subagent.
#   3. Skill continues to "0. Dry-run gate" → "Resolve auto wave size" → "Wave Lifecycle".
#   4. Auto wave size resolves to the subagent path (core-scaled target in [3,6],
#      candidate- and disk-clamped): both issues land in one wave of 2 (clamped
#      to the candidate count, or fewer if the scratch volume is tight).
#   5. Each issue runs Curator→Builder→Judge→Doctor→Merge in-session.
#      (Pass an explicit --builders-per-wave 1 to force the old fully-sequential behaviour.)
#   6. Skill exits when both issues have settled (potentially many minutes).
```

**`--no-daemon` opt-out:**

```bash
# Preconditions: any. The flag forces the subagent path.

/loom:sweep 123 456 --no-daemon

# Expected:
#   1. Stage -1 sees NO_DAEMON=true → PROBE_DAEMON skipped entirely.
#   2. DECIDE = use_subagent.
#   3. Skill continues to "0. Dry-run gate" → "Wave Lifecycle" → ... exactly as today.
```

**Mode C (PR-set):**

```bash
# Preconditions: any. Mode C short-circuits Stage -1's daemon path.

/loom:sweep --prs 200 201

# Expected:
#   1. PROBE_MODE = C (because --prs is present).
#   2. DECIDE = use_subagent (regardless of daemon/pool state).
#   3. Skill continues to "0. Dry-run gate" → "PR-set Wave Lifecycle" → ... exactly as today.
```

**Daemon-owned child (`LOOM_SWEEP_CLAIM_OWNED` set / `--claim-owned N` passed, #3829/#4111):**

```bash
# Preconditions: this session is itself a child that loom-daemon dispatched, so
#   LOOM_SWEEP_CLAIM_OWNED=<N> is exported into its environment AND --claim-owned
#   N is embedded in its own -p prompt text / $ARGUMENTS (by
#   SweepRegistry::dispatch → spawn_child). The daemon and multi-account pool are
#   therefore reachable BY CONSTRUCTION — but this child must NOT re-dispatch.

# (the daemon internally runs, for the issue it claimed:)
#   LOOM_SWEEP_CLAIM_OWNED=123 claude -p "/loom:sweep 123 --claim-owned 123" --dangerously-skip-permissions

# Expected:
#   1. Stage -1 sees LOOM_SWEEP_CLAIM_OWNED is set → PROBE_DAEMON skipped entirely
#      (never issues mcp__loom__list_sweeps back into the spawning daemon).
#   2. DECIDE = use_subagent regardless of daemon/pool reachability.
#   3. Skill continues to "0. Dry-run gate" → "Wave Lifecycle" → runs the full
#      Curator→Builder→Judge→Doctor→Merge lifecycle IN-PROCESS, exactly like --no-daemon.
#   4. No circular re-dispatch of its own issue number; no idle-hang on a stuck
#      MCP round-trip. This is the #3829 fix — every daemon-dispatched child
#      progresses to build rather than stalling in Stage -1.
```

### What Stage -1 does NOT do

- **Does not auto-start the daemon** if the pool exists but the daemon is unreachable. Auto-start is operator policy, not skill policy.
- **Does not write `~/.loom/loom-daemon.sock` cleanup** for stale sockets. Stale-socket cleanup belongs to the daemon's own startup logic and to operator tools.
- **Does not subscribe to the Phase B event bus.** Subscription is consumed by long-running monitors and the spawn loop, not by this skill. Phase D is dispatch-only.
- **Does not retry probe failures.** Either probe returns within 500ms (or its natural latency) and is treated as authoritative; no retry, no backoff.
- **Does not mutate any forge state** during the probes. `mcp__loom__list_sweeps` and the local pool checks are read-only. Even in the daemon path, mutation happens inside the daemon-side child sweep, not in this orchestrator session.
- **Does not log to `.loom/daemon-state.json` or any daemon-owned state file.** Read-only access is fine for situational awareness; writes are forbidden (same constraint as the legacy-daemon subsection of "Coexistence (peer `/loom:sweep` and legacy daemon)").
- **Does not re-probe or re-dispatch to the daemon when it is itself a daemon-dispatched child (#3829/#4111).** If `LOOM_SWEEP_CLAIM_OWNED` is set or `--claim-owned N` was passed, the child is already the daemon's work — the `DECIDE` tree short-circuits to `use_subagent()` **before** `PROBE_DAEMON` runs, so no `mcp__loom__list_sweeps` (and no `mcp__loom__dispatch_sweep` of its own issue) is ever issued back into the spawning daemon. Re-probing/re-dispatching there is circular by construction and, in a headless `-p` session with no operator to interrupt a stuck tool call, was the cause of the idle-hang this rule removes.

## Overlap-aware wave partitioning (file-surface scheduling signal, #4161)

Wave partitioning (Execution Model above) picks candidates into waves **in list order** with no awareness of which files each candidate is likely to touch. When two candidates in the **same** wave edit the same file, their builders branch off the same pre-wave `main` snapshot, each PR passes Judge independently, and both report `MERGEABLE`/`CLEAN` — but only until the first merges (see the base-branch-only callout in Execution Model). The reactive step 7 revalidation then pays a Doctor rebase cost that a smarter *partition* could have avoided. This section adds the **proactive** complement: estimate each candidate's file surface cheaply, and keep overlapping candidates out of the same wave (or, when that is impossible, warn loudly at the confirmation gate).

**File overlap is a *scheduling* signal only — never a *topology* signal.** Overlap decides **which wave** a candidate lands in (or produces a warning). It **must never** create a `--depends-on`/`--auto-stack` stacking edge: #3729 explicitly rejected file paths as a stacking-topology signal (only the authoritative `Depends on #A` / `Requires #A` body text creates stacks — see "Auto-stack detection and wave ordering"). Overlap-aware partitioning and auto-stacking are distinct uses of the same raw data; do not conflate them.

### 1. Estimate each candidate's file surface (cheap, non-blocking)

Per-issue pre-flight already reads each candidate's issue body (dry-run survey step 1 under `--auto-stack`; live Wave Lifecycle step 2 always). Add `body` to the survey read unconditionally — one extra `--json` field, **no extra API call** — and parse the issue's `## Affected Files` section:

- Collect the **backtick-quoted paths** from the bullets under an `## Affected Files` heading (the exact format Curators emit — see `curator.md`). A bullet like `` - `defaults/.claude/commands/loom/sweep.md` — … `` contributes the path `defaults/.claude/commands/loom/sweep.md`.
- **Missing `## Affected Files` section, or a section that reads "To be determined" / has no backtick paths → the candidate's surface is *unknown*.** An unknown-surface candidate is **excluded from overlap analysis entirely**: it never triggers a warning, never forces a reorder, and never blocks. Optionally note "surface unknown" beside it in the plan. Never serialize or block on missing data — a candidate without a parseable surface plans byte-for-byte as it does today.

Surface estimation is a heuristic on curated prose, not a diff inspection; it is deliberately cheap and best-effort. A candidate whose real diff touches a file its `## Affected Files` omitted is caught by the reactive step 7 / step 8 gates, which stay the backstop.

### 2. Adjust the partition to separate overlapping candidates

After the wave partition is computed — and, under `--auto-stack`, **after** the #3759 parent-before-child topological reorder — scan for **same-wave pairs whose estimated surfaces share ≥1 path**:

- **Reorder greedily.** For an overlapping same-wave pair, swap one member with a later **non-overlapping** candidate so the two land in different waves. Prefer swaps that preserve input order where possible; the algorithm is deterministic (same candidate list → same partition), single-pass, and uses no graph machinery.
- **Never break an auto-stack ordering constraint.** A swap must keep every parent's wave at or before its child's wave. A stacked child *intentionally* shares files with its parent, so **exclude any pair already related via `DEPENDS_ON[N]`** (either direction) from overlap detection — that is expected sharing, not an accidental collision, and it is already handled by the stacked-branch mechanics.
- **Unavoidable overlap → warn, don't loop.** If an overlapping group has more members than there are waves to spread them across (so no reorder can separate them all), **leave the placement as-is** and emit the confirmation-gate warning below. Do not enter a reorder loop; a single warning is the contract.

The reorder only changes **wave assignment**; it never adds/removes candidates and never creates a stacking edge.

### 3. Surface the analysis (dry-run block + confirmation-gate warning)

- **`--dry-run` overlap-analysis block** — the dry-run plan (Modes A/B, "Issue-set output spec") gains an `Overlap analysis` block naming each overlapping group's shared file(s), the resulting wave moves, and any unavoidable-overlap warning. This is exactly where an operator wants to catch a collision. See the block spec in "Procedure — Modes A and B".
- **Confirmation-gate warning** — both the Mode B candidate-set gate and the `all`-sentinel **mandatory** confirmation gate print any **unavoidable-overlap** warning **above** the candidate listing, naming the shared files and the specific candidates, so the operator can reorder manually or drop to `--builders-per-wave 1` in seconds before dispatch.

### 4. Reactive fallback (do not rebuild — cross-reference)

Whatever overlap the partition **could not** avoid (unavoidable groups, or a real diff that exceeded its `## Affected Files` estimate) is caught reactively at merge time by **Wave Lifecycle step 7 (Intra-wave overlap revalidation, #3647)** — it re-checks each about-to-merge PR's changed-file set against `WAVE_MERGED_FILES`, updates an overlapping branch onto the just-merged `main`, and routes `DIRTY` to an inline Doctor→Judge cycle. **Step 8 (post-wave integration gate, #3647)** additionally catches cross-file semantic coupling (source-vs-test) that file-path overlap cannot see. Proactive partitioning reduces how often that reactive cost is paid; it does not replace it. This section adds no new reactive machinery — step 7/8 already exist and are the fallback.

## Capability-aware `loom:operator-mechanical` lane (#6885 Part 2, #6893)

`loom:operator-only` is a hard skip for all four of its sub-kinds. One of them — `loom:operator-mechanical` — means "needs host/admin access or a credential, but **no judgement**" (`.loom/docs/label-state-machine.md` → "`loom:operator-only` sub-kinds"). This section is the *only* place that treats it differently, and it does so under four gates that all have to pass. **Nothing here weakens `loom:operator-only` for any other item**: `loom:operator-decision`, `loom:operator-blocked`, `loom:operator-objective`, `loom:needs-capability` and `loom:blocked` are byte-for-byte unchanged, and the routing tables above land every non-matching case on the ordinary hard-skip row.

### Eligibility (all four gates, in order — any failure means "skip exactly as today")

1. **This worker declares capabilities.** `LOOM_WORKER_CAPABILITIES` is set in the environment, e.g. `export LOOM_WORKER_CAPABILITIES="host-sudo,cloud-profile:prod-aws"`. Unset (the default on every host) ⇒ the lane is inert and this whole section is a no-op. It is read from the **environment only, never from `.loom/config.json`** — a capability is a property of the machine and its credentials, and a file committed to git must not be able to assert that the host running it has root.
2. **The labels are exactly the mechanical shape.** Both `loom:operator-only` and `loom:operator-mechanical` are present, and **none** of `loom:operator-decision` / `loom:operator-blocked` / `loom:operator-objective` / `loom:needs-capability` / `loom:blocked` / `loom:operator` is. A contradictory pairing (mechanical *and* a judgement sub-kind) resolves in favour of the judgement sub-kind — skip.
3. **The body declares at least one capability, and every declared value is recognized.** Parse the item body with the reference parser — never a hand-rolled regex, so this side and the daemon's (`loom-daemon/src/capability.rs`) cannot diverge:
   ```bash
   CAPS=$(gh issue view N --json body --jq .body | ./.loom/scripts/extract-capability-markers.sh); RC=$?
   ```
   `RC=1` (no marker at all) and `RC=2` (an unrecognized value — fail closed, per `.loom/docs/label-state-machine.md`) both mean **skip, unchanged, no comment**. Only `RC=0` continues.
4. **This worker holds every declared capability.** Markers are **ANDed**. Holding some but not all is a miss, and lands on the capability-request path below.

### Behavior when a declared capability is NOT held (AC1)

Do **not** stall silently — turn the park into a named request. Post one comment (idempotent: if a comment already names the same missing capability, skip re-posting) and move on to the next candidate:

```
Capability request (#6893): this item is `loom:operator-mechanical` and declares
`<!-- loom:capability=<missing> -->`, which this worker does not hold.

Held by this worker: <the LOOM_WORKER_CAPABILITIES list, or "(none)">
Missing:             <the specific declared capabilities that are absent>

The item stays parked. To unblock it, either run a sweep on a host that holds the
missing capability, or have an operator perform the mechanical step directly.
```

Never add, remove, or swap a label on this path. The item keeps `loom:operator-only` + `loom:operator-mechanical` exactly as it was.

### Behavior when every capability IS held: propose mode only (AC4)

**The lane's output is a proposal, never a live credentialed action.** This first rollout has no live-execution mode at all, by design — the daemon's own routing enum has exactly one dispatchable variant and it is `ProposeDispatch` (`loom-daemon/src/capability.rs`). Concretely, the phase that runs for such an item MUST:

- Produce **the exact commands** an operator would run (a fenced block, copy-pasteable, with real values substituted — not a sketch), **or** a PR containing the mechanical change, whichever fits the item.
- Post that proposal as a comment (or the PR body) and **stop**. The item stays `loom:operator-only` + `loom:operator-mechanical` and is not closed; an operator's approval and execution is the next step.
- **Never** execute against a credential, run the proposed commands, `sudo`, mutate cloud/infra state, or use an admin-scoped token — even though gate 1 asserts the worker *holds* the capability. Holding it is what makes the proposal accurate; it is not permission to use it.

Adding a live-execution mode is out of scope here and requires its own explicit, separate opt-in — file it as its own issue rather than extending this section.

### Conservative and auditable (AC3)

Three rules, all mandatory, all reported in the proposal itself:

- **Report what was done.** Every action taken under this lane is named in the comment/PR body — what was read, what is proposed, and what was deliberately not touched. An operator reading only the comment must be able to reconstruct the whole pass.
- **Never widen scope.** Do exactly the item's stated task. A neighbouring cleanup, a "while I'm here" fix, or a second mechanical step the item did not ask for is out of bounds — note it and file a separate issue via `./.loom/scripts/create-issue.sh` instead.
- **Hard-stop on any judgement call.** The moment anything encountered requires a *ruling* rather than mechanical execution — an ambiguous target, a choice between defensible options, a risk to accept on someone's behalf, a value nobody has specified — **stop immediately**, post what was found and why it is a judgement call, and relabel:
  ```bash
  gh issue edit N --remove-label "loom:operator-mechanical" --add-label "loom:operator-decision"
  ```
  (or `loom:operator-objective` when the blocker is a missing objective rather than an authority call — see the "classifying question" table in `.loom/docs/label-state-machine.md`). The relabel is what makes the stop durable: the item is no longer eligible for this lane on any subsequent pass, by gate 2. **Do not** finish the mechanical part first and flag the judgement afterwards — a partially-completed mechanical action plus an open question is exactly the state this rule exists to prevent.

### Scope

This lane applies under the `all` sentinel's "Aggressive candidate taxonomy" (issue-set path) and Mode C's C0 per-PR pre-flight, which are the two surfaces that resolve items regardless of label. **Mode A/B's conservative pre-flight skip rules are unchanged** (Wave Lifecycle step 1 still hard-skips `loom:operator-only` outright) — an explicitly-named issue in Mode A is not a whole-backlog routing decision, and narrowing the blast radius of a first rollout is worth more than uniformity here.

## Operator-gate advisory scan (body-phrase + title-prefix detection, #5137, extended #6391)

The `loom:operator-only` and `loom:needs-capability` **labels** are the hard exclusions the "Aggressive candidate taxonomy" table enforces for the `all` sentinel. In practice, some issues declare operator-gating in **body text** without ever getting either label — "this acquisition step is operator-gated", "Operator decision: hold for credentials", "requires operator authorization" — so the aggressive whole-backlog taxonomy would otherwise silently plan them for automated build. Some declare it in their **title** instead — a title starting with `Operator:` (e.g. "Operator: visit the county archive and photograph the 1850 census page") whose body contains none of the phrase vocabulary below; a real 2026-08-17 sweep run missed exactly this shape (#6391). This section adds **advisory-only** signals layered on top of the label check: a scan implemented by `./.loom/scripts/warn-operator-gated.sh --candidates "<resolved candidate numbers>"` (structured like the sibling `warn-out-of-set-deps.sh`, #3747 v2 item 4 — detect-and-warn, never mutates, dedups per candidate), run in Stage 0 step 1c over the same `body` the survey already reads for "Overlap-aware wave partitioning" and `--auto-stack` edge detection — **no extra survey read** — plus one extra `--json title` read per candidate for the title-prefix signal (#6391). It runs **unconditionally for every `all`-sentinel run** (it does not require `--auto-stack`).

**Advisory only — never a hard skip, never a label mutation, never blocking.** Unlike the `loom:operator-only` / `loom:needs-capability` label rows (a hard `Skip`), a body-text or title-prefix match here changes nothing about the candidate's planned action: it still shows its normal `would build` (or whatever the taxonomy routing already decided) — annotated with a `⚠` warning so the **operator** can decide at the `all` sentinel's mandatory confirmation gate. Nothing here removes a label, closes an issue, or skips a candidate — only a human decision (or a follow-up `loom:operator-only`/`loom:needs-capability` label applied by a human) does that. The script always exits `0`.

**1. Phrase scan — instruction-shaped fragments, not bare substrings.** Case-insensitive match against `body`, mirroring the `loom:blocked` row's hold/defer-phrase discipline (#4505 — instruction-shaped fragments, not a naive substring search) rather than flagging on a bare word that appears constantly in ordinary engineering prose (a bare `operator` or `credentials` substring would false-positive on "operator precedence" or "rotate credentials in CI"). The vocabulary (the script's `PHRASES` array, matched in declared order so the annotation is deterministic):

- `operator-gated`
- `operator-only in substance`
- `operator authorization required`
- `operator decision:`
- `operator task` (#6198 — the framing agents most often open an operator-task body with: "**Operator task — requires human action, not automation.**")
- `requires operator` (catches "requires operator authorization", "requires operator input", "requires operator action", …)
- `requires human action` (#6198 — `requires operator` misses this by one word; deliberately **not** the bare word `human`, which would false-positive on "the human-in-the-loop design")
- `needs human action` (same narrowing)
- `login-walled`
- `paid gpu`
- `requires credentials` (deliberately **not** the bare word `credentials` alone — see the false-positive rationale above)
- `needs credentials` (same narrowing)

A candidate whose body matches ≥1 phrase is flagged with the **first** matched vocabulary phrase (the phrase itself, quoted verbatim — e.g. a body "acquisition is operator-gated" yields `"operator-gated"`), dedup to one phrase annotation per candidate even when multiple phrases match.

**2. Dependency-declared operator-gating (the `#87 → #4` shape).** Parse the same `body` for a `Depends on #A` / `Requires #A` reference, reusing the exact `(Depends on|Requires)[*_:[:space:]]*#[0-9]+` vocabulary the `--auto-stack` edge-detection pass and `warn-out-of-set-deps.sh` already use (see "Auto-stack detection and wave ordering" / "Out-of-set dependency detect-and-warn") — deliberately excluding `Blocked by` (that phrase drives the distinct `loom:blocked` machinery) — but run this lookup **unconditionally**, independent of whether `--auto-stack` was passed. This check only *reads* the reference to look up `#A`'s labels; it never populates `DEPENDS_ON[N]` or creates a stacking edge, so it is exempt from the `--auto-stack` flag gating that scopes actual stack *topology* (#3729 kept that opt-in). If any declared `#A` currently carries the `loom:operator-only` **or** `loom:needs-capability` **label**, flag the **child**: its declared dependency cannot itself be completed by automation, so building the child now would build against a base nobody can finish — exactly the `#87 → #4` shape from #5137 (the sweep skips `#4` as operator-only, then dispatches `#87` which needs it), extended in #5817 to the same shape when `#4` instead carries `loom:needs-capability`. Unlike `--auto-stack`'s same-candidate-set edge rule, this is a plain label lookup on whatever issue number the body names, **in-set or not** — there is no stacking edge being created.

**3. Title-prefix scan — a distinct signal from the body-phrase vocabulary (#6391).** Independently of `body`, the script also reads the candidate's `title` (`gh issue view N --json title`) and checks whether it — after stripping leading whitespace and markdown decoration (heading `#`, emphasis `*`/`_`, blockquote `>`, list marker `-`) — **starts with** one of `Operator:` / `Operator —` / `Operator-only` (case-insensitive). This is a **prefix** match, not a substring-anywhere match — deliberately narrower than the body-phrase discipline: a title that merely *mentions* "operator" mid-sentence ("Fix operator dashboard rendering") must not match, only a title that opens by declaring operator-gating up front. It is checked independently of signal 1 and fires even when the body is empty or matches none of the `PHRASES` vocabulary — the exact gap that let #6391's title-prefixed, unlabeled issue through the confirmation gate unflagged.

**4. Surfacing — inline annotation plus a summary block.** The script emits one tab-separated line per matching candidate per signal (`<N>\t⚠ body declares operator-gating: "<phrase>"`, `<N>\t⚠ depends on #A, which is loom:operator-only` — or `loom:needs-capability`, whichever label `#A` actually carries — or `<N>\t⚠ title declares operator-gating: "<prefix>"`); a candidate matching more than one signal emits one line per signal. Sweep renders those matches two ways in the `all` sentinel's mandatory gate (and the `--dry-run` plan, which shares the same listing format):

- **Inline `⚠` suffix** appended to the matching candidate's own row, after its normal planned action (see "Per-candidate fields"):
  ```
  #87  "Acquire login-walled census index"  labels: loom:issue  → would build  ⚠ body declares operator-gating: "operator-gated"
  #87  "Acquire login-walled census index"  labels: loom:issue  → would build  ⚠ depends on #4, which is loom:operator-only
  #94  "Operator: photograph the 1850 census page"  labels: loom:issue  → would build  ⚠ title declares operator-gating: "Operator:"
  ```
- **A summary `Operator-gate advisory` block** above the wave listing when ≥1 candidate matched (see "Operator-gate advisory block"), one line per match, so the operator sees every flag in one place before scrolling the wave grouping.

The operator reads the annotation and decides — hold, `--depends-on`, manual dispatch, or proceed anyway; the sentinel never decides for them.

**5. Zero matches ⇒ byte-for-byte unchanged.** When no candidate's body matches any phrase, no candidate's title matches a declared prefix, and no candidate declares a `loom:operator-only` or `loom:needs-capability` dependency, the script emits zero lines, no `⚠` suffix and no summary block are printed anywhere, and the candidate-set / `--dry-run` output is identical to a run with this scan absent — no new "found nothing" line, exactly like the "Overlap analysis" and "Detected stacking pairs" blocks on a zero-match run.

**6. `--dry-run` composes.** The `--dry-run` plan ("Issue-set output spec") shows the same `⚠` annotations and summary block the confirmation gate does — this is a **read-only** scan (a body regex, a title-prefix check, plus label reads), so it runs identically whether or not `--dry-run` was supplied; nothing here is gated behind the dry-run/mutation boundary the way the orphaned-claim recovery pass is.

**What this scan does NOT catch (#6197) — a zero-match result is not an all-clear.** The signals above are phrase-based (item 1), label-based (item 2), and title-prefix-based (item 3); all three are good at catching "a human must *do* something first" — supply a credential, provision hardware, grant access, visit a physical location. None can catch **decision-shaped acceptance criteria**, where the gap lives in the *semantics* of the body text rather than in any fixed vocabulary — e.g. "a shortlist agreed from the above rather than all of it — this is a menu, not a backlog" names no operator-gating phrase, no `loom:operator-only`/`loom:needs-capability` dependency, and no `Operator:`-prefixed title, yet only a human can satisfy it; a Builder can either implement everything (violating the criterion) or guess. The scan also cannot catch **credential/verification-shaped criteria** that require a check only a human (or a live secret a Builder worktree doesn't have, e.g. a gitignored `.dev.vars` key) can perform, such as "a **verified** Amazon browse node" — a Builder that cannot verify can still emit a plausible value and mark the criterion done, which is a worse failure mode than a wasted build. Do not extend the `PHRASES` vocabulary to chase this class (see item 1's false-positive rationale — "agreed" or "decide" would false-positive on ordinary prose while still missing the next phrasing); this is a semantic gap, not a missing phrase. The mandatory confirmation gate and the **operator's own judgement** remain the intended backstop for both cases — the same gate this scan's `⚠` annotations feed (see "Mandatory confirmation gate" above) is where a human reads the full candidate body and catches what no scan can. This scan narrowing its own claim composes with, and does not replace, that judgement.

**Scope.** This scan runs only under the `all` sentinel's aggressive candidate survey (`SWEEP_ALL_AGGRESSIVE=true`, issue-set path) — the incident it closes (#5137) is specific to the aggressive whole-backlog taxonomy's hard-skip exclusions. Mode B's curated NL-filtered candidate sets and Mode C's PR-set path do not run it (a PR's own `loom:operator-only` / `loom:needs-capability` label check, C0, is unchanged).

## 0. Dry-run gate (if `--dry-run`)

If `--dry-run` was supplied, **this stage runs before any mutation** and EXITs after printing the plan. The dry-run gate is the single inviolable contract of `--dry-run`: no label edits, no `worktree.sh` invocation, no `gh pr create`, no `merge-pr.sh`, no daemon-state writes, no Task/subagent dispatch. This contract is uniform across Modes A, B, and C.

### Procedure — Modes A and B (issue-set)

1. **Survey each candidate (read-only).** For every deduplicated, validated issue number `N` in the candidate list:
   ```bash
   "$GH_READ" issue view N --json number,title,labels,state --jq '{number, title, state, labels: [.labels[].name]}'
   ```
   This is a `gh issue view` read — it does not mutate anything. It runs through the cached-read wrapper (see "Cached forge reads (`gh-cached`)"): a dry-run survey is pure observation whose output is a printed plan, never a claim, so 30s of staleness costs nothing. The **live** path's per-issue pre-flight (step 1 of the Wave Lifecycle) deliberately does *not* use the wrapper. (If `gh` is unauthenticated or the issue is unreachable, log the error against that candidate and continue surveying the rest.)

   **Add `body` to this read unconditionally** (`gh issue view N --json number,title,labels,state,body ...`) — one extra `--json` field, **no extra API call** — and parse its `## Affected Files` section into the candidate's estimated file surface per "Overlap-aware wave partitioning" step 1. A missing / "To be determined" section leaves the surface *unknown* (that candidate is excluded from overlap analysis; never blocked). The `body` fetched here also feeds the `--auto-stack` edge-detection pass when that flag is set (see below), so it is read once and used for both. **Under the `all` sentinel it also feeds the "Operator-gate advisory scan"** (step 1c below — phrase scan + operator-only-dependency check) — same read, no extra survey call, and independent of `--auto-stack`.

   **When `AUTO_STACK=true`, run the edge-detection pass** described in "Auto-stack detection and wave ordering (`--auto-stack`, #3759)" over the same `body`. Absent `--auto-stack`, no stacking detection runs — the `body` read still feeds overlap surface estimation only, and **no stacking edge is ever created from file overlap** (scheduling signal only, #3729).

1a. **Resolve stacking edges (only when `AUTO_STACK=true`).** Detect `Depends on #A` / `Requires #A` edges, keep only those whose `#A` is a member of this candidate set, reduce to a single parent per child (first-match-wins), drop cyclic edges — all per "Auto-stack detection and wave ordering". Populate the per-issue `DEPENDS_ON[N]` map. When zero edges survive, the run proceeds exactly as if `--auto-stack` were absent.

1b. **Warn on out-of-set dependency references (unconditional, Modes A/B).** Run the detect-and-warn pass described in "Out-of-set dependency detect-and-warn (v2 item 4, #3747)": `./.loom/scripts/warn-out-of-set-deps.sh --candidates "<resolved candidate numbers>" --depends-on "<operator --depends-on values, if any>"`. For each candidate whose body declares `Depends on`/`Requires`/`Part of #A` where `#A` is **open**, **not** in this sweep's candidate set, and **not** covered by an operator `--depends-on`, it emits a non-blocking advisory warning (stderr/log; also surfaced in the candidate-set preview in interactive/Mode B contexts). This runs regardless of `--auto-stack` — it never modifies the candidate set (detection + advisory only) and never blocks the sweep. In the `--dry-run` plan the warnings are printed above the wave listing.

1c. **Scan for operator-gated candidates (only when `SWEEP_ALL_AGGRESSIVE=true`, #5137, extended #6391).** Run `./.loom/scripts/warn-operator-gated.sh --candidates "<resolved candidate numbers>"` (see "Operator-gate advisory scan" above) over the same `body` read in step 1, plus its own per-candidate `title` read for the title-prefix signal. Record each matching candidate's annotation line(s) for step 3's plan output. Advisory only — never modifies the candidate set, never changes a planned action, never blocks. Absent the `all` sentinel (Mode B's own curated candidate set), this step does not run.

2. **Compute wave partition.** Partition the candidate list into waves of size `--builders-per-wave`, or the Stage -1 resolved auto wave size when the flag was omitted (see "Resolve auto wave size"), preserving input order. Record `(issue, wave_index, total_waves)` for each candidate. Apply the same silent-clamp and pre-flight-skip rules that the live path uses (closed / `loom:building` / `loom:blocked` issues are tagged as "would skip" in the plan but still appear in the output for transparency). **When stacking edges were resolved in step 1a, first reorder** so every parent's wave is at or before its child's wave (a parent/child pair may share a wave — the child still branches off the parent's branch, not the shared pre-wave `main` snapshot) per "Auto-stack detection and wave ordering", then partition the reordered list.

2a. **Adjust the partition for file-surface overlap.** After the (possibly auto-stack-reordered) partition is computed, run the overlap adjustment in "Overlap-aware wave partitioning" step 2: detect same-wave pairs whose estimated `## Affected Files` surfaces share ≥1 path (excluding pairs already related via `DEPENDS_ON[N]` and any candidate with an unknown surface), and greedily reorder to separate them without breaking parent-before-child ordering. Record the resulting wave moves and any unavoidable-overlap groups for the plan output.

3. **Print the plan.** Emit a table or block per the issue-set format below, including the `Overlap analysis` block when any overlap was detected and the `Operator-gate advisory` block (only under the `all` sentinel) when step 1c found any match.

4. **EXIT.** Do not proceed to "Wave Lifecycle". The shell must return as soon as the plan is printed.

**Issue-set output spec** (Modes A and B; minimum useful — do **not** add token-pool selection or agent dispatch internals):

```
/loom:sweep --dry-run plan: M candidate(s) across W wave(s) (wave size 10, auto; mechanism=daemon detached-process)
  Wave sizing: daemon + multi-account pool → detached-process path (target 10)

  Wave 1:
    #123  "Add foo widget"                labels: loom:issue                    → would build
    #124  "Fix bar bug"                   labels: loom:curated                  → would curate, build
    #199  "Tweak gizmo"                   labels: loom:issue                    → would route to Judge (existing PR #200 in flight)
  Wave 2:
    #125  "Refactor baz module"           labels: loom:building                 → would skip (already in flight)
    #126  "Document quux"                 labels: (none)                        → would curate, build
    #198  "Polish frobnicator"            labels: loom:issue                    → would merge (existing PR #201 already loom:pr)

Total: 3 would-build, 1 would-route-to-judge, 1 would-merge, 1 would-skip. No issues were modified.
```

When `--builders-per-wave` was passed explicitly, the header shows the number without `auto` and the "Wave sizing" line reads `explicit --builders-per-wave=N` (no mechanism/disk reason). A disk- or candidate-clamped auto run reads e.g. `(wave size 3, auto; mechanism=in-session subagent)` with `Wave sizing: reduced to 3 (only 6 GB free on /Volumes/scratch/loom)`.

**Per-candidate fields (required):**
- Issue number
- Title (truncated reasonably if very long)
- Current labels (comma-separated, or `(none)`)
- Planned action (`would build`, `would curate, build`, `would skip (<reason>)`, `would route to Judge (existing PR #X in flight)`, `would merge (existing PR #X already loom:pr)`, `would skip (PR #X held by loom:operator)` — the linked PR is `loom:pr` but also carries `loom:operator`, per #6398). Under the `all` sentinel (`SWEEP_ALL_AGGRESSIVE=true`) the aggressive actions also appear: `would reclaim (stale loom:building), build`, `would unblock (#N merged), build`, `would skip (still blocked by #N)`, `would skip (explicit hold: "<phrase>")`, `would expand epic (→ #a #b)`, `would skip (needs decomposition)`, `would reclaim (stale loom:abort), build`, `would skip (abort flag set)`, `would skip (operator-only)`, `would skip (needs-capability)`, `would propose (mechanical lane, holds: <caps>)`, `would skip (mechanical: missing capability <name>)` (#6893).
- Wave assignment (shown via the `Wave N:` group header)
- **Operator-gate annotation (only under the `all` sentinel, appended, never replacing the planned action — #5137)**: when step 1c matched, append `⚠ body declares operator-gating: "<phrase>"` and/or `⚠ depends on #A, which is loom:operator-only` (or `loom:needs-capability`) after the planned action, one per matched signal. No match → no suffix, row unchanged from today.

**Header/footer (required):** the header states the resolved wave size (and whether it is `auto` or explicit), the chosen **mechanism** (`daemon detached-process` vs `in-session subagent`), and — on the second line — the one-line **gating reason** from "Resolve auto wave size". The footer states total candidates, total waves, count of `would-build` vs `would-skip`, and an explicit confirmation that nothing was modified. (Dry-run resolves the auto wave size via the same Stage -1 helper but performs no dispatch — it prints the plan and EXITs.)

**Detected stacking pairs block (only when `AUTO_STACK=true` and ≥1 edge survived).** When auto-stack resolved at least one in-set edge, print a `Detected stacking pairs:` block above the wave listing, one line per honored edge, naming the child, its declared dependency phrase, and the parent it will stack on:

```
Detected stacking pairs (--auto-stack):
  #125 "Fix Y"  — Depends on #124 (in this sweep's candidate set) → will stack on #124's branch (feature/issue-124)
  #126 "Add Z"  — Requires #125 (in this sweep's candidate set) → will stack on #125's branch (feature/issue-125)
```

Each stacked child's per-candidate action then reads e.g. `→ would build (stacked on #124)` and the wave grouping reflects the parent-before-child ordering. When `--auto-stack` was passed but **zero** edges survived (no in-set `Depends on`, or every candidate independent), print **no** stacking block — the plan is identical to a run without the flag. Dropped edges (a second in-set parent on the same child, or a cycle) are surfaced as one-line warnings above the block (e.g. `WARNING: #127 declares multiple in-set parents (#124, #125) — honoring #124 only (single-parent edges)` / `WARNING: dropped cyclic stacking edges among #128 #129 — building independently`).

**Overlap analysis block (only when ≥1 same-wave surface overlap was detected, #4161).** When step 2a found candidates whose estimated `## Affected Files` surfaces overlap, print an `Overlap analysis` block above the wave listing: one line per overlapping group naming the shared file(s) and the candidates, the wave move applied (if any), and an explicit `UNAVOIDABLE` marker + warning when the group could not be separated. Candidates with an unknown surface (no `## Affected Files`) are never listed here — the analysis only reasons about parseable surfaces.

```
Overlap analysis (file-surface scheduling, #4161):
  #38, #37 share `install.sh` — moved #37 to wave 2 (separated)
  #36, #38, #39 share `hooks/repo/tests/run.sh` — only 2 waves for 3 overlappers → UNAVOIDABLE
  WARNING: unavoidable same-file overlap on `hooks/repo/tests/run.sh` among #36 #38 #39
           — sibling PRs will report CLEAN until the first merges, then conflict.
           Reorder manually or re-run with --builders-per-wave 1. Step 7 revalidation
           is the reactive fallback (an extra Doctor rebase per collision).
```

When every overlapping group was separated by the reorder, print the moves without a `WARNING:` line. When no surfaces overlap (or every candidate's surface is unknown), print **no** overlap block — the plan is byte-for-byte identical to a run with no `## Affected Files` data. **No stacking edges are ever created here** — overlap is a scheduling signal only (#3729).

**Operator-gate advisory block (only under the `all` sentinel, and only when ≥1 candidate matched, #5137, extended #6391).** When step 1c's `warn-operator-gated.sh` pass matched at least one candidate, print an `Operator-gate advisory` block above the wave listing: one line per matching candidate per signal, naming the candidate, the matched phrase, dependency, or title prefix, and a pointer to that candidate's row in the listing below (which also carries the same `⚠` suffix — see "Per-candidate fields"):

```
Operator-gate advisory (body-text + title scan, #5137/#6391):
  #87 declares "operator-gated" — body: "the index is login-walled, so acquisition is operator-gated"
  #87 depends on #4, which is loom:operator-only — sweep will skip #4 and dispatch #87 anyway
  #65 declares "operator decision:" — body: "Operator decision: send this paired with the paper..."
  #94 title declares "Operator:" — title: "Operator: photograph the 1850 census page"
  ADVISORY ONLY — no candidate above was skipped, re-routed, or relabeled because of this block.
  Review before confirming; hold, --depends-on, or dispatch manually as appropriate.
```

Absent the `all` sentinel, this block never appears (Mode B/C do not run the scan — see "Operator-gate advisory scan"). Under the `all` sentinel with **zero** matches, print **no** block at all — the plan is byte-for-byte identical to a run made before this scan existed, matching the same "no block when clean" contract the `Overlap analysis` and `Detected stacking pairs` blocks already honor. **No candidate's planned action, label, or wave assignment is ever changed by this block** — advisory only, per "Operator-gate advisory scan" above.

### Procedure — Mode C (PR-set)

1. **Survey each PR candidate (read-only).** For every deduplicated, validated PR number `P` in the candidate list:
   ```bash
   "$GH_READ" pr view P --json number,title,labels,state --jq '{number, title, state, labels: [.labels[].name]}'
   ```
   This is a `gh pr view` read — it does not mutate anything. Cached, for the same reason as the Modes A/B survey above; Mode C's **live** C0 pre-flight is deliberately uncached. (If `gh` is unauthenticated or the PR is unreachable, log the error against that candidate and continue surveying the rest.)

2. **Compute wave partition.** Mode C waves are size-1 (`--builders-per-wave` is ignored). Each PR is its own wave. Record `(pr, wave_index=N, total_waves=M)` for each candidate. Apply the same skip rules the live path uses (closed PRs, multiple-label conflicts, missing required label all tagged "would skip" in the plan but still listed for transparency).

3. **Print the plan.** Emit the PR-set output spec below.

4. **EXIT.** Do not proceed to "PR-set Wave Lifecycle". The shell must return as soon as the plan is printed.

**PR-set output spec** (Mode C):

```
/loom:sweep --prs --dry-run plan: M candidate(s) across M wave(s) (PR-set mode, --builders-per-wave ignored)

  Wave 1:
    PR #200  "Add foo widget"                labels: loom:review-requested        → would Judge
  Wave 2:
    PR #201  "Fix bar bug"                   labels: loom:changes-requested       → would Doctor → Judge (cycle 1/max_doctor_cycles)
  Wave 3:
    PR #202  "Refactor baz"                  labels: loom:pr                      → would merge (via merge-pr.sh --auto)
  Wave 4:
    PR #203  "Polish frobnicator"            labels: (none)                       → would skip (no actionable label)
  Wave 5:
    PR #204  "Document quux"                 state: MERGED                        → would skip (PR already merged)

Total: 1 would-judge, 1 would-doctor-then-judge, 1 would-merge, 2 would-skip. No PRs were modified.
```

**Per-PR fields (required):**
- PR number (prefixed `PR #` to distinguish from issue numbers)
- Title (truncated reasonably if very long)
- Current labels (comma-separated, or `(none)`)
- Planned action (`would Judge`, `would Doctor → Judge (cycle 1/max_doctor_cycles)`, `would merge (via merge-pr.sh --auto)`, `would skip (<reason>)`). The `cycle 1/N` form substitutes the resolved `sweep.max_doctor_cycles` value for `N` (default 1).
- Wave assignment (one PR per wave; shown via the `Wave N:` group header)

**Footer (required):** total candidates, total waves, count of `would-judge` / `would-doctor-then-judge` / `would-merge` / `would-skip`, and an explicit confirmation that nothing was modified.

**Mode C skip reasons** (action column should clearly state which applies):
- `would skip (no actionable label)` — PR has neither `loom:review-requested`, `loom:changes-requested`, nor `loom:pr`.
- `would skip (PR already merged)` — `gh pr view` reports `state: MERGED`.
- `would skip (PR closed without merge)` — `state: CLOSED` (non-merged).
- `would skip (loom:blocked)` — PR carries `loom:blocked` (do not act on operator-flagged PRs).
- `would skip (multiple actionable labels)` — PR carries two or more of `{loom:review-requested, loom:changes-requested, loom:pr}` simultaneously (human-attention case — which transition is canonical?).
- `would skip (loom:operator)` — PR is `loom:pr` but also carries `loom:operator` (Champion's merge-risk hold, or another operator-applied hold); C1c does not route it to Merge (#6398).

### Out of scope for dry-run output (all modes)

**Explicitly out of scope for dry-run output** (do not add these — see Limitations):
- Token-pool / account selection internals
- Subagent dispatch order or parallelism counts beyond wave size
- Persisting the plan to disk
- Diffing this plan against a previous or actual sweep

**Verifying "nothing mutates":**

```bash
# EVERY `gh` read below is plain `gh` — NEVER "$GH_READ". This is a
# before/after differential check: the identical command runs twice around the
# operation under test, so a cache hit on the "after" read would replay the
# "before" value and make the check pass vacuously (#4667).
# Before:
LABELS_BEFORE=$(gh pr view P --json labels --jq '[.labels[].name]|sort')   # Mode C
ISSUE_LABELS_BEFORE=$(gh issue view N --json labels --jq '[.labels[].name]|sort')  # Modes A/B
PRS_BEFORE=$(gh pr list --state open --json number --jq '[.[].number]|sort')
WORKTREES_BEFORE=$(ls .loom/worktrees/ 2>/dev/null | wc -l)
# Run: /loom:sweep --dry-run ...   (any mode)
# All three (or four, for Mode C) must be unchanged after the dry-run returns.
```

These checks — label set per candidate (issue or PR), open PR set, worktree count — are the acceptance criteria. If any of them differ pre/post a `--dry-run` invocation, the dry-run gate is broken.

## PR-set Wave Lifecycle (Mode C only)

If Mode C was selected, the wave lifecycle is the **back half** of the issue-side lifecycle: **no Curator, no Approval gate, no Builder**. Each PR is routed by its current label to Judge, Doctor→Judge, or Merge directly.

> **Stage skip is explicit and load-bearing for Mode C.** The issue-side "MANDATORY: do not skip any stage" rule applies to the **issue** lifecycle. For an existing open PR, the Curator and Builder stages already ran (the PR exists, so the issue was implemented). Re-running them would be incorrect and wasteful. Mode C's wave lifecycle is the symmetric counterpart that handles the post-Builder phases without touching the front half.

For each PR `P` in the candidate list, processed sequentially one PR per wave (size-1 waves):

### C0. Per-PR pre-flight (before any role dispatch)

```bash
# Plain `gh` — NOT "$GH_READ". This read routes the PR to Judge / Doctor /
# Merge, so it must observe a concurrent Judge's or Champion's just-written
# label. See "Cached forge reads (`gh-cached`)" for the uncached carve-outs.
gh pr view P --json number,state,labels,closingIssuesReferences \
  --jq '{number, state, labels: [.labels[].name], closes: [.closingIssuesReferences[].number]}'
```

Apply the following skip rules (each "skip" logs the reason; the PR does NOT contribute to any further phase; advance to the next PR):

| Condition | Action | Reason |
|-----------|--------|--------|
| `state != OPEN` (MERGED or CLOSED) | skip | PR is not open; nothing to do |
| Has `loom:blocked` | skip | Operator-flagged; do not act |
| Has none of `{loom:review-requested, loom:changes-requested, loom:pr}` | skip | No actionable label — Mode C only handles these three states |
| Has two or more of `{loom:review-requested, loom:changes-requested, loom:pr}` simultaneously | skip | Conflicting state; human-attention case |
| Has `loom:operator-only` **and** `loom:operator-mechanical`, and this worker holds **every** capability the PR body's `<!-- loom:capability=<name> -->` marker(s) declare | **do not skip** | Capability-aware mechanical lane (#6893). Route through the normal label branch (C1a/C1b/C1c) but in **propose mode**: the phase produces the exact commands / a PR description for an operator to approve, never a live credentialed action. See "Capability-aware `loom:operator-mechanical` lane" |
| Has `loom:operator-only` **and** `loom:operator-mechanical`, but a declared capability is **not** held | skip (comment first) | Post the capability request naming what is missing (lane section below), then log `PR #P: skip — mechanical, missing capability <name>` |
| Has `loom:operator-only` (any other case) | skip | Operator-only PR; do not act. Unchanged — the two rows above are the only carve-out, and they fail closed |
| Has `loom:needs-capability` | skip | Blocked on a missing tool/agent capability, not operator-by-right (#5817); do not act. Explicitly **not** part of the capability lane |

**`loom:reviewing`/`loom:treating` are claim *overlays*, not one of the three state labels the "two or more" conflict-skip row above counts (Issue #6167).** A PR carrying `loom:review-requested` **and** `loom:reviewing` together (a Judge has claimed it and is mid-review — or died mid-review) still has exactly **one** of `{loom:review-requested, loom:changes-requested, loom:pr}`, so it does not hit the conflict-skip row and routes normally to **C1a**. A *stale* `loom:reviewing` next to an actionable state label is therefore **recoverable, not a human-attention case**:

- judge.md's own "Stale `loom:reviewing` Claim Check" (Step 2, before claiming in C1a) reclaims it inline the moment a Judge is actually dispatched for that PR.
- The sweep-start orphan-recovery pass (`recover-orphaned-shepherds.sh --recover` under the `all` sentinel — see "Build-everything sentinel" below) now also reclaims stale `loom:reviewing`/`loom:treating` claims proactively, across the whole PR set, before any PR-specific Judge/Doctor is even dispatched — closing the gap where a dead Judge's claim on a PR nobody happens to re-visit could otherwise sit unrecovered indefinitely (observed on kicad-tools #4791/#4792, ~36h stale). Doctor's `loom:treating` claim label is the identical overlay for `loom:changes-requested`/C1b and is handled the same way.

Determine the **closing issue number** (used for checkpoint scope below) from `closingIssuesReferences`. This is the GitHub-native `Closes/Fixes/Resolves #N` parser (matches the convention used by the issue-side pre-flight via `closedByPullRequestsReferences`). Record up to one closing issue number per PR:

- **0 closing issues** → no checkpoint scope for this PR. Log a warning at PR start (`PR #P lacks a Closes #N reference; skipping per-issue checkpoint for this PR`) and proceed without checkpointing. Mid-phase resume after a kill will not be available for this PR — Judge / Doctor / Merge will simply re-run from scratch on the next sweep, which is acceptable since the operations are idempotent at the GitHub-state level (Judge re-runs if `loom:review-requested` is still set; Merge re-runs only if the PR is still open and labeled `loom:pr`).
- **1 closing issue** → use that issue number `N` as the checkpoint key. The existing `./.loom/scripts/sweep-checkpoint.sh` is keyed by issue number (#3373) and is reused as-is. **Read the existing checkpoint** before dispatching Judge:
  ```bash
  CHECKPOINT_PHASE=$(./.loom/scripts/sweep-checkpoint.sh phase N)
  ```
  If `CHECKPOINT_PHASE == "merge-done"`, the closing issue was already merged in a previous sweep — skip this PR with `already merged (per checkpoint)` and delete the stale checkpoint.
- **2 or more closing issues** → log all closing issue numbers and skip checkpointing (multi-closing PRs are uncommon; a follow-up issue can add a multi-key checkpoint variant if needed). Proceed with Judge/Doctor/Merge as normal.

### C1. Per-PR routing by current label

Apply exactly one of the three branches below, based on the PR's current label:

#### C1a. `loom:review-requested` → Judge phase only

- Load and follow the instructions in `.claude/commands/loom/judge.md` for this PR.
- Dispatch `loom-judge` as a **single subagent Task** from this orchestrator session. Do **NOT** invoke `/loom:sweep` or `/loom:judge` slash-commands as subagents — see "CRITICAL: One level deep" in the Execution Model.
- If a previous Judge attempt for this PR died mid-flight without a fresh checkpoint (rate limit, crash), re-verify forge state and complete only the missing steps before re-dispatching — see "Mid-phase-death recovery" in the Wave Lifecycle (the rule is phase-generic; Mode C inherits it, same as the Doctor-cycle cap).
- Expected exit states:
  - **Approve** → PR labeled `loom:pr` by Judge. If a closing-issue checkpoint is in scope, write `judge-done`:
    ```bash
    # Append --model <resolved> when you passed a model param to the judge subagent (#3482).
    ./.loom/scripts/sweep-checkpoint.sh write N judge-done --task-id "$RUN_ID" --pr-number P
    ```
    Continue to **C2 (Merge)** for this PR.
  - **Request changes** → PR labeled `loom:changes-requested` by Judge. If a closing-issue checkpoint is in scope, write `judge-rejected` **before** entering C1b, so an interrupted sweep resumes at Doctor rather than repeating this completed Judge pass:
    ```bash
    ./.loom/scripts/sweep-checkpoint.sh write N judge-rejected --task-id "$RUN_ID" --pr-number P
    ```
    Continue to **C1b (Doctor → Judge)** for this PR (inline Doctor → Judge cycle(s), up to `sweep.max_doctor_cycles`, matching the issue-side cap).

#### C1b. `loom:changes-requested` → inline Doctor → Judge (up to `sweep.max_doctor_cycles` cycles)

If the PR entered the wave already labeled `loom:changes-requested` (e.g., from a previous Judge run), or just transitioned there from C1a, run inline Doctor → Judge cycles for this PR — **up to `sweep.max_doctor_cycles`** (default 1; see "Doctor-cycle cap" in the Execution Model):

- Load and follow the instructions in `.claude/commands/loom/doctor.md` for this PR.
- Dispatch `loom-doctor` as a **single subagent Task** from this orchestrator session. Do **NOT** invoke `/loom:sweep` or `/loom:doctor` slash-commands as subagents — see "CRITICAL: One level deep".
- If a previous Doctor attempt for this PR died mid-flight without a fresh `doctor-done` checkpoint (rate limit, crash), re-verify forge state (pushed commit? already re-labeled `loom:review-requested`?) and complete only the missing steps rather than duplicating the pushed fix — see "Mid-phase-death recovery" in the Wave Lifecycle (inherited here, same as the Doctor-cycle cap).
- **Model escalation (#3481)**: Mode C inherits the issue-side rule unchanged — this Doctor is dispatched because of a `loom:changes-requested` rejection, so resolve its model per "Model escalation on Judge rejection" in the Execution Model: pass `ladder[1]` from `sweep.escalation` (default ladder: `opus`, resolved through `resolve-model.sh` to `claude-opus-5` — #3982) via the Task tool's `model` parameter, **unless** a tier-1/tier-2 pin applies (pins win) or escalation is disabled (`[]`/`false`). The pinned ID degrades to its alias on this Task-tool dispatch — run it through `resolve-model.sh --task-alias` (see "Pinned-ID degradation on Task-tool dispatch", #4282).
- Doctor addresses the judge feedback, commits the fixes, pushes, and re-labels the PR `loom:review-requested`.
- If a closing-issue checkpoint is in scope, write `doctor-done` (with the attempt counter and the model the Doctor actually ran on — escalated or pinned, #3482) **before** the follow-up Judge:
  ```bash
  # <attempt> is the cycle index + 1: 2 for the first Doctor cycle, 3 for the second, etc.
  ./.loom/scripts/sweep-checkpoint.sh write N doctor-done --task-id "$RUN_ID" --pr-number P --attempt <attempt> --model <doctor-model>
  ```
- Re-dispatch `loom-judge` for the PR (now `loom:review-requested` again).
- Expected exit states:
  - **Approve** → PR labeled `loom:pr`. Write `judge-done` checkpoint (if in scope), continue to **C2 (Merge)**.
  - **Request changes again, cap not yet reached** (`sweep.max_doctor_cycles > 1`) → if a closing-issue checkpoint is in scope, write `judge-rejected` (with `--attempt` matching the value the **next** `doctor-done` write will use) before running the next Doctor → Judge cycle for this PR (incrementing `--attempt`), up to the configured cap:
    ```bash
    ./.loom/scripts/sweep-checkpoint.sh write N judge-rejected --task-id "$RUN_ID" --pr-number P --attempt <next-attempt>
    ```
  - **Request changes again, cap reached** → PR labeled `loom:changes-requested`. **Do NOT run another Doctor** — mark this PR as blocked (log `PR #P blocked: doctor cycle exhausted after <k> Doctor→Judge round(s); human attention required`), advance to the next PR in the candidate list. Do NOT block the rest of the candidate list on it. **Do NOT write a `judge-rejected` checkpoint for this terminal rejection** — leave the last checkpoint (`doctor-done`) as-is for the stale-checkpoint cleanup path. **Distinct-defect exception (default cap only):** when `max_doctor_cycles` is at its default of 1 and this second rejection is a demonstrably distinct defect from the first, you MAY grant exactly one additional bounded cycle (single-use per PR, log `PR #P: granted one extra Doctor cycle — second rejection is a distinct defect (<short reason>)`) — see "Doctor-cycle cap". If granted, this is a "cap not yet reached" case per the bullet above — write `judge-rejected` with the matching `--attempt` before the grace cycle. Same-defect / ambiguous still blocks (no grace, no `judge-rejected` write).

This configurable cap matches the issue-side Wave Lifecycle §6 — Mode C inherits the same rule (and the same default-cap distinct-defect exception) for the same reason (bounds worst-case latency, prevents Judge/Doctor disagreement loops).

#### C1c. `loom:pr` → Merge phase only

If the PR entered the wave already labeled `loom:pr`, skip Judge and Doctor entirely — the PR has already been judged. Continue directly to **C2 (Merge)**, subject to the two gates below.

**First, check for an operator hold (#6398).** `loom:operator` (`.loom/docs/label-state-machine.md`) means "the engine will not work this item further; a human is the only transition out" — most commonly Champion's merge-risk hold (`champion:merge-risk-hold`), posted alongside the label. The `labels` array C0 already fetched for this PR carries this — no extra call needed. If it includes `loom:operator`, **do not continue to Merge**: log `PR #P: skip — held by loom:operator (human required)` and advance to the next PR in the candidate list, leaving `loom:pr` and `loom:operator` untouched (the hold is re-evaluable, per the label-state-machine doc, so the next sweep re-checks it — but the engine itself never overrides it). This check runs regardless of the verdict-staleness outcome below; `verdict-staleness-guard.sh` clears a *stale-SHA* verdict, not an operator hold (by design — the guard explicitly does not clear `loom:operator`, `loom:operator-only`, or `loom:blocked`), so it does not substitute for this check.

**Second, confirm the approval still describes THIS tree (#5686).** "Already judged" is a claim about a specific head SHA, and a `loom:pr` label survives a rebase or force-push that replaced every commit it was rendered against. Mode C is the one merge path that does not run `champion-pr-merge.md`'s Verdict-State Janitor, so run the same gate here before skipping review:

```bash
./.loom/scripts/verdict-staleness-guard.sh P --clear
VERDICT_RC=$?
```

| Exit | Meaning | Action |
|------|---------|--------|
| `0` (FRESH) / `11` (UNVERIFIABLE, no marker — pre-#5686 verdict, fails safe) | The approval stands | Continue to **C2 (Merge)** as today. |
| `12` (STALE) | The approval covers a tree that is gone. The guard has already cleared `loom:pr`, re-queued the PR as `loom:review-requested`, and commented naming both SHAs. | **Do not merge.** Log `PR #P: stale approval cleared (head moved) — routing to Judge`, then process this PR through **C1a** (`loom:review-requested` → Judge) on this same pass. |
| `10` / anything else | No verdict label, or a `gh`/environment error | **Do not merge.** Log and skip this PR; the next sweep re-evaluates it. |

### C2. Merge (per PR)

Use the dedicated merge script (CLAUDE.md "Merging PRs" mandate — never `gh pr merge`):

```bash
./.loom/scripts/merge-pr.sh P --auto
```

The script merges via the forge API and cleans up the worktree. `--auto` enables GitHub's server-side auto-merge queue (queues the merge until required checks pass); on PRs that are already in `CLEAN` state, the script transparently falls back to an immediate merge — see #3371. **On a repo with GitHub auto-merge disabled** (`allow_auto_merge:false`), `merge-pr.sh` now detects the setting up front and degrades `--auto` gracefully to wait-for-checks-then-merge (immediate if already CLEAN) instead of failing (#3820) — so you can pass `--auto` uniformly regardless of the repo's auto-merge setting; no per-repo branching is needed here. On a repo that additionally has **no GitHub Actions workflows configured** (so the Checks API itself 404s for every SHA), the degraded wait path now distinguishes that persistent-404 shape from a transient fetch blip and proceeds straight to the synchronous merge instead of polling to `LOOM_AUTO_MERGE_TIMEOUT` (#6389) — the "pass `--auto` uniformly" guidance above still holds without qualification.

**On successful merge** (script returns 0):
- If a closing-issue checkpoint is in scope, delete it:
  ```bash
  ./.loom/scripts/sweep-checkpoint.sh delete N
  ```
- Advance to the next PR in the candidate list.

**On merge failure** (script returns non-zero):
- Classify `<reason>` through "Forge write failure diagnosis (#6425)" above **before** writing the log line — do not assert a permission/credential diagnosis without running `forge_write_permission_confirmed` and getting positive evidence. Log `PR #P merge failed: <reason>` using that section's vocabulary (`forge-transient: …`, `permission fault not confirmed — will retry`, or the confirmed-and-cited form).
- Do **NOT** delete the checkpoint — leave it at `judge-done` (or earlier) so the next sweep retries.
- Advance to the next PR in the candidate list (do not block the rest of the list).

### C3. Wave settled → advance to next PR

Mode C waves are size-1, so "wave settled" is synonymous with "this PR reached a terminal state (merged, blocked, or skipped)". Advance to the next PR in the candidate list and repeat from C0. Do not parallelize PRs (sequential per-PR processing is load-bearing — see "CRITICAL: One level deep" in the Execution Model).

### Mode C summary output

When the entire PR list has been processed, print a per-PR summary:

```
/loom:sweep --prs complete. Processed M PR(s):

  PR #200  → merged                                                                  [judged, merged]
  PR #201  → blocked (judge requested changes after doctor cycle exhausted)          [judged, doctor, judged]
  PR #202  → merged  (was already loom:pr; no judge or doctor)                       [merge-only]
  PR #205  → merged  (rate-limited (resumed: doctor TOKEN_EXHAUSTED mid-phase — fix already pushed, re-labeled + re-judged))  [judged, doctor, judged, merged]
  PR #206  → rate-limited (unresumable: judge TOKEN_EXPIRED mid-phase, human attention required)  [judged]
  PR #203  → skipped (no actionable label)                                           [pre-flight skip]
  PR #204  → skipped (PR already merged)                                             [pre-flight skip]

Total: 3 merged, 1 blocked, 2 skipped, 1 rate-limited (unresumable).
```

`rate-limited (...)` here carries the same meaning as in the issue-set Summary Output (see "`rate-limited` vs `blocked`" there): the reason reuses `TOKEN_EXPIRED` / `TOKEN_EXHAUSTED` / `MODEL_CREDITS_EXHAUSTED` from `.loom/scripts/lib/classify-error.sh`, a `resumed:` or `downgraded:` outcome already succeeded (via mid-phase-death recovery and the credit-exhaustion model fallback respectively), and only an `unresumable:` outcome needs a human — distinct from `blocked (...)`, which means the work itself failed. Mode C inherits the credit-exhaustion fallback unchanged: a Judge or Doctor killed by `MODEL_CREDITS_EXHAUSTED` at C1a/C1b is re-dispatched one rung down for the same PR, same attempt, without consuming a Doctor cycle. Mode C likewise inherits the spend-limit fallback unchanged: a Judge or Doctor killed by the `"spend limit"` signature at C1a/C1b is re-dispatched for the same PR, same attempt, with the `model` param omitted first (see "Spend-limit fallback" above), without consuming a Doctor cycle.

## Wave Lifecycle (Modes A and B only — issue-set)

For each wave `W` (partition of the issue list into chunks of up to `--builders-per-wave` candidates, processed in given order), execute the full lifecycle below. **All stages are mandatory** for every issue — do not skip any stage (CLAUDE.md "Sweep Lifecycle (MANDATORY)"). This section applies to Modes A and B only — Mode C uses the shorter "PR-set Wave Lifecycle" section above.

> **Auto-stack pre-partition pass (only when `AUTO_STACK=true`, #3759).** Before partitioning the candidate list into waves, run the detection + edge-resolution + topological-ordering pass in "Auto-stack detection and wave ordering (`--auto-stack`, #3759)": read each candidate's `body` (one added field on the per-issue `gh issue view` already issued at pre-flight step 1), resolve same-candidate-set `Depends on #A` / `Requires #A` edges into the per-issue `DEPENDS_ON[N]` map, reorder so every parent's wave is at or before its child's wave, and — if ≥1 edge survived — print the "Detected stacking pairs" block and (Mode A) prompt for confirmation. When zero edges survive (or the flag is absent), partition proceeds on the original input order exactly as today. The per-issue `DEPENDS_ON[N]` map then feeds the Builder-phase gated path below.

The numbered phases below (Curator → Builder → Judge → Doctor → Merge) are the canonical phase-by-phase reference for this skill — including the label state machine and mid-phase-death recovery procedures. Each phase step tells you which subagent to dispatch and what forge state it should leave behind.

### 0. Snapshot the main-worktree baseline (once, before wave 1) (#3648)

**Before dispatching the first wave's builders**, snapshot main's current working-tree state so the per-builder contamination backstop (step 4's `check-main-clean.sh`) can distinguish builder contamination from dirt that predated the sweep:

```bash
MAIN_CLEAN_BASELINE=".loom/sweep-checkpoint/main-clean-baseline-${RUN_ID}.txt"
./.loom/scripts/check-main-clean.sh --snapshot "$MAIN_CLEAN_BASELINE"
```

Capture this **once, before wave 1 — never per-wave**. The baseline must reflect the pre-sweep state so that if an early wave contaminates main and the dirt is not reverted, every later wave's backstop still flags it (a per-wave re-snapshot would silently absorb that contamination into the "pre-existing" set). The baseline path is **keyed by this sweep's `RUN_ID`** (`main-clean-baseline-${RUN_ID}.txt`, not a fixed `main-clean-baseline.txt`) so that a **concurrent peer `/loom:sweep` never reads or clobbers this run's baseline** (#3768): before the RUN_ID keying, a second sweep re-snapshotting the shared fixed path mid-run of the first could silently absorb real contamination into the "pre-existing" set. The path is a per-sweep-run transient under `.loom/sweep-checkpoint/` whose lifetime is this sweep invocation — enforced by `sweep-run-registry.sh cleanup "$RUN_ID"` at sweep end (Step 0a), with `loom-daemon clean` as the bulk backstop for crashed runs (#4450); do not delete it mid-sweep. `.loom/sweep-checkpoint/` is gitignored in a current install, but a consumer repo's installed loom-managed `.gitignore` block can drift and omit it — so rather than depend on the consumer's `.gitignore` being up to date, `check-main-clean.sh` also excludes `.loom/sweep-checkpoint/` (and the other Loom-owned transient state paths) internally (#3778), so a stale consumer `.gitignore` no longer false-positives the backstop on it. `check-main-clean.sh` needs no change — it already accepts an arbitrary `--snapshot FILE` / `--baseline FILE` path; only this caller-side path construction is keyed by `RUN_ID`. If the snapshot step fails for any reason, proceed anyway — step 4's backstop falls back to the whole-status hard-fail when the baseline file is missing (fail-safe, never a silent pass).

### Checkpoint-driven resume (#3373)

Sweep persists a per-issue phase checkpoint after each successful lifecycle phase so that a killed-and-relaunched sweep can pick up where it left off. The checkpoint is the **only** state required to resume — worktree preservation is handled by `worktree.sh`'s idempotency (re-running for an existing worktree is a no-op).

- **Checkpoint file**: `.loom/sweep-checkpoint/issue-<N>.json` (gitignored).
- **Schema**: `{phase: "<curator-done|builder-done|judge-rejected|judge-done|doctor-done|merge-done>", task_id, timestamp, pr_number?, attempt?, model?}`.
- **Helper**: `.loom/scripts/sweep-checkpoint.sh {write|read|phase|attempt|model|exists|delete|list}` — wraps the read/write/delete operations with atomic writes (`.tmp` + `mv`) and validates the phase enum.
- **Model field (#3482, Phase 3a observability)**: when you resolved a model for the phase's subagent (i.e., you actually passed a `model` param to the Task tool — any tier above session default), record it on the checkpoint write with `--model <resolved>` (alias or pinned ID). When the subagent inherited the session default (tier 4, no `model` param passed), omit `--model` entirely. This is observability-only bookkeeping for per-model metrics — readers MUST tolerate checkpoints without the field (legacy checkpoints predate it; absence means default/unknown), and the field never feeds back into model selection or escalation decisions.
- **Write timing**: After the *successful completion* of each lifecycle phase below. Never write a checkpoint speculatively before the phase finishes — a kill mid-phase must resume at the start of that phase.
- **Read timing**: At the start of per-issue pre-flight (step 1) for every issue in the candidate list, before any worktree or label mutation for that issue.
- **Delete timing**: On `merge-done` (step 7) and on stale-checkpoint detection (step 1).
- **Scope limit (no mid-builder recovery)**: A kill during the Builder phase resumes at *builder start* — the worktree state and partial diff survive, but sweep does not inspect the diff or attempt to resume mid-edit. This is intentional per #3372/#3373. **This assumes the killed Builder subagent is actually dead** — on some resume paths it is not; see "Task-liveness check before Builder re-dispatch (#5897)" under "Mid-builder kill semantics" below before treating an in-flight partial diff as proof of death.

The skip rules per `phase` value are documented inline in each step below.

#### Mid-phase-death recovery (rate limit or crash, issue #3683)

A checkpoint is written only after a phase *completes* (see "Write timing"), so a subagent that is killed mid-phase — an account-level rate-limit kill (`TOKEN_EXPIRED` / `TOKEN_EXHAUSTED`, the same vocabulary `.loom/scripts/lib/classify-error.sh` uses), a per-model-tier credit exhaustion (`MODEL_CREDITS_EXHAUSTED`), a crash, an API error, or any other abnormal termination — leaves **no fresh checkpoint** even though it may already have pushed a commit, moved a label, or posted a comment. When you resume a **Judge, Doctor, or Merge** phase whose subagent was not observed to exit cleanly and no new checkpoint was written for it, **do not assume no work happened, and do not blindly re-run the whole phase.**

**`TOKEN_EXPIRED`/`TOKEN_EXHAUSTED` (account-side) vs. a forge GraphQL rate limit — do not conflate these (#4856).** The vocabulary above classifies the *Claude account credential* dying mid-turn (weekly/session limit, expired OAuth token) — the fix is to rotate to a different token in the pool; the same account retried immediately fails again. A **GitHub GraphQL quota exhaustion** (`gh` emitting `API rate limit already exceeded` / `secondary rate limit` / etc. — the same five-signature table documented under "GraphQL-exhaustion fallback" above and in judge.md/doctor.md's "REST Fallback for Labels/Comments") is a **different axis entirely**: the Claude account is perfectly healthy, only the shared `gh` credential's GraphQL quota (independent from REST, and shared across every agent + tool) is temporarily out. Rotating the Claude account does **nothing** for it. Most of the time this is invisible at the sweep-orchestrator level — a well-behaved Judge/Doctor subagent detects the rejection and falls back to REST inline (per the REST equivalents documented in judge.md/doctor.md), so the phase still completes normally and a checkpoint is written. It only becomes a **mid-phase-death** case when the rejection itself brings down the subagent (or its parent) before it can retry — in which case, treat it as **retryable, not exhausted**: no token rotation is needed, and a `gh api rate_limit --jq .resources.graphql` check tells you the reset time to wait out (typically well under an hour) before simply re-dispatching the same phase. Do not route this case through the token pool's bad-token/exhausted bookkeeping — that machinery exists for the Claude account axis, not the forge's.

**`MODEL_CREDITS_EXHAUSTED` is a THIRD axis — try the model fallback before treating it as a mid-phase death (#5687).** "You're out of usage credits" means the *account's credits for one model tier* ran out: the credential is fine, the forge is fine, and the same account on a cheaper model still works. It therefore has a remedy the other two axes do not — **re-dispatch the same attempt one rung down**, per "Credit-exhaustion fallback" above — and that remedy must be attempted **first**, because it usually completes the phase outright and leaves nothing to reconstruct. Only when it is unavailable (already at the cheapest rung ⇒ `resolve-model.sh --downgrade` exits 3) or the re-dispatch itself cannot finish does this become an ordinary mid-phase-death case, handled by the forge-state re-verification below. Rotating the Claude account may *also* work if a pool exists, but on the in-session Task-dispatch path there is no pool to rotate — the model rung is the only lever.

**`SPEND_LIMIT` is a FOURTH, related axis — try omitting the `model` param before treating it as a mid-phase death (#6518).** "You've hit your monthly spend limit" is `TOKEN_EXHAUSTED` for the subprocess/daemon path (classified by `.loom/scripts/lib/classify-error.sh`, unchanged), but on the in-session Task-tool path it needs the same reactive text-match treatment as `MODEL_CREDITS_EXHAUSTED` — see "Spend-limit fallback" above for the full remedy (re-dispatch the same attempt with the `model` param omitted first; only fall through to this mid-phase-death procedure once that is exhausted or was never available). Unlike credit exhaustion, a spend limit's scope (account-wide vs. per-tier) is not definitionally known, so its first-line remedy is "inherit the session default," not a cost-ladder walk.

Instead, before re-dispatching anything for that phase, **re-verify the PR's actual forge state against that phase's already-documented "Expected exit state(s)"** (Judge: step 5's Approve / Request-changes bullets; Doctor: step 6's push → relabel → re-Judge sequence; Merge: step 7's merge-then-checkpoint-delete). Specifically check:

- whether a **new commit** landed on the PR branch since the checkpoint's timestamp (`gh pr view <PR> --json commits`, or `git log <checkpoint-ts>..`),
- whether the **PR label** already reflects a later state than the checkpoint implies (e.g. `loom:review-requested` after a Doctor, or `loom:pr` after a Judge approval), and
- whether there are **PR comments** from the dead subagent describing work it already completed.

Then **complete only the missing steps** to reach that phase's expected exit state — never redo steps that already landed. Example (the exact #3676 incident this rule is drawn from): a Doctor pushed its fix but was rate-limit-killed before re-labeling `loom:changes-requested` → `loom:review-requested` and handing back to Judge. The correct recovery is to re-label and re-run Judge — **not** to dispatch a fresh Doctor that would duplicate the already-pushed commit.

- **Builder is exempt — unchanged, but only when the Builder subagent is actually dead.** This rule covers Judge / Doctor / Merge only. The Builder's "Scope limit (no mid-builder recovery)" above stands as-is: a Builder kill intentionally resumes from *builder start* and relies on `worktree.sh` idempotency for the builder to decide whether to commit / amend / discard its partial diff. Do not apply the forge-state-reverification rule to Builder. **But this exemption presupposes the orchestrator process that dispatched the Builder was itself killed** — on a resume path where the orchestrator survives (session-limit `/login` resume, `/clear` + re-invoke), the dispatched Builder Task can outlive the "kill" that prompted resume and complete later on its own. See "Task-liveness check before Builder re-dispatch (#5897)" under "Mid-builder kill semantics" below: before re-dispatching, check whether the original Task is still live rather than treating worktree state alone as proof of death.
- **Should-prefer (optional): resume the same subagent when the parent survives.** When the **orchestrator's own session** is still alive and the dead phase's Task-tool subagent conversation is still resumable (e.g. via `SendMessage` back into that same subagent thread rather than a brand-new Task dispatch), prefer that path — the original subagent already knows exactly what it committed / pushed / labeled, which is strictly more context than a fresh subagent re-deriving intent from a partial diff. This is a preference, not a requirement: an account-level rate-limit kill often takes the whole process (parent included) down, in which case no resumable thread exists. The mandatory forge-state-reverification rule above must **never** depend on this being available.

#### Stale-checkpoint cleanup

A "stale checkpoint" is one whose issue is already closed on the forge (e.g., the merge happened in a different sweep invocation, or the issue was closed manually after sweep was killed). Detect and clean these up on entry — see step 1.

### 1. Per-issue pre-flight (still per-issue, before the wave dispatch)

> **Aggressive-mode override (`all` sentinel).** When `SWEEP_ALL_AGGRESSIVE=true` (set **only** by the build-everything `all` sentinel — see "Build-everything sentinel (`all`)" under Validation rules), the hard-skip rules below are replaced by the recovery routing in the "Aggressive candidate taxonomy" table: stale `loom:building` is reclaimed (after the one-time `recover-orphaned-shepherds.sh --recover` pass), `loom:blocked` is probed and cleared where the blocker has resolved, `loom:epic` containers fan out to their `loom:epic-phase` children, and uncurated / `loom:triage` / `loom:curating` issues are curated inline before promotion. The existing-PR probe still runs first and still wins (an issue with an open PR is driven to Judge/Merge, never rebuilt). Only `loom:operator-only` and `loom:needs-capability` remain a hard skip — with the single narrow exception of a capability-matched `loom:operator-mechanical` item, which routes to the propose-only lane (#6893, see "Capability-aware `loom:operator-mechanical` lane"). Mode A/B explicit-list and NL sweeps leave the flag unset and use the conservative skips exactly as written below.

For each issue `N` in the wave, before any role skill is invoked:

0. **Read the resume checkpoint (if any).** Before any other pre-flight work for this issue:
   ```bash
   CHECKPOINT_PHASE=$(./.loom/scripts/sweep-checkpoint.sh phase N)
   ```
   `CHECKPOINT_PHASE` is one of: empty string (no checkpoint), `curator-done`, `builder-done`, `judge-rejected`, `judge-done`, `doctor-done`, `merge-done`. Carry this value through the rest of the lifecycle and use it at each phase to decide whether to skip.

   **Stale-checkpoint cleanup.** If a checkpoint exists for `N` *and* the issue's `state` (from step 1's `gh issue view`) is `CLOSED`, the checkpoint is stale (the issue was closed out-of-band — most commonly because a different sweep invocation already merged it, or a human closed it manually). Remove it with a warning and skip the issue entirely:
   ```bash
   if [[ -n "$CHECKPOINT_PHASE" && "$ISSUE_STATE" == "CLOSED" ]]; then
     echo "WARNING: stale sweep checkpoint for closed issue #N (phase=$CHECKPOINT_PHASE) — removing"
     ./.loom/scripts/sweep-checkpoint.sh delete N
     # Skip issue — does NOT contribute to this wave.
   fi
   ```

   **`merge-done` short-circuit.** If `CHECKPOINT_PHASE == "merge-done"`, the issue was already merged in a previous sweep run but the checkpoint was not deleted (rare — e.g., sweep was killed between the merge call and the delete call). Delete the checkpoint and log `already complete; skipping`. The issue does NOT contribute to this wave.

1. **Verify the issue is open and not already in flight.**
   ```bash
   # Plain `gh` — NOT "$GH_READ". This is claim arbitration: a 30s-stale label
   # set is exactly the window in which a competing Builder's `loom:building`
   # claim (or another sweep's freshly opened PR) lands, and answering from
   # cache would dispatch a duplicate builder onto claimed work. Uncached by
   # design — see "Cached forge reads (`gh-cached`)".
   gh issue view N --json state,labels,closedByPullRequestsReferences \
     --jq '{state, labels: [.labels[].name], linked_prs: [.closedByPullRequestsReferences[].url]}'
   ```
   - If the issue is closed, skip it (log a warning). It does NOT contribute to this wave.
   - **Step 1a — daemon self-claim check (#3823, flag added #4111). MANDATORY: evaluate this FIRST, for every issue in the wave, before the `loom:building` skip bullet immediately below — not only when a `loom:building` label happens to be noticed on `N`.** Two equivalent signals, either sufficient on its own:
     - `--claim-owned N` was present in this invocation's `$ARGUMENTS` (stripped during argument classification — see "Optional flags"), where `N` is the issue currently being pre-flighted; OR
     - the env var `LOOM_SWEEP_CLAIM_OWNED` is set and equals `N`.

     If either is true for `N`: `SweepRegistry::dispatch` flipped `loom:issue → loom:building` on the forge *immediately before spawning THIS session* (for immediate external visibility of the claim), and told this session so directly through two independent channels — the flag is literally part of the prompt this session is reading, the env var is in this session's own process environment. The `loom:building` label on `N` is therefore **this session's OWN daemon claim**, full stop. **Do NOT skip. Do NOT evaluate the `loom:building` skip bullet below for this issue.** Proceed straight to step 2 as if you had just claimed `N` yourself, and do **not** reason further about `loom:building` label timing, `loom-daemon status` output, PID tables, or worktree presence to "double-check" whether another worker owns it — issue #4111 is precisely a daemon-dispatched child that built a plausible, evidence-citing case that it was racing a competing worker using those exact signals, while the conclusive `--claim-owned`/`LOOM_SWEEP_CLAIM_OWNED` marker for its own issue sat unread the entire time. Once either signal names `N`, no other signal can override it.

     **Start this sweep's own lease renewal loop for `N` here, once (Issue #6180, Phase 1 of Epic #6165).** A daemon-dispatched claim on `N` also gets a lease record written by the dispatch itself (Issue #6179 — a `<!-- loom:lease host=... sweep=... -->` marker comment, its liveness signal being the comment's own forge-assigned `updated_at`, never a value in the marker text). That record must be kept fresh for as long as THIS session is actively working `N`, and **only this session may renew it — never `loom-daemon`**: role agents routinely outlive the daemon that spawned them (#6129), so a daemon-owned renewal loop would let a live sweep's lease expire across an ordinary daemon restart, inviting a peer host to reclaim work that was never actually abandoned. Fire-and-forget, best-effort, non-blocking — a failure here must never block or fail the sweep itself:
     ```bash
     ./.loom/scripts/sweep-lease-renew.sh start "$N" > /dev/null 2>&1 || true
     ```
     This spawns one detached background loop (default cadence: renew every 5 minutes, configurable via `--interval SECS` or `SWEEP_LEASE_RENEW_INTERVAL_SECS`) that watches this session's own long-lived orchestrator PID (the same liveness handle `sweep-run-registry.sh` resolves — never the one-shot Bash-subshell PID of the tool call that runs this command) and idempotently PATCHes THIS session's own `loom:lease` comment on `N` for as long as that PID stays alive. No explicit `--host`/`--sweep-id` needs to be passed here: `start` resolves them itself (`$LOOM_HOST_ID`'s opaque form, `$LOOM_TERMINAL_ID`'s `daemon-<sweep-id>` shape — both already set in this session's environment by the daemon that spawned it) and uses them for an EXACT-match renewal target. (Prior to Issue #6485's fix, `start` relied on "newest wins" — PATCHing whichever lease comment on `N` had the highest comment id — which is only this session's own lease "by construction" at the instant Step 1a runs; a peer dispatcher racing the same issue and posting a lease comment moments later silently misdirected this loop onto the PEER's lease for the rest of the sweep's lifetime, exactly the mechanism behind the #6470 incident. Exact-match targeting closes that gap.) The loop is self-terminating in two ways — once this session's process exits, OR once this session's own lease has itself yielded (`<!-- loom:lease-yield ... -->`, Issue #6485) — it stops renewing on its own and the lease simply ages out; there is nothing to explicitly stop or clean up. On an issue with no lease comment (manual `/loom:sweep`, GH Actions cron, `--no-daemon` — none of which have `--claim-owned`/`LOOM_SWEEP_CLAIM_OWNED` set, so this step never fires for them anyway) or if `#6179` has not yet landed, the loop's periodic renewal attempts are harmless no-ops. Nothing in this repo reads the lease yet (that is Phase 2) — this call has zero effect on dispatch or claim-arbitration behavior.

     If neither signal is present, or both name a *different* issue than `N`, this step does not apply to `N` — fall through to the ordinary `loom:building` skip bullet immediately below, completely unchanged. This is the normal case for an operator-run `/loom:sweep`, a GH Actions cron invocation, and every *other* issue in a daemon-dispatched wave (the flag/env var name only the ONE issue the daemon dispatched THIS session for).
   - If the issue already has `loom:building` **and Step 1a above did not already establish this is your own daemon claim on this issue**, skip it — another shepherd or builder is working on it. Log a warning. Does NOT contribute to this wave. (This bullet used to carry the self-claim exception inline as a trailing clause; #4111 promoted it to the mandatory Step 1a above after a daemon-dispatched child was observed reasoning about `loom:building` timing/PID/`loom-daemon status` and skipping its own claim without ever consulting the marker. See Step 1a for the full mechanism.)
   - If the issue has `loom:blocked`, skip it. Log a warning. Does NOT contribute to this wave.
   - If the issue has `loom:operator-only`, skip it — requires human action outside automation (credentials, infra rotations, manual deploys, hardware access). Log a warning with reason "operator-only". Does NOT contribute to this wave. **Checked before the existing-PR probe** so operator-only issues aren't probed at all.
   - If the issue has `loom:needs-capability`, skip it — blocked on a missing tool/agent capability, not an operator-by-right decision, but automation still cannot complete it (#5817). Log a warning with reason "needs-capability". Does NOT contribute to this wave. **Checked before the existing-PR probe**, same as the `loom:operator-only` bullet above.
   - **Existing-PR probe (#3359, #3677).** The set of open PRs for issue `N` is the **union of two GitHub-computed sources** — no body-grep. Both are additive and deduped by PR number before routing:

     1. **Closing-keyword PRs (`closedByPullRequestsReferences`, unchanged since #3359).** The `linked_prs` from the `gh issue view` above. GitHub's native `Closes/Fixes/Resolves #N` parser — populated only by closing keywords.
     2. **Non-closing cross-reference PRs (timeline, #3677), phrase-confirmed (#6216).** PRs that reference `N` with a **non-closing** phrase (`Part of #N` / `Contributes to #N`, the #3599 partial-increment convention — see `defaults/roles/builder-pr.md`) never appear in `closedByPullRequestsReferences` by design, so probe the issue's timeline for `cross-referenced` events whose source is a PR:
        ```bash
        # Plain `gh` — NOT "$GH_READ": same claim-arbitration carve-out as the
        # `gh issue view` read above (this probe decides whether to dispatch a
        # Builder at all).
        gh api "repos/OWNER/REPO/issues/N/timeline" --paginate \
          --jq '[.[] | select(.event == "cross-referenced"
                              and .source.issue.pull_request != null
                              and .source.issue.repository.full_name == "OWNER/REPO")
                 | {number: .source.issue.number, state: .source.issue.state}]
                | unique_by(.number)'
        ```
        This is GitHub's own reference parser (the same engine behind `closedByPullRequestsReferences`) surfacing **every** `#N` mention as a `cross-referenced` event, with `source.issue.pull_request` non-null when the referrer is a PR and `source.issue.state` giving its live state. Keep only entries whose `state == "open"` (lowercase — the timeline API returns lowercase issue/PR states, unlike the uppercase `closedByPullRequestsReferences` field). **Same-repo guard (required):** `cross-referenced` events include references from *other* repositories in a multi-repo ecosystem (e.g. a sibling repo's PR that mentions `OWNER/REPO#N`); the `.source.issue.repository.full_name == "OWNER/REPO"` filter (the field is reliably populated on every event) scopes the result to this repo so a foreign PR number is never misrouted to `gh pr view` below. This mirrors `closedByPullRequestsReferences`, which is inherently same-repo. No local regex is involved on GitHub's side; GitHub does the text parsing (the #3267 lesson: don't hand-roll what GitHub already parses).

        **Phrase filter (required, #6216).** A `cross-referenced` event fires for **any** mention of `#N` in a PR body — not only a partial-increment phrase — so a same-repo, open timeline hit from source 2 is not yet a confirmed linkage; it is only a candidate. For each surviving candidate PR, fetch its body once and keep the candidate only if the body actually contains a `Part of #N` / `Contributes to #N` reference to **this** issue, tolerant of markdown emphasis/colon between the phrase and `#N` (the same convention `parse_dependencies` uses in `defaults/.claude/commands/loom/guide.md`, #4508):
        ```bash
        # One extra call per surviving timeline candidate (bounded — see the
        # cost note below; not N×M).
        gh pr view <pr_number> --json body --jq '.body // ""' \
          | grep -iqE "(Part of|Contributes to)[*_:[:space:]]*#N" \
          && echo confirmed || echo bare-mention
        ```
        Discard any candidate whose body does not match — it was a bare mention, not a partial-increment linkage, and **must not** count as an open linked PR (it does not feed the union below and never produces the `skip (existing PR #X in flight)` log line). A discarded candidate may optionally get a distinct advisory log line — e.g. `note: #N is mentioned by open PR #Y but not confirmed as a partial-increment linkage` — visibly different from the confirmed-linkage skip line above; this is visibility only and never changes the routing decision.

     **Union, then filter at the per-PR read (#6217).** Merge the two source lists and dedupe by PR number, passing **every** source-1 PR through unfiltered at this step — `gh issue view --json closedByPullRequestsReferences` exposes only `id, number, repository, url` per entry, **not `state`**, so a `select(.state == "OPEN")` applied here matches nothing and silently drops every row (the bug #6217 fixed: the union step has no `state` field to filter on). Source 2 needs no further filtering here — its timeline read already filtered to `state == "open"` **and** the phrase filter above already discarded every bare mention (#6216), so only phrase-confirmed open PRs reach the union. For **every** candidate PR from the union (both sources), fetch its live state and labels — this per-PR read is the first and only point in the pipeline where `state` is actually available for a source-1 PR:
     ```bash
     # Plain `gh` — NOT "$GH_READ" (routing read; must be live).
     gh pr view <pr_number_or_url> --json state,labels --jq '{state, labels: [.labels[].name]}'
     ```
     **Apply the MERGED/CLOSED exclusion here, not at the union step**: drop any candidate whose returned `state != "OPEN"` (uppercase — `gh pr view --json state` returns uppercase; a source-1 PR can be MERGED or CLOSED, which is not the duplicate-builder hazard this probe exists to catch). Apply the routing rules below based on the count of distinct **open** linked PRs remaining (from either source):

     | Open linked PRs | Action |
     |-----------------|--------|
     | 0 | Continue with pre-flight (no behavior change). This includes the case where source 2's only hit(s) were bare mentions discarded by the phrase filter — the issue proceeds to Builder normally. |
     | 1, no `loom:pr` label | **Skip Builder phase.** Log `skip (existing PR #X in flight)` with the PR URL. The existing PR is routed into the Judge phase (step 5) **for this wave** in place of a freshly-built PR; the Builder is not dispatched. Wave size shrinks by one per the pre-flight skip rule. |
     | 1, has `loom:pr` label, no `loom:operator` | **Skip Curator + Builder + Judge.** Route the PR directly to Merge (step 7). The PR has already been judged. |
     | 1, has `loom:pr` label **and** `loom:operator` | **Do NOT route to Merge.** The `labels` array from the same `gh pr view` read above already carries this — no extra call. Champion's merge-risk hold (`.loom/docs/label-state-machine.md`) is still in force on this PR; the engine may not act on it further, but the label stays re-evaluable so **skip the issue** rather than mutating anything — log `skip (PR #X held by loom:operator — human required)` with the PR URL. Do not remove `loom:operator` or `loom:pr`, and do not call `merge-pr.sh`. |
     | 2 or more | Log all PR URLs and skip the issue. This is a human-attention case (which PR is canonical?) — sweep does not pick one. |

     The closing-keyword path (`closedByPullRequestsReferences`, verified working in `gh` 2.93.0; matches the convention used in `champion-reference.md` and `champion-pr-merge.md`) is **untouched** — this is purely additive. It uses GitHub's native parser for `Closes/Fixes/Resolves #N` (and correctly excludes `Updates #N` / `Related to #N`); the timeline source covers exactly the non-closing references that field deliberately omits, narrowed to confirmed partial-increment references by the phrase filter above. Do **not** body-grep PRs for closing keywords (re-introduces the #3267 bug) — the phrase filter is a narrower, additive check layered on top of GitHub's own `cross-referenced` event, not a replacement for it. Per-issue the open-PR count is 0 or 1 in practice, so the timeline call + a body fetch per surviving source-2 candidate + a secondary `gh pr view` for routing is two or three extra calls per surviving candidate, not N×M.

2. **Read the issue body before briefing any builder.** This is a non-negotiable rule from prior sweep sessions (a misleading title hid the real requirement in the body). Skipped only if pre-flight already routed the issue to Judge/Merge via the existing-PR rules above — those branches use the PR as the source of truth, not the issue body.
   ```bash
   gh issue view N --json title,body
   ```
   This same `body` also feeds the file-surface estimate ("Overlap-aware wave partitioning" step 1): parse its `## Affected Files` section for the candidate's estimated surface. A missing / "To be determined" section leaves the surface *unknown* (excluded from overlap analysis, never blocked). The estimate is used only to schedule waves and to warn — **never** to create a stacking edge (#3729, scheduling signal only).

> **Pre-flight skip rule.** If `K` of the wave's `N` candidates are skipped at pre-flight (closed, `loom:building`, `loom:blocked`, `loom:operator-only`, `loom:needs-capability`, or multi-PR ambiguity), dispatch only `N - K` builders for this wave. Issues routed to Judge or Merge via the existing-PR rules consume a wave slot but skip the Builder dispatch. **Do not pull a candidate forward** from the next wave to backfill. Wave boundaries stay clean, and the next wave runs at its originally planned size.

> **This per-issue check is not the only re-verification point.** When a daemon/champion is active in the repo (roleRunner/champion-on-idle, or the legacy daemon), the wave *plan* itself can go stale between waves — see "8a. Wave-boundary candidate re-verification" below, which re-runs this same existing-PR probe and these same skip rules across the **whole remaining candidate list** once per wave boundary, complementing (not replacing) this per-issue check (#4884).

### 2. Curator phase (still per-issue, before the wave dispatch)

For each surviving issue `N` in the wave:

- **Checkpoint skip.** If `CHECKPOINT_PHASE` is one of `curator-done`, `builder-done`, `judge-rejected`, `judge-done`, `doctor-done`, skip the curator phase entirely (it already completed in a prior sweep run). Do NOT re-invoke the curator skill — re-curating is wasted work and can produce churn on an issue that's already mid-lifecycle.
- Otherwise (no checkpoint, or `CHECKPOINT_PHASE` is empty): if the issue does not already have `loom:curated` or `loom:issue`, run the curator skill on it.
  - Load and follow the instructions in `.claude/commands/loom/curator.md` for issue `N`.
  - Expected exit state: issue has `loom:curated`.
- If the issue already has `loom:curated` or `loom:issue`, skip the curator skill invocation but still write the checkpoint below (so future sweep runs can skip the redundant label probe).
- **On successful completion** (curator ran, or curator-skip-because-already-curated), write the checkpoint:
  ```bash
  # Append --model <resolved> when you passed a model param to the curator subagent (#3482).
  ./.loom/scripts/sweep-checkpoint.sh write N curator-done --task-id "$RUN_ID"
  ```

Curator runs sequentially per-issue within wave setup — it is cheap and does not benefit from parallelism here. **Await each Curator's completion explicitly** (a bounded, non-blocking `TaskOutput` poll — see the context-safe recipe, #6168) before advancing — the harness may launch the subagent async even with `run_in_background: false`, so the sequencing here depends on an explicit await, not the dispatch flag (see "Subagent dispatch is async-only", #3822).

> **The `check-main-clean.sh` backstop (see "Backstop: verify the main worktree is clean after EACH builder returns" under the Builder phase below) is orchestrator-side only and does not cover the Curator phase.** It runs from *this* sweep skill, after each Builder's `TaskOutput`, and catches contamination a Builder subagent left in main. A Curator running in the main checkout (e.g. reproducing a measurement/board pipeline while re-baselining an issue) gets **no equivalent check here** — and a bare Champion cron tick or an interactive Curator session outside `/loom:sweep` gets none at all, orchestrator or not. Curators must self-enforce the worktree-or-restore rule in `curator.md` § "Running Measurement / Board-Pipeline Reproductions" (#4991) rather than rely on this backstop catching a missed restore.

### 3. Approval gate (per-issue)

Each issue must reach `loom:issue` before the Builder can claim it. This promotion is authorized — see `.loom/roles/curator.md` § "Who promotes `loom:curated` → `loom:issue`" for the full rule. In short: the orchestrator only ever promotes an issue that is already a member of *this sweep's own resolved candidate set*, so the promotion executes an approval already given one step earlier in this same run (the operator named or confirmed the issue, or the daemon dispatch that started this sweep did) — it is not independent agent judgment, and it is not the Curator acting.

- If the issue already has `loom:issue`, proceed.
- Otherwise, promote it (add-only — matches Champion promotion):
  ```bash
  gh issue edit N --add-label "loom:issue"
  ```
  **Do not remove `loom:curated`.** Per #3288 (Option A), `loom:curated` is a persistent milestone marker, not a transient step label — a promoted issue carries *both* `loom:curated` and `loom:issue`. Stripping it here would falsely surface the issue in the Curator Priority 1 "approved-but-uncurated" query (`loom:issue` without `loom:curated`) and drop the Builder prioritization signal that ranks `loom:issue` + `loom:curated` ahead of `loom:issue` alone. This keeps sweep promotion consistent with `champion-issue-promo.md`, which also preserves `loom:curated`.

### 4. Builder phase (parallel within the wave)

**Checkpoint skip.** For each surviving issue, if `CHECKPOINT_PHASE` is one of `builder-done`, `judge-rejected`, `judge-done`, `doctor-done`, the Builder phase has already completed for this issue. Read the `pr_number` from the checkpoint and route the PR directly into the Judge phase (step 5) — do NOT dispatch a builder subagent.

```bash
EXISTING_PR=$(./.loom/scripts/sweep-checkpoint.sh read N | sed -n 's/.*"pr_number"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p')
```

If `CHECKPOINT_PHASE` is `judge-rejected`, `judge-done`, or `doctor-done`, see the corresponding skip rules in steps 5/6 — the PR is routed further along, not back to Builder.

For issues without `builder-done`-or-later checkpoints, proceed with the normal Builder dispatch:

Dispatch up to `min(resolved-wave-size, surviving-candidates-in-wave-needing-builder)` `loom-builder` subagents **in a single tool-call block** from this orchestrator session, where `resolved-wave-size` is the explicit `--builders-per-wave` value or, when the flag was omitted, the Stage -1 auto wave size ("Resolve auto wave size"). Note this Wave Lifecycle is the **subagent** path, so the auto size here is core-scaled within `[3, 6]` (#3289-safe floor 3, ceiling 6, #3693) — the daemon path never runs this section (it dispatches detached processes and exits at Stage -1). **Do NOT invoke `/loom:sweep` as a subagent here** — see the "One level deep" rule in Execution Model above.

Each builder is responsible for:

- Claiming its issue (`loom:issue` → `loom:building`).
- Creating an issue worktree via `./.loom/scripts/worktree.sh N` (idempotent — re-entering after a kill reuses the existing worktree and branch).
- Implementing the change, running tests, committing.
- Filing a follow-up issue for any out-of-scope discovery via `./.loom/scripts/create-issue.sh` (per `builder-complexity.md`) — **never** a bare `gh issue create` (#5047). This is a single filing call, not the concurrent issue-creation-burst hazard the #3707 rule above guards against, so it is expected behavior inside a parallel wave.
- Pushing the branch and opening a PR labeled `loom:review-requested`.
- Closing references: `Closes #N` in the PR body.

**Stacked-dependency gated path (`--depends-on`, #3729 v1; per-issue map generalization, `--auto-stack`, #3759).** This gate fires **only** when a parent is set for the issue being built — look it up in the per-issue map `DEPENDS_ON[N]` (which subsumes the historical single global `DEPENDS_ON`: for a single-issue `--depends-on <parent>` dispatch, `DEPENDS_ON[N]` is just that one entry; for an `--auto-stack` wave, it is one entry per detected child). When `DEPENDS_ON[N]` is unset for issue `N`, the two steps below are byte-for-byte the default behavior. When `DEPENDS_ON[N]=<parent>` is set, the builder for issue `N` must:
  - Create its worktree branched off the parent's branch: `./.loom/scripts/worktree.sh N --base feature/issue-<parent>` (instead of the bare `./.loom/scripts/worktree.sh N`). `worktree.sh` resolves `feature/issue-<parent>` from `origin/feature/issue-<parent>` (or a local branch), so the parent sweep must have created/pushed its branch first; if the base cannot be resolved, `worktree.sh` hard-fails rather than silently branching off the default branch.
  - Open its PR against the parent branch: `gh pr create --base feature/issue-<parent> --label "loom:review-requested" --body "Closes #N ..."` (instead of the default base). The PR stays stacked on the parent until reconciliation, which now fires automatically when the parent squash-merges (see "Stacked dependency (auto-reconciliation on parent merge)").
  The **only** thing `--auto-stack` changes here is how `DEPENDS_ON[N]` is *sourced* — the `worktree.sh --base` / `gh pr create --base` mechanics are untouched. Two sources feed the map: (a) an explicit single-issue `--depends-on <parent>` (unchanged, typically a daemon `dispatch_sweep` forwarding `depends_on` as `--depends-on`), and (b) an auto-stack-detected same-candidate-set edge (see "Auto-stack detection and wave ordering"). Absent both, the wave lifecycle does not auto-create stacks.
  **Same-wave parent/child.** When the topological ordering placed a parent and its child in the **same** wave, the child's Builder branches off `feature/issue-<parent>` even though the parent's Builder is running concurrently in that wave — `worktree.sh --base` resolves the parent branch as soon as the parent Builder has pushed it. The child does **not** branch off the shared pre-wave `main` snapshot its unstacked wave-mates use.

**Await all builders in the wave** before proceeding to Judge. Collect each builder's PR number (or failure marker). This await is **mandatory and explicit** — a bounded, non-blocking `TaskOutput` poll per builder (see the context-safe recipe, #6168), not one large blocking call. The harness may launch each Task async regardless of `run_in_background: false`, so proceeding to Judge on a dispatch flag alone can start Judge before builders finish; the "await all builders before Judge" rule is enforced by this explicit block, not by any dispatch flag (see "Subagent dispatch is async-only", #3822).

**Run the main-clean check after EACH builder returns, not once per wave (#4380).** As each individual builder's `TaskOutput` arrives — before moving on to the next one's result and long before the wave advances to Judge — run the contamination check with that builder's issue in the label:

```bash
# Immediately after builder for issue N returns (per builder, inside the await loop):
./.loom/scripts/check-main-clean.sh \
    --baseline "$MAIN_CLEAN_BASELINE" \
    --quarantine \
    --label "run=$RUN_ID issue=$N"
# exit 0 ⇒ clean · exit 4 ⇒ contamination found and QUARANTINED (continue) · exit 3 ⇒ dirty, NOT quarantined (hard-block)
```

Why per builder rather than per wave: with a single post-wave check, N builders share one detection point, so any contamination is attributable only to "some builder in this wave" and the risk window is wave-sized. Checking after each `TaskOutput` narrows the window to one builder and makes attribution exact — the `--label` value names the culprit in the quarantine log entry. See the Backstop section below for the full semantics, and keep the per-wave check as a final belt-and-suspenders pass.

**Assert the Builder's cwd before it edits anything.** Before the Builder
subagent prompt does any Write/Edit/Bash file mutation, it MUST capture
`WORKTREE_ABS="$(cd .loom/worktrees/issue-N && pwd)"` and verify both: the
`.loom-managed` sentinel is present at `$WORKTREE_ABS`, and `git -C
"$WORKTREE_ABS" rev-parse --show-toplevel` equals `$WORKTREE_ABS` — then use
`$WORKTREE_ABS` (never a bare repo-relative path) for every subsequent
file-mutating call, Write/Edit or Bash alike (see `builder.md` → "Pre-Work
Validation" / "Validation Checklist", #4178). A denied write is never a signal
to retry the same target through a different tool (Edit/Write vs. Bash) — see
below.

**Backstop: verify the main worktree is clean after EACH builder returns (#3513, per-builder cadence + atomic quarantine #4380).** A builder subagent is dispatched via the Task tool ("one level deep", step 4 above) and inherits the orchestrator's single shared process env, which has **no** `LOOM_WORKTREE_PATH`, because the Task tool exposes no per-subagent env-injection parameter (#3719).

> **Do not re-derive the stale "the guard cannot arm here" claim.** The absent env var does **not** disable worktree confinement. `guard-worktree-paths.sh`'s **path-derived fallback** (#4007, PR #4129) arms with **no env var at all**: it denies any Edit/Write whose target resolves into the main checkout while any `.loom-managed` worktree exists anywhere in the repo. This is directly evidenced, not theoretical — `.loom/logs/hook-errors.log` records a dense deny cluster during the 2026-07-29 #4364 build (`[guard-worktree-paths] Denied: BLOCKED: Edit/Write path '…/loom-daemon/src/main_health_gate.rs' resolves to the main repository checkout …`). `guard-destructive-generic.sh` extends the identical confinement to the common Bash-tool write idioms (`>`/`>>` redirection, `tee`, `sed -i`, `cp`/`mv`, #4178 / PR #4210), closing the escape #4063 used (a write denied on Edit/Write retried through Bash instead).
>
> **Both guards are PreToolUse DENY hooks. Neither guard reverts anything** — they block *before* the write lands, and never touch a file that already exists. So a builder narrative like "the guard reverted most of my edits" is a misreading of *denied* writes (which never landed) as *reverted* writes; the "partial" part of that story came from the builder's own ad-hoc per-file `git checkout --` cleanup, not from any hook. That ad-hoc cleanup is exactly what the `--quarantine` mode below replaces.

This `check-main-clean.sh` backstop stays load-bearing despite the guard coverage: it is a whole-tree status check, not an idiom scan, so it catches anything the guards' heuristics don't recognize (an interpreter one-liner like `python -c`, `git apply`/`patch`, most deletion vectors — all deliberately out of scope for #4178's pattern list) or a write that landed before any worktree existed.

**Cadence: after each individual builder's `TaskOutput`, plus once more after the whole wave.** The per-builder run is the primary one — its `--label` carries that builder's issue number, which is what makes the quarantine log entry attributable. The post-wave run is belt-and-suspenders (it catches anything that landed between the last builder's return and the Judge hand-off) and is labelled with the wave rather than an issue:

```bash
# Per builder, inside the await loop (primary — narrow window, exact attribution):
./.loom/scripts/check-main-clean.sh --baseline "$MAIN_CLEAN_BASELINE" \
    --quarantine --label "run=$RUN_ID issue=$N"

# Once more after all builders in the wave return, before advancing any PR to Judge:
./.loom/scripts/check-main-clean.sh --baseline "$MAIN_CLEAN_BASELINE" \
    --quarantine --label "run=$RUN_ID wave=$WAVE_INDEX"
```

The `--baseline` argument points at the snapshot taken once at step 0 (before wave 1). With it, the check subtracts any dirt that predated the sweep and flags **only** changes that appeared after the snapshot — so pre-existing working-tree dirt (a regenerated lockfile, an operator scratch edit) no longer false-positives as contamination on every check (#3648). If the baseline file is missing or unreadable, the check warns and falls back to the whole-status hard-fail (fail-safe).

**Exit codes and what to do with each:**

| Exit | Meaning | Action |
|------|---------|--------|
| `0` | Main is clean (or carries only baselined dirt) | Continue normally. |
| `4` | New dirt was found and **quarantined** to a stash rescue ref; main is provably back at the baseline | **Continue** — do not hard-block. Record the quarantine in the wave summary (see below). |
| `3` | New dirt was found and could **not** be quarantined (or `--quarantine` was not passed) | **Hard-block** the wave from advancing any PR to Judge until it is resolved. |

**Remediation is ALL-OR-NOTHING, and `--quarantine` performs it.** On detection, the check moves **every** offending path — tracked modifications *and* untracked files together — into a stash rescue ref in **one** `git stash push --include-untracked` operation scoped to exactly those paths, then emits **exactly one** structured JSON line naming the label, the offending paths, and the stash commit:

```
{"event":"main-clean.quarantine","ts":"…","result":"quarantined","label":"run=… issue=4364","main":"/…","stash_ref":"stash@{0}","stash_commit":"<sha>","paths":["…"],"count":2}
```

That entry goes to stderr and is appended to `.loom/logs/main-quarantine.log` (override with `--log FILE`). Properties that matter:

- **It is a rescue, never a discard.** The full diff survives in the stash; recover it with `git stash show -p <sha>` and replay it into the owning issue worktree.
- **Baselined dirt is spared.** Only the paths the check flagged as *new* are stashed, so an operator's unrelated working-tree edits are untouched.
- **It is verified.** After stashing, the check re-runs detection and only reports success (exit `4`) if main is back at the baseline; a residual-dirt result is reported as a failure (exit `3`), never as a partial success.
- **An empty quarantine is never created (#5185).** The offending path set is re-derived from a fresh `git status` immediately before the stash push, so dirt that a concurrent sweep / the builder's own commit resolved in the meantime is dropped from the pathspec instead of producing a content-free stash entry. When nothing is left to rescue, no stash is pushed and the check emits `"result":"no_op"` and exits `0` (main is provably at the baseline — treat it exactly like any other exit `0`). `"result":"quarantined_empty"` marks the residual race where an entry was created but captured nothing.
- **The rescue ref stays outstanding until a human reconciles it.** Nothing drops it automatically, and `git stash`'s reflog is shared by every linked worktree, so entries accumulate. List them with `./.loom/scripts/check-main-clean.sh --list-quarantined` (also surfaced as a `Quarantined work` section by `./.loom/bin/loom status`).

**Do NOT restore contamination piecemeal.** Per-file `git checkout -- <path>` / `rm <path>` sequences are forbidden as the remediation path: they are what produced the half-restored main checkout this section exists to prevent, and a main checkout that is neither the baseline nor the builder's intended change is worse than either extreme. If for any reason you must remediate by hand (e.g. `--quarantine` itself failed and returned `3`), do it as a **single** `git stash push --include-untracked -m "loom-quarantine: run=$RUN_ID issue=$N" -- <all offending paths>` — one operation, all paths, logged.

**Reporting.** Surface every non-zero result loudly in the wave summary — **quote the specific offending paths** the check printed (under `Offending changes:` on exit `3`, or in the `paths` array of the quarantine entry on exit `4`) so the operator can see exactly which files escaped a worktree, along with the stash sha when one was created. The guard-hook denials plus the cwd-assertion prompt discipline above are the primary defense; this status check is the backstop that catches whatever they miss, and the quarantine is what makes its cleanup deterministic.

**On successful PR creation**, write the `builder-done` checkpoint for that issue (record the PR number):
```bash
# Append --model <resolved> when you passed a model param to the builder subagent (#3482).
./.loom/scripts/sweep-checkpoint.sh write N builder-done --task-id "$RUN_ID" --pr-number <PR>
```

If the builder failed (no PR opened), do NOT write a checkpoint — leave the checkpoint at the previous phase (typically `curator-done`) so the next sweep retries the builder from scratch.

**Genuine no-op conclusion vs. builder failure (#6670, first in-repo caller #6740).** No PR opened is not automatically a failure — distinguish two cases before deciding what to do next:

- **Builder failure** (build error, test failure, unrecoverable conflict, crash) — no PR, and no `.no-changes-needed` marker in the issue's worktree (`.loom/worktrees/issue-N/.no-changes-needed`). Leave the checkpoint as described above and fall through to "Per-builder failure isolation" below.
- **Genuine no-op** — the worktree carries an untracked `.no-changes-needed` marker (builder.md → "Signaling No Changes Needed"): the builder investigated and deliberately concluded no code changes are required this pass, rather than crashing or erroring out. This is a self-reported "no actionable delta this pass" conclusion — exactly the shape `autonomous.workFinder.noopCooldown` (#6670) exists to suppress immediate re-dispatch on (see `daemon-reference.md` → "No-op re-dispatch cooldown (#6670)"). Handle it as its own outcome, distinct from a failure:
  1. **Before releasing the claim**, record the no-op so the work finder does not immediately re-offer the same candidate:
     ```bash
     ./.loom/scripts/record-noop-release.sh N --reason "$(head -c 200 .loom/worktrees/issue-N/.no-changes-needed)"
     ```
     This call is best-effort and defensive, mirroring `build-gate.sh`'s `dispatch-backoff record` arm (#6192): a missing, unreachable, or older `loom-daemon` makes it a silent no-op and never fails the sweep.
  2. **Release the claim** back to `loom:issue` — do NOT close the issue (the marker records "nothing to do this pass," not "this issue is resolved"):
     ```bash
     gh issue edit N --remove-label "loom:building" --add-label "loom:issue"
     gh issue comment N --body "Builder found no actionable changes this pass: $(head -c 200 .loom/worktrees/issue-N/.no-changes-needed). Releasing the claim; a self-reported no-op cooldown was recorded so this issue is not immediately re-offered (#6670)."
     ```
  3. **Do NOT write a `builder-done` checkpoint** — leave it at the previous phase, exactly like the failure path. The marker is untracked and never committed, so nothing about this outcome is durable on the branch itself; the checkpoint staying at `curator-done` is what lets a future sweep pass re-evaluate the issue fresh (the noop-cooldown window from step 1, not the checkpoint, is what actually suppresses the immediate re-dispatch).
  4. Record it as `no-op (no changes needed)` in the wave summary — distinct from `blocked (builder failed)` — so an operator scanning the summary does not mistake a deliberate conclusion for an error.

**Per-builder failure isolation.** If builder for issue `#A` fails to open a PR (build error, test failure, unrecoverable conflict, etc.), log it and **continue** with the other builders' PRs in this wave. The failed issue is recorded as `blocked (builder failed)` in the summary. Do NOT abort the wave. Do NOT skip Judge for the other PRs.

**Mid-builder kill semantics (#3373).** If sweep is killed during the Builder phase, the next invocation will see `CHECKPOINT_PHASE == "curator-done"` (no `builder-done` was written), so the Builder dispatches again from scratch. The worktree from the killed run is preserved by `worktree.sh`'s idempotency — `./.loom/scripts/worktree.sh N` is a no-op if `.loom/worktrees/issue-N` already exists. The builder re-enters the worktree, sees the partial diff, and decides whether to commit / amend / discard. **Sweep itself does not introspect the partial diff** — that's the builder's job.

**Task-liveness check before Builder re-dispatch (#5897).** The "Mid-builder kill semantics" above is written for the case where sweep's own process died — but "sweep was killed" is not the only way an orchestrator ends up back at `CHECKPOINT_PHASE == "curator-done"` staring at a partial diff. On a **session-roll resume path** — a session-limit kill resumed via `/login` + re-invocation, or a `/clear` + re-invoke — the **orchestrator's own process survives**, and a Builder Task dispatched before the roll can survive with it: dispatched `Task`-tool subagents are not children of the terminal session in the way a killed CLI process's child processes are, so the roll does not necessarily take the in-flight Builder down with it. The incident this rule is drawn from: a `/loom:sweep all` run hit a session limit and was resumed via `/login`; at resume the orchestrator inspected the issue's worktree (uncommitted partial diff, no branch pushed, no PR) and concluded the Builder had died mid-edit — but the original Builder Task was still running, and completed ~40 minutes later (its completion notification arrived in the resumed context), by which point a duplicate Builder had already been dispatched into the same worktree. For a window, two Builders were live against one worktree — a genuine race, not just wasted work, because either could have committed/pushed over the other's in-flight state.

- **The failure signature, stated precisely: "worktree has an uncommitted partial diff, no pushed branch, no PR" is NOT proof the Builder is dead.** It is proof only that the Builder has not yet reached `builder-done`. When the *orchestrator* process survived whatever triggered the resume (session-limit `/login` resume, `/clear` re-invoke), the dispatched Task can still be running and can still complete later, producing a genuine PR after the orchestrator already treated it as dead.
- **Before re-dispatching a Builder for an issue whose worktree shows an in-flight partial diff, check whether the original Builder Task is still live in the harness** — not just forge/worktree state. Use whatever task-liveness surface the harness exposes for the current session (e.g., a non-blocking `TaskOutput` poll on the original dispatch's task id, or reviewing the transcript/session for a still-outstanding Task dispatch with no completion notification) before concluding the worktree state alone means the builder died.
- **If the original may still be live, prefer a bounded wait/poll over immediate re-dispatch.** A short, bounded non-blocking `TaskOutput` poll (same context-safe recipe as elsewhere in this doc, #6168) costs little and avoids the duplicate-builder race outright; only fall through to a fresh Builder dispatch once you have positive evidence of death (the harness reports the task itself as gone/errored, or the wait window elapses with no sign of life) — not merely the absence of a pushed branch.
- **If a duplicate is dispatched anyway and the original then completes, stop the duplicate before it can commit/push.** The moment the original Builder's completion notification (or its pushed branch / opened PR) is observed, treat that as authoritative and `TaskStop` the duplicate Builder Task immediately — before it reaches its own commit/push step — to prevent it from racing a second commit or push against the original's already-landed work into the same worktree/branch.

This rule only *adds* a liveness check ahead of "Mid-builder kill semantics" above — it does not change what happens once a Builder is confirmed dead (worktree preserved by idempotency, next Builder decides commit / amend / discard, sweep itself still does not introspect the diff).

**This generalizes beyond Builder re-dispatch (#6765).** The underlying rule — *query the harness's own liveness surface before treating an indirect proxy (a worktree diff, a lock's holder PID, silence, a stale kill notification) as proof a dispatched subagent has died* — is not specific to the worktree-diff proxy above. Apply the same three steps at **any** point an orchestrator is about to act on the assumption that a subagent it dispatched has died: (1) query positive liveness via the harness before inferring death from a proxy; (2) if liveness is genuinely unknowable from here (the agent was dispatched out-of-session, on another host, or otherwise unpollable), say so explicitly rather than picking a proxy anyway; (3) absent positive evidence of death, default to assuming the agent is live and do not adopt work associated with it. The lock-specific worked case this generalizes to — reaping an orphaned lock is fine, but adopting the branch/worktree it was protecting is a separate decision requiring its own positive "done" signal — is documented in `.loom/docs/troubleshooting.md` → "A dead lock-holder PID is not evidence the requesting agent is dead (#6765)".

### Stacked dependency (auto-reconciliation on parent merge) — #3729 (v1), #3747 (v2 items 1 & 2)

Stacked-PR mode pipelines a genuine dependency: when issue B consumes issue A's output (schema, file, manifest), B is built on `feature/issue-A` so B's lifecycle runs concurrently with A's review instead of serializing behind A's merge. **The dispatch surface is opt-in, daemon-`dispatch_sweep`-only, and linear-chains-only.**

**How to dispatch a chain.** A chain is N independent `dispatch_sweep` calls, each naming its immediate predecessor — there is no multi-node planner:

```text
# Parent A (independent):
mcp__loom__dispatch_sweep  kind={"Issue": A}  workspace_root=$WORKSPACE_ROOT
# Child B stacked on A:
mcp__loom__dispatch_sweep  kind={"Issue": B}  depends_on=A  workspace_root=$WORKSPACE_ROOT
# Grandchild C stacked on B (A→B→C works because each hop names only its parent):
mcp__loom__dispatch_sweep  kind={"Issue": C}  depends_on=B  workspace_root=$WORKSPACE_ROOT
```

The daemon forwards `depends_on` to the child as `--depends-on <parent>`; the child's Builder branches off `feature/issue-<parent>` and opens its PR with `--base feature/issue-<parent>` (see the gated path in the Builder phase above). A single optional parent makes diamonds / multi-parent stacks **unrepresentable** — there is no rejection logic because the type itself forbids them.

**Block-the-subtree on parent failure (daemon-side, #3729 item 4).** If the parent sweep ends in `loom:blocked` (Doctor-cycle budget exhausted, or an operator cancel), the daemon's reaper does **not** let a child whose `depends_on` names that parent auto-progress: it publishes `sweep.issue.{child}.blocker` on the existing frozen topic (no new topic) so the stuck stack surfaces to the operator. Auto-detach (rebasing an orphaned child onto the default branch) is **not** implemented in v1 — block-the-subtree is the only cascade behavior.

**Reconciliation now fires automatically on parent merge (v2 item 1, #3747).** The repo squash-merges, so after the parent squash-merges to the default branch as one commit, the child branch still carries the parent's original pre-squash commits. `merge-pr.sh` now reconciles child PRs automatically at its post-merge choke point (alongside the partial-increment label reset, before branch deletion): it discovers open child PRs via a **live forge query** (`gh pr list --base feature/issue-<parent>` — never the daemon registry, which is ephemeral and may not be running under a Champion cron or interactive merge), then splits safe/unsafe per child on the child **issue's** `loom:building` label (fresh, uncached `gh api` read):

- **Safe** (child issue not `loom:building`): invokes `./.loom/scripts/reconcile-stack.sh <child-pr> feature/issue-<parent>` for you.
- **Unsafe** (child issue still `loom:building`): a live Builder likely holds the child branch checked out, so the auto-rebase is **skipped** and a comment is posted on the child PR flagging deferred reconciliation. A later parent-merge-triggered pass (once the issue is no longer `loom:building`), or a manual run, picks it up.

The step is **best-effort** — a reconciliation failure never fails the parent merge — and idempotent (once a child's base is retargeted away from the parent branch, the query returns zero rows).

**Pre-merge merge-ordering guard now ships too (v2 item 2, #3747).** Item 1's reconciliation runs *after* the parent has already merged, and the repo setting Loom itself recommends (`delete_branch_on_merge:true`, applied by `setup-repository-settings.sh`) makes GitHub delete `feature/issue-<parent>` **synchronously during the merge API call** — before the post-merge reconcile pass runs, and once the ref is gone `reconcile-stack.sh`'s `git rebase --onto <default> <parent-branch>` can no longer resolve `<parent-branch>`. So item 1 could race and *lose* against the repo's own settings. To close that race, `merge-pr.sh` now runs a **pre-merge guard** (before both the auto-merge and synchronous-merge paths) that discovers open child PRs with the same live-forge query (`gh pr list --base feature/issue-<parent> --state open`) and, by default, **hard-blocks the merge** (`exit 1`, naming the blocking child PR number(s) and the `reconcile-stack.sh` unblock command) rather than letting the parent merge create the race. This is a normal, recoverable failure — Champion's cron retries it next tick, exactly like any other merge-blocking condition. Unlike item 1's post-merge pass, the guard keys **purely on "does an open child PR still target this branch"** — never on the child's `loom:building` label, since a "safe" child is just as exposed to branch deletion as an "unsafe" one. Pass **`--allow-stacked-children`** to `merge-pr.sh` to bypass the guard once you have manually reconciled/verified the children (operator asserts responsibility, mirroring `--worktree-path`); `--dry-run` still runs the guard and reports the would-be block without exiting 1.

`reconcile-stack.sh` remains available for **manual** invocation — for the unsafe/deferred case once the Builder finishes, or to reconcile ahead of a merge (`--dry-run` previews the surgery):

```bash
./.loom/scripts/reconcile-stack.sh <child-pr> feature/issue-<parent>
# = git rebase --onto <default-branch> feature/issue-<parent> <child-branch>
#   git push --force-with-lease
#   gh pr edit <child-pr> --base <default-branch>
```

**Rebase-on-parent-amend now ships too (v2 item 3, #3747).** Items 1 and 2 both handle the *parent-merge* moment; item 3 closes the far more common *pre-merge* case: while a stacked parent's PR (`feature/issue-<parent>`) is still open under review and Doctor amends the parent branch (interactive rewrite or additive commits + force-with-lease), any child that branched off the parent's *pre-amend* tip goes silently stale. The standalone `./.loom/scripts/rebase-stacked-children.sh feature/issue-<parent>` discovers open child PRs with the same live-forge query (`gh pr list --base feature/issue-<parent> --state open`), detects staleness per child via `git merge-base --is-ancestor origin/<parent> origin/<child>` (up-to-date children are skipped), and reuses item 1's safe/unsafe split on the child **issue's** `loom:building` label: safe stale children are rebased onto the parent's current tip (`git rebase origin/<parent> <child>` + `git push --force-with-lease`, **base NOT retargeted** — the child stays stacked on the parent), while unsafe children (issue still `loom:building`) get a deferred-rebase comment instead. It is manual-first (like v1's `reconcile-stack.sh`): **Doctor runs it as a documented workflow step 9a** after pushing to a `feature/issue-<N>` branch (see `doctor.md`), best-effort — a failure never fails the Doctor's own work. `--dry-run` previews the per-child outcome.

**Epic #3747 status (complete):** all four v2 items shipped — the **merge-ordering guard** (item 2), **rebase-on-parent-amend** (item 3, above), and **out-of-set dependency detect-and-warn** (item 4 — the *safe* half of broad dependency-awareness; see "Out-of-set dependency detect-and-warn" below). The two remaining v1-deferred ideas were **decided won't-do** (operator, 2026-07-23): **diamonds / multi-parent** (kept single-parent by design — `depends_on: Option<u32>`; reopen via a fresh issue if a real diamond need appears) and **auto-detach** (proving non-dependence is unreliable — an operator action, not automated). The unsafe **auto-expansion** form of dependency auto-detection is likewise rejected: item 4 ships *detection + advisory only*, never silently reaching out to external issues.

### Auto-stack detection and wave ordering (`--auto-stack`, #3759)

This section is the single home for the opt-in `--auto-stack` behavior. It is entered **only** when `AUTO_STACK=true` (Modes A/B). Absent the flag, none of this runs and the sweep is byte-for-byte unchanged. It **generalizes the single-value `--depends-on` / `worktree.sh --base` / auto-reconcile mechanics above (already shipped, #3729/#3747/#3752) from one global value to a per-issue dependency map** — it does **not** introduce any new worktree/PR/merge machinery. Mode C never runs this (no Builder phase to stack).

**1. Detection — authoritative body-text signal, same-candidate-set only.** During the Stage 0 candidate survey (which already reads each candidate's `title,labels,state` — auto-stack adds `body` to that same `gh issue view N --json` read, **no new API call**), grep each candidate's body for the dependency phrases. **Reuse the exact regex vocabulary already established in `defaults/.claude/commands/loom/guide.md` (`parse_dependencies`, the `(Blocked by|Depends on|Requires|\- \[.\])[*_:[:space:]]*#[0-9]+` convention — tolerant of markdown emphasis/colon between the phrase and `#N`, #4508), restricted here to `Depends on` / `Requires` only:**

```bash
# Modeled on guide.md's parse_dependencies — restricted to the two declaration phrases.
# Deliberately EXCLUDES `Blocked by` (that phrase drives the distinct loom:blocked
# unblock machinery in guide.md / champion-reference.md and is NOT repurposed here)
# and EXCLUDES the `- [ ]` task-list form (not a stacking declaration).
# Two-stage (#4508): select matching lines, tolerant of markdown emphasis/colon
# before the first #N, then extract every #N on those lines.
echo "$BODY" | grep -E '(Depends on|Requires)[*_:[:space:]]*#[0-9]+' | grep -oE '#[0-9]+' | tr -d '#' | sort -u
```

A matched `#A` becomes a **stacking edge only when `#A` is also a member of this sweep invocation's own deduplicated candidate list.** A `Depends on #A` naming an issue **outside** the candidate set is left completely untouched — it is not an edge, it does not stack, and it flows through the existing `loom:blocked` handling exactly as today (this feature never touches out-of-set references). This "same-candidate-set only" restriction is load-bearing: it is what keeps auto-stack scoped to one sweep's own resolved set and prevents it from silently reaching out to arbitrary external issues.

**2. Linear, single-parent edges only — no diamonds.** A candidate may declare at most **one** in-set parent, mirroring #3729's `Option<u32>` design (do **not** build a `Vec` of parents). If a body matches multiple in-set `#A` references, take the **first** and log a warning that only single-parent edges are honored (`WARNING: #<child> declares multiple in-set parents (#a, #b) — honoring #a only (single-parent edges)`). Diamonds / multi-parent stacks are structurally unrepresentable, consistent with #3729/#3747.

**3. Cycle guard — drop, never silently orient.** If the surviving edges form a cycle within the candidate set (e.g. `#128 Depends on #129` and `#129 Depends on #128`), **drop every edge in the cycle** and log a warning (`WARNING: dropped cyclic stacking edges among #128 #129 — building independently`), falling back to default (unstacked) behavior for those issues. Never silently pick a direction.

**4. Wave ordering — topological, parent at-or-before child.** After edges resolve, order candidates so **every parent lands in a wave at or before its child's wave** — a topological pass restricted to the linear-chain shape above (no general DAG solver). A parent/child pair **may** land in the *same* wave: in that case the child's Builder still branches off the parent's branch via `--base feature/issue-<parent>`, **not** off the shared pre-wave `main` snapshot its other wave-mates use. This ordering feeds the existing wave-partition pass (Stage 0 step 2 / the wave partition consumed by the Wave Lifecycle) — it reorders the candidate list, then the normal `--builders-per-wave` chunking applies.

**5. Per-issue `DEPENDS_ON[N]` map.** The detected edges populate a per-issue lookup `DEPENDS_ON[N] = <parent>`. This is the sole new data structure — it **generalizes** the pre-existing single global `DEPENDS_ON` value (from `--depends-on <parent>`) into a map keyed by child issue. Sourcing precedence: an explicit single-issue `--depends-on N` seeds `DEPENDS_ON[N]` and a detected auto-stack edge for `N` never overrides it; every other entry comes from detection. The **Builder-phase gate** and the **daemon-dispatch loop** consume `DEPENDS_ON[N]` (see those sections) — the underlying `worktree.sh N --base feature/issue-<parent>` and `gh pr create --base feature/issue-<parent>` mechanics are **not** touched, only how the per-issue parent value is sourced.

**6. Operator confirmation — reuse the existing gate.** When `--auto-stack` finds ≥1 edge, the "Detected stacking pairs" block (see the Stage 0 dry-run output spec) is printed as part of the same candidate-set display Mode B / `--dry-run` already show before awaiting confirmation. **Mode A** (explicit numeric list, today's no-prompt fast path) gains a confirmation prompt **only when `--auto-stack` actually found ≥1 edge** — a zero-edge `--auto-stack` run on Mode A stays prompt-free (identical to the flag being absent). Declining exits cleanly, matching every other gate in this skill. Mode B already prompts, so this adds only the stacking block to its existing display.

**Explicitly out of scope for v1** (do not attempt here): file-overlap-heuristic auto-detection (#3729 rejected file paths as a topology signal — the reactive #3647 in-wave overlap-and-revalidate gate stays the backstop for *accidental* same-file collisions this feature doesn't stack); diamonds / multi-parent stacks; cross-`/loom:sweep` coordination (two independently-running sweeps stacking each other's candidates is #3768's scope — this feature only ever stacks within one sweep invocation's own resolved candidate set); `Part of #A` / `Blocked by #A` timeline cross-reference detection; and any change to `merge-pr.sh` / `reconcile-stack.sh` / `worktree.sh` (reconciliation is reused unchanged).

### Out-of-set dependency detect-and-warn (v2 item 4, #3747)

This is the **safe** half of broad dependency-awareness: the *detection* of dependency references that point **outside** the sweep's resolved candidate set, **without** the unsafe auto-expansion `--auto-stack`'s "same-candidate-set only" restriction (above) deliberately forbids. It runs **unconditionally** in Modes A and B (there is no flag to enable it — an out-of-set dependency is always worth surfacing), and it **never modifies the candidate set**. Where `--auto-stack` acts on **in-set** `Depends on`/`Requires` edges (to *stack* them), this pass warns on **out-of-set** `Depends on`/`Requires`/`Part of` references (to *surface* them). Mode C never runs it (no Builder phase / candidate issues to stack).

**Mechanism.** During candidate-set resolution, for each resolved candidate issue, scan its `body` for dependency references and warn on any that would build against a base the sweep isn't producing:

```bash
# Reuses guide.md's parse_dependencies vocabulary (the same convention #3759's
# --auto-stack derives from), restricted to the three DECLARATION phrases:
./.loom/scripts/warn-out-of-set-deps.sh \
    --candidates "<resolved candidate issue numbers>" \
    --depends-on "<operator --depends-on values, if any>"
```

- **Parser reuse (not a second parser).** `warn-out-of-set-deps.sh` REUSES the exact `(Depends on|Requires|Part of)[*_:[:space:]]*#[0-9]+` vocabulary (tolerant of markdown emphasis/colon before `#N`, #4508) — a restriction of guide.md's `parse_dependencies` — rather than introducing a divergent parser. It EXCLUDES `Blocked by` (that phrase drives the distinct `loom:blocked` unblock machinery), exactly as `--auto-stack` does.
- **Warn condition.** For each referenced `#A` that is **open** AND **not** a member of this sweep's resolved candidate set AND **not** already covered by an operator `--depends-on`, emit a clear advisory warning, e.g.:
  `warning: issue #B declares "Depends on #A", but #A is not in this sweep's candidate set — pass --depends-on <A> or include #A to stack them; otherwise #B may build against a stale base.`
- **No auto-expansion — the load-bearing safety property stays intact.** The candidate set is **never** auto-grown to include `#A`; the tool never probes/expands to external issues beyond the single openness check on a referenced number. This is detection + advisory *only* — the inverse (auto-adding un-named external issues) was **rejected** (operator, 2026-07-23) precisely because it would break the same-set guarantee.
- **Non-blocking.** The warning never stops the sweep — the helper always exits `0`. In Mode A's no-prompt fast path the warnings go to **stderr/log** (never a prompt); in interactive/Mode B contexts they may also appear alongside the candidate-set preview before the confirmation gate.
- **Silent cases (no warning).** An **in-set** reference (that is `--auto-stack`'s domain), a reference already covered by an operator **`--depends-on`**, a **closed** dependency (nothing stale to build on), and a self-reference all produce **no** warning.
- **Dedup.** At most **one** warning per `(candidate, dependency)` pair, even if the body names the same dependency via multiple phrases.

The helper is covered by `defaults/scripts/tests/test-warn-out-of-set-deps.sh` (out-of-set open → warns; in-set → silent; `--depends-on`-covered → silent; closed → silent; dedup; non-blocking exit 0).

### 5. Judge phase (sequential per PR within the wave)

For each PR in the wave (including PRs whose Builder just ran *and* PRs routed in via a `builder-done` checkpoint), in the order the builders completed (or any deterministic order — wave-internal ordering is not load-bearing), run the Judge phase sequentially. **"Sequentially" means await each Judge's completion explicitly** (a bounded, non-blocking `TaskOutput` poll — see the context-safe recipe, #6168) — and, when Judge requests changes, await the inline Doctor→Judge cycle (step 6) — before dispatching the next PR's Judge. The harness may launch each Judge/Doctor Task async regardless of `run_in_background: false`, so this per-PR ordering is enforced by an explicit await, never by a dispatch flag (see "Subagent dispatch is async-only", #3822):

```
WAVE_MERGED_FILES = {}                          # union of changed paths merged so far this wave (#3647)
for pr in wave_prs:
    judge(pr)                                   # may approve or request changes — against the PR's own pre-wave base
    if changes_requested:
        doctor(pr)                              # Doctor->Judge cycle(s), up to the cap (see step 6)
    if still_approved:
        revalidate_if_overlaps(pr, WAVE_MERGED_FILES)   # step 7 gate — re-judge / Doctor if pr shares a file with an already-merged sibling
        merge(pr)                               # step 7
        WAVE_MERGED_FILES |= changed_files(pr)  # feed the next PR's overlap probe
post_wave_integration_gate()                    # step 8 — buildGate-against-main backstop for cross-file coupling
```

`WAVE_MERGED_FILES` is the load-bearing state for the intra-wave collision guard (#3647): it accumulates the changed-file paths of every PR already merged **in this wave**, so the step 7 gate can tell whether the next PR overlaps a sibling that has already landed. Seed it empty at the start of each wave (it does **not** carry across waves — each wave rebases onto a settled `main`). The `post_wave_integration_gate()` call (step 8) is the backstop for the cross-file case the file-path probe cannot see.

**Checkpoint skip.** For each PR:
- If `CHECKPOINT_PHASE == "judge-done"` for the corresponding issue, the Judge already approved the PR in a prior sweep run. Skip the Judge invocation and route the PR straight to Merge (step 7). The PR should already carry `loom:pr` (judge writes that label as part of the approve path); if it doesn't, the checkpoint and forge state have diverged — log a warning and re-run Judge.
- If `CHECKPOINT_PHASE == "doctor-done"`, Doctor has already addressed Judge's earlier feedback. **Re-run the Judge phase** for this PR — Judge has not yet evaluated the post-doctor diff in the current sweep run. (The previous Judge result that led to Doctor was `changes-requested`, not `judge-done`.)
- If `CHECKPOINT_PHASE == "judge-rejected"`, an earlier sweep run's Judge already completed and requested changes on this PR — the sweep was killed before the inline Doctor cycle finished. **Do NOT re-run the initial Judge pass.** Route directly to the Doctor phase (step 6) for this PR. **Forge/checkpoint divergence guard:** before trusting this checkpoint, verify the PR still carries `loom:changes-requested`:
  ```bash
  # Plain `gh` — NOT "$GH_READ": this recheck exists precisely to detect that a
  # concurrent process moved the PR on, which a cached label set would hide.
  gh pr view <PR> --json labels --jq '[.labels[].name] | contains(["loom:changes-requested"])'
  ```
  If it does not (e.g. a concurrent process already merged, re-judged, or otherwise moved the PR on), the checkpoint and forge state have diverged — log a warning and fall back to running Judge normally instead of trusting the stale checkpoint.
- Otherwise (`builder-done`, or no checkpoint yet because Builder just ran in this wave), run Judge normally.

- Load and follow the instructions in `.claude/commands/loom/judge.md` for the PR.
- The judge uses `gh pr comment` (NOT `gh pr review --approve`) because GitHub's self-review API restriction applies — see `judge.md` for the full explanation.
- Like the Builder above, the Judge files its own follow-up issues for out-of-scope discoveries via `./.loom/scripts/create-issue.sh` — see `judge.md` § "Creating Follow-up Issues"; the sweep orchestrator does not suppress this.
- **If a previous Judge attempt for this PR died mid-flight without writing a fresh checkpoint** (rate limit, crash), re-verify forge state and complete only the missing steps before re-dispatching — see "Mid-phase-death recovery" above.
- Expected exit states per PR:
  - **Approve** → PR labeled `loom:pr`. Write the `judge-done` checkpoint for this issue (carrying the PR number), then continue to Merge (step 7) for this PR, then advance to the next PR in the wave.
    ```bash
    # Append --model <resolved> when you passed a model param to the judge subagent (#3482).
    ./.loom/scripts/sweep-checkpoint.sh write N judge-done --task-id "$RUN_ID" --pr-number <PR>
    ```
  - **Request changes** → PR labeled `loom:changes-requested`. Write the `judge-rejected` checkpoint for this issue **before** continuing to Doctor, so a resume after a kill re-enters the Doctor phase directly instead of repeating this Judge pass:
    ```bash
    ./.loom/scripts/sweep-checkpoint.sh write N judge-rejected --task-id "$RUN_ID" --pr-number <PR>
    ```
    Continue to Doctor (step 6) **inline for this PR**, then re-judge, then merge or block. Do **not** write a `judge-done` checkpoint here — the PR is not yet approved. (Re-rejections after a Doctor cycle also write `judge-rejected` — with an `--attempt` — under the multi-cycle rules in step 6; the terminal rejection that exhausts the cap does not get a `judge-rejected` write. See step 6's "Doctor-cycle cap" bullets.)

**Why sequential and not parallel?** Parallel Judges add coordination complexity without clear benefit — each judge needs to checkout the PR and reason about it independently. Defer parallel-judge to a future issue if benchmarks justify it.

### 6. Doctor phase (inline per PR, only if Judge requested changes)

If Judge requests changes on PR `#X` mid-wave, **or `CHECKPOINT_PHASE == "judge-rejected"` resumed a Judge rejection that already completed in a prior sweep run** (see step 5's checkpoint skip — do NOT dispatch another initial Judge for `#X` in this case), run inline Doctor→Judge cycles for `#X` — **up to `sweep.max_doctor_cycles`** (default 1; see "Doctor-cycle cap" in the Execution Model) — before moving to the next PR's Judge:

- Load and follow the instructions in `.claude/commands/loom/doctor.md` for PR `#X`.
- **If a previous Doctor attempt for `#X` died mid-flight without writing a fresh `doctor-done` checkpoint** (rate limit, crash — the #3676 shape), re-verify forge state (pushed commit? already re-labeled `loom:review-requested`?) and complete only the missing steps rather than dispatching a fresh Doctor that would duplicate the pushed fix — see "Mid-phase-death recovery" above.
- **Model escalation (#3481)**: this Doctor is dispatched because of a Judge rejection, so resolve its model per "Model escalation on Judge rejection" in the Execution Model — pass `ladder[min(attempt - 1, len - 1)]` from `sweep.escalation` (cycle 1 → `ladder[1]`, default `opus`, resolved through `resolve-model.sh` to `claude-opus-5` — #3982) via the Task tool's `model` parameter, **unless** a tier-1/tier-2 pin applies (pins win) or escalation is disabled (`[]`/`false`). The pinned ID degrades to its alias on this Task-tool dispatch — run it through `resolve-model.sh --task-alias` (see "Pinned-ID degradation on Task-tool dispatch", #4282). **A Doctor dispatched from a resumed `judge-rejected` checkpoint resolves this identically** — read `attempt` from the checkpoint (`sweep-checkpoint.sh attempt N`); an absent `attempt` field means this is the first cycle, equivalent to attempt 2 (same convention as every other checkpoint reader in this doc).
- Doctor addresses the judge's feedback, commits the fixes, and pushes.
- **On successful Doctor completion**, write the `doctor-done` checkpoint for the issue (carrying the PR number, the attempt counter, and the model the Doctor actually ran on — escalated or pinned, #3482) **before** re-invoking Judge:
  ```bash
  # <attempt> is the cycle index + 1: 2 for the first Doctor cycle, 3 for the second, etc.
  ./.loom/scripts/sweep-checkpoint.sh write N doctor-done --task-id "$RUN_ID" --pr-number <PR> --attempt <attempt> --model <doctor-model>
  ```
  This way, if sweep is killed between Doctor and the follow-up Judge, the resume run will see `doctor-done` and re-enter at the Judge phase (step 5), not redo the Doctor work.
- On completion, re-label the PR from `loom:changes-requested` back to `loom:review-requested` and **re-run the Judge phase** (step 5) for this PR.
- **Cap: up to `sweep.max_doctor_cycles` Doctor→Judge cycles per PR (default 1).** If Judge still requests changes after the configured number of Doctor passes, mark this PR as blocked (`PR #X blocked: doctor cycle exhausted after <k> Doctor→Judge round(s); human attention required`), log the reason, and proceed to the next PR in the wave (do NOT block the wave on it). **Do NOT write a `judge-rejected` checkpoint for this terminal rejection** — the PR is leaving the sweep for this run, so leave the last checkpoint (`doctor-done`) as-is; the stale-checkpoint cleanup path handles it once the PR is closed or reconciled.
- **Re-rejection under the cap (multi-cycle, #4185).** If Judge requests changes again and the cap has **not** yet been reached (`sweep.max_doctor_cycles > 1`, or the distinct-defect grace cycle below is granted), write `judge-rejected` for this issue **before** dispatching the next Doctor cycle — same as the initial rejection in step 5, but this time carry `--attempt` matching the value the **next** `doctor-done` write will use, so a kill-and-resume re-enters the correct escalation cycle and the cap survives the kill:
  ```bash
  ./.loom/scripts/sweep-checkpoint.sh write N judge-rejected --task-id "$RUN_ID" --pr-number <PR> --attempt <next-attempt>
  ```
  Then proceed with the next Doctor cycle as usual.
- **Distinct-defect exception (default cap only).** When `max_doctor_cycles` is at its default of 1 and the second Judge rejection is a demonstrably distinct defect from the first (forward progress, not the same disagreement re-litigated), you MAY grant **exactly one** additional bounded Doctor→Judge cycle before blocking — single-use per PR, never composing with an operator-raised cap. Emit the required log line naming the distinction (`PR #X: granted one extra Doctor cycle — second rejection is a distinct defect (<short reason>)`). If granted, this is a "re-rejection under the cap" per the bullet above — write `judge-rejected` with the matching `--attempt` before the grace cycle. Same-defect or ambiguous rejections still block immediately (no grace, no `judge-rejected` write — see the cap bullet above). See "Doctor-cycle cap" for the full rule.

The Doctor cycle for `#X` does **not** block other PRs in the wave — but because Judge runs sequentially per-PR within the wave, the next PR's Judge waits for `#X`'s Doctor→Judge cycle to settle before it starts. This is the intended sequencing. "Waits for … to settle" means **await the Doctor Task's completion explicitly** (a bounded, non-blocking `TaskOutput` poll — see the context-safe recipe, #6168) and then await the re-run Judge — the harness may launch the Doctor async regardless of `run_in_background: false`, so this ordering is enforced by an explicit await, not a dispatch flag (see "Subagent dispatch is async-only", #3822).

### 7. Merge (per PR)

**Intra-wave overlap revalidation — run this BEFORE the merge below (#3647).** Every builder in this wave branched off the *same pre-wave `main`* (step 0's snapshot), and Judge (step 5) validated each PR against that shared base — never against the `main` that a *sibling* PR in the same wave just produced. So two PRs that both touch the same file can each pass independently and then break `main` once both land — a *semantic* merge conflict git reports as clean. As the Execution Model's base-branch-only callout states, GitHub's `mergeable`/`mergeStateStatus` compares each PR against the base branch alone and is **not** a sibling-PR conflict check, and the repo's branch ruleset gives **no** server-side protection here either: it has no `required_status_checks` and no "require branches up to date" rule, so `merge-pr.sh --auto` merges a clean-but-stale PR immediately without re-running checks against the new base. This gate is the **reactive** backstop for overlap the **proactive** partitioner ("Overlap-aware wave partitioning", #4161) could not separate into different waves; the step 8 integration gate closes the cross-file case this probe cannot see.

Before calling `merge-pr.sh` for PR `#X`:

1. **Cheap read-only overlap probe — a failed read is "unknown", never "disjoint" (#6390).** Fetch `#X`'s changed-file set and compare it against `WAVE_MERGED_FILES` (the union of paths already merged in this wave — see the step 5 loop), capturing the call's own success/failure explicitly rather than trusting an empty result:
   ```bash
   # Plain `gh` — NOT "$GH_READ". Everything from here to the merge call is
   # merge-gating: the last read before an irreversible action must observe
   # current state unconditionally (#4667).
   PROBE_FILES="$(gh pr view X --json files -q '.files[].path')"
   PROBE_RC=$?
   ```
   - **`PROBE_RC == 0`** (the read itself succeeded — regardless of how many lines came back) is the only case where `$PROBE_FILES` is authoritative:
     - **Disjoint** (no path in `$PROBE_FILES` shared with `WAVE_MERGED_FILES` — this includes a genuinely empty `$PROBE_FILES`, e.g. a real 0-file-diff PR) → **keep the fast path**: fall straight through to the merge below. Two PRs touching disjoint files are safe (the issue confirms this), so no revalidation latency is added. This is the common case. *(Caveat: file-path granularity cannot see cross-file semantic coupling — e.g. a `to_dict()` in a source file vs. an exact-dict assertion in a test file, which are disjoint paths. That class is the step 8 integration gate's job, not this probe's.)*
     - **Any shared path** → enter the revalidation path (step 2) before merging.
   - **`PROBE_RC != 0`** (the read failed — e.g. a 503/404/timeout during a forge outage or other transient failure) → the file list is **unknown**, not disjoint. An empty or missing `$PROBE_FILES` produced by a failed call carries no evidence either way — do **not** fast-path on it. Fall back to deriving `#X`'s changed-file set locally, scoped to `#X`'s own base/head (never a hardcoded ref — `<base>` is the default branch, or `feature/issue-<parent>` when `#X`'s issue is stacked per `DEPENDS_ON[N]`, and `N` is `#X`'s corresponding issue number):
     ```bash
     git fetch origin feature/issue-N
     git diff --name-only origin/<base>...origin/feature/issue-N
     ```
     Treat this fallback's output exactly like a successful `gh` read (disjoint → fast path, shared path → step 2). **If the fallback also fails** (no local access to `origin`, unresolvable ref, etc.), the overlap status is genuinely unknown — never guess disjoint. Default to treating `#X` as overlapping and enter the revalidation path (step 2), which is always safe (merely slower) even when it turns out the PRs did not actually overlap.
2. **Revalidate `#X` against the freshly-merged `main`.** Update `#X`'s branch onto the current `main` so it actually contains the already-merged sibling's changes:
   ```bash
   gh pr update-branch X    # or the forge equivalent (forge_update_branch)
   ```
   Re-check `mergeStateStatus` (`gh pr view X --json mergeStateStatus`) and route:
   - **`DIRTY`** (the merge introduced a textual conflict) → run an inline Doctor→Judge cycle for `#X` (step 6 — Doctor rebases onto the updated `main` and fixes, then re-Judge), then merge. Reuse — do **not** extend — the step 6 Doctor-cycle budget (a revalidation Doctor counts against `sweep.max_doctor_cycles` for `#X`, same as any other cycle).
   - **Clean, but the branch was updated** → the update pulled the sibling's changes into `#X`'s branch, so **re-run the Judge phase (step 5) against the integrated branch** before merging. Judge checks out the PR and runs its build/tests, which is what catches a *same-file* semantic break the pre-wave Judge could not. If the integrated build/tests fail → route to Doctor (or, if `#X`'s Doctor-cycle budget is already spent, mark `#X` `loom:blocked`, surface it, and do **not** merge a known-red change).
   - **A real break Doctor cannot clear in one cycle** → mark `#X` `loom:blocked`, log the reason, skip its merge, and continue with the rest of the wave (do not block the whole wave on it). Consistent with the step 6 cap.

Overlapping PRs in a wave are thus **serialized-with-revalidation**; disjoint PRs keep the parallel fast path. Under `--dry-run` nothing here runs — the plan may simply note that overlapping PRs in a wave will be serialized-with-revalidation.

Use the dedicated merge script (CLAUDE.md "Merging PRs" mandate — never `gh pr merge`):

```bash
./.loom/scripts/merge-pr.sh <PR_NUMBER> --auto
```

The script merges via the forge API and cleans up the worktree. `--auto` enables GitHub's server-side auto-merge queue (queues the merge until required checks pass); on PRs that are already in `CLEAN` state (fast CI), the script transparently falls back to an immediate merge — see #3371. **On a repo with GitHub auto-merge disabled** (`allow_auto_merge:false`), `merge-pr.sh` now detects the setting up front and degrades `--auto` gracefully to wait-for-checks-then-merge (immediate if already CLEAN) instead of failing (#3820) — so `--auto` is safe to pass uniformly here regardless of the repo's auto-merge setting; no per-repo branching is needed.

**If a previous Merge attempt for this PR died mid-flight without deleting the checkpoint** (rate limit, crash between `merge-pr.sh` success and the delete call), re-verify forge state first: if the PR is already **merged**, just delete the stale checkpoint — do **not** re-run the merge. See "Mid-phase-death recovery" above. (The step 1 stale-checkpoint cleanup is the belt-and-suspenders backstop for this.)

**On successful merge** (script returns 0), add `#X`'s changed-file paths to `WAVE_MERGED_FILES` (so the next PR's overlap probe sees them), then delete the issue's sweep checkpoint:
```bash
./.loom/scripts/sweep-checkpoint.sh delete N
```

This is the terminal state. The checkpoint must be removed so a future `/loom:sweep` invocation that references the same issue number (e.g., as part of a wider candidate set) doesn't take a `merge-done` short-circuit on the stale state. The stale-checkpoint cleanup in step 1 is the belt-and-suspenders defense if this delete is missed (e.g., sweep killed between `merge-pr.sh` success and the delete call); on the next sweep run that touches the issue, step 1 detects the closed-issue + checkpoint mismatch and removes it.

If `merge-pr.sh` fails (e.g., the merge queue rejects the PR, or required checks haven't passed and `--auto` is rejected), do **not** delete the checkpoint — leave it at `judge-done` so the next sweep retries the merge from a clean state. **Before logging why it failed, classify the failure text through "Forge write failure diagnosis (#6425)"** (Mode B, above) — a forge 5xx/outage signature or an unconfirmed permission-scope 403 must be logged as forge-transient / "will retry", never as a "needs operator attention" credential diagnosis without the positive-evidence check.

### 8. Wave settled → post-wave integration gate → advance to next wave

Once every PR in the wave has reached a terminal state (merged, blocked, or builder-failed), run the integration gate below **before** starting the next wave's builders.

**Post-wave integration gate (#3647).** The step 7 overlap probe is file-path-granular: it catches two PRs that edit the **same** file, but it **cannot** see cross-file semantic coupling. That is exactly the shape of the #3647 incident — PR A changed a `to_dict()` in a *source* file and PR B added an exact-dict assertion in a *test* file. Their changed-file sets are **disjoint**, so step 7 took the fast path for both, yet `main` went red once both landed. File-path overlap alone therefore cannot protect the original incident; this gate is the load-bearing backstop for it:

- **If a build/test command is configured** (`buildGate.command`, honoring `buildGate.enabled`, in `.loom/config.json`), run it once against the post-wave `main` — pull/refresh `main` to its just-merged state and run the command there. On failure, **halt the sweep**: do not start the next wave, log the failing command and its output, and surface the red `main` (e.g. leave a clear error in the summary and/or open a recovery issue). A red `main` must stop the run rather than compound across subsequent waves.
- **If no such command is configured**, the step 7 overlap revalidation is the only intra-wave protection — same-file collisions are caught, but cross-file semantic coupling (source-vs-test) is **not**. Log a one-line advisory recommending a `buildGate.command` for waves that cluster on one subsystem, and — per the issue's mitigation #3 — prefer placing issues likely to touch a shared serialization/schema surface in **separate size-1 waves** rather than parallelizing them.

Under `--dry-run` the gate does not run (no checkout, no command execution); the plan may note that a post-wave integration check would run if `buildGate.command` is configured.

Once the gate has passed (or is not configured), refresh this run's registry heartbeat (Step 0a's "Heartbeat refresh, at each wave boundary" — `./.loom/scripts/sweep-run-registry.sh heartbeat "$RUN_ID"`, best-effort, non-fatal) and advance to the next wave. Do not start the next wave's builders until the current wave's PRs are all settled and the integration gate (if configured) is green.

### 8a. Wave-boundary candidate re-verification (daemon/champion-active only, #4884)

The wave *plan* — which candidates land in which wave — is computed once, at the confirmation gate, before wave 1 ever starts. Per-issue pre-flight (step 1) re-verifies **one** issue immediately before it enters its own wave, but nothing re-checks the **rest of the still-queued candidate list** in between waves. When a daemon or Champion is independently active in the same repo, that gap is real: it can merge PRs and close issues out from under a plan the orchestrator has not looked at again since the confirmation gate (the #4884 incident — see "Modern daemon coexistence" below — merged 3 PRs and closed 4 issues while sweep waves 1-2 were still running, completing the entirety of the planned wave 4 and part of wave 3).

**Trigger.** Immediately after step 8's integration gate settles and before partitioning/dispatching the next wave (i.e. at every wave boundary except after the final wave, where there is no next wave to protect), check whether a daemon/champion is active using the detection already defined in "Coexistence (peer `/loom:sweep` and legacy daemon)" below — either the legacy `.loom/daemon-loop.pid` liveness check, **or** the modern case this issue is about: a reachable `loom-daemon` (reuse this run's Stage -1 `PROBE_DAEMON` result if already probed; otherwise a fresh 500ms `Ping`) whose resolved `.loom/config.json` has `autonomous.roleRunner.enabled=true` with `champion` present in `roleRunner.roles` (interval cadence) or `roleRunner.onIdle` (idle-edge cadence, #4364 — "champion-on-idle"). If neither signal is active, this step is a **no-op** — fall straight through to "advance to the next wave" exactly as step 8 already says, at zero added cost to the common single-runner case.

**When triggered**, before computing/dispatching the next wave's partition, re-read live forge state for **every** remaining (not-yet-dispatched) candidate in this sweep's list — the whole tail of the plan, not just the next issue up — and apply the **existing** step 1 rules, unchanged, to each:

- `gh issue view N --json state,labels,closedByPullRequestsReferences` per remaining candidate (uncached, same claim-arbitration rationale as step 1's per-issue read — a stale answer here is exactly the window a duplicate build or a stale Judge would slip through).
- Route each candidate through step 1's existing rules verbatim: closed → drop from the remaining list; `loom:building` (and not this sweep's own claim, per Step 1a) → drop; `loom:blocked` / `loom:operator-only` / `loom:needs-capability` → drop; the existing-PR probe (closing-keyword + timeline cross-reference union, both sources) → route to Judge/Merge instead of Builder, exactly as step 1 already does. When `SWEEP_ALL_AGGRESSIVE=true`, apply the aggressive-mode override / "Aggressive candidate taxonomy" table here too, not the conservative skips — same substitution rule step 1 already documents. **This step invents no new routing** — it is step 1's rules, re-run in a batch, at a different cadence.
- **New outcome class: "completed externally."** A remaining candidate found already **closed** here, whose close this sweep did not itself just perform (no `merge-done`/`judge-done` checkpoint written by *this run* for it), is classified `completed externally (daemon/champion)` rather than plain `skipped` — it did not fail pre-flight, it was finished by something else. Carry this classification into the Summary Output (see below) so a wave that looks "empty" is legible as external completion, not sweep failure.
- **Checkpoint hygiene.** For any candidate dropped here, delete a live `.loom/sweep-checkpoint/issue-<N>.json` immediately (`sweep-checkpoint.sh delete N`) rather than leaving it to be discovered later — this is the same stale-checkpoint cleanup step 1 performs on a closed issue at pre-flight, just applied proactively at the wave boundary so a candidate dropped here (and therefore never reaching its own step 1) doesn't strand an orphaned checkpoint for a future sweep run to trip over.
- **Scope.** Only touches the remaining, not-yet-processed tail of the candidate list — never re-touches a wave that has already settled (merged/blocked candidates from earlier waves are done, not re-opened by this step).

This step composes with, and does not replace, step 1: every surviving candidate still gets its own per-issue pre-flight immediately before its own dispatch, same as today — defense in depth against the (much shorter) window between this wave-boundary batch check and that candidate's actual dispatch. It also does not interact with `--auto-stack` ordering, `--builders-per-wave` sizing, or the step 8 integration gate — it only ever **drops or reroutes** candidates already in the plan; it never adds one or mutates a candidate this sweep has already dispatched a builder for.

## Summary Output

When the entire list has been processed, print a summary table that includes wave membership for each issue:

```
/loom:sweep complete. Processed M issue(s) across W wave(s):

  #123  → merged  (PR #456)                                              [wave 1]
  #124  → blocked (judge requested changes, doctor cycle exhausted)      [wave 1]
  #125  → skipped (already in flight: loom:building)                     [wave 1]
  #126  → blocked (builder failed: build error)                          [wave 2]
  #127  → merged  (PR #459)                                              [wave 2]
  #128  → merged  (PR #460; rate-limited (resumed: doctor TOKEN_EXHAUSTED mid-phase — fix already pushed, re-labeled + re-judged))  [wave 2]
  #129  → rate-limited (unresumable: judge TOKEN_EXPIRED mid-phase, human attention required)  [wave 2]
  #130  → merged  (PR #461; rate-limited (downgraded: builder MODEL_CREDITS_EXHAUSTED on opus — same attempt re-dispatched on sonnet))  [wave 2]
  #131  → merged  (PR #462; rate-limited (downgraded: builder SPEND_LIMIT on opus — same attempt re-dispatched on session default))  [wave 2]
  #199  → routed  (existing PR #200, judged in this wave)                [wave 2]
  #198  → merged  (existing PR #201, was loom:pr)                        [wave 2]
  #197  → skipped (multiple open PRs reference issue: #210, #211)        [wave 2]
  #196  → completed externally (daemon/champion; PR #212 merged, closed before wave 3) [wave 3]
  #195  → completed externally (daemon/champion; issue closed, no PR)    [wave 3]

Total: 7 merged, 2 blocked, 2 skipped, 1 rate-limited (unresumable), 2 completed externally.
```

Wave annotation makes it easier to triage failures (e.g., "every issue in wave 2 failed → probably a base-branch problem, not the issues themselves").

**`rate-limited` vs `blocked` (issue #3683).** These are semantically distinct — reuse the `TOKEN_EXPIRED` / `TOKEN_EXHAUSTED` vocabulary from `.loom/scripts/lib/classify-error.sh` for the reason. `blocked (...)` means the **work itself** failed (build error, doctor cycle exhausted) and a human must fix the actual problem. `rate-limited (...)` means only that a role subagent was killed by an account rate limit mid-phase, so an **extra orchestrator pass** was needed to reach the phase's expected exit state — it says nothing about work quality. A `rate-limited (resumed: <what completed>)` outcome already succeeded (the mid-phase-death recovery finished the missing steps); only a `rate-limited (unresumable: ...)` outcome — where the forge state cannot be recovered without human help — needs attention.

**Third reason prefix: `rate-limited (downgraded: ...)` (issue #5687, extended #6518).** Reserved for a kill that a model-substitution fallback recovered by re-dispatching the same attempt on a different model — either a **`MODEL_CREDITS_EXHAUSTED`** kill recovered one rung down the cost ladder ("Credit-exhaustion fallback"), or a **`SPEND_LIMIT`** kill recovered by omitting the `model` param entirely ("Spend-limit fallback"). Keep it distinct from the other prefixes and from the classes they name, because they tell the operator different things:

| Reason prefix | What died | What fixed it | Operator signal |
|---|---|---|---|
| `resumed: <phase> TOKEN_EXPIRED\|TOKEN_EXHAUSTED …` | the Claude **account credential** | token rotation + forge-state re-verification | account pool is thin — check `.bad_tokens` / add accounts |
| `downgraded: <phase> MODEL_CREDITS_EXHAUSTED on <model> — same attempt re-dispatched on <cheaper>` | the account's credits for **one model tier** | one rung down the cost ladder, same account, same attempt | the sweep is running above the tier the account can sustain — consider lowering `sweep.tierModels` / `sweep.optimization` rather than adding accounts |
| `downgraded: <phase> SPEND_LIMIT on <model> — same attempt re-dispatched on session default` | possibly the whole account's **spend cap**, possibly just `<model>`'s tier (scope not knowable from the signature alone) | the `model` param dropped, inheriting the session default | a spend cap is binding on this account — raise it at claude.ai/settings/usage, or check whether it recurs on the session-default model too (account-wide) |
| `unresumable: …` | any of the above | nothing — human needed | act now |

Always name the **classifier category** and the model substitution in a `downgraded:` reason. `MODEL_CREDITS_EXHAUSTED` / `SPEND_LIMIT` is what makes it greppable across runs, and the `<model> → <cheaper>` (or `→ session default`) pair is what lets the operator see how the run recovered. A `downgraded:` outcome that ends in `merged` needs **no** human attention — like `resumed:`, it already succeeded. A downgrade that ran out of options is **not** a `downgraded:` outcome; it is `rate-limited (unresumable: … no cheaper model rung available)` for credit exhaustion, or `rate-limited (unresumable: … no session-default fallback available)` for a spend limit. Do **not** report either as a forge GraphQL rate limit or as an account rotation — those are separate axes (see "Mid-phase-death recovery").

**Fourth reason prefix: `rate-limited (forge-transient: ...)` (issue #6425).** A **different axis again** from the three above — those name the Claude account/model dying; this names a **forge write** (merge, label, comment, PR/issue create) failing on an outage-shaped signature (5xx, "No server is currently available", a connection reset) or on an unconfirmed permission-scope 403. See "Forge write failure diagnosis (#6425)" (Mode B, above) for the full classification procedure (`is_forge_transient_error` / `forge_write_permission_confirmed` in `lib/forge-helpers.sh`) and its own rule: **never** promote this to a `blocked` "needs operator attention" / credential outcome without positive evidence (a same-credential read succeeding while the write fails, persisting across a retry) — and when you do, cite the check in the log line rather than using the `forge-transient:` prefix at all. This is the fix for the 2026-08-17 incident where two sweeps wrote a confident "GitHub App lacks write permission" diagnosis during a confirmed GitHub outage; both diagnoses were wrong (the writes succeeded normally once GitHub recovered).

**`completed externally` vs sweep-driven outcomes (issue #4884).** A third axis, distinct from both of the above: `completed externally (daemon/champion; ...)` means the candidate reached a terminal state (merged or closed) **without this sweep run doing the work** — a daemon/champion (roleRunner/champion-on-idle, or the legacy daemon) merged its PR or closed it independently, and this sweep's "8a. Wave-boundary candidate re-verification" (or step 1's per-issue pre-flight) discovered that on re-read rather than performing the merge/build itself. It is **not** a `merged` (this sweep did not produce or land that PR), **not** a `blocked` (nothing failed), and **not** a plain `skipped` (the candidate did not fail a pre-flight condition — it simply finished elsewhere first). Keep the three axes separate in the summary: `merged`/`routed` = this sweep drove the outcome; `blocked`/`skipped`/`rate-limited` = this sweep could not or did not proceed; `completed externally` = some other actor already finished the job. A wave with several `completed externally` entries is a signal the operator may want to check whether a daemon/champion is racing the sweep (see "Modern daemon coexistence" in Coexistence, below), not a sign the sweep itself is malfunctioning.

## Session Transcript Archival (completion hook, #3726)

After the entire sweep has settled (issue list exhausted / all PRs processed) and just before printing the Summary Output, run the transcript archiver once so this session's transcript and all its subagent transcripts are captured to durable storage — or, on the daemon path (no in-session wave settle and no Summary Output printed here), immediately after the last `mcp__loom__dispatch_sweep` call returns, alongside Step 0a's registry cleanup (see "The daemon-dispatch path" above). On the daemon path this orchestrator session itself has no role subagents of its own to archive — the work happens in the daemon's detached children, not here — so this step only captures the thin orchestrator session transcript; the cron periodic sync remains the backstop for the detached children's transcripts (see the "Daemon detached-child path" caveat below).

```bash
./.loom/scripts/archive-transcripts.sh
```

This is **safe to run unconditionally** — the archiver is a **no-op unless archival is opted in** (env `LOOM_TRANSCRIPT_ARCHIVE=<dir>` or `.loom/config.json → loom.transcriptArchive.enabled`). When enabled it copies `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/<cwd-slug>/` (the session's own `<uuid>.jsonl` plus the sibling `<uuid>/subagents/agent-*.jsonl` + `.meta.json` sidecars) into `<dir>/<repo>/<date>/<uuid>/`, emits an `agent-<id>`-keyed `index.json` join key, and is **idempotent** (a re-run copies nothing new).

**Caveat (why the cron backstop still matters):** at this completion point the session's own top-level `<uuid>.jsonl` may still be mid-flush — the final orchestrator messages can lag. The completion hook reliably captures finished subagents; the durable tail is guaranteed only by the cron-friendly periodic sync documented in CLAUDE.md ("Session Transcript Archival"). Run both.

**Guardrails apply** (off by default; destination `0700`/files `0600`; refuses if the destination is inside a git repo but not gitignored; prints a loud banner naming the destination when enabled). See CLAUDE.md → "Session Transcript Archival" for the full contract and the secrets caveat.

> **Daemon detached-child path (v1 refinement, not a blocker).** For sweeps dispatched by the daemon as detached children, the reaper in `loom-daemon/src/sweep_registry.rs` knows the child's PID and issue but **not** its Claude session-uuid, so it cannot yet trigger a precise single-session archive on exit. v1 relies on the **periodic sync** (which copies all recent sessions for the repo regardless of uuid mapping) as the backstop for the detached path; a reaper-triggered auto-invoke keyed on the child's session-uuid is a documented follow-on.

## Stop Conditions

Stop processing and print the summary when any of these conditions hold:

- The issue list is exhausted.
- The user interrupts (Ctrl-C or explicit stop).
- An unrecoverable error occurs (e.g., `gh` is not authenticated, repository state is broken). Log the error and exit.

This skill does **not** implement a disk-pressure *stop* condition (aborting an in-flight sweep when the disk fills), max-waves caps, or doctor-cycle global limits — those are deferred (see Limitations). It **does** apply a disk-headroom *gate* when resolving the auto wave size at Stage -1 (see "Resolve auto wave size"): the scratch-volume free space clamps the initial wave size down, but does not stop a running sweep.

## Host Sleep Readiness (#3350)

Long sweeps run for many minutes — sometimes hours overnight — and the host going to sleep mid-run tears down in-flight subagent sockets to `api.anthropic.com`, killing curator / builder / judge subagents and losing all their work (see #3350 for the incident report).

**Before the first wave — or, on the daemon path, before the first `mcp__loom__dispatch_sweep` call** (see "The daemon-dispatch path" above) — run the host-sleep readiness check and surface its output to the user:

```bash
./.loom/scripts/check-host-sleep.sh
```

This is advisory-only. The script always exits `0` and **must not block** the sweep — proceed regardless of what it prints. It prints a platform-aware warning to stderr when the host is configured in a way that allows it to sleep:

- **macOS:** even with a user-idle sleep assertion (Amphetamine, `caffeinate -dimsu`, etc.), macOS Maintenance Sleep can still fire and tear down sockets. The reliable defenses are `sudo pmset -c sleep 0` or flipping your sleep manager's "allow system sleep when display is off" toggle to OFF.
- **systemd Linux:** wrap the session in `systemd-inhibit --what=idle:sleep --who=loom --why=sweep -- <cmd>`, which IS reliable.

If the user is running an overnight sweep, they should heed the warning before walking away.

## Main Branch Freshness (#3770)

During a long sweep, other PRs can merge to `origin`'s default branch. Because the installed `.loom/scripts/` and `.loom/hooks/` copies are synced from `defaults/` at install time, a local default branch that has drifted behind `origin` means the session may be executing **stale orchestration scripts** that silently lack recently-merged logic. This actually happened (#3770): during a 2026-07-22 sweep, `worktree.sh --base` (#3742) and `merge-pr.sh` auto-reconcile (#3752) were absent from the copies the session was running even though both had merged to `origin/main` — a running sweep had no signal it was behind.

**Before the first wave — or, on the daemon path, before the first `mcp__loom__dispatch_sweep` call** — run the main-freshness check and surface its output to the user (same timing and sibling role as the Host Sleep Readiness check above):

```bash
./.loom/scripts/check-main-freshness.sh
```

This is advisory-only. The script always exits `0` and **must not block** the sweep — proceed regardless of what it prints. It is strictly **read-only**: it never runs `git pull` / `git merge` / `git reset` and never auto-reconciles. It does a bounded `git fetch` of the default branch (degrading gracefully to the last-known ref when offline), then compares the local default branch against `origin/<default-branch>`:

- **Behind by N commits:** prints a bordered warning to stderr noting that installed `.loom/scripts/` / `.loom/hooks/` copies may be stale, with the remediation `git merge --ff-only origin/<default-branch>`. When it can resolve both trees it also best-effort notes any installed script/hook whose content differs from its `defaults/` counterpart.
- **Up to date:** prints nothing to stderr; a one-line stdout confirmation (suppressible with `--quiet`, matching `check-host-sleep.sh`).

If the check warns, the operator should refresh local `main` (and re-sync installed copies if their install flow does so) before relying on stacked-dependency or auto-reconcile behavior mid-sweep.

**This check is pre-wave-only — it does not cover a subagent spawned mid-sweep
(#6718).** It runs exactly once, before the first wave/dispatch, using a live
`git fetch` at that moment. A Builder/Judge/Doctor child spawned later in the
same run — potentially hours and many merges afterward — never re-triggers it,
and that child's own session context still carries whatever git-status
snapshot was captured when *its* session started. If that child's guidance
leads it to reason about base-branch divergence (e.g. "are my worktrees
branching from current work?"), it must run its own live check — this
pre-wave result and the child's own session-start context are both stale by
construction, not evidence about the present. See
[`troubleshooting.md` → "The base-branch trap: a session-start git snapshot is
not evidence about the present"](../../../.loom/docs/troubleshooting.md) for
the three-ref live check every base-branch-trap mention must point to, and for
why a reported divergence must carry the live command output that established
it.

## Outstanding Quarantine Stashes (#5185)

`check-main-clean.sh --quarantine` (see the Wave Lifecycle "Backstop" step above) rescues contamination it finds in the main worktree into a labeled `git stash` entry — `On <branch>: loom-quarantine: run=<RUN_ID> issue=<N>` — rather than discarding it. This is correct and loses no data, but the quarantine is otherwise recorded only in the structured `.loom/logs/main-quarantine.log` JSON log; nothing surfaces that a rescue stash is outstanding. A labeled stash can therefore sit indefinitely with nobody aware there is quarantined work to reconcile — noticed, if at all, only by chance (e.g. an unrelated command that happens to count stashes).

**Before the first wave — or, on the daemon path, before the first `mcp__loom__dispatch_sweep` call** — run the quarantine-stash check and surface its output to the user (same timing and sibling role as the Host Sleep Readiness and Main Branch Freshness checks above):

```bash
./.loom/scripts/check-quarantine-stashes.sh
```

This is advisory-only. The script always exits `0` and **must not block** the sweep — proceed regardless of what it prints. It is strictly **read-only**: it never pops, drops, or applies a stash. `refs/stash` is shared across every worktree of the repo (not per-worktree — see the #4821 note under "CRITICAL: Only Builders parallelize"), so the check is meaningful regardless of which worktree it runs from:

- **≥1 outstanding `loom-quarantine:` stash:** prints a bordered warning to stderr listing each stash's `stash@{N}` selector, relative age, and label (run id / issue number), with `git stash show -p <ref>` / `git stash apply <ref>` as the inspection/reconciliation commands.
- **None outstanding:** prints nothing to stderr; a one-line stdout confirmation (suppressible with `--quiet`, matching `check-host-sleep.sh` / `check-main-freshness.sh`).

If the check warns, the operator should reconcile each listed stash into the issue worktree it belongs to (or consciously drop it) — this does not block the current sweep, but stale quarantines accumulate silently otherwise.

## Sweep Child Working-Set Contract (#3980)

Every dispatched child of a sweep — Curator/Builder/Judge/Doctor/Champion subagents, and any test suite or tool subprocess they invoke — is expected to stay within a fixed filesystem working set:

- the **workspace root** it was dispatched into (the repo checkout or its issue worktree under `.loom/worktrees/issue-<N>`),
- **`.loom/`** (worktrees, logs, tokens, checkpoints),
- **`.claude*`** config directories, and
- **`$TMPDIR` / `/private/tmp`** scratch space.

This matters most on macOS running the daemon as a launchd LaunchAgent (#3972): unlike the legacy nohup model, a launchd job is its own TCC-responsible process, so any child that reaches outside this contract into a protected folder (`~/Desktop`, `~/Documents`, `~/Downloads`, `~/Pictures`, `~/Music`, `~/Library/Mobile Documents`/iCloud, …) triggers a fresh macOS permission prompt — see [`.loom/docs/daemon-reference.md` § "macOS TCC hygiene under launchd"](../../../.loom/docs/daemon-reference.md#macos-tcc-hygiene-under-launchd-3980) for the full incident, the fix already applied to `claude-wrapper.sh`'s crash-recovery path, and why Full Disk Access is never the right remediation.

Recursive scans that escape this contract — `find ~`, `du -sh ~`, `grep -r` rooted at `$HOME`, a script that `cd`'d to the wrong place before globbing, a test suite writing fixtures to `~/Documents` instead of a tmpdir, a tool resolving an iCloud-synced path — are **out-of-scope defects** in the offending role prompt, hook, or test fixture, not ambient behavior. If a sweep child needs scratch space, it should stay under the workspace root or `$TMPDIR`, never under a bare `$HOME`-relative path.

## Coexistence (peer `/loom:sweep` and legacy daemon)

`/loom:sweep` coexists with two **distinct** kinds of other runner, detected by two **separate** mechanisms. Do not conflate them: "another `/loom:sweep` is running" (peer detection, #3768) is not the same as "the legacy daemon is running" (daemon-PID check). Both warnings are **loud but non-blocking** — warn once, never auto-stop, never block.

### Peer `/loom:sweep` detection (#3768)

The primary coexistence case in the current architecture is **another live `/loom:sweep` invocation in the same repo**. This is handled at sweep start by "Sweep Run Identity + Peer-`/loom:sweep` Detection" (Step 0b, above): `sweep-run-registry.sh peers "$RUN_ID"` lists other runs whose registered liveness PID is still alive (pruning dead-PID entries so a SIGKILL'd peer never warns forever), and a loud non-blocking warning fires when any are found.

Two concurrent sweeps are now **run-state isolated**, not just label-isolated:

- **Per-issue `loom:building` claims** (step 1 pre-flight) already prevent two sweeps from building the same issue — if a peer claimed an issue first, this sweep sees `loom:building` and skips. The existing-PR probe (#3359) is the complementary defense when a PR exists but the `loom:building` label was never set / since removed.
- **Main-clean baseline** is keyed by `RUN_ID` (`main-clean-baseline-${RUN_ID}.txt`), so a peer sweep's `--snapshot` can never clobber this run's pre-sweep baseline (the #3648 contamination backstop stays correct under concurrency).
- **Checkpoints** carry this run's `RUN_ID` as `task_id`, so a sweep can tell its own `.loom/sweep-checkpoint/issue-<N>.json` writes apart from a peer's.

What remains a shared, un-isolated surface is the **default branch itself**: both sweeps merge into a moving `main`, unaware of each other's in-flight PRs. The peer warning exists so the operator knows that; isolating the merge target is out of scope for #3768 (stacking is #3759's concern).

### Legacy daemon coexistence

> **Note**: the legacy `./.loom/scripts/daemon.sh` was removed in #3432 and is not restored. The historical PID-file daemon (`.loom/daemon-loop.pid`) is not part of the current architecture; the check below is a defensive coexistence guard that fires only if such a process is somehow already running — normally a no-op. The Tier 2 dispatch backend is now the Rust `loom-daemon` binary (observed via `mcp__loom__list_sweeps`); the background agent-pool control surface is `.loom/bin/loom start|status|stop`.

`/loom:sweep` does not require the daemon and does not interact with `.loom/daemon-state.json` for writes. If a legacy daemon process is running, `/loom:sweep` and the daemon may both try to claim the same `loom:issue` label. This is a **different** mechanism from peer-`/loom:sweep` detection above — the daemon is identified by its own `.loom/daemon-loop.pid`, not by the `.loom/sweep-run/` registry.

**Coexistence behavior:** before the first wave, check whether the daemon is running. If it is, warn the user once at the start of the sweep:

```bash
PID=$(cat .loom/daemon-loop.pid 2>/dev/null)
if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
  echo "⚠️  Loom daemon is running (PID $PID). /loom:sweep will race with the daemon"
  echo "   for issues in the loom:issue queue. Consider stopping the pool first:"
  echo "       ./.loom/bin/loom stop"
fi
```

Do not auto-stop the daemon. Do not block on this warning — proceed with the sweep. The same dead-PID liveness pattern (`kill -0`) is used by peer-`/loom:sweep` detection.

### Modern daemon coexistence (`autonomous.roleRunner` / champion-on-idle, #4884)

> **This is a different mechanism from "Legacy daemon coexistence" above — do not conflate the two.** The legacy PID-file daemon only ever raced `/loom:sweep` for `loom:issue` **label claims**. The modern Rust `loom-daemon`'s role runner is materially more disruptive: `autonomous.roleRunner.enabled=true` with `champion` in `roleRunner.roles` (interval cadence, every 5-15 min per role) or in `roleRunner.onIdle` (idle-edge cadence, #4364 — the config commonly called "champion-on-idle") periodically dispatches a live Champion subagent that **merges approved PRs and closes issues directly on the forge**, not merely claims labels. See [`.loom/docs/daemon-reference.md`](../../../.loom/docs/daemon-reference.md) for the full `roleRunner` config surface.

A `/loom:sweep` run sharing a repo with an active role-runner Champion can therefore find its own planned candidates already merged or closed by a later wave — externally completing part of the wave plan the confirmation gate committed to. This is the exact incident #4884 documents: on 2026-07-31, the daemon's role-runner Champion merged 3 PRs and closed 4 issues while a sweep's waves 1-2 were still running, completing the sweep's entire planned wave 4 and part of wave 3 before those waves ever started.

**Coexistence behavior:** `/loom:sweep` does not pause, stop, or coordinate with a role-runner Champion — same no-daemon-state-writes posture as the legacy-daemon case above. Instead, the two re-verification defenses catch the drift: per-issue pre-flight (step 1 of the Wave Lifecycle) re-reads live state for each candidate immediately before it is dispatched, and "8a. Wave-boundary candidate re-verification" (Wave Lifecycle, #4884) re-reads the **entire remaining candidate list** at every wave boundary specifically because a role-runner Champion can complete several candidates between waves, not just between pre-flight and dispatch of one issue. A candidate found already merged/closed by either check is logged and surfaced in the Summary Output as `completed externally (daemon/champion)`, distinct from a sweep-driven `merged`/`blocked`/`skipped` outcome (see "Summary Output" above). Detecting whether a role-runner Champion is active: a reachable `loom-daemon` (Stage -1's `PROBE_DAEMON`, reused if already probed this run) whose resolved `.loom/config.json` has `autonomous.roleRunner.enabled=true` with `champion` in `roleRunner.roles` or `roleRunner.onIdle`. As with the legacy-daemon and peer-`/loom:sweep` cases, this is **loud but non-blocking**: warn once (naming which mechanism was detected — legacy PID-file vs. modern role-runner), never auto-stop the daemon or Champion, never block the sweep.

## Constraints

- **Wave model, one level deep.** When `--builders-per-wave > 1` (Modes A/B only), dispatch `loom-builder` / `loom-judge` / `loom-doctor` subagents **directly from this orchestrator session** in a single tool-call block. In Mode C, dispatch `loom-judge` and `loom-doctor` as **single subagent Tasks** per PR (size-1 waves). **Never invoke `/loom:sweep`, `/loom:judge`, or `/loom:doctor` as a subagent from `/loom:sweep`** — that is the two-levels-deep pattern that triggers the #3289 stall. See "CRITICAL: One level deep" in the Execution Model.
- **Per-PR Judge is sequential within a wave.** Builders parallelize (Modes A/B); judges do not. Mode C inherits this: PRs are processed one per size-1 wave. Don't parallelize judges or PRs without a separate design pass.
- **Configurable Doctor→Judge cycle cap per PR (`sweep.max_doctor_cycles`, default 1).** Inline within the wave (Modes A/B issue-side and Mode C PR-side both enforce this). If Judge still requests changes after the configured number of Doctor passes, the PR is blocked — do not retry indefinitely. At the default cap of 1, the orchestrator may grant one extra bounded cycle when the second rejection is a demonstrably distinct defect (logged, single-use, never on an operator-raised cap) — see "Doctor-cycle cap".
- **Mode C skips Curator, Approval gate, and Builder.** These phases already ran (the PR exists). Re-running them would be incorrect.
- **No new labels.** Use only the existing Loom label set (see `.github/labels.yml`). Mode C operates entirely on `loom:review-requested`, `loom:changes-requested`, `loom:pr`, `loom:blocked`, `loom:operator-only`, `loom:needs-capability` — all existing.
- **No `gh pr merge`.** Always use `./.loom/scripts/merge-pr.sh` (uniform across Modes A/B/C).
- **No daemon-state writes.** Read-only access to `daemon-state.json` for situational awareness.
- **Read the issue body** (`gh issue view N --json body`) before briefing the builder (Modes A/B). Mode C uses the PR diff + comments as the source of truth and does not need the issue body.
- **Skip operator-only / needs-capability items.** Issues labeled `loom:operator-only` or `loom:needs-capability` (Modes A/B, see issue-set Wave Lifecycle step 1) and PRs labeled `loom:operator-only` or `loom:needs-capability` (Mode C, see C0) are skipped. Log and move on.

## Limitations (Deferred for Follow-up Issues)

The full `/loom:sweep` design in #3298 includes many features that are intentionally **not** part of this skill yet. Each of these is a candidate follow-up issue:

| Feature | Status | Notes |
|---------|--------|-------|
| Parallel waves (`--builders-per-wave N`) | **Implemented (#3316, auto default #3566, core-scaled #3693)** | Omitted flag resolves to an auto wave size at Stage -1 (#3566): up to 10 on the daemon detached-process path, core-scaled within `[3, 6]` (`clamp(floor((cores-2)/4), 3, 6)`, #3693) on the in-session subagent path. The `[3, 6]` band is **subagent-path-specific** (floor 3 is the #3289-safe validated minimum, ceiling 6 keeps a margin below single-account rate-limit burn and orchestrator context pressure — warns above only on explicit override `>= 7`); the daemon path scales to 10 because each sweep is an isolated process, not a nested subagent. This is a **width** knob — the #3289 "one level deep" nesting rule is unchanged: no nested `/loom:sweep` subagent. Issue-side only; ignored in Mode C. |
| Natural-language selectors (label/author/title/time-window filters via NL description) | **Implemented (#3318)** | Mode B in Arguments. Out-of-band queries (body/diff inspection, file-touch filters) still trigger clarification. |
| Build-everything sentinel (`/loom:sweep all`) | **Implemented (#3568; aggressive whole-backlog redefinition)** | Bare, sole `all` token (case-insensitive) resolves **every** open issue via `gh issue list --state open` (no label filter) and aggressively drives each toward a merged PR: curates uncurated/`loom:triage`/`loom:curating` issues, reclaims stale `loom:building` claims (one-time `recover-orphaned-shepherds.sh --recover` pass + `updatedAt` staleness) — since #6167 the same pass also reclaims stale PR-side `loom:reviewing`/`loom:treating` claim overlays on open PRs — probes `loom:blocked` for a cleared blocker, fans `loom:epic` out to its `loom:epic-phase` children, and routes existing open PRs to Judge/Doctor/Merge via the #3359 probe (which takes precedence). Only `loom:operator-only` and `loom:needs-capability` (#5817) are hard-skipped, except a capability-matched `loom:operator-mechanical` item, which routes to a propose-only (never live) lane (#6893). `all --prs` resolves every open PR (Mode C C0 filters non-actionable); a no-actionable-label advisory block (#6218) names any resolved PR C0 will skip, at the confirmation gate, before it silently drops out. Mandatory confirmation gate; `--dry-run` / `--builders-per-wave` / `--no-daemon` compose unchanged (recovery pass skipped under `--dry-run`). Multi-token `all …` phrases still route to Mode B/C. |
| `--dry-run` | **Implemented (#3319, extended in #3384)** | Prints the candidate plan (with wave grouping) and exits without mutating labels, worktrees, or PRs. Issue-set (Modes A/B) and PR-set (Mode C) output formats. |
| Existing-PR detection in pre-flight | **Implemented (#3359, #3677, phrase filter #6216)** | Pre-flight probes the union of `closedByPullRequestsReferences` (closing-keyword PRs) **and** timeline `cross-referenced` open-PR events that are phrase-confirmed against the PR body (non-closing `Part of #N` / `Contributes to #N` PRs only — a bare mention of `#N` in a PR body no longer counts, #6216); routes existing open linked PRs to Judge (or Merge if already `loom:pr`) instead of dispatching a duplicate Builder. Multi-PR ambiguity skips with a log. |
| `loom:operator-only` enforcement | **Implemented (#3360)** | Pre-flight skips issues with `loom:operator-only` (human action required: credentials, infra, hardware). Champion `--merge` mode also refuses to auto-promote them. |
| `loom:needs-capability` enforcement | **Implemented (#5817)** | Same hard-skip parity as `loom:operator-only` above, in both the Aggressive candidate taxonomy table and Mode C's C0 pre-flight — a narrower claim (missing tool/agent capability, not operator-by-right) that today skips identically. |
| Capability-aware `loom:operator-mechanical` lane | **Implemented (#6885 Part 2, #6893)** | The one carve-out from `loom:operator-only`'s hard skip, in the Aggressive candidate taxonomy table and Mode C's C0 pre-flight (Mode A/B conservative skips unchanged). Four fail-closed gates: `LOOM_WORKER_CAPABILITIES` set on this host (unset ⇒ inert, the default everywhere); labels are exactly `loom:operator-only` + `loom:operator-mechanical` and none of the other three sub-kinds / `loom:needs-capability` / `loom:blocked` / `loom:operator`; the body declares ≥1 recognized `<!-- loom:capability=<name> -->` marker (#6892, parsed by `./.loom/scripts/extract-capability-markers.sh` — never a hand-rolled regex); every declared capability is held (ANDed). **Propose mode only** — commands or a PR for an operator to approve, never a live credentialed action; a live mode needs its own explicit opt-in. A declared-but-unheld capability posts a **capability request** comment naming the gap instead of stalling silently. Any judgement call mid-task hard-stops and relabels to `loom:operator-decision`/`-objective`. Daemon-side parity: `loom-daemon/src/capability.rs` + `WorkItem::is_skipped_with_capabilities`. See "Capability-aware `loom:operator-mechanical` lane". |
| `loom:operator` merge-hold enforcement | **Implemented (#6398)** | Unlike the two hard-exclusion rows above, `loom:operator` (`.loom/docs/label-state-machine.md`) is **not** a candidate-wide skip — it stays in the normal re-evaluation queue. The gap it closes is narrower: before routing an already-`loom:pr`-labeled PR straight to `merge-pr.sh` (issue-side existing-PR probe's `1, has loom:pr label` row, and Mode C's C1c), check the same PR labels already read for `loom:operator` and skip the merge (not the whole candidate) if present — Champion's merge-risk hold stays in force until a human clears it. `verdict-staleness-guard.sh` (#5686) does not clear this hold either, so the two checks are additive, not redundant. |
| Operator-gate advisory scan (`all` sentinel body-text phrasing + title-prefix) | **Implemented (#5137, extended #5817, #6391)** | The `all` sentinel's aggressive candidate survey scans each candidate's already-fetched `body` (no new API call) for instruction-shaped operator-gate phrasing (`operator-gated`, `Operator decision:`, `login-walled`, `requires credentials`, …), for a declared `Depends on`/`Requires #A` dependency where `#A` carries `loom:operator-only` **or** `loom:needs-capability` (the `#87 → #4` shape), and independently reads each candidate's `title` (one extra `--json title` call) for a prefix match against `Operator:` / `Operator —` / `Operator-only` (#6391 — catches a title-declared operator-gated issue whose body has none of the phrase vocabulary). Matches ANNOTATE the confirmation-gate listing and `--dry-run` plan with a `⚠` suffix — advisory only, never a hard skip, never a label mutation, never blocking. Zero matches ⇒ byte-for-byte unchanged output. `./.loom/scripts/warn-operator-gated.sh`, covered by `defaults/scripts/tests/test-warn-operator-gated.sh`. **Does not catch decision-shaped or credential/verification-shaped acceptance criteria** — those are a semantic gap, not a missing phrase; the confirmation gate's operator judgement is the backstop (#6197, see "What this scan does NOT catch" under "Operator-gate advisory scan"). |
| Checkpoint/resume after kill | **Implemented (#3373)** | Per-issue phase checkpoint at `.loom/sweep-checkpoint/issue-<N>.json`. Sweep reads on entry and skips completed phases. No mid-builder recovery — kill during Builder resumes at builder start, worktree preserved by `worktree.sh` idempotency. Mode C reuses the helper keyed by the PR's closing-issue number (`closingIssuesReferences`); PRs without a `Closes #N` reference run without checkpointing. |
| PR-set mode (`--prs` flag and PR NL triggers; Judge/Doctor/Merge from current PR label) | **Implemented (#3384)** | Mode C. Skips Curator, Approval gate, Builder. Size-1 waves. `--builders-per-wave` ignored. Reuses issue-keyed checkpoint via `closingIssuesReferences`. |
| Daemon backend detection (Stage -1) | **Implemented (#3454, daemon-owned-child short-circuit #3829, `--claim-owned` flag #4111)** | Strict-AND between daemon reachability and multi-account pool. Mode C, `--no-daemon`, and a daemon-dispatched child (`LOOM_SWEEP_CLAIM_OWNED` set or `--claim-owned N` passed, #3829/#4111) short-circuit to subagent — the last **before** any probe, so a daemon child never re-probes/re-dispatches the daemon that spawned it (the circular-round-trip idle-hang fix). No implicit auto-start. Dispatch-only — Phase D does not subscribe to the event bus. See "Stage -1: Backend detection". |
| Concurrent-`/loom:sweep` run-state isolation + peer detection | **Implemented (#3768)** | A stable per-sweep-run id (`sweep-run-registry.sh new`) is generated once at sweep start and threaded through all `--task-id` checkpoint writes and the main-clean baseline path (`main-clean-baseline-${RUN_ID}.txt`), so two concurrent sweeps no longer clobber each other's baseline or share an ambiguous `sweep-$$` `task_id`. Stage 0b adds a loud, NON-BLOCKING peer-`/loom:sweep` warning via a dead-PID-pruned run registry (`.loom/sweep-run/`). Merge-target (default-branch) isolation is out of scope — that is #3759's stacking concern. See "Sweep Run Identity + Peer-`/loom:sweep` Detection". |
| Sweep-owned forge-claim lease renewal (Phase 1 of epic #6165) | **Implemented (#6180)** | For a daemon-dispatched child only (Step 1a's self-claim signal): once, immediately after confirming self-claim ownership, `./.loom/scripts/sweep-lease-renew.sh start "$N"` spawns a detached background loop that idempotently PATCHes the `<!-- loom:lease host=... sweep=... -->` comment #6179's dispatch-time write leaves on `N` (never creates one), every ~5 minutes (configurable), for as long as THIS session's own long-lived process stays alive. Self-terminating — the loop stops on its own once this session exits, so the lease ages out rather than being explicitly deleted, and a `loom-daemon` restart mid-sweep never interrupts it (the daemon is not involved in renewal at all). See Step 1a in "1. Per-issue pre-flight". The daemon's own reclamation path reads it (Phase 2, #6286/#6287/#6288) and the sweep's own Builder phase fences on it before push/PR-open (Phase 3, #6309) — see the two rows below. |
| Daemon-side reclamation consults lease freshness (Phase 2 of epic #6165) | **Implemented (#6286/#6287/#6288)** | The daemon's periodic/startup reconciliation pass and the `recover-orphans` CLI both consult the freshest `loom:lease` comment's forge-assigned `updated_at` as the LAST gate before reclaiming a peer's `loom:building` claim (`loom-daemon/src/claim_reconciliation.rs` — `lease_is_fresh` / `fetch_freshest_lease_updated_at`, TTL = `LEASE_TTL_MINUTES_ENV`/`LOOM_LEASE_TTL_MINUTES`, default 15 min). `loom-daemon`-internal — see `defaults/docs/lease-record.md` § "Phase 2". |
| Sweep-side lease fencing before push/PR-open (Phase 3 of epic #6165) | **Implemented (#6309)** | The symmetric, sweep-owned counterpart to Phase 2 — fencing, not reclamation. Immediately before `git push` + opening the PR, the Builder phase runs `./.loom/scripts/sweep-lease-fence.sh check "$N"` (wired in `defaults/roles/builder-pr.md` § "Lease Fencing: Confirm You Still Own the Claim"), which reads the freshest `loom:lease` comment on `N` and confirms it is both fresh (same `LOOM_LEASE_TTL_MINUTES`/15-min-default TTL Phase 2 uses) and still owned by this sweep's own host. On failure (expired, exit `3`, or superseded by a different host's lease, exit `4`) the Builder aborts before pushing or opening a PR — it does not contest or clean up the peer's claim, the `loom:building` label is left alone. This bounds an acquisition-race overlap's cost to one wasted build rather than a duplicate reviewed/merged PR; it does not prevent the race itself (#4028 remains open). Runs unconditionally per-issue (fails open — exit `0` — when there is no lease to fence against, e.g. a manual `/loom:sweep`, GH Actions cron, or `--no-daemon` run), so this row has no Step-1a-style daemon-only gate unlike the two rows above. |
| `--max-waves` cap | Deferred | Operator-level brake on long sweeps. |
| `--paused-merge` / `--no-judge` | Deferred | Merge-mode variants for trusted batches. |
| `--include-blocked` (unblock pass) | Deferred | Currently `/loom:sweep` skips `loom:blocked` issues outright. |
| `--curator-also` (parallel curators on `loom:triage`) | Deferred | Parallel triage is a separate orchestration question. |
| Config-driven defaults (`.loom/config.json` keys `sweep.*`) | **Partially implemented** | `sweep.escalation` (#3481, model ladder) and `sweep.max_doctor_cycles` (#3668, Doctor-cycle cap, default 1) are live and read at lifecycle-entry time. Other `sweep.*` knobs (e.g. `--max-waves` persistence) remain deferred. |
| Disk-pressure *gate* on auto wave size | **Implemented (#3566)** | Stage -1 resolves the auto wave size against free space on the **worktree-root filesystem** (via `loom_worktree_root`, so it measures the dedicated scratch volume when `LOOM_WORKTREE_ROOT` / `worktree.root` is set — #3539/#3541), clamping the target down and logging the reason. `LOOM_PER_WORKTREE_GB` (default 2) is the per-worktree estimate. |
| Disk-pressure *stop* condition (abort a running sweep on low disk) | Deferred | Only the initial auto wave size is gated (above); no mid-sweep abort. Wave sequencing limits disk usage; revisit if waves grow large. |
| Doctor-cycle counting across PRs | Deferred | The per-PR cap is now configurable (`sweep.max_doctor_cycles`, #3668, default 1) with a default-cap distinct-defect grace cycle, enforced inline. A *cross-PR aggregate* cycle budget (e.g. "at most K total Doctor cycles across a whole sweep") is still deferred. |
| Parallel Judges within a wave | Deferred | Sequential per-PR Judge today; needs benchmarking before parallelizing. Mode C is also strictly sequential per PR (size-1 waves). |
| Parallel PRs in Mode C | Deferred | Mode C uses size-1 waves. Multi-PR-per-wave is feasible (one judge per PR in parallel) but inherits the same #3289 risk that gated parallel issue-side Judges. |
| Mixed-mode invocations (some issues + some PRs in one `/loom:sweep`) | Won't fix (split into two calls) | Routing logic for the cross product of issue-state × PR-state is complex; cleaner to require two invocations. |
| Multi-closing-issue PRs (PR with `Closes #N` + `Closes #M`) | Partial — runs without checkpoint | Mode C logs all closing issues and proceeds with Judge/Doctor/Merge but skips checkpointing for the PR. Multi-key checkpoint variant is a follow-up. |
| PRs without `Closes #N` references | Partial — runs without checkpoint | Mode C logs a warning and processes the PR without checkpointing. Judge/Doctor/Merge are idempotent at the GitHub-state level so re-running on the next sweep is safe. |
| Cross-wave backfill on pre-flight skips | Won't fix | Intentionally clean wave boundaries — see step 1 of the Wave Lifecycle. |
| Intra-wave collision guard (overlapping PRs off a shared base) | **Implemented (#3647)** | Step 7 runs a read-only file-path overlap probe before each in-wave merge; overlapping PRs are updated onto the just-merged `main` and re-Judged (or Doctor→re-Judge on `DIRTY`) before merging, disjoint PRs keep the fast path. Step 8 adds a post-wave `buildGate.command`-against-`main` integration gate — the load-bearing backstop for cross-file semantic coupling (source-vs-test) that path-overlap cannot see; halts the sweep on a red `main`. Symbol/AST-level overlap detection is out of scope. |
| Partition-time (proactive) overlap awareness | **Implemented (#4161)** | Pre-flight parses each candidate's `## Affected Files` into an estimated file surface (missing/"To be determined" → unknown surface, excluded from analysis, never blocked); same-wave overlapping candidates are greedily reordered into different waves without breaking `--auto-stack` parent/child wave ordering, unavoidable overlap raises an explicit confirmation-gate warning naming the shared files + candidates, and `--dry-run` prints an `Overlap analysis` block. Complements the reactive #3647 step 7/8 gates (the fallback for overlap the partition couldn't avoid) so the Doctor-rebase cost is avoided rather than paid. **File overlap is a *scheduling* signal only — it never creates a stacking edge (#3729's rejection of file paths as a stacking-topology signal stays intact).** Cross-sweep coordination (#3768) and diff/AST-level surface inspection are out of scope. |
| Spinoff-issue filing for out-of-scope discoveries | Deferred | Only *orchestrator-side aggregation* of role-filed spinoffs into the Summary Output is deferred (build it once we have richer summary output to surface them cleanly) — role subagents (Builder/Judge/Doctor) already file their own follow-up issues via `./.loom/scripts/create-issue.sh` per their own role docs, and the sweep orchestrator must never suppress that behavior in a dispatch prompt. |
| Daemon `pipeline_state` situational awareness reads | Deferred | Skill only warns when the daemon is running. |
| Top-level vs namespaced naming (`/sweep` vs `/loom:sweep`) | **Resolved** | Ships as the namespaced `/loom:sweep` (and `/loom:loom` for the daemon operator), matching CLAUDE.md and `help.md`. Originally #3298 open question #1. |

For the full design discussion (including the open questions raised by the curator), see issue #3298.

## Daemon event bus (Phase B of #3449 — #3453)

When the in-process **loom-daemon** is running, the sweep child **must** publish phase-transition events onto the daemon's in-memory pub/sub bus so monitoring tools, the spawn loop, and any subscribed MCP layer can react in real time. This is the **wire-protocol contract** the skill exposes to the daemon (and via the daemon to the rest of Loom).

The bus is an in-process `tokio::sync::broadcast::channel<Event>` with a default capacity of **1024** events. It is **not** NATS/ZeroMQ — it lives only inside the running daemon and is gone the moment the daemon exits. Subscribers route by **topic prefix** (segment-aligned — `sweep.issue` matches `sweep.issue.123.phase` but not `sweep.issuetype.foo`). Slow subscribers receive a synthetic `topic_lag` event when they fall behind, then resume at the current channel head (pass-through, no silent drops; matches tokio's `Receiver::Lagged` semantics).

### When to publish

Publish a `sweep.issue.{N}.phase` event **immediately after the sweep skill commits a phase transition** — i.e. once the phase is durable in the forge (label flipped, comment posted, checkpoint written via `sweep-checkpoint.sh`). Do not publish before the side effects have landed; downstream subscribers treat the event as the authoritative signal that the phase is complete.

Publish a `sweep.issue.{N}.blocker` event when the skill chooses to mark the issue with a Loom-recognized blocker label (e.g., `loom:blocked`, `loom:operator-only`) and exits the lifecycle without proceeding to the next phase.

The daemon publishes `sweep.issue.{N}.exited`, `sweep.issue.{N}.crashed`, `sweep.global.dispatch`, and `sweep.global.completed` itself — the sweep child does **not** publish those.

### Topic taxonomy (frozen for v0.10.0)

The following six topics are the **entire** event vocabulary for v0.10.0. New topics require a follow-up issue — do not invent topics outside this table.

| Topic | Publisher | Payload (JSON) |
|-------|-----------|----------------|
| `sweep.issue.{N}.phase` | Sweep child via `PublishEvent` | `{"phase": "<phase-name>", "pr_number": <int or null>, "repo": "<workspace-root>"?}` |
| `sweep.issue.{N}.blocker` | Sweep child | `{"reason": "<short-text>", "label_added": "<label>", "repo": "<workspace-root>"?}` |
| `sweep.issue.{N}.exited` | Daemon reaper | `{"exit_code": <int or null>, "duration_sec": <int>, "repo": "<workspace-root>"?}` |
| `sweep.issue.{N}.crashed` | Daemon reaper | `{"checkpoint_phase": "<phase-name or null>", "repo": "<workspace-root>"?}` |
| `sweep.global.dispatch` | Daemon | `{"sweep_id": "<id>", "kind": {"type": "Issue", "value": <N>}}` |
| `sweep.global.completed` | Daemon | `{"sweep_id": "<id>", "outcome": "exited" | "crashed"}` |

`{N}` is the issue number (a positive integer). Phase names match the sweep-checkpoint schema (#3373): `curator`, `builder`, `judge`, `doctor`, `merge`, etc.

**`repo` field (optional, #3929)**: the four `sweep.issue.{N}.*` payloads carry an additive `repo` field naming the owning managed-workspace root, so a subscriber on the shared bus can disambiguate two managed repos that each dispatched a sweep for issue #N (the topic string is issue-scoped only). The **daemon stamps `repo` automatically** on the events it emits (`exited` / `crashed`). For the **child-published** `phase` / `blocker` events, include `repo` in the payload sourced from the `LOOM_WORKSPACE` env var the daemon exports to the sweep child at dispatch (e.g. `{"phase": "builder", "pr_number": 501, "repo": "$LOOM_WORKSPACE"}`). `repo` is optional and backward-compatible — omitting it is byte-for-byte the pre-#3929 behavior, and single-repo subscribers ignore it.

### How to publish — IPC contract

The daemon exposes a `Request::PublishEvent { topic, payload }` variant over its line-delimited JSON Unix-socket framing (the same socket used for `DispatchSweep`, `ListSweeps`, etc. — see `loom-daemon/src/ipc.rs`). One request → one `Response::EventPublished { topic, receivers }` ack frame.

**Sample wire frame** — sweep child advertises that it just finished the builder phase and opened PR #501:

```json
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.phase", "payload": {"phase": "builder", "pr_number": 501}}}
```

The daemon responds with:

```json
{"type": "EventPublished", "payload": {"topic": "sweep.issue.123.phase", "receivers": 2}}
```

If no subscribers are listening, `receivers` is `0` and the event is dropped. **This is not an error condition** — the sweep child treats `receivers: 0` as "best-effort delivery, nobody home" and continues. Do not retry; the event is fire-and-forget.

### Sample payloads for the six initial topics

The following six samples are the authoritative reference for the payload schema of each frozen topic.

```json
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.phase", "payload": {"phase": "curator", "pr_number": null}}}
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.phase", "payload": {"phase": "builder", "pr_number": 501}}}
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.phase", "payload": {"phase": "judge", "pr_number": 501}}}
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.phase", "payload": {"phase": "merge", "pr_number": 501}}}
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.blocker", "payload": {"reason": "missing credentials", "label_added": "loom:operator-only"}}}
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.456.blocker", "payload": {"reason": "dependent on #999", "label_added": "loom:blocked"}}}
```

The daemon-side events (these are **emitted by the daemon**, not by the sweep child — included here as the contract for subscribers):

```json
{"type": "EventStream", "payload": {"events": [{"type": "SweepExited", "issue": 123, "exit_code": 0, "duration_sec": 1842}]}}
{"type": "EventStream", "payload": {"events": [{"type": "SweepCrashed", "issue": 456, "checkpoint_phase": "judge"}]}}
{"type": "EventStream", "payload": {"events": [{"type": "SweepGlobalDispatch", "sweep_id": "sweep-issue-789-1717599600", "kind": {"type": "Issue", "value": 789}}]}}
{"type": "EventStream", "payload": {"events": [{"type": "SweepGlobalCompleted", "sweep_id": "sweep-issue-123-1717599600", "outcome": "exited"}]}}
```

### Subscription (for tooling, not the sweep child)

Long-running monitors subscribe with a single `Request::SubscribeEvents { topics }` frame and receive a stream of `Response::EventStream { events }` frames on the same open connection. Topic matching is prefix-aligned: `["sweep.issue.123"]` matches every event for issue 123; `["sweep.global"]` matches the two global topics; `[]` (empty list) matches everything on the bus.

```json
{"type": "SubscribeEvents", "payload": {"topics": ["sweep.issue.123", "sweep.global.completed"]}}
```

The sweep child itself does **not** subscribe — it only publishes. Subscription is consumed by the operator-facing monitoring tools that shipped in Phase C (#3455) and any custom MCP-bridged tool an operator wires up.

### Failure modes (publisher side)

- **Daemon not running**: the Unix-socket connect fails. The sweep child must treat this as a soft error and continue without publishing — Loom is designed to run without the daemon. Log a single `debug` line and proceed.
- **Daemon running but no subscribers**: `Response::EventPublished { receivers: 0 }`. Fire-and-forget; continue.
- **Bus capacity exhausted on the subscriber side**: the slow subscriber sees a `topic_lag` event; **the publisher is unaffected** and never blocks. The bus is bounded but tokio's broadcast channel has pass-through overflow on the receiver, not the sender.

### Out-of-scope for Phase B

These live in the daemon, not the sweep skill — do **not** implement them in the sweep skill. The operator-facing MCP tools shipped in Phase C (#3455); the rest are frozen non-goals:

- Operator-facing MCP tools (`get_sweep_status`, `subscribe_to_events`, `tail_event_bus`) — daemon-side, shipped in Phase C (#3455).
- New topics beyond the six listed — frozen for v0.10.0 per epic #3449; file a follow-up issue if you "need" one.
- Distributed bus / cross-daemon coordination — explicit non-goal (single broker, in-process).
- Persistent event log or replay — explicit non-goal (transient bus).
- Consumer groups / durable subscriptions — explicit non-goal.

## Reference Documentation

- **Per-issue lifecycle**: the "Wave Lifecycle (Modes A and B only — issue-set)" section of this skill — canonical phase-by-phase reference (Curator → Builder → Judge → Doctor → Merge).
- **Builder skill**: `.claude/commands/loom/builder.md`
- **Judge skill**: `.claude/commands/loom/judge.md`
- **Doctor skill**: `.claude/commands/loom/doctor.md`
- **Curator skill**: `.claude/commands/loom/curator.md`
- **Label definitions**: `.github/labels.yml`
- **Merge script**: `./.loom/scripts/merge-pr.sh`
- **Sweep checkpoint helper**: `./.loom/scripts/sweep-checkpoint.sh` — read/write/delete per-issue phase checkpoints for resume after kill (#3373). Mode C reuses this via the PR's closing-issue number when available.
- **Original proposal & open questions**: issue #3298
- **PR-set mode (Mode C) design**: issue #3384
- **Nested-dispatch stall hazard**: issue #3289
- **Checkpoint/resume design**: issue #3373 (Phase 0 of #3372 shepherd/daemon deprecation epic)
- **Daemon backend detection (Stage -1)**: issue #3454 (Phase D of #3449 daemon rebuild epic)
- **Daemon dispatch MCP tool (`mcp__loom__dispatch_sweep`)**: issue #3452 (Phase A of #3449)
- **Daemon event bus (Phase B)**: issue #3453 (Phase B of #3449)
