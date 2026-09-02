#!/usr/bin/env bash
# loom-daemon-watchdog.sh - Host-side autonomy-loss detector for the RAW
# loom-daemon process (Issue #4011).
#
# THE PROBLEM IT SOLVES
#   On 2026-07-26 the loom-daemon launchd job took a SIGTERM two seconds after
#   starting and was left `bootout`-ed (unloaded) from launchd. Autonomous
#   dispatch (work finder, role runner) silently stopped. NOTHING surfaced it —
#   no log line, no forge signal, no notification. It was discovered hours later
#   only because someone happened to run `loom-daemon status` by hand. A pull
#   nobody performed for hours is not a detector.
#
# WHAT THIS IS
#   The payload of a SECOND launchd job (`<daemon-label>-watchdog`) that runs on
#   a `StartInterval` cadence, SEPARATE from the daemon job. It compares two
#   things:
#     (1) operator INTENT — the durable `autonomy-desired` marker that
#         loom-daemon-start.sh writes on a successful start and only an
#         operator-initiated loom-daemon-stop.sh removes; and
#     (2) REALITY — whether a daemon for the expected launchd label is actually
#         loaded and alive, and whether its declared-cadence heartbeat file
#         (written by the daemon, #4011) is fresh.
#   When intent says "a daemon should be running" but reality disagrees, it
#   REPORTS loudly (a timestamped line to the watchdog log + stderr, which
#   launchd captures) instead of staying silent — and, since #5391, it also
#   RECOVERS: a confirmed-down daemon is restarted under bounded retries with
#   exponential backoff behind a circuit breaker, escalating to a forge issue
#   (not just a logfile) once the attempt budget is spent. See "GENERAL-CASE
#   BOUNDED RECOVERY + CIRCUIT BREAKER (#5391)" below for the full policy and
#   the reasoning behind recovering rather than only reporting.
#
# WHY A SECOND LAUNCHD JOB, NOT A RESIDENT PROCESS
#   The reporter must live OUTSIDE the daemon process: a dead daemon cannot
#   report its own death (which is why #3971's in-daemon watch loop is not
#   reusable here). And it must itself be supervised — but a long-lived resident
#   watchdog just moves the "who watches the watchdog" problem up one level (it
#   too can crash and stay dead). A `StartInterval` job owns NO long-lived
#   process: launchd re-runs it every interval regardless of how the last run
#   exited, so it structurally cannot crash-and-stay-dead. That is what resolves
#   the recursion.
#
# WHY THE MARKER, NOT "is the pid file / launchd job present"
#   loom-daemon-stop.sh boots out the job AND deletes the pid file, so after ANY
#   stop those would be gone — making a deliberately-stopped daemon and a
#   silently-dead one byte-identical. A detector built on them would page on
#   every intentional stop or never page at all. The marker's lifetime is
#   OPERATOR INTENT: present ⇒ a daemon is expected; absent ⇒ it was
#   deliberately stopped (or never started) ⇒ stay silent.
#
# HANG-AWARE IPC LIVENESS PROBE (#4398)
#   Process-exists + heartbeat-fresh are BOTH out-of-band signals: neither ever
#   talks to the daemon over the socket it actually serves work on. Two observed
#   incidents show why that is not enough:
#     - #4381: the installed loom-daemon binary was replaced by a stub that
#       answered `--version` and then hung forever. A pid-alive check passes
#       against that indefinitely.
#     - 2026-07-29: the production daemon (pid 1484) was alive AND writing a
#       FRESH heartbeat while every `loom-daemon status` round-trip hung. The
#       heartbeat writer (`daemon_heartbeat::spawn_heartbeat_task`) and the IPC
#       accept loop are independent `tokio::spawn`ed tasks on a multi-threaded
#       runtime, so one can keep ticking while the other is wedged.
#   So after liveness is confirmed, this job also runs a BOUNDED in-band IPC
#   round-trip through the installed loom-daemon CLI. Design points:
#     * BOUNDED TWICE. The CLI bounds its own connect + round-trip (5s each,
#       `query_daemon_bounded`); this job additionally wraps the invocation in a
#       hard external timeout, so even a CLI that never returns at all (the
#       #4381 stub) cannot hang the watchdog tick.
#     * LIGHTWEIGHT SUBCOMMAND, NOT `status`. The probe defaults to
#       `quarantine list` — a pure IPC round-trip with no post-reply work.
#       `loom-daemon status --json` looks like the obvious probe but, AFTER a
#       successful round-trip, it also runs a per-account token-pool network
#       check plus a self-update check; measured at 15.3s against a HEALTHY
#       daemon, which would make the probe timeout itself the false-positive
#       generator. Override with LOOM_WATCHDOG_IPC_PROBE_ARGS if desired.
#     * DEBOUNCED. A single failed round-trip can be transient contention (a
#       per-connection task dropping a `status` under concurrent-sweep load —
#       #4279). One failure is REPORTED (loud, logged) but only N consecutive
#       failures, tracked in <loom_dir>/.watchdog-probe-fail-count and keyed to
#       the live pid, are called a CONFIRMED hang.
#     * STARTUP-GRACE AWARE. For up to ~90s after a supervised relaunch the
#       socket may not be bound yet even though launchd reports a live pid
#       (#4213). A probe against a process younger than the grace window is
#       skipped entirely and never counts toward the confirmed-hang tally —
#       the same window `daemon_install_state::DEFAULT_STARTUP_GRACE_SECS`
#       uses, so `status` and this watchdog cannot disagree.
#     * GRACEFULLY DEGRADING. No resolvable loom-daemon binary, a build whose
#       CLI does not know the probe subcommand, or any daemon-side application
#       error (the IPC round-trip demonstrably WORKED) skips the probe rather
#       than inventing a divergence. The probe must never become a new hard
#       dependency that pages on its own absence.
#     * REPORT-ONLY, DELIBERATELY. Unlike #4232's narrow auto-`kickstart`, there
#       is NO provably-safe unattended remediation for a wedged-but-alive
#       process: the only real fix is killing it, which would also kill a daemon
#       that is merely under heavy legitimate load. A confirmed hang therefore
#       escalates to a maximally actionable DIVERGENCE report (exit 1) with the
#       explicit recovery commands — never an automatic kill/restart.
#
# TICK-GRANULARITY DETECTION LATENCY IS AN ACCEPTED TRADEOFF (#5790)
#   An operator saw `loom-daemon status` itself time out on IPC twice ~20
#   minutes apart (one successful `status` in between) while this watchdog's
#   own log read `[OK] daemon healthy` throughout. Two independent, additive
#   mechanisms explain that without the probe being absent or broken:
#     1. SUB-TICK WEDGES ARE STRUCTURALLY INVISIBLE. This probe samples ONCE
#        per StartInterval tick (default LOOM_WATCHDOG_INTERVAL_SECS=300, see
#        defaults/docs/daemon-reference.md). A wedge that resolves faster than
#        one tick interval can fall entirely between two samples and never be
#        observed at all. This is inherent to any periodic sampled probe, not
#        a bug: closing it completely would require continuous polling or
#        multiple samples per tick, trading resource cost and forge-issue /
#        log noise for lower latency — a real design change, deliberately left
#        as future work rather than folded into this fix.
#     2. A SUB-THRESHOLD DIVERGENCE USED TO BE MASKED BY THE SAME TICK'S OK
#        LINE. Before #5790, a tick whose probe DID fire during a wedge and
#        correctly logged a sub-threshold `[DIVERGENCE] ... consecutive
#        failure N of <threshold>` line then fell through to section 4 and
#        unconditionally appended `[OK] daemon healthy ...` right after it —
#        so an operator or log-scraper reading only the last line, or grepping
#        for `[OK]`, saw a clean bill of health in the SAME tick a divergence
#        was already reported. #5790 fixes this specific defect (see
#        `report_heartbeat_ok()` below): that OK line now folds in the
#        divergence instead of reading as unambiguously healthy. This was a
#        pure reporting defect — the exit code (via `exit_ok()`) already
#        reflected `PROBE_DIVERGED` correctly, so any consumer keyed off exit
#        code alone (rather than the log text) was never misled by it.
#   Given (2) is now fixed, ANY wedge that persists through at least one full
#   tick is reported the very tick it is observed — detection latency for that
#   class is bounded by ONE tick (≤300s by default), not by
#   LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD (that threshold only gates the
#   CONFIRMED/report-vs-recover distinction, never whether a single failure is
#   reported at all — see "consecutive failure N of threshold" above). Only
#   (1), sub-tick-duration wedges, remains genuinely undetectable by this
#   probe's design, and this default cadence — 300s ticks, 3-consecutive
#   CONFIRMED threshold — is kept as the correct tradeoff: it already reports
#   every observed divergence immediately (loud, unconditional stderr, logged
#   regardless of --verbose) while reserving the escalation-worthy CONFIRMED
#   label and its exit-1-every-tick behavior for a hang proven to be sustained
#   across debounced samples (#4279's transient-contention rationale, above).
#   Lowering the default interval would shrink the sub-tick blind spot but
#   raise per-host probe overhead and false-positive risk fleet-wide for a
#   marginal gain; that tradeoff is not taken here without fleet data
#   justifying it.
#
# A WINDOWED/RATE FAILURE SIGNAL, ALONGSIDE THE CONSECUTIVE ONE (#5944)
#   Both mechanisms above — the same-tick fold-in (#5790) and the CONFIRMED
#   escalation (#4398) — key off `probe_fail_count_*`, a streak that is reset
#   to ZERO by a single successful round-trip ("ends any prior failure streak
#   outright", see that function's own comment). That reset is exactly right
#   for the CONFIRMED-hang decision — a hang that heals on its own should not
#   count toward the next one — but it leaves a THIRD failure shape
#   unhandled: a probe that fails, succeeds, fails, succeeds... under host
#   load, never 3-in-a-row. Every individual failing tick still gets its own
#   sub-threshold DIVERGENCE line (unchanged, and still correct), but each
#   intervening SUCCESS resets the streak to zero and reports a bare
#   `[OK] daemon healthy` for that tick — so hours of intermittent failure
#   never accumulate into anything an operator or log-scraper skimming the
#   last line, or grepping for `[OK]`, would notice. Observed live on a host
#   at load 45-65 across 28 cores: `loom-daemon status` timed out on
#   essentially every operator invocation for hours while this watchdog's log
#   read `[OK] daemon healthy` throughout.
#
#   The fix is a SECOND, independent tally: a rolling WINDOW of the last
#   LOOM_WATCHDOG_IPC_PROBE_WINDOW_TICKS probe outcomes (default 6 — 30
#   minutes at the default 300s cadence), keyed to the live pid exactly like
#   the consecutive tally (a relaunched daemon starts a fresh window, same
#   reasoning as #4398's pid-keyed streak: a fresh process must never inherit
#   its wedged predecessor's history). Unlike the consecutive tally, a single
#   SUCCESS does NOT clear this history — only the window sliding an old tick
#   out of range does. When LOOM_WATCHDOG_IPC_PROBE_WINDOW_FAIL_THRESHOLD
#   (default 3) or more of the last WINDOW_TICKS ticks failed, even a tick
#   whose OWN round-trip just succeeded is reported DEGRADED (not a bare OK,
#   via `report_heartbeat_ok()`'s existing PROBE_DIVERGED fold-in — the same
#   plumbing #5790 built, reused rather than duplicated) and the tick's exit
#   code reflects it (non-zero), carrying the host load average at probe time
#   (`get_load_average()`, #5790) so the report doubles as the load-contention
#   diagnosis.
#
#   Deliberately weaker than a CONFIRMED hang: the windowed signal is
#   report-only, never escalates to the "IPC UNRESPONSIVE (CONFIRMED)" text,
#   its exit-1-no-remediation posture, or the #5391 bounded-recovery path —
#   the daemon DID answer on the ticks in between, which is real evidence
#   against a genuine wedge (#4279's transient-contention rationale). The
#   default threshold (3 of the last 6) also will not fire on a single
#   isolated failure — that already gets its own sub-threshold DIVERGENCE
#   line via the unchanged #5790 path and is not this mechanism's job.
#
# SOCKET-FIRST LIVENESS, PID FILE DEMOTED TO A HINT (#5118)
#   Until #5118 the OUT-OF-BAND probe above was the ONLY thing that could
#   establish liveness, and on a host with neither a launchd job nor a
#   systemd-managed unit to ask, that reduced to a single artifact: the pid
#   file. Three stacked defects made that unusable fleet-wide:
#     1. PATH DISAGREEMENT. This script derived the pid file from the SOCKET's
#        directory (`<dirname $LOOM_SOCKET_PATH>/.daemon.pid`) in the
#        marker-absent path, while the daemon derives it from the START
#        SCRIPT's `LOOM_PID_FILE` / the workspace (`daemon_pidfile.rs`). On a
#        workspace-rooted install those are never the same directory.
#     2. `LOOM_PID_FILE` WAS IGNORED HERE. It is exported by
#        loom-daemon-start.sh and baked into every plist/unit it renders — and
#        honored by the daemon as precedence tier 1 — but this script had no
#        support for it at all, so the one variable that is supposed to
#        single-source the path could not align the two ends.
#     3. A STALE pid file reads as a CONFIRMED death. A pid file naming a
#        long-dead pid (#4774, before the daemon self-wrote it at bind) is
#        indistinguishable here from "no daemon".
#   Observed 2026-08-03: BOTH fleet hosts ran healthy daemons while this
#   watchdog reported `[DIVERGENCE] ... no live pid file at ~/.loom/.daemon.pid`
#   every five minutes since 2026-08-01. An alarm that is always on carries no
#   information — and two genuine outages that same session were
#   indistinguishable from it.
#
#   The fix inverts the precedence to match what `loom-daemon health` already
#   does: the IPC ROUND-TRIP IS AUTHORITATIVE, the pid file is a corroborating
#   hint. Concretely, when the out-of-band probe does NOT find a live daemon
#   this script now ASKS THE SOCKET before reporting anything:
#     * socket ANSWERS + no supervisor was consulted (pid-file path) ⇒ HEALTHY.
#       The pid file's absence/staleness is a note, never a page.
#     * socket ANSWERS + the supervisor (launchd/systemd) says its job is down
#       ⇒ a WARN state mismatch: a daemon is serving work but nothing is
#       supervising it. Auto-remediation is SUPPRESSED — kickstarting a second
#       daemon at a socket a live one already owns only produces a refusal.
#     * socket is UNREACHABLE ⇒ the daemon really is down: the existing
#       DIVERGENCE + bounded auto-remediation path, unchanged.
#     * liveness CANNOT BE DETERMINED (no probe available — no resolvable
#       binary, probe disabled, CLI wedged — AND no usable pid file) ⇒ a
#       DISTINCT `UNKNOWN` report and exit 3. Defaulting to "the daemon is
#       gone" is what manufactured the permanent false positive; an alerting
#       component must say "I cannot tell" in its own words.
#   The pid file's PATH is also now derived by the same precedence the daemon
#   uses (`LOOM_PID_FILE` > the marker's `pid_file=` > machine/workspace >
#   `<loom dir>`), so the two ends can no longer disagree about which file
#   they mean.
#
# BOUNDED AUTO-REMEDIATION (#4232)
#   The watchdog was deliberately report-only until #4232: on 2026-07-28 a
#   `loom-daemon restart` was ack'd (the running daemon exited 0, honoring its
#   #4054/#4077 restart contract) but launchd never relaunched it — the
#   watchdog could describe that outage but not fix it, which matters once
#   #4055's unattended self-update path can hit the same race with no operator
#   watching. This job now auto-runs `launchctl kickstart <label>` (PLAIN,
#   NEVER `-k`) for EXACTLY ONE divergence signature: the launchd job is
#   LOADED (launchctl still knows about it) + NOT running + its last exit
#   status was 0. That signature can ONLY arise from a restart-primitive exit
#   that launchd failed to honor — an operator SIGTERM stop exits 143/130
#   (loom-daemon-stop.sh), a crash exits non-zero, and a booted-out/never-
#   loaded job fails `launchctl print` outright. Every other divergence stays
#   report-only, exactly as before: no crash-loop revival, no reviving a
#   deliberate stop.
#
#   #4862 adds the systemd --user mirror of this gate: a clean main-process
#   exit (ExecMainCode=exited, ExecMainStatus=0) with the unit LOADED but not
#   running auto-runs 'systemctl --user reset-failed <unit> && systemctl
#   --user start <unit>' — the systemd analog of 'launchctl kickstart', for
#   the exact "Restart=on-success didn't fire" incident #4862 reported (and
#   the render_systemd_unit KillMode=mixed fix in loom-daemon-start.sh
#   addresses at the source). Same narrow construction: an operator stop, a
#   genuine crash, or a never-installed unit cannot produce this signature.
#
# GENERAL-CASE BOUNDED RECOVERY + CIRCUIT BREAKER (#5391)
#   THE DECISION: this watchdog RECOVERS. It is not a report-only detector.
#
#   The #4232/#4862 gates above only ever covered ONE divergence signature
#   ("loaded + down + last exit 0"). Every other confirmed outage — a genuine
#   crash, a booted-out job, a never-relaunched unit, a dead pid with an
#   unreachable socket — fell through to a plain `[DIVERGENCE] … Recover with:
#   loom-daemon-start.sh` line and stopped there. Observed on one fleet host:
#   252 such lines between 2026-07-28 and 2026-08-05, including a single
#   continuous 1h40m outage that ended only because a human went looking. A
#   detector that prints the exact recovery command every five minutes and never
#   runs it is, operationally, indistinguishable from no watchdog at all.
#
#   The counter-argument to auto-restarting is real: a naive reviver pointed at
#   a genuinely broken binary becomes a restart loop that burns tokens, hides
#   the real fault, and hammers the forge. So this is deliberately NOT a naive
#   reviver. Four constraints bound it:
#
#     1. BOUNDED ATTEMPTS. At most LOOM_WATCHDOG_RECOVER_MAX_ATTEMPTS (default
#        5) recovery attempts per outage EPISODE — not per tick. The tally is
#        durable (<loom dir>/.watchdog-recovery-state) so it survives the fact
#        that each tick is a brand-new process, and it is cleared the moment a
#        tick observes a healthy daemon (that, not a timer, is what ends an
#        episode).
#     2. EXPONENTIAL BACKOFF. Attempt N is only made once
#        base × 2^(N-1) seconds (base LOOM_WATCHDOG_RECOVER_BACKOFF_SECS=60,
#        capped at LOOM_WATCHDOG_RECOVER_BACKOFF_CAP_SECS=1800) have elapsed
#        since the last attempt. Ticks inside the backoff window still report,
#        they just do not re-run the recovery command. The attempt is recorded
#        BEFORE the command runs, so an overlapping tick cannot double-fire it.
#     3. CIRCUIT BREAKER. Once the attempt budget is spent the breaker LATCHES
#        OPEN: no further automatic attempts are made for this episode, at all,
#        until either a tick observes a healthy daemon or an operator deletes
#        the state file. A broken binary therefore gets at most 5 restarts,
#        never an unbounded loop.
#     4. NEVER REVIVES A DELIBERATE STOP — KEYED ON THE MARKER, NOT THE EXIT
#        CODE (#6388). A scripted stop (loom-daemon-stop.sh) removes the
#        autonomy-desired marker BEFORE it kills the daemon, so a deliberate
#        stop never reaches this block at all — the marker-ABSENT branch near
#        the top of this script is the ONLY place "never revive" fires, and it
#        fires unconditionally there, before any exit code is ever read. A
#        signal-shaped exit code recorded HERE (marker confirmed present) —
#        launchd `last exit status` 143/130/-15/-2; systemd
#        ExecMainCode=killed with TERM/INT — used to be misread as the SAME
#        "operator stop" intent and skipped recovery outright; a stray SIGTERM
#        (e.g. an unrelated test run) then reads exactly like a deliberate
#        stop and starves autonomy for as long as no human notices (an 11h
#        outage, #6388). It is now just another kind of death: it gets the
#        SAME bounded recovery as any other crash, with the signal named in
#        the report text so an operator can see which rule fired.
#
#   WHAT IT RUNS: the sibling ./loom-daemon-start.sh — the exact command this
#   watchdog has always printed — replaying ONLY the autonomy flags the last
#   start persisted to `<state home>/.daemon.flags` (#3968), filtered through a
#   strict allowlist so the FLAGS-OFF/opt-in contract can never widen across a
#   recovery. The invocation is wrapped in a hard wall-clock budget
#   (LOOM_WATCHDOG_RECOVER_TIMEOUT_SECS, default 120) so a wedged start can
#   never wedge the tick. Override the whole command with
#   LOOM_WATCHDOG_RECOVER_CMD; disable recovery entirely with
#   LOOM_WATCHDOG_AUTO_RECOVER=0 (the watchdog then says so explicitly in its
#   own DIVERGENCE text, so an operator can never mistake a report-only host
#   for a self-healing one).
#
#   ESCALATION THAT DOES NOT REQUIRE TAILING A LOGFILE. When the breaker trips —
#   or when recovery is structurally impossible on this host (disabled, no
#   resolvable start script) and the outage has persisted for
#   LOOM_WATCHDOG_RECOVER_MAX_ATTEMPTS consecutive ticks — the watchdog files
#   ONE tracking issue on the forge via ./.loom/scripts/create-issue.sh (never a
#   bare `gh issue create`), reusing the escalation channel #5343 already
#   established in loom-daemon-start.sh. It is deduped by a persistent sentinel
#   (<loom dir>/.watchdog-outage-escalated) so a multi-hour outage files exactly
#   one issue, and the sentinel is cleared with the rest of the episode state as
#   soon as a daemon is seen healthy again — so the NEXT outage escalates again.
#   Best-effort and non-fatal: no forge auth, offline, or no create-issue.sh
#   degrades to the log line it always was.
#
# EXIT CODES (a StartInterval/OnUnitActiveSec job's exit code does not affect
# relaunch — these exist for testability and for a human running it by hand):
#   0  no divergence — daemon healthy, OR no daemon expected AND none running
#      (marker absent + nothing alive), OR the #4232/#4862 bounded
#      auto-remediation (see above) successfully relaunched it via
#      'launchctl kickstart' / 'systemctl --user start', OR (#5391) the
#      general-case bounded recovery successfully relaunched it via
#      loom-daemon-start.sh
#   1  DIVERGENCE / state mismatch reported — a daemon is expected but is not
#      running (and either the #4232 remediation gate did not apply, or it fired
#      but the daemon is still not confirmed running, or #5391's bounded
#      recovery ran/was deferred/was suppressed and the daemon is still down),
#      or is running but its
#      heartbeat is stale (possibly wedged), OR (#4398) it is running with a
#      fresh heartbeat but its bounded IPC round-trip failed, OR (#5944) its
#      round-trip succeeded THIS tick but failed often enough across the
#      recent windowed history to report DEGRADED anyway, OR (#4331) a daemon
#      IS running while the marker is ABSENT (crash protection disarmed — a WARN
#      state mismatch), OR (#5118) the socket answers while the supervisor says
#      its job is down (an UNSUPERVISED daemon — serving work, but not crash
#      protected)
#   2  usage error
#   3  (#5118) LIVENESS UNDETERMINED — deliberately NOT exit 1: no out-of-band
#      signal found a live daemon AND the in-band socket probe could not run (no
#      resolvable loom-daemon binary, the probe is disabled, or the CLI itself
#      never returned), so this tick has NO EVIDENCE either way. Reported as
#      UNKNOWN, never as "the daemon is down".
#
# Usage:
#   ./.loom/scripts/cli/loom-daemon-watchdog.sh            Check once, report on divergence
#   ./.loom/scripts/cli/loom-daemon-watchdog.sh --verbose  Also log the healthy/idle no-op cases
#   ./.loom/scripts/cli/loom-daemon-watchdog.sh --help
#
# Environment:
#   LOOM_AUTONOMY_MARKER           Path to the intent marker (default: derived
#                                  from LOOM_SOCKET_PATH's dir, else ~/.loom/autonomy-desired)
#   LOOM_WATCHDOG_LOG              Report log path (default: <loom dir>/logs/daemon-watchdog.log)
#   LOOM_DAEMON_HEARTBEAT_STALE_SECS  Staleness threshold in seconds (default:
#                                  max(5 × heartbeat cadence, 300))
#   LOOM_SOCKET_PATH              Override the daemon socket (its dir is the loom dir)
#   LOOM_PID_FILE                 #5118: the pid file path, honored END-TO-END —
#                                 the same precedence tier 1 the daemon itself
#                                 uses (daemon_pidfile.rs) and the value
#                                 loom-daemon-start.sh exports into every plist
#                                 / systemd unit it renders. Wins over the
#                                 marker's `pid_file=` field. The pid file is
#                                 only ever a CORROBORATING hint here: the
#                                 socket round-trip is authoritative.
#   LOOM_LAUNCHD_LABEL            macOS: the DAEMON label to probe (default com.rjwalters.loom-daemon)
#   LOOM_LAUNCHD_DOMAIN          macOS: pin the launchd domain (gui/<uid> or user/<uid>);
#                                else auto-resolved gui→user (#4130), matching the start
#   LOOM_DAEMON_LAUNCHD          0/false/no: treat as a non-launchd (nohup) daemon; check the pid file only
#   LOOM_SYSTEMD_UNIT             #4862: the systemd --user unit to probe (default
#                                loom-daemon.service, else the marker's systemd_unit)
#   LOOM_WATCHDOG_SYSTEMD_PROBE   #4862: 1/true/yes: opt in to the systemd --user
#                                probe (default: per the marker's use_systemd
#                                field, else OFF -- `systemctl` merely being on
#                                PATH is not proof of a systemd-managed daemon).
#                                0/false/no forces it off regardless of the marker.
#   LOOM_WATCHDOG_KICKSTART_RECHECK_ATTEMPTS  #4232/#4862/#5391: how many times to
#                                re-check for a live pid after the auto-kickstart /
#                                systemctl-start fallback, and after the #5391
#                                general-case recovery (default 3).
#   LOOM_WATCHDOG_KICKSTART_RECHECK_INTERVAL  #4232: seconds between re-checks
#                                (default 1; may be fractional).
#   LOOM_WATCHDOG_AUTO_RECOVER    #5391: 0/false/no disables the general-case
#                                bounded recovery entirely — the watchdog then
#                                becomes report-only for confirmed outages and
#                                SAYS SO in its own DIVERGENCE text (default: on).
#   LOOM_WATCHDOG_RECOVER_MAX_ATTEMPTS  #5391: circuit-breaker budget — recovery
#                                attempts per outage episode before the breaker
#                                latches open (default 5). Also the consecutive-tick
#                                threshold at which a structurally un-recoverable
#                                outage escalates.
#   LOOM_WATCHDOG_RECOVER_BACKOFF_SECS  #5391: base backoff; attempt N waits
#                                base × 2^(N-1) since the last attempt (default 60).
#   LOOM_WATCHDOG_RECOVER_BACKOFF_CAP_SECS  #5391: backoff ceiling (default 1800).
#   LOOM_WATCHDOG_RECOVER_TIMEOUT_SECS  #5391: hard wall-clock budget for the
#                                recovery command itself (default 120).
#   LOOM_WATCHDOG_RECOVER_CMD     #5391: override the recovery command (argv,
#                                word-split). Default: the sibling
#                                loom-daemon-start.sh + the allowlisted autonomy
#                                flags persisted in <state home>/.daemon.flags.
#   LOOM_WATCHDOG_RECOVERY_STATE  #5391: path to the durable episode state
#                                (default <loom dir>/.watchdog-recovery-state).
#   LOOM_WATCHDOG_ESCALATE        #5391: 0/false/no suppresses the forge-issue
#                                escalation when the breaker trips (default: on).
#   LOOM_WATCHDOG_ESCALATION_SENTINEL  #5391: dedupe sentinel for that escalation
#                                (default <loom dir>/.watchdog-outage-escalated).
#   LOOM_WATCHDOG_IPC_PROBE       #4398: 0/false/no disables the bounded in-band
#                                IPC probe entirely (pid + heartbeat checks only).
#   LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS  #4398: hard external budget for the
#                                probe (default 15). Must stay comfortably above
#                                the CLI's own 5s connect + 5s round-trip bound.
#   LOOM_WATCHDOG_IPC_PROBE_ARGS  #4398: the loom-daemon argv used as the probe
#                                (default 'quarantine list' — a pure IPC
#                                round-trip with no post-reply work).
#   LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD  #4398: consecutive failed probes
#                                before a hang is called CONFIRMED (default 3).
#   LOOM_WATCHDOG_IPC_PROBE_GRACE_SECS  #4398: post-relaunch socket-bind grace
#                                window; a younger process is not probed
#                                (default: LOOM_DAEMON_STARTUP_GRACE_SECS, else 90).
#   LOOM_WATCHDOG_IPC_PROBE_STATE  #4398: path to the consecutive-failure counter
#                                (default <loom dir>/.watchdog-probe-fail-count).
#   LOOM_WATCHDOG_IPC_PROBE_WINDOW_TICKS  #5944: size (in ticks) of the rolling
#                                window used for the rate-based failure signal,
#                                alongside (not instead of) the consecutive
#                                tally above (default 6 — 30 minutes at the
#                                default 300s cadence).
#   LOOM_WATCHDOG_IPC_PROBE_WINDOW_FAIL_THRESHOLD  #5944: failures within the
#                                last WINDOW_TICKS ticks that report a
#                                DEGRADED verdict even on a tick whose own
#                                round-trip succeeded (default 3). Report-only,
#                                distinct from the CONFIRMED-hang escalation:
#                                never triggers exit-1-no-remediation posture
#                                changes or #5391 bounded recovery on its own.
#   LOOM_WATCHDOG_IPC_PROBE_WINDOW_STATE  #5944: path to the rolling-window
#                                state (default <loom dir>/.watchdog-probe-window).
#   LOOM_WATCHDOG_LOAD_AVG_PROC_PATH  #5790: test seam for the /proc/loadavg
#                                source get_load_average() reads on Linux
#                                (default /proc/loadavg). Not meant for
#                                production use — point it at an unreadable
#                                path to exercise the "unavailable" degrade.
#   LOOM_FORCE_PORTABLE_TIMEOUT    #4398, renamed from the watchdog-local
#                                LOOM_WATCHDOG_FORCE_PORTABLE_TIMEOUT by #4832
#                                when bounded_run() moved to the shared
#                                lib/bounded-run.sh: 1 forces the built-in
#                                bounded runner instead of `timeout(1)` (test
#                                seam for the default macOS no-`timeout` shape).
#   LOOM_DAEMON_BIN               Explicit loom-daemon binary for the IPC probe
#                                (same resolution order as loom-daemon-start.sh).
#   LOOM_WATCHDOG_PEER_COORD_CHECK  #6222 (Layer 3 of #6157): 0/false/no disables
#                                the peer-coordination alert entirely (default: on).
#                                Only ever attempted on a tick whose own IPC
#                                round-trip already succeeded (`probe_verdict ==
#                                healthy`) — never adds a new hang surface.
#   LOOM_WATCHDOG_PEER_COORD_TIMEOUT_SECS  #6222: hard external budget for the
#                                `loom-daemon peer-claims --json` query (default:
#                                same as LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS).
#   LOOM_WATCHDOG_PEER_COORD_SENTINEL  #6222: dedupe sentinel for the peer-
#                                coordination escalation (default <loom dir>
#                                /.watchdog-peer-coordination-escalated). Stores
#                                `<timestamp> <issue-url>` so recovery can
#                                comment on and close the exact filed issue.
#   LOOM_WATCHDOG_CREATE_ISSUE_FALLBACK_DIR  #6272: test seam ONLY — overrides
#                                the third (production-intent) branch of the
#                                create-issue.sh resolution shared by
#                                escalate_daemon_outage() and
#                                escalate_peer_coordination_degraded(), which
#                                otherwise resolves relative to wherever THIS
#                                SCRIPT lives on disk (default
#                                $_LOOM_WATCHDOG_CLI_DIR/..), not relative to
#                                the sandboxable $repo_root the first two
#                                branches use. Not meant for production use —
#                                point it at an empty sandbox dir in tests that
#                                need "no create-issue.sh reachable anywhere"
#                                to be genuinely unreachable (#6271: an earlier
#                                test without this seam fell through to this
#                                repo's own real create-issue.sh and filed a
#                                spurious live issue).

