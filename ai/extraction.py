"""Read a meeting transcript into draft outcomes with a text model.

One entry point, :func:`extract_outcomes`: a transcript, the ranked agenda and
the project roster in, a plain ``{"summary", "decisions", "action_items"}`` dict
out. Nothing here reads or writes the database, imports a model, or knows which
retrospective it is reading — `meetings/extraction.py` owns all of that, and this
module takes plain data and returns plain data. That is what makes it mockable
and swappable by provider, exactly like `ai/clustering.py` and
`ai/transcription.py` before it (issue #23, "Constraints").

What leaves the server, and what does not
-----------------------------------------

The request carries the transcript, the agenda and the roster, and nothing else.
The agenda is the discussion's clusters — each an integer id, a name and a vote
weight, the same handles the board already shows the whole team
(`_docs/decisions.md` item 9 keeps a *card's* pk on the server; a *cluster's* is
made in front of the team and is fine to send). The roster is the project
members' **display names** — never an email address, never a username where a
display name exists (#23, "Constraints"; item 8: there are no addresses to send).

Nothing about a card leaves through here: no card text, no card author, no
`Card.pk`, no anonymity flag. A draft this module produces names a cluster or
nothing, and an owner or nobody, and so cannot leak either of the two facts
`_docs/decisions.md` items 9 and 10 keep off a screen. The transcript itself goes
to OpenAI — the privacy fact #47 documents, the same as the recording did.

Resolving an owner is this module's job (#23)
---------------------------------------------

The model hands back an owner *name*, a string it read off the transcript.
:func:`resolve_owner` matches it against the roster with the standard library's
`difflib` — no new dependency — and returns the matched display name, or None:

* an exact or near match to exactly one roster entry resolves to it;
* a name that matches nobody closely enough resolves to None — the facilitator
  picks in #24, and a guess is worse than a blank;
* a name that matches two roster entries equally well — two members called Alex,
  "Alex will do it" — is *ambiguous* and also resolves to None. Picking the first
  is worse than leaving it blank, because it names the wrong person.

This is #23's resolution and not #17's: #17's manual form rejects a non-member as
a 400 and does no name resolution, so the NULL-on-unmatched-or-ambiguous rule
lives here, next to the model output it defends against.

Dates are resolved against the meeting, never against now
---------------------------------------------------------

The model is told the meeting's date and asked to return every due date as an
absolute ISO date it has already resolved against it, so "by next Friday" is the
Friday after the meeting and not the Friday after the job happens to run
(#23, "Dates"). :func:`resolve_due_date` then only parses an ISO string — it
reads no clock — so nothing here depends on when the worker ran. A date that will
not parse, or one that falls before the meeting (a relative date resolved the
wrong way, or a hallucination), is left None: an action item with no due date is
normal, and a date nobody can stand behind is worse than none.

Malformed output cannot corrupt the retrospective
-------------------------------------------------

Structured outputs make the shape likely, not certain — a different provider, a
model that ignores the schema, an empty body. So :func:`parse_outcomes` defends,
per-item, the way `ai/clustering.py`'s `parse_suggestions` does: a decision whose
text is not a string is dropped, an action item whose description is not a string
is dropped, an owner that is not a string resolves to None, a due date that is
not a string resolves to None, a summary that is not a string becomes empty, and
an empty response is an empty result rather than an error. The valid drafts still
land; one bad item does not fail the batch.
"""

import difflib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

logger = logging.getLogger(__name__)

#: The text model. The same one `ai/clustering.py` names; it is not a fallback,
#: and no second model is named here.
MODEL = "gpt-5.6-terra"

#: How long one extraction request may take. A meeting is a single transcript and
#: a handful of clusters, so the model answers well inside this.
REQUEST_TIMEOUT_SECONDS = 2 * 60

#: How close a model's owner name must be to a roster entry to match it at all.
#: Below this the name resolves to nobody rather than to the least-bad guess. A
#: `difflib` ratio, so 1.0 is identical and 0.8 is a small typo or a shortened
#: form; it is deliberately high, because a wrong owner is worse than a blank the
#: facilitator fills in (#24).
OWNER_MATCH_THRESHOLD = 0.8

#: The structured-output schema. One object: a summary string, a list of
#: decisions and a list of action items. `strict` mode forbids extra keys and
#: makes every property required, so nullable fields are typed ``["string",
#: "null"]`` rather than omitted — a response that parses at all parses into this
#: shape, and :func:`parse_outcomes` still defends against a provider that does
#: not honour it.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "excerpt": {"type": "string"},
                },
                "required": ["text", "excerpt"],
                "additionalProperties": False,
            },
        },
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    # A name the model read off the transcript, resolved against
                    # the roster here — never a member id the model made up.
                    "owner": {"type": ["string", "null"]},
                    # An absolute ISO date the model has resolved against the
                    # meeting date, or null when there is none.
                    "due_date": {"type": ["string", "null"]},
                    "excerpt": {"type": "string"},
                },
                "required": ["description", "owner", "due_date", "excerpt"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "decisions", "action_items"],
    "additionalProperties": False,
}

