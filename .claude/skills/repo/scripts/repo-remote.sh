#!/usr/bin/env bash
# repo-remote.sh — headless, scriptable entry point for /repo:remote provisioning.
#
# This is the non-interactive implementation of the provisioning contract
# documented (as prose) in commands/repo/remote.md. The interactive skill wraps
# this script with its wizard / cost-confirmation UX and, once the human has
# confirmed, calls `repo-remote up --yes`; a caller such as loom's
# `fleet add-worker` invokes the same `up --yes --json` path directly. There is
# ONE implementation of the contract so the two paths cannot drift (repo#52).
#
# The seam this produces, consumed by loom fleet orchestration: "a reachable
# Ubuntu box, this repo's SSH alias written, instance id recorded" — emitted as
# machine-readable JSON (instance id, public IP, SSH alias, estimated hourly
# cost) so a caller can implement loom's "plan shown before money is spent" rule.
#
# ─────────────────────────────────────────────────────────────────────────────
# STABLE INTERFACE (downstream tooling gates on this via the package version)
# ─────────────────────────────────────────────────────────────────────────────
#
# Subcommands (both the `up`/`down`/`status` verbs and the remote.md-style
# `--status`/`--down` flags are accepted, for parity with the prose command):
#
#   repo-remote up [--yes] [--force] [--json] [aws|gcp]   Provision (or reuse)
#       an instance.
#       Without --yes: a DRY-RUN plan (resolved spec + estimated cost) is emitted
#       and NOTHING is created — this is the "plan shown before money spent" path.
#       With --yes: the plan is executed. --yes removes the *prompt*, never the
#       *consent requirement*: a cost-relevant field missing from config is a
#       loud, non-zero-exit failure, never a silent default (repo#52 cost gate).
#       --force overrides the fleet-marker guard described below. It does NOT
#       relax the cost gate.
#
#   repo-remote status|--status [--json] [aws|gcp]   List instances this command
#       created (tagged repo-remote=<name>) with state; no mutation.
#
#   repo-remote verify|--verify [--json] [aws|gcp]   Prove the SSH alias still
#       reaches THIS repo's instance before anything is trusted to it. Opens one
#       SSH session over repo-remote-<name>, asks the host for its OWN instance
#       id, and compares it with the id this repo expects (a pinned
#       REPO_REMOTE_INSTANCE_ID, else the repo-remote=<name> tag). Exits 6 on a
#       mismatch AND on any answer it cannot establish (unreachable host, no
#       identity source) — it fails CLOSED, because "I could not tell" is not
#       evidence that the host is the right one. See "Host-identity
#       verification" below. No cloud mutation; no cloud call at all when the
#       instance id is pinned.
#
#   repo-remote down|--down [--yes] [--force] [--json] [aws|gcp]   Teardown.
#       Without --yes: a DRY-RUN listing of exactly what would stop/terminate
#       (fleet-marked instances, if any, are annotated but never block a dry
#       run — see the fleet-marker guard below).
#       With --yes: stop them; add --delete to terminate (disk goes with it).
#       --force overrides the fleet-marker guard described below, same as `up`.
#       With REPO_REMOTE_MARKET=spot, `down --yes` without --delete is refused
#       (exit 2) before any cloud call — see "AWS market" below.
#
#   repo-remote prune|--prune [--yes] [--json] [aws|gcp]   Garbage-collect stale
#       SSH aliases (#628). Lists the `Host repo-remote-*` blocks in
#       REPO_REMOTE_SSH_CONFIG (default ~/.ssh/config) whose box no longer
#       exists at the configured provider/region -- the block's HostName matches
#       no live instance's public IP or id, and its alias is not
#       repo-remote-<tag> for a live instance's repo-remote tag -- and, with
#       --yes, removes them plus their known_hosts entries (`ssh-keygen -R
#       <HostName>`). Dry run by default. Fails CLOSED: if the provider lookup
#       errors or returns anything unparseable (exit 4), nothing is removed. A
#       block it cannot judge (multi-pattern Host line, no HostName) and every
#       block that is not `Host repo-remote-*` are never touched. The removal
#       uses the same lock, same-directory temp file, `ssh -G` parse check and
#       atomic mv as the alias writer. It judges only the configured
#       provider/region, so run the dry run first when you use several.
#       `down --yes --delete` removes the terminated box's alias and
#       known_hosts entry itself; a stop-only `down --yes` keeps both.
#
# Naming (#628): <name> is the directory name of the PRIMARY checkout (the
# parent of `git rev-parse --git-common-dir`), so a main checkout and every
# linked worktree of one repo resolve the same alias, repo-remote=<name> tag
# and SSH ingress marker. Outside a `.git`-dir checkout (bare repo, no git) it
# falls back to the basename of the git toplevel / cwd. A box tagged with a
# worktree directory name by an earlier version is no longer found by tag;
# pin it with REPO_REMOTE_INSTANCE_ID or re-tag it repo-remote=<name>.
#
#   repo-remote attach|--attach [--container|--host] [--command <cmd>] [aws|gcp]
#       Open a session on THIS repo's existing instance, carrying a freshly
#       resolved GitHub credential (repo#565). It is the credential-aware
#       connect AND reconnect entry point: every attach resolves the
#       credential again, so re-running it after a short-lived token expires
#       gives the new session a new token while reusing the same instance and
#       container (nothing is provisioned, started, or stopped; no cloud
#       mutation at all). Order, each step gating the next:
#         1. validate config (transport, gateway, repo name) — no network;
#         2. open ONE SSH master connection over repo-remote-<name> and verify
#            the host identity over it exactly like `verify` (exit 6 on a
#            mismatch or an unverifiable host — fails closed);
#         3. only then resolve the credential (run GitHub's device flow for
#            a per-user token, or REPO_REMOTE_GH_TOKEN_CMD, or — break-glass
#            only — read the static REPO_REMOTE_GH_TOKEN) — exit 7 if it
#            fails, with NO fallback to any other source;
#         4. open the session over that same verified master connection, with
#            the token carried as an SSH environment value (never argv), and
#            land in the dev container (`docker exec`) when it is running,
#            else in ~/<repo> on the host. --container / --host force one;
#            --command runs <cmd> non-interactively instead of a login shell.
#       Processes already running on the VM keep the environment they started
#       with; only the new attachment sees the replacement token.
#       See "GitHub credentials on the VM" below.
#
# Config: two layers, shared first then repo (repo overrides), matching the
# skill exactly:
#   1. ${XDG_CONFIG_HOME:-$HOME/.config}/repo/remote.env   (shared cloud creds)
#   2. <git-root>/.env                                     (per-repo machine)
#
# Out-of-tree per-repo config (repo#492): some repos forbid ANY `.env` in the
# checkout or its worktrees (worktrees are copied/rsynced constantly, and a
# gitignored file is one `git add -f` away from leaking). For them, set
# REPO_REMOTE_ENV_FILE=<absolute path> -- in the process environment or in the
# shared remote.env -- and layer 2 becomes that file instead of
# <git-root>/.env (a leading `~/` is expanded; the environment value wins over
# the shared file's). The instance-id write-back goes there too.
#
# Instance-id write-back never CREATES <git-root>/.env: it updates the resolved
# per-repo file when REPO_REMOTE_ENV_FILE is set (creating that file if
# needed) or when <git-root>/.env already exists; otherwise it only logs the id
# with a hint to pin it. REPO_REMOTE_NO_WRITEBACK=1 disables the write-back
# entirely (the id is still logged).
#
# Linked-worktree warning (repo#538): when the checkout is a LINKED git
# worktree (`git worktree add`, e.g. .loom/worktrees/issue-N -- detected by
# `git rev-parse --git-dir` differing from `--git-common-dir`) and no
# REPO_REMOTE_ENV_FILE is set, reading a pre-existing <git-root>/.env or writing
# the instance id back into it prints a loud WARNING naming the file. The
# read/write still happens (warn-then-proceed); the fix is to move the per-repo
# settings out of the tree and set REPO_REMOTE_ENV_FILE. The primary checkout
# is unaffected (no warning).
#
# Box name override (#664): the box identity (instance tag repo-remote=<name>,
# SSH alias repo-remote-<name>, security-group names, SSH-ingress marker) is
# normally the primary checkout's directory name. Set REPO_REMOTE_NAME=<name>
# (environment, shared remote.env, or the per-repo env file) to target a
# different box from the same checkout, e.g. a separate big-RAM "fit" box next
# to the build/test box, each with its own remote-run lock. Allowed characters:
# [A-Za-z0-9._-]; anything else exits 2 before any cloud call. Empty/unset
# leaves the derived name unchanged.
#
# Cost gate (repo#52 — the highest-cost-of-being-wrong element): `up` (with or
# without --yes) REQUIRES the provider, that provider's credentials, and
# REPO_REMOTE_INSTANCE_TYPE to be present in config. Instance type is the
# cost-relevant field and is NEVER defaulted here — that removes interactivity,
# not consent. Non-cost-relevant fields (disk, idle window, image) do fall back
# to built-in defaults, matching the prose command.
#
# Fleet-marker guard (repo#164, repo#170): both `up` and `down` resolve an
# instance from the SAME two never-expiring handles — a pinned
# REPO_REMOTE_INSTANCE_ID, or one carrying the repo-remote=<name> tag/label
# (`down`'s tag-discovery path can resolve more than one). That resolution is
# stale-tag-prone: a host provisioned once for an ephemeral dev session can
# later become a persistent fleet worker while still carrying the repo-remote
# tag, at which point this ephemeral tooling would happily reuse it
# (operator incident, private tracker). `down` is the strictly worse case: it STOPS the resolved
# instance, or — with --delete — TERMINATES it, disk and all, unrecoverable.
# So before `up` starts/aliases a REUSED instance, or `down` stops/terminates
# any resolved instance, its tags (AWS) / labels (GCP) are checked for a fleet
# marker — by default Fleet=loom, configurable via REPO_REMOTE_FLEET_TAG_KEY /
# REPO_REMOTE_FLEET_TAG_VALUE. If present, the run STOPS (exit 5) with a clear
# message unless --force is given; with --force it proceeds after a loud
# warning. `down` refuses the WHOLE resolved batch if ANY id in it carries the
# marker, rather than silently acting on a subset. Setting
# REPO_REMOTE_FLEET_TAG_KEY= (empty) disables the check entirely. The guard
# never applies to a freshly created instance (nothing to inherit) and never to
# a dry run (which touches no cloud resource at all) — a `down` dry run
# annotates any fleet-marked instances in its listing instead of blocking.
#
# Host-identity verification (repo#458): an EC2 auto-assigned public IPv4 is
# RELEASED when the instance stops and a different one is assigned on its next
# start — and the released address is handed to whoever starts an instance next,
# very possibly another AWS customer entirely. The SSH alias this script writes
# is therefore only as fresh as the last `up` (or `verify`) run: nothing keeps it
# current between sessions. In the incident behind this check, a stopped/started
# box's old address came back up on a stranger's instance, the unchanged alias
# resolved, ssh connected, key auth succeeded, and three agents wrote work
# products to a machine that was not theirs. Every signal the tooling had said
# "fine".
#
# So `up` (after it writes the alias) and the standalone `verify` subcommand both
# ask the reached host for its OWN instance id and compare it against the id this
# repo expects. The id is read, in order, from: the marker file this tool drops at
# provision time (REPO_REMOTE_HOST_ID_FILE, default /etc/repo-remote-instance-id),
# the instance metadata service (IMDSv2, then IMDSv1, then GCP's), and finally
# cloud-init's /var/lib/cloud/data/instance-id — so an instance provisioned before
# the marker existed is still verifiable. A MISMATCH always exits 6, in both
# paths; --force does NOT override it (that flag is the fleet-marker override and
# nothing else). The two paths differ only on an answer that could not be
# established at all:
#   verify  — exits 6. Fails CLOSED: it exists precisely to be run before an
#             agent trusts a session it did not just create.
#   up      — warns loudly and continues. `up` resolved the IP from the cloud API
#             and rewrote the alias in the SAME run, so the alias is fresh by
#             construction there; failing the run on an IMDS-locked-down or
#             pre-marker box would break provisioning for no safety gain.
# Pinning REPO_REMOTE_INSTANCE_ID is the recommended configuration for any
# session expected to survive a stop/start: it makes the expectation explicit
# rather than re-derived from a tag that a fleet host can also be wearing.
#
# AWS root volume (repo#559): the root EBS volume is launched EXPLICITLY typed,
# never left to the AMI default (gp2, whose 3 IOPS/GiB baseline — 150 IOPS at
# 50 GiB — leaves sustained builds/tests stalled in disk wait once its burst
# credits drain). Settings, all AWS-only, resolved through the same two config
# layers as everything else:
#   REPO_REMOTE_DISK_GB            root volume size in GiB (default 50)
#   REPO_REMOTE_VOLUME_TYPE        gp3 (default) | gp2
#   REPO_REMOTE_VOLUME_IOPS        gp3 only; default 3000 (the gp3 baseline)
#   REPO_REMOTE_VOLUME_THROUGHPUT  gp3 only, MiB/s; default 125 (the baseline)
# They are validated BEFORE any cloud call (exit 2): an unsupported type, a
# non-positive-integer value, IOPS/throughput set alongside gp2, or a gp3 value
# outside AWS's documented limits (IOPS 3000-80000 and <= 500 per GiB;
# throughput 125-2000 MiB/s and <= 0.25 MiB/s per provisioned IOPS) is refused.
# On REUSE, `up` inspects the instance's actual root volume and, if it is gp2,
# prints a stderr advisory with the copyable `aws ec2 modify-volume` command and
# the `ec2:ModifyVolume` permission it needs. It never runs that command, and a
# failed inspection is a notice, never a failed `up`.
#
# AWS transport (repo#564): how `up`/`verify` reach the box, AWS-only, resolved
# through the same two config layers:
#   REPO_REMOTE_TRANSPORT          ssh (default) | ssm
#   REPO_REMOTE_INSTANCE_PROFILE   optional IAM instance profile NAME, passed to
#                                  run-instances as --iam-instance-profile
#                                  Name=<name> on a fresh launch. A profile alone
#                                  never changes the transport.
# ssh (default) is the behavior described above: a public IP, a per-caller /32
# tcp/22 rule, and an alias whose HostName is that IP. ssm reaches the box over
# AWS Systems Manager Session Manager instead: the alias's HostName is the
# instance ID and a ProxyCommand runs `aws ssm start-session --region <region>
# --target %h --document-name AWS-StartSSHSession --parameters portNumber=22`.
# SSH user/key authentication is unchanged; readiness, the host-identity probe,
# interactive ssh, and rsync all go through that alias. Under ssm:
#   * a FRESH launch requires REPO_REMOTE_INSTANCE_PROFILE (exit 2 before any
#     mutation otherwise) and gets a tool-owned security group tagged
#     repo-remote-ssm=<name> with NO inbound rule. An explicit
#     REPO_REMOTE_SECURITY_GROUP, or a previously created SSM group, that has
#     any inbound rule is refused (exit 2) — its rules are never deleted.
#   * no ingress is authorized or revoked, no current-IP lookup is made, and no
#     public IP is polled — on create OR reuse. A reused instance's existing
#     exposure is reported, never "fixed", and a reused instance without an
#     instance profile gets a warning (a role is never attached automatically).
#   * the local `session-manager-plugin` must be installed (exit 2 before any
#     cloud call otherwise; REPO_REMOTE_SSM_PLUGIN overrides the binary name).
#   * the readiness probe retries the agent-registration delay
#     (TargetNotConnected) within REPO_REMOTE_SSH_READY_TIMEOUT, fails at once on
#     an access denial or a missing plugin, and never falls back to direct SSH.
# Invalid values (an unknown transport, ssm or a profile with GCP, a malformed
# profile name, a region that is not a plain AWS region code) exit 2 before any
# cloud call. The `up` JSON gains additive "transport", "connect_target" (the
# alias HostName: IP or instance ID) and "instance_profile" fields; under ssm
# "public_ip" is "" because none is looked up.
#
# AWS market (repo#616): which EC2 purchasing option a FRESH launch uses,
# AWS-only, resolved through the same two config layers:
#   REPO_REMOTE_MARKET   on-demand (default) | spot — exactly those values.
# on-demand (or unset) changes nothing: no market flags are passed. spot adds,
# to the SAME single run-instances call, the two argv elements
#   --instance-market-options MarketType=spot,SpotOptions={SpotInstanceType=one-time,InstanceInterruptionBehavior=terminate}
#   --instance-initiated-shutdown-behavior terminate
# so a Spot interruption AND the idle guard's `shutdown -h` both TERMINATE the
# box (root volume included); it is never stopped and never restarted. Hence,
# with spot configured:
#   * `up` never starts a stopped instance (pinned or tag-discovered) — exit 2
#     with guidance, before start-instances is called. A running instance is
#     still reused; a pinned instance that is already terminated (or
#     shutting-down) is treated as gone and a fresh one is launched.
#   * `down --yes` without --delete is refused (exit 2) before any cloud call;
#     `down --yes --delete` terminates as usual (fleet-marker guard included).
#   * a capacity or price rejection is a hard failure (exit 4). There is no
#     retry, no fallback to on-demand, and no other instance type: run-instances
#     is still called EXACTLY ONCE.
# Any other value, or spot with GCP, exits 2 before any cloud call. The plan,
# `up` and `down` JSON gain an additive "market" field. The estimated cost
# stays the on-demand rate (also the default Spot maximum price), so for Spot
# it is an upper bound; Spot price and capacity are never guaranteed.
#
# GitHub credentials on the VM (repo#565): `attach` is the only subcommand
# that touches a dev-session credential; `up` (dry run or --yes), `status`,
# `verify` and `down` never run the token command or send a token anywhere.
# attach REQUIRES a credential (repo#627): with no source configured it exits
# 7 instead of opening an unauthenticated session.
# Settings, resolved through the same two config layers (repo wins);
# precedence device > command > static:
#   REPO_REMOTE_GH_APP_CLIENT_ID  REQUIRED PATH. The public client ID of a
#                              GitHub App with Device Flow enabled. Each attach
#                              runs GitHub's OAuth device flow LOCALLY: it
#                              prints a verification URL and a one-time code on
#                              stderr, waits (honoring interval / slow_down, at
#                              most expires_in, capped at 15 min) while the
#                              person running attach approves it in a browser,
#                              and delivers the resulting GitHub App USER access
#                              token (that person's own account, limited to the
#                              App's permissions, expiring after about 8 hours).
#                              No client secret and no refresh token; re-run
#                              attach to refresh. The device code reaches curl
#                              on stdin only, never argv. Declined, expired,
#                              timed out, unreachable or malformed: exit 7.
#   REPO_REMOTE_GH_TOKEN_CMD   A command run LOCALLY by
#                              `bash -c` (stdin </dev/null) whose stdout is the
#                              token, e.g. a GitHub App installation-token
#                              minter. Used when no client ID is set; when
#                              non-empty it takes precedence over
#                              REPO_REMOTE_GH_TOKEN. It runs with this
#                              process's environment and the operator's own
#                              trust (the config files are already sourced as
#                              shell); it may use local credentials, which are
#                              never transmitted — only its stdout is. Fails
#                              closed (exit 7, nothing sent, no static-token
#                              fallback) on a non-zero exit, empty output,
#                              more than one line, or any whitespace/control
#                              character in the token. Its stdout and stderr
#                              are never displayed.
#   REPO_REMOTE_GH_TOKEN       a static token (legacy). REFUSED (exit 2, before
#                              connecting) unless REPO_REMOTE_ALLOW_STATIC_GH_TOKEN=1
#                              is also set — whatever else is configured, so a
#                              shared remote.env cannot quietly keep a shared
#                              PAT. With the opt-out it is delivered the same
#                              way, with a prominent warning on every attach
#                              that the session runs as one shared, long-lived
#                              identity. Never written anywhere.
#   REPO_REMOTE_ALLOW_STATIC_GH_TOKEN  `1` = break-glass opt-out for the above.
#   REPO_REMOTE_GH_API_HOST    optional API gateway hostname (bare DNS name, no
#                              scheme/port/path; github.com and *.github.com
#                              refused). The gateway must serve the GitHub
#                              Enterprise Server API layout over HTTPS with a
#                              certificate the VM trusts (gh offers no
#                              verification bypass). The session then gets
#                              GH_HOST=<gw>, GH_REPO=<gw>/<owner>/<repo> (from
#                              this checkout's github.com origin) and the token
#                              as GH_ENTERPRISE_TOKEN, with GH_TOKEN emptied —
#                              gh would otherwise talk to github.com directly.
#                              git keeps its github.com remote and authenticates
#                              there with the same token. Invalid values exit 2
#                              before anything connects.
# Delivery: the token travels as the SSH environment value
# LC_REPO_REMOTE_GH_TOKEN (SendEnv; Ubuntu's stock sshd has `AcceptEnv LANG
# LC_*`) on a session multiplexed over the verified master connection. The
# remote bootstrap unsets that variable and exports the token only into the
# process it starts: `docker exec -e GH_TOKEN` (name only — exec environment
# is not part of the container's persisted configuration) or the host login
# shell. git is pointed at it through GIT_CONFIG_* environment entries that
# reset any configured github.com credential helper (so no `store` helper can
# persist it) and add one that reads the variable by NAME. Nothing is written
# to remote.env, the per-repo config, the SSH config, or the VM's disk; no
# `gh auth login`, no stored git credential.
# Before the session starts, the VM runs `gh api user --jq .login` where the
# session will run and prints "acting as GitHub user <login>" (repo#627); a
# failed lookup is a warning, never a refusal.
#
# Exit codes:
#   0  success (including a dry-run plan)
#   2  missing / invalid required config (the cost gate; loud failure) — also
#      the REPO_REMOTE_MARKET=spot lifecycle refusals (restarting a stopped
#      instance, `down --yes` without --delete; repo#616) — also
#      the SSH-ingress fail-closed refusals: current-IP detection failed and no
#      REPO_REMOTE_SSH_CIDR was pinned, or the pinned CIDR is wider than
#      REPO_REMOTE_SSH_MIN_PREFIX without REPO_REMOTE_ALLOW_WORLD_SSH=1
#      (repo#487)
#   3  provider authentication failed
#   4  cloud operation failed (for `prune`: the provider lookup failed and
#      nothing was removed)
#   5  refused to act (reuse via `up`, stop/terminate via `down`) on a
#      fleet-marked instance (pass --force to override)
#   6  host-identity verification failed — the host reachable at the SSH alias
#      is not the instance this repo expects, or its identity could not be
#      established (NOT overridable with --force)
#   7  `attach` could not resolve the dev-session credential: none is
#      configured, the device flow was declined, expired, timed out or failed,
#      or REPO_REMOTE_GH_TOKEN_CMD failed or printed a malformed token. Nothing
#      was sent to the VM and no other source was used instead.
#   64 usage error
# (`attach` otherwise exits with the remote session's own status.)
#
# Testability hooks (honored so the suite can exercise the full contract against
# mocked cloud CLIs without touching real infrastructure or a real ~/.ssh):
#   XDG_CONFIG_HOME            locates the shared remote.env (already standard)
#   REPO_REMOTE_SSH_CONFIG     SSH config file to write the alias into
#                              (default: ~/.ssh/config)
#   PATH                       mock `aws`/`gcloud`/`curl`/`ssh` are picked up
#                              from PATH (curl backs current-IP detection,
#                              ssh backs the end-of-run reachability check)
#   REPO_REMOTE_IP_ECHO_URL    override the HTTPS echo service used for
#                              current-IP detection (default:
#                              checkip.amazonaws.com); see aws_resolve_ssh_cidr
#   REPO_REMOTE_SSH_LOCK_TIMEOUT       seconds to wait for the write_ssh_alias
#                                      lock before failing loudly (default 15;
#                                      see "SSH alias lock" below, repo#213)
#   REPO_REMOTE_SSH_LOCK_POLL_INTERVAL seconds between lock-acquisition
#                                      retries (default 1)
#   REPO_REMOTE_CALLER                 stable id that tags this host/operator's
#                                      own tcp/22 rules (Description marker), so
#                                      a second caller's `up` on a shared box
#                                      never revokes this one's rule and locks
#                                      it out (default: short hostname;
#                                      rjwalters/loom#11616)
#   REPO_REMOTE_SSH_READY_TIMEOUT      total seconds the end-of-run SSH
#                                      reachability probe waits for a
#                                      still-booting guest before failing
#                                      loudly (default 120; see
#                                      aws_check_reachability, repo#449)
#   REPO_REMOTE_SSH_READY_POLL_INTERVAL seconds between readiness probe
#                                      attempts (default 5)
#   REPO_REMOTE_IP_POLL_ATTEMPTS       how many times `up` polls for the
#                                      instance's public IP after it is running
#                                      (default 6; see aws_wait_public_ip,
#                                      repo#451)
#   REPO_REMOTE_IP_POLL_INTERVAL       seconds between those polls (default 2;
#                                      0 makes the suite's exhausted-budget
#                                      case instant)
#   REPO_REMOTE_HOST_ID_FILE           on-host path of the instance-id marker
#                                      written at provision time and read back
#                                      by `verify` (default
#                                      /etc/repo-remote-instance-id; repo#458)
#   REPO_REMOTE_VERIFY_SSH_TIMEOUT     ConnectTimeout for the single
#                                      host-identity probe session (default 10)
#   REPO_REMOTE_SSM_PLUGIN             name/path of the Session Manager plugin
#                                      the ssm-transport preflight looks for
#                                      (default session-manager-plugin)
#   REPO_REMOTE_ATTACH_CTL_BASE        parent directory for `attach`'s private
#                                      (mode 700) SSH control-socket directory
#                                      (default /tmp — short, because a unix
#                                      socket path is limited to ~104 bytes)
#
set -uo pipefail

# ── output helpers ──────────────────────────────────────────────────────────
log()  { printf '%s\n' "repo-remote: $*" >&2; }
die()  { local code="$1"; shift; printf '%s\n' "repo-remote: ERROR: $*" >&2; exit "$code"; }

JSON_OUT=false   # --json
YES=false        # --yes
FORCE=false      # --force (override the fleet-marker guard)
DELETE=false     # --down --delete
ATTACH_MODE=auto       # attach: auto | container | host (repo#565)
ATTACH_COMMAND=""      # attach --command <cmd>
ATTACH_HAS_COMMAND=false
ACTION=""        # up | down | status | verify | attach | prune
PROVIDER_ARG=""  # aws | gcp (positional override)

# ── JSON emission (no jq dependency for output; values are controlled) ──────
# json_escape <string> -> a JSON string body (without surrounding quotes)
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\t'/\\t}"
  s="${s//$'\r'/}"
  printf '%s' "$s"
}

# ── config loading ──────────────────────────────────────────────────────────
# Load the two env layers into the environment, shared first then repo (repo
# wins because it is sourced last). Mirrors commands/repo/remote.md step 2.
SHARED_ENV=""
REPO_ENV=""
REPO_ENV_OVERRIDDEN=false   # true when REPO_ENV came from REPO_REMOTE_ENV_FILE (repo#492)
GIT_ROOT=""
# The caller's REPO_REMOTE_ENV_FILE, captured before any config file is sourced
# so an explicit environment value beats one set in the shared remote.env.
ENV_FILE_FROM_ENV="${REPO_REMOTE_ENV_FILE:-}"

# expand_tilde <path> -> the path with a leading `~` / `~/` expanded to $HOME
expand_tilde() {
  local p="$1"
  # shellcheck disable=SC2088  # matching a literal `~`, not expanding one
  case "$p" in
    "~")   printf '%s' "$HOME" ;;
    "~/"*) printf '%s/%s' "$HOME" "${p:2}" ;;
    *)     printf '%s' "$p" ;;
  esac
}

# apply_env_file_override -- point REPO_ENV at REPO_REMOTE_ENV_FILE when set
# (environment first, then whatever the shared remote.env set), else leave the
# <git-root>/.env default from resolve_paths in place.
apply_env_file_override() {
  local override="${ENV_FILE_FROM_ENV:-${REPO_REMOTE_ENV_FILE:-}}"
  if [[ -n "$override" ]]; then
    REPO_ENV="$(expand_tilde "$override")"
    REPO_ENV_OVERRIDDEN=true
  fi
}

# is_linked_worktree -- true when the current checkout is a linked git worktree
# (`git worktree add`), false in the primary checkout, a plain clone, or outside
# git. The standard test: --git-dir differs from --git-common-dir only in a
# linked worktree. Both are resolved physically, since git may print either one
# relative to the cwd.
is_linked_worktree() {
  local gitdir commondir
  gitdir="$(git rev-parse --git-dir 2>/dev/null)" || return 1
  commondir="$(git rev-parse --git-common-dir 2>/dev/null)" || return 1
  [[ -n "$gitdir" && -n "$commondir" ]] || return 1
  gitdir="$(cd "$gitdir" 2>/dev/null && pwd -P)" || return 1
  commondir="$(cd "$commondir" 2>/dev/null && pwd -P)" || return 1
  [[ "$gitdir" != "$commondir" ]]
}

