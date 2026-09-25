"""iMessage intake — the spine adapter: the ONE module that touches the engine.

Every other module in ``_local/imessage/`` is plug-in code that reads Messages,
groups conversation-days and keeps its own ledger.  This module is the only one
that imports engine code or reads ``memory.db`` tables.  Keeping that seam to one
file is what makes :func:`check_contract` meaningful: if the engine changes a
signature the plug-in relies on, it changes HERE, and one check can see it and
pause the feed with a plain sentence instead of writing something wrong.

What it wraps, and why each wrapper exists
------------------------------------------
* :func:`open_conn` — ``people_db.init_structured_schema()``, never bare
  ``connect()``.  ``connect`` runs no schema, and an older member database then
  lacks the ``account`` column ``insert_proposal`` writes into (E9).
* :func:`resolve` — ``people_resolve.resolve`` with the identifier routed as an
  email or a phone and ``source="conversation"``.  It returns the engine's own
  ``ResolveResult`` untouched, so all FOUR outcomes reach the caller (E2): an
  ``ambiguous`` result carries ``proposal_id=None`` and raises nothing.
* :func:`stamp` — ``people_stamp.stamp_interaction``, the one sanctioned writer of
  a person card.  It never raises for a lock timeout or a disk error (E7), and it
  passes ``stamped=False`` through with the engine's own reason when the engine
  declines without raising (bad direction, no frontmatter, snapshot refusal).  A
  caller that flipped its ledger on "no exception" would lose that day for good.
* :func:`project` — ``people_index.project_changed(rels, conn=conn)``.  **The
  caller commits afterwards.**  See that function's docstring; it is G1, the
  showstopper the pre-build grade found.
* The proposal guards (:func:`is_pending`, :func:`dismissed_identifier`,
  :func:`pending_identifier`, :func:`dismissal_day`, :func:`reopen_justified`,
  :func:`proposal_state`, :func:`accepted_person`) — they read engine tables, so
  they live here.  They mirror ``email_people``'s helpers line for line where the
  two overlap, and ``tests/test_imspine.py`` drives both over the same rows so the
  day they diverge is a red test rather than a quiet difference.
* :func:`reopen_proposal` — the one proposal WRITE here, ``email_people._reopen``'s
  two statements, because a dismissal can only be taken back by flipping the row
  (E3 hands a dismissed id straight back to any identical re-raise).  Added with
  ``imrun`` (CP4), which decides when a re-open is earned.
* The review doors (CP6, ``imreview``): :func:`accept_proposal` and
  :func:`dismiss_proposal` are the member's own ``people.py proposals accept|dismiss``
  (``people.cmd_accept`` / ``people.cmd_proposal_set``), never a re-implementation, so
  a card created or a number attached from a text goes through exactly the lint gate,
  snapshot and projection the member's own yes goes through.  :func:`lint_card` is the
  engine's linter over one card.  Beside them, the reads ``review`` needs from engine
  tables: :func:`valid_identifier`, :func:`match_route` (HOW a number matched a card:
  exact, or only by its last digits, E5b), :func:`person_name`,
  :func:`proposal_detail`.  They are pinned by :func:`check_review_contract`, which is
  separate from :func:`check_contract` on purpose: a drift in a review door pauses the
  review alone, never the morning feed.
* :func:`snapshot_local` (CP6, G7) — ``local_snapshot.snapshot``, the engine's undo ring
  for machine-local files, taken on the plug-in's own ``config.local.json`` before
  ``exclude`` / ``include`` rewrite it.  Here, and imported only when called, so this
  module stays the one engine importer and a missing ring can never pause the feed.

What it never does
------------------
It never commits, never closes a connection it was handed, and never writes a
person card except through the engine's single writer.  It prints nothing on the
normal path; the one stderr line is for an engine that will not import at all.

Import order (the sys.path law)
-------------------------------
``imconfig`` is imported first: importing it puts ``.claude/scripts`` on
``sys.path`` and then re-asserts this folder at index 0, so a plug-in module can
never lose a name collision to an engine module.  The engine imports come after,
in their own block, and are guarded: an engine that will not import at all is
reported by :func:`check_contract` as the same plain refusal sentence a changed
signature gets, so the morning run can exit 0 with one line instead of a
traceback that would count against the failure latch.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import sqlite3
import sys
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, NamedTuple

# imconfig FIRST, before any engine module. See its docstring's sys.path section.
import imconfig  # noqa: E402

imconfig.ensure_engine_path()

_IMPORT_ERROR: ImportError | None
try:
    import config
    import note_parse
    import people_db
    import people_index
    import people_lint
    import people_norm
    import people_resolve
    import people_stamp
    import people_vocab  # noqa: F401 - read by name through _lookup() in check_contract
    import shared
except ImportError as _exc:  # pragma: no cover - exercised only on a broken engine
    _IMPORT_ERROR = _exc
    print(
        f"[imessage] the engine would not import ({type(_exc).__name__}: {_exc}); "
        "texts are paused until it does.",
        file=sys.stderr,
    )
else:
    _IMPORT_ERROR = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The interaction source every text lands under. Legal end to end
#: (``people_vocab.INTERACTION_SOURCE``); this plug-in is its first producer.
SOURCE = "conversation"

#: The one sentence a caller prints when the engine no longer matches.
REFUSAL_ENGINE_CHANGED = "texts paused: the engine changed, update the plug-in"

#: How long :func:`file_lock` waits by default. Deliberately NOT ``shared.file_lock``'s
#: 30 seconds: the morning stage is granted about 20 seconds in total, and a lock that
#: waits longer than the whole run is a lock that gets the run SIGKILLed.
DEFAULT_LOCK_TIMEOUT_S = 5.0

# The kinds whose payload carries an ``identifier`` the member may have dismissed.
# ``email_people`` only ever sees ``new_stub``; texts also raise ``add_identifier``
# (a known name, an unknown number), so the guard has to read both.
_GUARDED_KINDS = ("new_stub", "add_identifier")

# What ``people._apply_new_stub`` writes into ``change_log.detail`` when an accept
# creates the person (people.py:3239-3244). The ONLY place the engine records which
# person a new_stub became: the proposal row's ``person_id`` stays NULL.
_CREATED_PERSON_RE = re.compile(r"^created person (?P<slug>[a-z0-9][a-z0-9-]*) from new_stub$")

# ``people_index._LINK_RE`` is ``\(\s*→\s*([^)]+)\)``. A topic carrying ``(→`` would be
# read as the link; see :func:`_line_refusal`.
_ARROW_OPEN_RE = re.compile(r"\(\s*→")


# ---------------------------------------------------------------------------
# The engine contract — exact signatures, pinned. See check_contract().
# ---------------------------------------------------------------------------

#: Every engine callable this module (and the fixture) calls, with its exact
#: signature as ``inspect.signature`` prints it once annotations are stripped.
#: Recorded from the plan's VERIFIED section and re-read against the source on
#: 2026-09-24. EXACT on purpose: an added keyword can change behaviour as easily as
#: a removed one, and a paused feed loses nothing (the watermark does not move),
#: while a feed that writes on a changed contract can write the wrong thing.
ENGINE_SIGNATURES: dict[str, str] = {
    "config.now_local": "()",
    "note_parse.split_frontmatter": "(text)",
    "note_parse.load_note": "(text)",
    "people_db.init_structured_schema": "(conn=None, db_path=None)",
    "people_db.lookup_person_by_slug": "(conn, slug)",
    "people_index.project_changed": "(changed_paths, *, conn=None)",
    "people_resolve.resolve": (
        "(conn, *, email=None, phone=None, handle=None, name=None, source='email', "
        "emit=True, propose_identities=True, account=None)"
    ),
    "people_resolve._identifier_key": "(email, phone, handle)",
    "people_stamp.stamp_interaction": (
        "(person_path, *, source, occurred_at, direction, topic=None, legacy_topic=None, "
        "link=None, note=None, source_meeting_id=None, conn=None)"
    ),
    "people_stamp._interaction_line": (
        "(source, occurred_at, direction, topic, link, source_meeting_id=None)"
    ),
    "shared.file_lock": "(lock_path, timeout=30.0)",
}

#: Result fields read by attribute. A SUBSET check: a field the engine adds is
#: harmless, a field it removes or renames is not.
ENGINE_FIELDS: dict[str, tuple[str, ...]] = {
    "people_resolve.ResolveResult": (
        "status", "person_id", "proposal_kind", "proposal_id", "candidates", "detail",
    ),
    "people_stamp.StampResult": ("stamped", "detail"),
}

#: Vocabulary values this plug-in writes. Each must still be legal.
ENGINE_VOCAB: dict[str, tuple[str, ...]] = {
    "people_vocab.INTERACTION_SOURCE": (SOURCE,),
    "people_vocab.DIRECTION": ("they_reached_out", "i_reached_out", "mutual"),
}

#: Columns the guards read with raw SQL. Checked only when check_contract gets a conn.
ENGINE_COLUMNS: dict[str, tuple[str, ...]] = {
    "person": ("id", "slug"),
    "person_proposal": ("id", "kind", "payload", "status"),
    "change_log": ("entity_type", "entity_id", "action", "field", "detail", "created"),
}


def _lookup(dotted: str) -> Any:
    """``"people_stamp.stamp_interaction"`` → the live object, read at CALL time.

    Read through the module every time rather than bound at import, so a monkeypatch
    in a test (or a reloaded module) is what gets checked.
    """
    module_name, _, attr = dotted.partition(".")
    module = sys.modules.get(module_name) or __import__(module_name)
    return getattr(module, attr)


def signature_text(fn: Any) -> str:
    """``inspect.signature`` with every annotation stripped, as a string.

    Annotations are left out because the engine uses ``from __future__ import
    annotations`` and a re-spelled type hint is not a contract change; names, kinds
    (positional / keyword-only) and defaults are.
    """
    sig = inspect.signature(fn)
    bare = sig.replace(
        parameters=[p.replace(annotation=inspect.Parameter.empty) for p in sig.parameters.values()],
        return_annotation=inspect.Signature.empty,
    )
    return str(bare)


def check_contract(conn: sqlite3.Connection | None = None) -> str | None:
    """``None`` when the engine still matches, else the plain refusal sentence.

    The sentence is :data:`REFUSAL_ENGINE_CHANGED` followed by what changed, in
    brackets, so the member reads one line and a maintainer reads which seam moved.
    A caller that gets a sentence must write NOTHING this run and exit 0 (never a
    latch-counting failure): a paused feed loses no day, because the watermark only
    moves after a clean run.

    With ``conn`` it also checks the columns the guards read with raw SQL. Never
    raises: an unexpected error while checking is itself reported as a mismatch.
    """
    if _IMPORT_ERROR is not None:
        return f"{REFUSAL_ENGINE_CHANGED} (the engine would not import: {_IMPORT_ERROR})"

    problems: list[str] = []
    for dotted, expected in ENGINE_SIGNATURES.items():
        try:
            actual = signature_text(_lookup(dotted))
        except (AttributeError, ImportError, TypeError, ValueError) as exc:
            problems.append(f"{dotted} is gone ({type(exc).__name__})")
            continue
        if actual != expected:
            problems.append(f"{dotted} is now {actual}, expected {expected}")

    for dotted, wanted in ENGINE_FIELDS.items():
        try:
            have = {f.name for f in dataclasses.fields(_lookup(dotted))}
        except (AttributeError, ImportError, TypeError) as exc:
            problems.append(f"{dotted} is gone ({type(exc).__name__})")
            continue
        missing = [name for name in wanted if name not in have]
        if missing:
            problems.append(f"{dotted} lost {', '.join(missing)}")

    for dotted, values in ENGINE_VOCAB.items():
        try:
            legal = tuple(_lookup(dotted))
        except (AttributeError, ImportError, TypeError) as exc:
            problems.append(f"{dotted} is gone ({type(exc).__name__})")
            continue
        missing = [value for value in values if value not in legal]
        if missing:
            problems.append(f"{dotted} no longer allows {', '.join(missing)}")

    if conn is not None:
        for table, wanted in ENGINE_COLUMNS.items():
            try:
                have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error as exc:
                problems.append(f"table {table} unreadable ({type(exc).__name__})")
                continue
            missing = [name for name in wanted if name not in have]
            if missing:
                problems.append(f"table {table} lost {', '.join(missing)}")

    if not problems:
        return None
    return f"{REFUSAL_ENGINE_CHANGED} ({'; '.join(problems)})"


# ---------------------------------------------------------------------------
# Connection, clock, lock
# ---------------------------------------------------------------------------


def open_conn(db_path: str | Path | None = None) -> sqlite3.Connection:
    """The structured-store connection, schema ensured (E9).

    ``init_structured_schema`` commits its OWN schema work; everything the caller
    writes afterwards is the caller's to commit, exactly once, and the caller closes
    the connection in a ``finally`` (the ``email_triage.py:485-526`` shape).
    ``db_path=None`` is the member's real ``memory.db``; tests always pass a temp one.
    """
    return people_db.init_structured_schema(db_path=db_path)


def now_local() -> datetime:
    """The engine's clock in the configured zone (``config.now_local``)."""
    return config.now_local()


