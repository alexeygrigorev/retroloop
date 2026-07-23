"""Turn a revealed cycle's cards into suggested clusters, and write them down.

This is the body the `cluster_retrospective` job runs. It is the only half of
clustering that touches a model: `ai/clustering.py` takes plain data and returns
plain data and never imports a row, and everything that reads or writes the
board happens here.

Where it sits in the transition
-------------------------------

It is enqueued by the `-> REVEAL` transition (`retro/services.py`) with
`enqueue_on_commit`, so it runs *after* the reveal has committed — after the
cards are frozen, shuffled and anonymised (`_docs/decisions.md` items 1, 3 and
10). It reads card text, never an author or a pk: a card is addressed by its
``public_id`` (item 9), and that is the only handle sent to the model and the
only one it hands back. Nothing about a card's authorship can leave through the
request or return attached to a cluster.

What it guarantees, whatever the model returns
----------------------------------------------

The acceptance criteria of #22, each enforced here rather than trusted:

* an id the model returns that is not a card in this cycle is ignored;
* a card the model puts in two groups joins the first and is not duplicated —
  ``Card.cluster`` is a single foreign key, so this is a fact of the schema, and
  the first cluster to claim a card keeps it;
* a card the model omits stays ungrouped, which is the normal state of a card;
* names are trimmed and capped to ``CLUSTER_NAME_MAX_LENGTH`` before they are
  stored, and a name that is empty after trimming groups nothing;
* a cycle with no cards makes no API call and creates no clusters;
* running twice does nothing the second time — it stops the moment it sees an
  auto-generated cluster already on the board, so it never overwrites the team's
  own grouping (#48 owns re-clustering, and it is out of scope here);
* a failure leaves the reveal untouched and is recorded on the retrospective
  where a facilitator sees it, not only in the worker log.

The whole write is one transaction under the retrospective's row lock, so the
new clusters and the version bump commit together and a poll never sees half of
them.
"""

import logging

from django.db import models, transaction

from ai.clustering import CardInput, ClusteringError, suggest_clusters
from cycles.models import Card
from retro.models import CLUSTER_NAME_MAX_LENGTH, Cluster, Retrospective
from retro.services import bump_version

logger = logging.getLogger(__name__)

#: Appended to every failure message. A clustering failure is recoverable by
#: hand — unlike a transcription failure, nothing was destroyed — so the words
#: say what the team does next rather than pointing at a button that is not
#: there.
RECOVERY = "The cards are left ungrouped, so the team can group them by hand."

#: What a facilitator is told when the failure is not one we recognised. The
#: real exception goes to the log with its traceback; a stack trace on a page
#: every member can open tells them nothing and tells everyone else too much.
UNEXPECTED = "Something went wrong while grouping the cards automatically."


def cluster_retrospective_cards(retro_id: int, *, client=None) -> None:
    """Cluster the cards of one retrospective, if it is still there and unclustered.

    Takes an id and re-fetches, per the queue conventions (AGENTS.md): time
    passes between the enqueue and the run, so the row may have gone, and the
    board may already carry suggestions. Both are a return rather than an error.

    `client` is the clustering client, for a test that wants to script one. Left
    unset, `ai.clustering` builds whatever `CLUSTERING_CLIENT` names.
    """
    retro = Retrospective.objects.select_related("cycle").filter(pk=retro_id).first()
    if retro is None:
        logger.info("retrospective %s is gone; nothing to cluster", retro_id)
        return

    # A cheap early-out before the API is called: if the board already carries
    # auto-generated clusters, a run has happened. The write below re-checks
    # this under the row lock, so two workers cannot both suggest.
    if retro.clusters.filter(is_auto_generated=True).exists():
        logger.info("retrospective %s already has suggestions; leaving them alone", retro_id)
        return

    inputs = _card_inputs(retro)
    if not inputs:
        logger.info("retrospective %s has no cards; nothing to cluster", retro_id)
        return

    try:
        suggestions = suggest_clusters(inputs, client=client)
    except ClusteringError as exc:
        logger.exception("clustering retrospective %s failed", retro_id)
        _record_failure(retro, str(exc))
        return
    except Exception:
        logger.exception("clustering retrospective %s failed unexpectedly", retro_id)
        _record_failure(retro, UNEXPECTED)
        return

    _write_clusters(retro, suggestions)


