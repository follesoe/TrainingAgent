"""Command line entry points for exploring intervals.icu data."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import audit, zwift as zwift_api
from .config import ConfigError, Settings, ZwiftSettings
from .intervals import IntervalsAPIError, IntervalsClient
from .zwift import ZwiftAPIError, ZwiftClient

app = typer.Typer(add_completion=False, help="Endurance training agent tools.")
zwift_app = typer.Typer(add_completion=False, help="Zwift routes, meetups and workouts.")
app.add_typer(zwift_app, name="zwift")
console = Console()


def _client() -> IntervalsClient:
    try:
        return IntervalsClient(Settings.load())
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


def _fail(exc: IntervalsAPIError) -> None:
    if exc.status in (401, 403):
        console.print(
            "[red]Authentication failed.[/red] Check INTERVALS_API_KEY in .env "
            "(https://intervals.icu/settings -> Developer Settings)."
        )
    else:
        console.print(f"[red]{exc}[/red]")
    raise typer.Exit(code=1)


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "-"
    total = int(seconds)
    return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


@app.command()
def ping() -> None:
    """Verify the API key works and show who we are connected as."""
    client = _client()
    try:
        athlete = client.athlete()
        settings = client.sport_settings()
    except IntervalsAPIError as exc:
        _fail(exc)

    console.print("[green]Connected to intervals.icu[/green]")

    table = Table(show_header=False, box=None)
    table.add_row("Athlete", f"{athlete.get('name')} ({athlete.get('id')})")
    table.add_row("Location", f"{athlete.get('city') or '-'}, {athlete.get('country') or '-'}")
    table.add_row("Timezone", str(athlete.get("timezone")))
    table.add_row("Weight", f"{athlete.get('icu_weight') or '-'} kg")
    table.add_row("Sport settings", f"{len(settings)} profiles")
    if client.last_rate_limit:
        table.add_row(
            "Rate limit",
            f"{client.last_rate_limit['remaining']} of "
            f"{client.last_rate_limit['limit']} remaining (15min,day)",
        )
    console.print(table)
    client.close()


@app.command()
def zones() -> None:
    """Show training zones and thresholds per sport."""
    client = _client()
    try:
        settings = client.sport_settings()
    except IntervalsAPIError as exc:
        _fail(exc)

    for sport in settings:
        types = ", ".join(sport.get("types") or []) or "default"
        console.print(f"\n[bold cyan]{types}[/bold cyan]")

        facts = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        for label, key, unit in [
            ("FTP", "ftp", "W"),
            ("W'", "w_prime", "J"),
            ("LTHR", "lthr", "bpm"),
            ("Max HR", "max_hr", "bpm"),
            ("Threshold pace", "threshold_pace", "m/s"),
        ]:
            if sport.get(key) is not None:
                facts.add_row(label, f"{sport[key]} {unit}")
        console.print(facts)

        for zone_key, name_key, label in [
            ("power_zones", "power_zone_names", "Power zones (% FTP)"),
            ("hr_zones", "hr_zone_names", "HR zones (bpm)"),
            ("pace_zones", "pace_zone_names", "Pace zones (% threshold)"),
        ]:
            values = sport.get(zone_key)
            if not values:
                continue
            names = sport.get(name_key) or [f"Z{i + 1}" for i in range(len(values))]
            zt = Table(title=label, title_justify="left", title_style="dim")
            zt.add_column("Zone")
            zt.add_column("Upper", justify="right")
            for name, value in zip(names, values, strict=False):
                zt.add_row(str(name), str(value))
            console.print(zt)

    client.close()


@app.command()
def activities(
    days: int = typer.Option(30, help="How many days back to list."),
    limit: int = typer.Option(20, help="Max activities to show."),
) -> None:
    """List recent activities."""
    client = _client()
    oldest = dt.date.today() - dt.timedelta(days=days)
    try:
        items = client.activities(oldest=oldest, limit=limit)
    except IntervalsAPIError as exc:
        _fail(exc)

    table = Table(title=f"Activities since {oldest}")
    table.add_column("Date", no_wrap=True)
    table.add_column("Src", no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("Name", no_wrap=True, max_width=30)
    for col in ("Time", "km", "TSS", "Avg W", "Avg HR"):
        table.add_column(col, justify="right", no_wrap=True)

    for a in items:
        # Activities that reached intervals.icu through the Strava connection are
        # returned as a stub - Strava's terms forbid re-exposing them via the API.
        if a.get("_note"):
            table.add_row(
                str(a.get("start_date_local", ""))[:10],
                "[yellow]strava[/yellow]",
                "[dim]?[/dim]",
                "[dim]not readable via API[/dim]",
                *([""] * 5),
            )
            continue

        distance = a.get("distance")
        table.add_row(
            str(a.get("start_date_local", ""))[:10],
            str(a.get("source") or "-").lower(),
            str(a.get("type") or "-"),
            (a.get("name") or "-")[:30],
            _fmt_duration(a.get("moving_time")),
            f"{distance / 1000:.1f}" if distance else "-",
            str(round(a["icu_training_load"]) if a.get("icu_training_load") else "-"),
            str(a.get("icu_average_watts") or "-"),
            str(a.get("average_heartrate") or "-"),
        )
    console.print(table)

    blocked = sum(1 for a in items if a.get("_note"))
    summary = f"{len(items)} activities"
    if blocked:
        summary += f", [yellow]{blocked} unreadable (synced via Strava)[/yellow]"
    console.print(f"[dim]{summary}[/dim]")
    client.close()


@app.command()
def wellness(days: int = typer.Option(14, help="How many days back to show.")) -> None:
    """Show recent wellness records and fitness metrics."""
    client = _client()
    oldest = dt.date.today() - dt.timedelta(days=days)
    try:
        items = client.wellness(oldest=oldest)
    except IntervalsAPIError as exc:
        _fail(exc)

    table = Table(title=f"Wellness since {oldest}")
    for col in ("Date", "Weight", "RHR", "HRV", "Sleep", "CTL", "ATL", "Form"):
        table.add_column(col, justify="right" if col != "Date" else "left")

    for w in sorted(items, key=lambda x: x.get("id", "")):
        ctl, atl = w.get("ctl"), w.get("atl")
        sleep = w.get("sleepSecs")
        table.add_row(
            str(w.get("id")),
            f"{w['weight']:.1f}" if w.get("weight") else "-",
            str(w.get("restingHR") or "-"),
            str(w.get("hrv") or "-"),
            f"{sleep / 3600:.1f}h" if sleep else "-",
            f"{ctl:.0f}" if ctl is not None else "-",
            f"{atl:.0f}" if atl is not None else "-",
            f"{ctl - atl:+.0f}" if ctl is not None and atl is not None else "-",
        )
    console.print(table)
    client.close()


@app.command()
def upload(
    file: Path = typer.Argument(..., help="fit / tcx / gpx (or zip / gz) file to upload."),
    name: str = typer.Option(None, help="Activity name."),
    description: str = typer.Option(None, help="Activity description."),
    device_name: str = typer.Option(None, help="Device it was recorded on."),
    replaces: str = typer.Option(
        None,
        help="Activity id this file replaces (e.g. an unreadable Strava entry). "
        "Deleted only after the upload succeeds.",
    ),
) -> None:
    """Upload an activity file. Makes Strava-only sessions readable via the API."""
    if not file.exists():
        console.print(f"[red]No such file: {file}[/red]")
        raise typer.Exit(code=1)

    client = _client()
    try:
        status, body = client.upload_activity(
            file, name=name, description=description, device_name=device_name
        )
    except IntervalsAPIError as exc:
        _fail(exc)

    if status == 200:
        console.print(
            "[yellow]Already uploaded[/yellow] — intervals.icu de-dupes on file hash, "
            "so no new activity was created."
        )
        client.close()
        return

    created = body if isinstance(body, list) else [body]
    for item in created:
        if not isinstance(item, dict):
            continue
        console.print(
            f"[green]uploaded[/green] {item.get('id')}  {item.get('start_date_local')}  "
            f"{item.get('type')}  {(item.get('distance') or 0) / 1000:.1f} km  "
            f"load {item.get('icu_training_load')}"
        )

    if replaces:
        try:
            client.delete_activity(replaces)
            console.print(f"[green]deleted[/green] superseded activity {replaces}")
        except IntervalsAPIError as exc:
            console.print(f"[red]upload succeeded but delete failed:[/red] {exc}")

    console.print("[dim]Run 'training-agent check' to confirm no duplicate remains.[/dim]")
    client.close()


@app.command()
def check(days: int = typer.Option(90, help="How many days back to audit.")) -> None:
    """Audit the calendar for duplicates, plan placeholders and unlinked workouts.

    Read-only: it reports what it finds and never deletes anything.
    """
    client = _client()
    try:
        findings = audit.run(client, days=days)
    except IntervalsAPIError as exc:
        _fail(exc)
    client.close()

    if not findings:
        console.print(f"[green]No issues found[/green] in the last {days} days.")
        return

    headings = {
        "duplicate": ("red", "Duplicate sessions"),
        "placeholder": ("red", "Plan placeholders (marked done)"),
        "unpaired": ("yellow", "Planned workouts not linked to an activity"),
        "blocked": ("yellow", "Unreadable via the API"),
    }

    at_risk = 0
    for kind, (color, heading) in headings.items():
        group = [f for f in findings if f.kind == kind]
        if not group:
            continue
        console.print(f"\n[bold {color}]{heading}[/bold {color}] ({len(group)})")
        for finding in group:
            console.print(f"  {finding.date}  {finding.summary}")
            for line in finding.detail:
                console.print(f"    [dim]{line}[/dim]")
            at_risk += finding.load_at_risk

    console.print(f"\n[dim]{len(findings)} finding(s) over {days} days.[/dim]")
    if at_risk:
        console.print(
            f"[red]~{at_risk} training load double-counted[/red] — this inflates CTL/ATL "
            "and form."
        )


@app.command()
def report(
    file: Path = typer.Argument(..., help="Markdown report to publish."),
    date: str = typer.Option(
        None, help="Calendar date (YYYY-MM-DD). Default: from filename, else today."
    ),
    title: str = typer.Option(None, help="Note title. Default: derived from the filename."),
) -> None:
    """Publish a markdown performance report to intervals.icu as a dated NOTE.

    Re-publishing the same date and title replaces the existing note rather than
    stacking duplicates, so a report can be revised in place.
    """
    if not file.exists():
        console.print(f"[red]No such file: {file}[/red]")
        raise typer.Exit(code=1)

    body = file.read_text()

    # Filenames are conventionally 'YYYY-MM-DD-some-title.md'.
    stem = file.stem
    match = re.match(r"(\d{4}-\d{2}-\d{2})[-_]?(.*)", stem)
    if date is None:
        date = match.group(1) if match else dt.date.today().isoformat()
    if title is None:
        slug = (match.group(2) if match else stem) or "performance report"
        title = f"Performance report - {slug.replace('-', ' ').replace('_', ' ')}"

    client = _client()
    try:
        existing = [
            e
            for e in client.events(oldest=date, newest=date)
            if e.get("category") == "NOTE" and e.get("name") == title
        ]
        for old in existing:
            client.delete_event(old["id"])

        created = client.create_event(
            {
                "start_date_local": f"{date}T00:00:00",
                "category": "NOTE",
                "name": title,
                "description": body,
                "color": "blue",
                "tags": ["performance-report"],
            }
        )
    except IntervalsAPIError as exc:
        _fail(exc)

    verb = "replaced" if existing else "published"
    console.print(
        f"[green]{verb}[/green] {title!r} on {date} "
        f"({len(body)} chars, event {created.get('id')})"
    )
    client.close()


@app.command()
def raw(
    path: str = typer.Argument(..., help="API path after /api/v1, e.g. /athlete/0/profile"),
    param: list[str] = typer.Option(
        [], "--param", "-p", help="Query param as key=value. Repeatable."
    ),
) -> None:
    """Call any endpoint and dump the JSON. Useful for exploring the API."""
    client = _client()
    params = dict(p.split("=", 1) for p in param)
    # "@me" is a convenience for the configured athlete id.
    path = path.replace("@me", client.athlete_id)
    try:
        console.print_json(json.dumps(client.get(path, **params)))
    except IntervalsAPIError as exc:
        _fail(exc)
    client.close()


# ---- Zwift ------------------------------------------------------------------
#
# Zwift has no personal API keys and no public developer programme, so these all
# run against the same unofficial API the game client uses. See zwift.py.


def _zwift() -> ZwiftClient:
    try:
        return ZwiftClient(ZwiftSettings.load())
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


def _zwift_fail(exc: ZwiftAPIError) -> None:
    if exc.status in (400, 401) and "auth" in exc.url:
        console.print(
            "[red]Zwift login failed.[/red] Check ZWIFT_EMAIL and ZWIFT_PASSWORD in .env.\n"
            "[dim]An account that signs in only via Apple/Google/Facebook has no password "
            "and cannot use this API - set one at https://www.zwift.com/settings/account.[/dim]"
        )
    elif exc.status == 401:
        console.print(
            "[red]Zwift rejected the token.[/red] Run 'training-agent zwift logout' and retry."
        )
    else:
        console.print(f"[red]{exc}[/red]")
    raise typer.Exit(code=1)


def _event_start(event: dict) -> str:
    """Zwift returns event starts in UTC. Show them in the athlete's local time."""
    raw = str(event.get("eventStart") or event.get("event_start") or "")
    if not raw:
        return "-"
    try:
        moment = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw.replace("T", " ")[:16]
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%Y-%m-%d %H:%M")