# primary_checkout_name -- the directory name of the PRIMARY checkout (#628):
# the parent of `git rev-parse --git-common-dir`, resolved physically. The
# primary checkout and every linked worktree share one common dir, so they all
# get the same name -- and therefore the same repo-remote-<name> alias,
# repo-remote=<name> instance tag and SSH ingress marker. Prints nothing and
# returns 1 when the common dir is not a directory literally named `.git`
# (bare repo, submodule, no git), so the caller falls back to the
# --show-toplevel basename.
primary_checkout_name() {
  local commondir
  commondir="$(git rev-parse --git-common-dir 2>/dev/null)" || return 1
  [[ -n "$commondir" ]] || return 1
  commondir="$(cd "$commondir" 2>/dev/null && pwd -P)" || return 1
  [[ "$(basename "$commondir")" == .git ]] || return 1
  basename "$(dirname "$commondir")"
}

# warn_worktree_env <read|write> -- repo#538: loud warning when an in-tree
# <git-root>/.env is about to be read or written from inside a linked worktree.
# Callers gate on IN_LINKED_WORKTREE + !REPO_ENV_OVERRIDDEN; this only prints.
warn_worktree_env() {
  local verb
  case "$1" in
    read)  verb="loading per-repo config from" ;;
    *)     verb="writing the instance id into" ;;
  esac
  log "WARNING: ${verb} ${REPO_ENV}, a .env inside a linked git worktree."
  log "  Worktrees get copied/rsynced and a gitignored .env is one 'git add -f' from leaking;"
  log "  move these settings out of the tree and set REPO_REMOTE_ENV_FILE=<absolute path>"
  log "  (e.g. ~/.config/<repo>/remote.env) in your environment or the shared remote.env."
}

IN_LINKED_WORKTREE=false   # set by resolve_paths (repo#538)

resolve_paths() {
  SHARED_ENV="${XDG_CONFIG_HOME:-$HOME/.config}/repo/remote.env"
  GIT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  [[ -n "$GIT_ROOT" ]] && REPO_ENV="$GIT_ROOT/.env"
  if [[ -n "$GIT_ROOT" ]] && is_linked_worktree; then
    IN_LINKED_WORKTREE=true
  fi
  apply_env_file_override
}

load_config() {
  # Config files hold secrets (cloud keys, REPO_REMOTE_GH_TOKEN): sourcing them
  # under `bash -x` would print every assignment, so tracing is suspended here
  # (repo#565) and restored afterwards.
  local _x=""
  [[ $- == *x* ]] && { _x=1; set +x; }
  set -a
  # shellcheck disable=SC1090
  [[ -f "$SHARED_ENV" ]] && . "$SHARED_ENV"
  set +a
  # The shared file may itself name the per-repo file (repo#492).
  apply_env_file_override
  # repo#538: one warning (not per key) when the per-repo layer is an in-tree
  # .env inside a linked worktree. Still sourced -- warn, then proceed.
  if [[ -n "$REPO_ENV" && -f "$REPO_ENV" && "$IN_LINKED_WORKTREE" == true \
        && "$REPO_ENV_OVERRIDDEN" != true ]]; then
    warn_worktree_env read
  fi
  set -a
  # shellcheck disable=SC1090
  [[ -n "$REPO_ENV" && -f "$REPO_ENV" ]] && . "$REPO_ENV"
  set +a
  [[ -n "$_x" ]] && set -x
  return 0
}

# ── effective settings ──────────────────────────────────────────────────────
PROVIDER=""
NAME=""
INSTANCE_TYPE=""
INSTANCE_ID=""
DISK_GB=""
VOLUME_TYPE=""       # AWS only: root EBS volume type, gp3 (default) | gp2 (repo#559)
VOLUME_IOPS=""       # AWS only, gp3 only: provisioned IOPS (default 3000)
VOLUME_THROUGHPUT="" # AWS only, gp3 only: provisioned throughput, MiB/s (default 125)
IMAGE=""
GPU_ACCEL=""       # GCP accelerator string, e.g. nvidia-l4:1
IDLE_MIN=""
IS_GPU=false
FLEET_TAG_KEY=""   # tag/label key that marks a managed fleet host ("" disables)
FLEET_TAG_VALUE="" # required value for that key ("" = any non-empty value)
SSH_CIDR=""        # AWS only: pinned SSH-ingress CIDR override (see aws_resolve_ssh_cidr)
SSH_MIN_PREFIX=""  # AWS only: narrowest IPv4 prefix length accepted for SSH ingress (default 32)
ALLOW_WORLD_SSH="" # AWS only: "1" opts in to an SSH-ingress CIDR wider than SSH_MIN_PREFIX
TRANSPORT=""        # AWS only: ssh (default) | ssm (repo#564)
INSTANCE_PROFILE="" # AWS only: IAM instance profile name for a fresh launch
MARKET=""           # AWS only: on-demand (default) | spot (repo#616)
REGION=""
COST_HOURLY=""
COST_APPROX=false
COST_BASIS=""      # "table" | "vcpu-scaled" | "heuristic" — how COST_HOURLY was derived

# GPU-family detection: infer a GPU host from the instance family so the caller
# needn't set a separate flag (remote.md "GPU hosts").
is_gpu_family() {  # <provider> <instance-type>
  local p="$1" t="$2"
  [[ -n "${GPU_ACCEL:-}" ]] && return 0
  case "$p" in
    aws) [[ "$t" =~ ^(g3|g4|g4dn|g5|g5g|g6|g6e|p2|p3|p4|p4d|p5)\. ]] && return 0 ;;
    gcp) [[ "$t" =~ ^(g2|a2|a3)- ]] && return 0 ;;
  esac
  return 1
}

# AWS instance-type size suffixes double vCPU count in a well-known, fixed
# progression (large=2 ... 32xlarge=128). This holds for AWS's current
# general-purpose/compute/memory families named `<family>.<size>` — but NOT
# for the burstable `t`-family (a differently-priced CPU-credit model, not a
# flat vCPU rate) or for nano/micro/small/medium sizes (they don't follow the
# doubling pattern). Prints the vCPU count on stdout and returns 0 when the
# type is confidently parseable this way; returns 1 (no output) otherwise so
# the caller falls through to the last-resort flat heuristic.
aws_vcpu_from_size() {  # <instance-type> -> vcpu count
  local t="$1" family size
  family="${t%%.*}"
  size="${t#*.}"
  [[ "$family" == "$size" ]] && return 1   # no "family.size" dot -> not AWS-style
  [[ "$family" =~ ^t[0-9] ]] && return 1   # burstable credit model, not a flat rate
  case "$size" in
    large)    printf '2'   ;;
    xlarge)   printf '4'   ;;
    2xlarge)  printf '8'   ;;
    4xlarge)  printf '16'  ;;
    8xlarge)  printf '32'  ;;
    12xlarge) printf '48'  ;;
    16xlarge) printf '64'  ;;
    24xlarge) printf '96'  ;;
    32xlarge) printf '128' ;;
    *) return 1 ;;
  esac
}

# Approximate on-demand USD/hour by instance type. COST_BASIS records how the
# number was derived so a caller (and the plan/up output) can distinguish a
# real price-table hit from a scaled or last-resort guess:
#   table        — exact match in the case table below
#   vcpu-scaled  — no table entry, but the AWS size suffix parsed to a vCPU
#                  count, scaled by a blended $/vCPU-hr rate
#   heuristic    — no table entry and no parseable vCPU count (or a GPU type
#                  with no table entry — vCPU count is a poor proxy for GPU
#                  instance price, so those stay on the flat GPU heuristic)
# The JSON always carries a number so a caller can implement a budget check,
# and never silently claims precision it doesn't have.
estimate_cost() {  # sets COST_HOURLY, COST_APPROX, COST_BASIS
  local t="$1"
  COST_APPROX=false
  COST_BASIS="table"
  case "$t" in
    # AWS general purpose / compute
    t3.medium)   COST_HOURLY=0.0416 ;;
    t3.large)    COST_HOURLY=0.0832 ;;
    t3.xlarge)   COST_HOURLY=0.1664 ;;
    t3.2xlarge)  COST_HOURLY=0.3328 ;;
    m5.large)    COST_HOURLY=0.096  ;;
    m5.xlarge)   COST_HOURLY=0.192  ;;
    m5.2xlarge)  COST_HOURLY=0.384  ;;
    m5.4xlarge)  COST_HOURLY=0.768  ;;
    m6i.xlarge)  COST_HOURLY=0.192  ;;
    m6i.2xlarge) COST_HOURLY=0.384  ;;
    c5.xlarge)   COST_HOURLY=0.17   ;;
    c5.2xlarge)  COST_HOURLY=0.34   ;;
    c5.4xlarge)  COST_HOURLY=0.68   ;;
    # AWS current-gen (7th-gen) compute/general/memory, x86 (c7i/m7i/r7i).
    # Approximate on-demand, us-east-1, derived from a ~$0.0446/vCPU-hr
    # blended rate (cross-checked against this issue's reported
    # c7i.24xlarge ~$4.28/hr anchor: 4.28 / 96 vCPU ≈ $0.0446/vCPU-hr) —
    # re-verify against live AWS pricing before relying on these for a
    # budget-critical decision; larger/unlisted sizes fall through to the
    # vcpu-scaled fallback below rather than being hand-populated here.
    c7i.large)   COST_HOURLY=0.0893 ;;
    c7i.xlarge)  COST_HOURLY=0.1785 ;;
    c7i.2xlarge) COST_HOURLY=0.357  ;;
    m7i.large)   COST_HOURLY=0.1008 ;;
    m7i.xlarge)  COST_HOURLY=0.2016 ;;
    r7i.large)   COST_HOURLY=0.1323 ;;
    r7i.xlarge)  COST_HOURLY=0.2646 ;;
    # AWS GPU
    g4dn.xlarge) COST_HOURLY=0.526  ;;
    g5.xlarge)   COST_HOURLY=1.006  ;;
    g5.2xlarge)  COST_HOURLY=1.212  ;;
    g6.xlarge)   COST_HOURLY=0.8048 ;;
    g6e.xlarge)  COST_HOURLY=1.861  ;;
    g6e.2xlarge) COST_HOURLY=2.242  ;;
    p4d.24xlarge) COST_HOURLY=32.77 ;;
    # GCP predefined
    e2-standard-2)  COST_HOURLY=0.067 ;;
    e2-standard-4)  COST_HOURLY=0.134 ;;
    e2-standard-8)  COST_HOURLY=0.268 ;;
    n1-standard-4)  COST_HOURLY=0.19  ;;
    n1-standard-8)  COST_HOURLY=0.38  ;;
    g2-standard-4)  COST_HOURLY=0.71  ;;
    g2-standard-8)  COST_HOURLY=0.85  ;;
    a2-highgpu-1g)  COST_HOURLY=3.67  ;;
    *)
      COST_APPROX=true
      local vcpu
      if [[ "$IS_GPU" != true ]] && vcpu="$(aws_vcpu_from_size "$t")"; then
        COST_BASIS="vcpu-scaled"
        COST_HOURLY="$(awk -v v="$vcpu" 'BEGIN{printf "%.4f", v * 0.045}')"
      else
        COST_BASIS="heuristic"
        if [[ "$IS_GPU" == true ]]; then COST_HOURLY=1.50; else COST_HOURLY=0.20; fi
      fi
      ;;
  esac
  # A GCP accelerator adds to the machine price; fold in a rough per-card cost so
  # the estimate for GPU-on-GCP is not silently the bare-machine price.
  if [[ -n "${GPU_ACCEL:-}" && "$PROVIDER" == gcp ]]; then
    COST_APPROX=true
    COST_HOURLY="$(awk -v c="$COST_HOURLY" 'BEGIN{printf "%.4f", c + 0.70}')"
  fi
}

resolve_settings() {
  PROVIDER="${PROVIDER_ARG:-${REPO_REMOTE_PROVIDER:-}}"
  # lower-case the provider
  PROVIDER="$(printf '%s' "$PROVIDER" | tr '[:upper:]' '[:lower:]')"

  # REPO_REMOTE_NAME (#664) overrides the derived box name; empty/unset keeps
  # the primary-checkout / basename behavior. Validated before any cloud call.
  # REPO_DIR is always the checkout's directory name (where the repo lives on
  # the VM, ~/<REPO_DIR>); only NAME, the box identity, is overridable.
  REPO_DIR="$(primary_checkout_name || true)"
  [[ -n "$REPO_DIR" ]] || REPO_DIR="$(basename "${GIT_ROOT:-$PWD}")"
  if [[ -n "${REPO_REMOTE_NAME:-}" ]]; then
    [[ "$REPO_REMOTE_NAME" =~ ^[A-Za-z0-9._-]+$ ]] \
      || die 2 "invalid REPO_REMOTE_NAME '${REPO_REMOTE_NAME}': use only letters, digits, '.', '_' and '-'"
    NAME="$REPO_REMOTE_NAME"
  else
    NAME="$REPO_DIR"
  fi
  CALLER="$(repo_remote_caller)"

  INSTANCE_TYPE="${REPO_REMOTE_INSTANCE_TYPE:-}"
  INSTANCE_ID="${REPO_REMOTE_INSTANCE_ID:-}"
  GPU_ACCEL="${REPO_REMOTE_GPU:-}"
  IMAGE="${REPO_REMOTE_IMAGE:-}"

  # Non-cost-relevant fields DO fall back to defaults (matches the prose command).
  DISK_GB="${REPO_REMOTE_DISK_GB:-50}"
  # AWS root-volume type and performance (repo#559). The type is lower-cased
  # and defaulted here; IOPS/throughput stay exactly as configured (possibly
  # empty) so validate_aws_volume_config() can tell "explicitly set alongside
  # gp2" apart from "unset", and it fills in the gp3 defaults itself.
  VOLUME_TYPE="$(printf '%s' "${REPO_REMOTE_VOLUME_TYPE:-gp3}" | tr '[:upper:]' '[:lower:]')"
  VOLUME_IOPS="${REPO_REMOTE_VOLUME_IOPS:-}"
  VOLUME_THROUGHPUT="${REPO_REMOTE_VOLUME_THROUGHPUT:-}"
  IDLE_MIN="${REPO_REMOTE_IDLE_SHUTDOWN_MIN:-120}"
  # Idle-exit marker contract (see commands/repo/remote.md). A daemon-managed
  # host (e.g. one running loom-daemon) may write this file on clean idle-exit;
  # the guard treats its mtime as an authoritative "idle since" timestamp. The
  # path is always embedded in the guard so the contract is self-contained and
  # works standalone — it stays inert until the file actually exists on-host.
  IDLE_MARKER="${REPO_REMOTE_IDLE_MARKER:-/var/run/repo-remote-daemon-idle.marker}"

  # Fleet marker (repo#164). Defaults match the tag 2am's remediation already
  # sets on its persistent workers, so the guard is useful with zero config.
  # An empty key is the deliberate opt-out: no marker lookup is performed.
  FLEET_TAG_KEY="${REPO_REMOTE_FLEET_TAG_KEY-Fleet}"
  FLEET_TAG_VALUE="${REPO_REMOTE_FLEET_TAG_VALUE-loom}"

  # AWS-only SSH-ingress CIDR override (repo#176). Unset (the default) means
  # "detect it"; see aws_resolve_ssh_cidr for the detection logic, which now
  # fails CLOSED rather than falling back to 0.0.0.0/0 (repo#487).
  SSH_CIDR="${REPO_REMOTE_SSH_CIDR:-}"
  # Narrowest IPv4 prefix length accepted for SSH ingress (repo#487). The
  # default 32 means "a single address"; raise the allowed width deliberately
  # (e.g. 24 for a known ISP block) rather than by accident. Anything wider
  # than this — 0.0.0.0/0 included — additionally requires
  # REPO_REMOTE_ALLOW_WORLD_SSH=1.
  SSH_MIN_PREFIX="${REPO_REMOTE_SSH_MIN_PREFIX:-32}"
  ALLOW_WORLD_SSH="${REPO_REMOTE_ALLOW_WORLD_SSH:-0}"

  # Transport + instance profile (repo#564). Validated, before any cloud call,
  # by validate_transport_config().
  TRANSPORT="$(printf '%s' "${REPO_REMOTE_TRANSPORT:-ssh}" | tr '[:upper:]' '[:lower:]')"
  INSTANCE_PROFILE="${REPO_REMOTE_INSTANCE_PROFILE:-}"

  # Purchasing option (repo#616). Taken verbatim — validate_market_config()
  # accepts exactly "on-demand" or "spot" — so a typo can never be normalized
  # into a billable mode. Unset or empty means the historical default.
  MARKET="${REPO_REMOTE_MARKET:-on-demand}"

  # Dev-session GitHub credential (repo#565). Only the SOURCE is decided here;
  # nothing is run or read beyond the config values themselves.
  resolve_gh_credential_config

  case "$PROVIDER" in
    aws) REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}" ;;
    gcp) REGION="${GCP_ZONE:-}" ;;
  esac

  if [[ -n "$INSTANCE_TYPE" ]] && is_gpu_family "$PROVIDER" "$INSTANCE_TYPE"; then
    IS_GPU=true
  fi
  [[ -n "$INSTANCE_TYPE" ]] && estimate_cost "$INSTANCE_TYPE"
}

# ── the cost gate ───────────────────────────────────────────────────────────
# Enforce that every field whose absence could cause an *unexpected* bill is
# present in config. This is what makes `--yes` safe: it removes the interactive
# prompt but not the requirement that the human pre-supplied the budget-relevant
# choices. Missing config fails loudly (exit 2), never a silent default.
require_cost_config() {
  local missing=()

  [[ -n "$PROVIDER" ]] || missing+=("REPO_REMOTE_PROVIDER (or an aws|gcp argument)")
  case "$PROVIDER" in
    aws)
      [[ -n "${AWS_ACCESS_KEY_ID:-}" ]]     || missing+=("AWS_ACCESS_KEY_ID")
      [[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]] || missing+=("AWS_SECRET_ACCESS_KEY")
      [[ -n "$REGION" ]]                    || missing+=("AWS_REGION")
      ;;
    gcp)
      [[ -n "${GCP_PROJECT:-}" ]]                     || missing+=("GCP_PROJECT")
      [[ -n "$REGION" ]]                              || missing+=("GCP_ZONE")
      [[ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]  || missing+=("GOOGLE_APPLICATION_CREDENTIALS")
      ;;
    "") : ;;  # provider itself already reported above
    *)  die 2 "unknown provider '$PROVIDER' (expected aws or gcp)" ;;
  esac

  # THE cost-relevant field. Never defaulted — an unpinned instance type is
  # exactly how an unexpected bill happens.
  [[ -n "$INSTANCE_TYPE" ]] || missing+=("REPO_REMOTE_INSTANCE_TYPE (required — no default, so a run never silently picks a billable size)")

  if [[ ${#missing[@]} -gt 0 ]]; then
    local m
    log "cannot proceed — required config is missing (no silent defaults for cost-relevant fields):"
    for m in "${missing[@]}"; do log "  - $m"; done
    log "set them in ${SHARED_ENV} (shared) or ${REPO_ENV:-<git-root>/.env or REPO_REMOTE_ENV_FILE} (per-repo), or run /repo:remote --configure."
    exit 2
  fi
}

# ── AWS root-volume configuration (repo#559) ────────────────────────────────
# Limits below are AWS's documented EBS values (Amazon EBS User Guide,
# "General Purpose SSD volumes", checked 2026-10-05): gp3 = 1 GiB-64 TiB,
# 3,000 IOPS / 125 MiB/s baseline included, provisionable up to 80,000 IOPS at
# <= 500 IOPS per GiB and up to 2,000 MiB/s at <= 0.25 MiB/s per provisioned
# IOPS; gp2 = 1 GiB-16 TiB with performance derived from size (3 IOPS/GiB), so
# it takes no IOPS/throughput parameters at all. AWS has raised these ceilings
# before; if they move again, update the constants rather than the logic.
GP3_MIN_IOPS=3000
GP3_MAX_IOPS=80000
GP3_MAX_IOPS_PER_GIB=500
GP3_MIN_THROUGHPUT=125
GP3_MAX_THROUGHPUT=2000
GP3_MAX_GIB=65536
GP2_MAX_GIB=16384

is_positive_int() {  # <value> -> 0 for a plain positive decimal integer
  # Capped at 9 digits so the arithmetic below can never overflow.
  [[ "$1" =~ ^[1-9][0-9]{0,8}$ ]]
}

# Validate (and default) the AWS root-volume settings. Runs for every AWS `up`
# — dry run included — BEFORE any cloud call, so a bad value fails loudly
# (exit 2) without spending or mutating anything. GCP never reaches this.
validate_aws_volume_config() {
  local -a bad=()
  is_positive_int "$DISK_GB" \
    || bad+=("REPO_REMOTE_DISK_GB='${DISK_GB}' must be a positive whole number of GiB")

  case "$VOLUME_TYPE" in
    gp3)
      VOLUME_IOPS="${VOLUME_IOPS:-$GP3_MIN_IOPS}"
      VOLUME_THROUGHPUT="${VOLUME_THROUGHPUT:-$GP3_MIN_THROUGHPUT}"
      local iops_ok=false tp_ok=false
      if ! is_positive_int "$VOLUME_IOPS"; then
        bad+=("REPO_REMOTE_VOLUME_IOPS='${VOLUME_IOPS}' must be a positive whole number")
      elif (( VOLUME_IOPS < GP3_MIN_IOPS || VOLUME_IOPS > GP3_MAX_IOPS )); then
        bad+=("REPO_REMOTE_VOLUME_IOPS=${VOLUME_IOPS} is outside the gp3 range ${GP3_MIN_IOPS}-${GP3_MAX_IOPS}")
      else
        iops_ok=true
      fi
      if ! is_positive_int "$VOLUME_THROUGHPUT"; then
        bad+=("REPO_REMOTE_VOLUME_THROUGHPUT='${VOLUME_THROUGHPUT}' must be a positive whole number of MiB/s")
      elif (( VOLUME_THROUGHPUT < GP3_MIN_THROUGHPUT || VOLUME_THROUGHPUT > GP3_MAX_THROUGHPUT )); then
        bad+=("REPO_REMOTE_VOLUME_THROUGHPUT=${VOLUME_THROUGHPUT} is outside the gp3 range ${GP3_MIN_THROUGHPUT}-${GP3_MAX_THROUGHPUT} MiB/s")
      else
        tp_ok=true
      fi
      if is_positive_int "$DISK_GB"; then
        (( DISK_GB > GP3_MAX_GIB )) \
          && bad+=("REPO_REMOTE_DISK_GB=${DISK_GB} exceeds the gp3 maximum of ${GP3_MAX_GIB} GiB")
        # The 3,000 IOPS baseline is included at every size; only IOPS
        # provisioned ABOVE it are bound by the per-GiB ratio.
        if [[ "$iops_ok" == true ]] && (( VOLUME_IOPS > GP3_MIN_IOPS && VOLUME_IOPS > DISK_GB * GP3_MAX_IOPS_PER_GIB )); then
          bad+=("REPO_REMOTE_VOLUME_IOPS=${VOLUME_IOPS} exceeds ${GP3_MAX_IOPS_PER_GIB} IOPS per GiB for a ${DISK_GB} GiB gp3 volume (max $(( DISK_GB * GP3_MAX_IOPS_PER_GIB ))); raise REPO_REMOTE_DISK_GB or lower the IOPS")
        fi
      fi
      # 0.25 MiB/s per provisioned IOPS  <=>  throughput * 4 <= IOPS.
      if [[ "$iops_ok" == true && "$tp_ok" == true ]] && (( VOLUME_THROUGHPUT * 4 > VOLUME_IOPS )); then
        bad+=("REPO_REMOTE_VOLUME_THROUGHPUT=${VOLUME_THROUGHPUT} exceeds 0.25 MiB/s per provisioned IOPS (REPO_REMOTE_VOLUME_IOPS=${VOLUME_IOPS} allows at most $(( VOLUME_IOPS / 4 )) MiB/s)")
      fi
      ;;
    gp2)
      # gp2 performance is a function of size; AWS rejects Iops/Throughput on
      # it. Refuse rather than silently drop a setting the operator asked for.
      [[ -z "$VOLUME_IOPS" ]] \
        || bad+=("REPO_REMOTE_VOLUME_IOPS is set but REPO_REMOTE_VOLUME_TYPE=gp2 does not accept provisioned IOPS (unset it, or use gp3)")
      [[ -z "$VOLUME_THROUGHPUT" ]] \
        || bad+=("REPO_REMOTE_VOLUME_THROUGHPUT is set but REPO_REMOTE_VOLUME_TYPE=gp2 does not accept provisioned throughput (unset it, or use gp3)")
      if is_positive_int "$DISK_GB" && (( DISK_GB > GP2_MAX_GIB )); then
        bad+=("REPO_REMOTE_DISK_GB=${DISK_GB} exceeds the gp2 maximum of ${GP2_MAX_GIB} GiB")
      fi
      ;;
    *)
      bad+=("REPO_REMOTE_VOLUME_TYPE='${VOLUME_TYPE}' is not supported (expected gp3 or gp2)")
      ;;
  esac

  if [[ ${#bad[@]} -gt 0 ]]; then
    local b
    log "cannot proceed — invalid AWS root-volume config (nothing was created or changed):"
    for b in "${bad[@]}"; do log "  - $b"; done
    log "see 'AWS root volume' in commands/repo/remote.md for supported values."
    exit 2
  fi
}

# The --block-device-mappings value for the root volume. Type-aware: gp3 gets
# its explicit IOPS/throughput, gp2 gets neither (AWS rejects them there).
aws_root_block_device_mapping() {
  local ebs="VolumeSize=${DISK_GB},VolumeType=${VOLUME_TYPE}"
  if [[ "$VOLUME_TYPE" == gp3 ]]; then
    ebs+=",Iops=${VOLUME_IOPS},Throughput=${VOLUME_THROUGHPUT}"
  fi
  printf 'DeviceName=/dev/sda1,Ebs={%s}' "$ebs"
}

# ── transport configuration (repo#564) ──────────────────────────────────────
# Every value that ends up inside a generated SSH config line (and, for ssm,
# inside a ProxyCommand that ssh hands to a shell) is matched against a strict
# allow-list here, so a config value can never add an SSH directive or a shell
# command. Runs for `up` (dry run included) and `verify` BEFORE any cloud call.
is_aws_region() { [[ "$1" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]{1,2}$ ]]; }
# IAM instance-profile names: 1-128 of [A-Za-z0-9+=,.@_-] (IAM naming rules).
is_instance_profile_name() { [[ "$1" =~ ^[A-Za-z0-9+=,.@_-]{1,128}$ ]]; }
# EC2 instance ids are i-<hex>; letters beyond hex are tolerated (still no
# shell or ssh-config metacharacters) so fixtures can use readable ids.
is_instance_id() { [[ "$1" =~ ^i-[A-Za-z0-9]{1,32}$ ]]; }

validate_transport_config() {
  local -a bad=()
  case "$TRANSPORT" in
    ssh|ssm) : ;;
    *) bad+=("REPO_REMOTE_TRANSPORT='${TRANSPORT}' is not supported (expected ssh or ssm)") ;;
  esac
  if [[ "$PROVIDER" != aws ]]; then
    [[ "$TRANSPORT" == ssm ]] \
      && bad+=("REPO_REMOTE_TRANSPORT=ssm is AWS-only (provider is '${PROVIDER}'); GCP uses OS Login / IAP instead")
    [[ -n "$INSTANCE_PROFILE" ]] \
      && bad+=("REPO_REMOTE_INSTANCE_PROFILE is AWS-only (provider is '${PROVIDER}'); unset it")
  fi
  if [[ -n "$INSTANCE_PROFILE" ]] && ! is_instance_profile_name "$INSTANCE_PROFILE"; then
    bad+=("REPO_REMOTE_INSTANCE_PROFILE='${INSTANCE_PROFILE}' is not a valid IAM instance profile NAME (1-128 of A-Z a-z 0-9 + = , . @ _ -; pass the name, not an ARN)")
  fi
  if [[ "$TRANSPORT" == ssm && "$PROVIDER" == aws ]]; then
    [[ -z "$REGION" ]] || is_aws_region "$REGION" \
      || bad+=("AWS_REGION='${REGION}' is not a plain AWS region code (e.g. us-west-2); it is written into the SSM ProxyCommand, so nothing else is accepted")
  fi
  local user="${REPO_REMOTE_SSH_USER:-ubuntu}"
  [[ "$user" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$ ]] \
    || bad+=("REPO_REMOTE_SSH_USER='${user}' is not a plain user name (letters, digits, . _ -)")
  local key="${REPO_REMOTE_SSH_KEY:-~/.ssh/id_ed25519}"
  [[ "$key" != *$'\n'* && "$key" != *$'\r'* ]] \
    || bad+=("REPO_REMOTE_SSH_KEY contains a line break; it is written into the SSH config as one IdentityFile line")

  if [[ ${#bad[@]} -gt 0 ]]; then
    local b
    log "cannot proceed — invalid transport config (nothing was created or changed):"
    for b in "${bad[@]}"; do log "  - $b"; done
    log "see 'AWS transport: direct SSH or SSM Session Manager' in commands/repo/remote.md."
    exit 2
  fi
}

# ── purchasing option (repo#616) ────────────────────────────────────────────
# Runs for `up` (dry run included) and `down` BEFORE any cloud call. Exactly
# two spellings are accepted; anything else — including a differently-cased
# "Spot" — is refused rather than guessed at, because the value decides what
# kind of capacity is bought.
validate_market_config() {
  case "$MARKET" in
    on-demand) : ;;
    spot)
      [[ "$PROVIDER" == aws ]] \
        || die 2 "REPO_REMOTE_MARKET=spot is AWS-only (provider is '${PROVIDER}'); unset it or set REPO_REMOTE_MARKET=on-demand. Nothing was created or changed."
      ;;
    *)
      die 2 "REPO_REMOTE_MARKET='${MARKET}' is not supported (expected exactly 'on-demand' (the default) or 'spot'). Nothing was created or changed. See 'AWS market: on-demand or Spot' in commands/repo/remote.md."
      ;;
  esac
}

