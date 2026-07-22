"""Auth routes, wired one by one.

`django.contrib.auth.urls` is deliberately not included: it brings the password
reset views, which need a mail backend this project does not have. Everything
here works without one.
"""

from django.contrib.auth import views as auth_views
from django.urls import path

from accounts.views import SignupView

urlpatterns = [
    path("signup/", SignupView.as_view(), name="signup"),
    path(
        "login/",
        auth_views.LoginView.as_view(redirect_authenticated_user=True),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "password_change/",
        auth_views.PasswordChangeView.as_view(),
        name="password_change",
    ),
    path(
        "password_change/done/",
        auth_views.PasswordChangeDoneView.as_view(),
        name="password_change_done",
    ),
]
