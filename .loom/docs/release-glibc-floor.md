# Release build host glibc floor

`.github/workflows/release.yml`'s `build-daemon` job produces the
`loom-daemon` binaries the fleet's install/resync/`--fetch` surface ships to
every worker. This doc records the decided minimum glibc those binaries must
run against, so a future runner-label rotation is an intentional decision,
not a silent surprise caught by the next fleet incident.

## The incident this closes (#8837 / #8842)

On 2026-09-24 the fleet install surface replaced a worker's `loom-daemon`
with a cross-host prebuilt requiring `GLIBC_2.38`/`GLIBC_2.39`, bricking the
CLI (and its own self-repair path) on a glibc-2.35 (Ubuntu 22.04) host. #8837
added a defensive gate — `loom-daemon/src/release_fetch/glibc.rs`
(https://github.com/rjwalters/loom/blob/main/loom-daemon/src/release_fetch/glibc.rs)
at fetch time, `scripts/install/provision-daemon.sh` at install time — that
now REFUSES an incompatible artifact rather than silently installing it. That
gate treats the symptom: it stops a bad artifact from landing, but does
nothing about a release build host that keeps producing one.

The root cause: `build-daemon`'s `x86_64-unknown-linux-gnu` leg built on
`runs-on: ubuntu-latest` with no OS version pinned. `ubuntu-latest` is a
GitHub Actions-managed label, not a fixed OS — it had already rolled from
Ubuntu 22.04 (glibc 2.35) to 24.04 (glibc 2.39) by 2026-09-24, so every
release built after that rotation required a newer glibc than the fleet's
oldest worker provides, with nothing in this repo's CI surfacing the change.

## The decision: pin the floor to glibc 2.35 (Ubuntu 22.04)

`build-daemon`'s Linux legs (`x86_64-unknown-linux-gnu` and
`aarch64-unknown-linux-gnu`) pin `os: ubuntu-22.04` instead of
`ubuntu-latest`. Ubuntu 22.04 ships glibc 2.35, matching:

- the oldest glibc observed on an active fleet worker as of this decision
  (`repo-remote-gf180-surge`, confirmed both by the #8837 incident report and
  by #8842's own filing environment);
- `security.yml`'s pre-existing `ubuntu-22.04` pin (`cargo-deny`/`cargo-audit`
  jobs) — this repo already had one precedent for pinning below
  `ubuntu-latest` rather than trusting the rolling label.

Both Linux legs are pinned, not just the reported `x86_64` one: the
`aarch64-unknown-linux-gnu` leg cross-compiles via `gcc-aarch64-linux-gnu`
installed with `apt-get` on the SAME runner, so its sysroot (and therefore
its glibc floor) tracks that runner's OS exactly as the native `x86_64` leg
does — an unpinned `ubuntu-latest` there would drift identically, just
unobserved so far.

The macOS leg (`aarch64-apple-darwin`, `macos-14`) is unaffected — glibc is a
Linux-only concept.

## Defense-in-depth: a release-time drift check

Pinning `ubuntu-22.04` fixes the root cause, but a pinned label is still a
GitHub Actions-managed string, not a promise this repo controls. `build-daemon`
also runs a cheap **"Verify build host glibc floor"** step (right after
checkout, before any Rust setup) on both Linux legs: it reads `ldd --version`
and fails the build loudly if the runner it actually landed on reports a
newer glibc than the floor above. This is option 3's cheap release-time gate,
layered on top of the pin rather than instead of it — it catches a future
`ubuntu-22.04` retirement/reroute (or a well-intentioned but unreviewed
`ubuntu-latest` revert) at build time, before a newer-glibc artifact ever
reaches the Release, rather than relying solely on #8837's install-time gate
to catch it downstream on some fleet worker.

## Relationship to #8837's gate

The two fixes are complementary, not redundant, and intentionally live in
separate PRs (#8837 as the defensive fix, this doc's change as the root-cause
fix) because they operate on different hosts:

| | Host | What it does |
|---|---|---|
| #8837 (`release_fetch::glibc`, `provision-daemon.sh`) | The **consuming** fleet worker | Refuses to install an artifact it cannot load, no matter where it was built |
| This decision (`release.yml` pin + drift check) | The **producing** release build host | Stops the build host itself from drifting to a newer glibc floor than the fleet supports |

Losing either one regresses a real, already-observed failure mode: without
#8837's gate, a floor mismatch (from anywhere — a mis-pin, a fork, a manual
artifact) still bricks a host; without this decision, every release keeps
requiring `objdump`/`ldd` on the install side to catch what a pinned build
host would have prevented from ever being produced.

## Revisiting the floor

The floor is a decision, not a law — it should move only when there is a
concrete reason, the same way [release cadence](release-cadence.md) is cut
deliberately rather than on a fixed schedule. Raise it (e.g. to `ubuntu-24.04`
/ glibc 2.39) only once every fleet worker that still needs the older floor
has itself been upgraded or retired; until then, dropping the pin (or the
drift check) reopens the exact 2026-09-24 gap. If the floor ever needs to
move, update it in **three** places together: the `os:` pin(s) above, the
`floor_major`/`floor_minor` values in the "Verify build host glibc floor"
step, and this doc's decided-floor section.

## See also

- [`daemon-reference.md`](daemon-reference.md) — daemon self-update wrapper
  scripts and the update lifecycle #8837's install-time gate sits in.
- [`release-cadence.md`](release-cadence.md) — when a release is cut at all;
  this doc governs what a cut release's Linux artifacts require, not when
  one happens.
- Issue [#8837](https://github.com/rjwalters/loom/issues/8837) — the incident
  and the install-time safety gate.
- Issue [#8842](https://github.com/rjwalters/loom/issues/8842) — the
  root-cause follow-up this doc satisfies.
