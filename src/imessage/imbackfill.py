"""iMessage intake — ``backfill``: bring older texts in, a tranche at a time, resumable, dry first.

Why this module exists
----------------------
The morning run reads only what arrived since it last ran; the first one reads yesterday.
Everything older is this verb's.  It runs the SAME pipeline as ``daily``
(:class:`imrun.Pipeline`: read → group → floor → fold → write the transcripts → resolve →
queue or hold) over a window of closed days the member names, so a backfilled day is
indistinguishable from a daily one except for one field: ``origin: "backfill"``.

The rules it runs under
-----------------------
* **Dry by default.**  Without ``confirm`` it reads the window and reports how many
  conversation-days and people it holds, how many cards it would reach, and how long a
  real run would take, measured from this very read (see :func:`estimate`), BEFORE any
  yes.  It writes nothing: no transcript, no ledger line, no lock file, no memory.db row.
* **It never raises a card.**  ``emit_new=False, reopen=False, follow_through=False``: a
  new number is held for the texts review list; a number the member dismissed stays
  dismissed however much it was texted since (KNOWN LIMIT 9); and a card the member
  accepted elsewhere is not followed through here (the next daily run does that).  The
  people queue gains nothing, ever, from a backfill.
* **Never daily's watermark.**  The watermark is the highest message row the morning run
  consumed; a backfill reads by DATE and never moves it.  Its own progress lives beside it
  in ``state.json`` under ``backfill``: the window, the next day to read, and whether it
  finished.
* **Tranched and paced.**  ``backfill_tranche_days`` days per tranche, each tranche one
  ledger session and one committed run (so a morning run waiting on the ledger lock gets
  in between tranches), with a pause of ``pause_s`` seconds between tranches.
* **Resumable.**  A tranche's progress is saved only after its ledger run commits.  A
  backfill killed mid-tranche (SIGKILL included) leaves that tranche uncommitted, which
  the ledger discards on its next load; asking for the same window again continues from
  the first unfinished tranche.  Re-reading is harmless by construction: a transcript is
  rewritten whole, a queued day keeps its frozen record, a filed day keeps its one line.
* **Queued, never stamped here.**  A resolved day is queued for its summary with
  ``origin: "backfill"``; it reaches its card through a summary pass or, after
  ``synth_stale_days``, the morning run's plain-line fallback, ``max_stamps_per_run`` a
  morning.  Before the FIRST backfilled line lands on a card, :func:`imrun.land` copies
  that card into ``pre-backfill/`` (G2, KNOWN LIMIT 10): the backfill rolls the card's undo
  history over, and that copy is the way back.

What it never does
------------------
It never calls a model, never writes a card, never raises or re-opens a proposal, never
moves the watermark, and never reads ``chat.db`` except ``mode=ro``.
"""

from __future__ import annotations

# imconfig FIRST, before any engine module: it puts `.claude/scripts` on sys.path and
# then re-asserts this folder ahead of it. See imconfig's docstring.
import imconfig

imconfig.ensure_engine_path()

import contextlib  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import sqlite3  # noqa: E402
import time  # noqa: E402
from collections import Counter  # noqa: E402
from collections.abc import Callable, Mapping, Sequence  # noqa: E402
from datetime import date, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import imchat  # noqa: E402
import imcontacts  # noqa: E402
import imledger  # noqa: E402
import imrun  # noqa: E402
import imspine  # noqa: E402
import imthreads  # noqa: E402

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Where a backfill keeps its progress in ``state.json``: beside the watermark, never in it.
PROGRESS_KEY: str = "backfill"

#: The pause between tranches, in seconds: long enough for a morning run that is waiting
#: on the ledger lock (it waits two seconds) to take it.
PAUSE_S: float = 1.0

#: How long a tranche waits for the ledger lock before saying "busy".
LOCK_TIMEOUT_S: float = 5.0

#: One transcript write, in seconds.  Measured on a desktop Mac: 400 fixture
#: transcripts of twelve messages written through ``imthreads.write_thread``,
#: median 0.79 ms, p90 0.81 ms, max 1.2 ms.  Rounded UP to 1 ms, so the estimate errs long.
THREAD_WRITE_S: float = 0.001

#: Per tranche: opening the ledger, its run, the WAL fsync and the compaction.  The
#: ledger's own measure is 16 ms to compact a 5.1 MB ledger; this is generous.
TRANCHE_OVERHEAD_S: float = 0.1