def _card_inputs(retro: Retrospective) -> list[CardInput]:
    """Every card in the cycle, as the model sees it: public id, category, text.

    Deliberately selects no author and no primary key. `public_id` is the only
    handle that leaves the server (`_docs/decisions.md` item 9), and an author
    is never sent (item 10); the values query names exactly the three columns
    the model is given and no fourth one to leak.
    """
    rows = Card.objects.filter(cycle_id=retro.cycle_id).values_list("public_id", "category", "text")
    return [
        CardInput(id=str(public_id), category=category, text=text)
        for public_id, category, text in rows
    ]


def _write_clusters(retro: Retrospective, suggestions: list[dict]) -> None:
    """Create the suggested clusters and move the cards into them, in one transaction.

    Under the retrospective's row lock, so the clusters and the version bump
    commit together and a concurrent run finds the board already suggested.
    """
    with transaction.atomic():
        locked = Retrospective.objects.select_for_update(of=("self",)).get(pk=retro.pk)
        if Cluster.objects.filter(retrospective=locked, is_auto_generated=True).exists():
            logger.info("retrospective %s was suggested while we worked; standing down", locked.pk)
            return

        # public_id -> pk, for the cards of this cycle only. The pk stays on the
        # server: it is used to move the card and never sent anywhere.
        id_to_pk = {
            str(public_id): pk
            for public_id, pk in Card.objects.filter(cycle_id=locked.cycle_id).values_list(
                "public_id", "pk"
            )
        }

        assigned: set[int] = set()
        position = _next_position(locked)
        created_any = False
        for suggestion in suggestions:
            name = _cap_name(suggestion["name"])
            if not name:
                continue
            pks = _unassigned_pks(suggestion["card_ids"], id_to_pk, assigned)
            if not pks:
                # A group whose cards were all unknown or already claimed — the
                # "more clusters than cards" case among others — is not written:
                # an empty suggested cluster is noise, not a group.
                continue
            cluster = Cluster.objects.create(
                retrospective=locked,
                name=name,
                position=position,
                is_auto_generated=True,
            )
            Card.objects.filter(pk__in=pks).update(cluster=cluster)
            assigned.update(pks)
            position += 1
            created_any = True

        # The run reached the model and came back: clear any error a previous
        # attempt recorded, whether or not it produced clusters this time.
        if locked.clustering_error:
            Retrospective.objects.filter(pk=locked.pk).update(clustering_error="")

        # One bump for the whole write, and only when something changed, so an
        # open board re-reads once and a run that suggested nothing wakes no
        # poll. The reveal transition already bumped once for itself; this is a
        # separate transaction and a separate, legitimate version.
        if created_any:
            bump_version(locked)


def _unassigned_pks(card_ids, id_to_pk: dict[str, int], assigned: set[int]) -> list[int]:
    """The pks of the named cards that are in this cycle and not yet in a cluster.

    An id that names no card in the cycle is dropped, and a card already claimed
    by an earlier cluster is dropped — first cluster wins, and no card lands in
    two. Order and uniqueness are preserved so the assignment is deterministic.
    """
    pks: list[int] = []
    seen: set[int] = set()
    for card_id in card_ids:
        pk = id_to_pk.get(card_id)
        if pk is None or pk in assigned or pk in seen:
            continue
        seen.add(pk)
        pks.append(pk)
    return pks


def _cap_name(raw: str) -> str:
    """A cluster name, trimmed and capped, or an empty string if it is neither.

    The model's name is a string by the time it reaches here (`ai.clustering`
    drops the rest), so this only has to hold the storage rules #12 put on the
    column: not blank, and no longer than the cap. Trimmed, cut to the cap, and
    trimmed again in case the cut landed on a space.
    """
    return raw.strip()[:CLUSTER_NAME_MAX_LENGTH].strip()


def _next_position(retro: Retrospective) -> int:
    """One past the last cluster on this board, or 1 for the first one.

    Auto-generated clusters run on a board that has none yet, so this is 1 in
    practice; it is computed rather than assumed so a board that somehow already
    holds a hand-made cluster is appended to rather than collided with.
    """
    highest = Cluster.objects.filter(retrospective=retro).aggregate(models.Max("position"))
    return (highest["position__max"] or 0) + 1


def _record_failure(retro: Retrospective, reason: str) -> None:
    """Write the failure onto the retrospective, where a facilitator reads it.

    A committed write of its own — the reveal has long since committed, so there
    is no transaction to roll this back with, and it must survive whether or not
    the caller re-raises. The message names the reason and what to do next, and
    carries no traceback.
    """
    message = f"{reason} {RECOVERY}"
    Retrospective.objects.filter(pk=retro.pk).update(clustering_error=message)
