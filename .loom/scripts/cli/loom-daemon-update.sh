#!/usr/bin/env bash
# loom-daemon-update.sh - Self-update the RAW loom-daemon process (Issue #3968)
#
# Closes the "self-update gap" observed during the 2026-07-25/26 canary
# rollout: the daemon's self-repair loop filed AND fixed 16 of its own
# defects, but every merged fix only took effect after an operator manually
# rebuilt the Rust binary, reprovisioned it, and restarted the process. This
# script is the single operator command that does all three, in order,
# preserving the FLAGS-OFF/opt-in autonomy contract across the restart.
#
# Staleness detection strategy (primary, zero-network): compare the git
# commit BAKED INTO the currently-resolved `loom-daemon` binary (embedded at
# build time via build.rs -> LOOM_DAEMON_GIT_COMMIT, surfaced in
# `loom-daemon --version`) against the LOCAL source tree's current HEAD short
# commit. This answers the directly actionable question — "would rebuilding
# right now produce a different binary?" — without touching the network.
#
# Checkout freshness (default, ff-first — #4330): the whole point of running
# this script is to get the daemon onto the LATEST code, so before resolving
# the local HEAD used for the staleness comparison above, this script attempts
# a bounded, best-effort `git fetch` of origin/<default-branch> and, if local
# HEAD is behind, a `git merge --ff-only`. On success the rebuild below builds
# the freshly-synced HEAD. If the ff-merge cannot apply (diverged local
# commits, or a dirty tracked file conflicts with the incoming change) the
# script ABORTS (exit 1) rather than guessing or hard-resetting — a stale
# rebuild silently missing merged commits (the 2026-07-29 incident this issue
# closes) is worse than a loud abort asking the operator to resolve it by
# hand. A fetch failure/timeout (offline, network degraded) is NOT treated as
# "behind" — the script warns and proceeds with local HEAD as-is, so this
# check never makes the script hard-network-dependent. `--allow-stale`
# restores the pre-#4330 build-what's-here behavior (skips the fetch+merge
# entirely) for deliberate use (bisecting, testing a local patch) — see below.
#
# It:
#   - detects whether the resolved binary is stale vs. the local source tree
#     (or, in artifact-fetch mode, vs. the latest GitHub Release — see below),
#   - Artifact-fetch mode (Epic #4990 Phase 3, #5020, ON by default, opt out
#     with --no-fetch): resolves the latest GitHub Release with version >=
#     the installed daemon and, when that release publishes an artifact for
#     this host's platform, downloads + verifies it (checksum unconditional,
#     signature when present) INSTEAD of rebuilding from source — a
#     saturated host with no Rust toolchain converges on a release alone.
#     Resolution failure of any kind (unrecognized platform, no `gh` CLI, no
#     Releases yet, API unreachable, no artifact for this platform) SOFTLY
#     falls back to the rebuild path below (--fetch instead hard-fails).
#   - rebuilds (`cargo build --release`) in loom-daemon/ when stale (or
#     --force) AND no artifact was fetched,
#   - provisions the fresh binary to wherever the resolved binary lives
#     (LOOM_DAEMON_BIN override, else the machine-level ~/.local/bin install
#     via scripts/install/provision-daemon.sh, matching #3922's convention),
#   - reads the flags loom-daemon-start.sh persisted at the last invocation
#     (.loom/.daemon.flags, #3968) and restarts with EXACTLY those flags —
#     never more, never fewer. A daemon that was NOT running is left
#     stopped (this script never widens FLAGS-OFF by starting autonomy that
#     wasn't already running).
#
# Stale-entry-point advisory (#4079 hardening, epic #4081 Phase 4 / #4557):
# on every path — including --check, --dry-run, and an up-to-date no-op — this
# script scans PATH for `loom-*` executables that do NOT resolve to the
# loom-daemon binary it just resolved, and WARNS about each one. This is the
# #4079 failure mode: a long-gone `pip install -e loom-tools` left frozen
# console scripts in ~/.local/bin that kept shadowing the Rust binary's own
# entry points, so agents ran ancient logic while `--version` looked fresh.
# Since #4557 deleted that Python package, nothing regenerates such scripts
# ever again. The warning ALSO fires when PATH holds more than one
# `loom-daemon` (the first shadows the rest). It is advisory only: nothing is
# deleted, PATH is untouched, and the exit code is unaffected. The
# auto-generated `loom-clean`/`loom-recover-orphans`/`loom-claim` shims
# (#4272/#4275) pointing at the resolved binary are never flagged. (`loom-search`
# was allowlisted here as a legitimate non-daemon Python console script from
# #4557 through #4969 — #4970 retired that package too, so a `loom-search` on
# PATH is now exactly the #4079 failure shape and IS flagged, like any other
# stale entry point.) Suppress with LOOM_SKIP_STALE_ENTRY_POINT_CHECK=1.
#
# Launchd-managed daemons (#4042): on Darwin the daemon is commonly launchd-
# managed (default since #3972/#4054), in which case NEITHER .loom/.daemon.pid
# nor .loom/.daemon.flags reliably reflects "is it running" — the pid file goes
# stale after any KeepAlive:SuccessfulExit relaunch, and a hand-bootstrapped
# daemon has no state files at all. This script therefore checks the launchd job
# state (`launchctl print <domain>/<label>`, where <domain> is
# resolve_launchd_domain()'s gui/<uid> ↦ user/<uid> pick — #4130 — mirroring
# loom-daemon-stop.sh) AHEAD of the pid-file tier when resolving whether/how the
# daemon is running.
# When launchd-managed, it restarts via the `loom-daemon restart` primitive
# (#4077 — sends Request::RestartDaemon over the IPC socket; the supervised
# daemon exits 0 and launchd relaunches it onto the fresh binary with the
# plist's persisted ProgramArguments/EnvironmentVariables). .daemon.flags is NOT
# consulted in this mode (the plist's EnvironmentVariables IS the durable flag
# source), and no "restarting FLAGS-OFF" warning fires. If the running (old)
# binary predates #4077 and refuses the request, this script REFUSES LOUDLY
# (exit 6) and prints how to re-render the plist + relaunch under supervision
# (loom-daemon-update.sh --relaunch), rather than reporting a half-update — the
# exact #4011 silent-autonomy-loss class this closes. The old advice to bootstrap
# the EXISTING plist was itself a bug (#4118): it relaunched under the STALE plist
# (no KeepAlive:SuccessfulExit, no LOOM_DAEMON_SUPERVISOR), so every subsequent
# roll hit the same exit 6 forever. It was ALSO documented (here, through
# 2026-08-03) as killing in-flight sweeps via bootout tearing down the job tree
# — that claim is STALE (#5081): since #3800 (2026-07-22) every sweep is spawned
# as its OWN process group (`Command::process_group(0)`), and launchd's bootout
# tears down the job's process group specifically, so a detached sweep is never
# reached by it — it reparents to pid 1 and keeps running. Confirmed both by
# code (dispatch.rs, `#4980`'s persisted pgid relies on this same fact) and by
# observation (three bootout+bootstrap cycles on 2026-08-03, #5081). --relaunch
# is still the recommended path here, but for a DIFFERENT reason: `launchctl
# bootout` is asynchronous, and a hand-run bootstrap immediately after it can
# race the still-in-progress teardown and fail with "Bootstrap failed: 5:
# Input/output error", leaving the daemon down until a retry. --relaunch
# re-renders via loom-daemon-start.sh (installing the supervised keys) while
# preserving the live plist's LOOM_* autonomy env; loom-daemon-start.sh's own
# bootout+bootstrap sequence settles after bootout, retries the bootstrap step
# on that specific race, and verifies the relaunched job's live pid + reported
# environment before reporting success (#5081) — a hand-typed bootout+bootstrap
# gets none of that safety net.
#
# systemd --user-managed daemons (Linux, #4260 sub-issue C): the exact same
# ownership-tiering + supervised-restart contract, ported to the systemd --user
# service loom-daemon-start.sh installs (#4268). A `systemd --user` unit's pid
# also goes stale on every `Restart=on-success` relaunch, so it is checked at the
# SAME tier as launchd, ahead of the pid-file tier. The restart itself is driven
# by the identical `loom-daemon restart` IPC primitive (recognized daemon-side by
# `detect_supervisor()` since #4267/PR #4298 when `LOOM_DAEMON_SUPERVISOR=systemd`
# is present — baked into the rendered unit by loom-daemon-start.sh); a clean
# exit 0 lets systemd's `Restart=on-success` relaunch onto the fresh binary.
# That ack is verified, not trusted (#4950, mirroring #4232's launchd
# verification): the script polls for a NEW, live MainPID within a bounded
# window before reporting success, and — if the unit does not come back on its
# own — self-heals via `systemctl --user reset-failed <unit> && systemctl --user
# start <unit>` before giving up (exit 7). #5119 widened that self-heal: on a
# busy host the daemon's clean exit can leave the unit sitting in
# `deactivating (stop-sigterm)` for the full TimeoutStopSec while systemd reaps
# the sweep/role children still in the service cgroup — far past the pid poll on
# a STALE unit (default 90s). The verifier now WAITS for a transitional stop to
# settle (LOOM_DAEMON_STOP_SETTLE_SECS), then self-heals ANY non-`active` settled
# state (`failed` — the #4950 stop-timeout shape — as well as `inactive`),
# instead of the pre-#5119 behavior of only acting on an already-`failed`
# snapshot and otherwise "refusing to guess" (which left the 2026-08-03
# loom-worker-1 daemon down until a hand `reset-failed && start`). On a
# refused restart (a pre-#4267 binary with no RestartDaemon handler, or a dead
# socket), the script refuses loudly (exit 6) exactly like launchd, and
# --relaunch (or LOOM_DAEMON_UPDATE_RELAUNCH=1) re-renders the unit and forces
# the relaunch: it harvests the live unit's `Environment=` LOOM_*/token lines,
# SIGTERMs the running daemon (so sweep children reparent instead of being torn
# down), then re-invokes loom-daemon-start.sh — which re-renders the unit
# (installing `LOOM_DAEMON_SUPERVISOR=systemd`), reloads, and `enable --now`s it
# onto a now-inactive unit, i.e. a genuine restart. `LOOM_DAEMON_SYSTEMD=0`
# disables ALL systemd interaction symmetrically with loom-daemon-start.sh
# --no-systemd / loom-daemon-stop.sh, so a --no-systemd install is never probed
# via systemctl and follows the PID-file/nohup restart path instead. Darwin
# behavior is entirely unaffected — this tier is inert unless
# `is_linux_systemd()` (lib/systemd-user.sh) resolves true.
#
# Usage:
#   ./.loom/scripts/cli/loom-daemon-update.sh              Detect, rebuild if stale, provision, restart (preserving flags)
#   ./.loom/scripts/cli/loom-daemon-update.sh --check       Detect only; exit 0 (up to date) or 3 (update available); no writes
#   ./.loom/scripts/cli/loom-daemon-update.sh --dry-run     Print the plan without building/provisioning/restarting
#   ./.loom/scripts/cli/loom-daemon-update.sh --force       Rebuild + provision + restart even if already up to date
#   ./.loom/scripts/cli/loom-daemon-update.sh --no-restart  Rebuild + provision only; leave the running daemon untouched
#   ./.loom/scripts/cli/loom-daemon-update.sh --relaunch    Launchd/systemd only: after a refused restart, re-render the plist/unit and relaunch under supervision (SIGTERMs the daemon so sweep children reparent; preserves the live LOOM_* env)
#   ./.loom/scripts/cli/loom-daemon-update.sh --drain        Launchd/systemd only (Issue #5138): restart via the existing `loom-daemon restart --drain` primitive (#4090) instead of an immediate restart — pauses dispatch, waits for every in-flight sweep to finish (so sweep.completed/sweep.outcome telemetry is never lost, #5084), THEN relaunches. On systemd this is now the DEFAULT (see below) because an immediate restart there is actively destructive (#5119), not merely lossy — this flag is for opting IN on launchd, or for being explicit on systemd. A drain that cannot finish within its timeout leaves the CURRENT (pre-update) binary running rather than cancelling sweeps (fail-safe, unchanged from #4090) and this script says so clearly (exit 8). Same as LOOM_DAEMON_UPDATE_DRAIN=1. Combine with the single-invocation roll this issue closes: build + provision + drain-restart in one command, no more manual `--no-restart` then `loom-daemon restart --drain` two-step (that workaround still works unchanged, see --no-restart above).
#   ./.loom/scripts/cli/loom-daemon-update.sh --timeout SECS Passthrough to `loom-daemon restart --drain --timeout SECS` (only meaningful with an active drain, explicit or systemd-default): max seconds to wait for in-flight sweeps before the fail-safe refuses the restart (daemon default: tens of minutes, currently 1800s). Ignored (with no error) when no drain is active.
#   ./.loom/scripts/cli/loom-daemon-update.sh --force-after-timeout  Passthrough to `loom-daemon restart --drain --force-after-timeout` (only meaningful with an active drain): on a drain timeout, cancel the remaining in-flight sweep(s) and restart anyway instead of refusing. Without this, a drain timeout keeps the daemon running its PRE-update binary (the fail-safe) and this script exits 8. Ignored (with no error) when no drain is active.
#   ./.loom/scripts/cli/loom-daemon-update.sh --restart-now   Systemd only (Issue #5138): opt OUT of the systemd drain-by-default behavior above and restart IMMEDIATELY (the pre-#5138 behavior on every supervisor) — for an operator who has confirmed nothing is in flight and wants the roll to finish as fast as possible. Mutually exclusive with --drain (exit 1 if both are given). No effect on launchd (already the default there) or the bare pid-file/nohup path (which never speaks the drain primitive at all).
#   ./.loom/scripts/cli/loom-daemon-update.sh --allow-stale Skip the default ff-first sync with origin/<default-branch> and build the current (possibly stale) checkout as-is (#4330) — for deliberate use: bisecting, testing a local patch
#   ./.loom/scripts/cli/loom-daemon-update.sh --auto-resolve-safe-abort  When the ff-only sync would otherwise hard-abort (#4330), auto-perform the fix IF the blocking state classifies as safe (#4951): content-identical diverged commits with an otherwise-CLEAN working tree (`git reset --hard origin/<default-branch>`), or dirty tracked files that are ALL Loom-managed installed copies (`git checkout --` them + re-run resync-installed.sh). Any other cause (genuine content divergence, any unmanaged dirty file, or a dirty tracked file co-existing with the content-identical divergence) still hard-aborts unchanged — this flag never widens what's classified as safe, only whether the safe cases are printed (default) or performed
#   ./.loom/scripts/cli/loom-daemon-update.sh --fetch        Artifact-fetch mode (Epic #4990 Phase 3, #5020): REQUIRE a verified GitHub Release artifact for this host's platform (resolve latest Release >= installed version, download, verify checksum unconditionally + signature when present, provision) instead of `cargo build --release`; hard-fails (exit 1) rather than silently falling back to a source build when no matching artifact resolves. Same as LOOM_DAEMON_UPDATE_FETCH=1.
#   ./.loom/scripts/cli/loom-daemon-update.sh --no-fetch      Disable artifact-fetch mode entirely; always use the local source-build path (pre-#5020 behavior). Same as LOOM_DAEMON_UPDATE_FETCH=0.
#   ./.loom/scripts/cli/loom-daemon-update.sh --prune-stale-entry-points  Remove exactly the PATH entries the stale-entry-point advisory below (#4079/#4557) classifies as a "Python console script (stale pip/pipx editable install)" — a frozen console script left behind by the retired pip/pipx package (epic #4081 Phase 4 / #4557) that never resolves to the current loom-daemon binary and so is never touched by an update again. Conservative by construction: it reuses the SAME classification the advisory already computes, so `loom-daemon` itself and the auto-generated bash-wrapper shims (`loom-clean`/`loom-recover-orphans`/`loom-claim`, #4272/#4275 — including a STALE shim whose target moved) are never candidates, only ever reported. Standalone: performs the prune, reports what it removed, and exits — no build/provision/restart. Idempotent (a second run finds nothing to remove) and reports "nothing to prune" when the PATH is already clean. Honors LOOM_SKIP_STALE_ENTRY_POINT_CHECK=1 (a no-op, matching the check it would otherwise act on) (#5139).
#   ./.loom/scripts/cli/loom-daemon-update.sh --help
#
# Environment:
#   LOOM_DAEMON_UPDATE_FETCH  Artifact-fetch precedence (Epic #4990 Phase 3,
#                          #5020): 1/true/yes forces it (same as --fetch,
#                          hard-fails without a matching artifact); 0/false/no
#                          disables it (same as --no-fetch); unset (default)
#                          is "auto" — prefer a verified artifact when one
#                          resolves, else soft-fall-back to the source build.
#   LOOM_DAEMON_UPDATE_GH_REPO  Override the "owner/repo" slug used to
#                          resolve GitHub Releases (else parsed from the
#                          `origin` git remote).
#   LOOM_DAEMON_UPDATE_TARGET  Override the release target triple this host
#                          resolves to (else auto-detected from `uname -s -m`,
#                          e.g. aarch64-apple-darwin) — mainly for tests.
#   LOOM_DAEMON_UPDATE_COSIGN_PUBKEY  Path to the cosign public key used to
#                          verify a Linux release's detached `.sig` (else a
#                          conventional checked-in `.loom/cosign.pub` /
#                          `defaults/cosign.pub` if present). Consulted ONLY
#                          for a `.sig` with no sibling `.pem` certificate:
#                          releases signed keylessly (the default since
#                          #5054) carry a certificate and are verified against
#                          the signer identity below, with no key material
#                          distributed on either side.
#   LOOM_DAEMON_UPDATE_COSIGN_IDENTITY  Pin one EXACT expected keyless signer
#                          identity (cosign --certificate-identity). Default:
#                          unset — the expected identity is DERIVED from the
#                          release being fetched, as the regexp
#                          ^https://github\.com/<slug>/\.github/workflows/[^@]+@refs/tags/<tag>$
#                          ("a workflow in the release repo, run at exactly
#                          this release's tag").
#   LOOM_DAEMON_UPDATE_COSIGN_OIDC_ISSUER  Expected keyless certificate issuer
#                          (default https://token.actions.githubusercontent.com,
#                          i.e. GitHub Actions' OIDC provider).
#   LOOM_PID_FILE         #6386: the pid file consulted for liveness detection,
#                          TIER 1 -- ahead of the $PWD/machine-derived
#                          "<state home>/.daemon.pid". Same precedence
#                          loom-daemon-stop.sh (which this script delegates the
#                          restart's stop half to), loom-daemon-watchdog.sh and
#                          the daemon itself use, so the restart can never plan
#                          against a different file than the stop acts on.
#   LOOM_DAEMON_BIN       Path to the loom-daemon binary (else auto-detected,
#                          same resolution as loom-daemon-start.sh). When set,
#                          the fresh binary is provisioned directly to this
#                          exact path instead of the machine-level default.
#   LOOM_DAEMON_BIN_DIR   Machine-level install dir (default ~/.local/bin),
#                          forwarded to provision-daemon.sh.
#   LOOM_SKIP_STALE_ENTRY_POINT_CHECK  1/true/yes suppresses the advisory
#                          stale-`loom-*`-entry-point warning described below.
#   LOOM_SKIP_IDLE_SHUTDOWN_NOTICE  1/true/yes suppresses the advisory
#                          post-update idle-shutdown cron-guard notice (#4697):
#                          "this host will power itself off after N idle
#                          minutes" when a `fleet add-worker
#                          --idle-shutdown-minutes` guard is installed. Silent
#                          (no notice at all) when no such guard exists.
#   LOOM_DAEMON_LAUNCHD    macOS only: 0/false/no disables ALL launchd interaction
#                          (ownership detection + launchd restart), symmetric with
#                          loom-daemon-start.sh / loom-daemon-stop.sh. A daemon
#                          started with --no-launchd / LOOM_DAEMON_LAUNCHD=0 gets
#                          an update that never reads the machine-global launchd
#                          domain and follows the PID-file/nohup restart path.
#   LOOM_LAUNCHD_LABEL     macOS only: the LaunchAgent label to inspect/restart
#                          (default com.rjwalters.loom-daemon).
#   LOOM_LAUNCHD_DOMAIN    macOS only: pin the launchd domain (gui/<uid> or
#                          user/<uid>); else auto-resolved gui→user (#4130),
#                          matching loom-daemon-start.sh / -stop.sh.
#   LOOM_DAEMON_SYSTEMD    Linux only: 0/false/no disables ALL systemd interaction
#                          (ownership detection + systemd restart), symmetric with
#                          loom-daemon-start.sh --no-systemd / loom-daemon-stop.sh
#                          (#4268). A daemon started with --no-systemd /
#                          LOOM_DAEMON_SYSTEMD=0 gets an update that never invokes
#                          systemctl and follows the PID-file/nohup restart path.
#   LOOM_SYSTEMD_UNIT      Linux only: the systemd --user unit to inspect/restart
#                          (default loom-daemon.service); must match the start's.
#   LOOM_DAEMON_UPDATE_RELAUNCH  Launchd/systemd only: 1/true/yes is equivalent to
#                          passing --relaunch (opt in to the re-render + relaunch
#                          on a refused restart).
#   LOOM_DAEMON_UPDATE_DRAIN  Launchd/systemd only (Issue #5138): 1/true/yes is
#                          equivalent to passing --drain. Has no effect on
#                          systemd's own default (already drained unless
#                          --restart-now/LOOM_DAEMON_UPDATE_RESTART_NOW is given) —
#                          it exists mainly to opt IN on launchd, or in a script
#                          that wants to be explicit.
#   LOOM_DAEMON_UPDATE_RESTART_NOW  Systemd only (Issue #5138): 1/true/yes is
#                          equivalent to passing --restart-now (opt OUT of the
#                          systemd drain-by-default and restart immediately).
#   LOOM_DAEMON_DRAIN_POLL_SECS  Seconds this script polls for the supervisor to
#                          relaunch a NEW, live pid after a drain-mode restart is
#                          accepted (#5138), before treating it as either the
#                          fail-safe (no --force-after-timeout: leaves the
#                          pre-update binary running, exit 8) or an unconfirmed
#                          failure (--force-after-timeout: falls through to the
#                          same self-heal fallback a plain restart uses).
#                          Default: the drain's own --timeout (or the daemon's
#                          built-in default, 1800s) plus a 60s grace buffer —
#                          long enough that a real drain is never mistaken for a
#                          hang. If LOOM_DAEMON_RESTART_POLL_SECS (below) is ALSO
#                          set, it wins over this default (drain or not) — an
#                          explicit poll-window override always takes
#                          precedence over either default.
#   LOOM_MACHINE_CHECKOUT  Machine mode (Epic #3835 Phase 3b, #4229): set by the
#                          `scripts/loom` dispatcher to the resolved
#                          ~/.local/share/loom checkout before it execs this
#                          script. When set, THIS is the source tree rebuilt
#                          from (overriding the $PWD-based find_repo_root()
#                          below), so `loom update` works from any directory --
#                          a consumer repo, or no repo at all -- instead of
#                          requiring $PWD to already be inside a Loom source
#                          checkout. Direct invocation of this script (no
#                          dispatcher -- the existing dev workflow) never sets
#                          it and is unaffected.
#   LOOM_DAEMON_RESTART_POLL_SECS  macOS/launchd (#4232) AND Linux/systemd
#                          (#4950): seconds to poll for a NEW, live pid after a
#                          `restart` ack before falling back to the
#                          supervisor's self-heal (`launchctl kickstart` on
#                          launchd; `systemctl --user reset-failed && start` on
#                          systemd, and only when the unit is `failed`)
#                          (default 30).
#   LOOM_DAEMON_RESTART_POLL_INTERVAL  Poll interval in seconds between pid
#                          checks (default 1; may be fractional, e.g. 0.5).
#   LOOM_DAEMON_RESTART_KICKSTART_POLL_SECS  macOS/launchd (#4232) AND
#                          Linux/systemd (#4950): seconds to re-poll for a new,
#                          live pid after the self-heal fallback before giving
#                          up (default 15).
#   LOOM_DAEMON_STOP_SETTLE_SECS  Linux/systemd (#5119): after the pid poll
#                          expires, seconds to wait for a still-transitioning
#                          unit (ActiveState=deactivating/activating) to SETTLE
#                          into a terminal state before running the reset-failed+
#                          start self-heal. Sized to exceed systemd's default 90s
#                          TimeoutStopSec so a stale unit's slow SIGTERM→SIGKILL
#                          cgroup teardown is waited out rather than mistaken for
#                          "refusing to guess" (default 100).
#
# Exit codes:
#   0  up to date (no-op) OR rebuild+provision+restart succeeded
#   1  usage error / not a source checkout / build or provision failure /
#      the default ff-first sync with origin/<default-branch> could not apply
#      (diverged local commits, a dirty tracked file conflicting with the
#      incoming change, or HEAD is not on <default-branch>) — the script
#      NEVER guesses or hard-resets; resolve manually or pass --allow-stale
#      (#4330). Two of the ff-abort causes classify as safely resolvable
#      (#4951): content-identical diverged commits with an otherwise-CLEAN
#      working tree, or dirty tracked files that are ALL Loom-managed
#      installed copies — by default these still
#      exit 1 but the abort message names the exact safe command; pass
#      --auto-resolve-safe-abort to have the script perform it instead (exit
#      0 on success). Any other cause still hard-aborts unchanged. Also used
#      by artifact-fetch mode (#5020) for: a checksum mismatch on a
#      downloaded artifact (unconditional — leaves the running daemon
#      untouched), a signature-verification failure on a PRESENT signature
#      (distinct from "unsigned", which is not an error), and --fetch given
#      with no matching release artifact resolvable (refuses to silently
#      fall back to a source build). Also used by --prune-stale-entry-points
#      (#5139) if any candidate path failed to `rm` (permissions, a race) —
#      the paths that DID remove successfully are still reported individually
#      above the error.
#   3  (--check only) update available
#   4  build verification FAILED: the freshly-built binary's embedded commit
#      does not match the source HEAD it was built from. This is a BUILD-SYSTEM
#      defect (a stale baked-in commit — e.g. a build.rs watch-set bug), NOT a
#      compile failure, and retrying cannot fix it; the script refuses to
#      provision the mis-stamped binary (#4053).
#   5  post-provision verification FAILED: the destination binary after a
#      claimed-successful provision is not the expected build (a silent no-op
#      roll — "reports success while shipping nothing"). Distinct from both a
#      compile failure and a provisioning soft-failure (#4053).
#   6  supervised restart FAILED: the daemon is launchd- or systemd-managed but
#      the running (old) binary refused the `loom-daemon restart` IPC request (a
#      pre-#4077/#4267 binary with no RestartDaemon handler, or a dead socket).
#      The fresh binary IS provisioned but the OLD one is still running; the
#      script refuses to report success. Without --relaunch it prints how to
#      re-render the plist/unit and relaunch under supervision, then exits 6;
#      with --relaunch (or LOOM_DAEMON_UPDATE_RELAUNCH=1) it performs that
#      re-render+relaunch itself, propagating loom-daemon-start.sh's exit code
#      (#4042, #4118, #4260 sub-issue C).
#   7  restart ACK'd but never took effect: the running (old) binary accepted
#      the `restart` IPC request (exit 0), but the supervisor never relaunched
#      the job/unit onto a NEW, live pid within the poll window, AND the
#      self-heal fallback also failed to bring it up within its own poll
#      window. On launchd (#4232) the fallback is a plain `launchctl
#      kickstart` (never -k). On systemd (#4950/#5119) the fallback is
#      `systemctl --user reset-failed <unit> && systemctl --user start <unit>`,
#      tried whenever the unit is NOT `active` — the confirmed-`failed`
#      `Result=timeout` stop escalation #4950 closes, AND (#5119) a unit still
#      stuck `deactivating`/`inactive` when the poll expired: after the poll the
#      script now WAITS up to LOOM_DAEMON_STOP_SETTLE_SECS for a transitional
#      unit's stop to complete, then self-heals the settled non-`active` state
#      rather than "refusing to guess" and leaving the daemon down (systemd never
#      auto-relaunches a failed/stopped unit even under `Restart=on-success`).
#      Only a genuinely `active` unit on an unobserved pid is left untouched. The
#      fresh binary IS provisioned, but the daemon's live status
#      is NOT confirmed — this is the "restart scheduled but the supervisor
#      silently never relaunched" outage class these issues close; the script
#      refuses to report success on the ack alone. A drain-mode restart
#      (--drain, or the systemd default, #5138) reaches this code ONLY when
#      --force-after-timeout was also given and the forced restart still never
#      took effect — see exit 8 for the (expected, not a failure) case where a
#      drain timed out WITHOUT --force-after-timeout.
#   8  drain fail-safe preserved (Issue #5138, launchd/systemd only): a
#      drain-mode restart (--drain, or the systemd default) timed out WITHOUT
#      --force-after-timeout, and the daemon's own fail-safe (#4090) held:
#      dispatch resumed, no in-flight sweep was cancelled or killed, and
#      loom-daemon is STILL RUNNING its PRE-update binary (pid unchanged from
#      before the restart request). This is NOT a failure — it is the drain
#      primitive refusing to choose between "cancel work" and "restart" on the
#      operator's behalf, exactly as designed. The freshly-built/provisioned
#      binary IS staged at the resolved destination; it just was not activated
#      this run. On a #6007+ daemon the roll is RETAINED (dispatch stays paused
#      and the restart re-arms itself when in-flight reaches zero), so this exit
#      means "not yet", not "not ever": the script says so and there is nothing
#      to re-run. Against an older daemon (or when the status probe cannot
#      confirm the pending roll) the historical advice still applies — re-run
#      once the in-flight sweep(s) finish, or re-run with
#      --force-after-timeout to force the roll through immediately.
#
# See also: loom-daemon-start.sh (writes .loom/.daemon.flags), loom-daemon-stop.sh
# (SIGTERM -> grace -> SIGKILL; in-flight sweeps survive by design — this
# script relies on that: stopping+restarting the dispatcher never kills
# dispatched work), scripts/install/provision-daemon.sh (machine-level
# provisioning, #3922).

