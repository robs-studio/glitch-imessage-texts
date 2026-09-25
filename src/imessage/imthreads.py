"""iMessage intake — conversation-days, the person-day fold, the topic, and the stored thread.

:mod:`imchat` hands back one :class:`imchat.Message` per readable row.  This
module is what turns that stream into the units a person's card is stamped from,
and it writes the transcript each stamp points at.  Nothing here reaches the
network, nothing here opens the Messages database, and nothing here imports
engine code — ``imspine`` is the only module in this plug-in allowed to do that,
so the containment check below is written out in the standard library rather than
borrowed from ``config._path_is_within``.

The one thing that is written anywhere: a thread file under
:data:`imconfig.THREADS_DIR`, atomically, at mode ``0600``.

The shape of the pipeline
--------------------------
``messages → group() → chat-days → is_included() → fold_to_person_days() →
person-days``, which is what :func:`units_for` does end to end.  The two unit
types are deliberately different things:

* a **chat-day** is *one conversation on one local date* — it is what the
  substance floor judges, what a topic is written for, and what a thread file
  holds;
* a **person-day** is *one person on one local date*, folded across every
  conversation they spoke in — it is what a card is stamped from.

``is_included`` and :func:`direction_for` accept either, because both expose the
same surface (``day``, ``messages``, ``turns``, ``senders``, ``speakers``,
``counterparts``).  :func:`topic_for` and :func:`write_thread` take a chat-day,
because a topic quotes one conversation and a filename carries one
``chat_rowid``.

Why a conversation that crosses midnight splits at midnight
------------------------------------------------------------
A chat-day is keyed by **each message's own local date**, so a conversation that
runs from 23:40 to 00:20 becomes two chat-days.  A card's line says "we talked on
the 14th"; a conversation filed whole under the 14th would put words spoken on
the 15th onto the 14th's line, and the member reading their own card would find a
day that does not match what happened.  The day comes from
:attr:`imchat.Message.dt_local`, which is already in the **configured** zone
(``HEARTBEAT_TIMEZONE``) rather than wherever the member is standing — a
boundary that moved with the traveller would re-file yesterday on every trip.

Why the person-day fold exists at all (decision 1)
---------------------------------------------------
One person appearing in a 1:1 and two group chats on one date is **one** thing
that happened between the member and that person, not three.  Measured over a
year of one real history, folding removes about one interaction line in eight —
and ``/recall`` reads only the twenty most recent interactions off a card
(``recall.py:207``), so every duplicate line is a real memory pushed off the end.

Why a person only earns a day they SPOKE in (the speaker rule)
----------------------------------------------------------------
The substance floor judges the *unit*; this rule judges the *person*.  A person
is folded in only from chat-days where **they themselves sent at least one
turn**.  Without it, every silent member of a family or club group chat gets a
line saying they reached out on a day they said nothing — with somebody else's
sentence quoted as the topic.  Group chats can carry confidential, sensitive
conversations, so an interaction stamped onto the wrong person's card, carrying
a third party's words, is precisely the harm this plug-in exists to avoid.
Unknown senders (a row with no handle) are counted as real speakers for the
substance floor but earn nobody a day, because there is no person to earn it.

Turns, not messages — and why the substance floor is a floor
--------------------------------------------------------------
A **turn** is a maximal run of consecutive messages from one speaker inside one
conversation.  Six rounds of "ok" from one person is ONE turn from ONE sender and
fails; "Can we move to 4pm?" / "Works for me" is two turns from two senders and
is the kind of truth worth keeping.  Turns are counted per conversation and a
person-day's turns are its chat-days' turns added together, because a turn is a
conversational fact and interleaving two separate conversations would invent
turn-taking that never happened.

``substance_min_chars`` is ANDed with the turn and sender floors, exactly as the
brief and ``imconfig``'s docstring specify.  It shipped at 80 and the member set it
to 20 on 2026-09-20, because at 80 the "Can we move to 4pm?" / "Works for me" exchange
(31 characters) is dropped even though it is the worked example for keeping
short exchanges.  The tests pin both sides of it, so the trade stays visible;
:func:`is_included` carries the measured numbers.

The member is never a counterpart
-----------------------------------
The engine's owner filter is **email-only** (``meeting_scaffold.py:263`` reads
``if a.email and a.email.lower() in owner_emails``), and an iMessage handle is a
phone number, so the engine cannot recognise the member here.  This module has to
do it itself, and it does it two ways at once: :attr:`imchat.Message.is_from_me`
**and** membership of ``own_handles`` from ``config.local.json`` (which catches a
message the member sent from another of their own numbers, arriving as an inbound
row).  An empty ``own_handles`` is a REFUSAL, not a warning — :func:`owner_handles`
raises :class:`OwnHandlesRequired` carrying
:data:`imconfig.REFUSAL_NO_OWN_HANDLES`, and the entry points refuse with it
rather than proceed.  Without that, the member's own outbound texts are filed as
somebody else's turns onto the member's own card.

One line per person per day, and the ledger key it is recorded under
----------------------------------------------------------------------
**Every counterpart gets one line per ``(person, local day)``, however often they
text** (the member's ruling, 2026-09-24: frequent contacts getting one line a week
"doesn't make sense").  So a person who texts on thirty days gets thirty lines, each keyed
``<identifier>|day|<YYYY-MM-DD>`` by :func:`ledger_key`.  The cost is accepted
and said once: ``/recall`` lists a person's twenty newest interactions
(``recall.py:207``), so for the heaviest texters that list will be mostly texts,
while their email and meeting lines stay on the card and in search.

The key keeps three parts although the middle one is always ``day``.  It costs
nothing, and it keeps every key already written, and every module already coded
against that shape, readable with its meaning unchanged.  :func:`ledger_key`
refuses any other grain, so no second shape can creep back in.

The stored thread — why every rule about the filename is load-bearing
------------------------------------------------------------------------
* **The label is slugified before it reaches a filename or a link**
  (:func:`imcontacts.slugify_label`).  Contacts labels often carry brackets
  (``Mom (cell)``) and the engine's pointer parser is
  ``people_index.py:53`` — ``re.compile(r"\\(\\s*→\\s*([^)]+)\\)")`` — which stops
  at the first ``)``, truncating the stored link and spilling the filename's tail
  into the card's visible summary.  ``/`` and ``:`` break the path outright.
* **Any disambiguating suffix is the ``chat_rowid``, never run order.**  Two
  conversations sharing a Contacts label would otherwise swap filenames between
  runs, so yesterday's link would point at today's other thread.
* **The link is Brain-root-relative** — ``_local/imessage/threads/YYYY/<file>``,
  never a bare ``threads/…``.  Every other link on the spine is vault-relative
  and resolved against ``glitch-mem/Memory/``, so a bare path would be read as
  ``glitch-mem/Memory/threads/…`` and lead nowhere on every card, forever.  The
  leading ``_local/`` is what makes it visibly not a vault path.  It is built
  with forward slashes on every OS, because a backslash in a card is a dead link
  on the Mac that reads it next.
* **The write refuses any target not contained within** :data:`imconfig.THREADS_DIR`,
  with **both sides resolved first** — on macOS the temp tree and ``/var`` are
  symlinks, and comparing a resolved path against an unresolved one silently
  decides "different folder" and waves the write through.  familywall's
  ``session.py:165`` predicate (``resolved.parent == home``) is deliberately NOT
  copied: a thread file at ``HOME/threads/YYYY/…`` has a parent that is never
  ``HOME``, so that test would pass everything here — including ``~/.ssh/x``.
* **The payload is serialised BEFORE the temp file is created**, so a body this
  module cannot render leaves nothing at all on disk: no file, no temp file, no
  empty year folder.

Reactions are not filtered here.  ``imchat`` excludes them by
``associated_message_type``, which is the reliable signal (their bodies arrive in
more than one language on real data — "Gefällt", "ein Herz" — so a text
filter would leak them).  The test suite sweeps every produced thread file for
reaction wording anyway, as a floor on the whole pipeline rather than a second
filter here.
"""

