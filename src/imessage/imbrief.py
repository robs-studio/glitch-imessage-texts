"""iMessage intake — the morning brief: run the day's intake, then say it in one line.

The engine's morning pass runs this plug-in's entry script once a day and reads back
exactly one JSON object, ``{"headline": str, "items": [str, ...]}``, which it folds
into the member's morning catch-up.  This module is everything between "the stage started"
and "one JSON line on stdout": it runs :func:`imrun.daily` (the deterministic intake:
read, queue, file the stale days) and turns that run's report, plus the ledger's
standing state, into a headline the member reads and a few items they may act on.

Why the logic lives HERE and not in the entry script
----------------------------------------------------
The engine's consent key is ``sha256(capability.json + entry-script bytes)``
(``morning_reports.approval_hash``).  Helpers are not hashed.  So the entry script,
``imdaily.py``, is a tiny shim that never needs to change, and every wording or
counting change happens in this file without lapsing the member's yes or pausing the feed.

The contract this module keeps, and why each part matters
----------------------------------------------------------
* **Exactly one JSON object, exit 0, on every path.**  The engine counts a non-zero
  exit, invalid JSON or output past 65,536 bytes as a failure, and three failures in
  a row pause the feed.  A refusal (not a Mac, no own handles, no Full Disk Access,
  the engine changed shape, a damaged ledger, a busy lock) is NOT a failure: it is
  a truthful quiet line, and the run it describes wrote nothing.  An unexpected
  exception is caught here too and said plainly; its detail goes to stderr only.
* **The caps are enforced here, not left to the engine.**  The engine silently
  truncates a headline past 160 characters, items past 10, an item past 300.  A
  sentence cut mid-word by someone else reads as a bug, so each string is cut
  cleanly at a word boundary here, and the whole line stays far under the byte cap.
* **Never a message body, a phone number or an address.**  Every string is built
  from counts, dates and fixed sentences.  The sentences the run hands back (a
  refusal, the engine's own words on drift) carry paths and app names, never a
  handle; :func:`_scrub` still masks anything phone- or address-shaped, as a belt
  and braces, because this line leaves the plug-in for the morning brief.
* **Pace on the time actually granted.**  ``GLITCH_BUDGET_S`` is the seconds the
  engine really gave this child (it can be less than the declared 20).  ``imrun``
  already paces on it, measured from when ``daily()`` starts; this module passes it
  the grant minus the time the interpreter and imports already spent, minus a small
  reserve for reading the ledger and printing, so the SIGKILL at the wall can never
  land in the middle of the run's own bookkeeping.

What the headline and items say
-------------------------------
Headline: ``Texts: 14 conversation-days in, 2 filed to cards, 12 waiting for a
summary, 3 numbers to review (say 'numbers to review')`` — each part from a real
report or ledger field, and a part that is zero is left out.  Items, most important
first: a missing ledger, the days that would not file, the numbers on the texts review
list (how many are new, how many days they hold), the days waiting for a summary (say
"summarise my texts"), and why a run stopped short.  When nothing came in, one item
says why, because "zero" is never left unexplained.
"""

from __future__ import annotations

import time

#: When this module was first imported: the shim imports it first thing, so this is
#: within a few milliseconds of the engine starting the child's clock.
_LOADED_AT: float = time.monotonic()

# imconfig FIRST, before any engine module: it puts `.claude/scripts` on sys.path and
# then re-asserts this folder ahead of it. See imconfig's docstring.
import imconfig  # noqa: E402

imconfig.ensure_engine_path()

import json  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
from collections.abc import Mapping, Sequence  # noqa: E402
from typing import Any  # noqa: E402

# ---------------------------------------------------------------------------
# The stage's output contract (morning_reports.py: MAX_HEADLINE_CHARS, MAX_ITEMS,
# MAX_ITEM_CHARS, OUTPUT_BYTE_CAP).  Restated, not imported: importing the engine's
# morning runner into its own child would be the coupling it was built to avoid.
# ---------------------------------------------------------------------------

MAX_HEADLINE_CHARS: int = 160
MAX_ITEMS: int = 10
MAX_ITEM_CHARS: int = 300

#: The engine's hard cap is 65,536 bytes.  At the caps above the line is at most a
#: few kilobytes; this is the size past which :func:`render` refuses its own output
#: and prints the fixed fallback instead, well inside the engine's cap.
MAX_LINE_BYTES: int = 16_384

