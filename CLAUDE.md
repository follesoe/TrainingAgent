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

Zwift (optional, needs `ZWIFT_EMAIL` / `ZWIFT_PASSWORD` — see the Zwift section):

```bash
uv run training-agent zwift ping                 # verify login, show rider profile
uv run training-agent zwift route alpe           # route distance/elevation — works offline
uv run training-agent zwift schedule "Crêpe Escape" --date tomorrow --watts 200
                                                 # → planned ride on the intervals.icu calendar
uv run training-agent zwift meetups --days 30    # scheduled private rides
uv run training-agent zwift events               # public events signed up for
uv run training-agent zwift plan                 # workouts on the Zwift calendar
uv run training-agent zwift workouts --search vo2 # workout library (~900)
uv run training-agent zwift workout <uuid> --as-intervals  # steps as intervals.icu syntax
uv run training-agent zwift raw /api/profiles/me # any endpoint
```

## Layout

```
src/training_agent/
  config.py      env / secrets
  intervals.py   API client (get/post/put/delete + typed helpers)
  zwift.py       unofficial Zwift API client, route lookup, .zwo parsing
  audit.py       data-integrity checks
  cli.py         commands
reference/       git-ignored, regenerate on a fresh clone (see below)
                 intervals-openapi.json — spec (pretty-printed, ~20k lines - grep it)
                 zwift-routes.json — 335 routes keyed by Zwift routeId
data/            personal data, git-ignored
  performance-reports/   YYYY-MM-DD-title.md
bikefit/         third-party bike-fit prompt, unrelated to the agent
```

`reference/intervals-openapi.json` is the authority on endpoints and field names.
Grep it before guessing — it has 117 paths and the field names are not obvious.

### Regenerating reference data

`reference/` is git-ignored — both files are large, vendored third-party data that is
not ours to republish. **They will be missing on a fresh clone**, so recreate them before
relying on any "grep the spec" instruction above:

```bash
# intervals.icu OpenAPI spec (no auth needed; pretty-print so it greps well)
curl -s https://intervals.icu/api/v1/docs \
  | python3 -m json.tool > reference/intervals-openapi.json

# Zwift route catalogue, from andipaetzold/zwift-data (MIT)
curl -sL https://raw.githubusercontent.com/andipaetzold/zwift-data/main/src/routes.ts \
  -o /tmp/routes.ts
# strip the TS import line, the ': ReadonlyArray<Route>' annotation and 'as const',
# then print JSON.stringify(routes) with node -- see zwift-routes.json's _regenerate key
```

The Zwift catalogue is only the **offline fallback**. When logged in, `live_routes()`
prefers `/api/game_info`, which has more routes and is always current, cached to
`data/.zwift-game-info.json`.

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

### Correcting an activity's distance

`PUT /activity/{id}` **silently ignores `distance`** — the response echoes the old value.
The editable override is `icu_distance`; setting it sticks and recomputes
`average_speed`/`pace`. Load is **not** recomputed from it: `pace_load` still comes from
the recorded stream, so when the recording itself is wrong (Apple Watch clips treadmill
pace to ~5:25/km), correct load separately via `icu_training_load`. Document any such
correction with `POST /activity/{id}/messages` `{"content": ...}` so the edit is visible.

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
- **A prose-only `description` still builds an empty `workout_doc`**, and that is what
  zeroes `distance`. Worse, once any `workout_doc` exists, explicitly posted `distance`,
  `moving_time` and `icu_training_load` are all **silently ignored** — intervals.icu
  recomputes them from the steps. Verified by experiment, not documented anywhere.

  To get a planned ride that shows real distance *and* duration, give it one **distance
  step with a power target** and put the prose after a blank line:

  ```
  - 7191mtr 50%

  Loop de loop de loop (paris)
  7.2 km, 36 m elevation
  ```

  intervals.icu then derives distance, duration and load itself. A *time* step
  (`- 15m 50%`) sets duration but leaves `distance` at 0, so prefer the distance form when
  the length is the point. The step percentage is an integer, so the resolved target can
  land ~1 W off the number you asked for.

### Indoor FTP is a separate field

The cycling sport-settings entry carries **both** `ftp` and `indoor_ftp`, and the two
differ. An event with `indoor: true` resolves its percentage targets against `indoor_ftp`
— verified by fetching back with `resolve=true` and confirming the step's `_power` was the
indoor-derived wattage, not the outdoor one.

So when planning anything indoor (all Zwift work), read `indoor_ftp` and fall back to
`ftp` only if it is unset. Using the outdoor number silently under-states intensity.

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

## Zwift

intervals.icu stays the source of truth for load, zones and completed work. Zwift is
consulted for what intervals.icu cannot know: which *route* a ride was on, what is
*scheduled* in Zwift itself, and the step structure of Zwift's workout library.

### The built-in intervals.icu ↔ Zwift connection is not symmetric

intervals.icu has its own Zwift connection in `/settings` with an **"Upload planned
workouts"** checkbox. It runs on Zwift's official Training API — the same partner API that
is closed to individuals — and its scope is narrow:

| Direction | What moves |
|---|---|
| intervals.icu → Zwift | planned **workouts**, roughly the next week's worth |
| Zwift → intervals.icu | completed **activities** only |

Nothing else comes back. Zwift-side scheduled rides, meetups, route selections and Zwift's
own training plans do **not** appear in intervals.icu, and past Zwift workouts cannot be
pulled down. Confirmed on the [announcement thread](https://forum.intervals.icu/t/intervals-icu-and-zwift-integration-is-live/81764):
the workout sync is one-way out of intervals.icu.

That asymmetry is exactly the gap `training-agent zwift` fills — meetups, event signups,
route metadata and the Zwift workout library are only reachable through the unofficial API
below. Turning the checkbox on is still worth it and does not conflict: let it own
intervals.icu → Zwift, and use this tooling for the other direction.

Two limitations of that connection that matter here:

- **FTP is not synced.** It must be kept equal on both platforms by hand, or the same
  workout means different watts in each place.
- **Run workouts need the 5K time entered on Zwift** to match the intervals.icu threshold
  pace. Heart-rate targets are not supported at all; power (% FTP), pace and cadence are.

### There is no public API, and no key

Zwift's official API ("Training Connections") is a B2B arrangement via
developers@zwift.com and is not offered to individuals. What the game client uses is a
JSON REST API behind an OAuth **password grant**, so the account password itself goes in
`.env`. It is exchanged once for a token cached in `data/.zwift-token.json` (git-ignored,
chmod 600) and refreshed from there. Consequences worth stating plainly:

- An account that signs in only via Apple/Google/Facebook has no password and cannot be
  used. Set one at <https://www.zwift.com/settings/account>.
- Nothing here is supported or documented by Zwift. Endpoints can change without notice —
  a sudden 404 or a changed field name is maintenance, not a caller bug.
- Keep request volume near what the Companion app would produce.

Endpoint knowledge came from [SauceLLC/sauce4zwift](https://github.com/SauceLLC/sauce4zwift)
(`src/zwift.mjs`), the most actively maintained reference implementation.

### Verified endpoints

Auth is `POST https://secure.zwift.com/auth/realms/zwift/protocol/openid-connect/token`
with `client_id=Zwift Game Client`. Everything else is on `https://us-or-rly101.zwift.com`
and needs the game-client `Platform` / `Source` / `User-Agent` headers.

| Endpoint | Gives |
|---|---|
| `/api/profiles/me` | FTP, weight, level, total distance |
| `/api/private_event/feed`, `/api/private_event/{id}` | **Meetups** — scheduled private rides, with `routeId` |
| `/api/events/upcoming` | public events signed up for |
| `/api/event-feed` | public event catalogue (dups/skips across pages — one page only) |
| `/api/workout/schedule/list` | workouts pinned to calendar dates, i.e. an active plan |
| `/api/workout/workouts` | the whole library, ~900 workouts |
| `/api/workout/collections` | Zwift's curated collections and training plans |

`/api/events/{id}` and `/api/events/search` are **protobuf-only** and ignore
`Accept: application/json`. `zwift raw` prints the raw body rather than pretending.

### Zwift traps

- **The workout asset host needs the bearer token too.** Fetching `workoutAssetUrl`
  unauthenticated returns `403 RBAC: access denied`, not a 401.
- **`/api/workout/workouts/{id}` takes the `workoutId` uuid.** The `legacyId` integer
  sitting next to it in the same record 404s.
- **Run targets live in the `Power` attribute.** A `.zwo` stores a run's target as a
  fraction of threshold pace in the very same field a ride uses for FTP fraction. Emitting
  it without the `Pace` keyword silently puts a wattage target on a run. `zwo_to_intervals`
  adds `Pace` when `sportType` is `run`.
- **`FreeRide` steps have no target.** They convert to a bare duration, which
  intervals.icu accepts but a device export rejects (see the targets trap above).
- Workout `stressPoints` is Zwift's TSS equivalent — the useful number when slotting a
  Zwift session into a week intervals.icu is already tracking load for.
- **A meetup has no `name`.** The title the organiser typed is in `description`. `sport`
  is top-level, not nested under `eventSubgroup`.
- **`/api/private_event/feed` only returns *live* meetups.** Once one is over it drops out
  completely, whatever `start_date` is passed — the date params really are as buggy as
  sauce4zwift warns. A finished meetup is only reachable through
  `/api/notifications`, where a `PRIVATE_EVENT_INVITE` carries `{"eventId": ...}` as a
  **JSON string** inside `argString0`. `meetup_invites()` does that recovery.
- **`eventStart` is UTC** (trailing `Z`) while the athlete thinks in local time. Off-by-an-hour
  start times are a display bug, not a data one.
- **The activity list carries no `routeId`** — only `worldId`. The route name is embedded in
  the activity `name` ("Zwift - Road to Sky in Watopia"). Do not promise per-activity route
  data from that endpoint.

### The Companion planner is NOT on this API (searched — do not repeat)

Zwift Companion's **Planning** feature (schedule routes/workouts to specific days, launched
late April 2026) is **not reachable** from the API the game client uses. Its data does not
appear anywhere on `us-or-rly101.zwift.com`.

What `/api/queue` actually is: the older **"My List"** — an undated, 25-item queue that
Planning *replaced*. A stale `lastUpdatedAt` on it is the tell that nothing has written to
My List since Planning took over. Items scheduled in the planner never appear there.

Ruled out empirically, so don't re-run any of it:

- ~200 candidate paths (`/api/planning`, `/api/schedule`, `/api/calendar`, `/api/planner`,
  `-service` variants matching Zwift's newer `hvc-ingestion-service` / `actions-service`
  convention, profile-scoped `/api/profiles/{id}/…`) — all 404.
- Client identity: `Source: Companion` with iOS and Android `Platform` headers — identical
  results to Game Client.
- `Zwift-Api-Version` 2.7 / 3.0 / 4.0 — no change.
- OAuth scope: `Zwift_Mobile_Link` is a **valid second client_id** (longer-lived token than
  `Zwift Game Client`), but it unlocks nothing extra here. Both 404 the same paths.
- Hosts: `api.zwift.com`, `my.zwift.com`, regional `rly` variants — do not resolve or 301.
  `us-or-rly101.zwift.com` is the only API host.

Zwift Insider's launch coverage matches: there is no sign of the schedule surfacing on the
game client, the website, or third-party platforms. Planning is a Companion-only backend.

To actually get it, the endpoint must be captured from the Companion app itself (proxy the
phone, or mine endpoint strings from the APK). Until then, treat Zwift-side scheduling as
**write-only from intervals.icu** — plan there, let the connection push workouts across.
Note the planner accepts *routes*, which the intervals.icu connection cannot send.

### `/api/game_info` is the real route catalogue

`GET /api/game_info` with header `Zwift-Api-Version: 2.7` returns ~600 KB containing
Zwift's **own** route catalogue under `maps[].routes[]` — **393 routes**, more than any
vendored copy, always current, and exactly the numbers the Companion app displays.
`live_routes()` normalises it onto the vendored field names and caches it to
`data/.zwift-game-info.json`.

Per route: `distanceInMeters`, `ascentInMeters`, `xp`, `difficulty`, `levelLocked`,
`supportedLaps`, `publicEventsOnly`, and all four lead-in variants
(`leadin…` for events, `freeRideLeadin…`, `meetupLeadin…`, `defaultLeadin…`). It has **no
segment list** — that only exists in the vendored file.

- **`duration` on a route is a 0–4 bucket, not minutes.** Buckets overlap heavily on
  distance; do not read it as a time.
- **Zwift's "15 min" estimate is not stored anywhere.** The app computes it client-side as
  route distance ÷ the rider's own recent average speed, so it ignores the power you plan
  to hold. Verified by dividing a route's distance by the rider's median speed over recent
  rides and landing on the figure the Companion app displays.
- `/api/routes` and `/api/routes/{id}` return **403**, not 404 — they exist but are closed
  to both known client ids.

### Estimating ride duration

`estimate_duration()` solves a standard power/speed equation for the flat case, then scales
it by a correction **fitted at runtime to the athlete's own rides** — nothing athlete-specific
is hardcoded, and it is cached in `data/.zwift-speed-model.json`.

The correction matters: flat physics alone predicted a mountain route ~39% too fast. The
fitted form is `time = flat_time × (a + k × ascent_per_km)`, which on a ~70-ride history
brought all rides inside 15% and the large majority inside 10%.

When fitting, **filter to `sport == "CYCLING"`** — runs share the activity feed and their
speed has no relation to watts. One 10 km run skewed an early fit by 61%.

### Activity feed traps

- **`limit` above 30 is rejected** with `{"message":"limit.too.large"}`. Page with `start`.
- **`duration` is a display string** (`"1:38"`). Use `movingTimeInMs` for arithmetic.
- Useful per ride: `distanceInMeters`, `totalElevation`, `avgWatts`, `sport`, `worldId`.

### Route data

`reference/zwift-routes.json` holds 335 routes keyed by Zwift's own `routeId`, which is
what events, meetups and player state return. Distances are km, elevation m. Lead-in
differs between events, free ride and meetups — pick the matching one, and remember the
lead-in is part of what lands in the recorded activity.

Vendored from [andipaetzold/zwift-data](https://github.com/andipaetzold/zwift-data) (MIT).
To refresh, fetch `src/routes.ts`, strip the TS `import` line, the
`: ReadonlyArray<Route>` annotation and `as const`, then run it through node printing
`JSON.stringify(routes)`.

`zwift route <query>` matches on name, slug, world **and segment**, because athletes name
climbs rather than routes — "alpe" finds Road to Sky.

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

- **Formatting is black**, configured in `pyproject.toml` at `line-length = 100` (the
  codebase predates the tool and was already written to ~100). Run `uv run black src/`
  before committing. Black is a formatter, not a linter — `uvx ruff check src/` is the
  linter, and it is deliberately unconfigured, so treat its default-ruleset output as
  advisory rather than a gate.

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
