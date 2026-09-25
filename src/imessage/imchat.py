"""iMessage intake — the read of this Mac's Messages database.

This module is the plug-in's ONLY door onto ``~/Library/Messages/chat.db``.  It
opens that database **read-only and never any other way**, hands back one
:class:`Message` per qualifying row, and writes nothing anywhere — no temp copy,
no journal, no state file.  Everything above it (the substance floor, the
conversation-day fold, the people stamps) is built on what comes out of here, so
a row this module gets wrong is a line filed onto the wrong person's card.

It is safe to IMPORT on any platform.  The data only exists on a Mac, so on
Windows or Linux :func:`has_access` returns ``(False, <one plain sentence>)`` and
nothing else in the module runs.  Standard library only, plus this plug-in's own
:mod:`imconfig` and :mod:`imtypedstream`.

Every figure quoted below was measured on one member's live database with the
commands in ``tests/test_imchat.py``, which re-measures them on whatever Mac runs
the suite rather than trusting this docstring.  Only the shape of each figure is
kept here (a share, a ratio, a direction); a different Mac comes back with its own
numbers and the same shape.

``mode=ro`` — and why ``immutable=1`` is FORBIDDEN
--------------------------------------------------
Messages runs in WAL mode, and the most recent messages live in the
write-ahead log until macOS checkpoints it back into the main file.
``immutable=1`` tells SQLite the file cannot change and it may therefore ignore
the WAL entirely.  Measured on a real store, with a ~1 MB ``-wal`` sitting beside
the database::

    mode=ro      message rows=N    max ROWID=R
    immutable=1  message rows=N-2  max ROWID=R-2

Two real messages, silently absent.  Nothing raises, no warning is printed, and
the two rows that vanish are the NEWEST ones — exactly the messages a daily run
exists to read.  So :func:`connect` opens ``?mode=ro`` and only that.  A copy of
the database is refused for the same reason plus a worse one: a copy without its
``-wal`` and ``-shm`` companions is a strictly older database, and copying a
member's entire message history to a second place on disk is a privacy cost this
plug-in has no reason to pay.

``ORDER BY message.date`` — never ``ROWID``
--------------------------------------------
ROWID is insertion order, not chronology.  iCloud back-fills old conversations
onto a Mac long after they happened, so a row inserted today can carry a date
from months ago.  Measured over every qualifying row of one real store, walked in
ROWID order:

* **15.6% of rows** carry a date EARLIER than the row before them;
* the worst single step backwards is **nearly six months**;
* against a running maximum rather than the immediate predecessor, **32.9% of
  rows** sit behind something already seen.

A caller that trusts ROWID order therefore gets a stream that jumps backwards
every seventh row.  Every incremental cursor built on it — "resume after the last
ROWID I saw" — skips whatever arrives later with an older date, permanently.
:func:`iter_messages` orders by ``message.date`` and the test suite re-measures
the disorder on every run.

ROWID as ARRIVAL order — the one thing it is good for
------------------------------------------------------
The same disorder is why the daily run's watermark is a ROWID and not a date.
A message the phone sent while this Mac slept lands here hours or days later,
carrying its real, EARLIER send time: a date watermark has already moved past
that time and never reads it.  ``message`` is declared ``AUTOINCREMENT`` in the
Messages schema (checked: ``sqlite_sequence`` holds its high-water mark), so a
ROWID is never handed out twice and "ROWID greater than the last one I consumed"
means exactly "arrived since".  :func:`iter_row_refs` answers that question and
nothing else — which row, which conversation, when — with no body and no order,
and the caller then re-reads each touched conversation-day in full, BY DATE,
through :func:`iter_messages`.  ROWID finds what is new; the date still decides
where it belongs.

The date column is not one unit, so the ordering expression normalises
------------------------------------------------------------------------
``message.date`` is **nanoseconds** since 2001-01-01 UTC on modern rows and
**seconds** on legacy ones, in the same column, with no flag to tell them apart.
The reference measurement this plug-in was built from branches on ``v > 1e11``
and this module keeps that exact threshold: a legacy seconds value for any real
date is ~1e8-1e9, and a nanosecond value is ~1e17-1e18, so the threshold sits
four orders of magnitude clear of both.

On the store it was measured on, every row is nanoseconds and the legacy branch
never fires, so it is pinned by fixture rather than by live data.  It still has to be
right: a plain ``ORDER BY m.date`` over a mixed database would sort every legacy
row before every modern one regardless of when it happened, and a window bound
expressed in nanoseconds would match every legacy row or none.  So the SQL orders
and windows on a normalised expression (seconds promoted to nanoseconds), while
``Message.date_raw`` keeps the untouched integer.

The exclusions — three of them, each for a measured reason
-----------------------------------------------------------
``associated_message_type <> 0`` — **reactions.**  Several percent of all rows.
    A tapback is not a message; it is an annotation on one, and its body is a
    localised sentence Apple generated, not anything anybody typed.  On real
    data those bodies arrive in more than one language — German ("Gefällt",
    "ein Herz") among them — so a filter written against the English wording
    would let them through.  The type code is the reliable signal.  Measured
    spread, most common first: 2001, 2000, 2006, 2003, 2004, and a long tail
    down to single rows.

``item_type <> 0`` — **system events.**  A fraction of a percent of rows.
    "X joined the conversation", a name change, a leave.  Types 1, 3, 2, 4, 5
    and 6, most common first.

``is_system_message`` — **useless, and deliberately not used.**
    It is ``0`` on every row measured; ``SELECT is_system_message, COUNT(*) FROM
    message GROUP BY 1`` returns exactly one group, ``(0, <every row>)``.  The
    genuine system events all carry ``is_system_message = 0``.  A filter built on
    it would look correct, pass review, and do nothing at all.

``message_summary_info`` — **also useless, and the reason edits are gated on a date.**
    It is non-NULL on **98.0% of rows**.  Anyone treating it as "this message was
    edited" would throw away 98% of the database.  The real edit signal is
    ``date_edited > 0``: **under 1% of rows**, and every one of them also carries
    a non-NULL ``message_summary_info``, which is how the confusion starts.
    This module reads neither column — an edited message is still a message and is
    yielded like any other, with the body Messages currently shows.  If the
    plug-in ever needs to know that a message was edited, ``date_edited > 0`` is
    the gate; the summary blob never is.  The test suite builds every statement
    this module can produce and asserts that none of them names that column.

A message that belongs to no chat is also not yielded: under 1% of rows have no
``chat_message_join`` row, and a message with no conversation cannot be filed
onto a conversation-day.  Roughly 93% of the rows in ``message`` survive all of
it.

``is_from_me`` branches FIRST — ``handle_id`` is not "who spoke"
------------------------------------------------------------------
On an **inbound** row ``handle_id`` is the sender.  On an **outbound** row it is
the RECIPIENT.  Reading it as the speaker attributes the member's own words to
the person they were talking to, and a plug-in whose whole job is filing text
onto people's cards would then write the member's voice onto everyone else's
history.  So :attr:`Message.handle` is documented and named as the *counterpart*,
never the sender, and :attr:`Message.is_from_me` is what says who spoke.

Measured, so the claim is not folklore: across every outbound one-to-one row
that carries a handle, the handle's string equals that chat's
``chat_identifier`` in **100% of cases** — it really is the recipient.
(Comparing handle ROWIDs instead says the opposite and is a trap: ``handle``
holds more rows than there are distinct addresses, one per service, so the same
person is several ROWIDs and ``chat_handle_join`` often names a different one.
Compare the strings, never the row ids.)

``handle`` is ``None`` when the counterpart is unknown, and that is common:

* **A large share of qualifying outbound rows** have ``handle_id = 0`` — every
  outbound group row, plus many outbound one-to-one rows.  Messages simply did
  not record a destination handle on them.
* **A handful of inbound rows** in the whole table have ``handle_id = 0``.
  Every one of them is an ``item_type <> 0`` system event, so **none of them
  survives the exclusions**: after filtering, zero inbound qualifying rows lack a
  handle.  (The brief this module was built from expected them to reach the
  caller.  They do not, and the difference is entirely the ``item_type`` filter.)

The counterpart of an outbound row is therefore usually unknown at the row level
and is resolved by the caller from the conversation, which is what the reference
measurement does: it collects counterpart handles from INBOUND rows only.  The
caller renders ``None`` as "Unknown".

Group vs one-to-one is ``chat.style``, never a head count
-----------------------------------------------------------
``chat.style`` is 43 for a group and 45 for one-to-one.  Counting participants
instead gets it wrong often: on one real store **about 42% of style-43 chats**
have exactly two participants — a group that shrank, or one started with two
people and a name.  Those conversations are genuinely groups; treating them
as one-to-one would file a group thread onto one person as if it were a private
exchange.  :attr:`Message.is_group` is ``chat_style == 43``, and no statement this
module builds touches the participant table at all; the test suite asserts that
by name against every query it can produce.

The body: the decoder is the primary path, ``text`` is the exception
----------------------------------------------------------------------
``message.text`` is populated on only **29.9% of rows** on the store measured,
and macOS stopped writing it around February 2026; ``attributedBody`` is present
on **99.6%**.  So the body is ``text`` when it is there and
:func:`imtypedstream.extract_text` otherwise, and the result of either goes
through :func:`imtypedstream.clean`.  Reading ``text`` alone would return empty
bodies for every recent conversation without raising anything.

:attr:`Message.text` keeps the decoder's two "no words" answers apart, because
they mean opposite things: ``None`` is a FAILURE (the bytes could not be read),
and ``""`` is a SUCCESS meaning the message genuinely carries no words — a photo,
a sticker, an app payload.  A caller that treats ``""`` as an error loses every
photo-only message; a caller that treats ``None`` as empty pretends it knows
something it does not.

The window is half-open: ``[since, until)``
---------------------------------------------
``since_dt`` is included, ``until_dt`` is excluded.  A message landing exactly on
the boundary belongs to the LATER window and is yielded exactly once across two
adjacent runs, which is what makes "run for yesterday, then run for today" add up
to every message with nothing counted twice.  Either bound may be ``None`` for
"no bound on that side".  A naive datetime is read in the configured zone.

The zone comes from the engine, not from this machine
-------------------------------------------------------
Every :attr:`Message.dt_local` is timezone-aware in the zone behind
``config.now_local()`` (``HEARTBEAT_TIMEZONE``), read at call time so a change to
that setting is picked up rather than frozen at import.  The machine's own local
time is NOT the same answer: ``datetime.now().astimezone().tzinfo`` is a fixed
offset, so converting last winter's messages through today's offset is wrong by
a full hour across a DST boundary — measured at exactly 3,600 seconds of
disagreement on real rows.  With the engine's zone the conversion matches the
reference ``ts()`` to within 1 microsecond over 5,000 random rows.

When the engine cannot be imported at all this module degrades to the machine's
own zone with one stderr line, because a plug-in on a half-set-up machine should
still read the database.

Failing to open: one sentence, nothing written, and NO retry
--------------------------------------------------------------
:func:`has_access` is the pre-flight and never raises.  It checks that the file
is THERE before it tries to read it, because SQLite's error text for "this file
does not exist" and "this file is protected" is word-for-word identical — and
sending a member to System Settings when Messages was never set up, or telling
them Messages is missing when the real answer is one checkbox, are both wasted
afternoons.  When access is refused, the sentence names the app that actually
needs Full Disk Access — the app that owns this process, worked out by walking
the process tree, rather than a guess like "Terminal".

When the store cannot be read at run time the contract is: one plain sentence to
stderr, an empty result, nothing written, and **no retry loop**.  A retry is
specifically wrong here.  The scheduled run happens at 03:00, when Messages.app
is idle and macOS may have checkpointed and removed the WAL; a read-only open can
genuinely fail in that moment, and hammering a member's live message store in a
loop at three in the morning is the one thing this plug-in must never do.  The
run reports and stops; the next run picks up exactly where this one left off,
because nothing was consumed.
"""