from __future__ import annotations

# imconfig FIRST: importing it puts the engine's `.claude/scripts` on sys.path
# and then re-asserts THIS folder in front of it, so no engine module can ever
# shadow one of ours. See imconfig's docstring for the bug that rule prevents.
import imconfig

import contextlib
import os
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import imcontacts
import imredact

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: The speaker key for the member's own turns. Not a canonical identifier and it
#: can never collide with one: :func:`imcontacts.canonicalise` returns an address
#: (which contains ``@``) or bare/`+`-prefixed digits, and never parentheses.
OWNER: str = "(me)"

#: The speaker key for an inbound row that names no handle. It counts as a real
#: distinct sender for the substance floor — somebody spoke — but it earns nobody
#: a conversation-day, because there is no person to earn it. Measured: no
#: qualifying inbound row lacked a handle on a real store (imchat's docstring).
UNKNOWN_SPEAKER: str = "(unknown)"

#: The one grain a ledger key is ever built at: one line per person per local
#: day, however often they text. See the module docstring.
DAY_GRAIN: str = "day"
#: Every grain :func:`ledger_key` accepts and :func:`parse_ledger_key` reads.
#: Exactly one, on purpose; see the module docstring.
GRAINS: frozenset[str] = frozenset({DAY_GRAIN})

#: The engine's direction vocabulary. Exactly three strings; inventing a fourth
#: makes ``stamp_interaction`` return ``stamped=False`` without raising
#: (``people_stamp.py:65-68``), which loses the day silently.
THEY_REACHED_OUT: str = "they_reached_out"
I_REACHED_OUT: str = "i_reached_out"
MUTUAL: str = "mutual"
DIRECTIONS: frozenset[str] = frozenset({THEY_REACHED_OUT, I_REACHED_OUT, MUTUAL})

#: The separator in a ledger key. ``|`` cannot occur in a canonical identifier,
#: and :func:`parse_ledger_key` splits from the RIGHT regardless, so even an
#: identifier carrying one round-trips.
LEDGER_KEY_SEP: str = "|"

#: The Brain-root-relative prefix every stored-thread link carries. Written out
#: rather than derived so it is the same string on every machine and in every
#: test, and so the leading ``_local/`` is visible in the source.
THREAD_LINK_PREFIX: str = "_local/imessage/threads"

#: A sender whose canonical form is all digits and SHORTER than this is a bulk
#: sender (a short code), not a person. Seven is the engine's own bar: its
#: ``same_phone`` matches on a >=7-digit suffix, so nothing shorter can be
#: matched to a real person anyway.
SHORTCODE_MIN_DIGITS: int = 7

#: The label a group with no display name gets. Most group chats are unnamed
#: (over nine in ten on a real store), and the ``chat_rowid`` suffix is what keeps
#: their filenames distinct.
UNNAMED_GROUP_LABEL: str = "group"

#: The label for a one-to-one whose counterpart is not named anywhere.
UNKNOWN_LABEL: str = "unknown"

#: How much of the opener a 1:1 topic may carry, ellipsis included.
TOPIC_OPENER_MAX: int = 80

#: What a rendered line says instead of a body. ``""`` and ``None`` mean opposite
#: things out of :mod:`imchat` (wordless vs unreadable) and the transcript keeps
#: them apart, because "a photo" and "I could not read this" are different facts.
NO_WORDS: str = "[no words]"
UNREADABLE: str = "[unreadable]"

#: Every character that would split one message across two lines of the
#: transcript. CR and LF are the ones that actually occur; the Unicode line and
#: paragraph separators are included because a file read line-by-line breaks on
#: them just as badly and they cost nothing to fold.
_LINE_BREAKS: tuple[str, ...] = ("\r\n", "\r", "\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\u0085", " ", " ")


