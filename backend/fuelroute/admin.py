from django.contrib import admin

from .models import Place, Station


@admin.register(Place)
class PlaceAdmin(admin.ModelAdmin):
    list_display = ("label", "state", "latitude", "longitude")
    list_filter = ("state",)
    search_fields = ("label", "name")


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "state", "price")
    list_filter = ("state",)
    search_fields = ("name", "city", "opis_id")