set -uo pipefail

# ---------- output helpers ----------
if [[ -t 1 ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; NC=''
fi
err()  { echo -e "${RED}$*${NC}" >&2; }
warn() { echo -e "${YELLOW}$*${NC}" >&2; }
ok()   { echo -e "${GREEN}$*${NC}"; }

# _read_help_banner -- one awk pass over "$0" printing the leading comment
# banner (line 2 through the last comment line before `set -uo pipefail`),
# stripping the leading "# ". Split out of show_help() below so it can be
# invoked more than once for the torn-read stability check.
_read_help_banner() {
    awk 'NR>=2 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0" 2>/dev/null
}

show_help() {
    # Reads "$0" at runtime -- fragile against a same-path truncate+rewrite
    # of THIS exact file landing while the awk pass above is reading it (the
    # shape `git checkout`/`cp` writes use -- open+truncate+write in place,
    # not an atomic rename-into-place). That is not hypothetical: a shared
    # self-hosted-runner checkout, a resync step, or an operator's own
    # `git merge --ff-only` (this script's ff-sync step, #4330) landing on
    # this file mid-read can all do it. There is no I/O error to catch --
    # just a torn, incomplete banner missing whatever lines the writer had
    # not reached yet. Confirmed reproducible locally: concurrently
    # overwriting this file with itself during a --help loop took the
    # failure rate from 0% to 100% (#7201).
    #
    # Hardened with a cheap, content-agnostic stability check instead of a
    # hardcoded sentinel line (robust to future banner edits): read twice
    # back-to-back and require an identical, non-empty result before
    # trusting it -- a torn read from a write landing mid-pass is extremely
    # unlikely to reproduce byte-for-byte on the very next pass a moment
    # later. Bounded retries with a short backoff so a genuinely corrupted
    # file (not a transient race) still terminates instead of looping
    # forever -- it prints whatever the last pass read rather than hanging.
    local banner banner2 _attempt
    banner="$(_read_help_banner)"
    for _attempt in 1 2 3 4 5; do
        banner2="$(_read_help_banner)"
        [[ -n "$banner2" && "$banner" == "$banner2" ]] && { banner="$banner2"; break; }
        banner="$banner2"
        sleep 0.05
    done
    printf '%s\n' "$banner"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# harvest_plist_env / harvest_unit_env (used by perform_relaunch /
# perform_systemd_relaunch below) live in lib/daemon-env-harvest.sh (#4581) so
# scripts/loom's loom_cmd_restart() bare-exec fallback can apply the identical
# harvest-and-preserve pattern from a single source instead of a second copy.
_LOOM_ENV_HARVEST_LIB="$SCRIPT_DIR/../lib/daemon-env-harvest.sh"
if [[ -r "$_LOOM_ENV_HARVEST_LIB" ]]; then
    # shellcheck source=/dev/null
    source "$_LOOM_ENV_HARVEST_LIB"
else
    err "daemon-env-harvest.sh not found at $_LOOM_ENV_HARVEST_LIB — this checkout is missing an expected lib file."
    exit 1
fi

# ---------- repo root ----------
# Walk up from $1 (default $PWD) to the nearest ancestor that is a Loom
# repository root: a directory holding BOTH a `.git` entry and a `.loom/`
# directory.
#
# Requiring `.git` alongside `.loom/` is load-bearing (#5140). This walk used
# to accept any ancestor with a `.loom/` directory, and every fleet host that
# has run `loom-daemon tokens bootstrap` keeps machine-level daemon state in
# `~/.loom` — so invoking this script from $HOME matched $HOME on the very
# first iteration, set REPO_ROOT=$HOME and then refused with the misleading
# "No loom-daemon/Cargo.toml found at $HOME/loom-daemon". `.git` alone is any
# git checkout; `.loom/` alone is machine state; only the pair is a Loom repo.
# Mirrors loom-daemon's own `crate::repo_root::find_repo_root`.
find_repo_root() {
    local dir="${1:-$PWD}"
    dir="$(cd "$dir" 2>/dev/null && pwd)" || { echo ""; return 0; }
    while [[ -n "$dir" && "$dir" != "/" ]]; do
        if [[ -d "$dir/.git" && -d "$dir/.loom" ]]; then echo "$dir"; return 0; fi
        if [[ -f "$dir/.git" ]]; then
            local gitdir main_repo
            gitdir=$(sed 's/^gitdir: //' "$dir/.git")
            main_repo=$(dirname "$(dirname "$(dirname "$gitdir")")")
            if [[ -d "$main_repo/.loom" ]]; then echo "$main_repo"; return 0; fi
        fi
        dir="$(dirname "$dir")"
    done
    echo ""
}

# Whether $1 is a Loom SOURCE checkout (has the crate this script rebuilds).
is_loom_source_checkout() {
    [[ -n "${1:-}" && -f "$1/loom-daemon/Cargo.toml" ]]
}

# ---------- locate the daemon binary ----------
# Shared with loom-daemon-start.sh / loom-daemon-watchdog.sh / loom-status.sh
# / `.loom/bin/loom health` via lib/locate-daemon-bin.sh (#4875) — includes
# the machine-level $LOOM_DAEMON_BIN_DIR (default ~/.local/bin) fallback,
# reusing the SAME variable this script's own --provision path already
# writes to (see DEST_DIR below), so discovery and provisioning can never
# point at different directories.
_LOOM_LOCATE_BIN_LIB="$SCRIPT_DIR/../lib/locate-daemon-bin.sh"
if [[ -r "$_LOOM_LOCATE_BIN_LIB" ]]; then
    # shellcheck source=../lib/locate-daemon-bin.sh
    source "$_LOOM_LOCATE_BIN_LIB"
else
    err "locate-daemon-bin.sh not found at $_LOOM_LOCATE_BIN_LIB — this checkout is missing an expected lib file."
    exit 1
fi
locate_daemon_bin() { loom_locate_daemon_bin "$1"; }

# Extract the short commit from `loom-daemon --version` output, e.g.
# "loom-daemon 0.15.0 (commit ab12cd3, built 2026-07-26T12:00:00Z)" -> ab12cd3
extract_commit() {
    echo "$1" | grep -oE 'commit [0-9a-f]+' | head -n1 | awk '{print $2}'
}

# Extract the semver from `loom-daemon --version` output, e.g.
# "loom-daemon 0.15.0 (commit ab12cd3, ...)" -> 0.15.0
extract_version() {
    echo "$1" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1
}

# =====================================================================
# Artifact-fetch mode (Epic #4990 Phase 3, #5020)
# =====================================================================
#
# Phase 1 (#5003) and Phase 2 (#5011/#5018) publish, on every GitHub Release,
# `loom-daemon-<target>` + `<...>.sha256` for every fleet target triple
# (`aarch64-apple-darwin`, `x86_64-unknown-linux-gnu`,
# `aarch64-unknown-linux-gnu`), plus an OPTIONAL Linux-only `<...>.sig`
# (detached cosign signature) when a cosign key secret was configured for
# that release. macOS artifacts are signed IN PLACE (embedded Developer ID
# signature, no separate asset) when the signing secrets were configured;
# there is no way to tell from the filename alone whether a given release's
# macOS asset is signed.
#
# This section resolves the latest Release with version >= the currently
# installed daemon, downloads the artifact for the host's own platform +
# its checksum (and signature, when present), and verifies both:
#   - checksum: UNCONDITIONAL. A mismatch hard-aborts the whole update
#     (exit 1) and leaves the running daemon untouched -- this is a
#     tamper/corruption signal, never a soft-fallback condition.
#   - signature: verified ONLY when present. macOS: `codesign --verify
#     --strict` against the downloaded binary (distinguishes "not signed"
#     -- expected, soft-skip -- from "signed but verification failed" --
#     abort). Linux: detached `.sig` via `cosign verify-blob` — KEYLESS
#     (Sigstore/OIDC) whenever the release also publishes the sibling
#     `<...>.pem` signing certificate, which needs no distributed key
#     material and therefore verifies for real on a stock install (#5054);
#     otherwise the pre-#5054 KEY mode, which still needs an operator-
#     provided public key and loud-skips without one. A missing `cosign`
#     binary is likewise a LOUD skip (never a block) per the epic's design
#     principle 2. Absence of a signature never blocks the update.
#
# Any OTHER resolution failure (unrecognized host platform, no `gh` CLI, no
# Releases yet, GitHub API unreachable/rate-limited, no artifact published
# for this platform) is a SOFT fallback to the existing local
# `cargo build --release` path below -- never a hard error -- unless the
# operator forced `--fetch` (see FETCH_MODE handling near the arg parser).

# detect_target_triple -- echo this host's release target triple, or "" for
# an unrecognized platform (e.g. x86_64 macOS, which the release matrix does
# not build for as of Phase 1). Overridable via LOOM_DAEMON_UPDATE_TARGET
# (tests, or a host whose uname doesn't match the expected values).
detect_target_triple() {
    local os arch
    os="$(uname -s 2>/dev/null || echo unknown)"
    arch="$(uname -m 2>/dev/null || echo unknown)"
    case "$os" in
        Darwin)
            case "$arch" in
                arm64|aarch64) echo "aarch64-apple-darwin" ;;
                *) echo "" ;;
            esac
            ;;
        Linux)
            case "$arch" in
                aarch64|arm64) echo "aarch64-unknown-linux-gnu" ;;
                x86_64|amd64)  echo "x86_64-unknown-linux-gnu" ;;
                *) echo "" ;;
            esac
            ;;
        *) echo "" ;;
    esac
}

# resolve_gh_repo_slug -- echo "owner/repo" parsed from the `origin` remote
# (git@github.com:, https://github.com/, and ssh://git@github.com/ forms),
# or "" when it cannot be resolved. Overridable via
# LOOM_DAEMON_UPDATE_GH_REPO.
resolve_gh_repo_slug() {
    local remote_url slug
    remote_url="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
    [[ -z "$remote_url" ]] && { echo ""; return 0; }
    case "$remote_url" in
        git@github.com:*)
            slug="${remote_url#git@github.com:}"
            echo "${slug%.git}"
            ;;
        https://github.com/*|http://github.com/*)
            echo "$remote_url" | sed -E 's#^https?://github\.com/##; s/\.git$//'
            ;;
        ssh://git@github.com/*)
            echo "$remote_url" | sed -E 's#^ssh://git@github\.com/##; s/\.git$//'
            ;;
        *) echo "" ;;
    esac
}

