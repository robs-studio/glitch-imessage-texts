"""iMessage intake — the pipeline: read what arrived, file it as queued days, land the stale ones.

``imessage.py`` is the CLI; this module is what its ``daily`` verb actually runs, and its
:class:`Pipeline` is what ``imbackfill`` drives over an older window.  It is deterministic
from end to end and never
calls a model: the morning stage gives it about twenty seconds, kills it with a
SIGKILL when it overruns, and reads nothing back but one JSON line, so the summary a
person's card deserves is written later, in a session (``imsynth``), and this run
only QUEUES the day.  The one thing it stamps is the safety net: a queued day that
has waited longer than ``synth_stale_days`` lands with its plain, mechanical line,
so no day is ever lost to a summary backlog.

The order of one run, as built
------------------------------
1. **Gates**, each one plain sentence and NOTHING written: not a Mac · no own
   handles (``imconfig``'s refusal) · no Full Disk Access (naming the app) · the
   engine changed shape (``imspine.check_contract``) · the ledger lock is busy · the
   ledger or ``state.json`` is damaged.  A paused run loses no day, because the
   watermark only moves after a clean one.
2. **Recovery**: every stamp ``intent`` left open by a run that never committed is
   re-checked against its card by the exact pointer line.  On the card → ``done``,
   and that card is projected again in THIS run (its projection may have been
   rolled back with the run that wrote it).  Not on the card → the intent is
   dropped; the queue entry from the earlier committed run still stands.
3. **Read** what arrived since the watermark (below), then re-read each touched
   closed conversation-day IN FULL, by date.
4. **Group → floor → fold** to person-days (``imthreads.units_for``'s steps).
5. **Write every constituent chat-day's thread file** — before anything can point at
   it, so a link on a card always opens.
6. **Resolve** each counterpart once per run (``imspine.resolve``), then **queue**
   (resolved) or **hold** (anything else) its person-days.  A number that resolves
   releases every day held against it into the queue in the same run.
7. **Stale fallback** through :func:`land`, the one stamping path.
8. **Project** every card stamped or recovered this run, INSIDE the transaction
   (G1), then ``conn.commit()``, then the ledger's ``commit_run``, ``compact``, and
   ``state.json`` with the watermark LAST.  One connection, one commit, closed in a
   ``finally``.

The watermark is a ROWID, not a date
------------------------------------
``message.ROWID`` is arrival order (``AUTOINCREMENT`` in the Messages schema, so never
handed out twice); ``message.date`` is when the phone sent it.  A message the phone
sent while the Mac slept arrives with an EARLIER date than rows already read, and a
date watermark would never read it: a day lost, silently.  So each run reads the
rows with ``ROWID > watermark``, collects the local days they touch, and re-reads each
touched CLOSED day in full by date — every conversation on it, not only the one the new
row landed in, so a person-day is never rebuilt from part of its day.  A row in today's
still-open day is not consumed: the watermark stops just below the lowest such row,
and just below the lowest row of any touched day the budget did not reach.  The
first-ever run reads yesterday only (older history is ``backfill``) and places the
watermark just below today's first row.  Re-reading is harmless by construction: a
thread file is rewritten complete, a queued day keeps its frozen record (only its list
of transcripts may grow, through ``imledger.refresh_links``, when a late chat adds a
conversation to the day, so the summary writer reads all of it), and a stamped day
keeps its one line.

Day boundaries are drawn in the CONFIGURED zone (``config.now_local()``), never
wherever the Mac happens to be; the report and ``status`` name the zone in force.

The member is never a counterpart
---------------------------------
Twice over.  Upstream, ``imthreads`` files the member's own turns (``is_from_me``
and ``own_handles``) as the member's, so no person-day is ever built for them.
Downstream, the engine has no notion of "the owner's person" (its owner filter is
email-only), so :func:`owner_person_ids` asks the resolver, dry, which cards the
member's own handles already resolve to — the member's own card carries their
addresses — and nothing is ever queued or stamped onto one of those cards, even for a handle
``own_handles`` does not list.

Resolution policy (the build contract, as the member ruled it on 2026-09-24)
---------------------------------------------------------------------------
* **A number NEW to the ledger never raises a card on its own.**  ``daily`` looks it
  up DRY (``emit=False``) and HOLDS its days on the plug-in's own review list, exactly
  as ``backfill`` does: new numbers "wait on a texts review list, never on your main
  people queue" (the member).  No card carried a phone yet, and one day of texts alone
  would have put a flood of proposals on the main queue, the flood THE WHY names as this
  feature's first failure.  ``emit_new`` stays a parameter so CP6's ``review`` can raise
  a card on the member's word.  The engine is still asked whether a card for that number is already
  pending or was dismissed (keyed on the identifier, never on payload equality,
  E3/E4), so a card raised elsewhere is linked, never siblinged.
* A number the ledger already knows (pending, held, ambiguous, dismissed,
  attach_pending) is looked up DRY until it resolves.
* **E1 follow-through** (it stays, by the same ruling): a pending ``new_stub`` the
  member accepted on the main queue created a person WITHOUT the number, so the run
  raises the ``add_identifier`` that attaches it (the second yes; a proposal they
  accepted must still get its number) and waits in ``attach_pending``; only a later
  ``resolved`` releases the held days.  It raises only when a dry look shows that card
  would name exactly the new person, so a renamed card never mints a sibling.
* **Reopen**: a dismissed number stays dismissed.  ``daily`` never re-opens one on its
  own (``reopen=False``); the re-open rule (texted on a day strictly after the
  dismissal day) applies only when a caller turns it on, on the member's word.
* **Ambiguous** (a shared number): held, never guessed; nothing is raised.
* **One bound for when raising is on** (``emit_new=True``): a new number whose newest
  day is older than :data:`RECENT_DAYS` is still held for review.  The ROWID watermark
  means an iCloud re-sync can deliver a year of history as "new arrivals".
* The report counts the numbers on the review list (``awaiting_review``) beside the
  ones waiting on a yes on the main queue (``awaiting_yes``), for the morning line.
* **Answers given in ``review`` stand** (``imreview``, CP6): a number dismissed there
  with no engine card behind it (a shared number, a business ID) stays dismissed; a
  person added there waits in ``attach_pending`` for the second yes that attaches
  the number, never reverting to "new"; and a number the member said reached the
  WRONG card by its last digits (``hold_reason: wrong_match``) is held while that
  card is still the only one it reaches.  Every resolved number records HOW it
  matched (``match``: ``exact`` or ``suffix``), so ``review`` can show each suffix
  match for the member to check (E5b).

The copy a backfill owes each card (G2, KNOWN LIMIT 10)
------------------------------------------------------
Every stamp puts the card's previous version into the engine's memory undo ring, which
keeps thirty.  A backfill lands hundreds of lines on its busiest cards, so after one,
"undo that memory change" reaches only a mid-backfill version.  The engine's own two
rings cannot help: ``local_snapshot`` refuses any file inside the vault, and
``memory_snapshot`` writes into the very ring the backfill then rolls over.  So before
the FIRST backfilled line lands on a card, :func:`land` copies that card byte for byte
into ``pre-backfill/<slug>.md`` in the plug-in home (owner-only, atomic, never
overwritten), and that copy, not the undo ring, is the way back.  It lives in
:func:`land` and is keyed on the unit's ``origin`` (``backfill``), because a backfilled
day can reach a card by three roads (the stale fallback here, a summary commit in
``imsynth``, a number ``review`` releases), and all three stamp through :func:`land`.
A copy that cannot be taken stops that day's line (it stays queued, the reason recorded
for ``status``): a backfilled line with no way back is the one thing this must not write.

Dry run
-------
``daily(dry_run=True)`` reads, groups and resolves with ``emit=False`` and reports
what would happen.  No thread file, no ledger line, no watermark, no DB write, and
it does not even create the ledger's lock file: the ledger is read under the lock
only if that lock file already exists.
"""

from __future__ import annotations

# imconfig FIRST, before any engine module: it puts `.claude/scripts` on sys.path and
# then re-asserts this folder ahead of it. See imconfig's docstring.
import imconfig

imconfig.ensure_engine_path()

import contextlib  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from collections import Counter  # noqa: E402
from collections.abc import Iterable, Iterator, Mapping, Sequence  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import date, datetime, timedelta, tzinfo  # noqa: E402
from datetime import time as dtime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Protocol  # noqa: E402

import imchat  # noqa: E402
import imcontacts  # noqa: E402
import imledger  # noqa: E402
import imspine  # noqa: E402
import imthreads  # noqa: E402

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: How long a run waits for the ledger lock before saying "busy" and stopping.  Short
#: on purpose: the morning stage has about twenty seconds in all, and a second run
#: that is still going is a reason to stop, not to queue up behind it.
LOCK_TIMEOUT_S: float = 2.0

#: The ``watermark_kind`` written beside the watermark.  A state file without it (or
#: with another kind) is refused rather than guessed at: a date-sized integer read as
#: a row number would silently mean "nothing ever again".
WATERMARK_KIND: str = "rowid"

#: A NEW number whose newest conversation-day is older than this many days is held
#: for review rather than raised, even by ``daily``.  See the module docstring.
RECENT_DAYS: int = 7

#: Identifier states a sweep revisits when no new day of theirs arrived: the ones
#: waiting on the member (their yes may have landed) and any holding days.
_SWEEP_STATES: frozenset[str] = frozenset({"pending", "attach_pending"})

#: The projector reads ``(→`` in a topic as the start of the link.
_ARROW_OPEN = re.compile(r"\(\s*→")

#: The folder under the plug-in home that holds each card as it was before its first
#: backfilled line (G2).  Gitignored and kept-local, like ``threads/``: a card is private.
PRE_BACKFILL_DIRNAME: str = "pre-backfill"

#: The ``origin`` every unit a backfill reads carries, onto the queue and the hold list
#: alike.  :func:`land` keys the pre-backfill copy on it.
ORIGIN_BACKFILL: str = "backfill"

