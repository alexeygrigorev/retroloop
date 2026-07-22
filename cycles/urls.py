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
]
