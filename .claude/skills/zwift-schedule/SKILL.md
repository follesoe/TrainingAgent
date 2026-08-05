---
name: zwift-schedule
description: Schedule a Zwift route as a planned ride on the intervals.icu calendar, translating a zone or effort description into a wattage from the athlete's indoor zones and sizing the session against the rest of the week. Use when the athlete says they plan to ride a named Zwift route, asks to put a route on the calendar, or wants a Zwift session scheduled for a given day.
---

# Schedule a Zwift route

The athlete names a route and an effort — "tomorrow I'll do Crêpe Escape, Z1/Z2" — and it
lands on the intervals.icu calendar with the right distance, duration and load.

`training-agent zwift schedule` does the mechanics. The judgment below is the part worth
getting right.

## Resolve the route first

```bash
uv run training-agent zwift route "crepe escape"
```

Exact name matches win; a substring that hits many routes lists candidates instead of
guessing. Athletes name climbs rather than routes, so "alpe" finds Road to Sky.

Distance and elevation come from Zwift's own catalogue and match what the Companion app
shows. Quote the numbers back — a route being 30 km rather than the 15 km they pictured
changes the plan.

## Translate the effort into watts — from the INDOOR zones

This is the step that bites. The cycling sport-settings entry carries **both** `ftp` and
`indoor_ftp`, and they differ. Every Zwift ride is indoor, so read `indoor_ftp`:

```bash
uv run training-agent zones      # or read sport_settings() directly
```

Take the zone boundaries as percentages of **indoor** FTP, pick a wattage inside the
named zone, and say which number you chose and why. Mid-zone is the safe read of a bare
"Z1"; a boundary like "Z1/Z2" sits at the top of Z1 / bottom of Z2.

Using outdoor FTP silently under-states the intensity. Verify afterwards (below) that the
target resolved to the watts you intended.

**Check the two platforms agree.** The Zwift profile carries its own FTP, and the
intervals.icu connection does not sync it. If they differ, say so — anything pushed to
Zwift will be scaled against Zwift's number, not this one.

## Read the calendar before writing to it

```bash
uv run training-agent zwift schedule "<route>" --date tomorrow --watts <W> --dry-run
```

Always dry-run first, and look at what is already on that day and the days around it:

- If the ride follows another session that day, order it with `--time HH:MM`.
- Sum the day's load. An easy Zwift spin on top of a long run is a different day than it
  looks in isolation.
- Look ahead for the next quality session. Adding volume the day before a threshold test
  or a race is worth naming even if you leave it in — the athlete decides.

Re-running for the same route and date **replaces** that entry rather than stacking a
duplicate, so revising an effort is safe.

## Verify it landed

```python
events = client.events(oldest=date, newest=date, resolve=True)
```

Confirm `distance` and `moving_time` are populated and the step's `_power` matches the
intended watts. If `distance` is 0, the description lost its distance step — see the
`workout_doc` trap in `CLAUDE.md`.

intervals.icu computes the duration itself from distance and the power target, so it is
**not elevation-aware**. On a flat route it lands within ~1% of the calibrated estimate;
on a mountain route it reads optimistic. Pass `--duration` when the route climbs hard and
the time matters.

## What this cannot do

Zwift's own Companion planner is not reachable from any API, so the plan lives on
intervals.icu only. The intervals.icu → Zwift connection pushes **workouts**, not routes,
so selecting the route in Zwift stays a manual step. Say this rather than implying the
ride will appear in Zwift.