# semver_compare <a> <b> -- echoes -1, 0, or 1 for a<b, a==b, a>b. Compares
# up to 3 dot-separated numeric components (non-numeric suffixes are
# stripped defensively); missing components default to 0.
semver_compare() {
    local v1="${1:-0.0.0}" v2="${2:-0.0.0}"
    local oldifs="$IFS"
    IFS='.'
    # shellcheck disable=SC2206
    local -a a=($v1) b=($v2)
    IFS="$oldifs"
    local i ai bi
    for i in 0 1 2; do
        ai="${a[i]:-0}"; ai="${ai//[!0-9]/}"; ai="${ai:-0}"
        bi="${b[i]:-0}"; bi="${bi//[!0-9]/}"; bi="${bi:-0}"
        if (( 10#$ai < 10#$bi )); then echo -1; return 0; fi
        if (( 10#$ai > 10#$bi )); then echo 1; return 0; fi
    done
    echo 0
}

# fetch_resolve_latest -- read-only resolution (no downloads): resolve the
# latest GitHub Release + confirm it has an artifact for this host's target.
# Sets FETCH_REPO_SLUG, FETCH_TARGET, FETCH_LATEST_TAG, FETCH_LATEST_VERSION,
# FETCH_RESOLVE_OK, and (on failure) FETCH_RESOLVE_REASON. Returns 0/1
# matching FETCH_RESOLVE_OK so callers can `if fetch_resolve_latest; then`.
FETCH_REPO_SLUG=""
FETCH_TARGET=""
FETCH_LATEST_TAG=""
FETCH_LATEST_VERSION=""
# shellcheck disable=SC2034  # set for API completeness; callers use the function's own return code instead
FETCH_RESOLVE_OK=false
FETCH_RESOLVE_REASON=""
fetch_resolve_latest() {
    FETCH_RESOLVE_OK=false
    FETCH_RESOLVE_REASON=""

    FETCH_TARGET="${LOOM_DAEMON_UPDATE_TARGET:-$(detect_target_triple)}"
    if [[ -z "$FETCH_TARGET" ]]; then
        FETCH_RESOLVE_REASON="unrecognized host platform ($(uname -s 2>/dev/null || echo '?')/$(uname -m 2>/dev/null || echo '?')) -- no release target-triple mapping"
        return 1
    fi

    if ! command -v gh >/dev/null 2>&1; then
        FETCH_RESOLVE_REASON="'gh' CLI not found on PATH"
        return 1
    fi

    FETCH_REPO_SLUG="${LOOM_DAEMON_UPDATE_GH_REPO:-$(resolve_gh_repo_slug)}"
    if [[ -z "$FETCH_REPO_SLUG" ]]; then
        FETCH_RESOLVE_REASON="could not resolve owner/repo from git remote 'origin' (set LOOM_DAEMON_UPDATE_GH_REPO to override)"
        return 1
    fi

    local tag
    if ! tag=$(gh release view --json tagName -R "$FETCH_REPO_SLUG" --jq '.tagName' 2>/dev/null) || [[ -z "$tag" ]]; then
        FETCH_RESOLVE_REASON="'gh release view' found no latest release for $FETCH_REPO_SLUG (no Releases yet, an unreachable/rate-limited API, or an auth failure)"
        return 1
    fi
    FETCH_LATEST_TAG="$tag"
    FETCH_LATEST_VERSION="$(extract_version "$tag")"
    if [[ -z "$FETCH_LATEST_VERSION" ]]; then
        FETCH_RESOLVE_REASON="could not parse a semver version out of release tag '$tag'"
        return 1
    fi

    local assets bin_name sha_name
    assets="$(gh release view --json assets -R "$FETCH_REPO_SLUG" --jq '.assets[].name' 2>/dev/null || true)"
    bin_name="loom-daemon-${FETCH_TARGET}"
    sha_name="${bin_name}.sha256"
    if ! grep -qxF "$bin_name" <<<"$assets" || ! grep -qxF "$sha_name" <<<"$assets"; then
        FETCH_RESOLVE_REASON="release $tag has no artifact for target $FETCH_TARGET (checked for $bin_name + $sha_name)"
        return 1
    fi

    # shellcheck disable=SC2034  # set for API completeness; callers use the function's own return code instead
    FETCH_RESOLVE_OK=true
    return 0
}

# resolve_cosign_pubkey -- echo a resolvable cosign public key path, or "".
# KEY mode only (a `.sig` published without a sibling `.pem` certificate):
# LOOM_DAEMON_UPDATE_COSIGN_PUBKEY (env) first, else a conventional checked-in
# path. No public key is committed to this repo, and #5054 deliberately did
# NOT add one -- see resolve_cosign_identity_regexp for why keyless is the
# default trust root instead. An empty result means "signature present but
# unverifiable" -- a loud skip, never a block (see verify_artifact_signature).
resolve_cosign_pubkey() {
    if [[ -n "${LOOM_DAEMON_UPDATE_COSIGN_PUBKEY:-}" && -r "${LOOM_DAEMON_UPDATE_COSIGN_PUBKEY}" ]]; then
        echo "$LOOM_DAEMON_UPDATE_COSIGN_PUBKEY"
        return 0
    fi
    local candidate
    for candidate in "$REPO_ROOT/.loom/cosign.pub" "$REPO_ROOT/defaults/cosign.pub"; do
        [[ -r "$candidate" ]] && { echo "$candidate"; return 0; }
    done
    echo ""
}

# _regex_escape <literal> -- escape POSIX ERE metacharacters so a repo slug or
# release tag can be embedded literally inside the identity regexp below
# (`.` in `github.com` / `v0.17.0` is the one that actually matters, but the
# whole metacharacter class is escaped so no future tag shape can widen the
# expected identity).
_regex_escape() {
    printf '%s' "$1" | sed 's/[][\.^$*+?(){}|\\]/\\&/g'
}

# resolve_cosign_identity_regexp -- echo the expected KEYLESS signer identity
# (a POSIX ERE for cosign's --certificate-identity-regexp), or "" when it
# cannot be derived.
#
# WHY KEYLESS IS THE DEFAULT (#5054, the decision this function encodes):
# Phase 2 (#5011) signed Linux artifacts with a cosign PRIVATE KEY held in an
# Actions secret, and Phase 3 (#5020) verified them against a public key that
# was never distributed -- so every real host loud-skipped. Distributing that
# public key (committing `defaults/cosign.pub`) would fix the skip but pins the
# whole fleet to one keypair: rotating it silently breaks verification on every
# host still carrying the old key, and the key must be provisioned as a secret
# before ANY release can be signed at all. Keyless Sigstore signing has neither
# problem -- the signer proves its identity with the GitHub Actions OIDC token
# the workflow already has, so signing needs NO secret to be provisioned and
# verification needs NO key material to be distributed. The trust root becomes
# an assertion about *who signed*, which is exactly what we care about:
#
#   "a workflow in the SAME repo this artifact was downloaded from, running at
#    EXACTLY this release's tag, with a certificate issued by GitHub Actions"
#
# The workflow FILE is intentionally not pinned (any `[^@]+` under
# `.github/workflows/`): pinning it would turn a future rename of
# `release.yml` into a fleet-wide hard-abort, while adding nothing -- anything
# able to run a workflow in this repo at this tag can already publish the
# release assets themselves. Operators who want the stricter form set
# LOOM_DAEMON_UPDATE_COSIGN_IDENTITY to the exact identity.
resolve_cosign_identity_regexp() {
    local slug="${FETCH_REPO_SLUG:-}" tag="${ARTIFACT_TAG:-}"
    [[ -n "$slug" && -n "$tag" ]] || { echo ""; return 0; }
    printf '^https://github\\.com/%s/\\.github/workflows/[^@]+@refs/tags/%s$' \
        "$(_regex_escape "$slug")" "$(_regex_escape "$tag")"
}

# resolve_cosign_oidc_issuer -- echo the expected keyless certificate issuer.
# GitHub Actions' OIDC provider by default; overridable for a self-hosted
# forge or a differently-issued token.
resolve_cosign_oidc_issuer() {
    echo "${LOOM_DAEMON_UPDATE_COSIGN_OIDC_ISSUER:-https://token.actions.githubusercontent.com}"
}

# verify_artifact_checksum <bin_path> <sha256_path> -- unconditional
# checksum verification. The `.sha256` format is `shasum -a 256` /
# `sha256sum` output (`<hex>  <filename>`), so the hex digest is always the
# first field.
verify_artifact_checksum() {
    local bin_path="$1" sha_path="$2" expected actual
    expected="$(awk 'NR==1{print $1}' "$sha_path" 2>/dev/null)"
    [[ -n "$expected" ]] || return 1
    if command -v shasum >/dev/null 2>&1; then
        actual="$(shasum -a 256 "$bin_path" | awk '{print $1}')"
    elif command -v sha256sum >/dev/null 2>&1; then
        actual="$(sha256sum "$bin_path" | awk '{print $1}')"
    else
        err "Neither 'shasum' nor 'sha256sum' is available -- cannot verify the artifact checksum."
        return 1
    fi
    [[ "$expected" == "$actual" ]]
}

# verify_artifact_signature <target> <bin_path> <sig_path-or-empty>
#                           [cert_path-or-empty] --
# signature verification, present-only (absence always passes). Distinguishes
# "not signed" (expected/allowed, soft-skip) from "signed but verification
# failed" (tamper evidence, hard-fail) per the epic's design principle 2.
#
# On Linux the ARTIFACT's OWN SHAPE selects the verification mode -- never
# local configuration (#5054): a `.sig` accompanied by its `.pem` signing
# certificate was signed keylessly and is verified against the expected signer
# identity; a bare `.sig` was signed with a private key and needs a resolvable
# public key. Deciding by artifact shape is what makes a stale operator-set
# LOOM_DAEMON_UPDATE_COSIGN_PUBKEY harmless against keyless releases instead of
# a fleet-wide false "tamper" abort.
verify_artifact_signature() {
    local target="$1" bin_path="$2" sig_path="$3" cert_path="${4:-}"
    case "$target" in
        *-apple-darwin)
            if ! command -v codesign >/dev/null 2>&1; then
                warn "'codesign' not available -- skipping macOS signature verification (best-effort; checksum already verified)."
                return 0
            fi
            local desc
            desc="$(codesign -dv "$bin_path" 2>&1 || true)"
            if grep -q 'code object is not signed at all' <<<"$desc"; then
                warn "Downloaded artifact is unsigned (no Developer ID secrets were configured for this release) -- proceeding without signature verification, per design (checksum is unconditional; signature is optional)."
                return 0
            fi
            if codesign --verify --strict "$bin_path" 2>/dev/null; then
                ok "macOS codesign verification passed for $(basename "$bin_path")."
                return 0
            fi
            err "macOS codesign verification FAILED for $(basename "$bin_path") -- an embedded signature is present but invalid. This is NOT the 'unsigned' case; treating as tamper evidence."
            return 1
            ;;
        *-linux-*)
            if [[ -z "$sig_path" ]]; then
                # No .sig asset published for this release (cosign secret was
                # not configured for it) -- absence never blocks, by design.
                return 0
            fi
            if ! command -v cosign >/dev/null 2>&1; then
                warn "A detached signature ($(basename "$sig_path")) is present for this release but 'cosign' is not installed -- SKIPPING verification (loud skip, not a block; checksum already verified)."
                return 0
            fi
            # ---- keyless (Sigstore/OIDC): the default since #5054 ----
            if [[ -n "$cert_path" ]]; then
                local issuer identity_desc
                local -a identity
                issuer="$(resolve_cosign_oidc_issuer)"
                if [[ -n "${LOOM_DAEMON_UPDATE_COSIGN_IDENTITY:-}" ]]; then
                    identity_desc="$LOOM_DAEMON_UPDATE_COSIGN_IDENTITY"
                    identity=(--certificate-identity "$LOOM_DAEMON_UPDATE_COSIGN_IDENTITY")
                else
                    identity_desc="$(resolve_cosign_identity_regexp)"
                    if [[ -z "$identity_desc" ]]; then
                        warn "A detached signature ($(basename "$sig_path")) and its signing certificate are present but the expected signer identity could not be derived (no release slug/tag in scope) -- SKIPPING verification (loud skip, not a block; checksum already verified)."
                        return 0
                    fi
                    identity=(--certificate-identity-regexp "$identity_desc")
                fi
                if cosign verify-blob \
                        --certificate "$cert_path" \
                        --signature "$sig_path" \
                        "${identity[@]}" \
                        --certificate-oidc-issuer "$issuer" \
                        "$bin_path" >/dev/null 2>&1; then
                    ok "cosign keyless signature verification passed for $(basename "$bin_path") (signer identity ${identity_desc}, issuer ${issuer})."
                    return 0
                fi
                err "cosign keyless signature verification FAILED for $(basename "$bin_path") against $(basename "$sig_path") + $(basename "$cert_path") (expected signer identity ${identity_desc}, issuer ${issuer})."
                return 1
            fi
            # ---- key mode: a bare `.sig`, pre-#5054 signing ----
            local pubkey
            pubkey="$(resolve_cosign_pubkey)"
            if [[ -z "$pubkey" ]]; then
                warn "A detached signature ($(basename "$sig_path")) is present without a signing certificate (key-signed release) and no cosign public key is resolvable (set LOOM_DAEMON_UPDATE_COSIGN_PUBKEY) -- SKIPPING verification (loud skip, not a block; checksum already verified)."
                return 0
            fi
            if cosign verify-blob --key "$pubkey" --signature "$sig_path" "$bin_path" >/dev/null 2>&1; then
                ok "cosign signature verification passed for $(basename "$bin_path")."
                return 0
            fi
            err "cosign signature verification FAILED for $(basename "$bin_path") against $(basename "$sig_path") using key $pubkey."
            return 1
            ;;
        *) return 0 ;;
    esac
}

# Temp dirs/files created by fetch_and_verify_artifact (and, since #6160,
# the `cargo build --release --message-format=json-render-diagnostics`
# capture below) cleaned up on any exit (including the hard-abort `exit 1`
# calls both make on a verification/resolution failure) so an abort never
# leaves an unverified artifact or a build-JSON temp file lying around.
_LOOM_FETCH_TMPDIRS=()
_cleanup_fetch_tmpdirs() {
    local d
    for d in ${_LOOM_FETCH_TMPDIRS[@]+"${_LOOM_FETCH_TMPDIRS[@]}"}; do
        rm -rf "$d" 2>/dev/null || true
    done
}
trap _cleanup_fetch_tmpdirs EXIT

# fetch_and_verify_artifact -- download loom-daemon-<ARTIFACT_TARGET> +
# its .sha256 (required) + its .sig and .pem (both best-effort) for ARTIFACT_TAG from
# FETCH_REPO_SLUG, verify checksum (unconditional) and signature (when
# present), and set ARTIFACT_BIN (path to the verified binary),
# ARTIFACT_VERSION_OUTPUT (its full `--version` string, the post-provision
# identity) and ARTIFACT_COMMIT (its embedded commit, when resolvable) on
# success.
#
# A checksum or signature-verification FAILURE hard-aborts the whole script
# (exit 1) -- these are tamper/corruption signals, never soft-fallback
# conditions (AC2/AC3). A DOWNLOAD failure (network blip, asset renamed
# server-side after fetch_resolve_latest checked) instead returns 1 so the
# caller can decide -- in practice this script has already committed to
# ARTIFACT_MODE by the time this runs, so the caller also aborts, but the
# distinction keeps this function's contract composable.
fetch_and_verify_artifact() {
    local tmpdir bin_name sha_name sig_name cert_name bin_path sha_path sig_path="" cert_path=""
    tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/loom-daemon-fetch.XXXXXX" 2>/dev/null)" || {
        err "Could not create a temp dir for the artifact download."
        return 1
    }
    _LOOM_FETCH_TMPDIRS+=("$tmpdir")

    bin_name="loom-daemon-${ARTIFACT_TARGET}"
    sha_name="${bin_name}.sha256"
    sig_name="${bin_name}.sig"
    cert_name="${bin_name}.pem"

    echo "Downloading ${bin_name} + ${sha_name} from ${FETCH_REPO_SLUG}@${ARTIFACT_TAG}..."
    if ! gh release download "$ARTIFACT_TAG" -R "$FETCH_REPO_SLUG" \
            -p "$bin_name" -p "$sha_name" -D "$tmpdir" --clobber >/dev/null 2>&1; then
        err "Failed to download release assets (${bin_name}, ${sha_name}) for ${ARTIFACT_TAG} from ${FETCH_REPO_SLUG}."
        return 1
    fi
    bin_path="$tmpdir/$bin_name"
    sha_path="$tmpdir/$sha_name"
    if [[ ! -f "$bin_path" || ! -f "$sha_path" ]]; then
        err "Download reported success but expected files are missing under $tmpdir."
        return 1
    fi
    chmod 755 "$bin_path" 2>/dev/null || true

    # ---- checksum: unconditional ----
    if ! verify_artifact_checksum "$bin_path" "$sha_path"; then
        err "Checksum verification FAILED for $bin_name -- the downloaded artifact does not match its published $sha_name."
        err "Aborting the update; the running daemon (if any) is left untouched."
        exit 1
    fi
    ok "Checksum verified: $bin_name matches $sha_name."

    # ---- signature: best-effort download (may not exist), verify when present ----
    if gh release download "$ARTIFACT_TAG" -R "$FETCH_REPO_SLUG" \
            -p "$sig_name" -D "$tmpdir" --clobber >/dev/null 2>&1 \
        && [[ -f "$tmpdir/$sig_name" ]]; then
        sig_path="$tmpdir/$sig_name"
        # A keyless-signed release also publishes the ephemeral signing
        # certificate next to the signature (#5054); its presence is what
        # selects keyless verification in verify_artifact_signature. Fetched
        # separately (and only when there IS a signature) so a key-signed
        # release, which publishes no `.pem`, never turns a "pattern matched
        # nothing" download into an error.
        if gh release download "$ARTIFACT_TAG" -R "$FETCH_REPO_SLUG" \
                -p "$cert_name" -D "$tmpdir" --clobber >/dev/null 2>&1 \
            && [[ -f "$tmpdir/$cert_name" ]]; then
            cert_path="$tmpdir/$cert_name"
        fi
    fi
    if ! verify_artifact_signature "$ARTIFACT_TARGET" "$bin_path" "$sig_path" "$cert_path"; then
        err "Signature verification FAILED for $bin_name (see above)."
        err "Aborting the update; the running daemon (if any) is left untouched."
        exit 1
    fi

    ARTIFACT_BIN="$bin_path"
    # Captured from the VERIFIED download, before provisioning: the identity
    # verify_destination_artifact() asserts against afterwards (see its own
    # doc comment for why the full --version string, not the commit or a
    # byte checksum, is the right identity here).
    ARTIFACT_VERSION_OUTPUT="$("$bin_path" --version 2>/dev/null || true)"
    ARTIFACT_COMMIT="$(extract_commit "$ARTIFACT_VERSION_OUTPUT")"
    return 0
}

# ---------- stale `loom-*` entry-point check (#4079 hardening, #4557) ---------
#
# THE INCIDENT THIS EXISTS FOR (#4079, the direct motivation for epic #4081):
# a `pip install -e loom-tools` from months earlier had left FROZEN console
# scripts in ~/.local/bin. Those scripts outlived the Python package: they kept
# shadowing the Rust `loom-daemon` binary's own PATH entry points, so operators
# and agents silently ran ancient logic while `loom-daemon --version` reported a
# fresh build. Epic #4081 Phase 4 (#4557) deleted the Python package outright,
# which makes every surviving `loom-*` pip console script pure hazard: nothing
# regenerates or updates them ever again.
#
# This check itself is a WARNING ONLY, on every ordinary run. It never deletes
# anything, never mutates PATH, and never changes this script's exit code — an
# operator's ~/.local/bin is theirs, and a false positive must not block an
# update. Opt out entirely with LOOM_SKIP_STALE_ENTRY_POINT_CHECK=1.
#
# The warning used to be the end of the story: it fired on every single run,
# forever, and nothing ever removed what it found (#5139) — an operator had to
# `rm` each path by hand, and a fresh host doing so once would still get warned
# again next run if it missed one (observed on loom-worker-1: removing the
# first batch surfaced two more). `--prune-stale-entry-points` (below) closes
# that gap: an explicit, opt-in, standalone action that removes EXACTLY the
# entries this check classifies as a "Python console script (stale pip/pipx
# editable install)" — reusing the same classification helpers the warning
# uses (_lde_shim_target / _lde_describe), never reimplementing it. A stale
# SHIM (an auto-generated loom-clean/loom-recover-orphans/loom-claim wrapper
# whose sibling loom-daemon moved) is reported by the warning too but is
# deliberately NOT a prune candidate — it is a legitimate wrapper that needs
# re-provisioning, not deletion, and this script has no way to tell "moved" from
# "an operator is mid-migration". `loom-daemon` itself and any allowlisted
# entry are excluded up front, the same as the warning.
#
# A `loom-*` PATH entry is considered LEGITIMATE when it is either:
#   1. `loom-daemon` itself (the native binary), or
#   2. one of the auto-generated PATH shims provision-daemon.sh installs
#      (`loom-clean`, `loom-recover-orphans`, `loom-claim` — #4272/#4275), whose
#      sibling `loom-daemon` resolves to the SAME binary this script resolved.
# Anything else is reported — including a `loom-search` executable, now that
# #4970 retired that package too (see defaults/docs/semantic-search.md): it
# is no longer a legitimate console script, just a frozen entry point from a
# deleted package, exactly the #4079 failure shape.

#: `loom-*` names that are not daemon entry points and must not be flagged.
#: Empty as of #4970 — the one entry this ever held, `loom-search`, is retired.
STALE_ENTRY_POINT_ALLOWLIST=""

# Portable realpath (macOS ships no GNU `realpath`/`readlink -f`).
_lde_realpath() {
    local target="$1" dir base
    [[ -e "$target" ]] || { echo ""; return 0; }
    # Resolve a chain of symlinks by hand, bounded to avoid a link loop.
    local depth=0
    while [[ -L "$target" && $depth -lt 32 ]]; do
        local link
        link="$(readlink "$target")"
        case "$link" in
            /*) target="$link" ;;
            *)  target="$(dirname "$target")/$link" ;;
        esac
        depth=$((depth + 1))
    done
    dir="$(dirname "$target")"; base="$(basename "$target")"
    if cd "$dir" 2>/dev/null; then
        echo "$(pwd -P)/$base"
        cd - >/dev/null 2>&1 || true
    else
        echo "$target"
    fi
}

# _lde_shim_target <path> — for an auto-generated PATH shim, echo the
# `loom-daemon` binary it execs (its sibling). Echoes "" for anything that is
# not such a shim (a compiled binary, a Python console script, an operator's own
# wrapper).
_lde_shim_target() {
    local path="$1"
    # Binaries are not shims. `grep -Iq .` is the portable "is this a text
    # file?" test (-I treats binary as non-matching).
    grep -Iq . "$path" 2>/dev/null || { echo ""; return 0; }
    if grep -q 'exec .*/loom-daemon"\? ' "$path" 2>/dev/null \
       || grep -q 'Auto-generated PATH shim' "$path" 2>/dev/null; then
        echo "$(dirname "$path")/loom-daemon"
        return 0
    fi
    echo ""
}

# _lde_describe <path> — one-phrase classification for the warning line.
_lde_describe() {
    local path="$1" first_line
    first_line="$(head -n1 "$path" 2>/dev/null || true)"
    case "$first_line" in
        *python*) echo "Python console script (stale pip/pipx editable install)" ;;
        '#!'*)    echo "script, not a loom-daemon shim" ;;
        *)        echo "not a loom-daemon PATH shim" ;;
    esac
}

# warn_stale_entry_points <resolved_daemon_bin>
warn_stale_entry_points() {
    local resolved="$1"
    [[ "${LOOM_SKIP_STALE_ENTRY_POINT_CHECK:-0}" =~ ^(1|true|yes)$ ]] && return 0

    local resolved_real=""
    [[ -n "$resolved" ]] && resolved_real="$(_lde_realpath "$resolved")"

    # Counters are tracked ALONGSIDE the arrays rather than derived from them
    # with `${#arr[@]}`. Under `set -u` (line 217), bash < 4.4 — notably the
    # bash 3.2 stock macOS still ships, and this script's launchd paths are
    # macOS-first — treats an empty array as unset, so `${#stale_lines[@]}`
    # would abort the whole update with "unbound variable" on the overwhelmingly
    # common clean-PATH case. Every array read below is likewise guarded with
    # the `${arr[@]+"${arr[@]}"}` idiom for the same reason.
    local -a stale_lines=()
    local -a daemon_hits=()
    local -a seen_dirs=()
    local stale_count=0
    local daemon_count=0

    local oldifs="$IFS"
    IFS=':'
    # shellcheck disable=SC2206 # deliberate word-splitting of $PATH on ':'
    local -a path_dirs=($PATH)
    IFS="$oldifs"

    local dir
    for dir in "${path_dirs[@]}"; do
        [[ -z "$dir" ]] && dir="."
        [[ -d "$dir" ]] || continue
        # Dedupe repeated PATH entries so one file is never reported twice.
        local dir_real seen skip
        dir_real="$(_lde_realpath "$dir")"
        skip=false
        for seen in ${seen_dirs[@]+"${seen_dirs[@]}"}; do
            [[ "$seen" == "$dir_real" ]] && { skip=true; break; }
        done
        [[ "$skip" == "true" ]] && continue
        seen_dirs+=("$dir_real")

        local entry
        for entry in "$dir"/loom-*; do
            [[ -f "$entry" && -x "$entry" ]] || continue
            local name
            name="$(basename "$entry")"

            if [[ "$name" == "loom-daemon" ]]; then
                daemon_hits+=("$entry")
                daemon_count=$((daemon_count + 1))
                continue
            fi

            # Allowlisted non-daemon entry points. Empty as of #4970 (see the
            # STALE_ENTRY_POINT_ALLOWLIST definition above); kept as a hook for
            # any future legitimate non-daemon `loom-*` console script.
            case " $STALE_ENTRY_POINT_ALLOWLIST " in
                *" $name "*) continue ;;
            esac

            local shim_target shim_real
            shim_target="$(_lde_shim_target "$entry")"
            if [[ -n "$shim_target" && -x "$shim_target" ]]; then
                shim_real="$(_lde_realpath "$shim_target")"
                if [[ -n "$resolved_real" && "$shim_real" == "$resolved_real" ]]; then
                    continue  # a current shim pointing at the resolved binary
                fi
                stale_lines+=("$entry — PATH shim execs $shim_target, which is NOT the resolved binary (${resolved:-<none>})")
                stale_count=$((stale_count + 1))
                continue
            fi

            stale_lines+=("$entry — $(_lde_describe "$entry")")
            stale_count=$((stale_count + 1))
        done
    done

    if (( stale_count > 0 )); then
        warn "Stale 'loom-*' entry points found on PATH ($stale_count):"
        local line
        for line in ${stale_lines[@]+"${stale_lines[@]}"}; do
            warn "  - $line"
        done
        warn "These do NOT resolve to the current loom-daemon binary. Loom's Python package"
        warn "was retired (epic #4081 Phase 4, #4557), so nothing regenerates them — they are"
        warn "frozen and will shadow the real binary's entry points (incident #4079)."
        warn "Remove them, e.g.:  rm <path>    (or 'pipx uninstall loom-tools')"
        warn "Or run:  $(basename "$0") --prune-stale-entry-points   (removes exactly the stale Python console scripts above, #5139)."
        warn "Suppress this check with LOOM_SKIP_STALE_ENTRY_POINT_CHECK=1."
    fi

    # A second, distinct hazard: more than one `loom-daemon` on PATH. The first
    # wins for every caller that resolves by name, so later ones are shadowed —
    # exactly the ambiguity #4079 made costly.
    if (( daemon_count > 1 )); then
        warn "Multiple 'loom-daemon' binaries on PATH — the FIRST shadows the rest:"
        local hit
        for hit in ${daemon_hits[@]+"${daemon_hits[@]}"}; do
            warn "  - $hit ($("$hit" --version 2>/dev/null | head -n1 || echo 'version unreadable'))"
        done
        warn "Callers resolving 'loom-daemon' by name get ${daemon_hits[0]}. Remove the others"
        warn "or pin LOOM_DAEMON_BIN explicitly."
    fi
}

# prune_stale_entry_points <resolved_daemon_bin> — remove EXACTLY the PATH
# entries warn_stale_entry_points() above would classify as "Python console
# script (stale pip/pipx editable install)" (#5139). Deliberately narrower
# than the full warning:
#   - `loom-daemon` itself and any STALE_ENTRY_POINT_ALLOWLIST entry are
#     excluded up front, same as the warning.
#   - ANY auto-generated PATH shim (`_lde_shim_target` returns non-empty) is
#     skipped, whether it currently resolves to the resolved binary or not.
#     A stale shim (its sibling loom-daemon moved) IS reported by the warning,
#     but it is a legitimate bash wrapper that needs re-provisioning, not a
#     frozen Python console script — pruning it would violate the "never
#     touch the legitimate bash wrappers" guardrail.
#   - only entries whose classification (`_lde_describe`) is EXACTLY the
#     Python-console-script string are removed.
# Idempotent: a second run finds nothing left to remove and reports that.
# Reports every path removed (and any removal failure, without aborting the
# rest — a permissions problem on one path should not hide a successful
# removal of the others). Returns 1 if any `rm` failed, 0 otherwise (including
# the "nothing to prune" case).
prune_stale_entry_points() {
    local resolved="$1"
    if [[ "${LOOM_SKIP_STALE_ENTRY_POINT_CHECK:-0}" =~ ^(1|true|yes)$ ]]; then
        echo "LOOM_SKIP_STALE_ENTRY_POINT_CHECK is set — leaving all 'loom-*' PATH entries untouched."
        return 0
    fi

    # Not consulted for classification (a shim is skipped outright, whatever
    # it points at), but kept for parity with warn_stale_entry_points and in
    # case a future classification wants it.
    local resolved_real=""
    [[ -n "$resolved" ]] && resolved_real="$(_lde_realpath "$resolved")"

    # Counter tracked alongside the array — see warn_stale_entry_points'
    # comment above on why `${#arr[@]}` is unsafe under `set -u` on bash 3.2.
    local -a to_remove=()
    local remove_count=0
    local -a seen_dirs=()

    local oldifs="$IFS"
    IFS=':'
    # shellcheck disable=SC2206 # deliberate word-splitting of $PATH on ':'
    local -a path_dirs=($PATH)
    IFS="$oldifs"

    local dir
    for dir in "${path_dirs[@]}"; do
        [[ -z "$dir" ]] && dir="."
        [[ -d "$dir" ]] || continue
        local dir_real seen skip
        dir_real="$(_lde_realpath "$dir")"
        skip=false
        for seen in ${seen_dirs[@]+"${seen_dirs[@]}"}; do
            [[ "$seen" == "$dir_real" ]] && { skip=true; break; }
        done
        [[ "$skip" == "true" ]] && continue
        seen_dirs+=("$dir_real")

        local entry
        for entry in "$dir"/loom-*; do
            [[ -f "$entry" && -x "$entry" ]] || continue
            local name
            name="$(basename "$entry")"
            [[ "$name" == "loom-daemon" ]] && continue

            case " $STALE_ENTRY_POINT_ALLOWLIST " in
                *" $name "*) continue ;;
            esac

            # Any shim — current OR stale — is never a prune candidate; only
            # warn_stale_entry_points reports a stale one, and it is a
            # legitimate wrapper, not a frozen Python console script.
            local shim_target
            shim_target="$(_lde_shim_target "$entry")"
            [[ -n "$shim_target" ]] && continue

            if [[ "$(_lde_describe "$entry")" == "Python console script (stale pip/pipx editable install)" ]]; then
                to_remove+=("$entry")
                remove_count=$((remove_count + 1))
            fi
        done
    done

    if (( remove_count == 0 )); then
        ok "No stale Python console-script entry points found on PATH — nothing to prune."
        return 0
    fi

    echo "Pruning $remove_count stale Python console-script entry point(s):"
    local path failures=0
    for path in ${to_remove[@]+"${to_remove[@]}"}; do
        if rm -f -- "$path" 2>/dev/null; then
            ok "  removed: $path"
        else
            err "  FAILED to remove: $path"
            failures=$((failures + 1))
        fi
    done
    if (( failures > 0 )); then
        err "Pruning finished with $failures failure(s) above — check permissions on those paths."
        return 1
    fi
    ok "Pruned $remove_count stale entry point(s). Re-run with --check (or the check on the next update) to confirm the advisory is now silent."
    return 0
}

# verify_destination_binary <dest_path> — assert the provisioned binary at
# <dest_path> embeds the expected source-HEAD commit (#4053). This is the
# direct answer to "reports success while shipping nothing": after a provision
# step returns success, the destination must actually be the freshly-built
# binary. Exits 5 on mismatch — distinguishable from a compile failure (exit 1)
# and from a provisioning soft-failure. Skipped only when the source HEAD is
# unknown (a tarball build with no .git), where there is nothing to compare
# against. Relies on $SOURCE_COMMIT being resolved (it is, before any build).
#
# SOURCE-BUILD PATH ONLY. The artifact-fetch path (#5020) uses
# verify_destination_artifact() below instead: a fetched release's commit has
# nothing to do with the local checkout's HEAD, so this function's whole
# premise does not apply there.
verify_destination_binary() {
    local dest="$1"
    if [[ "$SOURCE_COMMIT" == "unknown" ]]; then
        warn "Source HEAD is unknown (no .git?) — skipping post-provision verification."
        return 0
    fi
    if [[ -z "$dest" || ! -x "$dest" ]]; then
        err "Post-provision verification FAILED: provisioning reported success but no executable binary was found at the destination ('${dest:-<unknown>}')."
        exit 5
    fi
    local dest_version dest_commit
    dest_version=$("$dest" --version 2>/dev/null || true)
    dest_commit=$(extract_commit "$dest_version")
    if [[ "$dest_commit" != "$SOURCE_COMMIT" ]]; then
        err "Post-provision verification FAILED: destination binary at $dest embeds commit '${dest_commit:-<none>}' but the expected source HEAD is '$SOURCE_COMMIT'."
        err "Provisioning reported success yet the destination is NOT the freshly-built binary — a silent no-op roll. This is distinct from a compile failure and from a provisioning soft-failure; refusing to report success."
        exit 5
    fi
    ok "Post-provision verification: destination binary at $dest embeds source HEAD commit ($dest_commit)."
}

# verify_destination_artifact <dest_path> — the artifact-fetch (#5020)
# counterpart of verify_destination_binary above, closing the SAME "reports
# success while shipping nothing" hole for a fetched release binary. Exits 5
# on mismatch, identically.
#
# Why a different identity than the source path's embedded-commit compare:
#   - A fetched release artifact's commit is the RELEASE's commit, unrelated to
#     the local checkout's HEAD — comparing against $SOURCE_COMMIT would fail
#     on every successful artifact roll.
#   - A byte/checksum compare of the destination is NOT usable either:
#     provision_machine_daemon() calls sign_daemon_binary() on the DESTINATION,
#     which (on Darwin, for a genuinely unsigned artifact) ad-hoc re-signs it
#     in place and therefore changes its bytes vs. the verified download.
# So the identity used here is the artifact's own full `--version` STRING
# (version + commit + build timestamp), which is stable across a re-sign and
# is exactly the same string provision_machine_daemon()'s own version-equality
# short-circuit compares — so this assertion also proves that short-circuit
# did not silently no-op a real roll.
#
# $ARTIFACT_VERSION_OUTPUT is captured from the VERIFIED download before
# provisioning. It is empty only if the artifact refused to report a version,
# in which case there is nothing to compare against and we skip loudly rather
# than invent a comparison.
verify_destination_artifact() {
    local dest="$1"
    if [[ -z "${ARTIFACT_VERSION_OUTPUT:-}" ]]; then
        warn "Fetched artifact reported no --version output — skipping post-provision verification (checksum/signature were already verified pre-provision)."
        return 0
    fi
    if [[ -z "$dest" || ! -x "$dest" ]]; then
        err "Post-provision verification FAILED: provisioning reported success but no executable binary was found at the destination ('${dest:-<unknown>}')."
        exit 5
    fi
    local dest_version
    dest_version=$("$dest" --version 2>/dev/null || true)
    if [[ "$dest_version" != "$ARTIFACT_VERSION_OUTPUT" ]]; then
        err "Post-provision verification FAILED: destination binary at $dest reports '${dest_version:-<none>}' but the fetched release artifact reports '$ARTIFACT_VERSION_OUTPUT'."
        err "Provisioning reported success yet the destination is NOT the freshly-fetched binary — a silent no-op roll. Refusing to report success."
        exit 5
    fi
    ok "Post-provision verification: destination binary at $dest is the fetched release artifact ($dest_version)."
}