from __future__ import annotations

# imconfig FIRST, before any engine module. Importing it puts the engine's
# `.claude/scripts` on sys.path and then re-asserts THIS folder in front of it,
# so `import config` below reaches the engine while no engine module can ever
# shadow one of ours. See imconfig's docstring for the bug that rule prevents.
import imconfig  # noqa: F401  (imported for its import-time sys.path work)

import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from collections.abc import Iterator
from pathlib import Path

import imtypedstream

try:  # The engine, for the configured zone only. Optional on purpose.
    import config as _engine_config
except Exception:  # noqa: BLE001 — a half-set-up machine must still read texts.
    _engine_config = None


# ---------------------------------------------------------------------------
# Constants. Every number here is measured; see the module docstring.
# ---------------------------------------------------------------------------

#: The member's Messages database. Never copied, never opened for writing.
DB_PATH: Path = Path.home() / "Library" / "Messages" / "chat.db"

#: ``chat.style`` for a group conversation. The ONLY signal for "is this a
#: group" — many real group chats have just two participants.
GROUP_STYLE: int = 43

#: ``chat.style`` for a one-to-one conversation.
ONE_TO_ONE_STYLE: int = 45

#: Apple's epoch: 2001-01-01 00:00:00 UTC.
APPLE_EPOCH: datetime = datetime(2001, 1, 1, tzinfo=timezone.utc)

