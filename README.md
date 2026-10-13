# Appetiser_App

# Fuel route

Django app that takes a start and a finish in the US, draws the driving route, and picks truck-stop fuel stops from `fuel-prices-for-be-assessment.csv`.

The car has a 500-mile tank and gets 10 miles per gallon. It starts full. Fuel already in the tank is not added to the total, because the price file has no rate for it. Stops are chosen so the money spent on the fuel you still have to buy stays as low as it can without the tank running dry.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt
cd backend
python manage.py migrate
python manage.py import_stations
python manage.py runserver
```

`import_stations` reads the CSV at the repo root, keeps the lowest price when a stop is listed more than once, and matches each city to a coordinate. The US list is the Census places gazetteer. Canadian cities in the file come from GeoNames. Both downloads happen on the first import and are saved under `backend/data/gazetteer/`.

The virtualenv, the database, the gazetteer download, and cached routes are gitignored.

## API

`POST /api/route/`

```bash
curl -s http://127.0.0.1:8000/api/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start": "Chicago, IL", "finish": "Dallas, TX"}'
```

The JSON includes the route line, each stop (name, price, gallons, cost, mile marker), the total gallons, the total dollars, and a `map_url`.

A trip under 500 miles comes back with no stops and a fuel total of 0. If the car cannot reach the finish within range, the status is 422.

Open the `map_url`, or go to [http://127.0.0.1:8000/map/](http://127.0.0.1:8000/map/) and enter the same cities.

Use a city and state, like `Denver, CO`. Both ends have to be in the US.

## How a request is answered

City names are resolved from the local place list. The Census geocoder is called only when a name is missing from that list.

The drive itself is one request to the public OSRM server (`router.project-osrm.org`). The response is cached on disk, so repeating the same trip does not call OSRM again. Stops are chosen locally from stations within about 20 miles of the route.

## Assumptions

- Coordinates are the city the stop is listed in, not the highway exit. The exit text in the CSV does not geocode cleanly, so a stop can sit a few miles off the interchange. The 20-mile corridor is there for that reason.
- Stations that land within a mile of each other along the route are treated as one stop, and the cheaper price wins.
- Pulling off the highway and back counts against the tank.
- Routes come from OSRM, using OpenStreetMap data. The map tiles are Esri's street map. Give them credit if you demo this.

## Demo notes

A short trip, `New York, NY` to `Philadelphia, PA`, should show an empty fuel log. A long one, `Seattle, WA` to `Miami, FL`, should show several stops and a dollar total. The map page and Postman are showing the same calculation.