# verify_supervisor_matches_provisioned <provisioned_dest> — the #6009
# counterpart to verify_destination_binary()/verify_destination_artifact()
# above: those two only prove the binary THIS RUN WROTE embeds the expected
# commit. Neither says anything about whether that is the binary
# systemd/launchd will actually launch on the restart that follows — if the
# supervisor's persisted config (SUPERVISOR_BIN, resolved during staleness
# detection above from the unit's ExecStart= / the plist's
# ProgramArguments[0]) still points at a DIFFERENT path than what was just
# provisioned, the freshly-built binary never reaches the running daemon: the
# restart request goes to the OLD binary, and systemd/launchd's own relaunch
# re-execs that SAME stale path again afterward.
#
# Advisory only (never exits non-zero) — provisioning to <provisioned_dest>
# already succeeded at what it set out to do; correcting the supervisor's own
# config is what --relaunch is for (it re-renders the plist/unit against the
# current binary resolution).
verify_supervisor_matches_provisioned() {
    local provisioned="$1"
    [[ -n "$SUPERVISOR_BIN" ]] || return 0
    [[ -n "$provisioned" ]] || return 0
    local provisioned_real supervisor_real
    provisioned_real="$(_lde_realpath "$provisioned")"
    supervisor_real="$(_lde_realpath "$SUPERVISOR_BIN")"
    if [[ -n "$provisioned_real" && "$provisioned_real" != "$supervisor_real" ]]; then
        warn "The ${DAEMON_MANAGER}-managed binary (${SUPERVISOR_BIN}) is NOT the one just provisioned (${provisioned}) — restarting below re-launches the daemon via the EXISTING ${DAEMON_MANAGER} config, not from this build, so it may still come back on the OLD binary. Run '$(basename "$0") --relaunch' to re-render the ${DAEMON_MANAGER} config so it points at the current binary."
    fi
}

# ---------- args ----------
DRY_RUN=false
FORCE=false
CHECK_ONLY=false
NO_RESTART=false
RELAUNCH=false
ALLOW_STALE=false
AUTO_RESOLVE_SAFE_ABORT=false
PRUNE_STALE=false
# Drain-mode restart passthrough (Issue #5138) -- see build_restart_invoke_args
# below for how these thread into `loom-daemon restart --drain ...`.
DRAIN=false
DRAIN_TIMEOUT=""
FORCE_AFTER_TIMEOUT=false
RESTART_NOW=false
# FETCH_MODE: auto (default -- prefer a verified release artifact when one
# resolves, else soft-fall-back to the source build), force (--fetch --
# require an artifact, hard-fail rather than silently building from source),
# or off (--no-fetch -- always build from source, the pre-#5020 behavior).
FETCH_MODE="auto"
[[ "${LOOM_DAEMON_UPDATE_RELAUNCH:-}" =~ ^(1|true|yes)$ ]] && RELAUNCH=true
[[ "${LOOM_DAEMON_UPDATE_DRAIN:-}" =~ ^(1|true|yes)$ ]] && DRAIN=true
[[ "${LOOM_DAEMON_UPDATE_RESTART_NOW:-}" =~ ^(1|true|yes)$ ]] && RESTART_NOW=true
[[ "${LOOM_DAEMON_UPDATE_FETCH:-}" =~ ^(0|false|no)$ ]] && FETCH_MODE="off"
[[ "${LOOM_DAEMON_UPDATE_FETCH:-}" =~ ^(1|true|yes)$ ]] && FETCH_MODE="force"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h) show_help; exit 0 ;;
        --dry-run) DRY_RUN=true; shift ;;
        --force) FORCE=true; shift ;;
        --check) CHECK_ONLY=true; shift ;;
        --no-restart) NO_RESTART=true; shift ;;
        --relaunch) RELAUNCH=true; shift ;;
        --drain) DRAIN=true; shift ;;
        --timeout)
            [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || { err "--timeout requires a numeric SECS argument"; exit 1; }
            DRAIN_TIMEOUT="$2"; shift 2 ;;
        --force-after-timeout) FORCE_AFTER_TIMEOUT=true; shift ;;
        --restart-now) RESTART_NOW=true; shift ;;
        --allow-stale) ALLOW_STALE=true; shift ;;
        --auto-resolve-safe-abort) AUTO_RESOLVE_SAFE_ABORT=true; shift ;;
        --fetch) FETCH_MODE="force"; shift ;;
        --no-fetch) FETCH_MODE="off"; shift ;;
        --prune-stale-entry-points) PRUNE_STALE=true; shift ;;
        *) err "Unknown option '$1'"; echo "Use --help for usage" >&2; exit 1 ;;
    esac
done

if [[ "$DRAIN" == "true" && "$RESTART_NOW" == "true" ]]; then
    err "--drain and --restart-now are mutually exclusive (drain vs. immediate restart)."
    exit 1
fi

REPO_ROOT=$(find_repo_root)

# ---------- self-location fallback (#5140) ----------------------------------
# This script rebuilds FROM SOURCE, and its own location is unambiguous when it
# is invoked by absolute path (`bash ~/GitHub/loom/.loom/scripts/cli/loom-daemon-update.sh`
# from $HOME — the reported case). When $PWD is not inside ANY Loom checkout but
# the checkout this very script lives in is a source checkout, use that instead
# of refusing on a path the operator never named. Announced on stderr, never
# silent: choosing a different checkout than $PWD implies is exactly the kind of
# thing an operator must be able to see in the log.
#
# Deliberately scoped to the "no checkout at all" case. When $PWD DOES resolve
# to a Loom checkout that simply has no loom-daemon/ crate (a consumer repo),
# the pre-existing refusal stands — retargeting the machine checkout from
# inside another repo is opt-in via LOOM_MACHINE_CHECKOUT (#4229), not a guess.
SELF_REPO_ROOT=$(find_repo_root "$SCRIPT_DIR")
if [[ -z "$REPO_ROOT" ]] && is_loom_source_checkout "$SELF_REPO_ROOT"; then
    warn "\$PWD ($PWD) is not inside a Loom source checkout; using this script's own checkout: $SELF_REPO_ROOT"
    REPO_ROOT="$SELF_REPO_ROOT"
fi

# ---------- machine-mode source-tree override (Epic #3835 Phase 3b, #4229) --
# Gap 1: this script rebuilds FROM SOURCE and used to resolve that source tree
# by walking up from $PWD -- so from a consumer repo (find_repo_root() finds
# the consumer repo, which has no loom-daemon/Cargo.toml) or a non-repo
# directory (find_repo_root() finds nothing) it refused with "only works
# inside a Loom source checkout", even though the `scripts/loom` dispatcher
# had ALREADY resolved+validated the machine checkout before exec'ing here.
# LOOM_MACHINE_CHECKOUT overrides the $PWD-derived REPO_ROOT with that
# checkout, so `loom update` rebuilds it from any directory. Direct invocation
# of this script (no dispatcher) never sets it and is unaffected -- the
# pre-#4229 $PWD-based contract is the fallback below.
MACHINE_CHECKOUT="${LOOM_MACHINE_CHECKOUT:-}"
if [[ -n "$MACHINE_CHECKOUT" ]]; then
    if [[ ! -d "$MACHINE_CHECKOUT" ]]; then
        err "LOOM_MACHINE_CHECKOUT does not exist: $MACHINE_CHECKOUT"
        exit 1
    fi
    REPO_ROOT="$MACHINE_CHECKOUT"
    DAEMON_STATE_HOME="$HOME/.loom"
elif [[ -n "$REPO_ROOT" ]]; then
    DAEMON_STATE_HOME="$REPO_ROOT/.loom"
else
    # #5140: name what was searched and what is required, so this never reads
    # as "your checkout is broken" when the real answer is "you are standing in
    # the wrong directory".
    err "Not in a Loom workspace: neither \$PWD ($PWD) nor this script's own location ($SCRIPT_DIR) is inside a Loom checkout."
    echo "A Loom checkout is a directory containing BOTH .git and .loom/ (a bare ~/.loom, e.g. the token pool, is not one)." >&2
    echo "cd into a Loom source checkout, or set LOOM_MACHINE_CHECKOUT=<path-to-checkout>." >&2
    exit 1
fi

DAEMON_DIR="$REPO_ROOT/loom-daemon"
if [[ ! -f "$DAEMON_DIR/Cargo.toml" ]]; then
    err "No loom-daemon/Cargo.toml found at $DAEMON_DIR (repo root resolved as $REPO_ROOT)."
    echo "loom-daemon-update.sh rebuilds FROM SOURCE and only works inside a Loom source checkout." >&2
    echo "cd into a Loom source checkout, or set LOOM_MACHINE_CHECKOUT=<path-to-checkout>." >&2
    exit 1
fi

# Resolve a lifecycle script under REPO_ROOT: the INSTALLED copy first (a
# self-hosted checkout's own .loom/scripts/cli/, kept in sync by
# resync-installed.sh), falling back to defaults/scripts/cli/ -- the shipped
# source of truth every Loom source checkout has, including a fresh clone that
# has never been "installed" onto itself (machine mode may point at exactly
# that). Direct (non-machine) invocation almost always resolves the first
# candidate, matching pre-#4229 behavior byte-for-byte.
resolve_lifecycle_script() {
    local rel="$1" candidate
    for candidate in \
        "$REPO_ROOT/.loom/scripts/cli/$rel" \
        "$REPO_ROOT/defaults/scripts/cli/$rel"; do
        if [[ -x "$candidate" ]]; then echo "$candidate"; return 0; fi
    done
    echo ""
}

# ---------- pid-file resolution (#6386) ----------
# LOOM_PID_FILE is TIER 1 here for the SAME reason (and with the same
# precedence) as in loom-daemon-stop.sh: this script restarts the daemon by
# invoking that stop script, so if the two disagreed about which pid file is
# "the daemon", update would plan its restart against one file while the stop
# it delegates to acts on another. The pid file only feeds liveness detection
# here (DAEMON_MANAGER/WAS_RUNNING below); the actual kill happens inside
# loom-daemon-stop.sh --restarting. FLAGS_FILE stays state-home-derived --
# LOOM_PID_FILE names the pid file, not the state home.
PID_FILE="${LOOM_PID_FILE:-$DAEMON_STATE_HOME/.daemon.pid}"
FLAGS_FILE="$DAEMON_STATE_HOME/.daemon.flags"
START_SCRIPT="$(resolve_lifecycle_script loom-daemon-start.sh)"
STOP_SCRIPT="$(resolve_lifecycle_script loom-daemon-stop.sh)"
if [[ -z "$START_SCRIPT" || -z "$STOP_SCRIPT" ]]; then
    err "Could not resolve loom-daemon-start.sh / loom-daemon-stop.sh under $REPO_ROOT (.loom/scripts/cli or defaults/scripts/cli)."
    exit 1
fi

# ---------- sync with origin/<default-branch> (ff-first default, #4330) ----------
# Runs BEFORE the staleness detection below resolves SOURCE_COMMIT, so a
# successful ff-sync is reflected in the rebuild decision (rebuilding the
# freshly-synced HEAD, not the pre-merge one). Never touches the network or
# the tree in --check / --dry-run (both are documented "no writes" contracts)
# or under --allow-stale (today's build-what's-here behavior) — those paths
# fall through to the read-only advisory branch below instead.
#
# Globals set for downstream consumers (staleness echo + the final
# "installed" summary, AC4):
#   DEFAULT_BRANCH        resolved default branch name, or "" if unresolvable
#   ORIGIN_COMMIT         short commit of origin/<DEFAULT_BRANCH> at fetch
#                         time, or "unknown" if unreachable/unresolvable
#   ORIGIN_BEHIND_COUNT   commits local <DEFAULT_BRANCH> was behind origin
#                         BEFORE any sync (0 if unknown or already current)
#   FF_SYNCED             true if this run fast-forwarded local HEAD
DEFAULT_BRANCH=""
ORIGIN_COMMIT="unknown"
ORIGIN_BEHIND_COUNT=0
FF_SYNCED=false

# ---------- ff-abort classification (#4951) ----------
# The `git merge --ff-only` failure branch below used to emit ONE generic
# "resolve manually" message for two structurally distinct, often-safe
# failure shapes (surfaced by the 2026-08-02 fleet roll aborting on 2/3
# hosts on local state that turned out to be safely resolvable by hand).
# These helpers classify which shape applies so the abort message can name
# (or, with --auto-resolve-safe-abort, perform) the exact safe resolution
# instead of a bare "resolve manually".
#
# Safety note: this is the safety-critical branch of loom-daemon-update.sh —
# #4381 was a live incident where an update-script code path silently
# overwrote a real production binary; the same "automation quietly does
# something destructive to real machine state" risk applies here. Do NOT
# widen either check below (e.g. a loose substring/prefix match, or an
# empty-diff check that misses a rename/mode-only change) beyond exactly
# what's specified in #4951 — when genuinely unsure, both helpers must
# return false so the caller falls through to the existing hard abort.

# True (exit 0) iff NO tracked file is dirty in the working tree per
# `git status --porcelain`. Untracked (`??`) entries are excluded, matching
# _ff_abort_all_dirty_tracked_managed below: they cannot conflict with a
# fast-forward merge, and `git reset --hard` never touches them either.
_ff_abort_no_dirty_tracked_files() {
    local repo_root="$1" line status
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        status="${line:0:2}"
        [[ "$status" == '??' ]] && continue
        return 1
    done < <(cd "$repo_root" && git status --porcelain)
    return 0
}

# True (exit 0) iff local <default> and origin/<default> are content-IDENTICAL
# despite having diverged in commit history (e.g. a resync commit + its own
# revert nets to no change). Deliberately the plain three-dot merge-base diff
# form with no rename-detection flags, matching the incident's own manual
# check (`git diff origin/main...main`); `git diff --quiet` also treats a
# mode-only change as non-empty, which is the conservative (safe) direction.
#
# Only meaningful when local <default> has commit(s) NOT reachable from
# origin/<default> (a genuine history divergence) — the `--is-ancestor` guard
# below is load-bearing, not an optimization: when local <default> IS an
# ancestor of origin/<default> (the far more common shape: origin simply
# advanced and local added no commits of its own), the three-dot form trivially
# reduces to diffing local HEAD against itself (merge-base == local HEAD),
# which is ALWAYS empty regardless of what origin changed — so without this
# guard, every plain "blocked by a dirty tracked file" ff-abort (managed or
# not) would misclassify as content-identical and risk an incorrect
# `reset --hard` under --auto-resolve-safe-abort. In that ancestor case the ff
# failure is necessarily a dirty/conflicting working-tree file instead, which
# the OTHER classifier below is responsible for.
#
# ALSO requires a CLEAN working tree relative to local HEAD: both checks above
# compare only COMMITTED refs, so on their own they say nothing about
# uncommitted work sitting in the working tree. A host can simultaneously have
# (a) diverged local commits that net to zero content diff vs. origin and (b)
# an entirely unrelated dirty tracked file (an operator's manual scratch edit,
# managed or not) — `git merge --ff-only` fails on the history divergence
# alone, so this branch is reached, and without this guard the caller's
# --auto-resolve-safe-abort `git reset --hard origin/<default>` would silently
# discard that file's uncommitted changes. The resolution this classifier
# vouches for is only safe when there is no uncommitted work for it to
# destroy, so ANY dirty tracked file makes us return false and fall through —
# to the managed-dirty classifier below, and failing that to the existing hard
# abort. Mirrors the strictness _ff_abort_all_dirty_tracked_managed already
# applies, and the "when genuinely unsure, return false" rule above.
_ff_abort_content_identical() {
    local repo_root="$1" branch="$2"
    if (cd "$repo_root" && git merge-base --is-ancestor "$branch" "origin/${branch}" 2>/dev/null); then
        return 1
    fi
    _ff_abort_no_dirty_tracked_files "$repo_root" || return 1
    (cd "$repo_root" && git diff --quiet "origin/${branch}...${branch}" -- 2>/dev/null)
}

# Loom-managed installed-surface prefixes/files this script is allowed to
# discard local edits to when auto-resolving. MUST mirror
# defaults/scripts/resync-installed.sh's own header comment (search "Surfaces
# resynced" in that file) — that file, not this list, is the authoritative
# source; update both together if it ever widens again (#4239 already widened
# it once).
_FF_ABORT_MANAGED_PREFIXES=(
    ".loom/hooks/"
    ".loom/scripts/"
    ".loom/roles/"
    ".loom/docs/"
    ".loom/runtimes/"
    ".loom/bin/"
    ".claude/commands/loom/"
)
_FF_ABORT_MANAGED_FILES=(
    ".loom/install-metadata.json"
)

# True (exit 0) iff the ONLY diff between the working tree's .gitignore and
# HEAD's is inside the marker-delimited Loom-managed block (loom-daemon's
# GITIGNORE_BEGIN_MARKER / GITIGNORE_END_MARKER, loom-daemon/src/init/post_init.rs)
# — never the whole file, so a consumer's own hand-edited lines outside that
# block are never silently discarded.
_ff_abort_gitignore_only_managed_block_dirty() {
    local repo_root="$1"
    local file="$repo_root/.gitignore"
    [[ -f "$file" ]] || return 1
    local begin='# >>> loom-managed (do not edit) >>>'
    local end='# <<< loom-managed <<<'
    local working_stripped head_stripped
    working_stripped=$(awk -v b="$begin" -v e="$end" 'BEGIN{skip=0} $0==b{skip=1;next} $0==e{skip=0;next} skip==0{print}' "$file" 2>/dev/null)
    head_stripped=$(cd "$repo_root" && git show "HEAD:.gitignore" 2>/dev/null | awk -v b="$begin" -v e="$end" 'BEGIN{skip=0} $0==b{skip=1;next} $0==e{skip=0;next} skip==0{print}')
    [[ "$working_stripped" == "$head_stripped" ]]
}

# True (exit 0) iff $2 (a repo-relative path from `git status --porcelain`) is
# inside the managed installed-surface set above.
_ff_abort_is_managed_path() {
    local repo_root="$1" path="$2" p f
    for f in "${_FF_ABORT_MANAGED_FILES[@]}"; do
        [[ "$path" == "$f" ]] && return 0
    done
    for p in "${_FF_ABORT_MANAGED_PREFIXES[@]}"; do
        [[ "$path" == "$p"* ]] && return 0
    done
    if [[ "$path" == ".gitignore" ]]; then
        _ff_abort_gitignore_only_managed_block_dirty "$repo_root" && return 0
    fi
    return 1
}

# Populates the global array _FF_ABORT_DIRTY_MANAGED_PATHS and returns 0 iff
# (a) at least one TRACKED file is dirty per `git status --porcelain`
# (untracked `??` entries are excluded — they cannot conflict with a
# fast-forward merge the way a dirty tracked file can) AND (b) EVERY one of
# them is a managed path. Conjunctive by design, matching the issue's "every
# blocking file" wording: one unmanaged dirty file alongside managed ones
# still falls through to the hard abort.
_FF_ABORT_DIRTY_MANAGED_PATHS=()
_ff_abort_all_dirty_tracked_managed() {
    local repo_root="$1"
    _FF_ABORT_DIRTY_MANAGED_PATHS=()
    local line status path found_any=false
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        status="${line:0:2}"
        [[ "$status" == '??' ]] && continue
        path="${line:3}"
        if [[ "$path" == *" -> "* ]]; then
            path="${path##* -> }"
        fi
        path="${path%\"}"
        path="${path#\"}"
        found_any=true
        _ff_abort_is_managed_path "$repo_root" "$path" || return 1
        _FF_ABORT_DIRTY_MANAGED_PATHS+=("$path")
    done < <(cd "$repo_root" && git status --porcelain)
    [[ "$found_any" == "true" ]]
}

# Best-effort resolve of resync-installed.sh under repo_root: the installed
# copy first, else the shipped defaults/ source — same installed-then-defaults
# precedence as resolve_lifecycle_script() above, adjusted for
# resync-installed.sh living directly under scripts/, not scripts/cli/.
_ff_abort_resolve_resync_script() {
    local repo_root="$1" candidate
    for candidate in \
        "$repo_root/.loom/scripts/resync-installed.sh" \
        "$repo_root/defaults/scripts/resync-installed.sh"; do
        if [[ -x "$candidate" ]]; then echo "$candidate"; return 0; fi
    done
    echo ""
}