is_spot() { [[ "$PROVIDER" == aws && "$MARKET" == spot ]]; }

# The lifecycle refusal shared by both stopped-instance reuse paths in aws_up:
# a one-time Spot launch is terminated, never stopped, so a stopped box is not
# one this configuration can manage. Dies BEFORE start-instances is called.
spot_refuse_stopped_reuse() {  # <instance-id> <state> <how-it-was-resolved>
  local iid="$1" state="$2" how="$3"
  die 2 "REPO_REMOTE_MARKET=spot never restarts a stopped instance, but ${how} ${iid} is ${state}. A one-time Spot instance is terminated (not stopped) on interruption and on idle shutdown, so this box is not one the Spot configuration manages, and nothing was started or created. Either set REPO_REMOTE_MARKET=on-demand (or unset it) to restart this instance as before, or terminate it with 'repo-remote down --yes --delete' and re-run 'repo-remote up --yes' to launch a fresh Spot instance."
}

# The local half of the SSM prerequisites: the AWS CLI's Session Manager
# plugin. Checked before any cloud call. The remote half (instance profile,
# running agent, a network path to the regional SSM endpoints, the caller's
# ssm:StartSession grant) cannot be proved locally; the readiness probe reports
# those distinctly.
ssm_plugin_present() { command -v "${REPO_REMOTE_SSM_PLUGIN:-session-manager-plugin}" >/dev/null 2>&1; }
ssm_preflight() {
  ssm_plugin_present && return 0
  die 2 "REPO_REMOTE_TRANSPORT=ssm needs the AWS CLI Session Manager plugin ('${REPO_REMOTE_SSM_PLUGIN:-session-manager-plugin}' is not on PATH). Install it (AWS docs: \"Install the Session Manager plugin for the AWS CLI\"), or set REPO_REMOTE_TRANSPORT=ssh. Nothing was created or changed."
}

# ── the fleet-marker guard (reuse discovery, repo#164) ──────────────────────
# `up` never re-attaches user-data to an instance it reuses — the guard a host
# carries is whatever it got at its one-time creation. What reuse DOES do is
# start a stopped instance and rewrite this repo's SSH alias to point at it. The
# instance it reaches is resolved from a pinned REPO_REMOTE_INSTANCE_ID or from
# the repo-remote=<name> tag/label, and neither of those handles expires: a box
# provisioned once as an ephemeral dev session can since have become a
# persistent, daemon-managed fleet worker while still carrying the old tag. That
# is exactly how `repo-remote=anvil` tooling kept rediscovering a fleet host
# after it became a fleet host (operator incident, private tracker).
#
# So: before a REUSED instance is started or aliased, look for a fleet marker
# the fleet-management side already had to set deliberately elsewhere. This is a
# provisioning-time check against declared metadata — deliberately NOT an
# on-host "is some process running" heuristic, which repo#79 rejected for the
# guard's runtime logic and which nothing here reopens.

# True when a discovered marker value counts as "this is a fleet host".
# Compared case-insensitively: GCP lower-cases label values, AWS tags don't.
# An empty REPO_REMOTE_FLEET_TAG_VALUE means "any non-empty value matches".
fleet_marker_matches() {  # <discovered-value>
  local got want
  [[ -n "$1" && "$1" != "None" ]] || return 1
  [[ -n "$FLEET_TAG_VALUE" ]] || return 0
  got="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  want="$(printf '%s' "$FLEET_TAG_VALUE" | tr '[:upper:]' '[:lower:]')"
  [[ "$got" == "$want" ]]
}

# Warn-or-refuse once a marker has been read off a resource being reused.
# Dies with exit 5 unless --force; with --force it warns loudly and continues.
fleet_marker_gate() {  # <resource-id> <marker-value> <"tag"|"label">
  local id="$1" val="$2" kind="$3"
  fleet_marker_matches "$val" || return 0
  if [[ "$FORCE" == true ]]; then
    log "WARNING: ${id} carries the fleet marker ${kind} ${FLEET_TAG_KEY}=${val} — it looks like a managed fleet/daemon host, not an ephemeral dev box. Proceeding anyway because --force was given; this run will start and/or re-alias a production host."
    return 0
  fi
  # Same "repo-remote: ERROR:" shape as die(), but die() is a single line and
  # this refusal is only actionable with the remediation lines that follow it.
  printf '%s\n' "repo-remote: ERROR: refusing to reuse ${id}: it carries the fleet marker ${kind} ${FLEET_TAG_KEY}=${val}." >&2
  log "  That marker means the host is managed as part of a fleet (e.g. a persistent loom-daemon worker), so starting or re-aliasing it from ephemeral dev-session tooling is almost certainly not what you want (operator incident, private tracker)."
  log "  If you really mean to target it, re-run with --force."
  log "  To use a different box instead, clear REPO_REMOTE_INSTANCE_ID from ${REPO_ENV:-<git-root>/.env or REPO_REMOTE_ENV_FILE} (and/or remove the repo-remote=${NAME} tag from the fleet host)."
  log "  To disable this check entirely, set REPO_REMOTE_FLEET_TAG_KEY= (empty)."
  exit 5
}

# AWS: read the fleet tag off an instance. Echoes "" when absent/disabled.
aws_fleet_marker() {  # <instance-id>
  [[ -n "$FLEET_TAG_KEY" ]] || return 0
  local v
  v="$(aws ec2 describe-instances --instance-ids "$1" \
      --query "Reservations[0].Instances[0].Tags[?Key=='${FLEET_TAG_KEY}'].Value | [0]" \
      --output text 2>/dev/null)" || return 0
  [[ "$v" == "None" ]] && v=""
  printf '%s' "$v"
}

# GCP: read the fleet label off an instance. Label keys are lower-case on GCP,
# so the configured key is lower-cased for the lookup. Echoes "" when absent.
gcp_fleet_marker() {  # <instance-name>
  [[ -n "$FLEET_TAG_KEY" ]] || return 0
  local k v
  k="$(printf '%s' "$FLEET_TAG_KEY" | tr '[:upper:]' '[:lower:]')"
  v="$(gcloud compute instances describe "$1" --zone "$REGION" \
      --format="value(labels.${k})" 2>/dev/null || true)"
  printf '%s' "$v"
}

# ── the idle-shutdown guard (cloud-init user-data) ──────────────────────────
# A forgotten VM — GPU ones especially — must turn itself off. Emitted as a
# cloud-init script that installs a cron watchdog running `shutdown -h` after
# IDLE_MIN minutes with no active SSH session and low CPU.
#
# "Activity" is defined by exactly two local signals: an open SSH session (`who`)
# OR CPU load average > 0.2. There is NO process-name veto — a running daemon
# (loom-daemon or otherwise) does NOT, by itself, keep this host alive. If a
# future daemon-presence veto is ever wanted it must be added deliberately here
# and documented; it is not implied by the current logic.
#
# BACKGROUND JOBS DO NOT HOLD THIS GUARD (repo#451). A job started over a single
# non-interactive SSH command — `ssh <alias> 'nohup make -j8 &'` and friends —
# stops counting as activity via `who` the MOMENT that ssh command returns: the
# login shell that ran it has already exited, so there is no session left for
# `who` to report. From then on the job is protected ONLY by its own CPU usage
# keeping the load average above 0.2, and there is no process-name veto to fall
# back on. A long build with I/O-bound or license-wait lulls can dip under that
# threshold for a full IDLE_MIN window and be powered off mid-run (the reported
# incident: a ~20-minute nohup'd build killed by a 120-minute window's guard).
# For work that may go CPU-idle, either hold a real session for its duration
# (`tmux`/`screen` on the host, or an interactive SSH session left open) or size
# REPO_REMOTE_IDLE_SHUTDOWN_MIN to the job.
#
# Idle-exit marker contract (published by this repo so a daemon side can conform
# without this repo depending on it): when $IDLE_MARKER exists on-host, the guard
# treats its mtime as an authoritative "idle since" timestamp and shuts down
# IDLE_MIN minutes after that mtime — REPLACING (not supplementing) its own
# $STAMP-based countdown start for that pass. A daemon that idle-exits cleanly can
# `touch` this file to hand the guard a precise idle-start instead of waiting for
# the guard's own load-average sampling to first read idle. The guard works
# standalone: with no marker file present it falls back to the unchanged
# who/load/$STAMP behavior. The marker path is always embedded (default below,
# overridable via REPO_REMOTE_IDLE_MARKER) so the branch is inert-but-ready.
#
# IDLE_MIN <= 0 means "guard disabled" (repo#163) — the opt-out an operator
# reaches for via REPO_REMOTE_IDLE_SHUTDOWN_MIN=0, e.g. for a fleet-tagged host
# that should never self-shutdown. This must NOT be handled by feeding 0 into
# the generated script's `(NOW - LAST) / 60 -ge IDLE_MIN` arithmetic — that
# makes the guard fire almost immediately (0 >= 0 is true on the very first
# post-$STAMP tick) instead of never. So the window is validated here, before
# any guard/cron script is emitted at all: a non-positive (or non-numeric)
# IDLE_MIN short-circuits to no output, and callers (aws_create, gcp_up) must
# check idle_guard_enabled too so they skip embedding user-data entirely.
idle_guard_enabled() {
  [[ "$IDLE_MIN" =~ ^[0-9]+$ ]] && (( IDLE_MIN > 0 ))
}

idle_guard_userdata() {
  idle_guard_enabled || return 0
  cat <<EOF
#!/bin/bash
# repo-remote idle-shutdown guard (idle window: ${IDLE_MIN} min)
cat >/usr/local/bin/repo-remote-idle-check <<'GUARD'
#!/bin/bash
IDLE_MIN=${IDLE_MIN}
STAMP=/var/run/repo-remote-idle.stamp
# Idle-exit marker: mtime = authoritative "idle since" (e.g. written by
# loom-daemon on clean idle-exit). Overridable via REPO_REMOTE_IDLE_MARKER.
MARKER=${IDLE_MARKER}
# An active SSH session or non-trivial CPU load is real activity: reset the
# idle timer and veto shutdown regardless of any marker (never power off a box
# someone is actively using). No process-name check — see the note in the
# generating script.
if who | grep -q . || [ "\$(awk '{print (\$1 > 0.2)}' /proc/loadavg)" = "1" ]; then
  date +%s > "\$STAMP"; exit 0
fi
# Marker present ⇒ its mtime REPLACES the local \$STAMP countdown start. The
# on-host image is Ubuntu (GNU coreutils), so \`stat -c %Y\` is authoritative;
# the \`|| echo 0\` guards a vanished/unreadable file. A future mtime (clock
# skew) yields a negative age, which is never >= IDLE_MIN, so it can't trigger a
# spurious shutdown.
if [ -f "\$MARKER" ]; then
  MARKER_AGE_MIN=\$(( ( \$(date +%s) - \$(stat -c %Y "\$MARKER" 2>/dev/null || echo 0) ) / 60 ))
  if [ "\$MARKER_AGE_MIN" -ge "\$IDLE_MIN" ]; then
    /sbin/shutdown -h now "repo-remote: daemon idle-exit marker aged \${MARKER_AGE_MIN}m"
  fi
  exit 0
fi
# No marker ⇒ unchanged local stamp-based countdown.
[ -f "\$STAMP" ] || { date +%s > "\$STAMP"; exit 0; }
NOW=\$(date +%s); LAST=\$(cat "\$STAMP")
if [ \$(( (NOW - LAST) / 60 )) -ge "\$IDLE_MIN" ]; then
  /sbin/shutdown -h now "repo-remote: idle for \${IDLE_MIN}m"
fi
GUARD
chmod +x /usr/local/bin/repo-remote-idle-check
echo "* * * * * root /usr/local/bin/repo-remote-idle-check" >/etc/cron.d/repo-remote-idle
EOF
}

# ── host-identity verification (repo#458) ───────────────────────────────────
# See "Host-identity verification" in the header block for the incident and the
# contract. The short version: an SSH alias is a cached IP, an auto-assigned EC2
# public IP is released on stop, and the tooling's other checks (does the alias
# resolve? does ssh connect? does auth succeed?) ALL pass on a stranger's box
# that inherited the address. The host's own instance id is the one thing that
# cannot be inherited with the IP, so it is what gets compared.
#
# Deliberately NOT a heuristic (hostname, uptime, "does our repo exist here") —
# those are guesses that a coincidentally-similar host can satisfy. This asks
# the cloud's own authority for the host's identity and compares exact strings.
REPO_REMOTE_HOST_ID_FILE="${REPO_REMOTE_HOST_ID_FILE:-/etc/repo-remote-instance-id}"
REPO_REMOTE_VERIFY_SSH_TIMEOUT="${REPO_REMOTE_VERIFY_SSH_TIMEOUT:-10}"

# The user-data fragment that records the instance's OWN id on disk at boot.
# The id is NOT interpolated from this script (it isn't known until after
# run-instances returns anyway) — the host reads it from its metadata service,
# so the marker can only ever name the box it is actually sitting on. Best
# effort: if IMDS is unreachable the marker is simply not written and the probe
# below falls back to querying IMDS itself at verify time.
host_identity_userdata() {
  cat <<EOF
# repo-remote host-identity marker (repo#458): lets a later session prove this
# SSH alias still reaches THIS instance and not a stranger's box that inherited
# the public IP released when this one was stopped.
RR_MARKER='${REPO_REMOTE_HOST_ID_FILE}'
RR_TOK="\$(curl -s -m 5 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null || true)"
RR_ID="\$(curl -sf -m 5 -H "X-aws-ec2-metadata-token: \${RR_TOK}" http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || true)"
[ -n "\$RR_ID" ] || RR_ID="\$(curl -sf -m 5 http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || true)"
if [ -n "\$RR_ID" ]; then
  printf '%s\n' "\$RR_ID" >"\$RR_MARKER"
  chmod 644 "\$RR_MARKER"
fi
EOF
}

# The POSIX-sh script executed ON the remote host to report its identity.
# Ordered cheapest-and-most-specific first; exits 1 (printing nothing) when it
# can name no identity at all, which the caller treats as "unverified", never as
# "verified".
host_identity_probe() {
  # Only the marker path is interpolated; everything else is literal.
  cat <<EOF
rr_marker='${REPO_REMOTE_HOST_ID_FILE}'
EOF
  cat <<'EOF'
if [ -r "$rr_marker" ]; then head -n1 "$rr_marker"; exit 0; fi
rr_imds=http://169.254.169.254
rr_tok="$(curl -s -m 5 -X PUT "$rr_imds/latest/api/token" -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null || true)"
if [ -n "$rr_tok" ]; then
  rr_id="$(curl -sf -m 5 -H "X-aws-ec2-metadata-token: $rr_tok" "$rr_imds/latest/meta-data/instance-id" 2>/dev/null || true)"
  if [ -n "$rr_id" ]; then printf '%s\n' "$rr_id"; exit 0; fi
fi
rr_id="$(curl -sf -m 5 "$rr_imds/latest/meta-data/instance-id" 2>/dev/null || true)"
if [ -n "$rr_id" ]; then printf '%s\n' "$rr_id"; exit 0; fi
rr_id="$(curl -sf -m 5 -H 'Metadata-Flavor: Google' "$rr_imds/computeMetadata/v1/instance/name" 2>/dev/null || true)"
if [ -n "$rr_id" ]; then printf '%s\n' "$rr_id"; exit 0; fi
if [ -r /var/lib/cloud/data/instance-id ]; then head -n1 /var/lib/cloud/data/instance-id; exit 0; fi
exit 1
EOF
}

# remote_host_identity <alias> -- sets REMOTE_HOST_IDENTITY to the id the host
# reports and returns 0; returns 1 with it empty when the identity could not be
# established. The ssh stderr is captured into REMOTE_HOST_IDENTITY_ERR so the
# refusal can say WHY rather than just "failed".
#
# Results come back through globals rather than stdout DELIBERATELY: a caller
# writing `id="$(remote_host_identity …)"` would run this in a subshell, and the
# captured stderr — the whole point of REMOTE_HOST_IDENTITY_ERR — would be
# discarded with that subshell. Runs ONE ssh session; nothing here can create,
# start, or otherwise touch a cloud resource.
REMOTE_HOST_IDENTITY=""
REMOTE_HOST_IDENTITY_ERR=""
SSH_MUX_OPTS=()
remote_host_identity() {  # <alias>
  local alias="$1" script out rc errf
  REMOTE_HOST_IDENTITY=""
  REMOTE_HOST_IDENTITY_ERR=""
  script="$(host_identity_probe)"
  errf="$(mktemp)"
  # SSH_MUX_OPTS (repo#565) is set only by `attach`, which runs this probe over
  # the SAME master connection the credential is later delivered on.
  out="$(ssh -o ConnectTimeout="$REPO_REMOTE_VERIFY_SSH_TIMEOUT" -o BatchMode=yes \
             -o StrictHostKeyChecking=accept-new \
             ${SSH_MUX_OPTS[@]+"${SSH_MUX_OPTS[@]}"} "$alias" 'sh -s' <<<"$script" 2>"$errf")"
  rc=$?
  REMOTE_HOST_IDENTITY_ERR="$(cat "$errf" 2>/dev/null)"
  rm -f "$errf"
  # An instance id is a single bare token; normalize away CR/whitespace and any
  # trailing chatter so a cosmetic difference can never read as a mismatch.
  out="$(printf '%s' "$out" | head -n1 | tr -d '[:space:]')"
  [[ $rc -eq 0 && -n "$out" ]] || return 1
  REMOTE_HOST_IDENTITY="$out"
  return 0
}

# verify_host_identity <alias> <expected-id> <expected-source> [strict|advisory]
# Sets HOST_ID_OBSERVED to what the host actually said (empty when unknown).
# A MISMATCH exits 6 in BOTH modes — that is the incident, and --force does not
# override it. The modes differ only on an identity that could not be
# established: strict (the `verify` subcommand) exits 6 too, advisory (the tail
# of `up`) warns and returns 0. See the header block for why.
HOST_ID_OBSERVED=""
verify_host_identity() {
  local alias="$1" expected="$2" src="$3" mode="${4:-strict}" got=""
  HOST_ID_OBSERVED=""

  if remote_host_identity "$alias"; then
    got="$REMOTE_HOST_IDENTITY"
    HOST_ID_OBSERVED="$got"
  else
    local why="${REMOTE_HOST_IDENTITY_ERR:-the host answered but named no identity source (no ${REPO_REMOTE_HOST_ID_FILE} marker and no reachable instance metadata service)}"
    if [[ "$mode" == strict ]]; then
      printf '%s\n' "repo-remote: ERROR: could not establish the identity of the host reachable at ssh alias '${alias}'." >&2
      log "  expected instance: ${expected} (${src})"
      log "  reason: ${why}"
      log "  Failing closed: an unverifiable host is NOT evidence that the alias still points at your instance. An EC2 auto-assigned public IP is released when the instance stops, so a stale alias can resolve to an unrelated AWS customer's box (repo#458)."
      log "  Re-run 'repo-remote up --yes' to re-resolve the public IP and rewrite the alias, then verify again."
      exit 6
    fi
    log "WARNING: could not verify the host identity of ${alias} (expected ${expected}); ${why}"
    log "  The alias was rewritten from this run's freshly resolved address, so it is current as of now — but before trusting a LATER reconnect over it, run: repo-remote verify"
    return 0
  fi

  if [[ "$got" != "$expected" ]]; then
    # Same "repo-remote: ERROR:" shape as die(), but spelled out over several
    # lines because the remediation is the actionable part (cf.
    # fleet_marker_gate above).
    printf '%s\n' "repo-remote: ERROR: HOST IDENTITY MISMATCH for ssh alias '${alias}'." >&2
    log "  expected instance:     ${expected} (${src})"
    log "  host at that alias is: ${got}"
    log "  An auto-assigned EC2 public IP is released when its instance stops and reassigned on the next start — very possibly to another AWS customer's instance. The alias resolving and ssh connecting therefore prove NOTHING about which machine you reached (repo#458)."
    log "  Do NOT read from or write to this alias: work written through it lands on somebody else's host."
    log "  Fix: re-run 'repo-remote up --yes' to re-resolve the public IP and rewrite the alias, then re-run 'repo-remote verify'."
    log "  If ${expected} is not the box you meant, correct REPO_REMOTE_INSTANCE_ID in ${REPO_ENV:-<git-root>/.env or REPO_REMOTE_ENV_FILE}."
    log "  --force does NOT override this check (it is the fleet-marker override only)."
    exit 6
  fi
  return 0
}

# ── AWS provider ────────────────────────────────────────────────────────────
aws_authenticate() {
  aws sts get-caller-identity >/dev/null 2>&1 \
    || die 3 "AWS authentication failed with the resolved credentials (aws sts get-caller-identity). Not falling back to ambient/other credentials."
}

# Expand a leading '~' to $HOME. REPO_REMOTE_SSH_KEY's documented default
# (and any operator override) commonly uses '~/...', but bash only
# tilde-expands a literal token — not a value that has been through a
# variable — so anywhere THIS script opens/stats the file itself (unlike
# write_ssh_alias, which hands the raw string to the SSH config's
# IdentityFile and lets ssh expand it) needs this first.
expand_home() {  # <path>
  local p="$1"
  [[ "$p" == "~"* ]] && p="${HOME}${p#\~}"
  printf '%s' "$p"
}

# Compute the EC2 "imported key pair" fingerprint for a public key file, using
# the same DER-encoded-SubjectPublicKeyInfo basis AWS uses for an imported
# (not AWS-generated) key pair: MD5 for RSA, SHA256/base64 for ED25519
# (repo#177). Echoes empty — never dies — on an unsupported key type or a
# missing ssh-keygen/openssl: the caller treats that as "can't dedupe by
# fingerprint" and falls through to import-key-pair, which is always safe (a
# genuine duplicate --key-name is handled by the caller too).
aws_keypair_fingerprint() {  # <path-to-.pub>
  local pub="$1" type
  command -v ssh-keygen >/dev/null 2>&1 && command -v openssl >/dev/null 2>&1 || return 0
  type="$(awk '{print $1}' "$pub" 2>/dev/null)"
  case "$type" in
    ssh-rsa)
      ssh-keygen -f "$pub" -e -m PKCS8 2>/dev/null \
        | openssl pkey -pubin -outform DER 2>/dev/null \
        | openssl md5 -c 2>/dev/null | awk '{print $NF}'
      ;;
    ssh-ed25519)
      # ssh-keygen cannot -e/PKCS8-export an ED25519 key, so the DER
      # SubjectPublicKeyInfo is built by hand: the fixed 12-byte ASN.1 header
      # for an Ed25519 SPKI (RFC 8410) followed by the raw 32-byte public
      # key, which is always the LAST 32 bytes of the OpenSSH wire-format
      # blob (4-byte-length-prefixed "ssh-ed25519" + 4-byte-length-prefixed
      # key material).
      {
        printf '\x30\x2a\x30\x05\x06\x03\x2b\x65\x70\x03\x21\x00'
        awk '{print $2}' "$pub" | openssl base64 -d -A 2>/dev/null | tail -c 32
      } | openssl dgst -sha256 -binary 2>/dev/null | openssl base64 -A 2>/dev/null
      ;;
    *)
      return 0
      ;;
  esac
}

# Resolve (or import) an EC2 key pair name from the local SSH public key
# derived from REPO_REMOTE_SSH_KEY, so aws_create() ALWAYS has a --key-name to
# pass (repo#177: a launch with no key pair attached is unreachable by
# design — this is the fix for that). Sets RESOLVED_KEY_NAME and
# RESOLVED_PUB_KEY_LINE (globals, so a `die` here propagates instead of being
# swallowed by a `$(...)` subshell, matching aws_resolve_image's pattern).
RESOLVED_KEY_NAME=""
RESOLVED_PUB_KEY_LINE=""
aws_resolve_keypair() {
  local priv pub fp existing kname impf imperr
  priv="$(expand_home "${REPO_REMOTE_SSH_KEY:-~/.ssh/id_ed25519}")"
  pub="${priv}.pub"
  [[ -f "$pub" ]] \
    || die 2 "SSH public key not found at ${pub} (derived from REPO_REMOTE_SSH_KEY=${priv}). Generate one (ssh-keygen) or point REPO_REMOTE_SSH_KEY at an existing key pair before provisioning -- a launch with no key pair attached is unreachable by design."
  RESOLVED_PUB_KEY_LINE="$(head -n1 "$pub")"

  fp="$(aws_keypair_fingerprint "$pub")"
  if [[ -n "$fp" ]]; then
    existing="$(aws ec2 describe-key-pairs \
      --filters "Name=fingerprint,Values=${fp}" \
      --query 'KeyPairs[0].KeyName' --output text 2>/dev/null)"
    if [[ -n "$existing" && "$existing" != "None" ]]; then
      RESOLVED_KEY_NAME="$existing"
      return 0
    fi
  fi

  kname="repo-remote-${NAME}"
  impf="$(mktemp)"
  if aws ec2 import-key-pair --key-name "$kname" \
      --public-key-material "fileb://${pub}" >/dev/null 2>"$impf"; then
    RESOLVED_KEY_NAME="$kname"
  else
    imperr="$(cat "$impf" 2>/dev/null)"
    # A prior run may already have imported this exact name (e.g. the
    # fingerprint lookup above missed it) -- AWS rejects that as a duplicate,
    # which is safe to just reuse rather than treat as a hard failure.
    if printf '%s' "$imperr" | grep -q 'InvalidKeyPair.Duplicate'; then
      RESOLVED_KEY_NAME="$kname"
    else
      rm -f "$impf"
      die 4 "aws ec2 import-key-pair failed for ${pub} (key-name ${kname}): ${imperr:-unknown error}"
    fi
  fi
  rm -f "$impf"
}

# Resolve the AMI into RESOLVED_AMI: an explicit override wins; a GPU host
# defaults to the AWS Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu
# 22.04); otherwise the latest Ubuntu 22.04 LTS. (remote.md "GPU hosts".)
# Sets a global (rather than echoing) so a `die` here propagates to the whole
# process instead of being swallowed by a `$(...)` subshell.
RESOLVED_AMI=""
aws_resolve_image() {
  if [[ -n "$IMAGE" ]]; then RESOLVED_AMI="$IMAGE"; return 0; fi
  local q name
  if [[ "$IS_GPU" == true ]]; then
    name='Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*'
    q="$(aws ec2 describe-images --owners amazon \
      --filters "Name=name,Values=$name" \
      --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null)"
  else
    name='ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*'
    q="$(aws ec2 describe-images --owners 099720109477 \
      --filters "Name=name,Values=$name" \
      --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null)"
  fi
  [[ -n "$q" && "$q" != "None" ]] || die 4 "could not resolve an AMI for ${INSTANCE_TYPE} (GPU=${IS_GPU}). Set REPO_REMOTE_IMAGE to override."
  RESOLVED_AMI="$q"
}

# Describe a single instance's state; echoes state name or "missing".
aws_instance_state() {  # <instance-id>
  local out
  out="$(aws ec2 describe-instances --instance-ids "$1" \
        --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null)" || { echo missing; return; }
  [[ -n "$out" && "$out" != "None" ]] && echo "$out" || echo missing
}

# Find a running|stopped instance previously created for this repo (by tag).
aws_find_tagged() {  # echoes "<id> <state>" or empty
  aws ec2 describe-instances \
    --filters "Name=tag:repo-remote,Values=${NAME}" \
              "Name=instance-state-name,Values=running,stopped,stopping,pending" \
    --query 'Reservations[].Instances[].[InstanceId,State.Name]' --output text 2>/dev/null \
    | grep -v '^None' | head -n1
}

# aws_public_ip: echoes the instance's public IP (or the literal "None" when
# AWS has not assigned one yet) on success. On an API-call failure (an
# unfulfilled spot request, throttling, a transient AWS error, etc.) it
# returns the underlying `aws` exit code and logs the captured stderr instead
# of silently swallowing it -- repo#216: the two failure modes ("instance
# exists but has no public IP yet" vs "the describe-instances call itself
# failed") are NOT the same thing and must not be collapsed into the same
# empty-string return the caller cannot tell apart. Callers that only care
# about "no IP yet" can keep ignoring the exit status (an empty/"None" string
# is still returned in that case); callers that want to distinguish an actual
# API failure should check `$?`.
aws_public_ip() {  # <instance-id>
  local out rc errf
  errf="$(mktemp)"
  out="$(aws ec2 describe-instances --instance-ids "$1" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>"$errf")"
  rc=$?
  if [[ $rc -ne 0 ]]; then
    log "aws ec2 describe-instances (public IP lookup for ${1}) failed: $(cat "$errf" 2>/dev/null)"
  fi
  rm -f "$errf"
  printf '%s' "$out"
  return "$rc"
}

