from django.urls import path

from projects import views

urlpatterns = [
    path("", views.project_list, name="project-list"),
    path("new/", views.project_create, name="project-create"),
    path("<int:pk>/", views.project_detail, name="project-detail"),
    path("<int:pk>/rotate-link/", views.rotate_join_token, name="project-rotate-link"),
]
