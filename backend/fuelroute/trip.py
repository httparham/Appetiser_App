import hashlib
import json
import math
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import requests
from django.conf import settings

from fuelroute.fuel import OutOfRange, collapse_stops, plan_fuel
from fuelroute.models import Station
from fuelroute.places import PlaceLookupError, resolve_us_place

EARTH_MILES = 3958.7613
METERS_PER_MILE = 1609.344
CELL = 0.5
# Wider than the 20-mile corridor so a stop just across a grid line is still seen.
CELL_BUFFER = 0.7

_station_index = None
_station_count = None


class TripError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def haversine(lat1, lon1, lat2, lon2):
    rlat1, rlon1, rlat2, rlon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_MILES * math.asin(min(1.0, math.sqrt(a)))


def _cell(lat, lon):
    return (math.floor(lat / CELL), math.floor(lon / CELL))


def _cells_for_segment(lat1, lon1, lat2, lon2):
    i0 = math.floor((min(lat1, lat2) - CELL_BUFFER) / CELL)
    i1 = math.floor((max(lat1, lat2) + CELL_BUFFER) / CELL)
    j0 = math.floor((min(lon1, lon2) - CELL_BUFFER) / CELL)
    j1 = math.floor((max(lon1, lon2) + CELL_BUFFER) / CELL)
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            yield (i, j)


def _project(lat, lon, lat1, lon1, lat2, lon2):
    scale = math.cos(math.radians((lat1 + lat2) / 2.0)) or 1e-6
    ax, ay = lon1 * scale, lat1
    bx, by = lon2 * scale, lat2
    px, py = lon * scale, lat
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom == 0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    clon = (ax + t * dx) / scale
    clat = ay + t * dy
    return t, haversine(lat, lon, clat, clon)


class StationIndex:
    def __init__(self, stations):
        self.grid = {}
        for station in stations:
            self.grid.setdefault(_cell(station.latitude, station.longitude), []).append(station)

    def along(self, coordinates, distance_miles, corridor_miles):
        # City coordinates only need the road within a couple of miles.
        # Thinning the line keeps the search off the full OSRM point list.
        points, cumulative = _measure(_thin(coordinates, min_step=4))
        if len(points) < 2:
            return []
        scale = distance_miles / cumulative[-1] if cumulative[-1] else 1.0
        segment_cells = {}
        for index in range(len(points) - 1):
            lon1, lat1 = points[index]
            lon2, lat2 = points[index + 1]
            for cell in _cells_for_segment(lat1, lon1, lat2, lon2):
                segment_cells.setdefault(cell, []).append(index)

        found = []
        for cell, indexes in segment_cells.items():
            for station in self.grid.get(cell, ()):
                best_detour = None
                best_mile = None
                for index in indexes:
                    lon1, lat1 = points[index]
                    lon2, lat2 = points[index + 1]
                    t, detour = _project(station.latitude, station.longitude, lat1, lon1, lat2, lon2)
                    if best_detour is None or detour < best_detour:
                        best_detour = detour
                        best_mile = cumulative[index] + t * (cumulative[index + 1] - cumulative[index])
                if best_detour is None or best_detour > corridor_miles:
                    continue
                best_mile *= scale
                if best_mile < 1 or best_mile >= distance_miles:
                    continue
                found.append({
                    "name": station.name,
                    "address": station.address,
                    "city": station.city,
                    "state": station.state,
                    "price": station.price,
                    "latitude": station.latitude,
                    "longitude": station.longitude,
                    "mile": best_mile,
                    "detour": best_detour,
                    "opis_id": station.opis_id,
                })
        # A station can sit in a cell the route brushes more than once.
        unique = {}
        for item in found:
            current = unique.get(item["opis_id"])
            if current is None or item["detour"] < current["detour"]:
                unique[item["opis_id"]] = item
        return list(unique.values())


def _thin(coordinates, min_step=2.0):
    if len(coordinates) < 3:
        return coordinates
    kept = [coordinates[0]]
    last = coordinates[0]
    for point in coordinates[1:-1]:
        if haversine(last[1], last[0], point[1], point[0]) >= min_step:
            kept.append(point)
            last = point
    if coordinates[-1] != kept[-1]:
        kept.append(coordinates[-1])
    return kept


