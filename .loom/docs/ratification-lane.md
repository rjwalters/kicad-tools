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

## Flow

1. The Champion role tick calls the lane for the root when `enabled`.
2. Selection finds open PRs that add or edit `ratification/requests/*.md`, or
   flip a `Status:` line to ratified under `spec/**`, `decisions/**` or
   `docs/decisions/**`. A PR whose current head already carries both markers is
   skipped. The head SHA is captured here.
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

The two keys are separate sessions, neither the author of the PR. A PR authored
by the dispatching identity is refused before any session is launched or
comment posted. Nothing crosses between the sessions.