#: Above this, ``message.date`` is nanoseconds; at or below it, seconds. The
#: threshold the reference ``ts()`` uses, kept byte-for-byte. Real seconds values
#: are ~1e8-1e9 and real nanosecond values ~1e17-1e18, so it is four orders of
#: magnitude clear of both.
NANOSECOND_THRESHOLD: int = 100_000_000_000

#: The first sixteen bytes of any SQLite file. Reading them is how
#: :func:`has_access` proves it can really open the store rather than merely
#: see it in a directory listing.
SQLITE_MAGIC: bytes = b"SQLite format 3\x00"

#: How many rows :func:`iter_messages` pulls from SQLite at a time. Large enough
#: that tens of thousands of rows cost a handful of round trips, small enough
#: that the blobs of one batch are the only message bodies in memory at once.
FETCH_BATCH: int = 512

# The ordering/windowing expression: `message.date` normalised to nanoseconds so
# that a legacy seconds row and a modern nanosecond row can be compared at all.
# The raw column is still what lands in `Message.date_raw`.
_APPLE_NS = (
    f"(CASE WHEN m.date > {NANOSECOND_THRESHOLD} "
    f"THEN m.date ELSE m.date * 1000000000 END)"
)

# The columns, the joins and the exclusions — in ONE place, shared by the row
# query, the count and the range, so those three can never disagree about what
# "a message this plug-in can read" means.
_COLUMNS = (
    "cmj.chat_id, ch.style, ch.display_name, "
    "m.date, m.is_from_me, h.id, m.text, m.attributedBody"
)

