---
name: performance-report
description: Produce a full training assessment for the athlete - season shape, fitness trend, race analysis and where they stand against their goals - as a markdown report published to intervals.icu. Use when the user asks "how am I doing", "assess my training", "where do I stand", asks for a baseline or periodic review, or wants a report on a training block.
---

# Performance report

Produces a dated markdown report in `data/performance-reports/YYYY-MM-DD-title.md` and
publishes it to intervals.icu as a calendar NOTE.

## Before analysing: clean the data

Run `training-agent check --days 400` first. Duplicated sessions inflate monthly load
and CTL, and a report built on them will be wrong. This has happened: an early version
of this project overstated March–May load by up to 12% and put peak CTL 5.7 points too
high because of duplicate splitboard days.

Also note how many activities are Strava-blocked. If a chunk of a period is unreadable,
say so — do not present a partial picture as complete.

## Gather

Work from the athlete's own data, not assumptions.

```python
client.activities(oldest=...)            # season shape by month and sport
client.wellness(oldest=...)              # CTL/ATL/form, RHR, HRV, sleep
client.sport_settings()                  # FTP, LTHR, threshold pace, zones
client.activity_streams(id)              # per-second data for key sessions
```

eFTP progression is in `wellness[].sportInfo[].eftp` — a much better fitness signal than
CTL, because it reflects what the training achieved rather than how much of it there was.

## Analyse

Structure the report around these, in roughly this order:

1. **Headline** — the single most important finding, stated plainly.
2. **Key races** — one section each, with pacing and decoupling (see `activity-analysis`).
3. **Current fitness** — CTL/ATL/form, eFTP, thresholds. Note the *pre-race* form when
   assessing a taper: race-day form includes the race's own load and reads misleadingly
   negative.
4. **Season shape** — monthly table of hours and load split by sport. Look for whether
   periodisation actually happened and whether any sport has quietly dropped out.
5. **Where they stand against their goals** — be concrete and arithmetic. For a time
   goal, work out the required speed/power and say honestly whether it is realistic.
6. **Recommendations** — ordered by expected return, with the reasoning visible.

### The most useful single diagnostic

Compare short and long duration power (or pace) from the same period:

```
            1min   5min   10min   20min   60min
race         383    320     292     261     231
recent       393    340     317     267     221
change       +10    +20     +25      +6     -10
```

A widening gap between 20-minute and 60-minute power means top-end has outrun aerobic
durability. In a well-developed endurance athlete that gap is ~10%; 17% is a problem for
any event over two hours. This is usually more actionable than any single FTP number.

## Write it

- Lead with the finding, not the methodology.
- Every claim carries its number. "Decoupling 15.8%" not "faded badly".
- Separate what the data shows from what it implies, and name the caveats — different
  measurement methods, missing power meter, course features, small samples.
- If revising an earlier report, add a visible **data-correction note** rather than
  silently changing figures.

## Publish

```bash
uv run training-agent report data/performance-reports/2026-07-29-baseline-assessment.md
```

Date and title come from the filename. Re-running replaces that date's note rather than
creating a duplicate. Consider also attaching race-specific analysis to the activity
itself with `POST /activity/{id}/messages` so it surfaces when reviewing that ride.