sync_with_origin() {
    local repo_root="$1"
    # shellcheck disable=SC1091
    if [[ -r "$SCRIPT_DIR/../lib/default-branch.sh" ]]; then
        source "$SCRIPT_DIR/../lib/default-branch.sh" 2>/dev/null || return 0
    else
        return 0
    fi
    declare -F loom_default_branch >/dev/null 2>&1 || return 0
    DEFAULT_BRANCH="$(cd "$repo_root" && loom_default_branch origin 2>/dev/null)" || { DEFAULT_BRANCH=""; return 0; }
    [[ -z "$DEFAULT_BRANCH" ]] && return 0

    # Bounded, best-effort fetch — a fetch failure/timeout must NOT make this
    # script network-dependent: warn and proceed with local HEAD as-is (behind
    # count stays unknown, not "known stale").
    local fetch_ok=true
    if command -v timeout >/dev/null 2>&1; then
        (cd "$repo_root" && timeout 5 git fetch origin "$DEFAULT_BRANCH" --quiet >/dev/null 2>&1) || fetch_ok=false
    else
        (cd "$repo_root" && git fetch origin "$DEFAULT_BRANCH" --quiet >/dev/null 2>&1) || fetch_ok=false
    fi
    if [[ "$fetch_ok" == "false" ]]; then
        warn "note: could not reach origin to check ${DEFAULT_BRANCH} for updates (fetch failed or timed out) — proceeding with local HEAD as-is."
        return 0
    fi

    ORIGIN_COMMIT="$(cd "$repo_root" && git rev-parse --short "origin/${DEFAULT_BRANCH}" 2>/dev/null || echo "unknown")"

    local n
    n="$(cd "$repo_root" && git rev-list --count "${DEFAULT_BRANCH}..origin/${DEFAULT_BRANCH}" 2>/dev/null || echo 0)"
    [[ "$n" =~ ^[0-9]+$ ]] || n=0
    ORIGIN_BEHIND_COUNT="$n"
    [[ "$n" -eq 0 ]] && return 0

    # Read-only modes and --allow-stale never write — just advise, mirroring
    # the pre-#4330 advisory-only behavior.
    if [[ "$CHECK_ONLY" == "true" || "$DRY_RUN" == "true" ]]; then
        warn "note: local ${DEFAULT_BRANCH} is ${n} commit(s) behind origin/${DEFAULT_BRANCH}."
        return 0
    fi
    if [[ "$ALLOW_STALE" == "true" ]]; then
        warn "note: local ${DEFAULT_BRANCH} is ${n} commit(s) behind origin/${DEFAULT_BRANCH} — building the current (stale) checkout as-is per --allow-stale."
        return 0
    fi

    # Default: attempt the ff-sync. Only well-defined when HEAD IS the default
    # branch — on a feature branch or detached HEAD, `git merge --ff-only
    # origin/<default>` would merge into the WRONG ref, so refuse instead of
    # guessing (an operator deliberately elsewhere, e.g. bisecting, is exactly
    # the --allow-stale use case).
    local current_branch
    current_branch="$(cd "$repo_root" && git symbolic-ref --short HEAD 2>/dev/null || true)"
    if [[ "$current_branch" != "$DEFAULT_BRANCH" ]]; then
        err "Local ${DEFAULT_BRANCH} is ${n} commit(s) behind origin/${DEFAULT_BRANCH}, but the checkout HEAD is on '${current_branch:-<detached HEAD>}', not '${DEFAULT_BRANCH}' — refusing to guess which branch to sync."
        err "Check out ${DEFAULT_BRANCH} and re-run, or pass --allow-stale to build the current checkout as-is (e.g. bisecting, testing a local patch)."
        return 1
    fi

    echo "Local ${DEFAULT_BRANCH} is ${n} commit(s) behind origin/${DEFAULT_BRANCH} — fast-forwarding before building (default; pass --allow-stale to build the current checkout as-is)..."
    if ! (cd "$repo_root" && git merge --ff-only "origin/${DEFAULT_BRANCH}" --quiet); then
        # Classify the failure (#4951) before falling back to the generic
        # hard abort — see the "ff-abort classification" helpers above.
        if _ff_abort_content_identical "$repo_root" "$DEFAULT_BRANCH"; then
            warn "Fast-forward merge from origin/${DEFAULT_BRANCH} did not apply, but local ${DEFAULT_BRANCH} is content-IDENTICAL to origin/${DEFAULT_BRANCH} (git diff origin/${DEFAULT_BRANCH}...${DEFAULT_BRANCH} is empty) — local-only commit(s) that net to no change (e.g. a resync commit and its own revert)."
            if [[ "$AUTO_RESOLVE_SAFE_ABORT" == "true" ]]; then
                if (cd "$repo_root" && git reset --hard "origin/${DEFAULT_BRANCH}" --quiet); then
                    ok "Auto-resolved (--auto-resolve-safe-abort): reset local ${DEFAULT_BRANCH} to origin/${DEFAULT_BRANCH}."
                    FF_SYNCED=true
                    return 0
                fi
                err "Auto-resolve (--auto-resolve-safe-abort) failed: 'git reset --hard origin/${DEFAULT_BRANCH}' did not succeed."
                return 1
            fi
            err "Safe to resolve: git -C \"$repo_root\" reset --hard origin/${DEFAULT_BRANCH}"
            err "Re-run with --auto-resolve-safe-abort to perform this automatically, or run the command above by hand."
            return 1
        fi
        if _ff_abort_all_dirty_tracked_managed "$repo_root"; then
            warn "Fast-forward merge from origin/${DEFAULT_BRANCH} was blocked by dirty tracked file(s), but ALL of them are Loom-managed installed copies (regenerated from defaults/ by resync-installed.sh, not real local work): ${_FF_ABORT_DIRTY_MANAGED_PATHS[*]}"
            if [[ "$AUTO_RESOLVE_SAFE_ABORT" == "true" ]]; then
                if (cd "$repo_root" && git checkout -- "${_FF_ABORT_DIRTY_MANAGED_PATHS[@]}") \
                    && (cd "$repo_root" && git merge --ff-only "origin/${DEFAULT_BRANCH}" --quiet); then
                    ok "Auto-resolved (--auto-resolve-safe-abort): discarded local edits to managed file(s) and fast-forwarded to origin/${DEFAULT_BRANCH}."
                    local resync_script
                    resync_script="$(_ff_abort_resolve_resync_script "$repo_root")"
                    if [[ -n "$resync_script" ]]; then
                        if (cd "$repo_root" && "$resync_script" >/dev/null 2>&1); then
                            ok "Post-roll resync-installed.sh completed."
                        else
                            warn "Post-roll resync-installed.sh failed — run it by hand: $resync_script"
                        fi
                    else
                        warn "Could not resolve resync-installed.sh — run it by hand after this update to re-sync managed files."
                    fi
                    FF_SYNCED=true
                    return 0
                fi
                err "Auto-resolve (--auto-resolve-safe-abort) failed: discarding managed edits + fast-forward did not both succeed."
                return 1
            fi
            err "Safe to resolve: git -C \"$repo_root\" checkout -- ${_FF_ABORT_DIRTY_MANAGED_PATHS[*]} && ./.loom/scripts/resync-installed.sh"
            err "Re-run with --auto-resolve-safe-abort to perform this automatically, or run the commands above by hand."
            return 1
        fi
        err "Fast-forward merge from origin/${DEFAULT_BRANCH} did not apply — local commits have diverged, or a dirty tracked file conflicts with the incoming change."
        err "Refusing to guess or hard-reset: resolve manually (rebase/merge by hand), or pass --allow-stale to build the current (stale) checkout as-is."
        # #6008: on a fleet host the most common non-managed blocker here is a
        # host-specific edit parked in a TRACKED config tier — it re-blocks every
        # roll forever, so the checkout drifts hundreds of commits behind. Name
        # the host-local tier instead of only saying "resolve manually".
        local dirty_cfg_tiers
        dirty_cfg_tiers="$( (cd "$repo_root" && git diff --name-only HEAD -- \
            .loom/config.json .loom-project/project.json 2>/dev/null) | tr '\n' ' ')"
        dirty_cfg_tiers="${dirty_cfg_tiers%"${dirty_cfg_tiers##*[![:space:]]}"}"
        if [[ -n "$dirty_cfg_tiers" ]]; then
            err "Dirty TRACKED config tier(s) present: ${dirty_cfg_tiers}"
            err "If that diff is host-specific — a per-host path or socket, or an on/off switch that is true of this box and false of the others (worktree.root, safehouse.enabled, safehouse.socket) — it does not belong in a tracked file at all. Move it to the gitignored .loom-local/local.json tier, which never shows up in git status and so can never block this sync again. Per-key runbook: https://github.com/rjwalters/loom/blob/main/docs/design/config-resolution-tiers.md"
        fi
        return 1
    fi
    ok "Fast-forwarded local ${DEFAULT_BRANCH} to origin/${DEFAULT_BRANCH} (${n} commit(s))."
    FF_SYNCED=true
    return 0
}
if ! sync_with_origin "$REPO_ROOT"; then
    exit 1
fi

# ---------- launchd ownership detection (macOS, mirrors loom-daemon-stop.sh #4042) ----------
# launchd is checked AHEAD of the .loom/.daemon.pid tier because the plist's
# KeepAlive:SuccessfulExit assigns a FRESH pid on every supervised relaunch, so
# the pid file goes stale after the first relaunch even for a launchd job that
# loom-daemon-start.sh itself started; a hand-bootstrapped daemon has no state
# files at all. Honors LOOM_DAEMON_LAUNCHD symmetrically with start/stop.sh so a
# --no-launchd install never reaches into the machine-global launchd domain.
# Shared domain resolver (#4130): gui/<uid> ↦ user/<uid>, sourced verbatim so
# update agrees with the domain the start put the job in.
#
# NOTE (#6009): this section — plus the systemd mirror below it and the
# DAEMON_MANAGER resolution that follows — was moved up from its original
# position (well after the staleness-detection block that used to sit here)
# so that the supervisor's OWN exec path can be resolved and compared
# BEFORE the staleness decision is made, not after it.
_LOOM_LAUNCHD_LIB_DIR="$(cd "$SCRIPT_DIR/../lib" 2>/dev/null && pwd)"
if [[ -r "$_LOOM_LAUNCHD_LIB_DIR/launchd-domain.sh" ]]; then
    # shellcheck source=../lib/launchd-domain.sh
    source "$_LOOM_LAUNCHD_LIB_DIR/launchd-domain.sh"
fi

IS_DARWIN=false
[[ "$(uname -s)" == "Darwin" ]] && IS_DARWIN=true
USE_LAUNCHD="$IS_DARWIN"
if [[ "${LOOM_DAEMON_LAUNCHD:-}" =~ ^(0|false|no)$ ]]; then
    USE_LAUNCHD=false
fi
DEFAULT_LAUNCHD_LABEL="com.rjwalters.loom-daemon"
LAUNCHD_LABEL="${LOOM_LAUNCHD_LABEL:-$DEFAULT_LAUNCHD_LABEL}"
# Resolve the domain ONLY when launchd interaction is on (#4130): probing
# `launchctl print gui/<uid>` when LOOM_DAEMON_LAUNCHD=0 would reach the
# machine-global launchd domain the disabled path must never touch (#4078). The
# placeholder is inert — launchd_job_loaded and the launchd restart path all
# short-circuit on USE_LAUNCHD, so it is never consumed when launchd is off.
if [[ "$USE_LAUNCHD" == "true" ]]; then
    LAUNCHD_SERVICE="$(resolve_launchd_domain)/${LAUNCHD_LABEL}"
else
    LAUNCHD_SERVICE="/${LAUNCHD_LABEL}"
fi
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"

launchd_job_loaded() {
    [[ "$USE_LAUNCHD" == "true" ]] || return 1
    command -v launchctl >/dev/null 2>&1 || return 1
    launchctl print "$LAUNCHD_SERVICE" >/dev/null 2>&1
}
launchd_job_pid() {
    launchctl print "$LAUNCHD_SERVICE" 2>/dev/null | awk -F'= ' '/^[[:space:]]*pid = /{gsub(/[^0-9]/, "", $2); print $2; exit}'
}

# ---------- systemd --user ownership detection (Linux, #4260 sub-issue C) ----------
# The Linux mirror of the launchd tier just above, checked at the SAME level
# (ahead of the pid-file tier): a `systemd --user` unit's pid also goes stale on
# every `Restart=on-success` relaunch (loom-daemon-start.sh #4268), so the pid
# file alone cannot answer "is it running, and how". Honors LOOM_DAEMON_SYSTEMD
# symmetrically with loom-daemon-start.sh --no-systemd / loom-daemon-stop.sh: a
# --no-systemd install must never invoke systemctl at all. Shared resolver
# (lib/systemd-user.sh, #4268) sourced verbatim so update agrees with the unit
# name start/stop resolve.
_LOOM_SYSTEMD_LIB_DIR="$(cd "$SCRIPT_DIR/../lib" 2>/dev/null && pwd)"
if [[ -r "$_LOOM_SYSTEMD_LIB_DIR/systemd-user.sh" ]]; then
    # shellcheck source=../lib/systemd-user.sh
    source "$_LOOM_SYSTEMD_LIB_DIR/systemd-user.sh"
fi

IS_LINUX_SYSTEMD=false
if ! [[ "${LOOM_DAEMON_SYSTEMD:-}" =~ ^(0|false|no)$ ]] \
    && declare -f is_linux_systemd >/dev/null 2>&1 && is_linux_systemd; then
    IS_LINUX_SYSTEMD=true
fi

# Resolved ONLY when systemd interaction is on -- mirrors the launchd guard just
# above; these calls are inert placeholders otherwise since every systemd
# function below short-circuits on IS_LINUX_SYSTEMD.
if [[ "$IS_LINUX_SYSTEMD" == "true" ]]; then
    SYSTEMD_UNIT="$(resolve_systemd_unit)"
    SYSTEMD_UNIT_PATH="$(resolve_systemd_unit_path)"
else
    SYSTEMD_UNIT="${LOOM_SYSTEMD_UNIT:-loom-daemon.service}"
    SYSTEMD_UNIT_PATH=""
fi

systemd_unit_loaded() {
    [[ "$IS_LINUX_SYSTEMD" == "true" ]] || return 1
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl --user is-active --quiet "$SYSTEMD_UNIT" 2>/dev/null \
        || systemctl --user is-enabled --quiet "$SYSTEMD_UNIT" 2>/dev/null
}
systemd_unit_pid() {
    systemctl --user show -p MainPID --value "$SYSTEMD_UNIT" 2>/dev/null
}
# systemd_unit_active_state / systemd_unit_result — the two `systemctl --user
# show` properties the #4950 verification/recovery logic below keys off of:
# ActiveState (e.g. active/inactive/failed) and Result (success/timeout/...).
# Mirror systemd_unit_pid's plain --value query shape.
systemd_unit_active_state() {
    systemctl --user show -p ActiveState --value "$SYSTEMD_UNIT" 2>/dev/null
}
systemd_unit_result() {
    systemctl --user show -p Result --value "$SYSTEMD_UNIT" 2>/dev/null
}

# Resolve which manager owns the running daemon: launchd, then systemd (both
# checked ahead of the pid-file tier -- their pids go stale on every supervised
# relaunch), then the .loom/.daemon.pid file (nohup/script-managed), or none.
# WAS_RUNNING is derived from this — a launchd- or systemd-loaded job counts as
# running regardless of pid-file state.
DAEMON_MANAGER="none"
WAS_RUNNING=false
if launchd_job_loaded; then
    DAEMON_MANAGER="launchd"
    WAS_RUNNING=true
elif systemd_unit_loaded; then
    DAEMON_MANAGER="systemd"
    WAS_RUNNING=true
elif [[ -f "$PID_FILE" ]]; then
    existing_pid=$(cat "$PID_FILE" 2>/dev/null || true)
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
        DAEMON_MANAGER="pidfile"
        WAS_RUNNING=true
    fi
fi

describe_manager() {
    case "$DAEMON_MANAGER" in
        launchd) echo "Running daemon manager: launchd (label ${LAUNCHD_LABEL})." ;;
        systemd) echo "Running daemon manager: systemd --user (unit ${SYSTEMD_UNIT})." ;;
        pidfile) echo "Running daemon manager: PID-file/nohup (.loom/.daemon.pid)." ;;
        *)       echo "Running daemon manager: not running." ;;
    esac
}

# ---------- supervisor's actual exec path (#6009) ----------
# DAEMON_MANAGER is now known. Resolve the path the detected supervisor will
# actually exec -- systemd's `ExecStart=`, launchd's `ProgramArguments[0]` --
# read from the supervisor's OWN persisted config, deliberately SEPARATE from
# `locate_daemon_bin()`'s PATH-based resolution below. The two are not
# guaranteed to agree (e.g. a stray/older `loom-daemon` earlier on PATH than
# the one the supervisor was actually pointed at), and PATH resolution alone
# has no way to notice that divergence on its own.
#
# Echoes the resolved absolute path on stdout and returns 0, or returns 1
# with no output when it cannot be determined (no supervisor, unreadable
# config, or the config's binary entry could not be parsed).
resolve_supervisor_bin() {
    case "$DAEMON_MANAGER" in
        launchd)
            [[ -r "$LAUNCHD_PLIST" ]] || return 1
            command -v /usr/libexec/PlistBuddy >/dev/null 2>&1 || return 1
            local bin
            bin="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$LAUNCHD_PLIST" 2>/dev/null)"
            [[ -n "$bin" ]] || return 1
            echo "$bin"
            ;;
        systemd)
            [[ -n "$SYSTEMD_UNIT_PATH" && -r "$SYSTEMD_UNIT_PATH" ]] || return 1
            local line
            # The LAST ExecStart= wins if the unit file somehow has more than
            # one (matches systemd's own override semantics); take only the
            # binary token, dropping any trailing args -- matches how
            # render_systemd_unit() in loom-daemon-start.sh renders it
            # (`ExecStart=<bin>`, no args).
            line="$(grep '^ExecStart=' "$SYSTEMD_UNIT_PATH" 2>/dev/null | tail -1)"
            [[ -n "$line" ]] || return 1
            echo "${line#ExecStart=}" | awk '{print $1}'
            ;;
        *)
            return 1
            ;;
    esac
}

SUPERVISOR_BIN=""
if [[ "$DAEMON_MANAGER" == "launchd" || "$DAEMON_MANAGER" == "systemd" ]]; then
    SUPERVISOR_BIN="$(resolve_supervisor_bin || true)"
fi

# ---------- staleness detection ----------
DAEMON_BIN=$(locate_daemon_bin "$REPO_ROOT")

# Staleness is compared against the binary the detected supervisor actually
# launches (SUPERVISOR_BIN, resolved just above), falling back to the
# PATH-resolved DAEMON_BIN only when no supervisor is detected or its exec
# path could not be determined (#6009) — PATH resolution alone cannot notice
# a supervisor pointed at a different absolute path than the PATH-resolved
# one.
STALENESS_BIN="$DAEMON_BIN"
STALENESS_SOURCE="PATH resolution"
if [[ -n "$SUPERVISOR_BIN" ]]; then
    STALENESS_BIN="$SUPERVISOR_BIN"
    STALENESS_SOURCE="${DAEMON_MANAGER} supervisor config"
fi

INSTALLED_COMMIT="unknown"
INSTALLED_VERSION=""
if [[ -n "$STALENESS_BIN" && -x "$STALENESS_BIN" ]]; then
    installed_version_output=$("$STALENESS_BIN" --version 2>/dev/null || true)
    extracted=$(extract_commit "$installed_version_output")
    [[ -n "$extracted" ]] && INSTALLED_COMMIT="$extracted"
    INSTALLED_VERSION=$(extract_version "$installed_version_output")
fi

SOURCE_COMMIT=$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo "unknown")
# Source tree's own VERSION file (#5517 keeps this in sync with Cargo.toml et
# al.) -- used below purely for the artifact-fetch gap-visibility note (#6010):
# comparing the newest release against the CURRENT source tree, not just
# against whatever happens to be installed already.
SOURCE_VERSION=""
[[ -r "$REPO_ROOT/VERSION" ]] && SOURCE_VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION" 2>/dev/null || true)"

echo "Installed binary: ${STALENESS_BIN:-<none found>} (commit ${INSTALLED_COMMIT}, resolved via ${STALENESS_SOURCE})"
# Divergence advisory (#6009 AC2). Compared through _lde_realpath so the two
# spellings of the SAME file (the common `/usr/local/bin/loom-daemon` symlink
# into `~/.local/bin/loom-daemon`) are not reported as a divergence — only a
# genuinely different file is.
if [[ -n "$SUPERVISOR_BIN" && -n "$DAEMON_BIN" ]]; then
    _sup_real="$(_lde_realpath "$SUPERVISOR_BIN")"
    _path_real="$(_lde_realpath "$DAEMON_BIN")"
    # _lde_realpath echoes "" for a path that does not exist (e.g. a supervisor
    # config still pointing at a deleted binary) — fall back to the raw spelling
    # so the comparison stays meaningful instead of collapsing to "" == "".
    [[ -n "$_sup_real" ]] || _sup_real="$SUPERVISOR_BIN"
    [[ -n "$_path_real" ]] || _path_real="$DAEMON_BIN"
else
    _sup_real=""; _path_real=""
fi
if [[ -n "$SUPERVISOR_BIN" && -n "$DAEMON_BIN" && "$_sup_real" != "$_path_real" ]]; then
    warn "PATH-resolved loom-daemon (${DAEMON_BIN}) is NOT the binary ${DAEMON_MANAGER} will actually launch (${SUPERVISOR_BIN}) — the staleness comparison above deliberately used the ${DAEMON_MANAGER}-managed binary, not the PATH one, since only the former is what the running daemon comes back on. If the PATH one is a leftover entry point, the stale-entry-point advisory below says whether --prune-stale-entry-points can remove it."
fi
echo "Source tree HEAD:  ${SOURCE_COMMIT}"
if [[ -n "$MACHINE_CHECKOUT" ]]; then
    echo "Source tree:       $REPO_ROOT (machine checkout, LOOM_MACHINE_CHECKOUT)"
fi
if [[ "$FF_SYNCED" == "true" ]]; then
    echo "Source tree:       fast-forwarded to origin/${DEFAULT_BRANCH} before this run (#4330)."
fi

UPDATE_NEEDED=false
if [[ -z "$STALENESS_BIN" ]]; then
    echo "No loom-daemon binary currently resolvable (checked: ${STALENESS_SOURCE}) — a build is needed. PATH search checked:"
    loom_daemon_bin_search_paths "$REPO_ROOT" | sed 's/^/  - /'
    UPDATE_NEEDED=true
elif [[ "$INSTALLED_COMMIT" == "unknown" || "$SOURCE_COMMIT" == "unknown" ]]; then
    warn "Could not determine one or both commits (installed=$INSTALLED_COMMIT, source=$SOURCE_COMMIT) — staleness unknown; treating as needing a rebuild to be safe."
    UPDATE_NEEDED=true
elif [[ "$INSTALLED_COMMIT" != "$SOURCE_COMMIT" ]]; then
    UPDATE_NEEDED=true
fi

# ---------- artifact-fetch resolution (Epic #4990 Phase 3, #5020) ----------
# Read-only resolution (no downloads yet — see fetch_and_verify_artifact()
# for the actual download/verify, which only runs once we're past --check
# and --dry-run below). When a newer release resolves for this host's
# platform, it takes precedence over the source-commit comparison above:
# ARTIFACT_MODE=true and UPDATE_NEEDED is forced true, regardless of what
# the local source tree looks like. Any resolution failure softly falls back
# to the source-commit-based UPDATE_NEEDED computed above — UNLESS the
# operator forced --fetch, checked further below once we know UPDATE_NEEDED.
ARTIFACT_MODE=false
ARTIFACT_TAG=""
ARTIFACT_VERSION=""
ARTIFACT_TARGET=""
ARTIFACT_BIN=""
ARTIFACT_COMMIT=""
ARTIFACT_VERSION_OUTPUT=""
ARTIFACT_FALLBACK_REASON=""
# Gap-visibility (#6010): true when the newest resolved release is behind the
# CURRENT source tree's VERSION file — independent of ARTIFACT_MODE, which
# only compares against the (possibly much older) INSTALLED_VERSION. This is
# the condition that made `--fetch` unusable fleet-wide once releases fell
# behind `main`: every host reported "not newer than installed" even though
# the release was also behind source, so the real gap was invisible until a
# forced `--fetch` hard-failed.
FETCH_RELEASE_BEHIND_SOURCE=false
if [[ "$FETCH_MODE" != "off" ]]; then
    if fetch_resolve_latest; then
        FETCH_VERSION_CMP="$(semver_compare "$FETCH_LATEST_VERSION" "${INSTALLED_VERSION:-0.0.0}")"
        # Strictly newer wins. An EQUAL version only wins under an explicit
        # --fetch: `--force` alone keeps its established meaning ("rebuild this
        # checkout even though it isn't stale"), which an operator running it
        # inside a source tree would be surprised to see silently turn into a
        # release download.
        if [[ "$FETCH_VERSION_CMP" == "1" ]] || { [[ "$FETCH_VERSION_CMP" == "0" ]] && [[ "$FETCH_MODE" == "force" ]]; }; then
            ARTIFACT_MODE=true
            ARTIFACT_TAG="$FETCH_LATEST_TAG"
            ARTIFACT_VERSION="$FETCH_LATEST_VERSION"
            ARTIFACT_TARGET="$FETCH_TARGET"
            UPDATE_NEEDED=true
            echo "Release artifact available: ${ARTIFACT_TAG} (target ${ARTIFACT_TARGET}) — preferring fetch over a local rebuild."
        else
            echo "Latest release ${FETCH_LATEST_TAG} (${FETCH_LATEST_VERSION}) is not newer than the installed version (${INSTALLED_VERSION:-unknown}) — nothing to fetch; falling back to the local source-tree comparison."
        fi

        if [[ -n "$SOURCE_VERSION" ]] && [[ "$(semver_compare "$FETCH_LATEST_VERSION" "$SOURCE_VERSION")" == "-1" ]]; then
            FETCH_RELEASE_BEHIND_SOURCE=true
            warn "Artifact path cannot reach current source: newest release ${FETCH_LATEST_TAG} (${FETCH_LATEST_VERSION}) is behind this source tree's VERSION (${SOURCE_VERSION}) — a forced '--fetch' will hard-fail until a release >= ${SOURCE_VERSION} is cut; '--no-fetch' (source build) remains available in the meantime."
        fi
    else
        ARTIFACT_FALLBACK_REASON="$FETCH_RESOLVE_REASON"
        warn "Artifact-fetch: ${ARTIFACT_FALLBACK_REASON} — falling back to the local source-build path."
    fi
fi

# ---------- --prune-stale-entry-points: standalone action, then exit (#5139) ----------
# Deliberately checked BEFORE the advisory below (skipping it, not running it
# first) — this flag exists precisely so an operator does not have to read the
# warning and act on it by hand; it performs the prune and reports the result
# directly. Never builds/provisions/restarts; combining it with other flags on
# the same invocation is not supported (--check et al. are simply ignored once
# this fires).
if [[ "$PRUNE_STALE" == "true" ]]; then
    if prune_stale_entry_points "$DAEMON_BIN"; then
        exit 0
    else
        exit 1
    fi
fi

# Advisory only, and deliberately placed here so it is reported on EVERY path —
# --check, --dry-run, an up-to-date no-op, and a full rebuild alike. A stale
# entry point is invisible precisely when the daemon looks healthy (#4079).
warn_stale_entry_points "$DAEMON_BIN"

# ---------- idle-shutdown cron-guard post-update notice (#4697) ----------
#
# THE INCIDENT THIS EXISTS FOR: a remote worker was updated via this script —
# the rebuild + supervised restart succeeded onto the new binary — and ~15
# minutes later the host powered itself off. Nothing in the update flow
# warned that the "successful" update was landing on a host about to
# evaporate: the STAGE-2 cron guard `fleet add-worker --idle-shutdown-minutes`
# installs (`render_idle_shutdown()` in loom-daemon/src/fleet/add_worker.rs,
# NOT `autonomous.idleExit` stage 1 — that daemon-level exit is self-defeating
# under `Restart=on-success` systemd/launchd supervision, since the supervisor
# immediately relaunches it) fired once the freshly-relaunched, idle daemon
# crossed the configured window, and powered the WHOLE HOST off — SSH,
# tailnet, everything.
#
# This is purely advisory: it never disables/touches the guard, never changes
# this script's exit code, and is silent when no guard is installed
# (LOOM_SKIP_IDLE_SHUTDOWN_NOTICE=1 also suppresses it for scripted/quiet
# use). The idle-shutdown guard's own design (#3998/#4477) is correct and out
# of scope here — the gap this closes is purely operator awareness at the
# moment a "successful" update is reported.
IDLE_SHUTDOWN_GUARD_SCRIPT="$HOME/.local/bin/loom-idle-shutdown.sh"

idle_shutdown_notice() {
    [[ "${LOOM_SKIP_IDLE_SHUTDOWN_NOTICE:-0}" =~ ^(1|true|yes)$ ]] && return 0
    command -v crontab >/dev/null 2>&1 || return 0
    crontab -l 2>/dev/null | grep -q 'loom-idle-shutdown' || return 0

    local minutes=""
    if [[ -r "$IDLE_SHUTDOWN_GUARD_SCRIPT" ]]; then
        minutes="$(grep -oE 'LIMIT=[0-9]+' "$IDLE_SHUTDOWN_GUARD_SCRIPT" 2>/dev/null \
            | head -n1 | cut -d= -f2)"
    fi

    if [[ -n "$minutes" ]]; then
        warn "Heads up: this host has an idle-shutdown cron guard installed (fleet add-worker --idle-shutdown-minutes ${minutes}) — after ~${minutes} idle minute(s) it POWERS THE WHOLE HOST OFF (SSH/tailnet included), not just this daemon. This is expected/by-design (#3998/#4477), not a fault in this update. Wake path (provider console/CLI restart; Loom never calls a cloud CLI itself) and tailnet-identity/re-registration notes: daemon-reference.md, 'fleet add-worker' step 9 (idle-shutdown)."
    else
        warn "Heads up: this host has an idle-shutdown cron guard installed (crontab holds a loom-idle-shutdown entry, but the configured window could not be read from $IDLE_SHUTDOWN_GUARD_SCRIPT) — it WILL power the whole host off after some idle window. This is expected/by-design (#3998/#4477), not a fault in this update. See daemon-reference.md, 'fleet add-worker' step 9 (idle-shutdown), for the wake path."
    fi
}