# aws_wait_public_ip: poll aws_public_ip() with a BOUNDED retry budget, echoing
# the resolved IP and returning 0, or echoing nothing and returning 1 when the
# budget is exhausted (repo#451).
#
# Why a poll at all: a stop/start cycle (the idle guard stops the box; the next
# `up` starts it again) assigns a BRAND NEW public IP, and AWS does not always
# have it attached by the time `wait instance-running` returns. The single
# unretried query this replaces could therefore observe "no IP yet" and hand
# write_ssh_alias() nothing to write — which, by design (repo#216), leaves the
# PREVIOUS session's now-wrong HostName in place. Nothing in the old output
# distinguished that silent staleness from a host that has no public IP on
# purpose, so the exhausted-budget case below says so explicitly.
#
# "Not yet" for retry purposes means any of: an empty value, the AWS CLI's
# literal "None" rendering of a null scalar, or an outright API-call failure
# (throttling / a transient error — exactly the case worth retrying).
# aws_public_ip() still surfaces the underlying stderr on every failing attempt,
# so nothing is swallowed; the budget is what keeps this from hanging.
#
# The budget is deliberately small (6 attempts, 2s apart => ~10s worst case) and
# is overridable via REPO_REMOTE_IP_POLL_ATTEMPTS / REPO_REMOTE_IP_POLL_INTERVAL
# so the test suite can drive both the late-IP and exhausted paths without real
# sleeps. Both overrides are validated: a non-numeric (or zero) attempt count
# falls back to the default rather than degenerating into "never poll" or an
# unbounded loop.
aws_wait_public_ip() {  # <instance-id> -> echoes the IP, or returns 1
  local iid="$1" attempts interval i ip rc=0
  attempts="${REPO_REMOTE_IP_POLL_ATTEMPTS:-6}"
  interval="${REPO_REMOTE_IP_POLL_INTERVAL:-2}"
  [[ "$attempts" =~ ^[0-9]+$ ]] && (( attempts >= 1 )) || attempts=6
  [[ "$interval" =~ ^[0-9]+$ ]] || interval=2

  for (( i = 1; i <= attempts; i++ )); do
    ip="$(aws_public_ip "$iid")"; rc=$?
    # Trim whitespace (the CLI appends a newline) and normalize "None" to empty.
    ip="${ip#"${ip%%[![:space:]]*}"}"
    ip="${ip%"${ip##*[![:space:]]}"}"
    [[ "$ip" == "None" ]] && ip=""
    if [[ $rc -eq 0 && -n "$ip" ]]; then
      (( i > 1 )) && log "public IP for ${iid} resolved on attempt ${i}/${attempts}"
      printf '%s' "$ip"
      return 0
    fi
    (( i < attempts )) && sleep "$interval"
  done

  if [[ $rc -ne 0 ]]; then
    # Preserved verbatim from the pre-poll implementation: the API call itself
    # failed and the run continues anyway (the instance is up; the IP may
    # resolve on a later `status`/`up`).
    log "continuing with no public IP for ${iid} (see error above)"
  fi
  # The distinct, actionable warning the generic "@ <no public ip>" result line
  # never gave (repo#451): say that the alias was NOT refreshed, so a silently
  # stale HostName is distinguishable from a host with no public IP by design.
  log "WARNING: no public IP for ${iid} after ${attempts} attempt(s) over ~$(( (attempts - 1) * interval ))s — the SSH alias 'repo-remote-${NAME}' was NOT refreshed. If this host previously had a public IP (e.g. it was just restarted after an idle shutdown), the HostName recorded in ${REPO_REMOTE_SSH_CONFIG:-$HOME/.ssh/config} is now STALE and 'ssh repo-remote-${NAME}' will reach the wrong address — re-run 'repo-remote up --yes' once AWS reports an IP. If this instance has no public IP by design (e.g. a private-subnet host), this is informational."
  return 1
}

# ── AWS: security group resolve-or-create + SSH ingress (repo#176) ─────────
# aws_create() previously only conditionally attached a PRE-EXISTING security
# group via REPO_REMOTE_SECURITY_GROUP; if unset, run-instances fell back to
# the VPC's default security group, which has no SSH ingress rule at all — an
# instance provisioned that way times out on SSH indefinitely (the reported
# incident: describe-security-groups showed an empty ingress set). The
# functions below resolve-or-create a security group (idempotent across `up`
# runs, mirroring aws_find_tagged's tag-based instance reuse), authorize SSH
# ingress into it, and verify the rule actually landed before run-instances is
# ever called.

# Find a security group previously created for this repo (by tag). Echoes the
# group id, or empty when none exists yet.
aws_find_tagged_sg() {
  aws ec2 describe-security-groups \
    --filters "Name=tag:repo-remote,Values=${NAME}" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null \
    | grep -v '^None' || true
}

# Resolve the security group to attach into RESOLVED_SG (global, so a `die`
# here propagates rather than being swallowed by a command-substitution
# subshell): an explicit REPO_REMOTE_SECURITY_GROUP wins outright (unchanged
# prior behavior — the operator already chose one); else reuse a
# previously-tagged SG (idempotent across repeated `up` runs); else create one
# and tag it the same way instances are tagged.
RESOLVED_SG=""
aws_resolve_or_create_sg() {
  local sg="${REPO_REMOTE_SECURITY_GROUP:-}"
  if [[ -n "$sg" ]]; then
    RESOLVED_SG="$sg"
    return 0
  fi

  sg="$(aws_find_tagged_sg)"
  if [[ -n "$sg" ]]; then
    RESOLVED_SG="$sg"
    log "reusing existing security group ${sg} (tagged repo-remote=${NAME})"
    return 0
  fi

  local out rc
  out="$(aws ec2 create-security-group \
    --group-name "repo-remote-${NAME}" \
    --description "repo-remote: SSH access for ${NAME}" \
    --tag-specifications "ResourceType=security-group,Tags=[{Key=repo-remote,Value=${NAME}}]" \
    --query 'GroupId' --output text 2>&1)"; rc=$?
  if [[ $rc -ne 0 || -z "$out" || "$out" == "None" ]]; then
    die 4 "aws ec2 create-security-group failed: ${out:-unknown error}"
  fi
  RESOLVED_SG="$out"
  log "created security group ${RESOLVED_SG} (tagged repo-remote=${NAME})"
}

# Validate an SSH-ingress CIDR before anything is authorized from it
# (repo#487). Exposure decisions must be DELIBERATE: this tooling refuses any
# IPv4 prefix wider than REPO_REMOTE_SSH_MIN_PREFIX (default 32 — a single
# address) and any all-of-IPv6 `::/0`, so a stray `0.0.0.0/0` in a config file
# cannot quietly open tcp/22 to the internet. REPO_REMOTE_ALLOW_WORLD_SSH=1 is
# the explicit, loudly-logged escape hatch for an operator who really does
# want a wider rule (a known ISP block, a bastion-free CI network).
#
# Rejection is exit 2 ("invalid required config") rather than 4: the remedy is
# always a config change, never a retry.
aws_validate_ssh_cidr() {  # <cidr> <source-label>
  local cidr="$1" src="$2" prefix min="${SSH_MIN_PREFIX:-32}"

  [[ "$min" =~ ^[0-9]+$ && "$min" -ge 1 && "$min" -le 32 ]] \
    || die 2 "REPO_REMOTE_SSH_MIN_PREFIX must be an integer from 1 to 32 (got '${min}')"

  if [[ "$cidr" == *:* ]]; then
    # IPv6. Only a /0 ("the whole internet") is judged here; narrower IPv6
    # prefixes are passed through unchanged.
    prefix="${cidr##*/}"
    [[ "$cidr" == */* && "$prefix" =~ ^[0-9]+$ ]] \
      || die 2 "${src} is not a valid CIDR: '${cidr}' (expected <address>/<prefix-length>)"
    [[ "$prefix" -ne 0 ]] && return 0
    if [[ "${ALLOW_WORLD_SSH:-0}" == 1 ]]; then
      log "WARNING: REPO_REMOTE_ALLOW_WORLD_SSH=1 — authorizing SSH ingress from ${cidr}, i.e. ALL of IPv6. Every host on the internet may reach tcp/22 on this instance (key-only auth is the only thing left in front of it)."
      return 0
    fi
    die 2 "refusing to authorize SSH ingress from ${cidr} (${src}): that is all of IPv6. Pin a specific address instead, or set REPO_REMOTE_ALLOW_WORLD_SSH=1 to opt in deliberately."
  fi

  [[ "$cidr" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$ ]] \
    || die 2 "${src} is not a valid IPv4 CIDR: '${cidr}' (expected a.b.c.d/0-32)"
  prefix="${cidr##*/}"
  [[ "$prefix" -ge "$min" ]] && return 0

  if [[ "${ALLOW_WORLD_SSH:-0}" == 1 ]]; then
    local scope=""
    [[ "$cidr" == "0.0.0.0/0" ]] && scope="That is the ENTIRE INTERNET. "
    log "WARNING: REPO_REMOTE_ALLOW_WORLD_SSH=1 — authorizing SSH ingress from ${cidr}, which is WIDER than the /${min} minimum (REPO_REMOTE_SSH_MIN_PREFIX). ${scope}Key-only auth is the only thing left in front of tcp/22 on this instance."
    return 0
  fi
  die 2 "refusing to authorize SSH ingress from ${cidr} (${src}): /${prefix} is wider than the required /${min} minimum (REPO_REMOTE_SSH_MIN_PREFIX). Pin a narrower CIDR, raise REPO_REMOTE_SSH_MIN_PREFIX deliberately, or set REPO_REMOTE_ALLOW_WORLD_SSH=1 to opt in to the wider rule."
}

# Resolve the CIDR to authorize for SSH ingress into RESOLVED_SSH_CIDR. An
# explicit REPO_REMOTE_SSH_CIDR always wins — after validation (see
# aws_validate_ssh_cidr). Otherwise a best-effort current-IP lookup via an
# HTTPS echo service is treated as UNVERIFIED: there is no reliable way for
# this script to confirm the detected address is the one SSH egress will
# actually use — behind an HTTPS proxy it commonly isn't (the reported
# incident: the echo service returned the proxy's address, not the SSH egress
# address, producing a correct-looking /32 that could never match).
#
# When detection itself fails outright this FAILS CLOSED (repo#487): returns 1
# with RESOLVED_SSH_CIDR left empty and lets the caller decide. It must never
# fall back to 0.0.0.0/0 — a single flaky HTTPS call is not consent to open
# tcp/22 to the internet, and a consumer auditing its security groups cannot
# tell an accidental opening from a deliberate one.
RESOLVED_SSH_CIDR=""
aws_resolve_ssh_cidr() {
  RESOLVED_SSH_CIDR=""
  if [[ -n "${SSH_CIDR:-}" ]]; then
    aws_validate_ssh_cidr "$SSH_CIDR" "REPO_REMOTE_SSH_CIDR"
    RESOLVED_SSH_CIDR="$SSH_CIDR"
    log "using REPO_REMOTE_SSH_CIDR override for SSH ingress: ${RESOLVED_SSH_CIDR}"
    return 0
  fi

  local url ip
  url="${REPO_REMOTE_IP_ECHO_URL:-https://checkip.amazonaws.com}"
  ip="$(curl -fsS --max-time 5 "$url" 2>/dev/null | tr -d '[:space:]')"
  if [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    aws_validate_ssh_cidr "${ip}/32" "the detected current IP"
    RESOLVED_SSH_CIDR="${ip}/32"
    log "detected current IP ${ip} via ${url} for SSH ingress (unverified — behind an HTTPS proxy this can be a different address than the one SSH egress actually uses; if SSH cannot connect afterward, set REPO_REMOTE_SSH_CIDR explicitly)"
    return 0
  fi
  return 1
}

# The single message every fail-closed detection failure prints (repo#487), so
# the create and reuse paths cannot drift in what they tell the operator.
aws_die_no_ssh_cidr() {
  die 2 "could not detect the current IP via ${REPO_REMOTE_IP_ECHO_URL:-https://checkip.amazonaws.com}, so there is no address to authorize SSH ingress from. Refusing to fall back to 0.0.0.0/0. Set REPO_REMOTE_SSH_CIDR to the address you connect from (e.g. REPO_REMOTE_SSH_CIDR=203.0.113.7/32) and re-run."
}

# A stable per-CALLER id. The owner marker below includes it so each host (or
# operator) only ever reclaims ITS OWN earlier /32 — never a peer's. Without
# this, two hosts that both `up` the same repo's box share one marker, and the
# second host's run revokes the first's rule and locks it out (rjwalters/loom#11616:
# a builder host's `up` on a shared repo-remote box revoked volt's SSH rule).
# Override with REPO_REMOTE_CALLER; default is the short hostname. Sanitised to
# the Description charset, and `:` (the marker delimiter) is never emitted.
repo_remote_caller() {
  local c="${REPO_REMOTE_CALLER:-$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo host)}"
  printf '%s' "$c" | tr -c 'A-Za-z0-9_.-' '-' | cut -c1-40
}

# Every tcp/22 rule this tooling writes carries a Description marked with this
# prefix (repo#487), so an audit can trace each rule to its owner and so the
# refresh below knows which rules are ITS OWN and may be replaced. The marker is
# per repo-name AND per caller (repo#487, rjwalters/loom#11616), so one host
# never reclaims another host's rule. A rule without this exact marker — a
# peer's, an operator's hand-added rule, or an old pre-caller-marker rule — was
# added by somebody else and is never touched (it is left in place, not revoked).
ssh_rule_marker_prefix() { printf 'repo-remote:%s:%s:' "$NAME" "$CALLER"; }
ssh_rule_description()   { printf 'repo-remote:%s:%s:%s' "$NAME" "$CALLER" "$(date -u +%Y-%m-%d)"; }

# Idempotently authorize tcp/22 from the resolved CIDR, labelled with the
# owner marker. A duplicate rule on a reused security group is success, not an
# error. --ip-permissions (rather than the simpler --protocol/--port/--cidr
# trio) is required because only that form can carry a per-rule Description.
aws_authorize_ssh_ingress() {  # <sg-id> <cidr>
  local sg="$1" cidr="$2" out rc desc
  desc="$(ssh_rule_description)"
  out="$(aws ec2 authorize-security-group-ingress --group-id "$sg" \
    --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=${cidr},Description=${desc}}]" 2>&1)"; rc=$?
  if [[ $rc -ne 0 ]] && ! printf '%s' "$out" | grep -q 'InvalidPermission.Duplicate'; then
    die 4 "aws ec2 authorize-security-group-ingress failed for ${sg} (tcp/22 from ${cidr}): ${out:-unknown error}"
  fi
}

# List the CIDRs of tcp/22 rules on <sg> that THIS tooling wrote (their
# Description starts with the repo-remote:<name>: marker), one per line.
# Rules with any other Description — or none at all — are somebody else's and
# are deliberately excluded.
#
# Two non-obvious details in the JMESPath, both load-bearing:
#   * the `| ` pipe STOPS the `IpRanges[]` projection before the filter. Without
#     it the filter is applied element-wise to each IpRange *object* (which is
#     not a list), so the query silently matches nothing at all.
#   * `Description != null &&` short-circuits before starts_with(). A rule with
#     no Description is extremely common on a real group, and starts_with(null)
#     is a hard JMESPathTypeError that fails the whole describe call.
aws_tool_owned_ssh_cidrs() {  # <sg-id>
  local sg="$1" marker
  marker="$(ssh_rule_marker_prefix)"
  aws ec2 describe-security-groups --group-ids "$sg" \
    --query "SecurityGroups[0].IpPermissions[?ToPort==\`22\`].IpRanges[] | [?Description != null && starts_with(Description, '${marker}')].CidrIp" \
    --output text 2>/dev/null \
    | tr '[:space:]' '\n' | grep -vE '^(None)?$' || true
}

# Replace, don't accumulate (repo#487). Each `up` from a new address used to
# ADD a /32 and never remove the old one, so a group roamed across a few weeks
# of coffee shops ended up admitting every address the operator had ever had.
# Revoke this tooling's OWN earlier tcp/22 rules before authorizing the
# current address; keep <keep-cidr> (the rule we are about to authorize) so a
# repeat run from the same address doesn't flap it.
#
# Best-effort by design: a failed revoke is a loud NOTICE, not a fatal error.
# It leaves a stale-but-narrow rule in place (never a wider one), and an
# operator whose credentials lack RevokeSecurityGroupIngress should still be
# able to provision.
aws_revoke_stale_ssh_ingress() {  # <sg-id> <keep-cidr>
  local sg="$1" keep="$2" cidr out rc
  while read -r cidr; do
    [[ -n "$cidr" && "$cidr" != "$keep" ]] || continue
    out="$(aws ec2 revoke-security-group-ingress --group-id "$sg" \
      --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=${cidr}}]" 2>&1)"; rc=$?
    if [[ $rc -ne 0 ]]; then
      log "NOTICE: could not revoke this tooling's stale SSH ingress rule (tcp/22 from ${cidr}) on ${sg}: ${out:-unknown error}. It is left in place; revoke it by hand with: aws ec2 revoke-security-group-ingress --group-id ${sg} --protocol tcp --port 22 --cidr ${cidr}"
    else
      log "revoked this tooling's stale SSH ingress rule on ${sg} (tcp/22 from ${cidr}) — superseded by ${keep}"
    fi
  done < <(aws_tool_owned_ssh_cidrs "$sg")
}

# Legacy (pre-per-caller) rules carry `repo-remote:<name>:<YYYY-MM-DD>` and no
# longer match any caller's prefix, so they are never auto-revoked (the owner is
# unknown). Surface them in one NOTICE so the leftover exposure is visible.
aws_notice_legacy_ssh_ingress() {  # <sg-id>
  local sg="$1" legacy legacy_len
  # Legacy shape is exactly repo-remote:<name>:<YYYY-MM-DD>. JMESPath has no
  # regex or arithmetic, so pin the total length: a per-caller rule whose
  # caller id starts with "20" is longer and must not match.
  legacy_len=$(( ${#NAME} + 13 + 10 ))
  legacy="$(aws ec2 describe-security-groups --group-ids "$sg" \
    --query "SecurityGroups[0].IpPermissions[?ToPort==\`22\`].IpRanges[] | [?Description != null && starts_with(Description, 'repo-remote:${NAME}:20') && length(Description) == \`${legacy_len}\`].CidrIp" \
    --output text 2>/dev/null | tr '[:space:]' ' ' | sed -E 's/(^| )None( |$)/ /g; s/^ +| +$//g')" || true
  [[ -n "$legacy" ]] || return 0
  log "NOTICE: legacy-format SSH ingress rule(s) on ${sg} (tcp/22 from: ${legacy}) are no longer revoked automatically. If stale, revoke by hand with: aws ec2 revoke-security-group-ingress --group-id ${sg} --protocol tcp --port 22 --cidr <cidr>"
}

# Post-authorize verification — this is what would have caught the reported
# incident in-run: a security group whose ingress set was empty
# ({port: null, cidr: []}), with SSH timing out indefinitely as the only
# symptom. Fail loudly here instead, before any instance is even launched.
aws_has_ssh_ingress() {  # <sg-id> -> 0 when a tcp/22 rule is present
  local sg="$1" out
  out="$(aws ec2 describe-security-groups --group-ids "$sg" \
    --query 'SecurityGroups[0].IpPermissions[?ToPort==`22`]' --output text 2>/dev/null)"
  [[ -n "$out" && "$out" != "None" ]]
}

aws_verify_ssh_ingress() {  # <sg-id>
  local sg="$1"
  aws_has_ssh_ingress "$sg" \
    || die 4 "security group ${sg} has no tcp/22 ingress rule after provisioning — SSH would time out indefinitely. Check REPO_REMOTE_SECURITY_GROUP / REPO_REMOTE_SSH_CIDR, or add the rule manually with: aws ec2 authorize-security-group-ingress --group-id ${sg} --protocol tcp --port 22 --cidr <your-ip>/32"
}

# The whole ingress chain in one place: resolve the group, resolve the CIDR to
# authorize, authorize it, prove it landed. Run on EVERY `up` — both when
# creating (before run-instances, so the money-spending call is never made
# against a group that cannot admit SSH) and when REUSING an existing instance
# (repo#451).
#
# Why reuse needs it too: the authorized CIDR is pinned to whatever the
# operator's IP was at ORIGINAL create time. Laptop IPs move (the reported
# incident: 52.119.115.124 -> 104.7.12.215 between sessions), so restarting an
# instance that was created days ago left ingress pointing at an address that no
# longer reaches it — an SSH timeout whose only fix was revoking/re-authorizing
# the /32 by hand. Re-running the chain on reuse re-authorizes for the CURRENTLY
# detected CIDR, so `up` self-heals instead.
#
# Repeating it is safe: the group is RESOLVED (explicit REPO_REMOTE_SECURITY_GROUP,
# else the repo-remote=<name> tag) rather than created per run, and a duplicate
# tcp/22 rule is treated as success by aws_authorize_ssh_ingress.
#
# --no-create (the reuse path) additionally refuses to CREATE a group when none
# resolves, and logs a notice instead. Creating one there would be worse than
# doing nothing: the new group is not attached to the already-running instance,
# so it would neither restore SSH nor be reachable — it would just leak an
# unused group per repo while *looking* like the ingress had been repaired.
# (repo#176's "no new SG accumulates per invocation" property, asserted by the
# suite, says the same thing.) The same limitation applies whenever a reused
# instance is attached to some OTHER group (created outside this tooling, or
# before repo#176): the group refreshed here is not the one guarding it, and
# that group has to be fixed by hand.
#
# --no-create also never WIDENS an existing rule on a failed IP detection — see
# the inline note below. Refreshing ingress on reuse must not be able to turn a
# working /32 into 0.0.0.0/0 just because an echo service was unreachable. Since
# repo#487 NO path can do that: a failed detection resolves to nothing at all
# and the run fails closed (exit 2) unless there is already a rule to preserve.
#
# The chain also REPLACES rather than accumulates (repo#487): every rule this
# tooling writes is labelled `repo-remote:<name>:<date>`, and the labelled rules
# from earlier runs are revoked before the current address is authorized, so a
# roaming laptop can no longer leave a group admitting every address it ever
# had. Rules without that label belong to somebody else and are never touched.
aws_refresh_ssh_ingress() {  # [--no-create]
  if [[ "${1:-}" == "--no-create" ]]; then
    local sg="${REPO_REMOTE_SECURITY_GROUP:-}"
    [[ -n "$sg" ]] || sg="$(aws_find_tagged_sg)"
    if [[ -z "$sg" ]]; then
      log "NOTICE: SSH ingress was NOT refreshed for this reused instance — no group is pinned via REPO_REMOTE_SECURITY_GROUP and none is tagged repo-remote=${NAME}, so there is no group this tooling owns to re-authorize (creating one would not be attached to an already-running instance). If SSH does not connect, authorize tcp/22 from your current address on the instance's own security group: aws ec2 authorize-security-group-ingress --group-id <its-sg> --protocol tcp --port 22 --cidr <your-ip>/32"
      return 0
    fi
    RESOLVED_SG="$sg"
    # Reuse must never WIDEN exposure, and since repo#487 no path widens on a
    # failed detection at all — detection failure resolves to NOTHING rather
    # than to 0.0.0.0/0. On reuse the group normally already admits SSH from an
    # earlier run, so the right move is to leave that rule exactly as it is and
    # say so; only a group with no tcp/22 rule at all (nothing to preserve, and
    # nothing this run can safely invent) is a hard failure.
    if ! aws_resolve_ssh_cidr; then
      if aws_has_ssh_ingress "$RESOLVED_SG"; then
        log "NOTICE: current-IP detection failed, so SSH ingress on ${RESOLVED_SG} was left EXACTLY as it is rather than changed for this reused instance. If SSH cannot connect, set REPO_REMOTE_SSH_CIDR to the address you are connecting from and re-run."
        return 0
      fi
      aws_die_no_ssh_cidr
    fi
  else
    aws_resolve_or_create_sg                            # sets RESOLVED_SG
    aws_resolve_ssh_cidr || aws_die_no_ssh_cidr         # sets RESOLVED_SSH_CIDR
  fi
  # Replace, don't accumulate: drop this tooling's own earlier /32s first
  # (repo#487), then authorize the address actually in use now.
  aws_revoke_stale_ssh_ingress "$RESOLVED_SG" "$RESOLVED_SSH_CIDR"
  aws_notice_legacy_ssh_ingress "$RESOLVED_SG"
  aws_authorize_ssh_ingress "$RESOLVED_SG" "$RESOLVED_SSH_CIDR"
  aws_verify_ssh_ingress "$RESOLVED_SG"
}

# ── AWS: zero-inbound security group for the ssm transport (repo#564) ──────
# Session Manager needs no inbound port at all, so an ssm launch gets a group
# with an EMPTY inbound permission set. That group is deliberately a different
# tool-owned group from the direct-SSH one: it carries the tag
# repo-remote-ssm=<name> (not repo-remote=<name>), so neither transport's
# lookup can pick up the other's group — a fresh ssm launch must never land in
# the direct-SSH group's open tcp/22 while reporting a zero-ingress box.

# aws_sg_ingress_count <sg-id> -- echoes the number of inbound permission
# entries on the group; returns non-zero (echoing nothing) when the group
# cannot be inspected.
aws_sg_ingress_count() {
  local out rc
  out="$(aws ec2 describe-security-groups --group-ids "$1" \
    --query 'length(SecurityGroups[0].IpPermissions)' --output text 2>/dev/null)"; rc=$?
  out="$(printf '%s' "$out" | tr -d '[:space:]')"
  [[ $rc -eq 0 && "$out" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$out"
}

aws_find_tagged_ssm_sg() {
  aws ec2 describe-security-groups \
    --filters "Name=tag:repo-remote-ssm,Values=${NAME}" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null \
    | grep -v '^None' || true
}

# Refuse (exit 2) an existing group that admits anything inbound. Its rules
# are the operator's (or an earlier run's) and are never deleted here.
aws_require_empty_ingress() {  # <sg-id> <how-it-was-resolved>
  local sg="$1" how="$2" n
  n="$(aws_sg_ingress_count "$sg")" \
    || die 4 "could not inspect the inbound rules of security group ${sg} (${how}); an ssm launch must prove its group admits nothing inbound before run-instances. Check ec2:DescribeSecurityGroups."
  [[ "$n" == 0 ]] && return 0
  die 2 "refusing to launch with REPO_REMOTE_TRANSPORT=ssm into security group ${sg} (${how}): it has ${n} inbound rule(s), and an ssm box is promised a group with NO inbound rule. Its rules were left untouched (this tool never deletes them). Unset REPO_REMOTE_SECURITY_GROUP to get a tool-owned empty group, point it at an empty group, or remove the rules yourself after reviewing them: aws ec2 describe-security-groups --group-ids ${sg}"
}

# Resolve-or-create the zero-inbound group into RESOLVED_SG.
aws_resolve_or_create_ssm_sg() {
  local sg="${REPO_REMOTE_SECURITY_GROUP:-}"
  if [[ -n "$sg" ]]; then
    aws_require_empty_ingress "$sg" "REPO_REMOTE_SECURITY_GROUP"
    RESOLVED_SG="$sg"
    return 0
  fi
  sg="$(aws_find_tagged_ssm_sg)"
  if [[ -n "$sg" ]]; then
    aws_require_empty_ingress "$sg" "tagged repo-remote-ssm=${NAME}"
    RESOLVED_SG="$sg"
    log "reusing existing zero-inbound security group ${sg} (tagged repo-remote-ssm=${NAME})"
    return 0
  fi
  local out rc
  out="$(aws ec2 create-security-group \
    --group-name "repo-remote-ssm-${NAME}" \
    --description "repo-remote: SSM Session Manager only, no inbound, for ${NAME}" \
    --tag-specifications "ResourceType=security-group,Tags=[{Key=repo-remote-ssm,Value=${NAME}}]" \
    --query 'GroupId' --output text 2>&1)"; rc=$?
  if [[ $rc -ne 0 || -z "$out" || "$out" == "None" ]]; then
    die 4 "aws ec2 create-security-group failed: ${out:-unknown error}"
  fi
  RESOLVED_SG="$out"
  log "created zero-inbound security group ${RESOLVED_SG} (tagged repo-remote-ssm=${NAME})"
  # A new group starts with no inbound rule; prove it before spending money.
  aws_require_empty_ingress "$RESOLVED_SG" "just created"
}

# Reused instance under ssm: report, never change. Any inbound rule on the
# instance's own groups predates this run (e.g. a direct-SSH /32 from when the
# box was launched with REPO_REMOTE_TRANSPORT=ssh); switching transport does not
# remove it, and this says so instead of implying otherwise. Also warns when no
# instance profile is attached — the most common reason a reused box never
# registers with SSM — without attaching one. Best effort: an inspection
# failure is a NOTICE, never a failed `up`.
aws_ssm_reuse_notices() {  # <instance-id>
  local iid="$1" out rc sg n
  out="$(aws ec2 describe-instances --instance-ids "$iid" \
    --query 'Reservations[0].Instances[0].IamInstanceProfile.Arn' --output text 2>/dev/null)"; rc=$?
  out="$(printf '%s' "$out" | tr -d '[:space:]')"
  if [[ $rc -ne 0 ]]; then
    log "NOTICE: could not read the instance profile of reused instance ${iid}; if SSM never connects, check it has one with AmazonSSMManagedInstanceCore."
  elif [[ -z "$out" || "$out" == None ]]; then
    log "WARNING: reused instance ${iid} has NO IAM instance profile attached, so its SSM agent most likely cannot register and the ssm transport will time out (TargetNotConnected). This run does not attach one. Attach a profile whose role has AmazonSSMManagedInstanceCore yourself: aws ec2 associate-iam-instance-profile --instance-id ${iid} --iam-instance-profile Name=<profile> --region ${REGION}"
  fi

  out="$(aws ec2 describe-instances --instance-ids "$iid" \
    --query 'Reservations[0].Instances[0].SecurityGroups[].GroupId' --output text 2>/dev/null)"; rc=$?
  if [[ $rc -ne 0 ]]; then
    log "NOTICE: could not list the security groups of reused instance ${iid}, so any existing inbound exposure was not reported."
    return 0
  fi
  for sg in $out; do
    [[ "$sg" == None ]] && continue
    if ! n="$(aws_sg_ingress_count "$sg")"; then
      log "NOTICE: could not inspect the inbound rules of ${sg} (attached to reused instance ${iid})."
      continue
    fi
    if [[ "$n" != 0 ]]; then
      log "NOTICE: security group ${sg} on reused instance ${iid} still has ${n} inbound rule(s) (e.g. a tcp/22 rule from a direct-SSH launch). The ssm transport does not need them, and this run did NOT remove or change them. Review and revoke them yourself if they are no longer wanted: aws ec2 describe-security-groups --group-ids ${sg} --region ${REGION}"
    fi
  done
  return 0
}

# ── reused-instance root-volume advisory (repo#559) ─────────────────────────
# An instance created before repo#559 (or by hand) may still have a gp2 root
# volume. `up` cannot fix that at launch time — the box already exists — so on
# REUSE it looks the actual root volume up and, if it is gp2, prints the
# operator-run conversion command. It NEVER runs modify-volume itself: changing
# a volume is the operator's decision and needs a permission (ec2:ModifyVolume)
# this tooling does not otherwise require.
#
# Strictly best effort and stderr-only: every failure (API error, permission
# denial, no root mapping, empty/"None" answers) ends in a NOTICE and a return
# 0, never a failed `up`, and never a guessed volume id. The root volume is
# found by matching the instance's RootDeviceName against its
# BlockDeviceMappings — never "the first attached volume" — and the EC2 device
# name (/dev/sda1) is used as reported, not the guest's NVMe name.
aws_root_volume_advisory() {  # <instance-id>
  local iid="$1" root_dev mappings vol vtype errf rc err
  local how="to check it yourself: aws ec2 describe-instances --instance-ids ${iid} --region ${REGION} --query 'Reservations[0].Instances[0].[RootDeviceName,BlockDeviceMappings]' (needs ec2:DescribeInstances), then aws ec2 describe-volumes --volume-ids <root-volume-id> --region ${REGION} --query 'Volumes[0].VolumeType' (needs ec2:DescribeVolumes)."
  local unknown="NOTICE: could not determine the root volume type of reused instance ${iid}, so the gp2 -> gp3 check was skipped (this does not affect the run)"

  # Configured gp2 on purpose: an existing gp2 root is what was asked for.
  [[ "$VOLUME_TYPE" == gp3 ]] || return 0

  errf="$(mktemp)"
  root_dev="$(aws ec2 describe-instances --instance-ids "$iid" \
    --query 'Reservations[0].Instances[0].RootDeviceName' --output text 2>"$errf")"; rc=$?
  err="$(head -n1 "$errf" 2>/dev/null)"
  if [[ $rc -ne 0 ]]; then
    rm -f "$errf"
    log "${unknown}: describe-instances failed (exit ${rc}${err:+: ${err}}). ${how}"
    return 0
  fi
  root_dev="$(printf '%s' "$root_dev" | tr -d '[:space:]')"
  if [[ -z "$root_dev" || "$root_dev" == None ]]; then
    rm -f "$errf"
    log "${unknown}: the instance reports no RootDeviceName. ${how}"
    return 0
  fi

  mappings="$(aws ec2 describe-instances --instance-ids "$iid" \
    --query 'Reservations[0].Instances[0].BlockDeviceMappings[].[DeviceName,Ebs.VolumeId]' \
    --output text 2>"$errf")"; rc=$?
  err="$(head -n1 "$errf" 2>/dev/null)"
  if [[ $rc -ne 0 ]]; then
    rm -f "$errf"
    log "${unknown}: describe-instances (block device mappings) failed (exit ${rc}${err:+: ${err}}). ${how}"
    return 0
  fi
  vol="$(printf '%s\n' "$mappings" | awk -v d="$root_dev" '$1 == d { print $2; exit }')"
  if [[ ! "$vol" =~ ^vol-[0-9a-zA-Z]+$ ]]; then
    rm -f "$errf"
    log "${unknown}: no EBS volume is attached at its root device ${root_dev}. ${how}"
    return 0
  fi

  vtype="$(aws ec2 describe-volumes --volume-ids "$vol" \
    --query 'Volumes[0].VolumeType' --output text 2>"$errf")"; rc=$?
  err="$(head -n1 "$errf" 2>/dev/null)"
  rm -f "$errf"
  if [[ $rc -ne 0 ]]; then
    log "${unknown}: describe-volumes on root volume ${vol} failed (exit ${rc}${err:+: ${err}}) — reading a volume's type needs the ec2:DescribeVolumes permission. ${how}"
    return 0
  fi
  vtype="$(printf '%s' "$vtype" | tr -d '[:space:]')"
  if [[ -z "$vtype" || "$vtype" == None ]]; then
    log "${unknown}: describe-volumes returned no type for root volume ${vol}. ${how}"
    return 0
  fi

  [[ "$vtype" == gp2 ]] || return 0

  local cmd="aws ec2 modify-volume --volume-id ${vol} --volume-type gp3 --region ${REGION}"
  if [[ "$VOLUME_IOPS" != "$GP3_MIN_IOPS" || "$VOLUME_THROUGHPUT" != "$GP3_MIN_THROUGHPUT" ]]; then
    cmd+=" --iops ${VOLUME_IOPS} --throughput ${VOLUME_THROUGHPUT}"
  fi
  # One notice, several lines, same "repo-remote:" prefix as log(); the command
  # sits alone on its line so it can be copied verbatim.
  {
    printf '%s\n' "repo-remote: WARNING: root volume ${vol} of reused instance ${iid} (region ${REGION}) is gp2. gp2's baseline is 3 IOPS per GiB (150 IOPS at 50 GiB); sustained builds/tests drain its burst credits and the box then crawls in disk wait. gp3 has a flat 3,000 IOPS / 125 MiB/s baseline with no burst credits. This run did NOT change the volume. To convert it (online, no stop or detach needed on current-generation instances), run:"
    printf '%s\n' "repo-remote:   ${cmd}"
    printf '%s\n' "repo-remote: That call needs the IAM permission ec2:ModifyVolume — request it if your AWS user is denied. A volume can be modified at most 4 times per rolling 24 hours; track progress with: aws ec2 describe-volumes-modifications --volume-ids ${vol} --region ${REGION}"
  } >&2
  return 0
}

# Belt-and-suspenders SSH access (repo#177): append the resolved public key to
# ~ubuntu/.ssh/authorized_keys on every boot (this runs on every boot, same as
# the idle-guard cron install below), so the box stays reachable even if
# key-pair attachment itself ever regresses. grep -qxF guards against a
# duplicate line across repeat boots.
authorized_keys_userdata() {  # <pubkey-line>
  local pubkey="$1"
  [[ -n "$pubkey" ]] || return 0
  cat <<EOF
mkdir -p ~ubuntu/.ssh
touch ~ubuntu/.ssh/authorized_keys
grep -qxF '${pubkey}' ~ubuntu/.ssh/authorized_keys || echo '${pubkey}' >>~ubuntu/.ssh/authorized_keys
chown -R ubuntu:ubuntu ~ubuntu/.ssh
chmod 700 ~ubuntu/.ssh
chmod 600 ~ubuntu/.ssh/authorized_keys
EOF
}

# Build the full AWS EC2 user-data script for a newly created instance:
# ALWAYS injects the resolved SSH public key into authorized_keys
# (unconditional belt-and-suspenders, repo#177) and ALWAYS records the
# host-identity marker (repo#458 — unconditional for the same reason: the check
# that reads it must not depend on optional configuration), then folds in the
# idle-shutdown guard's cron watchdog when idle_guard_enabled (repo#163's
# IDLE_MIN<=0 opt-out still applies to THAT section only).
aws_userdata() {  # <pubkey-line>
  printf '#!/bin/bash\n'
  authorized_keys_userdata "$1"
  host_identity_userdata
  if idle_guard_enabled; then
    # idle_guard_userdata() emits its own leading shebang; strip it since the
    # combined script only needs the ONE shebang emitted above.
    idle_guard_userdata | tail -n +2
  fi
}

# Create a fresh instance into CREATED_ID (global, so a `die` propagates rather
# than being swallowed by a command-substitution subshell). run-instances is
# invoked EXACTLY ONCE — capturing stdout and stderr in one call — because a
# retry-to-read-the-error would risk launching a second billable instance.
CREATED_ID=""
aws_create() {
  local ami key udfile errfile iid rc err attached

  # repo#564: an ssm launch without an instance profile would boot a box whose
  # agent cannot register — unreachable by design. Refuse before ANY mutation
  # (the key-pair import and security-group creation below included).
  if [[ "$TRANSPORT" == ssm && -z "$INSTANCE_PROFILE" ]]; then
    die 2 "REPO_REMOTE_TRANSPORT=ssm needs REPO_REMOTE_INSTANCE_PROFILE for a fresh launch: an instance profile whose role has the AmazonSSMManagedInstanceCore policy, so the SSM agent can register. Nothing was created. Set REPO_REMOTE_INSTANCE_PROFILE=<profile-name> (the caller also needs iam:PassRole on its role), or set REPO_REMOTE_TRANSPORT=ssh."
  fi

  aws_resolve_image; ami="$RESOLVED_AMI"
  aws_resolve_keypair; key="$RESOLVED_KEY_NAME"

  if [[ "$TRANSPORT" == ssm ]]; then
    # repo#564: zero-inbound group; no ingress authorization, no IP lookup.
    aws_resolve_or_create_ssm_sg                        # sets RESOLVED_SG
  else
    # Resolve-or-create the security group and prove it actually allows SSH
    # BEFORE spending money on run-instances (repo#176). The reuse paths in
    # aws_up() run the same chain (repo#451).
    aws_refresh_ssh_ingress                             # sets RESOLVED_SG
  fi

  udfile="$(mktemp)"; aws_userdata "$RESOLVED_PUB_KEY_LINE" >"$udfile"
  errfile="$(mktemp)"

  local -a args=(ec2 run-instances
    --image-id "$ami"
    --instance-type "$INSTANCE_TYPE"
    # Explicit type (repo#559): left unset, EC2 uses the AMI default — gp2.
    --block-device-mappings "$(aws_root_block_device_mapping)"
    --security-group-ids "$RESOLVED_SG"
    --tag-specifications "ResourceType=instance,Tags=[{Key=repo-remote,Value=${NAME}}]"
    # NOTE (repo#177): `--user-data` here takes `file://<path>` at LAUNCH time.
    # A POST-launch update (e.g. a future repair-in-place tool) is a different
    # call — `modify-instance-attribute --user-data Value=<base64>` — NOT
    # `--attribute userData --value fileb://...`, which fails AWS CLI
    # parameter validation.
    --user-data "file://${udfile}"
    # Always pass --key-name — never conditionally. A key-less launch is
    # exactly the `KeyName: None` / unreachable-by-design failure this fix
    # exists to prevent (repo#177); aws_resolve_keypair() above either
    # resolves one or dies loudly, so $key is never empty here.
    --key-name "$key"
    # IMDS hardening (repo#562): pin the instance metadata options at LAUNCH,
    # where they take precedence over AMI and account defaults, so a box never
    # depends on the image for them (Ubuntu 22.04 AMIs default to IMDSv1). The
    # host-side readers in our user-data and identity probe already request a
    # token first; hop limit 1 is enough because nothing here reads IMDS from
    # inside a bridged container. A launch AWS rejects stays a hard failure —
    # never retried with weaker settings.
    --metadata-options "HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled"
    --query 'Instances[0].InstanceId' --output text)
  # repo#564: attach the configured instance profile on either transport. The
  # caller needs iam:PassRole on its role; AWS rejects the launch otherwise,
  # and that rejection is surfaced below like any other.
  [[ -n "$INSTANCE_PROFILE" ]] && args+=(--iam-instance-profile "Name=${INSTANCE_PROFILE}")
  # repo#616: one-time Spot, terminated on interruption AND on an
  # instance-initiated shutdown (the idle guard), so the box is never left
  # stopped. Each structured value is ONE argv element. On-demand (the
  # default) passes neither flag, exactly as before.
  if is_spot; then
    args+=(--instance-market-options "MarketType=spot,SpotOptions={SpotInstanceType=one-time,InstanceInterruptionBehavior=terminate}"
           --instance-initiated-shutdown-behavior terminate)
  fi

  # EXACTLY ONE launch attempt, on every market: no retry, no fallback to
  # on-demand, no other instance type (repo#616 keeps this invariant).
  iid="$(aws "${args[@]}" 2>"$errfile")"; rc=$?
  err="$(cat "$errfile" 2>/dev/null)"
  rm -f "$udfile" "$errfile"

  if [[ $rc -ne 0 || -z "$iid" || "$iid" == "None" ]]; then
    # Surface the quota-exceeded case with the exact remediation (remote.md).
    if is_spot && printf '%s' "$err" | grep -qE 'InsufficientInstanceCapacity|InsufficientCapacity|MaxSpotInstanceCountExceeded|SpotMaxPriceTooLow|VcpuLimitExceeded'; then
      local hint="Spot capacity, price and quota are not guaranteed"
      if printf '%s' "$err" | grep -qE 'MaxSpotInstanceCountExceeded|VcpuLimitExceeded'; then
        if [[ "$IS_GPU" == true ]]; then
          hint="the Spot vCPU quota is exhausted: request more at Service Quotas -> EC2 -> 'All G and VT Spot Instance Requests' (quota code L-3819A6DF)"
        else
          hint="the Spot vCPU quota is exhausted: request more at Service Quotas -> EC2 -> 'All Standard (A, C, D, H, I, M, R, T, Z) Spot Instance Requests' (quota code L-34B43A08)"
        fi
      fi
      die 4 "aws ec2 run-instances rejected the Spot launch of ${INSTANCE_TYPE} (REPO_REMOTE_MARKET=spot): ${err}. ${hint}. Nothing was retried — not as on-demand and not as another instance type. Retry later, pick another REPO_REMOTE_INSTANCE_TYPE or region, or set REPO_REMOTE_MARKET=on-demand if your spending policy allows it."
    fi
    if printf '%s' "$err" | grep -q 'VcpuLimitExceeded'; then
      if [[ "$IS_GPU" == true ]]; then
        die 4 "AWS GPU vCPU quota is 0 by default (VcpuLimitExceeded). Request a limit >= this type's vCPUs at Service Quotas -> EC2 -> quota code L-DB2E81BA, then retry."
      else
        die 4 "AWS standard vCPU quota exceeded (VcpuLimitExceeded). Request a limit >= this type's vCPUs at Service Quotas -> EC2 -> quota code L-1216C47A (Running On-Demand Standard instances), then retry."
      fi
    fi
    if [[ -n "$INSTANCE_PROFILE" ]] && printf '%s' "$err" | grep -qiE 'iam:PassRole|IamInstanceProfile|instance profile'; then
      die 4 "aws ec2 run-instances rejected the instance profile '${INSTANCE_PROFILE}' (REPO_REMOTE_INSTANCE_PROFILE): ${err}. Check that the profile exists in this account and that the caller has iam:PassRole on its role (condition iam:PassedToService = ec2.amazonaws.com). Nothing was launched."
    fi
    die 4 "aws ec2 run-instances failed: ${err:-unknown error}"
  fi

  # Post-create verification (repo#177): confirm the instance actually came up
  # with a key pair attached before reporting success — catches a regression
  # in-run instead of surfacing later as a silent `Permission denied
  # (publickey)`. Deliberately checked here (creation only); a REUSED instance
  # is not re-verified since this run never touched its key-pair attachment.
  attached="$(aws ec2 describe-instances --instance-ids "$iid" \
    --query 'Reservations[0].Instances[0].KeyName' --output text 2>/dev/null)"
  if [[ -z "$attached" || "$attached" == "None" ]]; then
    die 4 "instance ${iid} launched with no KeyName attached (expected '${key}') — refusing to report success on a host that is unreachable by design. It was NOT auto-terminated; clean it up manually: aws ec2 terminate-instances --instance-ids ${iid}"
  fi

  CREATED_ID="$iid"
}

# End-of-run reachability probe (repo#176 AC3): a correctly-authorized
# security group can still leave an unreachable box (a detected-but-wrong
# CIDR, no route, an unexpected image/user, etc.) — this catches that in-run,
# right after the SSH alias is written, instead of it surfacing as a bare
# timeout on the caller's next attempt. AWS-only, mirroring the scope of the
# rest of this fix (GCP already documents OS Login / IAP instead).
#
# Readiness wait (repo#449): a single 10s attempt fired immediately after
# `run-instances` returns is a coin flip — a freshly booted guest routinely
# refuses connections for tens of seconds while cloud-init and sshd come up,
# which made `up` report a *false* provisioning failure on a perfectly good
# instance. The probe therefore retries across a bounded, configurable window,
# using the same deadline/poll-interval shape as acquire_ssh_alias_lock()
# below, and only ever re-runs the `ssh` call: nothing in this function can
# create, start, or otherwise touch the instance, so a readiness retry can
# never relaunch anything.
REPO_REMOTE_SSH_READY_TIMEOUT="${REPO_REMOTE_SSH_READY_TIMEOUT:-120}"
REPO_REMOTE_SSH_READY_POLL_INTERVAL="${REPO_REMOTE_SSH_READY_POLL_INTERVAL:-5}"

# ssh_error_is_boot_in_progress <ssh-stderr> -- true (0) only when the captured
# stderr matches a known "the host is not listening yet" phrasing. Everything
# else -- `Permission denied`, a bad key/user, an unrecognized message, or no
# stderr at all -- is deliberately treated as a hard failure so an
# authentication/configuration error surfaces immediately instead of silently
# burning the whole retry budget. `ssh` exits 255 for both classes, so the
# stderr text is the only available discriminator.
ssh_error_is_boot_in_progress() {  # <ssh-stderr>
  printf '%s' "$1" | grep -Eq \
    'Connection refused|Operation timed out|Connection timed out|No route to host|Connection reset|Connection closed by remote host|Network is unreachable|Host is unreachable'
}

# ssm_error_class <ssh-stderr> (repo#564) -- classify a failed probe over the
# SSM ProxyCommand. The proxy's own error lands on ssh's stderr ahead of ssh's
# generic "Connection closed" line, so the specific causes are matched FIRST:
#   denied      the caller lacks ssm:StartSession (or the document) — hard fail
#   plugin      the local Session Manager plugin is missing — hard fail
#   registering TargetNotConnected: the agent has not registered yet — retry
#   other       anything else — hard fail (never burn the window on it)
ssm_error_class() {  # <ssh-stderr>
  local e="$1"
  if printf '%s' "$e" | grep -Eq 'AccessDenied|not authorized to perform|UnauthorizedOperation'; then
    printf 'denied'
  elif printf '%s' "$e" | grep -Eqi 'SessionManagerPlugin is not found|session-manager-plugin.*not found'; then
    printf 'plugin'
  elif printf '%s' "$e" | grep -Eq 'TargetNotConnected|is not connected'; then
    printf 'registering'
  else
    printf 'other'
  fi
}

# The remote SSM prerequisites, named in every ssm readiness failure so the
# operator gets the whole checklist rather than one guess.
ssm_prereq_hint() {
  printf '%s' "SSM prerequisites: the instance has an instance profile whose role carries AmazonSSMManagedInstanceCore; its SSM agent is running (preinstalled on Ubuntu AMIs) with outbound HTTPS to the regional ssm, ssmmessages and ec2messages endpoints (or VPC endpoints for them); the caller may call ssm:StartSession on the instance and on the AWS-StartSSHSession document; the local session-manager-plugin is installed."
}

aws_check_reachability() {  # <ssh-alias> <target> [ssm]
  local alias="$1" ip="$2" via="${3:-ssh}"
  if [[ -z "$ip" ]]; then
    log "no public IP resolved yet; skipping the end-of-run SSH reachability check"
    return 0
  fi

  local started; started="$(date +%s)"
  local deadline=$(( started + REPO_REMOTE_SSH_READY_TIMEOUT ))
  local attempts=0 err="" cls=""

  if [[ "$via" == ssm ]]; then
    # repo#564: same bounded budget, same probe, through the SSM alias. There
    # is NO fallback to direct SSH on any failure.
    while true; do
      attempts=$(( attempts + 1 ))
      if err="$(ssh -o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$alias" true 2>&1 >/dev/null)"; then
        if (( attempts > 1 )); then
          log "SSH-over-SSM reachability check passed (${alias} -> ${ip}) after ${attempts} attempts / $(( $(date +%s) - started ))s of readiness wait"
        else
          log "SSH-over-SSM reachability check passed (${alias} -> ${ip})"
        fi
        return 0
      fi
      cls="$(ssm_error_class "$err")"
      case "$cls" in
        denied)
          die 4 "SSH-over-SSM failed for ${alias} (${ip}): access denied by AWS — the caller needs ssm:StartSession on this instance and on the AWS-StartSSHSession document. Not retrying and not falling back to direct SSH. Error: ${err}" ;;
        plugin)
          die 4 "SSH-over-SSM failed for ${alias} (${ip}): the AWS CLI could not find the Session Manager plugin. Install session-manager-plugin. Not falling back to direct SSH. Error: ${err}" ;;
        other)
          # A not-listening-yet sshd behind a registered agent looks like the
          # direct-SSH boot case; anything else (Permission denied, a bad key)
          # cannot be fixed by waiting.
          if ! ssh_error_is_boot_in_progress "$err" || printf '%s' "$err" | grep -q 'An error occurred'; then
            die 4 "SSH-over-SSM failed for ${alias} (${ip}) and the failure does not look like an agent that is still registering, so waiting longer will not help: ${err:-(ssh produced no error output)}. Check REPO_REMOTE_SSH_KEY and REPO_REMOTE_SSH_USER. $(ssm_prereq_hint) Not falling back to direct SSH."
          fi
          ;;
      esac
      if [[ $(date +%s) -ge $deadline ]]; then
        die 4 "SSH-over-SSM did not become ready for ${alias} (${ip}) within ${REPO_REMOTE_SSH_READY_TIMEOUT}s (${attempts} attempt(s)); last error: ${err:-(none)}. The instance id was already recorded, so the box is not orphaned. $(ssm_prereq_hint) An agent can take a minute or more to register after boot: raise REPO_REMOTE_SSH_READY_TIMEOUT, or retry: ssh ${alias}. Not falling back to direct SSH."
      fi
      log "SSM target not ready yet on ${alias} (attempt ${attempts}: ${err:-no error output}); retrying in ${REPO_REMOTE_SSH_READY_POLL_INTERVAL}s (up to ${REPO_REMOTE_SSH_READY_TIMEOUT}s total)"
      sleep "$REPO_REMOTE_SSH_READY_POLL_INTERVAL"
    done
  fi

  while true; do
    attempts=$(( attempts + 1 ))
    if err="$(ssh -o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$alias" true 2>&1 >/dev/null)"; then
      if (( attempts > 1 )); then
        log "SSH reachability check passed (${alias}) after ${attempts} attempts / $(( $(date +%s) - started ))s of readiness wait"
      else
        log "SSH reachability check passed (${alias})"
      fi
      return 0
    fi

    if ! ssh_error_is_boot_in_progress "$err"; then
      die 4 "SSH reachability check failed for ${alias} (${ip}) after provisioning. The failure does not look like a host that is still booting, so waiting longer will not help: ${err:-(ssh produced no error output)}. Check the security group ingress rule (REPO_REMOTE_SSH_CIDR), REPO_REMOTE_SSH_KEY, and REPO_REMOTE_SSH_USER, or retry: ssh ${alias}"
    fi

    if [[ $(date +%s) -ge $deadline ]]; then
      die 4 "SSH reachability check failed for ${alias} (${ip}) after provisioning. The instance was created/started and its SSH alias written, but SSH did not respond within ${REPO_REMOTE_SSH_READY_TIMEOUT}s (${attempts} attempt(s)); last error: ${err:-(none)}. Check the security group ingress rule (REPO_REMOTE_SSH_CIDR), REPO_REMOTE_SSH_KEY, and REPO_REMOTE_SSH_USER, raise REPO_REMOTE_SSH_READY_TIMEOUT if this image is simply slow to boot, or retry: ssh ${alias}"
    fi

    log "SSH not ready yet on ${alias} (attempt ${attempts}: ${err:-no error output}); still booting -- retrying in ${REPO_REMOTE_SSH_READY_POLL_INTERVAL}s (up to ${REPO_REMOTE_SSH_READY_TIMEOUT}s total)"
    sleep "$REPO_REMOTE_SSH_READY_POLL_INTERVAL"
  done
}

