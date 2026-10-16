# Judge: CI Is Red — Finished Run, Named Test, Recorded Flake Rerun (#12011)

**Load when**: `loom-daemon forge wait-checks` printed `LOOM-CHECKS-RED`, or
`post-verdict.sh … approved` exited 6, and a CI-failing verdict is next.

## Why this exists

On PR #11974 (YAML and docs only) a Judge posted `changes-requested` +
`loom:ci-failure` because one leg, `Rust Unit Tests (2/3)`, was red while other
jobs still ran. `wait-checks` reports RED as soon as one check fails, and
`gh run view --log-failed` cannot read a log until the whole run completes, so
the verdict named no test. The test was a tracked flake (#11073); attempt 2 was
green minutes later. This file prevents three things: a CI verdict on a
partial run, a CI verdict that names no test, and a changes-requested for a
known flake.

## 1. The run must be finished

```bash
err="$(mktemp)"; first="$(loom-daemon forge wait-checks <number> --timeout 20 --settle-red 2>"$err" | head -n1)"
```

| First line | Action |
|---|---|
| `LOOM-CHECKS-TIMEOUT*` / `-HEAD-MOVED*` | The run has not finished. Follow judge.md "When CI is Pending". No CI-failing verdict on a partial run. In single-PR mode you may instead wait in the foreground with `--timeout 1800 --settle-red`. |
| `LOOM-CHECKS-GREEN*` / `-NONE*` | The red was superseded. Continue the review. |
| `LOOM-CHECKS-RED*` | Finished red. `$err` holds `<name>\t<url>\t<run_id>` per failing check. Go to step 2. |
| `-ERROR*` or empty (older binary without `--settle-red`, or Gitea) | Run `gh pr checks <number>` once. If any check is still pending, treat it as TIMEOUT. |

## 2. Name the failing test or step

For each `run_id`, read the finished log:

```bash
gh run view <run_id> --json attempt,jobs -q '{attempt, failed: [.jobs[] | select(.conclusion=="failure") | {name, url, steps: [.steps[] | select(.conclusion=="failure") | .name]}]}'
gh run view <run_id> --log-failed | grep -E 'FAILED|FAIL |panicked|^failures:|error(\[|:)' | head -40
```

Write down the failing **test** (full path, e.g.
`observability::sender::tests::a_batch_is_cut_at_the_byte_budget_and_never_below_one_envelope`).
If the failure is not a test (clippy, fmt, a `check-*.sh` gate, a build), name
the **step**. "A job failed" is not a name. A check with `run_id` `-` (not an
Actions run) has no log here: name the check and its URL, then go to step 5.

## 3. Flake or regression?

Route to step 4 only when **all** of these hold. Otherwise go to step 5.

- **Attempt 1.** Each failing run's `attempt` is `1`. A head that goes red again on
  attempt 2 or later gets the normal verdict. Never rerun the same head twice.
- **Every failing test qualifies**, by one of:
  - **Tracked flake**: an issue, open or closed, whose title calls a test
    flaky or intermittent and whose title or body names that exact test
    (`gh issue list --state all --search '"<test name>" in:title,body'`).
    Merely naming it (the issue that added it, this PR's own) does not count.
    A closed tracker still counts: its fix did not hold (#11073). The diff
    must not touch that test or the code it exercises: there a red is the
    PR's until proven otherwise.
  - **Untouched**: the diff touches neither the test, the code it exercises,
    nor its fixtures, **and** the diff touches no CI or test-harness config
    (`.github/workflows/*`, `.config/nextest.toml`, `Cargo.toml`/`Cargo.lock`,
    build scripts, test fixtures). A CI-config diff can make an untouched test
    fail for real, so it never qualifies as untouched (it can still be tracked).
- **No failing step is a deterministic gate** (lint, fmt, build, `check-*.sh`)
  unless an open issue tracks that exact gate as flaky.

A PR-caused red misread as a flake costs one rerun: a red on attempt 2 gets
the normal verdict, and the tracker comment keeps attempt 1 on record.

## 4. Flake route: record, then rerun once

1. **Record it** (#7789: a retry records, it does not hide). If an open
   tracker exists, comment on it: the test, the PR, the head SHA, the run and
   job URLs, and "attempt 1, rerun requested by Judge". Otherwise (none, or
   only closed ones, which you link) file one:
   `./.loom/scripts/create-issue.sh --title "test flake: <test> fails on <job>" --label loom:triage --body-file <scratch file>`
   with the same facts plus why the diff cannot reach it.
2. **Rerun**: `loom-daemon forge rerun <run_id> --failed`, and branch on its
   sentinel as `ci-refusals-reference.md` shows. On `LOOM-RERUN-DENIED …` or
   `ERROR`, go to step 5 and quote the refusal in the verdict.
3. **On `LOOM-RERUN-OK`**: post a PR comment naming the test, the tracker and
   the rerun. Add no end-state label and no `loom:ci-failure`. Release the claim
   exactly as "When CI is Pending" does
   (`gh pr edit <number> --remove-label "loom:reviewing"`; `loom:review-requested` stays).
   In single-PR mode you may instead wait for the rerun with
   `wait-checks --settle-red --timeout 1800` and continue from step 1.

## 5. The CI-failing verdict

Run the Verdict-Time CAS Recheck immediately before this. **Do NOT approve.**
For an external approval-required workflow, say so and point at the operator,
not the Doctor.

```bash
./.loom/scripts/post-verdict.sh <number> changes-requested "$VERDICT_SHA" --body "$(cat <<'EOF'
❌ **Changes Requested - CI Failing**

Run <run_id> (attempt <N>) finished red:

- `<check name>`: test `<test path>` (or step `<step name>`) — <one-line error from the log> — <job url>

Not routed as a flake because: <the test is in code this PR changes / attempt <N> of this head is red again / rerun refused: LOOM-RERUN-DENIED …>.

Please fix these issues before the PR can be approved. I'll evaluate again once CI passes.
EOF
)" && \
  gh pr edit <number> --remove-label "loom:review-requested" --remove-label "loom:reviewing" --add-label "loom:changes-requested" --add-label "loom:ci-failure"
```

Every failing check gets a line, and every line names a test or a step.