_FROM_QUALIFYING = """
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat ch ON ch.ROWID = cmj.chat_id
LEFT JOIN handle h ON h.ROWID = m.handle_id
WHERE m.date IS NOT NULL
  AND IFNULL(m.associated_message_type, 0) = 0
  AND IFNULL(m.item_type, 0) = 0
"""

#: What a caller sees when the store is there but cannot be read right now.
#: One sentence, no retry, and it says what happens next.
UNREADABLE_SENTENCE: str = (
    "texts: I couldn't read the Messages database just now ({reason}), so I read "
    "nothing this run and left everything where it was; the next run picks up "
    "from the same place."
)

_warned_no_engine = False
_warned_unreadable = False
_warned_bad_timestamp = False


def _warn(message: str) -> None:
    """One plain line to stderr, the same shape :mod:`imconfig` uses."""
    print(f"[imessage] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# The row.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    """One readable message, already decoded and already placed in time.

    Frozen because a row is a fact: the caller folds these into conversation-days
    and stamps them onto people, and a mutated row would be a fact that changed
    after it was counted.

    Attributes:
        chat_rowid: ``chat.ROWID`` — the conversation this message belongs to.
        chat_style: ``chat.style`` — 43 for a group, 45 for one-to-one.
        chat_name: The chat's display name, or ``None``. Usually ``None``: most
            real group chats are unnamed.
        is_group: ``chat_style == GROUP_STYLE``. Derived from the style and
            NEVER from a participant count — many real groups have exactly two
            participants and would be misread as private conversations.
        date_raw: The untouched ``message.date`` integer, nanoseconds on modern
            rows and seconds on legacy ones. Kept raw so a caller can key or
            de-duplicate on exactly what the database holds.
        dt_local: ``date_raw`` as a timezone-aware datetime in the configured
            zone (see the module docstring).
        is_from_me: True when the member sent it. **Read this before reading
            handle** — on an outbound row the handle is the recipient.
        handle: The COUNTERPART's handle (a phone number or an address), or
            ``None`` when the row does not name one. On an inbound row this is
            the sender; on an outbound row it is the recipient. It is never
            "who spoke".
        text: The body, decoded and cleaned. ``None`` means the bytes could not
            be read at all; ``""`` means the message genuinely carries no words
            (an attachment, a sticker, an app payload) and is not an error.
    """

    chat_rowid: int
    chat_style: int
    chat_name: str | None
    is_group: bool
    date_raw: int
    dt_local: datetime
    is_from_me: bool
    handle: str | None
    text: str | None


# ---------------------------------------------------------------------------
# Access — is the store there, and may this process read it?
# ---------------------------------------------------------------------------


def _is_macos() -> bool:
    """True on a Mac. A seam, so the other-platform branch can be tested."""
    return sys.platform == "darwin"


def _responsible_app() -> str:
    """The app that needs Full Disk Access — worked out, never assumed.

    macOS grants Full Disk Access to the application that owns the process tree,
    which for this plug-in is whatever the member launched: Terminal, iTerm,
    Visual Studio Code, the Claude app.  Naming the wrong one sends them to a
    checkbox that changes nothing, so the name is derived: walk from this process
    up its parents and take the OUTERMOST ``.app`` bundle any ancestor lives in.

    Outermost, not nearest: a real ancestry reads
    ``…/Visual Studio Code.app/Contents/Frameworks/Code Helper.app/Contents/MacOS/Code Helper``
    and the answer a member can act on is "Visual Studio Code", not "Code
    Helper".  Taking the first ``.app`` component of each path, and the last such
    ancestor walking up, gives that.

    The whole process table is read in ONE ``ps`` call rather than one call per
    ancestor: a dozen subprocess launches inside an error path is its own kind of
    failure.  Measured at 0.03s for 884 processes.

    Falls back to the running interpreter's own name when there is no ``.app`` in
    the tree at all — a launchd job, a bare SSH session — because that is then
    genuinely the thing macOS has to be told about.  Never raises.
    """
    fallback = Path(sys.executable).name or "the program running this"
    if not _is_macos():
        return fallback

    try:
        completed = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:  # noqa: BLE001 — ps missing, sandboxed, slow: all the same.
        return fallback

    parents: dict[int, int] = {}
    commands: dict[int, str] = {}
    for line in completed.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        parents[pid] = parent
        commands[pid] = parts[2].strip()

    name: str | None = None
    pid = os.getpid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        command = commands.get(pid, "")
        marker = command.find(".app/")
        if marker != -1:
            bundle = os.path.basename(command[: marker + len(".app")])
            if bundle.endswith(".app"):
                name = bundle[: -len(".app")]
        pid = parents.get(pid, 0)

    return name or fallback


def has_access(path: Path | None = None) -> tuple[bool, str]:
    """Can this process read the Messages database? ``(ok, one plain sentence)``.

    Three answers, kept apart on purpose, because they send a member to three
    different places:

    * **not a Mac** — there is no Messages database to read, and no amount of
      permission-granting creates one;
    * **MISSING** — the file is not there: Messages was never set up on this Mac,
      or the store has been moved;
    * **DENIED** — the file is there and this process may not read it: Full Disk
      Access, granted to the app named in the sentence.

    Existence is checked BEFORE the read is attempted, because SQLite's error
    text for the last two is identical ("unable to open database file") and
    conflating them sends the member to the wrong fix.  ``os.path.exists`` is the
    first gate; when it says no, :func:`os.stat` is asked for the errno as well,
    because ``exists()`` swallows a permission failure and reports it as absence
    — which would print "Messages is missing" at the exact moment the true answer
    is "grant Full Disk Access".  Then a real read of the first bytes is
    attempted, since being able to stat a file is not being able to open it.

    Never raises, and never retries.  The returned sentence is plain English and
    names the fix; the caller prints it and exits 0.

    Args:
        path: Overrides :data:`DB_PATH`. For tests, so a missing-store check
            never has to go near the member's real store.

    Returns:
        ``(True, sentence)`` when the store is readable, ``(False, sentence)``
        otherwise. The sentence is always safe to print.
    """
    store = DB_PATH if path is None else Path(path)

    if not _is_macos():
        return (
            False,
            "texts: Messages only keeps its database on a Mac, and this machine "
            f"is not one ({sys.platform}), so there are no texts to read here.",
        )

    if not os.path.exists(store):
        # exists() cannot tell "not there" from "not allowed to look" — it
        # swallows the errno and answers False for both — so ask stat directly
        # before naming either one.
        absent = True
        try:
            os.stat(store)
        except PermissionError:
            return (
                False,
                "texts: this Mac won't let "
                f"{_responsible_app()} near the Messages database. Grant it Full "
                "Disk Access in System Settings > Privacy & Security > Full Disk "
                "Access, then run this again.",
            )
        except FileNotFoundError:
            absent = True
        except OSError as exc:
            return (
                False,
                f"texts: I couldn't check {store} ({exc.__class__.__name__}), so "
                "I can't tell whether Messages is set up on this Mac.",
            )
        else:
            # It appeared between the two calls. Carry on to the real read
            # rather than reporting an absence that is already out of date.
            absent = False

        if absent:
            return (
                False,
                f"texts: there is no Messages database at {store}, so there is "
                "nothing to read. That is what a Mac looks like when Messages has "
                "never been set up, or when the store has been moved.",
            )

    try:
        with open(store, "rb") as handle:
            header = handle.read(len(SQLITE_MAGIC))
    except PermissionError:
        return (
            False,
            "texts: the Messages database is there, but this Mac won't let "
            f"{_responsible_app()} read it. Grant it Full Disk Access in System "
            "Settings > Privacy & Security > Full Disk Access, then run this "
            "again.",
        )
    except OSError as exc:
        return (
            False,
            f"texts: I couldn't open the Messages database ({exc.__class__.__name__}), "
            "so I read nothing this run.",
        )

    if not header.startswith(SQLITE_MAGIC):
        return (
            False,
            f"texts: the file at {store} is not a SQLite database, so it is not "
            "the Messages store I know how to read.",
        )

    return (True, "texts: the Messages database is there and I can read it.")


def _read_only_uri(path: Path) -> str:
    """The one URI this module ever opens: ``file://…?mode=ro``.

    Built with :meth:`Path.as_uri`, which percent-encodes a path containing
    spaces (or anything else a URI cannot carry literally) — a home directory
    with a space in it would otherwise produce a URI SQLite parses into a
    different, absent file and then reports as "unable to open database file".

    ``mode=ro`` and nothing else.  ``immutable=1`` is forbidden here: it lets
    SQLite ignore the write-ahead log, which on a real store silently cost the
    two newest messages.  See the module docstring for the measurement.
    """
    return path.as_uri() + "?mode=ro"


def connect(path: Path | None = None) -> sqlite3.Connection:
    """A read-only handle on the Messages database.

    ``?mode=ro`` via URI, with a five-second busy timeout so a checkpoint in
    progress is waited out once rather than failed on.  The member's live store
    is opened in place: never a copy, never read-write, never ``immutable=1``.

    Raises :class:`sqlite3.Error` only when the caller ignored :func:`has_access`
    — this function is the second half of a two-step, and the first half is the
    one that produces a sentence a member can act on.  It does NOT retry: see the
    module docstring on why a retry loop against a live message store at 03:00 is
    the wrong answer.

    Args:
        path: Overrides :data:`DB_PATH`. For tests only.

    Returns:
        An open, read-only :class:`sqlite3.Connection`. The caller closes it.
    """
    store = DB_PATH if path is None else Path(path)
    return sqlite3.connect(_read_only_uri(store), uri=True, timeout=5.0)


# ---------------------------------------------------------------------------
# Time.
# ---------------------------------------------------------------------------


def _zone() -> tzinfo:
    """The configured timezone, asked for fresh every time.

    ``config.now_local()`` carries the zone behind ``HEARTBEAT_TIMEZONE``, and it
    is read at call time rather than cached at import so that a member who
    changes their timezone gets the new one on the next run instead of on the
    next restart.  The machine's own local time is not a substitute: it is a
    fixed offset, so a message from last winter converts an hour wrong.

    Degrades to the machine's zone (with one stderr line for the process) when
    the engine cannot be imported at all.  Never raises.
    """
    global _warned_no_engine

    if _engine_config is not None:
        try:
            zone = _engine_config.now_local().tzinfo
            if zone is not None:
                return zone
        except Exception:  # noqa: BLE001 — a broken engine must not stop the read.
            pass

    if not _warned_no_engine:
        _warned_no_engine = True
        _warn(
            "the engine's config isn't importable, so message times are being "
            "read in this machine's own timezone rather than the configured one."
        )
    own = datetime.now().astimezone().tzinfo
    return own if own is not None else timezone.utc


def _apple_to_dt(raw: int, zone: tzinfo) -> datetime:
    """:func:`apple_to_dt` with the zone already resolved. Integer maths only.

    Kept separate so :func:`iter_messages` resolves the zone once per call rather
    than once per row.  The arithmetic runs through :class:`timedelta` on whole
    microseconds instead of a float seconds value, so a nanosecond timestamp
    (~1e17) does not lose its sub-second part to float precision the way
    ``fromtimestamp(raw / 1e9 + offset)`` does.
    """
    if raw > NANOSECOND_THRESHOLD:
        microseconds = raw // 1000  # nanoseconds; the last three digits are lost
    else:
        microseconds = raw * 1_000_000  # legacy seconds
    return (APPLE_EPOCH + timedelta(microseconds=microseconds)).astimezone(zone)


def apple_to_dt(raw: int) -> datetime:
    """An Apple ``message.date`` integer as a timezone-aware local datetime.

    The column holds nanoseconds since 2001-01-01 UTC on modern rows and seconds
    on legacy ones, with nothing to tell them apart but their size, so the value
    is branched on :data:`NANOSECOND_THRESHOLD` exactly as the reference
    measurement does.

    The result is aware, in the zone behind ``config.now_local()`` — never the
    machine's local offset, which is wrong by an hour across a DST boundary.
    Verified against the reference ``ts()`` on 5,000 random live rows: the two
    agree to within 1 microsecond.

    Args:
        raw: The raw ``message.date`` integer.

    Returns:
        A timezone-aware datetime in the configured zone.
    """
    return _apple_to_dt(raw, _zone())


def _dt_to_apple_ns(moment: datetime, zone: tzinfo) -> int:
    """A datetime as Apple nanoseconds, for a window bound. Exact integer maths.

    A naive datetime is read in ``zone``: a caller saying "since midnight" means
    midnight where the member lives, and silently treating it as UTC would move
    every window boundary by the offset.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    delta = moment - APPLE_EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


# ---------------------------------------------------------------------------
# The read.
# ---------------------------------------------------------------------------


def _row_query(
    columns: str,
    since_dt: datetime | None,
    until_dt: datetime | None,
    zone: tzinfo,
    order: bool,
    after_rowid: int | None = None,
) -> tuple[str, tuple[int, ...]]:
    """The statement and its parameters, built once for every reader here.

    The window is half-open: ``>= since`` and ``< until``, both compared against
    the normalised nanosecond expression so a legacy seconds row is bounded
    correctly rather than falling entirely inside or entirely outside.

    ``after_rowid`` bounds by ARRIVAL instead (``m.ROWID > ?``), for
    :func:`iter_row_refs`.  It never orders anything: see the module docstring on
    why ROWID says when a row arrived and never where it belongs in time.
    """
    sql = f"SELECT {columns} {_FROM_QUALIFYING}"
    params: list[int] = []
    if since_dt is not None:
        sql += f"  AND {_APPLE_NS} >= ?\n"
        params.append(_dt_to_apple_ns(since_dt, zone))
    if until_dt is not None:
        sql += f"  AND {_APPLE_NS} < ?\n"
        params.append(_dt_to_apple_ns(until_dt, zone))
    if after_rowid is not None:
        sql += "  AND m.ROWID > ?\n"
        params.append(int(after_rowid))
    if order:
        # By date. Never by ROWID: 15.6% of rows on a real store step backwards
        # in ROWID order, the worst by months.
        sql += f"ORDER BY {_APPLE_NS}"
    return sql, tuple(params)


def _as_text(value: object) -> str:
    """A TEXT column's value as a string, keeping ``""`` as ``""``.

    Separate from :func:`_clean_str` on purpose.  A ``message.text`` of ``""`` is
    a body that was read and carries no words — the same thing the decoder says
    with ``""`` — while a blank ``display_name`` means "no name".  Blanking one
    to ``None`` like the other would turn a wordless message into a message whose
    bytes could not be read, which is the one distinction this plug-in cannot
    afford to lose.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _clean_str(value: object) -> str | None:
    """A database value as a trimmed string, with blank and NULL both ``None``.

    An empty ``display_name`` and an empty ``handle.id`` are the database's way
    of saying "nothing here", and a caller checking ``if chat_name:`` should get
    the same answer as one checking ``is None``.  A TEXT column holding bytes
    that are not valid UTF-8 is decoded with ``replace`` rather than lost.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).strip()
    return text or None


