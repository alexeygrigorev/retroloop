from typing import ClassVar

from django import forms

from projects.models import Project


class ProjectForm(forms.ModelForm):
    """Creating a project asks for a name and nothing else.

    The owner comes from the session and the join token generates itself, so
    neither is ever accepted from the request.
    """

    class Meta:
        model = Project
        fields: ClassVar[list[str]] = ["name"]
