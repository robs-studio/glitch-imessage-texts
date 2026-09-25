"""iMessage intake — ``review``: every held number, one ranked list, cleared on the member's word.

Why this exists
---------------
The daily run never puts a number on the member's main people queue (the member's
ruling, 2026-09-24): a number it cannot file is looked up DRY and its days are HELD in
the plug-in's own ledger.  On day one that can be hundreds of numbers, and a queue of
hundreds of cards is the flood THE WHY names as this feature's first failure: the
member stops reading the queue.  So they wait here instead, on one list, ranked by how much of the
member's life each one holds, and a whole batch is cleared in one sweep (decision 7):
accept the top, dismiss the tail.

The list, in order
------------------
Each section is ranked by conversation-days held, highest first (then the most
recent, then the name, so the order is stable), and numbered 1, 2, 3 … straight
through:

(a) **attach** — a person the member already added whose number is not on their card
    yet: "<name> was added; say yes again to attach their number so their texts keep
    landing."  The engine creates a person from a number WITHOUT the number (E1), so
    adding someone from a text takes two yeses (KNOWN LIMIT 14).  Hiding the second
    would leave the number re-asking forever with no one knowing why.
(b) **known** — a number whose Contacts name matches ONE card: add it to that card.
(c) **new** — named in Contacts, no card yet: a new person.
(d) **bare** — a number with no name: a new person only if the member names them
    (``--name ROW=NAME``), because a card needs a name.
(e) **not a number** — not a valid phone number (E.164) or address, such as a 16- or
    25-digit business ID.  The engine can never hold it, so it can only be dismissed.
(f) **ambiguous** — on more than one card, or its name matches several: pinned by name,
    never guessed, never accepted from here.

And, numbered apart (``m1``, ``m2`` …) so a "dismiss the tail" sweep can never reach
it: **check these matches** — every number the daily run filed onto a card through the
resolver's phone SUFFIX fallback (seven or more trailing digits, no country table,
E5b / KNOWN LIMIT 12) rather than an exact match.  It shows the card it went to, so a
wrong-person match is caught by the only one who can catch it.  ``--accept m1`` says
the match is right; ``--dismiss m1`` says it is someone else, which takes that
number's waiting days off that card's queue and holds its new ones.

Rows acted on are exactly the rows read
---------------------------------------
Every listing carries a short code, a fingerprint of the whole list as shown (each
row's number, section, name, day count, card and the engine card it would act on).
``--confirm`` refuses unless ``--listing <code>`` is given and the list rebuilt at that
moment, under the ledger lock, has the same code.  A list that changed in between (a
new text, a Contacts rename, a decision made on the people queue) is refused with one
sentence and nothing written, never silently shifted onto the wrong rows.  No file is
written to remember the list: the code is recomputed, so a plain ``review`` stays
read-only.

Acting, and the order of the commits
------------------------------------
Without ``--confirm`` a selection is a preview: what each row would do, in plain words,
and nothing written.  With it, each row is one small transaction:

* **accept (b), and the second yes (a)**: raise the ``add_identifier`` on this
  module's connection (``imspine.resolve(emit=True)``, or reuse the one already
  waiting) → COMMIT it → the ENGINE's own accept door (``people.py proposals
  accept``, its own connection: lint gate, snapshot, projection, commit) → look the
  number up again on this connection; once it RESOLVES, a ledger run releases every
  day held for it into the summary queue and records the identifier as accepted.
* **accept (c)/(d)**: raise the ``new_stub`` → COMMIT → the engine's accept door
  creates the card under the Contacts name (or the name given) → the ledger records
  ``attach_pending``, so the row comes back in (a).  **The number is NOT attached in
  the same act** and no attach card is left waiting on the people queue: the second
  yes is asked here, on this list, and raised and accepted in that act.  (An address
  is the one exception the engine makes: it attaches an email on the first yes, so
  that row resolves at once and its days are released.)
* **dismiss**: the engine's own dismiss door, on the card already waiting or on one
  raised for the purpose in the same act, so the "no" is on the engine's record,
  keyed on the number (``change_log``; ``imspine.dismissed_identifier``), and no
  drifted Contacts name can mint a sibling card for it (E4).  A number the engine
  cannot hold ((e), and a shared number, (f)) is dismissed in the ledger alone.

Why that order: this module's connection never holds an uncommitted write while an
engine door runs (the door would wait out ``busy_timeout`` and fail), and it reads
the door's result with no transaction open, so it sees exactly what the door
committed.  The ledger is written LAST for each row, after the engine has committed:
a kill between the two leaves the ledger one step behind the engine, which the next
``daily`` or ``review`` reads straight off the engine, never the reverse (a ledger that
claimed a yes the engine never recorded).  This connection projects nothing: every
card written in an act is written, projected and committed by the engine's own door,
so G1's "project, then commit" is the door's, on the door's connection.

The people queue ends every act with NOTHING new waiting from this plug-in:
everything raised is accepted or dismissed in the same act.  The one exception is the
engine refusing its own door (a name its gate will not take): that card is left on the
people queue with the engine's reason, and the act says so by row
(``left_on_people_queue``), never silently.

Privacy
-------
The plain list never prints a full number or address: the Contacts name and the last
four digits (``…0142``; an address shows its first letter and domain).
``--show-numbers`` prints them whole; ``--json`` follows the same rule.
"""

from __future__ import annotations

# imconfig FIRST, before any engine module: it puts `.claude/scripts` on sys.path and
# then re-asserts this folder ahead of it. See imconfig's docstring.
import imconfig

imconfig.ensure_engine_path()

import contextlib  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
from collections import Counter  # noqa: E402
from collections.abc import Iterator, Mapping, Sequence  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import date, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import imcontacts  # noqa: E402
import imledger  # noqa: E402
import imrun  # noqa: E402
import imspine  # noqa: E402
import imthreads  # noqa: E402

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

ATTACH = "attach"
KNOWN = "known"
NEW = "new"
BARE = "bare"
NOT_A_NUMBER = "not_a_number"
AMBIGUOUS = "ambiguous"
CHECK = "check"

#: The ranked list's sections, in the order they are shown and numbered.
ORDER: tuple[str, ...] = (ATTACH, KNOWN, NEW, BARE, NOT_A_NUMBER, AMBIGUOUS)

TITLES: dict[str, str] = {
    ATTACH: "Say yes again to attach their number",
    KNOWN: "Numbers that match someone you already have",
    NEW: "New people, named in your Contacts",
    BARE: "Numbers with no name",
    NOT_A_NUMBER: "Can't be added by number",
    AMBIGUOUS: "On more than one card: pin by name, never guessed",
    CHECK: "Check these matches",
}

#: How long an act waits for the ledger lock before saying "busy".
LOCK_TIMEOUT_S: float = imrun.LOCK_TIMEOUT_S

#: The longest name ``--name`` takes.  A card's name is one line a person reads.
MAX_NAME_CHARS: int = 100

#: The fingerprint's length in hex characters: enough that two different lists never
#: share a code by chance, short enough to type.
TOKEN_CHARS: int = 12