aws_up() {
  # repo#564: the local SSM prerequisite is checked before any cloud call.
  [[ "$TRANSPORT" == ssm ]] && ssm_preflight
  aws_authenticate
  local iid="" state="" reused=false

  if [[ -n "$INSTANCE_ID" ]]; then
    state="$(aws_instance_state "$INSTANCE_ID")"
    # Fleet-marker guard BEFORE any start/alias: a pinned id can outlive the
    # host's role as an ephemeral dev box (repo#164). Skipped when the pin is
    # already stale (missing) — there is nothing to protect.
    if [[ "$state" != missing ]]; then
      fleet_marker_gate "$INSTANCE_ID" "$(aws_fleet_marker "$INSTANCE_ID")" tag
    fi
    # repo#616: under a Spot configuration a stopped pin is refused (never
    # started), and a pin that Spot interruption or the idle guard has already
    # terminated is simply gone -- launch a fresh one. On-demand is unchanged.
    if is_spot; then
      case "$state" in
        stopped|stopping) spot_refuse_stopped_reuse "$INSTANCE_ID" "$state" "pinned REPO_REMOTE_INSTANCE_ID" ;;
        terminated|shutting-down)
          log "pinned REPO_REMOTE_INSTANCE_ID=$INSTANCE_ID is ${state} (a Spot instance is terminated on interruption and idle shutdown); creating a fresh instance."
          state=spot-gone ;;
      esac
    fi
    case "$state" in
      running)          iid="$INSTANCE_ID"; reused=true ;;
      stopped|stopping) aws ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null 2>&1 \
                          || die 4 "failed to start stopped instance $INSTANCE_ID"
                        iid="$INSTANCE_ID"; reused=true ;;
      missing)          log "pinned REPO_REMOTE_INSTANCE_ID=$INSTANCE_ID no longer exists; creating a fresh instance."
                        INSTANCE_ID="" ;;
      spot-gone)        INSTANCE_ID="" ;;   # repo#616, logged above
      *)                iid="$INSTANCE_ID"; reused=true ;;
    esac
  fi

  if [[ -z "$iid" ]]; then
    local found; found="$(aws_find_tagged)"
    if [[ -n "$found" ]]; then
      iid="$(printf '%s' "$found" | awk '{print $1}')"
      state="$(printf '%s' "$found" | awk '{print $2}')"
      # Same guard on the tag-discovery path — the repo-remote=<name> tag is
      # exactly the stale handle that let dev tooling rediscover a fleet host.
      fleet_marker_gate "$iid" "$(aws_fleet_marker "$iid")" tag
      reused=true
      if [[ "$state" == stopped || "$state" == stopping ]]; then
        is_spot && spot_refuse_stopped_reuse "$iid" "$state" "the instance tagged repo-remote=${NAME},"
        aws ec2 start-instances --instance-ids "$iid" >/dev/null 2>&1 \
          || die 4 "failed to start reused instance $iid"
      fi
    fi
  fi

  if [[ -z "$iid" ]]; then
    aws_create           # sets CREATED_ID or dies (main-shell context)
    iid="$CREATED_ID"
    reused=false
  else
    # REUSE path (already-running pinned id, restarted pinned id, or restarted
    # tag-discovered instance): re-authorize SSH ingress for the CURRENTLY
    # detected CIDR (repo#451). aws_create() already ran this chain for the
    # create path, pre-launch, so this is the reuse half of the same contract
    # -- deliberately placed AFTER the fleet-marker gate above, which must
    # still be able to refuse a run before it touches any cloud resource.
    # --no-create: never conjure a group for a host that is already attached to
    # one (see aws_refresh_ssh_ingress).
    if [[ "$TRANSPORT" == ssm ]]; then
      # repo#564: ssm needs no inbound rule, so the direct-SSH refresh (and its
      # current-IP lookup) is skipped ENTIRELY; existing exposure and a missing
      # instance profile are reported, never changed.
      aws_ssm_reuse_notices "$iid"
    else
      aws_refresh_ssh_ingress --no-create
    fi
    # repo#559: a reused box keeps whatever root volume it was born with; flag
    # a gp2 one (advice only, stderr only, never fatal, never modifies it).
    aws_root_volume_advisory "$iid"
  fi

  aws ec2 wait instance-running --instance-ids "$iid" >/dev/null 2>&1 || true

  if [[ "$TRANSPORT" == ssm ]]; then
    # repo#564: the connection target is the instance ID, not an IP — no public
    # IP is polled, and its absence is not a warning. The id is persisted
    # BEFORE the readiness probe can die, exactly as on the ssh path.
    is_instance_id "$iid" \
      || die 4 "unexpected instance id '${iid}' from AWS; refusing to write it into an SSM ProxyCommand alias"
    writeback_instance_id "$iid"
    local alias
    if ! alias="$(write_ssh_alias "$iid" "$REGION")"; then
      die 4 "SSH alias write for ${alias} was rejected (see error above); the SSH config was left untouched. The instance id ${iid} was recorded. Not falling back to direct SSH."
    fi
    aws_check_reachability "$alias" "$iid" ssm
    verify_host_identity "$alias" "$iid" "this run's resolved instance" advisory
    if [[ -n "$HOST_ID_OBSERVED" ]]; then
      log "host identity verified: ${alias} reaches ${HOST_ID_OBSERVED}"
    fi
    emit_up_result "$iid" "" "$alias" "$reused" "$iid"
    return 0
  fi
  # Bounded poll rather than a single query (repo#451): a just-restarted
  # instance's NEW public IP is not always propagated by the time `wait
  # instance-running` returns, and an empty value here silently leaves the
  # previous session's stale HostName in the SSH config. aws_wait_public_ip()
  # logs the underlying API error (if any) plus an explicit "alias was NOT
  # refreshed" warning when its budget is exhausted; the run still continues
  # -- the instance is up, the IP may resolve on a later `status`/`up`, and
  # write_ssh_alias() independently refuses to write a broken stanza for an
  # empty IP either way (repo#216).
  local ip
  ip="$(aws_wait_public_ip "$iid")" || ip=""

  writeback_instance_id "$iid"
  local alias
  if ! alias="$(write_ssh_alias "$ip")"; then
    log "SSH alias write for ${alias} was rejected (see error above); the SSH config was left untouched -- ssh ${alias} (or git-over-SSH via it) will not work until this is retried"
  fi

  # The readiness wait inside aws_check_reachability applies to EVERY `up`, not
  # just a freshly created instance (repo#449): a stopped -> started instance
  # goes through the exact same boot sequence and refuses connections for the
  # same window, so gating the retry on `reused == false` would leave the
  # identical false failure on the reuse path. `reused` is therefore
  # deliberately not passed down. Note this call is AFTER writeback_instance_id
  # above on purpose -- the instance id must already be persisted to REPO_ENV
  # before the probe can die, so a readiness timeout never orphans the box.
  aws_check_reachability "$alias" "$ip"

  # Host-identity verification (repo#458). Reaching SOMETHING at the alias is
  # not the same as reaching THIS instance: the probe above is satisfied by any
  # host that answers, including a stranger's box that inherited the public IP
  # released when this instance was last stopped. So confirm the box at the
  # other end says it is $iid before `up` reports success.
  #
  # Advisory mode: a MISMATCH still exits 6 (that is the incident — the alias
  # this run just wrote does not reach the instance this run just resolved),
  # but an identity it could not establish is a warning, because `up` rewrote
  # the alias from a freshly-resolved address in this very run. `verify` is the
  # fail-closed half, for the reconnect case where nothing re-derived the IP.
  if [[ -n "$ip" ]]; then
    verify_host_identity "$alias" "$iid" "this run's resolved instance" advisory
    if [[ -n "$HOST_ID_OBSERVED" ]]; then
      log "host identity verified: ${alias} reaches ${HOST_ID_OBSERVED}"
    fi
  else
    log "WARNING: no public IP resolved, so ${alias} was not refreshed and its host identity could not be verified -- whatever HostName it still carries is from a previous session and may now resolve to an unrelated instance. Re-run 'repo-remote up --yes' once the IP is available, then 'repo-remote verify', before using it."
  fi

  emit_up_result "$iid" "$ip" "$alias" "$reused" "$ip"
}

