"""Explicit pytest timeout for tests that drive a full ``kct check`` on board 03.

Issue #8901.  ``Test (extended)`` runs with a global ``--timeout=60`` under
``-n auto``, on ``ubuntu-latest`` (4 vCPU) with
``PYTEST_XDIST_AUTO_NUM_WORKERS: "4"`` -- so four tests share four vCPUs.
Seventeen tests across four modules run a complete ``kct check --mfr jlcpcb*``
over ``boards/03-usb-joystick``'s committed routed PCB (some in-process via
``check_cmd.main``, some as a subprocess), and in CI run 43353715620 (``main``
@ ``cebe5919a``) every one of them failed with ``Failed: Timeout (>60.0s) from
pytest-timeout`` -- not one of them on an assertion.

Where that time actually went
-----------------------------
One pass dominated, and it has been fixed at source rather than waited out.
Tallying the ``kicad_tools`` frames in the timeout tracebacks of that run shows
13 of the 13 in-process kills inside
``mask_copper.assess_attributed_geometry`` -- 12 of them in the single
``opening.distance(copper)`` call of its ``openings x objects`` scan.  (The
remaining 4 of the 17 are subprocess-driven; the log does not show the child's
stack, so they are not measured.)  Nothing was killed in ampacity, the Gerber
export or the drift banner.

That scan was an unindexed all-pairs sweep.  Measured directly on board 03's
real attributed geometry (1140 objects, 148,618 pairs) it cost **170.4 s**;
with a bounding-box prefilter over the copper geometries it costs **12.9 s**,
for a byte-identical assessment (same ``evaluated_pairs``, same 410
measurements, same coverage).  One full
``kct check --mfr jlcpcb-tier1`` on that board, with the attribution pass
actually running, went from **194.0 s to 34.9 s** on a 16-core Linux host
(same exit code and findings on both).

That before-number also settles what the old 300 s cap would have bought: at
the ~2.2x hosted-over-4-workers factor below, a 194 s check lands near 427 s,
so raising the cap to 300 s would have converted 17 timeouts at 60 s into 17
timeouts at 300 s and left the job red.  The cost had to come out.
The prefilter is ``mask_copper.py``'s ``STRtree`` candidate query; its
invariants are pinned by
``tests/test_mask_copper.py::test_distance_prefilter_keeps_every_in_reach_pair_and_the_pair_count``.

Why an explicit timeout is still needed after that fix
------------------------------------------------------
34.9 s is under the global 60 s cap, but not by enough to rely on: that figure
is a single-process run on a 16-core host, while the job runs four such checks
concurrently on 4 vCPU in a container.  At the ~2.2x hosted-over-4-workers
factor this job's own comment uses to size its ``timeout-minutes``, 34.9 s
lands near **77 s** -- still over the cap.  So these tests keep an explicit
per-test timeout; what changed is that it now covers the *residual* cost of a
board-03 check instead of a 170 s pass that should never have been there.

The number is **not** measured on CI, and cannot be from a PR: ``Test
(extended)`` is gated ``if: github.event_name != 'pull_request'``
(``.github/workflows/ci.yml:830``), so it only runs on the schedule/dispatch
against ``main``.  300 s is ~4x the 77 s estimate above and ~9x the measured
34.9 s, so it has room for a slower runner without becoming a licence to hang;
it remains two orders of magnitude under the job's own ``timeout-minutes: 90``.
The global ``--timeout=60`` in ``.github/workflows/ci.yml`` is untouched, so
every other test keeps the tight cap.

Caveat on the local numbers.  This host's ``kicad-cli`` build string
(``10.0.6-10.0.6~ubuntu24.04.1``) is not in
``mask_copper_geometry.QUALIFIED_NATIVE_VERSIONS``, so a plain local
``kct check`` abandons the attribution pass and reports ``mask_to_copper``
*incomplete* -- which is why the earlier profile of this check missed the
dominant pass entirely.  The measurements above were taken with that version
gate admitting this host's build string, so the pass that CI runs was really
exercised; the gate governs whether the plot profile is *trusted*, not the size
or shape of the geometry being compared.  Absolute times on a 4 vCPU runner
will still differ.

#8888 and #8843 own the remaining ~35 s of this check.
"""

from __future__ import annotations

import pytest

#: Seconds allowed for one test that shells out to a full board-03 ``kct
#: check``.  See the module docstring for the measurements behind it.
BOARD03_CHECK_TIMEOUT_S = 300

#: Apply to any test whose body runs a complete ``kct check`` over board 03's
#: committed routed PCB.  Replaces the global ``--timeout=60`` for that test
#: only.
board03_check_timeout = pytest.mark.timeout(BOARD03_CHECK_TIMEOUT_S)