SENTENCE_BUSY: str = (
    "texts: another texts run is using the ledger right now, so I changed nothing; "
    "try the review again in a minute."
)
SENTENCE_STALE: str = (
    "The list changed since you read it (a new text, a Contacts name, or a decision made "
    "on your people queue), so I did nothing; run review again and use its new row numbers."
)
SENTENCE_NO_TOKEN: str = (
    "To act I need the list you read: add --listing <code> (the code is printed at the top "
    "of the list), so the rows I act on are exactly the rows you saw; nothing was done."
)
SENTENCE_DB_MISMATCH: str = (
    "texts review paused: the database this review was pointed at is not the one the "
    "engine's own doors write, so a card raised in one would be accepted in the other; "
    "nothing was changed."
)
SENTENCE_NOTHING_YET: str = (
    "no texts have been read into the ledger yet, so no number is waiting; the first "
    "daily run starts it"
)

_ROW_TOKEN = re.compile(r"^(m?)(\d+)(?:-(m?)(\d+))?$", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f  ‪-‮⁦-⁩]")


def _warn(message: str) -> None:
    """One diagnostic line on stderr.  Stdout belongs to the listing."""
    print(f"[imessage] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Privacy: what a number looks like on the page.
# ---------------------------------------------------------------------------


def tail(ident: str) -> str:
    """A number as the plain list shows it: its last four digits, ``…0142``.

    An address shows its first letter and its domain (``j…@example.com``).  Never the
    whole thing: the list is read on screens and pasted into chats.
    """
    text = str(ident or "")
    if "@" in text:
        local, _, domain = text.partition("@")
        return f"{local[:1]}…@{domain}"
    digits = re.sub(r"\D", "", text)
    return f"…{digits[-4:]}" if digits else "…"


def shown(ident: str, show_numbers: bool) -> str:
    """The number in full when the member asked for it, else :func:`tail`."""
    return str(ident) if show_numbers else tail(ident)


def _clean(value: Any, limit: int = 120) -> str:
    """One tidy line of a name, or ``""``."""
    return imthreads.sanitise_body(value)[:limit].strip() if isinstance(value, str) else ""


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count:,} {one if count == 1 else (many or one + 's')}"


def _verb(count: int, one: str, many: str) -> str:
    """The verb that agrees with ``count``: "1 day is", "2 days are"."""
    return one if count == 1 else many


#: What an ambiguous row's reason says when it is the NAME that matches several cards,
#: as opposed to the number itself sitting on several (a shared family line).
_NAME_MATCHES_SEVERAL = "its name matches several cards, and I never guess whose it is"


# ---------------------------------------------------------------------------
# The listing.
# ---------------------------------------------------------------------------


@dataclass
class Row:
    """One line on the review list.  ``ident`` is the canonical number or address.

    ``days`` is the conversation-days held for it (for a check row: the days filed or
    waiting on the card it reached).  ``person_id`` is the card an accept would attach
    the number to (a, b), or the card a check row's number went to.  ``proposal_id`` is
    an engine card ALREADY waiting for this number, which an act uses instead of raising
    a sibling.
    """

    section: str
    ident: str
    name: str | None
    days: int
    first: str | None
    last: str | None
    state: str | None
    label: str = ""
    person_id: str | None = None
    person_name: str | None = None
    candidates: list[tuple[str, str]] = field(default_factory=list)
    proposal_id: str | None = None
    proposal_name: str | None = None
    matched_value: str | None = None
    matched_digits: int = 0
    queued: int = 0
    stamped: int = 0
    can_accept: bool = True
    needs_name: bool = False
    accept_needs: str | None = None
    owner_card: bool = False

    def fingerprint(self) -> list[Any]:
        """Everything the member reads on this row, and everything an act would use."""
        return [
            self.label, self.section, self.ident, self.name, self.days, self.first,
            self.last, self.state, self.person_id, self.person_name,
            [list(c) for c in self.candidates], self.proposal_id, self.proposal_name,
            self.matched_value, self.matched_digits, self.queued, self.stamped,
            self.can_accept, self.needs_name, self.accept_needs, self.owner_card,
        ]


@dataclass
class Listing:
    """The ranked rows, the check rows, the code that pins them, and what is not listed."""

    rows: list[Row]
    checks: list[Row]
    token: str
    notes: dict[str, int]

    def by_label(self) -> dict[str, Row]:
        return {row.label: row for row in [*self.rows, *self.checks]}


def _ordinal(day: str | None) -> int:
    try:
        return date.fromisoformat(str(day)[:10]).toordinal() if day else 0
    except ValueError:
        return 0


def _names(conn: sqlite3.Connection, ids: Sequence[str]) -> list[tuple[str, str]]:
    return [(pid, imspine.person_name(conn, pid) or pid) for pid in ids]


def _attach_names(
    conn: sqlite3.Connection, person_id: str, record: Mapping[str, Any], name: str | None
) -> list[str]:
    """The names to raise the attach under: the card's own first, then the ledger's.

    Uncut: the resolver matches a name whole, and a trimmed one would match nobody.
    """
    names = [imspine.person_name(conn, person_id), record.get("name"), name]
    return [n for n in dict.fromkeys(n.strip() for n in names if isinstance(n, str)) if n]


#: What :func:`attach_name` returns when the number reaches the card with no name at all
#: (an address whose domain belongs to that one person, the resolver's step 3b).
NO_NAME = ""


def attach_name(
    conn: sqlite3.Connection, ident: str, person_id: str, names: Sequence[str | None]
) -> str | None:
    """The first name under which the resolver would attach ``ident`` to exactly ``person_id``.

    The engine's resolver raises an ``add_identifier`` for a phone only through a NAME
    that matches cards (step 3); it must match this one card and no other, or the
    attach could land on a namesake.  With no usable name, a nameless lookup is tried
    (an address can reach one card by its domain) and :data:`NO_NAME` comes back.
    ``None`` when nothing reaches that card alone.  Dry: nothing is written.
    """
    tried = [n for n in names if n] or [None]
    for candidate in tried:
        dry = imspine.resolve(conn, ident, candidate, emit=False)
        if (dry.status == "proposed" and dry.proposal_kind == "add_identifier"
                and list(dry.candidates) == [person_id]):
            return candidate or NO_NAME
    return None


def _pending_attach(conn: sqlite3.Connection, ident: str, person_id: str) -> str | None:
    """A PENDING ``add_identifier`` that attaches ``ident`` to exactly ``person_id``."""
    pending = imspine.pending_identifier(conn, ident)
    detail = imspine.proposal_detail(conn, pending) if pending else None
    if detail and detail["kind"] == "add_identifier" and detail["person_id"] == person_id:
        return pending
    return None


def _attach_person(conn: sqlite3.Connection, record: Mapping[str, Any]) -> str | None:
    """The person a number waits to be attached to (a), or ``None``.

    ``attach_pending`` names them itself.  A ``pending`` number whose ``new_stub`` the
    member accepted on the main queue names them through the engine's change log
    (``imspine.accepted_person``).  Either way the card must still exist.
    """
    state = record.get("state")
    person: str | None = None
    if state == "attach_pending":
        person = str(record.get("person_id") or "") or None
    elif state == "pending" and imspine.proposal_state(
        conn, record.get("proposal_id")
    ) == "accepted":
        person = imspine.accepted_person(conn, record.get("proposal_id"))
    if person and imspine.person_name(conn, person) is not None:
        return person
    return None