def local_zone_name() -> str:
    """The name of the zone ``now_local`` actually uses — for ``status`` to print.

    Read off the live tzinfo rather than ``config.HEARTBEAT_TIMEZONE``: that
    constant keeps the configured IANA name even when ``ZoneInfo`` could not load
    it and the engine fell back to the machine's fixed offset (``config.py:685-695``),
    and reporting a zone that is not in force is exactly the confusion ``status``
    exists to prevent.
    """
    tz = config.LOCAL_TZ
    key = getattr(tz, "key", None)
    if isinstance(key, str) and key:
        return key
    return config.now_local().tzname() or str(tz)


def file_lock(
    path: str | Path, timeout: float = DEFAULT_LOCK_TIMEOUT_S
) -> AbstractContextManager[None]:
    """``shared.file_lock`` — the engine's cross-platform ``.lock``-sidecar lock.

    Raises ``TimeoutError`` if it cannot be taken in time. The ledger uses it so the
    plug-in's own state and the engine's cards are locked the same way.
    """
    lock: AbstractContextManager[None] = shared.file_lock(Path(path), timeout=timeout)
    return lock


# ---------------------------------------------------------------------------
# Paths: person id → card, and card → the vault-relative name the projector wants
# ---------------------------------------------------------------------------


class PersonCard(NamedTuple):
    """A person's card: the absolute ``path`` and the vault-relative ``rel``.

    ``rel`` (``people/<slug>.md``) is what :func:`project` and the snapshot ring key
    on. Unpacks as ``path, rel = person_path(conn, pid)``.
    """

    path: Path
    rel: str