# `verify` (repo#458): resolve the instance id this repo EXPECTS at the alias,
# then prove the host actually reachable there agrees. A pinned
# REPO_REMOTE_INSTANCE_ID needs no cloud call at all, which is the point of
# recommending the pin: verification stays cheap enough to run before every
# session, and the expectation is explicit rather than re-derived from a tag
# that some other host may also be wearing.
#
# aws_expected_identity sets EXPECTED_ID / EXPECTED_SRC (globals, so the die
# propagates); shared by `verify` and `attach` (repo#565), which must agree on
# what "this repo's instance" means.
EXPECTED_ID=""
EXPECTED_SRC=""
aws_expected_identity() {
  EXPECTED_ID=""; EXPECTED_SRC=""
  if [[ -n "$INSTANCE_ID" ]]; then
    EXPECTED_ID="$INSTANCE_ID"
    EXPECTED_SRC="pinned REPO_REMOTE_INSTANCE_ID"
  else
    aws_authenticate
    local found; found="$(aws_find_tagged)"
    if [[ -n "$found" ]]; then
      EXPECTED_ID="$(printf '%s' "$found" | awk '{print $1}')"
      EXPECTED_SRC="discovered via the repo-remote=${NAME} tag"
    fi
  fi
  [[ -n "$EXPECTED_ID" ]] || die 2 "nothing to verify against: REPO_REMOTE_INSTANCE_ID is not set and no instance is tagged repo-remote=${NAME}. Pin REPO_REMOTE_INSTANCE_ID in ${REPO_ENV:-<git-root>/.env or REPO_REMOTE_ENV_FILE} (the recommended configuration for any session that outlives a stop/start), or run 'repo-remote up --yes' to provision one."
}

aws_verify() {
  aws_expected_identity
  local alias="repo-remote-${NAME}"
  verify_host_identity "$alias" "$EXPECTED_ID" "$EXPECTED_SRC" strict
  emit_verify_result "$alias" "$EXPECTED_ID" "$HOST_ID_OBSERVED" "$EXPECTED_SRC"
}

aws_status() {
  aws_authenticate
  local rows
  rows="$(aws ec2 describe-instances \
    --filters "Name=tag:repo-remote,Values=${NAME}" \
    --query 'Reservations[].Instances[].[InstanceId,State.Name,InstanceType,PublicIpAddress,LaunchTime]' \
    --output text 2>/dev/null | grep -v '^None' || true)"
  emit_status_result "$rows"
}

aws_down() {
  # repo#616: a Spot-configured box is terminated, never stopped, so a
  # stop-only teardown would misrepresent the lifecycle. Refuse BEFORE any
  # cloud call (stop-instances included); the dry run and --delete proceed.
  if is_spot && [[ "$YES" == true && "$DELETE" != true ]]; then
    die 2 "REPO_REMOTE_MARKET=spot: 'down --yes' would STOP the instance, but a one-time Spot instance cannot be stopped and later restarted -- this configuration terminates rather than stops (on interruption and on idle shutdown alike), and 'up' never restarts a stopped box. Nothing was stopped. Run 'repo-remote down --yes --delete' to terminate it (its disk goes with it), or set REPO_REMOTE_MARKET=on-demand to manage an on-demand instance."
  fi
  aws_authenticate
  local ids
  if [[ -n "$INSTANCE_ID" ]]; then
    ids="$INSTANCE_ID"
  else
    ids="$(aws ec2 describe-instances \
      --filters "Name=tag:repo-remote,Values=${NAME}" \
                "Name=instance-state-name,Values=running,stopped,stopping,pending" \
      --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null | grep -v '^None' || true)"
  fi
  ids="$(printf '%s' "$ids" | tr '\t' ' ' | xargs 2>/dev/null || true)"

  if [[ -z "$ids" ]]; then
    emit_down_result "" "noop"
    return 0
  fi

  # Fleet-marker guard (repo#164, repo#170) — `down` resolves instances from
  # the SAME never-expiring handles `up` does (a pinned REPO_REMOTE_INSTANCE_ID,
  # or the repo-remote=<name> tag, which can resolve MULTIPLE ids here unlike
  # `up`'s single-id resolution). A dry run touches no cloud resource, so it is
  # only annotated below, never blocked.
  if [[ "$YES" != true ]]; then
    local marked="" id mv
    for id in $ids; do
      mv="$(aws_fleet_marker "$id")"
      fleet_marker_matches "$mv" && marked="${marked:+$marked }$id"
    done
    emit_down_result "$ids" "dry-run" "$marked"
    return 0
  fi

  # About to actually mutate: check EVERY resolved id BEFORE any
  # stop/terminate call is made. fleet_marker_gate is a no-op for an unmarked
  # id; for a marked one it dies (exit 5) unless --force, in which case it
  # warns and returns. Checking the whole list up front — rather than
  # skipping just the marked ids and acting on the rest — means a refusal
  # here leaves every resolved instance untouched (refuse the WHOLE batch,
  # the safer default per repo#170: a partial stop/terminate is a worse
  # operator surprise than an outright refusal).
  local id
  for id in $ids; do
    fleet_marker_gate "$id" "$(aws_fleet_marker "$id")" tag
  done

  if [[ "$DELETE" == true ]]; then
    # shellcheck disable=SC2086
    aws ec2 terminate-instances --instance-ids $ids >/dev/null 2>&1 \
      || die 4 "aws ec2 terminate-instances failed for: $ids"
    remove_ssh_alias "repo-remote-${NAME}"
    emit_down_result "$ids" "terminated"
  else
    # shellcheck disable=SC2086
    aws ec2 stop-instances --instance-ids $ids >/dev/null 2>&1 \
      || die 4 "aws ec2 stop-instances failed for: $ids"
    emit_down_result "$ids" "stopped"
  fi
}

# ── GCP provider ────────────────────────────────────────────────────────────
gcp_authenticate() {
  gcloud auth activate-service-account --key-file="${GOOGLE_APPLICATION_CREDENTIALS}" >/dev/null 2>&1 \
    || die 3 "GCP authentication failed (gcloud auth activate-service-account)."
  gcloud config set project "${GCP_PROJECT}" >/dev/null 2>&1 || true
}

gcp_up() {
  gcp_authenticate
  local vm="repo-remote-${NAME}" ip="" reused=false state
  state="$(gcloud compute instances describe "$vm" --zone "$REGION" \
    --format='value(status)' 2>/dev/null || true)"
  if [[ -n "$state" ]]; then
    # Fleet-marker guard BEFORE any start/alias (repo#164) — the GCP analogue of
    # the AWS reuse check, against instance labels instead of tags.
    fleet_marker_gate "$vm" "$(gcp_fleet_marker "$vm")" label
    reused=true
    if [[ "$state" != "RUNNING" ]]; then
      gcloud compute instances start "$vm" --zone "$REGION" >/dev/null 2>&1 \
        || die 4 "failed to start existing instance $vm"
    fi
  else
    local -a args=(compute instances create "$vm"
      --zone "$REGION"
      --machine-type "$INSTANCE_TYPE"
      --boot-disk-size "${DISK_GB}GB"
      --labels "repo-remote=${NAME}"
      --image-family "${IMAGE:-ubuntu-2204-lts}" --image-project "${REPO_REMOTE_IMAGE_PROJECT:-ubuntu-os-cloud}")
    [[ -n "$GPU_ACCEL" ]] && args+=(--accelerator "type=${GPU_ACCEL%%:*},count=${GPU_ACCEL##*:}" --maintenance-policy TERMINATE)
    local ud; ud="$(mktemp)"; idle_guard_userdata >"$ud"
    # IDLE_MIN<=0 means the guard is disabled (repo#163) — skip embedding the
    # startup-script metadata at all rather than passing an empty/no-op script.
    idle_guard_enabled && args+=(--metadata-from-file "startup-script=${ud}")
    gcloud "${args[@]}" >/dev/null 2>&1 || { rm -f "$ud"; die 4 "gcloud compute instances create failed for $vm"; }
    rm -f "$ud"
  fi
  ip="$(gcloud compute instances describe "$vm" --zone "$REGION" \
    --format='value(networkInterfaces[0].accessConfigs[0].natIP)' 2>/dev/null || true)"

  writeback_instance_id "$vm"
  local alias; alias="$(write_ssh_alias "$ip")"
  emit_up_result "$vm" "$ip" "$alias" "$reused"
}

gcp_status() {
  gcp_authenticate
  local rows
  rows="$(gcloud compute instances list \
    --filter="labels.repo-remote=${NAME}" \
    --format='value(name,status,machineType.basename(),networkInterfaces[0].accessConfigs[0].natIP,creationTimestamp)' 2>/dev/null || true)"
  emit_status_result "$rows"
}

# GCP analogue of aws_verify (repo#458). GCP's identity token is the instance
# NAME (what the metadata server's instance/name key reports), and gcp_up()
# derives that name deterministically as repo-remote-<name> — so, unlike AWS,
# there is nothing to discover and no cloud call is ever needed here.
gcp_expected_identity() {
  if [[ -n "$INSTANCE_ID" ]]; then
    EXPECTED_ID="$INSTANCE_ID"
    EXPECTED_SRC="pinned REPO_REMOTE_INSTANCE_ID"
  else
    EXPECTED_ID="repo-remote-${NAME}"
    EXPECTED_SRC="the instance name derived from this repo"
  fi
}

gcp_verify() {
  gcp_expected_identity
  local alias="repo-remote-${NAME}"
  verify_host_identity "$alias" "$EXPECTED_ID" "$EXPECTED_SRC" strict
  emit_verify_result "$alias" "$EXPECTED_ID" "$HOST_ID_OBSERVED" "$EXPECTED_SRC"
}

gcp_down() {
  gcp_authenticate
  local vm="repo-remote-${NAME}"
  [[ -n "$INSTANCE_ID" ]] && vm="$INSTANCE_ID"
  local exists
  exists="$(gcloud compute instances describe "$vm" --zone "$REGION" --format='value(name)' 2>/dev/null || true)"
  if [[ -z "$exists" ]]; then emit_down_result "" "noop"; return 0; fi

  # Fleet-marker guard (repo#164, repo#170) — GCP analogue of aws_down's guard,
  # against the resolved instance's labels. `down` here only ever resolves a
  # single vm (derived name or pinned id), so no batch semantics are needed —
  # this mirrors gcp_up's single fleet_marker_gate call. A dry run touches no
  # cloud resource, so it is only annotated, never blocked.
  if [[ "$YES" != true ]]; then
    local marked=""
    fleet_marker_matches "$(gcp_fleet_marker "$vm")" && marked="$vm"
    emit_down_result "$vm" "dry-run" "$marked"
    return 0
  fi
  fleet_marker_gate "$vm" "$(gcp_fleet_marker "$vm")" label

  if [[ "$DELETE" == true ]]; then
    gcloud compute instances delete "$vm" --zone "$REGION" --quiet >/dev/null 2>&1 \
      || die 4 "gcloud compute instances delete failed for $vm"
    remove_ssh_alias "repo-remote-${NAME}"
    emit_down_result "$vm" "terminated"
  else
    gcloud compute instances stop "$vm" --zone "$REGION" --quiet >/dev/null 2>&1 \
      || die 4 "gcloud compute instances stop failed for $vm"
    emit_down_result "$vm" "stopped"
  fi
}

# ── write-back + SSH alias ──────────────────────────────────────────────────
# Write the new instance id back to the per-repo config file (REPO_ENV: the
# REPO_REMOTE_ENV_FILE override, else <git-root>/.env -- never the shared file,
# the handle is per-repo), updating in place or appending. remote.md §4.
#
# repo#492: this never CREATES <git-root>/.env. Some repos forbid any in-tree
# `.env` (worktrees get copied around; a gitignored file is one `git add -f`
# from leaking), and GIT_ROOT is the worktree root, so an unconditional append
# littered every worktree. The rule:
#   REPO_REMOTE_NO_WRITEBACK=1       -> write nothing; log the id + pin hint
#   REPO_REMOTE_ENV_FILE set         -> write there (created, with its parent
#                                       dir, if missing)
#   <git-root>/.env already exists   -> write there (unchanged behavior; from
#                                       a linked worktree it warns first,
#                                       repo#538)
#   otherwise                        -> write nothing; log the id + pin hint
# Not writing is safe: the instance also carries the repo-remote=<name> tag, so
# the next `up` still finds it; the pin is the stronger, recommended handle.
writeback_instance_id() {  # <instance-id>
  local id="$1"
  if [[ "${REPO_REMOTE_NO_WRITEBACK:-}" == 1 ]]; then
    log "instance id: ${id} (not written back: REPO_REMOTE_NO_WRITEBACK=1)."
    log "  Pin it yourself with REPO_REMOTE_INSTANCE_ID=${id} in your per-repo config."
    return 0
  fi
  if [[ -z "$REPO_ENV" ]]; then
    log "instance id: ${id} (no git root, so not written back; pin REPO_REMOTE_INSTANCE_ID=${id} yourself)."
    return 0
  fi
  if [[ "$REPO_ENV_OVERRIDDEN" != true && ! -f "$REPO_ENV" ]]; then
    log "instance id: ${id} (not written back: ${REPO_ENV} does not exist, and it is never created automatically)."
    log "  Pin it with REPO_REMOTE_INSTANCE_ID=${id} in ${REPO_ENV}, or set REPO_REMOTE_ENV_FILE to an out-of-tree file to have it written there."
    return 0
  fi
  if [[ ! -f "$REPO_ENV" ]] && ! mkdir -p "$(dirname "$REPO_ENV")" 2>/dev/null; then
    log "WARNING: could not create the directory for ${REPO_ENV}; pin REPO_REMOTE_INSTANCE_ID=${id} yourself."
    return 0
  fi
  # repo#538: the pre-existing in-tree .env branch, from a linked worktree --
  # warn loudly, then write (a repo that deliberately keeps one is not broken).
  if [[ "$REPO_ENV_OVERRIDDEN" != true && "$IN_LINKED_WORKTREE" == true ]]; then
    warn_worktree_env write
  fi
  if [[ -f "$REPO_ENV" ]] && grep -q '^REPO_REMOTE_INSTANCE_ID=' "$REPO_ENV"; then
    local tmp; tmp="$(mktemp)"
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == REPO_REMOTE_INSTANCE_ID=* ]]; then
        printf 'REPO_REMOTE_INSTANCE_ID=%s\n' "$id"
      else
        printf '%s\n' "$line"
      fi
    done <"$REPO_ENV" >"$tmp"
    mv "$tmp" "$REPO_ENV"
  else
    printf 'REPO_REMOTE_INSTANCE_ID=%s\n' "$id" >>"$REPO_ENV"
  fi
  log "wrote REPO_REMOTE_INSTANCE_ID=${id} to ${REPO_ENV}."
}

# ── SSH alias lock (repo#213) ───────────────────────────────────────────────
# write_ssh_alias()'s read-modify-write below (strip any existing "Host
# <alias>" block from $cfg, then append a fresh one) is NOT safe against a
# second, concurrent write_ssh_alias() call -- e.g. two overlapping
# `/repo:remote` launches, or any other writer of the same $cfg. Each
# invocation snapshots the file, appends its own block, and `mv`s its own copy
# over $cfg -- last writer wins, silently dropping the other alias block. This
# `mkdir`-based lock (the same POSIX-atomic primitive
# .loom/scripts/worktree.sh uses for its own concurrency guard -- chosen there
# because `flock` is unavailable on stock macOS, the same platform the
# incident that motivated this fix was observed on) wraps the ENTIRE
# read-modify-write, not just the final `mv`; locking only the `mv` would
# still let two writers race the `awk` read and clobber each other's edits.
REPO_REMOTE_SSH_LOCK_TIMEOUT="${REPO_REMOTE_SSH_LOCK_TIMEOUT:-15}"
REPO_REMOTE_SSH_LOCK_POLL_INTERVAL="${REPO_REMOTE_SSH_LOCK_POLL_INTERVAL:-1}"

# acquire_ssh_alias_lock <cfg> -- atomically creates "<cfg>.lock" (mkdir),
# retrying until REPO_REMOTE_SSH_LOCK_TIMEOUT elapses. A lock left behind by a
# process that no longer exists (stale, e.g. killed mid-write) is cleared once
# and retried. Fails LOUDLY (die, non-zero exit) on timeout rather than
# hanging forever or silently skipping the write.
acquire_ssh_alias_lock() {  # <cfg>
  local cfg="$1" lock="$1.lock"
  local deadline=$(( $(date +%s) + REPO_REMOTE_SSH_LOCK_TIMEOUT ))
  local stale_retry_done=0
  while true; do
    if mkdir "$lock" 2>/dev/null; then
      echo "$$" >"$lock/owner.pid" 2>/dev/null || true
      return 0
    fi

    local owner_pid=""
    [[ -f "$lock/owner.pid" ]] && owner_pid="$(cat "$lock/owner.pid" 2>/dev/null || true)"
    if [[ -n "$owner_pid" ]] && [[ "$stale_retry_done" -eq 0 ]] && ! kill -0 "$owner_pid" 2>/dev/null; then
      rm -rf "$lock" 2>/dev/null || true
      stale_retry_done=1
      continue
    fi

    if [[ $(date +%s) -ge $deadline ]]; then
      die 4 "timed out after ${REPO_REMOTE_SSH_LOCK_TIMEOUT}s waiting for the SSH config lock (${lock}) -- a concurrent repo-remote invocation may be writing ${cfg}; remove the lock dir manually if no such process is actually running"
    fi
    sleep "$REPO_REMOTE_SSH_LOCK_POLL_INTERVAL"
  done
}

release_ssh_alias_lock() {  # <cfg>
  rm -rf "$1.lock" 2>/dev/null || true
}

# Write/refresh the one-word SSH alias so the connection is `ssh repo-remote-<name>`.
# Honors REPO_REMOTE_SSH_CONFIG (default ~/.ssh/config) so tests never touch a
# real config. Echoes the alias name. Returns non-zero (without touching
# $cfg) if the generated stanza fails validation -- see below.
#
# repo#564: with a second argument (an AWS region) the alias is written for the
# ssm transport — <ip> is then an instance ID used as HostName, and a
# ProxyCommand opens the connection through `aws ssm start-session`. Both
# values are allow-list validated first (they reach a shell via ProxyCommand);
# a rejected value returns non-zero WITHOUT touching $cfg. Rewriting an alias
# replaces its whole block, so switching transport in either direction drops
# the previous HostName/ProxyCommand lines and leaves other Host blocks alone.
write_ssh_alias() {  # <ip | instance-id> [ssm-region]
  local ip="$1" ssm_region="${2:-}" alias="repo-remote-${NAME}"
  local cfg="${REPO_REMOTE_SSH_CONFIG:-$HOME/.ssh/config}"
  local key="${REPO_REMOTE_SSH_KEY:-~/.ssh/id_ed25519}"
  local user="${REPO_REMOTE_SSH_USER:-ubuntu}"

  # Treat empty, whitespace-only, and the literal "None" (the AWS CLI's
  # `--output text` rendering of a null scalar -- see aws_public_ip()) as "no
  # IP yet" identically: skip writing a stanza and return the alias
  # unchanged. The bare `-z "$ip"` guard this replaces caught only the empty
  # string, so a whitespace value or the literal "None" could slip through
  # and produce a HostName-less stanza -- which OpenSSH does not skip, it
  # refuses to parse the ENTIRE config file, taking every other Host block
  # (and therefore git-over-SSH) down with it (repo#216).
  local ip_trimmed="$ip"
  ip_trimmed="${ip_trimmed#"${ip_trimmed%%[![:space:]]*}"}"
  ip_trimmed="${ip_trimmed%"${ip_trimmed##*[![:space:]]}"}"
  if [[ -z "$ip_trimmed" || "$ip_trimmed" == "None" ]]; then
    printf '%s' "$alias"
    return 0
  fi
  ip="$ip_trimmed"

  # Config-line hygiene for every transport: none of these may smuggle in a
  # second SSH directive (repo#564).
  if [[ ! "$user" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$ || "$key" == *$'\n'* || "$key" == *$'\r'* \
        || "$ip" =~ [[:space:]] ]]; then
    log "refusing to write SSH alias '${alias}': REPO_REMOTE_SSH_USER, REPO_REMOTE_SSH_KEY or the host value contains characters that are not allowed in a single SSH config line -- leaving ${cfg} untouched"
    printf '%s' "$alias"
    return 1
  fi
  if [[ -n "$ssm_region" ]]; then
    if ! is_instance_id "$ip" || ! is_aws_region "$ssm_region"; then
      log "refusing to write SSM SSH alias '${alias}': instance id '${ip}' or region '${ssm_region}' failed validation -- leaving ${cfg} untouched"
      printf '%s' "$alias"
      return 1
    fi
  fi

  mkdir -p "$(dirname "$cfg")" 2>/dev/null || true
  acquire_ssh_alias_lock "$cfg"

  # mktemp INSIDE $(dirname "$cfg") (never the default $TMPDIR) so the final
  # `mv` below is guaranteed a same-filesystem atomic rename regardless of
  # where $TMPDIR points -- a cross-filesystem mv degrades to
  # copy-then-unlink, which exposes a window where a concurrent reader (e.g.
  # ssh itself) could observe a partial file.
  local tmp; tmp="$(mktemp "$(dirname "$cfg")/.ssh_config.XXXXXX")"
  # Strip any prior block for this alias, then append a fresh one.
  if [[ -f "$cfg" ]]; then
    awk -v a="Host $alias" '
      $0 == a {skip=1; next}
      skip && /^Host / {skip=0}
      skip {next}
      {print}
    ' "$cfg" >"$tmp"
  fi
  {
    [[ -s "$tmp" ]] && printf '\n'
    printf 'Host %s\n' "$alias"
    printf '    HostName %s\n' "$ip"
    printf '    User %s\n' "$user"
    printf '    IdentityFile %s\n' "$key"
    if [[ -n "$ssm_region" ]]; then
      printf '    ProxyCommand aws ssm start-session --region %s --target %%h --document-name AWS-StartSSHSession --parameters portNumber=22\n' "$ssm_region"
    fi
  } >>"$tmp"

  # Validate the WRITTEN temp file is actually parseable before it ever
  # replaces the real config -- `ssh -G` resolves/prints the effective config
  # for the given host without connecting, so this is a pure syntax check
  # (repo#216). A tightened value check above should make this unreachable
  # in practice, but this is the backstop that closes the bug class
  # regardless of which upstream path produced a bad value: a tool that
  # edits ~/.ssh/config must never be able to leave it unparseable.
  if ! ssh -G -F "$tmp" "$alias" >/dev/null 2>&1; then
    log "generated SSH config block for '${alias}' (ip='${ip}') failed to parse -- leaving ${cfg} untouched"
    rm -f "$tmp"
    release_ssh_alias_lock "$cfg"
    printf '%s' "$alias"
    return 1
  fi

  mv "$tmp" "$cfg"
  chmod 600 "$cfg" 2>/dev/null || true
  release_ssh_alias_lock "$cfg"
  printf '%s' "$alias"
}

# ── alias removal + prune (#628) ────────────────────────────────────────────
# ssh_alias_hostname <cfg> <alias> -- the first HostName of the single-pattern
# "Host <alias>" block in <cfg>; empty when there is no such block.
ssh_alias_hostname() {  # <cfg> <alias>
  [[ -f "$1" ]] || return 0
  awk -v a="$2" '
    /^[[:space:]]*(Host|Match)[[:space:]]/ { inb = ($1 == "Host" && NF == 2 && $2 == a); next }
    inb && tolower($1) == "hostname" {print $2; exit}
  ' "$1"
}

# strip_ssh_alias_blocks <cfg> <out> <alias>... -- copy <cfg> to <out> without
# the single-pattern "Host <alias>" blocks named (a block ends at the next Host
# or Match line). Every other byte is preserved. Header lines are matched on
# fields ($1 == "Host", NF == 2), exactly like prune's scanner, so an indented
# or trailing-space header is both reported and actually stripped.
strip_ssh_alias_blocks() {  # <cfg> <out> <alias>...
  local cfg="$1" out="$2"; shift 2
  local list; list="$(printf '%s\n' "$@")"
  awk -v list="$list" '
    BEGIN { n = split(list, l, "\n"); for (i = 1; i <= n; i++) drop[l[i]] = 1 }
    /^[[:space:]]*(Host|Match)[[:space:]]/ { skip = ($1 == "Host" && NF == 2 && ($2 in drop)) }
    skip { next }
    { print }
  ' "$cfg" >"$out"
}

# remove_ssh_alias_blocks <alias>... -- remove the named Host blocks from
# REPO_REMOTE_SSH_CONFIG under the same lock, same-directory temp file, `ssh -G`
# parse check and atomic mv as write_ssh_alias, then `ssh-keygen -R` each
# removed HostName (unless a surviving block still uses it). Only
# `repo-remote-*` aliases are ever accepted. Returns non-zero, leaving the
# config untouched, if the result would not parse.
remove_ssh_alias_blocks() {  # <alias>...
  local cfg="${REPO_REMOTE_SSH_CONFIG:-$HOME/.ssh/config}" a
  for a in "$@"; do
    if [[ "$a" != repo-remote-* || ! "$a" =~ ^[A-Za-z0-9._-]+$ ]]; then
      log "refusing to remove non repo-remote alias '${a}'"
      return 1
    fi
  done
  [[ -f "$cfg" ]] || return 0
  acquire_ssh_alias_lock "$cfg"
  local tmp hosts="" h
  tmp="$(mktemp "$(dirname "$cfg")/.ssh_config.XXXXXX")"
  for a in "$@"; do
    h="$(ssh_alias_hostname "$cfg" "$a")"
    [[ -n "$h" ]] && hosts+="${h}"$'\n'
  done
  strip_ssh_alias_blocks "$cfg" "$tmp" "$@"
  if cmp -s "$cfg" "$tmp"; then
    rm -f "$tmp"; release_ssh_alias_lock "$cfg"; return 0
  fi
  if ! ssh -G -F "$tmp" repo-remote-prune-check >/dev/null 2>&1; then
    log "SSH config would not parse after removing the alias block(s) -- leaving ${cfg} untouched"
    rm -f "$tmp"; release_ssh_alias_lock "$cfg"
    return 1
  fi
  chmod --reference="$cfg" "$tmp" 2>/dev/null || chmod 600 "$tmp" 2>/dev/null || true
  mv "$tmp" "$cfg"
  release_ssh_alias_lock "$cfg"
  while IFS= read -r h; do
    [[ -n "$h" ]] || continue
    # Keep the entry if a surviving block still points at the same host.
    if grep -Eiq "^[[:space:]]*HostName[[:space:]]+${h//./\\.}[[:space:]]*$" "$cfg" 2>/dev/null; then
      continue
    fi
    ssh-keygen -R "$h" >/dev/null 2>&1 || true
  done <<<"$hosts"
  return 0
}

# remove_ssh_alias <alias> -- the post-terminate cleanup used by down --delete.
# The box is already gone, so a failure here is a warning, never an error.
remove_ssh_alias() {  # <alias>
  local cfg="${REPO_REMOTE_SSH_CONFIG:-$HOME/.ssh/config}"
  [[ -f "$cfg" ]] || return 0
  [[ -n "$(awk -v a="$1" '$1 == "Host" && NF == 2 && $2 == a {print "y"; exit}' "$cfg")" ]] || return 0
  if remove_ssh_alias_blocks "$1"; then
    log "removed SSH alias ${1} (and its known_hosts entry) from ${cfg}"
  else
    log "WARNING: could not remove SSH alias ${1} from ${cfg}; run 'repo-remote prune' later"
  fi
}

# prune_live_rows -- one "<id>\t<public-ip|None>\t<repo-remote tag|None>" row per
# instance that still exists at the provider (any non-terminated state).
# Fails (non-zero) on a lookup error or on any row it cannot parse, so prune
# fails CLOSED rather than judging aliases against a partial picture.
prune_live_rows() {
  local out line tab=$'\t' row_re
  row_re="^i-[A-Za-z0-9]+${tab}[^${tab}]+${tab}[^${tab}]+\$"
  if [[ "$PROVIDER" == aws ]]; then
    out="$(aws ec2 describe-instances \
      --filters "Name=tag-key,Values=repo-remote" \
                "Name=instance-state-name,Values=pending,running,stopping,stopped" \
      --query "Reservations[].Instances[].[InstanceId,PublicIpAddress,Tags[?Key=='repo-remote']|[0].Value]" \
      --output text 2>/dev/null)" || return 1
    out="$(printf '%s\n' "$out" | tr -s '[:blank:]' '\t')"
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      [[ "$line" =~ $row_re ]] || return 1
    done <<<"$out"
  else
    out="$(gcloud compute instances list --filter='labels.repo-remote:*' \
      --format='csv[no-heading](name,networkInterfaces[0].accessConfigs[0].natIP,labels.repo-remote)' 2>/dev/null)" \
      || return 1
    local n ip tag
    out="$(while IFS=, read -r n ip tag; do
      [[ -z "$n" ]] && continue
      printf '%s\t%s\t%s\n' "$n" "${ip:-None}" "${tag:-None}"
    done <<<"$out")"
  fi
  printf '%s\n' "$out"
}

