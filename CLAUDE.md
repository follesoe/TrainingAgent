# TrainingAgent

A Claude Code agent for analysing endurance training (cycling, running, ski touring,
strength) using **intervals.icu as the source of truth** for activities, zones, wellness
and the training calendar.

The athlete records on devices (Wahoo, Apple Watch, Zwift), which sync to intervals.icu.
This repo reads that data, analyses it, writes training plans back to the calendar, and
keeps performance reports as markdown.

## Setup for a new athlete

Nothing in the code is athlete-specific — zones, FTP, thresholds and weight are all read
from the API at runtime. A new user only needs credentials:

```bash
uv sync
cp .env.example .env      # then paste the API key
uv run training-agent ping
```

The API key comes from <https://intervals.icu/settings> → **Developer Settings**.
Leave `INTERVALS_ATHLETE_ID=0` — intervals.icu treats `0` as "the athlete this key
belongs to".

## Commands

```bash
uv run training-agent ping                  # verify auth, show athlete
uv run training-agent zones                 # thresholds and zones per sport
uv run training-agent activities --days 30  # recent activities
uv run training-agent wellness --days 14    # weight, RHR, HRV, sleep, CTL/ATL/form
uv run training-agent check --days 90       # data-integrity audit (see below)
uv run training-agent upload <file.fit>     # make a Strava-only activity readable
uv run training-agent report <file.md>      # publish a report as a dated NOTE
uv run training-agent raw /athlete/@me/...  # any endpoint; @me expands to the athlete id
```

`raw` is the exploration escape hatch. Prefer it over writing throwaway scripts when
poking at an unfamiliar endpoint.

## Layout

```
src/training_agent/
  config.py      env / secrets
  intervals.py   API client (get/post/put/delete + typed helpers)
  audit.py       data-integrity checks
  cli.py         commands
reference/       intervals.icu OpenAPI spec (pretty-printed, ~20k lines - grep it)
data/            personal data, git-ignored
  performance-reports/   YYYY-MM-DD-title.md
bikefit/         third-party bike-fit prompt, unrelated to the agent
```

`reference/intervals-openapi.json` is the authority on endpoints and field names.
Grep it before guessing — it has 117 paths and the field names are not obvious.

## Critical API knowledge

These were all learned the hard way. They are not in the public docs.

### Strava-sourced activities are invisible to the API

Activities that reach intervals.icu **through the Strava connection** return a stub:

```json
{"id": "...", "start_date_local": "...", "source": "STRAVA",
 "_note": "STRAVA activities are not available via the API"}
```

Strava's terms forbid re-exposing them. They are visible in the web UI and **do count
toward CTL/ATL** — they are only invisible to us. They also cannot be updated *or
deleted* via the API (both return 422).

Everything else is fully readable: `WAHOO`, `ZWIFT`, `OAUTH_CLIENT` (Companion app),
`UPLOAD`, `MANUAL`.

**Recovery path:** export the original file from Strava and `training-agent upload` it.
It lands as `UPLOAD` and becomes fully readable. intervals.icu then de-dupes the Strava
copy away by itself — the `--replaces` delete will 422, which is expected and harmless.

Keep Strava connected regardless: **activity titles and descriptions flow from Strava
onto directly-synced activities** via `strava_id`. Every description in the account
arrived that way. It is the metadata layer, not the data path.

### Pairing a plan to a completed activity

Set it from the **activity** side. The event side silently no-ops:

```python
client.put(f"/activity/{activity_id}", {"paired_event_id": event_id})   # works
client.update_event(event_id, {"paired_activity_id": activity_id})     # returns None
```

intervals.icu auto-pairs a same-day activity of matching type, so it will happily attach
a hard interval session to an easy-run plan if that is the only plan that day. Check.

### Structured workouts

Put the step syntax in the event `description`; intervals.icu parses it into
`workout_doc` and recomputes duration/distance itself.

```
Warmup
- 10m 6:00/km Pace

8x
- 1km 4:30/km Pace
- 90s 6:30/km Pace

Cooldown
- 5m 6:00/km Pace
```

