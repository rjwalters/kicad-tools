# Operator presence (#11563, #12121)

**Loom does not emit operator presence.** There is no `operator.presence`
signal, no presence hook, and no `loom-daemon presence …` subcommand.

## Rule: daemon work only (#12536)

Loom records telemetry for the work the daemon itself does, and nothing else.
No hook watches AI work outside the daemon, and no `loom-daemon` subcommand an
attended or outside session can call emits a span, log or metric about that
session's own work (`usage-record` and `live-output-attend` were removed).
Consumers infer external operator work from an item that advances with no
daemon span or record. The in-session lease *comments*
(`sweep-lease-publish.sh`, `sweep-lease-renew.sh`) are forge-side claim
coordination, not telemetry, and are kept.

## What the daemon records

The daemon reports telemetry for its own work, when it is running. It does not
capture other AI coding sessions on any machine: no hook watches for AI work
outside the daemon, the daemon never reads interactive session transcripts, and
outside sessions do not send telemetry through the daemon. Only daemon work is
recorded.

## How operator work is known

Consumers infer it. When an item (issue or PR) advanced and no daemon effort
(span) was reported for it, an operator session did the work. ETA and
dashboard consumers should treat "advanced with no daemon span" as
operator-driven, not as stalled.

The `operator.presence` kind and its OTLP mapping remain in the schema only so
rows already stored in a backend still decode ([telemetry-schema](telemetry-schema.md#operatorpresence)).
Nothing produces new ones.

## Retiring the old wiring

An earlier design merged three `presence-beat.sh` hook entries and
`env.LOOM_OPERATOR_PRESENCE = "1"` into `~/.claude/settings.json`.
`resync-installed.sh` (via `provision_operator_presence`) strips exactly those,
leaving everything else alone (including an explicit
`LOOM_OPERATOR_PRESENCE=0`). It takes effect per host on its next resync (see
[fleet-config-lifecycle](fleet-config-lifecycle.md): landed is not effective).
