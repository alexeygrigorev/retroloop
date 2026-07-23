from django.apps import AppConfig


class DemoConfig(AppConfig):
    """The demo-data app.

    It carries the `seed_demo` management command and nothing else: no models,
    no migrations, no views, no URLs. The command imports the models it fills
    from `projects/`, `cycles/`, `retro/` and `meetings/`, so this app sits
    below all of them and none of them import it.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "demo"
