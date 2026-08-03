# TrainingAgent

Tooling for analysing endurance training (cycling, running, split boarding, weights)
with intervals.icu as the source of truth.

Data flow today: Wahoo Elemnt / Apple Watch / Zwift → Strava → intervals.icu.

## Setup

```bash
uv sync
cp .env.example .env   # then paste your API key
```

Get the API key from <https://intervals.icu/settings> → **Developer Settings**.
Leave `INTERVALS_ATHLETE_ID=0` — intervals.icu treats `0` as "the athlete this key belongs to".

## Commands

```bash
uv run training-agent ping                  # verify auth, show athlete + rate limit
uv run training-agent zones                 # thresholds and training zones per sport
uv run training-agent activities --days 30  # recent activities
uv run training-agent wellness --days 14    # weight, RHR, HRV, sleep, CTL/ATL/form
uv run training-agent raw /athlete/@me/profile -p key=value   # any endpoint
uv run training-agent report data/performance-reports/2026-07-29-baseline-assessment.md
```

`raw` is the exploration escape hatch — `@me` expands to the configured athlete id.

## Data availability (important)

Activities that reach intervals.icu **through the Strava connection are not readable
via the API** — Strava's API terms forbid intervals.icu from re-exposing them. They come
back as a stub with `"_note": "STRAVA activities are not available via the API"`.
They are still visible in the intervals.icu web UI; only the API is blocked.

Anything that reaches intervals.icu another way is fully readable:

| Source | API readable |
| --- | --- |
| `WAHOO`, `ZWIFT` (direct connections) | yes |
| `UPLOAD` (file upload / bulk history import) | yes |
| `MANUAL` (entered by hand) | yes |
| `STRAVA` (via the Strava connection) | **no** |

Practical consequence: connect devices directly to intervals.icu rather than relying on
Strava as the relay. `training-agent activities` flags unreadable rows so gaps are never
silently treated as rest days.

## API notes

- Base URL `https://intervals.icu/api/v1`, HTTP Basic auth: user `API_KEY`, password = your key.
- Rate limits: 5000 requests/day, 2500 per rolling 15 min, 10/sec per IP.
- Dates are local ISO-8601 (`2026-07-29` or `2026-07-29T16:18:49`).
- Full spec: [`reference/intervals-openapi.json`](reference/intervals-openapi.json),
  live docs at <https://intervals.icu/api/v1/docs/swagger-ui/index.html>.

### Endpoints worth knowing

| Purpose | Endpoint |
| --- | --- |
| Athlete + sport settings | `GET /athlete/{id}` |
| Zones, FTP, LTHR, threshold pace | `GET /athlete/{id}/sport-settings` |
| Activity list for a date range | `GET /athlete/{id}/activities?oldest=&newest=` |
| Single activity (with intervals) | `GET /activity/{id}?intervals=true` |
| Per-second streams | `GET /activity/{id}/streams` |
| Power / HR / pace curves | `GET /athlete/{id}/power-curves`, `.../hr-curves`, `.../pace-curves` |
| Daily wellness incl. CTL/ATL | `GET /athlete/{id}/wellness?oldest=&newest=` |
| Calendar (planned workouts, races) | `GET /athlete/{id}/events?oldest=&newest=` |
| Write a planned workout | `POST /athlete/{id}/events` |
| Workout library | `GET /athlete/{id}/workouts` |

## Performance reports

Analysis lives as markdown in `data/performance-reports/`, named
`YYYY-MM-DD-title.md`. The file is the source of truth; intervals.icu holds a synced
copy so it is available online and on the phone.

```bash
uv run training-agent report data/performance-reports/2026-07-29-baseline-assessment.md
```

This publishes the file as a dated `NOTE` event tagged `performance-report`. Date and
title are taken from the filename. Re-running replaces the existing note for that
date and title rather than stacking duplicates, so a report can be revised in place.
Notes round-trip markdown exactly — a 10 KB report stored and returned byte-identical.

Two other places intervals.icu can hold analysis:

| Where | Endpoint | Use for |
| --- | --- | --- |
| Dated calendar note | `POST /athlete/{id}/events` with `category: NOTE` | Reports, weekly reviews |
| Comment on an activity | `POST /activity/{id}/messages` | Race analysis tied to the ride/run itself |
| Reusable content item | `POST /athlete/{id}/custom-item` | Templates, checklists |

Attaching race analysis to the activity is worth doing alongside the report — it shows
up when reviewing that ride in the intervals.icu UI.

## Structured workouts

Calendar events become structured workouts when the `description` uses intervals.icu's
step syntax. It parses into `workout_doc` and intervals.icu recomputes duration, distance
and load itself — so do **not** set `moving_time`/`distance` by hand (an empty
`workout_doc` overwrites `distance` with 0; use `distance_target` for a route distance).

```
Warmup
- 10m 6:00/km Pace

8x
- 1km 4:30/km Pace
- 90s 6:30/km Pace

Cooldown
- 5m 6:00/km Pace
```

Syntax rules learned by testing the API:

- `m` means **minutes**, not metres — use `km` / `mtr` for distance.
- A pace step needs the explicit `Pace` keyword: `1km 4:30/km Pace`. Without it the
  pace is ignored and the step falls back to a default duration.
- Repeats need a **blank line** before `8x`, otherwise the group is not formed and the
  steps are emitted flat.
- Ranges work: `5:40-6:10/km Pace` → `{start: 340, end: 370}`.
- Trailing prose after the steps is kept and does not break parsing.
- Power steps use `%` of FTP (`- 2h 55-72%`), HR steps use `Z1 HR`.

### Gotcha: targets need a threshold configured

A workout only exports to devices if every step resolves to a concrete target. Fetch
events with `resolve=true` and check each leaf step has `_pace` / `_hr` / `_power`.

If the sport's threshold is missing, intervals.icu silently falls back to
`target: POWER` and emits steps with no values. Wahoo then rejects the push with:

```
422 Plan validation error: each interval that is not of type 'repeat'
    must have a valid 'targets' array
```

Running pace workouts therefore require `threshold_pace` (m/s) on the Run
sport-settings entry — set it via `PUT /athlete/{id}/sport-settings/{id}`.

## Layout

```
src/training_agent/
  config.py      environment / secrets
  intervals.py   API client
  cli.py         commands
reference/       OpenAPI spec
data/            downloaded data and training notes (.md)
```