# print_final_installed_line <commit> — the AC4 "final installed line": states
# the built/installed commit AND whether it matches origin/<default-branch> at
# build time. Uses ORIGIN_COMMIT resolved by sync_with_origin above (no
# re-fetch). Prints an honest "unknown" comparison when the default branch or
# origin commit could not be resolved (offline, no origin remote, etc.) rather
# than silently omitting the currency claim. Also where the #4697 idle-shutdown
# notice fires — every successful/"already up to date" exit path funnels
# through this one function, so the notice is reported consistently without
# duplicating the call at each of this script's several exit points.
print_final_installed_line() {
    local commit="$1"
    # Artifact-fetch mode (#5020): the installed binary is a RELEASE build, so
    # comparing its commit against origin/<default-branch>'s tip is the wrong
    # currency claim — a released commit is normally BEHIND the branch tip and
    # saying "does NOT match" about it would be actively misleading. Report the
    # release identity instead.
    if [[ "${ARTIFACT_MODE:-false}" == "true" ]]; then
        echo "Installed: release ${ARTIFACT_TAG} (${ARTIFACT_VERSION}${commit:+, commit ${commit}}) for target ${ARTIFACT_TARGET} — fetched artifact, checksum verified"
        idle_shutdown_notice
        return 0
    fi
    if [[ -z "$DEFAULT_BRANCH" || "$ORIGIN_COMMIT" == "unknown" ]]; then
        echo "Installed: ${commit} (currency vs origin/<default-branch> unknown — unresolvable or unreachable)"
    elif [[ "$commit" == "$ORIGIN_COMMIT" ]]; then
        echo "Installed: ${commit} (matches origin/${DEFAULT_BRANCH})"
    else
        echo "Installed: ${commit} (origin/${DEFAULT_BRANCH} is at ${ORIGIN_COMMIT} — does NOT match; built from a checkout that was behind or diverged, e.g. --allow-stale)"
    fi
    idle_shutdown_notice
}

# ---------- verify a launchd restart actually relaunched the job (#4232) ----------
# THE PROBLEM: the launchd branch below used to treat a successful `restart`
# ack (the RUNNING binary accepting the IPC request, exit 0) as success and
# exit 0 immediately — fire-and-forget. On 2026-07-28 that ack was honest (the
# supervised daemon exited 0 per its #4054 contract) but launchd's own
# KeepAlive:SuccessfulExit relaunch never fired, so the script reported success
# while the daemon silently stayed down for ~4 minutes until an operator ran
# `launchctl kickstart` by hand. This closes that gap: verify a NEW pid before
# reporting success, and self-heal via `kickstart` when launchd doesn't.
#
# wait_for_new_launchd_pid <pre_pid> <timeout_secs> <interval_secs> — poll
# `launchd_job_pid` until it reports a pid that is BOTH different from
# <pre_pid> AND alive (`kill -0`), for up to <timeout_secs>. A pid that merely
# differs but is already dead (a race artifact) — or that still equals
# <pre_pid> (the old process lingering mid-teardown during the poll window) —
# must NEVER be mistaken for a successful relaunch. On success, echoes the new
# pid on stdout and returns 0; on timeout, returns 1 with no output.
# <interval_secs> may be fractional (e.g. 0.2), matching `sleep`'s own support.
wait_for_new_launchd_pid() {
    local pre_pid="$1" timeout_secs="$2" interval_secs="$3"
    local deadline cur_pid
    deadline=$(( $(date +%s) + timeout_secs ))
    while true; do
        cur_pid="$(launchd_job_pid)"
        if [[ -n "$cur_pid" && "$cur_pid" != "$pre_pid" ]] && kill -0 "$cur_pid" 2>/dev/null; then
            echo "$cur_pid"
            return 0
        fi
        if (( $(date +%s) >= deadline )); then
            return 1
        fi
        sleep "$interval_secs"
    done
}

# log_launchd_diagnostics — dump `launchctl print`'s current state as a
# diagnostic breadcrumb (state / last exit status) when a relaunch cannot be
# verified, so an operator (or the PR/issue this failure is reported to) has
# the exact evidence needed to tell "launchd never relaunched" apart from "the
# daemon crashed immediately after relaunching" (#4232).
log_launchd_diagnostics() {
    warn "launchctl print $LAUNCHD_SERVICE diagnostic snapshot:"
    local line
    while IFS= read -r line; do
        warn "  $line"
    done < <(launchctl print "$LAUNCHD_SERVICE" 2>&1)
}

# ---------- verify a systemd restart actually relaunched the unit (#4950) ----------
# THE PROBLEM (the systemd mirror of #4232's launchd gap): the systemd branch
# below used to treat a successful `restart` ack (the RUNNING binary accepting
# the IPC request and exiting 0, per #4054) as success and exit 0 immediately —
# fire-and-forget, with NO verification that `Restart=on-success` actually
# relaunched the unit. On 2026-08-02 that ack was honest (the daemon exited 0),
# but the unit's own STOP transition (systemd sends SIGTERM to the main process
# as part of processing the exit, then waits up to `TimeoutStopSec` for the
# unit to fully settle) exceeded the default 90s `TimeoutStopSec` — likely
# because the LIVE, already-installed unit predated #4862's `KillMode=mixed`
# fix (a plain `restart` IPC request never re-renders the unit; only
# `--relaunch` does — see perform_systemd_relaunch below) and lingering
# `claude`/`tee`/`sleep` sweep-worker children in the same cgroup were reaped
# only after the full timeout. systemd then marked the unit `failed (Result:
# timeout)`, and `Restart=on-success` does NOT match `Result=timeout` (only
# `Result=success` triggers it — see the `Restart=` table in
# systemd.service(5)), so the relaunch silently never fired and the host was
# daemonless until an operator ran `systemctl --user reset-failed <unit> &&
# systemctl --user start <unit>` by hand. This closes that gap: verify a NEW,
# live MainPID before reporting success, and self-heal via the exact
# reset-failed+start recovery when the unit lands in `failed`.
#
# wait_for_new_systemd_pid <pre_pid> <timeout_secs> <interval_secs> — poll
# `systemd_unit_pid` (the unit's MainPID) until it reports a pid that is BOTH
# different from <pre_pid> AND alive (`kill -0`), for up to <timeout_secs>. A
# reported "0" (not running) never counts, and neither does a pid that merely
# differs but is already dead (a race artifact) or still equals <pre_pid> (the
# old process lingering mid-teardown during the poll window). On success,
# echoes the new pid on stdout and returns 0; on timeout, returns 1 with no
# output. <interval_secs> may be fractional (e.g. 0.2), matching `sleep`'s own
# support. Mirrors wait_for_new_launchd_pid above byte-for-byte in contract.
wait_for_new_systemd_pid() {
    local pre_pid="$1" timeout_secs="$2" interval_secs="$3"
    local deadline cur_pid
    deadline=$(( $(date +%s) + timeout_secs ))
    while true; do
        cur_pid="$(systemd_unit_pid)"
        if [[ -n "$cur_pid" && "$cur_pid" != "0" && "$cur_pid" != "$pre_pid" ]] \
            && kill -0 "$cur_pid" 2>/dev/null; then
            echo "$cur_pid"
            return 0
        fi
        if (( $(date +%s) >= deadline )); then
            return 1
        fi
        sleep "$interval_secs"
    done
}

# wait_for_systemd_stop_settle <timeout_secs> <interval_secs> — poll the unit's
# ActiveState until it SETTLES out of a transitional state (deactivating /
# activating / reloading) into a terminal one (failed / inactive / active), for
# up to <timeout_secs>. Echoes the settled ActiveState and returns 0; on timeout
# echoes the last-observed (still-transitional) state and returns 1.
#
# WHY (#5119). On a busy host a `loom-daemon restart` exits the daemon 0, but the
# unit's stop job can sit in `deactivating (stop-sigterm)` for the full
# TimeoutStopSec while it SIGTERMs — then SIGKILLs — the sweep/role children still
# in the service cgroup. A STALE unit (one rendered before #4862's KillMode=mixed
# fix — the exact 2026-08-03 incident) drags that out to systemd's 90s default,
# far past the #4950 pid poll's default 30s. The pre-#5119 code read ActiveState
# ONCE right after that poll expired, saw `deactivating` (not yet `failed`), and
# fell through to "refusing to guess" — leaving the daemon down until an operator
# ran reset-failed+start by hand. This helper lets the recovery WAIT for the stop
# transition to complete so it can act on the settled state instead of a
# mid-teardown snapshot.
wait_for_systemd_stop_settle() {
    local timeout_secs="$1" interval_secs="$2"
    local deadline state
    deadline=$(( $(date +%s) + timeout_secs ))
    while true; do
        state="$(systemd_unit_active_state)"
        case "$state" in
            deactivating|activating|reloading|deactivating-sigterm|deactivating-sigkill)
                # still mid-transition — keep waiting
                ;;
            *)
                echo "$state"
                return 0
                ;;
        esac
        if (( $(date +%s) >= deadline )); then
            echo "$state"
            return 1
        fi
        sleep "$interval_secs"
    done
}

# log_systemd_diagnostics — dump `systemctl --user status`'s current state
# (including the `Active:`/`Result:` line the incident's journal excerpt was
# read off of) as a diagnostic breadcrumb when a relaunch cannot be verified,
# mirroring log_launchd_diagnostics above.
log_systemd_diagnostics() {
    warn "systemctl --user status $SYSTEMD_UNIT diagnostic snapshot:"
    local line
    while IFS= read -r line; do
        warn "  $line"
    done < <(systemctl --user status "$SYSTEMD_UNIT" --no-pager --full 2>&1)
}

# ---------- drain-restart flag passthrough (Issue #5138) ----------
# build_restart_invoke_args -- populate the global RESTART_INVOKE_ARGS array
# with the `restart` subcommand plus the drain passthrough flags (--drain,
# --timeout, --force-after-timeout), shared by the launchd and systemd
# supervised-restart branches below. All drain SEMANTICS (pause dispatch,
# wait for in-flight sweeps, fail-safe refusal on timeout) already live in
# `loom-daemon restart --drain` (#4090) -- this script only decides WHETHER to
# pass --drain (see the DRAIN default-selection block near DAEMON_MANAGER
# resolution below) and threads the operator's own --timeout/
# --force-after-timeout straight through, unchanged.
build_restart_invoke_args() {
    RESTART_INVOKE_ARGS=(restart)
    if [[ "$DRAIN" == "true" ]]; then
        RESTART_INVOKE_ARGS+=(--drain)
        [[ -n "$DRAIN_TIMEOUT" ]] && RESTART_INVOKE_ARGS+=(--timeout "$DRAIN_TIMEOUT")
        [[ "$FORCE_AFTER_TIMEOUT" == "true" ]] && RESTART_INVOKE_ARGS+=(--force-after-timeout)
    fi
}

# ---------- pending-roll detection (Issue #6007) ----------
# drain_roll_still_armed -- 0 when the still-running daemon reports a drain STILL
# in progress after our pid poll expired. Our poll window is the drain's own
# timeout + 60s (DRAIN_POLL_SECS above), so by here the deadline has passed: a
# pre-#6007 daemon would have cleared the flag and be reporting
# `draining: false`. Still-draining therefore means the daemon RETAINED the roll
# — on a #6007+ daemon a relaunch drain that misses its deadline keeps dispatch
# paused and re-arms itself, so the restart lands on its own once the in-flight
# set reaches zero. That changes the exit-8 advice completely: "re-run once the
# sweeps finish" is exactly the operator babysitting #6007 removed, and on a busy
# host re-running reproduces the same outcome.
#
# Deliberately conservative: any failure to reach or parse the status (pre-#6007
# daemon, no `--json`, absent binary) returns non-zero, so the message falls back
# to the historical wording rather than promising a convergence the running
# daemon may not implement. Uses `grep`, not `jq` (never assume jq on a worker).
drain_roll_still_armed() {
    local bin="${1:-$PROVISION_TARGET}"
    [[ -x "$bin" ]] || return 1
    local json
    json="$("$bin" status --json 2>/dev/null)" || return 1
    [[ -n "$json" ]] || return 1
    printf '%s' "$json" \
        | tr -d ' \n' \
        | grep -q '"drain":{[^}]*"draining":true'
}

# ---------- re-render + relaunch on a refused restart (#4118) ----------
# The exit-6 fallback USED to tell the operator to `launchctl bootstrap` the
# EXISTING plist. That plist is stale by construction (it is the pre-#4077 file
# that caused the refused restart) — bootstrapping it relaunches WITHOUT
# KeepAlive:SuccessfulExit and WITHOUT LOOM_DAEMON_SUPERVISOR, so the next roll
# refuses identically, forever. The correct fix is to RE-RENDER the plist via
# loom-daemon-start.sh (which hardcodes the two supervised keys), preserving
# the live plist's autonomy/auth env.
#
# `launchctl bootout` itself no longer needs to be avoided for sweep safety
# (#5081): a bare bootout does not kill in-flight sweeps on a current build —
# see the top-of-file note near #4118 for why. This function still stops the
# old daemon with a graceful SIGTERM rather than calling bootout directly,
# both to avoid double-tearing-down the job (loom-daemon-start.sh's own
# launchd block below already bootouts the loaded job before re-bootstrapping)
# and so a daemon that predates this fix keeps behaving safely either way.

# harvest_plist_env is defined in lib/daemon-env-harvest.sh (#4581, sourced
# near the top of this script) — shared with scripts/loom's loom_cmd_restart()
# bare-exec fallback so both call sites apply the identical harvest-and-
# preserve pattern from one source instead of two drifting copies.

# perform_relaunch <plist> <service> — re-render the LaunchAgent and relaunch it
# under launchd supervision, preserving the live plist's autonomy/auth env.
# Invoked ONLY from the exit-6 fallback when the operator opted in (--relaunch /
# LOOM_DAEMON_UPDATE_RELAUNCH=1), so the sweep-disrupting relaunch is a consented
# action, never silent. Returns loom-daemon-start.sh's exit code (or 6 if the env
# harvest fails — refusing to relaunch into a silently-narrowed env).
perform_relaunch() {
    local plist="$1"
    echo "--relaunch: re-rendering the LaunchAgent and relaunching under launchd supervision."

    # 1. Preserve the live plist's autonomy/auth env across the re-render.
    local harvested
    if ! harvested=$(harvest_plist_env "$plist"); then
        err "Refusing to relaunch: could not read the live plist's EnvironmentVariables."
        err "Relaunching now would silently narrow the autonomy flags to FLAGS-OFF defaults (#4011) — aborting."
        return 6
    fi
    local k v64 count=0
    while IFS=$'\t' read -r k v64; do
        [[ -z "$k" ]] && continue
        export "$k=$(printf '%s' "$v64" | base64 --decode)"
        count=$((count + 1))
    done <<< "$harvested"
    echo "Preserved ${count} LOOM_*/token env var(s) from the live plist across the re-render (PATH/HOME/LOOM_DAEMON_SUPERVISOR excluded by design)."

    # 2. Stop the old daemon GRACEFULLY with SIGTERM rather than calling
    #    `launchctl bootout` directly here (bootout itself no longer kills
    #    in-flight sweeps, #5081 — this is belt-and-braces against a double
    #    bootout, since loom-daemon-start.sh's launchd block below bootouts the
    #    loaded job again before re-bootstrapping). kill -TERM makes the daemon
    #    exit non-zero, so the stale plist's KeepAlive=false does not relaunch
    #    it — start.sh below installs the fresh, supervised plist and
    #    bootstraps the new process (with its own settle/retry/verify, #5081).
    local daemon_pid
    daemon_pid=$(launchd_job_pid)
    if [[ -n "$daemon_pid" ]] && kill -0 "$daemon_pid" 2>/dev/null; then
        echo "Sending SIGTERM to the running daemon (pid ${daemon_pid}) — sweep children reparent and keep working; in-flight sweeps are not otherwise at risk here (bootout no longer kills them either, #5081)."
        kill -TERM "$daemon_pid" 2>/dev/null || true
        local _waited
        for _waited in 1 2 3 4 5; do
            kill -0 "$daemon_pid" 2>/dev/null || break
            sleep 1
        done
    fi

    # 3. Re-render + bootstrap via loom-daemon-start.sh. It hardcodes
    #    KeepAlive:{SuccessfulExit:true} + LOOM_DAEMON_SUPERVISOR=launchd, and
    #    harvests the LOOM_* env we just re-exported. In launchd mode the plist's
    #    EnvironmentVariables — not .daemon.flags — is the durable config, so no
    #    flags are passed here.
    echo "Invoking ${START_SCRIPT} to re-render the supervised plist and relaunch."
    "$START_SCRIPT"
}

# harvest_unit_env is defined in lib/daemon-env-harvest.sh (#4581, sourced
# near the top of this script) — see the harvest_plist_env pointer comment
# above perform_relaunch for why it moved.

# perform_systemd_relaunch <unit_path> <unit> — re-render the systemd --user
# unit and relaunch it under supervision, preserving the live unit's
# autonomy/auth env. The systemd mirror of perform_relaunch above. Invoked ONLY
# from the exit-6 fallback when the operator opted in (--relaunch /
# LOOM_DAEMON_UPDATE_RELAUNCH=1). Returns loom-daemon-start.sh's exit code (or 6
# if the env harvest fails — refusing to relaunch into a silently-narrowed env).
#
# Note on "systemctl --user restart": re-rendering the unit file alone does not
# make an ALREADY-ACTIVE unit pick up the new binary/env -- `enable --now` on an
# active unit is a no-op start, not a restart. So this SIGTERMs the running
# daemon first (Restart=on-success does not fire on a signal death, mirroring
# launchd's KeepAlive:SuccessfulExit), leaving the unit inactive, and THEN
# invokes loom-daemon-start.sh to re-render + `enable --now` it -- which, against
# an inactive unit, genuinely starts a fresh process. This achieves the same
# effect as `systemctl --user restart <unit>` while reusing render_systemd_unit
# rather than duplicating it here.
perform_systemd_relaunch() {
    local unit_path="$1" unit="$2"
    echo "--relaunch: re-rendering the systemd --user unit ${unit} and relaunching under supervision."

    # 1. Preserve the live unit's autonomy/auth env across the re-render.
    local harvested
    if ! harvested=$(harvest_unit_env "$unit_path"); then
        err "Refusing to relaunch: could not read the live unit's Environment= values."
        err "Relaunching now would silently narrow the autonomy flags to FLAGS-OFF defaults (#4011) — aborting."
        return 6
    fi
    local k v count=0
    while IFS=$'\t' read -r k v; do
        [[ -z "$k" ]] && continue
        export "$k=$v"
        count=$((count + 1))
    done <<< "$harvested"
    echo "Preserved ${count} LOOM_*/token env var(s) from the live unit across the re-render (PATH/HOME/LOOM_DAEMON_SUPERVISOR excluded by design)."

    # 2. Stop the old daemon GRACEFULLY so its sweep children reparent and keep
    #    working, instead of `systemctl stop` (which SIGKILLs the whole cgroup
    #    after TimeoutStopSec, tearing down sweep children the same way a
    #    launchd bootout would). kill -TERM makes the daemon exit by signal, so
    #    Restart=on-success does not relaunch it -- start.sh below installs the
    #    fresh, supervised unit and enables + starts the new process.
    local daemon_pid
    daemon_pid=$(systemd_unit_pid)
    if [[ -n "$daemon_pid" && "$daemon_pid" != "0" ]] && kill -0 "$daemon_pid" 2>/dev/null; then
        echo "Sending SIGTERM to the running daemon (pid ${daemon_pid}) — sweep children reparent and keep working (NOT 'systemctl stop', which tears down the whole cgroup)."
        kill -TERM "$daemon_pid" 2>/dev/null || true
        local _waited
        for _waited in 1 2 3 4 5; do
            kill -0 "$daemon_pid" 2>/dev/null || break
            sleep 1
        done
    fi

    # 3. Re-render + enable via loom-daemon-start.sh. It hardcodes
    #    Restart=on-success + LOOM_DAEMON_SUPERVISOR=systemd, and harvests the
    #    LOOM_* env we just re-exported. In systemd mode the unit's
    #    Environment= lines — not .daemon.flags — are the durable config, so no
    #    flags are passed here.
    echo "Invoking ${START_SCRIPT} to re-render the supervised unit and relaunch."
    "$START_SCRIPT"
}

# ---------- drain-restart default selection (Issue #5138) ----------
# On systemd an IMMEDIATE (non-drained) restart is actively destructive
# (#5119): the daemon exits 0, but its role-run/sweep children remain in the
# unit's cgroup, so the stop job can sit in `deactivating` past
# TimeoutStopSec while systemd SIGKILLs them, landing the unit in `failed`
# with `Restart=on-success` never firing — a real outage, not merely lossy
# telemetry. So DRAIN now defaults to true on systemd unless the operator
# opts out with --restart-now. On launchd/pidfile an immediate restart is
# "only" lossy (#5084: sweeps adopted across the restart never export
# sweep.completed/sweep.outcome telemetry), so the pre-#5138 default
# (immediate restart) is unchanged there — --drain opts IN explicitly.
DRAIN_DEFAULTED=false
if [[ "$RESTART_NOW" != "true" && "$DRAIN" != "true" && "$DAEMON_MANAGER" == "systemd" ]]; then
    DRAIN=true
    DRAIN_DEFAULTED=true
fi

# --drain (explicit or the systemd default above) only has an effect through
# the launchd/systemd supervised `restart` IPC path below — the bare
# pid-file/nohup branch stops+starts directly and never speaks it. Warn
# (never fail) so an operator who passed --drain on an unsupervised host
# isn't left wondering why nothing drained.
if [[ "$DRAIN" == "true" && "$DAEMON_MANAGER" != "launchd" && "$DAEMON_MANAGER" != "systemd" ]]; then
    warn "--drain (or LOOM_DAEMON_UPDATE_DRAIN=1) was given, but loom-daemon is not launchd- or systemd-managed — there is no supervisor to relaunch it, so drain mode has no effect here. Proceeding with the ordinary stop+start restart."
fi

# Poll window for a drain-mode restart to actually relaunch (#5138): a drain
# can legitimately take up to its own --timeout (daemon default 1800s) before
# it either relaunches or hits the fail-safe, so the fast ~30s
# LOOM_DAEMON_RESTART_POLL_SECS default used for an immediate restart would
# false-negative on every real drain. Mirrors fleet/drain.rs's own
# WAIT_EXIT_GRACE_SECS=60 pattern for the same class of wait.
if [[ "$DRAIN" == "true" ]]; then
    DRAIN_POLL_SECS="${LOOM_DAEMON_DRAIN_POLL_SECS:-$(( ${DRAIN_TIMEOUT:-1800} + 60 ))}"
fi

# ---------- --check: report only, no writes ----------
if [[ "$CHECK_ONLY" == "true" ]]; then
    describe_manager
    if [[ "$FETCH_RELEASE_BEHIND_SOURCE" == "true" ]]; then
        warn "Release gap: installed ${INSTALLED_VERSION:-unknown}, newest release ${FETCH_LATEST_VERSION:-unknown}, source ${SOURCE_VERSION:-unknown} — the artifact-fetch path cannot reach current source until a release >= ${SOURCE_VERSION} is cut."
    fi
    if [[ "$UPDATE_NEEDED" == "true" ]]; then
        if [[ "$ARTIFACT_MODE" == "true" ]]; then
            warn "Update available via release artifact ${ARTIFACT_TAG} (installed=${INSTALLED_VERSION:-unknown}, latest=${ARTIFACT_VERSION}, target=${ARTIFACT_TARGET})."
        else
            warn "Update available (installed=${INSTALLED_COMMIT}, source=${SOURCE_COMMIT})."
        fi
        exit 3
    fi
    ok "loom-daemon binary is already up to date with source HEAD (${SOURCE_COMMIT})."
    print_final_installed_line "$SOURCE_COMMIT"
    exit 0
fi

if [[ "$FORCE" == "true" && "$UPDATE_NEEDED" == "false" ]]; then
    echo "--force given: rebuilding even though the binary already matches source HEAD."
    UPDATE_NEEDED=true
fi

if [[ "$UPDATE_NEEDED" == "false" ]]; then
    # UPDATE_NEEDED compares the installed binary against the CURRENT HEAD. When
    # the checkout is behind origin, a real run fast-forwards first, so HEAD --
    # and therefore that comparison -- would change before anything is built.
    # Reporting a bare "Nothing to do" here would hide the pending ff-sync from
    # exactly the mode whose job is to print the plan, so --dry-run surfaces it
    # before exiting.
    if [[ "$DRY_RUN" == "true" && "$ALLOW_STALE" != "true" \
          && -n "$DEFAULT_BRANCH" && "$ORIGIN_BEHIND_COUNT" -gt 0 ]]; then
        echo "[dry-run] Plan includes fast-forwarding local ${DEFAULT_BRANCH} to origin/${DEFAULT_BRANCH} (${ORIGIN_BEHIND_COUNT} commit(s) behind) before building; the up-to-date check below is against the CURRENT HEAD and may change once that ff-merge applies."
    fi
    ok "loom-daemon binary is already up to date with source HEAD (${SOURCE_COMMIT}). Nothing to do."
    print_final_installed_line "$SOURCE_COMMIT"
    exit 0
fi

