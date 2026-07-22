"""`manage.py sweep_media` — collect the media the pipeline could not delete.

A command rather than a scheduled task on purpose. The queue is a Postgres table
with no scheduler in front of it, and adding one would be the second piece of
infrastructure #71 rules out; a command is run by cron, by a Compose one-shot,
or by a person, and needs nothing that is not already installed.

It is safe to run at any time — including while transcriptions are in progress,
which is the whole subject of `meetings/sweeper.py` — and safe to run again
straight afterwards.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError

from meetings.sweeper import DEFAULT_MINIMUM_AGE, sweep


class Command(BaseCommand):
    help = "Delete scratch media that belongs to no record, or to no run that can resume."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--min-age",
            type=int,
            default=int(DEFAULT_MINIMUM_AGE.total_seconds()),
            metavar="SECONDS",
            help=(
                "How old a file with no record naming it must be before it is deleted "
                "(default: %(default)s). It covers uploads whose transaction has not "
                "committed yet; files a record does name are decided by that record's "
                "lock and are not affected by this."
            ),
        )

    def handle(self, *args, **options) -> None:
        seconds = options["min_age"]
        if seconds < 0:
            raise CommandError("--min-age cannot be negative")

        report = sweep(minimum_age=timedelta(seconds=seconds))

        for path in report.removed:
            self.stdout.write(f"removed {path}")
        for record_id in report.abandoned:
            self.stdout.write(f"record {record_id} was left transcribing by a dead worker; failed")
        for path in report.kept_live:
            self.stdout.write(f"kept {path}: a worker is using it")
        for path in report.kept_owned:
            self.stdout.write(f"kept {path}: its record still expects it")
        for path in report.kept_young:
            self.stdout.write(f"kept {path}: too new to be sure nothing owns it")
        for path in report.refused:
            self.stderr.write(f"could not delete {path}")

        self.stdout.write(self.style.SUCCESS(report.summary()))
