"""iMessage intake — ``exclude`` / ``include``: stop reading someone's texts, or start again.

Why this module exists
----------------------
``never_ingest`` in ``config.local.json`` is the list of numbers and addresses whose
conversations are never read at all: the grouper drops any conversation one of them takes
part in, before a transcript is written or a day is queued.  An assistant's number or a
business bot is the usual entry.  This is the member's road onto and off that list by name
("don't read texts from Nina"), so nobody ever hand-edits the JSON.

``exclude --person <name|number>``
    Resolves the words (:mod:`imwho`).  A NUMBER or an address is excluded exactly as
    typed.  A NAME is excluded by every number and address on that person's card, every
    one the texts ledger has tied to them, and every Contacts entry of that name.  An
    ambiguous name lists the candidates and stops.  Without ``confirm`` it shows what
    would change and writes nothing.  With it, in this order:

    1. ``config.local.json`` is backed up into the engine's local undo ring
       (``local_snapshot``, through :func:`imspine.snapshot_local`, G7), and nothing is
       written if that backup fails;
    2. the identifiers are added to ``never_ingest`` through the plug-in's one atomic,
       deep-merging writer (:func:`imessage.write_never_ingest`), which never removes an
       entry already there;
    3. in the texts ledger (when one exists), their days that are queued but not yet on
       a card, and their days held for review, are DROPPED; any summary pass that had
       claimed one of those days is cancelled (its other days go back to the queue, so no
       summary writer ever reads the excluded conversations); and any of their numbers
       waiting on the texts review list is set aside (``dismissed``, ``hold_reason``
       ``never_ingest``, its earlier standing kept so ``include`` can restore it).

    Said plainly every time: **lines already on cards stay**, and **transcripts already
    stored stay on this machine** (the cold files are the archive; nothing here deletes).

``include --person <name|number>``
    The reverse, and the ONLY road that shortens the list: the same backup first, then
    :func:`imessage.remove_never_ingest`, then every number the exclusion set aside goes
    back to where it stood.  Days dropped while excluded are not brought back, and texts
    that arrived while excluded were never read: a ``backfill`` over those dates reads them.

The member's own numbers are never put on the list (their own voice is ``own_handles``,
a different and opposite meaning) and never needed off it.

What it never does
------------------
It writes no card, raises nothing on the people queue, never touches ``memory.db``
(its connection is read-only in practice: rolled back, checkpoint-on-close off), and
never deletes a transcript.  It imports no engine module.
"""

from __future__ import annotations

# imconfig FIRST, before any engine module: it puts `.claude/scripts` on sys.path and
# then re-asserts this folder ahead of it. See imconfig's docstring.
import imconfig

imconfig.ensure_engine_path()

import contextlib  # noqa: E402
import secrets  # noqa: E402
import sqlite3  # noqa: E402
from collections.abc import Mapping  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import imcontacts  # noqa: E402
import imessage  # noqa: E402
import imledger  # noqa: E402
import imrun  # noqa: E402
import imspine  # noqa: E402
import imwho  # noqa: E402

#: The ``hold_reason`` a number set aside by ``exclude`` carries on the ledger.
NEVER_INGEST: str = "never_ingest"

#: How long an act waits for the ledger lock.
LOCK_TIMEOUT_S: float = 5.0

SENTENCE_BUSY: str = (
    "texts: another texts run is using the ledger right now, so I changed nothing; try "
    "again in a minute."
)
SENTENCE_STAYS: str = (
    "Lines already on their card stay, and the transcripts already stored stay on this "
    "machine; nothing here deletes."
)
SENTENCE_NOT_BACK: str = (
    "Texts that arrived while they were excluded were never read, and days dropped then "
    "are not brought back; a backfill over those dates reads them."
)


def _pause(report: dict[str, Any], reason: str, sentence: str) -> dict[str, Any]:
    report["paused"] = {"reason": reason, "sentence": sentence}
    return report


