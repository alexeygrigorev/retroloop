from django.urls import path

from board import views

urlpatterns = [
    # No trailing slash, and deliberately so: this is the URL #11 names and the
    # one #14 polls every 1.5s per open board. Django's APPEND_SLASH only
    # redirects when nothing matches, and this pattern matches, so the poll
    # never pays for a 301.
    path("retros/<int:pk>/state", views.board_state_view, name="board-state"),
    # The writes (#12), under the same prefix and in the same style. The
    # retrospective is in the URL because it is what the request acts on and
    # what gets locked; the card and cluster the request names are in the body,
    # so that resolving them is this app's rule — a card is found by its
    # `public_id` and an integer is a 404 — and not the URL resolver's.
    path("retros/<int:pk>/cards/move", views.card_move_view, name="board-card-move"),
    path("retros/<int:pk>/cards/ungroup", views.card_ungroup_view, name="board-card-ungroup"),
    path("retros/<int:pk>/clusters/create", views.cluster_create_view, name="board-cluster-create"),
    path("retros/<int:pk>/clusters/rename", views.cluster_rename_view, name="board-cluster-rename"),
    path("retros/<int:pk>/clusters/merge", views.cluster_merge_view, name="board-cluster-merge"),
    path("retros/<int:pk>/clusters/split", views.cluster_split_view, name="board-cluster-split"),
    path("retros/<int:pk>/clusters/delete", views.cluster_delete_view, name="board-cluster-delete"),
    # Voting (#15), same prefix and style. Cast and withdraw are POSTs that
    # answer with the board; progress is a facilitator-only GET returning a bare
    # count of how many members have spent every vote.
    path("retros/<int:pk>/votes/cast", views.vote_cast_view, name="board-vote-cast"),
    path("retros/<int:pk>/votes/withdraw", views.vote_withdraw_view, name="board-vote-withdraw"),
    path("retros/<int:pk>/votes/progress", views.vote_progress_view, name="board-vote-progress"),
]
