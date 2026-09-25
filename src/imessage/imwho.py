"""iMessage intake — who the member means: their words for someone, to that person and numbers.

Why this module exists
----------------------
``show``, ``exclude`` and ``include`` all start from the member's own words: "where did we
leave off with Nina", "don't read texts from 202-555-0123", "include Samuel again".  Each
needs the same answer: which person that is, and every number and address of theirs that
texts arrive from.  One resolver answers it for all three, so "Nina" cannot mean one
person to ``show`` and another to ``exclude``.

The roads, in order
-------------------
1. **A card id** (``prs_…``, what ``/recall`` hands over) → that card, through
   :func:`imspine.person_path`.  Works for anyone in the directory.
2. **A number or an address** (anything with an ``@``, or seven or more digits and no
   letters) → that identifier, canonicalised, and the person it reaches: the texts
   ledger's own record first, then a DRY resolve (``emit=False``, nothing raised).
3. **A name** → matched, word for word, against every person the texts ledger has filed,
   queued or resolved (their directory name through :func:`imspine.person_name` and the
   ``name:`` line of their card) and every Contacts entry.  A Contacts entry reaches a
   card when its number resolves to one (dry); otherwise it stands as a Contacts-only
   person, merged into a card of exactly the same name.  Three tiers, best first:
   the same words (``exact``, any order), every word the member said is one of the name's
   (``words``: "Nina" finds "Nina Castell"), every word the member said starts one of the
   name's (``prefix``, two letters or more).  Only the best tier any candidate reached
   counts.  **One person** there is the answer.  **Several** is ``ambiguous``: the
   candidates come back and the caller stops, because guessing whose texts to show, or
   worse to stop reading, is the one thing this must never do.

A person in the directory who has never texted and is not in Contacts cannot be found by
name here (the plug-in never reads the directory beyond what ``imspine`` offers); the
member's own words then go through ``/recall`` first and its card id comes back through
road 1.  The ``none`` answer says so.

What it never does
------------------
It writes nothing and raises nothing on the people queue: every resolve is dry.  It
imports no engine module (``imspine`` is the plug-in's one engine importer, G7).  The
card's identifiers are read off the card's own frontmatter the way ``imsynth`` reads its
``name:`` line, one plain scan, no YAML parser.
"""

from __future__ import annotations

# imconfig FIRST, before any engine module: it puts `.claude/scripts` on sys.path and
# then re-asserts this folder ahead of it. See imconfig's docstring.
import imconfig

imconfig.ensure_engine_path()

import json  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
import unicodedata  # noqa: E402
from collections.abc import Iterable, Mapping  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import imcontacts  # noqa: E402
import imledger  # noqa: E402
import imspine  # noqa: E402
import imthreads  # noqa: E402

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: How well the member's words matched a name.  Only the best tier reached counts.
EXACT: int = 3
WORDS: int = 2
PREFIX: int = 1
TIER_NAMES: dict[int, str] = {EXACT: "exact", WORDS: "words", PREFIX: "prefix"}

#: The shortest word that may match as the start of a name word ("Sa" is not "Samuel").
PREFIX_MIN_CHARS: int = 2

#: A card id as the engine mints it: ``prs_`` and eight of ``[a-z2-7]``.
_CARD_ID = re.compile(r"^prs_[a-z2-7]{8}$")

_TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")
_VALUE = re.compile(r"""value\s*:\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|([^,}\s][^,}]*))""")
_LETTERS = re.compile(r"[^\W\d_]")