# An update IS needed at this point. --fetch (or LOOM_DAEMON_UPDATE_FETCH=1)
# means "I know a release artifact should exist; don't silently fall back to
# building from source" — refuse rather than mask a resolution failure.
if [[ "$FETCH_MODE" == "force" && "$ARTIFACT_MODE" != "true" ]]; then
    err "--fetch (or LOOM_DAEMON_UPDATE_FETCH=1) was given but no usable release artifact was resolved${ARTIFACT_FALLBACK_REASON:+ (${ARTIFACT_FALLBACK_REASON})}."
    if [[ "$FETCH_RELEASE_BEHIND_SOURCE" == "true" ]]; then
        err "Cause: the newest release (${FETCH_LATEST_VERSION:-unknown}) is behind this source tree's VERSION (${SOURCE_VERSION:-unknown}) — no release has been cut yet for the current tree (#6010)."
    fi
    err "Refusing to silently fall back to a source build; re-run without --fetch to allow that, or resolve the cause above."
    exit 1
fi

# ---------- resolve the restart plan up front (read-only; safe for --dry-run) ----------
# WAS_RUNNING + DAEMON_MANAGER were resolved above (launchd checked ahead of the
# pid file). The flags below are only consulted for the pid-file/nohup restart
# path — a launchd-managed restart replays flags from the plist, not this file.
RESTART_ARGS=()
FLAGS_SOURCE="none (defaulting to FLAGS-OFF bare restart)"
if [[ -f "$FLAGS_FILE" ]]; then
    FLAGS_SOURCE="$FLAGS_FILE"
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        RESTART_ARGS+=("$line")
    done < "$FLAGS_FILE"
fi

DEST_DIR="${LOOM_DAEMON_BIN_DIR:-$HOME/.local/bin}"
PROVISION_TARGET="${LOOM_DAEMON_BIN:-$DEST_DIR/loom-daemon}"

if [[ "$DRY_RUN" == "true" ]]; then
    echo
    if [[ "$ARTIFACT_MODE" == "true" ]]; then
        echo "[dry-run] Would fetch + verify release artifact ${ARTIFACT_TAG} (target ${ARTIFACT_TARGET}) from ${FETCH_REPO_SLUG} — checksum unconditional, signature verified when present. No 'cargo build' would run."
    else
        if [[ "$ALLOW_STALE" == "true" ]]; then
            echo "[dry-run] --allow-stale given: would build the current checkout as-is (no fetch/ff-merge)."
        elif [[ -n "$DEFAULT_BRANCH" && "$ORIGIN_BEHIND_COUNT" -gt 0 ]]; then
            echo "[dry-run] Plan includes fast-forwarding local ${DEFAULT_BRANCH} to origin/${DEFAULT_BRANCH} (${ORIGIN_BEHIND_COUNT} commit(s) behind) before building; would abort instead of building stale if the ff-merge cannot apply."
        fi
        if [[ -n "$ARTIFACT_FALLBACK_REASON" ]]; then
            echo "[dry-run] Artifact-fetch was not used: ${ARTIFACT_FALLBACK_REASON}."
        fi
        echo "[dry-run] Would run: (cd $DAEMON_DIR && cargo build --release --message-format=json-render-diagnostics)"
    fi
    echo "[dry-run] Would provision the fresh binary to: $PROVISION_TARGET"
    build_restart_invoke_args
    if [[ "$NO_RESTART" == "true" ]]; then
        echo "[dry-run] --no-restart given: would leave the running daemon (if any) untouched."
    elif [[ "$DAEMON_MANAGER" == "launchd" ]]; then
        if [[ "$DRAIN" == "true" ]]; then
            echo "[dry-run] loom-daemon is launchd-managed (label ${LAUNCHD_LABEL}) — would restart via '$PROVISION_TARGET ${RESTART_INVOKE_ARGS[*]}' (Issue #5138, the #4090 drain primitive): pauses dispatch, waits for in-flight sweeps to finish (preserving sweep.completed/sweep.outcome telemetry, #5084), THEN relaunches. A drain timeout without --force-after-timeout leaves the pre-update binary running (fail-safe, exit 8) instead of cancelling sweeps."
        else
            echo "[dry-run] loom-daemon is launchd-managed (label ${LAUNCHD_LABEL}) — would restart via '$PROVISION_TARGET restart' (the #4077 supervised primitive); .daemon.flags is NOT consulted (the plist's EnvironmentVariables carries the equivalent config)."
        fi
    elif [[ "$DAEMON_MANAGER" == "systemd" ]]; then
        if [[ "$DRAIN" == "true" ]]; then
            if [[ "$DRAIN_DEFAULTED" == "true" ]]; then
                echo "[dry-run] loom-daemon is systemd-managed (unit ${SYSTEMD_UNIT}) — would restart via '$PROVISION_TARGET ${RESTART_INVOKE_ARGS[*]}', the systemd DEFAULT since Issue #5138 (an immediate restart there can kill in-flight sweeps and land the unit in 'failed', #5119): pauses dispatch, waits for in-flight sweeps to finish, THEN relaunches. Pass --restart-now to opt back into an immediate (non-drained) restart."
            else
                echo "[dry-run] loom-daemon is systemd-managed (unit ${SYSTEMD_UNIT}) — would restart via '$PROVISION_TARGET ${RESTART_INVOKE_ARGS[*]}' (Issue #5138, the #4090 drain primitive): pauses dispatch, waits for in-flight sweeps to finish, THEN relaunches. A drain timeout without --force-after-timeout leaves the pre-update binary running (fail-safe, exit 8) instead of cancelling sweeps."
            fi
        else
            echo "[dry-run] loom-daemon is systemd-managed (unit ${SYSTEMD_UNIT}) — --restart-now given: would restart IMMEDIATELY (non-drained) via '$PROVISION_TARGET restart', which can kill in-flight sweeps and land the unit in 'failed' if any are running (#5119)."
        fi
    elif [[ "$WAS_RUNNING" == "true" ]]; then
        echo "[dry-run] Would stop + restart loom-daemon with flags from ${FLAGS_SOURCE}: ${RESTART_ARGS[*]:-<none>}"
    else
        echo "[dry-run] loom-daemon is not currently running — would NOT start it (this script never widens FLAGS-OFF by starting autonomy that wasn't already running)."
    fi
    exit 0
fi

# ---------- rebuild (source) OR fetch (artifact, Epic #4990 Phase 3, #5020) ----------
if [[ "$ARTIFACT_MODE" == "true" ]]; then
    echo
    echo "Fetching loom-daemon release artifact ${ARTIFACT_TAG} (target ${ARTIFACT_TARGET}) from ${FETCH_REPO_SLUG}..."
    # fetch_and_verify_artifact() exits 1 directly on a checksum or
    # signature-verification failure (AC2/AC3 — a hard abort, not a
    # fallback); a `return 1` here means a DOWNLOAD-layer failure instead
    # (network blip, or an asset that vanished between resolve and
    # download), which is likewise fatal at this point since the script has
    # already committed to ARTIFACT_MODE.
    if ! fetch_and_verify_artifact; then
        err "Artifact download failed (see above) — the running daemon (if any) was left untouched."
        exit 1
    fi
    NEW_BIN="$ARTIFACT_BIN"
    BUILT_COMMIT="$ARTIFACT_COMMIT"
    # NOTE: the source path's exit-4 "build verification" (built commit ==
    # source HEAD) deliberately has NO artifact-mode equivalent — it guards a
    # build.rs staleness defect that cannot exist for a binary this host did
    # not compile. The artifact's integrity was instead established by the
    # unconditional checksum + present-signature verification above, and its
    # arrival at the destination is asserted post-provision by
    # verify_destination_artifact().
    ok "Fetched + verified: $NEW_BIN (release ${ARTIFACT_TAG}${BUILT_COMMIT:+, commit $BUILT_COMMIT})"