def _new_report(verb: str, person: str, confirm: bool) -> dict[str, Any]:
    return {
        "verb": verb, "confirm": bool(confirm), "paused": None, "refused": None,
        "asked": person, "match": None,
        "identifiers": [], "changes": [], "unchanged": [], "own_skipped": [],
        "snapshot": None, "config_written": False,
        "ledger": {"exists": False, "queued_days": 0, "held_days": 0, "claims_cancelled": 0,
                   "numbers": 0},
        "done": False,
        "note": SENTENCE_STAYS if verb == "exclude" else SENTENCE_NOT_BACK,
    }


def exclude(person: str, **kw: Any) -> dict[str, Any]:
    """Stop reading ``person``'s texts.  Preview unless ``confirm``.  See the module docstring."""
    return _run("exclude", person, **kw)


def include(person: str, **kw: Any) -> dict[str, Any]:
    """Read ``person``'s texts again.  Preview unless ``confirm``.  See the module docstring."""
    return _run("include", person, **kw)


def _canon_set(values: Any) -> set[str]:
    return {c for c in (imcontacts.canonicalise(v) for v in values or []
                        if isinstance(v, str)) if c}


def _run(
    verb: str,
    person: str,
    *,
    confirm: bool = False,
    home: Path | str | None = None,
    local_config: Path | str | None = None,
    db_path: Path | str | None = None,
    contacts: Mapping[str, str] | None = None,
    cfg: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    show_numbers: bool = False,
) -> dict[str, Any]:
    report = _new_report(verb, person, confirm)
    cfg = dict(cfg) if cfg is not None else imconfig.load_config()
    home_dir = Path(home) if home is not None else Path(imconfig.HOME)
    target = Path(local_config) if local_config is not None else home_dir / "config.local.json"
    moment = now if now is not None else imrun._engine_now()
    own = _canon_set(imconfig.require_own_handles(cfg) or [])
    drift = imspine.check_contract()
    if drift:
        return _pause(report, "engine_changed", drift)
    try:
        listed = imessage.never_ingest_list(target=target, home=home_dir)
    except (imessage.LocalConfigWriteError, OSError) as exc:
        report["refused"] = str(exc)
        return report

    has_ledger = imrun._ledger_exists(home_dir)
    report["ledger"]["exists"] = has_ledger
    view: contextlib.AbstractContextManager[imledger.Ledger]
    if confirm and has_ledger:
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
        imrun._no_checkpoint_on_close(conn)
        try:
            drift = imspine.check_contract(conn)
            if drift:
                return _pause(report, "engine_changed", drift)
            contact_map = dict(contacts) if contacts is not None else imcontacts.load_map()
            found = imwho.find(person, conn=conn, led=led, contacts=contact_map)
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            conn.close()
        report["match"] = imwho.found_data(found, show_numbers)
        if found.status != "one" or found.who is None:
            if confirm:
                report["refused"] = (
                    found.why if found.status == "none" else
                    "more than one person goes by that, so I changed nothing; say which "
                    "one (their full name, their number, or their card id)")
            return report
        _plan(report, verb, found.who, listed=listed, own=own, led=led)
        if not confirm:
            return report
        if not report["identifiers"]:
            report["refused"] = (
                f"I found no number or address for {found.who.name} to "
                f"{'stop' if verb == 'exclude' else 'start'} reading, so nothing was changed.")
            return report
        return _act(report, verb, led=led if has_ledger else None, target=target,
                    home_dir=home_dir, now=moment)
    return report  # pragma: no cover - the with block always returns


def _plan(
    report: dict[str, Any], verb: str, who: imwho.Who, *, listed: list[str],
    own: set[str], led: imledger.Ledger,
) -> None:
    """Work out, without writing, exactly what the act would change."""
    candidates = [who.asked] if who.asked else who.identifiers
    on_list = _canon_set(listed)
    report["own_skipped"] = [i for i in candidates if i in own]
    idents = [i for i in candidates if i not in own]
    if verb == "include":
        idents = [i for i in idents if i in on_list or _set_aside(led, i)]
        report["changes"] = [i for i in idents if i in on_list]
        report["unchanged"] = []
    else:
        report["changes"] = [i for i in idents if i not in on_list]
        report["unchanged"] = [i for i in idents if i in on_list]
    report["identifiers"] = idents
    queued, held = _their_days(led, set(idents))
    report["ledger"]["queued_days"] = len(queued) if verb == "exclude" else 0
    report["ledger"]["held_days"] = sum(len(v) for v in held.values()) if verb == "exclude" else 0
    movable = _can_set_aside if verb == "exclude" else _set_aside
    report["ledger"]["numbers"] = sum(1 for i in idents if movable(led, i))