def _message_from_row(row: tuple, zone: tzinfo) -> Message | None:
    """One database row as a :class:`Message`, or ``None`` if it is unreadable.

    ``None`` costs the caller one message, never the run.  The only way it
    happens is a ``message.date`` so far outside the representable range that
    :class:`timedelta` refuses it — zero rows on a real store, but a corrupt row
    must not take down a morning's intake.
    """
    global _warned_bad_timestamp

    chat_rowid, style, display_name, date_raw, is_from_me, handle, text, blob = row

    try:
        dt_local = _apple_to_dt(int(date_raw), zone)
    except (OverflowError, ValueError, OSError, TypeError):
        if not _warned_bad_timestamp:
            _warned_bad_timestamp = True
            _warn(
                "at least one message carries a timestamp I can't place in time; "
                "those messages are left out and everything else is read as usual."
            )
        return None

    # The body: `text` when the database still has it, the typedstream decoder
    # otherwise — which is 70% of rows and rising. clean() keeps None (could not
    # be read) and "" (read, and genuinely wordless) apart.
    raw_body = _as_text(text) if text is not None else imtypedstream.extract_text(blob)
    body = imtypedstream.clean(raw_body)

    style_value = int(style) if style is not None else -1

    return Message(
        chat_rowid=int(chat_rowid),
        chat_style=style_value,
        chat_name=_clean_str(display_name),
        # From the style, and ONLY the style. Many real group chats have two
        # participants, so a head count would call them private conversations.
        is_group=style_value == GROUP_STYLE,
        date_raw=int(date_raw),
        dt_local=dt_local,
        is_from_me=bool(is_from_me),
        # The COUNTERPART. On an inbound row the sender; on an outbound row the
        # recipient. is_from_me above is what says who actually spoke.
        handle=_clean_str(handle),
        text=body,
    )