set -uo pipefail

# ---------- output helpers ----------
if [[ -t 2 ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; NC=''
fi

show_help() {
    awk 'NR>=2 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
}

# Shared domain resolver (#4130): gui/<uid> ↦ user/<uid>, sourced verbatim so the
# watchdog probes the daemon in the same domain the start put it in.
_LOOM_LAUNCHD_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" 2>/dev/null && pwd)"
# This script's own directory — the #5391 general-case recovery invokes its
# SIBLING loom-daemon-start.sh from here (never a PATH lookup: the watchdog runs
# from a launchd/systemd timer with a minimal, non-login environment, and the
# recovery must relaunch the daemon from the SAME installed tree that provisioned
# this watchdog, not whatever happens to be first on a stray PATH).
_LOOM_WATCHDOG_CLI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
if [[ -r "$_LOOM_LAUNCHD_LIB_DIR/launchd-domain.sh" ]]; then
    # shellcheck source=../lib/launchd-domain.sh
    source "$_LOOM_LAUNCHD_LIB_DIR/launchd-domain.sh"
fi
# bounded_run() (#4398, shared with loom-daemon-start.sh's print_calibrate_hint,
# de-duplicated from this script's own former inline copy by #4832) — the IPC
# probe's hard wall-clock budget. Unlike the start script's advisory hint, the
# probe below is NOT optional, so run_ipc_probe() checks explicitly for a
# missing `bounded_run` rather than letting an undefined-function call degrade
# silently.
if [[ -r "$_LOOM_LAUNCHD_LIB_DIR/bounded-run.sh" ]]; then
    # shellcheck source=../lib/bounded-run.sh
    source "$_LOOM_LAUNCHD_LIB_DIR/bounded-run.sh"
fi
# loom_locate_daemon_bin() (#4875, shared with loom-daemon-start.sh /
# loom-daemon-update.sh / loom-status.sh / `.loom/bin/loom health`) — includes
# the machine-level ~/.local/bin fallback so this watchdog (which runs from a
# systemd/launchd timer with a minimal, non-login environment) still finds a
# machine-level install even though $PATH never carries ~/.local/bin here.
if [[ -r "$_LOOM_LAUNCHD_LIB_DIR/locate-daemon-bin.sh" ]]; then
    # shellcheck source=../lib/locate-daemon-bin.sh
    source "$_LOOM_LAUNCHD_LIB_DIR/locate-daemon-bin.sh"
fi

VERBOSE=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h) show_help; exit 0 ;;
        --verbose|-v) VERBOSE=true; shift ;;
        *) echo "Unknown option '$1' (use --help)" >&2; exit 2 ;;
    esac
