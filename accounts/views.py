from django.conf import settings
from django.contrib.auth import login
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.generic import CreateView

from accounts.forms import SignupForm


class SignupView(CreateView):
    """Create the account and start the session in one step."""

    form_class = SignupForm
    template_name = "accounts/signup.html"

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect(settings.LOGIN_REDIRECT_URL)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form: SignupForm) -> HttpResponse:
        response = super().form_valid(form)
        login(self.request, self.object)
        return response

    def get_success_url(self) -> str:
        return settings.LOGIN_REDIRECT_URL
