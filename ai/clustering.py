"""Group a cycle's revealed cards into suggested themes with a text model.

One entry point, :func:`suggest_clusters`: the cards of a cycle in, a list of
plain ``{"name", "card_ids"}`` dicts out. Nothing here reads or writes the
database, imports a model, or knows which retrospective it is clustering —
`retro/clustering.py` owns all of that, and this module takes plain data and
returns plain data. That is what makes it mockable and swappable by provider
(issue #22, "Constraints").

A card is addressed by its ``public_id`` and never by its primary key
---------------------------------------------------------------------

`_docs/decisions.md` item 9: `Card.pk` comes from a table-wide sequence, so it
recovers the submission order the reveal exists to destroy, and it stays inside
the server. The ``id`` this module sends and receives is the card's
``public_id`` — a random UUID4 — carried as a string. The caller builds the
:class:`CardInput` values from ``public_id``; nothing here ever sees a pk, an
author or an ``is_anonymous`` flag, so nothing about a card's authorship can
leave through the request or come back attached to a cluster (decisions 9 and
10). Card text is sent to OpenAI, anonymous cards included — accepted, and the
privacy fact #47 documents.

The API client is a seam, not an import
---------------------------------------

Everything that talks to OpenAI is behind :class:`ClusteringClient`: one method,
``cluster(cards) -> list[dict]``. :class:`OpenAIClusteringClient` is the real
one; ``settings.CLUSTERING_CLIENT`` names the class the pipeline builds, and the
suite points it at a stand-in in `ai.fakes` so no test needs a key, a network,
or a skip when neither is there. The real client also takes its SDK object as an
argument, which is where a test asserts the model name, the request shape and
the parsing without a key.

Malformed output cannot corrupt the board
------------------------------------------

Structured outputs make the shape likely, not certain — a different provider,
a model that ignores the schema, an empty body. So :func:`parse_suggestions`
defends: a suggestion whose name is not a string is dropped, a ``card_ids`` that
is not a list of strings becomes an empty list, and an empty response is an
empty list of suggestions rather than an error. The domain rules that finish the
job — which ids are really in this cycle, first-cluster-wins for a card named
twice, trimming and length-capping the name — live in `retro/clustering.py`,
next to the rows they protect.
"""

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

#: The text model. `gpt-4o` is superseded; it is not a fallback, and no second
#: model is named here.
MODEL = "gpt-5.6-terra"

#: How long one clustering request may take. A cycle is at most a few dozen
#: short cards, so the model answers well inside this.
REQUEST_TIMEOUT_SECONDS = 2 * 60

#: The structured-output schema. The model is asked for an object with one
#: `clusters` array of `{name, card_ids}`, and nothing else — `strict` mode
#: forbids extra keys, so a response that parses at all parses into this shape.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "card_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "card_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clusters"],
    "additionalProperties": False,
}

#: The name the schema is registered under in the request. Structured outputs
#: require one; it is not a model name and carries no meaning downstream.
RESPONSE_SCHEMA_NAME = "card_clusters"

#: What the model is told to do. It never receives an author, a pk or an
#: anonymity flag — only an id (the card's public handle), a category and text —
#: so it has nothing to leak into a cluster name but the text it was shown.
SYSTEM_PROMPT = (
    "You group retrospective feedback cards into a few themed clusters. "
    "Each card has an opaque id, a category (START, STOP or CONTINUE) and text. "
    "Return clusters of related cards, each with a short descriptive name and "
    "the ids of the cards it contains. Use only the ids you were given. A card "
    "may go in at most one cluster, and a card that fits nowhere may be left out."
)

MISSING_KEY_MESSAGE = (
    "OPENAI_API_KEY is not set in this environment, so the cards were never sent to be grouped."
)


class ClusteringError(Exception):
    """The cards could not be grouped. The message is shown to a facilitator."""


class MissingAPIKeyError(ClusteringError):
    """`OPENAI_API_KEY` is not configured.

    Raised before anything is sent, so a facilitator is told which variable is
    missing rather than being handed an authentication error out of the SDK.
    """


@dataclass(frozen=True)
class CardInput:
    """One card as the model sees it: a public handle, a category and text.

    ``id`` is the card's ``public_id`` as a string — never its primary key
    (`_docs/decisions.md` item 9). There is deliberately no field for an author
    or an anonymity flag: neither is sent, so neither can come back attached to
    a cluster (decision 10).
    """

    id: str
    category: str
    text: str


class ClusteringClient(Protocol):
    """The seam. Cards in, suggested groups out, no knowledge of the database."""

    def cluster(self, cards: Sequence[CardInput]) -> list[dict]: ...


def suggest_clusters(
    cards: Iterable[CardInput],
    *,
    client: ClusteringClient | None = None,
) -> list[dict]:
    """Group ``cards`` into suggested themes, defensively normalised.

    `client` defaults to whatever ``settings.CLUSTERING_CLIENT`` names, which is
    how the suite gets a fake without a key. An empty input makes no call and
    returns no suggestions — the caller checks this too, so a cycle with no
    cards never reaches a client at all.

    Returns a list of ``{"name": str, "card_ids": list[str]}``. The names are
    strings and the ids are strings; which ids are real, and which name is
    storable, is decided by the caller against the cycle.
    """
    cards = list(cards)
    if not cards:
        return []
    if client is None:
        client = build_client()
    raw = client.cluster(cards)
    return parse_suggestions(raw)


