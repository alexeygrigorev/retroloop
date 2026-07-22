"""The forms that open a cycle and write a card into one.

Everything a form refuses is refused for the same reason: the database would
otherwise refuse it with an `IntegrityError`, which reaches the user as a 500
instead of as a sentence. The constraints stay in the database — this is the
polite reading of them, not a replacement.
"""

from typing import ClassVar

from django import forms
from django.contrib.auth import get_user_model
from django.utils.formats import date_format

from cycles.models import CARD_TEXT_MAX_LENGTH, Card, FeedbackCycle, monday_of
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


class CardForm(forms.ModelForm):
    """Write or re-word one card.

    Neither the cycle, the category nor the author is a field. The first two
    come from the URL and the third from the session, so none of them can be
    supplied by the request — a card can never be posted into someone else's
    cycle, into a section it is not under, or in another member's name.

    `is_anonymous` is a field on both create and edit, because the checkbox may
    still be changed while the cycle is collecting.
    """

    class Meta:
        model = Card
        fields: ClassVar[list[str]] = ["text", "is_anonymous"]
        widgets: ClassVar[dict[str, forms.Widget]] = {
            "text": forms.Textarea(
                attrs={
                    "rows": 2,
                    # A browser-side courtesy that stops the typing, next to the
                    # server-side cap the field length already enforces.
                    "maxlength": CARD_TEXT_MAX_LENGTH,
                    "placeholder": "One short thing.",
                    # The remaining-characters counter, kept next to the widget
                    # that feeds it. `maxLength` is read off the element, so the
                    # cap is written once, in `CARD_TEXT_MAX_LENGTH`.
                    "@input": "remaining = $event.target.maxLength - $event.target.value.length",
                }
            ),
        }
        labels: ClassVar[dict[str, str]] = {
            "text": "Your card",
            "is_anonymous": "Post this anonymously",
        }
        help_texts: ClassVar[dict[str, str]] = {
            # Next to the checkbox, in words, because the consequence is
            # permanent and `_docs/decisions.md` item 3 has no way back from it.
            "is_anonymous": (
                "When the retrospective starts, your name is removed from this card "
                "permanently. That cannot be undone, and nobody can look it up afterwards."
            ),
        }

    def clean_text(self) -> str:
        """Reject nothing-but-space, and store what is left without it.

        A card of spaces is an empty card that passes `required`, so the check
        is on the stripped value; the stripped value is also what gets stored,
        so no row carries padding a member cannot see.
        """
        text = self.cleaned_data["text"].strip()
        if not text:
            raise forms.ValidationError("A card needs some words on it.")
        return text
