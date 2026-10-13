from django.db import models


class Place(models.Model):
    """City or town used to resolve a typed location. Coordinates come from
    the census gazetteer (US) or GeoNames (Canada), not from a live geocoder.
    """

    name = models.CharField(max_length=180)
    state = models.CharField(max_length=2)
    label = models.CharField(max_length=180)
    latitude = models.FloatField()
    longitude = models.FloatField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["name", "state"], name="place_name_state"),
        ]

    def __str__(self):
        return f"{self.label}, {self.state}"


class Station(models.Model):
    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=80)
    address = models.CharField(max_length=120)
    city = models.CharField(max_length=80)
    state = models.CharField(max_length=2)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    price = models.DecimalField(max_digits=6, decimal_places=4)
    latitude = models.FloatField()
    longitude = models.FloatField()

    class Meta:
        indexes = [
            models.Index(fields=["latitude", "longitude"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state})"