def _classify(
    conn: sqlite3.Connection,
    led: imledger.Ledger,
    ident: str,
    record: Mapping[str, Any],
    contacts: Mapping[str, str],
    owner_ids: frozenset[str],
    notes: Counter[str],
) -> Row | None:
    """Which section one held number belongs in — or ``None`` when it is not listed."""
    state = record.get("state")
    if state == "dismissed":
        notes["wrong_match" if record.get("hold_reason") == "wrong_match" else "dismissed"] += 1
        return None
    if state not in ("held", "ambiguous", "pending", "attach_pending"):
        return None
    held = led.held(ident)
    days = sorted({str(u.get("day")) for u in held if u.get("day")})
    name = _clean(contacts.get(ident)) or _clean(record.get("name")) or None
    base: dict[str, Any] = {
        "ident": ident, "name": name, "days": len(held),
        "first": days[0] if days else record.get("first_seen"),
        "last": days[-1] if days else record.get("last_seen"),
        "state": state,
    }
    if not imspine.valid_identifier(ident):
        return Row(NOT_A_NUMBER, **base, can_accept=False,
                   accept_needs="it is not a phone number or an address")

    dry = imspine.resolve(conn, ident, name, emit=False)
    if dry.status == "resolved" and dry.person_id:
        notes["owner" if str(dry.person_id) in owner_ids else "resolved"] += 1
        return None
    if imspine.dismissed_identifier(conn, ident) is not None:
        notes["dismissed"] += 1
        return None

    person = _attach_person(conn, record)
    if person is not None:
        pending = _pending_attach(conn, ident, person)
        can = pending is not None or attach_name(
            conn, ident, person, _attach_names(conn, person, record, name)) is not None
        return Row(ATTACH, **base, person_id=person,
                   person_name=imspine.person_name(conn, person), proposal_id=pending,
                   can_accept=can, owner_card=person in owner_ids,
                   accept_needs=None if can else (
                       "its card's name is now shared with someone else, or was changed, so "
                       "the number can't be attached by name"))

    if dry.status == "ambiguous":
        return Row(AMBIGUOUS, **base, candidates=_names(conn, sorted(dry.candidates)),
                   can_accept=False,
                   accept_needs="it is on more than one card, and I never guess whose it is")

    pending = imspine.pending_identifier(conn, ident)
    detail = imspine.proposal_detail(conn, pending) if pending else None
    if detail is not None:
        # A card for this number already waits on the people queue: that card is what an
        # act decides, never a sibling beside it.
        kind, target = detail["kind"], detail["person_id"]
        if kind == "new_stub":
            stub_name = _clean(detail["name"]) or None
            return Row(NEW if stub_name else BARE, **base, proposal_id=pending,
                       proposal_name=stub_name, needs_name=not stub_name,
                       can_accept=bool(stub_name),
                       accept_needs=None if stub_name else (
                           "its waiting card has no name, and the engine will not make a card "
                           "without one"))
        if kind == "add_identifier" and target:
            return Row(KNOWN, **base, person_id=target,
                       person_name=imspine.person_name(conn, target), proposal_id=pending,
                       owner_card=target in owner_ids)
        return Row(AMBIGUOUS, **base, proposal_id=pending,
                   candidates=_names(conn, detail["candidates"]), can_accept=False,
                   accept_needs=_NAME_MATCHES_SEVERAL)

    if dry.status == "proposed" and dry.proposal_kind == "add_identifier":
        if len(dry.candidates) == 1:
            target = str(dry.candidates[0])
            return Row(KNOWN, **base, person_id=target,
                       person_name=imspine.person_name(conn, target),
                       owner_card=target in owner_ids)
        return Row(AMBIGUOUS, **base, candidates=_names(conn, sorted(dry.candidates)),
                   can_accept=False,
                   accept_needs=_NAME_MATCHES_SEVERAL)
    if dry.status == "proposed" and dry.proposal_kind == "new_stub":
        if name:
            return Row(NEW, **base)
        return Row(BARE, **base, needs_name=True)
    return Row(NOT_A_NUMBER, **base, can_accept=False,
               accept_needs="the directory has nothing to add it by")


def _check_row(
    conn: sqlite3.Connection,
    ident: str,
    record: Mapping[str, Any],
    owner_ids: frozenset[str],
    filed: Counter[str],
    waiting: Counter[str],
) -> Row | None:
    """A number filed onto a card only through the phone SUFFIX fallback (E5b), or ``None``.

    The ledger's ``match`` (recorded by the daily run) says ``exact`` for most numbers,
    and those are skipped without a lookup; anything else is looked up again, because
    only the directory as it is NOW says where the next day would go.
    """
    if record.get("match") == "exact":
        return None
    dry = imspine.resolve(conn, ident, None, emit=False)
    if dry.status != "resolved" or not dry.person_id:
        return None
    person = str(dry.person_id)
    if person in owner_ids or record.get("match_confirmed") == person:
        return None
    route, card_value = imspine.match_route(conn, ident, person)
    if route != "suffix" or not card_value:
        return None
    return Row(
        CHECK, ident=ident, name=_clean(record.get("name")) or None,
        days=filed[ident] + waiting[ident], first=record.get("first_seen"),
        last=record.get("last_seen"), state=record.get("state"), person_id=person,
        person_name=imspine.person_name(conn, person), matched_value=card_value,
        matched_digits=imspine.suffix_digits(ident, card_value),
        queued=waiting[ident], stamped=filed[ident],
    )


def build_listing(
    conn: sqlite3.Connection,
    led: imledger.Ledger,
    *,
    contacts: Mapping[str, str],
    owner_ids: frozenset[str],
) -> Listing:
    """Every number the ledger holds, classified, ranked and numbered.  Writes nothing.

    Every lookup is DRY (``emit=False``).  The code is a fingerprint of every row as
    shown; :func:`review` refuses an act whose code no longer matches.
    """
    notes: Counter[str] = Counter()
    filed: Counter[str] = Counter()
    waiting: Counter[str] = Counter()
    for key in led.stamped:
        parsed = imthreads.parse_ledger_key(key)
        if parsed and led.is_stamped(key):
            filed[parsed[0]] += 1
    for record in led.queued().values():
        waiting[str(record.get("identifier") or "")] += 1

    rows: list[Row] = []
    checks: list[Row] = []
    for ident, record in sorted(led.all_identifiers().items()):
        if record.get("state") == "accepted":
            check = _check_row(conn, ident, record, owner_ids, filed, waiting)
            if check is not None:
                checks.append(check)
            continue
        row = _classify(conn, led, ident, record, contacts, owner_ids, notes)
        if row is not None:
            rows.append(row)

    rows.sort(key=lambda r: (ORDER.index(r.section), -r.days, -_ordinal(r.last),
                             (r.name or "￿").casefold(), r.ident))
    for index, row in enumerate(rows, start=1):
        row.label = str(index)
    checks.sort(key=lambda r: (-r.days, (r.person_name or "").casefold(), r.ident))
    for index, row in enumerate(checks, start=1):
        row.label = f"m{index}"
    material = json.dumps([r.fingerprint() for r in [*rows, *checks]], ensure_ascii=False,
                          sort_keys=True, default=str)
    token = hashlib.sha256(material.encode("utf-8")).hexdigest()[:TOKEN_CHARS]
    return Listing(rows=rows, checks=checks, token=token, notes=dict(notes))


# ---------------------------------------------------------------------------
# Choosing rows: the selection, checked in full before anything is done.
# ---------------------------------------------------------------------------


class Refusal(Exception):  # noqa: N818 (a plain answer, not a programming error)
    """One plain sentence: the selection cannot be done as asked, and nothing was."""


