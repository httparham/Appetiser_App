import csv
import io
import json
import re
import zipfile
from pathlib import Path

import requests
from django.conf import settings

from .models import Place

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL",
    "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME",
    "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI",
    "WY",
}

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

PROVINCE_NAMES = {
    "alberta": "AB",
    "british columbia": "BC",
    "manitoba": "MB",
    "new brunswick": "NB",
    "newfoundland and labrador": "NL",
    "nova scotia": "NS",
    "ontario": "ON",
    "prince edward island": "PE",
    "quebec": "QC",
    "saskatchewan": "SK",
    "yukon": "YT",
    "northwest territories": "NT",
    "nunavut": "NU",
}

# Trailing words the census adds. "Kansas City city" is Kansas City.
# Only applied to gazetteer names, never to a city typed by a person.
_DESIGNATIONS = (
    " city and borough",
    " metropolitan government",
    " metro government",
    " unified government",
    " consolidated government",
    " charter township",
    " metro township",
    " municipality",
    " comunidad",
    " zona urbana",
    " urbanizacion",
    " township",
    " plantation",
    " borough",
    " village",
    " city",
    " town",
    " cdp",
    " gore",
)

# Lower is a better match when two places share a name.
_LSAD_RANK = {
    "25": 0,  # city
    "21": 0,  # city and borough
    "43": 1,  # town
    "37": 2,  # borough
    "47": 2,  # village
    "57": 5,  # CDP
}

_GEONAME_RANK = {
    "PPLC": 0,
    "PPLA": 1,
    "PPLA2": 2,
    "PPLA3": 3,
    "PPLA4": 4,
    "PPL": 5,
}

US_GAZETTEER_URLS = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_place_national.zip",
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_place_national.zip",
)
CA_GAZETTEER_URL = "https://download.geonames.org/export/dump/CA.zip"
ADMIN1_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"

_CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
_places = None
_place_count = None


class PlaceLookupError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def normalize_place_name(value):
    text = value.strip().lower().replace(".", "")
    text = text.replace("saint ", "st ")
    text = text.replace("mount ", "mt ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_designation(value):
    text = re.sub(r"\s*\(balance\)\s*$", "", value.strip(), flags=re.IGNORECASE)
    lowered = text.lower()
    for suffix in _DESIGNATIONS:
        if lowered.endswith(suffix) and len(lowered) > len(suffix):
            return text[: -len(suffix)].strip()
    return text


def _name_forms(original):
    """Official census name, plus the city in a consolidated-government name."""
    text = re.sub(r"\s*\(balance\)\s*$", "", original.strip(), flags=re.IGNORECASE)
    forms = [text]
    consolidated = re.match(
        r"^(.+?)[/\-][A-Za-z .]+ County (?:unified|consolidated|metro|metropolitan) government$",
        text,
        flags=re.IGNORECASE,
    )
    metro = re.match(r"^(.+?)-[A-Za-z .]+ metropolitan government$", text, flags=re.IGNORECASE)
    comma = re.match(r"^([^,]+),\s+.+ County metropolitan government$", text, flags=re.IGNORECASE)
    silver = re.match(r"^([A-Za-z .]+)-Silver Bow$", text)
    for match in (consolidated, metro, comma, silver):
        if match:
            forms.append(match.group(1).strip())
    return forms


def parse_location(text):
    raw = (text or "").strip()
    if not raw:
        return None
    if "," in raw:
        city, region = raw.rsplit(",", 1)
    else:
        parts = raw.rsplit(None, 1)
        if len(parts) != 2:
            return None
        city, region = parts
    city = city.strip()
    region = region.strip()
    if not city or not region:
        return None
    upper = region.upper()
    if len(upper) == 2 and upper.isalpha():
        return city, upper
    code = STATE_NAMES.get(region.lower())
    if code:
        return city, code
    return None


def _headers():
    return {"User-Agent": settings.OSRM_USER_AGENT}


def _download(url, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return
    response = requests.get(url, headers=_headers(), timeout=90)
    response.raise_for_status()
    dest.write_bytes(response.content)


def _remember(index, name, state, label, lat, lon, rank, size):
    key = (name, state)
    current = index.get(key)
    if current is None or rank < current["rank"] or (
        rank == current["rank"] and size > current["size"]
    ):
        index[key] = {
            "name": name,
            "state": state,
            "label": label,
            "latitude": lat,
            "longitude": lon,
            "rank": rank,
            "size": size,
        }


def _read_us_places(path: Path):
    found = {}
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".txt")]
        if not names:
            raise RuntimeError(f"No text file inside {path.name}")
        raw = archive.read(names[0])
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    reader.fieldnames = [name.strip() for name in reader.fieldnames]
    for row in reader:
        state = (row.get("USPS") or "").strip().upper()
        if state not in US_STATES:
            continue
        status = (row.get("FUNCSTAT") or "A").strip().upper()
        if status in {"I", "N"}:
            continue
        try:
            lat = float(row["INTPTLAT"])
            lon = float(row["INTPTLONG"])
            area = float(row.get("ALAND") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        original = (row.get("NAME") or "").strip()
        if not original:
            continue
        rank = _LSAD_RANK.get((row.get("LSAD") or "").strip(), 3)
        if status == "F":
            rank = min(rank, 0)
        for form in _name_forms(original):
            label = _strip_designation(form)
            name = normalize_place_name(label)
            if not name:
                continue
            _remember(found, name, state, label, lat, lon, rank, area)
    return found


def _province_codes(admin_path: Path):
    codes = {}
    for line in admin_path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].startswith("CA."):
            continue
        admin_code = parts[0].split(".", 1)[1]
        abbr = PROVINCE_NAMES.get(parts[1].strip().lower())
        if abbr:
            codes[admin_code] = abbr
    return codes