#: Seconds kept back from the engine's grant for reading the ledger and printing.
BRIEF_RESERVE_S: float = 1.0

#: The standing ledger is read only when at least this much of the grant is left:
#: the read may wait on the ledger lock for ``imrun.LOCK_TIMEOUT_S`` (2 s).
LEDGER_READ_NEEDS_S: float = 2.5

#: What the member says to act on an item (the ``/texts`` skill's words, CP6).
SAY_REVIEW: str = "numbers to review"
SAY_SUMMARISE: str = "summarise my texts"

#: Printed when even this module cannot produce its line.  Valid JSON, checked by test.
FALLBACK_LINE: str = json.dumps({
    "headline": "Texts: the morning brief could not be put together, so nothing is "
                "reported this morning.",
    "items": ["Run `imessage.py status` to see where your texts stand."],
})

#: The item after a refusal: the run's own sentence (it names the fix, or the app
#: that needs Full Disk Access, or what changed in the engine) ...
USE_SENTENCE: str = "<the run's sentence>"
#: ... or, for a damaged ledger or state file, that sentence's first clause (which
#: file, what is wrong) with the fix after it, so the 300-character cap never cuts
#: the fix off the end of the engine-length original.
USE_DAMAGE: str = "<the damaged file>"

#: Per refusal reason (``imrun``'s ``paused.reason``): the headline, and the one item
#: (a fixed sentence, :data:`USE_SENTENCE`, :data:`USE_DAMAGE`, or none where the
#: run's sentence would only repeat the headline).
_PAUSED: dict[str, tuple[str, str | None]] = {
    "not_mac": (
        "Texts: Messages keeps its database only on a Mac, and this machine is not one, "
        "so there is nothing to read here.", None),
    "no_own_handles": (
        "Texts paused: I don't know which numbers are yours yet, so I read nothing.",
        "Run `imessage.py check`: it finds your own numbers for you to confirm into "
        "config.local.json, and the next run goes ahead."),
    "no_access": (
        "Texts paused: I can't read the Messages database right now, so I read nothing.",
        USE_SENTENCE),
    "engine_changed": (
        "Texts paused: the Glitch engine changed shape, so I wrote nothing until the "
        "plug-in is updated.", USE_SENTENCE),
    "busy": (
        "Texts: another texts run was using the ledger, so this one changed nothing; the "
        "next run picks up from the same place.", None),
    "ledger_damaged": (
        "Texts paused: a texts ledger file is damaged, so I changed nothing.", USE_DAMAGE),
    "state_unrecognised": (
        "Texts paused: state.json holds a watermark this version does not recognise, so I "
        "read nothing.", USE_SENTENCE),
    "unreadable": (
        "Texts: the Messages database could not be read just now, so I read nothing; the "
        "next run picks up from the same place.", None),
    "memory_busy": (
        "Texts: the memory database stayed busy, so I undid this run; the next one picks "
        "up from the same place.", None),
}

_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PREFIX_RE = re.compile(r"^\s*texts(?:\s+paused)?\s*:\s*", re.IGNORECASE)
#: An absolute path (POSIX or Windows), captured by its last part.
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][^\s\\/()]+)+[\\/]([^\s\\/()]+)")
_EM_DASH_RE = re.compile(r"\s*" + chr(0x2014) + r"\s*")
#: A handle as this plug-in stores one (E.164), or a bare run of ten or more digits.
#: A date never matches: its longest digit run is four.
_PHONE_RE = re.compile(r"\+\d{7,15}|\d{10,}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f" + chr(0x2028) + chr(0x2029) + "]")


