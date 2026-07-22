from django.contrib import admin
from django.urls import include, path

from config.views import frontend_check, home
from projects.views import join_project

urlpatterns = [
    path("", home, name="home"),
    path("frontend-check/", frontend_check, name="frontend_check"),
    path("accounts/", include("accounts.urls")),
    path("projects/", include("projects.urls")),
    # Opening a cycle lives under its project, the cycle itself under /cycles/,
    # so the app owns both halves of its URL space in one file.
    path("", include("cycles.urls")),
    # Short and shareable, and outside /projects/ because the person opening it
    # is not a member of anything yet.
    path("join/<uuid:token>/", join_project, name="join-project"),
    path("admin/", admin.site.urls),
]
