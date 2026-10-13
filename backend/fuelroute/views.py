import json
from urllib.parse import urlencode

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from fuelroute.trip import TripError, plan_trip
from fuelroute.models import Place, Station
from fuelroute.places import US_STATES, normalize_place_name

_stop_counts = None


def _stops_by_city():
    global _stop_counts
    if _stop_counts is None:
        counts = {}
        for city, state in Station.objects.values_list("city", "state"):
            key = (normalize_place_name(city), state)
            counts[key] = counts.get(key, 0) + 1
        _stop_counts = counts
    return _stop_counts


@csrf_exempt
@require_POST
def route_api(request):
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Send a JSON body."}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"error": "Send a JSON object."}, status=400)

    start = str(body.get("start") or "").strip()
    finish = str(body.get("finish") or "").strip()
    if not start or not finish:
        return JsonResponse({"error": "Start and finish are required."}, status=400)

    try:
        trip = plan_trip(start, finish)
    except TripError as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)

    trip["map_url"] = request.build_absolute_uri(
        "/map/?" + urlencode({"start": start, "finish": finish})
    )
    return JsonResponse(trip)


@require_GET
def place_suggest(request):
    raw = (request.GET.get("q") or "").strip()
    if len(raw) < 2:
        return JsonResponse({"places": []})

    state_prefix = ""
    city_raw = raw
    if "," in raw:
        city_raw, region = raw.split(",", 1)
        region = region.strip().upper()
        if region.isalpha():
            state_prefix = region[:2]

    city = normalize_place_name(city_raw)
    if len(city) < 2:
        return JsonResponse({"places": []})

    matches = Place.objects.filter(state__in=US_STATES, name__startswith=city)
    if state_prefix:
        matches = matches.filter(state__startswith=state_prefix)
    rows = list(matches.order_by("name").values_list("name", "label", "state")[:80])
    counts = _stops_by_city()
    rows.sort(key=lambda row: (
        -(counts.get((row[0], row[2]), 0) * 4),
        len(row[0]),
        row[0],
        row[2],
    ))
    return JsonResponse({"places": [f"{label}, {state}" for _name, label, state in rows[:8]]})


def map_page(request):
    start = (request.GET.get("start") or "").strip()
    finish = (request.GET.get("finish") or "").strip()
    trip = None
    error = None
    if start or finish:
        if not start or not finish:
            error = "Start and finish are both required."
        else:
            try:
                trip = plan_trip(start, finish)
            except TripError as exc:
                error = str(exc)
    return render(request, "fuelroute/map.html", {
        "start": start,
        "finish": finish,
        "trip": trip,
        "error": error,
        "duration_text": _duration_text(trip["duration_minutes"]) if trip else "",
    })


def _duration_text(minutes):
    hours, mins = divmod(int(minutes), 60)
    if hours:
        return f"{hours} h {mins} min"
    return f"{mins} min"
