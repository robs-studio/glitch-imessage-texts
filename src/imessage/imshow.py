"""iMessage intake — ``show``: where did we leave off with someone, from card and stored texts.

Why this module exists
----------------------
THE WHY's experiential line: the member asks "where did we leave off with X" and it answers.  A
card carries one ``conversation`` line per day of texts, which says THAT they talked and,
once summarised, WHAT about; the stored transcript behind each line says the rest.  This
verb is the read source ``/recall`` (and ``/texts``) hand that question to: resolve the
person (:mod:`imwho`), list their ``conversation`` lines newest first, and print the
transcript each line links to under it.

What it reads
-------------
* **The card's own lines**: every ``- YYYY-MM-DD — conversation (…): … (→ link)`` line in
  its ``## Interactions`` section, read from the card itself (the source of truth), in the
  exact shape the engine's projector reads (``people_index._INTERACTION_RE``).
* **Days not on the card yet**: a day waiting for its summary (the queue; it lands within
  ``synth_stale_days`` either way) and a day held until the number is known (the review
  list).  The newest conversations are usually here, so leaving them out would answer
  "where did we leave off" with where they left off three days ago.
* **Today, only when asked** (``--today``): today's still-open messages with that person,
  read live from ``chat.db`` (``mode=ro``) and rendered in memory, redacted the same way
  a stored transcript is.  Nothing of today is stored by this verb; the morning run files
  the day once it is over.

The transcripts it prints are the stored ones, already redacted when they were written.

Bounded, and says what it cut
-----------------------------
At most ``limit`` days (default :data:`DEFAULT_DAYS`) get their transcript, each one cut
to its LAST :data:`MAX_THREAD_LINES` lines (where a conversation left off is at its end),
and the whole print stops adding transcripts at :data:`MAX_TOTAL_CHARS`.  Every cut is
named in plain words with the flag that shows more, so a short answer is never mistaken
for a short history.

What it never does
------------------
It writes nothing: no ledger lock file is created to read the ledger, no transcript is
written, and the memory database connection is rolled back and closed with its
checkpoint-on-close turned off (``imrun._no_checkpoint_on_close``).  It imports no engine
module (``imspine`` is the one importer).
"""

from __future__ import annotations

# imconfig FIRST, before any engine module: it puts `.claude/scripts` on sys.path and
# then re-asserts this folder ahead of it. See imconfig's docstring.
import imconfig

imconfig.ensure_engine_path()

import contextlib  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
from collections.abc import Mapping  # noqa: E402
from datetime import date, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import imcontacts  # noqa: E402
import imledger  # noqa: E402
import imrun  # noqa: E402
import imspine  # noqa: E402
import imsynth  # noqa: E402
import imthreads  # noqa: E402
import imwho  # noqa: E402

# ---------------------------------------------------------------------------
# Bounds.
# ---------------------------------------------------------------------------

#: Days shown with their transcript when the member does not say how many.
DEFAULT_DAYS: int = 5

#: The most lines of one transcript printed: its LAST lines, where it left off.
MAX_THREAD_LINES: int = 40

#: Once the transcripts printed reach this many characters, the rest are listed only.
MAX_TOTAL_CHARS: int = 12_000

#: The line shapes the engine's projector reads (``people_index._INTERACTION_RE`` and
#: ``_LINK_RE``), narrowed to the one source this plug-in writes.
_LINE = re.compile(
    r"^-\s+(?P<day>\d{4}-\d{2}-\d{2})\s+[—–-]\s+conversation\s*"
    r"\((?P<direction>\w+)\)\s*:?\s*(?P<rest>.*)$"
)
_LINK = re.compile(r"\(\s*→\s*([^)]+)\)")

#: Where each day stands, in the member's words.
STATE_WORDS: dict[str, str] = {
    "on_card": "on the card",
    "waiting": "not on the card yet: waiting for its summary",
    "held": "not on the card yet: its number is on your texts review list",
}

SENTENCE_BUSY: str = (
    "texts: another texts run is using the ledger right now; ask again in a minute."
)


def _pause(report: dict[str, Any], reason: str, sentence: str) -> dict[str, Any]:
    report["paused"] = {"reason": reason, "sentence": sentence}
    return report


# ---------------------------------------------------------------------------
# The card's lines, and the days not on it yet.
# ---------------------------------------------------------------------------