done

# ---------- path resolution (mirrors loom-daemon-start.sh / resolve_loom_dir) ----------
SOCKET_PATH="${LOOM_SOCKET_PATH:-$HOME/.loom/loom-daemon.sock}"
LOOM_DIR="$(dirname "$SOCKET_PATH")"
MARKER="${LOOM_AUTONOMY_MARKER:-$LOOM_DIR/autonomy-desired}"
WATCHDOG_LOG="${LOOM_WATCHDOG_LOG:-$LOOM_DIR/logs/daemon-watchdog.log}"

# ---------- IPC probe knobs (#4398) ----------
# The external budget must stay comfortably ABOVE the CLI's own worst case (5s
# connect + 5s round-trip, doubled by the single #4279 reconnect retry) so on a
# merely-slow-but-healthy daemon the CLI's own bound is always what fires first
# and this timeout is never the thing that manufactures a "hang".
PROBE_TIMEOUT_SECS="${LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS:-15}"
[[ "$PROBE_TIMEOUT_SECS" =~ ^[0-9]+$ ]] || PROBE_TIMEOUT_SECS=15
PROBE_ARGS="${LOOM_WATCHDOG_IPC_PROBE_ARGS:-quarantine list}"
# #6222 (Layer 3 of #6157): the peer-coordination alert's own external budget.
# Defaults to the same value as the general IPC probe's — it is the identical
# shape of call (a bounded round-trip through the installed CLI) — but stays
# independently overridable for tests and tuning.
PEER_COORD_TIMEOUT_SECS="${LOOM_WATCHDOG_PEER_COORD_TIMEOUT_SECS:-$PROBE_TIMEOUT_SECS}"
[[ "$PEER_COORD_TIMEOUT_SECS" =~ ^[0-9]+$ ]] || PEER_COORD_TIMEOUT_SECS="$PROBE_TIMEOUT_SECS"
PROBE_FAIL_THRESHOLD="${LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD:-3}"
[[ "$PROBE_FAIL_THRESHOLD" =~ ^[1-9][0-9]*$ ]] || PROBE_FAIL_THRESHOLD=3
# Mirrors daemon_install_state::DEFAULT_STARTUP_GRACE_SECS (90) and honors the
# same LOOM_DAEMON_STARTUP_GRACE_SECS override, so `loom-daemon status` and this
# watchdog can never disagree about when a young daemon is merely *starting*.
PROBE_GRACE_SECS="${LOOM_WATCHDOG_IPC_PROBE_GRACE_SECS:-${LOOM_DAEMON_STARTUP_GRACE_SECS:-90}}"
[[ "$PROBE_GRACE_SECS" =~ ^[0-9]+$ ]] || PROBE_GRACE_SECS=90
PROBE_STATE_FILE="${LOOM_WATCHDOG_IPC_PROBE_STATE:-$LOOM_DIR/.watchdog-probe-fail-count}"
# #5944: the WINDOWED/rate signal, alongside (never instead of) the
# consecutive tally above — see "A WINDOWED/RATE FAILURE SIGNAL" in the header
# for the full rationale.
PROBE_WINDOW_TICKS="${LOOM_WATCHDOG_IPC_PROBE_WINDOW_TICKS:-6}"
[[ "$PROBE_WINDOW_TICKS" =~ ^[1-9][0-9]*$ ]] || PROBE_WINDOW_TICKS=6
PROBE_WINDOW_FAIL_THRESHOLD="${LOOM_WATCHDOG_IPC_PROBE_WINDOW_FAIL_THRESHOLD:-3}"
[[ "$PROBE_WINDOW_FAIL_THRESHOLD" =~ ^[1-9][0-9]*$ ]] || PROBE_WINDOW_FAIL_THRESHOLD=3
PROBE_WINDOW_STATE_FILE="${LOOM_WATCHDOG_IPC_PROBE_WINDOW_STATE:-$LOOM_DIR/.watchdog-probe-window}"
# Set true once a probe divergence has been REPORTED on this tick, so the
# heartbeat section's otherwise-healthy exits still surface a non-zero code.
PROBE_DIVERGED=false
# #5944: overrides the default report_heartbeat_ok() fold-in note (see that
# function) when PROBE_DIVERGED was set for a reason OTHER than "this same
# tick's own probe failed" — currently only the windowed/rate signal, which
# can fire on a tick whose own round-trip succeeded.
PROBE_DIVERGED_NOTE=""
# #5790: test seam only (see the env-var doc block above) — production hosts
# should never set this.
LOAD_AVG_PROC_PATH="${LOOM_WATCHDOG_LOAD_AVG_PROC_PATH:-/proc/loadavg}"

# ---------- host load-average capture (#5790) ----------
# A DIVERGENCE report says "the IPC round-trip failed" but not WHY — attaching
# the host's load average lets an operator see at a glance whether the tick
# correlates with heavy contention (consistent with #4279's transient-failure
# rationale) or occurred on an otherwise-idle host (pointing at the daemon
# itself). Best-effort ONLY: never allowed to fail or slow down a tick, and
# degrades to the literal string "unavailable" if no source can be read on
# this platform, rather than aborting or inventing a number.
#
# Tried in this order:
#   1. `uptime` — the most portable source, present on both Linux and macOS/BSD
#      (with slightly different wording: "load average:" vs "load averages:";
#      the sed pattern below matches either).
#   2. `sysctl -n vm.loadavg` — the macOS-native fallback for a host whose
#      `uptime` output cannot be parsed (or whose text format changes).
#   3. /proc/loadavg — the Linux-native fallback, notably still present in
#      minimal containers that ship no `uptime` binary at all.
get_load_average() {
    local out=""
    if command -v uptime >/dev/null 2>&1; then
        out="$(uptime 2>/dev/null | sed -n 's/.*load average[s]*: *//p')"
    fi
    if [[ -z "$out" ]] && command -v sysctl >/dev/null 2>&1; then
        out="$(sysctl -n vm.loadavg 2>/dev/null | tr -d '{}')"
    fi
    if [[ -z "$out" ]] && [[ -r "$LOAD_AVG_PROC_PATH" ]]; then
        out="$(cut -d' ' -f1-3 "$LOAD_AVG_PROC_PATH" 2>/dev/null)"
    fi
    # Trim leading/trailing whitespace picked up from `uptime`/`sysctl` output.
    # xargs is a portable no-dependency trim; read -r would also work but this
    # matches the trim idiom already used elsewhere in this script.
    out="$(printf '%s' "$out" | xargs 2>/dev/null || true)"
    if [[ -n "$out" ]]; then
        printf '%s' "$out"
    else
        printf 'unavailable'
    fi
}

# ---------- general-case bounded-recovery knobs (#5391) ----------
# See "GENERAL-CASE BOUNDED RECOVERY + CIRCUIT BREAKER (#5391)" in the header for
# why every one of these bounds exists. Each falls back to its documented default
# on a malformed value rather than erroring: a scheduled tick must never abort on
# a typo'd env var and leave the host with no detector at all.
RECOVER_ENABLED=true
[[ "${LOOM_WATCHDOG_AUTO_RECOVER:-}" =~ ^(0|false|no)$ ]] && RECOVER_ENABLED=false
RECOVER_MAX_ATTEMPTS="${LOOM_WATCHDOG_RECOVER_MAX_ATTEMPTS:-5}"
[[ "$RECOVER_MAX_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || RECOVER_MAX_ATTEMPTS=5
RECOVER_BACKOFF_SECS="${LOOM_WATCHDOG_RECOVER_BACKOFF_SECS:-60}"
[[ "$RECOVER_BACKOFF_SECS" =~ ^[0-9]+$ ]] || RECOVER_BACKOFF_SECS=60
RECOVER_BACKOFF_CAP_SECS="${LOOM_WATCHDOG_RECOVER_BACKOFF_CAP_SECS:-1800}"
[[ "$RECOVER_BACKOFF_CAP_SECS" =~ ^[0-9]+$ ]] || RECOVER_BACKOFF_CAP_SECS=1800
RECOVER_TIMEOUT_SECS="${LOOM_WATCHDOG_RECOVER_TIMEOUT_SECS:-120}"
[[ "$RECOVER_TIMEOUT_SECS" =~ ^[0-9]+$ ]] || RECOVER_TIMEOUT_SECS=120
RECOVERY_STATE_FILE="${LOOM_WATCHDOG_RECOVERY_STATE:-$LOOM_DIR/.watchdog-recovery-state}"
ESCALATION_SENTINEL="${LOOM_WATCHDOG_ESCALATION_SENTINEL:-$LOOM_DIR/.watchdog-outage-escalated}"
# #6222 (Layer 3 of #6157): dedupe sentinel for the peer-coordination alert,
# separate from the daemon-outage sentinel above — the two escalations are
# independent episodes (a daemon can be fully alive and answering while its
# peer-claim receive path is degraded). Stores "<timestamp> <issue-url>" (not
# just a timestamp, unlike ESCALATION_SENTINEL) so the recovery path below can
# comment on and close the EXACT issue this episode filed.
PEER_COORD_SENTINEL="${LOOM_WATCHDOG_PEER_COORD_SENTINEL:-$LOOM_DIR/.watchdog-peer-coordination-escalated}"

# Append a timestamped line to the watchdog log (best-effort) and echo to
# stderr, which launchd captures to the job's StandardErrorPath. This IS the
# report — the durable, operator-visible signal that a pull never surfaced.
report() {
    local level="$1"; shift
    local msg="$*"
    local ts
    ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    mkdir -p "$(dirname "$WATCHDOG_LOG")" 2>/dev/null || true
    echo "$ts [$level] $msg" >> "$WATCHDOG_LOG" 2>/dev/null || true
    case "$level" in
        DIVERGENCE) echo -e "${RED}$ts [$level] $msg${NC}" >&2 ;;
        OK)         [[ "$VERBOSE" == "true" ]] && echo -e "${GREEN}$ts [$level] $msg${NC}" >&2 ;;
        *)          echo -e "${YELLOW}$ts [$level] $msg${NC}" >&2 ;;
    esac
}

# Section 4 (the heartbeat-derived reality check) has several exit paths that
# each end in a plain `report OK "daemon healthy/alive ..."` line. Before
# #5790, that call happened UNCONDITIONALLY — even on a tick where the #4398
# IPC probe above already logged a sub-threshold DIVERGENCE
# (PROBE_DIVERGED=true, loom-daemon-watchdog.sh's probe-verdict case
# statement). An operator or log-scraper reading only the log's last line, or
# grepping for `[OK]`, would see a clean bill of health in the SAME tick a
# divergence was reported moments earlier — exactly the "[OK] daemon healthy"
# blind spot #5790 reports. exit_ok() already made the EXIT CODE correct
# (non-zero when PROBE_DIVERGED); this closes the matching gap in the LOG
# TEXT: every section-4 OK-shaped call must route through here instead of
# calling `report OK` directly, so a diverged tick is never described as
# unambiguously healthy in its own log line.
report_heartbeat_ok() { # <message, matches the historical "report OK" text>
    local msg="$*"
    if [[ "$PROBE_DIVERGED" == "true" ]]; then
        # #5944: PROBE_DIVERGED_NOTE lets a diverging path OTHER than "this
        # same tick's own probe failed" (currently only the windowed/rate
        # signal, which can fire on a tick whose round-trip just succeeded)
        # override the historical note text below with one that describes
        # itself accurately instead of pointing at a DIVERGENCE line that, on
        # that path, was never printed this tick.
        local note="${PROBE_DIVERGED_NOTE:-the IPC probe diverged earlier this tick (see the DIVERGENCE line above) — dispatch may be degraded despite a fresh/liveness-only-OK heartbeat signal; the exit code for this tick reflects the divergence, not this line.}"
        report DEGRADED "${msg} NOTE: ${note}"
    else
        report OK "$msg"
    fi
}

# ---------- marker reader ----------
# Defined UP HERE (moved by #5118) rather than beside the marker-present path
# below: locate_daemon_bin() consults `marker_get repo_root`, and #5118's
# socket probe now runs on the marker-ABSENT path too, which executes before
# that path's own code. A function must exist at CALL time, so this has to be
# defined before section 1 runs.
marker_get() {
    local key="$1"
    # First matching `key=value` line; strip the key= prefix. Comments start '#'.
    [[ -f "$MARKER" ]] || return 0
    grep -E "^${key}=" "$MARKER" 2>/dev/null | head -n1 | cut -d= -f2-
}

# ---------- pid-file path resolution (#5118) ----------
# Mirrors the daemon's own `daemon_pidfile::resolve_pid_file_path_from`
# precedence EXACTLY so the two ends can never mean different files — the #5118
# path disagreement (this script read `<socket dir>/.daemon.pid`; the daemon
# wrote `<workspace>/.loom/.daemon.pid`) was possible only because each side
# derived its own path:
#   1. LOOM_PID_FILE            — explicit, exported by loom-daemon-start.sh and
#                                 baked into the rendered plist / systemd unit,
#                                 so every supervisor relaunch resolves it too.
#   2. the marker's `pid_file=` — the path the start script chose on THIS host
#                                 (i.e. what it exported as LOOM_PID_FILE).
#   3. LOOM_MACHINE_CHECKOUT    — machine mode keeps runtime state in the
#                                 machine-level loom dir.
#   4. LOOM_WORKSPACE / the marker's repo_root — repo mode: `<repo>/.loom`.
#   5. `<loom dir>/.daemon.pid` — the daemon's own final fallback.
# Whatever this returns is only ever a CORROBORATING hint; an absent or stale
# file must not by itself produce a divergence (that is the whole bug).
resolve_pid_file() { # [marker pid_file value]
    local from_marker="${1:-}"
    if [[ -n "${LOOM_PID_FILE:-}" ]]; then
        echo "$LOOM_PID_FILE"; return 0
    fi
    if [[ -n "$from_marker" ]]; then
        echo "$from_marker"; return 0
    fi
    if [[ -n "${LOOM_MACHINE_CHECKOUT:-}" ]]; then
        echo "$LOOM_DIR/.daemon.pid"; return 0
    fi
    local workspace="${LOOM_WORKSPACE:-$(marker_get repo_root)}"
    if [[ -n "$workspace" ]]; then
        echo "$workspace/.loom/.daemon.pid"; return 0
    fi
    echo "$LOOM_DIR/.daemon.pid"
}

# ---------- reality probe (shared) ----------
# Determine whether the expected daemon is actually alive. Reads the resolved
# USE_LAUNCHD / USE_SYSTEMD / LABEL / SYSTEMD_UNIT / PID_FILE and sets globals
# the callers branch on:
#   daemon_alive     true|false
#   liveness_detail  human-readable string (mirrored into status/log messages)
#   job_loaded       true|false — launchd job / systemd unit known but with no
#                    live pid (feeds the #4232 / #4862 bounded auto-remediation
#                    gates)
#   launchd_service  <domain>/<label> for the launchd path (else empty)
#   systemd_service  <unit> for the systemd path (else empty)
#   liveness_source  launchd|systemd|pidfile — WHICH out-of-band signal was
#                    consulted (#5118: a supervisor's "job is down" is real
#                    evidence; a missing pid file is not, so the two must be
#                    told apart by the callers)
#   pidfile_evidence alive|dead|absent — only meaningful on the pidfile path
#                    (#5118: `dead` — a file naming a non-live pid — is positive
#                    evidence; `absent` is no evidence at all)
# Factored out (#4331) so the no-marker state-mismatch check below and the
# marker-present path below run the IDENTICAL liveness logic — they can never
# diverge on what "alive" means.
detect_daemon_liveness() {
    daemon_alive=false
    liveness_detail=""
    job_loaded=false
    launchd_service=""
    systemd_service=""
    live_pid=""
    liveness_source=pidfile
    pidfile_evidence=absent
    if [[ "$USE_LAUNCHD" == "true" ]] && command -v launchctl >/dev/null 2>&1; then
        # Resolve the domain (gui/<uid> ↦ user/<uid>, #4130) the same way the
        # start did, so a headless daemon in user/<uid> is probed correctly.
        liveness_source=launchd
        launchd_service="$(resolve_launchd_domain)/${LABEL}"
        launchd_print_output="$(launchctl print "$launchd_service" 2>/dev/null)"
        launchd_print_rc=$?
        launchd_pid="$(printf '%s\n' "$launchd_print_output" | awk -F'= ' '/^[[:space:]]*pid = /{gsub(/[^0-9]/, "", $2); print $2; exit}')"
        if [[ -n "$launchd_pid" ]] && kill -0 "$launchd_pid" 2>/dev/null; then
            daemon_alive=true
            liveness_detail="launchd job $launchd_service alive (pid $launchd_pid)"
            live_pid="$launchd_pid"
        elif [[ "$launchd_print_rc" -eq 0 ]]; then
            job_loaded=true
            liveness_detail="launchd job $launchd_service is LOADED but NOT running (no live pid)"
        else
            liveness_detail="launchd job $launchd_service is not loaded/alive"
        fi
    elif [[ "$USE_SYSTEMD" == "true" ]] && command -v systemctl >/dev/null 2>&1; then
        # systemd --user path (#4862): mirrors the launchd branch above so the
        # #4232-style auto-remediation gate below has an equivalent signal on
        # Linux. `show -p X --value` against an unknown unit answers cleanly
        # (LoadState=not-found) rather than erroring, so no separate rc check
        # is needed the way launchd's print exit code is used above.
        liveness_source=systemd
        systemd_service="$SYSTEMD_UNIT"
        systemd_main_pid="$(systemctl --user show -p MainPID --value "$systemd_service" 2>/dev/null)"
        if [[ -n "$systemd_main_pid" && "$systemd_main_pid" != "0" ]] && kill -0 "$systemd_main_pid" 2>/dev/null; then
            daemon_alive=true
            liveness_detail="systemd unit $systemd_service alive (pid $systemd_main_pid)"
            live_pid="$systemd_main_pid"
        else
            systemd_load_state="$(systemctl --user show -p LoadState --value "$systemd_service" 2>/dev/null)"
            if [[ "$systemd_load_state" == "loaded" ]]; then
                job_loaded=true
                liveness_detail="systemd unit $systemd_service is LOADED but NOT running (no live MainPID)"
            else
                liveness_detail="systemd unit $systemd_service is not loaded/alive"
            fi
        fi
    else
        # Non-launchd, non-systemd (nohup) path: the pid file is the only
        # OUT-OF-BAND signal — and, since #5118, explicitly the WEAKEST one.
        # A live pid here is still accepted as liveness (cheap, and correct
        # whenever the file is fresh), but neither of the negative outcomes
        # below may end the check: the caller asks the socket before reporting.
        liveness_source=pidfile
        if [[ -n "$PID_FILE" && -f "$PID_FILE" ]]; then
            pid="$(cat "$PID_FILE" 2>/dev/null || true)"
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                daemon_alive=true
                pidfile_evidence=alive
                liveness_detail="pid $pid (from $PID_FILE) alive"
                live_pid="$pid"
            else
                pidfile_evidence=dead
                liveness_detail="pid file $PID_FILE present but pid not alive"
            fi
        else
            pidfile_evidence=absent
            liveness_detail="no live pid file at ${PID_FILE:-<none>}"
        fi
    fi
}

