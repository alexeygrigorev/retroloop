from typing import ClassVar

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower


class User(AbstractUser):
    """The project's own user model.

    Every later table carries a foreign key to a user, and swapping the user
    model once those exist is a data migration on live rows, so it is defined
    here from the first migration. The inherited `email` field stays unused: the
    project has no mail backend and never asks for or renders an address.
    """

    display_name = models.CharField(
        max_length=150,
        blank=True,
        help_text="Shown wherever this user appears. Need not be unique.",
    )

    # Only the username and a password. AbstractUser would also prompt
    # createsuperuser for an email address.
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta(AbstractUser.Meta):
        # `username` is already unique; this makes two names that differ only by
        # case impossible in the database, not just in the signup form.
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                Lower("username"),
                name="accounts_user_username_ci_unique",
            )
        ]

    def __str__(self) -> str:
        # Accounts created by createsuperuser have no display name.
        return self.display_name or self.username