def card_lines(path: Path | None) -> list[dict[str, Any]]:
    """Every ``conversation`` line in the card's ``## Interactions``, in file order."""
    if path is None:
        return []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out: list[dict[str, Any]] = []
    capturing = False
    for raw in text.splitlines():
        if raw.startswith("## "):
            capturing = raw[3:].strip().lower() == "interactions"
            continue
        if not capturing:
            continue
        match = _LINE.match(raw.strip())
        if not match:
            continue
        rest = match.group("rest")
        link = _LINK.search(rest)
        summary = _LINK.sub("", rest).strip()
        out.append({
            "day": match.group("day"),
            "direction": match.group("direction"),
            "summary": summary or None,
            "links": [link.group(1).strip()] if link else [],
            "state": "on_card",
        })
    return out


def waiting_days(led: imledger.Ledger, who: imwho.Who) -> list[dict[str, Any]]:
    """Days of theirs the ledger holds that are not on a card yet: queued, then held."""
    idents = set(who.identifiers)
    out: list[dict[str, Any]] = []
    for key, record in sorted(led.queued().items()):
        if led.is_stamped(key):
            continue
        mine = record.get("identifier") in idents or (
            who.person_id is not None and record.get("person_id") == who.person_id)
        if not mine:
            continue
        out.append(_ledger_day(record, "waiting"))
    for ident in sorted(idents):
        for unit in led.held(ident):
            if not led.is_stamped(str(unit.get("key") or "")):
                out.append(_ledger_day(unit, "held"))
    return out


def _ledger_day(record: Mapping[str, Any], state: str) -> dict[str, Any]:
    links = record.get("links")
    return {
        "day": str(record.get("day") or ""),
        "direction": str(record.get("direction") or imthreads.MUTUAL),
        "summary": str(record.get("topic") or "") or None,
        "links": [x for x in links if isinstance(x, str)] if isinstance(links, list) else [],
        "state": state,
    }


# ---------------------------------------------------------------------------
# Reading the transcripts, bounded.
# ---------------------------------------------------------------------------


def _tail(text: str, max_lines: int) -> tuple[str, int]:
    """The last ``max_lines`` lines of ``text`` and how many earlier lines were cut."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines), 0
    return "\n".join(lines[-max_lines:]), len(lines) - max_lines


class _Budget:
    """The characters of transcript still allowed in this one answer."""

    def __init__(self, total: int) -> None:
        self.left = total

    def take(self, text: str) -> bool:
        if len(text) > self.left:
            self.left = 0
            return False
        self.left -= len(text)
        return True


def _read_threads(
    entry: dict[str, Any], threads: Path, budget: _Budget, max_lines: int
) -> None:
    """Fill ``entry["threads"]`` with each linked transcript's tail, within the budget."""
    found, missing = imsynth.thread_paths(entry.get("links") or [], threads)
    entry["threads"] = []
    entry["missing_threads"] = missing
    for path in found:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            entry["missing_threads"] += 1
            continue
        shown, cut = _tail(text, max_lines)
        if not budget.take(shown):
            entry["threads"].append({"link": _link_of(path, threads), "text": None,
                                     "lines_cut": 0, "capped": True})
            continue
        entry["threads"].append({"link": _link_of(path, threads), "text": shown,
                                 "lines_cut": cut, "capped": False})


def _link_of(path: str | Path, threads: Path) -> str:
    return imthreads.thread_link(Path(path), threads)


# ---------------------------------------------------------------------------
# Today, read live.
# ---------------------------------------------------------------------------


def today_with(
    who: imwho.Who,
    *,
    cfg: Mapping[str, Any],
    contacts: Mapping[str, str],
    source: imrun.MessageSource,
    now: datetime,
    max_lines: int = MAX_THREAD_LINES,
) -> dict[str, Any]:
    """Today's still-open conversations with this person, read live and rendered in memory.

    ``chat.db`` is opened ``mode=ro`` (:class:`imrun.ChatDbSource`); nothing is written.
    Every conversation of today in which one of their numbers or addresses took part is
    rendered the way a stored transcript is (redacted, one message per line, the member
    as ``Me``), unless it involves someone on ``never_ingest``.  No substance floor: an
    unanswered text this morning is exactly what "where did we leave off" wants to see.
    """
    out: dict[str, Any] = {"read": False, "error": None, "messages": 0, "conversations": []}
    try:
        own = imthreads.owner_handles(cfg)
    except imthreads.OwnHandlesRequired as exc:
        out["error"] = str(exc)
        return out
    if not source.on_mac():
        out["error"] = source.access()[1]
        return out
    ok, sentence = source.access()
    if not ok:
        out["error"] = sentence
        return out
    idents = set(who.identifiers)
    if not idents:
        out["error"] = "I know no number or address for them to look for today's texts under."
        return out
    start = imrun._day_start(now.date(), now.tzinfo)
    until = imrun._day_start(now.date() + timedelta(days=1), now.tzinfo)
    try:
        with source.opened():
            messages = source.messages_between(start, until)
    except imrun.SourceUnreadable as exc:
        out["error"] = f"today's texts could not be read ({exc})"
        return out
    out["read"] = True
    blocked = imthreads.never_ingest(cfg)
    days = sorted(imthreads.group(messages, own_handles=own).values(),
                  key=lambda cd: (cd.messages[0].dt_local, cd.chat_rowid))
    for chat_day in days:
        taking_part = set(chat_day.speakers) | set(chat_day.counterparts)
        if not taking_part & idents or taking_part & blocked:
            continue
        shown, cut = _tail(imthreads.render_thread(chat_day, contacts), max_lines)
        out["messages"] += chat_day.message_count
        out["conversations"].append({
            "label": imthreads.label_for(chat_day, contacts),
            "group": bool(chat_day.is_group),
            "messages": chat_day.message_count,
            "text": shown,
            "lines_cut": cut,
        })
    return out