def iter_messages(
    conn: sqlite3.Connection,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
    *,
    strict: bool = False,
) -> Iterator[Message]:
    """Every qualifying message in ``[since_dt, until_dt)``, oldest first.

    Ordered by ``message.date``, never by ROWID.  Rows are pulled from SQLite in
    batches of :data:`FETCH_BATCH`, so a full-history pass holds one batch of
    message bodies in memory rather than the whole history.

    What "qualifying" leaves out, and why, is the module docstring's exclusions
    section: reactions, system events, and messages attached to no conversation.
    Edited messages are INCLUDED, with the body Messages currently shows.

    The window is half-open — ``since_dt`` included, ``until_dt`` excluded — so a
    message exactly on the boundary belongs to the later window and two adjacent
    runs together see every message exactly once.  Either bound may be ``None``.
    A naive bound is read in the configured zone.

    If the store cannot be read, this yields NOTHING, prints one plain sentence
    to stderr, and does not retry.  A caller can treat an empty result as "no new
    messages" without special-casing the failure, which is what makes the 03:00
    run exit 0 instead of waking anybody up.

    ``strict=True`` is for the one caller that cannot treat "could not read" as
    "nothing there": the daily run, which moves a watermark past whatever it
    believes it has read.  An empty day that was really an unreadable day would
    be a day skipped for good, so under ``strict`` the :class:`sqlite3.Error` is
    raised to the caller instead of swallowed — still no retry, still nothing
    written.

    Args:
        conn: A connection from :func:`connect`.
        since_dt: Inclusive lower bound, or ``None``.
        until_dt: Exclusive upper bound, or ``None``.
        strict: Raise a read failure rather than yield nothing.

    Yields:
        :class:`Message`, oldest first.
    """
    global _warned_unreadable

    zone = _zone()
    sql, params = _row_query(_COLUMNS, since_dt, until_dt, zone, order=True)

    try:
        cursor = conn.execute(sql, params)
    except sqlite3.Error as exc:
        if strict:
            raise
        if not _warned_unreadable:
            _warned_unreadable = True
            _warn(UNREADABLE_SENTENCE.format(reason=exc.__class__.__name__))
        return

    while True:
        try:
            batch = cursor.fetchmany(FETCH_BATCH)
        except sqlite3.Error as exc:
            if strict:
                raise
            # Mid-read: the store went away under us, or a checkpoint took the
            # WAL. One sentence, stop where we are, no retry.
            if not _warned_unreadable:
                _warned_unreadable = True
                _warn(UNREADABLE_SENTENCE.format(reason=exc.__class__.__name__))
            return
        if not batch:
            return
        for row in batch:
            message = _message_from_row(row, zone)
            if message is not None:
                yield message


