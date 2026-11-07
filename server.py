"""Seismic MCP Server (Streamable HTTP) - seismic data & insurance analytics.

Data sources (APIs confirmed working by the user):
  * USGS Earthquake FDSN API (no auth)
  * TMD DailySeismicEvent demo API (demo uid/ukey)

Transport: MCP Streamable HTTP, endpoint ``/mcp``. Health check: ``/health``.
Logs go to stderr only.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
SERVER_NAME = "seismic-mcp-server"
SERVER_VERSION = "0.1.0"

HOST = os.getenv("MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("MCP_PORT", "8000"))
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Asia/Bangkok"))

USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
TMD_URL = "https://data.tmd.go.th/api/DailySeismicEvent/v1/"
# Demo credentials (fixed per spec); override with env vars if ever needed.
TMD_UID = os.getenv("TMD_UID", "api")
TMD_UKEY = os.getenv("TMD_UKEY", "api12345")

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "20"))
USER_AGENT = "seismic-mcp-server/0.1.0"

logging.basicConfig(
    stream=sys.stderr,
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(SERVER_NAME)

mcp = FastMCP(
    SERVER_NAME,
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
    # Stateless: every request is self-contained, which suits containers,
    # restarts and multiple clients without session affinity.
    stateless_http=True,
    json_response=True,
)
# Report our own version in serverInfo (FastMCP defaults to the SDK version).
mcp._mcp_server.version = SERVER_VERSION


# --------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------
async def _get_json(url: str, params: dict[str, Any]) -> Any:
    """GET a URL and return parsed JSON. Raises ValueError with a clean message."""
    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        raise ValueError(
            f"Upstream HTTP {e.response.status_code} from {url}: "
            f"{e.response.text[:200]}"
        ) from e
    except httpx.HTTPError as e:
        raise ValueError(f"Upstream request failed for {url}: {e!r}") from e
    except ValueError as e:  # JSON decode
        raise ValueError(f"Upstream response from {url} was not valid JSON") from e


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------
def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def usgs_feature_to_event(feature: dict[str, Any]) -> dict[str, Any]:
    props = feature.get("properties") or {}
    coords = (feature.get("geometry") or {}).get("coordinates") or [None, None, None]
    coords = list(coords) + [None] * (3 - len(coords))
    t_ms = props.get("time")
    time_iso = (
        datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).isoformat()
        if isinstance(t_ms, (int, float))
        else None
    )
    return {
        "id": feature.get("id"),
        "time": time_iso,
        "place": props.get("place"),
        "magnitude": _num(props.get("mag")),
        "latitude": _num(coords[1]),
        "longitude": _num(coords[0]),
        "depth_km": _num(coords[2]),
        "source": "USGS",
        "url": props.get("url"),
        "raw": feature,
    }


def _first(item: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in item and item[k] not in (None, ""):
            return item[k]
    return None


def _tmd_time(item: dict[str, Any]) -> str | None:
    """Return ISO8601 with offset. Prefers the UTC field (unambiguous); naive
    Thai-time values get +07:00, naive UTC-named fields get +00:00.
    Real TMD values look like "2026-11-07 06:54:07.000"."""
    candidates = (
        (("DateTimeUTC", "OriginTimeUTC", "OriginTime"), timezone.utc),
        (("DateTimeThai", "DateTime", "OriginTimeThai"), LOCAL_TZ),
    )
    for keys, assume in candidates:
        raw = _first(item, *keys)
        if raw is None:
            continue
        text = str(raw).strip().replace(" ", "T", 1).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=assume)
        return dt.isoformat()
    return None


def _tmd_id(item: dict[str, Any], time_iso: str | None, lat: float | None, lon: float | None) -> str:
    event_id = _first(item, "EventID", "EventId", "ID")
    if event_id is not None:
        return f"TMD-{event_id}"
    # The real API has no event id: build a stable one from time + epicentre.
    stamp = re.sub(r"\D", "", time_iso or "")[:14] or "unknown"
    pos = f"{lat:.3f}_{lon:.3f}" if lat is not None and lon is not None else "na"
    return f"TMD-{stamp}-{pos}"


def _tmd_place(item: dict[str, Any]) -> str | None:
    title = _first(item, "TitleEnglish", "Location", "Title", "TitleThai", "Region")
    if title is None:
        return None
    title = str(title).strip()
    # Real titles are Thai text ending with the English name in parentheses.
    m = re.search(r"\(([^()]+)\)\s*$", title)
    return m.group(1).strip() if m else title


def tmd_item_to_event(item: dict[str, Any]) -> dict[str, Any]:
    time_iso = _tmd_time(item)
    lat = _num(_first(item, "Latitude", "Lat"))
    lon = _num(_first(item, "Longitude", "Lon", "Long"))
    return {
        "id": _tmd_id(item, time_iso, lat, lon),
        "time": time_iso,
        "place": _tmd_place(item),
        "magnitude": _num(_first(item, "Magnitude", "Mag")),
        "latitude": _num(_first(item, "Latitude", "Lat")),
        "longitude": _num(_first(item, "Longitude", "Lon", "Long")),
        "depth_km": _num(_first(item, "DepthKm", "Depth")),
        "source": "TMD_DailySeismicEvent",
        "url": _first(item, "Url", "URL", "Link"),
        "raw": item,
    }


# The real API returns the list under "DailyEarthquakes" (the spec said
# "DailySeismicEvent"); accept both.
TMD_LIST_KEYS = ("DailyEarthquakes", "DailySeismicEvent")


def _tmd_items(payload: Any) -> list[dict[str, Any]]:
    """Extract the event list; tolerate list / single dict / wrapper."""
    if not isinstance(payload, dict):
        raise ValueError("Unexpected TMD response: expected a JSON object")
    for key in TMD_LIST_KEYS:
        data = payload.get(key)
        if data is None:
            continue
        if isinstance(data, dict):
            # either a single event or a {"<key>": [...]} style wrapper
            for inner_key in TMD_LIST_KEYS:
                inner = data.get(inner_key)
                if inner_key in data:
                    data = inner
                    break
            else:
                if any(k in data for k in ("EventID", "DateTime", "DateTimeUTC", "DateTimeThai")):
                    return [data]
        if isinstance(data, list) and all(isinstance(item, dict) for item in data):
            return data
        raise ValueError(f"Unexpected TMD response: invalid {key} event list")
    raise ValueError("Unexpected TMD response: missing event list")


async def _fetch_tmd(date: str | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"uid": TMD_UID, "ukey": TMD_UKEY, "format": "json"}
    if date:
        params["Date"] = date
    payload = await _get_json(TMD_URL, params)
    return [tmd_item_to_event(i) for i in _tmd_items(payload)]


def _today() -> datetime:
    return datetime.now(LOCAL_TZ)


def _valid_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"date must be YYYY-MM-DD, got {value!r}") from e


# --------------------------------------------------------------------------
# Geo helpers
# --------------------------------------------------------------------------
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


def _event_coords(event: dict[str, Any]) -> tuple[float, float, float]:
    lat, lon, mag = (
        _num(event.get("latitude")),
        _num(event.get("longitude")),
        _num(event.get("magnitude")),
    )
    if (
        lat is None or not -90 <= lat <= 90
        or lon is None or not -180 <= lon <= 180
        or mag is None or not math.isfinite(mag)
    ):
        raise ValueError(
            "event must include latitude in [-90, 90], longitude in [-180, 180] "
            "and finite magnitude "
            f"(id={event.get('id')!r})"
        )
    return lat, lon, mag


# --------------------------------------------------------------------------
# Input models
# --------------------------------------------------------------------------
class Asset(BaseModel):
    model_config = ConfigDict(extra="allow")
    asset_id: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    name: str | None = None
    sum_insured: float | None = Field(default=None, ge=0, description="Total insured value.")


class PayoutAsset(Asset):
    sum_insured: float = Field(ge=0, allow_inf_nan=False, description="Total insured value.")


class PayoutBand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_distance_km: float = Field(gt=0, allow_inf_nan=False)
    payout_percent: float = Field(ge=0, le=1, description="Fraction of sum_insured (0.0-1.0).")


DEFAULT_CURVE = [
    {"max_distance_km": 50, "payout_percent": 0.5},
    {"max_distance_km": 100, "payout_percent": 0.25},
    {"max_distance_km": 200, "payout_percent": 0.1},
]


# --------------------------------------------------------------------------
# Tools - data access
# --------------------------------------------------------------------------
@mcp.tool()
async def usgs_search_earthquakes(
    starttime: str = Field(description="YYYY-MM-DD or ISO8601."),
    endtime: str = Field(description="YYYY-MM-DD or ISO8601."),
    minmagnitude: float = Field(default=4.5, description="Minimum magnitude."),
    maxmagnitude: float | None = Field(default=None, description="Maximum magnitude, optional."),
    latitude: float | None = Field(default=None, description="Center latitude for radius search, optional."),
    longitude: float | None = Field(default=None, description="Center longitude for radius search, optional."),
    maxradiuskm: float | None = Field(default=None, description="Search radius in kilometers, optional."),
    limit: int = Field(default=20, ge=1, le=200, description="Maximum number of events."),
) -> dict[str, Any]:
    """Search earthquakes from the USGS FDSN API with basic filters."""
    radius_args = (latitude, longitude, maxradiuskm)
    if any(v is not None for v in radius_args) and not all(v is not None for v in radius_args):
        raise ValueError("latitude, longitude and maxradiuskm must be provided together")

    params: dict[str, Any] = {
        "format": "geojson",
        "starttime": starttime,
        "endtime": endtime,
        "minmagnitude": minmagnitude,
        "limit": limit,
    }
    for key, val in (
        ("maxmagnitude", maxmagnitude),
        ("latitude", latitude),
        ("longitude", longitude),
        ("maxradiuskm", maxradiuskm),
    ):
        if val is not None:
            params[key] = val

    log.info("USGS query: %s", params)
    payload = await _get_json(USGS_URL, params)
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        raise ValueError("Unexpected USGS response: missing 'features' list")
    return {"events": [usgs_feature_to_event(f) for f in features], "query": params}


@mcp.tool()
async def tmd_get_daily_earthquakes(
    date: str | None = Field(default=None, description="YYYY-MM-DD. If omitted, use today's date."),
) -> dict[str, Any]:
    """Get daily earthquakes from the TMD DailySeismicEvent demo API."""
    resolved = _valid_date(date) if date else _today().strftime("%Y-%m-%d")
    log.info("TMD daily query: %s", resolved)
    events = await _fetch_tmd(resolved)
    return {"events": events, "query": {"date": resolved}}


@mcp.tool()
async def tmd_get_recent_earthquakes(
    days: int = Field(default=3, ge=1, le=7, description="Number of days back from today (including today)."),
) -> dict[str, Any]:
    """Get recent earthquakes from TMD over the last N days using DailySeismicEvent."""
    today = _today()
    dates = [(today - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(days)]
    results = await asyncio.gather(*(_fetch_tmd(d) for d in dates), return_exceptions=True)

    events: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for d, res in zip(dates, results):
        if isinstance(res, Exception):
            log.warning("TMD fetch failed for %s: %s", d, res)
            warnings.append(f"{d}: {res}")
            continue
        for ev in res:
            key = ev["id"] or f"{ev['time']}|{ev['latitude']}|{ev['longitude']}"
            events.setdefault(key, ev)
    if len(warnings) == len(dates):
        raise ValueError("All TMD requests failed: " + "; ".join(warnings))

    ordered = sorted(events.values(), key=lambda e: _sort_key(e["time"]), reverse=True)
    out: dict[str, Any] = {"events": ordered, "query": {"days": days}}
    if warnings:
        out["warnings"] = warnings
    return out


def _sort_key(time_iso: str | None) -> float:
    if not time_iso:
        return float("-inf")
    try:
        dt = datetime.fromisoformat(time_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.timestamp()
    except ValueError:
        return float("-inf")


# --------------------------------------------------------------------------
# Tools - insurance / parametric analytics
# --------------------------------------------------------------------------
@mcp.tool()
def match_events_to_assets(
    events: list[dict[str, Any]] = Field(description="List of EarthquakeEvent objects."),
    assets: list[Asset] = Field(description="Insured assets with coordinates."),
    max_distance_km: float = Field(default=100, gt=0, description="Max distance to consider an asset affected."),
    min_magnitude: float = Field(default=4.5, description="Minimum magnitude to consider."),
) -> dict[str, Any]:
    """Match earthquake events to insured assets within a given radius and magnitude threshold."""
    eligible = []
    for ev in events:
        lat, lon, mag = _event_coords(ev)
        if mag >= min_magnitude:
            eligible.append((ev, lat, lon))

    matches = []
    for asset in assets:
        hits = []
        for ev, lat, lon in eligible:
            dist = haversine_km(lat, lon, asset.latitude, asset.longitude)
            if dist <= max_distance_km:
                hits.append({"event": ev, "distance_km": round(dist, 2)})
        if hits:
            hits.sort(key=lambda h: h["distance_km"])
            matches.append(
                {
                    "asset_id": asset.asset_id,
                    "name": asset.name,
                    "sum_insured": asset.sum_insured,
                    "events": hits,
                }
            )
    return {
        "matches": matches,
        "parameters": {"max_distance_km": max_distance_km, "min_magnitude": min_magnitude},
    }


@mcp.tool()
def compute_parametric_payout(
    event: dict[str, Any] = Field(description="Single EarthquakeEvent to evaluate."),
    assets: list[PayoutAsset] = Field(description="Assets with coordinates and sum_insured."),
    min_magnitude_trigger: float = Field(
        default=5.0, allow_inf_nan=False, description="Minimum magnitude to trigger any payout."
    ),
    max_distance_km_trigger: float = Field(
        default=200, gt=0, allow_inf_nan=False, description="Max distance for eligibility."
    ),
    payout_curve: list[PayoutBand] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Optional distance-based payout curve. Default: <=50km 50%, "
            "<=100km 25%, <=200km 10%."
        ),
    ),
) -> dict[str, Any]:
    """Compute a simple parametric payout for an earthquake event and a set of assets,
    based on distance and magnitude thresholds."""
    lat, lon, mag = _event_coords(event)
    curve = sorted(
        DEFAULT_CURVE if payout_curve is None else (b.model_dump() for b in payout_curve),
        key=lambda b: b["max_distance_km"],
    )
    triggered = mag >= min_magnitude_trigger

    asset_payouts = []
    total = 0.0
    for a in assets:
        dist = haversine_km(lat, lon, a.latitude, a.longitude)
        pct = 0.0
        if triggered and dist <= max_distance_km_trigger:
            for band in curve:
                if dist <= band["max_distance_km"]:
                    pct = band["payout_percent"]
                    break
        amount = round(a.sum_insured * pct, 2)
        total += amount
        asset_payouts.append(
            {
                "asset_id": a.asset_id,
                "name": a.name,
                "distance_km": round(dist, 2),
                "sum_insured": a.sum_insured,
                "payout_percent": pct,
                "payout_amount": amount,
            }
        )

    return {
        "event_id": event.get("id"),
        "event_magnitude": mag,
        "parameters": {
            "min_magnitude_trigger": min_magnitude_trigger,
            "max_distance_km_trigger": max_distance_km_trigger,
            "payout_curve": curve,
        },
        "asset_payouts": asset_payouts,
        "total_payout": round(total, 2),
    }


# --------------------------------------------------------------------------
# Health check (for Docker / load balancers)
# --------------------------------------------------------------------------
@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION})


if __name__ == "__main__":
    log.info("Starting %s v%s on http://%s:%s/mcp", SERVER_NAME, SERVER_VERSION, HOST, PORT)
    mcp.run(transport="streamable-http")