- `m` means **minutes**, not metres. Use `km` / `mtr` for distance.
- A pace step needs the explicit `Pace` keyword. Without it the pace is silently ignored.
- Repeats need a **blank line** before `8x`, or the group is not formed.
- Ranges work: `5:40-6:10/km Pace`. Power: `- 2h 55-72%`. HR: `- 10m Z1 HR`.
- Trailing prose after the steps is preserved and does not break parsing.
- **Do not set `moving_time`/`distance` by hand.** An empty `workout_doc` overwrites
  `distance` with 0. For a route distance on an unstructured ride use `distance_target`.

### Targets must resolve or device export fails

A workout only exports to a device if every leaf step resolves to a concrete target.
Fetch events with `resolve=true` and check each leaf has `_pace` / `_hr` / `_power`.

If the sport has no threshold configured, intervals.icu silently falls back to
`target: POWER` and emits steps with no values. Wahoo then rejects the push:

```
422 Plan validation error: each interval that is not of type 'repeat'
    must have a valid 'targets' array
```

Running pace workouts therefore require `threshold_pace` (m/s) on the Run sport-settings
entry: `PUT /athlete/{id}/sport-settings/{id}`.

### Other traps

- **Uploads de-dupe on file hash**, so re-uploading the same file is safe (200, no new
  activity). A Strava-API activity has no hash, so it will not block an upload.
- **Wellness/CTL recalculation is asynchronous.** After deleting or uploading, the first
  read may return stale CTL. Poll until `ctlLoad` matches the sum of activity load.
- **`latlng` streams split across two fields**: `data` holds latitude, `data2` longitude.
- Distance/altitude streams contain `None` padding at both ends. `x or 0` turns those
  into zeros and produces nonsense deltas — skip to the nearest non-null instead.
- Athlete id `0` means "me". `@me` in the `raw` command expands to the configured id.
- Rate limits: 5000/day, 2500 per rolling 15 min, 10/sec per IP.

## Data integrity

Two mechanisms silently inflate training load, and both are invisible unless checked.
Run `training-agent check` after any bulk sync.

1. **Duplicate sessions** — the same workout recorded twice (e.g. Zwift plus an Apple
   Watch copy). Both carry load, so the day counts double. Detected by *time overlap*,
   not exact start time — copies often start seconds apart.
2. **Plan placeholders** — marking a planned workout "done" materialises the plan as a
   `MANUAL` activity beside the real recording. Signature: `MANUAL` + `paired_event_id`
   set + no distance. Advise the athlete not to use "mark as done"; just let the upload
   land and intervals.icu matches it.

When resolving duplicates, keep the copy that carries **training load** first, then the
one with a **real name** over a generic one (`Snowboarding`, `Other Workout`,
`Slopes - a day ...`), then greater distance. Before deleting, carry over any place name
from the doomed copy so it is not lost.

## Conventions

- **Performance reports** live in `data/performance-reports/YYYY-MM-DD-title.md` and are
  published to intervals.icu with `training-agent report`. The markdown file is the
  source of truth; the calendar NOTE is a synced copy. Re-publishing replaces that date's
  note rather than stacking duplicates.
- **Race analysis** can also be attached to the activity itself via
  `POST /activity/{id}/messages`, which surfaces it in the intervals.icu UI. Messages
  support `GET`/`POST` only — no editing, so corrections go alongside the original.
- Correct reports in place with a visible **data-correction note** rather than silently
  overwriting figures. Analysis gets revised as data improves; say so.

## Working style

- Verify against the athlete's own data before asserting anything. Several conclusions in
  this project were wrong until duplicates were cleaned up or a course feature was found
  in the GPS.
- Quantify. "Decoupling 15.8%" beats "faded badly".
- Distinguish what the data shows from what it implies. When an athlete's subjective read
  conflicts with the numbers, investigate rather than defer to either.
- Destructive actions (deleting activities, changing sport settings) need explicit
  confirmation, and a dry-run listing first when more than one record is affected.