def words(text: str | None) -> tuple[str, ...]:
    """``text`` as lower-case word tokens: case, accents' composed forms and punctuation
    folded away, so "Nina Castell", "nina-castell" and "CASTELL, Nina" compare alike."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return tuple(re.findall(r"\w+", folded))


def tier(query: tuple[str, ...], name: str | None) -> int:
    """How well ``query`` (from :func:`words`) matches ``name``: 3, 2, 1, or 0 for no match."""
    have = words(name)
    if not query or not have:
        return 0
    if sorted(query) == sorted(have):
        return EXACT
    if all(word in have for word in query):
        return WORDS
    if all(len(word) >= PREFIX_MIN_CHARS and any(h.startswith(word) for h in have)
           for word in query):
        return PREFIX
    return 0


def looks_like_identifier(text: str) -> bool:
    """True when the member typed a number or an address rather than a name."""
    if "@" in text:
        return True
    digits = sum(ch.isdigit() for ch in text)
    return digits >= imthreads.SHORTCODE_MIN_DIGITS and not _LETTERS.search(text)


# ---------------------------------------------------------------------------
# The card, read the plain way.
# ---------------------------------------------------------------------------


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            loaded = json.loads(value)
        except ValueError:
            return value[1:-1]
        return loaded if isinstance(loaded, str) else value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def _values(fragment: str) -> list[str]:
    """Every identifier value on one frontmatter line of a ``phones``/``emails`` list.

    Three shapes, all of which the engine or a member's editor writes: the engine's flow
    mapping ``- { value: "+1…", type: … }``, a block mapping's ``- value: …`` line, and a
    bare scalar item ``- "+1…"``.  A flow list on the key's own line is split the same way.
    """
    found = [a or b or c for a, b, c in _VALUE.findall(fragment)]
    if found:
        return [_unquote(v) if v.startswith(('"', "'")) else v.strip() for v in found]
    text = fragment.strip()
    if text.startswith("[") and text.endswith("]"):
        return [_unquote(part) for part in text[1:-1].split(",") if part.strip()]
    if text.startswith("- "):
        item = text[2:].strip()
        if item and not item.startswith(("{", "[")) and ":" not in item.split("@")[0]:
            return [_unquote(item)]
    return []


def card_facts(path: Path | str | None) -> tuple[str, list[str]]:
    """``(the card's name line, every phone and address on it, canonicalised)``.

    Read straight off the card's frontmatter, the way ``imsynth`` reads a card's name:
    the card is the source of truth, and this module may not import the engine's parser.
    An unreadable card answers ``("", [])``; the caller then has the ledger and Contacts.
    """
    if path is None:
        return "", []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "", []
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return "", []
    name, raw_values, current = "", [], None
    for raw in lines[1:]:
        if raw.strip() == "---":
            break
        top = raw[:1] and not raw[0].isspace() and not raw.startswith("-")
        if top:
            current = None
            match = _TOP_KEY.match(raw)
            if not match:
                continue
            key, rest = match.group(1), match.group(2).strip()
            if key == "name":
                name = imthreads.sanitise_body(_unquote(rest))[:120]
            elif key in ("phones", "emails"):
                current = key
                if rest:
                    raw_values.extend(_values(rest))
            continue
        if current is not None:
            raw_values.extend(_values(raw))
    found: list[str] = []
    for value in raw_values:
        canonical = imcontacts.canonicalise(value)
        if canonical and canonical not in found:
            found.append(canonical)
    return name, found


# ---------------------------------------------------------------------------
# The answer.
# ---------------------------------------------------------------------------


@dataclass
class Who:
    """One person the member may mean, with every number and address of theirs.

    ``person_id`` and ``card`` are ``None`` for someone known only to Contacts or only
    by a number.  ``asked`` is the identifier the member typed, when they typed one:
    ``exclude`` then acts on exactly that number, never on every number of the card it
    happens to reach.
    """

    name: str
    person_id: str | None = None
    card: Path | None = None
    card_rel: str | None = None
    card_name: str = ""
    card_identifiers: list[str] = field(default_factory=list)
    ledger_identifiers: list[str] = field(default_factory=list)
    contacts_identifiers: list[str] = field(default_factory=list)
    asked: str | None = None
    route: str = ""
    tier: int = 0

    @property
    def identifiers(self) -> list[str]:
        """Every identifier, the asked one first, then card, ledger and Contacts."""
        out: list[str] = []
        for ident in (self.asked, *self.card_identifiers, *self.ledger_identifiers,
                      *self.contacts_identifiers):
            if ident and ident not in out:
                out.append(ident)
        return out


@dataclass
class Found:
    """``status`` is ``one`` (``who`` set), ``ambiguous`` (``candidates``) or ``none``."""

    status: str
    asked: str
    who: Who | None = None
    candidates: list[Who] = field(default_factory=list)
    why: str = ""


def mask(ident: str) -> str:
    """A number as a list shows it: ``…0142``; an address as ``j…@example.com``."""
    text = str(ident or "")
    if "@" in text:
        local, _, domain = text.partition("@")
        return f"{local[:1]}…@{domain}"
    digits = re.sub(r"\D", "", text)
    return f"…{digits[-4:]}" if digits else "…"


def who_data(who: Who | None, show_numbers: bool = False) -> dict[str, Any] | None:
    """A person as plain data for ``--json``; numbers masked unless asked for in full."""
    if who is None:
        return None
    shown = (lambda i: i) if show_numbers else mask
    return {
        "name": who.name,
        "person_id": who.person_id,
        "card": who.card_rel,
        "identifiers": [shown(i) for i in who.identifiers],
        "route": who.route,
    }


def found_data(found: Found, show_numbers: bool = False) -> dict[str, Any]:
    return {
        "status": found.status,
        "asked": found.asked,
        "who": who_data(found.who, show_numbers),
        "candidates": [who_data(c, show_numbers) for c in found.candidates],
        "why": found.why or None,
    }


# ---------------------------------------------------------------------------
# The ledger's people.
# ---------------------------------------------------------------------------


def ledger_people(led: imledger.Ledger) -> dict[str, set[str]]:
    """``{person_id: {identifier, …}}`` for everyone the texts ledger has tied to a card.

    From the identifier records (a number that resolved), the queue (a day waiting for
    its summary) and the stamped map (a day on a card).  Held days carry no person.
    """
    out: dict[str, set[str]] = {}

    def add(person_id: Any, ident: Any) -> None:
        if isinstance(person_id, str) and person_id and isinstance(ident, str) and ident:
            out.setdefault(person_id, set()).add(ident)

    for ident, record in led.all_identifiers().items():
        add(record.get("person_id"), ident)
    for record in led.queued().values():
        add(record.get("person_id"), record.get("identifier"))
    for key, record in led.stamped.items():
        parsed = imthreads.parse_ledger_key(key)
        if parsed and isinstance(record, Mapping):
            add(record.get("person_id"), parsed[0])
    return out


# ---------------------------------------------------------------------------
# find()
# ---------------------------------------------------------------------------


def _person(
    conn: sqlite3.Connection,
    person_id: str,
    people: Mapping[str, set[str]],
    *,
    route: str,
    contacts_identifiers: Iterable[str] = (),
) -> Who | None:
    card = imspine.person_path(conn, person_id)
    if card is None:
        return None
    card_name, card_ids = card_facts(card.path)
    name = imspine.person_name(conn, person_id) or card_name or card.path.stem
    return Who(
        name=name, person_id=person_id, card=card.path, card_rel=card.rel,
        card_name=card_name, card_identifiers=card_ids,
        ledger_identifiers=sorted(people.get(person_id, set())),
        contacts_identifiers=sorted(set(contacts_identifiers)),
        route=route,
    )


def _by_identifier(
    conn: sqlite3.Connection,
    led: imledger.Ledger,
    asked: str,
    people: Mapping[str, set[str]],
    contacts: Mapping[str, str],
) -> Found:
    canonical = imcontacts.canonicalise(asked)
    if not canonical:
        return Found("none", asked, why="that is not a phone number or an address I can read")
    person_id = None
    record = led.identifier(canonical) or {}
    if record.get("state") == "accepted" and record.get("person_id"):
        person_id = str(record["person_id"])
    if person_id is None:
        for pid, idents in people.items():
            if canonical in idents:
                person_id = pid
                break
    if person_id is None:
        dry = imspine.resolve(conn, canonical, contacts.get(canonical), emit=False)
        if dry.status == "resolved" and dry.person_id:
            person_id = str(dry.person_id)
    who = _person(conn, person_id, people, route="number") if person_id else None
    if who is None:
        name = imthreads.sanitise_body(contacts.get(canonical) or record.get("name") or "")
        who = Who(name=name[:120] or mask(canonical), route="number")
    who.asked = canonical
    return Found("one", asked, who=who)


def find(
    asked: str,
    *,
    conn: sqlite3.Connection,
    led: imledger.Ledger,
    contacts: Mapping[str, str],
) -> Found:
    """Who ``asked`` means.  Reads only; every resolve is dry.  See the module docstring."""
    text = (asked or "").strip()
    if not text:
        return Found("none", asked, why="no name, number or card was given")
    people = ledger_people(led)

    if _CARD_ID.match(text):
        who = _person(conn, text, people, route="card_id")
        if who is None:
            return Found("none", asked, why="no card in the directory has that id")
        return Found("one", asked, who=who)
    if looks_like_identifier(text):
        return _by_identifier(conn, led, text, people, contacts)

    stripped = re.sub(r"^people/", "", text)
    stripped = re.sub(r"\.md$", "", stripped)
    query = words(stripped)
    if not query:
        return Found("none", asked, why="those words hold no name to look for")

    # Every person the ledger knows, scored on their directory name and their card's.
    scored: dict[str, tuple[int, Who]] = {}
    for person_id in sorted(people):
        who = _person(conn, person_id, people, route="name")
        if who is None:
            continue
        best = max(tier(query, who.name), tier(query, who.card_name))
        scored[person_id] = (best, who)

    # Every Contacts entry whose name matches: onto the card its number reaches, else
    # a Contacts-only person (merged into a card of exactly the same name).
    loose: dict[tuple[str, ...], tuple[int, Who]] = {}
    for ident, contact_name in sorted(contacts.items()):
        grade = tier(query, contact_name)
        if not grade:
            continue
        owner = next((pid for pid, idents in people.items() if ident in idents), None)
        if owner is None:
            dry = imspine.resolve(conn, ident, contact_name, emit=False)
            if dry.status == "resolved" and dry.person_id:
                owner = str(dry.person_id)
        if owner is not None:
            if owner not in scored:
                who = _person(conn, owner, people, route="name")
                if who is None:
                    owner = None
                else:
                    scored[owner] = (0, who)
        if owner is not None:
            best, who = scored[owner]
            if ident not in who.contacts_identifiers:
                who.contacts_identifiers.append(ident)
            scored[owner] = (max(best, grade), who)
            continue
        group = words(contact_name)
        best, who = loose.get(group, (0, Who(name=imthreads.sanitise_body(contact_name)[:120],
                                             route="contacts")))
        who.contacts_identifiers.append(ident)
        loose[group] = (max(best, grade), who)

    for group, (grade, who) in list(loose.items()):
        twin = next((pid for pid, (_g, card_who) in scored.items()
                     if sorted(words(card_who.name)) == sorted(group)), None)
        if twin is None:
            continue
        best, card_who = scored[twin]
        for ident in who.contacts_identifiers:
            if ident not in card_who.contacts_identifiers:
                card_who.contacts_identifiers.append(ident)
        scored[twin] = (max(best, grade), card_who)
        del loose[group]

    everyone = [*scored.values(), *loose.values()]
    top = max((grade for grade, _who in everyone), default=0)
    if not top:
        return Found("none", asked, why=(
            "nobody you text, and nobody in your Contacts, goes by that name; a card that "
            "has never had a text can be named by its card id (prs_…) instead"))
    chosen = [who for grade, who in everyone if grade == top]
    for who in chosen:
        who.tier = top
        who.route = f"{who.route}:{TIER_NAMES[top]}"
        who.contacts_identifiers.sort()
    chosen.sort(key=lambda w: (w.name.casefold(), w.person_id or ""))
    if len(chosen) == 1:
        return Found("one", asked, who=chosen[0])
    return Found("ambiguous", asked, candidates=chosen,
                 why="more than one person goes by that, and I never guess which")
