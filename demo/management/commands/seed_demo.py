"""`manage.py seed_demo` — fill an empty development database with one demo team.

It writes rows directly (see `demo/seed.py`), makes no network call, enqueues no
task, and calls neither `advance_stage()` nor `reveal_cycle()`. Run twice it
produces the same database, deleting only the demo projects and demo users it
owns.

It refuses to run unless `DEBUG` is on. That is the whole production guard: no
`--force`, no environment variable, no second switch. `DEBUG` is already
environment-driven and already false in production and in the test suite, so the
refusal needs no new setting and no line in `.env.example`.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.urls import reverse

from demo.seed import DEMO_PASSWORD, DEMO_SEED, ROSTER, seed_demo


class Command(BaseCommand):
    help = "Seed a development database with one realistic demo team (DEBUG only)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--seed",
            type=int,
            default=DEMO_SEED,
            metavar="INT",
            help=(
                "The value the demo's RNG is seeded with (default: %(default)s). "
                "Change it for a different-but-reproducible dataset."
            ),
        )
        parser.add_argument(
            "--password",
            default=DEMO_PASSWORD,
            metavar="VALUE",
            help="The shared password for every demo user (default: %(default)s).",
        )

    def handle(self, *args, **options) -> None:
        # The one guard, and nothing else may override it. A switch to run this
        # in production is the thing that gets used in production.
        if not settings.DEBUG:
            raise CommandError(
                "seed_demo refuses to run with DEBUG off. This is demo data for "
                "local development only; it must never be seeded anywhere real. "
                "Nothing was written."
            )

        with transaction.atomic():
            result = seed_demo(seed=options["seed"], password=options["password"])

        self._report(result)

    # -- output -----------------------------------------------------------

    def _report(self, result) -> None:
        """Print how to log in and where to look. All URLs come from `reverse()`."""
        w = self.stdout.write
        password = result.password

        w("")
        w("Demo data seeded. Log in at:")
        w(f"  {reverse('login')}")
        w("")
        w(f"Every demo user's password is: {password}")
        w("")
        w("Users (username / display name / role in Platform Team):")
        for username, display_name, role in ROSTER:
            w(f"  {username:<14} {display_name:<14} {role}")
        w(f"  {'demo_admin':<14} {'Demo Admin':<14} superuser, on no project (404 on any project)")
        w("")

        platform = result.platform

        def look(label: str, url: str, login: str) -> None:
            w(f"  {label:<20} {url:<12} {login}")

        w("Where to look (each names the demo user to log in as):")
        look("Project dashboard", reverse("project-detail", args=[platform.pk]), "as demo_priya")
        look("Join link", platform.join_path(), "any signed-in user")
        look(
            "Open cycle feedback",
            reverse("cycle-cards", args=[result.open_cycle.pk]),
            "as demo_priya",
        )
        look(
            "Completed summary",
            reverse("retro-summary", args=[result.complete_retro.pk]),
            "as demo_priya",
        )
        look(
            "Cluster board",
            reverse("retro-detail", args=[result.discuss_retro.pk]),
            "as demo_priya",
        )
        look(
            "Draft review",
            reverse("retro-review", args=[result.discuss_retro.pk]),
            "as demo_sam (facilitator only; demo_priya gets 404)",
        )
        w("")
        w(
            "This password is published in the repository. This data must never be "
            "seeded anywhere real."
        )