#: The name the schema is registered under in the request. Structured outputs
#: require one; it is not a model name and carries no meaning downstream.
RESPONSE_SCHEMA_NAME = "meeting_outcomes"

#: What the model is told to do. It is given the transcript, the ranked agenda
#: and the roster of display names, and asked for draft outcomes — never for an
#: owner it invents, a card, or anything about who wrote a card.
SYSTEM_PROMPT = (
    "You read the transcript of a team's retrospective meeting and write down what "
    "the team settled, as drafts a facilitator will review. You are given the "
    "meeting's date, the ranked agenda of discussion topics, and a roster of the "
    "project members' names.\n"
    "Return three things:\n"
    "- decisions: each a short sentence of something the team decided, with a "
    "short verbatim excerpt from the transcript it came from;\n"
    "- action_items: each a task someone agreed to do, with the owner's name "
    "exactly as it appears in the roster when you can tell who it is (otherwise "
    "null), an optional due date, and a short verbatim excerpt;\n"
    "- summary: two or three sentences summarising the meeting.\n"
    "Owners: use only names from the roster. If you cannot tell which member is "
    "meant, or the name is ambiguous, set owner to null rather than guessing.\n"
    "Due dates: return every due date as an absolute ISO date (YYYY-MM-DD) that "
    "you have resolved against the meeting's date, so 'next Friday' is the Friday "
    "after the meeting. If there is no clear due date, set it to null.\n"
    "Invent nothing. A meeting where nothing was decided is a valid result: "
    "return empty lists and a one-sentence summary saying so."
)

MISSING_KEY_MESSAGE = (
    "OPENAI_API_KEY is not set in this environment, so the transcript was never sent to be read."
)


class ExtractionError(Exception):
    """The transcript could not be read. The message is shown to a facilitator."""


class MissingAPIKeyError(ExtractionError):
    """`OPENAI_API_KEY` is not configured.

    Raised before anything is sent, so a facilitator is told which variable is
    missing rather than being handed an authentication error out of the SDK.
    """


@dataclass(frozen=True)
class AgendaItem:
    """One discussion topic as the model sees it: a cluster's public handle.

    ``id`` is the cluster's integer pk — a handle the whole team made in front of
    the team, which `_docs/decisions.md` item 9 keeps public — ``name`` its name,
    and ``weight`` the vote weight #16 ranks the agenda by. No card, no author, no
    ``Card.pk`` is reachable from here.
    """

    id: int
    name: str
    weight: int


@dataclass(frozen=True)
class ExtractionInput:
    """One meeting as the model sees it.

    ``transcript`` is the diarized text #21 stored; ``agenda`` the ranked topics
    #16 produced; ``roster`` the project members' display names; ``meeting_date``
    the date every relative due date is resolved against. There is deliberately no
    field for a card, an author or an email address: none is sent, so none can
    come back attached to a draft.
    """

    transcript: str
    meeting_date: date
    agenda: tuple[AgendaItem, ...] = ()
    roster: tuple[str, ...] = ()


class ExtractionClient(Protocol):
    """The seam. A meeting in, the model's raw outcomes out, no database."""

    def extract(self, meeting: ExtractionInput) -> dict: ...


def empty_result() -> dict:
    """The shape :func:`extract_outcomes` returns, with nothing in it.

    A meeting where nothing was decided, and an empty transcript that is never
    sent, both land here: no drafts, an empty summary, and a caller that finishes
    the record as READY rather than treating "nothing" as a failure.
    """
    return {"summary": "", "decisions": [], "action_items": []}


def extract_outcomes(
    meeting: ExtractionInput,
    *,
    client: ExtractionClient | None = None,
) -> dict:
    """Read ``meeting`` into draft outcomes, defensively normalised.

    `client` defaults to whatever ``settings.EXTRACTION_CLIENT`` names, which is
    how the suite gets a fake without a key. An empty or whitespace-only
    transcript makes no call and returns :func:`empty_result` — a meeting with
    nothing in it is a real outcome, not an error.

    Returns ``{"summary": str, "decisions": [...], "action_items": [...]}``. Each
    decision is ``{"text": str, "excerpt": str}``; each action item is
    ``{"description": str, "owner": str | None, "due_date": date | None,
    "excerpt": str}``, the owner already resolved against the roster and the due
    date already parsed against the meeting.
    """
    if not meeting.transcript or not meeting.transcript.strip():
        return empty_result()
    if client is None:
        client = build_client()
    raw = client.extract(meeting)
    return parse_outcomes(raw, roster=meeting.roster, meeting_date=meeting.meeting_date)