def _warn(message: str) -> None:
    """One diagnostic line on stderr.  Stdout belongs to the one JSON object."""
    print(f"[imessage] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Small, total helpers: they take whatever the report holds and never raise.
# ---------------------------------------------------------------------------


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(value: Any) -> int:
    """A non-negative whole number from a report field, else 0."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _n(count: int, one: str, many: str | None = None) -> str:
    """``1 day`` / ``3 days``, with thousands separated."""
    return f"{count:,} {one if count == 1 else (many or one + 's')}"


def _scrub(text: str) -> str:
    """One clean line: no control characters, no phone number, no address.

    Line breaks and tabs become spaces (one JSON line, one display line), and
    anything shaped like a handle is masked.  Every string this module builds is
    counts and fixed words already; this is the belt and braces for the sentences
    the run hands back.
    """
    flat = _CONTROL_RE.sub(" ", str(text))
    flat = _EMAIL_RE.sub("[address]", flat)
    flat = _PHONE_RE.sub("[number]", flat)
    flat = _EM_DASH_RE.sub("; ", flat)  # the house voice uses none
    return " ".join(flat.split())


def _unprefix(sentence: str) -> str:
    """The run's sentence without its ``texts:`` / ``texts paused:`` lead-in, capitalised.

    Under a headline that already starts ``Texts``, the lead-in only stutters.
    """
    body = _PREFIX_RE.sub("", sentence.strip())
    return body[:1].upper() + body[1:]


def _cap(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters at a word boundary, with an ellipsis."""
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    cut = head.rfind(" ")
    if cut >= limit // 2:
        head = head[:cut]
    return head.rstrip(" ,;:.-") + chr(0x2026)  # an ellipsis


def finalise(headline: str, items: Sequence[str]) -> dict[str, Any]:
    """The brief with every cap enforced and every string scrubbed."""
    clean_items = [_cap(_scrub(i), MAX_ITEM_CHARS) for i in items]
    return {
        "headline": _cap(_scrub(headline), MAX_HEADLINE_CHARS) or "Texts:",
        "items": [i for i in clean_items if i][:MAX_ITEMS],
    }


def render(brief: Mapping[str, Any]) -> str:
    """One JSON line, ASCII-only (so no stdout encoding can break it), capped.

    ASCII because the morning stage runs under launchd, where the locale can be
    anything; ``\\u`` escapes decode back to the same text at the engine.  A line
    that somehow comes out too big is refused for the fixed fallback, rather than
    handed to an engine that would cut it into invalid JSON.
    """
    final = finalise(str(brief.get("headline") or ""), [
        str(i) for i in brief.get("items") or [] if isinstance(i, str)
    ])
    line = json.dumps(final, ensure_ascii=True)
    if len(line.encode("ascii")) > MAX_LINE_BYTES:
        _warn(f"the brief came out {len(line):,} bytes long; printing the fallback instead.")
        return FALLBACK_LINE
    return line


# ---------------------------------------------------------------------------
# Composing the brief.
# ---------------------------------------------------------------------------


def compose_paused(paused: Mapping[str, Any]) -> dict[str, Any]:
    """A refused run: one quiet line saying so, and the run's own sentence naming the fix."""
    reason = str(paused.get("reason") or "")
    sentence = _unprefix(str(paused.get("sentence") or ""))
    known = _PAUSED.get(reason)
    if known is None:
        headline = f"Texts paused: {sentence}" if sentence else (
            "Texts paused, and the run gave no reason; nothing was written.")
        return finalise(headline, [])
    headline, item = known
    if item == USE_SENTENCE:
        item = sentence or None
    elif item == USE_DAMAGE:
        # imledger.LedgerCorrupt reads "<path> is damaged (<problem>). Nothing was ...".
        first = _PATH_RE.sub(r"\1", sentence).split(". ", 1)[0].rstrip(".")
        item = (f"{first}. " if first else "") + (
            "Repair or restore that file by hand (deleting it would lose what it knows), "
            "then the next run carries on.")
    return finalise(headline, [item] if item else [])


def compose_error(kind: str) -> dict[str, Any]:
    """The run raised.  Its type is named; its message (which could hold data) is not.

    A plain dict of fixed words, built with nothing that can fail: it is also what
    :func:`brief_line` falls back to when composing went wrong, and :func:`render`
    applies the caps.
    """
    return {
        "headline": f"Texts: this morning's run stopped on an unexpected error ({kind}), so "
                    "it will try again on the next run.",
        "items": ["If it happens again, run `imessage.py daily --dry-run` to see the error "
                  "without writing anything."],
    }


def compose(
    report: Mapping[str, Any],
    ledger: Mapping[str, Any] | None,
    cfg: Mapping[str, Any],
) -> dict[str, Any]:
    """The brief for a run that went ahead.

    ``report`` is :func:`imrun.daily`'s report; ``ledger`` is the ``ledger`` block of
    :func:`imrun.status` read straight after it, for the days held and the summary
    claims (``None`` when it could not be read: the line is then thinner, still true).

    "Numbers to review" is the report's ``awaiting_review``: the numbers on the texts
    review list, and ``new_for_review`` says how many arrived this run.  By the member's
    ruling (2026-09-24) ``daily`` never raises a people proposal, and this line never
    mentions the people queue.
    """
    stale = _map(report.get("stale"))
    queue = _map(report.get("queue"))
    recovered = _map(report.get("recovered"))
    mark = _map(report.get("watermark"))
    raw_stopped = report.get("stopped")
    stopped: list[Any] = raw_stopped if isinstance(raw_stopped, list) else []

    came_in = _count(report.get("conversation_days"))
    filed = _count(stale.get("stamped"))
    waiting = _count(queue.get("depth"))
    oldest = queue.get("oldest_day")
    oldest = oldest if isinstance(oldest, str) and _DAY_RE.fullmatch(oldest) else None
    failures = _count(report.get("failures"))

    ledger = ledger or {}
    review = _count(report.get("awaiting_review", ledger.get("awaiting_review")))
    new = min(_count(report.get("new_for_review")), review)
    held_days = _count(ledger.get("held_units"))
    unclaimed: int | None = None
    if isinstance(ledger.get("unclaimed"), Mapping):
        unclaimed = min(_count(_map(ledger.get("unclaimed")).get("depth")), waiting)

    parts: list[str] = []
    if came_in:
        parts.append(_n(came_in, "conversation-day") + " in")
    if filed:
        parts.append(f"{filed:,} filed to " + ("a card" if filed == 1 else "cards"))
    if waiting:
        parts.append(f"{waiting:,} waiting for a summary")
    if review:
        parts.append(_n(review, "number") + f" to review (say '{SAY_REVIEW}')")
    short = "budget" in stopped or bool(mark.get("held_by_budget"))
    if parts:
        headline = "Texts: " + ", ".join(parts)
    elif short:
        headline = ("Texts: the run ran out of time before anything came in; the next run "
                    "carries on from the same place.")
    else:
        headline = "Texts: nothing new to file."

    items: list[str] = []
    if report.get("ledger_missing"):
        items.append(
            "Warning: the texts ledger is missing but its watermark is not, so days queued "
            "or held before it went are no longer known; restore ledger.json if you have a "
            "copy."
        )
    if failures:
        items.append(
            f"{_n(failures, 'day')} would not file onto "
            + ("its card and stays" if failures == 1 else "their cards and stay")
            + " queued to try again next run; ask for your texts status to see why."
        )
    if review:
        text = f"{_n(review, 'number')} on your texts review list"
        if new:
            text += ", new this run" if review == 1 else f", {new:,} of them new this run"
        if held_days:
            text += f", holding {_n(held_days, 'day')} of texts until you decide"
        items.append(text + ".")
    if waiting:
        text = (f"{_n(waiting, 'day')} of texts {'is' if waiting == 1 else 'are'} waiting "
                "for a summary")
        if oldest:
            text += f", the oldest from {oldest}"
        if unclaimed is not None and unclaimed < waiting:
            text += f" ({waiting - unclaimed:,} already taken by a summary pass)"
        text += f"; say '{SAY_SUMMARISE}'." if unclaimed is None or unclaimed else "."
        stale_days = _count(cfg.get("synth_stale_days", imconfig.DEFAULTS["synth_stale_days"]))
        if stale_days:
            text += (f" A day still waiting after {_n(stale_days, 'day')} in the queue files "
                     "with its plain line instead.")
        items.append(text)
    remaining = _count(stale.get("remaining"))
    if short:
        if parts:
            items.append("The run ran out of its time budget, so the rest carries to the next "
                         "run.")
    elif remaining:
        items.append(
            f"{remaining:,} more {'day was' if remaining == 1 else 'days were'} due to file "
            "but left for the next run, which files them first."
        )
    settled = _count(recovered.get("done")) + _count(recovered.get("dropped"))
    if settled:
        items.append(
            "The previous run was cut off mid-filing; this run settled its "
            f"{_n(settled, 'unfinished filing')} against the cards."
        )
    if mark.get("store_reset"):
        items.append(
            "Your Messages history looks rebuilt, so I read yesterday afresh; older days "
            "need a backfill."
        )
    elif mark.get("first_run") and parts:
        items.append("This was the first run, so it read yesterday only; older days need a "
                     "backfill.")
    if not parts and not short:
        why = report.get("why_nothing_queued")
        if isinstance(why, str) and why.strip():
            items.append(f"Why: {why.strip().rstrip('.')}.")
    return finalise(headline, items)


# ---------------------------------------------------------------------------
# Running it.
# ---------------------------------------------------------------------------


def _granted_left() -> float | None:
    """Seconds of the engine's grant still unspent, or ``None`` with no grant.

    ``None`` when ``GLITCH_BUDGET_S`` is absent (a hand run: no clock) or unreadable
    (``imrun`` then warns and paces on 20 seconds by itself).
    """
    raw = os.environ.get("GLITCH_BUDGET_S")
    if raw is None or not raw.strip():
        return None
    try:
        granted = float(raw)
    except ValueError:
        return None
    if not math.isfinite(granted):
        return None
    return granted - (time.monotonic() - _LOADED_AT)


def _ledger_view(run: Any, home: Any, now: Any) -> Mapping[str, Any] | None:
    """The ledger block of ``imrun.status`` (read-only), or ``None`` if it cannot be had.

    Skipped when too little of the grant is left to wait out a held ledger lock:
    the brief then counts from the report alone, which is thinner but still true.
    """
    left = _granted_left()
    if left is not None and left < LEDGER_READ_NEEDS_S:
        _warn(f"only {max(left, 0.0):.1f}s of the grant left; the brief skips the ledger read.")
        return None
    try:
        status = run.status(home=home, now=now)
    except Exception as exc:  # noqa: BLE001 - the brief degrades, it never fails the stage
        _warn(f"could not read the ledger for the brief ({type(exc).__name__}: {exc}).")
        return None
    ledger = _map(_map(status).get("ledger"))
    return ledger if ledger.get("exists") else None


def morning(*, home: Any = None, cfg: Mapping[str, Any] | None = None,
            **daily_kw: Any) -> dict[str, Any]:
    """Run the day's intake and return the brief ``{"headline", "items"}``.

    Every keyword besides ``home`` and ``cfg`` goes straight to :func:`imrun.daily`
    (``source``, ``now``, ``threads_dir``, ``db_path``, ``contacts``, ``budget_s``),
    so a test can point the whole run at temp places; the stage passes none.  Never
    raises for anything the run does: a refusal, a missing run module and an
    unexpected exception each come back as a truthful brief.
    """
    try:
        import imrun  # noqa: PLC0415 - lazy, so a broken run module is a brief, not a crash
    except Exception as exc:  # ImportError, SyntaxError, anything its import runs
        _warn(f"the run module (imrun.py) would not load ({type(exc).__name__}: {exc}).")
        return finalise(
            "Texts paused: the plug-in's run module would not load "
            f"({type(exc).__name__}), so I read nothing.",
            ["Run `imessage.py status` to see the error."],
        )

    settings = dict(cfg) if cfg is not None else imconfig.load_config()
    kw = dict(daily_kw)
    if "budget_s" not in kw:
        left = _granted_left()
        kw["budget_s"] = None if left is None else max(0.0, left - BRIEF_RESERVE_S)
    try:
        raw = imrun.daily(home=home, cfg=settings, **kw)
    except Exception as exc:  # noqa: BLE001 - daily() has already undone its run
        _warn(f"the daily run stopped on an unexpected error ({type(exc).__name__}: {exc}); "
              "its database work was rolled back and the next run starts from the same "
              "place.")
        return compose_error(type(exc).__name__)

    report = _map(raw)
    paused = report.get("paused")
    if paused:
        return compose_paused(_map(paused))
    return compose(report, _ledger_view(imrun, home, kw.get("now")), settings)


def brief_line(**kw: Any) -> str:
    """The one JSON line for stdout.  Never raises, whatever happens underneath."""
    try:
        brief: Mapping[str, Any] = morning(**kw)
    except BaseException as exc:  # noqa: BLE001 - one JSON line and exit 0, always
        _warn(f"the morning brief hit an unexpected error ({type(exc).__name__}: {exc}).")
        brief = compose_error(type(exc).__name__)
    try:
        return render(brief)
    except BaseException as exc:  # noqa: BLE001
        _warn(f"the brief could not be rendered ({type(exc).__name__}); printing the fallback.")
        return FALLBACK_LINE


def main() -> int:
    """Print exactly one JSON object on stdout, in one write, and return 0."""
    line = brief_line()
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
