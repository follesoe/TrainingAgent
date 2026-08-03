---
name: data-hygiene
description: Audit the intervals.icu calendar for duplicate sessions, plan placeholders, unlinked workouts and Strava-blocked activities, then resolve them safely. Use after a bulk sync or app change, when training load or CTL looks wrong, when the same session appears twice, or before producing any analysis that depends on load.
---

# Data hygiene

Training load silently inflates in ways that are invisible unless checked. Run this
before any analysis that depends on load, CTL or form.

```bash
uv run training-agent check --days 400
```

Read-only. It reports; it never deletes.

## What inflates load

**Duplicate sessions.** The same workout recorded twice — a Zwift ride plus an Apple
Watch copy, or a phone app plus a watch. Both carry load, so the day counts double.
Detected by *time overlap*, not exact start time: copies typically start seconds apart,
so an exact-match scan misses most of them. One audit found 21 duplicate groups and
~491 double-counted load that an exact-start-time scan had reduced to 3.

**Plan placeholders.** Marking a planned workout "done" materialises the plan as a
`MANUAL` activity next to the real recording. Signature: `MANUAL` source, a
`paired_event_id`, no distance, and load exactly equal to the plan. Tell the athlete not
to use "mark as done" — just let the upload land and intervals.icu matches it.

## Confirming an inflation

Compare the day's activity load against the wellness record:

```python
sum(a["icu_training_load"] or 0 for a in activities_that_day)  # vs
wellness[date]["ctlLoad"]
```

If `ctlLoad` equals the sum and there is more than one copy, it is double-counted. Note
intervals.icu sometimes already excludes one copy — check rather than assume.

## Resolving duplicates

Never delete on a blunt rule. Rank the copies:

1. **Carries training load** — a copy with no load is always the redundant one.
2. **Has a real name** over a generic one (`Snowboarding`, `Tennis`, `Other Workout`,
   `Slopes - a day ...`).
3. **Greater distance**, then more streams.

This resolves differently across periods, which is why it must be checked rather than
assumed: where the export copies had no load, keep the app copy; where the athlete's own
named upload already carried load, keep that.

**Before deleting, carry over anything the doomed copy holds uniquely** — usually a place
name. `PUT /activity/{id}` with `{"name": ...}`.

Always produce a **dry-run listing** showing keep/delete per group and get confirmation
before applying. This is the athlete's training history.

## Afterwards

Recalculation is **asynchronous**. The first read after deleting may return stale CTL —
poll until `ctlLoad` matches the sum of activity load. On one cleanup the CTL peak read
unchanged immediately after deletion and only settled seconds later, which nearly
produced a wrong conclusion.

Then re-check: any analysis already written from the old numbers needs correcting, with
a visible note rather than a silent edit.

## Strava-blocked activities

Not a duplicate, but a gap — see `CLAUDE.md`. They carry load and count toward CTL while
being unreadable. Recover the important ones by exporting the original file from Strava
and running `training-agent upload`. Prioritise by the load they carry; a 196-load
mountain day is worth recovering, a 4-load walk is not.