def parse_suggestions(raw) -> list[dict]:
    """Read the model's groups into clean dicts, dropping what cannot be stored.

    Read by shape rather than by type, because the client is a seam and a fake
    hands back objects of its own. The rules, each one an acceptance criterion
    of #22 that malformed output must not violate:

    * a suggestion that is not a mapping is skipped;
    * a name that is not a string is skipped — a group with no usable name is
      not a cluster the team can talk about, and its cards stay ungrouped;
    * ``card_ids`` that is not a list becomes an empty list, and any id in it
      that is not a string is dropped.

    Trimming, length-capping and checking the ids against the cycle happen in
    the caller, where the cycle and the storage rules are.
    """
    suggestions: list[dict] = []
    for item in _as_list(raw):
        name = _field(item, "name")
        if not isinstance(name, str):
            continue
        raw_ids = _field(item, "card_ids")
        ids = (
            [value for value in raw_ids if isinstance(value, str)]
            if isinstance(raw_ids, list)
            else []
        )
        suggestions.append({"name": name, "card_ids": ids})
    return suggestions


def _as_list(raw) -> list:
    return list(raw) if isinstance(raw, list | tuple) else []


def _field(item, key: str):
    """One field of a suggestion, whether it is a dict or an object."""
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def build_client() -> ClusteringClient:
    """Build the client ``settings.CLUSTERING_CLIENT`` names.

    Imported lazily, like everything Django in this package, so the module stays
    importable — and testable — without a configured settings module.
    """
    from django.conf import settings
    from django.utils.module_loading import import_string

    return import_string(settings.CLUSTERING_CLIENT)()


def configured_api_key() -> str:
    """`OPENAI_API_KEY`, as the environment supplied it, or the empty string."""
    from django.conf import settings

    return getattr(settings, "OPENAI_API_KEY", "") or ""


@dataclass
class OpenAIClusteringClient:
    """The real client: one structured-output request per cycle, and nothing else.

    `sdk` is the injection point. Left unset it is built on first use from
    `OPENAI_API_KEY`; a test passes an object shaped like `openai.OpenAI` and
    asserts what was sent to it — the model, the schema, the card payload —
    without a key and without a network.

    The SDK's own retries are switched off. A clustering failure leaves the
    cards ungrouped and the team clusters by hand; there is nothing to gain from
    a hidden retry budget, and #22 asks for none.
    """

    api_key: str | None = None
    sdk: object | None = None
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    model: str = MODEL

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = configured_api_key()
        # Checked here rather than at the first request, so a missing key is
        # reported as a missing key before any card is sent.
        if not self.api_key and self.sdk is None:
            raise MissingAPIKeyError(MISSING_KEY_MESSAGE)

    def _client(self):
        if self.sdk is None:
            import openai

            self.sdk = openai.OpenAI(
                api_key=self.api_key,
                timeout=self.timeout_seconds,
                max_retries=0,
            )
        return self.sdk

    def cluster(self, cards: Sequence[CardInput]) -> list[dict]:
        """Send the cards, and read the suggested groups back out of the response."""
        sdk = self._client()
        payload = [{"id": card.id, "category": card.category, "text": card.text} for card in cards]
        try:
            response = sdk.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps({"cards": payload})},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": RESPONSE_SCHEMA_NAME,
                        "strict": True,
                        "schema": RESPONSE_SCHEMA,
                    },
                },
            )
        except Exception as exc:
            translated = classify(exc)
            if translated is None:
                raise
            raise translated from exc
        return read_clusters(response)


def read_clusters(response) -> list:
    """Pull the `clusters` array out of the structured response, defensively.

    An empty body, content that is not JSON, or a JSON value that is not the
    expected object are each "no suggestions" rather than an error: a model that
    returned nothing usable must leave the board unclustered, not fail the job.
    :func:`parse_suggestions` then normalises whatever list this returns.
    """
    content = _content_of(response)
    if not content:
        return []
    try:
        data = json.loads(content)
    except ValueError, TypeError:
        logger.warning("clustering response was not JSON; treating it as no suggestions")
        return []
    clusters = data.get("clusters") if isinstance(data, dict) else None
    return clusters if isinstance(clusters, list) else []


def _content_of(response) -> str:
    """The assistant message text, or an empty string if the shape is not there."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    return str(getattr(message, "content", "") or "")


def classify(exc: BaseException) -> ClusteringError | None:
    """Turn an SDK exception into ours, or return None if it is not one.

    A rejected or missing key names `OPENAI_API_KEY`, so a facilitator is told
    which variable to fix rather than shown an authentication error out of the
    SDK. Everything else the SDK raises becomes a plain :class:`ClusteringError`
    with the reason, which the job records and the cards stay ungrouped.
    """
    try:
        import openai
    except ImportError:  # pragma: no cover - the SDK is a pinned dependency
        return None

    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ClusteringError(
            "the grouping API rejected OPENAI_API_KEY; the key is missing a permission "
            "or is no longer valid"
        )
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return ClusteringError(f"the grouping API was unreachable ({exc})")
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        return ClusteringError(f"the grouping API refused the request ({status})")
    if isinstance(exc, openai.OpenAIError):
        return ClusteringError(f"the grouping API could not be called ({exc})")
    return None
