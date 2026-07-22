from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST


def home(request: HttpRequest) -> HttpResponse:
    return render(request, "home.html")


@require_POST
def frontend_check(request: HttpRequest) -> HttpResponse:
    """Return the htmx fragment on its own — no base layout around it.

    The template partial lives in home.html, so the fragment and the page that
    swaps it in stay together. POST rather than GET so the round trip exercises
    the CSRF token htmx sends with every request.
    """
    return render(
        request,
        "home.html#frontend_check",
        {"served_at": timezone.now().strftime("%H:%M:%S")},
    )