def parse_rows(spec: str | None, flag: str) -> list[str]:
    """``"1-20,25,m1"`` → ``["1", …, "20", "25", "m1"]`` (order kept, repeats dropped)."""
    if spec is None:
        return []
    labels: list[str] = []
    for raw in str(spec).split(","):
        part = raw.strip().replace(" ", "")
        if not part:
            continue
        found = _ROW_TOKEN.match(part)
        if not found:
            raise Refusal(f"I could not read {part!r} in {flag}: write rows like 1-20,25 "
                          "(or m1 for a match to check), so I did nothing.")
        first_prefix, low, second_prefix, high = found.groups()
        prefix = first_prefix.lower()
        start = int(low)
        if high is None:
            end = start
        else:
            if second_prefix and second_prefix.lower() != prefix:
                raise Refusal(f"{part!r} in {flag} mixes a row and a match; write them "
                              "apart, so I did nothing.")
            end = int(high)
        if start < 1 or end < start:
            raise Refusal(f"{part!r} in {flag} is not a range of rows, so I did nothing.")
        if end - start > 100_000:
            raise Refusal(f"{part!r} in {flag} is far longer than any list, so I did nothing.")
        labels.extend(f"{prefix}{number}" for number in range(start, end + 1))
    return list(dict.fromkeys(labels))


def parse_names(names: Mapping[str, str] | Sequence[str] | None) -> dict[str, str]:
    """``["6=Jane Doe"]`` (or ``{"6": "Jane Doe"}``) → ``{"6": "Jane Doe"}``, checked."""
    if not names:
        return {}
    pairs: list[tuple[str, str]] = []
    if isinstance(names, Mapping):
        pairs = [(str(k), str(v)) for k, v in names.items()]
    else:
        for item in names:
            label, sep, value = str(item).partition("=")
            if not sep:
                raise Refusal(f"--name takes ROW=NAME (for example 6=\"Jane Doe\"); "
                              f"{item!r} has no '=', so I did nothing.")
            pairs.append((label, value))
    out: dict[str, str] = {}
    for label, value in pairs:
        key = label.strip().lower()
        if not key.isdigit():
            raise Refusal(f"--name is for a numbered row; {label!r} is not one, so I did "
                          "nothing.")
        text = value.strip().strip('"').strip("'").strip()
        if not text or _CONTROL.search(text) or len(text) > MAX_NAME_CHARS:
            raise Refusal(f"The name for row {key} must be one line of at most "
                          f"{MAX_NAME_CHARS} characters, so I did nothing.")
        if text.startswith("-") or "(→" in text or "[mtg:" in text:
            raise Refusal(f"The name for row {key} starts or holds characters a card cannot "
                          "carry, so I did nothing.")
        out[str(int(key))] = text
    return out


@dataclass
class Step:
    """One row, one action, and the sentence that says what it will do."""

    row: Row
    action: str  # "accept" | "dismiss"
    name: str | None = None
    attach_to: str | None = None
    attach_to_name: str | None = None
    words: str = ""


def plan(
    conn: sqlite3.Connection,
    listing: Listing,
    *,
    accept: str | None,
    dismiss: str | None,
    dismiss_below: int | None,
    names: Mapping[str, str] | Sequence[str] | None,
    show_numbers: bool = False,
) -> list[Step]:
    """Turn the member's words into steps, or raise :class:`Refusal` with one sentence.

    Every check is made before anything is done, so an act does exactly what its
    preview said, row for row, or nothing at all.  Reads only (dry lookups).
    """
    labels = listing.by_label()
    accepted = parse_rows(accept, "--accept")
    dismissed = parse_rows(dismiss, "--dismiss")
    if dismiss_below is not None:
        numbered = len(listing.rows)
        if dismiss_below < 1 or dismiss_below > numbered:
            raise Refusal(f"There is no row {dismiss_below} on this list (it has "
                          f"{_plural(numbered, 'row')}), so I did nothing.")
        tail_rows = [row.label for row in listing.rows if int(row.label) > dismiss_below]
        if not tail_rows:
            raise Refusal(f"There is nothing below row {dismiss_below}, so I did nothing.")
        dismissed = list(dict.fromkeys([*dismissed, *tail_rows]))
    given = parse_names(names)

    for label in [*accepted, *dismissed]:
        if label not in labels:
            last = listing.rows[-1].label if listing.rows else None
            where = (f"it ends at row {last}" if last else "it has no numbered rows")
            if label.startswith("m"):
                where = (f"its matches end at {listing.checks[-1].label}" if listing.checks
                         else "it has no matches to check")
            raise Refusal(f"There is no row {label} on this list ({where}), so I did nothing.")
    both = [label for label in accepted if label in dismissed]
    if both:
        raise Refusal(f"Row {both[0]} is in both --accept and --dismiss, so I did nothing; "
                      "say one or the other.")
    # Done in list order (the numbered rows, then the matches), whatever order they were
    # typed in, so the preview and the act read the same top to bottom.
    in_order = {row.label: index for index, row in enumerate([*listing.rows, *listing.checks])}
    accepted.sort(key=in_order.__getitem__)
    dismissed.sort(key=in_order.__getitem__)
    for label in given:
        if label not in accepted:
            raise Refusal(f"--name is for a row you are adding, and row {label} is not in "
                          "--accept, so I did nothing.")
        if labels[label].section not in (NEW, BARE):
            raise Refusal(f"Row {label} already has a card to go to, so --name does not "
                          "apply to it; I did nothing.")
        if labels[label].proposal_id:
            raise Refusal(f"Row {label} already has a card waiting on your people queue under "
                          f"{labels[label].proposal_name or 'no name'}, so --name can't rename "
                          "it; I did nothing.")

    steps: list[Step] = []
    for label in accepted:
        row = labels[label]
        name = given.get(label)
        if not row.can_accept:
            raise Refusal(f"Row {label} can't be accepted: {row.accept_needs}. I did nothing; "
                          "dismiss it, or leave it out of --accept.")
        if row.needs_name and not name:
            raise Refusal(f"Row {label} has no name, and a new card needs one, so I did "
                          f"nothing; add --name {label}=\"Their Name\", or dismiss it.")
        step = Step(row, "accept", name=name or row.proposal_name or row.name)
        if row.section in (NEW, BARE) and name:
            # A name the member gave may already be someone's card: then the number goes
            # onto that card, never onto a second card of the same name.
            dry = imspine.resolve(conn, row.ident, name, emit=False)
            if dry.status == "proposed" and dry.proposal_kind == "add_identifier":
                if len(dry.candidates) != 1:
                    raise Refusal(f"More than one card is called {name}, so I can't tell which "
                                  f"one row {label} belongs to; I did nothing.")
                step.attach_to = str(dry.candidates[0])
                step.attach_to_name = imspine.person_name(conn, step.attach_to)
            elif not (dry.status == "proposed" and dry.proposal_kind == "new_stub"):
                raise Refusal(f"Row {label} can't be added under {name} (the directory "
                              f"answered {dry.status}), so I did nothing.")
        step.words = _step_words(step, show_numbers)
        steps.append(step)
    for label in dismissed:
        step = Step(labels[label], "dismiss")
        step.words = _step_words(step, show_numbers)
        steps.append(step)
    return steps


