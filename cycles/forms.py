"""The form that opens a cycle.

Everything the form refuses is refused for the same reason: the database would
otherwise refuse it with an `IntegrityError`, which reaches the user as a 500
instead of as a sentence. The constraints stay in the database — this is the
polite reading of them, not a replacement.
"""

from typing import ClassVar

from django import forms
from django.contrib.auth import get_user_model
from django.utils.formats import date_format

from cycles.models import FeedbackCycle, monday_of
from projects.models import Project

User = get_user_model()


class FeedbackCycleForm(forms.ModelForm):
    """Open a cycle for one project.

    The project is not a field: it comes from the URL, so it can never be
    supplied by the request. The facilitator is a field, because handing the
    role over for a single week is a thing a team does.
    """

    class Meta:
        model = FeedbackCycle
        fields: ClassVar[list[str]] = ["week_start", "opens_at", "closes_at", "facilitator"]
        widgets: ClassVar[dict[str, forms.Widget]] = {
            "week_start": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "opens_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "closes_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
        }

    def __init__(self, *args, project: Project, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.project = project
        # Only members are offered, so the dropdown neither lists the whole
        # user table nor lets someone outside the project be picked. The check
        # in clean_facilitator repeats the rule for requests that ignore the
        # dropdown.
        self.fields["facilitator"].queryset = User.objects.filter(
            memberships__project=project
        ).order_by("username")
        self.fields["facilitator"].label = "Facilitator for this week"

    def clean_week_start(self):
        """Any day of the intended week names that week's Monday."""
        week_start = self.cleaned_data["week_start"]
        return monday_of(week_start)

    def clean_facilitator(self):
        facilitator = self.cleaned_data["facilitator"]
        if not self.project.memberships.filter(user=facilitator).exists():
            raise forms.ValidationError("The facilitator has to be a member of this project.")
        return facilitator

    def clean(self):
        cleaned_data = super().clean()
        opens_at = cleaned_data.get("opens_at")
        closes_at = cleaned_data.get("closes_at")
        week_start = cleaned_data.get("week_start")

        if opens_at and closes_at and closes_at < opens_at:
            self.add_error("closes_at", "The cycle cannot close before it opens.")

        open_cycle = self.project.cycles.filter(status=FeedbackCycle.Status.COLLECTING).first()
        if open_cycle is not None:
            raise forms.ValidationError(
                f"{self.project.name} already has an open cycle, for the week of "
                f"{date_format(open_cycle.week_start, 'j F Y')}. "
                f"Close that one before opening another."
            )

        if week_start and self.project.cycles.filter(week_start=week_start).exists():
            self.add_error(
                "week_start",
                f"There is already a cycle for the week of {date_format(week_start, 'j F Y')}.",
            )

        return cleaned_data