def _people_rel(full: Path) -> str | None:
    """``full``'s vault-relative POSIX name if it is a card directly under people/.

    Both sides are resolved before comparing, because macOS temp and home folders
    can sit behind symlinks and an unresolved compare answers wrongly.
    """
    try:
        rel = full.resolve().relative_to(config.MEMORY_DIR.resolve()).as_posix()
    except (ValueError, OSError):
        return None
    parts = rel.split("/")
    if len(parts) != 2 or parts[0] != "people" or not parts[1].endswith(".md"):
        return None
    return rel


def _declares(card: Path, person_id: str) -> bool:
    """True iff the note's frontmatter ``id`` is ``person_id`` (identity is the id)."""
    try:
        fm, _body, _err = note_parse.load_note(card.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return False
    return fm.get("id") == person_id


def person_path(conn: sqlite3.Connection, person_id: str) -> PersonCard | None:
    """The card that belongs to ``person_id``, or ``None`` when there is none.

    The fast road is the one ``email_people._person_path`` takes — the ``slug``
    column names ``people/<slug>.md`` — but checked: the file must DECLARE the id.
    When it does not (the member renamed the note in their own editor), this falls
    back to what ``people._person_note_path`` does: the note whose frontmatter
    declares the id, found by reading each card. Identity is the id, never the
    filename; stamping the wrong file because a name was reused would put one
    person's texts on another person's card.
    """
    if not person_id:
        return None
    people_dir = config.PEOPLE_DIR
    row = conn.execute("SELECT slug FROM person WHERE id = ?", (person_id,)).fetchone()
    if row is not None and row[0]:
        candidate = people_dir / f"{row[0]}.md"
        if candidate.is_file() and _declares(candidate, person_id):
            rel = _people_rel(candidate)
            if rel is not None:
                return PersonCard(candidate, rel)
    if not people_dir.is_dir():
        return None
    for card in sorted(people_dir.glob("*.md")):
        if card.stem == "_TEMPLATE" or not _declares(card, person_id):
            continue
        rel = _people_rel(card)
        if rel is not None:
            return PersonCard(card, rel)
    return None


def _card(person_rel_or_path: str | Path) -> PersonCard | str:
    """Resolve a card argument to a :class:`PersonCard`, or a refusal sentence.

    A relative argument is vault-relative (``people/x.md``). Anything that is not a
    card directly under the vault's ``people/`` folder is REFUSED rather than
    passed on: ``memory_snapshot.snapshot_for_rewrite`` treats a path outside the
    vault as "outside this net" and lets the write go ahead with NO backup, so the
    engine would stamp such a file unprotected. That is also what keeps the test
    fixture honest — a fixture that forgot to move the vault gets a refusal, not an
    unbacked write.
    """
    raw = Path(person_rel_or_path)
    full = raw if raw.is_absolute() else config.MEMORY_DIR / raw
    rel = _people_rel(full)
    if rel is None:
        return "not a card in the people folder of the memory vault, so nothing was written"
    return PersonCard(full, rel)


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------


def _identifier_kwargs(identifier: str) -> dict[str, str]:
    """``{"email": x}`` or ``{"phone": x}``: an ``@`` makes it an email, else a phone."""
    return {"email": identifier} if "@" in identifier else {"phone": identifier}


def _engine_key(identifier: str | None) -> tuple[str, str | None, str] | None:
    """The resolver's own ``(kind, platform, normalised_value)`` for an identifier.

    The SAME normaliser ``people_resolve`` uses to write ``payload["identifier"]``,
    so a guard keyed on it compares like with like. ``None`` for a blank identifier
    or one that normalises to nothing (a "phone" with no digits).
    """
    ident = (identifier or "").strip()
    if not ident:
        return None
    kw = _identifier_kwargs(ident)
    key = people_resolve._identifier_key(kw.get("email"), kw.get("phone"), None)
    if key is None or not key[2]:
        return None
    return key


def resolve(
    conn: sqlite3.Connection,
    identifier: str,
    name: str | None = None,
    *,
    emit: bool,
) -> people_resolve.ResolveResult:
    """Route one canonical handle through the engine resolver. Returns its ``ResolveResult``.

    Four outcomes, all of which the caller must branch on (E2): ``resolved``
    (``person_id`` set), ``proposed`` (``proposal_id`` set — gate it on
    :func:`is_pending`, because a dismissed row's id can come back, E3),
    ``ambiguous`` (a shared number or several people; ``proposal_id`` is ``None``
    and NOTHING was raised — hold it, never guess), and ``unresolved``.

    ``emit=False`` is the dry lookup: no proposal row is written, and a
    would-be proposal still reports ``proposed`` with ``proposal_id=None``.

    A blank identifier, or one with nothing to match on, is answered
    ``unresolved`` HERE and never reaches the engine: the engine would otherwise
    route a nameless blank to its name-only step, or raise a ``new_stub`` whose
    identifier is the empty string. The detail never echoes the identifier.
    """
    if _engine_key(identifier) is None:
        return people_resolve.ResolveResult(
            "unresolved", detail="texts: no usable phone number or address to resolve"
        )
    ident = identifier.strip()
    return people_resolve.resolve(
        conn,
        **_identifier_kwargs(ident),
        name=(name or "").strip() or None,
        source=SOURCE,
        emit=emit,
    )


# ---------------------------------------------------------------------------
# Stamp
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StampOutcome:
    """What one stamp did. ``stamped`` is the ONLY signal a ledger may flip on.

    ``detail`` is the engine's own reason (``"stamped"``, ``"idempotent — …"``,
    ``"invalid source/direction"``, ``"no frontmatter — …"``, a snapshot refusal) or
    this module's (a held lock, a disk error, a refused line). ``pointer`` is the
    exact line the engine writes, for recovery. ``already_present`` is True when
    the engine declined because that exact line is already on the card — the one
    ``stamped=False`` that means the day HAS landed (a re-run, or a crash between
    the write and the ledger's ``done``).
    """

    stamped: bool
    detail: str
    path: Path | None
    rel: str | None = None
    pointer: str = ""
    already_present: bool = False


def pointer_line(
    *, occurred_at: str, direction: str, topic: str | None, link: str | None
) -> str:
    """The exact ``## Interactions`` line a stamp writes, built by the engine itself.

    ``people_stamp._interaction_line`` with ``source="conversation"``, so the ledger's
    crash recovery can check a card for the line without a second spelling of it.
    """
    line: str = people_stamp._interaction_line(SOURCE, occurred_at, direction, topic, link)
    return line


def _body_has(text: str, pointer: str) -> bool:
    """``people_stamp``'s own idempotency test: ``pointer.strip() in body`` (:83)."""
    _fm, body = note_parse.split_frontmatter(text)
    return pointer.strip() in body


def has_pointer(
    person_rel_or_path: str | Path,
    *,
    occurred_at: str,
    direction: str,
    topic: str | None,
    link: str | None,
) -> bool:
    """True iff the card already carries this exact interaction line.

    The write-ahead ledger's recovery check for an ``intent`` with no ``done``. The
    test is the engine's own, so "present" here means the engine would also treat
    a re-stamp as a no-op. An unreadable card answers False: re-stamping is safe
    because the engine's idempotency still holds, while a false True would lose a
    day.
    """
    card = _card(person_rel_or_path)
    if isinstance(card, str):
        return False
    pointer = pointer_line(occurred_at=occurred_at, direction=direction, topic=topic, link=link)
    try:
        return _body_has(card.path.read_text(encoding="utf-8"), pointer)
    except (OSError, UnicodeDecodeError):
        return False


def _line_refusal(occurred_at: str, topic: str | None, link: str | None) -> str | None:
    """A reason the line would land on the card but never reach ``memory.db``, else None.

    The engine does not check any of these, and each one is a SILENT loss: the line
    reads fine to a human and the projector skips or mangles it.

    * ``occurred_at`` must be a real ``YYYY-MM-DD`` day: the projector's line pattern
      starts ``\\d{4}-\\d{2}-\\d{2}``, and ``last_contacted`` is bumped on a plain
      string compare (``people_stamp.py:107``), so a timestamp would out-sort a day.
    * the link may hold no ``)`` — ``people_index._LINK_RE`` stops at the first one,
      truncating the stored link and leaking its tail into the summary (E5d) — and
      no line break, which ``_interaction_line`` does not flatten for a link.
    * the topic may hold no ``(→``, which the projector would read as the link.
    """
    if not isinstance(occurred_at, str) or len(occurred_at) != 10:
        return "the day must be written YYYY-MM-DD, so nothing was written"
    try:
        date.fromisoformat(occurred_at)
    except ValueError:
        return "the day is not a real calendar date, so nothing was written"
    if link is not None:
        if ")" in link:
            return "the link holds ')', which the projector would cut short, so nothing was written"
        if "\n" in link or "\r" in link:
            return "the link holds a line break, so nothing was written"
    if topic is not None and _ARROW_OPEN_RE.search(topic):
        return (
            "the topic holds '(→', which the projector would read as the link, so nothing "
            "was written"
        )
    return None


def stamp(
    person_rel_or_path: str | Path,
    *,
    occurred_at: str,
    direction: str,
    topic: str | None,
    link: str | None,
    conn: sqlite3.Connection | None = None,
) -> StampOutcome:
    """Stamp one ``conversation`` interaction onto a card. Never raises for E7.

    ``person_rel_or_path`` is ``people/<slug>.md`` (vault-relative) or the absolute
    card path. The write is the engine's single writer: locked (5 s), read,
    snapshotted into the memory undo ring, atomically replaced.

    ``stamped=True`` only when the engine says it wrote. Everything else comes back
    as ``stamped=False`` with a ``detail`` and is never an exception:

    * ``TimeoutError`` — another writer held the card's lock for 5 s (E7);
    * ``OSError`` — the read or the atomic write failed (E7), including a card
      that vanished;
    * ``UnicodeDecodeError`` — the card is not UTF-8, a fault of that one card;
    * the engine's own quiet refusals, passed through with its words: an invalid
      direction, a card with no frontmatter, a snapshot the undo ring could not
      take (in which case nothing was written);
    * this module's refusals, checked first (:func:`_card`, :func:`_line_refusal`).

    One person's failure must never abort a run, so the caller records ``detail``
    and moves on. Any OTHER exception is a bug and is allowed to surface.
    """
    card = _card(person_rel_or_path)
    if isinstance(card, str):
        return StampOutcome(False, card, None)
    full, rel = card
    refusal = _line_refusal(occurred_at, topic, link)
    if refusal is not None:
        return StampOutcome(False, refusal, full, rel)
    pointer = pointer_line(occurred_at=occurred_at, direction=direction, topic=topic, link=link)
    if not full.is_file():
        # Checked here so a missing card does not leave a stray `.md.lock` sidecar behind:
        # the engine's lock creates its sidecar before it reads the card.
        return StampOutcome(False, "the card is not on disk, so nothing was written", full, rel,
                            pointer)
    try:
        res = people_stamp.stamp_interaction(
            full,
            source=SOURCE,
            occurred_at=occurred_at,
            direction=direction,
            topic=topic,
            link=link,
            conn=conn,
        )
    except TimeoutError as exc:
        return StampOutcome(False, f"the card was busy (its lock was held): {exc}", full, rel,
                            pointer)
    except UnicodeDecodeError as exc:
        return StampOutcome(False, f"the card is not readable text ({exc.reason})", full, rel,
                            pointer)
    except OSError as exc:
        return StampOutcome(False, f"disk error while stamping ({type(exc).__name__}: {exc})",
                            full, rel, pointer)
    if res.stamped:
        return StampOutcome(True, str(res.detail), full, rel, pointer)
    try:
        present = _body_has(full.read_text(encoding="utf-8"), pointer)
    except (OSError, UnicodeDecodeError):
        present = False
    return StampOutcome(False, str(res.detail) or "not stamped (the engine gave no reason)",
                        full, rel, pointer, already_present=present)


# ---------------------------------------------------------------------------
# Project — read G1 before touching this
# ---------------------------------------------------------------------------


def _as_rel(item: str | Path) -> str:
    """A vault-relative POSIX name for the projector; absolute paths are converted."""
    p = Path(item)
    if p.is_absolute():
        rel = _people_rel(p)
        return rel if rel is not None else f"people/{p.name}"
    return p.as_posix()


def project(
    conn: sqlite3.Connection, rel_paths: Iterable[str | Path]
) -> people_index.ProjectionResult:
    """Project exactly these cards into ``memory.db``. **THE CALLER MUST COMMIT AFTER.**

    ``people_index.project_changed(rels, conn=conn)`` — the same call
    ``meeting_scaffold`` relies on, but with the caller's connection. Passed a
    connection, ``project_changed`` NEITHER COMMITS NOR CLOSES (its ``if own:``
    tail, ``people_index.py:188-190``). ``sqlite3.Connection.close()`` on an open
    transaction ROLLS IT BACK. So the one correct order is::

        stamp(...)  →  project(conn, rels)  →  conn.commit()  →  (state)  →  conn.close()

    Close without that commit and the ``conversation`` lines sit on the cards while
    the ``interaction`` rows never reach ``memory.db``: ``/recall`` shows nothing,
    no exception is raised and no test goes red unless it reopens the database. This
    is G1, the showstopper the pre-build grade found. ``tests/test_imspine.py``
    proves the order by closing, reopening and reading the row back, with a
    negative control that skips the commit.

    Duplicates are dropped, first occurrence kept. Returns the engine's
    ``ProjectionResult`` (``skipped_paths`` names any card the linter refused).
    """
    if conn is None:
        raise ValueError(
            "project() needs the caller's connection; without one the engine opens, commits "
            "and closes its own, which is a different contract"
        )
    rels: list[str] = []
    for item in rel_paths:
        rel = _as_rel(item)
        if rel not in rels:
            rels.append(rel)
    return people_index.project_changed(rels, conn=conn)


# ---------------------------------------------------------------------------
# The proposal guards — they read engine tables, so they live here
# ---------------------------------------------------------------------------


def proposal_state(conn: sqlite3.Connection, proposal_id: str | None) -> str | None:
    """``'pending'`` | ``'accepted'`` | ``'dismissed'``, or ``None`` for no such row."""
    if not proposal_id:
        return None
    row = conn.execute(
        "SELECT status FROM person_proposal WHERE id = ?", (proposal_id,)
    ).fetchone()
    return str(row[0]) if row is not None else None


def is_pending(conn: sqlite3.Connection, proposal_id: str | None) -> bool:
    """True iff the proposal is still PENDING — the only state that may be surfaced.

    Mirrors ``email_people._is_pending`` (:242-247). Every surfacing path gates on
    it, because ``insert_proposal`` dedups on payload equality and, for a
    ``new_stub``, hands back a DISMISSED row's id when the payload matches (E3).
    """
    return proposal_state(conn, proposal_id) == "pending"


def _identifier_proposal(conn: sqlite3.Connection, identifier: str, status: str) -> str | None:
    """Newest ``new_stub``/``add_identifier`` row in ``status`` whose payload names ``identifier``.

    Keyed on the payload's ``kind`` + ``identifier`` parsed from JSON, NEVER on
    payload equality: the payload also carries the display name and the source, so
    a Contacts name that drifted would otherwise look like a different person (E4).
    The identifier is normalised with the resolver's own key, so it compares with
    exactly what the resolver wrote. Unparseable payloads are skipped, as
    ``email_people`` skips them.
    """
    key = _engine_key(identifier)
    if key is None:
        return None
    kind, _platform, nv = key
    rows = conn.execute(
        "SELECT id, payload FROM person_proposal "
        "WHERE kind IN (?, ?) AND status = ? ORDER BY rowid DESC",
        (*_GUARDED_KINDS, status),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row[1])
        except (TypeError, ValueError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("kind") == kind
            and payload.get("identifier") == nv
        ):
            return str(row[0])
    return None


def dismissed_identifier(conn: sqlite3.Connection, identifier: str) -> str | None:
    """The newest DISMISSED proposal for this phone/address, or ``None``.

    The mirror of ``email_people._dismissed_email_stub`` (:270-288), widened in the
    one way texts need: it reads ``add_identifier`` as well as ``new_stub``, since a
    known name arriving from an unknown number raises the former, and a dismissal of
    either is the member saying no to that number.
    """
    return _identifier_proposal(conn, identifier, "dismissed")


def pending_identifier(conn: sqlite3.Connection, identifier: str) -> str | None:
    """The newest PENDING proposal for this phone/address, or ``None``.

    The mirror of ``email_people._pending_email_stub`` (:250-267): surface the card
    already waiting instead of letting a drifted name mint a sibling beside it.
    """
    return _identifier_proposal(conn, identifier, "pending")


def dismissal_day(conn: sqlite3.Connection, proposal_id: str | None) -> str | None:
    """The DAY (``YYYY-MM-DD``) of the latest logged dismissal of this proposal.

    Read from ``change_log``, not the proposal row (which holds no date), exactly as
    ``email_people._dismissal_day`` (:291-303) reads it. ``None`` when no dismissal
    was ever logged, and a caller then fails toward silence. Every engine dismissal
    door logs this row; any dismissal the plug-in itself writes must log it too, or
    this guard never fires.
    """
    if not proposal_id:
        return None
    row = conn.execute(
        "SELECT MAX(created) FROM change_log "
        "WHERE entity_type = 'person_proposal' AND entity_id = ? AND action = 'dismissed'",
        (proposal_id,),
    ).fetchone()
    return str(row[0])[:10] if row is not None and row[0] else None


def reopen_justified(
    conn: sqlite3.Connection, proposal_id: str | None, evidence_day: str | date | None
) -> bool:
    """True iff ``evidence_day`` is a day STRICTLY after the logged dismissal day.

    The rule of ``email_people._reopen_justified`` (:306-321): only contact NEWER
    than the member's "no" is new evidence, a same-day dismiss-then-text stays
    dismissed (self-healing on the next day's text, never a nag), and an unlogged
    dismissal never re-opens.

    Day precision on BOTH sides. The engine compares the evidence string as given, so
    a full timestamp from the dismissal day (``2026-09-10T09:00`` against
    ``2026-09-10``) would sort after it and re-open on the same day; here the
    evidence is cut to its day first. A ``date`` is accepted; anything that is not a
    real day answers False.
    """
    if isinstance(evidence_day, date):
        evidence = evidence_day.isoformat()[:10]
    elif isinstance(evidence_day, str):
        evidence = evidence_day.strip()[:10]
    else:
        return False
    try:
        date.fromisoformat(evidence)
    except ValueError:
        return False
    day = dismissal_day(conn, proposal_id)
    return day is not None and evidence > day


#: The two engine writes :func:`reopen_proposal` makes, pinned at CALL time. They are
#: not in :data:`ENGINE_SIGNATURES` (the contract test holds that table to the plan's
#: VERIFIED list, and pins these two separately as the fixture's), so a drift here
#: re-opens nothing rather than pausing the whole feed: a missed re-open costs one
#: card staying dismissed, which is the safe side.
_REOPEN_SIGNATURES: dict[str, str] = {
    "people_db.set_proposal_status": "(conn, proposal_id, status)",
    "people_db.log_change": (
        "(conn, entity_type, entity_id, action, detail, *, created, field=None, "
        "old_value=None, new_value=None, source=None, confidence=None)"
    ),
}


def reopen_proposal(conn: sqlite3.Connection, proposal_id: str | None, why: str) -> bool:
    """Flip a DISMISSED proposal back to pending, change-logged. True when it did.

    The same two writes as ``email_people._reopen`` (:326-339): the status flip, then
    the ``change_log`` row, because ``set_proposal_status`` logs nothing itself. It is
    the only road back from a dismissal: ``insert_proposal`` hands a dismissed row's id
    straight back for an identical payload (E3), so re-raising through the resolver
    would re-open nothing, and a drifted name would mint a sibling card instead (E4).

    The CALLER decides whether a re-open is earned (:func:`reopen_justified`, and the
    caller's own ``reopen`` flag, which a backfill turns off). This only performs it,
    on the caller's connection; the caller commits. ``why`` lands in the log and must
    carry no number or address. False, writing nothing, when the row is not dismissed
    or either engine write has changed shape.
    """
    if proposal_state(conn, proposal_id) != "dismissed" or not proposal_id:
        return False
    for dotted, expected in _REOPEN_SIGNATURES.items():
        try:
            if signature_text(_lookup(dotted)) != expected:
                return False
        except (AttributeError, ImportError, TypeError, ValueError):
            return False
    people_db.set_proposal_status(conn, proposal_id, "pending")
    people_db.log_change(
        conn, "person_proposal", proposal_id, "pending",
        f"proposal {proposal_id} re-opened: {why}",
        created=config.now_local().isoformat(),
    )
    return True


def accepted_person(conn: sqlite3.Connection, proposal_id: str | None) -> str | None:
    """The person a now-ACCEPTED ``new_stub`` created, or ``None``.

    Where the engine records it: ONLY in ``change_log``. ``people._apply_new_stub``
    (people.py:3233-3245) creates the person through ``cmd_new``, flips the
    proposal to ``accepted`` and logs ``action='accepted'``, ``field='new_stub'``,
    ``detail='created person <slug> from new_stub'``. The proposal row's
    ``person_id`` stays NULL (a new_stub is inserted with ``person_id=None`` and the
    flip touches only ``status``), and the new card carries no pointer back to the
    proposal. So the slug is read from that log line and resolved through
    ``person.slug``.

    One fallback: an email ``new_stub`` attaches the address on accept
    (people.py:3220-3223), so a dry resolve of that address names the person even if
    the slug has since changed. A PHONE stub has no such fallback — the phone is not
    attached (E1) — and answers ``None``, which the caller must treat as "not yet
    known", never as "no person".

    ``None`` too for any other kind, a proposal still pending or dismissed, or a
    person who has since been merged away.
    """
    if not proposal_id:
        return None
    row = conn.execute(
        "SELECT kind, status, payload FROM person_proposal WHERE id = ?", (proposal_id,)
    ).fetchone()
    if row is None or row[0] != "new_stub" or row[1] != "accepted":
        return None
    log = conn.execute(
        "SELECT detail FROM change_log WHERE entity_type = 'person_proposal' AND entity_id = ? "
        "AND action = 'accepted' AND field = 'new_stub' ORDER BY id DESC LIMIT 1",
        (proposal_id,),
    ).fetchone()
    if log is not None and log[0]:
        m = _CREATED_PERSON_RE.match(str(log[0]).strip())
        if m:
            found = people_db.lookup_person_by_slug(conn, m.group("slug"))
            if found:
                return str(found)
    try:
        payload = json.loads(row[2])
    except (TypeError, ValueError):
        return None
    if isinstance(payload, dict) and payload.get("kind") == "email" and payload.get("identifier"):
        res = people_resolve.resolve(
            conn, email=str(payload["identifier"]), source=SOURCE, emit=False
        )
        if res.status == "resolved" and res.person_id:
            return str(res.person_id)
    return None



# ---------------------------------------------------------------------------
# The review doors (CP6) — the engine's own accept, dismiss and lint
# ---------------------------------------------------------------------------

#: The engine callables ``imreview`` acts and reads through, pinned at CALL time.
#: Kept OUT of :data:`ENGINE_SIGNATURES` on purpose: that table is held to the plan's
#: VERIFIED list by the contract test, and the morning run never calls these.  So a
#: drift here pauses the review (one plain sentence, nothing written) and never the
#: daily feed, which does not need them.  ``tests/test_imspine.py`` pins each one
#: against a literal typed out by hand.
REVIEW_SIGNATURES: dict[str, str] = {
    "people.cmd_accept": "(proposal_id, *, decide_gate=None, replace=False)",
    "people.cmd_proposal_set": "(proposal_id, status, *, decide_gate=None)",
    "people_lint.lint_file": "(path, *, known_person_ids=None, prior_created=None)",
    "people_norm.is_valid_phone": "(value)",
    "people_norm.is_valid_email": "(value)",
    "people_norm.same_phone": "(a, b)",
    "people_norm.phone_lookup_digits": "(value)",
}

#: Columns the review reads with raw SQL.  Checked when a connection is given.
REVIEW_COLUMNS: dict[str, tuple[str, ...]] = {
    "person": ("id", "name"),
    "person_identifier": ("person_id", "kind", "platform", "normalised_value"),
    "person_proposal": ("id", "kind", "payload", "status", "person_id"),
}


def check_review_contract(conn: sqlite3.Connection | None = None) -> str | None:
    """``None`` when every door ``review`` uses still matches, else the plain sentence.

    :func:`check_contract` first (review resolves and reads through the same seams the
    daily run does), then the review's own doors and columns.  Never raises.
    """
    base = check_contract(conn)
    if base is not None:
        return base
    problems: list[str] = []
    for dotted, expected in REVIEW_SIGNATURES.items():
        try:
            actual = signature_text(_lookup(dotted))
        except (AttributeError, ImportError, TypeError, ValueError) as exc:
            problems.append(f"{dotted} is gone ({type(exc).__name__})")
            continue
        if actual != expected:
            problems.append(f"{dotted} is now {actual}, expected {expected}")
    if conn is not None:
        for table, wanted in REVIEW_COLUMNS.items():
            try:
                have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error as exc:
                problems.append(f"table {table} unreadable ({type(exc).__name__})")
                continue
            missing = [name for name in wanted if name not in have]
            if missing:
                problems.append(f"table {table} lost {', '.join(missing)}")
    if not problems:
        return None
    return f"{REFUSAL_ENGINE_CHANGED} ({'; '.join(problems)})"


def engine_db_path() -> Path:
    """The database the engine's own doors open (``config.DATABASE_PATH``, read now).

    :func:`accept_proposal` and :func:`dismiss_proposal` open their OWN connection with
    no path, so a caller handed a different ``db_path`` for its own connection would
    raise a card in one database and accept it in another.  ``imreview`` refuses that.
    """
    return Path(config.DATABASE_PATH)


def _door_result(out: Any) -> dict[str, Any]:
    if isinstance(out, Mapping):
        return dict(out)
    return {"status": "error", "reason": "the engine's door gave no answer it could read"}


def accept_proposal(proposal_id: str) -> dict[str, Any]:
    """Accept a proposal through the ENGINE's own door: ``people.py proposals accept <id>``.

    ``people.cmd_accept`` — for a ``new_stub`` it creates the person (lint-gated,
    projected); for an ``add_identifier`` it writes the number onto the card
    (snapshotted into the undo ring, lint-gated, projected) and flips the proposal.

    **It opens its OWN connection and commits it.**  SQLite's writer lock is per
    database, so the caller must COMMIT its own connection before calling this, or the
    door waits out ``busy_timeout`` and fails; and the caller reads the result on a
    connection with no transaction open, so it sees what the door committed.  Returns
    the engine's answer untouched (``status`` ``ok`` | ``needs_action`` | ``error`` and
    its reason); never raises for a refusal, which the engine reports as data.
    """
    return _door_result(_lookup("people.cmd_accept")(proposal_id))


def dismiss_proposal(proposal_id: str) -> dict[str, Any]:
    """Dismiss a proposal through the ENGINE's own door: ``people.py proposals dismiss``.

    ``people.cmd_proposal_set(id, "dismissed")`` flips the status AND writes the
    ``change_log`` row that :func:`dismissal_day` and :func:`dismissed_identifier` read,
    so the member's "no" is on the engine's own record, keyed on the number, and no
    drifted Contacts name can mint a sibling card for it later (E4).  Own connection,
    committed: the caller commits its own first, as for :func:`accept_proposal`.
    """
    return _door_result(_lookup("people.cmd_proposal_set")(proposal_id, "dismissed"))


def lint_card(person_rel_or_path: str | Path) -> list[str]:
    """The engine linter's ERROR details for one card (``[]`` = lint-clean).

    ``people_lint.lint_file`` — what ``people.py lint <slug>`` runs.  A path that is
    not a card in the vault's people folder answers with this module's refusal.
    """
    card = _card(person_rel_or_path)
    if isinstance(card, str):
        return [card]
    result = people_lint.lint_file(card.path)
    return [str(f.detail) for f in result.flags if f.severity == "error"]


def valid_identifier(identifier: str | None) -> bool:
    """True iff the engine could ever hold this as a phone (E.164) or an address.

    The accept doors lint every identifier they write and the lint is E.164-only for
    phones (``people_norm.is_valid_phone``), so a bare 16- or 25-digit business ID can
    never be added to a card: ``review`` offers such a number for dismissal only.
    """
    ident = (identifier or "").strip()
    if not ident:
        return False
    if "@" in ident:
        return bool(people_norm.is_valid_email(ident))
    return bool(people_norm.is_valid_phone(ident))


def match_route(
    conn: sqlite3.Connection, identifier: str, person_id: str | None
) -> tuple[str | None, str | None]:
    """HOW ``identifier`` reaches ``person_id``'s card: ``("exact"|"suffix", card value)``.

    The resolver's ``resolved`` says "strong identifier match" either way, but it
    reaches it by two roads (``people_resolve._identifier_rows``): an exact
    normalised value, or — only when nothing is exact — the phone digit-suffix
    fallback (``people_norm.same_phone``, 7 or more significant digits, no country
    table).  The second road can put one person's texts on ANOTHER person's card
    (E5b, KNOWN LIMIT 12), so the ledger records the road and ``review`` shows every
    suffix match for the member to check.  ``(None, None)`` when neither road joins
    the number to that card (a member ruling, a card that changed since).
    """
    key = _engine_key(identifier)
    if key is None or not person_id:
        return None, None
    kind, platform, nv = key
    rows = conn.execute(
        "SELECT kind, platform, normalised_value FROM person_identifier WHERE person_id = ?",
        (person_id,),
    ).fetchall()
    for row in rows:
        if row[0] == kind and (row[1] or "") == (platform or "") and row[2] == nv:
            return "exact", str(row[2])
    if kind == "phone":
        for row in rows:
            if row[0] == "phone" and row[2] and people_norm.same_phone(nv, str(row[2])):
                return "suffix", str(row[2])
    return None, None


def suffix_digits(identifier: str, card_value: str) -> int:
    """How many trailing digits a suffix match rests on (the shorter number's length)."""
    a = people_norm.phone_lookup_digits(identifier)
    b = people_norm.phone_lookup_digits(card_value)
    return min(len(a), len(b))


def person_name(conn: sqlite3.Connection, person_id: str | None) -> str | None:
    """A person's display name from the directory, or ``None`` when there is no such row."""
    if not person_id:
        return None
    row = conn.execute("SELECT name FROM person WHERE id = ?", (person_id,)).fetchone()
    return str(row[0]) if row is not None and row[0] else None


#: The engine's machine-local undo ring, pinned at CALL time like the review doors: a
#: drift refuses the backup (and so the edit waiting on it), never the morning feed.
LOCAL_SNAPSHOT_SIGNATURES: dict[str, str] = {
    "local_snapshot.snapshot": "(path)",
}


def snapshot_local(path: str | Path) -> Path | None:
    """Back a machine-local file up into the engine's local undo ring BEFORE it is rewritten.

    ``local_snapshot.snapshot(path)``: a byte-for-byte copy into
    ``.claude/data/local-snapshots/`` (``config.LOCAL_SNAPSHOTS_DIR``), restorable with
    ``local_snapshot.py restore <path>``.  A hand-edited ``_local/`` file has no other
    backup, and ``config.local.json`` carries ``never_ingest``, the list of people whose
    texts are never read, so ``exclude`` and ``include`` take this first and refuse to
    write when it fails.

    Returns the snapshot's path, or ``None`` when the file does not exist yet (a cold
    start has nothing to lose).  RAISES rather than answering quietly, because the caller
    must not write without it: ``ValueError`` when the engine's ring has changed shape or
    refuses the path (it takes only ``_local/``, ``plans/`` and a few named files, never
    one inside the vault), ``OSError`` when the ring will not import or the copy fails.

    The engine module is imported here, at call time, and never at the top of this
    module: this is the plug-in's one engine importer (G7), and a ring that fails to
    import must cost only this backup, never the daily run's contract check.
    """
    try:
        for dotted, expected in LOCAL_SNAPSHOT_SIGNATURES.items():
            actual = signature_text(_lookup(dotted))
            if actual != expected:
                raise ValueError(
                    f"the engine's local undo ring changed ({dotted} is now {actual}, "
                    f"expected {expected}), so nothing was backed up"
                )
        snapshot = _lookup("local_snapshot.snapshot")
    except ImportError as exc:
        raise OSError(f"the engine's local undo ring would not import ({exc})") from exc
    except AttributeError as exc:
        raise ValueError(f"the engine's local undo ring is not there any more ({exc})") from exc
    made = snapshot(Path(path))
    return Path(made) if made is not None else None


def proposal_detail(conn: sqlite3.Connection, proposal_id: str | None) -> dict[str, Any] | None:
    """One proposal as ``{id, kind, status, person_id, name, identifier, candidates}``.

    Read from the row and its JSON payload (never the payload bytes as a key, E3).
    ``None`` for no such row.  An unreadable payload reads as an empty one.
    """
    if not proposal_id:
        return None
    row = conn.execute(
        "SELECT id, kind, status, person_id, payload FROM person_proposal WHERE id = ?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[4])
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    candidates = payload.get("candidates")
    return {
        "id": str(row[0]),
        "kind": str(row[1]),
        "status": str(row[2]),
        "person_id": str(row[3] or payload.get("person_id") or "") or None,
        "name": payload.get("name") if isinstance(payload.get("name"), str) else None,
        "identifier": payload.get("identifier"),
        "candidates": [str(c) for c in candidates] if isinstance(candidates, list) else [],
    }