def _who(row: Row, show_numbers: bool) -> str:
    number = shown(row.ident, show_numbers)
    if row.name:
        return f"{row.name} ({number})"
    if row.section in (CHECK, NOT_A_NUMBER):
        return number
    return f"a number with no name ({number})"


def _step_words(step: Step, show_numbers: bool) -> str:
    """What one step will do, in the member's words.  Shown before, and it is what happens."""
    row = step.row
    held = _plural(row.days, "held day")
    go = _verb(row.days, "goes", "go")
    card = row.person_name or "that person"
    if step.action == "accept":
        if row.section == CHECK:
            return (f"{row.label}. {_who(row, show_numbers)} on {card}'s card: you confirm it "
                    "is them, and it leaves the matches to check.")
        if row.section == ATTACH:
            return (f"{row.label}. {card}: attach their number "
                    f"({shown(row.ident, show_numbers)}) to their card, your second yes; then "
                    f"their {held} {go} to the summary queue.")
        if row.section == KNOWN:
            own = " That is your own card, so its texts will never land on a card." \
                if row.owner_card else ""
            return (f"{row.label}. {_who(row, show_numbers)}: add the number to {card}'s card; "
                    f"then its {held} {go} to the summary queue.{own}")
        if step.attach_to:
            return (f"{row.label}. {shown(row.ident, show_numbers)} as {step.name}: you already "
                    f"have a card for {step.attach_to_name or step.name}, so the number goes "
                    f"onto that card instead of a second one; then its {held} {go} to the "
                    "summary queue.")
        if "@" in row.ident:
            return (f"{row.label}. {step.name} ({shown(row.ident, show_numbers)}): add them as a "
                    f"new person with this address; then their {held} {go} to the summary "
                    "queue. Their card will say they came from a meeting (an engine quirk).")
        return (f"{row.label}. {step.name} ({shown(row.ident, show_numbers)}): add them as a new "
                "person. The engine makes the card without the number, so they come back at "
                "the top of this list for your second yes, and their "
                f"{held} {_verb(row.days, 'waits', 'wait')} until then. Their card will say "
                "they came from a meeting (an engine quirk).")
    never = f"{held} {_verb(row.days, 'is', 'are')} never filed"
    if row.section == CHECK:
        kept = (f" {_plural(row.stamped, 'day')} already on the card "
                f"{_verb(row.stamped, 'stays', 'stay')} there; take "
                f"{_verb(row.stamped, 'it', 'them')} off by hand if you want "
                f"{_verb(row.stamped, 'it', 'them')} gone.") if row.stamped else ""
        return (f"{row.label}. {_who(row, show_numbers)} is not {card}: its "
                f"{_plural(row.queued, 'waiting day')} "
                f"{_verb(row.queued, 'comes', 'come')} off that card's queue and "
                f"{_verb(row.queued, 'is', 'are')} held, and its new texts stop filing "
                f"there.{kept}")
    if row.section in (NOT_A_NUMBER, AMBIGUOUS):
        return (f"{row.label}. {_who(row, show_numbers)}: dismiss; it leaves this list and its "
                f"{never}.")
    return (f"{row.label}. {_who(row, show_numbers)}: dismiss; your no is recorded on your "
            "people records against the number, so it never comes back under another name, "
            f"and its {never}.")


# ---------------------------------------------------------------------------
# Acting.
# ---------------------------------------------------------------------------


