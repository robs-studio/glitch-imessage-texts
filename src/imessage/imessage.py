#!/usr/bin/env python3
"""iMessage intake — the member-facing CLI: ``check``, ``preview``, ``daily``, ``status``,
``synthesise``, ``review``, ``show``, ``backfill``, ``exclude``, ``include``.

``daily`` and ``status`` (CP4) are thin: the pipeline lives in ``imrun.py`` and this file
only wires its verbs and renders its plain-data report.  ``daily --dry-run`` writes
nothing; ``status`` reads only.  ``synthesise`` (CP4b) is as thin over ``imsynth.py``:
without ``--commit`` it claims queued days into a private claim file and prints
``CLAIM_PATH: <path>`` as its last line (in ``--json`` mode the path is the JSON's
``claim_path`` instead, so the output stays one parseable object); with ``--commit PATH``
it lands the summaries a writer put into that file.

``check --write-never-ingest HANDLE`` adds to the never-ingest list through the same
atomic, deep-merging writer as ``--write-own-handles``, never removing an entry already
there.

``check`` is the plug-in's own front door and its own gate.
It reads, it reports in plain words, and — apart from the one file it is explicitly asked
to write with ``--write-own-handles`` — it changes nothing at all.
Run it before anything else, run it when something looks wrong, and run it when a later
verb refuses to start: it is the one command that says, in a member's own words, what this
plug-in can and cannot see on this machine right now.

``preview`` is the second verb and the one that answers "what would this actually do to my
people?".
It reads a window of texts, groups them into conversation-days, applies the substance
floor, folds them to person-days and reports — in plain words — how many lines would land,
one per person per day they texted, with a small sample of what a line would look like.
**It makes no contact with the people spine at all**: no engine import, no person card, no
proposal, no ``memory.db``, no ``stamp_interaction``.  Dry by default; ``--write`` writes
the conversation transcripts and nothing else.  Its own section is beside its code, and
``tests/test_impreview.py`` parses this file to prove the no-spine rule mechanically.

``review`` (CP6) is as thin over ``imreview.py``: every number the daily run held, one
ranked list in plain words (read-only), a preview of a selection (``--accept`` /
``--dismiss`` / ``--dismiss-below``, still read-only), and, with ``--listing <code>
--confirm``, the act, through the engine's own accept and dismiss doors.

The rest of CP6, each as thin over its own module:

``show --person <X>`` (``imshow.py``) is "where did we leave off": the person resolved from
the member's words (``imwho.py``; an ambiguous name lists the candidates and stops), their
``conversation`` lines newest first with the stored transcript each links to, the days not
on the card yet, and, with ``--today``, today's texts read live.  Read-only, bounded, and
it names what it cut.

``backfill --from --to`` (``imbackfill.py``) is dry by default: the conversation-day and
people counts and an estimate measured from the dry run itself, before any yes.  With
``--confirm`` it runs the daily pipeline over those days, a tranche at a time, resumable,
never raising a card and never moving the morning run's watermark.

``exclude`` / ``include --person <X>`` (``imexclude.py``) put a person's numbers on, or
take them off, ``never_ingest``: previewed, then with ``--confirm`` the file is backed up
into the engine's local undo ring first and written through the same atomic writer as
``check --write-never-ingest``.

Why ``check --write-own-handles`` exists — the block it clears
--------------------------------------------------------------
``imconfig.require_own_handles`` returns ``None`` while ``own_handles`` is empty, and a
``None`` means ``daily`` and ``backfill`` MUST NOT START (the reasoning is in
``imconfig``'s docstring: with no own-handles the member's own outbound texts get stamped
onto the member's own card as though someone else had said them).
``own_handles`` lives in ``config.local.json``, which is gitignored and therefore does not
exist on a fresh machine, and until this verb was written **nothing in the plug-in created
it**.
So the feature was blocked on a file nobody made.
``check`` finds the candidates, shows them, and — only when asked — writes them.

Where the candidates come from, and the ONE deviation from the brief
---------------------------------------------------------------------
Two sources, and neither of them invents anything:

**(a) the member's own addresses, already known to the system.**
``glitch-mem/Memory/USER.md`` carries a ``**Email:**`` line and a ``**Google:**`` line
listing the accounts the member has connected.
Those are the member's addresses by definition.
This file is opened **read-only and is never written**; it is the member's dossier, and a
diagnostic has no business editing it.

**(b) the member's own side of a conversation, from the Messages database.**
The brief for this module asked for "the handles appearing opposite ``is_from_me = 1`` in
one-to-one chats".
Measured against a real Messages database, ``message.handle_id`` is **the other
party in both directions** — the handle on a member's outgoing message is the person they
sent it to, not themselves.
Masked reproduction, the busiest one-to-one chats (counts left out)::

    chat A  is_from_me=0 -> [('+<11 digits>', n)]
            is_from_me=1 -> [('+<11 digits>', n), (NULL, n)]   <- same handle
    chat B  is_from_me=0 -> [('+<11 digits>', n)]
            is_from_me=1 -> [(NULL, n), ('+<11 digits>', n)]   <- same handle
    chat C  is_from_me=0 -> [('+<11 digits>', n)]
            is_from_me=1 -> [('+<11 digits>', n), (NULL, n)]   <- same handle

Taking that column would have written the member's **most frequent correspondents** into
``own_handles``, and ``own_handles`` is a suppression list: every one of those people's
messages would then be read as the member's own voice and never land on their card.
That is the exact failure ``own_handles`` exists to prevent, inverted.

So this module reads the columns that really do carry the member's own side, all of them
restricted to outgoing messages and one-to-one chats exactly as the brief framed it:

``message.account`` (on ``is_from_me = 1`` rows)
    the account that SENT the message — ``E:<address>`` or ``P:<number>``.
``chat.account_login``
    the account the conversation belongs to, same two prefixes.
``chat.last_addressed_handle``
    the member's own handle last used to address that conversation.

Verified the same way: the top ``last_addressed_handle`` is a single number carried by
most of the one-to-one chats — far more chats than any one counterparty appears on — and
both ``account_login`` values resolve to rows in the ``handle`` table.
Every value is canonicalised through :func:`imcontacts.canonicalise` and merged, so the
``+`` and no-``+`` spellings of one number, and two casings of one address, collapse to one
candidate rather than three.

Nothing is ever invented.
When discovery finds nothing at all, the verb says so and names the manual road — copy
``config.example.json`` to ``config.local.json`` and fill it in by hand.

Why the write is fussy
-----------------------
``config.local.json`` may already hold ``never_ingest``: the handles whose conversations
are never read at all.
For a member that can be a list of people whose texts must never be touched, and it
is the single most sensitive value in this build.
A writer that clobbered the file would silently switch that protection off, and nothing
downstream would notice — the conversations would simply start being read.
So the write is a **deep merge onto whatever is already there**, it **refuses outright** if
the existing file cannot be parsed (rather than treating a corrupt file as an empty one),
it is **atomic** (serialise, temp file in the same directory, ``0600``, ``os.replace``), it
is **owner-only**, and it **refuses any target outside this plug-in's own folder** with
both sides of that comparison resolved — macOS temp directories are symlinks, and an
unresolved compare waves foreign writes through.

It also refuses to overwrite an ``own_handles`` list that is already set.
Discovery is a good first draft, not a better answer than the member's own curation; once
the list exists, the file is the member's to edit.

Exit codes, and why they are nearly all zero
---------------------------------------------
``check`` exits ``0`` for every expected condition, including no Full Disk Access, a
missing database and a machine that is not a Mac.
This script is a diagnostic, and a diagnostic that exits non-zero on "nothing is set up
yet" becomes the reason a morning run latches.
The one exception is a ``--write-own-handles`` that was asked for and failed: the member
asked for a file to be written, it was not written, and that must not be reported as
success.

Working directory
------------------
Every path in this file comes from :mod:`imconfig`, which derives them from ``__file__``.
``check`` therefore reports the same paths whether it is run from the Brain root, from
``.claude/scripts`` (which is where ``uv run --directory`` puts you), or from this folder.
``tests/test_readonly_guard.py`` runs it from all three and compares, because a claim of
cwd-independence that is only ever exercised from one directory is a claim, not a fact.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

# imconfig FIRST, and before any engine module: importing it is what puts
# `.claude/scripts` on sys.path AND re-asserts this folder ahead of it, so a
# module in this plug-in can never lose a name collision to an engine module of
# the same name. See the `sys.path ORDER` section of imconfig's docstring.
import imconfig  # noqa: E402

imconfig.ensure_engine_path()

import imcontacts  # noqa: E402

# ---------------------------------------------------------------------------
# Constants. Everything path-shaped comes from imconfig, never from Path.cwd().
# ---------------------------------------------------------------------------

#: The member's dossier. READ-ONLY here, always: a diagnostic does not edit the
#: file that describes its member.
USER_PROFILE_PATH: Path = imconfig.BRAIN_ROOT / "glitch-mem" / "Memory" / "USER.md"

#: The two lines of USER.md that carry the member's own addresses.
PROFILE_ADDRESS_LABELS: tuple[str, ...] = ("**Email:**", "**Google:**")

#: How many recent messages the decode-rate sample reads. Enough to be a real
#: measurement, small enough that `check` stays a few seconds.
SAMPLE_ROWS: int = 2000

#: Look-back windows, in days, tried in order until the sample is full. The last
#: entry means "the whole history"; 30 days is thousands of messages for a busy texter.
SAMPLE_WINDOWS_DAYS: tuple[int | None, ...] = (30, 365, None)

#: At or above this share of sampled messages carrying readable words, the read
#: path is working. Attachments, reactions and empty rows carry no words at all,
#: so the healthy figure is high but never 100%.
HEALTHY_DECODE_RATE: float = 0.90

#: One-to-one chats. Apple's `chat.style`: 45 is a direct conversation, 43 a group.
CHAT_STYLE_ONE_TO_ONE: int = 45

#: The `E:` / `P:` prefix Messages puts in front of an account identifier.
_ACCOUNT_PREFIX = re.compile(r"^[EP]:", re.IGNORECASE)

#: Deliberately ordinary: it has to find an address inside prose, not validate one.
_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _warn(message: str) -> None:
    """One plain line to stderr, in the plug-in's house prefix.

    Diagnostics go to stderr and the report goes to stdout, so ``--json`` stays
    machine-readable even when something needed saying.
    """
    print(f"[imessage] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Masking — for output that will be looked at by someone other than the member.
# ---------------------------------------------------------------------------


def mask_handle(value: object) -> str:
    """A handle's SHAPE, with nothing identifying left in it.

    ``+12025550123`` becomes ``+<11 digits>``; ``someone@example.com`` becomes
    ``<local 7>@<domain 11>``.  Same shape as the masking in this plug-in's test
    suites, on purpose: one spelling of "safe to show" across the build.

    It exists because ``check`` is a diagnostic, and a diagnostic gets pasted —
    into a bug report, into a chat window, into a plan.  ``--mask`` makes the
    whole report shareable without the member having to redact it by hand, which
    is the only redaction that actually happens.
    """
    text = str(value)
    if "@" in text:
        local, _, domain = text.partition("@")
        return f"<local {len(local)}>@<domain {len(domain)}>"
    if text.startswith("+"):
        return f"+<{len(text) - 1} digits>"
    return f"<{len(text)} chars>"


def _show(value: object, mask: bool) -> str:
    """The handle as the member needs to see it, or its shape when masking."""
    return mask_handle(value) if mask else str(value)


# ---------------------------------------------------------------------------
# Source (a) — the member's own addresses, from their own dossier.
# ---------------------------------------------------------------------------


def profile_addresses(path: Path | None = None) -> list[tuple[str, str]]:
    """``[(address, the USER.md label it came from)]`` — READ-ONLY, never written.

    Reads only the ``**Email:**`` and ``**Google:**`` lines, because those are the
    two that carry addresses the member has told the system are theirs.  Anything
    else in the file is someone else's address or none at all, and this list ends
    up in a suppression list, so it is deliberately narrow.

    Order is the file's order and duplicates are dropped case-insensitively, so
    the same address named on both lines is one entry attributed to the first.

    Never raises: a missing profile costs one stderr line, an unreadable one the
    same, and discovery carries on with the database alone.
    """
    target = USER_PROFILE_PATH if path is None else Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        _warn(f"no profile at {target}; your own addresses can only come from the database.")
        return []
    except (OSError, UnicodeError) as exc:
        _warn(f"could not read {target.name} ({exc.__class__.__name__}); skipping it.")
        return []

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        label = next((lab for lab in PROFILE_ADDRESS_LABELS if lab in line), None)
        if label is None:
            continue
        for address in _EMAIL_IN_TEXT.findall(line):
            key = address.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append((address, label.strip("*:")))
    return found


# ---------------------------------------------------------------------------
# Source (b) — the member's own side of a conversation, from chat.db.
# ---------------------------------------------------------------------------


@dataclass
class OwnCandidate:
    """One candidate own-handle, with every piece of evidence behind it.

    The counters are kept apart rather than summed into a score so the report can
    say, in plain words, WHY a handle is being offered.  A member confirming their
    own phone number deserves "N messages were sent from it", not "rank 1".
    """

    handle: str
    messages: int = 0
    conversations: int = 0
    addressed: int = 0
    profile_labels: list[str] = field(default_factory=list)

    @property
    def from_database(self) -> bool:
        return bool(self.messages or self.conversations or self.addressed)

    def reasons(self) -> list[str]:
        """The evidence, in plain words, strongest first."""
        out: list[str] = []
        if self.messages:
            out.append(f"{self.messages:,} messages were sent from it")
        if self.conversations:
            out.append(f"it is the account on {self.conversations:,} conversations")
        if self.addressed:
            out.append(f"{self.addressed:,} conversations were last addressed from it")
        for label in self.profile_labels:
            out.append(f"it is on your {label} line in USER.md")
        return out


def _strip_account_prefix(value: str) -> str:
    """``E:you@example.com`` -> ``you@example.com``; ``P:+15555550123`` -> ``+15555550123``.

    Load-bearing, not tidy-up.  :func:`imcontacts.canonicalise` sees the ``@`` in
    ``E:you@example.com`` first and returns the whole string lowercased, prefix and
    all, which is a key that matches nothing for ever.  The prefix has to come off
    before canonicalisation, never after.
    """
    return _ACCOUNT_PREFIX.sub("", value.strip(), count=1)


def _tally(conn: sqlite3.Connection, sql: str, into: dict[str, OwnCandidate], attr: str) -> bool:
    """Run one read-only ``SELECT value, count`` and fold it into ``into``.

    Returns ``True`` when the query ran.  A query that fails — a column this macOS
    release does not have, a schema Apple changed — costs one stderr line and
    ``False``, and the other two sources still run.  Losing one signal is a worse
    answer; losing all three because one column moved is no answer at all.
    """
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        _warn(
            f"one own-handle source could not be read ({exc.__class__.__name__}: {exc}); "
            "the other sources were still used."
        )
        return False

    for value, count in rows:
        if value is None:
            continue
        key = imcontacts.canonicalise(_strip_account_prefix(str(value)))
        if not key:
            continue
        candidate = into.setdefault(key, OwnCandidate(handle=key))
        setattr(candidate, attr, getattr(candidate, attr) + int(count or 0))
    return True


def discover_own_handles(
    conn: sqlite3.Connection | None,
    profile: Sequence[tuple[str, str]] | None = None,
) -> list[OwnCandidate]:
    """Every candidate own-handle this machine can justify, best first.

    Merges the member's dossier addresses with the three database columns that
    carry the member's own side of a conversation (see this module's docstring for
    why those three and not ``message.handle_id``).  ``conn`` may be ``None`` when
    there is no database access, and then the dossier alone is used — which is a
    thinner but still honest answer, and beats refusing to help at all.

    Every value goes through :func:`imcontacts.canonicalise`, so ``+12025550123``,
    ``12025550123`` and ``(202) 555-0123`` are ONE candidate, and the key written
    into ``config.local.json`` is the same spelling every other part of this
    plug-in will compute for the same handle.

    Ordering is by evidence and is fully deterministic: outgoing-message count,
    then conversations, then conversations-last-addressed, then a dossier address
    ahead of a database-only one at equal weight, then the handle itself.  A member
    reads this list top down, so the order is part of the answer.
    """
    candidates: dict[str, OwnCandidate] = {}

    if conn is not None:
        _tally(
            conn,
            "SELECT m.account, COUNT(*) FROM message m "
            "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            "JOIN chat ch ON ch.ROWID = cmj.chat_id "
            f"WHERE m.is_from_me = 1 AND ch.style = {CHAT_STYLE_ONE_TO_ONE} "
            "GROUP BY m.account",
            candidates,
            "messages",
        )
        _tally(
            conn,
            "SELECT ch.account_login, COUNT(*) FROM chat ch "
            f"WHERE ch.style = {CHAT_STYLE_ONE_TO_ONE} GROUP BY ch.account_login",
            candidates,
            "conversations",
        )
        _tally(
            conn,
            "SELECT ch.last_addressed_handle, COUNT(*) FROM chat ch "
            f"WHERE ch.style = {CHAT_STYLE_ONE_TO_ONE} GROUP BY ch.last_addressed_handle",
            candidates,
            "addressed",
        )

    for address, label in profile_addresses() if profile is None else profile:
        key = imcontacts.canonicalise(address)
        if not key:
            continue
        candidate = candidates.setdefault(key, OwnCandidate(handle=key))
        if label not in candidate.profile_labels:
            candidate.profile_labels.append(label)

    return sorted(
        candidates.values(),
        key=lambda c: (
            -c.messages,
            -c.conversations,
            -c.addressed,
            0 if c.profile_labels else 1,
            c.handle,
        ),
    )


# ---------------------------------------------------------------------------
# The write. Read this module's "Why the write is fussy" section before editing.
# ---------------------------------------------------------------------------


class LocalConfigWriteError(ValueError):
    """The private settings file was NOT written, and here is the plain reason.

    A ``ValueError`` subclass so a caller that only knows to catch ``ValueError``
    still catches it; its own type so the CLI can tell a refusal apart from a
    programming mistake.
    """


def _chmod_600(path: Path) -> None:
    """Owner-only where the OS means it; a silent no-op where it does not.

    POSIX modes are advisory on Windows — ``os.chmod`` there can only flip the
    read-only bit, and on some filesystems it raises outright.  A settings file
    that saved is worth more than a mode that did not, so this degrades rather
    than crashing.  Windows is first-class here.
    """
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def _refuse_foreign_target(target: Path, home: Path) -> Path:
    """The written path, or a refusal — enforced in code rather than by care.

    The rule is one line: the file must sit directly inside this plug-in's own
    folder.  **Both sides are resolved before comparing**, and that is the whole
    trick: on macOS ``/tmp`` and the per-user temp tree are symlinks, so comparing
    a resolved path against an unresolved one silently decides "different folder"
    — which refuses a legitimate write, and, spelled the other way round, waves a
    foreign one through.  Resolving also means a symlink planted inside the folder
    and pointed somewhere else is judged on where it really lands.

    ``home`` is a parameter so the tests can prove this against a temp directory
    without going anywhere near the member's real ``config.local.json``.
    """
    resolved = Path(target).expanduser().resolve()
    root = Path(home).expanduser().resolve()
    if resolved.parent != root:
        raise LocalConfigWriteError(
            f"refusing to write {resolved}: this command writes only inside the plug-in's "
            f"own folder ({root}), and nothing outside it."
        )
    return resolved


def _read_existing_local(target: Path) -> dict[str, Any]:
    """Whatever ``config.local.json`` already holds — ``{}`` if it is not there yet.

    A MISSING file is the shipped state and returns ``{}``.  Anything else that is
    wrong — unreadable, invalid JSON, a top level that is not an object — RAISES,
    and that difference is the most important line in this function.
    :func:`imconfig.load_config` may treat a corrupt layer as absent and carry on,
    because a run must not wedge over a stray comma.  A *writer* must do the
    opposite: treating a corrupt file as empty would deep-merge onto ``{}`` and
    replace the member's ``never_ingest`` — the handles whose conversations are
    never read — with nothing at all, silently, while reporting success.
    """
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise LocalConfigWriteError(
            f"refusing to write {target.name}: it exists but could not be read "
            f"({exc.__class__.__name__}). Nothing was changed."
        ) from exc

    try:
        loaded = json.loads(text)
    except ValueError as exc:
        raise LocalConfigWriteError(
            f"refusing to write {target.name}: it is not valid JSON ({exc}), so merging "
            "into it could quietly drop settings you rely on, such as never_ingest. "
            "Fix or move that file, then run this again. Nothing was changed."
        ) from exc

    if not isinstance(loaded, dict):
        raise LocalConfigWriteError(
            f"refusing to write {target.name}: it holds a {type(loaded).__name__}, not a "
            "JSON object. Nothing was changed."
        )
    return loaded


#: Written only when the file is being created from nothing, so a member who opens
#: it later knows what it is. An existing `_readme` is left exactly as it is.
_FRESH_README = (
    "Your private settings for the iMessage intake plug-in. This file is gitignored and "
    "left out of backup, so it never leaves this machine. own_handles is your own numbers "
    "and addresses; never_ingest is anyone whose conversations must never be read at all."
)


def write_local_config(
    own_handles: Sequence[Any],
    *,
    target: Path | None = None,
    home: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Merge ``own_handles`` into ``config.local.json``, atomically and owner-only.

    Returns ``(the path written, the full merged object)``.

    The order of operations is the contract, and each step is here for a failure
    that has actually happened somewhere:

    1. **refuse a foreign target** (:func:`_refuse_foreign_target`), both sides
       resolved;
    2. **refuse an empty list** — writing ``own_handles: []`` is precisely the
       blocked state this command exists to clear, so doing it silently would be a
       success message for a no-op;
    3. **read what is already there**, and refuse rather than merge onto ``{}`` if
       it cannot be parsed (:func:`_read_existing_local`);
    4. **deep-merge** through :func:`imconfig._deep_merge` — the plug-in's ONE
       merge rule, the same one ``load_config`` uses, so "what this writes" and
       "what that reads" can never drift apart.  Lists replace wholesale and other
       keys are untouched, so ``never_ingest`` and any knob overrides survive
       exactly as written;
    5. **serialise BEFORE creating the temp file**, so a payload that cannot be
       serialised leaves nothing on disk at all — not even an empty temp file;
    6. **temp file in the SAME directory**, ``0600``, then ``os.replace``, which is
       a rename within one filesystem and therefore atomic: a crash leaves either
       the old file whole or the new one whole, never half of either;
    7. **``0600`` again on the final path**, so a file left loose by an earlier
       tool is tightened rather than inheriting its own past.

    ``own_handles`` is not type-checked before serialising, deliberately:
    ``json.dumps`` is the single gate on what can be written, and step 5 means a
    value it rejects costs an exception and no file.  The CLI only ever passes
    canonicalised strings.

    ``target`` and ``home`` default to the real ``config.local.json`` and the real
    plug-in folder.  They are parameters so the tests can prove every clause above
    against a temp directory and never touch the member's own file.
    """
    resolved = _refuse_foreign_target(
        imconfig.LOCAL_CONFIG_PATH if target is None else Path(target),
        imconfig.HOME if home is None else Path(home),
    )

    if not own_handles:
        raise LocalConfigWriteError(
            "refusing to write an empty own_handles list: that is the state that already "
            "blocks the daily and backfill runs. Nothing was changed."
        )

    existing = _read_existing_local(resolved)
    updates: dict[str, Any] = {"own_handles": list(own_handles)}
    if not existing:
        updates = {"_readme": _FRESH_README, **updates}

    # imconfig._deep_merge is private to the plug-in, not to the module: it is the
    # ONE merge rule this plug-in has, and load_config reads with it. A second
    # implementation here would be a second answer to the same question, and the
    # day the two disagreed, `never_ingest` is what would go missing.
    merged = imconfig._deep_merge(existing, updates)
    _atomic_write_json(resolved, merged)
    return resolved, merged


