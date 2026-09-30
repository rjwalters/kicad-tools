# Kimi native-ephemeral containment verification — 2026-09-22

Follow-up to #8565 (Kimi added to `docker/native/`'s per-sweep ephemeral
containment, in the shape #8403/#8437 built for Pi/OpenCode). Host: macOS
arm64, Docker Desktop 29.8.0. Image under test: `loom-worker-native:kimi-test`,
built locally from this branch's `docker/native/Dockerfile`
(`FROM ghcr.io/rjwalters/loom-worker:latest@sha256:8bb3c0fbbb4a…`), digest
`sha256:25a879216b434632ba5210d8eeed3c2f38751b5f34a76d8079e145b9fd158b0d`.

This is the same class of evidence #8434 asks for on the Pi/OpenCode side
(that issue stays open and unchanged by this receipt — it is not Kimi-specific
and its own live run has not happened). No Kimi credential (API key or OAuth
account) was available in this environment, so the one thing this receipt does
**not** cover is a credentialed end-to-end coding canary — `kimi` actually
completing a task against a real model. Everything about the *containment*
guarantee — image build, version pin, per-launch relocation, isolation, and
that no credential value reaches the writable layer — was verified against
real `docker run`/`docker build`, not only unit tests over constructed argv.

## 1. Image build and smoke test

`docker build -f docker/native/Dockerfile -t loom-worker-native:kimi-test .`
succeeded, including the build-time assertions this issue added:

- Node 24.19.0 (inherited from the existing Node LTS pin) clears Kimi's
  `engines.node` floor (`KIMI_NODE_FLOOR=22.19.0`) — the `sort -V` check ran
  and passed before any package was fetched.
- `npm install -g … "@moonshot-ai/kimi-code@2.0.2"` succeeded, and the
  post-install equality check (`kimi -V` vs `KIMI_CODE_VERSION`) passed:
  `Installed: opencode 1.18.31, pi 0.85.1, kimi 2.0.2 (node 24.19.0)`.

`bash docker/native/test-image.sh loom-worker-native:kimi-test` — **24/24
checks passed**, 0 failures, including the Kimi-specific additions:

```
PASS: kimi is pinned at the tested version: 2.0.2
PASS: node 24.19.0 meets Kimi's engines.node floor (22.19.0)
PASS: OPENCODE_DISABLE_AUTOUPDATE=1 is baked into the image
PASS: KIMI_CODE_NO_AUTO_UPDATE=1 is baked into the image
PASS: KIMI_DISABLE_TELEMETRY=1 is baked into the image
PASS: KIMI_CODE_HOME is not baked into the image (the dispatcher relocates it per launch)
PASS: /home/loom/.loom-native exists, is owned by uid 1000, and is writable
PASS: /home/loom/.loom-native is empty (no baked session store or auth.json)
PASS: a second container cannot see the first container's ephemeral state
PASS: no secret-shaped strings found in docker history
PASS: kimi present on PATH
```

(Full output has all 24 lines; the above is the Kimi-relevant subset.)

## 2. Two concurrent Kimi workers: disjoint homes and session stores

Two independent `docker run` invocations against the built image, each
passing `KIMI_CODE_HOME` exactly as `worker_spawn::containment::docker_command`
constructs it (`/home/loom/.loom-native/<launch-id>/kimi`), with two different
launch ids:

- Worker A (`launch-aaaa1111`): wrote a session file and a fake OAuth
  credential file under its `KIMI_CODE_HOME`.
- Worker B (`launch-bbbb2222`): independent container, inspected
  `/home/loom/.loom-native/` from the inside.

Observed:

```
=== Worker A ===
KIMI_CODE_HOME=/home/loom/.loom-native/launch-aaaa1111/kimi
total 16
drwxr-xr-x 3 loom loom 4096 Sep 22 16:40 .
drwxr-xr-x 3 loom loom 4096 Sep 22 16:40 ..
drwxr-xr-x 2 loom loom 4096 Sep 22 16:40 credentials
-rw-r--r-- 1 loom loom   21 Sep 22 16:40 session.json

=== Worker B ===
KIMI_CODE_HOME=/home/loom/.loom-native/launch-bbbb2222/kimi
Does worker A path exist in this container?
total 16
drwxr-xr-x 1 loom loom 4096 Sep 22 16:40 .
drwxr-x--- 1 loom loom 4096 Sep 22 16:37 ..
drwxr-xr-x 3 loom loom 4096 Sep 22 16:40 launch-bbbb2222
OK: worker A per-launch path is absent from worker B's filesystem
```

Worker B's container has no `launch-aaaa1111` entry at all — disjoint by
container (each `docker run` is its own filesystem) **and** by path (the
per-launch id), matching the OpenCode/Pi property `containment_tests.rs`
already asserted structurally, now shown live for Kimi.

## 3. Post-run writable-layer credential scan

A third container was run (kept alive, not `--rm`) with `KIMI_MODEL_API_KEY`
forwarded **by name only** (`-e KIMI_MODEL_API_KEY`, no `=value` — docker reads
the value live from the launching process's own environment) and asked to
write a session file plus a simulated OAuth token file under its
`KIMI_CODE_HOME`, exercising the one route where Kimi is expected to persist a
credential-shaped file to disk (`credentials/oauth.json`).

`docker diff` on the container after the run:

```
C /home
C /home/loom
C /home/loom/.loom-native
A /home/loom/.loom-native/launch-scan0001
A /home/loom/.loom-native/launch-scan0001/kimi
A /home/loom/.loom-native/launch-scan0001/kimi/credentials
A /home/loom/.loom-native/launch-scan0001/kimi/credentials/oauth.json
A /home/loom/.loom-native/launch-scan0001/kimi/session.json
```

Every changed path is under the per-launch `KIMI_CODE_HOME` — nothing else in
the writable layer moved.

`docker export` of the container's full filesystem, grepped for the literal
API key VALUE (`sk-kimi-fake-secret-for-verification-only`, forwarded only as
a process-environment variable, never as a CLI argument or file write by the
dispatcher): **0 matches**. The key reached the container process environment
(confirmed by the container printing it back) but never landed in any file.

The one credential-shaped artifact that *is* expected on disk — the simulated
OAuth token file — was found at exactly one path:
`/home/loom/.loom-native/launch-scan0001/kimi/credentials/oauth.json`, i.e.
only under the launch home, consistent with the doc's claim that a guarded or
unguarded OAuth route's `credentials/` directory lives inside the relocated
`KIMI_CODE_HOME` and nowhere else.

`docker rm -f` on the container removed it; a follow-up existence check
confirmed it (and therefore its writable layer, including that token file)
was gone — the container's ephemeral lifetime is what tears the credential
file down, matching "torn down with the container" in this issue's acceptance
criteria.

## What this does not cover

- A credentialed functional canary (Kimi completing a real coding task through
  the guarded or API-key route) — no Kimi credential was available in this
  environment. This is the same live-run gap #8434 already tracks for
  Pi/OpenCode; it is not reopened or duplicated here.
- The guarded-binding capability flip (`KIMI_GUARD_VERIFIED`) — unrelated to
  containment, tracked by #8636.
