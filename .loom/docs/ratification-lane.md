# Ratification reviewer lane

The daemon-side lane that gives a ratification PR two independent key reviews
(EE and market) and records each as a marker comment. Part of #13186; the
Champion hold that consumes the result is in [`ratification-hold.md`](ratification-hold.md).

Code: `loom-daemon/src/role_runner/ratification_lane.rs` (selection, markers,
tick) and `ratification_dispatch.rs` (the two sessions).

## Config

`ratificationLane` in `.loom/config.json`. Off by default; a missing,
malformed or half-configured block means the lane never runs.

| Key | Meaning |
|-----|---------|
| `enabled` | `true` to run the lane (anything else is off) |
| `ee`, `market` | each `{repo, path, commit}`: the instruction file for that key, pinned to a full 40-hex commit SHA. Both are required |

### Enabling it fleet-wide

Set the block once in the fleet-tier config (a repo's own `.loom/config.json`
overrides it; see [`fleet-config-lifecycle.md`](fleet-config-lifecycle.md)):

```json
{
  "ratificationLane": {
    "enabled": true,
    "ee":     {"repo": "<owner>/<instructions-repo>", "path": "<path-to-ee.md>",     "commit": "<40-hex SHA>"},
    "market": {"repo": "<owner>/<instructions-repo>", "path": "<path-to-market.md>", "commit": "<40-hex SHA>"}
  }
}
```

The `{repo, path, commit}` pins for `ee` and `market` are operator-owned values;
Loom ships no defaults for them, so the lane stays off until the operator sets
them. Selection also honors `champion.ratificationPaths` / `ratificationPhrases`.

## Flow

1. The Champion role tick calls the lane for the root when `enabled`.
2. Selection finds open PRs that Champion's ratification hold would hold: the
   same predicate (`champion_hold::ratification::scan` returns `Hold`), so every
   spec/decision file declaring ratification-by-merge counts, not only status
   flips. A candidate file that cannot be read is not selected (logged). A PR
   whose current head already carries both markers is skipped. The head SHA is captured here.
3. Two sessions run, `ratification-ee` and `ratification-market`. Each prompt is
   its pinned instructions plus the PR reference and nothing else.
4. For each key whose session ends in success the daemon posts one comment
   (below). A key that failed closed, or whose session did not succeed, posts
   nothing and the reason is logged; the other key is unaffected.

## Marker format

```
<!-- ratify:key=<ee|market> sha=<40-hex head> -->
reviewer=<ratification-ee|ratification-market>
```

The first line is the wire format `keys_marked` parses; `reviewer=` is a visible
line beside it and is not part of the regex. The `sha` is the head captured at
selection, not re-read at post time: a push mid-review leaves the new head
without markers, so the PR is selected again. After the marker the comment carries a
`Verdict:` section: the tail of the session's output (its role log from launch),
at most 4000 characters, with an explicit truncation notice when cut. `<!--` and
`-->` in it are escaped, so it cannot form or alter a marker. It is posted only
for a session that succeeded.

## Trust model

No signing key, broker or per-key App. The daemon posts each marker as the fleet
identity and readers trust it under `.loom/docs/comment-trust.md`, as they do a
Judge verdict; a marker from an untrusted author is prose.

## Independence rule

The two keys are separate sessions, and neither is the session that built the
PR. Author login is not the test: in a fleet the Builder opens the PR as the
same identity that dispatches the lane. The builder's sweep id is read from the
PR's `<!-- loom:pr-lease[-released] role=builder ... sweep=<id> -->` comment;
a lane session (`LOOM_SWEEP_ID`) equal to it is refused before any session is
launched or comment posted. Nothing crosses between the sessions.
