# CI Refusals: Permission vs Rate Limit, and Re-runs (#10633)

**Load when**: `loom-daemon forge wait-checks` printed `LOOM-CHECKS-ERROR` naming
an HTTP `401`/`403`/`429`, it left a stderr note about legacy commit statuses,
or a job needs a re-run (cancelled by a runner shutdown, or flaky).

## Why this exists

GitHub uses HTTP `403` both for "this credential may not do that" and for
"slow down". Before #10633 a Doctor saw only `HTTP 403`. It could not tell a
missing GitHub App permission from a secondary rate limit, so it handed PRs
back with CI unverified. When it could not re-run a cancelled job, it rebased
and pushed instead, which cost a full CI cycle and opened a new conflict window.

## Reading a `wait-checks` refusal

`wait-checks` classifies every refused read, and the class is in the reason:

| Reason contains | Meaning | What to do |
|---|---|---|
| `permission (needs <perm>)` | The credential lacks that App permission. | Waiting will not help. Quote the line in your hand-back comment. The operator grants `<perm>` on the App. |
| `secondary-rate-limit` / `rate-limit` (after `read-failed:`) | GitHub throttled the reads for the whole wait. | Say CI is unverified **because of a rate limit**, not a permission problem. Judge re-checks later. |
| `credential` | The token was refused (`401`). | Report it. Do not retry. |
| `forbidden` | GitHub refused the action itself. Its message follows. | Read the message. |

A stderr note `wait-checks: legacy commit statuses unreadable (…)` means the
credential lacks **Commit statuses: Read**. The verdict then covers check-runs
only. A required status context still counts as missing (never GREEN), but a
failing *non-required* legacy status is invisible. With no check-runs at all
the wait ends `LOOM-CHECKS-ERROR statuses-unreadable: …` rather than `NONE`.

## Re-running a job

Use the daemon verb, not a hand-rolled `gh run rerun` or REST call:

```bash
out="$(loom-daemon forge rerun <run_id> --failed)"   # or: forge rerun --job <job_id>
case "$out" in
  LOOM-RERUN-OK*) ;;                                       # re-run queued; wait-checks again
  "LOOM-RERUN-DENIED permission"*|"LOOM-RERUN-DENIED credential"*)
    echo "$out" ;;                                         # do not retry; name the grant in your PR comment
  "LOOM-RERUN-DENIED secondary-rate-limit"*|"LOOM-RERUN-DENIED rate-limit"*)
    echo "$out" ;;                                         # try once more later, never in a loop
  *) echo "$out" ;;                                        # forbidden (e.g. already running) / ERROR: read the message
esac
```

It posts on the **writer** credential. A rerun is a write, and reader Apps hold
no write permission. `DENIED permission (needs actions:write)` means the writer
App lacks **Actions: Read and write**. Push to trigger a fresh run only if the
PR needs one anyway, and say in the PR comment that the re-run was refused for
that permission.
