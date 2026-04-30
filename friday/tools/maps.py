"""
Maps tools — directions, ETAs, and nearby place search via Google Maps Platform.

Requires GOOGLE_MAPS_API_KEY in .env. Optional HOME_ADDRESS env var sets the
default origin; otherwise falls back to IP geolocation.

APIs used:
  - Routes API           directions + ETAs + traffic
  - Places API (New)     nearby place search via text query
  - Geocoding API        only when an address-string origin needs lat/lon for nearby search
"""

import json
import os
import subprocess
import urllib.parse
import urllib.request
from mcp.server.fastmcp import FastMCP

API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
HOME_ADDRESS = os.environ.get("HOME_ADDRESS", "")

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
PLACES_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

_MODE_MAP = {
    "driving":   "DRIVE",
    "drive":     "DRIVE",
    "car":       "DRIVE",
    "walk":      "WALK",
    "walking":   "WALK",
    "bike":      "BICYCLE",
    "biking":    "BICYCLE",
    "bicycle":   "BICYCLE",
    "transit":   "TRANSIT",
    "bus":       "TRANSIT",
    "train":     "TRANSIT",
}

_MODE_VERB = {
    "DRIVE":   "drive",
    "WALK":    "walk",
    "BICYCLE": "bike ride",
    "TRANSIT": "trip",
}

_MAPS_URL_MODE = {
    "DRIVE":   "driving",
    "WALK":    "walking",
    "BICYCLE": "bicycling",
    "TRANSIT": "transit",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post_json(url: str, payload: dict, headers: dict, timeout: int = 8) -> dict:
    body = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", **headers}
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _get_json(url: str, timeout: int = 6) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Friday-AI/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Location resolution
# ---------------------------------------------------------------------------

def _parse_latlng(text: str) -> tuple[float, float] | None:
    parts = text.split(",")
    if len(parts) != 2:
        return None
    try:
        return float(parts[0].strip()), float(parts[1].strip())
    except ValueError:
        return None


def _ip_latlng() -> tuple[float, float]:
    data = _get_json("http://ip-api.com/json/?fields=lat,lon,status", timeout=4)
    if data.get("status") != "success":
        raise RuntimeError("IP geolocation failed.")
    return float(data["lat"]), float(data["lon"])


def _geocode_address(address: str) -> tuple[float, float] | None:
    params = urllib.parse.urlencode({"address": address, "key": API_KEY})
    try:
        data = _get_json(f"{GEOCODE_URL}?{params}")
    except Exception:
        return None
    if data.get("status") != "OK" or not data.get("results"):
        return None
    loc = data["results"][0]["geometry"]["location"]
    return float(loc["lat"]), float(loc["lng"])


def _resolve_origin_str(origin: str) -> str:
    """Pick the origin as a string (address or 'lat,lon'). Used by Routes API,
    which can geocode addresses internally — no extra round-trip needed."""
    origin = origin.strip()
    if origin:
        return origin
    if HOME_ADDRESS:
        return HOME_ADDRESS
    lat, lng = _ip_latlng()
    return f"{lat},{lng}"


def _resolve_origin_latlng(origin: str) -> tuple[float, float]:
    """Always return (lat, lon). Used for Places nearby bias, which requires it."""
    origin = origin.strip()
    if origin:
        coords = _parse_latlng(origin)
        if coords:
            return coords
        coords = _geocode_address(origin)
        if coords:
            return coords
        # Fall through to IP if geocoding failed.
    if HOME_ADDRESS:
        coords = _parse_latlng(HOME_ADDRESS)
        if coords:
            return coords
        coords = _geocode_address(HOME_ADDRESS)
        if coords:
            return coords
    return _ip_latlng()


def _waypoint(text: str) -> dict:
    """Build a Routes API waypoint from a string (address or 'lat,lon')."""
    coords = _parse_latlng(text)
    if coords:
        return {"location": {"latLng": {"latitude": coords[0], "longitude": coords[1]}}}
    return {"address": text}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _parse_duration_seconds(value) -> int:
    """Routes API returns durations as 'NNNs' strings."""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value or "0s").rstrip("s")
    try:
        return int(s)
    except ValueError:
        return 0


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} seconds"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, mins = divmod(minutes, 60)
    if mins == 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours} hour{'s' if hours != 1 else ''} {mins} minute{'s' if mins != 1 else ''}"