SENTENCE_BUSY: str = (
    "texts: another texts run is using the ledger right now, so the backfill stopped where "
    "it was; ask for the same dates again in a minute and it continues from there."
)
SENTENCE_STATE_KIND: str = imrun.SENTENCE_STATE_KIND


def _pause(report: dict[str, Any], reason: str, sentence: str) -> dict[str, Any]:
    report["paused"] = {"reason": reason, "sentence": sentence}
    return report


# ---------------------------------------------------------------------------
# The window.
# ---------------------------------------------------------------------------


class WindowError(ValueError):
    """The dates asked for cannot be backfilled.  One plain sentence; nothing was read."""


def window(date_from: str | date, date_to: str | date, today: date) -> list[date]:
    """Every day from ``date_from`` to ``date_to``, both included, oldest first.

    Closed days only: ``date_to`` must be before ``today`` (the configured zone's), because
    today is still being texted and the morning run files it once it is over.
    """
    def parse(value: str | date, label: str) -> date:
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value).strip())
        except ValueError:
            raise WindowError(f"{label} takes a day written YYYY-MM-DD, not {value!r}") from None

    start, end = parse(date_from, "--from"), parse(date_to, "--to")
    if end < start:
        raise WindowError(f"--to ({end}) is before --from ({start}); nothing was read")
    if end >= today:
        raise WindowError(
            f"--to must be before today ({today}): today is not over yet, and the morning "
            "run files it once it is. The last day a backfill reads is yesterday."
        )
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def _tranches(days: Sequence[date], size: int) -> list[list[date]]:
    size = max(1, int(size))
    return [list(days[i:i + size]) for i in range(0, len(days), size)]


# ---------------------------------------------------------------------------
# Progress (in state.json, beside the watermark).
# ---------------------------------------------------------------------------