class OwnHandlesRequired(RuntimeError):
    """The refusal: this run must not start without the member's own handles.

    Carries :data:`imconfig.REFUSAL_NO_OWN_HANDLES` as its message, so a caller
    prints ``str(exc)`` and exits 0 rather than showing a traceback.  It is an
    exception rather than a return value because every entry point here has to
    stop, and a sentinel that a caller could ignore is exactly how the member's
    own texts end up stamped onto the member's own card.
    """


def _warn(message: str) -> None:
    """One plain line to stderr, in the plug-in's house prefix."""
    print(f"[imessage] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Settings.
# ---------------------------------------------------------------------------


def owner_handles(cfg: Mapping[str, Any] | None = None) -> frozenset[str]:
    """The member's own handles, canonicalised — or REFUSE.

    Raises :class:`OwnHandlesRequired` when ``own_handles`` is missing, empty or
    malformed, because :func:`imconfig.require_own_handles` returns ``None`` for
    all three and every one of them means the member's own voice would be filed
    as somebody else's.  See the module docstring.

    Each handle goes through :func:`imcontacts.canonicalise` so it is compared in
    the same spelling every other identifier here is stored in; a handle that
    canonicalises to ``None`` (no digits, no ``@``) is dropped, and if that
    leaves nothing at all the refusal fires too — a config full of junk is the
    same situation as an empty one.
    """
    handles = imconfig.require_own_handles(dict(cfg) if cfg is not None else None)
    if handles is None:
        raise OwnHandlesRequired(imconfig.REFUSAL_NO_OWN_HANDLES)

    canonical = {c for c in (imcontacts.canonicalise(h) for h in handles) if c}
    if not canonical:
        raise OwnHandlesRequired(imconfig.REFUSAL_NO_OWN_HANDLES)
    return frozenset(canonical)


def never_ingest(cfg: Mapping[str, Any] | None = None) -> frozenset[str]:
    """The canonicalised ``never_ingest`` list — conversations never recorded.

    Canonicalised for the same reason ``own_handles`` is: a member types
    ``212-555-0142`` and the database holds ``+12125550142``, and a list that
    only matches one spelling is a list that quietly does nothing.
    """
    if cfg is None:
        cfg = imconfig.load_config()
    raw = cfg.get("never_ingest")
    if not isinstance(raw, (list, tuple)):
        return frozenset()
    return frozenset(
        c for c in (imcontacts.canonicalise(entry) for entry in raw) if c
    )


def _int_setting(cfg: Mapping[str, Any] | None, key: str) -> int:
    """One integer knob, falling back to the shipped default rather than raising.

    A member edits ``config.json`` by hand, so a string or a null in a numeric
    key is a realistic accident.  The shipped default is a better answer than a
    traceback in the middle of a morning run, and it is the answer
    :func:`imconfig.load_config` already promises for a corrupt file.
    """
    default = int(imconfig.DEFAULTS[key])
    if cfg is None:
        return default
    value = cfg.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        _warn(f"{key} in your config is not a number; using the default ({default}).")
        return default


# ---------------------------------------------------------------------------
# Text: one line, always.
# ---------------------------------------------------------------------------


def sanitise_body(text: str | None) -> str:
    """A body as ONE line: every CR/LF collapsed to a space, before anything else.

    This runs first in every path that renders text — the topic and the stored
    transcript both.  ``people_stamp._esc`` does the same collapse on its way
    into a card, but the topic is *built* here and **the stored thread never
    passes through ``_esc`` at all**, so a newline that survives this function
    survives onto disk and turns one message into two lines of the transcript.

    Beyond the collapse: runs of whitespace become a single space and the result
    is stripped, so a paragraph break renders as one space rather than three, and
    a body that is nothing but whitespace renders as ``""``.  ``None`` and a
    non-string both return ``""`` rather than raising — this module renders a
    member's real messages and a single odd value must not take down a run.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)

    for token in _LINE_BREAKS:
        if token in text:
            text = text.replace(token, " ")
    return " ".join(text.split())


def _redacted(text: str | None) -> str:
    """Sanitise, redact, then sanitise again — and the second pass is the point.

    :mod:`imredact` is another module's contract: it promises a redaction, not a
    promise about whitespace.  The transcript is a line-per-message file and the
    "no CR/LF inside a rendered line" invariant has to hold whatever a
    replacement happens to contain, so the collapse is re-run over the result.
    :func:`sanitise_body` is idempotent, so on the ordinary path the second pass
    costs one scan and changes nothing.
    """
    body = sanitise_body(text)
    if not body:
        return ""
    return sanitise_body(imredact.redact(body) or "")


# ---------------------------------------------------------------------------
# Speakers and turns.
# ---------------------------------------------------------------------------


def _canon(handle: str | None) -> str | None:
    return imcontacts.canonicalise(handle)


def is_owner_message(message: Any, own: Iterable[str] = ()) -> bool:
    """Did the MEMBER send this? ``is_from_me`` **and** ``own_handles``.

    Both, not either.  ``is_from_me`` covers everything this Mac sent; the
    handle check covers a message the member sent from another of their own
    numbers or addresses, which arrives on this machine as an ordinary inbound
    row and would otherwise be filed as a stranger's turn — onto the member's own
    card, since the engine's owner filter is email-only.
    """
    if getattr(message, "is_from_me", False):
        return True
    canon = _canon(getattr(message, "handle", None))
    return canon is not None and canon in set(own)


def _speaker_key(message: Any, own: frozenset[str]) -> str:
    """Who spoke: :data:`OWNER`, a canonical identifier, or :data:`UNKNOWN_SPEAKER`.

    ``is_from_me`` is branched on FIRST, because on an outbound row the handle is
    the RECIPIENT (measured: every outbound one-to-one row on a real store).  Reading
    it as the sender writes the member's words onto everyone else's history.
    """
    if is_owner_message(message, own):
        return OWNER
    canon = _canon(getattr(message, "handle", None))
    return canon if canon is not None else UNKNOWN_SPEAKER


def _message_order(message: Any) -> tuple[datetime, int]:
    """Chronological, with ``date_raw`` as the tie-break — never ROWID.

    ROWID is insertion order and iCloud back-fill makes it disagree with
    chronology on 15.6% of the rows of a real store (imchat's docstring).
    """
    return (message.dt_local, int(getattr(message, "date_raw", 0) or 0))


@dataclass(frozen=True)
class Turn:
    """A maximal run of consecutive messages from one speaker in one conversation.

    Frozen for the same reason :class:`imchat.Message` is: a turn is counted, and
    a count that can change after it was taken is not a count.

    Attributes:
        speaker: :data:`OWNER`, :data:`UNKNOWN_SPEAKER`, or a canonical identifier.
        is_from_me: True when this is the member's own turn.
        messages: The run, oldest first. Never empty.
    """

    speaker: str
    is_from_me: bool
    messages: tuple

    @property
    def started_at(self) -> datetime:
        """When the turn opened — what :func:`direction_for` compares."""
        return self.messages[0].dt_local

    @property
    def body_chars(self) -> int:
        """Characters of real, sanitised body in this turn. Wordless rows add 0."""
        return sum(len(sanitise_body(m.text)) for m in self.messages if m.text)


def _turns(messages: Sequence[Any], own: frozenset[str]) -> tuple[Turn, ...]:
    """Consecutive messages by one speaker, folded into turns. Order preserved."""
    turns: list[Turn] = []
    run: list[Any] = []
    run_speaker: str | None = None

    for message in messages:
        speaker = _speaker_key(message, own)
        if speaker != run_speaker and run:
            turns.append(
                Turn(speaker=run_speaker, is_from_me=run_speaker == OWNER, messages=tuple(run))
            )
            run = []
        run_speaker = speaker
        run.append(message)

    if run:
        turns.append(
            Turn(speaker=run_speaker, is_from_me=run_speaker == OWNER, messages=tuple(run))
        )
    return tuple(turns)


def _ordered_distinct(values: Iterable[str | None]) -> tuple[str, ...]:
    """Distinct, in first-seen order. Order matters: it decides a 1:1's label."""
    seen: dict[str, None] = {}
    for value in values:
        if value is not None and value not in seen:
            seen[value] = None
    return tuple(seen)


# ---------------------------------------------------------------------------
# The two units.
# ---------------------------------------------------------------------------


class _Unit:
    """What :func:`is_included` and :func:`direction_for` accept.

    Both a chat-day and a person-day answer ``day``, ``messages``, ``turns``,
    ``senders``, ``speakers`` and ``counterparts``, so the floor and the
    direction are written once against that surface rather than twice.
    """

    @property
    def senders(self) -> tuple[str, ...]:
        """Every distinct speaker key, the member and an unknown sender included.

        This is what ``substance_min_senders`` counts: "did more than one voice
        actually speak", which is a question about the conversation, not about
        who can be filed.
        """
        return _ordered_distinct(turn.speaker for turn in self.turns)

    @property
    def speakers(self) -> tuple[str, ...]:
        """The real, non-member identifiers that spoke — who can earn a day.

        :data:`UNKNOWN_SPEAKER` is excluded here and included in
        :attr:`senders`, and the difference is the whole point: an unnamed voice
        is a real turn in the conversation and is nobody's interaction.
        """
        return tuple(
            s for s in self.senders if s != OWNER and s != UNKNOWN_SPEAKER
        )

    @property
    def body_chars(self) -> int:
        """Combined characters of real body across the unit."""
        return sum(turn.body_chars for turn in self.turns)

    @property
    def message_count(self) -> int:
        return len(self.messages)

    def turns_by(self, identifier: str) -> tuple[Turn, ...]:
        """That identifier's own turns, oldest first."""
        return tuple(turn for turn in self.turns if turn.speaker == identifier)

    @property
    def my_turns(self) -> tuple[Turn, ...]:
        """The member's own turns, oldest first."""
        return tuple(turn for turn in self.turns if turn.speaker == OWNER)


@dataclass(frozen=True)
class ChatDay(_Unit):
    """One conversation on one local date — the unit the substance floor judges.

    Attributes:
        chat_rowid: ``chat.ROWID``. The ONLY disambiguator a filename ever uses.
        day: The local date, from each message's own ``dt_local``.
        is_group: From ``chat.style``, never a head count — a group chat can
            have exactly two participants, and hundreds do on a real store.
        chat_name: The chat's display name, or ``None`` (most groups).
        own_handles: The member's canonical handles, carried so that everything
            downstream can tell the member apart without re-reading config.
        messages: Oldest first.
        turns: The messages folded into turns.
        counterparts: Distinct canonical handles seen on this conversation's
            rows, the member removed. On an inbound row that is the sender; on an
            outbound row the recipient. Not the same as :attr:`speakers` — a
            counterpart may never have said a word.
    """

    chat_rowid: int
    day: date
    is_group: bool
    chat_name: str | None
    own_handles: frozenset[str]
    messages: tuple
    turns: tuple[Turn, ...]
    counterparts: tuple[str, ...]


@dataclass(frozen=True)
class PersonDay(_Unit):
    """One person on one local date, folded across every chat they spoke in.

    This is the decision-1 dedupe: a 1:1 plus two group chats on one date is ONE
    line on that person's card, not three.

    Attributes:
        identifier: The canonical identifier. Never the member's own.
        day: The local date.
        chat_days: The conversations it was folded from, oldest first. Every one
            of them is a chat-day this person **spoke in**.
    """

    identifier: str
    day: date
    chat_days: tuple[ChatDay, ...]

    @property
    def messages(self) -> tuple:
        """Every message across the constituent chat-days, oldest first."""
        return tuple(
            sorted(
                (m for chat_day in self.chat_days for m in chat_day.messages),
                key=_message_order,
            )
        )

    @property
    def turns(self) -> tuple[Turn, ...]:
        """The chat-days' turns added together, ordered by when each opened.

        Added, not recomputed across the merged stream: a turn is a fact about
        one conversation, and interleaving two separate threads would invent
        turn-taking that never happened.
        """
        return tuple(
            sorted(
                (turn for chat_day in self.chat_days for turn in chat_day.turns),
                key=lambda turn: turn.started_at,
            )
        )

    @property
    def counterparts(self) -> tuple[str, ...]:
        return _ordered_distinct(
            c for chat_day in self.chat_days for c in chat_day.counterparts
        )

    @property
    def own_handles(self) -> frozenset[str]:
        merged: set[str] = set()
        for chat_day in self.chat_days:
            merged |= set(chat_day.own_handles)
        return frozenset(merged)

    @property
    def is_group(self) -> bool:
        """True when EVERY conversation it was folded from is a group chat."""
        return all(chat_day.is_group for chat_day in self.chat_days)


# ---------------------------------------------------------------------------
# 1. Grouping.
# ---------------------------------------------------------------------------


def group(
    messages: Iterable[Any], *, own_handles: Iterable[str] = ()
) -> dict[tuple[int, date], ChatDay]:
    """Chat-days keyed ``(chat_rowid, local_day)`` — midnight splits a conversation.

    The day is **each message's own** ``dt_local.date()``, which is already in the
    configured zone, so a conversation running from 23:40 to 00:20 becomes two
    chat-days and a card never claims words were spoken on a day they were not.

    ``own_handles`` is how the member is recognised beyond ``is_from_me``; it
    defaults to empty so this stays a pure function that a test can call
    directly, and the refusal for an empty list lives in the entry points
    (:func:`units_for`, :func:`owner_handles`) rather than here.

    Returns a dict rather than a list because the key is the identity of the
    unit, and a caller resuming a run needs to look one up.
    """
    own = frozenset(own_handles)
    buckets: dict[tuple[int, date], list[Any]] = {}

    for message in messages:
        key = (int(message.chat_rowid), message.dt_local.date())
        buckets.setdefault(key, []).append(message)

    chat_days: dict[tuple[int, date], ChatDay] = {}
    for (chat_rowid, day), rows in buckets.items():
        ordered = tuple(sorted(rows, key=_message_order))
        first = ordered[0]
        counterparts = _ordered_distinct(
            canon
            for canon in (_canon(m.handle) for m in ordered)
            if canon is not None and canon not in own
        )
        chat_days[(chat_rowid, day)] = ChatDay(
            chat_rowid=chat_rowid,
            day=day,
            is_group=bool(first.is_group),
            chat_name=first.chat_name,
            own_handles=own,
            messages=ordered,
            turns=_turns(ordered, own),
            counterparts=counterparts,
        )
    return chat_days


# ---------------------------------------------------------------------------
# 2. The person-day fold.
# ---------------------------------------------------------------------------


def _as_chat_days(chat_days: Any) -> list[ChatDay]:
    """Accept the dict :func:`group` returns, or any iterable of chat-days."""
    if isinstance(chat_days, Mapping):
        return list(chat_days.values())
    return list(chat_days)


def fold_to_person_days(chat_days: Any) -> dict[tuple[str, date], PersonDay]:
    """Chat-days folded to ``(canonical_identifier, local_day)`` — decision 1.

    One person in a 1:1 and two group chats on one date yields **ONE** unit, not
    three.  Measured over a year of one real history that removes about one line
    in eight, and ``/recall`` only ever reads the twenty most recent off a card.

    **A person is folded in only from chat-days they SPOKE in** — the rule the
    speaker-rule paragraph of the module docstring is about.  A silent participant
    in a family group chat earns nothing, so no card is ever stamped "they reached
    out" over somebody else's sentence.

    The member can never be a key: :data:`OWNER` is not in
    :attr:`_Unit.speakers`, and any identifier that is one of the member's own
    handles is skipped as well, so a stray row that named the member's number
    cannot mint a person-day for the member.
    """
    units: dict[tuple[str, date], list[ChatDay]] = {}

    for chat_day in _as_chat_days(chat_days):
        own = set(chat_day.own_handles)
        for identifier in chat_day.speakers:
            if identifier in own:
                continue  # belt and braces: the member is never a counterpart
            units.setdefault((identifier, chat_day.day), []).append(chat_day)

    folded: dict[tuple[str, date], PersonDay] = {}
    for (identifier, day), days in units.items():
        ordered = tuple(sorted(days, key=lambda c: (c.messages[0].dt_local, c.chat_rowid)))
        folded[(identifier, day)] = PersonDay(
            identifier=identifier, day=day, chat_days=ordered
        )
    return folded


# ---------------------------------------------------------------------------
# 3. The substance floor.
# ---------------------------------------------------------------------------


def is_shortcode(identifier: str | None) -> bool:
    """Is this a bulk sender rather than a person?

    An all-digit identifier shorter than :data:`SHORTCODE_MIN_DIGITS` — a 5- or
    6-digit short code is a bank, a delivery firm or a 2FA robot.  Seven digits is
    the engine's own bar (``same_phone`` matches on a >=7-digit suffix), so
    nothing shorter could be matched to a real person anyway.  An address is
    never a short code.
    """
    if not identifier:
        return False
    if "@" in identifier:
        return False
    digits = identifier[1:] if identifier.startswith("+") else identifier
    return digits.isdigit() and len(digits) < SHORTCODE_MIN_DIGITS


def is_included(unit: _Unit, cfg: Mapping[str, Any] | None = None) -> bool:
    """Is this unit worth recording at all? The floor, on TURNS not messages.

    Five gates, in the order that makes the cheapest, most absolute one first:

    1. **``never_ingest``** — a conversation with any of those handles is never
       recorded, whichever side it came from;
    2. **the shortcode floor** — a unit whose only real voices are short codes is
       bulk mail, not a conversation (and a person-day for a short code is
       dropped outright);
    3. **``substance_min_turns``** (2) — six rounds of "ok" from one person is
       ONE turn and fails here;
    4. **``substance_min_senders``** (2) — both sides must actually have spoken,
       so an unanswered outbound text is not logged as an interaction;
    5. **``substance_min_chars``** (20) — catches what the turn count misses,
       several messages that are all single emoji or reactions.

    Gate 5 is ANDed with 3 and 4.  **It shipped at 80 and the member set it to 20
    on 2026-09-20**, because 80 contradicted the worked example it was written
    beside: "Can we move to 4pm?" / "Works for me" is 31 characters and an
    80-char floor throws away the very exchange the rule calls "exactly the
    truth worth keeping".  Measured over a year of one real history: about 75% of
    chat-days clear 20 and about 70% clear 80 — **roughly one real contact in
    twenty lost to the higher floor**, which would have left ``last_contacted``
    wrong on the cards of the people the member texts most briefly.  The tests still pin BOTH
    sides, so the trade stays visible in the suite rather than being discovered
    on a card that never appeared.
    """
    if cfg is None:
        cfg = imconfig.load_config()

    blocked = never_ingest(cfg)
    participants = set(unit.speakers) | set(unit.counterparts)
    identifier = getattr(unit, "identifier", None)
    if identifier:
        participants.add(identifier)
    if blocked & participants:
        return False

    if identifier and is_shortcode(identifier):
        return False
    real_voices = [s for s in unit.speakers if not is_shortcode(s)]
    if unit.speakers and not real_voices:
        return False

    if len(unit.turns) < _int_setting(cfg, "substance_min_turns"):
        return False
    if len(unit.senders) < _int_setting(cfg, "substance_min_senders"):
        return False
    if unit.body_chars < _int_setting(cfg, "substance_min_chars"):
        return False
    return True


# ---------------------------------------------------------------------------
# 4. Direction.
# ---------------------------------------------------------------------------


def direction_for(unit: _Unit, identifier: str) -> str:
    """Who reached out — from THAT person's turns beside the member's.

    Never from "the day's first message": in a group that is whoever spoke first,
    who may be neither of them, and a card would then say a person reached out
    because a third party opened a thread they were both in.

    Returns exactly one of :data:`THEY_REACHED_OUT`, :data:`I_REACHED_OUT`,
    :data:`MUTUAL` — the engine's whole vocabulary.  ``mutual`` when both sent
    more than one turn (a real back-and-forth outranks who happened to be first);
    otherwise whoever opened.

    A unit where neither of them spoke returns :data:`MUTUAL` as the neutral
    answer: it is the only one of the three that asserts nothing about who
    opened, and this never raises.
    """
    canonical = _canon(identifier) or identifier
    theirs = unit.turns_by(canonical)
    mine = unit.my_turns

    if not theirs and not mine:
        return MUTUAL
    if not theirs:
        return I_REACHED_OUT
    if not mine:
        return THEY_REACHED_OUT
    if len(theirs) > 1 and len(mine) > 1:
        return MUTUAL
    return THEY_REACHED_OUT if theirs[0].started_at <= mine[0].started_at else I_REACHED_OUT


# ---------------------------------------------------------------------------
# 5. The ledger key.
# ---------------------------------------------------------------------------


def ledger_key(identifier: str, grain: str, period_key: date | str) -> str:
    """``"<identifier>|day|<YYYY-MM-DD>"`` — the key one person-day is recorded under.

    Every counterpart gets one line per ``(person, local day)``, so ``grain`` is
    always :data:`DAY_GRAIN` and ``period_key`` is that local day.  **Any other
    grain raises** :class:`ValueError`: a key built at a grain nothing else ever
    looks up is a day recorded where no reader will find it, and refusing at the
    source is what keeps a second key shape from creeping back in.

    Why three parts when the middle one never varies: it costs nothing, and it
    keeps every key already written, and every module already coded against this
    shape, readable with its meaning unchanged.  Every caller builds its key here
    so the shape cannot be got wrong in one place and right in another.
    """
    if grain not in GRAINS:
        raise ValueError(
            f"grain must be {DAY_GRAIN!r} (one line per person per day), not {grain!r}"
        )
    period = period_key.isoformat() if isinstance(period_key, date) else str(period_key)
    return f"{identifier}{LEDGER_KEY_SEP}{grain}{LEDGER_KEY_SEP}{period}"


def parse_ledger_key(key: str) -> tuple[str, str, date] | None:
    """A ledger key back into ``(identifier, grain, period)``, or ``None``.

    Split from the RIGHT, so an identifier that somehow carries the separator
    still round-trips.  ``None`` for anything that is not a key this module
    wrote, so a stray entry in a hand-edited ledger is ignored rather than
    crashing a run.
    """
    if not isinstance(key, str):
        return None
    parts = key.rsplit(LEDGER_KEY_SEP, 2)
    if len(parts) != 3:
        return None
    identifier, grain, period = parts
    if not identifier or grain not in GRAINS:
        return None
    try:
        return identifier, grain, date.fromisoformat(period)
    except ValueError:
        return None


def _stamped_map(ledger_like: Any) -> Mapping[str, Any]:
    """The ``stamped`` map out of whatever a caller passed.

    ``ledger_like`` is deliberately loose, because the ledger module lands later
    and a signature that only accepted its exact object would make this
    untestable until then.  Accepted: ``None``; a mapping with a ``stamped`` key;
    an object with a ``stamped`` attribute; a plain mapping of ledger keys; any
    iterable of ledger-key strings.
    """
    if ledger_like is None:
        return {}
    inner = None
    if isinstance(ledger_like, Mapping):
        inner = ledger_like.get("stamped", None)
        if inner is None:
            return ledger_like
    else:
        inner = getattr(ledger_like, "stamped", None)
        if inner is None:
            if isinstance(ledger_like, Iterable) and not isinstance(ledger_like, (str, bytes)):
                return {str(key): {} for key in ledger_like}
            return {}
    if isinstance(inner, Mapping):
        return inner
    if isinstance(inner, Iterable) and not isinstance(inner, (str, bytes)):
        return {str(key): {} for key in inner}
    return {}


def stamped_periods(identifier: str, ledger_like: Any) -> list[tuple[str, date]]:
    """Every ``(grain, period)`` already stamped for ``identifier``, from the ledger.

    The KEY is authoritative, because that is what idempotency is actually keyed
    on.  An older two-part key (``identifier|period``) is still read, taking the
    grain from the entry's own ``grain`` field and defaulting to ``day`` — so a
    ledger written before the grain joined the key is still read as already
    stamped instead of re-stamping a year of days.
    """
    found: list[tuple[str, date]] = []
    for key, entry in _stamped_map(ledger_like).items():
        parsed = parse_ledger_key(key)
        if parsed is not None:
            who, grain, period = parsed
        else:
            parts = str(key).rsplit(LEDGER_KEY_SEP, 1)
            if len(parts) != 2:
                continue
            who = parts[0]
            grain = str(entry.get("grain", DAY_GRAIN)) if isinstance(entry, Mapping) else DAY_GRAIN
            if grain not in GRAINS:
                grain = DAY_GRAIN
            try:
                period = date.fromisoformat(parts[1])
            except ValueError:
                continue
        if who == identifier:
            found.append((grain, period))
    return found


# ---------------------------------------------------------------------------
# 6. Labels and topics.
# ---------------------------------------------------------------------------


def label_for(unit: Any, contacts: Mapping[str, str] | None = None) -> str:
    """The human label for a conversation, BEFORE slugification.

    A named group is its name; an unnamed group is :data:`UNNAMED_GROUP_LABEL`
    (most groups have no name, and the ``chat_rowid`` suffix is what keeps their
    filenames apart).  A one-to-one is the Contacts name for its
    counterpart, falling back to the canonical handle itself and then to
    :data:`UNKNOWN_LABEL`.

    The first counterpart is taken in **first-seen order**, never in whatever
    order a set happened to iterate: a label that changed between runs would
    rename a person's thread files.
    """
    if getattr(unit, "is_group", False):
        return sanitise_body(unit.chat_name) or UNNAMED_GROUP_LABEL

    names = dict(contacts or {})
    for identifier in (*unit.speakers, *unit.counterparts):
        named = sanitise_body(names.get(identifier))
        if named:
            return named
    for identifier in (*unit.speakers, *unit.counterparts):
        if identifier:
            return identifier
    return UNKNOWN_LABEL


def slug_for(unit: Any, contacts: Mapping[str, str] | None = None) -> str:
    """:func:`label_for`, reduced to ``[a-z0-9-]`` and never empty.

    The slug is what reaches a filename and the ``(→ …)`` pointer on a card.  The
    engine's parser stops at the first ``)``, so an unslugged ``Mom (cell)``
    truncates the stored link and spills the filename's tail into the visible
    summary; ``/`` and ``:`` break the path outright.
    """
    return imcontacts.slugify_label(label_for(unit, contacts))


def _opener(unit: Any) -> str:
    """The first message in the unit that actually carries words, redacted.

    Redacted BEFORE it is truncated: truncating first can cut a one-time code in
    half so it no longer matches its pattern, which leaks the half that is left.
    """
    for message in unit.messages:
        body = _redacted(message.text)
        if body:
            if len(body) > TOPIC_OPENER_MAX:
                return body[: TOPIC_OPENER_MAX - 1].rstrip() + "…"
            return body
    return ""


def topic_for(unit: Any) -> str:
    """The one line that lands on a card.

    * one-to-one — ``Texts (N): <opener>``, the opener redacted and capped at
      :data:`TOPIC_OPENER_MAX` characters;
    * group — ``Group texts in <name> (N messages)``, and **no message body at
      all**.

    A group topic quotes nothing on purpose.  In a group the speaker of any given
    line is ambiguous on a card, so quoting one would attribute a third party's
    words to whoever the line was stamped onto — the same
    mis-attribution the person-day speaking rule guards against, arriving
    through the topic instead.  Only a 1:1 opener has an unambiguous speaker.
    """
    count = unit.message_count
    if getattr(unit, "is_group", False):
        name = sanitise_body(getattr(unit, "chat_name", None)) or "an unnamed group"
        return f"Group texts in {name} ({count} messages)"
    opener = _opener(unit)
    return f"Texts ({count}): {opener}" if opener else f"Texts ({count})"


# ---------------------------------------------------------------------------
# 7. The stored thread.
# ---------------------------------------------------------------------------


def _is_within(child: Path, parent: Path) -> bool:
    """True when ``child`` is a descendant of ``parent``, both already resolved.

    Case-folded, because APFS and NTFS are case-insensitive while
    ``Path.is_relative_to`` is case-sensitive, and a case-variant path would
    otherwise walk straight past the guard.  The same shape as the engine's
    ``config._path_is_within``, written out here because ``imspine`` is the only
    module in this plug-in that may import engine code.
    """
    try:
        return Path(str(child).casefold()).is_relative_to(Path(str(parent).casefold()))
    except ValueError:
        return False


def _threads_dir(threads_dir: Path | str | None) -> Path:
    return Path(imconfig.THREADS_DIR if threads_dir is None else threads_dir)


def _refuse_foreign_target(target: Path, threads_dir: Path | str | None = None) -> Path:
    """Refuse any write that is not CONTAINED WITHIN the threads folder.

    Both sides are resolved before comparing.  On macOS the temp tree and
    ``/var`` are symlinks, so comparing a resolved path against an unresolved one
    silently decides "different folder" and waves the write through — and a
    symlink planted inside the threads folder is exactly how a write escapes it.

    familywall's predicate is deliberately NOT mirrored here.  ``session.py:165``
    tests ``resolved.parent == home``: a whitelist of one *direct child* of HOME.
    A thread file at ``HOME/threads/YYYY/…`` has a parent that is never HOME, so
    copying that test literally would give **zero** protection here — and it
    would wave through ``~/.ssh/x``, whose parent is not HOME either.

    Returns the resolved target, so the caller writes the path that was checked
    rather than the one that was handed in.
    """
    base = _threads_dir(threads_dir).resolve()
    resolved = Path(target).resolve()
    if resolved == base or not _is_within(resolved, base):
        raise ValueError(
            f"refusing to write {resolved}: this module writes only inside {base}."
        )
    return resolved


def _chmod_600(path: Path) -> None:
    """Owner-only, where the OS means it; a silent no-op where it does not.

    POSIX modes are advisory on Windows, where ``os.chmod`` can only flip the
    read-only bit and on some mounts raises outright.  A transcript that saved is
    worth more than a mode that did not, and Windows is first-class here.
    """
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def thread_path(
    unit: Any,
    contacts: Mapping[str, str] | None = None,
    threads_dir: Path | str | None = None,
) -> Path:
    """Where a chat-day's transcript belongs: ``threads/YYYY/YYYY-MM-DD-<slug>-<rowid>.txt``.

    The suffix is the ``chat_rowid`` and **never run order** — two conversations
    sharing a Contacts label would otherwise swap filenames between runs, so a
    link written yesterday would open the other person's thread today.
    """
    chat_rowid = getattr(unit, "chat_rowid", None)
    if chat_rowid is None:
        raise ValueError(
            "a transcript is written for one conversation-day: this unit has no "
            "chat_rowid (a person-day is folded across several conversations)."
        )
    day = unit.day
    name = f"{day.isoformat()}-{slug_for(unit, contacts)}-{int(chat_rowid)}.txt"
    return _threads_dir(threads_dir) / f"{day.year:04d}" / name


def render_thread(unit: Any, contacts: Mapping[str, str] | None = None) -> str:
    """The transcript: one message per line, ``HH:MM  Who: body``, redacted.

    Every part of a line is sanitised, the speaker's name included — a Contacts
    name carrying a newline would otherwise turn one message into two lines and
    break every line-oriented reader of this file.

    ``""`` and ``None`` bodies are rendered differently on purpose: ``""`` is a
    photo or a sticker (:data:`NO_WORDS`) and ``None`` is bytes that could not be
    read (:data:`UNREADABLE`).  Collapsing them would claim to know something the
    decoder said it did not.

    There is no header line, so every line in the file is a message and an
    invariant sweep over the file needs no special cases.
    """
    names = dict(contacts or {})
    own = set(getattr(unit, "own_handles", frozenset()))
    lines: list[str] = []

    for message in unit.messages:
        if is_owner_message(message, own):
            who = "Me"
        else:
            canon = _canon(message.handle)
            who = sanitise_body(names.get(canon)) if canon else ""
            who = who or canon or "Unknown"

        if message.text is None:
            body = UNREADABLE
        else:
            body = _redacted(message.text) or NO_WORDS

        lines.append(f"{message.dt_local.strftime('%H:%M')}  {who}: {body}")

    return "\n".join(lines) + "\n" if lines else ""


def write_thread(
    unit: Any,
    contacts: Mapping[str, str] | None = None,
    threads_dir: Path | str | None = None,
) -> Path:
    """Write one chat-day's transcript atomically, owner-only. Returns its path.

    The order is the safety:

    1. **serialise first** — a body this module cannot render fails here, leaving
       no file, no temp file and not even an empty year folder;
    2. refuse any target not contained within the threads folder
       (:func:`_refuse_foreign_target`);
    3. write a temp file in the SAME directory, so ``os.replace`` is a rename
       within one filesystem and therefore atomic — a crash leaves either the old
       transcript whole or the new one whole, never half of either;
    4. ``0600`` on the temp file before the replace, and again on the final path,
       so a file left loose by an earlier tool is tightened rather than inherited.

    It is written **before** any stamp, so a link on a card always points at a
    file that already exists.
    """
    payload = render_thread(unit, contacts)
    target = thread_path(unit, contacts, threads_dir)
    resolved = _refuse_foreign_target(target, threads_dir)

    resolved.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temp_name = tempfile.mkstemp(
        dir=str(resolved.parent), prefix=f"{resolved.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        # newline="\n" so a Windows run writes the same bytes as a macOS one.
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        _chmod_600(temp_path)
        os.replace(temp_path, resolved)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise
    _chmod_600(resolved)
    return resolved


def thread_link(path: Path | str, threads_dir: Path | str | None = None) -> str:
    """The Brain-root-relative link a card carries: ``_local/imessage/threads/YYYY/<file>``.

    **Never a bare ``threads/…``.**  Every other link on the spine is
    vault-relative and resolved against ``glitch-mem/Memory/``, so a bare path
    would be read as ``glitch-mem/Memory/threads/…`` and lead nowhere on every
    card, forever.  The leading ``_local/`` is what makes it visibly not a vault
    path.

    Always forward slashes, on every OS: a backslash written into a card on
    Windows is a dead link on the Mac that reads it next.
    """
    target = Path(path)
    base = _threads_dir(threads_dir)
    try:
        relative = target.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        relative = Path(target.parent.name) / target.name
    return f"{THREAD_LINK_PREFIX}/{relative.as_posix()}"


# ---------------------------------------------------------------------------
# 8. The entry point.
# ---------------------------------------------------------------------------


def units_for(
    messages: Iterable[Any], cfg: Mapping[str, Any] | None = None
) -> tuple[dict[tuple[int, date], ChatDay], dict[tuple[str, date], PersonDay]]:
    """The whole pipeline: ``(included chat-days, person-days)``.

    Group, apply the substance floor to each conversation-day, fold what survives
    to person-days, then re-check the floor on each person-day — which at that
    point only catches ``never_ingest`` and a short code reaching the identifier,
    since a person-day inherits the turns of units that already passed.

    **Refuses** with :class:`OwnHandlesRequired` when ``own_handles`` is empty:
    without it the member's own outbound texts are filed as somebody else's
    turns, onto the member's own card.  The caller prints the exception's message
    (:data:`imconfig.REFUSAL_NO_OWN_HANDLES`) and exits 0.
    """
    if cfg is None:
        cfg = imconfig.load_config()
    own = owner_handles(cfg)

    chat_days = {
        key: unit
        for key, unit in group(messages, own_handles=own).items()
        if is_included(unit, cfg)
    }
    person_days = {
        key: unit
        for key, unit in fold_to_person_days(chat_days).items()
        if is_included(unit, cfg)
    }
    return chat_days, person_days
