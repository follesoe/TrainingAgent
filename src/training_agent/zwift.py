"""Client for the unofficial Zwift API.

Zwift publishes no developer API for individuals. The official one ("Training
Connections") is a B2B arrangement negotiated through developers@zwift.com and is
not offered to hobby developers. What the game and the Companion app talk to is a
plain JSON REST API sitting behind an OAuth password grant, and that is what this
module uses.

Endpoint knowledge is derived from SauceLLC/sauce4zwift (src/zwift.mjs), the most
actively maintained reference implementation, cross-checked against
strukturunion-mmw/zwift-api-documentation.

None of this is supported by Zwift. Endpoints and field names can change without
notice, so treat a sudden 404 or a shape change as expected maintenance, not as a
bug in the caller. Keep request volume near what the Companion app would produce.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import xml.etree.ElementTree as ET
from functools import lru_cache
from typing import Any, Iterator

import httpx

from .config import DATA_DIR, PROJECT_ROOT, ZwiftSettings

AUTH_URL = "https://secure.zwift.com/auth/realms/zwift/protocol/openid-connect/token"
BASE_URL = "https://us-or-rly101.zwift.com"

# The token endpoint only issues tokens to client ids it knows. This is the one
# the desktop game uses; there is no way to register your own.
CLIENT_ID = "Zwift Game Client"

# Zwift is picky about unidentified clients, so present ourselves the way the
# game does. Bumping these to match a current game build is harmless.
GAME_HEADERS = {
    "Platform": "OSX",
    "Source": "Game Client",
    "User-Agent": "CNL/3.44.0 (Darwin Kernel 23.2.0) zwift/1.0.122968 game/1.54.0 curl/8.4.0",
}

# Access tokens last hours and refresh tokens days, so caching one keeps the
# password out of all but the first request. data/ is git-ignored.
TOKEN_CACHE = DATA_DIR / ".zwift-token.json"

ROUTE_DATA = PROJECT_ROOT / "reference" / "zwift-routes.json"

DateLike = str | dt.date | dt.datetime | None


class ZwiftAPIError(RuntimeError):
    def __init__(self, status: int, url: str, body: str) -> None:
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")


def _epoch_ms(value: DateLike) -> int | None:
    """Zwift's feed endpoints take milliseconds since the epoch."""
    if value is None:
        return None
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value)
    if isinstance(value, dt.datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, dt.date):
        return int(dt.datetime.combine(value, dt.time.min).timestamp() * 1000)
    raise TypeError(f"cannot convert {value!r} to a timestamp")


