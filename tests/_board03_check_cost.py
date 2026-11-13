"""Explicit pytest timeout for tests that drive a full ``kct check`` on board 03.

Issue #8901.  ``Test (extended)`` runs with a global ``--timeout=60`` under
``-n auto``, on ``ubuntu-latest`` (4 vCPU) with
``PYTEST_XDIST_AUTO_NUM_WORKERS: "4"`` -- so four tests share four vCPUs.
Seventeen tests across four modules run a complete ``kct check --mfr jlcpcb*``
over ``boards/03-usb-joystick``'s committed routed PCB (some in-process via
``check_cmd.main``, some as a subprocess), and in CI run 43353715620 (``main``
@ ``cebe5919a``) every one of them failed with ``Failed: Timeout (>60.0s) from
pytest-timeout`` -- not one of them on an assertion.  The global cap is simply
below what one board-03 check costs there.

Why the cost is not removed at source instead
---------------------------------------------
One ``kct check --mfr jlcpcb-tier1`` on that board was profiled on
``feature/issue-8901`` (16-core Linux host, load ~9, ``cProfile``, so the
absolute numbers are inflated; the shape is what matters).  Of 52.8 s of
cumulative time there is no single dominant pass:

===========================================  ======  ===========================
pass                                         cumtime added by
===========================================  ======  ===========================
``check_ampacity`` / ``measure_pour_ampacity`` 13.6 s  pre-existing (76650
                                                      ``pour_ampacity._probe``
                                                      calls)
``check_mask_to_copper``                       13.4 s  #8842 (default-on)
``kicad-cli`` subprocesses (6 calls)            8.5 s  fab-view Gerber export
``_emit_drift_banner`` / ``analyze_drift``      5.4 s  pre-existing (1119 sexp
                                                      parses)
===========================================  ======  ===========================

Clearing the timeouts "at source" would therefore mean four independent
optimisations in three subsystems, each with its own correctness risk, to buy
a test-harness margin.  Issue #8901 explicitly allows the alternative: an
explicit per-test timeout carrying the measured reason.  #8888 and #8843 own
the cost itself.

Measured wall time (the basis for the number below)
---------------------------------------------------
Uninstrumented, same host, two consecutive runs of
``python -m kicad_tools.cli check boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb
--mfr jlcpcb-tier1``: **23.6 s and 25.7 s**.

The 17 tests, run with the extended-tier flags (``-o addopts=
--benchmark-disable --timeout=60 -m "ci_extended and not slow"``) at ``-n 2``
on the same host, measured **12.0 s to 34.5 s** each -- i.e. they fit inside
the 60 s cap here but not on CI's narrower container at ``-n auto``.  Note
that this host's ``kicad-cli`` build string is not a qualified plot profile,
so ``mask_to_copper`` reports *incomplete* locally while CI's pinned image
runs it to completion; the real CI cost is therefore **above** these numbers,
which is consistent with the observed timeouts.

``300`` s leaves roughly 9x headroom over the slowest local measurement while
still bounding a genuine hang two orders of magnitude below the job's own
``timeout-minutes``.  It does not touch the global ``--timeout=60`` in
``.github/workflows/ci.yml``, so every other test keeps the tight cap.
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