# prune_aliases -- `repo-remote prune [--yes] [--json]`. Remove the
# `Host repo-remote-*` blocks in REPO_REMOTE_SSH_CONFIG whose box no longer
# exists at the configured provider/region, plus their known_hosts entries.
# A block is KEPT when its HostName matches a live instance's public IP or id,
# or its alias is repo-remote-<tag> for a live instance's repo-remote tag (a
# stopped box's recorded IP is stale by design), or when it cannot be judged
# (multi-pattern Host line, unusual characters, no HostName). Dry run by default.
prune_aliases() {
  local cfg="${REPO_REMOTE_SSH_CONFIG:-$HOME/.ssh/config}" rows
  if [[ "$PROVIDER" == aws ]]; then aws_authenticate; else gcp_authenticate; fi
  rows="$(prune_live_rows)" \
    || die 4 "could not list the live ${PROVIDER} instances; nothing was removed (prune fails closed). Check credentials/region and retry."
  local -a stale=() stale_hosts=() kept=()
  local alias host id ip tag live
  if [[ -f "$cfg" ]]; then
    while IFS=$'\t' read -r alias host; do
      [[ -n "$alias" ]] || continue
      if [[ -z "$host" || ! "$host" =~ ^[A-Za-z0-9._:-]+$ ]]; then
        kept+=("$alias"); continue
      fi
      live=false
      while IFS=$'\t' read -r id ip tag; do
        [[ -n "$id" ]] || continue
        if [[ "$host" == "$id" || "$host" == "$ip" || "$alias" == "repo-remote-${tag}" || "$alias" == "$id" ]]; then
          live=true; break
        fi
      done <<<"$rows"
      if [[ "$live" == true ]]; then kept+=("$alias"); else stale+=("$alias"); stale_hosts+=("$host"); fi
    done < <(awk '
      /^[[:space:]]*(Host|Match)[[:space:]]/ {
        if (alias != "") print alias "\t" host
        alias = ""; host = ""
        if ($1 == "Host" && NF == 2 && $2 ~ /^repo-remote-[A-Za-z0-9._-]+$/) alias = $2
        next
      }
      alias != "" && host == "" && tolower($1) == "hostname" { host = $2 }
      END { if (alias != "") print alias "\t" host }
    ' "$cfg")
  fi

  local removed=false
  if [[ "$YES" == true && ${#stale[@]} -gt 0 ]]; then
    remove_ssh_alias_blocks "${stale[@]}" || die 4 "failed to remove the stale aliases from ${cfg}"
    removed=true
  fi

  local disp=dry-run
  [[ "$YES" == true ]] && disp=removed
  if [[ "$JSON_OUT" == true ]]; then
    local first=true a
    printf '{"action":"prune","provider":"%s","disposition":"%s","stale":[' "$(json_escape "$PROVIDER")" "$disp"
    for a in ${stale[@]+"${stale[@]}"}; do
      [[ "$first" == true ]] && first=false || printf ','
      printf '"%s"' "$(json_escape "$a")"
    done
    printf '],"kept":%s}\n' "${#kept[@]}"
  elif [[ ${#stale[@]} -eq 0 ]]; then
    log "no stale repo-remote-* aliases in ${cfg}"
  else
    local i
    for i in "${!stale[@]}"; do
      if [[ "$removed" == true ]]; then
        log "removed stale alias ${stale[$i]} (HostName ${stale_hosts[$i]})"
      else
        log "stale alias ${stale[$i]} (HostName ${stale_hosts[$i]}) -- would remove"
      fi
    done
    [[ "$removed" == true ]] || log "DRY RUN -- ${#stale[@]} stale alias(es) in ${cfg} (pass --yes to remove them and their known_hosts entries)"
  fi
}

# ── dev-session GitHub credential + attach (repo#565) ───────────────────────
# See "GitHub credentials on the VM" in the header block for the contract. The
# shape, in one line: verify the host, THEN resolve the credential, THEN hand
# it over as data on the connection that was just verified — never in argv, a
# command string, a log line, a config file, or anything on the VM's disk.
GH_TOKEN_CMD=""        # REPO_REMOTE_GH_TOKEN_CMD — run ONLY by attach
GH_TOKEN_STATIC=""     # REPO_REMOTE_GH_TOKEN, held in an unexported variable
GH_APP_CLIENT_ID=""    # REPO_REMOTE_GH_APP_CLIENT_ID — the GitHub App's public client ID (repo#627)
GH_STATIC_ALLOWED=false  # REPO_REMOTE_ALLOW_STATIC_GH_TOKEN=1 (break-glass opt-out, repo#627)
GH_STATIC_REFUSED=false  # a static token is configured WITHOUT the opt-out: attach refuses (exit 2)
GH_CRED_SOURCE="none"  # device | command | static | none
GH_API_HOST=""         # REPO_REMOTE_GH_API_HOST (optional gateway)
GH_GATEWAY_REPO=""     # <owner>/<repo> for GH_REPO in gateway mode
GH_SESSION_TOKEN=""    # the resolved token; set only inside attach's untraced window
ATTACH_CTL_DIR=""      # private dir holding attach's SSH control socket
# The SSH environment name the token travels under. LC_* because that is what
# a stock Ubuntu sshd accepts (`AcceptEnv LANG LC_*`); the remote bootstrap
# unsets it before starting anything.
RR_TOKEN_ENV=LC_REPO_REMOTE_GH_TOKEN
# GitHub's device-flow endpoints (repo#627). Fixed on purpose: there is no
# override, so a config value can never redirect the flow (and the device
# code) somewhere else. The suite drives them with a mock `curl` on PATH.
GH_DEVICE_CODE_URL="https://github.com/login/device/code"
GH_DEVICE_TOKEN_URL="https://github.com/login/oauth/access_token"
GH_DEVICE_GRANT="urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code"
GH_DEVICE_MAX_WAIT=900   # upper bound on the wait, whatever expires_in says

# Decide the credential SOURCE from config. Runs for every subcommand (it is
# part of resolve_settings) and therefore must never execute or read anything
# beyond the config values. Precedence (repo#627): a non-empty
# REPO_REMOTE_GH_APP_CLIENT_ID (device flow, a per-user token) wins over a
# non-empty REPO_REMOTE_GH_TOKEN_CMD, which wins over the static
# REPO_REMOTE_GH_TOKEN -- and the static token is a source ONLY with
# REPO_REMOTE_ALLOW_STATIC_GH_TOKEN=1; without it, a configured static token
# makes attach refuse (GH_STATIC_REFUSED). The usual layer rule (per-repo file
# over shared file) decides each value first, so a per-repo
# `REPO_REMOTE_GH_TOKEN_CMD=` (empty) switches a shared command off.
resolve_gh_credential_config() {
  local _x=""
  [[ $- == *x* ]] && { _x=1; set +x; }
  GH_TOKEN_CMD="${REPO_REMOTE_GH_TOKEN_CMD:-}"
  GH_TOKEN_STATIC="${REPO_REMOTE_GH_TOKEN:-}"
  GH_APP_CLIENT_ID="${REPO_REMOTE_GH_APP_CLIENT_ID:-}"
  GH_API_HOST="${REPO_REMOTE_GH_API_HOST:-}"
  GH_STATIC_ALLOWED=false
  [[ "${REPO_REMOTE_ALLOW_STATIC_GH_TOKEN:-}" == 1 ]] && GH_STATIC_ALLOWED=true
  GH_STATIC_REFUSED=false
  [[ -n "$GH_TOKEN_STATIC" && "$GH_STATIC_ALLOWED" != true ]] && GH_STATIC_REFUSED=true
  # load_config sources the layers under `set -a`, which EXPORTS every value:
  # left alone, the static token would be inherited by every child process
  # (aws, curl, ssh, the token command itself). Keep both shell-local.
  export -n REPO_REMOTE_GH_TOKEN REPO_REMOTE_GH_TOKEN_CMD 2>/dev/null || true
  if [[ -n "$GH_APP_CLIENT_ID" ]]; then
    GH_CRED_SOURCE="device"
  elif [[ -n "$GH_TOKEN_CMD" ]]; then
    GH_CRED_SOURCE="command"
  elif [[ -n "$GH_TOKEN_STATIC" && "$GH_STATIC_ALLOWED" == true ]]; then
    GH_CRED_SOURCE="static"
  else
    GH_CRED_SOURCE="none"
  fi
  [[ -n "$_x" ]] && set -x
  return 0
}

# validate_gh_credential_config -- attach's pre-connection credential checks
# (repo#627). Nothing has connected and nothing has been run when this fails.
validate_gh_credential_config() {
  local _x=""
  [[ $- == *x* ]] && { _x=1; set +x; }
  if [[ "$GH_STATIC_REFUSED" == true ]]; then
    die 2 "REPO_REMOTE_GH_TOKEN is set, but a static token is no longer accepted: it makes every session act on GitHub as one shared, long-lived identity. Replace it with the per-user device flow -- set REPO_REMOTE_GH_APP_CLIENT_ID to the GitHub App's client ID and remove REPO_REMOTE_GH_TOKEN (a per-repo 'REPO_REMOTE_GH_TOKEN=' clears a shared one). Break-glass only: REPO_REMOTE_ALLOW_STATIC_GH_TOKEN=1 keeps using it. Nothing was connected or sent. See 'GitHub credentials on the VM' in commands/repo/remote.md."
  fi
  if [[ -n "$GH_APP_CLIENT_ID" ]] && ! [[ "$GH_APP_CLIENT_ID" =~ ^[A-Za-z0-9._-]{1,128}$ ]]; then
    die 2 "REPO_REMOTE_GH_APP_CLIENT_ID is not a GitHub App client ID (expected letters, digits, '.', '_' or '-', e.g. Iv23li...). Nothing was connected or sent."
  fi
  if [[ "$GH_CRED_SOURCE" == static ]]; then
    local why
    why="$(gh_token_problem "$GH_TOKEN_STATIC")"
    [[ -z "$why" ]] || die 2 "REPO_REMOTE_GH_TOKEN is not a usable token (${why}); nothing was connected or sent."
  fi
  if [[ "$GH_CRED_SOURCE" == none ]]; then
    die 7 "no GitHub credential is configured, and attach no longer opens an unauthenticated session. Set REPO_REMOTE_GH_APP_CLIENT_ID to the GitHub App's client ID (in remote.env or the per-repo config): each attach then runs GitHub's device flow, you approve it in a browser, and the session acts as YOUR GitHub account for about 8 hours. Nothing was connected or sent. See 'GitHub credentials on the VM' in commands/repo/remote.md."
  fi
  [[ -n "$_x" ]] && set -x
  return 0
}

# gh_token_problem <value> -- prints why <value> is not a usable single token,
# or nothing when it is. NEVER prints the value itself.
gh_token_problem() {
  local v="$1"
  if [[ -z "$v" ]]; then
    printf 'it is empty'
  elif [[ "$v" == *$'\n'* || "$v" == *$'\r'* ]]; then
    printf 'it is more than one line'
  elif ! [[ "$v" =~ ^[[:graph:]]+$ ]]; then
    printf 'it contains whitespace or control characters'
  elif (( ${#v} > 4096 )); then
    printf 'it is longer than 4096 characters'
  fi
}

# A gateway is a bare DNS name with at least one dot: no scheme, port, path,
# user info, or whitespace. Ports are refused because nothing here has
# verified gh's handling of host:port; https on 443 is the tested shape.
is_gateway_hostname() {
  [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$ ]]
}

# origin_github_repo -- echoes <owner>/<repo> of this checkout's github.com
# origin, or returns 1. Used only in gateway mode (GH_REPO needs it).
origin_github_repo() {
  local url path
  url="$(git -C "${GIT_ROOT:-.}" remote get-url origin 2>/dev/null)" || return 1
  case "$url" in
    https://github.com/*)   path="${url#https://github.com/}" ;;
    git@github.com:*)       path="${url#git@github.com:}" ;;
    ssh://git@github.com/*) path="${url#ssh://git@github.com/}" ;;
    *) return 1 ;;
  esac
  path="${path%/}"
  path="${path%.git}"
  [[ "$path" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || return 1
  printf '%s' "$path"
}

# The gateway contract (evidence: gh 2.102.0 with GH_DEBUG=api against an
# unresolvable host — see "GitHub API gateway" in remote.md):
#   * GH_HOST alone does NOT route a github.com clone: gh refuses ("none of the
#     git remotes ... correspond to the GH_HOST environment variable");
#   * GH_REPO=<gw>/<owner>/<repo> routes `gh issue`/`gh pr` to
#     https://<gw>/api/graphql, but `gh api` follows GH_HOST, not GH_REPO —
#     so both are set;
#   * a non-github.com host is sent GH_ENTERPRISE_TOKEN, never GH_TOKEN.
# Anything outside that shape is refused here, before any connection.
validate_gh_gateway_config() {
  [[ -n "$GH_API_HOST" ]] || return 0
  local h="$GH_API_HOST" lower why=""
  lower="$(printf '%s' "$h" | tr '[:upper:]' '[:lower:]')"
  if [[ "$h" == *://* ]]; then
    why="give a bare hostname, not a URL (the scheme is always https; plain http is not supported)"
  elif ! is_gateway_hostname "$h"; then
    why="it is not a bare DNS hostname (no port, path, user, or whitespace; at least one dot)"
  elif [[ "$lower" == github.com || "$lower" == *.github.com ]]; then
    why="github.com is not a gateway; unset REPO_REMOTE_GH_API_HOST to talk to GitHub directly"
  elif ! GH_GATEWAY_REPO="$(origin_github_repo)"; then
    why="gateway mode routes gh with GH_REPO=<gateway>/<owner>/<repo>, so this checkout's origin must be a github.com repository (https://github.com/<owner>/<repo> or git@github.com:<owner>/<repo>)"
  fi
  [[ -z "$why" ]] && return 0
  die 2 "REPO_REMOTE_GH_API_HOST='${h}' is not supported: ${why}. Nothing was connected and no credential was resolved. See 'GitHub API gateway' in commands/repo/remote.md."
}

# ── device flow (repo#627) ──────────────────────────────────────────────────
# JSON is read without jq/python: only flat fields GitHub controls are needed,
# matched with bash's own regex engine (no subprocess ever sees a value). A
# string value holding a quote or a backslash is deliberately NOT matched, so
# it reads as absent and the response is rejected as malformed.
JV=""
json_get_str() {  # <json> <key> -> JV = the string value, or ""
  JV=""
  local re='"'"$2"'"[[:space:]]*:[[:space:]]*"([^"\]*)"'
  [[ "$1" =~ $re ]] && JV="${BASH_REMATCH[1]}"
  # shellcheck disable=SC2050  # deliberate: a fresh match drops the captured value from BASH_REMATCH
  [[ x =~ x ]]
  return 0
}
json_get_num() {  # <json> <key> -> JV = the non-negative integer value, or ""
  JV=""
  local re='"'"$2"'"[[:space:]]*:[[:space:]]*([0-9]{1,9})([^0-9]|$)'
  [[ "$1" =~ $re ]] && JV="${BASH_REMATCH[1]}"
  # shellcheck disable=SC2050  # deliberate, as above
  [[ x =~ x ]]
  return 0
}
json_is_object() { [[ "$1" =~ ^[[:space:]]*\{.*\}[[:space:]]*$ ]]; }
# An OAuth error code is shown to the operator only when it has the shape of
# one; anything else from the response body is never displayed.
gh_device_error_name() {  # <code>
  if [[ "$1" =~ ^[a-z_]{1,64}$ ]]; then printf '%s' "$1"; else printf 'an unrecognized error'; fi
}

# gh_device_flow -- obtain a GitHub App USER access token for the person
# running attach, through GitHub's OAuth device flow, into GH_SESSION_TOKEN.
# Called only from resolve_gh_session_token (untraced window, after the host
# identity is verified). Every failure is exit 7 with nothing sent to the VM
# and no fallback to any other source. Hygiene:
#   * the client ID is public and is the only value in curl's argv;
#   * the device code (a bearer secret for the poll) and the returned token
#     reach curl on STDIN through a pipe from the printf builtin, and are read
#     back with bash regexes -- never argv, never a file, never printed;
#   * response bodies are never displayed (only a validated error code).
gh_device_flow() {
  local body rc dcode ucode uri interval expires elapsed=0 start="$SECONDS" err new tok
  body="$(curl -sS --proto '=https' --max-time 30 -X POST -H 'Accept: application/json' \
            --data "client_id=${GH_APP_CLIENT_ID}" "$GH_DEVICE_CODE_URL" </dev/null 2>/dev/null)"
  rc=$?
  if (( rc != 0 )); then
    die 7 "could not reach GitHub to start the device flow (curl exit ${rc}); no credential was resolved and nothing was sent to the VM."
  fi
  if ! json_is_object "$body"; then
    body=""
    die 7 "GitHub's device-code response was not JSON; check that REPO_REMOTE_GH_APP_CLIENT_ID is the App's client ID and that the App has Device Flow enabled. Nothing was sent to the VM."
  fi
  json_get_str "$body" error; err="$JV"
  if [[ -n "$err" ]]; then
    body=""
    die 7 "GitHub refused to start the device flow ($(gh_device_error_name "$err")); check REPO_REMOTE_GH_APP_CLIENT_ID and that the App has Device Flow enabled in its settings. Nothing was sent to the VM."
  fi
  json_get_str "$body" device_code;      dcode="$JV"
  json_get_str "$body" user_code;        ucode="$JV"
  json_get_str "$body" verification_uri; uri="$JV"
  json_get_num "$body" interval;         interval=$(( 10#${JV:-5} ))
  json_get_num "$body" expires_in;       expires=$(( 10#${JV:-$GH_DEVICE_MAX_WAIT} ))
  body=""
  if ! [[ "$dcode" =~ ^[A-Za-z0-9_-]{1,255}$ && "$ucode" =~ ^[A-Za-z0-9-]{1,32}$ \
          && "$uri" =~ ^https://github\.com/[A-Za-z0-9/._-]*$ ]]; then
    dcode=""
    die 7 "GitHub's device-code response is missing or has a malformed device_code, user_code or verification_uri; nothing was sent to the VM."
  fi
  (( interval < 1 )) && interval=1
  (( interval > 120 )) && interval=120
  (( expires > GH_DEVICE_MAX_WAIT )) && expires=$GH_DEVICE_MAX_WAIT
  log "GitHub sign-in for this session (device flow; the session will act as YOUR GitHub account):"
  log "  1. open   ${uri}"
  log "  2. enter  ${ucode}"
  log "  Waiting up to $(( (expires + 59) / 60 )) min for you to authorize the App (Ctrl-C cancels; the verified SSH connection stays open meanwhile)."
  while :; do
    if (( elapsed + interval > expires || SECONDS - start >= expires )); then
      dcode=""
      die 7 "timed out after about $(( (expires + 59) / 60 )) min waiting for the device flow to be authorized; nothing was sent to the VM. Run attach again for a new code."
    fi
    sleep "$interval"
    elapsed=$(( elapsed + interval ))
    body="$(printf 'client_id=%s&device_code=%s&grant_type=%s' "$GH_APP_CLIENT_ID" "$dcode" "$GH_DEVICE_GRANT" \
              | curl -sS --proto '=https' --max-time 30 -X POST -H 'Accept: application/json' \
                  --data @- "$GH_DEVICE_TOKEN_URL" 2>/dev/null)"
    rc=$?
    if (( rc != 0 )); then
      body=""; dcode=""
      die 7 "lost contact with GitHub while waiting for the device flow (curl exit ${rc}); nothing was sent to the VM. Run attach again."
    fi
    if ! json_is_object "$body"; then
      body=""; dcode=""
      die 7 "GitHub's token response was not JSON; nothing was sent to the VM. Run attach again."
    fi
    json_get_str "$body" access_token; tok="$JV"
    json_get_str "$body" error;        err="$JV"
    json_get_num "$body" interval;     new="${JV:+$(( 10#$JV ))}"
    body=""; JV=""
    [[ -n "$tok" ]] && break
    case "$err" in
      authorization_pending) ;;
      slow_down)
        # GitHub: add 5 seconds to the interval (it also returns the new one).
        interval=$(( interval + 5 ))
        [[ -n "$new" ]] && (( new > interval && new <= 120 )) && interval="$new"
        ;;
      expired_token)
        dcode=""
        die 7 "the device code expired before it was authorized; nothing was sent to the VM. Run attach again for a new code." ;;
      access_denied)
        dcode=""
        die 7 "the device-flow authorization was declined at GitHub; no credential was resolved and nothing was sent to the VM." ;;
      "")
        dcode=""
        die 7 "GitHub's token response held neither an access token nor an error; nothing was sent to the VM." ;;
      *)
        dcode=""
        die 7 "GitHub ended the device flow with $(gh_device_error_name "$err"); nothing was sent to the VM. Run attach again." ;;
    esac
  done
  dcode=""
  local why
  why="$(gh_token_problem "$tok")"
  if [[ -n "$why" ]]; then
    tok=""
    die 7 "the device flow returned an unusable token (${why}); nothing was sent to the VM."
  fi
  GH_SESSION_TOKEN="$tok"
  tok=""
  log "GitHub credential: a per-user GitHub App token from the device flow (it expires after about 8 hours; run attach again to get a new one)."
}

# Resolve the session credential into GH_SESSION_TOKEN. Called ONLY by
# attach_session, after the host identity is verified, with xtrace disabled.
# Fails closed: a failed device flow or a failed/malformed command result is
# exit 7 and NEVER falls back to another source. The command's stdout/stderr
# are never displayed. `none` never reaches here (attach refused it already).
resolve_gh_session_token() {
  GH_SESSION_TOKEN=""
  local out="" rc why
  if [[ -n "$GH_TOKEN_STATIC" && "$GH_CRED_SOURCE" != static ]]; then
    log "NOTICE: the static REPO_REMOTE_GH_TOKEN is ignored because a higher-precedence source is configured (it is never used as a fallback)."
  fi
  case "$GH_CRED_SOURCE" in
    device)
      if [[ -n "$GH_TOKEN_CMD" ]]; then
        log "NOTICE: REPO_REMOTE_GH_APP_CLIENT_ID is set, so REPO_REMOTE_GH_TOKEN_CMD is not run (the device flow takes precedence)."
      fi
      gh_device_flow
      ;;
    command)
      out="$("${BASH:-bash}" -c "$GH_TOKEN_CMD" </dev/null 2>/dev/null)"
      rc=$?
      if (( rc != 0 )); then
        out=""
        die 7 "REPO_REMOTE_GH_TOKEN_CMD exited ${rc}: no credential was resolved, nothing was sent to the VM, and no other source was used instead. The command's output is never displayed (it may carry credential material); run it yourself to debug."
      fi
      why="$(gh_token_problem "$out")"
      if [[ -n "$why" ]]; then
        out=""
        die 7 "REPO_REMOTE_GH_TOKEN_CMD succeeded but its output is not a usable token (${why}); it must print exactly one line holding the token. Nothing was sent to the VM, and no other source was used instead."
      fi
      GH_SESSION_TOKEN="$out"
      out=""
      log "GitHub credential: minted for this session by REPO_REMOTE_GH_TOKEN_CMD."
      ;;
    static)
      GH_SESSION_TOKEN="$GH_TOKEN_STATIC"
      log "WARNING: ========================================================================"
      log "WARNING: this session uses the static REPO_REMOTE_GH_TOKEN (REPO_REMOTE_ALLOW_STATIC_GH_TOKEN=1)."
      log "WARNING: everything it does on GitHub runs as ONE shared, long-lived identity -- the"
      log "WARNING: token's owner -- not as you. Set REPO_REMOTE_GH_APP_CLIENT_ID (per-user device"
      log "WARNING: flow) and remove the static token; see 'GitHub credentials on the VM' in remote.md."
      log "WARNING: ========================================================================"
      ;;
    *)
      die 7 "no GitHub credential is configured; nothing was sent to the VM."
      ;;
  esac
}

# sq <string> -- <string> as one POSIX single-quoted word.
sq() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# attach_bootstrap <with-credential:0|1> -- the POSIX sh program run on the VM
# for one attachment. It carries NO secret: the token arrives separately, as
# the ${RR_TOKEN_ENV} SSH environment value, and is read here by name.
attach_bootstrap() {
  local cred="$1" tty=1
  [[ "$ATTACH_HAS_COMMAND" == true ]] && tty=0
  printf '%s\n' "# repo-remote attach bootstrap (repo#565): generated; carries no secret."
  printf 'rr_name=%s\n'    "$(sq "$NAME")"
  printf 'rr_dir=%s\n'     "$(sq "${REPO_DIR:-$NAME}")"
  printf 'rr_mode=%s\n'    "$(sq "$ATTACH_MODE")"
  printf 'rr_tty=%s\n'     "$tty"
  printf 'rr_cmd=%s\n'     "$(sq "$ATTACH_COMMAND")"
  printf 'rr_cred=%s\n'    "$cred"
  printf 'rr_gw=%s\n'      "$(sq "$GH_API_HOST")"
  printf 'rr_gw_repo=%s\n' "$(sq "$GH_GATEWAY_REPO")"
  cat <<'EOF'
rr_t="${LC_REPO_REMOTE_GH_TOKEN-}"
unset LC_REPO_REMOTE_GH_TOKEN
rr_envs=""
if [ "$rr_cred" = 1 ]; then
  if [ -z "$rr_t" ]; then
    echo "repo-remote: ERROR: the session credential did not arrive on the VM. Its SSH server must accept LC_* environment values ('AcceptEnv LANG LC_*', the Ubuntu default). Not opening a session without it." >&2
    exit 97
  fi
  if [ -n "$rr_gw" ]; then
    # Gateway mode: gh sends GH_ENTERPRISE_TOKEN to a non-github.com host and
    # needs GH_HOST (gh api) plus GH_REPO (gh issue/pr) to route there. GH_TOKEN
    # is emptied so nothing reaches github.com's API directly.
    GH_ENTERPRISE_TOKEN="$rr_t"; GH_HOST="$rr_gw"; GH_REPO="$rr_gw/$rr_gw_repo"; GH_TOKEN=
    export GH_ENTERPRISE_TOKEN GH_HOST GH_REPO GH_TOKEN
    rr_var=GH_ENTERPRISE_TOKEN
    rr_envs="GH_ENTERPRISE_TOKEN GH_HOST GH_REPO"
  else
    GH_TOKEN="$rr_t"; export GH_TOKEN
    rr_var=GH_TOKEN
    rr_envs="GH_TOKEN"
  fi
  # git keeps its own remote. An EMPTY helper value clears every credential
  # helper configured so far for github.com (so a `store` helper can never
  # write the token to disk); the one added after it reads the token from the
  # environment by NAME when git asks, and ignores store/erase.
  GIT_CONFIG_COUNT=2
  GIT_CONFIG_KEY_0=credential.https://github.com.helper
  GIT_CONFIG_VALUE_0=
  GIT_CONFIG_KEY_1=credential.https://github.com.helper
  GIT_CONFIG_VALUE_1="!f() { test \"\$1\" = get || return 0; echo username=x-access-token; echo \"password=\$$rr_var\"; }; f"
  export GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0 GIT_CONFIG_KEY_1 GIT_CONFIG_VALUE_1
  rr_envs="$rr_envs GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_KEY_1 GIT_CONFIG_VALUE_1"
fi
unset rr_t
rr_ctr="repo-remote-$rr_name"
rr_use_ctr=0
if [ "$rr_mode" != host ] && command -v docker >/dev/null 2>&1 \
   && [ "$(docker inspect -f '{{.State.Running}}' "$rr_ctr" 2>/dev/null)" = true ]; then
  rr_use_ctr=1
fi
if [ "$rr_mode" = container ] && [ "$rr_use_ctr" != 1 ]; then
  echo "repo-remote: ERROR: --container was given, but the dev container '$rr_ctr' is not running on this host." >&2
  exit 98
fi
# repo#627: say which GitHub account this session acts as, asked from where the
# session will run (the container, else the host). Best effort: the token was
# already checked when it was issued, so a failed lookup warns and the session
# still opens. Only a value shaped like a GitHub login is ever printed.
if [ "$rr_cred" = 1 ]; then
  rr_who=""
  if [ "$rr_use_ctr" = 1 ]; then
    set -- docker exec -i
    for rr_v in $rr_envs; do set -- "$@" -e "$rr_v"; done
    if [ -n "$rr_gw" ]; then set -- "$@" -e GH_TOKEN=; fi
    rr_who="$("$@" "$rr_ctr" gh api user --jq .login </dev/null 2>/dev/null)"
  elif command -v gh >/dev/null 2>&1; then
    rr_who="$(gh api user --jq .login </dev/null 2>/dev/null)"
  fi
  case "$rr_who" in
    ''|*[!A-Za-z0-9-]*)
      echo "repo-remote: WARNING: could not confirm which GitHub account this session acts as ('gh api user' failed, or gh is not installed where the session runs; an App installation token has no user, so this is expected with one). Opening the session anyway." >&2 ;;
    *)
      echo "repo-remote: acting as GitHub user $rr_who" >&2 ;;
  esac
  unset rr_who
fi
if [ "$rr_use_ctr" = 1 ]; then
  if docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$rr_ctr" 2>/dev/null | grep -q '^GH_TOKEN='; then
    echo "repo-remote: WARNING: container '$rr_ctr' was created with GH_TOKEN in its configuration, which Docker stores on the VM's disk. This session overrides it; recreate the container without '-e GH_TOKEN' to remove the stored copy." >&2
  fi
  # `-e NAME` passes the value from this process's environment: the token is
  # never in docker's argv, and exec environment is not part of the
  # container's persisted configuration.
  set -- docker exec
  if [ "$rr_tty" = 1 ]; then set -- "$@" -it; else set -- "$@" -i; fi
  set -- "$@" -w /work
  for rr_v in $rr_envs; do set -- "$@" -e "$rr_v"; done
  if [ "$rr_cred" = 1 ]; then
    set -- "$@" -e GIT_CONFIG_VALUE_0=
    if [ -n "$rr_gw" ]; then set -- "$@" -e GH_TOKEN=; fi
  fi
  set -- "$@" "$rr_ctr"
  if [ "$rr_tty" = 1 ]; then exec "$@" bash -l; fi
  exec "$@" bash -lc "$rr_cmd"
fi
cd "$HOME/$rr_dir" 2>/dev/null || cd "$HOME" || exit 98
if [ "$rr_tty" = 1 ]; then exec "${SHELL:-/bin/sh}" -l; fi
exec "${SHELL:-/bin/sh}" -lc "$rr_cmd"
EOF
}

attach_cleanup() {
  [[ -n "$ATTACH_CTL_DIR" ]] || return 0
  ssh -o ControlPath="$ATTACH_CTL_DIR/cm" -O exit "repo-remote-${NAME}" >/dev/null 2>&1 || true
  rm -rf "$ATTACH_CTL_DIR" 2>/dev/null || true
  ATTACH_CTL_DIR=""
}

# `attach` (repo#565). Returns the remote session's exit status.
attach_session() {
  local alias="repo-remote-${NAME}"

  # 1. Config only — nothing has connected and nothing has been run yet.
  [[ "$NAME" =~ ^[A-Za-z0-9._-]+$ ]] \
    || die 2 "the repo name '${NAME}' contains characters attach will not put into a remote command (allowed: letters, digits, . _ -)"
  validate_transport_config
  [[ "$PROVIDER" == aws && "$TRANSPORT" == ssm ]] && ssm_preflight
  validate_gh_credential_config
  validate_gh_gateway_config
  case "$PROVIDER" in
    aws) aws_expected_identity ;;
    gcp) gcp_expected_identity ;;
  esac

  # 2. One master connection; the identity probe and the session both ride it,
  #    so the credential goes to exactly the host that was verified.
  local base="${REPO_REMOTE_ATTACH_CTL_BASE:-/tmp}"
  ATTACH_CTL_DIR="$(mktemp -d "${base%/}/rr-attach.XXXXXX" 2>/dev/null)" \
    || die 4 "could not create a private SSH control directory under ${base} (set REPO_REMOTE_ATTACH_CTL_BASE)"
  chmod 700 "$ATTACH_CTL_DIR"
  trap attach_cleanup EXIT
  trap 'exit 130' INT TERM HUP
  local ctl="$ATTACH_CTL_DIR/cm" errf merr
  errf="$(mktemp)"
  # The master must itself carry the SendEnv pattern: a session multiplexed
  # over a master without it arrives with the variable EMPTY (observed with
  # OpenSSH 10.3). Its own environment never holds the token.
  if ! env -u "$RR_TOKEN_ENV" ssh -o ControlMaster=yes -o ControlPath="$ctl" -o ControlPersist=yes \
        -o ConnectTimeout="$REPO_REMOTE_VERIFY_SSH_TIMEOUT" -o BatchMode=yes \
        -o StrictHostKeyChecking=accept-new -o SendEnv="$RR_TOKEN_ENV" \
        -f -N "$alias" </dev/null 2>"$errf"; then
    merr="$(cat "$errf" 2>/dev/null)"; rm -f "$errf"
    printf '%s\n' "repo-remote: ERROR: could not open an SSH connection over alias '${alias}' to verify the host." >&2
    log "  expected instance: ${EXPECTED_ID} (${EXPECTED_SRC})"
    log "  reason: ${merr:-(ssh produced no error output)}"
    log "  Failing closed: the host identity could not be established, so no credential was resolved or sent. Re-run 'repo-remote up --yes' if the instance was stopped, then attach again."
    exit 6
  fi
  rm -f "$errf"
  SSH_MUX_OPTS=(-o ControlPath="$ctl" -o ControlMaster=no)
  verify_host_identity "$alias" "$EXPECTED_ID" "$EXPECTED_SRC" strict
  log "host identity verified: ${alias} reaches ${HOST_ID_OBSERVED} (${EXPECTED_SRC})"

  # 3. Credential — tracing off for the whole window the token is in memory.
  local had_x=false
  case "$-" in *x*) had_x=true; set +x ;; esac
  resolve_gh_session_token
  if ! ssh "${SSH_MUX_OPTS[@]}" -O check "$alias" >/dev/null 2>&1; then
    GH_SESSION_TOKEN=""
    die 6 "the verified SSH connection to ${alias} closed before the session started; no credential was sent. Run attach again."
  fi

  # 4. The session, over the verified master. StrictHostKeyChecking=yes is the
  #    backstop: were the master gone, a fresh connection must still present
  #    the host key recorded when the identity was verified.
  local -a args=()
  if [[ "$ATTACH_HAS_COMMAND" == true ]]; then args+=(-T); else args+=(-t); fi
  args+=("${SSH_MUX_OPTS[@]}" -o StrictHostKeyChecking=yes -o BatchMode=yes)
  local cred=0 remote rc
  [[ -n "$GH_SESSION_TOKEN" ]] && cred=1
  # /bin/sh runs the bootstrap whatever the login shell is.
  remote="exec /bin/sh -c $(sq "$(attach_bootstrap "$cred")")"
  if [[ "$cred" == 1 ]]; then
    # The token is in ssh's ENVIRONMENT only; SendEnv carries it as data.
    LC_REPO_REMOTE_GH_TOKEN="$GH_SESSION_TOKEN" \
      ssh "${args[@]}" -o SendEnv="$RR_TOKEN_ENV" "$alias" "$remote"
    rc=$?
  else
    env -u "$RR_TOKEN_ENV" ssh "${args[@]}" "$alias" "$remote"
    rc=$?
  fi
  GH_SESSION_TOKEN=""
  [[ "$had_x" == true ]] && set -x
  case "$rc" in
    97) log "the credential handoff failed on the VM (see the error above); nothing else was started." ;;
  esac
  return "$rc"
}

# ── result emitters ─────────────────────────────────────────────────────────
# cost_note_human -> a human-readable suffix explaining COST_BASIS/COST_APPROX
# on the printed cost line. A vcpu-scaled or heuristic guess says so
# explicitly rather than the generic "(approximate)" — a confidently-wrong
# flat number is worse for the cost-consent gate than an honestly-vague one.
cost_note_human() {
  [[ "$COST_APPROX" != true ]] && return 0
  case "$COST_BASIS" in
    vcpu-scaled) printf ' (no price data for this type — rough vCPU-scaled guess)' ;;
    heuristic)   printf ' (approximate — no price data for this type)' ;;
    *)           printf ' (approximate)' ;;  # e.g. a table price + GCP accelerator surcharge
  esac
}