@dataclass
class _Actor:
    """Carries one confirmed act out, row by row, in the commit order the module
    docstring sets out."""

    conn: sqlite3.Connection
    led: imledger.Ledger
    owner_ids: frozenset[str]
    now: datetime
    show_numbers: bool
    run_prefix: str
    results: list[dict[str, Any]] = field(default_factory=list)
    left_pending: list[str] = field(default_factory=list)
    touched_cards: list[str] = field(default_factory=list)

    # -- plumbing ------------------------------------------------------------

    @contextlib.contextmanager
    def _ledger_run(self, label: str) -> Iterator[imledger.Ledger]:
        """One ledger run per row, committed only once the engine has committed."""
        with self.led.run(f"{self.run_prefix}-{label}") as led:
            yield led

    def _result(self, step: Step, outcome: str, words: str, **extra: Any) -> dict[str, Any]:
        result = {"label": step.row.label, "section": step.row.section,
                  "action": step.action, "outcome": outcome, "words": words, **extra}
        self.results.append(result)
        return result

    def _needs_you(self, step: Step, why: str) -> dict[str, Any]:
        return self._result(step, "needs_you", f"{step.row.label}: not done, {why}.")

    def _card_rel(self, person_id: str) -> str | None:
        card = imspine.person_path(self.conn, person_id)
        if card is None:
            return None
        if card.rel not in self.touched_cards:
            self.touched_cards.append(card.rel)
        return card.rel

    def _lint_words(self, person_id: str) -> str:
        rel = self._card_rel(person_id)
        if rel is None:
            return ""
        errors = imspine.lint_card(rel)
        return f" The card does not lint clean: {errors[0]}." if errors else ""

    # -- raising, on this connection -------------------------------------------

    def _raise_attach(self, ident: str, person_id: str, names: Sequence[str | None]) -> tuple[
        str | None, str | None
    ]:
        """The PENDING ``add_identifier`` putting ``ident`` on exactly ``person_id``'s card.

        ``(proposal_id, None)``, or ``(None, why not)``.  One already waiting is used,
        never siblinged; otherwise one is raised under the first name that reaches that
        card alone.  E3: an identical card from before comes back with its old status,
        and a "no" given before is never overridden from here.
        """
        waiting = _pending_attach(self.conn, ident, person_id)
        if waiting is not None:
            return waiting, None
        chosen = attach_name(self.conn, ident, person_id, names)
        if chosen is None:
            return None, ("its card's name is now shared or changed, so the number can't be "
                          "attached by name; attach it on the card by hand")
        res = imspine.resolve(self.conn, ident, chosen or None, emit=True)
        if res.status != "proposed" or not res.proposal_id:
            return None, "the directory did not raise the card to attach it"
        status = imspine.proposal_state(self.conn, res.proposal_id)
        if status == "pending":
            return res.proposal_id, None
        if status == "dismissed":
            return None, "you said no to adding this number to that card before"
        return None, ("the engine attached this number to that card once before and it still "
                      "does not match; look at the card by hand")

    # -- the steps -------------------------------------------------------------

    def run(self, steps: Sequence[Step]) -> None:
        for step in steps:
            if step.action == "dismiss":
                self._dismiss(step)
            elif step.row.section == CHECK:
                self._confirm_match(step)
            elif step.row.section == ATTACH:
                person = step.row.person_id or ""
                self._accept_onto(step, person, _attach_names(
                    self.conn, person, self.led.identifier(step.row.ident) or {}, step.row.name))
            elif step.row.section == KNOWN:
                self._accept_onto(step, step.row.person_id or "", [step.name])
            elif step.attach_to:
                self._accept_onto(step, step.attach_to, [step.name])
            else:
                self._accept_new(step)

    def _accept_onto(self, step: Step, person_id: str, names: Sequence[str | None]) -> None:
        """(a), (b), and a new name that is already a card: attach, then release."""
        row = step.row
        proposal_id = row.proposal_id if imspine.is_pending(self.conn, row.proposal_id) else None
        if proposal_id is not None:
            detail = imspine.proposal_detail(self.conn, proposal_id) or {}
            if detail.get("kind") != "add_identifier" or detail.get("person_id") != person_id:
                proposal_id = None
        why = None
        if proposal_id is None:
            proposal_id, why = self._raise_attach(row.ident, person_id, names)
        if proposal_id is None:
            self._needs_you(step, why or "the number could not be raised for that card")
            return
        self.conn.commit()  # the raised card is committed BEFORE the engine's door opens
        out = imspine.accept_proposal(proposal_id)
        if out.get("status") != "ok":
            with self._ledger_run(row.label) as led:
                led.set_identifier(row.ident, state="attach_pending" if row.section == ATTACH
                                   else "pending", proposal_id=proposal_id, person_id=person_id)
            self.left_pending.append(row.label)
            self._needs_you(step, "the engine would not attach it ("
                            f"{_clean(out.get('reason'), 200) or 'no reason given'}); it waits "
                            "on your people queue with that reason")
            return
        self._settle(step, person_id, proposal_id, attached=True)

    def _settle(self, step: Step, person_id: str, proposal_id: str | None, *,
                attached: bool) -> None:
        """The number now on the card: once it RESOLVES there, release its held days."""
        row = step.row
        after = imspine.resolve(self.conn, row.ident, None, emit=False)
        if after.status == "resolved" and str(after.person_id) == person_id:
            route, _value = imspine.match_route(self.conn, row.ident, person_id)
            with self._ledger_run(row.label) as led:
                released = imrun.release_held(led, row.ident, person_id, now=self.now,
                                              owner_ids=self.owner_ids)
                led.set_identifier(row.ident, state="accepted", person_id=person_id,
                                   proposal_id=proposal_id, match=route, hold_reason=None,
                                   reviewed_at=self.now.isoformat())
            card = imspine.person_name(self.conn, person_id) or "the card"
            if released.owner_dropped:
                tail_words = (f" Those {_plural(released.owner_dropped, 'day')} were your own "
                              "texts, which never land on a card.")
            else:
                tail_words = (f" {_plural(released.queued, 'day')} went to the summary queue"
                              + (f" ({released.already:,} were already there)"
                                 if released.already else "") + ".")
            done = (f"Number attached to {card}'s card." if attached else
                    f"Added {card} as a new person; the engine put the address on the card "
                    "with this one yes.")
            self._result(step, "done", f"{row.label}. {done}{tail_words}"
                         + self._lint_words(person_id),
                         person_id=person_id, released=released.queued)
            return
        with self._ledger_run(row.label) as led:
            led.set_identifier(row.ident, state="held", hold_reason="cannot_attach",
                               proposal_id=proposal_id, person_id=person_id)
        self._needs_you(step, "the engine said yes, but the number still does not reach that "
                              "card; look at the card by hand")

    def _accept_new(self, step: Step) -> None:
        """(c)/(d): the first yes creates the card; the second yes is asked on this list."""
        row = step.row
        name = step.name or ""
        proposal_id: str | None = None
        if row.proposal_id and imspine.is_pending(self.conn, row.proposal_id):
            proposal_id = row.proposal_id
        else:
            res = imspine.resolve(self.conn, row.ident, name, emit=True)
            if not (res.status == "proposed" and res.proposal_kind == "new_stub"
                    and res.proposal_id):
                self._needs_you(step, "the directory no longer sees a new person here; run "
                                      "review again")
                return
            status = imspine.proposal_state(self.conn, res.proposal_id)
            if status != "pending":
                self._needs_you(step, "you said no to this number before" if status ==
                                "dismissed" else "a card for it was made once already")
                return
            proposal_id = res.proposal_id
        self.conn.commit()  # committed BEFORE the engine's door opens its own connection
        out = imspine.accept_proposal(proposal_id)
        if out.get("status") != "ok":
            with self._ledger_run(row.label) as led:
                led.set_identifier(row.ident, state="pending", proposal_id=proposal_id,
                                   kind="new_stub")
            self.left_pending.append(row.label)
            self._needs_you(step, "the engine would not make the card ("
                            f"{_clean(out.get('reason'), 200) or 'no reason given'}); it waits "
                            "on your people queue with that reason")
            return
        person_id = str(out.get("id") or "") or imspine.accepted_person(self.conn, proposal_id)
        if not person_id:
            self._needs_you(step, "the engine made the card but did not say which one; run "
                                  "review again")
            return
        after = imspine.resolve(self.conn, row.ident, None, emit=False)
        if after.status == "resolved" and str(after.person_id) == person_id:
            # An address: the engine attached it on the first yes.
            self._settle(step, person_id, proposal_id, attached=False)
            return
        with self._ledger_run(row.label) as led:
            led.set_identifier(row.ident, state="attach_pending", person_id=person_id,
                               stub_proposal_id=proposal_id, proposal_id=None, name=name,
                               hold_reason=None, reviewed_at=self.now.isoformat())
        self._result(step, "done", f"{row.label}. Added {name} as a new person. Their number is "
                     "not on the card yet: say yes again when they come back at the top of the "
                     "review list, and their held days follow." + self._lint_words(person_id),
                     person_id=person_id, released=0)

    def _dismiss(self, step: Step) -> None:
        """A "no": on the engine's own record where the engine can hold the number."""
        row = step.row
        if row.section == CHECK:
            self._wrong_match(step)
            return
        recorded: str | None = None
        if row.section in (ATTACH, KNOWN, NEW, BARE) and imspine.valid_identifier(row.ident):
            pending = imspine.pending_identifier(self.conn, row.ident)
            if pending is None:
                raise_name = (imspine.person_name(self.conn, row.person_id)
                              if row.section == ATTACH else None) or row.proposal_name or row.name
                res = imspine.resolve(self.conn, row.ident, raise_name, emit=True)
                if res.status == "proposed" and res.proposal_id:
                    status = imspine.proposal_state(self.conn, res.proposal_id)
                    if status == "pending":
                        pending = res.proposal_id
                    elif status == "dismissed":
                        recorded = res.proposal_id
            if pending is not None:
                self.conn.commit()  # committed BEFORE the engine's door opens its own
                out = imspine.dismiss_proposal(pending)
                if out.get("status") != "ok":
                    self.left_pending.append(row.label)
                    self._needs_you(step, "the engine would not record the no ("
                                    f"{_clean(out.get('reason'), 200) or 'no reason given'})")
                    return
                recorded = pending
        with self._ledger_run(row.label) as led:
            led.set_identifier(row.ident, state="dismissed", proposal_id=recorded,
                               hold_reason=None, dismissed_in="review",
                               dismissed_at=self.now.isoformat())
        where = " (recorded on your people records too)" if recorded else ""
        self._result(step, "done", f"{row.label}. Dismissed{where}; its held days are never "
                     "filed.")

    def _wrong_match(self, step: Step) -> None:
        """A check row the member says is someone else: stop filing it on that card."""
        row = step.row
        person = row.person_id or ""
        moved = 0
        with self._ledger_run(row.label) as led:
            for key, record in sorted(led.queued().items()):
                if record.get("identifier") != row.ident or led.is_stamped(key):
                    continue
                led.dequeue(key)
                unit = {k: v for k, v in record.items() if k not in ("person_id", "queued_at")}
                unit.update(reason="wrong_match", held_at=self.now.isoformat())
                led.hold(row.ident, unit)
                moved += 1
            led.set_identifier(row.ident, state="dismissed", hold_reason="wrong_match",
                               match_person=person, proposal_id=None, dismissed_in="review",
                               dismissed_at=self.now.isoformat())
        card = row.person_name or "that card"
        kept = (f" {_plural(row.stamped, 'day')} already on {card}'s card "
                f"{_verb(row.stamped, 'stays', 'stay')} there; take "
                f"{_verb(row.stamped, 'it', 'them')} off by hand if you want "
                f"{_verb(row.stamped, 'it', 'them')} gone.") if row.stamped else ""
        self._result(step, "done", f"{row.label}. Not {card}: {_plural(moved, 'waiting day')} "
                     f"came off that card's queue and {_verb(moved, 'is', 'are')} held, and new "
                     f"texts stop filing there.{kept}", held=moved)

    def _confirm_match(self, step: Step) -> None:
        row = step.row
        with self._ledger_run(row.label) as led:
            led.set_identifier(row.ident, match_confirmed=row.person_id,
                               match_confirmed_at=self.now.isoformat())
        self._result(step, "done", f"{row.label}. Confirmed: it is {row.person_name or 'them'}.")


