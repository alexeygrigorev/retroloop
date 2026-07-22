from django.urls import path

from meetings import views

urlpatterns = [
    # The meeting belongs to the retrospective, so handing it over hangs off
    # that; the record it creates is addressed by itself, the same shape the
    # cycles and retro apps already use.
    path(
        "retrospectives/<int:pk>/meeting/",
        views.meeting_upload,
        name="meeting-upload",
    ),
    path(
        "meetings/records/<int:pk>/status/",
        views.meeting_record_status,
        name="meeting-record-status",
    ),
]