# Parse a `ps -o etime=` duration ([[dd-]hh:]mm:ss) into whole seconds —
# mirrors the Rust probe's `parse_etime` exactly (`daemon_install_state.rs`,
# #4368) so the two never disagree on a process's age. Any unexpected shape
# or non-numeric field fails (exit 1, nothing echoed) — the caller treats an
# unparseable age as *unknown* and makes no prior-boot claim, never a false
# one either way.
parse_etime_secs() {
    local raw days rest hours minutes seconds parts
    raw="$(printf '%s' "${1:-}" | tr -d '[:space:]')"
    [[ -z "$raw" ]] && return 1
    days=0
    rest="$raw"
    if [[ "$raw" == *-* ]]; then
        days="${raw%%-*}"
        rest="${raw#*-}"
        [[ "$days" =~ ^[0-9]+$ ]] || return 1
    fi
    IFS=':' read -r -a parts <<< "$rest"
    case "${#parts[@]}" in
        1) hours=0; minutes=0; seconds="${parts[0]}" ;;
        2) hours=0; minutes="${parts[0]}"; seconds="${parts[1]}" ;;
        3) hours="${parts[0]}"; minutes="${parts[1]}"; seconds="${parts[2]}" ;;
        *) return 1 ;;
    esac
    [[ "$hours" =~ ^[0-9]+$ && "$minutes" =~ ^[0-9]+$ && "$seconds" =~ ^[0-9]+$ ]] || return 1
    echo $(( days * 86400 + hours * 3600 + minutes * 60 + seconds ))
}

# Live process age in seconds via `ps -o etime= -p <pid>`, degrading to
# nothing (no output, non-zero return) on any failure — the caller must never
# turn an unknown age into a false prior-boot claim (#4368).
process_age_secs() {
    local pid="$1" etime
    [[ -n "$pid" ]] || return 1
    etime="$(ps -o etime= -p "$pid" 2>/dev/null)" || return 1
    parse_etime_secs "$etime"
}

# ---------- bounded in-band IPC probe (#4398) ----------

# Resolve the loom-daemon binary the probe should invoke, via the shared
# loom_locate_daemon_bin() (lib/locate-daemon-bin.sh, sourced above) so this
# watchdog, loom-daemon-start.sh, loom-daemon-update.sh, loom-status.sh and
# `.loom/bin/loom health` can never disagree about which binary is "the"
# daemon CLI. Preserves this function's original contract (thin wrapper):
# echoes nothing and returns 1 when nothing is resolvable — the caller must
# then SKIP the probe, never report a divergence: a watchdog that pages
# because its own optional helper is missing is worse than one that quietly
# keeps doing the other two checks.
locate_daemon_bin() {
    local root bin
    root="$(marker_get repo_root 2>/dev/null)"
    bin="$(loom_locate_daemon_bin "$root")"
    [[ -n "$bin" ]] || return 1
    echo "$bin"
}

# bounded_run() — a HARD wall-clock budget around a command, returning 124 on
# timeout exactly like GNU `timeout` does. Sourced from the shared
# lib/bounded-run.sh (#4807 extracted this watchdog's own inline copy so
# loom-daemon-start.sh's print_calibrate_hint() could reuse it, #4799); see
# that file for the full implementation notes (the `-k 2` KILL escalation,
# the portable no-`timeout(1)` fallback for macOS, the 143/137 -> 124
# normalization). The lib is sourced just below (near launchd-domain.sh); if
# sourcing failed for any reason, `bounded_run` is simply undefined and
# run_ipc_probe() below detects that explicitly and skips the probe with a
# clear diagnostic — never a raw `command not found` on a scheduled tick
# (#4832).

# Read the consecutive-failure tally for <pid> from the state file. The tally is
# KEYED TO THE PID: a relaunched daemon is a different process and must start
# its own streak, never inherit the wedged predecessor's (which would escalate a
# healthy fresh daemon on its first failed tick).
probe_fail_count_for_pid() { # <pid>
    local pid="$1" saved_pid saved_count
    [[ -r "$PROBE_STATE_FILE" ]] || { echo 0; return 0; }
    read -r saved_pid saved_count < "$PROBE_STATE_FILE" 2>/dev/null || { echo 0; return 0; }
    [[ "$saved_pid" == "$pid" && "$saved_count" =~ ^[0-9]+$ ]] || { echo 0; return 0; }
    echo "$saved_count"
}

probe_fail_count_write() { # <pid> <count>
    mkdir -p "$(dirname "$PROBE_STATE_FILE")" 2>/dev/null
    printf '%s %s\n' "$1" "$2" > "$PROBE_STATE_FILE" 2>/dev/null || true
}

probe_fail_count_clear() {
    rm -f "$PROBE_STATE_FILE" 2>/dev/null || true
}

# ---------- windowed/rate failure tally (#5944) ----------
# A SECOND, INDEPENDENT tally alongside probe_fail_count_* above. That one is
# reset to zero by a single success (by design, for the CONFIRMED-hang
# decision); this one is not — see "A WINDOWED/RATE FAILURE SIGNAL" in the
# header for the full rationale. Format mirrors PROBE_STATE_FILE (`<pid>
# <payload>`) but the payload is a fixed-width HISTORY STRING instead of a
# count: one character per tick, oldest first, '0' for a healthy round-trip
# and '1' for unresponsive, capped at PROBE_WINDOW_TICKS characters. Keyed to
# the live pid exactly like the consecutive tally, for the identical reason
# (#4398: a relaunched daemon is a different process and must never inherit
# its wedged predecessor's history).

# Echoes the pid-keyed history string for <pid>, or "" (never an error) for a
# missing, malformed, or pid-mismatched state file — a fresh/relaunched pid
# therefore always starts with an empty window, exactly like
# probe_fail_count_for_pid.
probe_window_read() { # <pid>
    local pid="$1" saved_pid saved_hist
    [[ -r "$PROBE_WINDOW_STATE_FILE" ]] || { echo ""; return 0; }
    read -r saved_pid saved_hist < "$PROBE_WINDOW_STATE_FILE" 2>/dev/null || { echo ""; return 0; }
    [[ "$saved_pid" == "$pid" ]] || { echo ""; return 0; }
    echo "$saved_hist"
}

probe_window_write() { # <pid> <history>
    mkdir -p "$(dirname "$PROBE_WINDOW_STATE_FILE")" 2>/dev/null
    printf '%s %s\n' "$1" "$2" > "$PROBE_WINDOW_STATE_FILE" 2>/dev/null || true
}

# Append this tick's outcome to <pid>'s window, slide it to at most
# PROBE_WINDOW_TICKS characters (dropping the OLDEST entry, not the newest),
# persist it, and set two globals the caller reads immediately afterward:
#   PROBE_WINDOW_LEN         entries currently in the window (<= PROBE_WINDOW_TICKS;
#                             only fewer while the window has not filled yet)
#   PROBE_WINDOW_FAIL_COUNT  how many of those entries are failures ('1')
# Deliberately called for `healthy` and `unresponsive` verdicts only — a
# `skipped` verdict (startup grace, no resolvable binary, probe disabled, a
# daemon-side application error that PROVES IPC works, ...) is not evidence
# either way and must never be recorded into the window, mirroring the
# consecutive tally's own "none of these may increment the tally" contract.
probe_window_record() { # <pid> <outcome: 0=healthy 1=unresponsive>
    local pid="$1" outcome="$2" hist
    hist="$(probe_window_read "$pid")${outcome}"
    if (( ${#hist} > PROBE_WINDOW_TICKS )); then
        hist="${hist: -${PROBE_WINDOW_TICKS}}"
    fi
    probe_window_write "$pid" "$hist"
    PROBE_WINDOW_LEN=${#hist}
    PROBE_WINDOW_FAIL_COUNT="$(printf '%s' "$hist" | tr -dc '1' | wc -c | tr -d '[:space:]')"
}

# Run the bounded probe command ONCE. Extracted from run_ipc_probe() by #5118 so
# the post-liveness hang probe and the new pre-report socket-liveness probe
# invoke the daemon CLI through the IDENTICAL code path (same binary
# resolution, same argv, same hard budget) and can never disagree about what
# "the daemon answered" means.
#
# Sets PROBE_RUN_RC / PROBE_RUN_OUTPUT / PROBE_RUN_BIN and returns 0 when the
# command actually ran; returns 1 with PROBE_RUN_SKIP_REASON set when it could
# NOT be run at all (disabled, no bounded_run, no binary, empty argv). The
# distinction matters: "ran and failed" is evidence, "could not run" is not.
invoke_probe_command() {
    PROBE_RUN_RC=0
    PROBE_RUN_OUTPUT=""
    PROBE_RUN_BIN=""
    PROBE_RUN_SKIP_REASON=""

    if [[ "${LOOM_WATCHDOG_IPC_PROBE:-}" =~ ^(0|false|no)$ ]]; then
        PROBE_RUN_SKIP_REASON="IPC probe disabled via LOOM_WATCHDOG_IPC_PROBE"
        return 1
    fi

    # #4832: bounded_run is defined by sourcing lib/bounded-run.sh above. A
    # missing/unreadable lib file leaves it undefined -- without this explicit
    # check, the invocation below would fail as a raw shell "command not
    # found" (rc 127) on every scheduled tick instead of a diagnosed skip.
    if ! command -v bounded_run >/dev/null 2>&1; then
        PROBE_RUN_SKIP_REASON="bounded_run is undefined -- lib/bounded-run.sh failed to source (missing or unreadable)"
        return 1
    fi

    local bin
    bin="$(locate_daemon_bin)" || bin=""
    if [[ -z "$bin" ]]; then
        PROBE_RUN_SKIP_REASON="no loom-daemon binary resolvable (LOOM_DAEMON_BIN unset, not on PATH, no in-repo build)"
        return 1
    fi
    PROBE_RUN_BIN="$bin"

    local -a probe_argv
    # shellcheck disable=SC2206  # deliberate word-splitting of the argv override
    read -r -a probe_argv <<< "$PROBE_ARGS"
    if (( ${#probe_argv[@]} == 0 )); then
        PROBE_RUN_SKIP_REASON="LOOM_WATCHDOG_IPC_PROBE_ARGS is empty — nothing to probe with"
        return 1
    fi

    local out rc=0
    out="$(mktemp "${TMPDIR:-/tmp}/loom-watchdog-probe.XXXXXX" 2>/dev/null)" || out=""
    if [[ -n "$out" ]]; then
        LOOM_SOCKET_PATH="$SOCKET_PATH" bounded_run "$PROBE_TIMEOUT_SECS" \
            "$bin" "${probe_argv[@]}" > "$out" 2>&1
        rc=$?
        PROBE_RUN_OUTPUT="$(cat "$out" 2>/dev/null)"
        rm -f "$out" 2>/dev/null
    else
        LOOM_SOCKET_PATH="$SOCKET_PATH" bounded_run "$PROBE_TIMEOUT_SECS" \
            "$bin" "${probe_argv[@]}" >/dev/null 2>&1
        rc=$?
    fi
    PROBE_RUN_RC=$rc
    return 0
}

# ---------- authoritative socket-liveness probe (#5118) ----------
# Ask the daemon's own socket whether ANYTHING is serving there, and classify
# the answer for the liveness decision (NOT the hang decision — that is
# run_ipc_probe's job, and it only runs once liveness is established).
#
# Sets:
#   socket_verdict  answered | unreachable | indeterminate
#   socket_detail   human-readable reason, mirrored into every report
#
# The three verdicts are deliberately asymmetric about what counts as evidence:
#   answered      a round-trip completed, OR the daemon replied with an
#                 application-level error (which PROVES the socket is served),
#                 OR the CLI itself says the daemon is alive-but-starting.
#   unreachable   positive evidence of absence: the CLI could not reach the
#                 socket (no listener / connect refused / connect timed out).
#   indeterminate everything else — the probe could not be run, the CLI itself
#                 never returned, or the daemon looks alive-but-wedged. The
#                 caller must report UNKNOWN, never "the daemon is down".
probe_socket_liveness() {
    socket_verdict=indeterminate
    socket_detail=""

    if ! invoke_probe_command; then
        socket_detail="could not ask the socket: ${PROBE_RUN_SKIP_REASON}"
        return 0
    fi

    local label
    label="$(basename "$PROBE_RUN_BIN") ${PROBE_ARGS}"
    case "$PROBE_RUN_RC" in
        0)
            socket_verdict=answered
            socket_detail="'${label}' round-tripped over ${SOCKET_PATH} within ${PROBE_TIMEOUT_SECS}s"
            return 0
            ;;
        124)
            socket_detail="'${label}' did NOT return within the ${PROBE_TIMEOUT_SECS}s probe budget — the CLI itself is wedged, so this tick proves nothing about the daemon either way"
            return 0
            ;;
        2|126|127)
            socket_detail="probe command '${label}' is unsupported by this binary (exit ${PROBE_RUN_RC}) — no in-band liveness signal available"
            return 0
            ;;
    esac

    if printf '%s' "$PROBE_RUN_OUTPUT" | grep -qiE 'alive-starting|socket has not bound'; then
        socket_verdict=answered
        socket_detail="the daemon is alive and STARTING (its socket is not bound yet) — alive, not gone"
    elif printf '%s' "$PROBE_RUN_OUTPUT" | grep -qiE 'alive-but-unresponsive'; then
        socket_detail="the CLI reports the daemon process as alive-but-unresponsive — alive-vs-gone is UNDETERMINED from this tick"
    elif printf '%s' "$PROBE_RUN_OUTPUT" | grep -qiE 'could not reach loom-daemon|connect timed out|connect failed|connection refused|no such file'; then
        socket_verdict=unreachable
        socket_detail="'${label}' could not reach anything at ${SOCKET_PATH} (exit ${PROBE_RUN_RC}): $(printf '%s' "$PROBE_RUN_OUTPUT" | head -n1)"
    elif printf '%s' "$PROBE_RUN_OUTPUT" | grep -qiE 'round-trip timed out|closed the connection without responding'; then
        socket_detail="'${label}' connected but the round-trip did not complete (exit ${PROBE_RUN_RC}) — a wedge, not a proven absence: $(printf '%s' "$PROBE_RUN_OUTPUT" | head -n1)"
    else
        socket_verdict=answered
        socket_detail="'${label}' exited ${PROBE_RUN_RC} with an application-level error (the daemon ANSWERED, so the socket is served): $(printf '%s' "$PROBE_RUN_OUTPUT" | head -n1)"
    fi
}

# Perform the bounded IPC round-trip and classify the outcome. Sets:
#   probe_verdict  healthy | unresponsive | skipped
#   probe_detail   human-readable reason (mirrored into the report)
# NEVER exits, never blocks longer than PROBE_TIMEOUT_SECS (+ the 2s KILL grace
# of the portable fallback).
run_ipc_probe() { # <live_pid> <proc_age_or_empty>
    probe_verdict=skipped
    probe_detail=""
    local pid="$1" age="$2"

    # Post-relaunch socket-bind window (#4213/#4331): a live pid whose socket is
    # not bound yet is STARTING, not wedged. Skip outright so it cannot count
    # toward a confirmed hang. An UNPARSEABLE age does not skip — `ps` failing
    # for a live pid is not a real deployment state, and silently disabling the
    # probe on it would reintroduce the blind spot; the N-consecutive debounce
    # still spans far more than any bind window. Checked BEFORE the invocation
    # so a young daemon is never even asked.
    if [[ "$age" =~ ^[0-9]+$ ]] && (( age < PROBE_GRACE_SECS )); then
        probe_detail="process is only ${age}s old (< ${PROBE_GRACE_SECS}s startup grace) — socket may not be bound yet"
        return 0
    fi

    # #5118: the invocation itself (probe-disabled / missing bounded_run /
    # unresolvable binary / empty argv checks included) now lives in the shared
    # invoke_probe_command(). Its skip reasons are the SAME "could not run at
    # all" set this function has always degraded on, so they still map to
    # `skipped`, never to a divergence.
    if ! invoke_probe_command; then
        probe_detail="$PROBE_RUN_SKIP_REASON"
        return 0
    fi

    local bin="$PROBE_RUN_BIN" rc="$PROBE_RUN_RC" probe_output="$PROBE_RUN_OUTPUT"

    case "$rc" in
        0)
            probe_verdict=healthy
            probe_detail="'$(basename "$bin") ${PROBE_ARGS}' round-tripped over ${SOCKET_PATH} within ${PROBE_TIMEOUT_SECS}s"
            return 0
            ;;
        124)
            # The CLI never returned inside our hard budget even though it bounds
            # its own connect/round-trip at 5s each — i.e. the binary itself is
            # not answering (the #4381 hung-stub shape).
            probe_verdict=unresponsive
            probe_detail="'$(basename "$bin") ${PROBE_ARGS}' did NOT return within the ${PROBE_TIMEOUT_SECS}s probe budget (the CLI's own 5s connect + 5s round-trip bounds did not even fire)"
            return 0
            ;;
        2|126|127)
            # clap usage error / not executable: this build's CLI does not
            # understand the probe, or the binary cannot be run at all. Skip.
            probe_verdict=skipped
            probe_detail="probe command '$(basename "$bin") ${PROBE_ARGS}' is unsupported by this binary (exit ${rc}) — skipping the IPC probe"
            return 0
            ;;
    esac

    # Any other non-zero exit: only call it UNRESPONSIVE on positive evidence
    # that the round-trip itself failed. A daemon that ANSWERED with an
    # application-level error proves IPC works, so it must degrade to `skipped`.
    if printf '%s' "$probe_output" | grep -qiE 'alive-starting|socket has not bound'; then
        probe_verdict=skipped
        probe_detail="probe reports the daemon is still STARTING (socket not bound yet) — not counted as a hang"
    elif printf '%s' "$probe_output" | grep -qiE 'could not reach loom-daemon|round-trip timed out|connect timed out|connect failed|closed the connection without responding|alive-but-unresponsive'; then
        probe_verdict=unresponsive
        probe_detail="'$(basename "$bin") ${PROBE_ARGS}' failed the IPC round-trip (exit ${rc}): $(printf '%s' "$probe_output" | head -n1)"
    else
        probe_verdict=skipped
        probe_detail="probe exited ${rc} without an IPC-failure signature (the daemon answered) — not counted as a hang: $(printf '%s' "$probe_output" | head -n1)"
    fi
}

# ---------- general-case bounded recovery (#5391) ----------
# Durable per-EPISODE state. Each watchdog tick is a brand-new process, so the
# attempt tally, the backoff clock and the breaker latch cannot live in memory
# the way the #4232/#4862 in-tick recheck loops do — they have to survive to the
# next tick or the "bounded" half of "bounded recovery" is meaningless (an
# in-memory counter reset every 300s IS an unbounded restart loop, just a slow
# one). Format is `key=value` lines, read with the same tolerant reader the
# marker uses: any malformed/missing field degrades to its zero value, which at
# worst starts a fresh episode — never a crash on a scheduled tick.
#   down_since    epoch of the first tick of this outage episode
#   ticks         consecutive down ticks observed in this episode
#   attempts      recovery attempts SPENT in this episode (the breaker budget)
#   last_attempt  epoch of the most recent attempt (the backoff clock)
recovery_state_get() { # <key>
    local key="$1"
    [[ -f "$RECOVERY_STATE_FILE" ]] || return 0
    grep -E "^${key}=" "$RECOVERY_STATE_FILE" 2>/dev/null | head -n1 | cut -d= -f2-
}

recovery_state_write() { # <down_since> <ticks> <attempts> <last_attempt>
    mkdir -p "$(dirname "$RECOVERY_STATE_FILE")" 2>/dev/null || true
    printf 'down_since=%s\nticks=%s\nattempts=%s\nlast_attempt=%s\n' \
        "$1" "$2" "$3" "$4" > "$RECOVERY_STATE_FILE" 2>/dev/null || true
}

# Ends the episode. Called from EVERY path that observes a healthy daemon —
# observing health, not elapsed time, is what closes an episode, so a daemon
# that flaps back up genuinely gets a fresh attempt budget while one that stays
# down does not. The escalation sentinel is cleared with it so the NEXT outage
# files its own tracking issue instead of being deduped against a resolved one.
recovery_state_clear() {
    rm -f "$RECOVERY_STATE_FILE" "$ESCALATION_SENTINEL" 2>/dev/null || true
}

# base × 2^(N-1), capped. Pure arithmetic, no subshell — this runs on every down
# tick. The cap is what stops a long outage from pushing the next attempt beyond
# any useful horizon once an operator does fix the underlying fault.
recovery_backoff_for() { # <attempt-number, 1-based>
    local n="$1" backoff="$RECOVER_BACKOFF_SECS" i=1
    while (( i < n )); do
        backoff=$(( backoff * 2 ))
        (( backoff >= RECOVER_BACKOFF_CAP_SECS )) && { backoff="$RECOVER_BACKOFF_CAP_SECS"; break; }
        i=$(( i + 1 ))
    done
    echo "$backoff"
}

# Classify the supervisor's recorded last-exit as a TERMINATION SIGNAL
# (SIGTERM/SIGINT) rather than a plain nonzero-exit fault. Sets
# `supervisor_exit_signal_detail` non-empty when it is — PURELY INFORMATIONAL
# (#6388). This USED TO be the #5391 recovery's hard "never revive a
# deliberate stop" guard (forcing `recover_possible=false` whenever it fired,
# under the name `detect_operator_stop_signature`), on the theory that
# SIGTERM/SIGINT is "the signature of an operator-initiated stop, not a
# fault". That conflated two different facts: "the process received a
# termination signal" and "the operator wants autonomy off". Only the
# autonomy-desired marker (`$MARKER`) records the second one — its own
# contract is "removed ONLY by an operator-initiated loom-daemon-stop.sh" —
# and this function is called ONLY from the general bounded-recovery block
# below, which is reached ONLY once the top-of-script intent gate
# (`[[ ! -f "$MARKER" ]]`) has already confirmed the marker IS present. A
# scripted stop removes the marker before it kills the daemon, so a genuine
# deliberate stop never reaches this function at all — the marker-ABSENT
# branch is what "never revive a deliberate stop" actually rests on. So a
# signal-shaped exit code recorded HERE (a hand-`kill`, or a stray SIGTERM
# from a wholly unrelated process — a test suite, in the 11h outage #6388
# reports) is evidence the process died, not evidence the operator wants it
# to stay down. It now flows into the SAME bounded-recovery path as any other
# crash; the caller uses this detail only to name the rule in its own report
# text (`report DIVERGENCE`), never to skip recovery.
detect_supervisor_exit_signal() {
    supervisor_exit_signal_detail=""
    if [[ -n "$launchd_service" ]] && command -v launchctl >/dev/null 2>&1; then
        local last_status
        last_status="$(launchctl print "$launchd_service" 2>/dev/null \
            | grep -oE 'last exit (code|status)[[:space:]]*=[[:space:]]*[-0-9]+' \
            | head -n1 | grep -oE '[-0-9]+$')"
        case "$last_status" in
            143|130|-15|-2)
                supervisor_exit_signal_detail="launchd records the job's last exit status as ${last_status} (SIGTERM/SIGINT)"
                ;;
        esac
    elif [[ -n "$systemd_service" ]] && command -v systemctl >/dev/null 2>&1; then
        local exec_code exec_status
        exec_code="$(systemctl --user show -p ExecMainCode --value "$systemd_service" 2>/dev/null)"
        exec_status="$(systemctl --user show -p ExecMainStatus --value "$systemd_service" 2>/dev/null)"
        if [[ "$exec_code" == "killed" ]]; then
            case "$exec_status" in
                TERM|INT|15|2)
                    supervisor_exit_signal_detail="systemd records the unit's main process as killed by SIG${exec_status}"
                    ;;
            esac
        elif [[ "$exec_code" == "exited" ]]; then
            case "$exec_status" in
                143|130)
                    supervisor_exit_signal_detail="systemd records the unit's main process as exiting ${exec_status} (SIGTERM/SIGINT)"
                    ;;
            esac
        fi
    fi
}