#: The columns :func:`iter_row_refs` reads: which row, which conversation, when.
#: No body — the arrival scan only has to say what is new and where it goes.
_REF_COLUMNS = "m.ROWID, cmj.chat_id, m.date"


@dataclass(frozen=True)
class RowRef:
    """One qualifying row, as the arrival scan sees it: no body, just its place.

    Attributes:
        rowid: ``message.ROWID`` — arrival order, never chronology.
        chat_rowid: The conversation it belongs to.
        date_raw: The untouched ``message.date`` integer.
        dt_local: ``date_raw`` in the configured zone, so ``dt_local.date()`` is the
            same conversation-day :func:`iter_messages` would file it under.
    """

    rowid: int
    chat_rowid: int
    date_raw: int
    dt_local: datetime


def iter_row_refs(conn: sqlite3.Connection, after_rowid: int) -> Iterator[RowRef]:
    """Every qualifying row that ARRIVED after ``after_rowid``: which, where, when.

    The daily run's arrival scan.  The same joins and the same exclusions as
    :func:`iter_messages` (a reaction, a system event or a message in no
    conversation is not a row anyone has to re-read a day for), bounded by
    ``m.ROWID > after_rowid`` and in no particular order — the caller collects the
    conversation-days these rows touch and re-reads each one by date.

    A row whose timestamp cannot be placed in time is left out, exactly as
    :func:`iter_messages` leaves it out, so the two readers agree on what a
    qualifying row is.

    **Raises** :class:`sqlite3.Error` rather than yielding nothing.  This answer
    moves a watermark, and a failed read reported as "nothing new" would move it
    past rows nobody read.  No retry here either; the caller stops the run.
    """
    zone = _zone()
    sql, params = _row_query(
        _REF_COLUMNS, None, None, zone, order=False, after_rowid=int(after_rowid)
    )
    cursor = conn.execute(sql, params)
    while True:
        batch = cursor.fetchmany(FETCH_BATCH)
        if not batch:
            return
        for rowid, chat_rowid, date_raw in batch:
            try:
                moment = _apple_to_dt(int(date_raw), zone)
            except (OverflowError, ValueError, OSError, TypeError):
                continue
            yield RowRef(int(rowid), int(chat_rowid), int(date_raw), moment)