class ZwiftClient:
    """Synchronous client. Use as a context manager to reuse the connection."""

    def __init__(self, settings: ZwiftSettings | None = None, timeout: float = 30.0) -> None:
        self.settings = settings or ZwiftSettings.load()
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={**GAME_HEADERS, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )
        self._token: dict[str, Any] | None = None
        self._profile: dict[str, Any] | None = None

    def __enter__(self) -> "ZwiftClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---- auth --------------------------------------------------------------

    def _read_cached_token(self) -> dict[str, Any] | None:
        if not TOKEN_CACHE.exists():
            return None
        try:
            token = json.loads(TOKEN_CACHE.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        # A cached token for a different account is useless.
        if token.get("account") != self.settings.email:
            return None
        return token

    def _write_cached_token(self, token: dict[str, Any]) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE.write_text(json.dumps(token, indent=2))
        TOKEN_CACHE.chmod(0o600)

    def _request_token(self, grant: dict[str, str]) -> dict[str, Any]:
        response = httpx.post(
            AUTH_URL,
            data={"client_id": CLIENT_ID, **grant},
            headers=GAME_HEADERS,
            timeout=30.0,
        )
        if response.status_code >= 400:
            raise ZwiftAPIError(response.status_code, AUTH_URL, response.text)

        body = response.json()
        now = time.time()
        # Expire a minute early so a slow command cannot fall off the edge midway.
        token = {
            "account": self.settings.email,
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token", ""),
            "access_expires_at": now + body.get("expires_in", 0) - 60,
            "refresh_expires_at": now + body.get("refresh_expires_in", 0) - 60,
        }
        self._write_cached_token(token)
        return token

    def _access_token(self) -> str:
        token = self._token or self._read_cached_token()
        now = time.time()

        if token and token.get("access_expires_at", 0) > now:
            self._token = token
            return token["access_token"]

        if token and token.get("refresh_token") and token.get("refresh_expires_at", 0) > now:
            try:
                self._token = self._request_token(
                    {"grant_type": "refresh_token", "refresh_token": token["refresh_token"]}
                )
                return self._token["access_token"]
            except ZwiftAPIError:
                # Refresh token revoked or rejected - fall through to a full login.
                pass

        self._token = self._request_token(
            {
                "grant_type": "password",
                "username": self.settings.email,
                "password": self.settings.password,
            }
        )
        return self._token["access_token"]

    def logout(self) -> None:
        """Forget the cached token. The next call logs in again."""
        self._token = None
        TOKEN_CACHE.unlink(missing_ok=True)

    # ---- plumbing ----------------------------------------------------------

    def get(self, path: str, **params: Any) -> Any:
        """GET an API path (e.g. /api/profiles/me) and return decoded JSON."""
        clean = {k: v for k, v in params.items() if v is not None}
        response = self._client.get(
            path,
            params=clean,
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )
        if response.status_code >= 400:
            raise ZwiftAPIError(response.status_code, str(response.url), response.text)
        if not response.content:
            return None
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        # Some endpoints (/api/events/{id}, /api/events/search) only speak
        # protobuf and ignore the Accept header. Hand the bytes back rather than
        # pretending to have parsed them.
        return response.text

    def get_versioned(self, path: str, api_version: str, **params: Any) -> Any:
        """GET with a Zwift-Api-Version header. /api/game_info needs one."""
        clean = {k: v for k, v in params.items() if v is not None}
        response = self._client.get(
            path,
            params=clean,
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Zwift-Api-Version": api_version,
            },
        )
        if response.status_code >= 400:
            raise ZwiftAPIError(response.status_code, str(response.url), response.text)
        return response.json()

    def fetch_asset(self, url: str) -> str:
        """Fetch a workout asset (.zwo XML) from the url Zwift hands out.

        The asset host sits behind the same auth as the API and answers an
        unauthenticated request with "403 RBAC: access denied", so the bearer
        token has to travel with it.
        """
        response = httpx.get(
            url,
            headers={**GAME_HEADERS, "Authorization": f"Bearer {self._access_token()}"},
            timeout=30.0,
            follow_redirects=True,
        )
        if response.status_code >= 400:
            raise ZwiftAPIError(response.status_code, url, response.text)
        return response.text

    # ---- profile -----------------------------------------------------------

    def profile(self, athlete_id: str | int = "me") -> dict[str, Any]:
        """Rider profile: FTP, weight, level, total distance, current world."""
        return self.get(f"/api/profiles/{athlete_id}")

    @property
    def athlete_id(self) -> int:
        if self._profile is None:
            self._profile = self.profile()
        return self._profile["id"]

    def activities(
        self, athlete_id: str | int | None = None, start: int = 0, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Zwift's own record of rides, newest first.

        intervals.icu is the source of truth for training analysis - this is
        useful mainly for the Zwift-only fields (routeId, world, event id).
        """
        return self.get(
            f"/api/profiles/{athlete_id or self.athlete_id}/activities",
            start=start,
            limit=limit,
        )

    # ---- events and meetups ------------------------------------------------

    def upcoming_events(self) -> list[dict[str, Any]]:
        """Public events (races, group rides) the athlete has signed up for."""
        return self.get("/api/events/upcoming")

    def event_feed(
        self, from_time: DateLike = None, to_time: DateLike = None, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Browse the public event catalogue in a time window.

        One page only. The endpoint duplicates and skips entries across page
        boundaries, so paging it is not worth the trouble.
        """
        page = self.get(
            "/api/event-feed",
            **{"from": _epoch_ms(from_time), "to": _epoch_ms(to_time), "limit": limit},
        )
        # Entries are wrapped as {"event": {...}}.
        return [entry.get("event", entry) for entry in (page or {}).get("data", [])]

    def meetups(self, since: DateLike = None) -> list[dict[str, Any]]:
        """Meetups - the private rides you schedule in the Companion app.

        This is where a "we're riding this route on Saturday" plan lives. Each
        entry carries routeId, eventStart, durationInSeconds or distanceInMeters,
        and the invited riders.
        """
        # start_date and end_date exist but are buggy; the feed defaults to a
        # sensible window on its own. Look back two hours so a meetup starting
        # right now is still included.
        default = dt.datetime.now() - dt.timedelta(hours=2)
        return self.get(
            "/api/private_event/feed",
            start_date=_epoch_ms(since or default),
            organizer_only_past_events="false",
        )

    def meetup(self, event_id: str | int) -> dict[str, Any]:
        """One meetup in full, including the invite list and each rider's status.

        Note there is no `name` field - a meetup's title is `description`.
        """
        return self.get(f"/api/private_event/{event_id}")

    def meetup_invites(self) -> list[dict[str, Any]]:
        """Meetup invites recovered from the notification feed.

        /api/private_event/feed only returns live meetups; once one is over it
        drops out entirely regardless of the date parameters. The invite
        notification survives, and carries the event id in a JSON string, so this
        is the only way back to a past meetup.
        """
        invites = []
        for note in self.get("/api/notifications") or []:
            if note.get("type") != "PRIVATE_EVENT_INVITE":
                continue
            try:
                arg = json.loads(note.get("argString0") or "{}")
            except json.JSONDecodeError:
                continue
            if not arg.get("eventId"):
                continue
            sender = note.get("fromProfile") or {}
            invites.append(
                {
                    "event_id": arg["eventId"],
                    "start": dt.datetime.fromtimestamp(arg.get("eventStartDate", 0)),
                    "from": f"{sender.get('firstName') or ''} {sender.get('lastName') or ''}".strip(),
                }
            )
        return sorted(invites, key=lambda i: i["start"], reverse=True)

    # ---- workouts and training plans ---------------------------------------

    def workout_schedule(self) -> Any:
        """Workouts scheduled on the Zwift calendar, i.e. an active training plan."""
        return self.get("/api/workout/schedule/list")

    def workouts(self, page_size: int = 100, max_pages: int = 20) -> list[dict[str, Any]]:
        """Every workout in the athlete's library, custom ones included."""
        results: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            batch = self.get("/api/workout/workouts", page=page, pageSize=page_size)
            if not batch:
                break
            results.extend(batch)
            if len(batch) < page_size:
                break
        return results

    def workout(self, workout_id: str) -> dict[str, Any]:
        """Workout metadata. The steps live at workoutAssetUrl as .zwo XML.

        Takes the uuid from the `workoutId` field. The `legacyId` integer that
        sits alongside it 404s here.
        """
        return self.get(f"/api/workout/workouts/{workout_id}")

    def workout_steps(self, workout_id: str) -> dict[str, Any]:
        """Workout metadata with its .zwo fetched and parsed into a step list."""
        meta = self.workout(workout_id)
        url = meta.get("workoutAssetUrl")
        if not url:
            raise ZwiftAPIError(404, f"/api/workout/workouts/{workout_id}", "no workoutAssetUrl")
        # Keep the API's own name/description/sport; take only the steps from the
        # .zwo, which is the one thing the metadata does not carry.
        return {**meta, "steps": parse_zwo(self.fetch_asset(url))["steps"]}

    def workout_collections(self) -> Any:
        """Zwift's curated collections - training plans live here."""
        return self.get("/api/workout/collections", pageSize=100)

    def workout_collection(self, collection_id: str | int) -> Any:
        """The workouts inside one collection or training plan."""
        return self.get(f"/api/workout/collections/{collection_id}/workouts", pageSize=100)


# ---- live route catalogue --------------------------------------------------
#
# /api/game_info carries Zwift's own route catalogue - the same numbers the
# Companion app shows. It is the authority: more routes than any vendored copy,
# always current, and it has every lead-in variant. reference/zwift-routes.json
# stays as the offline fallback (and the only source of segment data).

GAME_INFO_CACHE = DATA_DIR / ".zwift-game-info.json"


def _world_slug(name: str) -> str:
    return str(name or "").strip().lower().replace("_", "-").replace(" ", "-")


def _from_game_info(entry: dict[str, Any], world: str) -> dict[str, Any]:
    """Normalise a game_info route onto the vendored file's field names."""
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "world": world,
        "distance": (entry.get("distanceInMeters") or 0) / 1000,
        "elevation": entry.get("ascentInMeters") or 0,
        "leadInDistance": (entry.get("leadinDistanceInMeters") or 0) / 1000,
        "leadInElevation": entry.get("leadinAscentInMeters") or 0,
        "leadInDistanceFreeRide": (entry.get("freeRideLeadinDistanceInMeters") or 0) / 1000,
        "leadInElevationFreeRide": entry.get("freeRideLeadinAscentInMeters") or 0,
        "leadInDistanceMeetups": (entry.get("meetupLeadinDistanceInMeters") or 0) / 1000,
        "leadInElevationInMeetups": entry.get("meetupLeadinAscentInMeters") or 0,
        "sports": [str(s).lower() for s in entry.get("sports") or []],
        "experience": entry.get("xp"),
        "difficulty": entry.get("difficulty"),
        "levelLocked": bool(entry.get("levelLocked")),
        "eventOnly": bool(entry.get("publicEventsOnly")),
        "lap": bool(entry.get("supportedLaps")),
        "segments": [],  # game_info has no segment list - vendored file has those
    }