else
    # Non-interactive SSH sessions (the fleet remote-update path, #4695) don't
    # source a login shell's profile, so a rustup-installed cargo living at the
    # default `~/.cargo/bin` is invisible to `command -v cargo` even though it IS
    # installed. Fall back the same way loom-daemon-start.sh's resolve_plist_path()
    # already does for launchd/systemd's non-login-shell PATH: prefer sourcing
    # rustup's own `~/.cargo/env` (the canonical PATH-setup snippet rustup
    # writes), then fall back to prepending `~/.cargo/bin` directly if that
    # script isn't present but the binary still is (e.g. a non-rustup or
    # partially-cleaned install), then finally fall back to the FULL shared
    # canonical PATH superset (lib/canonical-daemon-path.sh, #4831 — the same set
    # resolve_plist_path() renders and fleet add-worker's provisioning uses) in
    # case `cargo` was installed via Homebrew or another non-rustup path this
    # script doesn't special-case.
    if ! command -v cargo >/dev/null 2>&1; then
        if [[ -f "$HOME/.cargo/env" ]]; then
            # shellcheck disable=SC1091
            source "$HOME/.cargo/env"
        elif [[ -x "$HOME/.cargo/bin/cargo" ]]; then
            export PATH="$HOME/.cargo/bin:$PATH"
        fi
    fi
    if ! command -v cargo >/dev/null 2>&1; then
        _LOOM_CANONICAL_PATH_LIB="$SCRIPT_DIR/../lib/canonical-daemon-path.sh"
        if [[ -r "$_LOOM_CANONICAL_PATH_LIB" ]]; then
            # shellcheck source=../lib/canonical-daemon-path.sh
            source "$_LOOM_CANONICAL_PATH_LIB"
            if declare -F canonical_daemon_path >/dev/null 2>&1; then
                export PATH="$(canonical_daemon_path):$PATH"
            fi
        fi
    fi
    if ! command -v cargo >/dev/null 2>&1; then
        err "cargo not found on PATH (checked \$HOME/.cargo/bin and the shared canonical PATH too, see lib/canonical-daemon-path.sh) — cannot rebuild loom-daemon. Install Rust via rustup: https://rustup.rs"
        exit 1
    fi

    echo
    echo "Rebuilding loom-daemon (cargo build --release)..."
    # Resolve the artifact cargo ACTUALLY produced from cargo's own build
    # output (#6160), instead of probing two hardcoded target/release/
    # paths. The old probe was a SILENT no-op on any host where cargo's
    # output directory is redirected (CARGO_TARGET_DIR, or
    # ~/.cargo/config.toml's build.target-dir, e.g. after an ENOSPC-driven
    # move off the internal volume) -- the build fully succeeded, but this
    # script never found the binary it built and never provisioned it,
    # leaving the fleet host on its stale binary while reporting a rebuild.
    # `--message-format=json-render-diagnostics` keeps rustc's human-readable
    # errors/warnings on stderr (unchanged from a plain `cargo build`,
    # still visible to the operator/caller) while emitting cargo's
    # structured build JSON -- including the artifact's REAL on-disk path --
    # to stdout, which is captured below. This is exact and survives
    # CARGO_TARGET_DIR, --target-dir, workspace layouts, and per-profile
    # directories, unlike guessing at candidate paths.
    BUILD_JSON_LOG="$(mktemp "${TMPDIR:-/tmp}/loom-daemon-build-json.XXXXXX" 2>/dev/null || mktemp)"
    _LOOM_FETCH_TMPDIRS+=("$BUILD_JSON_LOG")
    if ! (cd "$DAEMON_DIR" && cargo build --release --message-format=json-render-diagnostics) >"$BUILD_JSON_LOG"; then
        err "cargo build --release failed — the running daemon (if any) was left untouched."
        exit 1
    fi

    # The LAST "executable":"...loom-daemon" field in the JSON stream is the
    # loom-daemon BIN target's own compiler-artifact message; every earlier
    # compiler-artifact message (its library target, its build script, and
    # every dependency) reports executable:null and is skipped automatically
    # -- the pattern below requires a quoted value. Parsed with grep/sed, not
    # jq: never assume jq is installed on a fleet worker (see the
    # artifact-fetch section above for the same convention).
    NEW_BIN="$(grep -o '"executable":"[^"]*/loom-daemon"' "$BUILD_JSON_LOG" | tail -n1 \
        | sed -E 's/^"executable":"//; s/"$//')"

    if [[ -z "$NEW_BIN" || ! -x "$NEW_BIN" ]]; then
        # Belt-and-braces fallback for a cargo version/output shape the grep
        # above doesn't expect: ask cargo directly where its target
        # directory is (`cargo metadata`, which resolves the SAME
        # CARGO_TARGET_DIR / build.target-dir redirect the build itself
        # used), then the redirect env var directly, then finally the
        # pre-#6160 hardcoded candidates (still correct on an unredirected
        # host).
        FALLBACK_CANDIDATES=()
        META_TARGET_DIR="$(cd "$DAEMON_DIR" && cargo metadata --format-version 1 --no-deps 2>/dev/null \
            | grep -o '"target_directory":"[^"]*"' | head -n1 | sed -E 's/^"target_directory":"//; s/"$//')"
        [[ -n "$META_TARGET_DIR" ]] && FALLBACK_CANDIDATES+=("$META_TARGET_DIR/release/loom-daemon")
        [[ -n "${CARGO_TARGET_DIR:-}" ]] && FALLBACK_CANDIDATES+=("$CARGO_TARGET_DIR/release/loom-daemon")
        FALLBACK_CANDIDATES+=("$DAEMON_DIR/target/release/loom-daemon" "$REPO_ROOT/target/release/loom-daemon")
        NEW_BIN=""
        for candidate in "${FALLBACK_CANDIDATES[@]}"; do
            if [[ -x "$candidate" ]]; then
                NEW_BIN="$candidate"
                break
            fi
        done
    fi

    if [[ -z "$NEW_BIN" || ! -x "$NEW_BIN" ]]; then
        # Hard failure (#6160): a build that reports success must NEVER be
        # silently treated as a no-op success -- exactly the "reports a
        # build succeeded... [host] stays on whatever binary it had" failure
        # mode this issue closes.
        err "cargo build --release reported success but no loom-daemon executable could be located -- checked cargo's own build JSON, cargo metadata's target_directory, \$CARGO_TARGET_DIR, and the conventional $DAEMON_DIR/target/release and $REPO_ROOT/target/release paths."
        err "Refusing to report a successful update when the built artifact cannot be found (#6160)."
        exit 1
    fi
    ok "Build succeeded: $NEW_BIN"

    # ---------- verify the freshly-built binary embeds the expected commit ----------
    # A rebuild can succeed (exit 0) yet bake in a STALE LOOM_DAEMON_GIT_COMMIT — the
    # exact hazard this script exists to close (a build.rs watch-set bug that lets
    # `--version` report the old commit). Provisioning such a binary would "report
    # success while shipping nothing" and, worse, turn any auto-update loop that
    # trusts the baked commit into an infinite rebuild-still-stale retry. So assert
    # the built commit == source HEAD BEFORE provisioning. On mismatch, fail loudly
    # and do NOT provision: this is a build-system defect that retrying cannot fix,
    # distinct from the compile failure handled above (#4053).
    BUILT_VERSION_OUTPUT=$("$NEW_BIN" --version 2>/dev/null || true)
    BUILT_COMMIT=$(extract_commit "$BUILT_VERSION_OUTPUT")
    if [[ "$SOURCE_COMMIT" == "unknown" ]]; then
        warn "Source HEAD is unknown (no .git?) — skipping built-commit verification (tarball build)."
    elif [[ -z "$BUILT_COMMIT" ]]; then
        err "Build verification FAILED: the freshly-built binary reports no commit in --version output ('${BUILT_VERSION_OUTPUT:-<empty>}')."
        err "Refusing to provision a binary that cannot prove what it was built from. This is a build-system defect, not a compile failure."
        exit 4
    elif [[ "$BUILT_COMMIT" != "$SOURCE_COMMIT" ]]; then
        err "Build verification FAILED: the freshly-built binary embeds commit '$BUILT_COMMIT' but source HEAD is '$SOURCE_COMMIT'."
        err "A successful build produced a binary stamped with the WRONG commit (a stale baked-in commit — e.g. a build.rs watch-set bug). Retrying will not fix it; refusing to provision (#4053)."
        exit 4
    else
        ok "Build verification: freshly-built binary embeds source HEAD commit ($BUILT_COMMIT)."
    fi
fi

# ---------- sign (Darwin-only, best-effort, non-fatal, #4016) ----------
# Ad-hoc-sign the freshly built binary with a stable identifier BEFORE
# provisioning, so both provisioning branches below (the LOOM_DAEMON_BIN
# override and provision_machine_daemon) copy an already-signed binary — the
# Mach-O signature survives `install`/`cp`. Signing does NOT make a TCC grant
# survive a rebuild (see sign_daemon_binary's own doc comment in
# scripts/install/provision-daemon.sh and .loom/docs/daemon-reference.md); it
# only pins a human-legible identifier in place of the rustc metadata hash.
#
# Skipped in artifact-fetch mode (#5020): a fetched macOS artifact may
# already carry a REAL Developer ID signature (Phase 2, #5011/#5018) —
# force-resigning it here (or via provision_machine_daemon's own
# belt-and-braces call below) would silently downgrade that to an ad-hoc
# signature. sign_daemon_binary() itself now guards against that (skips any
# binary that already carries a certificate-backed signature), but skip the
# direct call entirely here too: there is nothing useful for it to do to an
# already-signed or genuinely-unsigned fetched artifact.
# shellcheck disable=SC1091
if [[ -r "$REPO_ROOT/scripts/install/provision-daemon.sh" ]]; then
    source "$REPO_ROOT/scripts/install/provision-daemon.sh"
fi
if [[ "$ARTIFACT_MODE" != "true" ]] && declare -F sign_daemon_binary >/dev/null 2>&1; then
    sign_daemon_binary "$NEW_BIN"
fi

# ---------- provision ----------
if [[ -n "${LOOM_DAEMON_BIN:-}" ]]; then
    # Explicit operator override — provision directly to that exact path
    # (the one loom-daemon-start.sh will resolve to next via LOOM_DAEMON_BIN),
    # rather than the machine-level default.
    dest="$LOOM_DAEMON_BIN"
    if install -m 755 "$NEW_BIN" "$dest" 2>/dev/null || { cp -f "$NEW_BIN" "$dest" 2>/dev/null && chmod 755 "$dest" 2>/dev/null; }; then
        ok "Provisioned loom-daemon -> $dest"
    else
        err "Failed to provision to LOOM_DAEMON_BIN=$dest"
        exit 1
    fi
    # This override path has the same "shipped nothing" hazard as the
    # machine-level path — verify the destination is the freshly-built/fetched binary.
    if [[ "$ARTIFACT_MODE" == "true" ]]; then
        verify_destination_artifact "$dest"
    else
        verify_destination_binary "$dest"
    fi
    # #6009: also confirm the SUPERVISOR's own config points at this exact
    # destination, not a different (stale) path.
    verify_supervisor_matches_provisioned "$dest"
else
    if declare -F provision_machine_daemon >/dev/null 2>&1; then
        # Hard-fail on provisioning failure: a soft warn here (the pre-#4053
        # behavior) left the exit code at 0, which is exactly the "reports
        # success while shipping nothing" defect this issue closes.
        #
        # $REPO_ROOT is always a genuine Loom SOURCE checkout here (this
        # script rebuilds from source — see the REPO_ROOT resolution above),
        # so $REPO_ROOT/defaults is available; pass it through so an
        # already-installed standalone daemon picks up (or refreshes) its
        # `loom-daemon init` recovery payload on every update, not only on
        # first install (#5389).
        if ! provision_machine_daemon "$NEW_BIN" "" "$REPO_ROOT/defaults"; then
            err "Machine-level provisioning FAILED (see above). Refusing to report success; the freshly-built binary is at $NEW_BIN — set LOOM_DAEMON_BIN=$NEW_BIN to use it directly."
            exit 1
        fi
        # provision_machine_daemon exports the destination it wrote to (even on
        # the version-equality short-circuit) — verify that destination is the
        # expected build so the short-circuit can no longer produce a silent
        # no-op on a real roll (#4053).
        if [[ "$ARTIFACT_MODE" == "true" ]]; then
            verify_destination_artifact "${PROVISIONED_DAEMON_BIN:-}"
        else
            verify_destination_binary "${PROVISIONED_DAEMON_BIN:-}"
        fi
        # #6009: also confirm the SUPERVISOR's own config points at this
        # exact destination, not a different (stale) path.
        verify_supervisor_matches_provisioned "${PROVISIONED_DAEMON_BIN:-}"
    else
        warn "scripts/install/provision-daemon.sh not found/sourceable — skipping machine-level provisioning."
        warn "Freshly-built binary: $NEW_BIN (set LOOM_DAEMON_BIN=$NEW_BIN to use it directly)"
    fi
fi

# ---------- restart (preserve prior flags exactly — Issue #3968) ----------
if [[ "$NO_RESTART" == "true" ]]; then
    ok "Rebuilt + provisioned. Skipping restart (--no-restart)."
    if [[ "$WAS_RUNNING" == "true" ]]; then
        if [[ "$DAEMON_MANAGER" == "launchd" ]]; then
            echo "The running (launchd-managed) daemon is still the PRE-update binary. Restart it with:"
            echo "  $PROVISION_TARGET restart      (graceful: supervised in-place relaunch, in-flight sweeps preserved)"
            echo "  $PROVISION_TARGET restart --drain   (Issue #5138: pauses dispatch, waits for in-flight sweeps to finish first — no sweep.completed/sweep.outcome telemetry gap, #5084)"
            echo "(this two-step --no-restart + manual restart is equivalent to a single 'loom-daemon-update.sh --drain' invocation, which builds + provisions + drain-restarts in one command)"
            echo "If that binary predates #4077 and refuses the restart, re-render + relaunch under supervision:"
            echo "  loom-daemon-update.sh --relaunch   (preserves the live plist's LOOM_* env; SIGTERMs the daemon so sweep children reparent)"
            echo "'launchctl bootout $LAUNCHD_SERVICE' no longer kills in-flight sweeps on a current build (#5081 — each sweep runs in its own process group and reparents to pid 1), but a hand-run bootout+bootstrap can still race and leave the daemon down (bootout is asynchronous); prefer --relaunch above, which settles/retries/verifies the relaunch safely."
        elif [[ "$DAEMON_MANAGER" == "systemd" ]]; then
            echo "The running (systemd-managed) daemon is still the PRE-update binary. Restart it with:"
            # #5119: NOT "in-flight sweeps preserved" here -- unlike launchd, a
            # systemd stop job runs over the unit's whole cgroup, so a plain
            # restart's exit(0) reaps every sweep/role-run child with it. The
            # drain variant is the one that genuinely preserves them, which is
            # why it leads (and is the default on systemd, #5138).
            echo "  $PROVISION_TARGET restart --drain   (RECOMMENDED, Issue #5138: pauses dispatch, waits for in-flight sweeps to finish, THEN relaunches — the preserving variant; an immediate restart here can kill sweeps and land the unit in 'failed', #5119)"
            echo "  $PROVISION_TARGET restart      (immediate/non-drained — in-flight sweeps AND role runs in the unit cgroup ARE terminated; only if you have confirmed nothing is in flight)"
            echo "(this two-step --no-restart + manual restart is equivalent to a single 'loom-daemon-update.sh --drain' invocation, which builds + provisions + drain-restarts in one command — drain is also the DEFAULT on systemd for a plain 'loom-daemon-update.sh' run, no flag needed)"
            echo "If that binary predates #4267 and refuses the restart, re-render + relaunch under supervision:"
            echo "  loom-daemon-update.sh --relaunch   (preserves the live unit's LOOM_* env; SIGTERMs the daemon so sweep children reparent)"
            echo "Do NOT 'systemctl --user stop $SYSTEMD_UNIT' by hand — stop tears down the whole cgroup and KILLS in-flight sweeps (they are direct children of the unit)."
        else
            echo "The running daemon is still the PRE-update binary. Restart manually with:"
            echo "  $STOP_SCRIPT && $START_SCRIPT ${RESTART_ARGS[*]:-}"
        fi
    fi
    print_final_installed_line "$BUILT_COMMIT"
    exit 0
fi

if [[ "$WAS_RUNNING" != "true" ]]; then
    ok "Rebuilt + provisioned. loom-daemon was not running — nothing to restart."
    echo "Start it with: $START_SCRIPT [flags]"
    print_final_installed_line "$BUILT_COMMIT"
    exit 0
fi

# ---------- launchd-managed restart via the #4077 supervised primitive (#4042) ----------
# The daemon is launchd-supervised, so NEITHER stop.sh+start.sh NOR .daemon.flags
# apply: the plist's ProgramArguments + EnvironmentVariables are the durable
# source of truth. `loom-daemon restart` sends Request::RestartDaemon over the
# IPC socket; the supervised daemon exits 0 and KeepAlive:SuccessfulExit
# relaunches it onto the freshly-provisioned binary with the plist's config.
if [[ "$DAEMON_MANAGER" == "launchd" ]]; then
    echo "loom-daemon is launchd-managed (label ${LAUNCHD_LABEL})."
    build_restart_invoke_args
    if [[ "$DRAIN" == "true" ]]; then
        echo "Restarting via the supervised DRAIN restart primitive: $PROVISION_TARGET ${RESTART_INVOKE_ARGS[*]} (Issue #5138 / #4090) — pausing dispatch, waiting for in-flight sweeps to finish (preserving sweep.completed/sweep.outcome telemetry, #5084), then relaunching."
    else
        echo "Restarting via the supervised restart primitive: $PROVISION_TARGET restart"
    fi
    echo "(.daemon.flags is NOT consulted — the plist's EnvironmentVariables carries the equivalent config.)"

    # Capture the pre-restart pid BEFORE the request so the poll below can tell
    # "launchd relaunched onto a new pid" apart from "the same job never moved".
    PRE_RESTART_PID="$(launchd_job_pid)"

    if "$PROVISION_TARGET" "${RESTART_INVOKE_ARGS[@]}"; then
        # The RUNNING (old) binary accepted the request — but that ack is the
        # daemon's promise, not proof launchd actually honored it (#4232: the
        # daemon can exit 0 and launchd can still fail to relaunch it). Verify
        # a NEW, live pid before reporting success; the success message below
        # is intentionally the ONLY "restart scheduled"-style success line in
        # this branch, and it is unreachable until verification passes.
        RESTART_POLL_INTERVAL="${LOOM_DAEMON_RESTART_POLL_INTERVAL:-1}"
        KICKSTART_POLL_SECS="${LOOM_DAEMON_RESTART_KICKSTART_POLL_SECS:-15}"
        RESTART_KIND_NOTE="(#4232)"
        if [[ -n "${LOOM_DAEMON_RESTART_POLL_SECS:-}" ]]; then
            # An explicit override always wins, drain or not — an operator (or
            # test) who asked for a specific poll window gets exactly that.
            RESTART_POLL_SECS="$LOOM_DAEMON_RESTART_POLL_SECS"
        elif [[ "$DRAIN" == "true" ]]; then
            # A drain can legitimately take up to its own --timeout before it
            # relaunches — the fast #4232 default would false-negative on
            # every real drain (see the DRAIN_POLL_SECS computation above).
            RESTART_POLL_SECS="$DRAIN_POLL_SECS"
            RESTART_KIND_NOTE="(Issue #5138 drain window)"
        else
            RESTART_POLL_SECS=30
        fi
        echo "Restart request accepted (pre-restart pid: ${PRE_RESTART_PID:-<none>}). Verifying launchd relaunches onto a NEW, live pid within ${RESTART_POLL_SECS}s before reporting success ${RESTART_KIND_NOTE}..."

        if NEW_PID="$(wait_for_new_launchd_pid "$PRE_RESTART_PID" "$RESTART_POLL_SECS" "$RESTART_POLL_INTERVAL")"; then
            ok "loom-daemon restart scheduled — launchd relaunched it onto the freshly-provisioned binary (new pid ${NEW_PID}, verified within ${RESTART_POLL_SECS}s)."
            print_final_installed_line "$BUILT_COMMIT"
            exit 0
        fi

        # #5138: a drain that timed out WITHOUT --force-after-timeout is the
        # fail-safe working exactly as designed — the daemon refused the
        # restart and resumed dispatch on its CURRENT (pre-update) binary
        # rather than cancelling in-flight sweeps. NEVER kickstart in that
        # case: doing so would force exactly the sweep-cancelling restart the
        # fail-safe exists to prevent. Detect it by the pid being unchanged
        # (still alive, still the pre-restart pid) — anything else (pid gone,
        # or some other unrecognized shape) falls through to the ordinary
        # self-heal/investigation path below.
        if [[ "$DRAIN" == "true" && "$FORCE_AFTER_TIMEOUT" != "true" ]]; then
            CUR_PID_AFTER_DRAIN="$(launchd_job_pid)"
            if [[ -n "$CUR_PID_AFTER_DRAIN" && "$CUR_PID_AFTER_DRAIN" == "$PRE_RESTART_PID" ]] \
                && kill -0 "$CUR_PID_AFTER_DRAIN" 2>/dev/null; then
                warn "Drain timed out after ${RESTART_POLL_SECS}s without --force-after-timeout — the FAIL-SAFE held: loom-daemon is STILL RUNNING its PRE-update binary (pid ${CUR_PID_AFTER_DRAIN}). No in-flight sweep was cancelled or killed."
                warn "The freshly-built binary IS provisioned at $PROVISION_TARGET but was NOT activated this run."
                # Issue #6007: on a #6007+ daemon the roll is RETAINED rather than
                # discarded, so the recurrence advice is "do nothing", not
                # "re-run" — re-running on a busy host is what reproduced this.
                if drain_roll_still_armed "$PROVISION_TARGET"; then
                    warn "The daemon reports its drain STILL IN PROGRESS past the deadline — it has KEPT THE ROLL PENDING (#6007): new dispatch is still paused and the restart re-arms itself the moment the in-flight set reaches zero. Nothing to re-run — this host converges onto the provisioned binary on its own (or resumes dispatch and says so once the pending roll's paused-dispatch budget is spent)."
                    warn "Watch it with 'loom-daemon status' (the line under 'Drain: DRAINING …' explains the pending roll). To take over: 'loom-daemon restart --abort-drain' gives up and resumes dispatch now, or 'loom-daemon restart --drain --force-after-timeout' cancels the remaining sweep(s) and rolls immediately."
                else
                    warn "Re-run this script (or 'loom-daemon restart --drain' by hand) once the in-flight sweep(s) finish, or re-run with --force-after-timeout to force the roll through."
                fi
                exit 8
            fi
        fi

        warn "launchd did NOT relaunch within ${RESTART_POLL_SECS}s of the restart ack — no new, live pid observed (pre-restart pid was ${PRE_RESTART_PID:-<none>})."
        log_launchd_diagnostics
        warn "Falling back to 'launchctl kickstart $LAUNCHD_SERVICE' (plain — NEVER -k — so a daemon that DID relaunch during the race window above is never killed)."
        launchctl kickstart "$LAUNCHD_SERVICE" >/dev/null 2>&1

        if NEW_PID="$(wait_for_new_launchd_pid "$PRE_RESTART_PID" "$KICKSTART_POLL_SECS" "$RESTART_POLL_INTERVAL")"; then
            ok "loom-daemon restart scheduled — launchd's own relaunch did not occur within ${RESTART_POLL_SECS}s, but the 'launchctl kickstart' fallback relaunched it (new pid ${NEW_PID}, verified within ${KICKSTART_POLL_SECS}s). Remediation note: the kickstart fallback was required (#4232) — investigate why launchd did not relaunch the job on its own."
            print_final_installed_line "$BUILT_COMMIT"
            exit 0
        fi

        err "loom-daemon restart FAILED: no new, live pid was observed even after the 'launchctl kickstart' fallback."
        log_launchd_diagnostics
        err "The freshly-built binary IS provisioned, but the daemon's live status is NOT confirmed (pre-restart pid was ${PRE_RESTART_PID:-<none>})."
        err "Investigate manually: launchctl print $LAUNCHD_SERVICE"
        exit 7
    fi
    # The restart request is served by the RUNNING (old) binary. A pre-#4077
    # daemon has no RestartDaemon handler (and an unsupervised/dead socket also
    # fails), so the request was refused. Refuse loudly rather than claim a
    # half-update success: the fresh binary is provisioned but the OLD one is
    # still running (the #4011 silent-autonomy-loss class this issue closes).
    err "loom-daemon restart FAILED: the running daemon did not accept the restart request."
    err "This is expected on the FIRST roll onto a #4077-capable binary — the currently-running binary predates the 'restart' IPC command (or its socket is dead)."
    err "The freshly-built binary IS provisioned, but the OLD (unsupervised) binary is still running."

    if [[ "$RELAUNCH" == "true" ]]; then
        perform_relaunch "$LAUNCHD_PLIST"
        exit $?
    fi

    daemon_pid_hint=$(launchd_job_pid)
    err ""
    err "To finish the roll, re-render the plist and relaunch under launchd supervision"
    err "(this installs KeepAlive:{SuccessfulExit:true} + LOOM_DAEMON_SUPERVISOR=launchd so"
    err "the NEXT roll can use the supervised path) while preserving the live plist's LOOM_*"
    err "autonomy env — run:"
    err "  loom-daemon-update.sh --relaunch      (or: LOOM_DAEMON_UPDATE_RELAUNCH=1 loom-daemon-update.sh)"
    err ""
    err "NOTE (#5081): a bare 'launchctl bootout $LAUNCHD_SERVICE' no longer terminates"
    err "in-flight sweeps on a current build — every sweep runs in its own process group"
    err "(process_group(0), #3800), which bootout's job-tree teardown does not reach; it"
    err "reparents to pid 1 and keeps running. --relaunch above is still the recommended"
    err "path, but for a different reason: hand-running bootout immediately followed by a"
    err "plain bootstrap can race (bootout is asynchronous) and fail with 'Bootstrap"
    err "failed: 5: Input/output error', leaving the daemon down until a retry — start.sh"
    err "settles after bootout, retries on that race, and verifies the relaunched job's"
    err "live pid + env before reporting success, none of which a hand-typed sequence gets."
    err "If you must relaunch by hand anyway, prefer the graceful sequence below:"
    err "  kill -TERM ${daemon_pid_hint:-<daemon-pid>}   # daemon exits non-zero; sweep children reparent regardless; not relaunched (stale plist KeepAlive=false)"
    err "  $START_SCRIPT                                  # re-render + reload the supervised plist (settles/retries/verifies, #5081)"
    exit 6
fi

# ---------- systemd-managed restart via the #4267 supervised primitive (#4260 sub-issue C) ----------
# The systemd mirror of the launchd block above. The daemon is systemd-
# supervised, so NEITHER stop.sh+start.sh NOR .daemon.flags apply: the unit's
# ExecStart + Environment= lines are the durable source of truth. `loom-daemon
# restart` sends Request::RestartDaemon over the IPC socket; the supervised
# daemon exits 0 and `Restart=on-success` relaunches it onto the freshly-
# provisioned binary with the unit's config.
if [[ "$DAEMON_MANAGER" == "systemd" ]]; then
    echo "loom-daemon is systemd-managed (unit ${SYSTEMD_UNIT})."
    build_restart_invoke_args
    if [[ "$DRAIN" == "true" ]]; then
        if [[ "$DRAIN_DEFAULTED" == "true" ]]; then
            echo "Restarting via the supervised DRAIN restart primitive: $PROVISION_TARGET ${RESTART_INVOKE_ARGS[*]} — this is now the DEFAULT on systemd (Issue #5138): an immediate restart here can kill in-flight sweeps and land the unit in 'failed' (#5119). Pass --restart-now to opt out."
        else
            echo "Restarting via the supervised DRAIN restart primitive: $PROVISION_TARGET ${RESTART_INVOKE_ARGS[*]} (Issue #5138 / #4090) — pausing dispatch, waiting for in-flight sweeps to finish (preserving sweep.completed/sweep.outcome telemetry, #5084), then relaunching."
        fi
    else
        echo "--restart-now given: restarting via the IMMEDIATE (non-drained) supervised restart primitive: $PROVISION_TARGET restart"
    fi
    echo "(.daemon.flags is NOT consulted — the unit's Environment= lines carry the equivalent config.)"

    # Capture the pre-restart pid BEFORE the request so the poll below can tell
    # "systemd relaunched onto a new pid" apart from "the same unit never moved".
    PRE_RESTART_PID="$(systemd_unit_pid)"

    if "$PROVISION_TARGET" "${RESTART_INVOKE_ARGS[@]}"; then
        # The RUNNING (old) binary accepted the request — but that ack is the
        # daemon's promise, not proof systemd actually honored it (#4950: the
        # daemon can exit 0 and the unit can still land in `failed (Result:
        # timeout)` before `Restart=on-success` ever fires). Verify a NEW,
        # live MainPID before reporting success; the success message below is
        # intentionally the ONLY "restart scheduled"-style success line in
        # this branch, and it is unreachable until verification passes.
        RESTART_POLL_INTERVAL="${LOOM_DAEMON_RESTART_POLL_INTERVAL:-1}"
        KICKSTART_POLL_SECS="${LOOM_DAEMON_RESTART_KICKSTART_POLL_SECS:-15}"
        RESTART_KIND_NOTE="(#4950)"
        if [[ -n "${LOOM_DAEMON_RESTART_POLL_SECS:-}" ]]; then
            # An explicit override always wins, drain or not — an operator (or
            # test) who asked for a specific poll window gets exactly that.
            RESTART_POLL_SECS="$LOOM_DAEMON_RESTART_POLL_SECS"
        elif [[ "$DRAIN" == "true" ]]; then
            # A drain can legitimately take up to its own --timeout before it
            # relaunches — the fast #4950 default would false-negative on
            # every real drain (see the DRAIN_POLL_SECS computation above).
            RESTART_POLL_SECS="$DRAIN_POLL_SECS"
            RESTART_KIND_NOTE="(Issue #5138 drain window)"
        else
            RESTART_POLL_SECS=30
        fi
        echo "Restart request accepted (pre-restart pid: ${PRE_RESTART_PID:-<none>}). Verifying systemd relaunches onto a NEW, live MainPID within ${RESTART_POLL_SECS}s before reporting success ${RESTART_KIND_NOTE}..."

        if NEW_PID="$(wait_for_new_systemd_pid "$PRE_RESTART_PID" "$RESTART_POLL_SECS" "$RESTART_POLL_INTERVAL")"; then
            ok "loom-daemon restart scheduled — systemd relaunched it onto the freshly-provisioned binary (new pid ${NEW_PID}, verified within ${RESTART_POLL_SECS}s)."
            print_final_installed_line "$BUILT_COMMIT"
            exit 0
        fi

        # #5138: a drain that timed out WITHOUT --force-after-timeout is the
        # fail-safe working exactly as designed — the daemon refused the
        # restart and resumed dispatch on its CURRENT (pre-update) binary
        # (the unit stays `active` throughout — it was never told to stop)
        # rather than cancelling in-flight sweeps. NEVER run the reset-failed/
        # settle-wait self-heal below in that case: the unit isn't broken, and
        # forcing it would perform exactly the sweep-cancelling restart the
        # fail-safe exists to prevent. Detect it by the pid being unchanged
        # (still alive, still the pre-restart pid) — anything else falls
        # through to the ordinary #4950/#5119 investigation path.
        if [[ "$DRAIN" == "true" && "$FORCE_AFTER_TIMEOUT" != "true" ]]; then
            CUR_PID_AFTER_DRAIN="$(systemd_unit_pid)"
            if [[ -n "$CUR_PID_AFTER_DRAIN" && "$CUR_PID_AFTER_DRAIN" != "0" && "$CUR_PID_AFTER_DRAIN" == "$PRE_RESTART_PID" ]] \
                && kill -0 "$CUR_PID_AFTER_DRAIN" 2>/dev/null; then
                warn "Drain timed out after ${RESTART_POLL_SECS}s without --force-after-timeout — the FAIL-SAFE held: loom-daemon is STILL RUNNING its PRE-update binary (pid ${CUR_PID_AFTER_DRAIN}). No in-flight sweep was cancelled or killed."
                warn "The freshly-built binary IS provisioned at $PROVISION_TARGET but was NOT activated this run."
                # Issue #6007: on a #6007+ daemon the roll is RETAINED rather than
                # discarded, so the recurrence advice is "do nothing", not
                # "re-run" — re-running on a busy host is what reproduced this.
                if drain_roll_still_armed "$PROVISION_TARGET"; then
                    warn "The daemon reports its drain STILL IN PROGRESS past the deadline — it has KEPT THE ROLL PENDING (#6007): new dispatch is still paused and the restart re-arms itself the moment the in-flight set reaches zero. Nothing to re-run — this host converges onto the provisioned binary on its own (or resumes dispatch and says so once the pending roll's paused-dispatch budget is spent)."
                    warn "Watch it with 'loom-daemon status' (the line under 'Drain: DRAINING …' explains the pending roll). To take over: 'loom-daemon restart --abort-drain' gives up and resumes dispatch now, or 'loom-daemon restart --drain --force-after-timeout' cancels the remaining sweep(s) and rolls immediately."
                else
                    warn "Re-run this script (or 'loom-daemon restart --drain' by hand) once the in-flight sweep(s) finish, or re-run with --force-after-timeout to force the roll through."
                fi
                exit 8
            fi
        fi

        warn "systemd did NOT relaunch within ${RESTART_POLL_SECS}s of the restart ack — no new, live MainPID observed (pre-restart pid was ${PRE_RESTART_PID:-<none>})."
        log_systemd_diagnostics
        UNIT_ACTIVE_STATE="$(systemd_unit_active_state)"
        UNIT_RESULT="$(systemd_unit_result)"

        # #5119: the unit may still be mid-teardown when the #4950 pid poll
        # expires — the exact 2026-08-03 loom-worker-1 incident, where a busy
        # host's stale unit (default TimeoutStopSec=90s + KillMode=control-group)
        # sat in `deactivating (stop-sigterm)` while systemd SIGTERMed then
        # SIGKILLed the sweep/role children still in the cgroup, long past the
        # default 30s pid poll. The pre-#5119 code only self-healed a unit
        # ALREADY `failed`, so a `deactivating` snapshot fell through to "refusing
        # to guess" and left the daemon DOWN until an operator ran reset-failed+
        # start by hand. WAIT for the stop transition to settle so the recovery
        # can act on the terminal state (`failed`/`inactive`) it lands in.
        case "$UNIT_ACTIVE_STATE" in
            deactivating|activating|reloading|deactivating-sigterm|deactivating-sigkill)
                STOP_SETTLE_SECS="${LOOM_DAEMON_STOP_SETTLE_SECS:-100}"
                warn "Unit is still transitioning (ActiveState=${UNIT_ACTIVE_STATE}) — its stop job has not finished (a stale unit predating #4862's KillMode=mixed drags the SIGTERM→SIGKILL teardown of in-cgroup sweep/role children out to the default 90s TimeoutStopSec). Waiting up to ${STOP_SETTLE_SECS}s for it to settle before recovering (#5119)."
                SETTLED_STATE="$(wait_for_systemd_stop_settle "$STOP_SETTLE_SECS" "$RESTART_POLL_INTERVAL")"
                UNIT_ACTIVE_STATE="$SETTLED_STATE"
                UNIT_RESULT="$(systemd_unit_result)"
                warn "Unit settled to ActiveState=${UNIT_ACTIVE_STATE} (Result=${UNIT_RESULT:-unknown}) after its stop transition."
                ;;
        esac

        # A unit that is NOT `active` after that settle will NOT come back on its
        # own: `Restart=on-success` fires only for a clean-exit relaunch, never
        # for a stop-timeout escalation (`failed`, Result=timeout — the classic
        # #4950 shape) nor a completed stop (`inactive`). Only a genuinely
        # `active` unit on a pid the poll simply failed to observe is left alone —
        # touching that would risk bouncing a healthy daemon. Self-heal
        # everything else via the documented reset-failed+start recovery (#4950),
        # now reached for the #5119 `deactivating`/`inactive` cases too, not just
        # a confirmed `failed`.
        if [[ "$UNIT_ACTIVE_STATE" != "active" ]]; then
            warn "Unit is in a non-running state (ActiveState=${UNIT_ACTIVE_STATE:-unknown}, Result=${UNIT_RESULT:-unknown}) — systemd will NOT auto-relaunch it (Restart=on-success does not fire for a failed/stopped unit). Self-healing via 'systemctl --user reset-failed $SYSTEMD_UNIT && systemctl --user start $SYSTEMD_UNIT'."
            systemctl --user reset-failed "$SYSTEMD_UNIT" >/dev/null 2>&1
            systemctl --user start "$SYSTEMD_UNIT" >/dev/null 2>&1

            if NEW_PID="$(wait_for_new_systemd_pid "$PRE_RESTART_PID" "$KICKSTART_POLL_SECS" "$RESTART_POLL_INTERVAL")"; then
                ok "loom-daemon restart scheduled — systemd's own relaunch did not occur within ${RESTART_POLL_SECS}s (unit settled to '${UNIT_ACTIVE_STATE}', Result=${UNIT_RESULT:-unknown}), but 'systemctl --user reset-failed && start' recovered it (new pid ${NEW_PID}, verified within ${KICKSTART_POLL_SECS}s). Remediation note: the reset-failed+start fallback was required (#4950/#5119) — investigate why the unit's stop sequence exceeded TimeoutStopSec (a live unit that predates #4862's KillMode=mixed fix — never re-rendered by a plain restart — is the most likely cause; re-render it with 'loom-daemon-update.sh --relaunch')."
                print_final_installed_line "$BUILT_COMMIT"
                exit 0
            fi

            err "loom-daemon restart FAILED: no new, live MainPID was observed even after 'systemctl --user reset-failed && start'."
        else
            err "loom-daemon restart FAILED: the unit is not confirmed relaunched, yet its ActiveState is 'active' on an unchanged/unobserved pid — refusing to bounce a possibly-healthy daemon."
        fi
        log_systemd_diagnostics
        err "The freshly-built binary IS provisioned, but the daemon's live status is NOT confirmed (pre-restart pid was ${PRE_RESTART_PID:-<none>})."
        err "Investigate manually: systemctl --user status $SYSTEMD_UNIT"
        exit 7
    fi
    # The restart request is served by the RUNNING (old) binary. A pre-#4267
    # daemon has no RestartDaemon handler recognizing LOOM_DAEMON_SUPERVISOR=systemd
    # (and an unsupervised/dead socket also fails), so the request was refused.
    # Refuse loudly rather than claim a half-update success: the fresh binary is
    # provisioned but the OLD one is still running (the #4011 silent-autonomy-
    # loss class this issue closes).
    err "loom-daemon restart FAILED: the running daemon did not accept the restart request."
    err "This is expected on the FIRST roll onto a #4267-capable binary — the currently-running binary predates the 'restart' IPC command (or its socket is dead)."
    err "The freshly-built binary IS provisioned, but the OLD (unsupervised) binary is still running."

    if [[ "$RELAUNCH" == "true" ]]; then
        perform_systemd_relaunch "$SYSTEMD_UNIT_PATH" "$SYSTEMD_UNIT"
        exit $?
    fi

    daemon_pid_hint=$(systemd_unit_pid)
    err ""
    err "To finish the roll, re-render the unit and relaunch under systemd supervision"
    err "(this installs Restart=on-success + LOOM_DAEMON_SUPERVISOR=systemd so"
    err "the NEXT roll can use the supervised path) while preserving the live unit's LOOM_*"
    err "autonomy env — run:"
    err "  loom-daemon-update.sh --relaunch      (or: LOOM_DAEMON_UPDATE_RELAUNCH=1 loom-daemon-update.sh)"
    err ""
    err "WARNING: do NOT 'systemctl --user stop $SYSTEMD_UNIT' by hand to force this."
    err "stop tears down the whole cgroup, and in-flight sweep children are DIRECT"
    err "children of the unit, so it TERMINATES every running sweep — stranding"
    err "loom:building labels and leaving worktrees behind. --relaunch above instead stops"
    err "the daemon gracefully (SIGTERM) so sweep children reparent and keep working."
    err "If you must relaunch by hand, prefer the graceful sequence over stop+enable:"
    err "  kill -TERM ${daemon_pid_hint:-<daemon-pid>}   # daemon exits by signal; children reparent; not relaunched (Restart=on-success does not fire)"
    err "  $START_SCRIPT                                  # re-render + enable --now the supervised unit"
    exit 6
fi

# ---------- PID-file/nohup-managed restart (preserve prior flags exactly) ----------
if [[ "$FLAGS_SOURCE" == "$FLAGS_FILE" ]]; then
    echo "Restarting with the flags persisted at the last start ($FLAGS_FILE): ${RESTART_ARGS[*]:-<none>}"
else
    warn "No $FLAGS_FILE found — restarting FLAGS-OFF (bare) rather than guessing the prior autonomy flags."
fi

echo "Stopping loom-daemon..."
# --restarting preserves the autonomy-desired marker + watchdog across this
# internal stop (#4011): a self-update is NOT operator intent to stop, so the
# detector must NOT be disarmed — otherwise every self-update would silently turn
# off the very autonomy-loss detection this issue adds (the exact bug class it
# fixes). The subsequent start re-writes the marker and re-provisions the watchdog.
if ! "$STOP_SCRIPT" --restarting; then
    err "loom-daemon-stop.sh failed — NOT starting the new binary on top of a still-running old one."
    exit 1
fi

echo "Starting loom-daemon with preserved flags: ${RESTART_ARGS[*]:-<none>}"
# Guard the array expansion: RESTART_ARGS is empty for a bare/FLAGS-OFF
# restart, and "${arr[@]}" on a zero-element array is an unbound variable
# error under `set -u` on bash < 4.4 (still the default /bin/bash on stock
# macOS).
if [[ "${#RESTART_ARGS[@]}" -gt 0 ]]; then
    "$START_SCRIPT" "${RESTART_ARGS[@]}"
elif [[ "$FLAGS_SOURCE" == "$FLAGS_FILE" ]]; then
    # Issue #5429: a persisted-flags FILE that EXISTS but is empty is a
    # CONFIRMED prior FLAGS-OFF state (distinct from the "no file at all"
    # branch below, which is genuinely a guess). A bare `$START_SCRIPT`
    # invocation here would leave WANT_WORK_FINDER/WANT_HEALTH_GATE unset,
    # which loom-daemon-start.sh's autonomy-downgrade refusal (#4011/#5409)
    # cannot distinguish from a silent, un-intentional default -- and the
    # autonomy-desired intent marker it checks is written unconditionally on
    # ANY successful start (autonomous or not, see write_intent_marker in
    # loom-daemon-start.sh), so it is already present on every restart of a
    # daemon that was already running, regardless of whether autonomy was
    # ever on. That mismatch made this exact restart refuse with exit 1
    # whenever the marker pre-existed (e.g. immediately after a prior start
    # in the same process lifetime), even though the persisted-flags file
    # unambiguously says "no flags". Passing --no-work-finder/--no-health-gate
    # explicitly states that confirmed intent (byte-identical resulting
    # LOOM_WORK_FINDER=0/LOOM_MAIN_HEALTH_GATE=0 env — see the non-`--from-config`
    # branch in loom-daemon-start.sh, "on" is the only value forced differently
    # from "unset") while satisfying the downgrade check's own "explicit ask is
    # not silent" exemption.
    "$START_SCRIPT" --no-work-finder --no-health-gate
else
    "$START_SCRIPT"
fi
START_RC=$?
if [[ "$START_RC" -eq 0 ]]; then
    print_final_installed_line "$BUILT_COMMIT"
fi
exit "$START_RC"
