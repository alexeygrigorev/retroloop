from django.urls import path

from retro import views

urlpatterns = [
    # Starting hangs off the cycle, because that is what a retrospective
    # follows. Everything after that is addressed by the retrospective itself.
    path(
        "cycles/<int:cycle_pk>/retrospective/start/",
        views.retro_start,
        name="retro-start",
    ),
    path("retrospectives/<int:pk>/", views.retro_detail, name="retro-detail"),
    path("retrospectives/<int:pk>/advance/", views.retro_advance, name="retro-advance"),
]
