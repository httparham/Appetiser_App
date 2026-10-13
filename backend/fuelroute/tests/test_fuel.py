from django.test import SimpleTestCase

from fuelroute.fuel import OutOfRange, collapse_stops, plan_fuel


def station(mile, price, detour=0, name=None):
    return {
        "mile": mile,
        "price": price,
        "detour": detour,
        "name": name or f"stop-{mile}",
    }


class CollapseTests(SimpleTestCase):
    def test_keeps_the_cheaper_stop_in_a_cluster(self):
        picks = collapse_stops([
            station(10, 4.00, name="dear"),
            station(10.4, 3.10, name="cheap"),
            station(14, 3.50, name="later"),
        ])
        names = [item["name"] for item in picks]
        self.assertEqual(names, ["cheap", "later"])


class FuelPlanTests(SimpleTestCase):
    def test_short_trip_buys_nothing(self):
        stops = plan_fuel([station(100, 3.5)], distance_miles=400)
        self.assertEqual(stops, [])

    def test_prefers_the_cheaper_reachable_stop(self):
        stops = plan_fuel(
            [station(100, 5.00, name="close"), station(400, 2.00, name="cheap")],
            distance_miles=600,
        )
        self.assertEqual([item["name"] for item in stops], ["cheap"])
        self.assertAlmostEqual(stops[0]["miles_bought"], 100, places=3)

    def test_partial_fill_reaches_a_cheaper_stop(self):
        stops = plan_fuel(
            [station(450, 5.00, name="pricey"), station(700, 2.00, name="cheap")],
            distance_miles=900,
        )
        self.assertEqual([item["name"] for item in stops], ["pricey", "cheap"])
        self.assertAlmostEqual(stops[0]["miles_bought"], 200, places=3)
        self.assertAlmostEqual(stops[1]["miles_bought"], 200, places=3)

    def test_detour_is_fuel_that_has_to_be_bought(self):
        stops = plan_fuel(
            [station(200, 4.00, detour=10, name="offramp")],
            distance_miles=600,
        )
        self.assertEqual(len(stops), 1)
        self.assertAlmostEqual(stops[0]["miles_bought"], 120, places=3)

    def test_gap_longer_than_the_tank_fails(self):
        with self.assertRaises(OutOfRange):
            plan_fuel([station(100, 3.00)], distance_miles=1200)

    def test_uses_tank_room_at_a_cheap_stop(self):
        stops = plan_fuel(
            [station(100, 2.00, name="cheap"), station(400, 5.00, name="dear")],
            distance_miles=700,
        )
        cheap = next(item for item in stops if item["name"] == "cheap")
        dear = next(item for item in stops if item["name"] == "dear")
        self.assertAlmostEqual(cheap["miles_bought"], 100, places=3)
        self.assertAlmostEqual(dear["miles_bought"], 100, places=3)

    def test_does_not_buy_at_a_pricier_stop_it_can_pass(self):
        stops = plan_fuel(
            [station(400, 2.00, name="cheap"), station(420, 4.00, name="pricey")],
            distance_miles=800,
        )
        self.assertEqual([item["name"] for item in stops], ["cheap"])
        self.assertAlmostEqual(stops[0]["miles_bought"], 300, places=3)