# ---------------------------------------------------------------------------
# review(): the one entry point.
# ---------------------------------------------------------------------------


def _row_data(row: Row) -> dict[str, Any]:
    return {
        "label": row.label, "section": row.section, "number": row.ident, "name": row.name,
        "days": row.days, "first_day": row.first, "last_day": row.last, "state": row.state,
        "card": row.person_name, "card_id": row.person_id,
        "candidates": [{"card": name, "card_id": pid} for pid, name in row.candidates],
        "waiting_on_people_queue": row.proposal_id, "waiting_as": row.proposal_name,
        "matched_card_number": row.matched_value, "matched_digits": row.matched_digits,
        "days_queued": row.queued, "days_filed": row.stamped,
        "can_accept": row.can_accept, "needs_name": row.needs_name,
        "cannot_accept_because": row.accept_needs, "your_own_card": row.owner_card,
    }


def _new_report(mode: str) -> dict[str, Any]:
    return {"verb": "review", "mode": mode, "paused": None, "refused": None,
            "nothing": None, "listing": None, "plan": [], "results": [],
            "left_on_people_queue": [], "confirmed": False}


def _pause(report: dict[str, Any], reason: str, sentence: str) -> dict[str, Any]:
    report["paused"] = {"reason": reason, "sentence": sentence}
    return report


