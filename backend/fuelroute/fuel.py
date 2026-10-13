"""Choose where to buy fuel along a measured route.

The tank starts full, and that fuel is not billed. From a stop, the car
buys either just enough to reach the next stop or a full tank, whichever
costs less once later prices are taken into account.
"""


class OutOfRange(Exception):
    pass


def collapse_stops(stations, gap_miles=1):
    """One stop per cluster of stations sitting on top of each other."""
    ordered = sorted(stations, key=lambda station: station["mile"])
    groups = []
    for station in ordered:
        if groups and station["mile"] - groups[-1][0]["mile"] <= gap_miles:
            groups[-1].append(station)
        else:
            groups.append([station])
    return [
        min(group, key=lambda station: (float(station["price"]), station["detour"]))
        for group in groups
    ]


def _leg(origin, dest):
    """Miles burned leaving origin's pump and arriving at dest's pump."""
    return origin["detour"] + (dest["mile"] - origin["mile"]) + dest["detour"]


def _pareto(bucket):
    """Drop arrivals that cost at least as much as another arrival with more fuel."""
    best_cost = float("inf")
    kept = []
    for opt in sorted(bucket.values(), key=lambda item: item["fuel"], reverse=True):
        if opt["cost"] < best_cost - 1e-9:
            kept.append(opt)
            best_cost = opt["cost"]
    return kept


def plan_fuel(stations, distance_miles, tank_miles=500, mpg=10):
    distance = float(distance_miles)
    tank = float(tank_miles)
    if distance <= tank:
        return []

    points = [{"mile": 0.0, "price": 0, "detour": 0.0, "start": True}]
    points.extend(sorted(stations, key=lambda station: station["mile"]))
    points.append({"mile": distance, "price": 0, "detour": 0.0, "dest": True})

    # Fuel left on arrival, in whole miles. One cheapest way to get there.
    options = [{} for _ in points]
    options[0][int(tank)] = {
        "cost": 0.0,
        "fuel": tank,
        "prev": None,
        "buy": 0.0,
        "station": None,
    }

    for index, here in enumerate(points[:-1]):
        front = _pareto(options[index])
        if not front:
            continue
        reachable = []
        for later in range(index + 1, len(points)):
            if points[later]["mile"] - here["mile"] > tank + 1e-6:
                break
            dist = _leg(here, points[later])
            if dist <= tank + 1e-6:
                reachable.append((later, dist))

        price = float(here["price"])
        for opt in front:
            fuel = opt["fuel"]
            room = tank - fuel
            for later, dist in reachable:
                if here.get("start"):
                    buys = (0.0,) if dist <= fuel + 1e-6 else ()
                else:
                    buys = []
                    needed = dist - fuel
                    if needed <= 1e-6:
                        buys.append(0.0)
                    elif needed <= room + 1e-6:
                        buys.append(needed)
                    if room > 1e-6 and (not buys or room - buys[-1] > 0.5):
                        buys.append(room)
                for buy in buys:
                    if fuel + buy + 1e-6 < dist:
                        continue
                    arrive = int(fuel + buy - dist)
                    if arrive < 0:
                        continue
                    cost = opt["cost"] + (buy / mpg) * price
                    current = options[later].get(arrive)
                    if current is not None and current["cost"] <= cost:
                        continue
                    options[later][arrive] = {
                        "cost": cost,
                        "fuel": float(arrive),
                        "prev": opt,
                        "buy": buy,
                        "station": here if buy > 1e-4 and not here.get("start") else None,
                    }

    if not options[-1]:
        raise OutOfRange("No fuel stop is in range on this route. The car would run dry.")

    chosen = min(options[-1].values(), key=lambda opt: opt["cost"])
    stops = []
    while chosen is not None:
        if chosen["station"] is not None:
            stop = dict(chosen["station"])
            stop["miles_bought"] = chosen["buy"]
            stops.append(stop)
        chosen = chosen["prev"]
    stops.reverse()
    return stops