# --------------------------------------------------------------------------
# Reading the model's output
# --------------------------------------------------------------------------


def parse_outcomes(raw, *, roster: Sequence[str], meeting_date: date) -> dict:
    """Read the model's outcomes into clean dicts, dropping what cannot be stored.

    Read by shape rather than by type, because the client is a seam and a fake
    hands back objects of its own. The rules, each one an acceptance criterion of
    #23 that malformed output must not violate:

    * a top-level value that is not a mapping is the empty result;
    * a summary that is not a string becomes an empty string;
    * a decision that is not a mapping, or whose text is not a non-blank string,
      is dropped; its excerpt becomes empty if it is not a string;
    * an action item that is not a mapping, or whose description is not a non-blank
      string, is dropped; its owner is resolved against the roster (a non-string
      resolves to None), its due date parsed against the meeting (a non-string or
      an unparseable or past date is None), its excerpt emptied if not a string.

    The valid drafts still land; one malformed item does not fail the batch.
    """
    summary = _field(raw, "summary")
    result = empty_result()
    result["summary"] = summary.strip() if isinstance(summary, str) else ""

    for item in _as_list(_field(raw, "decisions")):
        text = _field(item, "text")
        if not isinstance(text, str) or not text.strip():
            continue
        excerpt = _field(item, "excerpt")
        result["decisions"].append(
            {
                "text": text.strip(),
                "excerpt": excerpt.strip() if isinstance(excerpt, str) else "",
            }
        )

    for item in _as_list(_field(raw, "action_items")):
        description = _field(item, "description")
        if not isinstance(description, str) or not description.strip():
            continue
        excerpt = _field(item, "excerpt")
        result["action_items"].append(
            {
                "description": description.strip(),
                "owner": resolve_owner(_field(item, "owner"), roster),
                "due_date": resolve_due_date(_field(item, "due_date"), meeting_date),
                "excerpt": excerpt.strip() if isinstance(excerpt, str) else "",
            }
        )

    return result


def resolve_owner(name, roster: Sequence[str]) -> str | None:
    """Match a model's owner name against the roster, or return None.

    The whole of #23's owner resolution, and deliberately conservative:

    * a value that is not a string, or is blank, resolves to None;
    * the best `difflib` match below :data:`OWNER_MATCH_THRESHOLD` is nobody —
      a name that is not a member at all leaves the item unassigned;
    * a best score shared by two or more roster entries is *ambiguous* and
      resolves to None — two members called Alex means "Alex" identifies neither,
      and naming the first is worse than a blank the facilitator fills in;
    * a single roster entry at the best score, at or above the threshold,
      resolves to that entry's name exactly as it appears in the roster.

    Case is folded and surrounding space ignored, so "alex" matches "Alex"; a
    roster entry that is blank matches nothing. The name returned is one of
    ``roster`` verbatim, so the caller can map it straight back to a member.
    """
    if not isinstance(name, str):
        return None
    query = name.strip().casefold()
    if not query:
        return None

    scored: list[tuple[float, str]] = []
    for original in roster:
        if not isinstance(original, str):
            continue
        candidate = original.strip().casefold()
        if not candidate:
            continue
        scored.append((_match_score(query, candidate), original))

    if not scored:
        return None
    best = max(score for score, _ in scored)
    if best < OWNER_MATCH_THRESHOLD:
        return None
    # Every roster entry at the top score. Two of them — two members the name fits
    # equally — is the ambiguous case, and it is counted by entry and not by name
    # so two members who happen to share a display name are ambiguous too.
    winners = [original for score, original in scored if score == best]
    if len(winners) != 1:
        return None
    return winners[0]


def _match_score(query: str, candidate: str) -> float:
    """How well a model's owner name fits one roster display name, 0.0 to 1.0.

    The best `difflib` ratio of the query against the whole display name and
    against each of its parts, both already case-folded. Matching the parts is
    what lets a first name the model read off the transcript — "Alex will do it" —
    match "Alex Kim", while still leaving "Alex" ambiguous when two members carry
    it, because both roster entries then score 1.0 and :func:`resolve_owner`
    refuses a tie.
    """
    best = difflib.SequenceMatcher(None, query, candidate).ratio()
    for part in candidate.split():
        best = max(best, difflib.SequenceMatcher(None, query, part).ratio())
    return best


