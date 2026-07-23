from django.urls import path

from projects import views

urlpatterns = [
    path("", views.project_list, name="project-list"),
    path("new/", views.project_create, name="project-create"),
    path("<int:pk>/", views.project_detail, name="project-detail"),
    path("<int:pk>/rotate-link/", views.rotate_join_token, name="project-rotate-link"),
    # The dashboard's tick-done interaction (#26). POST-only and answers with the
    # open-actions fragment, so ticking an item off drops it from the live list in
    # place. The action item is scoped to this project in the view.
    path(
        "<int:pk>/action-items/<int:item_pk>/toggle/",
        views.dashboard_action_toggle,
        name="dashboard-action-toggle",
    ),
]