def live_routes(client: "ZwiftClient", refresh: bool = False) -> list[dict[str, Any]]:
    """Zwift's own route catalogue, cached to disk between runs.

    game_info is ~600 KB and changes only when Zwift ships new roads, so it is
    cached and only re-fetched on demand.
    """
    if not refresh and GAME_INFO_CACHE.exists():
        try:
            return json.loads(GAME_INFO_CACHE.read_text())["routes"]
        except (json.JSONDecodeError, KeyError, OSError):
            pass

    raw = client.get_versioned("/api/game_info", api_version="2.7")
    routes = [
        _from_game_info(entry, _world_slug(world.get("name")))
        for world in raw.get("maps") or []
        for entry in world.get("routes") or []
        if entry.get("id")
    ]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GAME_INFO_CACHE.write_text(
        json.dumps({"hash": raw.get("gameInfoHash"), "routes": routes}, ensure_ascii=False)
    )
    return routes


# ---- route reference -------------------------------------------------------


@lru_cache(maxsize=1)
def _routes() -> tuple[dict[str, Any], ...]:
    """Vendored route metadata. See reference/zwift-routes.json for provenance."""
    return tuple(json.loads(ROUTE_DATA.read_text())["routes"])


@lru_cache(maxsize=1)
def _routes_by_id() -> dict[int, dict[str, Any]]:
    return {r["id"]: r for r in _routes() if r.get("id")}


