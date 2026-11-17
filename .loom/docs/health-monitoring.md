# Health Monitoring

A short guide to checking that an unattended Loom fleet is healthy, using the
supported `loom-daemon health` and `loom-daemon status` commands. The canonical
reference for every section these commands report is
[`daemon-reference.md`](daemon-reference.md) → "One-shot fleet vitals
(`loom-daemon health`, #4761)".

## `loom-daemon health`

```bash
loom-daemon health                 # one line per section, last 30 minutes
loom-daemon health --since 2h      # widen the role-tick / throughput window
loom-daemon health --json          # machine-readable report
```

`--since` accepts `30m` (default), `2h`, `90s`, `1d`, or a bare number of seconds.

The exit code lets a watch loop (cron, a dashboard, the `loom:watch` skill)
branch without parsing the output:

| exit | meaning |
|------|---------|
| `0` | every section green |
| `1` | degraded: at least one section is non-green, including "could not determine" |
| `2` | the daemon is genuinely dead |
| `3` | busy, not confirmed unhealthy (see "Busy vs degraded" in the daemon reference) |

Sections cover trusted liveness, dispatch, the token pool, role-tick health,
queue depth (including a review-stall verdict), merge throughput, and
`operator_attention` (always green: held work is normal, not a fault). What each
one means, and what turns it non-green, is documented in the daemon reference.

## `loom-daemon status`

```bash
loom-daemon status                       # in-flight sweeps, dynamic cap, token usage
loom-daemon status --pipeline            # plus the forge-side queue snapshot per repo
loom-daemon status --json --section dynamic_cap,in_flight
```

`status` shows the running daemon's autonomous-mode state. `--section` (with
`--json`) returns only the named top-level sections; `loom-daemon status --help`
lists them. `--pipeline` makes several `gh` calls per managed repo, so it is
opt-in.

## Where else to look

- Stuck workers, stale worktrees and orphaned claims:
  [`troubleshooting.md`](troubleshooting.md).
- Metrics, traces and the fleet dashboard: [`observability.md`](observability.md)
  and the "Fleet dashboard" section of the daemon reference.