def _atomic_write_json(resolved: Path, merged: dict[str, Any]) -> None:
    """Steps 5-7 of :func:`write_local_config`, shared by every writer of that file.

    Serialise FIRST (a value ``json.dumps`` rejects costs an exception and no file,
    not even an empty temp file), then a temp file in the SAME directory at ``0600``,
    then ``os.replace`` — a rename within one filesystem, so atomic — then ``0600``
    again on the final path.  ONE implementation for both the own-handles and the
    never-ingest write, so the two can never drift apart in how carefully they write.
    """
    payload = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"

    resolved.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temp_name = tempfile.mkstemp(
        dir=str(resolved.parent), prefix=f"{resolved.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        # newline="\n" so a Windows run writes the same bytes as a macOS one.
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        _chmod_600(temp_path)
        os.replace(temp_path, resolved)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise
    _chmod_600(resolved)


def write_never_ingest(
    handles: Sequence[Any],
    *,
    target: Path | None = None,
    home: Path | None = None,
) -> tuple[Path, dict[str, Any], list[str]]:
    """Add handles to ``never_ingest`` in ``config.local.json``.  ``(path, merged, added)``.

    The road the plan asks for so the member never hand-edits JSON to name a sender
    that is not a person — their own assistant, which reaches them from an ordinary
    address and no volume or shape rule can tell apart from a friend.  (Short codes
    need no entry: they are already left out.)

    The same guards, in the same order, as :func:`write_local_config`, through the
    same atomic writer:

    * every handle is canonicalised FIRST; one that is not a phone number or an
      address refuses the whole write, rather than storing a spelling that silently
      matches nothing;
    * the target must sit directly in the plug-in folder, both sides resolved;
    * an existing file that cannot be parsed is refused, never merged onto ``{}``;
    * **nothing already listed is ever removed or rewritten** — the existing entries
      stay exactly as written, and only handles not already there (compared in their
      canonical spelling) are appended, deduplicated.  This list can name the
      people a member most wants protected; a writer that could shorten it would be
      the one way to switch that protection off without anyone noticing;
    * ``never_ingest`` that is present but not a list is refused, not replaced.

    Nothing new to add writes nothing and returns ``added == []``.
    """
    wanted: list[str] = []
    for raw in handles:
        canonical = imcontacts.canonicalise(raw if isinstance(raw, str) else None)
        if not canonical:
            raise LocalConfigWriteError(
                f"refusing to write never_ingest: {raw!r} is not a phone number or an "
                "address I can read. Nothing was changed."
            )
        if canonical not in wanted:
            wanted.append(canonical)
    if not wanted:
        raise LocalConfigWriteError(
            "refusing to write never_ingest: no handle was given. Nothing was changed."
        )

    resolved = _refuse_foreign_target(
        imconfig.LOCAL_CONFIG_PATH if target is None else Path(target),
        imconfig.HOME if home is None else Path(home),
    )
    existing = _read_existing_local(resolved)
    current = existing.get("never_ingest", [])
    if not isinstance(current, list):
        raise LocalConfigWriteError(
            f"refusing to write {resolved.name}: its never_ingest is a "
            f"{type(current).__name__}, not a list, so adding to it could lose what it "
            "holds. Fix it by hand. Nothing was changed."
        )
    already = {imcontacts.canonicalise(x) for x in current if isinstance(x, str)}
    added = [handle for handle in wanted if handle not in already]
    if not added:
        return resolved, existing, []

    updates: dict[str, Any] = {"never_ingest": [*current, *added]}
    if not existing:
        updates = {"_readme": _FRESH_README, **updates}
    merged = imconfig._deep_merge(existing, updates)
    _atomic_write_json(resolved, merged)
    return resolved, merged, added


def never_ingest_list(
    *, target: Path | None = None, home: Path | None = None
) -> list[str]:
    """``never_ingest`` exactly as ``config.local.json`` holds it (``[]`` when absent).

    Read with the writers' own guards (the target must sit in the plug-in folder, and a
    file that cannot be parsed is refused rather than read as empty), because its caller
    decides what to add or remove from what this returns.
    """
    resolved = _refuse_foreign_target(
        imconfig.LOCAL_CONFIG_PATH if target is None else Path(target),
        imconfig.HOME if home is None else Path(home),
    )
    current = _read_existing_local(resolved).get("never_ingest", [])
    if not isinstance(current, list):
        raise LocalConfigWriteError(
            f"{resolved.name}'s never_ingest is a {type(current).__name__}, not a list; "
            "fix it by hand. Nothing was changed."
        )
    return [entry for entry in current if isinstance(entry, str)]


def remove_never_ingest(
    handles: Sequence[Any],
    *,
    target: Path | None = None,
    home: Path | None = None,
) -> tuple[Path, dict[str, Any], list[str]]:
    """Take handles OFF ``never_ingest`` in ``config.local.json``.  ``(path, merged, removed)``.

    The member's ``include`` road, and the ONLY one that shortens the list:
    :func:`write_never_ingest` never removes anything, so a list that names the people a
    member most wants protected cannot be shortened by accident.  This one is asked for
    by name, previewed, and preceded by a backup of the file (``imexclude`` takes
    ``local_snapshot`` first).

    The same guards and the same atomic writer as the other two writers: every handle
    canonicalised first (an unreadable one refuses the whole write), the target in the
    plug-in folder with both sides resolved, a corrupt file refused rather than merged
    onto ``{}``, a ``never_ingest`` that is not a list refused.  An entry is removed when
    its CANONICAL spelling matches, so ``212-555-0142`` removes ``+12125550142``; every
    other entry stays exactly as written, in its order.  Nothing to remove writes nothing
    and returns ``removed == []``.
    """
    wanted: set[str] = set()
    for raw in handles:
        canonical = imcontacts.canonicalise(raw if isinstance(raw, str) else None)
        if not canonical:
            raise LocalConfigWriteError(
                f"refusing to change never_ingest: {raw!r} is not a phone number or an "
                "address I can read. Nothing was changed."
            )
        wanted.add(canonical)
    if not wanted:
        raise LocalConfigWriteError(
            "refusing to change never_ingest: no handle was given. Nothing was changed."
        )
    resolved = _refuse_foreign_target(
        imconfig.LOCAL_CONFIG_PATH if target is None else Path(target),
        imconfig.HOME if home is None else Path(home),
    )
    existing = _read_existing_local(resolved)
    current = existing.get("never_ingest", [])
    if not isinstance(current, list):
        raise LocalConfigWriteError(
            f"refusing to write {resolved.name}: its never_ingest is a "
            f"{type(current).__name__}, not a list, so changing it could lose what it "
            "holds. Fix it by hand. Nothing was changed."
        )
    kept = [x for x in current
            if not (isinstance(x, str) and imcontacts.canonicalise(x) in wanted)]
    removed = [x for x in current if x not in kept]
    if not removed:
        return resolved, existing, []
    # Lists replace wholesale in the one merge rule, which is what lets an entry go.
    merged = imconfig._deep_merge(existing, {"never_ingest": kept})
    _atomic_write_json(resolved, merged)
    return resolved, merged, [str(x) for x in removed]


# ---------------------------------------------------------------------------
# Reading the message store — everything below goes through imchat.
# ---------------------------------------------------------------------------


def _import_imchat():
    """``imchat``, or ``None`` with one plain line saying it is not here.

    The chat reader is a separate module of this plug-in.  If it is missing, this
    verb still has something useful to say — the paths, the contacts, the dossier
    addresses — so it reports the gap and carries on rather than dying at import.
    ``tests/test_readonly_guard.py`` FAILS outright when the module is absent, so a
    missing reader can never pass silently as a green build; it is only here that
    it degrades.
    """
    try:
        import imchat  # noqa: PLC0415 - deliberately lazy; see the docstring

        return imchat
    except Exception as exc:  # ImportError, and anything its import-time code raises
        _warn(
            f"the chat reader (imchat.py) could not be loaded ({exc.__class__.__name__}: "
            f"{exc}); everything that needs your message history is skipped."
        )
        return None


def _full_disk_access_sentence() -> str:
    """The truthful fix for "no access", naming the app that actually needs it.

    Full Disk Access is granted to the APP the command runs inside, not to Python,
    so naming Python would send a member to a switch that does not exist.

    **One mechanism, not two.** :func:`imchat._responsible_app` walks the process
    tree to the OUTERMOST ``.app`` bundle and is the authority here.  This module
    previously named the app from ``TERM_PROGRAM``, which the terminal sets — but
    launchd does **not**, and the 03:00 scheduled run is exactly a launchd job.
    Measured with ``TERM_PROGRAM`` stripped: the process walk still answers
    "Visual Studio Code" while the environment version degrades to "the app you
    run Glitch in".  Two mechanisms that agree in a terminal and disagree at 03:00
    is the worst of both, so the walk wins and ``TERM_PROGRAM`` is kept only as a
    fallback for the case where ``imchat`` could not be imported at all.
    """
    try:
        import imchat as _chat

        app = (_chat._responsible_app() or "").strip()
        if app:
            return (
                f"Open System Settings > Privacy & Security > Full Disk Access, switch on {app}, "
                "then quit it and open it again. Until that is on, nothing here can read your "
                "messages, and nothing here will pretend otherwise."
            )
    except Exception:  # imchat absent or unreadable — fall through to the env var
        pass

    known = {
        "Apple_Terminal": "Terminal",
        "iTerm.app": "iTerm",
        "vscode": "Visual Studio Code",
        "WarpTerminal": "Warp",
        "ghostty": "Ghostty",
        "Hyper": "Hyper",
        "WezTerm": "WezTerm",
        "Alacritty": "Alacritty",
        "kitty": "kitty",
    }
    raw = os.environ.get("TERM_PROGRAM", "").strip()
    app = known.get(raw) or raw or "the app you run Glitch in"
    return (
        f"Open System Settings > Privacy & Security > Full Disk Access, switch on {app}, "
        "then quit it and open it again. Until that is on, nothing here can read your "
        "messages, and nothing here will pretend otherwise."
    )


def _sample_recent(chat_module, conn) -> list[Any]:
    """The most recent messages, up to :data:`SAMPLE_ROWS`, for the decode check.

    Widens the look-back window (:data:`SAMPLE_WINDOWS_DAYS`) until the sample is
    full or the whole history has been read.  Recent rows are the ones that matter:
    ``message.text`` is no longer populated on recent macOS releases, so a sample
    taken from the oldest end would measure a read path the plug-in no longer
    depends on and report a healthy number for a broken decoder.

    A ``deque`` with ``maxlen`` keeps the newest rows without holding the window in
    memory.
    """
    first, last = chat_module.date_range(conn)
    if first is None or last is None:
        return []

    for days in SAMPLE_WINDOWS_DAYS:
        since = first if days is None else max(first, last - timedelta(days=days))
        kept: deque = deque(
            chat_module.iter_messages(conn, since, last + timedelta(seconds=1)),
            maxlen=SAMPLE_ROWS,
        )
        if len(kept) >= SAMPLE_ROWS or since <= first:
            return list(kept)
    return list(kept)


# ---------------------------------------------------------------------------
# The report: gathered once as data, then rendered. One source of truth for both
# the plain-words output and --json, so the two can never disagree.
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _pretty_date(value: datetime | None) -> str:
    """``12 Aug 2015`` — built without ``%-d``, which Windows does not have."""
    if value is None:
        return "unknown"
    return f"{value.day} {value:%b %Y}"


def gather(mask: bool = False) -> dict[str, Any]:
    """Everything ``check`` knows, as plain data. Reads only; changes nothing."""
    chat_module = _import_imchat()

    report: dict[str, Any] = {
        "verb": "check",
        "paths": {
            "plugin_home": str(imconfig.HOME),
            "brain_root": str(imconfig.BRAIN_ROOT),
            "engine_scripts": str(imconfig.SCRIPTS_DIR),
            "config": str(imconfig.CONFIG_PATH),
            "local_config": str(imconfig.LOCAL_CONFIG_PATH),
            "user_profile": str(USER_PROFILE_PATH),
            "messages_db": str(chat_module.DB_PATH) if chat_module is not None else None,
        },
        "files": {
            "config_exists": imconfig.CONFIG_PATH.is_file(),
            "local_config_exists": imconfig.LOCAL_CONFIG_PATH.is_file(),
            "user_profile_exists": USER_PROFILE_PATH.is_file(),
            "chat_reader_present": chat_module is not None,
        },
        "platform": sys.platform,
    }

    # --- access -----------------------------------------------------------
    if chat_module is None:
        report["access"] = {
            "ok": False,
            "detail": "the chat reader (imchat.py) is not available in this copy",
            "fix": None,
        }
    else:
        ok, detail = chat_module.has_access()
        report["access"] = {
            "ok": bool(ok),
            "detail": str(detail),
            "fix": None if ok else _full_disk_access_sentence(),
        }

    # --- history, decode rate, own-handle discovery -----------------------
    conn = None
    if chat_module is not None and report["access"]["ok"]:
        try:
            conn = chat_module.connect()
        except Exception as exc:  # a locked, moved or unreadable store
            _warn(f"could not open your message history ({exc.__class__.__name__}: {exc}).")
            report["access"] = {
                "ok": False,
                "detail": f"the message store would not open ({exc.__class__.__name__})",
                "fix": _full_disk_access_sentence(),
            }

    try:
        if conn is not None:
            try:
                total = chat_module.count_rows(conn)
                first, last = chat_module.date_range(conn)
                report["history"] = {
                    "messages": total,
                    "first": _iso(first),
                    "last": _iso(last),
                }
            except Exception as exc:
                _warn(f"could not count your message history ({exc.__class__.__name__}: {exc}).")
                report["history"] = None

            try:
                sample = _sample_recent(chat_module, conn)
                readable = sum(1 for m in sample if m.text and m.text.strip())
                report["decode"] = {
                    "sampled": len(sample),
                    "readable": readable,
                    "rate": (readable / len(sample)) if sample else None,
                    "healthy": bool(sample) and readable / len(sample) >= HEALTHY_DECODE_RATE,
                }
            except Exception as exc:
                _warn(f"could not sample your messages ({exc.__class__.__name__}: {exc}).")
                report["decode"] = None
        else:
            report["history"] = None
            report["decode"] = None

        candidates = discover_own_handles(conn)
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

    # --- contacts ---------------------------------------------------------
    try:
        contact_map = imcontacts.load_map()
        report["contacts"] = {
            "entries": len(contact_map),
            "stores": len(imcontacts.store_paths()),
        }
    except Exception as exc:  # load_map is a never-raises function; belt and braces
        _warn(f"could not read Contacts ({exc.__class__.__name__}: {exc}).")
        report["contacts"] = {"entries": 0, "stores": 0}

    # --- own handles ------------------------------------------------------
    cfg = imconfig.load_config()
    configured = imconfig.require_own_handles(cfg)
    report["own_handles"] = {
        "state": "set" if configured else "not_set",
        "configured": len(configured) if configured else 0,
        "configured_handles": [_show(h, mask) for h in (configured or [])],
        "refusal": None if configured else imconfig.REFUSAL_NO_OWN_HANDLES,
        "candidates": [
            {
                "handle": _show(c.handle, mask),
                "messages": c.messages,
                "conversations": c.conversations,
                "addressed": c.addressed,
                "profile_labels": list(c.profile_labels),
                "from_database": c.from_database,
                "reasons": c.reasons(),
            }
            for c in candidates
        ],
    }
    report["_candidate_handles"] = [c.handle for c in candidates]
    report["write"] = {"performed": False, "target": str(imconfig.LOCAL_CONFIG_PATH)}
    report["never_ingest"] = _never_ingest_section(cfg, mask)
    return report


def _shortcode_min_digits() -> int:
    """imthreads' short-code bar, read from the module that applies it (7 if absent)."""
    try:
        import imthreads  # noqa: PLC0415 - lazy: check must work on a partial copy

        return int(imthreads.SHORTCODE_MIN_DIGITS)
    except Exception:  # a missing or broken grouper must not take check down
        return 7


def _never_ingest_section(cfg: dict[str, Any], mask: bool) -> dict[str, Any]:
    """The never-ingest list as ``check`` shows it, canonicalised, plus the short-code note."""
    raw = cfg.get("never_ingest")
    listed = [h for h in (raw if isinstance(raw, list) else []) if isinstance(h, str)]
    return {
        "count": len(listed),
        "handles": [_show(imcontacts.canonicalise(h) or h, mask) for h in listed],
        "shortcodes_already_filtered": True,
        "shortcode_min_digits": _shortcode_min_digits(),
        "write": None,
    }


def render(report: dict[str, Any]) -> str:
    """The plain-words report — what a member reads, not a debug dump."""
    lines: list[str] = []
    add = lines.append

    add("Your texts — what this plug-in can see. Nothing here changes anything.")
    add("")

    paths = report["paths"]
    files = report["files"]
    add("Where things live")
    add(f"  this plug-in    {paths['plugin_home']}")
    add(
        f"  settings        {paths['config']}"
        f"  ({'present' if files['config_exists'] else 'MISSING'})"
    )
    add(
        f"  your settings   {paths['local_config']}"
        f"  ({'present' if files['local_config_exists'] else 'not written yet'})"
    )
    add(
        f"  your profile    {paths['user_profile']}"
        f"  ({'read-only here' if files['user_profile_exists'] else 'MISSING'})"
    )
    add(f"  your messages   {paths['messages_db'] or 'unknown (the chat reader is missing)'}")
    add("")

    access = report["access"]
    add("Access")
    if access["ok"]:
        add("  OK. I can read your message history.")
    else:
        add(f"  NO. {access['detail']}.")
        if access.get("fix"):
            add(f"  {access['fix']}")
    add("")

    add("Your message history")
    history = report.get("history")
    if history:
        add(
            f"  {history['messages']:,} messages I can read, from "
            f"{_pretty_date(_parse_iso(history['first']))} to "
            f"{_pretty_date(_parse_iso(history['last']))}."
        )
        add("  (Reactions and system events are left out; they are not things anyone said.)")
    else:
        add("  Not read. Without access there is nothing to count.")
    add("")

    add("Whether the words come back")
    decode = report.get("decode")
    if decode and decode["sampled"]:
        rate = decode["rate"] or 0.0
        add(
            f"  I read {decode['sampled']:,} of your most recent messages and "
            f"{decode['readable']:,} came back with readable words ({rate * 100:.1f}%)."
        )
        if decode["healthy"]:
            add(
                "  That is healthy. Attachments, reactions and empty messages carry no "
                "words at all, so this is never quite 100%."
            )
        else:
            add(
                "  That is LOW. Something is wrong with reading message bodies, and "
                "anything built on this would be missing most of what was said."
            )
    else:
        add("  Not checked. Without access there is nothing to read.")
    add("")

    contacts = report["contacts"]
    add("Your contacts")
    if contacts["entries"]:
        add(
            f"  {contacts['entries']:,} saved contacts loaded from "
            f"{contacts['stores']} address book(s) on this Mac."
        )
    else:
        add(
            f"  None loaded (from {contacts['stores']} address book(s) found). "
            "Texts from numbers you have saved would arrive without a name."
        )
    add("")

    own = report["own_handles"]
    add("Your own numbers and addresses")
    if own["state"] == "set":
        add(f"  Set: {own['configured']} of them.")
        for handle in own["configured_handles"]:
            add(f"    {handle}")
        add("  Nothing here needs doing. To change the list, edit the file above by hand.")
    else:
        add("  NOT SET, and that is what is blocking the daily and backfill runs.")
        add(f"  {own['refusal']}")
        add("")
        candidates = own["candidates"]
        if candidates:
            add(f"  I found {len(candidates)} that look like yours, best first:")
            for index, candidate in enumerate(candidates, start=1):
                add(f"    {index}. {candidate['handle']}")
                for reason in candidate["reasons"]:
                    add(f"         - {reason}")
            add("")
            write = report["write"]
            if write["performed"]:
                add(f"  WRITTEN. All {len(candidates)} are now in:")
                add(f"    {write['target']}")
                add("  Read that file and delete anything that is not yours.")
            elif write.get("error"):
                add(f"  NOT WRITTEN. {write['error']}")
            else:
                add("  Nothing was written. To write all of the above into:")
                add(f"    {write['target']}")
                add("  run this command again with --write-own-handles.")
                add(
                    "  Anything already in that file, including never_ingest, is kept "
                    "exactly as it is."
                )
        else:
            add("  I could not find a single one, and I will not invent one.")
            add(
                f"  Copy {imconfig.HOME / 'config.example.json'} to "
                f"{imconfig.LOCAL_CONFIG_PATH} and put your own numbers and addresses in it."
            )

    never = report.get("never_ingest")
    if never is not None:
        add("")
        add("Never read")
        if never["count"]:
            add(f"  {never['count']} number(s) or address(es) whose texts are never read at all:")
            for handle in never["handles"]:
                add(f"    {handle}")
        else:
            add("  None. Nobody's texts are set aside by name.")
        add(
            f"  Short codes (fewer than {never['shortcode_min_digits']} digits: banks, delivery "
            "firms, sign-in codes) are already left out on their own and need no entry here."
        )
        add(
            "  Anything else that is not a person, your own assistant say, can be added with "
            "--write-never-ingest <number or address>."
        )
        write = never.get("write")
        if write and write.get("error"):
            add(f"  NOT WRITTEN. {write['error']}")
        elif write and write.get("added"):
            add(f"  ADDED {len(write['added'])}: " + ", ".join(write["added"]))
            add(f"    {write['target']}")
        elif write:
            add("  Nothing to add: every handle you named was already on the list.")

    return "\n".join(lines)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# preview — the second verb, and Checkpoint 3's own gate.
#
# WHAT IT DOES NOT DO, and this is its defining property: it makes NO CONTACT
# WITH THE PEOPLE SPINE.  It reads messages, groups them into conversation-days,
# folds them to person-days, applies the substance floor, and — only when asked —
# writes the transcript files.  It never imports `people_*`, `memory_*` or
# `meetings_*`, never calls `stamp_interaction`, never opens `memory.db`, never
# touches a person card and never files a proposal.  That is the whole point of
# a preview: the member gets to SEE what a run would land on their cards before
# anything lands on them.  `tests/test_impreview.py` parses this file with `ast`
# and fails the build if any of that becomes untrue, with a negative control
# proving the parser bites.
# ---------------------------------------------------------------------------

#: How many example lines the report shows. Three is enough to recognise the
#: shape of a card line and small enough that the report stays readable.
PREVIEW_SAMPLE: int = 3

#: The label an assumed own-handle carries, in the plain report and in --json
#: alike. ONE string, so what the member reads and what a tool parses cannot
#: drift apart, and so a test can pin the wording in either place.
PREVIEW_ASSUMED_LABEL: str = "ASSUMED, not configured"

#: The sentence that says what an assumption is and is not. It says the quiet
#: part out loud: nothing is saved, and the real runs still refuse.
PREVIEW_ASSUMED_NOTE: str = (
    f"{PREVIEW_ASSUMED_LABEL}: you passed this on the command line, so it is an "
    "assumption for this one preview only. Nothing about it is written anywhere, "
    "and the daily and backfill runs still refuse until your own numbers are "
    "really set. Run `imessage.py check --write-own-handles` to set them."
)

#: How often a person gets a line, in the words the report uses. ONE string,
#: carried in the plain report and in --json alike, so what the member reads and
#: what a tool parses cannot drift apart. Every counterpart gets one line per
#: (person, local day), however often they text (the member's ruling, 2026-09-24).
PREVIEW_LINE_RULE: str = "one line per person per day they texted"

#: The fix named whenever the own-handles refusal fires, in the preview's own
#: words. The refusal sentence itself comes from imconfig and is printed whole.
PREVIEW_REFUSAL_FIX: str = (
    "Run `imessage.py check --write-own-handles` and it will find your own "
    "numbers and addresses and write them into config.local.json. To look at a "
    "window before doing that, pass --assume-own-handle <your number> — that is "
    "a preview-only assumption and is never saved."
)


class PreviewRangeError(ValueError):
    """The window could not be read, and here is the plain reason.

    A refusal a member can act on, not a traceback: every caller prints
    ``str(exc)`` and exits 0.  ``preview`` is something a member runs to look at
    their own data, and a stack trace for "I typed the date wrong" is the fastest
    way to make a tool feel broken.
    """


def _preview_import_imthreads():
    """``imthreads``, or ``None`` with one plain line saying it is not here.

    The same shape as :func:`_import_imchat`, and for the same reason: ``check``
    must keep working on a copy where a later module is missing, and a lazy
    import is what keeps ``preview``'s dependency out of ``check``'s import
    graph entirely.
    """
    try:
        import imthreads  # noqa: PLC0415 - deliberately lazy; see the docstring

        return imthreads
    except Exception as exc:  # ImportError, and anything its import-time code raises
        _warn(
            f"the conversation grouper (imthreads.py) could not be loaded "
            f"({exc.__class__.__name__}: {exc}); there is nothing to preview."
        )
        return None


def _preview_today(chat_module) -> date:
    """Today's date in the CONFIGURED zone, not the machine's.

    The window is the member's days, and ``config.now_local()`` is where the rest
    of this plug-in already gets "what day is it" (``imchat._zone``).  A member
    who travels would otherwise find "yesterday" moving under them, and the
    conversation-day boundaries in :mod:`imthreads` are drawn in the configured
    zone, so a default built from the machine's zone could ask for a day that
    does not exist in the same calendar the units are keyed by.

    Falls back to this machine's date when the chat reader is not importable at
    all — a preview that will refuse two lines later for a better reason.
    """
    if chat_module is not None:
        try:
            return datetime.now(chat_module._zone()).date()
        except Exception:  # a broken engine config must not stop a date lookup
            pass
    return datetime.now().date()


def _preview_parse_day(value: str, label: str) -> date:
    """``YYYY-MM-DD`` as a date, or a plain refusal naming the flag."""
    try:
        return date.fromisoformat(str(value).strip())
    except (AttributeError, TypeError, ValueError):
        raise PreviewRangeError(
            f"I could not read {label} {value!r} as a date. Write it as "
            "year-month-day, like 2026-09-01."
        ) from None


def preview_window(
    date_from: str, date_to: str | None, today: date
) -> tuple[date, date]:
    """``(start, end)`` for the window, both INCLUSIVE — or a plain refusal.

    ``--to`` defaults to **yesterday**, because today is not over: a preview of
    today would report a conversation-day that is still being added to, and the
    member would re-run it an hour later and get a different answer for the same
    command.  Closed days only, by default.

    Two refusals, both :class:`PreviewRangeError` and both one sentence:

    * a window that **ends in the future** — there is nothing there to read, and
      silently clamping it would answer a question the member did not ask;
    * a window that **starts after it ends** — almost always two dates typed the
      wrong way round, and reading it as empty would report "nothing to see"
      about a range that was never looked at.

    ``today`` is a parameter rather than a clock read so a test can corner both
    boundaries without waiting for a date to arrive.
    """
    start = _preview_parse_day(date_from, "--from")
    end = _preview_parse_day(date_to, "--to") if date_to else today - timedelta(days=1)

    if end > today:
        raise PreviewRangeError(
            f"that window ends on {end.isoformat()}, which is in the future — "
            f"today is {today.isoformat()}, and there are no texts yet from days "
            "that have not happened. Pick an end date of today or earlier."
        )
    if start > end:
        raise PreviewRangeError(
            f"that window starts on {start.isoformat()} and ends on "
            f"{end.isoformat()}, so it ends before it begins. Swap the two dates "
            "and run it again."
        )
    return start, end


def _preview_mask_topic(topic: str) -> str:
    """A topic's SHAPE — the structure kept, every quoted word replaced.

    ``--mask`` exists so a preview can be pasted into a plan, a bug report or a
    chat window, and a topic is the one part of this report that carries a
    member's actual words.  The two shapes :func:`imthreads.topic_for` builds are
    matched explicitly and anything else is masked WHOLE, so a change to that
    function fails closed — an unrecognised topic leaks nothing rather than
    passing through as-is.
    """
    one_to_one = re.match(r"^(Texts \(\d+\)): (.*)$", topic, re.DOTALL)
    if one_to_one:
        return f"{one_to_one.group(1)}: <{len(one_to_one.group(2))} chars>"
    group_chat = re.match(r"^(Group texts in )(.*)( \(\d+ messages\))$", topic, re.DOTALL)
    if group_chat:
        return f"{group_chat.group(1)}<{len(group_chat.group(2))} chars>{group_chat.group(3)}"
    return f"<{len(topic)} chars>"


def _preview_mask_link(link: str) -> str:
    """A stored-thread link with the person's name taken out of the filename.

    The folders and the date are structure and say nothing about anybody; the
    slug is a Contacts name and the ``chat_rowid`` is a local integer that means
    nothing off this machine.  An unrecognised filename is masked whole, for the
    same fail-closed reason as :func:`_preview_mask_topic`.
    """
    head, _, name = link.rpartition("/")
    shaped = re.match(r"^(\d{4}-\d{2}-\d{2})-(.*)-(\d+)\.txt$", name)
    masked = (
        f"{shaped.group(1)}-<{len(shaped.group(2))} chars>-{shaped.group(3)}.txt"
        if shaped
        else f"<{len(name)} chars>"
    )
    return f"{head}/{masked}" if head else masked


def preview_gather(
    *,
    date_from: str,
    date_to: str | None = None,
    assume_own_handles: Sequence[str] = (),
    write: bool = False,
    mask: bool = False,
) -> tuple[dict[str, Any], int]:
    """Everything ``preview`` knows, as plain data, plus the exit code.

    Reads only, unless ``write`` is true — and then the ONLY thing written is a
    transcript per conversation-day, through :func:`imthreads.write_thread`,
    which refuses any target outside :data:`imconfig.THREADS_DIR`.  There is no
    second write path here on purpose: one guarded door, or none.

    The order of the gates is the order a member would ask the questions in, and
    each one returns early with a report that is still complete enough to read:
    is the grouper here → is the window sane → do I know which numbers are yours
    → can I read your messages at all.  Every one of them exits 0.

    **Every included person-day is one line** — :data:`PREVIEW_LINE_RULE`,
    however often that person texted.  So the line count needs no ledger and no
    history: it is what this window holds, whatever was recorded before it.
    """
    chat_module = _import_imchat()
    threads = _preview_import_imthreads()

    report: dict[str, Any] = {
        "verb": "preview",
        "paths": {
            "plugin_home": str(imconfig.HOME),
            "brain_root": str(imconfig.BRAIN_ROOT),
            "engine_scripts": str(imconfig.SCRIPTS_DIR),
            "config": str(imconfig.CONFIG_PATH),
            "local_config": str(imconfig.LOCAL_CONFIG_PATH),
            "threads_dir": str(imconfig.THREADS_DIR),
            "messages_db": str(chat_module.DB_PATH) if chat_module is not None else None,
        },
        "window": None,
        "own_handles": None,
        "access": None,
        "read": None,
        "units": None,
        "lines": None,
        "sample": [],
        "write": {
            "requested": bool(write),
            "performed": False,
            "files": 0,
            "bytes": 0,
            "directory": str(imconfig.THREADS_DIR),
        },
        "error": None,
    }

    if threads is None:
        report["error"] = (
            "the conversation grouper (imthreads.py) is not in this copy of the "
            "plug-in, so there is nothing to preview."
        )
        return report, 0

    # --- the window -------------------------------------------------------
    today = _preview_today(chat_module)
    try:
        start, end = preview_window(date_from, date_to, today)
    except PreviewRangeError as exc:
        report["error"] = str(exc)
        return report, 0

    report["window"] = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "days": (end - start).days + 1,
        "to_defaulted": date_to is None,
        "ends_today": end == today,
    }

    # --- whose numbers are yours -----------------------------------------
    cfg = imconfig.load_config()
    assumed = [h for h in (str(entry).strip() for entry in assume_own_handles) if h]
    if assumed:
        cfg["own_handles"] = list(assumed)

    try:
        own = threads.owner_handles(cfg)
    except threads.OwnHandlesRequired as exc:
        report["own_handles"] = {
            "source": "assumed" if assumed else "config",
            "assumed": bool(assumed),
            "label": PREVIEW_ASSUMED_LABEL if assumed else None,
            "count": 0,
            "handles": [],
            "refusal": str(exc),
            "fix": PREVIEW_REFUSAL_FIX,
            "detail": (
                "the handle you passed with --assume-own-handle is not a phone "
                "number or an address I can read."
                if assumed
                else None
            ),
        }
        return report, 0

    report["own_handles"] = {
        "source": "assumed" if assumed else "config",
        "assumed": bool(assumed),
        "label": PREVIEW_ASSUMED_LABEL if assumed else None,
        "count": len(own),
        "handles": sorted(_show(handle, mask) for handle in own),
        "refusal": None,
        "note": PREVIEW_ASSUMED_NOTE if assumed else None,
        "detail": None,
    }

    # --- access -----------------------------------------------------------
    if chat_module is None:
        report["access"] = {
            "ok": False,
            "detail": "the chat reader (imchat.py) is not available in this copy",
            "fix": None,
        }
        return report, 0

    ok, detail = chat_module.has_access()
    report["access"] = {
        "ok": bool(ok),
        "detail": str(detail),
        "fix": None if ok else _full_disk_access_sentence(),
    }
    if not ok:
        return report, 0

    # --- the read ---------------------------------------------------------
    # Inclusive dates, half-open read: imchat's window is [since, until), so an
    # inclusive end date means midnight at the START of the following day.
    since = datetime.combine(start, time.min)
    until = datetime.combine(end + timedelta(days=1), time.min)

    try:
        conn = chat_module.connect()
    except Exception as exc:  # a locked, moved or unreadable store
        _warn(f"could not open your message history ({exc.__class__.__name__}: {exc}).")
        report["access"] = {
            "ok": False,
            "detail": f"the message store would not open ({exc.__class__.__name__})",
            "fix": _full_disk_access_sentence(),
        }
        return report, 0

    try:
        messages = list(chat_module.iter_messages(conn, since, until))
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    try:
        contacts = imcontacts.load_map()
    except Exception as exc:  # load_map never raises; belt and braces
        _warn(f"could not read Contacts ({exc.__class__.__name__}: {exc}).")
        contacts = {}

    # --- group, floor, fold ----------------------------------------------
    chat_days = threads.group(messages, own_handles=own)
    included = {
        key: unit for key, unit in chat_days.items() if threads.is_included(unit, cfg)
    }
    person_days = {
        key: unit
        for key, unit in threads.fold_to_person_days(included).items()
        if threads.is_included(unit, cfg)
    }

    report["read"] = {
        "messages": len(messages),
        "days_with_texts": len({m.dt_local.date() for m in messages}),
    }
    report["units"] = {
        "chat_days": len(chat_days),
        "included": len(included),
        "set_aside": len(chat_days) - len(included),
        "person_days": len(person_days),
        "people": len({unit.identifier for unit in person_days.values()}),
    }

    # One line per person per day they texted, however often they text: every
    # included person-day is exactly one line, so the count is the person-days.
    report["lines"] = {
        "count": len(person_days),
        "rule": PREVIEW_LINE_RULE,
    }

    # --- the sample -------------------------------------------------------
    ordered = sorted(included.values(), key=lambda unit: (unit.day, unit.chat_rowid))
    for unit in ordered[-PREVIEW_SAMPLE:]:
        topic = threads.topic_for(unit)
        link = threads.thread_link(threads.thread_path(unit, contacts))
        report["sample"].append(
            {
                "day": unit.day.isoformat(),
                "topic": _preview_mask_topic(topic) if mask else topic,
                "link": _preview_mask_link(link) if mask else link,
            }
        )

    # --- the write, or what it would be ----------------------------------
    exit_code = 0
    if write:
        written: list[Path] = []
        try:
            for unit in ordered:
                written.append(threads.write_thread(unit, contacts))
        except (OSError, ValueError) as exc:
            report["write"]["error"] = (
                f"the transcripts stopped part-way through ({exc}). "
                f"{len(written)} of {len(ordered)} were written; nothing outside "
                "the threads folder was touched."
            )
            exit_code = 1

        total_bytes = 0
        for path in written:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
        report["write"]["performed"] = bool(written)
        report["write"]["files"] = len(written)
        report["write"]["bytes"] = total_bytes
    else:
        report["write"]["would_write"] = len(included)

    return report, exit_code