# Resolve the recovery argv into RECOVER_ARGV. Returns 1 (with
# RECOVER_ARGV_DETAIL explaining why) when nothing runnable exists — the caller
# must then report that fact explicitly, never silently do nothing.
#
# The default is the SIBLING loom-daemon-start.sh — literally the command every
# previous [DIVERGENCE] line told the operator to run — invoked through `bash`
# so a resynced install that lost its +x bit still recovers. Autonomy flags are
# replayed from the `.daemon.flags` record loom-daemon-start.sh persists (#3968)
# through a STRICT ALLOWLIST: the FLAGS-OFF/opt-in contract must not widen across
# an unattended recovery, and nothing outside the five autonomy flags that file
# can legitimately contain is ever passed through to an exec.
resolve_recovery_argv() {
    RECOVER_ARGV=()
    RECOVER_ARGV_DETAIL=""

    if [[ -n "${LOOM_WATCHDOG_RECOVER_CMD:-}" ]]; then
        # shellcheck disable=SC2206  # deliberate word-splitting of the argv override
        read -r -a RECOVER_ARGV <<< "$LOOM_WATCHDOG_RECOVER_CMD"
        if (( ${#RECOVER_ARGV[@]} == 0 )); then
            RECOVER_ARGV_DETAIL="LOOM_WATCHDOG_RECOVER_CMD is set but contains no command"
            return 1
        fi
        RECOVER_ARGV_DETAIL="LOOM_WATCHDOG_RECOVER_CMD override: ${RECOVER_ARGV[*]}"
        return 0
    fi

    local start_script="$_LOOM_WATCHDOG_CLI_DIR/loom-daemon-start.sh"
    if [[ ! -r "$start_script" ]]; then
        RECOVER_ARGV_DETAIL="no readable loom-daemon-start.sh beside this watchdog (${start_script}) — nothing to recover with"
        return 1
    fi
    RECOVER_ARGV=(bash "$start_script")

    local flags_file="" line
    [[ -n "$PID_FILE" ]] && flags_file="$(dirname "$PID_FILE")/.daemon.flags"
    if [[ -n "$flags_file" && -r "$flags_file" ]]; then
        while IFS= read -r line; do
            case "$line" in
                --from-config|--work-finder|--health-gate|--no-work-finder|--no-health-gate)
                    RECOVER_ARGV+=("$line") ;;
                *) : ;;   # anything else is dropped, deliberately and silently
            esac
        done < "$flags_file"
    fi
    RECOVER_ARGV_DETAIL="${RECOVER_ARGV[*]}"
    return 0
}

# Re-check liveness after a recovery attempt, reusing the SAME out-of-band probe
# the rest of this script uses plus one authoritative socket round-trip — so
# "recovered" means exactly what "healthy" means everywhere else in this file,
# never a weaker ad-hoc test. Returns 0 when a daemon is confirmed back.
# detect_daemon_liveness() clobbers liveness_detail, so the caller saves and
# restores it for the still-down report.
recovery_recheck_alive() {
    local attempts="${LOOM_WATCHDOG_KICKSTART_RECHECK_ATTEMPTS:-3}"
    local interval="${LOOM_WATCHDOG_KICKSTART_RECHECK_INTERVAL:-1}"
    [[ "$attempts" =~ ^[1-9][0-9]*$ ]] || attempts=3
    local i=0
    while (( i < attempts )); do
        detect_daemon_liveness
        [[ "$daemon_alive" == "true" ]] && return 0
        sleep "$interval" 2>/dev/null || sleep 1
        i=$(( i + 1 ))
    done
    # The out-of-band signals may legitimately lag a fresh relaunch (a pid file
    # not yet rewritten, a supervisor still settling). Ask the socket last: it is
    # the authoritative signal per #5118, and a daemon that ANSWERS is up no
    # matter what the pid file says.
    probe_socket_liveness
    if [[ "$socket_verdict" == "answered" ]]; then
        daemon_alive=true
        liveness_detail="a daemon ANSWERS on ${SOCKET_PATH} after recovery (${socket_detail})"
        return 0
    fi
    return 1
}

# ---------- out-of-band escalation when recovery cannot fix it (#5391) ----------
# The whole point of this issue: an operator must not have to tail
# daemon-watchdog.log to learn that autonomy died. Reuses the escalation channel
# #5343 already established in loom-daemon-start.sh (create-issue.sh, never a
# bare `gh issue create` — see CLAUDE.md), deduped by a persistent sentinel so a
# multi-hour outage files exactly ONE issue rather than one per 300s tick.
# Best-effort and NON-FATAL throughout: no create-issue.sh, no forge auth, or an
# offline host degrades back to the log line it always was.
escalate_daemon_outage() { # <reason-summary>
    local reason="$1"
    [[ "${LOOM_WATCHDOG_ESCALATE:-}" =~ ^(0|false|no)$ ]] && return 1
    [[ -f "$ESCALATION_SENTINEL" ]] && return 1

    local repo_root issue_script fallback_dir
    repo_root="$(marker_get repo_root)"
    issue_script=""
    if [[ -n "$repo_root" && -x "$repo_root/.loom/scripts/create-issue.sh" ]]; then
        issue_script="$repo_root/.loom/scripts/create-issue.sh"
    elif [[ -n "$repo_root" && -x "$repo_root/defaults/scripts/create-issue.sh" ]]; then
        issue_script="$repo_root/defaults/scripts/create-issue.sh"
    else
        # Branch 3 (production intent): find the sibling create-issue.sh next
        # to an INSTALLED watchdog, when neither of the $repo_root-relative
        # branches above found one. NOT sandboxable via $repo_root — by
        # default this resolves relative to wherever this script itself lives
        # on disk (#6272). A test that invokes this file from its real
        # in-repo path will silently fall through to THIS repo's own real,
        # gh-authenticated defaults/scripts/create-issue.sh here whenever
        # $repo_root has no copy of its own — already filed one spurious live
        # issue (#6271). Tests exercising "no create-issue.sh reachable
        # anywhere" MUST set LOOM_WATCHDOG_CREATE_ISSUE_FALLBACK_DIR to an
        # empty sandbox dir first (see test-loom-daemon-watchdog.sh's #6272
        # regression tests).
        fallback_dir="${LOOM_WATCHDOG_CREATE_ISSUE_FALLBACK_DIR:-$_LOOM_WATCHDOG_CLI_DIR/..}"
        [[ -x "$fallback_dir/create-issue.sh" ]] && issue_script="$fallback_dir/create-issue.sh"
    fi
    [[ -n "$issue_script" ]] || return 1

    local hostname_str body
    hostname_str="$(hostname 2>/dev/null || echo unknown-host)"
    body="$(cat <<EOF
\`loom-daemon-watchdog.sh\` has been unable to restore the loom-daemon on host
\`$hostname_str\`. Autonomous dispatch is DOWN and the watchdog bounded-recovery loop
has stopped trying — this issue is the escalation of last resort (#5391), filed so the
outage does not sit unnoticed in a logfile.

- **Host**: \`$hostname_str\`
- **Socket**: \`$SOCKET_PATH\`
- **Intent marker**: \`$MARKER\` (present — a daemon IS expected here)
- **Observed**: $liveness_detail
- **Why recovery stopped**: $reason
- **Recovery command**: \`${RECOVER_ARGV_DETAIL:-<none resolvable>}\`
- **Watchdog log**: \`$WATCHDOG_LOG\`
- **Episode state**: \`$RECOVERY_STATE_FILE\`

**To recover by hand**: run \`./.loom/scripts/cli/loom-daemon-start.sh [flags]\` on that
host and inspect \`loom-daemon status\`. The watchdog resumes automatic recovery (with a
fresh attempt budget) as soon as any tick observes a healthy daemon; deleting
\`$RECOVERY_STATE_FILE\` resets the circuit breaker immediately.

Filed automatically by the loom-daemon-watchdog.sh outage escalation (#5391). Deduped by a
sentinel at \`$ESCALATION_SENTINEL\`, which is cleared automatically once the daemon is
healthy again.
EOF
)"
    if "$issue_script" \
        --title "loom-daemon is DOWN on $hostname_str and watchdog recovery is exhausted" \
        --body "$body" \
        --label "loom:triage" >/dev/null 2>&1; then
        mkdir -p "$(dirname "$ESCALATION_SENTINEL")" 2>/dev/null || true
        date -u '+%Y-%m-%dT%H:%M:%SZ' > "$ESCALATION_SENTINEL" 2>/dev/null || true
        return 0
    fi
    return 1
}

# ---------- peer-coordination out-of-band alert (#6222, Layer 3 of #6157) ----------
# #6157/#6220 (Layers 1-2) made a degraded `peer_coordination` health section
# fail-visible to anything that already runs `loom-daemon health` by hand, and
# froze stale-claim reclamation while it holds. This is the deferred Layer 3:
# complain PROACTIVELY, the same way #5391's outage escalation above does —
# modeled on it directly, right down to the dedupe-sentinel shape — instead of
# waiting for an operator to think to check.
#
# Deliberately queries the THIN `loom-daemon peer-claims --json` wrapper, never
# `loom-daemon health --json`: the latter fans out a `gh` call PER MANAGED REPO
# for its `queues`/`throughput` sections (see the "LIGHTWEIGHT SUBCOMMAND, NOT
# `status`" note in this file's header — the same 15s-against-a-healthy-daemon
# trap that made `quarantine list` the IPC probe's own choice, not `status`).
# `peer-claims` is a single IPC round-trip with no such fan-out, carrying the
# exact `PeerCoordinationHealth` fields (`degraded`, `degraded_for_secs`,
# `consecutive_receives_toward_recovery`, `recovery_threshold`) this alert
# needs to be self-describing.
#
# Sets PEER_COORD_VERDICT to "Degraded" / "Green", plus PEER_COORD_SUMMARY and
# the individual fields below, on success; returns 1 with every field cleared
# when the state could not be determined this tick (disabled, no jq, no
# bounded_run, no resolvable binary, the query failed/timed out, or the JSON
# did not have the expected shape) — "could not tell" is never treated as
# evidence either way, mirroring every other best-effort probe in this file.
check_peer_coordination_health() {
    PEER_COORD_VERDICT=""
    PEER_COORD_SUMMARY=""
    PEER_COORD_DEGRADED_FOR=""
    PEER_COORD_CONSECUTIVE=""
    PEER_COORD_RECOVERY_THRESHOLD=""
    PEER_COORD_ADVERTISED=""
    PEER_COORD_RECEIVED=""

    [[ "${LOOM_WATCHDOG_PEER_COORD_CHECK:-}" =~ ^(0|false|no)$ ]] && return 1
    command -v jq >/dev/null 2>&1 || return 1
    command -v bounded_run >/dev/null 2>&1 || return 1

    local bin
    bin="$(locate_daemon_bin)" || return 1
    [[ -n "$bin" ]] || return 1

    local out rc json
    out="$(mktemp "${TMPDIR:-/tmp}/loom-watchdog-peercoord.XXXXXX" 2>/dev/null)" || return 1
    LOOM_SOCKET_PATH="$SOCKET_PATH" bounded_run "$PEER_COORD_TIMEOUT_SECS" \
        "$bin" peer-claims --json > "$out" 2>/dev/null
    rc=$?
    json="$(cat "$out" 2>/dev/null)"
    rm -f "$out" 2>/dev/null
    [[ "$rc" -eq 0 && -n "$json" ]] || return 1

    # NOTE: deliberately `| tostring`, never `.coordination.degraded // empty`.
    # jq's `//` treats `false` — not just `null`/missing — as falsy, so a
    # genuinely healthy `{"degraded": false, ...}` would silently collapse to
    # empty and be indistinguishable from "the field is absent" (an older
    # binary / malformed reply), permanently misreading every recovery as
    # "could not determine". `tostring` keeps `true`/`false` distinguishable
    # from the `null` a missing/absent field actually produces.
    local degraded
    degraded="$(printf '%s' "$json" | jq -r '.coordination.degraded | tostring' 2>/dev/null)"
    case "$degraded" in
        true) PEER_COORD_VERDICT="Degraded" ;;
        false) PEER_COORD_VERDICT="Green" ;;
        *) return 1 ;; # null / absent / not the expected shape — unknown
    esac

    PEER_COORD_DEGRADED_FOR="$(printf '%s' "$json" | jq -r '.coordination.degraded_for_secs // empty' 2>/dev/null)"
    PEER_COORD_CONSECUTIVE="$(printf '%s' "$json" | jq -r '.coordination.consecutive_receives_toward_recovery // empty' 2>/dev/null)"
    PEER_COORD_RECOVERY_THRESHOLD="$(printf '%s' "$json" | jq -r '.coordination.recovery_threshold // empty' 2>/dev/null)"
    PEER_COORD_ADVERTISED="$(printf '%s' "$json" | jq -r '.advertised // empty' 2>/dev/null)"
    PEER_COORD_RECEIVED="$(printf '%s' "$json" | jq -r '.received // empty' 2>/dev/null)"
    if [[ "$PEER_COORD_VERDICT" == "Degraded" ]]; then
        PEER_COORD_SUMMARY="peer-claim receive path DEGRADED (${PEER_COORD_RECEIVED:-0} received / ${PEER_COORD_ADVERTISED:-0} advertised), degraded for ${PEER_COORD_DEGRADED_FOR:-?}s — ${PEER_COORD_CONSECUTIVE:-0}/${PEER_COORD_RECOVERY_THRESHOLD:-?} sustained receive(s) toward recovery"
    else
        PEER_COORD_SUMMARY="peer-claim receive path healthy (${PEER_COORD_RECEIVED:-0} received / ${PEER_COORD_ADVERTISED:-0} advertised)"
    fi
    return 0
}