def route(route_id: int | str | None) -> dict[str, Any] | None:
    """Look up a route by the routeId the API returns on events and meetups."""
    if route_id is None:
        return None
    try:
        return _routes_by_id().get(int(route_id))
    except (TypeError, ValueError):
        return None


def find_routes(text: str) -> list[dict[str, Any]]:
    """Search routes by name, slug, world or segment. Case-insensitive substring.

    Segments are included because athletes name climbs, not routes - "alpe" is
    how you ask for Road to Sky.
    """
    needle = text.strip().lower()
    return [
        r
        for r in _routes()
        if needle in r["name"].lower()
        or needle in r["slug"]
        or needle in r["world"]
        or any(needle in s for s in r.get("segments") or [])
    ]


def route_label(route_id: int | str | None) -> str:
    """Human-readable 'Name (world) 42.6 km / 662 m', or the bare id if unknown."""
    found = route(route_id)
    if not found:
        return f"route {route_id}" if route_id else "-"
    return (
        f"{found['name']} ({found['world']}) "
        f"{found['distance']:.1f} km / {found['elevation']:.0f} m"
    )


# ---- ride duration estimate ------------------------------------------------
#
# Zwift's own "15 min" estimate is just route distance over the rider's recent
# average speed - it ignores the power you intend to hold. Estimating from watts
# needs a physics model, and a flat model alone is badly optimistic on climbs
# (it predicted Road to Sky ~39% too fast). So: solve the flat case from power,
# then scale by a correction fitted to the athlete's own history.

SPEED_MODEL_CACHE = DATA_DIR / ".zwift-speed-model.json"

# Rider-agnostic constants. CdA/Crr are Zwift-ish averages across bike choices.
_CDA = 0.35
_CRR = 0.004
_RHO = 1.225
_DRIVETRAIN = 0.975
_BIKE_KG = 8.0


def flat_speed(watts: float, rider_kg: float) -> float:
    """Steady-state speed (m/s) on flat ground for a given power. Bisection."""
    mass = rider_kg + _BIKE_KG
    low, high = 0.5, 25.0
    for _ in range(80):
        v = (low + high) / 2
        needed = (0.5 * _RHO * _CDA * v**3 + _CRR * mass * 9.81 * v) / _DRIVETRAIN
        if needed < watts:
            low = v
        else:
            high = v
    return (low + high) / 2


