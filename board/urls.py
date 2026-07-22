from django.urls import path

from board import views

urlpatterns = [
    # No trailing slash, and deliberately so: this is the URL #11 names and the
    # one #14 polls every 1.5s per open board. Django's APPEND_SLASH only
    # redirects when nothing matches, and this pattern matches, so the poll
    # never pays for a 301.
    path("retros/<int:pk>/state", views.board_state_view, name="board-state"),
]
