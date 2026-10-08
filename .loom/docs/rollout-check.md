# Rollout Check

A merged PR that moves work between hosts can pass CI and review and still be wrong
on the live fleet (#10498 moved ETA authority to one host and nothing confirmed it
did what was intended). The rollout check makes that confirmation a tracked step.
It is **tracked, not automated**: no daemon feature runs it (automation is deferred
until the singleton watchdog, #10897 / #10898, lands).

## Which PRs

PRs that move work between hosts or change who emits a fleet signal: authority,
captain, singleton jobs, gating, capability routing. Other PRs carry no section.

## The section

The Builder adds `## Rollout check` to the PR body, naming:

- the production signal (a SigNoz query, metric, or daemon command), and
- the value expected after the fleet rolls.

"Verify in prod" is not a signal. The Judge requests changes if the section is
missing or vague.

## Who runs it, and where it is recorded

1. On approval the Judge comments on the linked issue (for a `Part of #N` PR, on
   #N) with a fixed marker, so the item stays queryable after the issue closes:

   ```
   <!-- loom:rollout-check-pending pr=<PR> -->
   Rollout check (PR #<PR>): <signal> expects <value>
   ```

2. Every Champion pass lists candidates with a comment search (it covers closed
   issues; the search index can lag a few minutes):

   ```bash
   gh search issues --repo <owner>/<repo> --match comments '"rollout-check-pending"' \
     --limit 20 --json number --jq '.[].number'
   ```

   An issue is **pending** when one of its comments contains
   `loom:rollout-check-pending pr=<PR>` and none contains
   `loom:rollout-check-done pr=<PR>` (confirm via
   `gh issue view <N> --json comments`).
3. **When the roll counts as done**: 24 hours after the PR's `mergedAt`
   (`gh pr view <PR> --json mergedAt`). Before that, skip the item. This fixed grace
   period covers the fleet self-update cadence; Loom has no fleet-wide version
   check for Champion to use instead.
4. Once due, the Champion runs the signal (at most 3 items per pass) and comments
   on the issue, starting with `<!-- loom:rollout-check-done pr=<PR> -->`,
   giving the observed value. That marker closes the item.

## Escalation

If the observed value differs from expected, or the signal cannot be queried, the
Champion labels the issue `loom:operator` and comments the observed vs expected
values (still writing the done marker, so the next pass does not re-run it). A
human decides whether to roll back or file a fix.