def _same_file(a: Path | str, b: Path | str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


def review(
    *,
    accept: str | None = None,
    dismiss: str | None = None,
    dismiss_below: int | None = None,
    names: Mapping[str, str] | Sequence[str] | None = None,
    listing: str | None = None,
    confirm: bool = False,
    show_numbers: bool = False,
    home: Path | str | None = None,
    db_path: Path | str | None = None,
    cfg: Mapping[str, Any] | None = None,
    contacts: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The review list, a preview of a selection, or (``confirm``) the act itself.

    Returns plain data (``report["mode"]`` is ``list`` | ``preview`` | ``act``).
    ``paused`` is a gate (own handles, engine drift, the lock, a damaged ledger, a
    database mismatch); ``refused`` is a selection that cannot be done as asked (a
    stale or missing listing code, a row that is not there, an accept a row cannot
    take).  Either way, nothing was written.  Every keyword beyond the member's words
    exists so a test points this at temp places: ``home`` (ledger), ``db_path`` (the
    people database; it must be the engine's own), ``cfg``, ``contacts``, ``now``.
    """
    selecting = bool(accept or dismiss or dismiss_below is not None or names)
    report = _new_report("act" if confirm else ("preview" if selecting else "list"))
    if confirm and not selecting:
        report["refused"] = ("--confirm acts on rows, and none were named; say --accept or "
                             "--dismiss with the rows, so nothing was done.")
        return report
    cfg = dict(cfg) if cfg is not None else imconfig.load_config()
    home_dir = Path(home) if home is not None else Path(imconfig.HOME)
    moment = now if now is not None else imrun._engine_now()

    if not imrun._ledger_exists(home_dir):
        report["nothing"] = SENTENCE_NOTHING_YET
        return report
    try:
        own = imthreads.owner_handles(cfg)
    except imthreads.OwnHandlesRequired as exc:
        return _pause(report, "no_own_handles", str(exc))
    drift = imspine.check_review_contract()
    if drift:
        return _pause(report, "engine_changed", drift)
    if db_path is not None and not _same_file(db_path, imspine.engine_db_path()):
        return _pause(report, "database_mismatch", SENTENCE_DB_MISMATCH)

    view: contextlib.AbstractContextManager[imledger.Ledger]
    if confirm:
        view = imledger.session(
            lambda: imspine.file_lock(imledger.lock_target(home_dir), timeout=LOCK_TIMEOUT_S),
            home=home_dir,
        )
    else:
        view = imrun._read_view(home_dir)
    with contextlib.ExitStack() as stack:
        try:
            led = stack.enter_context(view)
        except TimeoutError:
            return _pause(report, "busy", SENTENCE_BUSY)
        except imledger.LedgerCorrupt as exc:
            return _pause(report, "ledger_damaged", f"texts paused: {exc}")
        conn = imspine.open_conn(db_path)
        if not confirm:
            imrun._no_checkpoint_on_close(conn)
        try:
            drift = imspine.check_review_contract(conn)
            if drift:
                return _pause(report, "engine_changed", drift)
            contact_map = dict(contacts) if contacts is not None else imcontacts.load_map()
            owner_ids = imrun.owner_person_ids(conn, own)
            built = build_listing(conn, led, contacts=contact_map, owner_ids=owner_ids)
            report["listing"] = {
                "code": built.token,
                "rows": [_row_data(r) for r in built.rows],
                "checks": [_row_data(r) for r in built.checks],
                "not_listed": dict(sorted(built.notes.items())),
            }
            if listing is not None and listing.strip().lower() != built.token:
                report["refused"] = SENTENCE_STALE
                return report
            if not selecting:
                return report
            try:
                steps = plan(conn, built, accept=accept, dismiss=dismiss,
                             dismiss_below=dismiss_below, names=names,
                             show_numbers=show_numbers)
            except Refusal as exc:
                report["refused"] = str(exc)
                return report
            report["plan"] = [{"label": s.row.label, "section": s.row.section,
                               "action": s.action, "words": s.words} for s in steps]
            if not confirm:
                return report
            if listing is None:
                report["refused"] = SENTENCE_NO_TOKEN
                return report
            actor = _Actor(conn=conn, led=led, owner_ids=owner_ids, now=moment,
                           show_numbers=show_numbers,
                           run_prefix=f"review-{moment:%Y%m%dT%H%M%S}-{secrets.token_hex(3)}")
            try:
                actor.run(steps)
            finally:
                if led.run_id is None:
                    try:
                        led.compact()
                    except Exception as exc:  # noqa: BLE001 - the log replays; never mask
                        _warn(f"the ledger could not be compacted ({type(exc).__name__}); "
                              "its log replays on the next load, so nothing is lost.")
                report["results"] = actor.results
                report["left_on_people_queue"] = actor.left_pending
                report["cards_touched"] = actor.touched_cards
            report["confirmed"] = True
            return report
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()  # an act committed each row already; a list wrote nothing
            conn.close()
    return report  # pragma: no cover - the with block always returns


# ---------------------------------------------------------------------------
# Words.
# ---------------------------------------------------------------------------


def _row_line(row: Row, show_numbers: bool) -> str:
    """One row as the member reads it: the name, the last four digits, the days."""
    number = shown(row.ident, show_numbers)
    held = _plural(row.days, "day") + " held"
    if row.section == ATTACH:
        who = row.person_name or row.name or "They"
        line = (f"{who} was added; say yes again to attach their number so their texts keep "
                f"landing. ({number}, {held})")
        if not row.can_accept:
            line += f" Can't be done from here: {row.accept_needs}."
        return line
    if row.section == KNOWN:
        who = f"{row.name} ({number})" if row.name else number
        own = " That is your own card." if row.owner_card else ""
        waiting = " A card for it already waits on your people queue." if row.proposal_id else ""
        return (f"{who} matches your card for {row.person_name or 'someone you have'}: add the "
                f"number to it. {held}.{own}{waiting}")
    if row.section == NEW:
        waiting = (f" Already waiting on your people queue as {row.proposal_name}."
                   if row.proposal_id else "")
        return f"{row.name or row.proposal_name} ({number}): a new person. {held}.{waiting}"
    if row.section == BARE:
        return (f"A number with no name ({number}): a new person if you name them "
                f"(--name {row.label}=\"Their Name\"), or dismiss it. {held}.")
    if row.section == NOT_A_NUMBER:
        who = f"{row.name} ({number})" if row.name else number
        return f"{who} can't be added by number ({row.accept_needs}): dismiss it. {held}."
    if row.section == AMBIGUOUS:
        who = f"{row.name} ({number})" if row.name else number
        cards = ", ".join(name for _pid, name in row.candidates) or "several cards"
        if row.accept_needs == _NAME_MATCHES_SEVERAL:
            return (f"{who}: its name matches more than one card ({cards}); pin it by name, "
                    f"never guessed. {held}.")
        return (f"{who} is on more than one card ({cards}): pin it by name, never guessed. "
                f"{held}.")
    return (f"{number} went to {row.person_name or 'a card'}'s card, but only its last "
            f"{row.matched_digits} digits match the number there, so it may be someone else. "
            f"{_plural(row.days, 'day')} filed or waiting there.")


def render(report: Mapping[str, Any], show_numbers: bool = False) -> str:
    """The review in plain words.  Never a full number unless ``show_numbers``."""
    lines: list[str] = []
    add = lines.append
    paused = report.get("paused")
    if paused:
        add(paused["sentence"])
        return "\n".join(lines)
    if report.get("nothing"):
        add(f"Nothing to review yet: {report['nothing']}.")
        return "\n".join(lines)
    data = report.get("listing") or {}
    if report.get("refused"):
        add(str(report["refused"]))
        return "\n".join(lines)
    if report.get("mode") == "act":
        return _render_act(report, data)
    if report.get("mode") == "preview":
        add(f"A preview for list {data.get('code')}: nothing has been written.")
        for step in report.get("plan") or []:
            add(f"  {step['words']}")
        add(f"To do exactly this, run it again with --listing {data.get('code')} --confirm.")
        return "\n".join(lines)
    return _render_list(data, show_numbers)


def _render_list(data: Mapping[str, Any], show_numbers: bool) -> str:
    lines: list[str] = []
    add = lines.append
    rows = [_row_from_data(r) for r in data.get("rows") or []]
    checks = [_row_from_data(r) for r in data.get("checks") or []]
    notes = data.get("not_listed") or {}
    if not rows and not checks:
        add("Nothing to review: every number I hold is decided.")
    else:
        add("Your texts review list. Nothing changes until you say so.")
        add(f"List {data.get('code')} (quote it when you act: --listing {data.get('code')}).")
        for section in ORDER:
            members = [r for r in rows if r.section == section]
            if not members:
                continue
            add("")
            add(f"{TITLES[section]} ({len(members)}):")
            for row in members:
                add(f"  {row.label}. {_row_line(row, show_numbers)}")
        if checks:
            add("")
            add(f"{TITLES[CHECK]} ({len(checks)}): each number reached a card by its last "
                "digits only, so it may be the wrong person.")
            for row in checks:
                add(f"  {row.label}. {_row_line(row, show_numbers)}")
    extra = []
    if notes.get("resolved"):
        extra.append(f"{_plural(notes['resolved'], 'number')} now match a card, so the next "
                     "daily run files their days")
    if notes.get("dismissed"):
        extra.append(f"{_plural(notes['dismissed'], 'number')} you said no to")
    if notes.get("wrong_match"):
        extra.append(f"{_plural(notes['wrong_match'], 'number')} you said reached the wrong "
                     "card, held until each reaches the right one")
    if notes.get("owner"):
        extra.append(f"{_plural(notes['owner'], 'number')} on your own card")
    if extra:
        add("")
        add("Not on the list: " + "; ".join(extra) + ".")
    if rows or checks:
        add("")
        add(f"To act: review --listing {data.get('code')} --accept <rows> --dismiss <rows>, "
            "rows written like 1-3,5 (or --dismiss-below N for every row after N). Without "
            "--confirm it only shows what would happen. A match to check (m1 …): --accept "
            "says it is right, --dismiss says it is someone else.")
        add("Numbers show as their last four digits; --show-numbers prints them whole.")
    return "\n".join(lines)


def _row_from_data(item: Mapping[str, Any]) -> Row:
    """The render works from the report's plain data, so it renders a --json report too."""
    return Row(
        section=str(item.get("section")), ident=str(item.get("number") or ""),
        name=item.get("name"), days=int(item.get("days") or 0), first=item.get("first_day"),
        last=item.get("last_day"), state=item.get("state"), label=str(item.get("label")),
        person_id=item.get("card_id"), person_name=item.get("card"),
        candidates=[(str(c.get("card_id")), str(c.get("card"))) for c in
                    item.get("candidates") or []],
        proposal_id=item.get("waiting_on_people_queue"), proposal_name=item.get("waiting_as"),
        matched_value=item.get("matched_card_number"),
        matched_digits=int(item.get("matched_digits") or 0),
        queued=int(item.get("days_queued") or 0), stamped=int(item.get("days_filed") or 0),
        can_accept=bool(item.get("can_accept")), needs_name=bool(item.get("needs_name")),
        accept_needs=item.get("cannot_accept_because"),
        owner_card=bool(item.get("your_own_card")),
    )


def _render_act(report: Mapping[str, Any], data: Mapping[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"Done, for list {data.get('code')}:")
    for result in report.get("results") or []:
        add(f"  {result['words']}")
    left = report.get("left_on_people_queue") or []
    if left:
        add(f"Waiting on your people queue now, with the engine's reason: row(s) "
            f"{', '.join(left)}.")
    else:
        add("Nothing new is waiting on your people queue from this.")
    add("Run review again for the updated list; its row numbers have changed.")
    return "\n".join(lines)


def public(report: Mapping[str, Any], show_numbers: bool = False) -> dict[str, Any]:
    """The report for ``--json``: numbers masked to their last four unless asked for."""
    out = json.loads(json.dumps(report, default=str))
    if show_numbers:
        return out
    data = out.get("listing") or {}
    for item in [*(data.get("rows") or []), *(data.get("checks") or [])]:
        if item.get("number"):
            item["number"] = tail(str(item["number"]))
        if item.get("matched_card_number"):
            item["matched_card_number"] = tail(str(item["matched_card_number"]))
    return out
