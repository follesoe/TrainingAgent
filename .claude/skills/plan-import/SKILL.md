---
name: plan-import
description: Import a structured training plan (PDF, image or text) into the intervals.icu calendar as structured workouts with pace or power targets, anchored to the right dates and adjusted around existing commitments. Use when the user provides a training program, asks to schedule a plan or a block, or wants planned workouts added to their calendar.
---

# Training plan import

Turn a written plan into structured calendar workouts that export to devices.

## Anchor the plan to real dates

Read the plan and establish the day-number-to-date mapping from something the athlete
has already done — "I did day 16 yesterday" is enough.

```python
ANCHOR_DAY, ANCHOR_DATE = 16, dt.date(2026, 7, 28)
date_for = lambda day: ANCHOR_DATE + dt.timedelta(days=day - ANCHOR_DAY)
```

**Sanity-check the anchor.** Confirm the weekday matches the plan's own structure, and
check whether the computed race day lands on a race already in the calendar. On one
import, day 56 resolved exactly onto an existing 10k event — strong confirmation the
anchoring was right. Also verify the named session matches what they actually did.

Do not recreate events that already exist. Check the calendar first.

## Calibrate paces to the athlete

Take easy-run pace from their recent Z1 activities rather than assuming. Interval paces
come from the plan. Check the plan's target paces against the athlete's actual threshold
— if the plan is soft or aggressive for them, say so rather than importing silently.

## Build structured workouts, not descriptions

Put step syntax in the `description` and let intervals.icu compute duration and
distance. See `CLAUDE.md` for the full syntax rules — the ones that bite are: `m` means
minutes, pace steps need the literal `Pace` keyword, and repeats need a blank line
before `8x`.

```
Warmup
- 10m 6:00/km Pace

8x
- 1km 4:30/km Pace
- 90s 6:30/km Pace

Cooldown
- 5m 6:00/km Pace

Coaching note in the athlete's own language.
```

Set `target: "PACE"` (or `POWER`), and `indoor: true` for treadmill or turbo sessions.
Never set `moving_time`/`distance` by hand — an empty `workout_doc` zeroes the distance.

Use `client.create_events(list)` for bulk import.

**Verify targets resolve.** Fetch back with `resolve=true` and confirm every leaf step
has `_pace`/`_hr`/`_power`. If not, the sport is missing a threshold and device export
will fail — see `CLAUDE.md`.

## Fit the plan around real life

The athlete's actual week takes priority over the plan's ideal week. When they have a
big ride, a trip or a race in the block:

- Protect the **quality sessions**; drop easy runs first — other aerobic work replaces
  that volume.
- Leave a rest or travel day before a big event rather than stacking intensity into it.
- Flag conflicts you did not resolve. A long run the morning after a gravel race is
  worth naming even if you leave both in.

State every adjustment you make and why, so the athlete can overrule it. Deviating from
the plan is their call; surfacing the trade-off is yours.

## Races

Create with `category: "RACE_A"` (or `RACE_B`/`RACE_C`), and set distance and duration
from the athlete's previous running of the same event where one exists.
