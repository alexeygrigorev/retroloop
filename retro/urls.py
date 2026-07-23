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
    # Decisions and action items (#17). The list hangs off the retrospective;
    # each entry is then addressed by its own integer pk. A decision and an
    # action item are not cards, so they carry no `public_id` — item 9 is about
    # `Card` — and their pk in a URL exposes no submission order.
    path(
        "retrospectives/<int:pk>/outcomes/",
        views.retro_outcomes,
        name="retro-outcomes",
    ),
    path(
        "retrospectives/<int:pk>/decisions/new/",
        views.decision_create,
        name="decision-create",
    ),
    path("decisions/<int:pk>/edit/", views.decision_edit, name="decision-edit"),
    path("decisions/<int:pk>/delete/", views.decision_delete, name="decision-delete"),
    path(
        "retrospectives/<int:pk>/action-items/new/",
        views.action_item_create,
        name="action-item-create",
    ),
    path("action-items/<int:pk>/edit/", views.action_item_edit, name="action-item-edit"),
    path("action-items/<int:pk>/delete/", views.action_item_delete, name="action-item-delete"),
    path("action-items/<int:pk>/status/", views.action_item_toggle, name="action-item-toggle"),
]