#: A copy's file name is the card's own: a plain name, never a path, never hidden.
_COPY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")

SENTENCE_BUSY: str = (
    "texts: another texts run is using the ledger right now, so I left everything as "
    "it was; the next run picks up from the same place."
)
SENTENCE_STATE_KIND: str = (
    "texts paused: state.json carries a watermark this version does not recognise "
    "(it is not a message row number), so I read nothing rather than guess; move "
    "state.json aside and the next run starts again from yesterday."
)
SENTENCE_MEMORY_BUSY: str = (
    "texts: the memory database was busy for longer than I can wait, so I undid this "
    "run and left everything where it was; the next run picks up from the same place."
)


def _warn(message: str) -> None:
    """One diagnostic line on stderr.  Stdout belongs to the report."""
    print(f"[imessage] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Message sources.  Injectable, so a test never reads the member's chat.db.
# ---------------------------------------------------------------------------


class SourceUnreadable(Exception):  # noqa: N818 (a state, not a programming error)
    """The message store failed mid-read.  The run stops and nothing advances."""


class MessageSource(Protocol):
    """What the pipeline needs from a message store — and nothing else."""

    def on_mac(self) -> bool:
        """False on a machine that has no Messages database at all."""

    def access(self) -> tuple[bool, str]:
        """``(readable, one plain sentence)`` — the Full Disk Access gate."""

    def opened(self) -> contextlib.AbstractContextManager[Any]:
        """Hold the store open for one run."""

    def row_refs(self, after_rowid: int) -> list[imchat.RowRef]:
        """Every qualifying row that arrived after ``after_rowid``."""

    def rowid_bounds(self, since: datetime | None) -> tuple[int | None, int | None]:
        """``(lowest ROWID dated at/after since, highest ROWID)``."""

    def messages_between(self, since: datetime, until: datetime) -> list[imchat.Message]:
        """Every qualifying message in ``[since, until)``, oldest first."""


class ChatDbSource:
    """The member's ``chat.db``, read through :mod:`imchat` (``mode=ro``, never a copy).

    Every read is STRICT: a failure raises :class:`SourceUnreadable` instead of
    reading as "nothing there", because this run moves a watermark on what it read.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._conn: sqlite3.Connection | None = None

    def on_mac(self) -> bool:
        return bool(imchat._is_macos())

    def access(self) -> tuple[bool, str]:
        return imchat.has_access(self.path)

    @contextlib.contextmanager
    def opened(self) -> Iterator[ChatDbSource]:
        try:
            self._conn = imchat.connect(self.path)
        except sqlite3.Error as exc:
            raise SourceUnreadable(type(exc).__name__) from exc
        try:
            yield self
        finally:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            self._conn = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("the message store is not open; use `with source.opened():`")
        return self._conn

    def row_refs(self, after_rowid: int) -> list[imchat.RowRef]:
        try:
            return list(imchat.iter_row_refs(self._db(), after_rowid))
        except sqlite3.Error as exc:
            raise SourceUnreadable(type(exc).__name__) from exc

    def rowid_bounds(self, since: datetime | None) -> tuple[int | None, int | None]:
        try:
            return imchat.rowid_bounds(self._db(), since)
        except sqlite3.Error as exc:
            raise SourceUnreadable(type(exc).__name__) from exc

    def messages_between(self, since: datetime, until: datetime) -> list[imchat.Message]:
        try:
            return list(imchat.iter_messages(self._db(), since, until, strict=True))
        except sqlite3.Error as exc:
            raise SourceUnreadable(type(exc).__name__) from exc


class ListSource:
    """An in-memory store: ``[(rowid, Message), ...]``.  For tests and dry sketches.

    It answers exactly what :class:`ChatDbSource` answers over the same rows: arrival
    by ROWID, windows by date, oldest first.
    """

    def __init__(self, rows: Iterable[tuple[int, imchat.Message]] = ()) -> None:
        self.rows: list[tuple[int, imchat.Message]] = list(rows)

    def add(self, rowid: int, message: imchat.Message) -> None:
        self.rows.append((int(rowid), message))

    def on_mac(self) -> bool:
        return True

    def access(self) -> tuple[bool, str]:
        return True, "texts: reading an in-memory store."

    @contextlib.contextmanager
    def opened(self) -> Iterator[ListSource]:
        yield self

    def row_refs(self, after_rowid: int) -> list[imchat.RowRef]:
        return [
            imchat.RowRef(rowid, m.chat_rowid, m.date_raw, m.dt_local)
            for rowid, m in self.rows
            if rowid > after_rowid
        ]

    def rowid_bounds(self, since: datetime | None) -> tuple[int | None, int | None]:
        if not self.rows:
            return None, None
        dated = [rowid for rowid, m in self.rows if since is None or m.dt_local >= since]
        return (min(dated) if dated else None), max(rowid for rowid, _ in self.rows)

    def messages_between(self, since: datetime, until: datetime) -> list[imchat.Message]:
        found = [m for _, m in self.rows if since <= m.dt_local < until]
        return sorted(found, key=lambda m: (m.dt_local, m.date_raw))


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------


def _day_start(day: date, zone: tzinfo | None) -> datetime:
    """Local midnight at the START of ``day`` in the configured zone."""
    return datetime.combine(day, dtime.min, tzinfo=zone)


def _parse_moment(value: Any, zone: tzinfo | None) -> datetime | None:
    """An ISO timestamp as an aware datetime (a naive one is read in ``zone``)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment


def _zone_name(moment: datetime) -> str:
    key = getattr(moment.tzinfo, "key", None)
    if isinstance(key, str) and key:
        return key
    return moment.tzname() or str(moment.tzinfo)


def _budget_deadline(
    started: float, budget_s: float | None, cfg: Mapping[str, Any]
) -> float | None:
    """The monotonic moment to stop starting new work, or ``None`` for no clock.

    Paced on ``GLITCH_BUDGET_S`` — the seconds the morning stage ACTUALLY granted,
    which may be less than the declared timeout — minus ``budget_margin_s``, so the
    ledger and the watermark are written whole before any SIGKILL.  A manual run
    (no variable set) has no clock; ``max_stamps_per_run`` still bounds it.
    """
    if budget_s is None:
        raw = os.environ.get("GLITCH_BUDGET_S")
        if raw is None or not raw.strip():
            return None
        try:
            budget_s = float(raw)
        except ValueError:
            _warn(f"GLITCH_BUDGET_S is not a number ({raw!r}); pacing on 20 seconds.")
            budget_s = 20.0
    try:
        margin = float(cfg.get("budget_margin_s", imconfig.DEFAULTS["budget_margin_s"]))
    except (TypeError, ValueError):
        margin = float(imconfig.DEFAULTS["budget_margin_s"])
    return started + max(0.0, float(budget_s) - margin)


def _past(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _int_cfg(cfg: Mapping[str, Any], key: str) -> int:
    default = int(imconfig.DEFAULTS[key])
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        _warn(f"{key} in your config is not a number; using the default ({default}).")
        return default


def safe_topic(topic: str) -> str:
    """The topic with anything the projector would read as a link neutralised.

    ``people_index._LINK_RE`` starts at ``(`` + optional space + ``→``; a body
    quoting one would hijack the stored link, and ``imspine.stamp`` refuses such a
    topic outright — so a mechanical topic that happened to quote one would fail
    every morning for ever.  ``(->`` reads the same to a person.
    """
    return _ARROW_OPEN.sub("(->", topic).strip() or "Texts"


def _primary_link(record: Mapping[str, Any]) -> str | None:
    links = record.get("links")
    if isinstance(links, list) and links and isinstance(links[0], str):
        return links[0]
    link = record.get("link")
    return link if isinstance(link, str) else None


# ---------------------------------------------------------------------------
# The member's own card.
# ---------------------------------------------------------------------------


def owner_person_ids(conn: sqlite3.Connection, own_handles: Iterable[str]) -> frozenset[str]:
    """The person ids the member's own handles already resolve to — never a counterpart.

    The engine has no owner person; the member's own card simply carries their addresses.
    So each own handle is looked up DRY (``emit=False`` writes nothing), and only a
    ``resolved`` answer counts: a shared or ambiguous handle would otherwise wall off
    a family member's card along with the member's own.
    """
    owners: set[str] = set()
    for handle in own_handles:
        res = imspine.resolve(conn, handle, None, emit=False)
        if res.status == "resolved" and res.person_id:
            owners.add(str(res.person_id))
    return frozenset(owners)


# ---------------------------------------------------------------------------
# The pre-backfill copy (G2) — taken inside land(), before a backfilled line.
# ---------------------------------------------------------------------------


def pre_backfill_dir(home: Path | str) -> Path:
    """``<home>/pre-backfill``: each card as it was before its first backfilled line."""
    return Path(home) / PRE_BACKFILL_DIRNAME


def _chmod_600(path: Path) -> None:
    """Owner-only where the OS means it; a silent no-op where it does not (Windows)."""
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(path, 0o600)


def _folded(path: Path) -> str:
    """A resolved path for comparing, case-folded as APFS and NTFS compare names."""
    return str(path).casefold()


def _checked_copy_dir(home: Path | str) -> Path:
    """``pre-backfill/``, made owner-only if absent, refused if it resolves anywhere but
    directly inside the home (a planted link would otherwise carry every copy out)."""
    home_dir = Path(home).resolve()
    base = pre_backfill_dir(home_dir)
    if base.is_symlink():
        raise ValueError(f"refusing {base}: it is a link, and the copies live only in the home")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = base.resolve()
    if _folded(resolved.parent) != _folded(home_dir) or resolved.name != PRE_BACKFILL_DIRNAME:
        raise ValueError(f"refusing {resolved}: the copies live only in {base}")
    return resolved


def _sweep_copy_temps(base: Path) -> None:
    """Remove temp files a killed copy left behind.  Safe under the ledger lock: every
    stamp (and so every copy) happens inside a ledger session, one at a time."""
    for stray in base.glob(".*.md.*.tmp"):
        if stray.is_file() and not stray.is_symlink():
            with contextlib.suppress(OSError):
                stray.unlink()


def keep_pre_backfill_copy(card_path: Path | str, home: Path | str) -> Path | None:
    """Copy a card, byte for byte, into ``pre-backfill/<slug>.md``, once and never again.

    G2 / KNOWN LIMIT 10: called by :func:`land` before the first line a backfill lands
    on a card, so the member has the card exactly as it was before the backfill began,
    outside both undo rings (the backfill rolls the memory ring over; the local ring
    refuses vault files).  Returns the new copy's path, or ``None`` when a copy is
    already there: an existing copy is NEVER overwritten, because it is the card before
    the FIRST backfilled line and any later copy would already carry some.

    The write: the card is read first (a card that cannot be read leaves no file at
    all), into a temp file beside the target, fsynced, owner-only, then published with
    ``os.link``, which is atomic AND refuses a name that already exists, so a kill
    leaves the copy whole or absent, never half written and then trusted for ever.  A
    filesystem with no hard links falls back to ``os.replace`` after an existence check,
    which is safe because every copy is taken under the ledger lock.

    Raises ``ValueError`` for a target that would not sit directly inside
    ``pre-backfill/`` in ``home`` (both sides resolved, so a planted link cannot move
    it) or that is already there as something other than a plain file, and
    ``OSError`` when the card cannot be read or the copy cannot be written.  Either
    way the caller writes no backfilled line.
    """
    source = Path(card_path)
    name = source.name
    if not _COPY_NAME.match(name):
        raise ValueError(f"refusing to copy {name!r}: not a card's file name")
    base = _checked_copy_dir(home)
    target = base / name
    if _folded(target.parent.resolve()) != _folded(base):
        raise ValueError(f"refusing {target}: the copies live only directly inside {base}")
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError(f"refusing {target}: it is there but is not a plain file")
    if target.exists():
        return None
    _sweep_copy_temps(base)
    payload = source.read_bytes()  # before any temp file exists
    handle_fd, temp_name = tempfile.mkstemp(dir=str(base), prefix=f".{name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _chmod_600(temp)
        try:
            os.link(temp, target)
        except FileExistsError:
            return None
        except (AttributeError, NotImplementedError, OSError):
            if target.exists() or target.is_symlink():
                return None
            os.replace(temp, target)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()
    _chmod_600(target)
    return target


def _ledger_home(led: imledger.Ledger) -> Path | None:
    """The home a ledger session was opened on: where its card copies belong.

    ``imledger`` exposes no public name for it, and this builder does not own that
    module, so it is read off the session's own paths record.  ``None`` (never
    expected) makes :func:`land` refuse every backfilled line rather than guess.
    """
    home = getattr(getattr(led, "_paths", None), "home", None)
    return Path(home) if home is not None else None


# ---------------------------------------------------------------------------
# land() — THE one stamping path.
# ---------------------------------------------------------------------------


@dataclass
class LandResult:
    """What :func:`land` did.  Keys only — no names, no numbers, no words.

    ``stamped`` are the days the engine wrote; ``already_present`` the days whose
    exact line was already on the card (the ledger caught up, nothing written);
    ``failures`` the days the engine or the card refused (they stay queued, with
    the detail recorded for ``status``); ``refused`` the days that must never land
    (the member's own card), taken off the queue; ``pre_backfill`` the cards
    (vault-relative) whose pre-backfill copy was taken in this call (G2).
    """

    stamped: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)
    rels: list[str] = field(default_factory=list)
    pre_backfill: list[str] = field(default_factory=list)
    projection: Any = None
    stopped: str | None = None
    remaining: int = 0

    @property
    def projection_skipped(self) -> list[str]:
        return list(getattr(self.projection, "skipped_paths", []) or [])


def land(
    conn: sqlite3.Connection,
    led: imledger.Ledger,
    items: Iterable[tuple[Mapping[str, Any], str]],
    *,
    owner_ids: Iterable[str] | None = None,
    deadline: float | None = None,
    limit: int | None = None,
    now: datetime | None = None,
    topic_kind: str = "summary",
    home: Path | str | None = None,
) -> LandResult:
    """Stamp queued days onto their cards: the ONE path a card is ever written by.

    ``items`` are ``(queued record, topic)`` pairs — the mechanical topic for the
    stale fallback, a written summary for ``imsynth.commit``.  Per item:
    ``stamp_intent`` (the write-ahead, BEFORE the card is touched) →
    :func:`imspine.stamp` → on ``stamped`` or ``already_present``, ``stamp_done``;
    otherwise ``record_failure(detail)`` and ``drop_intent``, and the day stays
    queued for the next run.  ``stamped=False`` is never read as "done" unless the
    exact line is really on the card.

    A record whose ``origin`` is ``backfill`` first has its card copied into
    ``pre-backfill/`` (:func:`keep_pre_backfill_copy`, G2), before the intent and
    before the card is touched; a copy already there is kept as it is.  ``home`` is
    where that folder lives (default: the home this ledger session was opened on).
    A copy that cannot be taken is recorded as that day's failure and the day stays
    queued: no backfilled line is ever written without its way back.

    The cards stamped (and caught up) are then projected with
    ``imspine.project(conn, rels)`` — inside the caller's transaction.  **THE CALLER
    COMMITS AFTER** (G1: a close without that commit rolls the projection back
    while the lines stay on the cards).  A ledger run must be open (the ledger
    refuses any change outside one).

    Bounded by ``limit`` engine writes and by ``deadline`` (monotonic); what is left
    stays queued and ``stopped``/``remaining`` say so.  The member's own card is
    refused outright: ``owner_ids`` defaults to :func:`owner_person_ids` over the
    configured ``own_handles``, so a caller that forgets it is still safe.
    """
    result = LandResult()
    if owner_ids is None:
        owner_ids = owner_person_ids(conn, imthreads.owner_handles(imconfig.load_config()))
    owners = frozenset(owner_ids)
    moment = now if now is not None else imspine.now_local()
    copy_home = Path(home) if home is not None else _ledger_home(led)
    pending = list(items)

    for index, (record, topic) in enumerate(pending):
        if limit is not None and len(result.stamped) >= limit:
            result.stopped, result.remaining = "limit", len(pending) - index
            break
        if _past(deadline):
            result.stopped, result.remaining = "budget", len(pending) - index
            break

        key = str(record.get("key") or "")
        if not key:
            continue
        if led.is_stamped(key):
            result.skipped.append(key)
            continue
        person_id = record.get("person_id")
        if not person_id:
            result.refused[key] = "no person to file it under"
            continue
        if person_id in owners:
            # The member's own card: never a counterpart.  Off the queue for good.
            led.dequeue(key)
            result.refused[key] = "that is your own card"
            continue
        card = imspine.person_path(conn, str(person_id))
        if card is None:
            detail = "the person's card could not be found"
            led.record_failure(key, detail)
            result.failures[key] = detail
            continue
        if record.get("origin") == ORIGIN_BACKFILL:
            # G2: the card as it was before its first backfilled line, taken BEFORE the
            # intent, so a retry after any crash finds the copy already made.
            refusal = _take_pre_backfill_copy(card, copy_home, result)
            if refusal is not None:
                led.record_failure(key, refusal)
                result.failures[key] = refusal
                continue

        parsed = imthreads.parse_ledger_key(key)
        day = str(record.get("day") or (parsed[2].isoformat() if parsed else ""))
        direction = str(record.get("direction") or imthreads.MUTUAL)
        line_topic = safe_topic(str(topic))
        link = _primary_link(record)
        intent = {
            "person_id": str(person_id),
            "identifier": record.get("identifier"),
            "day": day,
            "direction": direction,
            "topic": line_topic,
            "link": link,
            "turns": record.get("turns"),
            "rel": card.rel,
            "topic_kind": topic_kind,
        }
        try:
            led.stamp_intent(key, intent)
        except imledger.AlreadyStampedError:
            result.skipped.append(key)
            continue
        except imledger.IntentOpenError:
            detail = "an earlier attempt at this day is still waiting to be checked"
            led.record_failure(key, detail)
            result.failures[key] = detail
            continue

        out = imspine.stamp(
            card.rel, occurred_at=day, direction=direction, topic=line_topic, link=link,
            conn=conn,
        )
        if out.stamped or out.already_present:
            led.stamp_done(key, {"stamped_at": moment.isoformat(),
                                 "found_on_card": bool(not out.stamped)})
            (result.stamped if out.stamped else result.already_present).append(key)
            if card.rel not in result.rels:
                result.rels.append(card.rel)
        else:
            led.record_failure(key, out.detail)
            led.drop_intent(key)
            result.failures[key] = out.detail

    if result.rels:
        result.projection = imspine.project(conn, result.rels)
    return result


def _take_pre_backfill_copy(
    card: imspine.PersonCard, home: Path | None, result: LandResult
) -> str | None:
    """Make sure ``card`` has its pre-backfill copy; ``None`` when it does, else why not.

    The reason is plain words for ``status`` and never quotes the card.
    """
    if home is None:
        return ("its card's copy from before the backfill has nowhere to go (the plug-in "
                "home is unknown), so its backfilled line was not written")
    try:
        made = keep_pre_backfill_copy(card.path, home)
    except (OSError, ValueError) as exc:
        return ("its card's copy from before the backfill could not be taken "
                f"({type(exc).__name__}), so its backfilled line was not written")
    if made is not None and card.rel not in result.pre_backfill:
        result.pre_backfill.append(card.rel)
    return None


# ---------------------------------------------------------------------------
# Recovery.
# ---------------------------------------------------------------------------


def recover(conn: sqlite3.Connection, led: imledger.Ledger) -> tuple[int, int, list[str]]:
    """Settle every open stamp intent against its card.  ``(done, dropped, rels)``.

    An intent is the ledger saying "I was about to write this exact line".  The run
    that wrote it never committed, so the card is the only witness: the exact
    pointer line there → ``stamp_done`` (and the card goes back into this run's
    projection, since the projection it had may have been rolled back); not there →
    ``drop_intent``, leaving the day queued as the last committed run left it.
    """
    done, dropped, rels = 0, 0, []
    for key, record in led.open_intents().items():
        parsed = imthreads.parse_ledger_key(key)
        day = str(record.get("day") or (parsed[2].isoformat() if parsed else ""))
        person_id = record.get("person_id")
        card = imspine.person_path(conn, str(person_id)) if person_id else None
        target = card.rel if card is not None else record.get("rel")
        present = bool(target) and imspine.has_pointer(
            str(target),
            occurred_at=day,
            direction=str(record.get("direction") or ""),
            topic=record.get("topic"),
            link=record.get("link"),
        )
        if present:
            led.stamp_done(key, {"recovered": True})
            done += 1
            if str(target) not in rels:
                rels.append(str(target))
        else:
            led.drop_intent(key)
            dropped += 1
    return done, dropped, rels


# ---------------------------------------------------------------------------
# The pipeline.
# ---------------------------------------------------------------------------


@dataclass
class Tally:
    """Counts only, for the report.  No names, no numbers, no words."""

    rows_scanned: int = 0
    rows_open_day: int = 0
    messages_read: int = 0
    days_read: int = 0
    days_left: int = 0
    conversation_days: int = 0
    person_days: int = 0
    threads_written: int = 0
    queued: int = 0
    released: int = 0
    already_queued: int = 0
    links_refreshed: int = 0
    already_stamped: int = 0
    owner_skipped: int = 0
    identifiers: int = 0
    new_for_review: int = 0
    held: Counter = field(default_factory=Counter)
    raised: Counter = field(default_factory=Counter)
    would_raise: Counter = field(default_factory=Counter)
    would_reopen: int = 0
    recovered_done: int = 0
    recovered_dropped: int = 0
    open_intents: int = 0
    stale_due: int = 0
    stale_stamped: int = 0
    stale_found_on_card: int = 0
    stale_refused: int = 0
    stale_remaining: int = 0
    stale_failures: list[str] = field(default_factory=list)
    stale_pre_backfill: int = 0
    projected: int = 0
    projection_skipped: int = 0
    stopped: set[str] = field(default_factory=set)
    #: transcripts a dry run WOULD write (a real run counts ``threads_written``)
    threads_planned: int = 0
    #: the people (person ids) whose days were queued, or would be
    queued_people: set[str] = field(default_factory=set)
    #: the numbers that had at least one day held this run, or would have
    held_idents: set[str] = field(default_factory=set)


@dataclass
class _Decision:
    outcome: str  # "queue" | "hold" | "owner"
    person_id: str | None = None
    reason: str | None = None
    updates: dict[str, Any] = field(default_factory=dict)
    raised: str | None = None


def _hold(reason: str, raised: str | None = None, **updates: Any) -> _Decision:
    return _Decision("hold", reason=reason, updates=updates, raised=raised)


class Pipeline:
    """One run's worth of read → group → fold → write → resolve → queue/hold → land.

    ``daily`` and ``backfill`` (``imbackfill``) both drive it with ``emit_new=False,
    reopen=False`` (the member's ruling, 2026-09-24: a new number waits on the texts review
    list, never on the main people queue); ``review`` (CP6) may turn either on, on
    the member's word.  A backfill also turns ``follow_through`` off, so it raises
    nothing on the people queue at all, and sets ``origin="backfill"``: every unit
    it reads carries that ``origin`` onto the queue or the hold list, which is how
    :func:`land` knows to copy a card before its first backfilled line (G2).  In
    ``dry_run`` it performs no write of any kind: every ledger change, engine emit,
    re-open and thread write is skipped and counted as what WOULD happen (and the
    ledger itself refuses any change outside an open run, which a dry run never opens).
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        led: imledger.Ledger,
        cfg: Mapping[str, Any],
        own: frozenset[str],
        owner_ids: frozenset[str],
        contacts: Mapping[str, str],
        threads_dir: Path,
        now: datetime,
        deadline: float | None,
        dry_run: bool,
        emit_new: bool,
        reopen: bool,
        follow_through: bool = True,
        origin: str | None = None,
    ) -> None:
        self.conn, self.led, self.cfg = conn, led, cfg
        self.own, self.owner_ids = own, owner_ids
        self.contacts = dict(contacts)
        self.threads_dir = threads_dir
        self.now, self.zone, self.today = now, now.tzinfo, now.date()
        self.deadline = deadline
        self.dry_run, self.emit_new, self.reopen = dry_run, emit_new, reopen
        self.follow_through = follow_through
        #: written onto every unit record this run builds (``None``: not written at all,
        #: so a daily run's records are exactly what they were before backfill existed)
        self.origin = origin
        self.tally = Tally()
        #: identifier -> [unit record, ...] built this run (every day of theirs read)
        self.units: dict[str, list[dict[str, Any]]] = {}
        self.decided: set[str] = set()
        #: when the newest message this run read was sent (for ``state.json``)
        self.newest_read: datetime | None = None

    # -- phase A: read, group, fold, write the threads ------------------------

    def read_days(
        self, source: MessageSource, plan: Mapping[date, frozenset[int] | None],
        order: Sequence[date],
    ) -> list[date]:
        """Re-read each planned day in full, by date; build its person-day units.

        ``plan[day]`` is the set of conversations to keep for that day (``None`` =
        every conversation).  ``daily`` always passes ``None``: a late message in one
        conversation still re-reads the WHOLE day, because a person-day is folded from
        every conversation that person spoke in that day, and one rebuilt from only the
        touched conversation would replace a held day's record with a smaller one (its
        other threads' links and turns gone).  A queued day is frozen anyway; a held one
        is not, so the whole day is the only safe unit to re-read.  Returns the days NOT
        reached before the deadline.
        """
        left: list[date] = []
        for index, day in enumerate(order):
            if _past(self.deadline):
                left = list(order[index:])
                self.tally.stopped.add("budget")
                break
            self._read_day(source, day, plan.get(day))
        self.tally.days_left = len(left)
        return left

    def _read_day(
        self, source: MessageSource, day: date, chats: frozenset[int] | None
    ) -> None:
        start = _day_start(day, self.zone)
        until = _day_start(day + timedelta(days=1), self.zone)
        messages = [
            m for m in source.messages_between(start, until)
            if m.dt_local.date() == day and (chats is None or m.chat_rowid in chats)
        ]
        self.tally.days_read += 1
        self.tally.messages_read += len(messages)
        if not messages:
            return
        newest = max(m.dt_local for m in messages)
        if self.newest_read is None or newest > self.newest_read:
            self.newest_read = newest

        chat_days = imthreads.group(messages, own_handles=self.own)
        included = {
            key: cd for key, cd in chat_days.items() if imthreads.is_included(cd, self.cfg)
        }
        self.tally.conversation_days += len(included)
        person_days = {
            key: pd
            for key, pd in imthreads.fold_to_person_days(included).items()
            if imthreads.is_included(pd, self.cfg)
        }

        # Every constituent chat-day's transcript, written BEFORE anything can link to
        # it, so a pointer on a card always opens.
        links: dict[int, str] = {}
        for pd in person_days.values():
            for cd in pd.chat_days:
                if cd.chat_rowid in links:
                    continue
                if self.dry_run:
                    path = imthreads.thread_path(cd, self.contacts, self.threads_dir)
                    self.tally.threads_planned += 1
                else:
                    path = imthreads.write_thread(cd, self.contacts, self.threads_dir)
                    self.tally.threads_written += 1
                links[cd.chat_rowid] = imthreads.thread_link(path, self.threads_dir)

        for (identifier, _day), pd in sorted(person_days.items()):
            ident = imcontacts.canonicalise(identifier) or identifier
            if ident in self.own:
                continue  # belt and braces: imthreads never builds one
            self.tally.person_days += 1
            self.units.setdefault(ident, []).append(self._unit_record(ident, pd, links))

    def _unit_record(
        self, ident: str, pd: imthreads.PersonDay, links: Mapping[int, str]
    ) -> dict[str, Any]:
        """Everything needed to queue (or later release) this day without chat.db.

        The PRIMARY chat-day comes first: the one with the most of this person's own
        turns (a one-to-one before a group on a tie, since only its topic may quote an
        opener; then the earlier; then the lower ``chat_rowid``, for a stable answer).
        """
        ordered = sorted(
            pd.chat_days,
            key=lambda cd: (-len(cd.turns_by(ident)), cd.is_group,
                            cd.messages[0].dt_local, cd.chat_rowid),
        )
        primary = ordered[0]
        record = {
            "key": imthreads.ledger_key(ident, imthreads.DAY_GRAIN, pd.day),
            "identifier": ident,
            "name": self.contacts.get(ident),
            "day": pd.day.isoformat(),
            "links": [links[cd.chat_rowid] for cd in ordered],
            "direction": imthreads.direction_for(pd, ident),
            "turns": len(pd.turns),
            "their_turns": len(pd.turns_by(ident)),
            "messages": pd.message_count,
            "topic": safe_topic(imthreads.topic_for(primary)),
            "outbound": any(not cd.is_group and cd.my_turns for cd in pd.chat_days),
        }
        if self.origin:
            record["origin"] = self.origin
        return record

    # -- phase B: resolve each number once, queue or hold its days --------------

    def resolve_all(self) -> list[date]:
        """Decide every number read this run.  Returns days a deadline left undecided."""
        undecided_days: set[date] = set()
        for ident in sorted(self.units):
            units = self.units[ident]
            if _past(self.deadline):
                self.tally.stopped.add("budget")
                undecided_days.update(date.fromisoformat(u["day"]) for u in units)
                continue
            self.tally.identifiers += 1
            name = self.contacts.get(ident)
            outbound = [u["day"] for u in units if u.get("outbound")]
            evidence = max(outbound) if outbound else None
            newest = max(u["day"] for u in units)
            recent = newest >= (self.today - timedelta(days=RECENT_DAYS)).isoformat()
            decision = self.decide(ident, name, evidence, emit=self.emit_new and recent)
            self.apply(ident, name, units, decision)
            self.decided.add(ident)
        return sorted(undecided_days)

    def sweep(self) -> None:
        """Revisit known numbers with no new day this run: a yes may have landed.

        Numbers waiting on the member (``pending``, ``attach_pending``) and numbers
        holding days are looked up again — dry, apart from the E1 follow-through — so
        an accepted card releases its held days without that person texting again.
        """
        for ident, record in sorted(self.led.all_identifiers().items()):
            if ident in self.decided:
                continue
            state = record.get("state")
            if state == "accepted":
                continue
            if state not in _SWEEP_STATES and not self.led.held(ident):
                continue
            if _past(self.deadline):
                self.tally.stopped.add("budget")
                return
            decision = self.decide(ident, record.get("name"), None, emit=False)
            self.apply(ident, record.get("name"), [], decision)
            self.decided.add(ident)

    def decide(
        self, ident: str, name: str | None, evidence_day: str | None, *, emit: bool
    ) -> _Decision:
        """What this number's days do this run.  Engine writes happen here, if any."""
        conn = self.conn
        record = self.led.identifier(ident) or {}
        state = record.get("state")
        proposal_id = record.get("proposal_id")
        dry = imspine.resolve(conn, ident, name, emit=False)
        if dry.status == "resolved" and dry.person_id:
            if is_wrong_match(record, str(dry.person_id)):
                # The member said, in review, that this card is the wrong person: its
                # days stay held until the number reaches a DIFFERENT card.
                return _hold("wrong_match")
            return self._resolved(str(dry.person_id), ident)
        # Answers given on the texts review list (imreview) that carry no engine card of
        # their own stand until the number resolves.  Without these two, the next run
        # would read a "no" to a shared or not-a-number identifier as a first sighting
        # and put it straight back on the list, and would turn a person the member
        # added (waiting on the second yes to attach the number) back into "new".
        if state == "dismissed" and not proposal_id:
            return _hold("dismissed")
        if state == "attach_pending" and not proposal_id and record.get("person_id"):
            return _hold("attach_pending")
        if dry.status == "ambiguous":
            return _hold("ambiguous", state="ambiguous", proposal_id=None,
                         candidates=sorted(dry.candidates))
        if dry.status == "unresolved":
            return _hold("unresolved", state="held", hold_reason="unresolved")

        # The number is on no card.  What the ledger already knows comes first.
        if state in ("pending", "attach_pending") and proposal_id:
            proposal = imspine.proposal_state(conn, proposal_id)
            if proposal == "pending":
                return _hold(str(state))
            if proposal == "accepted":
                if state == "pending":
                    return self._follow_through(ident, name, record)
                return _hold("cannot_attach", state="held", hold_reason="cannot_attach")
            if proposal == "dismissed":
                return self._dismissed(proposal_id, evidence_day)
            # the card vanished from the engine: treat the number as newly seen
        elif state == "dismissed" and proposal_id:
            if imspine.proposal_state(conn, proposal_id) == "dismissed":
                return self._dismissed(proposal_id, evidence_day)
        elif state in ("held", "ambiguous"):
            # Known: looked up dry until it resolves; review (CP6) raises its card.
            return _hold(str(record.get("hold_reason") or state))
        return self._first_sighting(ident, name, evidence_day, emit, dry.proposal_kind)

    def _resolved(self, person_id: str, ident: str) -> _Decision:
        """Queue this number's days for ``person_id`` — and record HOW it matched.

        ``match`` is ``"exact"`` or ``"suffix"`` (:func:`imspine.match_route`).  A
        suffix match joins two numbers by their last seven or more digits, with no
        country table, so it can be a different person (E5b, KNOWN LIMIT 12); the
        route is kept on the identifier so ``review`` can show the member every card a
        number reached that way.
        """
        if person_id in self.owner_ids:
            return _Decision("owner")
        updates: dict[str, Any] = {"state": "accepted", "person_id": person_id}
        route, _card_value = imspine.match_route(self.conn, ident, person_id)
        if route is not None:
            updates["match"] = route
        return _Decision("queue", person_id=person_id, updates=updates)

    def _first_sighting(
        self, ident: str, name: str | None, evidence_day: str | None, emit: bool,
        would_kind: str | None,
    ) -> _Decision:
        conn = self.conn
        dismissed = imspine.dismissed_identifier(conn, ident)
        if dismissed is not None:
            return self._dismissed(dismissed, evidence_day)
        pending = imspine.pending_identifier(conn, ident)
        if pending is not None:
            return _hold("pending", state="pending", proposal_id=pending)
        if not emit:
            return _hold("new", state="held", hold_reason="new")
        if self.dry_run:
            self.tally.would_raise[would_kind or "new_stub"] += 1
            return _hold("pending")
        res = imspine.resolve(conn, ident, name, emit=True)
        if res.status == "resolved" and res.person_id:
            return self._resolved(str(res.person_id), ident)
        if res.status == "ambiguous":
            return _hold("ambiguous", state="ambiguous", proposal_id=None,
                         candidates=sorted(res.candidates))
        if res.status == "proposed" and res.proposal_id:
            if imspine.is_pending(conn, res.proposal_id):
                return _hold("pending", raised=res.proposal_kind, state="pending",
                             proposal_id=res.proposal_id, kind=res.proposal_kind)
            if imspine.proposal_state(conn, res.proposal_id) == "dismissed":
                return self._dismissed(res.proposal_id, evidence_day)
            return _hold("pending", state="pending", proposal_id=res.proposal_id,
                         kind=res.proposal_kind)
        return _hold("unresolved", state="held", hold_reason="unresolved")

    def _dismissed(self, proposal_id: str, evidence_day: str | None) -> _Decision:
        """A number the member said no to: silent, unless they have texted it since."""
        if (
            self.reopen
            and evidence_day
            and imspine.reopen_justified(self.conn, proposal_id, evidence_day)
        ):
            if self.dry_run:
                self.tally.would_reopen += 1
                return _hold("dismissed")
            if imspine.reopen_proposal(
                self.conn, proposal_id, "fresh texts after it was dismissed"
            ):
                return _hold("pending", raised="reopened", state="pending",
                             proposal_id=proposal_id)
        return _hold("dismissed", state="dismissed", proposal_id=proposal_id)

    def _follow_through(
        self, ident: str, name: str | None, record: Mapping[str, Any]
    ) -> _Decision:
        """E1: the member accepted a new card, which the engine made WITHOUT the number.

        Raise the ``add_identifier`` that attaches it — the second yes — but only when
        a dry look shows that card would name exactly the new person.  Anything else
        (a renamed card, a name now shared) waits, visibly, rather than minting a
        sibling card the member already said yes to once.
        """
        conn = self.conn
        new_person = imspine.accepted_person(conn, record.get("proposal_id"))
        if new_person is None:
            return _hold("accepted_unknown", state="pending")
        if new_person in self.owner_ids:
            return _Decision("owner")
        if not self.follow_through:
            return _hold("pending", state="pending")
        names = [n for n in dict.fromkeys((record.get("name"), name)) if n]
        for candidate_name in names:
            dry = imspine.resolve(conn, ident, candidate_name, emit=False)
            if dry.status == "resolved" and dry.person_id:
                return self._resolved(str(dry.person_id), ident)
            if not (dry.status == "proposed" and dry.proposal_kind == "add_identifier"
                    and list(dry.candidates) == [new_person]):
                continue
            if self.dry_run:
                self.tally.would_raise["add_identifier"] += 1
                return _hold("attach_pending")
            res = imspine.resolve(conn, ident, candidate_name, emit=True)
            if res.status == "proposed" and res.proposal_id:
                if imspine.is_pending(conn, res.proposal_id):
                    return _hold("attach_pending", raised="add_identifier",
                                 state="attach_pending", proposal_id=res.proposal_id,
                                 kind="add_identifier", person_id=new_person,
                                 stub_proposal_id=record.get("proposal_id"))
                answer = imspine.proposal_state(conn, res.proposal_id)
                if answer == "dismissed":
                    return self._dismissed(res.proposal_id, None)
                if answer == "accepted":
                    return _hold("cannot_attach", state="held", hold_reason="cannot_attach")
            break
        return _hold("accepted_unattached", state="pending", person_id=new_person)

    def apply(
        self, ident: str, name: str | None, units: list[dict[str, Any]], decision: _Decision
    ) -> None:
        """Carry one number's decision into the ledger (or, dry, into the counts)."""
        led, tally = self.led, self.tally
        if decision.raised:
            tally.raised[decision.raised] += 1

        if decision.outcome == "owner":
            tally.owner_skipped += len(units)
            if not self.dry_run:
                dropped = led.release(ident)  # the member's own texts never land
                tally.owner_skipped += len(dropped)
            return

        record = led.identifier(ident) or {}
        if (decision.outcome == "hold" and not record
                and decision.updates.get("state") in REVIEW_STATES):
            tally.new_for_review += 1  # a number first seen now, onto the review list
        new_days = [u for u in units if not self._seen(ident, u["key"])]

        if decision.outcome == "queue":
            person_id = str(decision.person_id)
            for unit in units:
                self._queue(unit, person_id)
            if not self.dry_run:
                for held in led.release(ident):
                    held.pop("reason", None)
                    if self._queue(held, person_id):
                        tally.released += 1
            else:
                tally.released += len(led.held(ident))
                tally.queued += len(led.held(ident))
                if led.held(ident):
                    tally.queued_people.add(person_id)
        else:
            reason = decision.reason or "held"
            existing = {u.get("key"): u for u in led.held(ident)}
            for unit in units:
                key = unit["key"]
                if led.is_stamped(key):
                    tally.already_stamped += 1
                    continue
                if led.is_queued(key):
                    tally.already_queued += 1
                    self._refresh_links(unit)
                    continue
                tally.held[reason] += 1
                tally.held_idents.add(ident)
                if self.dry_run:
                    continue
                before = existing.get(key) or {}
                led.hold(ident, {**unit, "reason": reason,
                                 "held_at": before.get("held_at") or self.now.isoformat()})

        if self.dry_run:
            return
        days = [u["day"] for u in units]
        updates: dict[str, Any] = dict(decision.updates)
        if "state" not in record and "state" not in updates:
            return  # nothing to record for a number never held and never resolved
        if name and name != record.get("name"):
            updates["name"] = name
        if days:
            first = min([*days, *([record["first_seen"]] if record.get("first_seen") else [])])
            last = max([*days, *([record["last_seen"]] if record.get("last_seen") else [])])
            updates.update(first_seen=first, last_seen=last,
                           days=int(record.get("days") or 0) + len(new_days))
        led.set_identifier(ident, **updates)

    def _seen(self, ident: str, key: str) -> bool:
        led = self.led
        if led.is_stamped(key) or led.is_queued(key):
            return True
        return any(u.get("key") == key for u in led.held(ident))

    def _refresh_links(self, unit: Mapping[str, Any]) -> None:
        """A re-read day already queued: add any transcript its record does not name.

        The record itself stays frozen (its topic and turns, E6); only its links
        grow, through ``imledger.refresh_links``, so the summary writer reads every
        conversation that person had that day, a late-syncing new chat included.
        The first link stays first.  Dry, it is only counted.
        """
        key = str(unit["key"])
        links = [link for link in unit.get("links") or [] if isinstance(link, str) and link]
        queued = self.led.queued_record(key)
        if not links or queued is None:
            return
        have = queued.get("links") if isinstance(queued.get("links"), list) else []
        if all(link in have for link in links):
            return
        if self.dry_run:
            self.tally.links_refreshed += 1
            return
        if self.led.refresh_links(key, links):
            self.tally.links_refreshed += 1

    def _queue(self, unit: Mapping[str, Any], person_id: str) -> bool:
        key = str(unit["key"])
        if self.led.is_stamped(key):
            self.tally.already_stamped += 1
            return False
        if self.led.is_queued(key):
            self.tally.already_queued += 1
            self._refresh_links(unit)
            return False
        self.tally.queued += 1
        self.tally.queued_people.add(person_id)
        if self.dry_run:
            return True
        record = {k: v for k, v in unit.items() if k not in ("reason",)}
        record.update(person_id=person_id, queued_at=self.now.isoformat())
        return self.led.enqueue(record)

    # -- the safety net ----------------------------------------------------------

    def stale(self, *, max_stamps: int) -> LandResult | None:
        """Queued days older than ``synth_stale_days`` with no live claim land now.

        With their mechanical topic, oldest day first, through :func:`land` — so a
        day is never lost to a summary backlog.  Bounded by ``max_stamps`` and the
        deadline; what is left waits for the next run.
        """
        stale_after = timedelta(days=_int_cfg(self.cfg, "synth_stale_days"))
        due: list[tuple[dict[str, Any], str]] = []
        for key, record in sorted(
            self.led.queued().items(), key=lambda kv: (str(kv[1].get("day", "")), kv[0])
        ):
            if self.led.is_stamped(key):
                continue
            queued_at = _parse_moment(record.get("queued_at"), self.zone)
            if queued_at is None or self.now - queued_at < stale_after:
                continue
            if self.led.live_claim(key, self.now) is not None:
                continue
            due.append((record, str(record.get("topic") or "Texts")))
        self.tally.stale_due = len(due)
        if self.dry_run or not due:
            return None
        result = land(
            self.conn, self.led, due, owner_ids=self.owner_ids, deadline=self.deadline,
            limit=max_stamps, now=self.now, topic_kind="mechanical",
        )
        self.tally.stale_stamped = len(result.stamped)
        self.tally.stale_found_on_card = len(result.already_present)
        self.tally.stale_refused = len(result.refused)
        self.tally.stale_remaining = result.remaining
        self.tally.stale_failures = sorted(set(result.failures.values()))
        self.tally.stale_pre_backfill = len(result.pre_backfill)
        if result.stopped:
            self.tally.stopped.add(result.stopped)
        if result.projection is not None:
            self.tally.projected += int(getattr(result.projection, "projected", 0) or 0)
            self.tally.projection_skipped += len(result.projection_skipped)
        return result


# ---------------------------------------------------------------------------
# The ledger, read without writing anything (dry run and status).
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _read_view(home: Path) -> Iterator[imledger.Ledger]:
    """The ledger for reading only — and it never creates the lock file to get it.

    The lock is taken when its file already exists (a real run made it), with the
    ledger's own stdlib lock, which opens without truncating; otherwise the read
    goes ahead unlocked.  Either way nothing is written: no run is opened, so the
    ledger refuses any change.
    """
    lock_path = imledger.paths(home).lock
    lock: contextlib.AbstractContextManager[Any]
    if lock_path.exists():
        lock = imledger.default_lock(home, timeout=LOCK_TIMEOUT_S)
    else:
        lock = contextlib.nullcontext()
    with imledger.session(lock, home=home) as led:
        yield led


def _no_checkpoint_on_close(conn: sqlite3.Connection) -> None:
    """Keep a dry run's close from rewriting ``memory.db``.

    Found on the member's machine, 2026-09-24: when nothing else holds the database,
    the dry run's connection is the LAST to close, and SQLite's default is then to
    checkpoint the write-ahead log into the main file and delete it.  No row changes,
    but both files do, and a dry run promises to write nothing.

    So when the WAL already holds frames, ``SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE``
    (Python 3.12+) turns that off for this one connection and the next real writer
    checkpoints as usual.  When it holds none (absent, or empty), the default is left
    alone ON PURPOSE: then the close folds nothing and deletes only the empty WAL and
    index this connection itself created, which puts the file set back exactly as it
    was — with the switch on, both would be left behind.  (``tests/test_imrun.py``
    pins both twins.)  On an interpreter without the switch, one stderr line says so.
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        main = Path(row[2]) if row and row[2] else None
    except sqlite3.Error:
        main = None
    try:
        has_frames = main is not None and main.with_name(main.name + "-wal").stat().st_size > 0
    except OSError:
        has_frames = False
    if not has_frames:
        return
    flag = getattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None)
    setconfig = getattr(conn, "setconfig", None)
    if flag is None or setconfig is None:
        _warn("this Python cannot stop SQLite checkpointing on close; a dry run may "
              "fold memory.db's write-ahead log into the main file (no row changes).")
        return
    try:
        setconfig(flag, True)
    except sqlite3.Error as exc:
        _warn(f"could not keep the dry run from checkpointing on close ({exc}).")


def _ledger_exists(home: Path) -> bool:
    p = imledger.paths(home)
    return p.ledger.exists() or p.wal.exists()


# ---------------------------------------------------------------------------
# daily()
# ---------------------------------------------------------------------------


def _engine_now() -> datetime:
    """The engine clock in the configured zone — or, if the engine will not even
    import, this machine's clock, so the run can still reach the contract gate and
    pause with its one plain sentence instead of a traceback."""
    try:
        return imspine.now_local()
    except Exception:  # noqa: BLE001 - the contract gate names what is wrong
        return datetime.now().astimezone()


def _new_report(dry_run: bool, now: datetime) -> dict[str, Any]:
    return {
        "verb": "daily",
        "dry_run": bool(dry_run),
        "paused": None,
        "zone": _zone_name(now),
        "today": now.date().isoformat(),
        "watermark": {"before": None, "after": None, "first_run": False,
                      "store_reset": False, "held_by_open_day": False,
                      "held_by_budget": False, "moved": False},
    }


def _pause(report: dict[str, Any], reason: str, sentence: str) -> dict[str, Any]:
    report["paused"] = {"reason": reason, "sentence": sentence}
    return report


def daily(
    *,
    dry_run: bool = False,
    source: MessageSource | None = None,
    now: datetime | None = None,
    cfg: Mapping[str, Any] | None = None,
    home: Path | str | None = None,
    threads_dir: Path | str | None = None,
    db_path: Path | str | None = None,
    contacts: Mapping[str, str] | None = None,
    budget_s: float | None = None,
    emit_new: bool = False,
    reopen: bool = False,
) -> dict[str, Any]:
    """The morning run.  Returns a plain-data report; never raises for a gate.

    Every keyword exists so a test (or the CP5 shim) can point the run somewhere
    other than the member's real files: ``source`` (default: ``chat.db`` via
    :class:`ChatDbSource`), ``now`` (default: the engine clock in the configured
    zone), ``cfg``, ``home`` (ledger + state), ``threads_dir``, ``db_path`` (the
    people database), ``contacts`` and ``budget_s`` (default: ``GLITCH_BUDGET_S``).

    ``emit_new`` and ``reopen`` are OFF by the member's ruling (2026-09-24): a number new to
    the ledger is held on the texts review list and a dismissed one stays dismissed,
    so the run itself never puts a proposal on the main people queue.  They exist for
    CP6's ``review``, which turns them on only on the member's word.

    A gate returns the report with ``paused = {reason, sentence}`` and nothing
    written; the caller prints the sentence and exits 0.  An unexpected exception is
    a bug: the ledger run is aborted, the database rolled back, and it propagates.
    """
    started = time.monotonic()
    cfg = dict(cfg) if cfg is not None else imconfig.load_config()
    home_dir = Path(home) if home is not None else Path(imconfig.HOME)
    threads = Path(threads_dir) if threads_dir is not None else Path(imconfig.THREADS_DIR)
    src: MessageSource = source if source is not None else ChatDbSource()
    moment = now if now is not None else _engine_now()
    report = _new_report(dry_run, moment)

    # --- the gates that need nothing but a question ---------------------------
    if not src.on_mac():
        _ok, sentence = src.access()
        return _pause(report, "not_mac", sentence)
    try:
        own = imthreads.owner_handles(cfg)
    except imthreads.OwnHandlesRequired as exc:
        return _pause(report, "no_own_handles", str(exc))
    ok, sentence = src.access()
    if not ok:
        return _pause(report, "no_access", sentence)
    drift = imspine.check_contract()
    if drift:
        return _pause(report, "engine_changed", drift)

    deadline = _budget_deadline(started, budget_s, cfg)
    if dry_run:
        view: contextlib.AbstractContextManager[imledger.Ledger] = _read_view(home_dir)
    else:
        view = imledger.session(
            lambda: imspine.file_lock(imledger.lock_target(home_dir), timeout=LOCK_TIMEOUT_S),
            home=home_dir,
        )
    with contextlib.ExitStack() as stack:
        try:
            led = stack.enter_context(view)
        except TimeoutError:
            return _pause(report, "busy", SENTENCE_BUSY)
        except imledger.LedgerCorrupt as exc:
            return _pause(report, "ledger_damaged", f"texts paused: {exc}")
        try:
            state = led.load_state()
        except imledger.LedgerCorrupt as exc:
            return _pause(report, "ledger_damaged", f"texts paused: {exc}")
        if "watermark" in state and state.get("watermark_kind") != WATERMARK_KIND:
            return _pause(report, "state_unrecognised", SENTENCE_STATE_KIND)
        if led.missing_but_watermarked:
            report["ledger_missing"] = True

        contact_map = dict(contacts) if contacts is not None else imcontacts.load_map()
        conn = imspine.open_conn(db_path)
        if dry_run:
            _no_checkpoint_on_close(conn)
        try:
            drift = imspine.check_contract(conn)
            if drift:
                return _pause(report, "engine_changed", drift)
            return _daily_run(
                report, led=led, conn=conn, state=state, cfg=cfg, own=own,
                source=src, contacts=contact_map, threads=threads, now=moment,
                deadline=deadline, dry_run=dry_run, emit_new=emit_new, reopen=reopen,
            )
        finally:
            if dry_run:
                with contextlib.suppress(sqlite3.Error):
                    conn.rollback()
            conn.close()
    return report  # pragma: no cover - the with block always returns


def _daily_run(
    report: dict[str, Any],
    *,
    led: imledger.Ledger,
    conn: sqlite3.Connection,
    state: Mapping[str, Any],
    cfg: Mapping[str, Any],
    own: frozenset[str],
    source: MessageSource,
    contacts: Mapping[str, str],
    threads: Path,
    now: datetime,
    deadline: float | None,
    dry_run: bool,
    emit_new: bool = False,
    reopen: bool = False,
) -> dict[str, Any]:
    """Everything between the gates and the report: one ledger run, one commit."""
    run_id = f"daily-{now:%Y%m%dT%H%M%S}-{os.getpid()}-{time.monotonic_ns()}"
    if not dry_run:
        led.begin_run(run_id)
    try:
        owner_ids = owner_person_ids(conn, own)
        pipe = Pipeline(
            conn=conn, led=led, cfg=cfg, own=own, owner_ids=owner_ids, contacts=contacts,
            threads_dir=threads, now=now, deadline=deadline, dry_run=dry_run,
            emit_new=emit_new, reopen=reopen,
        )
        rels: list[str] = []
        if dry_run:
            pipe.tally.open_intents = len(led.open_intents())
        else:
            done, dropped, rels = recover(conn, led)
            pipe.tally.recovered_done, pipe.tally.recovered_dropped = done, dropped

        with source.opened():
            new_state, mark = _read_since_watermark(pipe, source, state, now)
        report["watermark"].update(mark)

        pipe.sweep()
        pipe.stale(max_stamps=_int_cfg(cfg, "max_stamps_per_run"))

        if rels and not dry_run:
            projection = imspine.project(conn, rels)
            pipe.tally.projected += int(getattr(projection, "projected", 0) or 0)
            pipe.tally.projection_skipped += len(getattr(projection, "skipped_paths", []) or [])

        if not dry_run:
            conn.commit()
    except SourceUnreadable as exc:
        _undo(conn, led, run_id, dry_run)
        return _pause(report, "unreadable", imchat.UNREADABLE_SENTENCE.format(reason=exc))
    except sqlite3.OperationalError as exc:
        _undo(conn, led, run_id, dry_run)
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return _pause(report, "memory_busy", SENTENCE_MEMORY_BUSY)
        raise
    except BaseException:
        _undo(conn, led, run_id, dry_run)
        raise

    if not dry_run:
        led.commit_run(run_id)
        led.compact()
        if new_state is not None:
            led.save_state(new_state)  # the watermark, LAST
    _finish_report(report, pipe, led, dry_run)
    return report


def _undo(conn: sqlite3.Connection, led: imledger.Ledger, run_id: str, dry_run: bool) -> None:
    """Throw the run away: the database rolls back, the ledger segment is aborted.

    The ledger keeps any stamp intent (a card may already carry that line), and the
    next run's recovery settles it.  Thread files written this run stay: they are
    rewritten whole next time, and a transcript with no line pointing at it is
    harmless.
    """
    with contextlib.suppress(sqlite3.Error):
        conn.rollback()
    if not dry_run and led.run_id == run_id:
        with contextlib.suppress(Exception):
            led.abort_run(run_id)


def store_rebuilt(before: int, high: int | None) -> bool:
    """True when the Messages store holds less than a run already read: it was rebuilt.

    ``high`` is the store's newest ROWID (``None`` = no rows at all).  A watermark of
    ``0`` means the run before read an EMPTY store (a new Mac, a Messages that holds
    nothing yet), so an empty store now is the same empty store: a quiet run with
    nothing to read, never "your history looks rebuilt".  An empty store under a
    watermark above ``0`` did lose rows, and a newest row below the watermark means
    ``AUTOINCREMENT`` restarted — both are a rebuild.
    """
    if high is None:
        return before > 0
    return high < before


def _read_since_watermark(
    pipe: Pipeline, source: MessageSource, state: Mapping[str, Any], now: datetime
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Read what arrived, run phases A and B, and work out the next watermark.

    Returns ``(the state to save, or None to leave it alone; the watermark facts)``.
    """
    today, zone = now.date(), now.tzinfo
    today_start = _day_start(today, zone)
    before = state.get("watermark") if isinstance(state.get("watermark"), int) else None
    mark: dict[str, Any] = {"before": before, "after": before, "first_run": before is None}

    low_today, high = source.rowid_bounds(today_start)
    if before is not None and store_rebuilt(before, high):
        # The store's newest row is below what was already read: Messages rebuilt its
        # database.  Start again from yesterday, and say so — older days need backfill.
        mark["store_reset"] = True
        before = None

    newest_read: datetime | None = None
    if before is None:
        yesterday = today - timedelta(days=1)
        plan: dict[date, frozenset[int] | None] = {yesterday: None}
        left = pipe.read_days(source, plan, [yesterday])
        undecided = pipe.resolve_all()
        if left or undecided:
            mark["held_by_budget"] = True
            return None, mark  # no watermark yet: the next run is a first run again
        after = (low_today - 1) if low_today is not None else (high or 0)
        mark["held_by_open_day"] = low_today is not None
        newest_read = pipe.newest_read
    else:
        refs = source.row_refs(before)
        pipe.tally.rows_scanned = len(refs)
        closed = [r for r in refs if r.dt_local.date() < today]
        open_rows = [r.rowid for r in refs if r.dt_local.date() >= today]
        pipe.tally.rows_open_day = len(open_rows)
        first_row: dict[date, int] = {}
        for ref in closed:
            day = ref.dt_local.date()
            first_row[day] = min(first_row.get(day, ref.rowid), ref.rowid)
        order = sorted(first_row, key=lambda d: (first_row[d], d))
        # Every conversation on a touched day, not only the touched ones: a person-day
        # folds ALL of that person's conversations that day, and one rebuilt from part of
        # its day would replace a held day's record with a smaller one (see read_days).
        plan = {day: None for day in first_row}
        left = pipe.read_days(source, plan, order)
        undecided = pipe.resolve_all()
        blocked = list(open_rows) + [first_row[d] for d in {*left, *undecided}]
        if blocked:
            after = min(blocked) - 1
        elif refs:
            after = max(r.rowid for r in refs)
        else:
            after = before
        mark["held_by_open_day"] = bool(open_rows)
        mark["held_by_budget"] = bool(left or undecided)
        consumed = [r.dt_local for r in closed if r.rowid <= after]
        newest_read = max(consumed) if consumed else None

    mark["after"] = after
    mark["moved"] = after != state.get("watermark")
    new_state = dict(state)
    new_state.update(
        watermark=int(after),
        watermark_kind=WATERMARK_KIND,
        zone=_zone_name(now),
        last_run_at=now.isoformat(),
    )
    if newest_read is not None:
        new_state["newest_read_at"] = newest_read.isoformat()
    return new_state, mark


def _finish_report(
    report: dict[str, Any], pipe: Pipeline, led: imledger.Ledger, dry_run: bool
) -> None:
    t = pipe.tally
    identifiers = led.all_identifiers()
    queue = led.queued()
    oldest = min((str(r.get("day")) for r in queue.values() if r.get("day")), default=None)
    awaiting, review = waiting_counts(identifiers)
    if dry_run:
        awaiting += sum(t.would_raise.values())
        review += t.new_for_review  # a dry run wrote nothing, so they are not in the ledger
    report.update(
        read={"rows": t.rows_scanned, "rows_in_open_day": t.rows_open_day,
              "messages": t.messages_read, "days": t.days_read, "days_left": t.days_left},
        conversation_days=t.conversation_days,
        person_days=t.person_days,
        threads_written=t.threads_written,
        queued=t.queued,
        released_from_hold=t.released,
        already_queued=t.already_queued,
        links_refreshed=t.links_refreshed,
        already_stamped=t.already_stamped,
        held=dict(sorted(t.held.items())),
        held_total=sum(t.held.values()),
        raised=dict(sorted(t.raised.items())),
        would_raise=dict(sorted(t.would_raise.items())),
        would_reopen=t.would_reopen,
        owner_skipped=t.owner_skipped,
        recovered={"done": t.recovered_done, "dropped": t.recovered_dropped,
                   "open_intents": t.open_intents},
        stale={"due": t.stale_due, "stamped": t.stale_stamped,
               "found_on_card": t.stale_found_on_card, "refused": t.stale_refused,
               "remaining": t.stale_remaining, "failures": list(t.stale_failures),
               "pre_backfill_copies": t.stale_pre_backfill},
        projected=t.projected,
        projection_skipped=t.projection_skipped,
        failures=len(led.failures()),
        awaiting_yes=awaiting,
        awaiting_review=review,
        new_for_review=t.new_for_review,
        queue={"depth": len(queue), "oldest_day": oldest},
        stopped=sorted(t.stopped),
    )
    report["why_nothing_queued"] = _why_nothing_queued(report) if not t.queued else None


#: Identifier states that sit on the texts review list: first seen and held (``held``,
#: whatever its hold reason) or on more than one card (``ambiguous``).  ``pending``
#: waits on the main queue instead, and so does an ``attach_pending`` whose second yes
#: is a card there (the daily E1 follow-through raised it); an ``attach_pending``
#: with NO card (a person added through ``review``) waits on the review list, where
#: the second yes is asked.  ``dismissed`` waits on nobody.  See :func:`waiting_counts`.
REVIEW_STATES: frozenset[str] = frozenset({"held", "ambiguous"})


def waiting_counts(identifiers: Mapping[str, Mapping[str, Any]]) -> tuple[int, int]:
    """``(numbers waiting on the member's main people queue, numbers on the texts review list)``.

    Read per record rather than per state, because ``attach_pending`` sits on one list
    or the other depending on whether a card was raised for its second yes.
    """
    on_queue = on_review = 0
    for record in identifiers.values():
        state = record.get("state")
        if state == "pending":
            on_queue += 1
        elif state == "attach_pending":
            if record.get("proposal_id"):
                on_queue += 1
            else:
                on_review += 1
        elif state in REVIEW_STATES:
            on_review += 1
    return on_queue, on_review


def is_wrong_match(record: Mapping[str, Any], person_id: str) -> bool:
    """True when the member said, in ``review``, that this number is NOT ``person_id``.

    Set by ``imreview`` on a "check these matches" row: the number reached that card
    only by its last digits (E5b) and the member said it is someone else.  It holds the
    number's days for as long as the only card it reaches is that same one.
    """
    return (
        record.get("state") == "dismissed"
        and record.get("hold_reason") == "wrong_match"
        and str(record.get("match_person") or "") == person_id
    )


@dataclass
class Released:
    """What :func:`release_held` did with one number's held days.  Counts only."""

    queued: int = 0
    already: int = 0
    owner_dropped: int = 0


def release_held(
    led: imledger.Ledger,
    ident: str,
    person_id: str,
    *,
    now: datetime,
    owner_ids: Iterable[str] = (),
) -> Released:
    """Move every day held against ``ident`` into the summary queue, filed under ``person_id``.

    The same move ``Pipeline.apply`` makes when a held number resolves, for a caller
    (``imreview``) that has just seen it resolve on the member's yes: each held unit is
    queued with the person and ``queued_at``; a day already queued or already on a card
    is counted, never queued twice.  The member's own card is never a counterpart, so
    days released onto it are dropped.  A ledger run must be open.
    """
    out = Released()
    held = led.release(ident)
    if person_id in frozenset(owner_ids):
        out.owner_dropped = len(held)
        return out
    for unit in held:
        key = str(unit.get("key") or "")
        if not key or led.is_stamped(key) or led.is_queued(key):
            out.already += 1
            continue
        record = {k: v for k, v in unit.items() if k != "reason"}
        record.update(person_id=person_id, queued_at=now.isoformat())
        if led.enqueue(record):
            out.queued += 1
        else:
            out.already += 1
    return out


def _why_nothing_queued(report: Mapping[str, Any]) -> str:
    """The plain reason a run queued nothing — "zero" is never left unexplained."""
    read, mark = report["read"], report["watermark"]
    reasons: list[str] = []
    if mark.get("first_run") and not read["messages"]:
        reasons.append("yesterday had no texts to read")
    elif not mark.get("first_run") and not read["rows"]:
        reasons.append("no new texts have arrived since the last run")
    elif read["rows"] and read["rows"] == read["rows_in_open_day"]:
        reasons.append("the only new texts are from today, which is not over yet")
    if read["messages"] and not report["person_days"]:
        reasons.append("none of what was read cleared the floor for a real conversation")
    if report["already_queued"] or report["already_stamped"]:
        reasons.append(
            f"{report['already_queued'] + report['already_stamped']} conversation-day(s) "
            "read were already queued or already on a card"
        )
    if report["held_total"]:
        reasons.append(
            f"{report['held_total']} conversation-day(s) are held until their number is known"
        )
    if report["owner_skipped"]:
        reasons.append("the rest were your own texts, which never land on a card")
    if mark.get("held_by_budget"):
        reasons.append("the run ran out of time and left the rest for tomorrow")
    return "; ".join(reasons) or "there was nothing new to queue"


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


def status(*, home: Path | str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Everything a member needs to see the feed's health.  Reads only; writes nothing.

    G6: the watermark (raw, and when the newest message read was sent, in the zone),
    the queue's depth and oldest day, live summary claims, numbers by state, held
    days, EVERY ``stamped=False`` failure with its detail, and what the last load
    found in the write-ahead log.  Identifiers appear here (this is the member's own
    terminal); ``imessage.py status --mask`` shapes them for sharing.
    """
    home_dir = Path(home) if home is not None else Path(imconfig.HOME)
    moment = now if now is not None else _engine_now()
    try:
        zone = imspine.local_zone_name()
    except Exception:  # noqa: BLE001 - a broken engine must not stop a read-only status
        zone = _zone_name(moment)
    report: dict[str, Any] = {
        "verb": "status",
        "zone": zone,
        "home": str(home_dir),
        "error": None,
        "busy": False,
        "state": None,
        "ledger": None,
    }
    try:
        state = imledger.load_state(home_dir)
    except imledger.LedgerCorrupt as exc:
        report["error"] = str(exc)
        return report
    report["state"] = {
        "watermark": state.get("watermark"),
        "watermark_kind": state.get("watermark_kind"),
        "newest_read_at": state.get("newest_read_at"),
        "last_run_at": state.get("last_run_at"),
        "zone_of_last_run": state.get("zone"),
        "first_run_pending": "watermark" not in state,
        # A backfill keeps its own progress here, beside (never in) the watermark.
        "backfill": state.get("backfill") if isinstance(state.get("backfill"), dict) else None,
    }
    if not _ledger_exists(home_dir):
        report["ledger"] = {"exists": False}
        return report
    try:
        with _read_view(home_dir) as led:
            report["ledger"] = _ledger_status(led, moment)
    except TimeoutError:
        report["busy"] = True
    except imledger.LedgerCorrupt as exc:
        report["error"] = str(exc)
    return report


def _ledger_status(led: imledger.Ledger, now: datetime) -> dict[str, Any]:
    queue = led.queued()
    days = sorted(str(r.get("day")) for r in queue.values() if r.get("day"))
    unclaimed = sorted(
        str(r.get("day")) for k, r in queue.items()
        if r.get("day") and led.live_claim(k, now) is None
    )
    identifiers = led.all_identifiers()
    by_state = Counter(str(r.get("state")) for r in identifiers.values())
    awaiting_yes, awaiting_review = waiting_counts(identifiers)
    counts = led.counts()
    failures = []
    for key, record in sorted(led.failures().items()):
        parsed = imthreads.parse_ledger_key(key)
        failures.append({
            "identifier": parsed[0] if parsed else key,
            "day": parsed[2].isoformat() if parsed else None,
            "detail": record.get("detail"),
            "count": record.get("count"),
            "at": record.get("at"),
        })
    claims = []
    for claim_id in sorted({c for k in queue for c in [led.live_claim(k, now)] if c}):
        entry = led.claim_of(claim_id) or {}
        lease = entry.get("lease_until")
        claims.append({
            "claim": claim_id,
            "days": len(entry.get("keys") or []),
            "lease_until": (datetime.fromtimestamp(float(lease), now.tzinfo).isoformat()
                            if isinstance(lease, int | float) else None),
        })
    rec = led.recovery
    return {
        "exists": True,
        "counts": counts,
        "queue": {"depth": len(queue), "oldest_day": days[0] if days else None},
        # Beside "queue", not inside it, so the shape other readers already rely on holds.
        "unclaimed": {"depth": len(unclaimed),
                      "oldest_day": unclaimed[0] if unclaimed else None},
        "live_claims": claims,
        "identifiers_by_state": dict(sorted(by_state.items())),
        "awaiting_yes": awaiting_yes,
        "awaiting_review": awaiting_review,
        "held_units": counts["held_units"],
        "failures": failures,
        "open_intents": len(led.open_intents()),
        "recovery": {
            "replayed": rec.replayed,
            "discarded": rec.discarded,
            "intents_kept": rec.intents_kept,
            "torn_tail": rec.torn_tail,
            "ledger_missing_but_watermarked": led.missing_but_watermarked,
        },
    }
