import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuelroute.models import Place, Station
from fuelroute.places import clear_place_index, load_gazetteer, normalize_place_name


class Command(BaseCommand):
    help = "Load fuel prices and match each stop to a city coordinate."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            default=str(settings.BASE_DIR.parent / "fuel-prices-for-be-assessment.csv"),
            help="Path to the fuel price CSV.",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv"])
        if not csv_path.exists():
            raise CommandError(f"No price file at {csv_path}")

        self.stdout.write("Downloading place coordinates (first run only)...")
        try:
            gazetteer = load_gazetteer()
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Place names indexed: {len(gazetteer)}")

        grouped = self._read_prices(csv_path)
        stations = []
        unmatched = []
        for opis_id, row in grouped.items():
            key = (normalize_place_name(row["city"]), row["state"])
            place = gazetteer.get(key)
            if place is None:
                unmatched.append(f"{row['city']}, {row['state']}")
                continue
            stations.append(
                Station(
                    opis_id=opis_id,
                    name=row["name"][:80],
                    address=row["address"][:120],
                    city=row["city"][:80],
                    state=row["state"],
                    rack_id=row["rack_id"],
                    price=row["price"],
                    latitude=place["latitude"],
                    longitude=place["longitude"],
                )
            )

        place_rows = [
            Place(
                name=item["name"],
                state=item["state"],
                label=item["label"][:180],
                latitude=item["latitude"],
                longitude=item["longitude"],
            )
            for item in gazetteer.values()
        ]
        with transaction.atomic():
            Place.objects.all().delete()
            Station.objects.all().delete()
            Place.objects.bulk_create(place_rows, batch_size=1000)
            Station.objects.bulk_create(stations, batch_size=1000)
        clear_place_index()

        self.stdout.write(f"Price rows: {self.row_count}")
        self.stdout.write(f"Unique stops: {len(grouped)}")
        self.stdout.write(f"Stops with coordinates: {len(stations)}")
        self.stdout.write(f"Stops skipped: {len(unmatched)}")
        if unmatched:
            preview = ", ".join(sorted(set(unmatched))[:12])
            self.stdout.write(f"Unmatched sample: {preview}")

    def _read_prices(self, csv_path):
        grouped = {}
        self.row_count = 0
        with csv_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                self.row_count += 1
                try:
                    opis_id = int(row["OPIS Truckstop ID"])
                    price = Decimal(row["Retail Price"].strip())
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    continue
                city = (row.get("City") or "").strip()
                state = (row.get("State") or "").strip().upper()
                if not city or len(state) != 2:
                    continue
                current = grouped.get(opis_id)
                if current is not None and current["price"] <= price:
                    continue
                rack = (row.get("Rack ID") or "").strip()
                grouped[opis_id] = {
                    "name": (row.get("Truckstop Name") or "").strip(),
                    "address": (row.get("Address") or "").strip(),
                    "city": city,
                    "state": state,
                    "rack_id": int(rack) if rack.isdigit() else None,
                    "price": price,
                }
        return grouped