# File ONE tracking issue for a peer-coordination degradation episode, deduped
# by PEER_COORD_SENTINEL exactly like escalate_daemon_outage() dedupes on
# ESCALATION_SENTINEL — a multi-hour degradation must file exactly once, not
# once per tick. Unlike that sentinel (a bare timestamp), this one also stores
# the filed issue's URL so the recovery path can comment on and close the
# EXACT issue this episode filed, never a bare "clear and forget". Best-effort
# and NON-FATAL throughout: no create-issue.sh, no forge auth, or an offline
# host all degrade to the DIVERGENCE log line the caller already prints.
escalate_peer_coordination_degraded() {
    [[ "${LOOM_WATCHDOG_ESCALATE:-}" =~ ^(0|false|no)$ ]] && return 1
    [[ -f "$PEER_COORD_SENTINEL" ]] && return 1

    local repo_root issue_script fallback_dir
    repo_root="$(marker_get repo_root)"
    issue_script=""
    if [[ -n "$repo_root" && -x "$repo_root/.loom/scripts/create-issue.sh" ]]; then
        issue_script="$repo_root/.loom/scripts/create-issue.sh"
    elif [[ -n "$repo_root" && -x "$repo_root/defaults/scripts/create-issue.sh" ]]; then
        issue_script="$repo_root/defaults/scripts/create-issue.sh"
    else
        # Branch 3 (production intent): find the sibling create-issue.sh next
        # to an INSTALLED watchdog, when neither of the $repo_root-relative
        # branches above found one. NOT sandboxable via $repo_root — see the
        # identical comment in escalate_daemon_outage() above, and #6272 (the
        # same landmine, shared by both functions).
        fallback_dir="${LOOM_WATCHDOG_CREATE_ISSUE_FALLBACK_DIR:-$_LOOM_WATCHDOG_CLI_DIR/..}"
        [[ -x "$fallback_dir/create-issue.sh" ]] && issue_script="$fallback_dir/create-issue.sh"
    fi
    [[ -n "$issue_script" ]] || return 1

    local hostname_str body
    hostname_str="$(hostname 2>/dev/null || echo unknown-host)"
    body="$(cat <<EOF
\`loom-daemon health\`'s \`peer_coordination\` section has gone DEGRADED on host
\`$hostname_str\`. This host's one-way peer-claim RECEIVE path (Safehouse, #6157)
can no longer be trusted to prove another host has already claimed an issue.
This is diagnostic only since Epic #6165 Phase 4 (#6317): it no longer freezes
stale-claim reclamation, which now gates solely on the lease record (#6286)
(see \`.loom/docs/safehouse.md\` -> "Peer-claim coordination: cross-host soft
claim (#4028)").

- **Host**: \`$hostname_str\`
- **Verdict**: $PEER_COORD_SUMMARY
- **Degraded for**: ${PEER_COORD_DEGRADED_FOR:-unknown}s
- **Recovery progress**: ${PEER_COORD_CONSECUTIVE:-0}/${PEER_COORD_RECOVERY_THRESHOLD:-?} consecutive sustained receive(s) toward recovery
- **Watchdog log**: \`$WATCHDOG_LOG\`

**To recover by hand**: run \`loom-daemon peer-claims\` (or \`loom-daemon health\`)
on \`$hostname_str\` to confirm the live \`peer_coordination\` state, and check
Safehouse connectivity to peer hosts (\`.loom/docs/safehouse.md\`).

**This alert clears itself** — no manual close needed. Filed automatically by
the loom-daemon-watchdog.sh peer-coordination escalation (#6222, Layer 3 of
#6157). Deduped by a sentinel at \`$PEER_COORD_SENTINEL\`, which is cleared
automatically (and this issue commented on + closed) once a later watchdog
tick observes \`peer_coordination\` back to healthy.
EOF
)"
    local issue_url create_rc
    issue_url="$("$issue_script" \
        --title "peer-claim coordination is DEGRADED on $hostname_str (#6157 Layer 3)" \
        --body "$body" \
        --label "loom:triage" 2>/dev/null)"
    create_rc=$?
    [[ "$create_rc" -eq 0 && -n "$issue_url" ]] || return 1

    mkdir -p "$(dirname "$PEER_COORD_SENTINEL")" 2>/dev/null || true
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$issue_url" > "$PEER_COORD_SENTINEL" 2>/dev/null || true
    return 0
}

# Recovery counterpart: comment on and close the EXACT issue
# escalate_peer_coordination_degraded() filed, then clear the sentinel — the
# behavior #5391's own outage escalation never needed (that episode still
# requires operator action to actually fix; this one self-heals). Best-effort:
# a missing `gh`, no forge auth, or a failed close leaves the sentinel in place
# so a LATER healthy tick simply retries rather than silently losing track of
# an open tracking issue.
clear_peer_coordination_escalation() {
    [[ -f "$PEER_COORD_SENTINEL" ]] || return 1
    local ts issue_ref
    read -r ts issue_ref < "$PEER_COORD_SENTINEL" 2>/dev/null || true
    if [[ -z "$issue_ref" ]]; then
        # Malformed/legacy sentinel with no recorded issue reference — nothing
        # to close, so just clear it rather than retrying forever.
        rm -f "$PEER_COORD_SENTINEL" 2>/dev/null || true
        return 0
    fi
    command -v gh >/dev/null 2>&1 || return 1

    local hostname_str
    hostname_str="$(hostname 2>/dev/null || echo unknown-host)"
    gh issue comment "$issue_ref" --body "peer-claim coordination has RECOVERED on \`$hostname_str\` (${PEER_COORD_SUMMARY:-see 'loom-daemon peer-claims'}). Closing automatically — filed by the loom-daemon-watchdog.sh peer-coordination escalation (#6222)." >/dev/null 2>&1 || true

    if gh issue close "$issue_ref" --reason completed >/dev/null 2>&1; then
        rm -f "$PEER_COORD_SENTINEL" 2>/dev/null || true
        return 0
    fi
    return 1
}

# ---------- 1. intent: is a daemon expected at all? ----------
if [[ ! -f "$MARKER" ]]; then
    # A missing marker is SUPPOSED to mean "deliberately stopped (or never
    # started) — nothing to check". But the marker can go absent while a
    # supervised daemon is very much alive: an out-of-band delete, a failed
    # marker write, or a daemon rolled ONLY via `loom-daemon restart` / the
    # self-update loop (neither re-writes the marker — #4331). In that state the
    # daemon runs with crash protection DISARMED, and a bare `[OK] nothing to
    # check` hides exactly the gap the watchdog exists to surface. So before
    # staying quiet, cheaply probe reality with env-derived defaults.
    USE_LAUNCHD=true
    if [[ "${LOOM_DAEMON_LAUNCHD:-}" =~ ^(0|false|no)$ ]]; then
        USE_LAUNCHD=false
    fi
    [[ "$(uname -s)" == "Darwin" ]] || USE_LAUNCHD=false
    LABEL="${LOOM_LAUNCHD_LABEL:-com.rjwalters.loom-daemon}"
    # #4862: with no marker to read use_systemd/systemd_unit from, there is no
    # reliable signal that THIS host's daemon is systemd-managed rather than a
    # bystander user session with `systemctl` merely on PATH (every dev/test
    # host in this suite has that) -- so, unlike USE_LAUNCHD above (which is
    # genuinely platform-derived), the systemd probe stays OFF here unless
    # explicitly requested via LOOM_WATCHDOG_SYSTEMD_PROBE=1. The marker-present path
    # below (the common case — a daemon just started) gets it from the
    # use_systemd field loom-daemon-start.sh's systemd branch now writes.
    USE_SYSTEMD=false
    if [[ "$USE_LAUNCHD" != "true" ]] && [[ "$(uname -s)" != "Darwin" ]] \
        && [[ "${LOOM_WATCHDOG_SYSTEMD_PROBE:-}" =~ ^(1|true|yes)$ ]] && command -v systemctl >/dev/null 2>&1; then
        USE_SYSTEMD=true
    fi
    SYSTEMD_UNIT="${LOOM_SYSTEMD_UNIT:-loom-daemon.service}"
    # #5118: derived by the SAME precedence the daemon uses, not blindly from
    # the socket's directory (which is what made this file unfindable on a
    # workspace-rooted install).
    PID_FILE="$(resolve_pid_file "")"
    detect_daemon_liveness
    if [[ "$daemon_alive" != "true" ]]; then
        # #5118: the out-of-band signals found nothing — which, before this fix,
        # was accepted as "nothing is running" on the strength of a pid file
        # alone. Ask the socket before concluding: an unmarked-but-LIVE daemon
        # is exactly the #4331 state this section exists to surface, and it must
        # not be missed just because its pid file is absent or stale.
        probe_socket_liveness
        if [[ "$socket_verdict" == "answered" ]]; then
            daemon_alive=true
            liveness_detail="a daemon ANSWERS on ${SOCKET_PATH} (${socket_detail}); the out-of-band signal disagreed: ${liveness_detail}"
        fi
    fi
    if [[ "$daemon_alive" == "true" ]]; then
        report WARN \
            "STATE MISMATCH: no autonomy-desired marker at $MARKER, but a daemon IS running (${liveness_detail}). Crash protection is DISARMED — if this daemon dies the watchdog will NOT revive it. Heal it by restarting the daemon (it self-heals the marker at startup, #4331) or re-running ./.loom/scripts/cli/loom-daemon-start.sh; if the daemon should NOT be running, stop it with ./.loom/scripts/cli/loom-daemon-stop.sh."
        exit 1
    fi
    # Nothing alive ⇒ the load-bearing quiet case: a deliberate stop (which also
    # boots out the daemon job, so nothing is found here) must never page.
    # Preserve the silent OK exactly as before.
    # #5391: intent is gone, so any outage episode recorded against the previous
    # intent is over — clear it (and its escalation sentinel) so a future
    # start→crash gets a full attempt budget rather than inheriting a tripped
    # breaker from before the operator's deliberate stop.
    recovery_state_clear
    report OK "RULE: marker absent -> deliberate stop, not reviving (#6388): no autonomy-desired marker at $MARKER — no daemon expected; nothing to check."
    exit 0
fi

# ---------- parse the marker (key=value; ignore comments/blanks) ----------
# marker_get() itself is defined near the top (#5118) so the marker-ABSENT path
# above can use the same reader.
HEARTBEAT_FILE="$(marker_get heartbeat_file)"
HEARTBEAT_INTERVAL_SECS="$(marker_get heartbeat_interval_secs)"
MARKER_USE_LAUNCHD="$(marker_get use_launchd)"
MARKER_LABEL="$(marker_get launchd_label)"
MARKER_USE_SYSTEMD="$(marker_get use_systemd)"
MARKER_SYSTEMD_UNIT="$(marker_get systemd_unit)"
# #5118: LOOM_PID_FILE (the value loom-daemon-start.sh exports and the daemon
# honors as its own tier 1) now wins over the marker's recorded path, and an
# absent field falls through the daemon's remaining tiers instead of resolving
# to the empty string. Before this the two ends could — and on both fleet hosts
# did — mean different files.
PID_FILE="$(resolve_pid_file "$(marker_get pid_file)")"

# Fallbacks when the marker predates a field or the value is empty.
[[ -z "$HEARTBEAT_FILE" ]] && HEARTBEAT_FILE="$LOOM_DIR/daemon.heartbeat"
[[ "$HEARTBEAT_INTERVAL_SECS" =~ ^[0-9]+$ ]] || HEARTBEAT_INTERVAL_SECS=60

# Env overrides win over the marker (a stop/start under a different label should
# be probed with the current env, not a stale marker value).
USE_LAUNCHD="${MARKER_USE_LAUNCHD:-true}"
if [[ "${LOOM_DAEMON_LAUNCHD:-}" =~ ^(0|false|no)$ ]]; then
    USE_LAUNCHD=false
fi
[[ "$(uname -s)" == "Darwin" ]] || USE_LAUNCHD=false
LABEL="${LOOM_LAUNCHD_LABEL:-${MARKER_LABEL:-com.rjwalters.loom-daemon}}"

# #4862: use_systemd/systemd_unit are new marker fields, written by
# loom-daemon-start.sh's systemd branch (a marker from before this fix, or
# from the launchd/nohup branches, has no use_systemd=true line). A blank
# MARKER_USE_SYSTEMD stays OFF by default here -- same rationale as section 1
# above: `systemctl` merely being on PATH is not proof THIS daemon is
# systemd-managed, so no platform auto-detect. LOOM_WATCHDOG_SYSTEMD_PROBE=1 opts in
# explicitly for a pre-#4862 marker that has not been rewritten yet.
#
# Deliberately NO platform clobber here. An earlier revision ended this block
# with `[[ "$(uname -s)" == "Darwin" ]] && USE_SYSTEMD=false`, which ran after
# the opt-in and silently overrode both the marker and the documented
# LOOM_WATCHDOG_SYSTEMD_PROBE=1 escape hatch -- contradicting the comment above
# and making the whole #4862 systemd remediation path unreachable (and
# untestable) on a Darwin host.
#
# It was also redundant for the case it appeared to protect: the marker is the
# authority, and only loom-daemon-start.sh's systemd branch writes
# `use_systemd=true`. The launchd branch calls `write_intent_marker "true"
# "$LAUNCHD_LABEL"` with no third argument, so a real Darwin host's marker
# carries `use_systemd=false` and this resolves to false on its own.
USE_SYSTEMD="${MARKER_USE_SYSTEMD:-false}"
if [[ "${LOOM_WATCHDOG_SYSTEMD_PROBE:-}" =~ ^(1|true|yes)$ ]]; then
    USE_SYSTEMD=true
elif [[ "${LOOM_WATCHDOG_SYSTEMD_PROBE:-}" =~ ^(0|false|no)$ ]]; then
    USE_SYSTEMD=false
fi
SYSTEMD_UNIT="${LOOM_SYSTEMD_UNIT:-${MARKER_SYSTEMD_UNIT:-loom-daemon.service}}"

# ---------- 2. reality: is the expected daemon actually alive? ----------
# Shared probe (#4331): sets daemon_alive / liveness_detail / job_loaded /
# launchd_service from the resolved USE_LAUNCHD / LABEL / PID_FILE. job_loaded /
# launchd_service feed the #4232 bounded auto-remediation gate below:
# job_loaded=true means `launchctl print` succeeded (the job IS in launchd's
# table) even though no live pid was found — distinct from "not loaded at all"
# (a booted-out job, or a non-launchd host), which stays report-only no matter
# what.
detect_daemon_liveness

# ---------- 2b. authoritative in-band cross-check (#5118) ----------
# The probe above is entirely OUT-OF-BAND. When it does not find a live daemon,
# that is NOT yet a finding: on both fleet hosts the pid-file branch reported
# "no live pid file" every five minutes for two days while a healthy daemon was
# serving work on the socket the whole time. So before ANY report, ask the
# socket — the same signal `loom-daemon health` already treats as authoritative,
# with the pid file demoted to a corroborating hint.
#
# SOCKET_ONLY_LIVENESS records that liveness came from the socket alone (no live
# pid to age-check or key a hang streak to), which the sections below honor.
SOCKET_ONLY_LIVENESS=false
socket_verdict=""
socket_detail=""
if [[ "$daemon_alive" != "true" ]]; then
    probe_socket_liveness
    case "$socket_verdict" in
        answered)
            if [[ "$liveness_source" == "pidfile" ]]; then
                # No supervisor was consulted, so nothing contradicts the
                # socket: the daemon is HEALTHY and the pid file was simply not
                # a usable signal. This is the false-alarm case #5118 fixes —
                # it must be an OK, never a page.
                daemon_alive=true
                SOCKET_ONLY_LIVENESS=true
                liveness_detail="daemon ANSWERS on ${SOCKET_PATH} (${socket_detail}) — authoritative; the pid-file hint was unusable (${liveness_detail}), which is NOT evidence of an outage (#5118)"
                report OK "daemon healthy via the in-band socket round-trip: ${liveness_detail}."
            else
                # A supervisor DOES claim its job is down while something is
                # serving the socket. That is a real anomaly (an unsupervised
                # daemon: still dispatching, but with no crash protection) — but
                # it is NOT "autonomous dispatch has stopped", and auto-
                # remediation must NOT fire: kickstarting a second daemon at a
                # socket a live one already owns can only produce a refusal.
                report WARN \
                    "STATE MISMATCH: ${liveness_detail}, yet a daemon ANSWERS on ${SOCKET_PATH} (${socket_detail}). Dispatch is still running, so this is NOT an autonomy outage — but the daemon is UNSUPERVISED: nothing will relaunch it if it dies, and no auto-remediation is attempted here (relaunching into a served socket would only be refused by the singleton guard). Heal it by rolling the daemon through ./.loom/scripts/cli/loom-daemon-stop.sh && ./.loom/scripts/cli/loom-daemon-start.sh [flags] so the supervisor owns it again."
                exit 1
            fi
            ;;
        unreachable)
            # Positive evidence of absence — fall through to the existing
            # divergence + bounded auto-remediation path, now carrying BOTH
            # signals in its message.
            liveness_detail="${liveness_detail}; and the in-band probe confirms it: ${socket_detail}"
            ;;
        *)
            # Indeterminate. Report "the daemon is down" ONLY when some signal
            # actually says so: a supervisor that reports its job down, or a pid
            # file naming a pid that is not alive. With neither — the exact
            # shape that produced the permanent false positive — say UNKNOWN in
            # its own words and exit 3, distinct from a real outage (#5118).
            if [[ "$liveness_source" == "pidfile" && "$pidfile_evidence" == "absent" ]]; then
                report UNKNOWN \
                    "LIVENESS UNDETERMINED: a daemon is EXPECTED (autonomy-desired marker present, started $(marker_get started_at)) but this tick found NO evidence either way — ${liveness_detail}, and the in-band socket probe could not answer: ${socket_detail}. This is deliberately NOT reported as an outage (#5118): the pid file alone is too weak a signal to declare one. Restore the in-band probe (make a loom-daemon binary resolvable — LOOM_DAEMON_BIN / PATH / ~/.local/bin — and leave LOOM_WATCHDOG_IPC_PROBE enabled), then re-check with 'loom-daemon health'."
                exit 3
            fi
            liveness_detail="${liveness_detail}; the in-band probe could not corroborate: ${socket_detail}"
            ;;
    esac
