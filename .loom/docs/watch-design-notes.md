# `/loom:watch` Design Notes

Background and naming rationale for the `watch` skill (`defaults/.claude/commands/loom/watch.md`).
Not needed to run the skill correctly — read this only if you're curious why it's
shaped the way it is, or revisiting the naming/scope decision.

## Origin: the 2026-07-30→31 reference night

`/loom:watch` is the skill form of the manual "night watch" an operator otherwise
runs by hand. On 2026-07-30→31 that watch ran 21 ticks, re-typed the same
five-check battery every time, produced 42 merges, two `#4694` false-dead saves,
and one `#4688` pre-emptive mitigation — and burned ~60 tool calls of pure loop
mechanics getting there. Everything hard-won in that night is encoded in the
skill's remediation playbook and loop mechanics.

## Naming (#4762)

Shipped as `watch`. The alternatives considered were `sustain` and
`night-shift`:

- `night-shift` over-narrows it to overnight use — the same loop is useful for a
  90-minute lunch window.
- `sustain` implies the skill *drives* work — it does not; the daemon does.

`watch` was chosen because the loop is **observe-first**: the default posture
every tick is *look, decide, usually do nothing*, and remediation is the
exception. It also matches the verb operators already use ("watch the fleet")
and the existing `watch_registry.rs` / watchdog vocabulary.
