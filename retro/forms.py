"""The forms that write a decision and an action item by hand.

Both are the polite reading of a database rule: text that is only whitespace is
refused with a sentence rather than reaching the check constraint as a 500, and
an owner who is not on the project is refused before it becomes a row. The
constraints stay in the database — this is not a replacement for them.

Neither the retrospective, the source nor the review status is a field. The
retrospective comes from the URL, and the defaults on the model make a
hand-written entry `MANUAL` and `CONFIRMED` without the form saying so — a person
typing it is the review step. `created_by` is set from the session in the view,
never posted, so an entry can never be written in another member's name.
"""

from typing import ClassVar

from django import forms
from django.contrib.auth import get_user_model

from projects.models import Project
from retro.models import ActionItem, Cluster, Decision, Retrospective

User = get_user_model()


class DecisionForm(forms.ModelForm):
    """Write or re-word one decision, optionally against a cluster."""

    class Meta:
        model = Decision
        fields: ClassVar[list[str]] = ["text", "cluster"]
        widgets: ClassVar[dict[str, forms.Widget]] = {
            "text": forms.Textarea(attrs={"rows": 2, "placeholder": "What did the team decide?"}),
        }
        labels: ClassVar[dict[str, str]] = {
            "text": "The decision",
            "cluster": "About (optional)",
        }

    def __init__(self, *args, retrospective: Retrospective, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.retrospective = retrospective
        # Only this board's clusters are offered, so a decision can never be
        # attached to another retrospective's topic. Optional: a decision about
        # the retrospective as a whole has no cluster.
        self.fields["cluster"].queryset = Cluster.objects.filter(retrospective=retrospective)
        self.fields["cluster"].required = False
        self.fields["cluster"].empty_label = "The retrospective as a whole"

    def clean_text(self) -> str:
        """Reject nothing-but-space, and store what is left without it."""
        text = self.cleaned_data["text"].strip()
        if not text:
            raise forms.ValidationError("A decision needs some words on it.")
        return text


class ActionItemForm(forms.ModelForm):
    """Write or re-word one action item: what, who, and by when."""

    class Meta:
        model = ActionItem
        fields: ClassVar[list[str]] = ["description", "owner", "due_date", "cluster"]
        widgets: ClassVar[dict[str, forms.Widget]] = {
            "description": forms.Textarea(
                attrs={"rows": 2, "placeholder": "What is going to be done?"}
            ),
            "due_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }
        labels: ClassVar[dict[str, str]] = {
            "description": "The action",
            "owner": "Owner (optional)",
            "due_date": "Due date (optional)",
            "cluster": "About (optional)",
        }
        help_texts: ClassVar[dict[str, str]] = {
            # Said in words, because a past date looks like a bug otherwise. The
            # model allows it and this explains why.
            "due_date": (
                "A date that has already passed is fine — it records something "
                "that was already due."
            ),
            "owner": "Leave empty to record it unassigned. It stays on the list, not hidden.",
        }

    def __init__(self, *args, retrospective: Retrospective, project: Project, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.retrospective = retrospective
        self.project = project
        # Only project members are offered, so the dropdown neither lists the
        # whole user table nor lets someone outside the project be assigned. The
        # check in clean_owner repeats the rule for a request that ignores the
        # dropdown.
        self.fields["owner"].queryset = User.objects.filter(memberships__project=project).order_by(
            "username"
        )
        self.fields["owner"].required = False
        self.fields["owner"].empty_label = "Unassigned"
        self.fields["cluster"].queryset = Cluster.objects.filter(retrospective=retrospective)
        self.fields["cluster"].required = False
        self.fields["cluster"].empty_label = "The retrospective as a whole"

    def clean_description(self) -> str:
        description = self.cleaned_data["description"].strip()
        if not description:
            raise forms.ValidationError("An action item needs a description.")
        return description

    def clean_owner(self):
        """An owner has to be a member of this project, or empty.

        The queryset already limits the dropdown; this refuses a value that went
        round it, so any owner that is not a project member is a validation
        error rather than a stored row.
        """
        owner = self.cleaned_data.get("owner")
        if owner is not None and not self.project.memberships.filter(user=owner).exists():
            raise forms.ValidationError("The owner has to be a member of this project.")
        return owner


class ExtractionSummaryForm(forms.ModelForm):
    """Edit the extracted meeting summary on the review screen (#24).

    The facilitator re-words `Retrospective.extraction_summary` before confirming
    it, the same way a draft decision or action item is edited-then-accepted. The
    view sets `extraction_summary_confirmed` on save, so an edited-then-confirmed
    summary is indistinguishable from a plainly confirmed one afterwards. The text
    is optional — a facilitator may clear a summary they do not want on the record
    — and whitespace-only is stored as empty rather than as blank text.
    """

    class Meta:
        model = Retrospective
        fields: ClassVar[list[str]] = ["extraction_summary"]
        widgets: ClassVar[dict[str, forms.Widget]] = {
            "extraction_summary": forms.Textarea(attrs={"rows": 4}),
        }
        labels: ClassVar[dict[str, str]] = {
            "extraction_summary": "The meeting summary",
        }

    def clean_extraction_summary(self) -> str:
        return self.cleaned_data["extraction_summary"].strip()


class ReviewOwnerForm(forms.Form):
    """The owner a facilitator picks on the review screen when a draft has none (#24).

    A draft action item #23 could not resolve — an unmatched or ambiguous name —
    lands with `owner` NULL, and the facilitator resolves it here by choosing from
    the project's roster. This is the accept-with-owner step: a value off the
    roster is a validation error, not a stored row, exactly the rule
    `ActionItemForm.clean_owner` enforces on the hand-written form. Empty is
    allowed — an item is accepted unassigned rather than blocked, because an
    unowned action is better than a wrongly-owned one.
    """

    owner = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        empty_label="Leave unassigned",
    )

    def __init__(self, *args, project: Project, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.project = project
        # Only project members are offered, so the dropdown neither lists the
        # whole user table nor lets someone outside the project be assigned. A
        # value that went round the dropdown is refused by the queryset itself,
        # which is a validation error rather than a stored row.
        self.fields["owner"].queryset = User.objects.filter(memberships__project=project).order_by(
            "username"
        )
