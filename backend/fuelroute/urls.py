from django.urls import path

from . import views

urlpatterns = [
    path("api/route/", views.route_api, name="route_api"),
    path("api/places/", views.place_suggest, name="place_suggest"),
    path("map/", views.map_page, name="map"),
    path("", views.map_page, name="home"),
]