# ---------------------------------------------------------------------------
# show()
# ---------------------------------------------------------------------------


def _parse_day(value: str | date | None, label: str) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"{label} takes a day written YYYY-MM-DD, not {value!r}") from None


def show(
    person: str,
    *,
    since: str | date | None = None,
    today: bool = False,
    limit: int | None = None,
    show_numbers: bool = False,
    home: Path | str | None = None,
    threads_dir: Path | str | None = None,
    db_path: Path | str | None = None,
    contacts: Mapping[str, str] | None = None,
    cfg: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    source: imrun.MessageSource | None = None,
) -> dict[str, Any]:
    """Where the member left off with ``person``.  Plain data; writes nothing.

    ``report["match"]`` says who was found (``ambiguous`` carries the candidates and
    nothing else is read); ``report["days"]`` holds each day newest first with
    ``state`` (on the card, waiting for its summary, held) and, for the first
    ``limit``, its ``threads``; ``report["cut"]`` names everything left out.  Every
    keyword beyond the member's words exists so a test points this at temp places.
    """
    days_wanted = DEFAULT_DAYS if limit is None else max(1, int(limit))
    report: dict[str, Any] = {
        "verb": "show", "paused": None, "error": None, "asked": person,
        "since": None, "limit": days_wanted, "match": None, "days": [],
        "total_days": 0, "cut": [], "today": None,
    }
    try:
        since_day = _parse_day(since, "--since")
    except ValueError as exc:
        report["error"] = str(exc)
        return report
    report["since"] = since_day.isoformat() if since_day else None
    cfg = dict(cfg) if cfg is not None else imconfig.load_config()
    home_dir = Path(home) if home is not None else Path(imconfig.HOME)
    threads = Path(threads_dir) if threads_dir is not None else Path(imconfig.THREADS_DIR)
    moment = now if now is not None else imrun._engine_now()
    drift = imspine.check_contract()
    if drift:
        return _pause(report, "engine_changed", drift)

    with contextlib.ExitStack() as stack:
        try:
            led = stack.enter_context(imrun._read_view(home_dir))
        except TimeoutError:
            return _pause(report, "busy", SENTENCE_BUSY)
        except imledger.LedgerCorrupt as exc:
            return _pause(report, "ledger_damaged", f"texts paused: {exc}")
        conn = imspine.open_conn(db_path)
        imrun._no_checkpoint_on_close(conn)
        try:
            drift = imspine.check_contract(conn)
            if drift:
                return _pause(report, "engine_changed", drift)
            contact_map = dict(contacts) if contacts is not None else imcontacts.load_map()
            found = imwho.find(person, conn=conn, led=led, contacts=contact_map)
            report["match"] = imwho.found_data(found, show_numbers)
            if found.status != "one" or found.who is None:
                return report
            who = found.who
            entries = [*card_lines(who.card), *waiting_days(led, who)]
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            conn.close()

    if since_day is not None:
        entries = [e for e in entries if e["day"] >= since_day.isoformat()]
    # newest first; on a tie, the card's own line before a waiting one, then file order
    order = {"on_card": 0, "waiting": 1, "held": 2}
    entries = sorted(enumerate(entries), key=lambda ie: (ie[1]["day"], -order[ie[1]["state"]],
                                                         ie[0]), reverse=True)
    ordered = [e for _i, e in entries]
    report["total_days"] = len(ordered)
    budget = _Budget(MAX_TOTAL_CHARS)
    for entry in ordered[:days_wanted]:
        _read_threads(entry, threads, budget, MAX_THREAD_LINES)
    report["days"] = ordered[:days_wanted]
    _note_cuts(report, ordered, days_wanted, since_day)

    if today:
        report["today"] = today_with(
            who, cfg=cfg, contacts=contact_map,
            source=source if source is not None else imrun.ChatDbSource(), now=moment,
        )
    return report