def _preview_plural(count: int, singular: str, plural: str | None = None) -> str:
    """``"1 day"`` / ``"5 days"`` — a count and its noun, agreeing.

    A report written for a member should not say "1 conversation-day(s)". The
    parenthesised plural is the shortcut that turns plain words back into a form,
    and this report is the first thing a member ever reads about their own texts.
    """
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count:,} {word}"


def preview_render(report: dict[str, Any]) -> str:
    """The plain-words report — what it MEANS, before any of the machinery."""
    lines: list[str] = []
    add = lines.append

    window = report.get("window")
    if window:
        add(
            f"Your texts from {window['from']} to {window['to']} — what would land "
            "on people's cards, and what would not."
        )
    else:
        add("Your texts — what would land on people's cards, and what would not.")

    write = report["write"]
    if write["requested"] and write["files"]:
        add("Transcripts were written. Nothing was put on anybody's card.")
    elif write["requested"]:
        add(
            "No transcripts were written: there was nothing to write. Nothing was put on "
            "anybody's card."
        )
    else:
        add("Nothing was written. This is a look, not a run.")
    add("")

    if report.get("error"):
        add(report["error"])
        return "\n".join(lines)

    own = report.get("own_handles") or {}
    if own.get("refusal"):
        add("Whose numbers are yours")
        add(f"  {own['refusal']}")
        if own.get("detail"):
            add(f"  {own['detail']}")
        add(f"  {own['fix']}")
        return "\n".join(lines)

    add("Whose numbers I treated as yours")
    if own.get("assumed"):
        add(f"  {_preview_plural(own['count'], 'handle')} you passed on the command line:")
        for handle in own["handles"]:
            add(f"    {handle}")
        add(f"  {own['note']}")
    else:
        add(
            f"  {_preview_plural(own['count'], 'handle')} from your own settings "
            "file, as configured."
        )
    add("")

    access = report.get("access") or {}
    if not access.get("ok"):
        add("Access")
        add(f"  NO. {access.get('detail', 'your message history could not be read')}.")
        if access.get("fix"):
            add(f"  {access['fix']}")
        return "\n".join(lines)

    read = report["read"]
    units = report["units"]
    add("What I read")
    add(
        f"  {_preview_plural(window['days'], 'day')} of your history, "
        f"{window['from']} to {window['to']}, and you texted on "
        f"{read['days_with_texts']:,} of them."
    )
    add(
        f"  {_preview_plural(read['messages'], 'message')}, in "
        f"{_preview_plural(units['chat_days'], 'conversation-day')}."
    )
    if window["to_defaulted"]:
        add("  (I stopped at yesterday, because today is not over yet.)")
    elif window["ends_today"]:
        add("  (That window ends today, which is not over, so today's count will grow.)")
    add("")

    add("What is worth keeping")
    add(
        f"  {units['included']:,} of those {units['chat_days']:,} conversation-days "
        "clear the floor and are worth recording."
    )
    add(
        f"  {units['set_aside']:,} "
        f"{'was' if units['set_aside'] == 1 else 'were'} set aside as too thin — "
        "one side talking, a lone 'ok', or no real words at all."
    )
    add(f"  Between them they cover {_preview_plural(units['people'], 'person', 'people')}.")
    add("")

    landing = report["lines"]
    add("How many lines would land on cards")
    add(f"  {_preview_plural(landing['count'], 'line')}: {landing['rule']}.")
    add(
        "  However often someone texts, every day they texted gets a line of its "
        "own, and a person in several conversations on one day still gets one."
    )
    add("")

    if report["sample"]:
        add("What a line would look like")
        for entry in report["sample"]:
            add(f"  {entry['day']}  {entry['topic']}")
            add(f"    -> {entry['link']}")
        add("")

    add("The transcripts")
    if write["requested"] and not write["files"] and not write.get("error"):
        add("  None written: no conversation-day in this window cleared the floor.")
        add(f"    {write['directory']}")
    elif write["requested"]:
        if write.get("error"):
            add(f"  NOT ALL WRITTEN. {write['error']}")
        add(
            f"  {_preview_plural(write['files'], 'file')}, "
            f"{write['bytes']:,} bytes in total, in:"
        )
        add(f"    {write['directory']}")
        add(
            "  They stay on this machine: that folder is gitignored and left out of "
            "backup, and nothing here sends them anywhere."
        )
    else:
        add(
            "  Nothing was written. With --write I would write "
            f"{_preview_plural(write.get('would_write', 0), 'transcript file')} — "
            "one per conversation-day above — in:"
        )
        add(f"    {write['directory']}")
        add(
            "  Nothing outside that folder, nothing on anybody's card, and nothing "
            "off this machine."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# daily and status — the pipeline lives in imrun; this is only its CLI.
#
# Kept apart from preview on purpose: preview makes no contact with the people
# spine (tests/test_impreview.py proves it by walking preview's code path), while
# these two verbs reach the spine through imrun -> imspine. Nothing here imports
# an engine module itself, and imrun is imported lazily so `check` and `preview`
# still run on a copy where the spine side is missing or broken.
# ---------------------------------------------------------------------------

#: What each hold reason means, in the member's words. ONE table for the daily
#: line and status alike.
HOLD_WORDS: dict[str, str] = {
    "pending": "waiting on your yes to a new card",
    "attach_pending": "waiting on your second yes, to attach the number",
    "ambiguous": "a number on more than one card, waiting for you to say whose",
    "dismissed": "a number you said no to",
    "new": "held for your review",
    "unresolved": "nothing to match the number on",
    "cannot_attach": "accepted, but the number would not attach to the card",
    "accepted_unattached": "accepted; attaching the number is waiting on a card I can name",
    "accepted_unknown": "accepted; waiting to learn which card it became",
    "wrong_match": "a number you said matched the wrong card, held until it reaches the right one",
}


def _import_imrun():
    """``imrun``, or ``None`` with one plain line — the same shape as ``_import_imchat``."""
    try:
        import imrun  # noqa: PLC0415 - deliberately lazy; see the section comment

        return imrun
    except Exception as exc:  # ImportError, and anything its import-time code raises
        _warn(
            f"the run module (imrun.py) could not be loaded ({exc.__class__.__name__}: "
            f"{exc}); the daily run and status need it."
        )
        return None


def daily_render(report: dict[str, Any]) -> str:
    """The daily run in plain words — counts only, never a name, number or message."""
    lines: list[str] = []
    add = lines.append
    dry = report.get("dry_run")
    add(
        "Your texts, the daily run"
        + (" (a dry run: nothing was written, nothing was raised)." if dry else ".")
    )
    paused = report.get("paused")
    if paused:
        add(paused["sentence"])
        return "\n".join(lines)

    mark, read = report["watermark"], report["read"]
    add(f"Days are drawn in {report['zone']}. Today, {report['today']}, is read once it is over.")
    add("")
    if mark.get("store_reset"):
        add(
            "Your Messages history looks rebuilt (its newest row is below the last one I "
            "read), so I read yesterday afresh. Older days need a backfill."
        )
    elif mark.get("first_run"):
        add("First run: I read yesterday only. Older history is what backfill is for.")
    else:
        add(
            f"{read['rows']:,} message row(s) arrived since the last run"
            + (f", {read['rows_in_open_day']:,} of them from today" if read["rows_in_open_day"]
               else "")
            + "."
        )
    add(
        f"I read {read['messages']:,} message(s) across {read['days']} day(s): "
        f"{report['conversation_days']:,} conversation-day(s) worth keeping, "
        f"{report['person_days']:,} person-day(s)."
    )
    verb = "Would queue" if dry else "Queued"
    add(
        f"{verb} {report['queued']:,} for their summary"
        + (f" ({report['released_from_hold']:,} released from hold)"
           if report["released_from_hold"] else "")
        + "."
    )
    if report.get("links_refreshed"):
        add(
            f"{'Would add' if dry else 'Added'} a late conversation to "
            f"{report['links_refreshed']:,} day(s) already waiting for their summary."
        )
    if report["held_total"]:
        add(f"{'Would hold' if dry else 'Held'} {report['held_total']:,}:")
        for reason, count in report["held"].items():
            add(f"  {count:,} {HOLD_WORDS.get(reason, reason)}")
    if report.get("new_for_review"):
        add(
            f"{report['new_for_review']:,} new number(s) "
            + ("would go" if dry else "went")
            + " onto your texts review list; none were put on your main people queue."
        )
    raised = report["would_raise"] if dry else report["raised"]
    if raised:
        add(
            ("Would raise " if dry else "Raised ")
            + ", ".join(f"{n} {kind.replace('_', ' ')}" for kind, n in raised.items())
            + " for your yes."
        )
    if report.get("owner_skipped"):
        add(f"{report['owner_skipped']:,} were your own texts, which never land on a card.")
    stale = report["stale"]
    if stale["due"]:
        if dry:
            add(
                f"{stale['due']:,} queued day(s) have waited long enough to land with "
                "their plain line."
            )
        else:
            add(
                f"Filed {stale['stamped']:,} of {stale['due']:,} long-waiting day(s) with their "
                "plain line" + (f"; {stale['remaining']:,} left for tomorrow" if stale["remaining"]
                                else "") + "."
            )
        if stale.get("pre_backfill_copies"):
            add(
                f"Kept a copy of {stale['pre_backfill_copies']} card(s) in pre-backfill/ "
                "before their first backfilled line: that copy is the way back."
            )
        for detail in stale["failures"]:
            add(f"  not filed: {detail}")
    if report["recovered"]["done"] or report["recovered"]["dropped"]:
        add(
            f"Settled {report['recovered']['done'] + report['recovered']['dropped']} unfinished "
            "stamp(s) from an interrupted run."
        )
    if report.get("why_nothing_queued"):
        add(f"Nothing new was queued: {report['why_nothing_queued']}.")
    add("")
    add(
        f"Waiting: {report['queue']['depth']:,} day(s) in the queue"
        + (f", the oldest from {report['queue']['oldest_day']}" if report["queue"]["oldest_day"]
           else "")
        + f"; {report['awaiting_yes']:,} number(s) waiting on your yes"
        + f"; {report.get('awaiting_review', 0):,} number(s) on your texts review list"
        + f"; {report['failures']:,} day(s) that would not file (see status)."
    )
    after = mark.get("after")
    add(
        f"Watermark: row {mark.get('before')} -> row {after}"
        + (" (held below today's texts until the day is over)" if mark.get("held_by_open_day")
           else "")
        + (" (held where the run ran out of time)" if mark.get("held_by_budget") else "")
        + ("." if not dry else ", if this were a real run.")
    )
    return "\n".join(lines)


def status_render(report: dict[str, Any], mask: bool = False) -> str:
    """``status`` in plain words.  Numbers show as they are unless ``--mask``."""
    lines: list[str] = []
    add = lines.append
    add("Your texts, status. Nothing here changes anything.")
    add(f"Days are drawn in {report['zone']}.")
    if report.get("error"):
        add(f"PAUSED: {report['error']}")
        return "\n".join(lines)
    add("")
    state = report.get("state") or {}
    if state.get("first_run_pending"):
        add("Watermark: none yet. The first daily run reads yesterday only.")
    else:
        add(
            f"Watermark: message row {state.get('watermark')}; the newest message read was "
            f"sent {state.get('newest_read_at') or 'unknown'}."
        )
        add(f"Last run: {state.get('last_run_at') or 'unknown'} ({state.get('zone_of_last_run')}).")
    backfill = state.get("backfill")
    if backfill:
        if backfill.get("complete"):
            add(f"Backfill: {backfill.get('from')} to {backfill.get('to')}, finished.")
        else:
            add(
                f"Backfill: {backfill.get('from')} to {backfill.get('to')}, stopped before "
                f"{backfill.get('next')}; ask for the same dates again and it continues there."
            )
    if report.get("busy"):
        add("A run is going right now, so the ledger could not be read. Try again in a minute.")
        return "\n".join(lines)
    ledger = report.get("ledger") or {}
    if not ledger.get("exists"):
        add("Ledger: none yet. Nothing has been queued or filed.")
        return "\n".join(lines)
    queue = ledger["queue"]
    add(
        f"Queue: {queue['depth']:,} day(s) waiting for their summary"
        + (f", the oldest from {queue['oldest_day']}." if queue["oldest_day"] else ".")
    )
    for claim in ledger["live_claims"]:
        add(f"  a summary pass holds {claim['days']} of them until {claim['lease_until']}")
    unclaimed = ledger.get("unclaimed") or {}
    if queue["depth"] and unclaimed.get("depth") != queue["depth"]:
        add(
            f"  {unclaimed.get('depth', 0):,} wait for a summary pass"
            + (f", the oldest from {unclaimed['oldest_day']}." if unclaimed.get("oldest_day")
               else ".")
        )
    add(f"Filed: {ledger['counts']['stamped']:,} day(s) on cards.")
    add(f"Held: {ledger['held_units']:,} day(s) waiting until their number is known.")
    states = ledger["identifiers_by_state"]
    if states:
        add("Numbers: " + ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in states.items()) + ".")
    add(f"Waiting on your yes: {ledger['awaiting_yes']} number(s).")
    add(
        f"On your texts review list: {ledger.get('awaiting_review', 0)} number(s); a new "
        "number waits there, never on your main people queue."
    )
    failures = ledger["failures"]
    if failures:
        add(f"Would not file ({len(failures)}):")
        for failure in failures:
            who = _show(failure["identifier"], mask)
            add(f"  {who} {failure['day']}: {failure['detail']} (x{failure['count']})")
    else:
        add("Would not file: none.")
    recovery = ledger["recovery"]
    if ledger["open_intents"]:
        add(f"Unfinished stamps to settle on the next run: {ledger['open_intents']}.")
    if recovery["discarded"] or recovery["intents_kept"]:
        add(
            f"The last run did not finish: {recovery['discarded']} change(s) were set aside and "
            f"{recovery['intents_kept']} stamp(s) are being re-checked against the cards."
        )
    if recovery["torn_tail"]:
        add("The ledger's log ended mid-line after an interruption; that line is ignored.")
    if recovery["ledger_missing_but_watermarked"]:
        add(
            "WARNING: the ledger file is missing but the watermark is not, so days that were "
            "queued or held before it went are no longer known."
        )
    return "\n".join(lines)