def calibrate_speed_model(client: "ZwiftClient", min_rides: int = 10) -> dict[str, Any] | None:
    """Fit actual/flat-model time against route hilliness on the athlete's rides.

    Returns {"a", "k", "n", "median_error_pct"} where
    `time = flat_time * (a + k * ascent_per_km)`, or None if there is not enough
    history. Nothing athlete-specific is baked into the code - this is refitted
    per account and cached.
    """
    rider_kg = (client.profile().get("weight") or 0) / 1000
    if not rider_kg:
        return None

    rides = []
    for start in (0, 30, 60):
        try:
            # limit above 30 is rejected with "limit.too.large".
            rides += client.activities(start=start, limit=30)
        except ZwiftAPIError:
            break

    points = []
    for a in rides:
        distance = a.get("distanceInMeters") or 0
        moving_ms = a.get("movingTimeInMs") or 0
        watts = a.get("avgWatts") or 0
        # Runs and rowing share the feed; their speed has nothing to do with watts.
        if a.get("sport") != "CYCLING" or distance < 5000 or moving_ms <= 0 or watts < 50:
            continue
        modelled = distance / flat_speed(watts, rider_kg)
        points.append(
            (
                (a.get("totalElevation") or 0) / (distance / 1000),
                (moving_ms / 1000) / modelled,
            )
        )

    if len(points) < min_rides:
        return None

    n = len(points)
    sx = sum(h for h, _ in points)
    sy = sum(r for _, r in points)
    sxx = sum(h * h for h, _ in points)
    sxy = sum(h * r for h, r in points)
    denom = n * sxx - sx * sx
    if not denom:
        return None
    k = (n * sxy - sx * sy) / denom
    a = (sy - k * sx) / n

    errors = sorted(abs(((a + k * h) - r) / r * 100) for h, r in points)
    model = {"a": a, "k": k, "n": n, "median_error_pct": errors[len(errors) // 2]}

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SPEED_MODEL_CACHE.write_text(json.dumps(model, indent=2))
    return model


def cached_speed_model() -> dict[str, Any] | None:
    if not SPEED_MODEL_CACHE.exists():
        return None
    try:
        return json.loads(SPEED_MODEL_CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def estimate_duration(
    watts: float,
    distance_km: float,
    elevation_m: float,
    rider_kg: float,
    model: dict[str, Any] | None = None,
) -> float:
    """Seconds to ride a route at a given average power."""
    seconds = (distance_km * 1000) / flat_speed(watts, rider_kg)
    if model:
        seconds *= model["a"] + model["k"] * (elevation_m / max(distance_km, 0.1))
    return seconds


def training_load(watts: float, seconds: float, ftp: float) -> int:
    """TSS for a steady ride: hours x IF^2 x 100.

    Average power stands in for normalised power, which is right for a steady
    route ride and understates a session ridden with hard surges.
    """
    if not ftp:
        return 0
    intensity = watts / ftp
    return round((seconds / 3600) * intensity**2 * 100)


# ---- .zwo workout files ----------------------------------------------------

# Zwift expresses power as a fraction of FTP and duration in seconds. Interval
# blocks carry an on/off pair plus a repeat count; everything else is one step.
_POWER_ATTRS = ("Power", "PowerLow", "PowerHigh", "OnPower", "OffPower")


def _num(element: ET.Element, name: str) -> float | None:
    value = element.get(name)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_zwo(xml_text: str) -> dict[str, Any]:
    """Parse a Zwift .zwo workout into name, sport and a flat list of steps.

    Each step is {kind, repeat, duration, power_low, power_high, cadence}, with
    power as a fraction of FTP (0.75 = 75%) and duration in seconds. Interval
    blocks keep their repeat count and expose both halves as on/off steps.
    """
    root = ET.fromstring(xml_text)
    steps: list[dict[str, Any]] = []

    for element in root.find("workout") or []:
        kind = element.tag
        repeat = int(_num(element, "Repeat") or 1)

        if element.get("OnDuration") is not None:
            # IntervalsT and friends: one element describes a repeated pair.
            steps.append(
                {
                    "kind": kind,
                    "repeat": repeat,
                    "on": {
                        "duration": _num(element, "OnDuration"),
                        "power_low": _num(element, "OnPower"),
                        "power_high": _num(element, "OnPower"),
                        "cadence": _num(element, "Cadence"),
                    },
                    "off": {
                        "duration": _num(element, "OffDuration"),
                        "power_low": _num(element, "OffPower"),
                        "power_high": _num(element, "OffPower"),
                        "cadence": _num(element, "CadenceResting"),
                    },
                }
            )
            continue

        # Warmup/Cooldown/Ramp ramp between PowerLow and PowerHigh; SteadyState
        # and FreeRide hold a single Power (FreeRide has none at all).
        low = _num(element, "PowerLow")
        high = _num(element, "PowerHigh")
        flat = _num(element, "Power")
        steps.append(
            {
                "kind": kind,
                "repeat": repeat,
                "duration": _num(element, "Duration"),
                "power_low": low if low is not None else flat,
                "power_high": high if high is not None else flat,
                "cadence": _num(element, "Cadence"),
            }
        )

    return {
        "name": (root.findtext("name") or "").strip(),
        "description": (root.findtext("description") or "").strip(),
        "sport": (root.findtext("sportType") or "bike").strip(),
        "steps": steps,
    }


def _fmt_seconds(seconds: float | None) -> str:
    """intervals.icu duration: 'm' means minutes, so seconds need the s suffix."""
    total = int(seconds or 0)
    if total and total % 3600 == 0:
        return f"{total // 3600}h"
    if total and total % 60 == 0:
        return f"{total // 60}m"
    return f"{total}s"


def _fmt_power(low: float | None, high: float | None) -> str:
    if low is None and high is None:
        return ""  # FreeRide - no target
    lo = round((low if low is not None else high) * 100)
    hi = round((high if high is not None else low) * 100)
    return f"{lo}%" if lo == hi else f"{lo}-{hi}%"


def _leaf(step: dict[str, Any], pace: bool = False) -> str:
    target = _fmt_power(step.get("power_low"), step.get("power_high"))
    # Zwift stores a run's target as a fraction in the same Power attribute a
    # ride uses. Without the explicit Pace keyword intervals.icu would read it as
    # power and silently put a wattage target on a run.
    if target and pace:
        target += " Pace"
    parts = [_fmt_seconds(step.get("duration")), target]
    line = "- " + " ".join(p for p in parts if p)
    cadence = step.get("cadence")
    if cadence and not pace:
        line += f" {round(cadence)}rpm"
    return line


def is_run(parsed: dict[str, Any]) -> bool:
    """.zwo says 'run', the workout API says 'RUNNING'. Accept either."""
    return "run" in str(parsed.get("sport") or "").lower()


def zwo_to_intervals(parsed: dict[str, Any]) -> str:
    """Render a parsed .zwo as intervals.icu structured-workout syntax.

    Output goes in an intervals.icu event `description`; intervals.icu parses it
    into workout_doc itself.

    Each step becomes its own block and blocks are separated by blank lines. That
    is what keeps a heading attached to exactly one step - consecutive "- " lines
    under a heading would otherwise all be swallowed into that group - and it
    satisfies the rule that a repeat needs a blank line before its "Nx".

    Ride targets come out as % of FTP. Run targets get the Pace keyword, since
    Zwift stores them as a fraction of threshold pace in the same field - those
    need `threshold_pace` set on the Run sport-settings entry to resolve, and are
    worth checking with resolve=true before trusting a device export.

    A FreeRide step yields a duration with no target at all. intervals.icu
    accepts that, but a device export will reject the workout.
    """
    pace = is_run(parsed)
    blocks: list[list[str]] = []

    for step in parsed["steps"]:
        repeat = step["repeat"]
        if "on" in step:
            legs = [_leaf(step["on"], pace), _leaf(step["off"], pace)]
            # Repeat=1 is a plain pair, not a group. Emitting "1x" would build a
            # pointless repeat block in intervals.icu.
            blocks.append([f"{repeat}x", *legs] if repeat > 1 else legs)
        elif repeat > 1:
            blocks.append([f"{repeat}x", _leaf(step, pace)])
        elif step["kind"] in ("Warmup", "Cooldown"):
            blocks.append([step["kind"], _leaf(step, pace)])
        else:
            blocks.append([_leaf(step, pace)])

    return "\n\n".join("\n".join(block) for block in blocks)


def zwo_summary(parsed: dict[str, Any]) -> Iterator[str]:
    """One readable line per step, for terminal display."""
    for step in parsed["steps"]:
        if "on" in step:
            on, off = step["on"], step["off"]
            yield (
                f"{step['repeat']}x  {_fmt_seconds(on['duration'])} "
                f"{_fmt_power(on['power_low'], on['power_high'])} / "
                f"{_fmt_seconds(off['duration'])} {_fmt_power(off['power_low'], off['power_high'])}"
            )
        else:
            power = _fmt_power(step.get("power_low"), step.get("power_high")) or "free ride"
            yield f"{step['kind']:<12} {_fmt_seconds(step.get('duration')):>6}  {power}"
