from django.urls import path

from cycles import views

urlpatterns = [
    # Opening hangs off the project, because that is what a cycle is opened
    # for. Everything after that is addressed by the cycle itself.
    path(
        "projects/<int:project_pk>/cycles/new/",
        views.cycle_create,
        name="cycle-create",
    ),
    path("cycles/<int:pk>/", views.cycle_detail, name="cycle-detail"),
    path("cycles/<int:pk>/close/", views.cycle_close, name="cycle-close"),
    # The submission screen hangs off the cycle, and so does creating a card:
    # the category is in the URL rather than in the posted data, so a section a
    # card is filed under can never be a field a request supplies.
    path("cycles/<int:pk>/cards/", views.card_list, name="cycle-cards"),
    path("cycles/<int:pk>/cards/<str:category>/new/", views.card_create, name="card-create"),
    # A card is addressed by itself once it exists. Delete is POST-only; there
    # is no GET that removes anything.
    path("cards/<int:pk>/", views.card_show, name="card-show"),
    path("cards/<int:pk>/edit/", views.card_edit, name="card-edit"),
    path("cards/<int:pk>/delete/", views.card_delete, name="card-delete"),
]