def _read_ca_places(path: Path, admin_path: Path):
    provinces = _province_codes(admin_path)
    found = {}
    with zipfile.ZipFile(path) as archive:
        member = next(name for name in archive.namelist() if name.upper().startswith("CA"))
        with archive.open(member) as handle:
            for raw in handle:
                parts = raw.decode("utf-8", errors="replace").rstrip("\n").split("\t")
                if len(parts) < 15 or parts[6] != "P":
                    continue
                state = provinces.get(parts[10])
                if not state:
                    continue
                try:
                    lat = float(parts[4])
                    lon = float(parts[5])
                    population = float(parts[14] or 0)
                except ValueError:
                    continue
                label = parts[1].strip() or parts[2].strip()
                name = normalize_place_name(label)
                if not name:
                    continue
                rank = _GEONAME_RANK.get(parts[7], 6)
                _remember(found, name, state, label, lat, lon, rank, population)
    return found


def load_gazetteer():
    """Download place files if needed and return the best row per city/state."""
    folder = Path(settings.GAZETTEER_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    us_path = folder / "us_places.zip"
    last_error = None
    for url in US_GAZETTEER_URLS:
        try:
            _download(url, us_path)
            us_places = _read_us_places(us_path)
            break
        except (requests.RequestException, zipfile.BadZipFile, RuntimeError, OSError) as exc:
            last_error = exc
            if us_path.exists():
                us_path.unlink()
    else:
        raise RuntimeError(f"Could not download the US place list: {last_error}")

    places = dict(us_places)
    try:
        ca_path = folder / "CA.zip"
        admin_path = folder / "admin1CodesASCII.txt"
        _download(CA_GAZETTEER_URL, ca_path)
        _download(ADMIN1_URL, admin_path)
        places.update(_read_ca_places(ca_path, admin_path))
    except (requests.RequestException, zipfile.BadZipFile, StopIteration, OSError):
        # US cities are enough to route the assignment. Canada is a bonus.
        pass
    return places


def place_index():
    global _places, _place_count
    count = Place.objects.count()
    if _places is None or count != _place_count:
        _place_count = count
        _places = {(place.name, place.state): place for place in Place.objects.all()}
    return _places


def clear_place_index():
    global _places, _place_count
    _places = None
    _place_count = None


def _miss_path():
    return Path(settings.BASE_DIR) / ".cache" / "place_misses.json"


def _load_misses():
    path = _miss_path()
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        return set()


def _store_miss(key):
    misses = _load_misses()
    misses.add(key)
    path = _miss_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(misses)))


def _census_lookup(query, city, state):
    key = f"{normalize_place_name(city)}|{state}"
    if key in _load_misses():
        return None
    try:
        response = requests.get(
            _CENSUS_URL,
            params={
                "address": query,
                "benchmark": "Public_AR_Current",
                "format": "json",
            },
            headers=_headers(),
            timeout=12,
        )
        response.raise_for_status()
        matches = response.json().get("result", {}).get("addressMatches") or []
    except (requests.RequestException, ValueError) as exc:
        raise PlaceLookupError("Place lookup didn't respond. Try again.", 502) from exc
    if not matches:
        _store_miss(key)
        return None
    match = matches[0]
    components = match.get("addressComponents") or {}
    matched_state = (components.get("state") or "").strip().upper()
    if matched_state != state or matched_state not in US_STATES:
        _store_miss(key)
        return None
    coords = match.get("coordinates") or {}
    try:
        lat = float(coords["y"])
        lon = float(coords["x"])
    except (KeyError, TypeError, ValueError):
        _store_miss(key)
        return None
    label = (components.get("city") or city).title()
    place, _created = Place.objects.update_or_create(
        name=normalize_place_name(city),
        state=state,
        defaults={"label": label, "latitude": lat, "longitude": lon},
    )
    place_index()[(place.name, place.state)] = place
    return place


def resolve_us_place(text):
    parsed = parse_location(text)
    if parsed is None:
        raise PlaceLookupError("Use a city and state, like Chicago, IL.")
    city, state = parsed
    if state not in US_STATES:
        raise PlaceLookupError(f"{city}, {state} is outside the US.")
    if not place_index():
        raise PlaceLookupError("Place list is empty. Run import_stations first.")
    place = place_index().get((normalize_place_name(city), state))
    if place is None:
        place = _census_lookup(text, city, state)
    if place is None:
        raise PlaceLookupError(f"Couldn't find {city}, {state}.")
    return place
