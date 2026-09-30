# Bumping the pinned KiCad container image

Every KiCad-dependent CI job in `.github/workflows/ci.yml` runs inside (or
`docker run`s) the container pinned by the workflow-level `env:` value:

```yaml
env:
  KICAD_IMAGE: kicad/kicad@sha256:<digest>
```

That one line is the **single source of truth** for the pin (#5682). It exists
because Docker Hub republished the floating `kicad/kicad:10.0` tag from
KiCad 10.0.5 to 10.0.6 on 2026-09-22 with no repo-visible signal, silently
changing measured native behaviour and — because the native mask-to-copper
gate is not `continue-on-error` — skipping every later step of the `Test` job
(#5678). A digest pin makes the next tag move an announced, reviewable event
instead.

How the value reaches each consumer:

- The 12 container jobs use `image: ${{ needs.kicad_pin.outputs.image }}`.
  GitHub does not allow the `env` context in `jobs.<job_id>.container.image`
  (only `github`, `inputs`, `matrix`, `needs`, `strategy`, `vars`), so the
  tiny `kicad_pin` bridge job republishes the env value into the `needs`
  context. Do not "simplify" this back to `${{ env.KICAD_IMAGE }}` at the
  `image:` lines — it fails workflow parsing.
- The `routed-pcb-drc-check` job's `docker run` step uses `"$KICAD_IMAGE"`
  (workflow env is inherited by every step's shell).
- `scripts/ci/run_observed.py` records `configured_image` from the same env
  var, so the observer's identity metadata cannot drift from the workflow.

Note: `scripts/check_fill_fragment_bonding_vs_native.py` carries its own
independent `KICAD_IMAGE` digest pin. That is deliberate (it predates this
one and is the pattern's precedent); it is NOT governed by the workflow value
and is bumped only when that script's own oracle is re-measured.

## Bump procedure

A version bump is a **reviewable PR** that does all of the following in the
same change:

1. **Resolve the new digest** from the tag you want to move to, and confirm
   the version it actually is before touching the workflow:

   ```bash
   docker manifest inspect kicad/kicad:<tag>   # sanity: the tag exists
   docker run --rm kicad/kicad:<tag> kicad-cli --version
   ```

   Compare against
   `https://registry-1.docker.io/v2/kicad/kicad/manifests/<tag>`'s
   `Docker-Content-Digest` header if you need the canonical tag→digest
   mapping without a local Docker daemon.

2. **Update the single `KICAD_IMAGE` env value** in
   `.github/workflows/ci.yml`. There is exactly one digest literal in the
   workflow; do not add another.

3. **Update `QUALIFIED_NATIVE_VERSIONS`** in
   `src/kicad_tools/validate/mask_copper_geometry.py` so the new KiCad
   version is admitted by the native mask-to-copper gate. The constant is a
   tuple of `kicad-cli` version strings (e.g. `("10.0.5", "10.0.6")`); add
   the new version only after step 4's gates pass against the new image.

4. **Re-run the native gates against the new image** — this is what the PR's
   own CI run provides as reviewable evidence, so push the branch and let it
   go green before requesting review:

   - native mask-to-copper (`Test` job's `mask-copper` observed group),
   - native zone acceptance (`Run native zone acceptance`),
   - native physical-stitch acceptance (`Run native physical-stitch
     acceptance`),
   - fill-fragment bonding (`scripts/check_fill_fragment_bonding_vs_native.py`
     — its oracle must be re-measured under the new image before its own pin
     moves; file a separate PR for that if the measurements differ).

5. **Link the evidence** in the PR body: the digest resolution output, the
   `kicad-cli --version` output, and the green native-gate jobs.

Treat any gate failure under the new image as a behaviour change to
investigate (as in #5678/#5683, where 10.0.6 was measured byte-identical to
10.0.5 for everything the native gates check and then admitted) — never as a
reason to widen a tolerance quietly.
