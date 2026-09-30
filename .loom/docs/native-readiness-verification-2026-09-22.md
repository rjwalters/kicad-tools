# Native readiness verification — 2026-09-22

Follow-up to #8581/#8529, for #8600. Host: Linux x86_64 (container/VM sandbox,
6 logical CPUs reported by the tool itself, 8 per `nproc`; a peer build
competed for CPU during part of this run — see "Host conditions" below). CLI:
OpenCode **1.18.31**, fetched via `npm install opencode-ai@1.18.31` into a
scratch prefix (the sandbox has no system-wide `opencode`). No provider
credential exists anywhere in this sandbox; the probe's own `assert_credential_free`
check and `env_clear()`-from-scratch construction are unaffected by that either
way. No model call, no paid retry, no forge contact — the probe enforces all
three in code, not just in this report's prose.

The two questions #8600 opened against #8581's `plugin_load_proven: None` and
`resolve_packages`'s "equivalent, not necessarily identical" caveat are
answered below with a live receipt, not inferred from a zero exit status.

## 1. Does any provider-free argv load the guarded plugin set?

Answered by instrumenting the binding itself rather than trusting the CLI's
exit code: `plugins/loom.ts`'s factory now writes a receipt file (named by
`LOOM_NATIVE_READINESS_RECEIPT`, set only by this probe) the instant it runs,
before anything in the factory can throw. `Probe::readiness` reports whether
that file appeared — `Some(true)`/`Some(false)`, never inferred from exit 0.

Measured against the pinned CLI, one attempt per argv, cold phase:

| Argv | `plugin_load_proven` | `server_session_ready` |
| --- | --- | --- |
| `debug config` (new default) | **true** | 4182 ms (first cold attempt); 3112–3421 ms across the 5+3-attempt runs below |
| `models` | **true** | 3720 ms |
| `--version` (old default) | **false** | 602 ms |
| `--help` | not separately timed here; `--version`'s result stands for the argument-parser-only shape it shares | — |
| `help` | **false** | 623 ms |
| `info` | **false** | 905 ms |

`--version`/`help`/`info` all return in well under a second because they are
answered by the CLI's argument parser before it ever bootstraps a session or
evaluates `plugins/loom.ts` — that is the mechanism, not a coincidence of
timing. `debug config` and `models` both cost roughly 3.1–4.2 s because they
actually start a session, which resolves the pinned plugin package and runs
its factory. This module's `DEFAULT_READINESS` changed from `--version` to
`debug config` (issue #8600) precisely because the old default measured a
second, cheaper copy of `binary_probe` and never touched the boundary this
tool exists to observe.

With the guarded binding's initialization inputs present (`LOOM_WORKSPACE`,
`LOOM_NATIVE_TOOL_BIN` — both now set unconditionally by `Probe::command`, see
§3), every `debug config`/`models` attempt across every run below reported
`plugin_load_observed: true` with no exceptions; `ReadinessReport::plugin_load_proven`
is the AND of the individual attempts (`plugin_load_verdict`), so a single
run's `true` is a claim about every attempt in that run, not just one.

Before that env-var fix was in place, an early manual invocation (isolated
`HOME`, no `LOOM_WORKSPACE`/`LOOM_NATIVE_TOOL_BIN`) showed the failure mode
#8600 set out to find: `plugins/loom.ts`'s factory throws
`Loom native tool context is missing`, and OpenCode 1.18.31 still answers
`debug config` with exit 0 and a normal config JSON — a zero exit status
proves nothing about plugin load, confirmed directly rather than assumed.

## 2. Does OpenCode's own resolver produce the same artifact layout as npm?

Answered by running OpenCode's plugin bootstrap **standalone** (a fresh,
empty `OPENCODE_CONFIG_DIR` containing only `plugins/loom.ts` and the pinned
`package.json`, no prior `npm install`) and diffing the resulting tree against
a plain `npm install` of the byte-identical manifest:

```
$ echo '{"private":true,"dependencies":{"@opencode-ai/plugin":"1.18.31"}}' > package.json
$ npm install --no-audit --no-fund
added 27 packages in 5s
$ find node_modules | wc -l
3926
```

```
$ OPENCODE_CONFIG_DIR=<fresh dir with only plugins/loom.ts + package.json> \
  HOME=<isolated> XDG_*=<isolated> OPENCODE_DISABLE_AUTOUPDATE=1 \
  opencode debug config
{ "plugin": ["file:///…/plugins/loom.ts"], … }   # exit 0
$ find node_modules | wc -l     # inside OPENCODE_CONFIG_DIR, written by the CLI itself
3926
```

`diff <(find A/node_modules | sort) <(find B/node_modules | sort)` produced
**zero lines of output** — the two trees are path-for-path, byte-for-byte
identical, 3926 entries each. OpenCode's own plugin bootstrap writes an npm
`package-lock.json` (`lockfileVersion: 3`) and a `node_modules/.package-lock.json`
marker — it resolves the pin by invoking `npm` itself, not a different
resolver reimplementing npm's layout by convention. `resolve_packages`'s doc
comment previously called this "equivalent work, not necessarily identical" —
that caveat is now dropped: it is the **same** work under the same resolver,
live-confirmed rather than assumed.

