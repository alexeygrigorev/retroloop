"""Give every card a public handle — `_docs/decisions.md` item 9.

Three operations and not one, because a single `AddField` carrying
`default=uuid.uuid4` evaluates that callable *once* and writes the same UUID
into every existing row, which then fails the unique index it is supposed to
satisfy. The column therefore arrives nullable and not unique, every row is
given a value of its own, and only then is the column tightened to what the
model declares.

The backfill writes one UUID per row. It is deliberately not a single
`UPDATE ... SET public_id = gen_random_uuid()`: the values have to come from
the same generator the model uses, so that what the migration writes and what
`Card.objects.create()` writes cannot differ in kind.
"""

import uuid

from django.db import migrations, models

#: Rows per `bulk_update`. The table holds thousands of cards, not millions,
#: but a chunked update keeps the statement a fixed size whatever it holds.
BATCH_SIZE = 500


def assign_public_ids(apps, schema_editor):
    """One fresh UUID4 per existing card. Never one value shared by the table."""
    Card = apps.get_model("cycles", "Card")
    batch = []
    for card in Card.objects.filter(public_id__isnull=True).only("pk").iterator():
        card.public_id = uuid.uuid4()
        batch.append(card)
        if len(batch) >= BATCH_SIZE:
            Card.objects.bulk_update(batch, ["public_id"])
            batch = []
    if batch:
        Card.objects.bulk_update(batch, ["public_id"])


def drop_public_ids(apps, schema_editor):
    """The reverse of the backfill: the column is about to be dropped anyway.

    Written out rather than left as `RunPython.noop` so that reversing this
    migration is a supported operation in development.
    """
    Card = apps.get_model("cycles", "Card")
    Card.objects.update(public_id=None)


class Migration(migrations.Migration):
    dependencies = [
        ("cycles", "0003_cycleparticipation"),
    ]

    operations = [
        # 1. Nullable and not unique, so an existing table takes the column.
        migrations.AddField(
            model_name="card",
            name="public_id",
            field=models.UUIDField(editable=False, null=True),
        ),
        # 2. A value per row.
        migrations.RunPython(assign_public_ids, drop_public_ids),
        # 3. What the model declares: unique, not null, with the default that
        #    every row created from here on gets.
        migrations.AlterField(
            model_name="card",
            name="public_id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