@zwift_app.command("ping")
def zwift_ping() -> None:
    """Verify the Zwift login works and show the profile."""
    client = _zwift()
    try:
        profile = client.profile()
    except ZwiftAPIError as exc:
        _zwift_fail(exc)

    console.print("[green]Connected to Zwift[/green]")
    table = Table(show_header=False, box=None)
    table.add_row("Rider", f"{profile.get('firstName')} {profile.get('lastName')}".strip())
    table.add_row("Athlete id", str(profile.get("id")))
    table.add_row("Level", str(profile.get("achievementLevel", 0) // 100 or "-"))
    ftp = profile.get("ftp")
    table.add_row("FTP", f"{ftp} W" if ftp else "-")
    weight = profile.get("weight")
    table.add_row("Weight", f"{weight / 1000:.1f} kg" if weight else "-")
    distance = profile.get("totalDistance")
    table.add_row("Total distance", f"{distance / 1000:,.0f} km" if distance else "-")
    console.print(table)
    console.print(
        "[dim]Token cached in data/.zwift-token.json - no re-login until it expires.[/dim]"
    )
    client.close()


@zwift_app.command("logout")
def zwift_logout() -> None:
    """Delete the cached Zwift token."""
    client = _zwift()
    client.logout()
    client.close()
    console.print("[green]Cached Zwift token removed.[/green]")


@zwift_app.command("meetups")
def zwift_meetups(days: int = typer.Option(30, help="How many days back to include.")) -> None:
    """List scheduled meetups - the private rides planned in the Companion app."""
    client = _zwift()
    since = dt.datetime.now() - dt.timedelta(days=days)
    try:
        items = client.meetups(since=since)
    except ZwiftAPIError as exc:
        _zwift_fail(exc)

    if not items:
        # The feed only carries live meetups - a finished one drops out no matter
        # what date range is asked for. The invite notification outlives it, so
        # fall back to that rather than claiming there were never any.
        console.print("[dim]No scheduled meetups on the feed.[/dim]")
        try:
            invites = client.meetup_invites()
        except ZwiftAPIError as exc:
            _zwift_fail(exc)
        if invites:
            console.print(
                f"\n[dim]{len(invites)} past invite(s) recovered from notifications:[/dim]"
            )
            past = Table(show_header=True)
            past.add_column("Start", no_wrap=True)
            past.add_column("From", no_wrap=True)
            past.add_column("Route", max_width=44)
            past.add_column("Id", no_wrap=True)
            for invite in invites:
                try:
                    detail = client.meetup(invite["event_id"])
                except ZwiftAPIError:
                    detail = {}
                past.add_row(
                    invite["start"].strftime("%Y-%m-%d %H:%M"),
                    invite["from"] or "-",
                    zwift_api.route_label(detail.get("routeId")),
                    str(invite["event_id"]),
                )
            console.print(past)
        client.close()
        return

    table = Table(title="Zwift meetups")
    table.add_column("Start", no_wrap=True)
    # A meetup has no name field - the title the organiser typed is `description`.
    table.add_column("Title", max_width=28)
    table.add_column("Sport", no_wrap=True)
    table.add_column("Route", max_width=40)
    table.add_column("Planned", justify="right", no_wrap=True)
    table.add_column("Id", no_wrap=True)

    for m in sorted(items, key=_event_start):
        laps = m.get("laps") or 0
        distance = m.get("distanceInMeters") or 0
        duration = m.get("durationInSeconds") or 0
        if distance:
            planned = f"{distance / 1000:.1f} km"
        elif duration:
            planned = _fmt_duration(duration)
        elif laps:
            planned = f"{laps} lap(s)"
        else:
            planned = "-"

        table.add_row(
            _event_start(m),
            (m.get("description") or "-")[:28],
            str(m.get("sport") or "-").lower(),
            zwift_api.route_label(m.get("routeId")),
            planned,
            str(m.get("id")),
        )
    console.print(table)
    console.print("[dim]Details: training-agent zwift raw /api/private_event/<id>[/dim]")
    client.close()


@zwift_app.command("events")
def zwift_events() -> None:
    """List public events (races, group rides) you have signed up for."""
    client = _zwift()
    try:
        items = client.upcoming_events()
    except ZwiftAPIError as exc:
        _zwift_fail(exc)

    if not items:
        console.print("[dim]No upcoming event signups.[/dim]")
        client.close()
        return

    table = Table(title="Signed up")
    table.add_column("Start", no_wrap=True)
    table.add_column("Event", max_width=34)
    table.add_column("Route", max_width=40)
    table.add_column("Id", no_wrap=True)
    for e in sorted(items, key=_event_start):
        table.add_row(
            _event_start(e),
            (e.get("name") or "-")[:34],
            zwift_api.route_label(e.get("routeId")),
            str(e.get("id")),
        )
    console.print(table)
    client.close()


@zwift_app.command("plan")
def zwift_plan() -> None:
    """Show workouts scheduled on the Zwift calendar (an active training plan)."""
    client = _zwift()
    try:
        schedule = client.workout_schedule()
    except ZwiftAPIError as exc:
        _zwift_fail(exc)

    # The shape here varies with whether a plan is active, so stay defensive.
    entries = schedule if isinstance(schedule, list) else (schedule or {}).get("data") or []
    if not entries:
        console.print(
            "[dim]Nothing scheduled. Zwift only populates this while a training plan is "
            "active or you have pinned workouts to dates in the Companion app.[/dim]"
        )
        console.print_json(json.dumps(schedule))
        client.close()
        return

    table = Table(title="Zwift workout schedule")
    table.add_column("Date", no_wrap=True)
    table.add_column("Workout", max_width=40)
    table.add_column("Workout id", no_wrap=True)
    for entry in entries:
        workout = entry.get("workout") or {}
        table.add_row(
            str(entry.get("scheduledDate") or entry.get("date") or "-")[:10],
            (workout.get("name") or entry.get("name") or "-")[:40],
            str(workout.get("id") or entry.get("workoutId") or "-"),
        )
    console.print(table)
    console.print("[dim]Steps: training-agent zwift workout <workout id>[/dim]")
    client.close()


@zwift_app.command("workouts")
def zwift_workouts(
    limit: int = typer.Option(30, help="Max workouts to show."),
    search: str = typer.Option(None, help="Filter by name, case-insensitive."),
) -> None:
    """List workouts in your Zwift library (~900, including Zwift's own)."""
    client = _zwift()
    try:
        items = client.workouts()
    except ZwiftAPIError as exc:
        _zwift_fail(exc)

    if search:
        needle = search.lower()
        items = [w for w in items if needle in (w.get("name") or "").lower()]

    table = Table(title=f"Zwift workouts ({len(items)} matching)")
    table.add_column("Name", max_width=40)
    table.add_column("Sport", no_wrap=True)
    table.add_column("Time", justify="right", no_wrap=True)
    # stressPoints is Zwift's TSS equivalent - the useful number when slotting a
    # workout into a week that intervals.icu is already tracking load for.
    table.add_column("TSS", justify="right", no_wrap=True)
    table.add_column("Workout id", no_wrap=True)
    for w in items[:limit]:
        table.add_row(
            (w.get("name") or "-")[:40],
            str(w.get("sport") or "-").lower(),
            _fmt_duration(w.get("duration")),
            str(w.get("stressPoints") or "-"),
            str(w.get("workoutId") or "-"),
        )
    console.print(table)
    if len(items) > limit:
        console.print(f"[dim]showing {limit} of {len(items)} - narrow with --search[/dim]")
    client.close()


@zwift_app.command("workout")
def zwift_workout(
    workout_id: str = typer.Argument(..., help="Workout uuid from 'zwift workouts'."),
    as_intervals: bool = typer.Option(
        False, "--as-intervals", help="Emit intervals.icu structured-workout syntax."
    ),
) -> None:
    """Show a Zwift workout's steps, optionally as intervals.icu syntax."""
    client = _zwift()
    try:
        detail = client.workout_steps(workout_id)
    except ZwiftAPIError as exc:
        _zwift_fail(exc)
    client.close()

    if as_intervals:
        # Plain print, not rich - this is meant to be piped or pasted verbatim
        # into an intervals.icu event description.
        print(zwift_api.zwo_to_intervals(detail))
        return

    console.print(f"[bold cyan]{detail.get('name')}[/bold cyan]  [dim]{detail.get('sport')}[/dim]")
    if detail.get("description"):
        console.print(f"[dim]{detail['description']}[/dim]")
    console.print()
    for line in zwift_api.zwo_summary(detail):
        console.print(f"  {line}")
    if zwift_api.is_run(detail):
        console.print(
            "\n[dim]Percentages are % of threshold pace (Zwift stores run targets in the "
            "same field rides use for power).[/dim]"
        )
    else:
        console.print("\n[dim]Percentages are % of FTP.[/dim]")
    console.print(
        "[dim]Re-run with --as-intervals for intervals.icu syntax to paste into an "
        "event description.[/dim]"
    )


@zwift_app.command("route")
def zwift_route(
    query: str = typer.Argument(..., help="Route name, slug, world or routeId.")
) -> None:
    """Look up route distance, elevation and lead-in. Offline - no login needed."""
    exact = zwift_api.route(query) if query.isdigit() else None
    matches = [exact] if exact else zwift_api.find_routes(query)

    if not matches:
        console.print(f"[yellow]No route matching {query!r}.[/yellow]")
        raise typer.Exit(code=1)

    if len(matches) > 1:
        table = Table(title=f"{len(matches)} routes matching {query!r}")
        table.add_column("Route", max_width=32)
        table.add_column("World", no_wrap=True)
        table.add_column("km", justify="right")
        table.add_column("m", justify="right")
        table.add_column("routeId", no_wrap=True)
        for r in matches:
            table.add_row(
                r["name"], r["world"], f"{r['distance']:.1f}", f"{r['elevation']:.0f}", str(r["id"])
            )
        console.print(table)
        return

    r = matches[0]
    console.print(f"[bold cyan]{r['name']}[/bold cyan]  [dim]{r['world']}[/dim]")
    table = Table(show_header=False, box=None)
    table.add_row("routeId", str(r["id"]))
    table.add_row("Distance", f"{r['distance']:.2f} km")
    table.add_row("Elevation", f"{r['elevation']:.0f} m")
    if r.get("leadInDistance"):
        table.add_row(
            "Lead-in (event)",
            f"{r['leadInDistance']:.2f} km / {r.get('leadInElevation') or 0:.0f} m",
        )
    if r.get("leadInDistanceFreeRide"):
        table.add_row(
            "Lead-in (free ride)",
            f"{r['leadInDistanceFreeRide']:.2f} km / {r.get('leadInElevationFreeRide') or 0:.0f} m",
        )
    if r.get("leadInDistanceMeetups"):
        table.add_row(
            "Lead-in (meetup)",
            f"{r['leadInDistanceMeetups']:.2f} km / {r.get('leadInElevationInMeetups') or 0:.0f} m",
        )
    # Total distance is what actually lands in the activity, lead-in included.
    total = r["distance"] + (r.get("leadInDistance") or 0)
    table.add_row("Total with lead-in", f"{total:.2f} km")
    table.add_row("Sports", ", ".join(r.get("sports") or []) or "-")
    table.add_row("Lap route", "yes" if r.get("lap") else "no")
    table.add_row("Event only", "yes" if r.get("eventOnly") else "no")
    table.add_row("Segments", ", ".join(r.get("segments") or []) or "-")
    if r.get("zwiftInsiderUrl"):
        table.add_row("Details", r["zwiftInsiderUrl"])
    console.print(table)

    climbs = r.get("segmentsOnRoute") or []
    if climbs:
        console.print("\n[bold]Segments in order[/bold]")
        for seg in climbs:
            console.print(f"  {seg['from']:>6.2f} - {seg['to']:>6.2f} km  {seg['segment']}")


def _parse_when(value: str) -> str:
    """Accept today / tomorrow / +N / YYYY-MM-DD and return an ISO date."""
    text = value.strip().lower()
    today = dt.date.today()
    if text in ("today", "t"):
        return today.isoformat()
    if text in ("tomorrow", "tmr"):
        return (today + dt.timedelta(days=1)).isoformat()
    if text.startswith("+") and text[1:].isdigit():
        return (today + dt.timedelta(days=int(text[1:]))).isoformat()
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise typer.BadParameter(
            f"{value!r} is not a date. Use YYYY-MM-DD, 'today', 'tomorrow' or '+3'."
        ) from exc


def _parse_duration(value: str) -> int:
    """'1h30m' / '90m' / '45' (minutes) / '1:30' -> seconds."""
    text = value.strip().lower()
    if ":" in text:
        parts = [int(p) for p in text.split(":")]
        return parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)
    total, number = 0, ""
    for char in text:
        if char.isdigit():
            number += char
        elif char in "hms" and number:
            total += int(number) * {"h": 3600, "m": 60, "s": 1}[char]
            number = ""
    if number:  # bare number means minutes
        total += int(number) * 60
    if not total:
        raise typer.BadParameter(f"{value!r} is not a duration. Try '1h30m', '90m' or '1:30'.")
    return total


@zwift_app.command("schedule")
def zwift_schedule(
    route_query: str = typer.Argument(..., help="Route name, slug or routeId."),
    date: str = typer.Option("tomorrow", "--date", "-d", help="YYYY-MM-DD, today, tomorrow, +3."),
    watts: int = typer.Option(..., "--watts", "-w", help="Average power you intend to hold."),
    duration: str = typer.Option(None, help="Override the estimate, e.g. '1h30m'."),
    at: str = typer.Option(
        None, "--time", help="Time of day HH:MM. Orders it against other entries that day."
    ),
    laps: int = typer.Option(1, help="How many times round the route."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan without writing it."),
) -> None:
    """Put a Zwift route on the intervals.icu calendar as a planned ride.

    Zwift's own planner is not reachable from any API (see CLAUDE.md), so the
    plan lives on intervals.icu, which is the source of truth for load anyway.
    """
    zclient = _zwift()
    iclient = _client()
    try:
        # Prefer Zwift's live catalogue; fall back to the vendored copy offline.
        try:
            catalogue = zwift_api.live_routes(zclient)
        except ZwiftAPIError:
            catalogue = list(zwift_api._routes())

        needle = route_query.strip().lower()
        if route_query.isdigit():
            matches = [r for r in catalogue if str(r.get("id")) == route_query]
        else:
            matches = [r for r in catalogue if needle == (r.get("name") or "").lower()]
            if not matches:
                matches = [r for r in catalogue if needle in (r.get("name") or "").lower()]

        if not matches:
            console.print(f"[red]No route matching {route_query!r}.[/red]")
            raise typer.Exit(code=1)
        if len(matches) > 1:
            console.print(
                f"[yellow]{len(matches)} routes match {route_query!r} - be specific:[/yellow]"
            )
            for r in matches[:12]:
                console.print(f"  {r['name']}  [dim]({r['world']}, id {r['id']})[/dim]")
            raise typer.Exit(code=1)

        route = matches[0]
        profile = zclient.profile()
        rider_kg = (profile.get("weight") or 0) / 1000

        # A solo route ride gets the free-ride lead-in, and it counts.
        lead_km = route.get("leadInDistanceFreeRide") or 0
        lead_m = route.get("leadInElevationFreeRide") or 0
        total_km = route["distance"] * laps + lead_km
        total_m = route["elevation"] * laps + lead_m

        model = zwift_api.cached_speed_model()
        if model is None:
            model = zwift_api.calibrate_speed_model(zclient)

        if duration:
            seconds = _parse_duration(duration)
            source = "given"
        else:
            seconds = zwift_api.estimate_duration(watts, total_km, total_m, rider_kg, model)
            source = (
                f"estimated, model fitted on {model['n']} of your rides "
                f"(median error {model['median_error_pct']:.0f}%)"
                if model
                else "estimated, flat physics only - too few rides to calibrate"
            )

        # FTP comes from intervals.icu, which owns thresholds for this athlete.
        # A Zwift ride is always indoor, and indoor FTP is usually the lower of
        # the two - using the outdoor number would understate the intensity.
        cycling = (
            next(
                (s for s in iclient.sport_settings() if "VirtualRide" in (s.get("types") or [])),
                None,
            )
            or {}
        )
        ftp = cycling.get("indoor_ftp") or cycling.get("ftp")
        ftp_label = "indoor FTP" if cycling.get("indoor_ftp") else "FTP"
        load = zwift_api.training_load(watts, seconds, ftp) if ftp else 0

        name = f"Zwift - {route['name']}" + (f" x{laps}" if laps > 1 else "")

        # A single distance step with a power target. This is what makes
        # intervals.icu fill in distance, duration and load itself - a plain
        # prose description yields an *empty* workout_doc that zeroes `distance`,
        # and explicit distance/moving_time/icu_training_load are then ignored.
        # Trailing prose after a blank line is preserved.
        percent = round(watts / ftp * 100) if ftp else None
        metres = round(total_km * 1000)
        prose = (
            f"{route['name']} ({route['world']})\n"
            f"{total_km:.1f} km, {total_m:.0f} m elevation"
            + (f", {laps} laps" if laps > 1 else "")
            + f"\nTarget {watts} W"
            + (f" ({percent}% of {ftp} W {ftp_label})" if ftp else "")
            + f"\nEstimate {_fmt_duration(seconds)} [{source}]"
            + (f"\nLead-in {lead_km:.1f} km included" if lead_km else "")
            + f"\nrouteId {route['id']}"
        )
        description = (f"- {metres}mtr {percent}%\n\n{prose}") if percent else prose

        when = _parse_when(date)
        clock = "00:00:00"
        if at:
            try:
                clock = dt.time.fromisoformat(at).strftime("%H:%M:%S")
            except ValueError as exc:
                raise typer.BadParameter(f"{at!r} is not a time. Use HH:MM.") from exc

        console.print(f"[bold cyan]{name}[/bold cyan]  [dim]{when} {clock[:5]}[/dim]")
        summary = Table(show_header=False, box=None)
        summary.add_row("Distance", f"{total_km:.1f} km")
        summary.add_row("Elevation", f"{total_m:.0f} m")
        summary.add_row(
            "Power",
            f"{watts} W" + (f"  ({watts / ftp * 100:.0f}% of {ftp} W {ftp_label})" if ftp else ""),
        )
        summary.add_row("Duration", f"{_fmt_duration(seconds)}  [dim]{source}[/dim]")
        summary.add_row("Load", f"{load} TSS" if ftp else "[yellow]no Ride FTP set[/yellow]")
        console.print(summary)

        if dry_run:
            console.print("\n[yellow]dry run - nothing written.[/yellow]")
            return

        event = {
            "start_date_local": f"{when}T{clock}",
            "category": "WORKOUT",
            "type": "Ride",
            "name": name,
            "description": description,
            "indoor": True,
        }
        # Without a power target there is no step to parse, so fall back to the
        # target fields - they survive, even though the UI shows them as targets.
        if not percent:
            event["distance_target"] = metres
            event["time_target"] = round(seconds)
            if load:
                event["icu_training_load"] = load

        # Same convention as `report`: re-running for the same route and date
        # revises that entry rather than stacking a second copy beside it.
        superseded = [
            e
            for e in iclient.events(oldest=when, newest=when)
            if e.get("category") == "WORKOUT" and e.get("name") == name
        ]
        for old in superseded:
            iclient.delete_event(old["id"])

        created = iclient.create_event(event)
    except (IntervalsAPIError, ZwiftAPIError) as exc:
        if isinstance(exc, ZwiftAPIError):
            _zwift_fail(exc)
        _fail(exc)
    finally:
        zclient.close()

    console.print(f"\n[green]scheduled[/green] on {when} (event {created.get('id')})")
    iclient.close()


@zwift_app.command("raw")
def zwift_raw(
    path: str = typer.Argument(..., help="API path, e.g. /api/profiles/me"),
    param: list[str] = typer.Option(
        [], "--param", "-p", help="Query param as key=value. Repeatable."
    ),
) -> None:
    """Call any Zwift endpoint and dump the JSON. The exploration escape hatch."""
    client = _zwift()
    params = dict(p.split("=", 1) for p in param)
    try:
        result = client.get(path, **params)
    except ZwiftAPIError as exc:
        _zwift_fail(exc)
    client.close()

    if isinstance(result, str):
        # Protobuf-only endpoints ignore Accept: application/json.
        console.print("[yellow]Non-JSON response (endpoint is probably protobuf-only).[/yellow]")
        console.print(result[:2000])
    else:
        console.print_json(json.dumps(result))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