fi

if [[ "$daemon_alive" != "true" ]]; then
    # ---------- bounded auto-remediation (#4232) ----------
    # THE PROBLEM: the restart primitive's contract (#4054/#4077) is "the
    # supervised daemon exits 0 -> KeepAlive:SuccessfulExit relaunches it". On
    # 2026-07-28 that contract's exit-0 half held but launchd's relaunch half
    # silently didn't, and this watchdog (a report-only detector) could only
    # describe the outage, not fix it — exactly the unattended-#4055-rollout
    # risk this narrow gate closes.
    #
    # THE GATE IS NARROW BY CONSTRUCTION: auto-`kickstart` fires ONLY for the
    # exact signature "job LOADED (launchctl still knows about it) + NOT
    # running + last exit status 0". An operator-initiated SIGTERM stop exits
    # 143/130 (loom-daemon-stop.sh); a genuine crash exits non-zero; a booted-
    # out/never-loaded job fails `launchctl print` outright (job_loaded=false).
    # NONE of those can produce "loaded, down, exit 0" — only a restart-
    # primitive exit that launchd failed to honor can. So every OTHER
    # divergence (stop, crash, bootout) falls through to the report-only path
    # below unchanged: no crash-loop revival, no reviving a deliberate stop.
    if [[ "$job_loaded" == "true" && -n "$launchd_service" ]]; then
        last_exit_status="$(launchctl print "$launchd_service" 2>/dev/null \
            | grep -oE 'last exit (code|status)[[:space:]]*=[[:space:]]*[-0-9]+' \
            | head -n1 | grep -oE '[-0-9]+$')"
        if [[ "$last_exit_status" == "0" ]]; then
            report DIVERGENCE \
                "A daemon is EXPECTED (autonomy-desired marker present, started $(marker_get started_at)) but is NOT running: ${liveness_detail}. Last exit status was 0 — the restart-primitive's own exit-0 contract (#4054/#4077) — which launchd failed to honor. Auto-remediating with 'launchctl kickstart ${launchd_service}' (PLAIN, never -k, so a daemon that is mid-relaunch is never killed) (#4232)."
            launchctl kickstart "$launchd_service" >/dev/null 2>&1
            # Brief, bounded re-check — this is a StartInterval job (re-run
            # every cadence regardless), so a failure here is NOT the last
            # chance; it just means this pass still reports divergence and the
            # next pass tries again.
            RECHECK_ATTEMPTS="${LOOM_WATCHDOG_KICKSTART_RECHECK_ATTEMPTS:-3}"
            RECHECK_INTERVAL="${LOOM_WATCHDOG_KICKSTART_RECHECK_INTERVAL:-1}"
            recheck_pid=""
            for _ in $(seq 1 "$RECHECK_ATTEMPTS"); do
                recheck_pid="$(launchctl print "$launchd_service" 2>/dev/null | awk -F'= ' '/^[[:space:]]*pid = /{gsub(/[^0-9]/, "", $2); print $2; exit}')"
                if [[ -n "$recheck_pid" ]] && kill -0 "$recheck_pid" 2>/dev/null; then
                    break
                fi
                recheck_pid=""
                sleep "$RECHECK_INTERVAL"
            done
            if [[ -n "$recheck_pid" ]]; then
                report OK "auto-remediation succeeded: 'launchctl kickstart' relaunched ${launchd_service} (new pid ${recheck_pid})."
                recovery_state_clear   # #5391: a live daemon ends the episode
                exit 0
            fi
            report DIVERGENCE \
                "Auto-remediation attempted ('launchctl kickstart ${launchd_service}') but the daemon is STILL not confirmed running. Escalate manually: launchctl print ${launchd_service}  (or ./.loom/scripts/cli/loom-daemon-start.sh [flags])."
            exit 1
        fi
    fi

    # ---------- bounded auto-remediation, systemd (#4862) ----------
    # The Linux mirror of the #4232 launchd gate above, closing the exact gap
    # #4862 reported: on a systemd host the watchdog previously only LOGGED a
    # clean-exit-turned-timeout divergence, never acted on it. Narrow by the
    # SAME construction as the launchd gate: fires ONLY for "unit LOADED
    # (systemd still knows about it) + NOT running + main process's own last
    # exit was a clean status 0" (ExecMainCode=exited, ExecMainStatus=0). An
    # operator stop (loom-daemon-stop.sh) disables the unit outright; a genuine
    # crash leaves a non-zero/signal ExecMainStatus; a never-installed unit
    # fails LoadState=loaded (job_loaded=false). None of those can produce
    # "loaded, down, ExecMainStatus=0" — only a restart-primitive exit that
    # Restart=on-success failed to honor can (e.g. a pre-#4862 unit still
    # missing KillMode=mixed). Every other divergence falls through unchanged.
    if [[ "$job_loaded" == "true" && -n "$systemd_service" ]] && command -v systemctl >/dev/null 2>&1; then
        exec_main_code="$(systemctl --user show -p ExecMainCode --value "$systemd_service" 2>/dev/null)"
        exec_main_status="$(systemctl --user show -p ExecMainStatus --value "$systemd_service" 2>/dev/null)"
        if [[ "$exec_main_code" == "exited" && "$exec_main_status" == "0" ]]; then
            report DIVERGENCE \
                "A daemon is EXPECTED (autonomy-desired marker present, started $(marker_get started_at)) but is NOT running: ${liveness_detail}. Main process's last exit was clean (status 0) — the restart-primitive's own exit-0 contract (#4054/#4077) — which Restart=on-success failed to honor (likely a unit-result reclassification, #4862). Auto-remediating with 'systemctl --user reset-failed ${systemd_service} && systemctl --user start ${systemd_service}'."
            systemctl --user reset-failed "$systemd_service" >/dev/null 2>&1 || true
            systemctl --user start "$systemd_service" >/dev/null 2>&1
            RECHECK_ATTEMPTS="${LOOM_WATCHDOG_KICKSTART_RECHECK_ATTEMPTS:-3}"
            RECHECK_INTERVAL="${LOOM_WATCHDOG_KICKSTART_RECHECK_INTERVAL:-1}"
            recheck_pid=""
            for _ in $(seq 1 "$RECHECK_ATTEMPTS"); do
                recheck_pid="$(systemctl --user show -p MainPID --value "$systemd_service" 2>/dev/null)"
                if [[ -n "$recheck_pid" && "$recheck_pid" != "0" ]] && kill -0 "$recheck_pid" 2>/dev/null; then
                    break
                fi
                recheck_pid=""
                sleep "$RECHECK_INTERVAL"
            done
            if [[ -n "$recheck_pid" ]]; then
                report OK "auto-remediation succeeded: 'systemctl --user start' relaunched ${systemd_service} (new pid ${recheck_pid})."
                recovery_state_clear   # #5391: a live daemon ends the episode
                exit 0
            fi
            report DIVERGENCE \
                "Auto-remediation attempted ('systemctl --user start ${systemd_service}') but the daemon is STILL not confirmed running. Escalate manually: systemctl --user status ${systemd_service}  (or ./.loom/scripts/cli/loom-daemon-start.sh [flags])."
            exit 1
        fi
    fi

    # ---------- 2d. general-case bounded recovery + circuit breaker (#5391) ----------
    # Reaching here means: a daemon is EXPECTED, it is CONFIRMED down (the
    # out-of-band signal says so and, where a probe was possible, the socket
    # agreed — the "I cannot tell" shapes already exited 3 above), and neither
    # narrow #4232/#4862 gate applied. Until #5391 this was a dead end: report
    # the recovery command, never run it. On one fleet host that produced 252
    # identical [DIVERGENCE] lines in eight days, one of them spanning a
    # continuous 1h40m outage. Now it recovers — bounded, backed off, and behind
    # a circuit breaker (see the header for the full policy and its rationale).
    now_epoch="$(date -u +%s)"
    ep_down_since="$(recovery_state_get down_since)"
    [[ "$ep_down_since" =~ ^[0-9]+$ ]] || ep_down_since="$now_epoch"
    ep_ticks="$(recovery_state_get ticks)";             [[ "$ep_ticks" =~ ^[0-9]+$ ]] || ep_ticks=0
    ep_attempts="$(recovery_state_get attempts)";       [[ "$ep_attempts" =~ ^[0-9]+$ ]] || ep_attempts=0
    ep_last_attempt="$(recovery_state_get last_attempt)"; [[ "$ep_last_attempt" =~ ^[0-9]+$ ]] || ep_last_attempt=0
    ep_ticks=$(( ep_ticks + 1 ))
    outage_secs=$(( now_epoch - ep_down_since ))
    (( outage_secs < 0 )) && outage_secs=0

    # recover_skip_reason non-empty ⇒ no attempt on THIS tick.
    # recover_possible=false      ⇒ no attempt can EVER be made this episode, so
    #                               the outage escalates on tick count alone
    #                               rather than waiting for a budget that will
    #                               never be spent.
    # #6388: reaching this block already means the marker IS present (the
    # marker-ABSENT branch near the top of this script exits long before here)
    # — so the ONLY rule that can ever make "never revive a deliberate stop"
    # apply is marker ABSENCE, which by construction cannot be true at this
    # point. detect_supervisor_exit_signal() below is therefore purely
    # informational: a signal-shaped last-exit code is named in the report
    # text (the "stray signal, recovering" rule) but never blocks recovery.
    recover_skip_reason=""
    recover_possible=true
    RECOVER_ARGV_DETAIL=""
    detect_supervisor_exit_signal
    if [[ "$RECOVER_ENABLED" != "true" ]]; then
        recover_possible=false
        recover_skip_reason="NO auto-recovery was attempted: it is DISABLED on this host (LOOM_WATCHDOG_AUTO_RECOVER=0). This watchdog is REPORT-ONLY for this outage — an installed watchdog job here means DETECTION, not self-healing, and nothing will bring the daemon back but you."
    elif ! resolve_recovery_argv; then
        recover_possible=false
        recover_skip_reason="NO auto-recovery was attempted: ${RECOVER_ARGV_DETAIL}. This watchdog is therefore REPORT-ONLY on this host — an installed watchdog job here means DETECTION, not self-healing — until that is fixed."
    elif (( ep_attempts >= RECOVER_MAX_ATTEMPTS )); then
        recover_skip_reason="CIRCUIT BREAKER OPEN: ${ep_attempts} bounded recovery attempts (budget ${RECOVER_MAX_ATTEMPTS}) have already been spent on this outage and none restored the daemon. NO further automatic attempts will be made until a tick observes a healthy daemon or ${RECOVERY_STATE_FILE} is deleted — deliberately, so a genuinely broken binary is restarted a bounded number of times instead of forever."
    else
        recover_next_backoff="$(recovery_backoff_for $(( ep_attempts + 1 )))"
        recover_since_last=$(( now_epoch - ep_last_attempt ))
        if (( ep_attempts > 0 && recover_since_last < recover_next_backoff )); then
            # A deferral, NOT a permanent skip: recover_possible stays true, so
            # this tick does not count toward the un-recoverable escalation.
            recover_skip_reason="recovery attempt $(( ep_attempts + 1 )) of ${RECOVER_MAX_ATTEMPTS} is BACKED OFF for another $(( recover_next_backoff - recover_since_last ))s (exponential backoff: ${recover_next_backoff}s after ${ep_attempts} failed attempt(s)) — reporting only on this tick."
        fi
    fi

    if [[ -z "$recover_skip_reason" ]]; then
        ep_attempts=$(( ep_attempts + 1 ))
        ep_last_attempt="$now_epoch"
        # Record the attempt BEFORE running it. The command may take up to
        # RECOVER_TIMEOUT_SECS, and an overlapping tick (a long recovery vs. a
        # short StartInterval) must see the spent attempt and the started
        # backoff clock rather than firing a second concurrent start.
        recovery_state_write "$ep_down_since" "$ep_ticks" "$ep_attempts" "$ep_last_attempt"
        signal_rule_note=""
        if [[ -n "$supervisor_exit_signal_detail" ]]; then
            # RULE: marker present + signal-shaped exit -> stray signal,
            # recovering (#6388) — named explicitly so the report never again
            # reads like the contradictory pre-#6388 line (marker present,
            # yet refusing to recover "because" the exit code).
            signal_rule_note=" RULE: marker present, ${supervisor_exit_signal_detail} -> stray signal, recovering (NOT a deliberate operator stop — only marker ABSENCE means that, #6388)."
        fi
        report DIVERGENCE \
            "A daemon is EXPECTED (autonomy-desired marker present, started $(marker_get started_at)) but is NOT running: ${liveness_detail}. Autonomous dispatch has stopped (down ${outage_secs}s across ${ep_ticks} consecutive watchdog ticks).${signal_rule_note} AUTO-RECOVERING now — bounded attempt ${ep_attempts} of ${RECOVER_MAX_ATTEMPTS} (#5391), running: ${RECOVER_ARGV_DETAIL}"
        recover_saved_liveness_detail="$liveness_detail"
        if command -v bounded_run >/dev/null 2>&1; then
            bounded_run "$RECOVER_TIMEOUT_SECS" "${RECOVER_ARGV[@]}" >/dev/null 2>&1
            recover_rc=$?
        else
            # No shared lib/bounded-run.sh: still recover, just unbounded. A
            # missing optional helper must never turn recovery off (the same
            # graceful-degradation rule the IPC probe follows).
            "${RECOVER_ARGV[@]}" >/dev/null 2>&1
            recover_rc=$?
        fi
        if recovery_recheck_alive; then
            report OK \
                "auto-recovery SUCCEEDED on attempt ${ep_attempts} of ${RECOVER_MAX_ATTEMPTS} after a ${outage_secs}s outage: ${liveness_detail}. Recovery command exited ${recover_rc} (#5391)."
            recovery_state_clear
            exit 0
        fi
        liveness_detail="$recover_saved_liveness_detail"
        recover_skip_reason="Bounded recovery attempt ${ep_attempts} of ${RECOVER_MAX_ATTEMPTS} RAN ('${RECOVER_ARGV_DETAIL}', exit ${recover_rc}) and the daemon is STILL not confirmed running."
        if (( ep_attempts < RECOVER_MAX_ATTEMPTS )); then
            recover_skip_reason="${recover_skip_reason} The next attempt is backed off by $(recovery_backoff_for $(( ep_attempts + 1 )))s."
        else
            recover_skip_reason="${recover_skip_reason} The CIRCUIT BREAKER is now OPEN: the attempt budget is spent, so no further automatic attempts will be made until a tick observes a healthy daemon or ${RECOVERY_STATE_FILE} is deleted."
        fi
    fi

    # ---------- out-of-band escalation (#5391) ----------
    # Escalate exactly once per episode, either when the breaker has tripped or
    # when no attempt is even possible on this host and the outage has persisted
    # for the same number of consecutive ticks. Everything below is best-effort:
    # a failed escalation degrades to the log line, never to a failed tick.
    escalate_reason=""
    if (( ep_attempts >= RECOVER_MAX_ATTEMPTS )); then
        escalate_reason="the circuit breaker is OPEN — ${ep_attempts} bounded recovery attempts were spent and the daemon is still down"
    elif [[ "$recover_possible" != "true" ]] && (( ep_ticks >= RECOVER_MAX_ATTEMPTS )); then
        escalate_reason="automatic recovery is not possible on this host, and the outage has persisted for ${ep_ticks} consecutive watchdog ticks (${outage_secs}s)"
    fi
    escalation_note=""
    if [[ -n "$escalate_reason" ]]; then
        if [[ -f "$ESCALATION_SENTINEL" ]]; then
            escalation_note=" This outage has ALREADY been escalated out-of-band (sentinel ${ESCALATION_SENTINEL})."
        elif escalate_daemon_outage "$escalate_reason"; then
            escalation_note=" ESCALATED out-of-band: filed a forge tracking issue so this outage is not confined to a logfile nobody tails (#5391)."
        else
            escalation_note=" Out-of-band escalation was NOT possible (disabled, no create-issue.sh reachable, or the forge call failed), so THIS LOGFILE IS THE ONLY SIGNAL for this outage — ${WATCHDOG_LOG}."
        fi
    fi

    recovery_state_write "$ep_down_since" "$ep_ticks" "$ep_attempts" "$ep_last_attempt"
    report DIVERGENCE \
        "A daemon is EXPECTED (autonomy-desired marker present, started $(marker_get started_at)) but is NOT running: ${liveness_detail}. Autonomous dispatch has stopped (down ${outage_secs}s across ${ep_ticks} consecutive watchdog ticks). ${recover_skip_reason}${escalation_note} Recover with: ./.loom/scripts/cli/loom-daemon-start.sh [flags]  (or 'loom-daemon status' to inspect)."
    exit 1
