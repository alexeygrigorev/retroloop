from django.contrib.auth.forms import UserCreationForm

from accounts.models import User


class SignupForm(UserCreationForm):
    """Username, display name, password, confirmation. No email field.

    `UserCreationForm` already rejects a username that differs from an existing
    one only by case, and runs the password validators configured in
    `config.settings`.
    """

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "display_name")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Optional on the model, because createsuperuser and the admin can leave
        # it blank, but everyone who signs up has to pick one.
        self.fields["display_name"].required = True
