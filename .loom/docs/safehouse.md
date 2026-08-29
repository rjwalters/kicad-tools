# Safehouse fleet-comms narration (phase 1, #3997)

Loom coordinates through forge labels — that is unchanged and remains the sole
source of truth. **Safehouse** ([`rjwalters/safehouse`](https://github.com/rjwalters/safehouse))
adds an optional, additive **narration** side-channel: an end-to-end-encrypted
Matrix room a human watches in Element to follow a multi-host agent fleet in
real time, instead of polling `gh` or tailing daemon logs.

Phase 1 **narration** is daemon-side and emit-only: the `loom-daemon` subscribes
its existing in-process event bus and narrates sweep-lifecycle transitions into
the room, adding no new event topics and no new publish call sites. On top of
that, **peer-claim coordination (#4028) makes the room bidirectional** — a
dedicated read task consumes inbound peer advertisements so daemons on a shared
backlog back off before the non-atomic `loom:building` label flip would let them
race. See [Peer-claim coordination](#peer-claim-coordination-cross-host-soft-claim-4028)
below.

> **Out of scope** (tracked separately): per-worker personas (`loom_builder_42`)
> and `SAFEHOUSE_PERSONA` forwarding to workers → **#3999**; inbound **human**
> steering (reading `@`-mentions back to agents) → follow-up (it reuses the same
> inbound read task #4028 adds); carrying the judge verdict value in an event
> payload (needs a frozen-taxonomy amendment) → follow-up; the **atomic
> cross-host claim authority** (a real CAS behind the soft claim) → Phase 2 of
> #4028. Cloud-host provisioning of `safehoused` (formerly tracked here as
> **#3998**) has landed — see
> [Fleet provisioning: cloud workers](#fleet-provisioning-cloud-workers-fleet-add-worker---safehouse-3998)
> below.

## The degradation contract (read this first)

Safehouse is a **best-effort side-channel with no hard dependency** — it mirrors
the claude-monitor optional-integration pattern. **Loom never blocks a sweep on
safehouse.** Concretely:

- `safehouse.enabled` false/absent ⇒ **byte-for-byte no-op**: the daemon does
  not subscribe to the bus and makes zero socket syscalls.
- Enabled but the socket is missing/refused, the persona is rejected, or the
  peer restarts mid-run ⇒ every failure degrades to a single `warn!` (one per
  outage, not per event) and the sweep proceeds unaffected. The sink reconnects
  lazily with capped exponential backoff; dropped narration is never retried
  into a hot loop and never fails a sweep.

## Configuration

An optional `safehouse` block in `.loom/config.json` (shipped in
`defaults/config.json`), resolved with precedence **env > config >
default(disabled)**:

```jsonc
"safehouse": {
  "enabled": false,       // default off — additive, opt-in
  "socket": null,         // default: $SAFEHOUSED_SOCKET
  "room": null,           // omit only if safehoused joined exactly one room
  "persona": "loom_daemon"
  // "rooms": { … }       // optional attention-class routing — see below (#4225)
}
```

Env overrides (each wins over config for that key):

| Env var | Overrides |
|---|---|
| `LOOM_SAFEHOUSE_ENABLED` | `enabled` (`1`/`true`/`yes`/`on` ⇒ on; `0`/`false`/`no`/`off`/`""` ⇒ off) |
| `LOOM_SAFEHOUSE_SOCKET` | `socket` |
| `LOOM_SAFEHOUSE_ROOM` | `room` |
| `LOOM_SAFEHOUSE_PERSONA` | `persona` |
| `LOOM_SAFEHOUSE_ROOM_SIGNAL` | `rooms.signal` (#4225) |
| `LOOM_SAFEHOUSE_ROOMS_BY_REPO` | `rooms.byRepo`, as `repo=room[,repo=room…]` (#4225) |
| `LOOM_SAFEHOUSE_ROOM_CLAIMS` | `rooms.claims` — dedicated peer-claim coordination room (#4713) |

**Socket resolution** (precedence **env > config**, `resolve_socket` in
`loom-daemon/src/safehouse.rs`; the bash-side worker-injection path
(`defaults/scripts/lib/mcp-config.sh`'s `loom_mcp_safehouse_socket()`) mirrors
the same chain): `$LOOM_SAFEHOUSE_SOCKET` → `$SAFEHOUSED_SOCKET` (the
unprefixed convention `safehoused` clients also read) → the configured
`socket` value. If none resolves, narration logs one `warn!` and stays off — no
built-in `$HOME`-relative default, since safehouse is opt-in per-host.

**`socket` must never be committed to the shared `.loom/config.json`** — like
`observability.ingestKeyFile` (`observability.md`), it is host-specific by
definition (every host's `safehoused` binds a different, unshareable path). Leave
it unset in the committed file and either install `safehoused` at the
conventional path each host's `$SAFEHOUSED_SOCKET` already points at, or set a
per-host override in the gitignored `.loom-local/local.json` tier
(`config_resolver.rs`, highest precedence) or via `$LOOM_SAFEHOUSE_SOCKET` —
never in the committed file. #5457 is exactly the failure mode this avoids: a
macOS `safehouse.socket` path was committed to this repo's own shared
`.loom/config.json`, and — because `resolve_socket` checked the configured
value *before* env at the time — every other host that `git pull`ed `main`
inherited a path to a socket that did not exist on it, with no env override able
to take effect while that stale path stayed committed.

**Why there is still no built-in default, even after #5523.** #5457's fix left
a gap: with the committed default gone and nothing installed in its place, an
affected host's `safehouse.enabled: true` silently resolved to no socket at
all, and the only signal was a `log_warn` inside each sweep's own per-role log
— nobody was tailing those, so a real host ran with **zero** safehouse
narration for 11 hours before a human noticed the public fleet
pulse had gone stale (#5523). The tempting fix — teach the resolver a
conventional-path fallback (e.g. `~/.loom/safehoused/state/safehoused.sock`) —
was deliberately **rejected** for #5523: a code-level default *would* avoid
re-triggering #5457's exact mechanism (it can't go stale via `git pull` the
way a committed value can), but it would reintroduce the same underlying risk
in a different shape — "resolves to *something*" would quietly stop meaning
"actually reaches a live `safehoused`", which is precisely the gap that let
#5523 run unnoticed. #5523's fix instead makes the **absence** loud and cheap
to detect, in two ways, without touching this resolution chain at all:

- `spawn-claude.sh`'s warning, when `safehouse.enabled` is true and no socket
  resolves, now names the consequence ("no safehouse narration will be
  recorded... the public fleet pulse is fed exclusively from
  safehouse narration") instead of only the mechanism ("skipping safehouse MCP
  injection") — still a `log_warn`, never a failed spawn (the degradation
  contract above is unchanged: `safehouse.enabled: false`/absent stays a
  byte-for-byte no-op, and `enabled: true` never blocks a sweep).
- **`defaults/scripts/check-safehouse-socket.sh`** (installed as
  `.loom/scripts/check-safehouse-socket.sh`) is a new standalone, on-demand
  check that reports — per managed repo, without reading a single sweep log —
  whether `safehouse.enabled` is set and, if so, whether a socket resolves AND
  is present on disk. With no arguments it walks this host's machine-level
  workspace registry (`~/.loom/workspaces.json`, the same registry
  `loom-daemon workspace add/list` manages) and checks every registered repo
  in one pass; pass explicit repo roots to check a subset. `--json` emits a
  parseable array (`{repo, enabled, socket, present, status}` per repo) for
  scripting/cron. It exits `0` when every repo is either not configured or
  resolved-and-present, and `1` the moment any repo is enabled with an
  unreachable socket — the exact condition #5523 needed surfaced. Unlike the
  pre-existing static `Safehouse:` line in `loom-daemon-start.sh`'s startup
  banner (below), this check has **no dependency on the daemon
  (re)starting** — it can be run any time, which is what the incident
  actually needed: the affected daemon never restarted across the 11-hour
  window, so its own start-time check never re-ran.

### Room routing by attention class (`safehouse.rooms`, #4225)

One room carrying everything — operator conversation, human-must-act handoffs,
*and* the full narration firehose — drowns the signal it exists to deliver: at
full concurrency the operator's primary interface (phone, notifications on) takes
hundreds of messages a night. The optional `rooms` map routes by **attention
class first, repo second**:

```jsonc
"safehouse": {
  "enabled": true,
  "socket": "/run/safehoused.sock",
  "persona": "loom_daemon",
  "rooms": {
    "signal": "!AbC…:example.org",           // tier 1: loom-fleet, notifications ON
    "byRepo": {                                // tier 2: per-repo firehose, muted
      "loom": "!DeF…:example.org",
      "vibesql": "!GhI…:example.org"
    }
    // "claims": "!JkL…:example.org"         // optional: dedicated peer-claim
                                               // coordination room (#4713) —
                                               // see "Which room claim ads ride" below
  }
}
```

| Tier | Room | Carries | Volume / notifications |
|---|---|---|---|
| 1 | `rooms.signal` (`loom-fleet`) | operator ↔ fleet conversation, every `handoff`, terminal `ack` / `completion`, wave-dispatch `digest` roots (#4217) | low, notifications **on**, cross-repo by design |
| 2 | `rooms.byRepo[<repo>]` (`fleet-<repo>`) | `task` (dispatch + phase transitions) and `chat` (worker chatter) | high, **muted** by default, opened while actively watching a repo |

A Matrix **Space** (e.g. "Fleet") grouping these rooms is tracked separately in the
safehouse repo — Loom creates no Space.

**Routing rules**

- **Severity routes, never duplicates.** Every message lands in exactly **one**
  room; nothing is mirrored.
- The kind → tier table is the whole routing decision:
  | Envelope `type` | Room |
  |---|---|
  | `handoff`, `ack`, `completion`, `digest` | signal |
  | `task`, `chat` | repo firehose |
  It is written as a **compile-time-exhaustive `match`** over an `EnvelopeKind`
  enum (`safehouse.rs`), with no wildcard arm, so a future member fails to
  compile (and a type added to only one of `KNOWN_TYPES` / `EnvelopeKind` fails
  a test) rather than silently defaulting into the wrong room. `digest` (#4217)
  is the newest member.
- **Rooms are per-repo, not per-host.** Host attribution already rides the Matrix
  sender (per-host bot accounts), so a second host working the same repo posts
  into the same room.
- The repo key is the **workspace-root basename** — the same narration convention
  #4201 uses for `task_id`/body prefixes (`/Users/x/GitHub/vibesql` ⇒ `vibesql`).
- **`rooms.signal` falls back to the scalar `room`**, so the migration step "add a
  `byRepo` map, leave `room` as it is" keeps the existing room as the signal room.
- Workers follow the same map via `fleet-comms.md`: worker `chat`/`task` posts go
  to the repo room, worker `handoff` to the signal room.

**Lazy room creation.** A repo absent from `byRepo` gets its firehose created on
its **first narration** (never eagerly for every managed repo): the sink issues a
socket `create_room` op for the alias `fleet-<repo>` and remembers the resulting
id for the rest of the daemon's run. If creation is **refused**, that repo
degrades to the signal room with **one** `warn!` for the whole run (never one per
message) and the sweep is unaffected — after fixing permissions, restart the
daemon. A creation that fails at the *transport* layer (a dropped connection) is
not held against the room: it is retried after the reconnect. As with
`safehouse.mcpCommand`, the exact `create_room` request/reply shape lives in the
external `rjwalters/safehouse` repo and is not verifiable from here, so the client
is lenient (it names the room with both `name` and `alias`, accepts the room id
under any of the plausible reply keys, and falls back to addressing sends by the
alias it asked for).

**Migration notes**

- **Absent `rooms` map ⇒ nothing changes.** Every envelope goes to the single
  `room` exactly as before (including `room: null` ⇒ no `room` key on the wire).
  This is the default, and it is covered by explicit regression tests. A
  present-but-empty `"rooms": {}` (or one whose entries are all blank) normalizes
  back to single-room mode rather than to a routing mode with nowhere to route.
- **Once the map exists and the bot is in several rooms, explicit ids are
  required.** The `room: null` convenience only resolves while safehoused has
  joined *exactly one* room; a multi-room bot rejects every roomless `send` with
  `'room' required: N rooms joined`. So when you adopt `rooms`, give it a real
  `signal` id (or leave a real `room` for it to fall back to) — otherwise
  narration stops with the send-rejected status described below, which names the
  fix. See the troubleshooting entry.
- **Peer-claim ads default to the signal room** — the one deliberate exception
  to "signal-only" — unless an operator opts into a dedicated `rooms.claims`
  room (#4713). See the peer-claim section below for why, and for the
  cross-host provisioning requirement that comes with opting in.

New template keys reach existing consumer configs via the installer deep-merge
(template is the base, existing values win) — no migration needed. The tier
ownership of the block is noted in
[`docs/design/config-resolution-tiers.md`](https://github.com/rjwalters/loom/blob/main/docs/design/config-resolution-tiers.md).

## Operator setup: provisioning the persona (requires a safehoused restart)

`safehoused` reads its `personas` allowlist **once at boot** — a plain TOML
array with **no runtime registration, no glob/prefix matching, and no SIGHUP
reload**. So the persona Loom narrates as must be added to safehoused's config
and **safehoused must be restarted** before it will accept the connection:

1. Add the persona to safehoused's config (default `loom_daemon`):
   ```toml
   personas = ["loom_daemon"]
   ```
2. **Restart `safehoused`** — the allowlist is not hot-reloaded.
3. Enable Loom narration (`safehouse.enabled = true`, or
   `LOOM_SAFEHOUSE_ENABLED=1`) and ensure the socket path resolves.
4. Run a sweep; the room shows `loom-daemon → everyone · task` lines, threaded
   per **repo-qualified** issue (`<repo>_<issue>`, issue #4201) so two managed
   repos' identically-numbered issues never collide into one thread.

This static, operator-provisioned model is a phase-1 constraint. Per-issue
personas (`loom_builder_42`) are blocked upstream until safehoused grows prefix
support and are tracked in #3999 — do not attempt to register personas at
dispatch time; there is no such path.

## Connection status: not configured / unreachable / connected / send-rejected (#4345, #4464)

Before #4345, `safehouse.enabled` false/absent, enabled-but-unreachable, and
enabled-and-connected all looked identical to an operator — silence. Two
surfaces now report the live state:

- **`loom-daemon status`** (and `status --json`) prints a `Safehouse:` line
  (a `safehouse` object in JSON) with one of three states, self-reported by
  the daemon's own live connection — never a second, status-time connection
  attempt (a CLI-side probe could not know "room joined" the way the daemon's
  own connection can):
  - `not configured` — no `safehouse` block, `enabled: false`/absent, or
    enabled with no socket path resolving at all (nothing to even try). No
    connection has been attempted.
  - `configured, unreachable` — enabled, a socket path resolved, but the most
    recent connect attempt failed, was refused, or dropped. The resolved
    socket path is included.
  - `connected` — the most recent connect attempt completed the `hello`
    handshake. The configured room name is included when one was configured
    (`safehouse.room` unset is valid only when safehoused joined exactly one
    room, resolved server-side — the daemon is never told that resolved name,
    so the line omits it rather than guessing).
  - `connected, sends rejected: <reason>` (#4464) — the `hello` handshake
    succeeds (the socket is reachable) but every `send` is rejected at the
    protocol layer, so nothing reaches the room. The canonical cause is a
    **multi-room safehoused with `safehouse.room` unset**: safehoused replies
    `'room' required: N rooms joined` and the fix is to set `safehouse.room`
    (see the troubleshooting entry below). This state is **sticky** — a
    reconnect whose `hello` succeeds does not clear it; only a `send` that is
    actually accepted returns the line to `connected`. Distinct from
    `unreachable` on purpose: "unreachable" would send an operator chasing the
    socket/persona instead of the config.
- **`loom-daemon-start.sh`** prints a cheaper, **static**, pre-connect check at
  start time (`ok`/`warn` colored, one line): it runs *before* the daemon
  connects, so it can only distinguish "not configured" from "configured" —
  proving "connected" needs the daemon's own live socket, which is what
  `loom-daemon status` is for. Concretely: no `safehouse` block/disabled ⇒
  `not configured`; enabled with no socket resolving ⇒ `configured,
  unreachable`; enabled with a socket path that exists as a socket on disk ⇒
  `configured (socket present)`; enabled with a socket path that does not
  exist yet ⇒ `configured, unreachable` (the path is included either way).

Implementation: `loom-daemon/src/safehouse.rs`'s `SafehouseState` is a shared
`Arc<Mutex<..>>` cell (the same injection shape [`PeerClaimView`] already
uses) updated by both the narration sink ([`run_sink`]) and the peer-claim
coordination task ([`run_coordination`]) on every connect/disconnect
transition; `workspace_pool.rs`'s `WorkspacePool` owns one cell per daemon and
`ipc.rs`'s `build_daemon_status` reads it into a new optional
`DaemonStatusReport.safehouse` field (`#[serde(default)]`, so an older
daemon's wire payload — missing the field entirely — still parses).
`loom-daemon-start.sh`'s static check reuses the same env>config>default
resolvers `lib/mcp-config.sh` already defines for the safehouse-mcp worker
injection (phase 2, below) rather than re-deriving them.

### Troubleshooting: `'room' required` rejection (multi-room host) (#4464)

**Symptom.** `loom-daemon status` shows `Safehouse: connected, sends rejected:
'room' required: N rooms joined`, and the daemon log carries
`[WARN] safehouse: narration rejected — set safehouse.room — safehoused
rejected send: 'room' required: …`. Sweeps proceed normally (the degradation
contract holds), but the room shows nothing from this host and peer-claim dedup
(#4028/#4431) is silently disabled here.

**Cause.** Omitting `safehouse.room` is valid **only when safehoused joined
exactly one room** — safehoused then resolves the sole room server-side. Once
safehoused joins two or more rooms it can no longer guess, so it rejects every
`send` that does not name a `room`.

**Fix.** Set `safehouse.room` in `.loom/config.json` (or `LOOM_SAFEHOUSE_ROOM`)
to the room this host should narrate into, then restart the daemon
(`loom-daemon restart`). The status line returns to `connected` on the first
accepted send. `loom-daemon-start.sh` also prints a static caveat at start time
whenever a socket is configured but `safehouse.room` is unset.

**With attention-class routing (#4225)** this is the same failure with one more
place to look: adopting a `rooms` map *guarantees* a multi-room bot, so the
roomless convenience stops working for good. Set `rooms.signal` (or
`LOOM_SAFEHOUSE_ROOM_SIGNAL`) to a real id — or leave `safehouse.room` set, which
`rooms.signal` falls back to — and give each actively-watched repo a
`rooms.byRepo` entry (unset repos are created lazily as `fleet-<repo>`, and a
refused creation degrades that repo to the signal room with one `warn!`).

## New-host onboarding (#4345, #4346)

The path from a fresh interactive host (no `safehoused`, no `safehouse` config
block anywhere) to `loom-daemon status` reading `connected`. Step 3 registers
`safehoused` as a **supervised service** (launchd LaunchAgent on macOS,
`systemd --user` on Linux) via `safehoused-service.sh` (#4346); running it by
hand is still documented as the debug fallback.

1. **Bot account + credentials.** Provision (or reuse) a Matrix account for
   the `loom_daemon` persona in the target safehouse deployment — this is an
   operator-side step in the external `rjwalters/safehouse` repo/deployment,
   not something this repo automates. Note the account's credentials and the
   room the fleet uses.
2. **Build/install `safehoused`.** Build from the `rjwalters/safehouse`
   checkout per that repo's own instructions. Confirm the `personas` allowlist
   in its config includes `loom_daemon` (or whichever persona you assign this
   host, per "Operator setup" above) — the allowlist is boot-time and
   restart-only, no hot reload.
3. **Register `safehoused` as a supervised service.** Use
   [`safehoused-service.sh`](#supervised-service-wrapper-safehoused-servicesh-4346) —
   it renders and installs a launchd LaunchAgent (macOS) or `systemd --user`
   unit (Linux) that starts `safehoused` at login and keeps it up
   (`KeepAlive`/`Restart=always`, so it survives a crash or reboot), mirroring
   `loom-daemon-start.sh`'s own supervised-service pattern:
   ```bash
   # Preview the service definition first (no side effects):
   ./.loom/scripts/cli/safehoused-service.sh --print-plist   # macOS
   ./.loom/scripts/cli/safehoused-service.sh --print-unit    # Linux
   # Then install + start (point --bin at your built safehoused; the socket is
   # resolved from the same safehouse.socket / $SAFEHOUSED_SOCKET chain the
   # daemon uses, or pass --socket explicitly):
   ./.loom/scripts/cli/safehoused-service.sh install --bin "$(command -v safehoused)"
   ```
   On a headless Linux host, run `loginctl enable-linger "$USER"` once so the
   `systemd --user` unit survives a reboot. **Fallback (debug only):** start it
   by hand under any supervisor — `nohup safehoused &`, a tmux pane, a personal
   plist. Either way, note the socket path safehoused binds (its own config
   controls this) for the next step.
4. **Socket env or config.** Either export `SAFEHOUSED_SOCKET=<path>` (the
   convention safehoused's own clients read) machine-wide, or set
   `safehouse.socket` in this host's gitignored `.loom-local/local.json`
   override (never in the shared, committed `.loom/config.json` — see the
   callout above) — see [Socket resolution](#configuration) above for the
   full precedence.
5. **Enable the `safehouse` config block** in `.loom/config.json` (per
   workspace, since it lives in the per-repo config tier) or export
   `LOOM_SAFEHOUSE_ENABLED=1` machine-wide:
   ```jsonc
   "safehouse": {
     "enabled": true,
     "socket": null,      // omit to rely on $SAFEHOUSED_SOCKET
     "room": null,         // omit only if safehoused joined exactly one room
     "persona": "loom_daemon"
   }
   ```
6. **Start/restart `loom-daemon`** (`loom-daemon-start.sh`). Its startup
   banner prints the static `Safehouse:` line described above — confirm it
   reads `configured (socket present ...)`, not `not configured` or
   `configured, unreachable`, before moving on.
7. **Verify with `loom-daemon status`.** Give it a few seconds for the
   narration sink / peer-coordination task to complete their first connect
   (the sink connects lazily on the first narrated bus event; the
   peer-coordination task connects eagerly at daemon startup, so it is
   usually first to show `connected`). Confirm the `Safehouse:` line reads
   `connected` with the expected room, then run a sweep and confirm the room
   shows the `loom-daemon → everyone · task` dispatch line.

If the line sticks at `configured, unreachable`: confirm `safehoused` is
actually running and bound to the exact path `loom-daemon status` reports,
that the persona is in safehoused's allowlist (a rejected `hello` also
degrades to `unreachable` — check the daemon log for `safehoused rejected
persona`), and that the daemon process can reach the socket path (permissions,
same-host, no stale socket file from a crashed prior run).

### Supervised service wrapper: `safehoused-service.sh` (#4346)

`defaults/scripts/cli/safehoused-service.sh` (installed as
`.loom/scripts/cli/safehoused-service.sh`) registers `safehoused` as a
supervised service so it starts at login and comes back after a crash or
reboot — the interactive-host counterpart to the cloud-host provisioning path
(#3998). It mirrors `loom-daemon-start.sh`'s supervised-service pattern
(launchd LaunchAgent on macOS / `systemd --user` on Linux) including the
`--print-plist` / `--print-unit` preview modes.

| Command | Effect |
|---|---|
| `--print-plist` / `--print-unit` | Print the launchd plist / systemd unit that *would* be installed, no side effects (any platform). |
| `install` | Render + install + enable + start the service. |
| `uninstall` | Stop + disable + remove the service definition. |
| `status` | Report whether the supervised service is loaded / running. |

Parameters (precedence **flag > env > config > default**): `--bin`
(`SAFEHOUSED_BIN`, else `command -v safehoused`); `--exec "<argv>"`
(`SAFEHOUSED_EXEC`) for a full ExecStart override when safehoused needs flags;
`--socket` (else the `$LOOM_SAFEHOUSE_SOCKET` → `$SAFEHOUSED_SOCKET` →
`safehouse.socket` chain the daemon resolves); `--config`
(`SAFEHOUSED_CONFIG`); `--log` (default `~/.loom/logs/safehoused.log`);
`--label` / `--unit` for the launchd label / systemd unit name.

**Supervision policy differs from `loom-daemon`'s on purpose.** `loom-daemon`
uses `KeepAlive:{SuccessfulExit:true}` / `Restart=on-success` because it has a
clean-exit restart *primitive* (exit 0 == intentional relaunch, the
`RestartDaemon` path). `safehoused` has no such primitive — it is a persistent
connection daemon that should simply stay up — so the wrapper renders
`KeepAlive=true` (launchd) / `Restart=always` + `RestartSec=5` (systemd).

**Ownership decision (recorded here per #4346's acceptance criteria):** this
wrapper is deliberately **safehoused-agnostic** and lives in *this* repo, while
the **authoritative** service definition (safehoused's real argv, config
schema, and key-backup / steady-state teardown semantics) is owned by the
external `rjwalters/safehouse` repo. loom does not vendor safehoused's binary
invocation — that would rot the moment the external repo changes it — so the
wrapper only supervises an operator-supplied binary and bakes a minimal,
non-secret environment (`SAFEHOUSED_SOCKET` / `SAFEHOUSED_CONFIG` when
provided; never a forwarded token). If the safehouse repo ships its own service
files, point the runbook at those and treat this generator as the fallback.

## Fleet provisioning: cloud workers (`fleet add-worker --safehouse`, #3998)

The onboarding runbook above is for an interactive host an operator sets up by
hand. `loom-daemon fleet add-worker <ssh-host> --repo <owner/name> --safehouse
<inputs>` (epic #4340, `loom-daemon/src/fleet/add_worker.rs`) is the same
onboarding **encoded as an ordered, idempotent plan** that a cloud worker's
spin-up runs unattended over SSH — no cloud-init fragment, no cloud CLI, no
Tailscale API call from loom itself (epic #4340's boundary: a VM comes from
`repo:remote`, loom only consumes "a reachable box + an SSH alias").

### What the plan does

With `--safehouse`, `fleet add-worker` appends seven steps after the plain
worker's bootstrap (each following the same check/apply contract as the rest
of the plan, so a re-run against an already-provisioned host reports every one
`AlreadyDone`):

1. **`safehouse-tailscale-install`** — installs the `tailscale` apt package.
2. **`safehouse-tailscale-join`** — `tailscale up --auth-key=file:<path>` with
   the operator-minted key (below). No `--advertise-tags`: the tag is baked
   into the key server-side.
3. **`safehouse-build`** — `cargo build --release -p safehoused` from a fresh
   `rjwalters/safehouse` checkout.
4. **`safehouse-config`** — writes `~/.loom/safehoused/config.toml` (`0600`):
   homeserver URL, the per-host Matrix account, fresh store/recovery
   passphrases, and the persona allowlist. **Must precede step 6** — the
   allowlist is boot-time-only (no reload), and the plan's step order enforces
   this (asserted in `add_worker.rs`'s tests).
5. **`safehouse-room-invite`** — joins the fleet room via
   [safehouse#39](https://github.com/rjwalters/safehouse/issues/39)'s
   daemon-side `invite` op — never raw CS-API temporary devices. loom does not
   vendor this invocation (owned by the external repo); override it with
   `--safehouse-invite-exec "<argv>"` if it changes upstream.
6. **`safehouse-supervise`** — installs `safehoused` under `systemd --user` via
   [`safehoused-service.sh`](#supervised-service-wrapper-safehoused-servicesh-4346)
   (the same script the interactive runbook uses) and enables lingering.
7. **`safehouse-daemon-restart`** — wires `LOOM_SAFEHOUSE_ENABLED` /
   `_SOCKET` / `_ROOM` into the worker's own `loom-daemon` systemd unit and
   restarts it — env-only, per #3997's decision (no worker-side
   `.loom/config.json` edit).

**Without `--safehouse`, behavior is byte-for-byte unchanged**: a single
`safehouse` skip-with-notice entry, a plain worker, zero safehouse
provisioning.

### Inputs the operator must mint

Every secret travels the same way `AddWorkerConfig`'s existing `--pat-file` /
`--accounts-env` do: read locally at preflight, transferred to the worker only
over **ssh stdin**, landing only in `0600` files. None of these ever appear on
a command line, in the rendered `--dry-run` plan text, in a daemon log at any
level, or in the fleet registry.

| Flag | Contents | Notes |
|---|---|---|
| `--safehouse-tailnet-auth-key-file PATH` | A Tailscale auth key | **Operator-minted, ephemeral + `tag:loom-worker`** — loom never calls the Tailscale API. Ephemeral means a dead VM auto-deregisters from the tailnet with no fleet-roster bookkeeping. |
| `--safehouse-secrets-file PATH` | `KEY=VALUE` lines: `SAFEHOUSE_MATRIX_USER_ID`, `SAFEHOUSE_MATRIX_PASSWORD`, `SAFEHOUSE_STORE_PASSPHRASE`, `SAFEHOUSE_RECOVERY_PASSPHRASE` | The Matrix account is **operator-created** on the homeserver (the [safehouse#25 verified sequence](#operator-setup-provisioning-the-persona-requires-a-safehoused-restart)) — `fleet add-worker` never needs homeserver admin credentials, only the resulting account. Passphrases are freshly generated per host. |
| `--safehouse-homeserver-url URL` | Not secret | Must resolve inside the tailnet. |
| `--safehouse-room ROOM` | Not secret | The fleet room this host joins. |
| `--safehouse-persona NAME` (repeatable) | Not secret | Mirrors the studio host's allowlist (#3999) — at least one required. |
| `--safehouse-repo-url URL` | Not secret | Defaults to `rjwalters/safehouse`. |
| `--safehouse-invite-exec "ARGV"` | Not secret | Overrides the default `safehoused invite --config <path>` if safehouse#39's CLI surface changes upstream. |

`--safehouse` with any of the first five omitted fails **preflight** — before
any SSH connection — with a message naming exactly which flag is missing (no
half-joined host).

### Required tailnet ACL

The auth key's `tag:loom-worker` is expected to carry an ACL restricting
workers to reaching only the homeserver's `443`, not the rest of the tailnet.
loom documents this requirement; it does not manage the tailnet ACL itself
(epic #4340's boundary — no Tailscale API call from this repo).

### Teardown: `fleet drain`'s flush verification

`fleet drain <ssh-host>`'s `flush-safehouse` phase (`loom-daemon/src/fleet/drain.rs`)
is the spin-up's counterpart: it stops the worker's `safehoused` unit over SSH
(`systemctl --user stop safehoused`) — a supervised stop **is** the flush,
since safehoused's SIGTERM/ctrl-c shutdown path calls
`client.encryption().backups().wait_for_steady_state()` and prints
`"safehoused: room-key backup flushed; bye"` before exiting — then verifies via
the journal line, falling back to the unit's `ExecMainStatus` when the journal
has rotated. The verdict maps onto drain's existing contract:

- `safehouse.enabled == false` — `Skipped`, no room keys in play, exit `0`.
- Flush verified — `Changed`, eligible for "safe to power off", exit `0`.
- Flush **not** verified (nonzero remote exit, or the host was unreachable) —
  `Unverified`; the drain still completes (workspace/roster cleanup proceed —
  loom never refuses to retire a box over this), but the report withholds
  "safe to power off" and exits `3` so an operator/monitor treats it as a flag,
  not a clean success.

## What gets narrated

The sink maps the **existing frozen event taxonomy** (`event_bus.rs`,
`types.rs`) to envelope-v1 messages. All are broadcast (`to: "*"`). Which **room**
each one lands in is decided by its envelope `type` — see
[Room routing by attention class](#room-routing-by-attention-class-safehouserooms-4225):
`task` lines (dispatch/phase) go to that repo's firehose, while `handoff` /
`ack` / `completion` (blockers, crashes, terminal outcomes) go to the signal room.
With no `rooms` map configured they all go to the one `room`, as before.

### Repo qualification (issue #4201)

The daemon manages multiple workspaces (loom, vibesql, anvil, kicad-tools, …)
behind a **single shared event bus** (`workspace_pool.rs`), so a bare issue
number is not unique across them — loom `#4201` and vibesql `#4201` would
otherwise thread into the *same* Matrix room thread. Every narrated event's
`task_id` and body prefix are therefore **repo-qualified**:

- **Convention**: the repo name is the **basename of the workspace-root
  filesystem path** stamped onto the event's `repo` field by
  `SweepRegistry::emit_event` (Issue #3929's pattern) — e.g.
  `/Users/x/GitHub/vibesql` → `vibesql`. This is a path-derived directory name,
  not a forge `owner/repo` slug: it needs no network call, and the daemon's
  workspace registry already guarantees at most one managed registry per path.
- **`task_id`**: `<repo>_<issue>` (e.g. `vibesql_6173`), with any character
  outside the `[A-Za-z0-9_]` charset (`build_send_request` enforces this)
  folded to `_` — so `kicad-tools` becomes `kicad_tools`.
- **Body prefix**: `<repo>#<issue>` (e.g. `vibesql#6173`), used verbatim since
  the body is free text with no charset restriction.
- **Fallback**: an event with no `repo` known (a synthetic/test event, or one
  from an era before this field existed) narrates with the pre-#4201
  unqualified form — bare `<issue>` for `task_id`, bare `#<issue>` for the body
  prefix — rather than erroring.

`SweepGlobalDispatch` needed a small additive amendment (`repo: Option<String>`)
to carry this — it was the one sweep-scoped event that had not yet been stamped
with `repo`, unlike `SweepPhase`/`SweepBlocker`/`SweepExited`/`SweepCrashed`.

### Body grammar (issue #4201)

Every narrated body follows `<repo>#<issue> · <phase/status> [· <detail>] [—
<commentary>]` — informal by design (there is no single rigid 4-field parse),
but consistently repo-qualified and consistently favoring one line of
actionable detail over the previous terse `issue #N …` phrasing:

| Bus event | Envelope `type` | Body |
|---|---|---|
| `SweepGlobalDispatch` (Issue) | `task` | `<repo>#N · dispatch` — the sink best-effort appends ` — "<issue title>"` (see below) |
| `SweepPhase` | `task` | `<repo>#N · <phase>` (+ ` · PR #M open` when present) |
| `SweepBlocker` | `handoff` | `<repo>#N · BLOCKED — <reason>` (a human must act) |
| `SweepExited` (exit 0) | `ack` | `<repo>#N · done ✓ · <dur>` (e.g. `6m55s`) |
| `SweepExited` (exit ≠ 0) | `ack` | `<repo>#N · failed ✗ · exit <code>[ (decoded)] · <dur>` — exit `78` decodes to `(EX_CONFIG: token pool)`; every other code prints raw (no attempt at a full sysexits table) |
| `SweepCrashed` | `handoff` | `<repo>#N · crashed ✗ at <checkpoint_phase> — resumable (checkpoint kept)` |
| `SweepExited` **whose PR merged** (#4426) | `completion` | `<repo>#N · merged ✓ · PR #M · <dur>` — emitted *in addition to* the `ack`, carrying the `completion-v1` `meta` (see below) |

**Dispatch-line title (AC3)**: the operator's highest-value ask was seeing the
issue title on the dispatch line (the single most common message in the room —
33 of 60 messages in the first night's history were bare dispatch roots). The
payload-amendment route (threading the title through `SweepGlobalDispatch`)
was judged too heavy for this bug-fix issue, unlike the small `repo` amendment
above (which fixes an actual collision bug). Instead the **sink** fetches it at
narration time — one `gh issue view --json title --jq .title` in the event's
workspace root, bounded by a 5s timeout, with a 10-minute cache keyed by
`(workspace_root, issue)` so a re-dispatch of the same issue (e.g. after a
Doctor cycle) does not re-shell to `gh`. Every failure (missing `gh`, no
network, unauthenticated, timeout) degrades to narrating the dispatch line
**without** a title — this never blocks narration or the sweep itself.

`SweepGlobalCompleted` is intentionally **not** narrated: it carries only a
`sweep_id` (no issue number), and `SweepExited` already emits the completion
`ack` with richer data — narrating both would double-post per completion.

### Dispatch-digest batching (issue #4217)

A work-finder tick can admit several issues in quick succession (observed: 7
dispatches within seconds), and each one used to become its own `task`-kind
thread root — an operator watching the signal-adjacent timeline saw N
near-identical `#N · dispatch` lines at once. `run_sink` now buffers admitted
`SweepGlobalDispatch(Issue)` events for a coalescing window
(`LOOM_SAFEHOUSE_DISPATCH_DIGEST_WINDOW_MS`, default 30s, ms-precision test
override) measured from the *first* buffered dispatch, then flushes:

- **Exactly one buffered dispatch** ⇒ unchanged pre-#4217 behavior: the single
  `task`-kind envelope (`<repo>#N · dispatch`, title-enriched per AC3 above),
  repo-qualified `task_id`, routed via the normal per-repo firehose path.
- **More than one** ⇒ **one** `digest`-kind envelope instead, grouped per repo
  and counted, issue numbers ascending within a group, groups sorted by
  descending count (ties alphabetical): `dispatched 7: loom×6 (#4028 #4106
  #4144 #4157 #4162 #4164), vibesql×1 (#6173)`. No per-issue `task` envelope is
  sent for these — each issue's own thread still starts from its first
  *substantive* event (a `SweepPhase`/`SweepBlocker`/completion, all
  unaffected by this batching), not from the dispatch. No `gh` title lookups
  are made for a digest (would be N calls for one line).
- **`digest` is a new envelope kind** (`KNOWN_TYPES`/`EnvelopeKind`'s sixth
  member, #4217), routed to the signal room via `AttentionClass::Signal` —
  never the per-repo firehose, since one digest can span several repos. Each
  flushed digest gets a fresh `task_id` (`dispatch_digest_<seq>`) so
  consecutive digests are separate thread roots, not one perpetual thread.
- Buffering, grouping, and the window itself add no new failure mode: a
  digest send is rejected/dropped exactly like any other envelope
  (degradation contract unchanged), and the buffer lives only in `run_sink`'s
  in-memory state — a daemon restart loses at most one in-flight window's
  worth of not-yet-flushed dispatches, which simply narrate on the next
  restart's own first dispatch instead.

### Completion envelopes → the public fleet feed (#4426)

safehoused's egress subsystem mirrors well-formed **`completion`** envelopes out
of allowlisted rooms — redacted and delay-buffered — to a `sink_url`; that is
what feeds the public fleet feed. Loom is the producer:

- **Emit point (two, since #4583)**:
  1. The narration sink, on `SweepExited`. Exit status alone proves nothing, so
     the sink checks **forge truth** (`gh pr list --head feature/issue-N
     --state merged`, in the event's workspace root, 10s timeout) and emits
     the `completion` only when that issue's PR actually merged — the `ack`
     still goes out either way. Chosen over having the sweep child publish a
     post-merge phase event because it is daemon-only (no skill edit), has the
     sweep timing to hand, and verifies rather than trusts.
  2. **Periodic merge reconciliation** (`reconcile_recent_merges`, #4583): the
     `SweepExited` path only ever fires while a sweep process is alive to exit
     — it structurally cannot see a PR that merges *after* the sweep already
     exited, which is the **common steady-state path**: builder opens a PR,
     judge approves, the sweep exits, and champion merges it minutes-to-hours
     later on its own cron tick. On a fixed cadence (5 minutes;
     `LOOM_SAFEHOUSE_RECONCILE_INTERVAL_MS` overrides it, primarily a test
     seam), the sink round-robins one workspace at a time — drawn from the
     union of every repo it has observed a stamped-`repo` event for and the
     on-disk `WorkspaceRegistry` — and runs one **bulk**, branch-unfiltered
     `gh pr list --state merged --json
     number,headRefName,url,mergedAt,createdAt,title,additions,deletions
     --limit 30`. Each row's issue number is recovered from `headRefName` via
     the `feature/issue-<N>` convention
     (`worktree_ops::naming::issue_from_branch`); rows that do not match are
     skipped (not every merged PR is an issue sweep). A row with no sweep clock
     available uses the PR's own `createdAt`/`mergedAt` pair as the
     `started_at`/`completed_at` proxy instead of the reaper's clock. Both
     paths funnel through the same envelope-build/dedup-insert core
     (`build_and_narrate_completion`), so "exactly one completion per merge"
     holds regardless of which path observes it first.
     - **Lookback window**: rows merged more than **7 days** ago are dropped
       before the dedup check (`LOOM_SAFEHOUSE_RECONCILE_MAX_AGE_SECS`
       overrides; garbage/negative values fall back to the default). `--limit
       30` bounds a burst's *size*, not its *age* — without the window, a pass
       on a host with no persisted dedup set (fresh install, the upgrade to
       this version, a lost/corrupt completions file) would backfill the feed
       with the last 30 merges however old they are, which in a low-traffic
       workspace means narrating months-old PRs as if they just landed. Seven
       days is far beyond any plausible daemon outage, so the daemon-was-down
       case below still recovers.
     - **Seed-only first pass per workspace (#4649)**: the lookback window
       alone still allowed a *burst* — up to 30 genuinely-in-window merges
       narrated in one tick — the very first time a workspace was reconciled
       on a host with no reliable persisted dedup state (absent, corrupt, or
       unreadable `~/.loom/safehouse-completed.json`; a legitimately empty but
       valid file, e.g. a host that already reconciled to zero, does **not**
       count as fresh). In that case each workspace's first-ever reconciliation
       tick after startup inserts every in-window `(workspace, issue,
       merged-PR-number)` into the dedup set and persists it, but drops the
       resulting envelopes instead of
       narrating them. The same workspace's next tick — and every tick on a
       host whose dedup file was not fresh to begin with — narrates normally.
       Trade-off: a merge that landed just before a fresh install is silently
       never narrated, which is acceptable since nothing was watching the feed
       at install time anyway.
- **`meta` (`completion-v1`)**: `{schema, agent, repo, ref, result, started_at,
  completed_at}` required, plus optional `issue`/`tokens`/`tokens_by_model`/
  `title`/`additions`/`deletions`/`visibility` (envelope-v1 preserves unknown
  `meta` keys, so no schema rev is needed for extensions). `body` stays required
  human prose — a room reader sees a sentence, `meta` is the machine view.
- **`repo` is the forge `owner/repo` slug** (`gh repo view --json
  nameWithOwner,isPrivate`, cached per workspace for the daemon's lifetime),
  deliberately **not** the path-basename convention above: the feed links `ref`
  (the PR URL) and displays the forge identity.
- **`visibility` is the public-egress gate's input (#6596)** — `"public"` or
  `"private"`, from the `isPrivate` field of that same `gh repo view` call (no
  extra round-trip). The egress subsystem mirrors well-formed `completion`
  envelopes to a **public** sink with no visibility check of its own, so until
  #6596 the only thing keeping a private repo's PR titles off a public page was
  an accident: the daemon's credential could not read those repos at all (see
  the per-owner credential note below). Loom is the **producer** half of the
  gate only:
  - **Private repos still narrate**, exactly as before, into the (private)
    signal room — the tag withholds *egress*, never the room post.
  - **The consumer half must fail closed**: an egress that publishes only on an
    explicit `visibility == "public"` degrades safely when the key is absent (a
    `gh` too old to know `isPrivate` falls back to the `nameWithOwner`-only
    query, and an older Loom emits no tag at all). Treating an absent tag as
    public would reinstate the leak.
  - `validate_completion_meta` refuses any third value, so a consumer only ever
    has to reason about `"public"`, `"private"`, and absence. The consumer-side
    gate itself lives in the safehouse repo (rjwalters/safehouse#155) — this
    tag is inert until it lands.
  - **Staleness caveat**: the tag is cached alongside the slug for the daemon's
    lifetime, so flipping a repo public → private mid-run keeps the stale
    `public` tag until the daemon restarts.
- **Per-owner credential (#6596)**: every forge lookup in this module — the
  merge check, the slug/visibility resolution, the reconciliation pass, and the
  dispatch-line issue-title fetch — runs under the **workspace owner's**
  `GH_CONFIG_DIR` (`credential_preflight::apply_gh_config_for_root_async`), the
  same per-owner credential the three dispatch paths hand their sweep children
  (#5401/#5508/#5522/#6529). The daemon process itself runs under the *primary*
  installation's credential (#4458), which answers `Could not resolve to a
  Repository` for a **private** repo owned by another org — so before this,
  every private cross-owner workspace was permanently and invisibly absent from
  the completion feed. A total no-op for single-owner fleets and the root
  owner's own repos.
- **Display fields (#4497)** feed the site's row format
  `<repo>#<issue>: <title> +A −D · <dur> · <tokens> tok`, i.e. the
  development-cost-of-quality-code view:
  - `title`/`additions`/`deletions` come out of the **same** `gh pr list` call
    that verifies the merge (`--json number,url,mergedAt,title,additions,deletions`),
    so they cost **zero** extra forge round-trips. Each degrades independently:
    a row that omits one still publishes the completion without that key. A `gh`
    too old to know a field rejects the whole request, so that one case retries
    the pre-#4497 `number,url,mergedAt` set rather than losing the completion.
  - Unlike `tokens`, a real **`0`** additions/deletions is a fact about the merge
    (an empty-diff merge, a pure revert) and is published as `0`; a blank `title`
    is omitted.
  - `tokens` is a best-effort per-issue total from **two** sources, tried in
    order, each on the blocking pool with its own 5s cap:
    1. The in-process activity DB's per-issue rollup
       (`ActivityDb::get_cost_by_issue`) — **input + output**.
    2. **The sweep's own on-disk Claude Code transcripts** (#4699) — all four
       usage counters (`input`, `output`, `cache_read`, `cache_creation`), i.e.
       total tokens processed.

    **Attribution is knowingly imperfect** for both. The rollup keys on a bare
    issue number (no repo column, so a multi-repo daemon conflates identical
    numbers across repos) and only counts token samples linked to the issue
    through `agent_inputs`, making the figure a floor. That is a deliberate
    operator call: for a cost *trend*, imperfect-but-consistent beats absent.
    Both sources coming up empty omits the key rather than charting free work.

    > **Operational gotcha — why the DB rollup is silent on a fleet host
    > (#4699).** The `resource_usage` and `prompt_github` rows the rollup joins
    > are written from exactly **one** place: the IPC `GetTerminalOutput` handler
    > scraping a *managed terminal's* scrollback. `dispatch_sweep` spawns a
    > detached `claude -p` via `spawn-worker.sh` and reaps the OS process — it
    > issues no `SendInput`/`GetTerminalOutput` round trips — so on any host whose
    > work arrives by dispatch (i.e. every host that publishes to the feed) both
    > tables are empty **forever**, not merely "until the prompt↔usage linkage is
    > established". Measured on the reference host 2026-07-31: 0 rows in each,
    > across 76 published completions, every one of them `tokens: null`. Source 2
    > exists because of this; do not diagnose a `tokens: null` feed record by
    > looking for missing rollup rows.

    The transcript source locates a sweep's session by content, since nothing
    records a sweep-id → session-uuid mapping: a sweep runs `claude -p
    "/loom:sweep <issue> …"` with cwd = the workspace root, so Claude Code writes
    `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/<cwd-slug>/<uuid>.jsonl` (plus
    `<uuid>/subagents/agent-*.jsonl`, one per phase), and the parent session's
    first `user` record carries the slash command verbatim. Candidates are
    narrowed by file mtime against the completion's own time window (±2h slack)
    before any file is read, and a re-dispatched issue's several sessions are all
    summed. Because the project directory is keyed by the workspace path, this
    source *is* repo-qualified — it does not share the rollup's cross-repo
    issue-number conflation. Cache reads are included because they dominate a
    sweep both by volume (~99%) and — priced at ~10% of base — by cost, so an
    input+output-only figure under-reports spend by well over an order of
    magnitude and unevenly between sweeps. Set
    `LOOM_SAFEHOUSE_TRANSCRIPT_TOKENS=0` to opt out of the transcript scan
    entirely (the key is then simply omitted on dispatch-driven hosts).
  - **`tokens_by_model` (#5740)** is a per-`(model, speed, service_tier)`
    breakdown of the same transcript scan, because `tokens`' single sum
    cannot be priced: it merges five quantities (`input`, `cache_read`, the
    two `cache_write` buckets, `output`) that price between 0.1x-2x of each
    other, across models that are themselves 3-5x apart — on one measured
    36-hour window, pricing `tokens` at the base input rate overstated real
    spend **7.7x**. It is additive alongside `tokens` (which keeps its
    existing flat-sum meaning) and has **only one source** — the activity DB
    rollup has no per-model granularity to offer, so this key comes from the
    transcript scan alone and is `None` whenever that scan is (opted out,
    empty, or timed out).

    Each array entry is
    `{model, speed, service_tier, input, cache_read, cache_write_5m,
    cache_write_1h, output}` — raw counts, **never cost-weighted** (a pricing
    table change never needs a backfill of this data, same rationale as
    `tokens_in`/`tokens_out` in [telemetry-schema.md](telemetry-schema.md)). A
    usage block whose `model` is absent or the literal `"<synthetic>"` (Claude
    Code stamps that on some internal/tool-echo messages) is grouped under one
    explicit `<unattributed>` sentinel rather than dropped, and the sum across
    every entry's counters reconciles against the flat `tokens` total for the
    same sessions. `speed`/`service_tier` default to `"standard"` when a usage
    block does not carry them. Omitted (never `[]`) when nothing attributable
    was found, same "unknown != zero" contract as `tokens`.
  - Absent `tokens`/`tokens_by_model`/`title`/`additions`/`deletions` ⇒ the
    envelope is identical to the pre-#4497 one; none of the five can block or
    fail an emission.

  > **A `null` field on the public fleet feed is not evidence of a producer bug (#4699).**
  > The public feed applies its **own** server-side redaction on read: entries
  > whose `repo` is not on the site's linked-repo allowlist are served with
  > `ref` and `title` forced to `null`, keeping the sellable columns
  > (`repo#issue`, diff stats, timing) while withholding the outbound link. So a
  > mix of linked and unlinked repos in one feed response legitimately shows
  > `title: null` next to a populated `additions`. Loom itself cannot emit a
  > null/absent `ref` at all — it is in `COMPLETION_REQUIRED_KEYS`, so
  > `validate_completion_meta` rejects the envelope before it can be sent (and
  > the site's `/api/ingest` validator independently rejects such a payload with
  > a `400`). **Before filing a producer bug, check the feed record's `repo`
  > against the site's allowlist and compare against a linked repo's record from
  > the same tick.**
- **Timestamps** come from the reaper's clock (`started_at = exit − duration_sec`,
  `completed_at = exit`), so the pair is always self-consistent.
- **`result: "failure"` is out of scope for v1**: `completion-v1` requires a
  `ref`, and a sweep with no merged PR has no meaningful one (an open PR is
  unfinished, not failed, and is usually resumed). The wire support exists
  (`CompletionResult::Failure`) for a follow-up that identifies a genuinely
  terminal negative outcome.
- **At most one per merged PR, per host**, deduped on `(workspace, issue,
  merged-PR-number)` — shared by **both** emit points above, so a resumed
  sweep's second `SweepExited` does not double-post, and the periodic
  reconciliation pass does not re-post a merge the `SweepExited` path already
  narrated (in either order — whichever path observes the merge first wins;
  the other becomes a no-op dedup check). This dedup set is **persisted** to
  `~/.loom/safehouse-completed.json` (`LOOM_SAFEHOUSE_COMPLETIONS_PATH`
  overrides the path) and reloaded at startup — the in-memory set alone would
  not survive a daemon restart, which would otherwise either re-post every
  prior completion (if reconciliation's lookback window still covered them)
  or silently drop a merge that happened while the daemon was down. It is
  written atomically (temp file + rename) and every failure to read or write
  it is best-effort — a corrupt or unwritable file degrades to "no reliable
  prior state", never to a crash, which (#4649) now routes through the
  seed-only first pass above rather than re-narrating a potential backlog
  outright. It grows by one `["<workspace>", <issue>, <pr>]` triple (~40
  bytes) per narrated completion and is never pruned; at Loom's own merge rate
  that is a couple of MB per decade, so no compaction is implemented.
  Downstream ingest is additionally idempotent on `event_id`.
  - **Keyed on the merged PR number, not the issue alone (issue #6062)**. A
    single issue can legitimately merge more than one PR over its lifetime —
    a partial increment (`Part of #N`) leaves the issue open across several
    merges, each landing its own real, distinct change. Keying the dedup set
    on `(workspace, issue)` alone (the pre-#6062 shape) could not tell that
    case apart from a resumed sweep re-observing the *same* merge, and
    permanently suppressed every completion after the first one for a
    still-open issue — the same failure mode reported live as
    `klayout-tools#797` appearing to duplicate a merged PR when what had
    actually happened was two *different* dispatches racing the same issue
    (see the fleet-wide section below for the actual root cause of that
    incident). Because the merged PR number is only known once
    `fetch_merged_pr` answers, both `completion_for_exit` and
    `reconcile_recent_merges` always run that one bounded `pr list` lookup
    before consulting the dedup set — there is no cheaper issue-only
    short-circuit that would not reintroduce the bug.
  - **This dedup is per-host, not fleet-wide** — `~/.loom/safehouse-completed.json`
    lives on one host's disk, loaded once at daemon startup, with no code path
    that consults a *peer* host's dedup state. On a multi-dispatcher fleet
    (build on host A, merge observed on host B — or two hosts' reconciliation
    ticks both observing the same champion merge before either has recorded
    it locally) each host's own set starts empty for that `(workspace, issue,
    pr)` triple, so each independently narrates its own `completion`
    envelope: distinct Matrix `event_id`s, so sink-side `event_id` dedup does
    **not** collapse them (issue #6352). See
    [Fleet-wide completion dedup](#fleet-wide-completion-dedup-reusing-the-peer-claim-channel-6352)
    below for the cross-host layer built on top of this per-host set.
- **Strict client-side construction.** safehoused **silently degrades a
  malformed `meta` to `chat`** — the event then vanishes from the feed with no
  error anywhere — so `build_send_request` refuses to send a `completion` unless
  `validate_completion_meta` accepts it (all required fields present and
  non-empty, `schema == "completion-v1"`, `agent` a valid persona, `repo` an
  `owner/repo` slug, `ref` an absolute http(s) URL, `result` ∈
  {`success`,`failure`}, both timestamps RFC3339 with `completed_at >=
  started_at`, `issue`/`tokens`/`additions`/`deletions` non-negative integers
  when present, `title` a non-empty string when present, `visibility` ∈
  {`public`,`private`} when present). Nothing here relies on server-side
  validation.
- **Redaction is downstream.** Every `meta` string — `title` included — is
  published as an ordinary JSON string, so safehoused's egress deny-pattern pass
  redacts it exactly like `repo`/`ref`. Loom applies no bespoke encoding that
  could let a value slip past that pass.
- **Same degradation contract**: a failing/absent/slow `gh`, an unreachable
  safehoused, or a rejected envelope drops the completion silently and never
  affects the sweep — but a failed `gh` lookup now leaves **one `warn` per
  `(call, workspace)`** in the daemon log (repeats drop to `debug`, so a
  persistently unauthorized workspace cannot flood it across reconciliation
  ticks), quoting a capped one-line stderr head. The behavior stays silent; the
  *diagnosis* does not have to be (#6596: catching the daemon's child `gh` in
  `ps` and replaying it by hand was the only way to see this class of failure).

## Wire protocol (envelope v1)

- `AF_UNIX`, **newline-delimited JSON**, one object per line, bidirectional.
- Mandatory first request: `{"id":0,"op":"hello","persona":"<name>"}`.
- `send` carries `to`/`type`/`body` and optional `task_id`/`room`/`meta`. `type`
  is a closed enum owned by the safehouse repo, currently
  `{chat,task,handoff,ack,completion,digest}` — loom does not extend it
  unilaterally; each member (most recently `completion` in #4553, `digest` in
  #4217) is added in the same coordinated lockstep as the rest of this
  protocol. `task_id` must be `[A-Za-z0-9_]`; `meta` is valid **only** on a
  `completion`, which in turn **requires** it (see above) — all validated
  before sending. A `send` whose `type` safehoused does not yet recognize is
  rejected at the protocol layer like any other malformed request — the same
  degradation contract as everything else in this module (warn once, drop,
  sweep unaffected). The daemon **stamps `from`** from the socket identity —
  the client never sends one (no impersonation).
- Replies echo the request `id`. **Async push lines are interleaved on the same
  connection, carry an `event` key, and have no `id`** — the client
  demultiplexes by skipping any line with an `event` key. The **narration**
  connection is emit-only and discards inbound pushes; the **peer-claim
  coordination** connection (#4028) instead routes each inbound `event` line to a
  handler (see below).

## Implementation

- `loom-daemon/src/safehouse.rs` — config resolver, envelope-v1 client, the
  event→envelope mapping, the reconnecting bus-subscriber narration sink, and
  (#4028) the peer-claim coordination task + `InboundEventSink`. Also (#4225) the
  attention-class routing layer: `RoomMap` (config/env), `EnvelopeKind` +
  `AttentionClass` (the exhaustive kind → tier table), `RoomRouter` (per-envelope
  room resolution, lazy `fleet-<repo>` creation, warn-once degradation) and
  `SafehouseClient::send_to` / `create_room`. Also (#4217) the dispatch-digest
  batching in `run_sink`: `PendingDispatch`, `dispatch_digest_window()`,
  `dispatch_envelope()` (the single-dispatch shape, unchanged), and
  `build_dispatch_digest_envelope()` (the grouped burst root).
- `loom-daemon/src/transcript_tokens.rs` — (#4699) the on-disk token source
  behind the completion envelope's `tokens` field: Claude Code's project-slug
  mangling, the `/loom:sweep <issue>` session match, mtime windowing and the
  parent+subagents usage sum. Pure filesystem code with no safehouse dependency,
  so it is unit-testable on its own; `safehouse.rs` wraps it in the timeout and
  the `LOOM_SAFEHOUSE_TRANSCRIPT_TOKENS` opt-out.
- `loom-daemon/src/peer_claims.rs` — the pure, socket-free peer-claim view
  (TTL expiry, self-claim recognition, retraction, `ClaimAd` parse/serialize).
- `loom-daemon/src/workspace_pool.rs` — `start_safehouse_narration()` and
  `start_peer_coordination()` subscribe/attach the shared `Arc<EventBus>` /
  `PeerClaimView` and spawn on the daemon runtime; both no-ops when disabled.
  Also (#4345) owns the `SharedSafehouseState` cell and exposes
  `safehouse_status()` for `ipc::build_daemon_status`.
- `loom-daemon/src/types.rs` — `DaemonStatusReport.safehouse` /
  `SafehouseStatus` (#4345), the wire shape for the connection-state line.
- `loom-daemon/src/ipc.rs` / `loom-daemon/src/main.rs` — (#4345)
  `build_daemon_status` reads the pool's `safehouse_status()`; `main.rs`
  renders the `Safehouse:` human line and the `safehouse` JSON object.
- `defaults/scripts/cli/loom-daemon-start.sh` — (#4345) the static
  pre-connect `Safehouse:` line, via `lib/mcp-config.sh`'s existing
  `loom_mcp_safehouse_enabled`/`loom_mcp_safehouse_socket` resolvers.
- `defaults/scripts/check-safehouse-socket.sh` — (#5523) the standalone,
  daemon-restart-independent per-repo socket-resolution drift check described
  above under "Socket resolution".
- Tests: `safehouse.rs`'s `mod tests` (state-cell + wire-rendering cases),
  `workspace_pool.rs`'s `mod tests` (pool wiring), `ipc.rs`'s
  `test_build_daemon_status_reports_halt_and_in_flight` (report field),
  `defaults/scripts/tests/test-loom-daemon-start.sh` (start-wrapper line),
  `defaults/scripts/tests/test-mcp-config.sh` §13 (#5523: the loudened
  spawn-claude.sh warning text + check-safehouse-socket.sh's not-configured /
  resolved / unreachable-no-socket / unreachable-missing-file / multi-repo /
  `--json` behavior).

## Peer-claim coordination: cross-host soft claim (#4028)

> **Advisory-only, not a reclamation-correctness dependency (Epic #6165).**
> Everything below is #4028's original design: a **fast, optional backoff**
> that shrinks the dispatch-race window described in the next paragraph. It
> was never meant to be load-bearing for *reclamation* correctness (deciding
> whether an already-claimed issue's `loom:building` holder is still alive) —
> but the implementation drifted from that design, and for a period
> `claim_reconciliation`'s reclamation decision froze while peer-claim
> coordination was judged DEGRADED (Issue #6157), making this channel's
> health a de facto reclamation dependency. Epic #6165 closes that gap with a
> genuinely fleet-scoped liveness source — the lease record
> ([`lease-record.md`](lease-record.md)), consulted by
> `claim_reconciliation::forge::reconcile_workspace` as the authoritative
> gate before any reclaim fires (Phase 2, #6286) — and Phase 4 (#6317)
> removed the peer-claim/DEGRADED freeze from that decision path entirely,
> restoring this channel to exactly the advisory, fast-backoff role
> described below. See
> [`lease-renewal-measurement.md`](lease-renewal-measurement.md) for the
> renewal-cost data backing that authority, and Epic #6165 for the full
> phase history.

On a multi-host deployment the only cross-host claim signal is the forge label,
whose `loom:issue → loom:building` flip is **not** compare-and-swap
(`SweepRegistry::flip_label_to_building` is an unconditional
`--remove-label`/`--add-label`) — two hosts can both read `loom:issue` and
dispatch before either flip propagates, producing duplicate sweeps. Peer-claim
coordination shrinks that window:

- **Advertise.** At the dispatch decision point — right after the local claim
  lock, **before** the label flip — the daemon publishes a claim advertisement
  over the room: issue number, [repo slug](#), host identity, PID, and a
  wall-clock timestamp, carried as a **`task`** envelope (the `type` enum is
  closed and owned by the safehouse repo, so a claim rides `task` with the bare
  issue number as `task_id` — **no fifth type is invented**) whose `body` is the
  structured JSON payload (marked `loom_claim`).
- **Consume.** A **dedicated inbound read task** — separate from the narration
  sink — drains the socket continuously via `select!`, so an **idle** daemon that
  emits no narration still observes peer claims promptly (the narration
  connection only reads while it is emitting). Each inbound claim is folded into a
  shared `PeerClaimView`.
- **Back off.** The work-finder skips any issue with a live peer claim, counted
  under its **own** distinct `peer-claim-skip` reason on the per-tick summary line
  (never folded into #4085's collision count).
- **TTL.** Every peer claim expires after **`safehouse.peerClaimTtlSecs`
  (default 120s = 2× the 60s work-finder interval)**, so a crashed peer cannot
  permanently starve an issue. The TTL clock is the **local receipt `Instant`**,
  never the advertiser's wall clock (clock skew is not comparable across hosts).
  A peer also **retracts** its claim early when its sweep exits/crashes (a
  `retract`-kind ad emitted from the reaper), freeing the issue before the TTL.
- **Host identity.** loom's single, explicit host-identity concept is
  `sweep_registry::host_identity()` (`LOOM_HOST_ID` > `$HOSTNAME` > the `hostname`
  binary > `unknown-host`) — derived, not a new config block, and stable across
  restarts. safehoused stamps the socket `from` from the *persona* (all daemons
  share `loom_daemon`), which cannot distinguish hosts, so the identity travels in
  the claim body and is what powers self-claim recognition: **a daemon never backs
  off on its own advertisement.**
- **Event taxonomy.** The internal pub/sub topic taxonomy is frozen; peer claims
  add **no new bus topic** — they travel entirely over the safehouse room.

### Which room claim ads ride: the signal room by default, opt-in dedicated room (#4225, #4713)

A claim ad is a `task` envelope carrying per-repo machine chatter, so the
attention-class table would put it in the repo firehose. By default it rides the
**signal room** instead — the one deliberate exception to "the signal room is
signal-only":

1. **The signal room is the only room with guaranteed common membership.** Firehose
   rooms are created **lazily** by whichever host narrates that repo first, and
   each host runs its **own bot account**. Host A creating `fleet-loom` does not
   join host B's bot to it, so an ad posted there is invisible to B until an
   operator invites it — silently disabling cross-host dedup with no error
   anywhere (exactly the failure class #4464 had to add a status state for). Every
   host's bot is already in the signal room.
2. **Dedup is correctness; room hygiene is cosmetics.** A missed ad costs a
   duplicate cross-host sweep (wasted tokens, two PRs for one issue); a little
   machine JSON in the signal room costs some scroll. Correctness wins, and this
   is why the dedicated-room escape hatch below is opt-in rather than the new
   default.
3. **The reader agrees with the writer by construction.** The coordination task's
   inbound handler is unfiltered — it folds *any* inbound line with a parseable
   `loom_claim` body into the view — so keeping the write side and the read side
   on the same resolved room makes the pair trivially consistent instead of
   dependent on which rooms this host's bot happens to have joined.

**The volume problem (#4713).** Ads are low volume per dispatch/terminal outcome,
but `sweep_registry::readvertise_peer_claims` re-publishes every **live** sweep's
claim on every 30-second reaper tick — deliberately, to stay under the
peer-claim TTL; this is a liveness requirement, not a bug, so it cannot simply be
slowed down. At fleet scale (several sweeps in flight) that heartbeat cadence is
enough to flood the human-facing signal room with near-identical machine
messages, burying genuine sweep-lifecycle narration.

**The fix: `rooms.claims` (opt-in, default-unchanged).** An operator who finds
the heartbeat traffic disruptive can set `rooms.claims` (or
`LOOM_SAFEHOUSE_ROOM_CLAIMS`) to route claim advertise/retract traffic into a
**dedicated coordination room**, separate from both the signal room and the
per-repo firehose:

```jsonc
"safehouse": {
  "enabled": true,
  "rooms": {
    "signal": "!AbC…:example.org",
    "claims": "!JkL…:example.org"   // peer-claim heartbeats route here instead
  }
}
```

- **Resolution**: `rooms.claims` when set, else `rooms.signal` (which itself
  falls back to the legacy scalar `room`) — `SafehouseConfig::claims_room()`
  mirrors `signal_room()`'s own fallback chain one level down.
- **Absent (the default) is byte-identical to pre-#4713 behavior**: claim ads
  keep riding the signal room exactly as before. This is not merely convenient —
  it is the safe default, because of the provisioning caveat below.
- **This is provisioning, not just routing** — the reason #4225 deferred this in
  the first place. Setting `rooms.claims` to a room only some hosts' bots have
  joined reproduces the exact silent cross-host dedup failure reason 1 above
  exists to avoid: a host whose bot is not a member of the configured claims
  room silently stops seeing peers' advertisements, with no error anywhere.
  **Before setting `rooms.claims`, ensure every host's safehoused bot is already
  joined to that room** — the identical requirement already documented for
  `rooms.signal`.
- The narration sink is unaffected: `run_sink`'s attention-class routing
  (signal / per-repo firehose) keeps using `signal_room()`/`RoomRouter` exactly
  as before. Only the peer-claim coordination connection
  (`run_coordination`/`spawn_peer_coordination`) resolves `claims_room()`.
  This holds even for a **claims-only** map (`rooms.claims` set with no
  `rooms.signal`/`rooms.byRepo`): attention-class narration routing gates on a
  narration target (`signal` and/or `byRepo`), so a claims-only map keeps
  narration in byte-identical single-room mode — never lazily creating per-repo
  firehose rooms as a side effect. `rooms.claims` and narration routing are
  independent knobs.
- The inbound reader is unchanged and remains **room-agnostic**: it folds any
  parseable `loom_claim` line regardless of which room delivered it, so this
  change is purely about which room this daemon *writes into* — receipt-side
  filtering (teaching the separate `rjwalters/safehouse` server to mark
  `loom_claim` envelopes as ephemeral coordination traffic that never surfaces
  in a human-facing feed) is tracked as a **separate, out-of-scope, cross-repo
  follow-up**, not implemented here.

### Soft claim, NOT a mutex (the load-bearing caveat)

A room broadcast is eventually consistent, so this is a **fast backoff, not a
lock**: two hosts advertising near-simultaneously still race. Advertisement
*shrinks* the collision window; it does not close it. The atomic authority for
the final claim — a real cross-host CAS (e.g. a `git push` to a claim ref) — is
**Phase 2 of #4028**, deliberately out of scope here.

### Fail-open (never a liveness dependency)

Coordination is best-effort end to end: an unreachable/refusing/timing-out
`safehoused` socket, a malformed inbound envelope, or a full outbound channel is
logged (once) and **dispatch proceeds normally**. The outbound advertisement is a
bounded, non-blocking `try_send` off the dispatch path; a `Full`/`Closed` channel
drops the ad. `safehouse.enabled` false/absent is a **byte-for-byte no-op**: no
view, no channel, no coordination task, no socket.

### Fleet-wide completion dedup: reusing the peer-claim channel (#6352)

The [per-host completion dedup](#what-gets-narrated) documented under
"Completion envelopes → the public fleet feed (#4426)" above is exactly
that — **per host**. On a multi-dispatcher fleet, a build split
across hosts (host A opens the PR, host B's Champion merges it — or two hosts'
`reconcile_recent_merges` ticks both discovering the same champion merge before
either has recorded it locally) meant **each** host independently narrated its
own `completion` envelope for the same merge: distinct Matrix `event_id`s, so
the sink's `event_id` dedup does not collapse them, and the public feed showed
the same PR outcome twice (evidence: anvil PR #1124 and siblings, narrated
~30-40s apart by two hosts, 2026-08-16).

The fix reuses the peer-claim channel (#4028) described above rather than a new
socket or protocol amendment — a third `ClaimKind::Completed` ad rides the exact
same `task`-typed envelope, the same outbound `mpsc::Sender<ClaimAd>`, the same
`run_coordination` connection, and (by default) the same signal room as
`Advertise`/`Retract`:

- **Publish.** The instant `build_and_narrate_completion` — the shared
  envelope-build/dedup-insert core behind **both** trigger paths
  (`SweepExited` and `reconcile_recent_merges`) — successfully builds and
  sends a `completion` envelope, it publishes a `Completed` ad for that
  `(repo slug, issue, merged-PR-number)` over the same channel dispatch
  already uses — `ClaimAd` grew a `pr: Option<u32>` field for this (issue
  #6062; `None` for `Advertise`/`Retract`, always `Some` from this binary's
  own `ClaimAd::completed` constructor). Publish is fire-and-forget /
  fail-open, mirroring `publish_peer_claim`'s own contract: a dropped ad
  (channel `Full`/`Closed`, socket unreachable) never blocks or unwinds a
  narration that already succeeded locally — the completion still reaches the
  feed from this host either way, and worst case a peer that missed the ad
  narrates a rare duplicate.
- **Consume, in a separate map from claims.** `PeerClaimSink` — the same
  inbound consumer that already folds `Advertise`/`Retract` into
  `PeerClaimView`'s dispatch-claims map — routes a `Completed` ad by kind into
  a **second**, independent map (`PeerClaimView::observe_completion_at`/
  `is_narrated_at`) rather than the dispatch-claims one. This separation is
  deliberate: a completion is a one-shot durable fact with no heartbeat to
  refresh it (unlike a live, re-advertised dispatch claim), and — critically —
  observing one must **never** perturb the `#6157` peer-coordination-health
  bookkeeping (`advertised`/`received`/`expired`/`dispatch_skipped` counters,
  the DEGRADED/recovered verdict) that dispatch-claim receives feed. A
  narration-layer event answers a different question than "is dispatch
  coordination healthy", so it is invisible to that machinery entirely.
- **Check before narrating.** `build_and_narrate_completion` consults the
  fleet-wide view — keyed by the same
  [cross-host-stable repo slug](#which-room-claim-ads-ride-the-signal-room-by-default-opt-in-dedicated-room-4225-4713)
  peer claims already use (`$LOOM_REPO`, else the workspace directory
  basename), plus the issue and the merged PR number — **before** any
  forge/token work: if a peer already narrated this `(repo, issue, pr)`, this
  host adopts that outcome into its own local `already_narrated`/persisted-file
  dedup state (so its *own* future `SweepExited`/reconciliation passes also
  short-circuit locally) instead of posting a second envelope, and does **not**
  re-publish its own `Completed` ad (that would just re-arm every peer's TTL
  forever for no reason). A host with no peer coordination established
  (`safehouse.enabled` false, or enabled with no socket ever resolving) sees
  `None` throughout and degrades byte-for-byte to the pre-#6352 per-host-only
  behavior.
  - **Keyed on the merged PR number, not the issue alone (issue #6062).** The
    original #6352 shape keyed this view on `(repo, issue)` alone, which
    (like the per-host set above) could not distinguish "a peer already
    narrated *this* merge" from "a peer narrated a *different* merge against
    the same still-open issue" — permanently suppressing every completion
    after the first one for an issue with more than one merged PR, fleet-wide,
    for the rest of the (24-hour default) TTL. A `Completed` ad received from
    a peer still running a pre-#6062 binary carries no `pr` field at all; it
    degrades to key `0` rather than being dropped, so a mixed-version fleet
    mid-rollout can under-narrate for the transition window (bounded by the
    completion TTL) but never crashes or mis-keys against a *known* PR number.
- **TTL: much longer than the dispatch-claim TTL, and independently
  configurable.** `safehouse.peerCompletionTtlSecs` (env
  `LOOM_PEER_COMPLETION_TTL_SECS`) defaults to **24 hours** — deliberately far
  beyond `safehouse.peerClaimTtlSecs`'s 120s default, because a completion has
  no heartbeat re-advertising it (unlike a live dispatch claim) and the race
  it guards against (two hosts' independent reconciliation ticks, default
  5-minute cadence, both observing the same merge before either's ad has
  propagated) needs a window well beyond one reconciliation tick. A double-post
  after the window lapses is an accepted paper-cut — the same "soft, not a
  mutex" posture the dispatch-claim TTL already accepts — not a correctness
  gate: a rare duplicate narration wastes no build tokens and corrupts no
  state, unlike a duplicate *dispatch*.
- **Ordering dependency at startup.** `WorkspacePool::start_safehouse_narration`
  reads back the publisher + view `WorkspacePool::start_peer_coordination`
  establishes (to build the handle the narration sink uses), so peer-claim
  coordination **must** be started first — `daemon_service::run` does so.
  Reversing that order would silently leave completion dedup per-host-only
  even with `safehouse.enabled` true; neither call blocks on the other's
  socket connecting, only on the synchronous, non-blocking bookkeeping that
  establishes the shared publisher/view pair.

# Phase 2 — worker-side `safehouse-mcp` injection (#3999)

Phase 1 lets the daemon *narrate*. Phase 2 gives each **worker** session a
two-way handle: when the `safehouse` block is enabled, Loom injects the
`safehouse-mcp` stdio MCP server (`rjwalters/safehouse`, tools `safehouse_send` /
`safehouse_read` / `safehouse_create_room` / `safehouse_list_rooms`; env
`SAFEHOUSED_SOCKET` + `SAFEHOUSE_PERSONA`) into the worker's MCP config, so a
Builder can ask a question in the room mid-task and read the human's answer
instead of only signalling through labels. The MCP server holds no keys — the
socket path is the only credential-adjacent value written.

## Per-worker persona: a bounded pre-registered pool (design decision)

safehoused's persona allowlist is a **static boot-time TOML array** with no
runtime registration, no glob/prefix matching, and no SIGHUP reload (see phase-1
note above). So a literal per-issue name like `loom_builder_42` **cannot** be
minted at dispatch time — safehoused would reject the `hello` for a name not in
its boot allowlist, and it cannot restart per worker.

Loom therefore assigns each worker a persona from a **bounded pool the operator
pre-registers** in safehoused's allowlist ahead of time — the same "fixed pool,
rotate per slot" shape as the token pool. Configure the pool in the `safehouse`
block and add the identical names to safehoused's `personas`:

```jsonc
"safehouse": {
  "enabled": true,
  "socket": "/run/safehoused.sock",
  "persona": "loom_daemon",                     // scalar fallback (daemon + no-pool workers)
  "workerPersonas": ["loom_builder_1",          // the pre-registered worker pool
                     "loom_builder_2",
                     "loom_builder_3",
                     "loom_builder_4"],
  "mcpCommand": "safehouse-mcp"                  // launcher for the stdio MCP server
}
```

```toml
# safehoused config — restart required after editing (allowlist read once at boot)
personas = ["loom_daemon", "loom_builder_1", "loom_builder_2", "loom_builder_3", "loom_builder_4"]
```

Each worker is assigned `workerPersonas[issue_number % pool_size]` (round-robin
by worktree slot — the issue number comes from `LOOM_SWEEP_CLAIM_OWNED`). Two
**concurrently-running** workers (distinct issue numbers) get distinct personas
whenever the pool is at least as large as the concurrency level and the numbers
do not collide mod N — so size the pool to your max concurrent workers. With **no
`workerPersonas`** configured, every worker falls back to the scalar `persona`
(workspace-wide, no per-worker attribution) — the feature degrades, never fails.

Env overrides (each wins over config): `LOOM_SAFEHOUSE_WORKER_PERSONAS`
(comma-separated pool), `LOOM_SAFEHOUSE_MCP_COMMAND`, plus the phase-1
`LOOM_SAFEHOUSE_ENABLED` / `LOOM_SAFEHOUSE_SOCKET` / `LOOM_SAFEHOUSE_PERSONA`.

## Delivery: session-scoped `--mcp-config` at spawn time

Injection happens in `spawn-claude.sh` (the mandatory agent spawn path), not by
rewriting the workspace `.mcp.json`. Concurrent sweeps **share** the workspace
root, so a per-worker persona cannot live in that shared file; instead
spawn-claude generates a **session-scoped** MCP config (persona substituted for
this worker) and passes it via `claude --mcp-config <file>`. The file lists the
`loom` server FIRST (so it is self-contained even when the session cwd has no
project `.mcp.json`) and appends `safehouse` second.

`scripts/setup-mcp.sh` (the workspace-root generator, reached inside worktrees
via the `.mcp.json` symlink `worktree.sh` creates) **also** learns to append the
`safehouse` server when enabled — but with the scalar `persona`, since it is not
per-worker. Both writers keep `loom` first and unchanged so
`claude-wrapper.sh`'s MCP pre-flight (which keys off the first server with args)
still resolves the loom entry point.

## Degradation contract (unchanged from phase 1)

- `safehouse.enabled` false/absent ⇒ **byte-for-byte no-op**: spawn-claude
  appends no `--mcp-config`, and setup-mcp emits the identical loom-only file.
- Enabled but the launch command is missing, or no socket resolves ⇒ one
  `warn`, **injection skipped**, the `loom` MCP server unaffected and the worker
  starts normally.
- Socket configured but not yet present at spawn ⇒ one `warn`, injected anyway
  (best-effort — `safehouse-mcp` connects lazily and never blocks the worker).
- A persona absent from safehoused's boot allowlist is rejected by safehoused at
  `hello` with a clear message — provision the whole pool before enabling.

## Implementation (phase 2)

- `defaults/scripts/lib/mcp-config.sh` — shared resolvers (env > config >
  default, mirroring `safehouse.rs`), the pool round-robin persona picker, and
  the `loom`-first `.mcp.json` emitter.
- `defaults/scripts/spawn-claude.sh` — per-worker injection via `--mcp-config`.
- `scripts/setup-mcp.sh` — workspace-root two-server generation when enabled.
- Tests: `defaults/scripts/tests/test-mcp-config.sh`.

> The exact `safehouse-mcp` binary/protocol lives in the external
> `rjwalters/safehouse` repo and is not verifiable from this repo, so the
> launcher is configurable (`safehouse.mcpCommand`) and a missing command
> degrades to a logged skip rather than a broken server entry.