def _progress(state: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = state.get(PROGRESS_KEY)
    return dict(raw) if isinstance(raw, Mapping) else None


def resume_point(
    state: Mapping[str, Any], days: Sequence[date]
) -> tuple[int, dict[str, Any] | None]:
    """``(index of the first day still to read, an unfinished OTHER window or None)``.

    The same window, unfinished, resumes at its saved ``next`` day.  The same window
    finished, or any other window, starts from its first day (re-reading is harmless);
    an unfinished other window is handed back so the report can say it was set aside.
    """
    saved = _progress(state)
    if not saved or not days:
        return 0, None
    same = (saved.get("from") == days[0].isoformat() and saved.get("to") == days[-1].isoformat())
    if same and not saved.get("complete"):
        try:
            nxt = date.fromisoformat(str(saved.get("next")))
        except ValueError:
            return 0, None
        for index, day in enumerate(days):
            if day >= nxt:
                return index, None
        return len(days), None
    if not same and not saved.get("complete"):
        return 0, saved
    return 0, None


# ---------------------------------------------------------------------------
# The estimate — measured, never guessed.
# ---------------------------------------------------------------------------


def estimate(
    *, read_s: float, threads: int, person_days: int, tranches: int, pause_s: float,
    queued: int, cfg: Mapping[str, Any],
) -> dict[str, Any]:
    """How long a real run takes, built from what this dry run MEASURED.

    ``read_s`` is the dry run's own wall time for reading, sorting and looking every number
    up: a real run repeats exactly that work.  On top: one transcript write per
    conversation (:data:`THREAD_WRITE_S`, measured), a ledger line per day (negligible,
    counted anyway), a fixed cost per tranche and the pauses between tranches.  Rounded
    UP.  ``landing`` says the other half honestly: when the queued days reach the cards.
    """
    write_s = threads * THREAD_WRITE_S
    ledger_s = person_days * 0.0001
    pauses = max(0, tranches - 1) * max(0.0, float(pause_s))
    overhead = tranches * TRANCHE_OVERHEAD_S
    total = read_s + write_s + ledger_s + pauses + overhead
    per_morning = max(1, imrun._int_cfg(cfg, "max_stamps_per_run"))
    return {
        "seconds": math.ceil(total),
        "said": say_duration(total),
        "read_s": round(read_s, 3),
        "write_s": round(write_s, 2),
        "pause_s": round(pauses, 2),
        "overhead_s": round(overhead + ledger_s, 2),
        "landing": {
            "queued": queued,
            "stale_days": imrun._int_cfg(cfg, "synth_stale_days"),
            "per_morning": per_morning,
            "mornings": math.ceil(queued / per_morning) if queued else 0,
            "per_summary_pass": imrun._int_cfg(cfg, "synth_claim_limit"),
        },
    }


def say_duration(seconds: float) -> str:
    """Seconds in the words a person uses: "about 40 seconds", "about 3 minutes"."""
    seconds = max(1, math.ceil(seconds))
    if seconds < 90:
        return f"about {seconds} second{'s' if seconds != 1 else ''}"
    minutes = math.ceil(seconds / 60)
    if minutes < 90:
        return f"about {minutes} minutes"
    return f"about {minutes / 60:.1f} hours"


# ---------------------------------------------------------------------------
# backfill()
# ---------------------------------------------------------------------------


def _new_report(confirm: bool, now: datetime) -> dict[str, Any]:
    return {
        "verb": "backfill", "confirm": bool(confirm), "paused": None, "error": None,
        "zone": imrun._zone_name(now), "today": now.date().isoformat(),
        "window": None, "tranche_days": None, "tranches": 0,
        "resumes_from": None, "set_aside": None,
        "counts": None, "estimate": None,
        "done_tranches": 0, "next": None, "complete": False, "stopped": None,
        "elapsed_s": 0.0, "watermark_before": None, "watermark_after": None,
    }


def backfill(
    date_from: str | date,
    date_to: str | date,
    *,
    confirm: bool = False,
    source: imrun.MessageSource | None = None,
    now: datetime | None = None,
    cfg: Mapping[str, Any] | None = None,
    home: Path | str | None = None,
    threads_dir: Path | str | None = None,
    db_path: Path | str | None = None,
    contacts: Mapping[str, str] | None = None,
    pause_s: float | None = None,
    max_seconds: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """A dry run (default) or, with ``confirm``, the backfill itself.  Plain data.

    ``report["paused"]`` is a gate's ``{reason, sentence}`` (nothing further was done);
    ``report["error"]`` a window that cannot be read.  ``max_seconds`` stops a confirmed
    run cleanly between tranches (the same dates again continues it).  Every keyword
    beyond the member's words exists so a test points the run at temp places.
    """
    started = time.monotonic()
    cfg = dict(cfg) if cfg is not None else imconfig.load_config()
    home_dir = Path(home) if home is not None else Path(imconfig.HOME)
    threads = Path(threads_dir) if threads_dir is not None else Path(imconfig.THREADS_DIR)
    src: imrun.MessageSource = source if source is not None else imrun.ChatDbSource()
    moment = now if now is not None else imrun._engine_now()
    pause = PAUSE_S if pause_s is None else float(pause_s)
    report = _new_report(confirm, moment)

    try:
        days = window(date_from, date_to, moment.date())
    except WindowError as exc:
        report["error"] = str(exc)
        return report
    size = max(1, imrun._int_cfg(cfg, "backfill_tranche_days"))
    report["window"] = {"from": days[0].isoformat(), "to": days[-1].isoformat(),
                        "days": len(days)}
    report["tranche_days"] = size

    # --- the gates, in daily's order ---------------------------------------------
    if not src.on_mac():
        return _pause(report, "not_mac", src.access()[1])
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
    contact_map = dict(contacts) if contacts is not None else imcontacts.load_map()

    if not confirm:
        return _dry(report, days=days, size=size, cfg=cfg, own=own, home_dir=home_dir,
                    threads=threads, source=src, now=moment, db_path=db_path,
                    contacts=contact_map, pause=pause)
    return _confirmed(report, days=days, size=size, cfg=cfg, own=own, home_dir=home_dir,
                      threads=threads, source=src, now=moment, db_path=db_path,
                      contacts=contact_map, pause=pause, started=started,
                      max_seconds=max_seconds, sleep=sleep)


def _counts(pipe: imrun.Pipeline) -> dict[str, Any]:
    t = pipe.tally
    return {
        "messages": t.messages_read,
        "days_read": t.days_read,
        "conversation_days": t.conversation_days,
        "person_days": t.person_days,
        "people": len(pipe.units),
        "cards": len(t.queued_people),
        "numbers_held": len(t.held_idents),
        "new_for_review": t.new_for_review,
        "queued": t.queued,
        "released_from_hold": t.released,
        "held": dict(sorted(t.held.items())),
        "held_total": sum(t.held.values()),
        "already_queued": t.already_queued,
        "already_on_cards": t.already_stamped,
        "your_own": t.owner_skipped,
        "threads": t.threads_written or t.threads_planned,
        "raised": sum(t.raised.values()) + sum(t.would_raise.values()),
    }


def _dry(
    report: dict[str, Any], *, days: list[date], size: int, cfg: Mapping[str, Any],
    own: frozenset[str], home_dir: Path, threads: Path, source: imrun.MessageSource,
    now: datetime, db_path: Path | str | None, contacts: Mapping[str, str], pause: float,
) -> dict[str, Any]:
    """Read the days a real run would read, write nothing, and say what it would do."""
    with contextlib.ExitStack() as stack:
        try:
            led = stack.enter_context(imrun._read_view(home_dir))
        except TimeoutError:
            return _pause(report, "busy", SENTENCE_BUSY)
        except imledger.LedgerCorrupt as exc:
            return _pause(report, "ledger_damaged", f"texts paused: {exc}")
        try:
            state = led.load_state()
        except imledger.LedgerCorrupt as exc:
            return _pause(report, "ledger_damaged", f"texts paused: {exc}")
        report["watermark_before"] = report["watermark_after"] = state.get("watermark")
        index, set_aside = resume_point(state, days)
        report["set_aside"] = set_aside
        todo = days[index:]
        if index:
            report["resumes_from"] = todo[0].isoformat() if todo else None
        report["tranches"] = len(_tranches(todo, size))
        conn = imspine.open_conn(db_path)
        imrun._no_checkpoint_on_close(conn)
        try:
            drift = imspine.check_contract(conn)
            if drift:
                return _pause(report, "engine_changed", drift)
            began = time.monotonic()
            pipe = _pipeline(conn=conn, led=led, cfg=cfg, own=own, contacts=contacts,
                             threads=threads, now=now, dry_run=True)
            try:
                with source.opened():
                    pipe.read_days(source, dict.fromkeys(todo), todo)
            except imrun.SourceUnreadable as exc:
                return _pause(report, "unreadable",
                              imchat.UNREADABLE_SENTENCE.format(reason=exc))
            pipe.resolve_all()
            read_s = time.monotonic() - began
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            conn.close()
    counts = _counts(pipe)
    report["counts"] = counts
    report["estimate"] = estimate(
        read_s=read_s, threads=counts["threads"], person_days=counts["person_days"],
        tranches=report["tranches"], pause_s=pause, queued=counts["queued"], cfg=cfg,
    )
    report["next"] = todo[0].isoformat() if todo else None
    report["complete"] = not todo
    return report


def _pipeline(
    *, conn: sqlite3.Connection, led: imledger.Ledger, cfg: Mapping[str, Any],
    own: frozenset[str], contacts: Mapping[str, str], threads: Path, now: datetime,
    dry_run: bool,
) -> imrun.Pipeline:
    """The daily pipeline, set the one way a backfill runs it (see the module docstring)."""
    return imrun.Pipeline(
        conn=conn, led=led, cfg=cfg, own=own,
        owner_ids=imrun.owner_person_ids(conn, own), contacts=contacts,
        threads_dir=threads, now=now, deadline=None, dry_run=dry_run,
        emit_new=False, reopen=False, follow_through=False, origin=imrun.ORIGIN_BACKFILL,
    )


def _confirmed(
    report: dict[str, Any], *, days: list[date], size: int, cfg: Mapping[str, Any],
    own: frozenset[str], home_dir: Path, threads: Path, source: imrun.MessageSource,
    now: datetime, db_path: Path | str | None, contacts: Mapping[str, str], pause: float,
    started: float, max_seconds: float | None, sleep: Callable[[float], None],
) -> dict[str, Any]:
    """Tranche after tranche, each one committed and its progress saved before the next."""
    totals: Counter[str] = Counter()
    held: Counter[str] = Counter()
    cards: set[str] = set()
    plan: list[list[date]] | None = None
    started_at = now.isoformat()
    number = 0
    while True:
        if plan is not None and not plan:
            break
        if plan is not None and number:
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                report["stopped"] = "time"
                break
            if pause > 0:
                sleep(pause)
        outcome = _one_tranche(
            report, days=days, size=size, plan=plan, cfg=cfg, own=own, home_dir=home_dir,
            threads=threads, source=source, now=now, db_path=db_path, contacts=contacts,
            started_at=started_at,
        )
        if outcome is None:  # paused, or nothing left
            break
        plan, pipe, started_at = outcome
        number += 1
        report["done_tranches"] = number
        counts = _counts(pipe)
        for name in ("messages", "conversation_days", "person_days", "queued",
                     "released_from_hold", "held_total", "already_queued",
                     "already_on_cards", "your_own", "threads", "raised", "new_for_review"):
            totals[name] += int(counts[name])
        held.update(counts["held"])
        cards |= pipe.tally.queued_people
    summary = dict(totals)
    summary["held"] = dict(sorted(held.items()))
    summary["cards"] = len(cards)
    report["counts"] = summary
    report["elapsed_s"] = round(time.monotonic() - started, 2)
    return report


def _one_tranche(
    report: dict[str, Any], *, days: list[date], size: int,
    plan: list[list[date]] | None, cfg: Mapping[str, Any], own: frozenset[str],
    home_dir: Path, threads: Path, source: imrun.MessageSource, now: datetime,
    db_path: Path | str | None, contacts: Mapping[str, str], started_at: str,
) -> tuple[list[list[date]], imrun.Pipeline, str] | None:
    """One tranche in one ledger session.  ``(the tranches left, its pipeline, started_at)``,
    or ``None`` when the run paused (the report says why) or there was nothing to do."""
    view = imledger.session(
        lambda: imspine.file_lock(imledger.lock_target(home_dir), timeout=LOCK_TIMEOUT_S),
        home=home_dir,
    )
    with contextlib.ExitStack() as stack:
        try:
            led = stack.enter_context(view)
        except TimeoutError:
            _pause(report, "busy", SENTENCE_BUSY)
            return None
        except imledger.LedgerCorrupt as exc:
            _pause(report, "ledger_damaged", f"texts paused: {exc}")
            return None
        try:
            state = led.load_state()
        except imledger.LedgerCorrupt as exc:
            _pause(report, "ledger_damaged", f"texts paused: {exc}")
            return None
        if "watermark" in state and state.get("watermark_kind") != imrun.WATERMARK_KIND:
            _pause(report, "state_unrecognised", SENTENCE_STATE_KIND)
            return None
        if plan is None:
            # The first tranche decides where this run starts, from progress read under
            # the lock, so two backfills can never both think they own the same days.
            index, set_aside = resume_point(state, days)
            report["set_aside"] = set_aside
            report["watermark_before"] = state.get("watermark")
            if index:
                report["resumes_from"] = days[index].isoformat() if index < len(days) else None
            saved = _progress(state)
            if index and saved and saved.get("started_at"):
                started_at = str(saved["started_at"])
            plan = _tranches(days[index:], size)
            report["tranches"] = len(plan)
            if not plan:
                report["complete"] = True
                report["watermark_after"] = state.get("watermark")
                return None
        chunk = plan[0]
        conn = imspine.open_conn(db_path)
        try:
            drift = imspine.check_contract(conn)
            if drift:
                _pause(report, "engine_changed", drift)
                return None
            run_id = f"backfill-{now:%Y%m%dT%H%M%S}-{os.getpid()}-{time.monotonic_ns()}"
            led.begin_run(run_id)
            try:
                _done, _dropped, rels = imrun.recover(conn, led)
                pipe = _pipeline(conn=conn, led=led, cfg=cfg, own=own, contacts=contacts,
                                 threads=threads, now=now, dry_run=False)
                with source.opened():
                    pipe.read_days(source, dict.fromkeys(chunk), chunk)
                pipe.resolve_all()
                if rels:
                    imspine.project(conn, rels)
                conn.commit()
            except imrun.SourceUnreadable as exc:
                imrun._undo(conn, led, run_id, False)
                _pause(report, "unreadable", imchat.UNREADABLE_SENTENCE.format(reason=exc))
                return None
            except sqlite3.OperationalError as exc:
                imrun._undo(conn, led, run_id, False)
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    _pause(report, "memory_busy", imrun.SENTENCE_MEMORY_BUSY)
                    return None
                raise
            except BaseException:
                imrun._undo(conn, led, run_id, False)
                raise
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            conn.close()
        led.commit_run(run_id)
        led.compact()
        rest = plan[1:]
        nxt = rest[0][0].isoformat() if rest else None
        prior = _progress(state) or {}
        same_window = (prior.get("from") == days[0].isoformat()
                       and prior.get("to") == days[-1].isoformat()
                       and not prior.get("complete"))
        try:
            done_before = int(prior.get("tranches_done") or 0) if same_window else 0
        except (TypeError, ValueError):
            done_before = 0
        progress = {
            "from": days[0].isoformat(), "to": days[-1].isoformat(),
            "next": nxt, "complete": not rest, "started_at": started_at,
            "updated_at": now.isoformat(), "tranches_done": done_before + 1,
        }
        new_state = dict(state)  # the watermark, and everything else, exactly as read
        new_state[PROGRESS_KEY] = progress
        led.save_state(new_state)
        report["next"], report["complete"] = nxt, not rest
        report["watermark_after"] = new_state.get("watermark")
    return rest, pipe, started_at