def resolve_due_date(value, meeting_date: date) -> date | None:
    """Parse an ISO due date the model resolved against the meeting, or return None.

    Reads no clock: the model has already turned any relative date into an
    absolute one against ``meeting_date`` (#23, "Dates"), so this only parses the
    ISO string it returns. A value that is not a string, or is not an ISO date, or
    that falls before the meeting — a relative date resolved the wrong way, or a
    hallucination — is None. An action item with no due date is normal, and a date
    nobody can stand behind is worse than none.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        logger.warning("extraction returned an unparseable due date; leaving it blank")
        return None
    if parsed < meeting_date:
        logger.warning("extraction returned a due date before the meeting; leaving it blank")
        return None
    return parsed


def _as_list(raw) -> list:
    return list(raw) if isinstance(raw, list | tuple) else []


def _field(item, key: str):
    """One field of a model object, whether it is a dict or an object."""
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


# --------------------------------------------------------------------------
# The real client
# --------------------------------------------------------------------------


def build_client() -> ExtractionClient:
    """Build the client ``settings.EXTRACTION_CLIENT`` names.

    Imported lazily, like everything Django in this package, so the module stays
    importable — and testable — without a configured settings module.
    """
    from django.conf import settings
    from django.utils.module_loading import import_string

    return import_string(settings.EXTRACTION_CLIENT)()


def configured_api_key() -> str:
    """`OPENAI_API_KEY`, as the environment supplied it, or the empty string."""
    from django.conf import settings

    return getattr(settings, "OPENAI_API_KEY", "") or ""


@dataclass
class OpenAIExtractionClient:
    """The real client: one structured-output request per meeting, and nothing else.

    `sdk` is the injection point. Left unset it is built on first use from
    `OPENAI_API_KEY`; a test passes an object shaped like `openai.OpenAI` and
    asserts what was sent to it — the model, the schema, the payload — without a
    key and without a network.

    The SDK's own retries are switched off. An extraction failure leaves the
    transcript stored and the record retryable (`_docs/decisions.md` item 6: the
    input is durable, unlike the recording), so there is nothing to gain from a
    hidden retry budget.
    """

    api_key: str | None = None
    sdk: object | None = None
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    model: str = MODEL

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = configured_api_key()
        # Checked here rather than at the first request, so a missing key is
        # reported as a missing key before the transcript is sent.
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

    def extract(self, meeting: ExtractionInput) -> dict:
        """Send the meeting, and read the raw outcomes back out of the response."""
        sdk = self._client()
        payload = {
            "meeting_date": meeting.meeting_date.isoformat(),
            "roster": list(meeting.roster),
            "agenda": [
                {"id": item.id, "name": item.name, "weight": item.weight} for item in meeting.agenda
            ],
            "transcript": meeting.transcript,
        }
        try:
            response = sdk.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload)},
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
        return read_outcomes(response)


def read_outcomes(response) -> dict:
    """Pull the outcomes object out of the structured response, defensively.

    An empty body, content that is not JSON, or a JSON value that is not an
    object are each an empty result rather than an error: a model that returned
    nothing usable must leave the retrospective with no drafts, not fail the job.
    :func:`parse_outcomes` then normalises whatever mapping this returns.
    """
    content = _content_of(response)
    if not content:
        return {}
    try:
        data = json.loads(content)
    except ValueError, TypeError:
        logger.warning("extraction response was not JSON; treating it as no outcomes")
        return {}
    return data if isinstance(data, dict) else {}


def _content_of(response) -> str:
    """The assistant message text, or an empty string if the shape is not there."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    return str(getattr(message, "content", "") or "")


def classify(exc: BaseException) -> ExtractionError | None:
    """Turn an SDK exception into ours, or return None if it is not one.

    A rejected or missing key names `OPENAI_API_KEY`, so a facilitator is told
    which variable to fix rather than shown an authentication error out of the
    SDK. Everything else the SDK raises becomes a plain :class:`ExtractionError`
    with the reason, which the caller records against the record while the
    transcript stays stored and retryable.
    """
    try:
        import openai
    except ImportError:  # pragma: no cover - the SDK is a pinned dependency
        return None

    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ExtractionError(
            "the extraction API rejected OPENAI_API_KEY; the key is missing a permission "
            "or is no longer valid"
        )
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return ExtractionError(f"the extraction API was unreachable ({exc})")
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        return ExtractionError(f"the extraction API refused the request ({status})")
    if isinstance(exc, openai.OpenAIError):
        return ExtractionError(f"the extraction API could not be called ({exc})")
    return None