def rowid_bounds(
    conn: sqlite3.Connection, since_dt: datetime | None
) -> tuple[int | None, int | None]:
    """``(lowest ROWID dated at or after since_dt, highest ROWID)`` over qualifying rows.

    What a first-ever daily run needs to place its watermark without reading the
    whole history: everything that arrived before the first row of the still-open
    day is either the closed day it read or older history, which is backfill's
    job.  ``None`` on either side when there is no such row.  ``since_dt=None``
    makes the first value the lowest ROWID overall.

    Raises :class:`sqlite3.Error`, for the same reason :func:`iter_row_refs` does.
    """
    zone = _zone()
    low_sql, low_params = _row_query("MIN(m.ROWID)", since_dt, None, zone, order=False)
    high_sql, high_params = _row_query("MAX(m.ROWID)", None, None, zone, order=False)
    low = conn.execute(low_sql, low_params).fetchone()
    high = conn.execute(high_sql, high_params).fetchone()
    return (
        int(low[0]) if low and low[0] is not None else None,
        int(high[0]) if high and high[0] is not None else None,
    )


def count_rows(conn: sqlite3.Connection) -> int:
    """How many messages :func:`iter_messages` would yield over all of history.

    The same joins and the same exclusions, counted rather than read — so this is
    the size of the job, not the size of the table.  (Roughly 93% of the rows in
    ``message`` on a real store; the difference is reactions, system events and
    messages belonging to no conversation.)

    Returns ``0`` with one stderr sentence if the store cannot be read, rather
    than raising.
    """
    global _warned_unreadable

    sql, params = _row_query("COUNT(*)", None, None, timezone.utc, order=False)
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error as exc:
        if not _warned_unreadable:
            _warned_unreadable = True
            _warn(UNREADABLE_SENTENCE.format(reason=exc.__class__.__name__))
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def date_range(conn: sqlite3.Connection) -> tuple[datetime | None, datetime | None]:
    """The oldest and newest readable message, as aware local datetimes.

    Over exactly the set :func:`iter_messages` yields, so it answers "how far
    back can a backfill go" rather than "what is in the table".  ``(None, None)``
    when there is nothing to read, or when the store cannot be read — in which
    case one plain sentence goes to stderr and nothing raises.
    """
    global _warned_unreadable

    zone = _zone()
    sql, params = _row_query(
        f"MIN({_APPLE_NS}), MAX({_APPLE_NS})", None, None, zone, order=False
    )
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error as exc:
        if not _warned_unreadable:
            _warned_unreadable = True
            _warn(UNREADABLE_SENTENCE.format(reason=exc.__class__.__name__))
        return (None, None)

    if not row or row[0] is None or row[1] is None:
        return (None, None)
    try:
        return (_apple_to_dt(int(row[0]), zone), _apple_to_dt(int(row[1]), zone))
    except (OverflowError, ValueError, OSError, TypeError):
        return (None, None)