def show_day(
    day: str | date,
    *,
    show_numbers: bool = False,
    home: Path | str | None = None,
    db_path: Path | str | None = None,
    contacts: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Everyone the texts hold for one day: "who texted me yesterday".  Writes nothing.

    From the ledger's own records for that day, whatever each day's standing: on a card
    (the line's words), waiting for its summary, or held for review (its number not yet
    known).  Names come from the directory (through ``imspine``) or Contacts; a number
    with neither shows as its last four digits.  Only CLOSED days that a morning run has
    read are here, so the report carries when the last run was (``as_of``); today's texts
    are read live, one person at a time, with ``show --person <X> --today``.
    """
    report: dict[str, Any] = {"verb": "show_day", "paused": None, "error": None,
                              "day": None, "as_of": None, "people": []}
    try:
        wanted = _parse_day(day, "--day")
    except ValueError as exc:
        report["error"] = str(exc)
        return report
    if wanted is None:
        report["error"] = "--day takes a day written YYYY-MM-DD"
        return report
    report["day"] = wanted.isoformat()
    home_dir = Path(home) if home is not None else Path(imconfig.HOME)
    drift = imspine.check_contract()
    if drift:
        return _pause(report, "engine_changed", drift)
    shown = (lambda i: i) if show_numbers else imwho.mask
    with contextlib.ExitStack() as stack:
        try:
            led = stack.enter_context(imrun._read_view(home_dir))
            report["as_of"] = led.load_state().get("last_run_at")
        except TimeoutError:
            return _pause(report, "busy", SENTENCE_BUSY)
        except imledger.LedgerCorrupt as exc:
            return _pause(report, "ledger_damaged", f"texts paused: {exc}")
        conn = imspine.open_conn(db_path)
        imrun._no_checkpoint_on_close(conn)
        try:
            contact_map = dict(contacts) if contacts is not None else imcontacts.load_map()
            rows: list[tuple[str, dict[str, Any]]] = []
            for key, record in led.stamped.items():
                parsed = imthreads.parse_ledger_key(key)
                if parsed and parsed[2] == wanted and led.is_stamped(key):
                    rows.append((parsed[0], {**record, "state": "on_card"}))
            for key, record in led.queued().items():
                parsed = imthreads.parse_ledger_key(key)
                if parsed and parsed[2] == wanted and not led.is_stamped(key):
                    rows.append((parsed[0], {**record, "state": "waiting"}))
            for ident in led.all_identifiers():
                for unit in led.held(ident):
                    if unit.get("day") == wanted.isoformat():
                        rows.append((ident, {**unit, "state": "held"}))
            for ident, record in rows:
                person_id = record.get("person_id")
                name = (imspine.person_name(conn, str(person_id)) if person_id else None) \
                    or imthreads.sanitise_body(contact_map.get(ident) or "")[:120] \
                    or shown(ident)
                report["people"].append({
                    "name": name, "number": shown(ident), "state": record["state"],
                    "direction": str(record.get("direction") or imthreads.MUTUAL),
                    "summary": record.get("topic") if record["state"] != "held" else None,
                })
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            conn.close()
    report["people"].sort(key=lambda p: (p["name"].casefold(), p["number"]))
    return report


def _note_cuts(
    report: dict[str, Any], ordered: list[dict[str, Any]], days_wanted: int,
    since_day: date | None,
) -> None:
    """Name, in plain words, everything this answer left out."""
    cut: list[str] = report["cut"]
    older = len(ordered) - days_wanted
    if older > 0:
        oldest_shown = ordered[days_wanted - 1]["day"]
        cut.append(
            f"{older} older day(s) of texts are not shown (the oldest shown is "
            f"{oldest_shown}); add --limit {days_wanted + older} to see them all"
            + ("" if since_day else ", or --since YYYY-MM-DD to start from a day")
            + "."
        )
    trimmed = sum(1 for e in report["days"] for t in e.get("threads", []) if t["lines_cut"])
    if trimmed:
        cut.append(
            f"{trimmed} transcript(s) were long, so only their last {MAX_THREAD_LINES} lines "
            "(where the conversation left off) are shown."
        )
    capped = sum(1 for e in report["days"] for t in e.get("threads", []) if t["capped"])
    if capped:
        cut.append(
            f"{capped} transcript(s) were left out to keep this answer short; ask for "
            "that day with --since <day> --limit 1."
        )
    missing = sum(int(e.get("missing_threads") or 0) for e in report["days"])
    if missing:
        cut.append(f"{missing} linked transcript(s) are not on this machine.")
