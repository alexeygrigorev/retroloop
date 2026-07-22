from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.views import RedirectURLMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, resolve_url
from django.views.generic import CreateView

from accounts.forms import SignupForm


class SignupView(RedirectURLMixin, CreateView):
    """Create the account and start the session in one step.

    `RedirectURLMixin` gives signup the same `?next=` handling the login view
    already has, and it matters for the same reason: someone who follows a
    project's join link without an account has to land back on that link once
    they have one, not on the homepage. The mixin discards off-site values.
    """

    form_class = SignupForm
    template_name = "accounts/signup.html"

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect(self.get_success_url())
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form: SignupForm) -> HttpResponse:
        response = super().form_valid(form)
        login(self.request, self.object)
        return response

    def get_default_redirect_url(self) -> str:
        return resolve_url(settings.LOGIN_REDIRECT_URL)

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        # Carried through the form, so it survives a validation error.
        context[self.redirect_field_name] = self.get_redirect_url()
        return context