def _their_days(
    led: imledger.Ledger, idents: set[str]
) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    queued = [key for key, record in sorted(led.queued().items())
              if record.get("identifier") in idents and not led.is_stamped(key)]
    held = {ident: led.held(ident) for ident in sorted(idents) if led.held(ident)}
    return queued, held


def _can_set_aside(led: imledger.Ledger, ident: str) -> bool:
    """A number still waiting on someone: on the texts review list, or on the people queue.

    A number that reaches a card (``accepted``) keeps its standing, so its texts land
    again the moment it is included; one already answered no (``dismissed``) waits on
    nobody and is left exactly as the member left it.
    """
    record = led.identifier(ident)
    return bool(record) and record.get("state") not in ("accepted", "dismissed")


def _set_aside(led: imledger.Ledger, ident: str) -> bool:
    record = led.identifier(ident) or {}
    return record.get("hold_reason") == NEVER_INGEST and record.get("state") == "dismissed"


def _act(
    report: dict[str, Any], verb: str, *, led: imledger.Ledger | None, target: Path,
    home_dir: Path, now: datetime,
) -> dict[str, Any]:
    """Backup, then the list, then the ledger.  Nothing is written if the backup fails."""
    changes = list(report["changes"])
    if changes:
        try:
            made = imspine.snapshot_local(target)
        except (OSError, ValueError) as exc:
            report["refused"] = (
                f"I could not back up {target.name} first ({exc}), so I changed nothing.")
            return report
        report["snapshot"] = str(made) if made is not None else None
        try:
            if verb == "exclude":
                _path, _merged, done = imessage.write_never_ingest(
                    changes, target=target, home=home_dir)
            else:
                _path, _merged, done = imessage.remove_never_ingest(
                    changes, target=target, home=home_dir)
        except (imessage.LocalConfigWriteError, OSError, ValueError) as exc:
            report["refused"] = str(exc)
            return report
        report["config_written"] = bool(done)
    if led is not None:
        _ledger_act(report, verb, led, set(report["identifiers"]), now)
    report["done"] = True
    return report


def _ledger_act(
    report: dict[str, Any], verb: str, led: imledger.Ledger, idents: set[str], now: datetime
) -> None:
    """One committed ledger run: drop their waiting days and set their numbers aside
    (``exclude``), or put the numbers back where they stood (``include``)."""
    counts = {"queued_days": 0, "held_days": 0, "claims_cancelled": 0, "numbers": 0}
    run_id = f"{verb}-{now:%Y%m%dT%H%M%S}-{secrets.token_hex(4)}"
    with led.run(run_id):
        if verb == "exclude":
            queued, held = _their_days(led, idents)
            claims = {c for key in queued for c in [led.live_claim(key, now)] if c}
            for claim_id in sorted(claims):
                if led.release_claim(claim_id):
                    counts["claims_cancelled"] += 1
            for key in queued:
                if led.dequeue(key) is not None:
                    counts["queued_days"] += 1
            for ident in held:
                counts["held_days"] += len(led.release(ident))
            for ident in sorted(idents):
                record = led.identifier(ident)
                if record and _can_set_aside(led, ident):
                    led.set_identifier(
                        ident, state="dismissed", hold_reason=NEVER_INGEST,
                        excluded={"state": record.get("state"),
                                  "hold_reason": record.get("hold_reason"),
                                  "at": now.isoformat()})
                    counts["numbers"] += 1
        else:
            for ident in sorted(idents):
                if not _set_aside(led, ident):
                    continue
                prior = (led.identifier(ident) or {}).get("excluded") or {}
                state = prior.get("state")
                if state not in imledger.IDENTIFIER_STATES:
                    state = "held"
                led.set_identifier(ident, state=state, hold_reason=prior.get("hold_reason"),
                                   excluded=None)
                counts["numbers"] += 1
    led.compact()
    report["ledger"].update(counts)