def cmd_daily(args: argparse.Namespace) -> int:
    run = _import_imrun()
    if run is None:
        print("texts: the daily run is not available in this copy of the plug-in.")
        return 0
    try:
        report = run.daily(dry_run=args.dry_run)
    except Exception as exc:
        _warn(
            f"the daily run stopped on an unexpected error ({exc.__class__.__name__}: {exc}); "
            "nothing from this run counts, and the next run starts from the same place."
        )
        return 1
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(daily_render(report))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    run = _import_imrun()
    if run is None:
        print("texts: status is not available in this copy of the plug-in.")
        return 0
    report = run.status()
    if args.json:
        if args.mask:
            for failure in (report.get("ledger") or {}).get("failures", []):
                failure["identifier"] = mask_handle(failure["identifier"])
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(status_render(report, mask=args.mask))
    return 0


# ---------------------------------------------------------------------------
# synthesise (CP4b): the summary seam.  The work lives in imsynth.py; this wires it.
# ---------------------------------------------------------------------------


def _import_imsynth():
    """``imsynth``, or ``None`` with one plain line — the same shape as ``_import_imrun``."""
    try:
        import imsynth  # noqa: PLC0415 - deliberately lazy, like imrun

        return imsynth
    except Exception as exc:  # ImportError, and anything its import-time code raises
        _warn(
            f"the summary module (imsynth.py) could not be loaded ({exc.__class__.__name__}: "
            f"{exc}); synthesise needs it."
        )
        return None