### Cache bypass is real, and load-bearing, not a probe bug

`PackageCache::publish`'s safety rules (`FORBIDDEN_COMPONENTS`, no symlinks
in a shared entry) refuse the real dependency tree, confirmed directly against
the resolved `node_modules`:

```
$ find node_modules -type l | wc -l
7                                          # all under node_modules/.bin/
$ find node_modules -type d -iname storage
node_modules/kubernetes-types/storage      # "storage" is in FORBIDDEN_COMPONENTS
```

Every warm-phase attempt in every run below reports `"cache":"bypassed"`,
never `"hit"` — this is `PackageCache::publish` correctly refusing to share a
tree it cannot safely restore, not a fixture gap. Narrowing
`FORBIDDEN_COMPONENTS` would not fix this: `.bin`'s symlinks are refused
unconditionally, independent of the component-name list, and `storage` here
is an upstream dependency's own directory name, not anything Loom's guarded
binding writes. Warm-phase timings below therefore measure a second full
resolution, identically to cold — an honest degradation, reported as such by
the schema (`cache: bypassed`) rather than hidden.

## 3. What changed in the probe because of this run

- `Probe::command` now sets `LOOM_WORKSPACE` (the attempt's own private
  scratch root) and `LOOM_NATIVE_TOOL_BIN` (`std::env::current_exe()`)
  unconditionally. Neither is credential-shaped — the workspace is scratch
  state private to this attempt, and the tool binary path is inert without an
  assistant turn to invoke it, which this probe never takes. Without these,
  the guarded binding throws before it can report anything, which is exactly
  the false-negative §1 exists to prevent.
- `DEFAULT_READINESS` changed from `["--version"]` to `["debug", "config"]`
  (§1).
- `ReadinessReport::plugin_load_proven` is now computed by `plugin_load_verdict`
  from the attempts' own receipts, never hand-flipped and never inferred from
  an exit status — `None` when no attempt observed a boundary at all,
  `Some(false)` on any observed non-load (one counterexample refutes the
  claim), `Some(true)` only when every attempt that reached the boundary
  loaded the plugin.
- `resolve_packages`'s doc comment updated per §2: same resolver, not merely
  equivalent work.

## 4. Cold/warm timing data (`--attempts 5 --phases both`)

The exact `loom-daemon worker readiness --attempts 5 --phases both` command
#8600 asked for, run against the pinned CLI:

| Boundary | Cold min/median/max (ms) | Warm min/median/max (ms) |
| --- | --- | --- |
| `binary_probe` | 582 / 622 / 642 | 582 / 602 / 623 |
| `binding_provision` | 0 / 0 / 0 | 0 / 0 / 0 |
| `package_resolution` | 3724 / 3919 / 4380 | 3634 / 3924 / 4458 |
| `server_session_ready` | 3112 / 3219 / 3341 | 3115 / 3394 / 4206 |

All 5 cold and all 5 warm attempts reported `plugin_load_observed: true` and
`cache: bypassed` (§2). `host_conditions_comparable: false` — the cold phase's
1-minute load average (5.01 before, 3.59 after) and the warm phase's (3.59
before, 3.15 after) differ by more than the comparability threshold, because a
second, unrelated build was competing for CPU on this shared host for part of
the run. **No speedup is claimed, and cold vs. warm are not compared against
each other here** — the report says so itself, and the wide max/median spread
within a single phase (e.g. cold `package_resolution` 3724–4380 ms) is a
direct symptom of that same contention, not measurement noise worth
tightening.

`--attempts 3 --phases cold --deny-network` (the second command #8600 asked
for): `binary_probe` and `binding_provision` measured normally
(`nonzero_exit` was never reached for either); `package_resolution` failed at
242 ms every attempt (`npm`'s registry lookup refused immediately, not a
timeout); `server_session_ready` was `not_reached` for all three, and
`plugin_load_proven` is correctly `None` — nothing was observed either way.
This is exactly the contrast the module's own doc comment predicts for a
denied-network cold run.

`--readiness-arg debug --readiness-arg config` (the third command, explicit
rather than relying on the new default): `plugin_load_proven: true`,
`readiness_command: ["debug","config"]`, timings consistent with the table
above (cold `package_resolution` 4558/4584/7715 ms, warm 3836/3896/4599 ms).

## Disclosures

- Durations are wall times on one uncontrolled, shared sandbox host; they are
  not a benchmark and no speedup is claimed, per §4's host-conditions caveat.
- The historical ~157 s cold run in issue #8529 is a single observation from a
  different host and is not reused as a baseline here.
- Billed cost was not measured; nothing in this report is a claim about
  provider cost, because no model call was made anywhere in it.
- The OpenCode CLI used here (`opencode-ai@1.18.31` fetched via npm into a
  scratch prefix) is the pinned major/version, not a system install; this
  affects nothing measured, since the probe always launches by explicit
  `--bin`/`LOOM_OPENCODE_BIN` path.