fi

# #5391: reaching here means a daemon is CONFIRMED alive. Observing health — not
# elapsed time, not a tick count — is what ends an outage episode, so clear the
# attempt tally, the backoff clock and the escalation sentinel here. A daemon
# that flaps back up therefore gets a full fresh attempt budget for its next
# outage, while one that stays down never does.
recovery_state_clear

# ---------- 3. reality: does the daemon still ANSWER over its socket? ----------
# The two checks above are both out-of-band; this is the only in-band one (#4398).
# See "HANG-AWARE IPC LIVENESS PROBE" in the header for the full rationale — in
# short: the heartbeat writer and the IPC accept loop are independent tokio
# tasks, so "pid alive + heartbeat fresh" can hold while every socket round-trip
# hangs and dispatch is effectively dead.
#
# The process age is computed ONCE here and reused by the #4368 prior-boot
# heartbeat check below, so both consumers see the same number.
proc_age="$(process_age_secs "$live_pid" 2>/dev/null)" || proc_age=""

if [[ "$SOCKET_ONLY_LIVENESS" == "true" ]]; then
    # #5118: liveness was ESTABLISHED by a socket round-trip a moment ago, so
    # re-running the same probe would only spend a second budget to learn the
    # same thing. Adopt that result directly (and clear any stale streak: a
    # successful round-trip is exactly what ends one).
    probe_verdict=healthy
    probe_detail="$socket_detail"
else
    run_ipc_probe "$live_pid" "$proc_age"
fi

case "$probe_verdict" in
    healthy)
        # A successful round-trip ends any prior failure streak outright: the
        # confirmed-hang tally must only ever count CONSECUTIVE failures.
        probe_fail_count_clear

        # #5944: independently, record this SUCCESS into the windowed/rate
        # history too — unlike the consecutive tally above, a single success
        # must NOT erase it (see "A WINDOWED/RATE FAILURE SIGNAL" in the
        # header, and probe_window_record()'s own comment).
        probe_window_record "$live_pid" 0
        if (( PROBE_WINDOW_FAIL_COUNT >= PROBE_WINDOW_FAIL_THRESHOLD )); then
            load_avg="$(get_load_average)"
            report DEGRADED \
                "daemon IPC round-trip OK this tick (${probe_detail}), but ${PROBE_WINDOW_FAIL_COUNT} of the last ${PROBE_WINDOW_LEN} watchdog ticks failed the same probe (window threshold ${PROBE_WINDOW_FAIL_THRESHOLD}/${PROBE_WINDOW_TICKS}, #5944) — none of them were 3 CONSECUTIVE, so neither the same-tick (#5790) nor sustained-CONFIRMED (#4398) signal fired for this pattern, but failures this frequent are not a clean bill of health either. Host load average at probe time: ${load_avg}. NOT a confirmed hang (this tick's own round-trip answered) and no remediation is attempted; the window ages out on its own as old ticks roll off, and a run of clean ticks lets it clear naturally."
            PROBE_DIVERGED=true
            PROBE_DIVERGED_NOTE="the IPC probe has failed ${PROBE_WINDOW_FAIL_COUNT} of the last ${PROBE_WINDOW_LEN} watchdog ticks (see the DEGRADED line above, #5944) — dispatch may be intermittently degraded despite THIS tick's own round-trip succeeding; the exit code for this tick reflects that windowed/rate signal, not this line."
        else
            [[ "$VERBOSE" == "true" ]] && report OK "IPC probe OK: ${probe_detail}."
        fi
        ;;
    unresponsive)
        probe_fail_streak=$(( $(probe_fail_count_for_pid "$live_pid") + 1 ))
        probe_fail_count_write "$live_pid" "$probe_fail_streak"
        # #5944: also feed this FAILURE into the windowed/rate history — its
        # own DEGRADED verdict only ever fires from the `healthy` branch above
        # (a tick that already reports DIVERGENCE below needs no additional
        # signal), but the failure must still be recorded so a LATER
        # succeeding tick can see it in its window.
        probe_window_record "$live_pid" 1
        # #5790: sampled once per divergence, not per tick overall — healthy
        # and skipped ticks never pay for it. Attached to BOTH the CONFIRMED
        # and sub-threshold DIVERGENCE reports below so an operator can see at
        # a glance whether the failure correlates with host contention (#4279)
        # or occurred on an otherwise-idle host.
        load_avg="$(get_load_average)"
        if (( probe_fail_streak >= PROBE_FAIL_THRESHOLD )); then
            # CONFIRMED, SUSTAINED hang. Deliberately report-only: there is no
            # provably-safe unattended remediation for a wedged-but-alive
            # process (the only real fix is killing it, which would equally kill
            # a daemon merely under heavy legitimate load), so unlike #4232's
            # narrow auto-kickstart gate this escalates to a maximally
            # actionable report and stops there.
            report DIVERGENCE \
                "daemon IPC UNRESPONSIVE (CONFIRMED): the process is alive (${liveness_detail}) — and its heartbeat may well look FRESH — but the bounded socket round-trip has now failed on ${probe_fail_streak} CONSECUTIVE watchdog ticks (threshold ${PROBE_FAIL_THRESHOLD}). ${probe_detail}. Host load average at probe time: ${load_avg}. The heartbeat writer and the IPC accept loop are independent tokio tasks, so a fresh heartbeat does NOT prove the daemon can still serve work: autonomous dispatch is effectively DEAD while this holds. No automatic kill/restart is attempted (#4398 — there is no provably-safe unattended remediation for a wedged-but-alive process). RECOVER: 'loom-daemon restart' (note: the restart primitive travels over this same wedged socket and may itself hang), else ./.loom/scripts/cli/loom-daemon-stop.sh && ./.loom/scripts/cli/loom-daemon-start.sh [flags]. Diagnose with: LOOM_SOCKET_PATH=${SOCKET_PATH} ${PROBE_TIMEOUT_SECS}s-bounded 'loom-daemon status'; sample the process with 'sample ${live_pid}' (macOS) or 'gdb -p ${live_pid}' to capture the wedge before killing it."
            exit 1
        fi
        # Below threshold: loud + logged, but explicitly NOT a confirmed hang —
        # one failed round-trip can be transient contention (#4279: a
        # per-connection task dropping a request under concurrent-sweep load
        # that the very next one answers).
        report DIVERGENCE \
            "daemon IPC probe FAILED while the process is alive (${liveness_detail}): ${probe_detail}. This is consecutive failure ${probe_fail_streak} of ${PROBE_FAIL_THRESHOLD} — NOT yet a confirmed hang (a single failure can be transient contention under concurrent-sweep load, #4279) and no remediation is attempted. Host load average at probe time: ${load_avg}. If the next watchdog tick round-trips cleanly the streak resets."
        PROBE_DIVERGED=true
        ;;
    *)
        # skipped: startup grace, no resolvable binary, unsupported subcommand,
        # probe disabled, or a daemon-side application error (which PROVES IPC
        # works). None of these may increment the tally or invent a divergence —
        # and none may clear an existing streak either.
        [[ "$VERBOSE" == "true" ]] && report OK "IPC probe skipped: ${probe_detail}."
        ;;
esac

# ---------- peer-coordination out-of-band alert (#6222, Layer 3 of #6157) ----------
# Only attempted on a tick whose own IPC evidence already says the daemon is
# answering (`probe_verdict == healthy` — including the SOCKET_ONLY_LIVENESS
# path above, which sets it directly): a tick that just failed or skipped its
# OWN round-trip is not a good candidate to spend a second one on, and this
# check must never become a new hang surface for the ticks that already are.
if [[ "$probe_verdict" == "healthy" ]]; then
    check_peer_coordination_health
    case "$PEER_COORD_VERDICT" in
        Degraded)
            if [[ -f "$PEER_COORD_SENTINEL" ]]; then
                report OK "peer-coordination degradation already escalated out-of-band (sentinel ${PEER_COORD_SENTINEL})."
            elif escalate_peer_coordination_degraded; then
                report DIVERGENCE "peer-claim coordination is DEGRADED: ${PEER_COORD_SUMMARY}. ESCALATED out-of-band: filed a forge tracking issue so this degradation is not confined to a logfile nobody tails (#6222)."
            else
                report DIVERGENCE "peer-claim coordination is DEGRADED: ${PEER_COORD_SUMMARY}. Out-of-band escalation was NOT possible (disabled, no create-issue.sh reachable, or the forge call failed) — THIS LOGFILE IS THE ONLY SIGNAL for this degradation, ${WATCHDOG_LOG}."
            fi
            ;;
        Green)
            if [[ -f "$PEER_COORD_SENTINEL" ]]; then
                if clear_peer_coordination_escalation; then
                    report OK "peer-claim coordination has RECOVERED (${PEER_COORD_SUMMARY}) — closed the tracking issue and cleared the escalation sentinel (#6222)."
                else
                    report WARN "peer-claim coordination has RECOVERED (${PEER_COORD_SUMMARY}) but closing/commenting the tracking issue failed — the sentinel is left in place so a later healthy tick retries (#6222)."
                fi
            fi
            ;;
        *) : ;; # could not determine this tick — not evidence either way
    esac
fi

# Final exit for the paths below that find nothing wrong themselves. A probe
# divergence already REPORTED above still owns the exit code — otherwise a
# wedged-but-heartbeating daemon would exit 0 and the report would be the only
# trace, which is exactly the signal-vs-exit-code split #4398 closes.
exit_ok() {
    [[ "$PROBE_DIVERGED" == "true" ]] && exit 1
    exit 0
}

# ---------- 4. reality: is the heartbeat fresh? ----------
# The daemon writes HEARTBEAT_FILE on a declared cadence (#4011). A live daemon
# whose heartbeat has gone stale is likely wedged — still a process, but not
# doing its periodic work. The threshold is a comfortable multiple of the
# cadence so a single missed write never false-positives.
STALE_SECS="${LOOM_DAEMON_HEARTBEAT_STALE_SECS:-}"
if [[ ! "$STALE_SECS" =~ ^[0-9]+$ ]]; then
    STALE_SECS=$(( HEARTBEAT_INTERVAL_SECS * 5 ))
    (( STALE_SECS < 300 )) && STALE_SECS=300
fi

file_mtime() {
    # Portable mtime (epoch secs): GNU `stat -c` vs BSD/macOS `stat -f`.
    stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null
}

if [[ -f "$HEARTBEAT_FILE" ]]; then
    mtime="$(file_mtime "$HEARTBEAT_FILE")"
    if [[ "$mtime" =~ ^[0-9]+$ ]]; then
        now="$(date -u +%s)"
        age=$(( now - mtime ))
        # Prior-boot detection (#4368): if this heartbeat file predates the
        # live process's own start time, it is not evidence about the current
        # process at all — necessarily left over from a previous boot (or a
        # previous enablement of the opt-in heartbeat loop). Checked BEFORE
        # the staleness threshold below, mirroring the Rust probe's
        # `check_heartbeat` exactly (`daemon_install_state.rs`) so `status`
        # and this watchdog can never contradict each other. Only claim this
        # when the process age is actually known; an unparseable `ps` age
        # degrades to the ordinary Stale/Fresh checks below rather than a
        # false claim either way. `proc_age` is computed once in section 3.
        if [[ -n "$proc_age" ]] && (( age > proc_age )); then
            report_heartbeat_ok "daemon alive (${liveness_detail}); heartbeat ${HEARTBEAT_FILE} is from a PREVIOUS boot (${age}s old; this process is only ${proc_age}s old) — not evidence about the current process. Liveness-only OK; re-check after the process is well past startup if you still suspect a wedge."
            exit_ok
        fi
        if (( age > STALE_SECS )); then
            report DIVERGENCE \
                "Daemon process is alive (${liveness_detail}) but its heartbeat ${HEARTBEAT_FILE} is STALE (${age}s old > ${STALE_SECS}s threshold) — the daemon may be wedged. Inspect with 'loom-daemon status'; consider ./.loom/scripts/cli/loom-daemon-stop.sh && ...start.sh."
            exit 1
        fi
        report_heartbeat_ok "daemon healthy (${liveness_detail}); heartbeat fresh (${age}s ≤ ${STALE_SECS}s)."
        exit_ok
    fi
    # Unreadable mtime — degrade to liveness-only rather than false-report.
    report_heartbeat_ok "daemon alive (${liveness_detail}); heartbeat mtime unreadable — liveness-only OK."
    exit_ok
fi

# No heartbeat file but the daemon is alive: either the heartbeat loop is
# disabled (LOOM_DAEMON_HEARTBEAT=0) or the daemon just started and has not
# written yet. Degrade to liveness-only — do NOT false-report, since the daemon
# clearly IS running.
report_heartbeat_ok "daemon alive (${liveness_detail}); no heartbeat file at ${HEARTBEAT_FILE} (heartbeat disabled or not yet written) — liveness-only OK."
exit_ok
