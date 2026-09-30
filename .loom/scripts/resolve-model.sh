#!/bin/bash

# resolve-model.sh - Thin stub delegating to `loom-daemon resolve-model` (#3982)
#
# Ported from Python to native Rust in issue #4275 (epic #4081 Phase 3 family
# 5); flag names, stdout shape and exit codes are unchanged.
#
# The /loom:sweep skill shells out to this to resolve a *logical* model tier
# (`opus`, `sonnet`, `sonnet@xhigh`) to the concrete model ID it should dispatch
# on the wire, BEFORE it passes the model to a subagent Task. This is the single
# indirection point that fixes the non-monotonic escalation ladder: every rung,
# the tier-2.5 bump, the Judge/refusal fallbacks, and the experiment's Arm A keep
# naming the alias `opus`, and exactly one place decides that `opus` means
# `claude-opus-5` (the CLI's own `opus` alias still resolves to a gen-4 model).
#
# The mapping is configurable in `.loom/config.json` -> `sweep.modelAliases`, so
# an operator can repoint a tier (or drop the pin once the CLI alias rolls to
# gen-5) with no code change. Unknown aliases and pinned IDs (`claude-sonnet-4-6`)
# pass through unchanged.
#
# Usage:
#   resolve-model.sh <tier|alias|id>            # prints the resolved model ID
#   resolve-model.sh <tier> --generation        # prints the resolved generation number
#   resolve-model.sh <model|id> --task-alias    # prints the nearest Task-tool alias
#   resolve-model.sh <model|id> --downgrade     # prints the next CHEAPER Task-tool alias
#   resolve-model.sh <tier> --config <path>     # explicit .loom/config.json path
#
# Examples:
#   resolve-model.sh opus            # -> claude-opus-5
#   resolve-model.sh sonnet@xhigh    # -> sonnet@xhigh   (passthrough; CLI resolves)
#   resolve-model.sh claude-sonnet-4-6   # -> claude-sonnet-4-6 (pinned ID, unchanged)
#   resolve-model.sh claude-opus-5 --task-alias   # -> opus (Task-tool degradation, #4282)
#   resolve-model.sh opus --downgrade             # -> sonnet (credit-exhaustion fallback, #5687)
#   resolve-model.sh haiku --downgrade            # -> (exit 3; no cheaper rung)
#
# --task-alias (issue #4282): the daemon/process path passes a resolved model as
# `--model <id>` (pinned IDs OK), but the in-session Task/Agent tool's `model`
# parameter is an alias-only enum (sonnet|opus|haiku|fable) — a pinned ID is
# invalid there. --task-alias maps a resolved model back to its nearest Task-passable
# alias (`claude-opus-5` -> `opus`; @effort stripped), so the in-session dispatch
# degradation is a deterministic lookup, not per-orchestrator judgement. Exits 3
# with no output when there is no Task-passable alias (caller omits `model`).
#
# --downgrade (issue #5687): step one rung DOWN the Task-tool cost ladder
# (`fable -> opus -> sonnet -> haiku`). This is the deterministic remedy for a
# per-model-tier credit exhaustion (`MODEL_CREDITS_EXHAUSTED` — "You're out of
# usage credits"): credits are model-tier-scoped, so the same account can still
# serve a cheaper tier, and the in-session Task dispatch path has no account
# pool to rotate through. Accepts the same inputs as --task-alias (bare alias,
# pinned ID, @effort suffix) and always emits a Task-passable alias. Exits 3
# with no output at the cheapest rung (`haiku`) or on an unrecognized model, so
# the caller falls through to normal mid-phase-death handling instead of
# guessing. See sweep.md, "Credit-exhaustion fallback".

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=/dev/null
source "$SCRIPT_DIR/lib/script-helper.sh"

# `exec`s the native subcommand, so exit 3 ("no mapping — caller falls through")
# reaches the caller unmodified. See lib/script-helper.sh.
# requires-daemon: resolve-model >= 0.17.0   #4275/#4552 — the Rust port landed in 3f147e8f0 (2026-07-29), AFTER v0.16.0 was tagged (57cfec3aa) and while the workspace version still read 0.16.0, so 0.16.0 is the last version WITHOUT it; the next bump, 0.17.0 (3e5a9439e), is the first that shipped it. Hard, not `optional`: this stub only execs, it never probes or degrades. Default LOOM_SCRIPT_HELPER_MISSING_RC (1) is left alone deliberately — the DATA code here is 3 ("no mapping"), and 2 is the bad-argument code; 1 is used by neither, so the refusal is already unconfusable with an answer. Subcommand-level floor only, per the convention: a later FLAG (`--tier` #4238, `--task-alias` #4282, `--downgrade` #5687) still surfaces as clap's own argument error on a binary between 0.17.0 and that flag's release (#8484).
loom_exec_script_helper resolve-model "$@"
