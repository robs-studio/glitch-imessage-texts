"""The ledger: the idempotency the morning stage assumes it does not need.

Why this file exists
--------------------
The engine's morning pass re-runs an interrupted adapter by design (its readers
are meant to be side-effect-free) and kills one that overruns its 20 seconds with
a SIGKILL, which nothing can catch.  This plug-in is not side-effect-free: it
files conversation-days onto real people's cards.  The engine's own stamp
idempotency is ``pointer.strip() in body`` over a pointer that INCLUDES the
topic, so the same day landed once with the mechanical topic and once with a
model summary is two lines on a card, and there is no verb that takes one back.
Everything standing between an at-least-once scheduler and a double-filed day is
this file: each conversation-day's fate is written down here before the card is
touched and again after.

What it holds
-------------
Six maps, in ``ledger.json`` (the compacted snapshot):

* ``stamped``: ledger key -> ``{person_id, grain, turns, topic, link, stamped_at,
  status}``, where ``status`` is ``intent`` (about to touch the card) or ``done``
  (on the card).  ``done`` is terminal: its topic and turns are frozen for good,
  because the engine's idempotency key contains the topic.
* ``identifiers``: canonical identifier -> ``{state, proposal_id, kind, name,
  first_seen, last_seen, days}``.
* ``pending_units``: identifier -> the unit records held against it until it
  resolves to a person.
* ``synth_queue``: ledger key -> the queued unit awaiting a summary.
* ``claims``: claim id -> ``{keys, lease_until, claimed_at}``, the summary pass's
  leases.
* ``failures``: ledger key -> the last ``stamped=False`` detail and a count.

Every key is built by :func:`imthreads.ledger_key` with the DAY grain, and this
module refuses any other shape.  The member ruled on 2026-09-24 that there is no weekly
fold, so a week-shaped or non-canonical key here can only be a bug, and a bug in
a key is a day filed twice or never.

The write-ahead log
-------------------
Rewriting a multi-megabyte ``ledger.json`` twice per stamp, 200 stamps a run,
does not fit a 20-second budget, so every mutation is ONE JSON line appended to
``ledger.wal`` with a single ``os.write``.  Once ``os.write`` returns, the bytes
belong to the kernel, and a SIGKILL cannot take them back (power loss can, which
is why :meth:`Ledger.commit_run` and :meth:`Ledger.compact` fsync).  A run's
lines are bracketed ``begin`` ... ``committed``.  On load, ``ledger.json`` is read
and then the WAL is replayed over it:

* a segment with its ``committed`` marker is applied whole;
* a segment WITHOUT one (the process died, or the run was aborted) is DISCARDED,
  except its ``stamp_intent`` lines, which survive.  An intent means the card may
  already carry the line, so forgetting it is how a day gets filed twice; the
  caller re-checks each :meth:`Ledger.open_intents` entry against the card by
  exact pointer line, then calls :meth:`Ledger.stamp_done` or
  :meth:`Ledger.drop_intent`;
* a torn LAST line (a write cut short) is ignored with one stderr line and cut
  off before the next append, so it can never glue itself onto the next record;
* a damaged line anywhere else raises :class:`LedgerCorrupt`.  Only the tail can
  be torn by an interrupted append, so damage in the middle is something else,
  and guessing past it could replay half a history.

Every WAL line records a RESULT, never a request: the full record written, the
keys a claim actually took, the failure count reached.  Replaying a line
therefore sets a value rather than recomputing one, which makes replay converge.
That is what makes the one unavoidable crash window harmless: a kill after
``ledger.json`` is replaced but before the WAL is truncated replays the WAL over a
snapshot that already contains it, and lands on the same state.

Corruption is never "start fresh"
---------------------------------
A ``ledger.json`` that cannot be read raises :class:`LedgerCorrupt` naming the
file and is left byte-for-byte as found: never renamed, never deleted, never
replaced by an empty ledger.  An empty ``stamped`` map means "nothing is on any
card yet", and believing that would re-file every conversation-day already
landed.  ``state.json`` follows the same rule for the watermark.

Locking
-------
A session holds the lock for its whole life: the load, every mutation, the
compaction and the watermark write.  The caller passes the lock, because the
engine's ``file_lock`` belongs to the engine and this module imports nothing from
it.  :func:`lock_target` gives the path to hand the engine's ``file_lock`` so its
sidecar IS ``ledger.lock``; :func:`default_lock` locks that same file with the
same primitives (``fcntl.flock`` on macOS and Linux, ``msvcrt.locking`` on
Windows), so the two exclude each other and either is safe to pass.

Files and modes
---------------
``ledger.json``, ``ledger.wal``, ``ledger.lock`` and ``state.json``, all under the
``home`` directory (default :data:`imconfig.HOME`).  Owner-only (0600) wherever
the OS honours it, degrading silently where it does not (Windows), as the house
pattern does.  They hold names and topics from the member's texts.

No engine import
----------------
Pure plug-in state.  ``imconfig`` is imported first (the plug-in's sys.path
rule) and ``imthreads`` only for the key helpers.  Times this module generates
are timezone-aware UTC ISO strings; a caller that wants the configured zone
supplies its own ``stamped_at`` / ``queued_at`` in the record.
"""

from __future__ import annotations

import contextlib
import copy
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

# imconfig first: importing it puts the plug-in folder ahead of the engine's on
# sys.path for the whole process. This module needs only its HOME constant.
import imconfig
import imthreads

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

LEDGER_NAME: str = "ledger.json"
WAL_NAME: str = "ledger.wal"
LOCK_NAME: str = "ledger.lock"
STATE_NAME: str = "state.json"

#: The ``version`` written into ``ledger.json``.  A file from a NEWER format is
#: refused rather than read, because reading it would drop what it added.
FORMAT_VERSION: int = 1

#: How long :func:`default_lock` waits.  A 20-second stage cannot afford the
#: engine's 30-second default wait, so a busy ledger gives up early and the run
#: tries again tomorrow rather than being killed mid-work.
DEFAULT_LOCK_TIMEOUT_S: float = 5.0

INTENT: str = "intent"
DONE: str = "done"

IDENTIFIER_STATES: frozenset[str] = frozenset(
    {"pending", "held", "ambiguous", "dismissed", "attach_pending", "accepted"}
)

#: The six maps, in the order they are written.
MAPS: tuple[str, ...] = (
    "stamped",
    "identifiers",
    "pending_units",
    "synth_queue",
    "claims",
    "failures",
)

#: Fields that change between an intent and its retry without changing what the
#: card line says.  Everything else in an intent must match exactly, or the retry
#: could write a second, different line for a day that may already be on the card.
_VOLATILE_STAMP_FIELDS: frozenset[str] = frozenset({"status", "grain", "intent_at", "stamped_at"})

