# Curator completion estimates

For every issue you actually curate or re-curate, write exactly one concrete,
standalone comment in the current issue body (replace its previous estimate):

```html
<!-- loom:estimate lines=400 files=4 doctor_risk=0.2 -->
```

Estimate from this curated issue's title, body and scope only. Never look up
other issues' outcomes, PR sizes, elapsed time or Doctor histories to rate it.
Lines and files are nonnegative integer estimates
of the eventual change; doctor_risk is a finite probability between 0 and 1.
These are predictions, not promises. Do not copy the example as a default.

`loom:estimate` is an observational forecast that no consumer may route, gate
or order on; story points are the only decision-bearing size. Estimates never
change approval, consolidation, dependency, readiness or decomposition
decisions. Do not add an
estimate to an untouched historical issue, a duplicate you close, or an issue
you defer without completing curation.

After saving the body and your points marker, and passing the consolidation
gate, run the native completion command. It is also the points gate (see
`curator.md`), and it applies `points:N` and `loom:curated`:

```bash
loom-daemon curation-complete --repo owner/repo --issue N \
  --model ACTUAL_CURATOR_MODEL --runtime ACTUAL_RUNTIME \
  --completion-id UUID --workspace-root MAIN_WORKSPACE
```

The command is the points gate and label writer only; it writes no telemetry.
`--model`, `--runtime` and `--completion-id` are still accepted but ignored, so
existing invocations keep working. Pass your actual model/runtime (or `unknown`)
and any UUID; never guess, and never change readiness or strand otherwise
complete curation over them. The workspace root must be the main checkout
monitored by the daemon.

The command fetches the current body and rejects missing, malformed, duplicate
or invalid points markers; backticks and fenced examples do not count. On
fetch/validation failure, keep the curation claim and retry or report the
failure. Retrying is safe: it resolves the points again and re-applies the
labels idempotently.

The `issue.estimate` fact is the daemon's job, not yours (#12118). When it
observes an open, recently updated issue carrying `loom:curated` and a valid
`loom:estimate` marker, it journals the fact once, keyed on repo, issue and the
marker values; the Curator's model and runtime are not observable there, so the
fact carries them as null. A malformed or absent marker emits nothing.