def synthesise_render(report: dict[str, Any]) -> str:
    """A claim in plain words: how many days, until when, and what is left.  No names."""
    lines: list[str] = []
    add = lines.append
    paused = report.get("paused")
    if paused:
        add(paused["sentence"])
        return "\n".join(lines)
    if not report.get("claim_path"):
        add(f"Nothing to summarise right now: {report.get('why_nothing') or 'nothing is waiting'}.")
        return "\n".join(lines)
    days = report.get("days") or []
    span = f"{days[0]} to {days[-1]}" if len(days) > 1 else (days[0] if days else "")
    add(
        f"Claimed {report['claimed']} day(s) of texts for their summary ({span}); they are "
        f"held for this pass until {report['lease_until']}."
    )
    if report.get("left_waiting"):
        add(f"{report['left_waiting']} more day(s) wait for the next pass.")
    if report.get("no_transcript"):
        add(
            f"{report['no_transcript']} day(s) have no transcript on this machine and were left "
            "for their plain line."
        )
    add(
        "Write one summary per unit into the claim file's summaries map (its instructions "
        "say how), then run synthesise --commit with the path below."
    )
    return "\n".join(lines)


def synthesise_commit_render(report: dict[str, Any]) -> str:
    """A commit in plain words, per unit id and day.  Never a name, number or summary."""
    lines: list[str] = []
    add = lines.append
    paused = report.get("paused")
    if paused:
        add(paused["sentence"])
        return "\n".join(lines)
    if report.get("error"):
        add(f"Nothing was filed: {report['error']}.")
        return "\n".join(lines)
    days = report.get("days") or {}

    def label(uid: str) -> str:
        return f"{uid} ({days[uid]})" if days.get(uid) else uid

    landed = report["landed"] + report["already_on_card"]
    add(f"Filed {len(landed)} summary line(s) onto cards, of {report['units']} in the claim.")
    for heading, bucket in (
        ("Refused, back in the queue", report["refused"]),
        ("Back in the queue", report["released"]),
        ("Would not file, still queued (see status)", report["failed"]),
        ("No longer this claim's", report["skipped"]),
    ):
        if bucket:
            add(f"{heading} ({len(bucket)}):")
            for uid, why in bucket.items():
                add(f"  {label(uid)}: {why}")
    if report.get("unknown_summaries"):
        add(f"{report['unknown_summaries']} summary entr(ies) named no unit and were ignored.")
    recovered = report.get("recovered") or {}
    if recovered.get("done") or recovered.get("dropped"):
        add(
            f"Settled {recovered.get('done', 0) + recovered.get('dropped', 0)} unfinished "
            "stamp(s) from an interrupted run first."
        )
    add("The claim file was removed." if report.get("claim_file_removed")
        else "The claim file is still there; committing it again lands nothing new.")
    return "\n".join(lines)


