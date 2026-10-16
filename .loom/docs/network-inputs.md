# Network inputs to loom-daemon: audit and classification

Audit for [#11711](https://github.com/rjwalters/loom/issues/11711), part of
the operator's config-only-inputs initiative. It answers one question: **what does `loom-daemon` accept
or fetch from outside the process that can change what it does, and is each
such input config, forge, a wake-only webhook, or removed?**

- Audited at loom commit `4b3b3e77e` (main, 2026-10-13).
- Fleet-specific findings (host inventory, private tracking) are kept out of this install-wide doc.
- Code locations name files and functions that were read, not guessed. Line
  numbers are as of the audited commit and will drift; the symbol is the
  durable handle.

## The operator rule

Operator decisions of 2026-10-13 (the config-only-inputs rule):

1. **Apart from its configuration files, nothing from the outside world sends
   data or instructions into the daemon.** The control inputs are `fleet.json`
   and the host tiers rendered from the operator's fleet config repo, plus the repo's own
   committed `.loom/config.json`.
2. **The forge is the work surface, not a control input.** The daemon reads
   issues, PRs, labels and CI from the forge it works on.
3. **GitHub webhooks through the Cloudflare Worker are the one optional,
   wake-only exception** (the forge event plane, ADR-0021).
4. After the change, a service being down changes only telemetry delivery,
   never what the daemon does.

## Classes

Each row carries exactly one class.

| Class | Meaning |
|---|---|
| `config` | Reads fleet-gitops or repo config, **or** is a host-local surface that only processes on the same machine can reach and that carries no data from a remote party (IPC socket, loopback relay). The second reading is an interpretation; see Open question 1. |
| `forge` | Reads or writes the forge the daemon works on (GitHub via `gh`, `git` remotes, REST probes). Observation of work state, not an instruction channel. A `‡` marks a row whose class is provisional and listed under Open questions. |
| `webhook-wake-only` | The optional Worker-fed event feed. May only bring a loop's next tick forward (or skip a poll the loop would have done), never carry state. |
| `removed` | Must not remain a daemon input. The row names the replacement (a fleet-gitops key, or the literal word `nothing`). |

## Reproducible sweeps

Run from the repo root at the audited commit. The first two are the commands
the Curator specified; they are intentionally broad so that nothing is
silently missed, and the counts include comments and doc strings.

```bash
# Sweep 1: every HTTP client, listener, socket connect and curl mention
rg -n "reqwest|ureq|hyper|curl|TcpListener|UnixListener|TcpStream::connect|UnixStream::connect" \
  loom-daemon/src -g '!*tests*'
# 2026-10-13 @ 4b3b3e77e: 331 matching lines in 63 files

# Sweep 2: literal gh / curl / git spawns
rg -n 'Command::new\("(gh|curl|git)"' loom-daemon/src -g '!*tests*'
# 2026-10-13 @ 4b3b3e77e: 309 lines in 126 files (4 are curl, see Sweep 4)
```

Sweep 1 is noisy: `hyper` matches `hyperparams` (about 80 lines), and
`UnixListener`/`TcpListener` matches inside `#[cfg(test)]` modules that live in
non-`*tests*` files (`serve.rs` below roughly line 1220, `exporter.rs`,
`jev_merge_risk.rs`, `cli/dispatch.rs`, `cli/status.rs`, `cli/cancel.rs`,
`cli/fleet_send.rs`, `safehouse/envelopes.rs`, `serve/api_keys.rs`). These
tighter forms isolate the production hits and are what the tables below were
built from:

```bash
# Sweep 3: production listeners (hits inside cfg(test) modules removed by hand)
rg -n 'TcpListener::bind|UnixListener::bind' loom-daemon/src -g '!*tests*' -g '!*_tests.rs'
# Production binds, exactly four:
#   loom-daemon/src/ipc.rs:486                                    UnixListener::bind
#   loom-daemon/src/worker_spawn/egress_proxy/server.rs:102       TcpListener::bind (Bound::bind)
#   loom-daemon/src/observability/otlp/relay.rs:170               TcpListener::bind (bind_loopback)
#   loom-daemon/src/cli/serve_cmd.rs:31                           TcpListener::bind (loom-daemon serve)

# Sweep 4: curl shell-outs (the only non-gh, non-git network binaries)
rg -n 'Command::new\("curl"\)' loom-daemon/src -g '!*tests*'
# forge_probe.rs:168, script_helpers/usage.rs:54, write_scope/probe.rs:376, tokens_pool/check.rs:344

# Sweep 5: files that build a reqwest client in production code
rg -ln 'reqwest::Client|Client::builder' loom-daemon/src -g '!*tests*'
# observability/exporter.rs, observability/ops/quota/zai.rs, signoz_read.rs,
# forge_events.rs, jev_merge_risk.rs, worker_spawn/egress_proxy/server.rs,
# loom_ui_alerts/ (via observability/request_headers.rs), observability/otlp/

# Sweep 6: no HTTP framework, no other HTTP client crate
rg -n 'axum|ureq|isahc|hyper' loom-daemon/Cargo.toml        # no hits
rg -n 'reqwest' loom-daemon/Cargo.toml                       # one dependency

# Sweep 7: what is left of the retired inbound channels
rg -n -i 'MIRROR_PATH|handleGhMirror|gh.?mirror' loom-daemon/src      # comment in forge_cmd.rs only
rg -n -i 'safehouse_chatops|concierge' loom-daemon/src                 # tests/comments only, no module
```

Two limits a reviewer should know. `Command::new(<variable>)` spawns are not
matched by Sweep 2 (the `gh_invocation` choke point resolves the binary from
policy, so many forge calls are built through it); the module list in row 17
was therefore assembled from `rg -l 'gh_invocation|forge_'` as well as Sweep 2.
And Sweep 2 only sees the daemon crate. `mcp-loom/` is row 5; `loom-api/` is
listed under "Not an input". Sweep 6's "no HTTP framework" applies to
`loom-daemon/Cargo.toml` only.

## Inputs that change daemon behavior

### Listeners

| # | Input | Direction | Code location | Transport / peer | What it changes in daemon behavior | Gating config key (default) | Class | Replacement or follow-up |
|---|---|---|---|---|---|---|---|---|
| 1 | IPC socket | in | `ipc.rs` `IpcServer::run` (`UnixListener::bind`, :486); client side `cli/common.rs` `resolve_socket_path` | `AF_UNIX` at `~/.loom/loom-daemon.sock` (`LOOM_SOCKET_PATH` overrides). Peers: local `loom-daemon` CLI, `mcp-loom`, `serve`, `fleet_alert` | Operator commands: dispatch, cancel, drain, status, event publish and subscribe. The only way to steer a running daemon by hand | none (always on) | `config` | Not a network input. The code sets no explicit socket mode (no `set_permissions` in `ipc.rs`): reach is governed by the socket's directory and umask, so the boundary is the same OS user. See Open question 1 |
| 2 | Egress proxy | in (from worker containers and native harnesses) | `worker_spawn/egress_proxy/server.rs` `Bound::bind` (:102) and `serve`; bind choice in `egress_proxy/mod.rs` `resolve_bind` | Hand-rolled HTTP/1.1 over TCP, ephemeral port. Loopback by default (`Scope::NativeLoopback` is always loopback); docker bridge gateway on Linux when containers cannot reach loopback; `LOOM_EGRESS_PROXY_BIND` overrides. Per-launch placeholder token authenticates the caller | Forwards provider API calls outward, swapping a placeholder for the real credential. One inbound request shape alters daemon state: a worker's `proxy-rotate` (`rotate_client.rs`) asks the registry to rotate that launch's account | Per launch: a proxied runtime profile, or `LOOM_NATIVE_CREDENTIAL_PROXY` (off unless set) | `config` | Local, token-gated, serves only the daemon's own workers. On the Linux bridge it is reachable by other containers on that bridge; the module header documents this and why the token still gates it. Provider responses on the way back are row 21 |
| 3 | Agent OTLP relay receiver | in (from agent sessions the daemon launched) | `observability/otlp/relay.rs` `bind_loopback` (:169) | OTLP/HTTP (protobuf or JSON) on `127.0.0.1`, ephemeral port, per-session token in `OTEL_EXPORTER_OTLP_HEADERS` | Telemetry only: records are identity-bound, scrubbed, queued and forwarded to the `otlp` exporter. Nothing in a request reaches a dispatch or admission decision | `observability.agentRelay.enabled` (`false`; env `LOOM_OBSERVABILITY_AGENT_RELAY`), and an `otlp` exporter must be running | `config` | none |
| 4 | `loom-daemon serve` dashboard | in | `cli/serve_cmd.rs` `handle_serve_command` (`TcpListener::bind`, :31), `serve.rs` `validate_bind`, `run_with_state` | Hand-rolled HTTP/1.1, `GET` only: `/`, `/api/status`, `/api/events` (SSE), `/api/health`, `/api/pipeline`, `/api/tokens`, `/api/peers`, `/api/api-keys`. Default bind `127.0.0.1:7420`; non-loopback needs `--allow-non-loopback`; wildcard is refused | Nothing. Read-only and stateless; it proxies the IPC socket and never publishes an event. `--peers` is a list returned to the browser, which fans out itself; the daemon fetches no peer | Explicit `serve` subcommand only; never started from a config value | `config` | Serves data outward; accepts no instruction. Responses carry `Access-Control-Allow-Origin: *` by design (`CORS_ALLOW_ORIGIN_HEADER`), so an operator who binds a tailnet address exposes status to any page that can reach it |
| 5 | MCP server | in (local MCP client) | `mcp-loom/src/index.ts` (`StdioServerTransport`), `mcp-loom/src/shared/daemon.ts` (`socket.connect(SOCKET_PATH)`) | stdio to the MCP client; from there to row 1. No listener of its own | Same commands as row 1, e.g. `dispatch_sweep` | none | `config` | Not a daemon listener; shown so the Curator's "find the MCP entry" item is closed |

### Forge event feed (the optional webhook exception)

| # | Input | Direction | Code location | Transport / peer | What it changes in daemon behavior | Gating config key (default) | Class | Replacement or follow-up |
|---|---|---|---|---|---|---|---|---|
| 6 | Forge event feed | out request, in response | `forge_events.rs` `spawn_task`, `FeedClient::poll_once`, `resolve_endpoint`, `resolve_event_key_file`; consumers in `forge_events/wake.rs` (`WORK_FINDER`, `CLAIM_RECONCILE`, `IN_FLIGHT_PR` descriptors); `ci_telemetry/feed.rs` | HTTPS `GET {endpoint}/v1/hosts/{host}/events?after=&limit=` with a per-host bearer key re-read from a file on every poll. Peer: the operator's Cloudflare Worker (HMAC-verified GitHub App deliveries) | Publishes one `forge.event` bus prompt per non-empty page. Each consumer turns it into an **early tick of a loop that already exists**: `EarlyTicker::tick` returns `()`, so no event content reaches a decision. `ci_telemetry` additionally uses a finished `workflow_run`'s `(repo, run_id)` key to fetch that run from the forge (telemetry only). A dead feed leaves exactly the polling floor | `forgeEvents.enabled` (`false`), `.endpoint`, `.hostId`, `.eventKeyFile`, `.pollIntervalSecs`, `.pageSize`; per consumer `forgeEvents.events.workFinderTick`, `.claimReconcileWake`, `.inFlightPrWatch` (all `false`) | `webhook-wake-only` | Kept per the operator amendment. `fleet.json` has no `forgeEvents` key on any host, so it is off fleet-wide today |
| 7 | Event-gated polling | derived from row 6 | `forge_events/poll_gate.rs` `gated_list`, `PollGate::decide` | In-process, fed by row 6's events | Opposite direction from a wake: while the feed is `healthy`, a workspace with no event since its last poll **skips its discovery poll**, until a hard cap of `min(10 x base cadence, 15 min)`. Only `ReadKind::Discovery` can be skipped; claim, label-transition and merge reads always poll. Any non-healthy status restores the base cadence at once | `forgeEvents.pollGating` (`false`; env `LOOM_FORGE_EVENTS_POLL_GATING`) | `webhook-wake-only` | This is not strictly "a wake": it can delay a poll, never speed one up. Bounded to 15 minutes and fail-safe, but it is data from outside deciding to do less. See Open question 2 |
| 7a | State mirror: discovery reads and change-stream wakes | out request, in response | `forge_mirror.rs` (config, `Refresher`), `forge_mirror/discovery.rs` `list_open` (called from `work_finder/forge.rs` and `observability/ops/stage_dwell.rs`), `forge_mirror/wake.rs` `spawn`, `pass` | HTTPS `GET {origin}/v1/mirror/items` and `GET {origin}/v1/changes` with a bearer key read from `forge.mirror.keyRef` (SSM or an owner-only file). Peer: the operator's state-mirror service | Discovery listings (work-finder candidates, stage-dwell stage listings) are served from the mirror only while the repo's watermark is within `maxStalenessSecs` (cap 15 min); otherwise, and on any error, the listing reads the forge as before. Claim, label-transition and merge reads never use it. Change-stream pulls publish `forge.event` prompts (`source: github-events-mirror`) that tick armed consumers early, as row 6 | `forge.mirror` (unset: off), `.origin`, `.keyRef`, `.region`, `.maxStalenessSecs` (600), `.changePollSecs` (30); early ticks also need row 6's per-consumer flags | `webhook-wake-only` | Discovery state from outside, bounded by the watermark and falling back on any doubt; never a decision input. See [`forge-events.md`](forge-events.md) §8 |

### Controls delivered by services

| # | Input | Direction | Code location | Transport / peer | What it changes in daemon behavior | Gating config key (default) | Class | Replacement or follow-up |
|---|---|---|---|---|---|---|---|---|
| 8 | `/ingest` response: operator-priority and workspace-priority intents | in (response body) | `observability/exporter.rs` `HttpsExporter::emit_batch`, `handle_ack`, struct `IngestAck`; loom-ui side `src/routes/ingestRoute.ts` `ackWorkForHost` | HTTPS `POST` to `observability.endpoint` with the host ingest key | **Nothing today.** `IngestAck` deserializes only `host_id`. `operator_priority_intents` was retired by #11104 and a backend that still sends it is ignored (`a_retired_intents_field_on_the_ack_is_ignored`); the 64 KiB body bound remains so a misconfigured endpoint cannot be buffered into memory. Decision bounces never reached the daemon | `observability.enabled`, `observability.exporters` containing `"https"`, `.endpoint`, `.ingestKeyFile` | `removed` | Replacement: fleet-gitops config (`operator_levels` already exists; per-workspace priority follows it), tracked in the operator's fleet config repo. Daemon side is already done for the control fields; the rest of the `https` exporter goes with rjwalters/loom#11712 steps 1-2. **Correction for #11712 step 1:** there are no intent or bounce acks left to stop parsing, only the `host_id` echo (see "Not an input") |
| 9 | Safehouse peer claims (inbound room events) | in | `safehouse.rs` `spawn_peer_coordination` (dedicated inbound read task), `safehouse/claim_sink.rs` `PeerClaimSink::on_event`, `peer_claims.rs` `PeerClaimView`; consumed in `sweep_registry/guards.rs` (`detect_and_record_collision`) and `SweepRegistry::dispatch` | `AF_UNIX` NDJSON to the local `safehoused`, which holds a Matrix room. Peer: other hosts' claim advertisements | A live peer claim makes this host **back off an issue before dispatch**, retracting its own advertisement and lock. Soft, TTL-bound (`peerClaimTtlSecs`, 120) | `safehouse.enabled` (`false`; env `LOOM_SAFEHOUSE_ENABLED`), `safehouse.socket`, `safehouse.rooms.claims` (env `LOOM_SAFEHOUSE_ROOM_CLAIMS`) | `removed` | Replacement: `nothing`. ADR-0025 already decides the forge lease is the only cross-host claim. Covered by rjwalters/loom#11112 (safehouse removal) and #11712 step 3 |
| 10 | Safehouse filing-lock mirror | in | `safehouse/claim_sink.rs` `mirror_filing_lock_to_disk`, `filing_lock.rs` `record_peer_hold` / `clear_peer_hold` | Same channel as row 9, a `FilingLock` / `FilingUnlock` ad | Writes a marker file whose mtime (local receipt time) ages out a **peer's hold on issue filing**, which the local filing lock then respects | same as row 9 | `removed` | Replacement: `nothing` (host-only serialization, which `filing_lock.rs` documents as the degraded mode). Covered by #11112 |
| 11 | Safehouse ChatOps and Concierge | in | No module at this commit: `rg safehouse_chatops` finds tests and comments only. Residue: `types.rs` `dispatch_origin` doc, per-repo `.loom/config.json` `chatops` / `concierge` blocks, and `defaults/docs/safehouse.md` sections that still describe them | Was Matrix room text via `safehoused` | Was: typed verbs (dispatch, cancel, ...) and an LLM persona proposing them. The daemon-side parser is gone (#11112 slice 1) | `safehouse.chatops.*`, `safehouse.concierge.*` (`enabled: false` everywhere) | `removed` | Replacement: `nothing`. Residual cleanup (stale seeds, stale doc sections, config schema keys) belongs to #11112's remaining slices |
| 12 | loom-ui GitHub mirror route (`MIRROR_PATH`, `handleGhMirrorRoute`) | in | **No daemon client exists.** `rg -i 'MIRROR_PATH\|handleGhMirror\|gh.?mirror' loom-daemon/src` finds one doc comment in `forge_cmd.rs` (about the retired `forge_cli.py`). The `peer_claims.rs` hit for "mirror" is the filing-lock mirror, row 10. `forge_pr_congestion.rs`, `forge_disable_auto_merge.rs`, `forge_cached_*`, `forge_read_pool.rs` and `forge_etag_store*` all read through `gh` (row 17), not a dashboard | n/a | n/a | Nothing: the "push-not-poll via a loom-ui read mirror" idea (#10332) never landed in the daemon | none | `removed` | Replacement: `nothing` (daemon reads GitHub directly). Nothing to delete daemon-side. loom-ui owns removing the route itself |

### Config from fleet-gitops and the repo

| # | Input | Direction | Code location | Transport / peer | What it changes in daemon behavior | Gating config key (default) | Class | Replacement or follow-up |
|---|---|---|---|---|---|---|---|---|
| 13 | Fleet store fetch (`fleet.json`, `repos.yml`, `fleet/state.yml`, `fleet/admins.json`) | in | `fleet_store/fetch.rs` `load` with `Policy::AllowStale` / `FailClosed`; `fleet_store/compiled.rs`, `roster.rs`, `state.rs`, `admins.rs`, `floor.rs`; driver `fleet_sync.rs` `start` | **GitHub REST through the daemon's own `gh` forge path and GitHub App credentials, not `git`**: `GET repos/{repo}/commits/{ref}` (ETag, `304` is free), then `git/trees/{sha}?recursive=1` and `git/blobs/{blob}` for changed contract files. Cached under a manifest of blob SHAs | Everything the operator calls config: both config tiers, the repo roster (fail closed), this host's desired run state, the admin roster that comment trust consults, `loom_min_version` | `fleet.repo` (env `LOOM_FLEET_REPO`), `fleet.ref` (`LOOM_FLEET_REF`, default `main`), `fleet.autoApply`, `fleet.syncIntervalSecs`; unset means the feature is off | `config` | This is the operator's own definition of config (a reviewed commit to the fleet config repo). The transport is the forge API, so a forge outage degrades to the last good snapshot; the roster fails closed. Keys that come in are listed above |
| 14 | Captain gauge heartbeat in the fleet store | in | `observability/captain_gauges.rs` (`captain-gauges/v1` heartbeat on branch `fleet.captainGauges.ref`, default `eta-fit`) | Same `gh` REST path as row 13, conditional read of a non-`main` store branch written by another host | Only which host **produces telemetry gauges**: a dispatcher stops producing a gauge job for repos a fresh captain heartbeat covers, and `queueBlocked` builds blocked rows of `queue.snapshot` from it. Per-host gauges and every dispatch gate stay local | `fleet.captainGauges.enabled` (`false`), `.standDown` (`false`; env `LOOM_CAPTAIN_GAUGES_STAND_DOWN`), `.queueBlocked` (`false`) | `config` | Transport is the config store, but the content is runtime data written by another host, not operator config. It steers telemetry only. See Open question 4 |
| 15 | Workspace sync: installed-Loom resync, checkout fast-forward, roster clone | in | `fleet_sync/workspace_resync.rs`, `fleet_sync/checkout_ff.rs` (`checkout_ff/git.rs`), `fleet_sync/roster_clone.rs` | `git` (fetch, fast-forward, clone) over the workspace's own remote, plus forge reads via `gh` to classify the default branch | Advances each registered workspace's main checkout to the default branch, so the host dispatches from the committed `.loom/config.json`, role prompts and spawn scripts. `roster_clone` clones a missing roster repo | `fleet.autoApply` (`true` in the fleet-gitops defaults), `fleet.cloneMaxPerPass`; `checkout_ff` only fast-forwards a clean, strictly-behind checkout | `config` | The repo's committed `.loom/config.json` is config by the operator's definition. The same pull also carries role prompts and scripts, which are instructions to agents rather than daemon config; see Open question 3 |
| 16 | Auto-update: release resolve, fetch and verify | in (binary artifact) | `release_resolve/resolve.rs` `resolve` (`gh release view` / `list`), `release_fetch/fetch.rs` `fetch_and_verify` (`gh release download`), `release_fetch/{checksum,signature,cosign,glibc,source}.rs`, `auto_update/floor_roll.rs` `select_target`, `auto_update/loop_mode.rs`, `auto_update/runner.rs`, `self_update.rs`, `daemon_update/*` | `gh` against the releases of the Loom repo. Verification: `.sha256` sibling is **unconditional**; `.sig`/`.pem` signature is present-only by default, required by `SignaturePolicy::require_signature`; GLIBC compatibility; optional approved-source anchor `LOOM_DAEMON_UPDATE_APPROVED_SOURCE_ANCHOR` with tag-ancestry and tag-movement checks | Replaces the running daemon binary. Fleet hosts move **only** when `loom_min_version` (row 13) rises above the running version: they never chase the newest release and never rebuild from source (#10885, `floor_roll.rs`). A host with no fleet store may opt in to chasing latest, settle-gated | `loom_min_version` (fleet-gitops); `autonomous.autoUpdate.enabled` (`false`) only for hosts without a fleet store | `forge` ‡ | Classified `forge` for transport and `config` for trigger, deliberately not silently `config`. The trigger on a fleet host is operator config, but the bits are code that the forge publishes. Open question 3 asks the operator to rule on it. Integrity: checksum always, signature when published |
| 17 | Forge reads and writes through `gh` (work surface) | in and out | Pollers and their forge reads: `work_finder` (`work_finder/main_red_fix.rs` and the per-root listing), `intake_reconcile` (`singleton.rs`), `claim_reconciliation`, `epic_supervisor`, `watch_registry/monitor_loop.rs`, `main_health_gate.rs`, `metrics_collector.rs`, `ci_telemetry/{feed,poll}.rs`, `observability/ops/ratelimit.rs`. Plumbing: `gh_invocation/` (single spawn choke point), `forge_listing.rs`, `forge_pull_listing.rs`, `forge_read_pool.rs`, `forge_cached_*.rs`, `forge_etag_store*`, `forge_repo_facts/`, `forge_wait_checks/`, `forge_merge_queue/`, `forge_pr_congestion.rs`, `forge_disable_auto_merge.rs`, `forge_identity/`, `credential_preflight.rs`. Writers: `merge_pr/`, `sweep_registry` label flips, `cli/lease_*`, `cli/push_lease.rs`, `forge_comment/`, `forge_cmd.rs`. Rate-limit circuit breaker: `rate_limit_breaker.rs` | `gh` subprocess (REST, GraphQL, ETag-conditional) to GitHub | The daemon's work: which issues are ready, claim state and lease records, PR and CI state, merges, label transitions. Comment text is untrusted input (`comment-trust.md`). The rate-limit breaker pauses `gh`-polling loops when the shared budget is exhausted | Per-loop cadences (`work_finder` interval, `claimReconcileIntervalSecs` 3600 in `fleet.json`, `intake_reconcile`, ...) | `forge` | Forge is the work surface per the operator's reading. Every `gh` shell-out is one family, grouped by module per the Curator's instruction. Row 6 only accelerates these loops |
| 18 | `git` over the network | in and out | `git_utils.rs`, `worktree_cli/` (`upstream`, `stale_ref`, `branch_landed`, `default_branch`), `cli/push_lease.rs`, `merge_pr/*`, `fetch_headroom.rs`, `tokens_pool/private_workspace/repository.rs` (credential-free HTTPS clone, URL checked with `reqwest::Url::parse`) | `git` subprocess to the repo remotes | Refreshes refs, pushes work branches, clones the account-private repository | Per feature; `tokens_pool` private-workspace is opt-in | `forge` | Same trust position as row 17 |
| 19 | Forge capability and credential probes | out, response read | `write_scope/probe.rs` (`curl`, `GET /repos/{owner}/{repo}` then `GET /installation/repositories`, cached), `forge_probe.rs` and `cli/forge_probe_cmd.rs` (`curl`, operator-run hosted-qualification harness) | HTTPS to the forge API | Whether a write would be allowed (`write_scope`), qualification receipts (`forge-probe`, read-only unless `--live-write`) | `write_scope` probe cached per repo; `forge-probe` is an explicit subcommand | `forge` | Probes of the forge itself |
| 20 | Model-provider account probe | out, response read | `tokens_pool/check.rs` `probe_account` / `run_check` (`curl`, :344), `token_ranking_refresh.rs` (runs the daemon's own check), `credential_preflight.rs` | HTTPS `POST /v1/messages` (minimal request) to the provider; rate-limit headers matched by suffix (`-5h-utilization`, `-7d-utilization`, `-7d-reset`) | Writes `.loom/tokens/.ranking`, which decides **which account a launch uses** and whether an account is exhausted | Account pool present; refresh cadence in `token_ranking_refresh.rs` | `forge` ‡ | A provider is not the forge. These are observations of a resource limit, comparable to the forge rate-limit breaker, not instructions. Provisional class; see Open question 1 |
| 21 | Egress proxy, provider response path | in (provider response) | `worker_spawn/egress_proxy/server.rs` (`capture_usage_headers` :659, `reset_evidence` :695, `record_usage`, `read_capped`), `egress_proxy/mod.rs` | HTTPS to the provider through `reqwest`, redirects disabled | Records usage and derives exhaustion and reset evidence for the launch's account; a provider error body is buffered (cap 1 MiB) and classified. I traced the recording call sites and not every downstream consumer of the exhaustion evidence | Same gating as row 2 | `forge` ‡ | Same position as row 20 |

## Not an input

Outbound-only traffic, or traffic whose response is read for display or
diagnostics. Each was read and considered; none steers dispatch, admission or
holds.

| Item | Code location | Why it is not an input |
|---|---|---|
| `/ingest` `host_id` echo | `observability/exporter.rs` `check_host_identity` | The only field of the ack still parsed. A mismatch logs one WARN and sets `HostIdStatus`, shown in `loom-daemon status` / `health` (`observability_host_id_mismatch`). It never changes the export result or any dispatch decision. Goes away with the `https` exporter (#11712) |
| OTLP exporter and its responses | `observability/otlp/{mod,transport}.rs` | Response policy keeps counts (`rejectedLogRecords` etc.) for loss accounting; receiver text is never copied. Telemetry delivery only |
| Native `/ingest` batch and `queue.snapshot` | `observability/{exporter,queue_snapshot,sender}.rs` | Outbound telemetry |
| loom-ui alerts | `loom_ui_alerts/` (`post`, `post_blocking`, `outbox`), `fleet_sync/loom_ui.rs`, `safehouse/loom_ui_watch.rs` | `POST {loom-ui}/api/alerts/external`. A 2xx is delivered, other 4xx are dropped and logged, 429/5xx are retried with backoff. The response never reaches a decision. Off with `LOOM_LOOM_UI_ALERTS=0` or without ingest config |
| Safehouse narration (outbound) | `safehouse.rs` `spawn_sink` | Sweep lifecycle sent to the room. Failures degrade to a `warn!`; "Loom never blocks a sweep on safehouse". Removal is #11112; the inbound half is rows 9-10 |
| SigNoz reads | `signoz_read.rs`, `telemetry_replay*.rs`, `cli/telemetry_replay.rs`, `config_resolver.rs` (endpoint resolve only) | `POST /v1/query` to the telemetry reader, driven only by the `telemetry-replay` CLI. No daemon loop calls it |
| Z.ai quota | `observability/ops/quota/zai.rs`, `observability/collector/zai_accounts.rs` | `GET api.z.ai/.../quota/limit`, cached per seat; the result becomes `tokens.snapshot` account rows and pool gauges. Unknown stays unknown, never `0` |
| Anthropic OAuth usage | `script_helpers/usage.rs` `call_usage_api` (`curl`) | Behind `loom-daemon usage` / `check-usage.sh`; the only callers found are that script and docs. No daemon loop consumes it. Cached in `.loom/usage-cache.json` |
| Kimi, Codex, OpenCode, Pi usage | `kimi_usage/`, `codex_usage/`, `opencode_usage/`, `pi_usage/` | No HTTP client or network `Command` in any of them; they read local session files |
| Jev merge-risk score | `jev_merge_risk.rs` `run` (`POST https://api.typesafe.ai/v1/systemone`, `LOOM_JEV_ENDPOINT`) | CLI invoked by Champion after it has decided. Shadow telemetry: prints a number, comments, labels and merges nothing. The old `jev_tier` routing is retired and old journal lines are ignored |
| `loom-api/` analytics server | `loom-api/src/main.rs` (axum `TcpListener` on `127.0.0.1:9999`) | Separate binary, not part of `loom-daemon`. Loopback, GET-only analytics routes; feeds nothing back to the daemon |
| Operator-run fleet CLIs | `fleet/add_worker*.rs`, `fleet/status.rs`, `session_exec/host.rs`, `remote_fleet_exec/` (`ssh`, `curl` inside provisioning scripts) | Operator-initiated outbound actions against fleet hosts |
| `daemon_service.rs`, `fleet/path_bootstrap.rs`, `live_gh_guard.rs` | as named | `which`, PATH construction and a stand-in guard for `gh`; no network |

## Safehouse hosts

Safehouse is rows 9-11 and is `removed`. The per-host inventory of where it is
enabled today is fleet-specific and does not ship in this install-wide doc.
`safehouse.enabled` is also an environment switch (`LOOM_SAFEHOUSE_ENABLED`),
so a fleet config file alone cannot prove which daemons are enabled; the
authoritative per-host answer is `loom-daemon status --json`. Removal (#11112)
makes the question moot, but any host with safehouse enabled has peer claims
(rows 9-10) live today.

## Open questions

1. **Local surfaces and provider responses.** Rows 1-5 (IPC socket, egress
   proxy, relay, `serve`, MCP) are host-local and carry no remote party, and
   rows 20-21 are model-provider observations (rate-limit headers, exhaustion
   evidence). The four classes do not name either. I put the first group under
   `config` and the second under `forge` ‡. If the rule is "no outside service
   changes behavior", rows 20-21 are a real exception and need a ruling:
   keep as a fifth acknowledged class (resource-limit observation), or remove
   by having a host configure fixed account limits in fleet-gitops. Also
   confirm the IPC socket mode: nothing in the code sets one.
2. **Poll gating (row 7).** It is the only "wake" input that can make the daemon
   do less. It is bounded (15 minutes, discovery reads only, restored on any
   non-healthy status) and default off. Is that acceptable inside "wake-only",
   or should the wake-only plane drop `pollGating`?
3. **Code and artifact distribution (rows 15, 16).** Auto-update fetches a
   signed-or-checksummed binary and workspace sync pulls role prompts and
   scripts; neither is configuration. On a fleet host the trigger is operator
   config (`loom_min_version`; a fleet host never chases latest), and
   integrity is a mandatory SHA-256 plus an optional signature and
   source-anchor policy. I classified auto-update `forge` ‡ and workspace sync
   `config`. Does the operator rule cover "the forge publishes the release the
   floor selected"? If not, the alternative is to require
   `SignaturePolicy::require_signature` plus the approved-source anchor
   fleet-wide so a compromised release cannot execute on its own.
4. **Fleet-store content that is not config (row 14).** The captain heartbeat
   is runtime data written by one host and read by others through the store's
   `eta-fit` branch. It changes only which host produces telemetry. Does the
   rule allow it, or should the heartbeat leave the store?
5. **`safehouse.md` and config schema.** `defaults/docs/safehouse.md` still
   documents ChatOps and Concierge as if present. Cleanup is part of #11112's
   remaining slices; recorded here so the audit and the doc set are not read as
   disagreeing.

## Follow-ups per `removed` row

No new issue was filed: every `removed` row is covered by an open issue or has
nothing to remove daemon-side.

| Row | Covered by |
|---|---|
| 8 `/ingest` control fields | Daemon side done (#11104). `https` exporter removal: rjwalters/loom#11712 steps 1-2. Priority controls as fleet config (outside this repo's scope) |
| 9, 10, 11 Safehouse | rjwalters/loom#11112 (safehouse removal, ADR-0025) and #11712 step 3 |
| 12 loom-ui GitHub mirror | Nothing daemon-side; loom-ui removes its route |

Fleet config changes needed (listed in the operator's fleet tracking, not
here): the priority-controls and exporter cleanup, removing host-local
`safehouse` keys and drop-ins once #11112 ships, and, if Open question 3 is
ruled for hardening, a fleet-wide signature policy.

## Related

- rjwalters/loom#11711 (this audit), #11712, #11112, #11104
- [`forge-events.md`](forge-events.md) and ADR-0021 for rows 6-7
- [`lease-record.md`](lease-record.md) and ADR-0025 for the forge lease that replaces rows 9-10
- [`fleet-config-lifecycle.md`](fleet-config-lifecycle.md) for rows 13-15
- [`observability.md`](observability.md) for rows 8 and the telemetry items
