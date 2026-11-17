"""Epilog text for ``kct route --help`` (``route_cmd._route_parser``).

``route_cmd._route_parser`` is the only ``kct route`` parser: ``cli.main``
hands the argv after ``route`` straight to ``route_cmd.main`` (issue #7017).
The text lives in this import-light module so tests and docs can read it
without importing the huge ``route_cmd`` module (issue #6291).
"""

ROUTE_EXIT_CODES_EPILOG = """\
exit codes:
  0  all nets routed (or meets --min-completion), DRC clean
  1  fatal failure -- no nets routed
  2  partial routing -- below threshold or requested placement-blocked nets
  3  post-route validation or processing failed:
     DRC violations remain, --auto-fix rolled back (issue #2852),
     the --voltage-map HV pairwise audit failed (issue #4588),
     selective zone filling failed, or staged repair could not
     preserve fixed copper and net membership
  4  partial routing AND segment-segment or HV pairwise
     clearance violations (issues #1666, #4588)
  5  interrupted by SIGINT with partial results saved
  8  --complete: one or more unroutable links remain (issue #4477)
  9  a pre-route gate fired -- detailed routing was not started;
     the stderr prefix says which:
     [crosstail-gate] --census-advisory-gate predicts an inert
     crossover lattice (issue #4799, before any router work), or
     [plan-gate] --plan-gate found the routing plan infeasible
     (issue #5510-1c, after the plan stage, before any detailed
     routing); --force overrides the latter
  10 UNVERIFIED -- would have been 0, but the post-route DRC and/or
     stranded-pour check did not complete (--timeout ran out, or
     kicad-cli crashed/was killed/timed out, issues #6273, #6724);
     the board is saved, run 'kct check' on it.
     JSON summary.verdict is "unverified"
"""

ROUTE_PERFORMANCE_EPILOG = """\
PERFORMANCE: The C++ backend provides 10-100x faster routing.
Check status with 'kct build-native --check' and build with
'kct build-native'. The --backend flag defaults to 'auto' which
uses C++ when available and falls back to Python otherwise.
"""