def _measure(coordinates):
    points = []
    cumulative = []
    for lon, lat in coordinates:
        if points:
            step = haversine(points[-1][1], points[-1][0], lat, lon)
            if step < 1e-4:
                continue
            cumulative.append(cumulative[-1] + step)
        else:
            cumulative.append(0.0)
        points.append((lon, lat))
    return points, cumulative


def station_index():
    global _station_index, _station_count
    count = Station.objects.count()
    if _station_index is None or count != _station_count:
        _station_count = count
        _station_index = StationIndex(list(Station.objects.all()))
    return _station_index


def _cache_path(start, finish):
    key = (
        f"{start.latitude:.4f},{start.longitude:.4f}"
        f"|{finish.latitude:.4f},{finish.longitude:.4f}"
    )
    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    return Path(settings.ROUTE_CACHE_DIR) / f"{digest}.json"


def fetch_route(start, finish):
    path = _cache_path(start, finish)
    if path.exists():
        try:
            cached = json.loads(path.read_text())
            if cached.get("geometry", {}).get("coordinates"):
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    pair = f"{start.longitude},{start.latitude};{finish.longitude},{finish.latitude}"
    url = f"{settings.OSRM_BASE_URL.rstrip('/')}/route/v1/driving/{pair}"
    try:
        response = requests.get(
            url,
            params={"overview": "full", "geometries": "geojson", "steps": "false"},
            headers={"User-Agent": settings.OSRM_USER_AGENT},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise TripError("Routing service didn't respond.", 502) from exc

    routes = payload.get("routes") if payload.get("code") == "Ok" else None
    if not routes:
        raise TripError("Routing service didn't return a road route.", 502)
    route = routes[0]
    geometry = route.get("geometry") or {}
    coordinates = geometry.get("coordinates") or []
    if len(coordinates) < 2:
        raise TripError("Routing service didn't return a road route.", 502)

    parsed = {
        "distance_miles": route["distance"] / METERS_PER_MILE,
        "duration_minutes": route["duration"] / 60.0,
        "geometry": {"type": "LineString", "coordinates": coordinates},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(parsed))
    return parsed


def _money(gallons, price):
    cost = (gallons * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return cost


def plan_trip(start_text, finish_text):
    try:
        start = resolve_us_place(start_text)
        finish = resolve_us_place(finish_text)
    except PlaceLookupError as exc:
        raise TripError(str(exc), exc.status) from exc

    route = fetch_route(start, finish)
    nearby = station_index().along(
        route["geometry"]["coordinates"],
        route["distance_miles"],
        settings.STATION_CORRIDOR_MILES,
    )
    try:
        planned = plan_fuel(
            collapse_stops(nearby),
            route["distance_miles"],
            tank_miles=settings.FUEL_RANGE_MILES,
            mpg=settings.FUEL_MPG,
        )
    except OutOfRange as exc:
        raise TripError(str(exc), 422) from exc

    mpg = Decimal(str(settings.FUEL_MPG))
    stops = []
    total_gallons = Decimal("0.00")
    total_cost = Decimal("0.00")
    for item in planned:
        price = item["price"] if isinstance(item["price"], Decimal) else Decimal(str(item["price"]))
        gallons = (Decimal(str(item["miles_bought"])) / mpg).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        cost = _money(gallons, price)
        total_gallons += gallons
        total_cost += cost
        stops.append({
            "name": item["name"],
            "address": item["address"],
            "city": item["city"],
            "state": item["state"],
            "price_per_gallon": float(price.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)),
            "mile_marker": round(float(item["mile"]), 1),
            "gallons": float(gallons),
            "cost": float(cost),
            "latitude": item["latitude"],
            "longitude": item["longitude"],
        })

    return {
        "start": {
            "label": f"{start.label}, {start.state}",
            "latitude": start.latitude,
            "longitude": start.longitude,
        },
        "finish": {
            "label": f"{finish.label}, {finish.state}",
            "latitude": finish.latitude,
            "longitude": finish.longitude,
        },
        "distance_miles": round(route["distance_miles"], 1),
        "duration_minutes": int(round(route["duration_minutes"])),
        "fuel_stops": stops,
        "total_gallons": float(total_gallons),
        "total_cost": float(total_cost),
        "route": route["geometry"],
    }
