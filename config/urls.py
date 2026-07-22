from django.contrib import admin
from django.urls import include, path

from config.views import frontend_check, home

urlpatterns = [
    path("", home, name="home"),
    path("frontend-check/", frontend_check, name="frontend_check"),
    path("accounts/", include("accounts.urls")),
    path("admin/", admin.site.urls),
]
