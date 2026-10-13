---
name: loom-curator-estimate
description: "For every issue you actually curate or re-curate, write exactly one concrete, standalone comment in the current issue body (replace its previous estimate):"
---
<!-- loom-managed-skill -->
<!-- GENERATED FILE — DO NOT EDIT DIRECTLY.
     Produced by `loom-daemon generate-agent-skills` from
     defaults/.claude/commands/loom/curator-estimate.md (the same source Claude Code
     reads as /loom:curator-estimate via .claude/commands/loom/). This is the
     cross-vendor skill-discovery surface (.agents/skills/<name>/SKILL.md)
     read natively by Codex, Kimi Code, Mistral Vibe, and Grok — see
     runtime-adapters.md §5. To change this file, edit the source above
     and re-run the generator; CI (`loom-daemon generate-agent-skills
     --check`) fails if this file is stale. -->

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

Generate a fresh UUID once for this completed pass; retain it for retries.
A genuine re-curation uses a new UUID even when numeric values are unchanged.
Use the actual model/runtime of this Curator, never a Builder or a later tick.
Daemon-mediated sessions expose `LOOM_CURATION_MODEL` and
`LOOM_CURATION_RUNTIME` from their admitted launch; attended sessions supply
their known actual identities. Use `unknown` for unavailable provenance; the fact carries JSON null and no
model/runtime attribute for that field. Never guess, and never change readiness
or strand otherwise complete curation because provenance is unknown.
The workspace root must be the main checkout monitored by the daemon. Both
attended and daemon-mediated curation use this same command and producer.

The command fetches the current body and rejects missing, malformed, duplicate
or invalid concrete estimates; backticks and fenced examples do not count.
On fetch/validation/persistence failure, keep the curation claim and retry or
report the failure. Never announce completion without its journalled fact.
The durable envelope dates the completion, not later export; normal daemon
backfill delivers it to configured telemetry queues without new credentials.
Export retries keep the same UUID, timestamp and fact identity. Retrying a
successful completion does not reread its numeric values into a new fact.