#: Windows' C runtime opens a descriptor in TEXT mode unless told otherwise, and
#: would then turn every ``\n`` the WAL writes into ``\r\n``.
_O_BINARY: int = getattr(os, "O_BINARY", 0)

_MARKERS: frozenset[str] = frozenset({"begin", "committed", "aborted"})


# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class LedgerError(Exception):
    """The ledger's protocol was broken by the caller.

    No run open, a mismatched run id, a closed session, a write attempted after
    the log could not be repaired.  Always a programming or environment error,
    never damaged data: the files on disk are exactly as consistent as before.
    """


class AlreadyStampedError(LedgerError):
    """An intent was asked for on a day that is already ``done``.

    Raised rather than returned because the caller's very next step is a write
    to a real card that no one can take back.  A day already on the card must
    never be stamped again, so a caller that skipped :meth:`Ledger.is_stamped`
    is stopped here.
    """


class IntentOpenError(LedgerError):
    """A new, different intent was asked for while an earlier one is still open.

    The earlier intent may already be on the card.  Re-check it by exact pointer
    line first, then :meth:`Ledger.stamp_done` it (it landed) or
    :meth:`Ledger.drop_intent` it (it did not).  Letting a different intent
    overwrite it is how a day ends up with a mechanical line AND a summary line.
    """


class LedgerCorrupt(Exception):  # noqa: N818 (the contract names it)
    """A ledger file cannot be trusted, and has been left exactly as found.

    Deliberately NOT a :class:`LedgerError`: this is damaged data, not a caller's
    mistake, and it is handled differently (the morning line says the texts are
    paused).  The message names the file and never quotes its contents, which
    are the member's texts.
    """

    def __init__(self, path: Path | str, problem: str) -> None:
        self.path = Path(path)
        self.problem = problem
        super().__init__(
            f"{self.path} is damaged ({problem}). Nothing was changed and the file "
            "was left exactly as found, because starting again from an empty ledger "
            "would re-file every conversation-day already on the cards. Repair or "
            "restore that file by hand, then run again."
        )


# ---------------------------------------------------------------------------
# Paths and the lock.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerPaths:
    """The four files, all derived from one ``home`` so a test can never reach
    the real plug-in folder by forgetting to redirect one of them."""

    home: Path
    ledger: Path
    wal: Path
    lock: Path
    state: Path


def paths(home: Path | str | None = None) -> LedgerPaths:
    """The ledger's files under ``home`` (default: the plug-in folder)."""
    base = Path(home) if home is not None else Path(imconfig.HOME)
    return LedgerPaths(
        home=base,
        ledger=base / LEDGER_NAME,
        wal=base / WAL_NAME,
        lock=base / LOCK_NAME,
        state=base / STATE_NAME,
    )


def lock_target(home: Path | str | None = None) -> Path:
    """The path to hand the engine's ``file_lock`` so it locks ``ledger.lock``.

    The engine's ``file_lock(p)`` locks the sidecar
    ``p.with_suffix(p.suffix + ".lock")``.  For ``<home>/ledger`` that is
    ``<home>/ledger.lock``: the same file :func:`default_lock` locks, with the same
    primitive, so an engine-locked run and a default-locked one exclude each
    other.  Handing it ``ledger.json`` instead would lock ``ledger.json.lock`` and
    silently stop excluding anything.
    """
    return paths(home).home / "ledger"


_LOCK_BUSY: tuple[type[BaseException], ...]

if sys.platform == "win32":
    # msvcrt raises a plain OSError (EACCES / EDEADLOCK) when another handle holds
    # the byte, so every OSError here means "try again".
    _LOCK_BUSY = (OSError,)

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    # Only EWOULDBLOCK means "held by someone else".  Any other OSError (a
    # filesystem with no flock support) is raised at once, rather than being
    # waited out and reported as a busy ledger.
    _LOCK_BUSY = (BlockingIOError,)

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def default_lock(
    home: Path | str | None = None, timeout: float = DEFAULT_LOCK_TIMEOUT_S
) -> Iterator[None]:
    """An exclusive lock on ``<home>/ledger.lock``, stdlib only, for tests and CLI use.

    The same primitives as the engine's ``file_lock`` (``flock`` / ``msvcrt``)
    on the same file (see :func:`lock_target`), so it interoperates with it.  The
    lock file is created owner-only and never truncated.  A process that dies
    holding it releases it with its descriptors, SIGKILL included, so a killed
    run never leaves the ledger locked.  Raises :class:`TimeoutError` when another
    holder keeps it past ``timeout``.
    """
    lock_path = paths(home).lock
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o600)
    acquired = False
    try:
        _chmod_600(lock_path)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                _try_lock(fd)
                acquired = True
                break
            except _LOCK_BUSY:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not take {lock_path.name} within {timeout}s: "
                        "another run is using the texts ledger"
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                _unlock(fd)
        os.close(fd)


LockArg = Callable[[], AbstractContextManager[Any]] | AbstractContextManager[Any] | None


def _lock_for(lock_cm: LockArg, p: LedgerPaths) -> AbstractContextManager[Any]:
    """Turn the caller's lock argument into one context manager.

    A context-manager OBJECT is checked first, because a
    ``@contextmanager``-built one is also callable (it doubles as a decorator),
    and calling it would not take the lock at all.
    """
    if lock_cm is None:
        return default_lock(p.home)
    if hasattr(lock_cm, "__enter__") and hasattr(lock_cm, "__exit__"):
        return lock_cm
    if callable(lock_cm):
        made = lock_cm()
        if hasattr(made, "__enter__") and hasattr(made, "__exit__"):
            return made
        raise TypeError("lock_cm() must return a context manager")
    raise TypeError("lock_cm must be a zero-argument callable returning a context manager")


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------


def _warn(message: str) -> None:
    """One diagnostic line on stderr.  Stdout belongs to the stage's JSON."""
    print(f"imledger: {message}", file=sys.stderr)


