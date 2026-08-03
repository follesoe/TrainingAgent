"""Command line entry points for exploring intervals.icu data."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import audit
from .config import ConfigError, Settings
from .intervals import IntervalsAPIError, IntervalsClient

app = typer.Typer(add_completion=False, help="Endurance training agent tools.")
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
    date: str = typer.Option(None, help="Calendar date (YYYY-MM-DD). Default: from filename, else today."),
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

        created = client.create_event({
            "start_date_local": f"{date}T00:00:00",
            "category": "NOTE",
            "name": title,
            "description": body,
            "color": "blue",
            "tags": ["performance-report"],
        })
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