def cmd_synthesise(args: argparse.Namespace) -> int:
    synth = _import_imsynth()
    if synth is None:
        print("texts: the summary pass is not available in this copy of the plug-in.")
        return 0
    if args.commit is not None:
        try:
            report = synth.commit(args.commit)
        except Exception as exc:
            _warn(
                f"the commit stopped on an unexpected error ({exc.__class__.__name__}: {exc}); "
                "nothing from it counts, and committing the same file again is safe."
            )
            return 1
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        else:
            print(synthesise_commit_render(report))
        return 1 if report.get("error") else 0
    try:
        report = synth.claim(args.limit)
    except Exception as exc:
        _warn(
            f"the claim stopped on an unexpected error ({exc.__class__.__name__}: {exc}); "
            "nothing was claimed."
        )
        return 1
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(synthesise_render(report))
        if report.get("claim_path"):
            print(f"CLAIM_PATH: {report['claim_path']}")
    return 0


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--limit takes a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError("--limit takes a number of days, 1 or more")
    return number


# ---------------------------------------------------------------------------
# review (CP6): the bulk pass over every held number.  The work lives in imreview.py.
# ---------------------------------------------------------------------------


def _import_imreview():
    """``imreview``, or ``None`` with one plain line — the same shape as ``_import_imrun``."""
    try:
        import imreview  # noqa: PLC0415 - deliberately lazy, like imrun

        return imreview
    except Exception as exc:  # ImportError, and anything its import-time code raises
        _warn(
            f"the review module (imreview.py) could not be loaded ({exc.__class__.__name__}: "
            f"{exc}); review needs it."
        )
        return None