def _format_distance(meters: int) -> str:
    miles = meters * 0.000621371
    if miles < 0.1:
        return f"{round(meters * 3.281)} feet"
    if miles < 10:
        return f"{miles:.1f} miles"
    return f"{round(miles)} miles"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def register(mcp: FastMCP):

    @mcp.tool(name="get_directions")
    def get_directions(destination: str, origin: str = "", mode: str = "driving") -> str:
        """Open Google Maps with a route AND speak the ETA.
        Use for 'directions to X', 'navigate to Y', 'route to the airport'.
        Mode is 'driving' (default), 'walking', 'biking', or 'transit'.
        Leave origin empty to use the user's home or current location."""
        if not API_KEY:
            return "Maps isn't configured — GOOGLE_MAPS_API_KEY is missing."
        try:
            origin_str = _resolve_origin_str(origin)
        except Exception:
            return "I couldn't determine your starting location, sir."

        travel_mode = _MODE_MAP.get(mode.lower().strip(), "DRIVE")

        payload = {
            "origin": _waypoint(origin_str),
            "destination": _waypoint(destination),
            "travelMode": travel_mode,
        }
        if travel_mode == "DRIVE":
            payload["routingPreference"] = "TRAFFIC_AWARE"

        headers = {
            "X-Goog-Api-Key": API_KEY,
            "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
        }

        try:
            data = _post_json(ROUTES_URL, payload, headers)
        except Exception:
            return "The maps service didn't respond, sir."

        routes = data.get("routes")
        if not routes:
            return f"No route found to '{destination}'."

        r = routes[0]
        eta = _format_duration(_parse_duration_seconds(r.get("duration")))
        dist = _format_distance(int(r.get("distanceMeters", 0)))
        verb = _MODE_VERB[travel_mode]

        # Open the route in the default browser via Google Maps URL.
        try:
            params = urllib.parse.urlencode({
                "api": 1,
                "origin": origin_str,
                "destination": destination,
                "travelmode": _MAPS_URL_MODE[travel_mode],
            })
            url = f"https://www.google.com/maps/dir/?{params}"
            subprocess.Popen(
                f'cmd /c start "" "{url}"',
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass  # Speaking the ETA is the main payload; opening the map is bonus.

        return f"Pulled up the route, sir — about a {eta} {verb}, roughly {dist}."

    @mcp.tool(name="get_travel_time")
    def get_travel_time(destination: str, origin: str = "", mode: str = "driving") -> str:
        """Get just the ETA to a destination, no map opened.
        Use for 'how long to X', 'ETA to the airport', 'time to drive to Y'.
        Mode is 'driving' (default), 'walking', 'biking', or 'transit'."""
        if not API_KEY:
            return "Maps isn't configured — GOOGLE_MAPS_API_KEY is missing."
        try:
            origin_str = _resolve_origin_str(origin)
        except Exception:
            return "I couldn't determine your starting location, sir."

        travel_mode = _MODE_MAP.get(mode.lower().strip(), "DRIVE")

        payload = {
            "origin": _waypoint(origin_str),
            "destination": _waypoint(destination),
            "travelMode": travel_mode,
        }
        if travel_mode == "DRIVE":
            payload["routingPreference"] = "TRAFFIC_AWARE"

        headers = {
            "X-Goog-Api-Key": API_KEY,
            "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
        }

        try:
            data = _post_json(ROUTES_URL, payload, headers)
        except Exception:
            return "The maps service didn't respond, sir."

        routes = data.get("routes")
        if not routes:
            return f"No route found to '{destination}'."

        eta = _format_duration(_parse_duration_seconds(routes[0].get("duration")))
        return f"About {eta}."

    @mcp.tool(name="find_nearby_place")
    def find_nearby_place(query: str, radius_m: int = 5000) -> str:
        """Find a nearby place matching a description.
        Use for 'nearest coffee shop', 'find a pharmacy near me', 'closest gas station'.
        Returns the name and address of the top match.
        radius_m defaults to 5000 (about 3 miles), max 50000."""
        if not API_KEY:
            return "Maps isn't configured — GOOGLE_MAPS_API_KEY is missing."
        try:
            lat, lng = _resolve_origin_latlng("")
        except Exception:
            return "I couldn't determine your location, sir."

        radius = float(max(100, min(50000, radius_m)))
        payload = {
            "textQuery": query,
            "maxResultCount": 3,
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": radius,
                }
            },
        }

        headers = {
            "X-Goog-Api-Key": API_KEY,
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress",
        }

        try:
            data = _post_json(PLACES_TEXT_URL, payload, headers)
        except Exception:
            return "The maps service didn't respond, sir."

        places = data.get("places")
        if not places:
            return f"I couldn't find anything matching '{query}' nearby."

        p = places[0]
        name = (p.get("displayName") or {}).get("text", query)
        addr = p.get("formattedAddress", "")
        return f"{name}, at {addr}." if addr else f"{name}."
