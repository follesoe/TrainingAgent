"""Thin client for the intervals.icu REST API.

Auth is HTTP Basic with username "API_KEY" and the personal API key as password.
Full endpoint reference: reference/intervals-openapi.json (or https://intervals.icu/api/v1/docs).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Iterable

import httpx

from .config import Settings

BASE_URL = "https://intervals.icu"

# Cloudflare in front of intervals.icu rejects some default library user agents,
# so identify ourselves as a normal client.
USER_AGENT = "TrainingAgent/0.1 (+https://github.com/follesoe) httpx"

DateLike = str | dt.date | dt.datetime | None


def _fmt_date(value: DateLike) -> str | None:
    """intervals.icu wants local ISO-8601, e.g. 2026-07-29 or 2026-07-29T16:18:49."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


class IntervalsAPIError(RuntimeError):
    def __init__(self, status: int, url: str, body: str) -> None:
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")


class IntervalsClient:
    """Synchronous client. Use as a context manager to reuse the connection."""

    def __init__(self, settings: Settings | None = None, timeout: float = 30.0) -> None:
        self.settings = settings or Settings.load()
        self._client = httpx.Client(
            base_url=BASE_URL,
            auth=("API_KEY", self.settings.api_key),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )
        self.last_rate_limit: dict[str, str] = {}

    def __enter__(self) -> "IntervalsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @property
    def athlete_id(self) -> str:
        return self.settings.athlete_id

    # ---- plumbing ----------------------------------------------------------

    def get(self, path: str, **params: Any) -> Any:
        """GET an API path (relative to /api/v1) and return decoded JSON."""
        clean = {k: v for k, v in params.items() if v is not None}
        response = self._client.get(f"/api/v1{path}", params=clean)

        # Format is "<15min>,<daily>" for both headers.
        if "X-RateLimit-Limit" in response.headers:
            self.last_rate_limit = {
                "limit": response.headers["X-RateLimit-Limit"],
                "remaining": response.headers.get("X-RateLimit-Remaining", "?"),
            }

        if response.status_code >= 400:
            raise IntervalsAPIError(response.status_code, str(response.url), response.text)
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.text

    def post(self, path: str, body: Any) -> Any:
        """POST JSON to an API path and return the decoded response."""
        response = self._client.post(f"/api/v1{path}", json=body)
        if response.status_code >= 400:
            raise IntervalsAPIError(response.status_code, str(response.url), response.text)
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.text

    def put(self, path: str, body: Any) -> Any:
        """PUT JSON to an API path and return the decoded response."""
        response = self._client.put(f"/api/v1{path}", json=body)
        if response.status_code >= 400:
            raise IntervalsAPIError(response.status_code, str(response.url), response.text)
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.text

    def delete(self, path: str) -> Any:
        """DELETE an API path. Destructive - callers should confirm intent first."""
        response = self._client.delete(f"/api/v1{path}")
        if response.status_code >= 400:
            raise IntervalsAPIError(response.status_code, str(response.url), response.text)
        return response.text

    def _athlete_path(self, suffix: str, athlete_id: str | None = None) -> str:
        return f"/athlete/{athlete_id or self.athlete_id}{suffix}"

    # ---- athlete / settings ------------------------------------------------

    def athlete(self, athlete_id: str | None = None) -> dict[str, Any]:
        """Athlete record including sportSettings (zones, FTP, thresholds)."""
        return self.get(self._athlete_path("", athlete_id))

    def profile(self, athlete_id: str | None = None) -> dict[str, Any]:
        return self.get(self._athlete_path("/profile", athlete_id))

    def sport_settings(self, athlete_id: str | None = None) -> list[dict[str, Any]]:
        """One entry per sport group: FTP, LTHR, threshold pace, zone definitions."""
        return self.get(f"/athlete/{athlete_id or self.athlete_id}/sport-settings")

    # ---- activities --------------------------------------------------------

    def activities(
        self,
        oldest: DateLike,
        newest: DateLike = None,
        limit: int | None = None,
        fields: Iterable[str] | None = None,
        athlete_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Completed activities in a date range, newest first."""
        return self.get(
            self._athlete_path("/activities", athlete_id),
            oldest=_fmt_date(oldest),
            newest=_fmt_date(newest),
            limit=limit,
            fields=",".join(fields) if fields else None,
        )

    def activity(self, activity_id: str, intervals: bool = False) -> dict[str, Any]:
        return self.get(f"/activity/{activity_id}", intervals=intervals or None)

    def activity_streams(
        self, activity_id: str, types: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        """Raw per-second streams (watts, heartrate, altitude, latlng, ...)."""
        return self.get(
            f"/activity/{activity_id}/streams",
            types=",".join(types) if types else None,
        )

    def activity_intervals(self, activity_id: str) -> dict[str, Any]:
        return self.get(f"/activity/{activity_id}/intervals")

    def delete_activity(self, activity_id: str) -> Any:
        """Permanently remove an activity from intervals.icu."""
        return self.delete(f"/activity/{activity_id}")

    def upload_activity(
        self,
        path: "Path",
        name: str | None = None,
        description: str | None = None,
        device_name: str | None = None,
        external_id: str | None = None,
        athlete_id: str | None = None,
    ) -> tuple[int, Any]:
        """Upload a fit/tcx/gpx (or zip/gz) file as a new activity.

        intervals.icu de-dupes on a hash of the file, so re-uploading the same
        file is safe - it returns 200 with no new activity rather than 201.
        Note that an activity which arrived over the Strava API has no file hash,
        so uploading its original file *will* create a second copy.
        """
        params = {
            k: v
            for k, v in {
                "name": name,
                "description": description,
                "device_name": device_name,
                "external_id": external_id,
            }.items()
            if v is not None
        }
        with path.open("rb") as handle:
            response = self._client.post(
                f"/api/v1{self._athlete_path('/activities', athlete_id)}",
                params=params,
                files={"file": (path.name, handle, "application/octet-stream")},
            )
        if response.status_code >= 400:
            raise IntervalsAPIError(response.status_code, str(response.url), response.text)
        body = response.json() if response.content else None
        return response.status_code, body

    # ---- wellness ----------------------------------------------------------

    def wellness(
        self,
        oldest: DateLike = None,
        newest: DateLike = None,
        athlete_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Daily wellness: weight, resting HR, HRV, sleep, plus CTL/ATL/form."""
        return self.get(
            self._athlete_path("/wellness", athlete_id),
            oldest=_fmt_date(oldest),
            newest=_fmt_date(newest),
        )

    # ---- calendar / planning -----------------------------------------------

    def events(
        self,
        oldest: DateLike = None,
        newest: DateLike = None,
        category: Iterable[str] | None = None,
        resolve: bool = False,
        athlete_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Calendar entries: planned workouts, notes, races, holidays."""
        return self.get(
            self._athlete_path("/events", athlete_id),
            oldest=_fmt_date(oldest),
            newest=_fmt_date(newest),
            category=",".join(category) if category else None,
            resolve=resolve or None,
        )

    def create_event(self, event: dict[str, Any], athlete_id: str | None = None) -> dict[str, Any]:
        """Add one calendar entry (planned workout, race, note)."""
        return self.post(self._athlete_path("/events", athlete_id), event)

    def create_events(
        self, events: list[dict[str, Any]], athlete_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Add many calendar entries in one call."""
        return self.post(self._athlete_path("/events/bulk", athlete_id), events)

    def update_event(
        self, event_id: str | int, changes: dict[str, Any], athlete_id: str | None = None
    ) -> dict[str, Any]:
        return self.put(self._athlete_path(f"/events/{event_id}", athlete_id), changes)

    def delete_event(self, event_id: str | int, athlete_id: str | None = None) -> Any:
        return self.delete(self._athlete_path(f"/events/{event_id}", athlete_id))

    def workouts(self, athlete_id: str | None = None) -> list[dict[str, Any]]:
        """Workouts saved in the athlete's library folders."""
        return self.get(self._athlete_path("/workouts", athlete_id))