def _row_number(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--dismiss-below takes a row number") from None
    if number < 1:
        raise argparse.ArgumentTypeError("--dismiss-below takes a row number, 1 or more")
    return number


def cmd_review(args: argparse.Namespace) -> int:
    """List, preview or act.  Exit 1 only when a preview or an act was refused."""
    rev = _import_imreview()
    if rev is None:
        print("texts: the review is not available in this copy of the plug-in.")
        return 0
    try:
        report = rev.review(
            accept=args.accept, dismiss=args.dismiss, dismiss_below=args.dismiss_below,
            names=args.name or None, listing=args.listing, confirm=args.confirm,
            show_numbers=args.show_numbers,
        )
    except Exception as exc:
        _warn(
            f"the review stopped on an unexpected error ({exc.__class__.__name__}: {exc}); "
            "every row finished before it stands, and running review again shows where "
            "things are."
        )
        return 1
    if args.json:
        print(json.dumps(rev.public(report, args.show_numbers), indent=2, ensure_ascii=False))
    else:
        print(rev.render(report, show_numbers=args.show_numbers))
    # An act or preview that could not be done as asked is not a success; a list is.
    refused = report.get("refused") or (report.get("paused") and args.confirm)
    return 1 if refused else 0


def cmd_preview(args: argparse.Namespace) -> int:
    report, exit_code = preview_gather(
        date_from=args.date_from,
        date_to=args.date_to,
        assume_own_handles=args.assume_own_handle or (),
        write=args.write,
        mask=args.mask,
    )
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(preview_render(report))
    return exit_code


# ---------------------------------------------------------------------------
# show, backfill, exclude, include (CP6).  The work lives in imshow, imbackfill and
# imexclude; this wires them and says what they did in plain words.
# ---------------------------------------------------------------------------


def _import_cp6(name: str):
    """One of the CP6 modules, or ``None`` with one plain line — the ``_import_imrun`` shape."""
    try:
        return __import__(name)
    except Exception as exc:  # ImportError, and anything its import-time code raises
        _warn(f"{name}.py could not be loaded ({exc.__class__.__name__}: {exc}).")
        return None


#: A direction as the member would say it.
_DIRECTION_WORDS = {
    "they_reached_out": "they reached out",
    "i_reached_out": "you reached out",
    "mutual": "back and forth",
}


def _match_lines(match: dict[str, Any] | None) -> list[str]:
    """Who the words found, or why nobody, or the candidates: never a guess."""
    if not match:
        return []
    if match["status"] == "none":
        return [f'I found nobody for "{match["asked"]}": {match.get("why")}.']
    if match["status"] == "ambiguous":
        lines = [f'More than one person goes by "{match["asked"]}", and I never guess which:']
        for index, cand in enumerate(match["candidates"], start=1):
            where = f"card {cand['card']}" if cand.get("card") else "in your Contacts only"
            numbers = ", ".join(cand.get("identifiers") or []) or "no number known"
            lines.append(f"  {index}. {cand['name']} ({where}; {numbers})")
        lines.append("Say which one: their full name, their number, or their card id (prs_…).")
        return lines
    return []


def show_render(report: dict[str, Any]) -> str:
    """``show`` in plain words: newest first, each day's transcript tail under its line."""
    lines: list[str] = []
    add = lines.append
    if report.get("paused"):
        return report["paused"]["sentence"]
    if report.get("error"):
        return f"Nothing was read: {report['error']}."
    match = report.get("match") or {}
    if match.get("status") != "one":
        return "\n".join(_match_lines(match))
    who = match["who"]
    add(f"Texts with {who['name']}"
        + (f" (their card: {who['card']})." if who.get("card") else " (no card yet)."))
    total, shown = report["total_days"], len(report["days"])
    if not total:
        add("No day of texts with them is on file"
            + (f" since {report['since']}" if report.get("since") else "") + " yet.")
    else:
        add(f"Newest first: {shown} of {total} day(s) of texts"
            + (f" since {report['since']}" if report.get("since") else "") + ".")
    for entry in report["days"]:
        add("")
        state = {"on_card": "on the card",
                 "waiting": "not on the card yet, waiting for its summary",
                 "held": "not on the card yet, its number is on your texts review list"}
        add(f"{entry['day']}, {_DIRECTION_WORDS.get(entry['direction'], entry['direction'])}, "
            f"{state.get(entry['state'], entry['state'])}")
        if entry.get("summary"):
            add(f"  {entry['summary']}")
        for thread in entry.get("threads", []):
            if thread.get("capped"):
                add(f"  (the transcript {thread['link']} is left out to keep this short)")
                continue
            note = (f" (its first {thread['lines_cut']} line(s) are not shown)"
                    if thread.get("lines_cut") else "")
            add(f"  From {thread['link']}{note}:")
            lines.extend(f"    {line}" for line in (thread.get("text") or "").splitlines())
        if entry.get("missing_threads"):
            add("  (its transcript is not on this machine)")
    today = report.get("today")
    if today is not None:
        add("")
        if today.get("error"):
            add(f"Today: not read. {today['error']}")
        elif not today["conversations"]:
            add("Today so far: no texts with them yet (read live; nothing was stored).")
        else:
            add(f"Today so far ({today['messages']} message(s), read live; nothing was stored):")
            for conv in today["conversations"]:
                cut = (f", its first {conv['lines_cut']} line(s) not shown"
                       if conv.get("lines_cut") else "")
                add(f"  In {conv['label']}{cut}:")
                lines.extend(f"    {line}" for line in conv["text"].splitlines())
    if report.get("cut"):
        add("")
        add("Left out:")
        lines.extend(f"  {item}" for item in report["cut"])
    return "\n".join(lines)


def show_day_render(report: dict[str, Any]) -> str:
    """``show --day`` in plain words: who, and where each of their days stands."""
    if report.get("paused"):
        return report["paused"]["sentence"]
    if report.get("error"):
        return f"Nothing was read: {report['error']}."
    lines = [f"Your texts on {report['day']}"
             + (f", as of the last morning run ({report['as_of']})." if report.get("as_of")
                else ", as far as the texts have been read in.")]
    if not report["people"]:
        lines.append("Nobody: no conversation that day is on file. A day is read in the "
                     "morning after it ends, and a thin one (a lone 'ok') is never kept.")
        return "\n".join(lines)
    words = {"on_card": "on their card", "waiting": "waiting for its summary",
             "held": "their number is on your texts review list"}
    for person in report["people"]:
        lines.append(f"  {person['name']}: {_DIRECTION_WORDS.get(person['direction'], '')}, "
                     f"{words.get(person['state'], person['state'])}"
                     + (f". {person['summary']}" if person.get("summary") else ""))
    lines.append("Where you left off with any of them: show --person <their name>.")
    return "\n".join(lines)


def cmd_show(args: argparse.Namespace) -> int:
    mod = _import_cp6("imshow")
    if mod is None:
        print("texts: show is not available in this copy of the plug-in.")
        return 0
    try:
        if args.day is not None:
            report = mod.show_day(args.day, show_numbers=args.show_numbers)
        else:
            report = mod.show(args.person, since=args.since, today=args.today,
                              limit=args.limit, show_numbers=args.show_numbers)
    except Exception as exc:
        _warn(f"show stopped on an unexpected error ({exc.__class__.__name__}: {exc}); "
              "nothing was written.")
        return 1
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(show_day_render(report) if args.day is not None else show_render(report))
    return 1 if report.get("error") else 0


def backfill_render(report: dict[str, Any]) -> str:
    """``backfill`` in plain words: counts, the measured duration, and the undo note."""
    lines: list[str] = []
    add = lines.append
    win = report.get("window") or {}
    dry = not report.get("confirm")
    head = (f"Your texts, a backfill of {win.get('from')} to {win.get('to')}" if win
            else "Your texts, a backfill")
    add(head + (" (a dry run: nothing was written, nothing was raised)." if dry else "."))
    if report.get("error"):
        add(f"Nothing was read: {report['error']}")
        return "\n".join(lines)
    if report.get("paused"):
        add(report["paused"]["sentence"])
        return "\n".join(lines)
    if report.get("set_aside"):
        other = report["set_aside"]
        add(f"(An unfinished backfill of {other.get('from')} to {other.get('to')} stopped "
            f"before {other.get('next')}; ask for those dates again to finish it.)")
    if report.get("resumes_from"):
        add(f"An earlier run of these dates stopped; this "
            f"{'would continue' if dry else 'continued'} from {report['resumes_from']}.")
    counts = report.get("counts") or {}
    if dry:
        if report.get("complete"):
            add("These dates are already fully read in; there is nothing left to do.")
            return "\n".join(lines)
        add(f"It would read {counts.get('days_read', 0):,} day(s) in {report['tranches']} "
            f"tranche(s) of up to {report['tranche_days']} days.")
        add(f"What is there: {counts.get('conversation_days', 0):,} conversation-day(s) worth "
            f"keeping, {counts.get('person_days', 0):,} person-day(s), with "
            f"{counts.get('people', 0):,} people.")
        add(f"  {counts.get('queued', 0):,} day(s) are with {counts.get('cards', 0):,} people "
            "who already have a card: they would be queued for their summary.")
        if counts.get("held_total"):
            add(f"  {counts['held_total']:,} day(s) are with {counts.get('numbers_held', 0):,} "
                "number(s) not yet known: they would wait on your texts review list. "
                "Nothing goes on your main people queue.")
        already = counts.get("already_on_cards", 0) + counts.get("already_queued", 0)
        if already:
            add(f"  {already:,} day(s) are already on a card or already waiting.")
        est = report.get("estimate") or {}
        add(f"How long: {est.get('said')} (measured, not guessed: reading "
            f"and sorting these days took {est.get('read_s')} s in this dry run; writing "
            f"{counts.get('threads', 0):,} transcript(s) adds about {est.get('write_s')} s; "
            f"the pauses between tranches add {est.get('pause_s')} s).")
        landing = est.get("landing") or {}
        if landing.get("queued"):
            add(f"When the lines reach the cards: the {landing['queued']:,} queued day(s) land "
                f"through a summary pass ({landing['per_summary_pass']} at a time), or, once "
                f"they have waited {landing['stale_days']} day(s), with their plain line, up "
                f"to {landing['per_morning']} a morning: about {landing['mornings']} "
                "morning(s) if no summary pass runs.")
            add(f"The way back: before a card gets its first backfilled line, I keep a copy of "
                "it exactly as it was in pre-backfill/<its name>.md. A backfill rolls that "
                "card's undo history (\"undo that memory change\") past its reach, so that "
                f"copy is the way back. Up to {counts.get('cards', 0):,} card(s).")
        add(f"To run it: backfill --from {win.get('from')} --to {win.get('to')} --confirm")
        return "\n".join(lines)
    add(f"Read {report.get('done_tranches', 0)} tranche(s): {counts.get('person_days', 0):,} "
        f"person-day(s) from {counts.get('conversation_days', 0):,} conversation-day(s); "
        f"queued {counts.get('queued', 0):,} for their summary"
        + (f" ({counts['released_from_hold']:,} released from hold)"
           if counts.get("released_from_hold") else "")
        + f", held {counts.get('held_total', 0):,} for your texts review list, "
        f"wrote {counts.get('threads', 0):,} transcript(s).")
    add("Nothing went on your main people queue, and the morning run's place in your texts "
        "(its watermark) did not move.")
    if report.get("complete"):
        add("Finished: every day asked for is read in.")
    elif report.get("stopped") == "time":
        add(f"Stopped for time before {report.get('next')}; ask for the same dates again and "
            "it continues there.")
    elif report.get("next"):
        add(f"Stopped before {report['next']}; ask for the same dates again and it continues "
            "there.")
    if counts.get("queued"):
        add("Each card gets its copy in pre-backfill/ just before its first backfilled line "
            "lands.")
    return "\n".join(lines)


def cmd_backfill(args: argparse.Namespace) -> int:
    mod = _import_cp6("imbackfill")
    if mod is None:
        print("texts: backfill is not available in this copy of the plug-in.")
        return 0
    try:
        report = mod.backfill(args.date_from, args.date_to, confirm=args.confirm,
                              max_seconds=args.max_seconds)
    except Exception as exc:
        _warn(f"the backfill stopped on an unexpected error ({exc.__class__.__name__}: {exc}); "
              "the tranche it was in does not count, and the same dates again continue from "
              "the last finished tranche.")
        return 1
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(backfill_render(report))
    if report.get("error"):
        return 1
    return 1 if (report.get("paused") and args.confirm) else 0


def exclude_render(report: dict[str, Any], show_numbers: bool = False) -> str:
    """``exclude`` / ``include`` in plain words.  Numbers masked unless asked for in full."""
    lines: list[str] = []
    add = lines.append
    verb = report["verb"]
    if report.get("paused"):
        return report["paused"]["sentence"]
    match = report.get("match") or {}
    if match.get("status") != "one":
        lines.extend(_match_lines(match))
        if report.get("confirm"):
            add("Nothing was changed.")
        return "\n".join(lines)
    who = match["who"]
    shown = (lambda i: i) if show_numbers else _mask_tail
    done, confirm = report.get("done"), report.get("confirm")
    title = "Stop reading" if verb == "exclude" else "Read again"
    add(f"{title}: texts with {who['name']}"
        + ("." if done else " (a preview: nothing was changed)." if not confirm else "."))
    if report.get("refused"):
        add(f"Nothing was changed: {report['refused']}")
        return "\n".join(lines)
    changes = [shown(i) for i in report["changes"]]
    if verb == "exclude":
        if changes:
            add(("Put on" if done else "Would go on") + " your never-read list: "
                + ", ".join(changes) + ".")
        if report["unchanged"]:
            add("Already on it: " + ", ".join(shown(i) for i in report["unchanged"]) + ".")
    else:
        if changes:
            add(("Taken off" if done else "Would come off") + " your never-read list: "
                + ", ".join(changes) + ".")
        elif not report["identifiers"]:
            add("None of their numbers is on your never-read list.")
    if report["own_skipped"]:
        add("Your own number(s) are never put on that list: "
            + ", ".join(shown(i) for i in report["own_skipped"]) + ".")
    if match["who"].get("route") == "number" and verb == "exclude":
        add("Only that number: their other numbers, if any, are still read. Say their name to "
            "stop reading every number of theirs.")
    led = report["ledger"]
    if verb == "exclude" and (led["queued_days"] or led["held_days"] or led["numbers"]):
        add(("Dropped" if done else "Would drop") + f" from the texts waiting: "
            f"{led['queued_days']} day(s) waiting for their summary, {led['held_days']} held "
            f"for review; {led['numbers']} number(s) "
            + ("came" if done else "would come") + " off your texts review list."
            + (f" {led['claims_cancelled']} summary pass(es) holding their days were cancelled."
               if led.get("claims_cancelled") else ""))
    if verb == "include" and led["numbers"]:
        add(f"{led['numbers']} number(s) " + ("went" if done else "would go")
            + " back to where they stood before.")
    if done and report.get("snapshot"):
        add(f"config.local.json was backed up first ({report['snapshot']}); "
            "local_snapshot.py restore _local/imessage/config.local.json --confirm puts it "
            "back.")
    add(report["note"])
    if not confirm and report["identifiers"]:
        add(f'To do it: {verb} --person "{report["asked"]}" --confirm')
    return "\n".join(lines)


def _mask_tail(ident: str) -> str:
    """``…0142`` / ``j…@example.com``: the shape review shows, never the whole number."""
    text = str(ident or "")
    if "@" in text:
        local, _, domain = text.partition("@")
        return f"{local[:1]}…@{domain}"
    digits = re.sub(r"\D", "", text)
    return f"…{digits[-4:]}" if digits else "…"


def cmd_exclude(args: argparse.Namespace) -> int:
    """``exclude`` and ``include``: exit 1 only when a --confirm was refused."""
    mod = _import_cp6("imexclude")
    if mod is None:
        print(f"texts: {args.verb} is not available in this copy of the plug-in.")
        return 0
    action = mod.exclude if args.verb == "exclude" else mod.include
    try:
        report = action(args.person, confirm=args.confirm, show_numbers=args.show_numbers)
    except Exception as exc:
        _warn(f"{args.verb} stopped on an unexpected error ({exc.__class__.__name__}: {exc}); "
              "run it again to see where things stand.")
        return 1
    if args.json:
        public = dict(report)
        if not args.show_numbers:
            for field_name in ("identifiers", "changes", "unchanged", "own_skipped"):
                public[field_name] = [_mask_tail(i) for i in report[field_name]]
        print(json.dumps(public, indent=2, ensure_ascii=False, default=str))
    else:
        print(exclude_render(report, show_numbers=args.show_numbers))
    refused = report.get("refused") or (report.get("paused") and args.confirm)
    return 1 if (refused and args.confirm) else 0


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="imessage.py",
        description=(
            "The iMessage intake plug-in: check (what it can see, changes nothing), "
            "preview (what a run would land on cards, before it does), daily (the "
            "morning run: queue each conversation-day, file the long-waiting ones), "
            "synthesise (the summary pass: claim queued days, then commit one summary "
            "line per day), review (every held number, one ranked list, cleared in bulk), "
            "show (where you left off with someone), backfill (bring older texts in), "
            "exclude / include (stop or start reading someone's texts) and status (the "
            "feed's health)."
        ),
    )
    subs = parser.add_subparsers(dest="verb", required=True)

    check = subs.add_parser(
        "check",
        help="what this plug-in can see on this machine, in plain words",
        description=(
            "Read-only. Reports access to your message history, how much of it is "
            "readable, how many contacts were loaded, and whether your own numbers and "
            "addresses are set — and offers the ones it can find."
        ),
    )
    check.add_argument(
        "--write-own-handles",
        action="store_true",
        help=(
            "write the numbers and addresses it found into config.local.json, merged "
            "into whatever is already there. Without this flag nothing is written."
        ),
    )
    check.add_argument(
        "--write-never-ingest",
        action="append",
        default=None,
        metavar="HANDLE",
        help=(
            "add this number or address to never_ingest in config.local.json, so its "
            "texts are never read. Repeatable. Anything already on the list is kept "
            "exactly as it is; short codes need no entry, they are already left out."
        ),
    )
    check.add_argument("--json", action="store_true", help="machine-readable output")
    check.add_argument(
        "--mask",
        action="store_true",
        help="replace every number and address with its shape, so the report can be shared",
    )

    daily = subs.add_parser(
        "daily",
        help="the morning run: queue each new conversation-day, file the long-waiting ones",
        description=(
            "Reads the texts that arrived since the last run, writes each conversation's "
            "transcript, queues each person's day for its summary (or holds it until "
            "their number is known), and files any day that has waited too long with its "
            "plain line. Refuses, with one sentence and nothing written, until your own "
            "numbers are set."
        ),
    )
    daily.add_argument(
        "--dry-run",
        action="store_true",
        help="read and report what would happen; write nothing, raise nothing",
    )
    daily.add_argument("--json", action="store_true", help="machine-readable output")

    status = subs.add_parser(
        "status",
        help="the feed's health: watermark, queue, held days, every day that would not file",
        description="Reads only. Changes nothing.",
    )
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.add_argument(
        "--mask",
        action="store_true",
        help="replace every number and address with its shape, so the report can be shared",
    )

    synthesise = subs.add_parser(
        "synthesise",
        help="the summary pass: claim queued days for their summary, or commit the summaries",
        description=(
            "Without --commit: claims up to --limit queued days (default: synth_claim_limit), "
            "writes a private claim file listing each day's transcripts, and prints "
            "CLAIM_PATH: <path> last. A writer puts one summary per day into that file. "
            "With --commit PATH: checks every summary (one line, at most 400 characters, no "
            "em dash, more than the plain line) and files the good ones onto the cards; "
            "the rest go back to the queue with the reason. Refuses, with one sentence and "
            "nothing written, until your own numbers are set."
        ),
    )
    mode = synthesise.add_mutually_exclusive_group()
    mode.add_argument(
        "--limit", type=_positive_int, default=None, metavar="N",
        help="claim at most N days (default: synth_claim_limit in the config)",
    )
    mode.add_argument(
        "--commit", default=None, metavar="PATH",
        help="commit the summaries written into this claim file",
    )
    synthesise.add_argument("--json", action="store_true", help="machine-readable output")

    review = subs.add_parser(
        "review",
        help="every number your texts are holding, one ranked list; accept or dismiss in bulk",
        description=(
            "Without a selection: the list, read-only, ranked by days held, each number shown "
            "by its name and last four digits, with a code for the list. With --accept / "
            "--dismiss / --dismiss-below: what each row WOULD do, still read-only. Add "
            "--listing <code> --confirm to do it; a list that changed since you read it is "
            "refused, never shifted. Accepting a new person takes two yeses: the engine "
            "makes the card without the number, so they come back at the top for the second."
        ),
    )
    review.add_argument("--accept", default=None, metavar="ROWS",
                        help="rows to say yes to, like 1-20,25 (m1 confirms a match to check)")
    review.add_argument("--dismiss", default=None, metavar="ROWS",
                        help="rows to say no to, like 30-40 (m1 says a match is someone else)")
    review.add_argument("--dismiss-below", type=_row_number, default=None, metavar="N",
                        help="say no to every numbered row after row N")
    review.add_argument("--name", action="append", default=None, metavar="ROW=NAME",
                        help="the name for a new person from a number with no name; repeatable")
    review.add_argument("--listing", default=None, metavar="CODE",
                        help="the code printed with the list you read (needed to act)")
    review.add_argument("--confirm", action="store_true",
                        help="do it; without this nothing is written")
    review.add_argument("--show-numbers", action="store_true",
                        help="print numbers and addresses in full")
    review.add_argument("--json", action="store_true", help="machine-readable output")

    preview = subs.add_parser(
        "preview",
        help="what a run over a window WOULD land on people's cards, before it does",
        description=(
            "Dry by default: reads your texts over a window, groups them into "
            "conversation-days, works out how each person's card would be written, and "
            "reports it. It never touches a person's card, a proposal or the memory "
            "database. Only --write puts anything on disk, and only the transcripts."
        ),
    )
    preview.add_argument(
        "--from",
        dest="date_from",
        required=True,
        metavar="YYYY-MM-DD",
        help="the first day of the window, included",
    )
    preview.add_argument(
        "--to",
        dest="date_to",
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "the last day of the window, included. Defaults to yesterday, because "
            "today is not over yet."
        ),
    )
    preview.add_argument(
        "--write",
        action="store_true",
        help=(
            "actually write the conversation transcripts into the plug-in's own "
            "threads folder. Without this flag nothing is written at all."
        ),
    )
    preview.add_argument(
        "--assume-own-handle",
        action="append",
        default=None,
        metavar="HANDLE",
        help=(
            "treat this number or address as one of yours, for this preview only. "
            "Repeatable. Nothing is saved, and it is labelled as an assumption in the "
            "output. Without it, and without your own settings file, preview refuses."
        ),
    )
    preview.add_argument("--json", action="store_true", help="machine-readable output")
    preview.add_argument(
        "--mask",
        action="store_true",
        help=(
            "replace every number, address and quoted message with its shape, so the "
            "report can be shared"
        ),
    )

    show = subs.add_parser(
        "show",
        help="where you left off with someone: their text days, newest first, with the texts",
        description=(
            "Reads only. Finds the person from your words (a name, a number, or a card id; "
            "a name that fits more than one person lists them and stops), then lists their "
            "days of texts newest first, each with the end of its stored conversation, plus "
            "any day not on their card yet. Bounded, and it says what it left out. With "
            "--day instead: everyone your texts hold for that day."
        ),
    )
    who_or_when = show.add_mutually_exclusive_group(required=True)
    who_or_when.add_argument("--person", metavar="NAME|NUMBER|prs_ID",
                             help="who: their name, a number or address, or their card id")
    who_or_when.add_argument("--day", metavar="YYYY-MM-DD",
                             help="instead: everyone your texts hold for that day "
                                  "(\"who texted me yesterday\")")
    show.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                      help="only days from this one on")
    show.add_argument("--limit", type=_positive_int, default=None, metavar="N",
                      help="how many days to show with their texts (default 5)")
    show.add_argument("--today", action="store_true",
                      help="also read today's texts with them, live (nothing is stored)")
    show.add_argument("--show-numbers", action="store_true",
                      help="print numbers and addresses in full")
    show.add_argument("--json", action="store_true", help="machine-readable output")

    backfill = subs.add_parser(
        "backfill",
        help="bring older texts in: a dry run first, then --confirm; resumable",
        description=(
            "Dry by default: reads the window and says how many conversation-days and "
            "people it holds and how long a real run takes, measured from that read. With "
            "--confirm: the morning run's pipeline over those days, a tranche at a time. It "
            "never puts anyone on your main people queue and never moves the morning run's "
            "place in your texts. Killed or stopped, the same dates again continue it."
        ),
    )
    backfill.add_argument("--from", dest="date_from", required=True, metavar="YYYY-MM-DD",
                          help="the first day, included")
    backfill.add_argument("--to", dest="date_to", required=True, metavar="YYYY-MM-DD",
                          help="the last day, included (yesterday at the latest)")
    backfill.add_argument("--confirm", action="store_true",
                          help="do it; without this nothing is written")
    backfill.add_argument("--max-seconds", type=float, default=None, metavar="S",
                          help="stop cleanly between tranches after about this long")
    backfill.add_argument("--json", action="store_true", help="machine-readable output")

    for verb, words in (
        ("exclude", "stop reading someone's texts: their numbers go on your never-read list"),
        ("include", "read someone's texts again: their numbers come off your never-read list"),
    ):
        sub = subs.add_parser(
            verb, help=words,
            description=(
                f"{words[0].upper()}{words[1:]}. A preview unless --confirm; with it, "
                "config.local.json is backed up first. A name covers every number and "
                "address on their card and in Contacts; a number covers only that number."
            ),
        )
        sub.add_argument("--person", required=True, metavar="NAME|NUMBER|prs_ID",
                         help="who: their name, a number or address, or their card id")
        sub.add_argument("--confirm", action="store_true",
                         help="do it; without this nothing is written")
        sub.add_argument("--show-numbers", action="store_true",
                         help="print numbers and addresses in full")
        sub.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def cmd_check(args: argparse.Namespace) -> int:
    report = gather(mask=args.mask)
    exit_code = 0

    if args.write_own_handles:
        own = report["own_handles"]
        handles = report["_candidate_handles"]
        if own["state"] == "set":
            report["write"]["error"] = (
                "your own numbers and addresses are already set, and I will not overwrite "
                "a list you have curated. Edit the file by hand to change it."
            )
        elif not handles:
            report["write"]["error"] = (
                "there was nothing to write: I found no candidate, and I will not invent one."
            )
        else:
            try:
                written, _merged = write_local_config(handles)
                report["write"] = {
                    "performed": True,
                    "target": str(written),
                    "own_handles": [_show(h, args.mask) for h in handles],
                }
                report["files"]["local_config_exists"] = True
            except (LocalConfigWriteError, OSError, TypeError, ValueError) as exc:
                report["write"]["error"] = str(exc)
                exit_code = 1

    if args.write_never_ingest:
        try:
            written, merged, added = write_never_ingest(args.write_never_ingest)
            report["never_ingest"] = _never_ingest_section(
                imconfig._deep_merge(imconfig.load_config(), merged), args.mask
            )
            report["never_ingest"]["write"] = {
                "performed": bool(added),
                "target": str(written),
                "added": [_show(h, args.mask) for h in added],
            }
            report["files"]["local_config_exists"] = written.is_file()
        except (LocalConfigWriteError, OSError, TypeError, ValueError) as exc:
            report["never_ingest"]["write"] = {"performed": False, "error": str(exc)}
            exit_code = 1

    report.pop("_candidate_handles", None)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render(report))
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    if args.verb == "check":
        return cmd_check(args)
    if args.verb == "preview":
        return cmd_preview(args)
    if args.verb == "daily":
        return cmd_daily(args)
    if args.verb == "status":
        return cmd_status(args)
    if args.verb == "synthesise":
        return cmd_synthesise(args)
    if args.verb == "review":
        return cmd_review(args)
    if args.verb == "show":
        return cmd_show(args)
    if args.verb == "backfill":
        return cmd_backfill(args)
    if args.verb in ("exclude", "include"):
        return cmd_exclude(args)
    # argparse's required=True makes this unreachable; it is here so a future verb
    # added to the parser and not to this dispatch fails loudly rather than silently.
    raise SystemExit(f"unknown verb: {args.verb!r}")


if __name__ == "__main__":
    raise SystemExit(main())
