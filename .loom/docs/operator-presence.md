# Operator presence on operator hosts (#11713)

`operator.presence` (#11563) tells the ETA fit whether a human is driving an
issue. It is emitted by the **operator's own Claude session**, never by a
fleet worker (workers stay inert through `LOOM_DAEMON_ITEM_ID`), and it needs
**no running daemon**.

## Turning it on (one step)

Presence is a fleet key, `operatorPresence.enabled`, set in fleet-gitops
`config.defaults` (tier 1, rendered to the machine-level `defaults.json`):

```json
{ "operatorPresence": { "enabled": true } }
```

On the host's next `loom update` / `resync-installed.sh` / full install, the
installer merges these into the **user-scope** `~/.claude/settings.json`:

- `presence-beat.sh` on `UserPromptSubmit`, `PostToolUse` and `Stop`
- `env.LOOM_OPERATOR_PRESENCE = "1"` (enables the in-session `sweep.started` /
  `sweep.outcome` facts that `lease ensure` emits)

The merge is idempotent and only adds or removes Loom's own entries (identified
by `defaults/hooks/presence-beat.sh`); other hooks, env and permissions are
preserved, and the previous file is backed up first. Nobody hand-edits
`settings.json`.

An operator without fleet-gitops can opt in alone: run any provisioning with
`LOOM_OPERATOR_PRESENCE=1`, or set `operatorPresence.enabled` in
`~/.local/share/loom/config/defaults.json`.

## Turning it off

- **Fleet-wide, one step:** set `operatorPresence.enabled` to `false`; each
  host strips the entries on its next resync.
- **Per operator, locally:** `touch ~/.loom/operator-presence.optout` (path
  overridable with `LOOM_PRESENCE_OPTOUT_FILE`) or run with
  `LOOM_OPERATOR_PRESENCE=0`. This beats a fleet `true`; the next resync
  removes the wiring and `rm` of the file lets fleet policy apply again.

Precedence: `LOOM_OPERATOR_PRESENCE` env, then the opt-out file, then the fleet
key. With none of them set, existing wiring is left alone.

## How facts reach SigNoz without a daemon

1. The hook runs `loom-daemon presence beat`, which appends an
   `operator.presence` envelope to `.loom/logs/operator-presence.jsonl`
   (throttled; no prompt text is ever stored).
2. On `Stop` (session end) the same command then runs the one-shot
   `loom-daemon presence flush` logic: it POSTs the journal to the OTLP ingest
   with the daemon's own `OtlpExporter`, releases the records the collector
   acknowledged, and exits. A failed or partial send keeps every record for the
   next flush; failures land in `operator-presence.errors`.

The OTLP target resolves from `$LOOM_PRESENCE_OTLP_ENDPOINT` (optionally
`$LOOM_PRESENCE_OTLP_HEADERS_FILE`), else the first enabled `otlp` entry of
`observability.exporters`. The Bearer key comes from
`observability.ingestKeyFile` / `$LOOM_OBSERVABILITY_INGEST_KEY_FILE` /
`~/.loom/observability/ingest.key`, and is not read when the entry has its own
`headers_file`. `loom-daemon presence status` prints the pending count, the
resolved target (or why there is none) and recent failures;
`loom-daemon presence flush` drains by hand.

## Rollout and verification

Flipping the key only takes effect per host on its next resync (see
[fleet-config-lifecycle](fleet-config-lifecycle.md): landed is not effective).
Confirm on a host: `jq '.hooks, .env' ~/.claude/settings.json`, then
`loom-daemon presence status` after a session, and `operator.presence` in
SigNoz.
