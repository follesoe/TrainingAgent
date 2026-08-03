---
name: activity-analysis
description: Deep-dive a single ride or run from its per-second streams - pacing, decoupling, zone distribution, course features, climbs and turns. Use when the user asks about a specific race or session, "how did I pace that", "why did I fade", "analyse that ride", or when a result needs explaining.
---

# Activity analysis

Analyse one activity from its streams rather than its summary fields. The summary hides
what matters.

## Get the streams

```python
raw = {s["type"]: s for s in client.activity_streams(activity_id)}
watts = raw["watts"]["data"]; hr = raw["heartrate"]["data"]
```

Typical streams: `time watts heartrate cadence distance altitude latlng
velocity_smooth temp torque`. Runs add `stance_time vertical_oscillation step_length`.
`fixed_altitude` is intervals.icu's corrected elevation — prefer it when present.

**Two traps.** `latlng` splits across `data` (latitude) and `data2` (longitude).
Distance and altitude streams have `None` padding at both ends — `x or 0` turns those
into zeros and yields negative deltas. Skip to the nearest non-null value instead.

## Pacing

Split into quarters and compare. Look for a hot start that bought nothing:

```
seg  km    climb   avg W   NP    IF     HR
Q1   31.5  566 m     218   263   0.94   166
Q2   33.1  488 m     183   224   0.80   159
Q3   31.7  440 m     164   201   0.72   159
Q4   31.5  409 m     165   202   0.72   157
```

Always pull elevation per segment too — front-loaded climbing legitimately raises early
power, and you cannot separate pacing error from terrain without it. In the example
above Q2 was the *fastest* quarter at 35 W less than Q1, which is what proves the Q1
effort was wasted.

## Decoupling

Power-to-heart-rate ratio, first half versus second:

```
1st half   200 W @ HR 162   ratio 1.234
2nd half   164 W @ HR 158   ratio 1.039
                            -> 15.8%
```

Above ~5% means the aerobic system is giving way. Note intervals.icu's own `decoupling`
field uses a different method and will differ from a simple halves calculation — report
both rather than picking one.

Negative decoupling on a long easy ride is a good sign, and worth distinguishing from
decoupling at tempo. "Poor durability" and "poor durability above tempo" are different
diagnoses with different training implications.

## Course features before conclusions

**Check the GPS before attributing a slow section to fatigue.** Bin pace, gradient and
cumulative heading change every 250 m, then zoom to 50 m on anything anomalous.

- A short steep hill shows as a gradient spike and costs a few seconds.
- A hairpin shows as >1000° of cumulative heading change inside 50 m and can cost 20+
  seconds while looking flat.
- A 400 m track shows as ~560° of turning per 200 m inside a small bounding box.

To isolate genuine fade on a lapped course, compare **matched positions between laps**
with the course features excluded. On one race this reduced apparent fade from 23 s/km
to 6 s/km — the rest was a turnaround.

Athletes reliably misattribute this. Hills are felt and remembered; turns quietly take
the time. Present the measured cost of each rather than deferring to either account.

## Also worth computing

- **Mean-max curve** for 1/5/10/20/60 min, as % of FTP and W/kg.
- **Time in zone**, including time at 0 W. High coasting plus high VI is a surge-and-
  coast pattern that is expensive over long events.
- **Detected intervals** via `client.activity_intervals(id)` — intervals.icu classifies
  WORK/RECOVERY with per-interval power and HR.