def _chmod_600(path: Path) -> None:
    """Owner-only where the OS means it; a silent no-op where it does not.

    POSIX modes are advisory on Windows (``os.chmod`` can only flip the read-only
    bit there, and some filesystems raise).  A ledger that saved is worth more
    than a mode that did not, so this degrades rather than crashing.
    """
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def _now_iso() -> str:
    """A timezone-aware UTC timestamp.  The engine's clock is not importable here,
    and an aware string compares correctly against any other aware time."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json_default(value: Any) -> Any:
    """Dates and paths become strings; anything else is refused.

    Refused rather than ``str()``-ed, so an object that would not survive a
    round trip is caught when it is written, not discovered after a reload.
    """
    if isinstance(value, date):  # datetime is a date subclass
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"a {type(value).__name__} cannot be stored in the ledger")


def _dumps(obj: Any) -> str:
    """Compact, NaN-free JSON.  Compact because the C encoder only runs without
    ``indent``, which is the difference that keeps compaction inside the budget."""
    return json.dumps(
        obj,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )


def _canon(obj: Any) -> Any:
    """``obj`` exactly as a reload would produce it.

    Memory holds only JSON round-tripped values, so what a session sees is
    byte-for-byte what the next session replays: no tuple that becomes a list, no
    date that becomes a string, only after a restart.
    """
    return json.loads(_dumps(obj))


def _record(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    out = _canon(dict(value))
    assert isinstance(out, dict)
    return out


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _epoch(value: datetime | float | int | None, what: str) -> float:
    """Epoch seconds from an aware datetime or a number (``None`` = now).

    A naive datetime is refused: read as this machine's zone it can be hours away
    from the configured one, and a lease that is hours wrong either strands a
    unit or lets two summary passes write the same day.
    """
    if value is None:
        return time.time()
    if isinstance(value, bool):
        raise TypeError(f"{what} must be a timezone-aware datetime or epoch seconds")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{what} must be timezone-aware")
        return value.timestamp()
    if isinstance(value, int | float):
        seconds = float(value)
        if not math.isfinite(seconds):
            raise ValueError(f"{what} must be a finite time")
        return seconds
    raise TypeError(f"{what} must be a timezone-aware datetime or epoch seconds")


def _day_key(key: Any) -> tuple[str, date]:
    """Validate a ledger key: three parts, DAY grain, canonical spelling.

    The canonical check matters: ``date.fromisoformat`` also accepts
    ``20260914``, so without it one day could be spelled as two keys and land
    twice.  Messages never quote the key, which carries a phone number or email.
    """
    if not isinstance(key, str):
        raise TypeError("a ledger key must be a string")
    parsed = imthreads.parse_ledger_key(key)
    if parsed is None:
        raise ValueError(
            "not a ledger key: build it with imthreads.ledger_key(identifier, DAY_GRAIN, day)"
        )
    ident, grain, day = parsed
    if grain != imthreads.DAY_GRAIN:
        raise ValueError(
            f"only {imthreads.DAY_GRAIN!r}-grain keys are written (one line per "
            f"conversation-day, ruled 2026-09-24); this key has grain {grain!r}"
        )
    if imthreads.ledger_key(ident, imthreads.DAY_GRAIN, day) != key:
        raise ValueError("the ledger key is not in canonical form; build it with ledger_key")
    return ident, day


def _ident(ident: Any) -> str:
    if not isinstance(ident, str) or not ident.strip():
        raise ValueError("an identifier must be a non-empty string")
    return ident


def _fsync_dir(directory: Path) -> None:
    """Make a rename durable on POSIX.  Windows cannot open a directory for this,
    and some filesystems refuse it; both are skipped, as the rename itself is
    still atomic."""
    if sys.platform == "win32":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(target: Path, payload: bytes) -> None:
    """The house atomic write: payload serialised FIRST (by the caller), a temp
    file in the SAME directory (so ``os.replace`` is a rename within one
    filesystem, and atomic), fsynced, owner-only, then renamed over the target.
    A crash leaves the old file whole or the new one whole, never half of one."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f"{target.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _chmod_600(temp_path)
        os.replace(temp_path, target)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise
    _chmod_600(target)
    _fsync_dir(target.parent)


# ---------------------------------------------------------------------------
# Reading the files.
# ---------------------------------------------------------------------------


def _empty_maps() -> dict[str, Any]:
    return {name: {} for name in MAPS}


def _parse_json_file(path: Path, raw: bytes) -> Any:
    if not raw.strip():
        raise LedgerCorrupt(path, "the file is empty")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise LedgerCorrupt(path, "it is not UTF-8 text") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LedgerCorrupt(
            path, f"it is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
        ) from None
    except ValueError:
        raise LedgerCorrupt(path, "it is not valid JSON") from None


def _read_ledger(path: Path) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """``(maps, extra top-level keys, existed)``.  Missing is a fresh ledger;
    anything unreadable raises :class:`LedgerCorrupt` and the file is not touched.

    Every map must be present with the shape this module writes.  A ``{}`` or a
    file missing ``stamped`` would otherwise read as "nothing is on any card yet".
    An OSError other than "not found" (a permissions problem) propagates as
    itself: it is not damage, and it must not read as an empty ledger either.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _empty_maps(), {}, False
    loaded = _parse_json_file(path, raw)
    if not isinstance(loaded, dict):
        raise LedgerCorrupt(path, f"it holds a JSON {type(loaded).__name__}, not an object")
    version = loaded.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise LedgerCorrupt(path, "its format version is missing or unreadable")
    if version > FORMAT_VERSION:
        raise LedgerCorrupt(
            path,
            f"it was written by a newer version of the plug-in (format {version}, "
            f"this one reads up to {FORMAT_VERSION})",
        )
    maps: dict[str, Any] = {}
    for name in MAPS:
        if name not in loaded:
            raise LedgerCorrupt(path, f"its {name!r} map is missing")
        value = loaded[name]
        if not isinstance(value, dict):
            raise LedgerCorrupt(path, f"its {name!r} map is not an object")
        for entry in value.values():
            if name == "pending_units":
                if not isinstance(entry, list) or not all(isinstance(u, dict) for u in entry):
                    raise LedgerCorrupt(path, "a 'pending_units' entry is not a list of units")
            elif not isinstance(entry, dict):
                raise LedgerCorrupt(path, f"an entry in its {name!r} map is not an object")
        maps[name] = value
    extras = {
        key: value
        for key, value in loaded.items()
        if key not in MAPS and key not in ("version", "compacted_at")
    }
    return maps, extras, True


def _read_wal(path: Path, *, announce: bool) -> tuple[list[tuple[int, dict[str, Any]]], Any]:
    """The WAL's records (with line numbers) and the tail repair still owed.

    The repair is ``("truncate", offset)`` for a torn tail (cut it off before the
    next append, so it cannot glue itself onto the next record),
    ``("newline", 0)`` for a whole record whose newline never made it, or
    ``None``.  Nothing is written here: the repair waits for the first append,
    so a session that only reads writes nothing at all.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return [], None
    if not raw:
        return [], None
    end = raw.rfind(b"\n")
    body, tail = (raw[: end + 1], raw[end + 1 :]) if end >= 0 else (b"", raw)
    records: list[tuple[int, dict[str, Any]]] = []
    lineno = 0
    for line in body.split(b"\n")[:-1]:
        lineno += 1
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise LedgerCorrupt(path, f"line {lineno} is not a complete record") from None
        if not isinstance(obj, dict) or not isinstance(obj.get("op"), str):
            raise LedgerCorrupt(path, f"line {lineno} is not a ledger record")
        records.append((lineno, obj))
    repair: Any = None
    if tail:
        try:
            obj = json.loads(tail.decode("utf-8"))
            whole = isinstance(obj, dict) and isinstance(obj.get("op"), str)
        except (UnicodeDecodeError, ValueError):
            whole = False
        if whole:
            # A record whose closing brace landed: a prefix of a JSON object that
            # still parses as one is the whole object, so it is kept.
            records.append((lineno + 1, obj))
            repair = ("newline", 0)
        else:
            if announce:
                _warn(
                    f"{path.name}: ignored a torn last line ({len(tail)} bytes) left by "
                    "an interrupted write; it is cut off before the next change"
                )
            repair = ("truncate", end + 1)
    return records, repair


def _read_state(path: Path) -> dict[str, Any]:
    """``state.json`` as a dict; ``{}`` when it does not exist yet.

    Unreadable is :class:`LedgerCorrupt`, never ``{}``: an empty state reads as
    "first run, import yesterday only", which would silently skip every day
    between the lost watermark and yesterday.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    loaded = _parse_json_file(path, raw)
    if not isinstance(loaded, dict):
        raise LedgerCorrupt(path, f"it holds a JSON {type(loaded).__name__}, not an object")
    if "watermark" in loaded and not _is_int(loaded["watermark"]):
        raise LedgerCorrupt(
            path,
            "its watermark is not a whole number (it is the highest message row number "
            "already read, message.ROWID, and anything else cannot be trusted)",
        )
    return loaded


def load_state(home: Path | str | None = None) -> dict[str, Any]:
    """Read ``state.json`` without a session, for a read-only status line.

    Writing goes through :meth:`Ledger.save_state` only, under the lock and never
    while a run is open.
    """
    return _read_state(paths(home).state)


# ---------------------------------------------------------------------------
# Replay.  One applier per operation, used by the live path AND by replay, so
# what a session did and what the next session reconstructs cannot differ.
# Each one SETS or DELETES a fixed location; none recomputes from prior state.
# ---------------------------------------------------------------------------


def _s(op: Mapping[str, Any], field: str) -> str:
    value = op[field]
    if not isinstance(value, str):
        raise ValueError(f"{field} is not a string")
    return value


def _d(op: Mapping[str, Any], field: str) -> dict[str, Any]:
    value = op[field]
    if not isinstance(value, dict):
        raise ValueError(f"{field} is not an object")
    return value


def _apply_stamp_intent(data: dict[str, Any], op: dict[str, Any]) -> None:
    data["stamped"][_s(op, "key")] = _d(op, "record")


def _apply_stamp_done(data: dict[str, Any], op: dict[str, Any]) -> None:
    key = _s(op, "key")
    data["stamped"][key] = _d(op, "record")
    # A day on the card is no longer waiting for a summary and no longer failing.
    data["synth_queue"].pop(key, None)
    data["failures"].pop(key, None)


def _apply_drop_intent(data: dict[str, Any], op: dict[str, Any]) -> None:
    key = _s(op, "key")
    entry = data["stamped"].get(key)
    if isinstance(entry, dict) and entry.get("status") == INTENT:
        del data["stamped"][key]


def _apply_set_identifier(data: dict[str, Any], op: dict[str, Any]) -> None:
    data["identifiers"][_s(op, "ident")] = _d(op, "record")


def _apply_hold(data: dict[str, Any], op: dict[str, Any]) -> None:
    ident = _s(op, "ident")
    unit = _d(op, "unit")
    key = unit.get("key")
    if not isinstance(key, str):
        raise ValueError("a held unit has no key")
    units = data["pending_units"].setdefault(ident, [])
    for index, existing in enumerate(units):
        if existing.get("key") == key:
            units[index] = unit
            return
    units.append(unit)


def _apply_release(data: dict[str, Any], op: dict[str, Any]) -> None:
    data["pending_units"].pop(_s(op, "ident"), None)


def _apply_enqueue(data: dict[str, Any], op: dict[str, Any]) -> None:
    data["synth_queue"][_s(op, "key")] = _d(op, "record")


def _apply_dequeue(data: dict[str, Any], op: dict[str, Any]) -> None:
    data["synth_queue"].pop(_s(op, "key"), None)


def _apply_refresh_links(data: dict[str, Any], op: dict[str, Any]) -> None:
    # The WHOLE record after the refresh, set rather than merged, like every other
    # line here: replay converges however many times it runs.
    data["synth_queue"][_s(op, "key")] = _d(op, "record")


def _apply_claim(data: dict[str, Any], op: dict[str, Any]) -> None:
    data["claims"][_s(op, "claim_id")] = _d(op, "record")


def _apply_release_claim(data: dict[str, Any], op: dict[str, Any]) -> None:
    data["claims"].pop(_s(op, "claim_id"), None)


def _apply_record_failure(data: dict[str, Any], op: dict[str, Any]) -> None:
    data["failures"][_s(op, "key")] = _d(op, "record")


def _apply_clear_failure(data: dict[str, Any], op: dict[str, Any]) -> None:
    data["failures"].pop(_s(op, "key"), None)


_APPLY: dict[str, Callable[[dict[str, Any], dict[str, Any]], None]] = {
    "stamp_intent": _apply_stamp_intent,
    "stamp_done": _apply_stamp_done,
    "drop_intent": _apply_drop_intent,
    "set_identifier": _apply_set_identifier,
    "hold": _apply_hold,
    "release": _apply_release,
    "enqueue": _apply_enqueue,
    "dequeue": _apply_dequeue,
    "refresh_links": _apply_refresh_links,
    "claim": _apply_claim,
    "release_claim": _apply_release_claim,
    "record_failure": _apply_record_failure,
    "clear_failure": _apply_clear_failure,
}


@dataclass
class Recovery:
    """What the last load found in the WAL, for ``status`` and the tests."""

    replayed: int = 0
    discarded: int = 0
    intents_kept: int = 0
    torn_tail: bool = False


def _replay(
    data: dict[str, Any], records: list[tuple[int, dict[str, Any]]], wal_path: Path
) -> Recovery:
    """Apply committed segments whole; from every other segment keep only intents.

    Segments are POSITIONAL: a ``begin`` opens one and closes any still open as
    uncommitted.  So a run id reused after an interrupted run (the morning job
    re-running the same day) can never borrow the later segment's ``committed``
    marker for the earlier, unfinished one.  A record outside its run's brackets
    is treated as uncommitted: it is never trusted, except as an intent.
    """
    recovery = Recovery()
    segment: list[tuple[int, dict[str, Any]]] | None = None
    segment_run: Any = None

    def apply(lineno: int, op: dict[str, Any]) -> None:
        try:
            _APPLY[op["op"]](data, op)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise LedgerCorrupt(
                wal_path, f"line {lineno} cannot be applied ({type(exc).__name__})"
            ) from None

    def discard(items: list[tuple[int, dict[str, Any]]]) -> None:
        for lineno, op in items:
            if op["op"] == "stamp_intent":
                apply(lineno, op)
                recovery.intents_kept += 1
            else:
                recovery.discarded += 1

    for lineno, op in records:
        kind = op["op"]
        if kind == "begin":
            if segment is not None:
                discard(segment)
            segment, segment_run = [], op.get("run")
        elif kind == "committed":
            if segment is not None and op.get("run") == segment_run:
                for item in segment:
                    apply(*item)
                recovery.replayed += len(segment)
                segment = None
        elif kind == "aborted":
            if segment is not None and op.get("run") == segment_run:
                discard(segment)
                segment = None
        elif kind in _APPLY:
            if segment is not None and op.get("run") == segment_run:
                segment.append((lineno, op))
            else:
                discard([(lineno, op)])
        else:
            raise LedgerCorrupt(wal_path, f"line {lineno} names an unknown operation")
    if segment is not None:
        discard(segment)
    return recovery


# ---------------------------------------------------------------------------
# The session.
# ---------------------------------------------------------------------------


class Ledger:
    """One locked session over the ledger.  Get it from :func:`session`.

    Reads are served from memory.  Every mutation needs an open run
    (:meth:`begin_run`), appends one JSON line to the WAL, and only then changes
    memory, so memory is never ahead of disk.  Reads return copies, because a
    caller editing a returned dict in place would change memory without a WAL
    line, and the next session would silently disagree with this one.
    """

    def __init__(self, ledger_paths: LedgerPaths) -> None:
        self._paths = ledger_paths
        self._data: dict[str, Any] = _empty_maps()
        self._extras: dict[str, Any] = {}
        self._run: str | None = None
        self._fd: int | None = None
        self._repair: Any = None
        self._closed = False
        self._broken: str | None = None
        #: What the load found in the WAL (discarded work, kept intents, a torn tail).
        self.recovery = Recovery()
        #: True when there is no ledger at all but ``state.json`` carries a
        #: watermark: the ledger was lost after runs had happened, so every held and
        #: queued day it knew about is gone.  Surfaced, never guessed around.
        self.missing_but_watermarked = False

    # -- loading and closing --------------------------------------------------

    def _load(self, *, announce: bool) -> None:
        data, extras, existed = _read_ledger(self._paths.ledger)
        records, repair = _read_wal(self._paths.wal, announce=announce)
        recovery = _replay(data, records, self._paths.wal)
        recovery.torn_tail = bool(repair and repair[0] == "truncate")
        self._data, self._extras, self._repair, self.recovery = data, extras, repair, recovery
        if not announce:
            return
        if recovery.discarded or recovery.intents_kept:
            _warn(
                f"a run that never committed was set aside: {recovery.discarded} change(s) "
                f"discarded, {recovery.intents_kept} stamp intent(s) kept to re-check "
                "against the cards"
            )
        if not existed and not records:
            with contextlib.suppress(LedgerCorrupt, OSError):
                if "watermark" in _read_state(self._paths.state):
                    self.missing_but_watermarked = True
                    _warn(
                        f"{LEDGER_NAME} is missing but {STATE_NAME} records a watermark: "
                        "held and queued days from earlier runs are no longer known"
                    )

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._run is not None:
            _warn(
                "a run ended without commit_run; its changes will be discarded on the "
                "next load (stamp intents are kept)"
            )
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None

    # -- the WAL ----------------------------------------------------------------

    def _require_writable(self) -> None:
        if self._closed:
            raise LedgerError("this ledger session is closed; open a new one")
        if self._broken is not None:
            raise LedgerError(
                "the ledger log could not be repaired after a failed write "
                f"({self._broken}); this session refuses further changes"
            )

    def _ensure_fd(self) -> int:
        if self._fd is None:
            self._paths.home.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(
                self._paths.wal, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_BINARY, 0o600
            )
            _chmod_600(self._paths.wal)
        if self._repair is not None:
            action, offset = self._repair
            if action == "truncate":
                os.ftruncate(self._fd, offset)
            else:
                os.write(self._fd, b"\n")
            self._repair = None
        return self._fd

    def _append(self, line: bytes) -> None:
        """One record, one ``os.write`` where the kernel allows.

        No Python buffer sits in between, so once this returns the line survives
        a SIGKILL.  A write that fails part-way is cut back to where it started,
        so a later append can never land after half a record.  If even that fails,
        the session refuses every further change rather than leave a damaged
        middle line for the next load.
        """
        self._require_writable()
        fd = self._ensure_fd()
        start = os.lseek(fd, 0, os.SEEK_END)
        try:
            view = memoryview(line)
            while view:
                written = os.write(fd, view)
                view = view[written:]
        except BaseException:
            try:
                os.ftruncate(fd, start)
            except OSError as exc:
                self._broken = exc.strerror or type(exc).__name__
            raise

    def _marker(self, kind: str, run_id: str) -> None:
        self._append((_dumps({"op": kind, "run": run_id, "at": _now_iso()}) + "\n").encode())

    def _mutate(self, op_name: str, **fields: Any) -> dict[str, Any]:
        self._require_writable()
        if self._run is None:
            raise LedgerError("no run is open: call begin_run(run_id) before changing the ledger")
        line = _dumps({"op": op_name, "run": self._run, **fields})
        op = json.loads(line)
        self._append(line.encode("utf-8") + b"\n")
        _APPLY[op_name](self._data, op)
        assert isinstance(op, dict)
        return op

    # -- runs -------------------------------------------------------------------

    @property
    def run_id(self) -> str | None:
        """The open run, or ``None``."""
        return self._run

    def _require_this_run(self, run_id: str) -> None:
        if self._run is None:
            raise LedgerError("no run is open")
        if run_id != self._run:
            raise LedgerError("that is not the run that is open")

    def begin_run(self, run_id: str) -> None:
        """Open a run segment.  Its changes count only once :meth:`commit_run` lands."""
        self._require_writable()
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if self._run is not None:
            raise LedgerError("a run is already open; commit or abort it first")
        self._marker("begin", run_id)
        self._run = run_id

    def commit_run(self, run_id: str) -> None:
        """Write the ``committed`` marker and fsync it: the run now counts.

        The marker IS the commit: once it is written the run counts (a SIGKILL
        cannot take it back), so the run is closed before the fsync.  The fsync is
        one per run and cheap; it makes the commit survive power loss too, before
        the watermark can move past it, and an error from it is raised, not hidden.
        """
        self._require_writable()
        self._require_this_run(run_id)
        self._marker("committed", run_id)
        self._run = None
        os.fsync(self._ensure_fd())

    def abort_run(self, run_id: str) -> None:
        """Abandon the open run: memory is rebuilt from disk, exactly as a crash
        would have left it (its intents kept, everything else gone)."""
        self._require_writable()
        self._require_this_run(run_id)
        self._marker("aborted", run_id)
        self._run = None
        self._load(announce=False)

    @contextlib.contextmanager
    def run(self, run_id: str) -> Iterator[Ledger]:
        """``with led.run(run_id):`` commits on success and aborts on any exception."""
        self.begin_run(run_id)
        try:
            yield self
        except BaseException:
            if self._run == run_id:
                try:
                    self.abort_run(run_id)
                except Exception as exc:  # the original error is the one that matters
                    _warn(f"could not abort the run cleanly ({type(exc).__name__})")
            raise
        self.commit_run(run_id)

    # -- compaction ----------------------------------------------------------

    def compact(self) -> dict[str, Any]:
        """Fold the WAL into ``ledger.json`` atomically, then truncate the WAL.

        Refused while a run is open, or ``ledger.json`` would hold changes that
        never committed.  Serialised first, written to a temp file in the same
        directory, fsynced, chmod 0600, renamed over the old one, and only then is
        the WAL truncated.  A kill between the rename and the truncate replays a
        WAL the snapshot already contains, which converges (see the module
        docstring).  Returns what it did, with its timing.
        """
        self._require_writable()
        if self._run is not None:
            raise LedgerError("a run is open: commit or abort it before compacting")
        started = time.perf_counter()
        wal_bytes = self._wal_size()
        if wal_bytes == 0 and self._repair is None and self._paths.ledger.exists():
            return {"skipped": True, "bytes": 0, "wal_bytes": 0, "seconds": 0.0}
        document: dict[str, Any] = {"version": FORMAT_VERSION, **self._extras}
        for name in MAPS:
            document[name] = self._data[name]
        document["compacted_at"] = _now_iso()
        payload = (_dumps(document) + "\n").encode("utf-8")
        _atomic_write(self._paths.ledger, payload)
        if self._paths.wal.exists():
            fd = self._ensure_fd()
            os.ftruncate(fd, 0)
            os.fsync(fd)
            _chmod_600(self._paths.wal)
        self._sweep_temps()
        return {
            "skipped": False,
            "bytes": len(payload),
            "wal_bytes": wal_bytes,
            "seconds": round(time.perf_counter() - started, 4),
        }

    def _wal_size(self) -> int:
        try:
            return os.stat(self._paths.wal).st_size
        except FileNotFoundError:
            return 0

    def _sweep_temps(self) -> None:
        """Remove temp files a killed atomic write left behind.  Safe under the
        lock: no other writer of these two files can be mid-write."""
        for name in (LEDGER_NAME, STATE_NAME):
            for stray in self._paths.home.glob(f"{name}.*.tmp"):
                with contextlib.suppress(OSError):
                    stray.unlink()

    # -- stamped -------------------------------------------------------------

    @property
    def stamped(self) -> Mapping[str, Any]:
        """A read-only view of the ``stamped`` map, for
        ``imthreads.stamped_periods(identifier, led)``.  A view, not a copy: it is
        read once per person-day, and copying 6,000 entries each time would cost
        seconds.  Never mutate what it holds."""
        return MappingProxyType(self._data["stamped"])

    def is_stamped(self, key: str) -> bool:
        """True once the day is ``done`` on the card.  An open intent is not."""
        entry = self._data["stamped"].get(key)
        if entry is None:
            return False
        # Anything that is not plainly an intent counts as landed: when unsure,
        # never file the day again.
        return not (isinstance(entry, dict) and entry.get("status") == INTENT)

    def stamp_record(self, key: str) -> dict[str, Any] | None:
        """A copy of the entry for ``key`` (intent or done), or ``None``."""
        entry = self._data["stamped"].get(key)
        return copy.deepcopy(entry) if entry is not None else None

    def stamp_intent(self, key: str, record: Mapping[str, Any]) -> None:
        """Write the intent BEFORE touching the card.

        ``record`` must carry the exact ``topic`` (and should carry ``link``,
        ``person_id``, ``turns``) the card line will use: after a crash, the intent
        is the only way to find that line again by exact pointer.  An identical
        intent already open is a no-op.  Raises :class:`AlreadyStampedError` for a day
        already on the card, :class:`IntentOpenError` for a different open intent.
        """
        _day_key(key)
        rec = _record(record, "record")
        if rec.get("grain", imthreads.DAY_GRAIN) != imthreads.DAY_GRAIN:
            raise ValueError("a stamp's grain is always day")
        topic = rec.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError(
                "an intent must carry the exact topic, so the card can be re-checked "
                "by pointer line after a crash"
            )
        existing = self._data["stamped"].get(key)
        if existing is not None:
            if self.is_stamped(key):
                raise AlreadyStampedError("that conversation-day is already on the card")
            if _pointer_fields(existing) == _pointer_fields(rec):
                return
            raise IntentOpenError(
                "an earlier intent for that day is still open: re-check it against the "
                "card, then stamp_done or drop_intent it"
            )
        rec["grain"] = imthreads.DAY_GRAIN
        rec["status"] = INTENT
        rec.setdefault("intent_at", _now_iso())
        self._mutate("stamp_intent", key=key, record=rec)

    def stamp_done(self, key: str, record: Mapping[str, Any] | None = None) -> bool:
        """Mark the day landed.  Only on ``stamped is True`` from the engine.

        Merges ``record`` over the open intent (if any).  Also takes the key off
        the summary queue and clears its failure.  Returns False, changing
        nothing, when the day is already done: its topic and turns are frozen.
        """
        _day_key(key)
        rec = _record(record or {}, "record")
        if rec.get("grain", imthreads.DAY_GRAIN) != imthreads.DAY_GRAIN:
            raise ValueError("a stamp's grain is always day")
        if self.is_stamped(key):
            return False
        existing = self._data["stamped"].get(key)
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(rec)
        merged["grain"] = imthreads.DAY_GRAIN
        merged["status"] = DONE
        merged.setdefault("stamped_at", _now_iso())
        self._mutate("stamp_done", key=key, record=merged)
        return True

    def drop_intent(self, key: str) -> bool:
        """Forget an open intent that the card check proved never landed."""
        entry = self._data["stamped"].get(key)
        if not (isinstance(entry, dict) and entry.get("status") == INTENT):
            return False
        self._mutate("drop_intent", key=key)
        return True

    def open_intents(self) -> dict[str, dict[str, Any]]:
        """Every intent without its ``done``: re-check each against the card by
        exact pointer line before anything else in a run."""
        return {
            key: copy.deepcopy(entry)
            for key, entry in self._data["stamped"].items()
            if isinstance(entry, dict) and entry.get("status") == INTENT
        }

    # -- identifiers ---------------------------------------------------------

    def identifier(self, ident: str) -> dict[str, Any] | None:
        """A copy of the identifier's record, or ``None`` if the ledger never saw it."""
        entry = self._data["identifiers"].get(ident)
        return copy.deepcopy(entry) if entry is not None else None

    def all_identifiers(self) -> dict[str, dict[str, Any]]:
        """Copies of every identifier record (for ``status`` and ``review``)."""
        return copy.deepcopy(self._data["identifiers"])

    def set_identifier(self, ident: str, **fields: Any) -> dict[str, Any]:
        """Merge ``fields`` into the identifier's record and return the result.

        A new identifier needs a ``state``; a ``state`` must be one of
        :data:`IDENTIFIER_STATES`.  A merge that changes nothing writes nothing.
        """
        ident = _ident(ident)
        if "state" in fields and fields["state"] not in IDENTIFIER_STATES:
            raise ValueError(f"state must be one of {sorted(IDENTIFIER_STATES)}")
        existing = self._data["identifiers"].get(ident)
        merged = dict(existing) if isinstance(existing, dict) else {}
        if not merged and "state" not in fields:
            raise ValueError("a new identifier needs a state")
        merged.update(fields)
        canonical = _record(merged, "fields")
        if canonical != existing:
            self._mutate("set_identifier", ident=ident, record=canonical)
        return copy.deepcopy(canonical)

    # -- held units ----------------------------------------------------------

    def hold(self, ident: str, unit_record: Mapping[str, Any]) -> bool:
        """Hold a unit against an unresolved identifier until it resolves.

        Keyed by the unit's ledger key: holding the same day again (a morning
        re-run) replaces it rather than holding it twice.  The key must belong to
        ``ident``, so a day can never be parked under the wrong person.  Returns
        False, changing nothing, for a day already on a card.
        """
        ident = _ident(ident)
        unit = _record(unit_record, "unit_record")
        key_ident, _ = _day_key(unit.get("key"))
        if key_ident != ident:
            raise ValueError("the unit's ledger key belongs to a different identifier")
        if self.is_stamped(unit["key"]):
            return False
        for existing in self._data["pending_units"].get(ident, []):
            if existing.get("key") == unit["key"] and existing == unit:
                return True
        self._mutate("hold", ident=ident, unit=unit)
        return True

    def held(self, ident: str) -> list[dict[str, Any]]:
        """Copies of the units held against ``ident`` (oldest hold first)."""
        return copy.deepcopy(self._data["pending_units"].get(ident, []))

    def release(self, ident: str) -> list[dict[str, Any]]:
        """Remove and return every unit held against ``ident``.

        Call it inside the same run that enqueues them, so the move is one
        commit: a crash between the two can then never lose the days.
        """
        units = self._data["pending_units"].get(ident)
        if not units:
            return []
        out: list[dict[str, Any]] = copy.deepcopy(units)
        self._mutate("release", ident=ident)
        return out

    # -- the summary queue ---------------------------------------------------

    def enqueue(self, record: Mapping[str, Any]) -> bool:
        """Queue a unit for its summary.  Idempotent on the record's ``key``.

        Returns True when newly queued.  Returns False, writing nothing, when the
        key is already queued (the first record, and its ``queued_at``, stand, so
        a re-run cannot reset how stale a unit is) or already stamped done (the
        refusal: that day is on the card).  ``queued_at`` is filled when absent.
        """
        rec = _record(record, "record")
        key = rec.get("key")
        key_ident, _ = _day_key(key)
        if "identifier" in rec and rec["identifier"] != key_ident:
            raise ValueError("the record's identifier does not match its ledger key")
        assert isinstance(key, str)
        if self.is_stamped(key) or key in self._data["synth_queue"]:
            return False
        rec.setdefault("queued_at", _now_iso())
        self._mutate("enqueue", key=key, record=rec)
        return True

    def queued(self) -> dict[str, dict[str, Any]]:
        """Copies of every queued unit, by ledger key."""
        return copy.deepcopy(self._data["synth_queue"])

    def is_queued(self, key: str) -> bool:
        return key in self._data["synth_queue"]

    def queued_record(self, key: str) -> dict[str, Any] | None:
        """A copy of ONE queued unit, or ``None``.  :meth:`queued` copies them all,
        which is the wrong price for a per-day check inside a run."""
        entry = self._data["synth_queue"].get(key)
        return copy.deepcopy(entry) if entry is not None else None

    def dequeue(self, key: str) -> dict[str, Any] | None:
        """Remove and return a queued unit (``None`` if it was not queued)."""
        entry = self._data["synth_queue"].get(key)
        if entry is None:
            return None
        out: dict[str, Any] = copy.deepcopy(entry)
        self._mutate("dequeue", key=key)
        return out

    def refresh_links(self, key: str, links: Iterable[str]) -> bool:
        """Add a queued day's newly written transcripts to its record.  Links ONLY.

        Why it exists: a queued record is frozen (E6), but a conversation that
        syncs late can add a whole new chat to a day already queued.  Its
        transcript is written, and without this the summary writer, who reads
        only the links on the record, would summarise the day without it.

        What it never changes: the ``topic`` and ``turns`` (frozen, because the
        engine's idempotency key contains the topic), and the ORDER of the links
        already there.  New links are appended after them, so the first link, the
        one the card's pointer carries and the mechanical topic describes, stays
        the first.  A link is never removed: the transcript it names is still on
        disk and still part of that day.

        Returns True when the record changed, False (writing nothing) when every
        link was already there or the day is not queued.  Raises
        :class:`AlreadyStampedError` for a day already on a card: its line is
        written and nothing about it may move.
        """
        _day_key(key)
        if isinstance(links, str):
            raise TypeError("links must be a collection of links, not one string")
        wanted = list(links)
        if not all(isinstance(link, str) and link.strip() for link in wanted):
            raise ValueError("every link must be a non-empty string")
        if self.is_stamped(key):
            raise AlreadyStampedError("that conversation-day is already on the card")
        entry = self._data["synth_queue"].get(key)
        if not isinstance(entry, dict):
            return False
        have = entry.get("links")
        current = [link for link in have if isinstance(link, str)] if isinstance(have, list) else []
        merged = current + [link for link in dict.fromkeys(wanted) if link not in current]
        if merged == have:
            return False
        record = _record({**entry, "links": merged}, "record")
        self._mutate("refresh_links", key=key, record=record)
        return True

    # -- claims --------------------------------------------------------------

    def claim(
        self,
        keys: Iterable[str],
        claim_id: str,
        lease_until: datetime | float | int,
        now: datetime | float | int | None = None,
    ) -> list[str]:
        """Lease queued units to one summary pass; return the keys actually taken.

        Skipped: keys not queued, keys already done, and keys under a LIVE lease
        held by a different claim.  An expired lease blocks nothing.  Claiming
        again under the same ``claim_id`` replaces that claim.  A claim that
        takes nothing writes nothing.  ``lease_until`` and ``now`` are aware
        datetimes or epoch seconds; the lease is stored as epoch seconds.
        """
        if not isinstance(claim_id, str) or not claim_id.strip():
            raise ValueError("claim_id must be a non-empty string")
        if isinstance(keys, str):
            raise TypeError("keys must be a collection of ledger keys, not one string")
        lease = _epoch(lease_until, "lease_until")
        at = _epoch(now, "now")
        if lease <= at:
            raise ValueError("lease_until must be later than now")
        taken: list[str] = []
        for key in keys:
            if key in taken or key not in self._data["synth_queue"] or self.is_stamped(key):
                continue
            holder = self.live_claim(key, at)
            if holder is not None and holder != claim_id:
                continue
            taken.append(key)
        if not taken:
            return []
        record = {"keys": taken, "lease_until": lease, "claimed_at": _now_iso()}
        self._mutate("claim", claim_id=claim_id, record=record)
        return list(taken)

    def release_claim(self, claim_id: str) -> bool:
        """End a claim.  Its units stay queued (or were dequeued as they landed)."""
        if claim_id not in self._data["claims"]:
            return False
        self._mutate("release_claim", claim_id=claim_id)
        return True

    def claim_of(self, claim_id: str) -> dict[str, Any] | None:
        """A copy of a claim's record (``keys``, ``lease_until``, ``claimed_at``)."""
        entry = self._data["claims"].get(claim_id)
        return copy.deepcopy(entry) if entry is not None else None

    def live_claim(self, key: str, now: datetime | float | int | None = None) -> str | None:
        """The claim id holding a live lease on ``key``, or ``None``."""
        at = _epoch(now, "now")
        best: str | None = None
        best_lease = -math.inf
        for claim_id, entry in self._data["claims"].items():
            if not isinstance(entry, dict):
                continue
            lease = entry.get("lease_until")
            if isinstance(lease, bool) or not isinstance(lease, int | float) or lease <= at:
                continue
            held_keys = entry.get("keys")
            if isinstance(held_keys, list) and key in held_keys and lease > best_lease:
                best, best_lease = claim_id, float(lease)
        return best

    def prune_claims(self, now: datetime | float | int | None = None) -> list[str]:
        """Release every claim whose lease has passed; return their ids."""
        at = _epoch(now, "now")
        expired = [
            claim_id
            for claim_id, entry in self._data["claims"].items()
            if isinstance(entry, dict)
            and not isinstance(entry.get("lease_until"), bool)
            and isinstance(entry.get("lease_until"), int | float)
            and entry["lease_until"] <= at
        ]
        for claim_id in expired:
            self._mutate("release_claim", claim_id=claim_id)
        return expired

    # -- failures ------------------------------------------------------------

    def record_failure(self, key: str, detail: str) -> int:
        """Record a ``stamped=False`` (or a caught error) for a day; return the count.

        The engine returns ``stamped=False`` without raising for several causes,
        and a day that failed quietly is a day lost; this is where ``status`` finds
        them.  Cleared automatically when the day lands.
        """
        _day_key(key)
        previous = self._data["failures"].get(key)
        count = 1
        if isinstance(previous, dict) and _is_int(previous.get("count")):
            count = previous["count"] + 1
        record = {"detail": str(detail), "count": count, "at": _now_iso()}
        self._mutate("record_failure", key=key, record=record)
        return count

    def clear_failure(self, key: str) -> bool:
        if key not in self._data["failures"]:
            return False
        self._mutate("clear_failure", key=key)
        return True

    def failures(self) -> dict[str, dict[str, Any]]:
        """Copies of every recorded failure, by ledger key."""
        return copy.deepcopy(self._data["failures"])

    # -- status --------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        """The sizes ``status`` reports.  Counts only: no names, no numbers."""
        stamped = self._data["stamped"]
        intents = sum(
            1
            for entry in stamped.values()
            if isinstance(entry, dict) and entry.get("status") == INTENT
        )
        pending = self._data["pending_units"]
        return {
            "stamped": len(stamped) - intents,
            "open_intents": intents,
            "identifiers": len(self._data["identifiers"]),
            "held_identifiers": len(pending),
            "held_units": sum(len(units) for units in pending.values()),
            "queued": len(self._data["synth_queue"]),
            "claims": len(self._data["claims"]),
            "failures": len(self._data["failures"]),
        }

    # -- state.json ----------------------------------------------------------

    def load_state(self) -> dict[str, Any]:
        """``state.json`` as a dict (``{}`` before the first run)."""
        return _read_state(self._paths.state)

    def save_state(self, state: Mapping[str, Any]) -> None:
        """Replace ``state.json`` atomically, owner-only.

        Refused while a run is open.  The watermark is written LAST: a watermark
        that moved past days whose run then never committed would skip those days
        for good, because they are never read again.  ``watermark``, when present,
        must be an int: the highest ``message.ROWID`` a run consumed (see
        ``imrun``'s docstring for why it is a row number and not the send date).
        A float, a string or a bool is refused, because a row number that is not
        exact means rows read twice or never.
        """
        self._require_writable()
        if self._run is not None:
            raise LedgerError(
                "a run is open: commit it before moving the watermark, or a crash could "
                "leave the watermark past days that never landed"
            )
        if not isinstance(state, Mapping):
            raise TypeError("state must be a mapping")
        if "watermark" in state and not _is_int(state["watermark"]):
            raise ValueError(
                "the watermark must be a whole number: the highest message row "
                "(message.ROWID) already read"
            )
        payload = (
            json.dumps(
                dict(state), ensure_ascii=False, indent=2, allow_nan=False, default=_json_default
            )
            + "\n"
        ).encode("utf-8")
        _atomic_write(self._paths.state, payload)


def _pointer_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in _VOLATILE_STAMP_FIELDS}


@contextlib.contextmanager
def session(lock_cm: LockArg = None, *, home: Path | str | None = None) -> Iterator[Ledger]:
    """Open the ledger under the lock and keep the lock until the block ends.

    ``lock_cm`` is a zero-argument callable returning a context manager (a
    context-manager object is accepted too).  In production pass the engine's
    lock on :func:`lock_target`::

        with imledger.session(lambda: imspine.file_lock(imledger.lock_target(home)),
                              home=home) as led:

    ``None`` uses :func:`default_lock`, which excludes the engine's lock on the
    same file.  The lock is taken BEFORE anything is read, because the load cuts
    a torn tail and replays the WAL, and doing either while another writer
    appends would be a race.  A run left open when the block ends is not
    committed: the next load discards it and keeps its intents.
    """
    ledger_paths = paths(home)
    with _lock_for(lock_cm, ledger_paths):
        _chmod_600(ledger_paths.lock)
        led = Ledger(ledger_paths)
        led._load(announce=True)
        try:
            yield led
        finally:
            led._close()