emit_plan() {  # dry-run plan (no cloud mutation)
  if [[ "$JSON_OUT" == true ]]; then
    printf '{'
    printf '"action":"plan",'
    printf '"dry_run":true,'
    printf '"provider":"%s",' "$(json_escape "$PROVIDER")"
    printf '"name":"%s",' "$(json_escape "$NAME")"
    printf '"instance_type":"%s",' "$(json_escape "$INSTANCE_TYPE")"
    printf '"region":"%s",' "$(json_escape "$REGION")"
    printf '"disk_gb":%s,' "$DISK_GB"
    if [[ "$PROVIDER" == aws ]]; then
      printf '"volume_type":"%s",' "$(json_escape "$VOLUME_TYPE")"
      if [[ "$VOLUME_TYPE" == gp3 ]]; then
        printf '"volume_iops":%s,"volume_throughput_mibps":%s,' "$VOLUME_IOPS" "$VOLUME_THROUGHPUT"
      else
        printf '"volume_iops":null,"volume_throughput_mibps":null,'
      fi
      # repo#564 (additive): which transport a run would use, and the profile a
      # fresh launch would attach ("" = none).
      printf '"transport":"%s",' "$(json_escape "$TRANSPORT")"
      printf '"instance_profile":"%s",' "$(json_escape "$INSTANCE_PROFILE")"
    fi
    # repo#616 (additive): the purchasing option a fresh launch would use,
    # "on-demand" | "spot", so automation can tell them apart before spending.
    printf '"market":"%s",' "$(json_escape "$MARKET")"
    printf '"gpu":%s,' "$IS_GPU"
    printf '"idle_shutdown_min":%s,' "$IDLE_MIN"
    printf '"ssh_alias":"repo-remote-%s",' "$(json_escape "$NAME")"
    # repo#565 (additive): where `attach` would get the GitHub credential —
    # "device" (repo#627) | "command" | "static" | "none". Reported, never
    # resolved: a dry run neither runs the command nor starts the device flow.
    printf '"gh_credential_source":"%s",' "$(json_escape "$GH_CRED_SOURCE")"
    printf '"estimated_hourly_cost_usd":%s,' "$COST_HOURLY"
    printf '"estimated_cost_approximate":%s,' "$COST_APPROX"
    printf '"estimated_cost_basis":"%s"' "$(json_escape "$COST_BASIS")"
    printf '}\n'
  else
    log "PLAN (dry run — nothing created; pass --yes to provision):"
    log "  provider:            $PROVIDER"
    log "  instance type:       $INSTANCE_TYPE$([[ "$IS_GPU" == true ]] && echo ' (GPU)')"
    log "  region/zone:         $REGION"
    if [[ "$PROVIDER" == aws && "$VOLUME_TYPE" == gp3 ]]; then
      log "  disk:                ${DISK_GB} GB gp3 (${VOLUME_IOPS} IOPS, ${VOLUME_THROUGHPUT} MiB/s)"
    elif [[ "$PROVIDER" == aws ]]; then
      log "  disk:                ${DISK_GB} GB ${VOLUME_TYPE}"
    else
      log "  disk:                ${DISK_GB} GB"
    fi
    log "  idle shutdown:      ${IDLE_MIN} min"
    if is_spot; then
      log "  market:              spot (one-time; TERMINATED, never stopped, on interruption or idle shutdown; a stopped box is never restarted; price and capacity are not guaranteed)"
      log "  est. hourly cost:    \$${COST_HOURLY}/hr$(cost_note_human) — the on-demand rate, an upper bound for Spot (the default Spot maximum price)"
    else
      log "  market:              on-demand"
      log "  est. hourly cost:    \$${COST_HOURLY}/hr$(cost_note_human)"
    fi
    log "  ssh alias:           repo-remote-${NAME}"
    case "$GH_CRED_SOURCE" in
      device)  log "  gh credential:       per-user GitHub App token via the device flow, run by each attach (not run in a dry run)" ;;
      command) log "  gh credential:       minted per attach by REPO_REMOTE_GH_TOKEN_CMD (not run in a dry run)" ;;
      static)  log "  gh credential:       static REPO_REMOTE_GH_TOKEN (REPO_REMOTE_ALLOW_STATIC_GH_TOKEN=1): a shared, long-lived identity" ;;
      *)       log "  gh credential:       none -- attach will refuse; set REPO_REMOTE_GH_APP_CLIENT_ID (device flow)" ;;
    esac
    if [[ "$GH_STATIC_REFUSED" == true ]]; then
      log "  NOTE: REPO_REMOTE_GH_TOKEN is set without REPO_REMOTE_ALLOW_STATIC_GH_TOKEN=1; attach will refuse until it is removed (use REPO_REMOTE_GH_APP_CLIENT_ID)."
    fi
    if [[ "$PROVIDER" == aws ]]; then
      if [[ "$TRANSPORT" == ssm ]]; then
        log "  transport:           ssm (SSH over SSM Session Manager to the instance ID; zero-inbound security group, no public IP needed)"
      else
        log "  transport:           ssh (direct SSH to the public IP; tcp/22 from your address only)"
      fi
      log "  instance profile:    ${INSTANCE_PROFILE:-(none)}"
      if [[ "$TRANSPORT" == ssm && -z "$INSTANCE_PROFILE" ]]; then
        log "  NOTE: a FRESH ssm launch requires REPO_REMOTE_INSTANCE_PROFILE and would be refused; reusing an existing instance does not."
      fi
      if [[ "$TRANSPORT" == ssm ]] && ! ssm_plugin_present; then
        log "  NOTE: session-manager-plugin is not on PATH; 'up --yes' would be refused until it is installed."
      fi
    fi
  fi
}

emit_up_result() {  # <id> <ip> <alias> <reused> [connect-target]
  local id="$1" ip="$2" alias="$3" reused="$4" target="${5-$2}"
  if [[ "$JSON_OUT" == true ]]; then
    printf '{'
    printf '"action":"up",'
    printf '"provider":"%s",' "$(json_escape "$PROVIDER")"
    printf '"name":"%s",' "$(json_escape "$NAME")"
    printf '"instance_id":"%s",' "$(json_escape "$id")"
    printf '"public_ip":"%s",' "$(json_escape "$ip")"
    printf '"ssh_alias":"%s",' "$(json_escape "$alias")"
    if [[ "$PROVIDER" == aws ]]; then
      # repo#564 (additive): the transport, the alias HostName it resolves to
      # (public IP for ssh, instance ID for ssm), and the configured profile.
      printf '"transport":"%s",' "$(json_escape "$TRANSPORT")"
      printf '"connect_target":"%s",' "$(json_escape "$target")"
      printf '"instance_profile":"%s",' "$(json_escape "$INSTANCE_PROFILE")"
    fi
    printf '"instance_type":"%s",' "$(json_escape "$INSTANCE_TYPE")"
    printf '"region":"%s",' "$(json_escape "$REGION")"
    # repo#616 (additive): the CONFIGURED purchasing option. On reuse this is
    # the configuration, not an inspection of the existing instance.
    printf '"market":"%s",' "$(json_escape "$MARKET")"
    printf '"gpu":%s,' "$IS_GPU"
    printf '"idle_shutdown_min":%s,' "$IDLE_MIN"
    printf '"reused":%s,' "$reused"
    printf '"estimated_hourly_cost_usd":%s,' "$COST_HOURLY"
    printf '"estimated_cost_approximate":%s,' "$COST_APPROX"
    printf '"estimated_cost_basis":"%s"' "$(json_escape "$COST_BASIS")"
    printf '}\n'
  else
    if [[ "$PROVIDER" == aws && "$TRANSPORT" == ssm ]]; then
      log "$([[ "$reused" == true ]] && echo reused || echo created) instance $id (${INSTANCE_TYPE}) via SSM Session Manager (region ${REGION})"
    else
      log "$([[ "$reused" == true ]] && echo reused || echo created) instance $id (${INSTANCE_TYPE}) @ ${ip:-<no public ip>}"
    fi
    log "  ssh alias:        $alias"
    log "  attach:           repo-remote attach   (verifies the host, then opens a session with a fresh GitHub credential)"
    log "  est. hourly cost: \$${COST_HOURLY}/hr$(cost_note_human)"
    if is_spot; then
      log "  market:           spot (terminated on interruption or idle shutdown; never stopped or restarted)"
      log "  teardown:         repo-remote down --yes --delete   (Spot terminates; a stop-only down is refused)"
    else
      log "  teardown:         repo-remote down --yes   (or /repo:remote --down)"
    fi
  fi
}

emit_verify_result() {  # <alias> <expected-id> <observed-id> <source>
  local alias="$1" expected="$2" observed="$3" src="$4"
  if [[ "$JSON_OUT" == true ]]; then
    printf '{'
    printf '"action":"verify",'
    printf '"provider":"%s",' "$(json_escape "$PROVIDER")"
    printf '"name":"%s",' "$(json_escape "$NAME")"
    printf '"ssh_alias":"%s",' "$(json_escape "$alias")"
    printf '"expected_instance_id":"%s",' "$(json_escape "$expected")"
    printf '"host_instance_id":"%s",' "$(json_escape "$observed")"
    printf '"identity_source":"%s",' "$(json_escape "$src")"
    # Only ever emitted on the success path -- verify_host_identity() exits 6
    # before reaching here on a mismatch or an unverifiable host, so this field
    # is never false and a caller can gate on its presence alone.
    printf '"verified":true'
    printf '}\n'
  else
    log "host identity verified: ssh alias ${alias} reaches ${observed} (expected ${expected} — ${src})"
  fi
}

emit_status_result() {  # <rows: id state type ip launch, tab/space separated per line>
  local rows="$1"
  if [[ "$JSON_OUT" == true ]]; then
    printf '{"action":"status","provider":"%s","name":"%s","instances":[' \
      "$(json_escape "$PROVIDER")" "$(json_escape "$NAME")"
    local first=true line id state type ip launch
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      id="$(printf '%s' "$line" | awk '{print $1}')"
      state="$(printf '%s' "$line" | awk '{print $2}')"
      type="$(printf '%s' "$line" | awk '{print $3}')"
      ip="$(printf '%s' "$line" | awk '{print $4}')"
      launch="$(printf '%s' "$line" | awk '{print $5}')"
      [[ "$ip" == "None" ]] && ip=""
      [[ "$first" == true ]] && first=false || printf ','
      printf '{"instance_id":"%s","state":"%s","instance_type":"%s","public_ip":"%s","launch_time":"%s"}' \
        "$(json_escape "$id")" "$(json_escape "$state")" "$(json_escape "$type")" "$(json_escape "$ip")" "$(json_escape "$launch")"
    done <<<"$rows"
    printf ']}\n'
  else
    if [[ -z "$rows" ]]; then
      log "no instances tagged repo-remote=${NAME}"
    else
      log "instances tagged repo-remote=${NAME}:"
      printf '%s\n' "$rows" >&2
    fi
  fi
}

emit_down_result() {  # <ids> <disposition: noop|dry-run|stopped|terminated> [fleet-marked ids]
  # The 3rd arg (repo#170) is populated only for a "dry-run" disposition — a
  # dry run never blocks on the fleet-marker guard (see aws_down/gcp_down), so
  # this is how it surfaces which of the listed ids WOULD be refused (absent
  # --force) if the caller re-ran with --yes.
  local ids="$1" disp="$2" marked="${3:-}"
  if [[ "$JSON_OUT" == true ]]; then
    # "market" (repo#616, additive): the configured purchasing option.
    printf '{"action":"down","provider":"%s","name":"%s","disposition":"%s","market":"%s","instances":[' \
      "$(json_escape "$PROVIDER")" "$(json_escape "$NAME")" "$(json_escape "$disp")" "$(json_escape "$MARKET")"
    local first=true id
    for id in $ids; do
      [[ "$first" == true ]] && first=false || printf ','
      printf '"%s"' "$(json_escape "$id")"
    done
    printf '],"fleet_marked":['
    first=true
    for id in $marked; do
      [[ "$first" == true ]] && first=false || printf ','
      printf '"%s"' "$(json_escape "$id")"
    done
    printf ']}\n'
  else
    case "$disp" in
      noop)     log "no instances tagged repo-remote=${NAME} to stop" ;;
      dry-run)  log "DRY RUN — would stop$([[ "$DELETE" == true ]] && echo /terminate): $ids (pass --yes to act)"
                [[ -n "$marked" ]] && log "  NOTE: fleet-marked (would be refused without --force): $marked"
                if is_spot && [[ "$DELETE" != true ]]; then
                  log "  NOTE: REPO_REMOTE_MARKET=spot — 'down --yes' (stop) would be refused; use 'down --yes --delete' to terminate."
                fi ;;
      stopped)  log "stopped: $ids" ;;
      terminated) log "terminated (disks removed): $ids" ;;
    esac
  fi
}

# ── argument parsing ────────────────────────────────────────────────────────
usage() {
  # Print the whole leading comment block (from line 4 to the first non-comment
  # line) rather than a hard-coded line range, so --help cannot silently start
  # truncating the header when documentation is added to it.
  awk 'NR >= 4 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

parse_args() {
  local attach_flag=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      up|status|down|verify|attach|prune) [[ -z "$ACTION" ]] && ACTION="$1" || die 64 "multiple actions given ($ACTION, $1)" ;;
      --status)       ACTION="status" ;;
      --down)         ACTION="down" ;;
      --verify)       ACTION="verify" ;;
      --attach)       ACTION="attach" ;;
      --prune)        ACTION="prune" ;;
      --container)    [[ "$ATTACH_MODE" == host ]] && die 64 "--container and --host are mutually exclusive"
                      ATTACH_MODE=container; attach_flag=true ;;
      --host)         [[ "$ATTACH_MODE" == container ]] && die 64 "--container and --host are mutually exclusive"
                      ATTACH_MODE=host; attach_flag=true ;;
      --command)      [[ $# -ge 2 ]] || die 64 "--command needs a command string"
                      ATTACH_COMMAND="$2"; ATTACH_HAS_COMMAND=true; attach_flag=true; shift ;;
      --yes|-y)       YES=true ;;
      --force)        FORCE=true ;;
      --json)         JSON_OUT=true ;;
      --delete)       DELETE=true ;;
      aws|gcp)        PROVIDER_ARG="$1" ;;
      -h|--help)      usage; exit 0 ;;
      *)              die 64 "unknown argument: $1 (see --help)" ;;
    esac
    shift
  done
  [[ -n "$ACTION" ]] || die 64 "no action given (expected: up | status | verify | down | attach | prune; see --help)"
  if [[ "$attach_flag" == true && "$ACTION" != attach ]]; then
    die 64 "--container, --host and --command apply only to attach"
  fi
  if [[ "$ACTION" == attach && "$JSON_OUT" == true ]]; then
    die 64 "attach opens a session; it has no --json output"
  fi
}

# ── main ────────────────────────────────────────────────────────────────────
main() {
  parse_args "$@"
  resolve_paths
  load_config
  resolve_settings

  case "$ACTION" in
    up)
      require_cost_config
      # repo#559: validate the root-volume settings before the plan or any
      # cloud call, so a bad value never reaches run-instances.
      [[ "$PROVIDER" == aws ]] && validate_aws_volume_config
      # repo#564: transport/profile values are validated just as early.
      validate_transport_config
      # repo#616: the purchasing option, before the plan or any cloud call.
      validate_market_config
      if [[ "$YES" != true ]]; then
        emit_plan          # dry-run: the plan (with cost) is shown, nothing spent
        exit 0
      fi
      case "$PROVIDER" in
        aws) aws_up ;;
        gcp) gcp_up ;;
        *)   die 2 "unknown provider '$PROVIDER'" ;;
      esac
      ;;
    status)
      [[ -n "$PROVIDER" ]] || die 2 "REPO_REMOTE_PROVIDER (or an aws|gcp argument) is required for status"
      case "$PROVIDER" in
        aws) aws_status ;;
        gcp) gcp_status ;;
        *)   die 2 "unknown provider '$PROVIDER'" ;;
      esac
      ;;
    verify)
      # No cost gate: `verify` spends nothing and mutates nothing — it opens one
      # SSH session and compares strings (repo#458).
      [[ -n "$PROVIDER" ]] || die 2 "REPO_REMOTE_PROVIDER (or an aws|gcp argument) is required for verify"
      # repo#564: an ssm alias needs the local plugin; say so plainly rather
      # than as an opaque probe failure.
      validate_transport_config
      [[ "$PROVIDER" == aws && "$TRANSPORT" == ssm ]] && ssm_preflight
      case "$PROVIDER" in
        aws) aws_verify ;;
        gcp) gcp_verify ;;
        *)   die 2 "unknown provider '$PROVIDER'" ;;
      esac
      ;;
    attach)
      # No cost gate and no cloud mutation: attach reaches an EXISTING
      # instance (repo#565). It verifies the host before resolving any
      # credential, so the token command never runs for the wrong box.
      [[ -n "$PROVIDER" ]] || die 2 "REPO_REMOTE_PROVIDER (or an aws|gcp argument) is required for attach"
      case "$PROVIDER" in
        aws|gcp) attach_session; exit $? ;;
        *)       die 2 "unknown provider '$PROVIDER'" ;;
      esac
      ;;
    prune)
      # No cost gate: prune spends nothing and touches only the local SSH
      # config (and known_hosts) -- see prune_aliases (#628).
      [[ -n "$PROVIDER" ]] || die 2 "REPO_REMOTE_PROVIDER (or an aws|gcp argument) is required for prune"
      case "$PROVIDER" in
        aws|gcp) prune_aliases ;;
        *)       die 2 "unknown provider '$PROVIDER'" ;;
      esac
      ;;
    down)
      [[ -n "$PROVIDER" ]] || die 2 "REPO_REMOTE_PROVIDER (or an aws|gcp argument) is required for down"
      # repo#616: the market decides whether a stop-only teardown is allowed,
      # so an invalid value fails here, before any cloud call.
      case "$PROVIDER" in aws|gcp) validate_market_config ;; esac
      case "$PROVIDER" in
        aws) aws_down ;;
        gcp) gcp_down ;;
        *)   die 2 "unknown provider '$PROVIDER'" ;;
      esac
      ;;
  esac
}

# Guard so this file can be `source`d (e.g. by the test suite, to call
# write_ssh_alias() directly for the concurrency test in repo#213) without
# also invoking main() -- mirrors the identical idiom in
# .loom/scripts/lib/github-app-token.sh.
if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
  main "$@"
fi
