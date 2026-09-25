"""iMessage intake — settings, paths, and the import-path order.

This module is the ONE place the plug-in learns where things live and what it is
configured to do.  Every other module in ``_local/imessage/`` imports it.
Nothing here reaches the network, nothing here reads the Messages database, and
nothing here holds or reads a credential: this plug-in has no ``.env`` and needs
none.  Standard library only, so it imports cleanly even on a machine where the
engine's virtualenv is not the interpreter in play.

Why this module is named ``imconfig`` — and must never be renamed
-----------------------------------------------------------------
``_local/imessage/`` is ``sys.path[0]`` whenever a file in this folder runs as a
script, and :func:`ensure_engine_path` below puts the engine's
``.claude/scripts/`` on ``sys.path`` as well.  A module named ``config.py``
sitting HERE would therefore shadow the engine's ``config`` for the whole
process — every importer, including engine code pulled in later, would silently
get this file instead of the engine's timezone, paths and clock.  Naming it
``imconfig`` is what keeps the two apart.  Do not rename it to ``config``; the
shadowing is silent and would look like an engine bug.  The same rule binds
every module added to this plug-in: before naming one, check it against
``ls .claude/scripts/*.py``.

Why every path resolves from ``__file__`` and never ``Path.cwd()``
------------------------------------------------------------------
This code is entered from three different working directories:

* the morning stage launches the adapter with cwd = ``_local/imessage/``;
* a member CLI is invoked from the Brain root;
* ``uv run --directory .claude/scripts`` sets cwd = ``.claude/scripts``.

``Path.cwd()`` is a different answer in each one, so a cwd-relative path would
read the ledger in one entry point and write it in another — the failure mode is
silent duplicate ingestion, not a crash.  ``__file__`` is the same answer in all
three.  Every path constant below is derived from :data:`HOME` for that reason.

The sys.path ORDER — engine first, then this folder back on top
----------------------------------------------------------------
:func:`ensure_engine_path` does two things, in this order, and the order is the
whole point:

1. it inserts ``.claude/scripts`` at index 0 so the engine's modules (``config``,
   ``people_resolve``, ``shared``, …) are importable at all;
2. it then RE-ASSERTS this folder at index 0, so a module in this plug-in always
   beats an engine module of the same name.

Step 2 exists because step 1 alone is a live bug, found in a sibling plug-in
whose config module inserts the engine's scripts folder at index 0 and stops
there, which leaves the ENGINE ahead of the plug-in on ``sys.path``.  That
plug-in has its own ``preflight.py`` and so does the engine, so the wrong one
wins.  Reproduced on 2026-09-20 (``<sibling>`` stands for that plug-in)::

    $ uv run --directory .claude/scripts python -c "
    import sys
    sys.path.insert(0, '.../_local/<sibling>')
    import <sibling>config
    print(sys.path[0]); import preflight; print(preflight.__file__)"
    <your Glitch folder>/.claude/scripts
    <your Glitch folder>/.claude/scripts/preflight.py

That is the engine's ``preflight``, not the plug-in's.  Nothing raises; the plug-in
just quietly runs someone else's code.  This module refuses to inherit that, and
the re-assert is NOT redundant tidy-up — deleting it reintroduces the bug in a
form that shows up as a mystified bug report months later.  Keep both steps.

``ensure_engine_path`` is idempotent and is called once at import time, so simply
importing ``imconfig`` is enough to make the ordering true for the process.  It
never raises: a missing engine folder costs one stderr line and the plug-in
carries on, because the parts of this plug-in that do not touch the engine
(reading config, validating handles) should still work on a half-set-up machine.

``config.json`` keys (JSON cannot carry comments, so they are documented here)
------------------------------------------------------------------------------
``config.json`` is the file a member opens, so every key it can hold is explained
below in plain words, including what changing it would do.

``substance_min_turns``
    The fewest TURNS a conversation-day must contain before it is worth recording
    at all — turns, not messages: six rounds of "ok" is one person talking and
    still fails, while "Can we move to 4pm?" / "Works for me" is two turns from
    two people and passes, which is exactly the truth worth keeping.  Below the
    floor it is noise and is dropped rather than stamped onto a person's card.
    Raise it to be stricter; set it to 1 and every stray text becomes an
    interaction.
``substance_min_senders``
    How many distinct people must have actually spoken.  At the default of 2 both
    sides must have said something, so a message you sent that was never answered
    is not logged as an interaction.  Lower it to 1 and unanswered outbound texts
    start landing on cards too.
``substance_min_chars``
    The fewest characters of combined text a conversation-day must carry.  It
    catches the case ``substance_min_turns`` misses: several messages that are all
    reactions or single emoji.  ANDed with the turn and sender floors, so all
    three must pass.

    **Default 20, set by the member on 2026-09-20.**  The original design
    specified 80, which is self-contradicting: its own worked example of "exactly
    the truth worth keeping" — *"Can we move to 4pm?" / "Works for me"* — is 31
    characters and an 80-char floor throws it away.  Measured over a year of one
    member's texts, an 80-char floor dropped **about 7% of the chat-days a
    20-char floor keeps**, most weeks several real contacts.
    Those are the "can you grab the kids / yep" days: thin to read, but real
    contact, and ``last_contacted`` on a person's card is wrong without them.
    The member chose the complete contact history.  Raise it to filter harder; the
    trade is cleaner cards against days of genuine contact going unrecorded.
``max_stamps_per_run``
    A ceiling on how many interactions one run may write to the people directory.
    It is both a pacing bound and a blast-radius limit: the morning run has a
    hard time budget and is SIGKILLed when it overruns, so a run stops at this
    many stamps and **carries the remainder to the next run** rather than being
    cut off mid-write; and a misconfigured first pass over years of history stops
    and says so instead of rewriting every card in one go.  Nothing is lost when
    it trips — the untouched days are still ahead of the watermark.  Raise it
    deliberately, for a backfill you are watching.
``budget_margin_s``
    Seconds of headroom the run keeps in hand before its time budget expires.
    Work stops this long before the deadline so the ledger and state file are
    written whole instead of being cut off mid-write.  Raise it on a slow disk;
    lowering it buys a little more work per run and risks an unfinished one.
``backfill_tranche_days``
    How many days of history one backfill pass takes at a time.  Backfill is
    deliberately incremental so it can be stopped, inspected and resumed; this is
    the size of each bite.  Raise it to catch up faster in fewer passes, at the
    cost of a longer, heavier single run.
``synth_stale_days``
    How many days a text day may wait for its written summary before it is filed
    anyway, with its plain one-line opener instead.  The summaries are written
    later, in a session, because the morning run can never call a model; this is
    the safety net for the days that session never comes.  Past this age the day
    lands on the person's card with the plain line, so **a day is never lost to a
    summary backlog**.  Default 3.  Raise it to give the summaries longer to
    catch up (more days waiting unrecorded in the meantime); lower it and more
    days land with the plain line rather than a real summary.
``synth_claim_limit``
    How many conversation-days one summary pass takes at a time.  Each pass
    reads those days' transcripts and writes one summary for each, so this is
    the size of one bite of the backlog.  Default 15, about a day's texting.
    Raise it to clear a backlog in fewer passes, at the cost of a longer, heavier
    single pass; lower it for smaller passes that are quicker to check.
``never_ingest``
    Handles — phone numbers or addresses — whose conversations are never read at
    all.  PRIVATE: it belongs in ``config.local.json``, not here.  See below.
``own_handles``
    The member's OWN phone numbers and addresses, so their own side of a thread
    is recognised as theirs.  PRIVATE: it belongs in ``config.local.json``, not
    here.  See below, and see :func:`require_own_handles` — without it the
    plug-in refuses to run.

The two private keys live in ``config.local.json``
---------------------------------------------------
``never_ingest`` and ``own_handles`` are personal data: the people a member has
chosen not to record, and the member's own numbers.  Neither belongs in a file
that travels.  They live in ``config.local.json``, which ``.gitignore`` already
excludes and which backup leaves alone; ``config.example.json`` shows its shape.
``config.json`` carries ONLY the eight shipped, non-personal knobs above.

Because :func:`load_config` merges ``config.local.json`` last, a member may also
override any of the eight knobs there if they want a local-only setting — but the
two private keys should never travel back the other way into ``config.json``.

Why an empty ``own_handles`` is a REFUSAL, not a warning
----------------------------------------------------------
The engine's owner filter is email-only.  ``meeting_scaffold.py:263`` reads
``if a.email and a.email.lower() in owner_emails: continue`` — a participant with
no email address is never recognised as the member, and an iMessage handle is a
phone number.  Meanwhile the member's own card (``glitch-mem/Memory/people/<you>.md``)
usually exists, carries only ``emails:`` in its front matter, and already holds a
list of interaction lines.
Put those together and an empty ``own_handles`` means the member's own outbound
texts are treated as someone else's turns and stamped onto the member's own card
— a person's history quietly filled with their own voice, which is exactly the
drift the people spine exists to prevent.  So :func:`require_own_handles` makes
``daily`` and ``backfill`` refuse to start, with one plain sentence naming the
fix.  It is a refusal rather than an assertion because the member should read a
sentence and act on it, not read a traceback.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths — all from __file__, never Path.cwd(); see the module docstring for why.
# ---------------------------------------------------------------------------

#: This plug-in's own folder, ``_local/imessage/``.
HOME: Path = Path(__file__).resolve().parent

#: Per-conversation working files. Private data; gitignored.
THREADS_DIR: Path = HOME / "threads"

#: Where the last run got to (cursors, watermarks). Private data; gitignored.
STATE_PATH: Path = HOME / "state.json"

#: What has already been stamped, so a re-run cannot double-write. Gitignored.
LEDGER_PATH: Path = HOME / "ledger.json"

#: The ledger's write-ahead companion, for a crash mid-write. Gitignored.
LEDGER_WAL: Path = HOME / "ledger.wal"

#: The shipped, non-personal knobs. Committed.
CONFIG_PATH: Path = HOME / "config.json"

#: The member's own private overrides, including the two private keys. Gitignored.
LOCAL_CONFIG_PATH: Path = HOME / "config.local.json"

# HOME.parents[0] is `_local/`, HOME.parents[1] is the Brain root. Verified on
# disk: _local/imessage -> _local -> the repo root that holds .claude/.
BRAIN_ROOT: Path = HOME.parents[1]

#: The engine's script folder — the only engine path this plug-in needs.
SCRIPTS_DIR: Path = BRAIN_ROOT / ".claude" / "scripts"

# ---------------------------------------------------------------------------
# Defaults — the shipped shape of config.json. No secrets, ever.
# Every key is explained in plain words in the module docstring above; JSON
# cannot carry comments, so that docstring is the member-facing reference.
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "substance_min_turns": 2,
    "substance_min_senders": 2,
    "substance_min_chars": 20,
    "max_stamps_per_run": 200,
    "budget_margin_s": 3.0,
    "backfill_tranche_days": 30,
    "synth_stale_days": 3,
    "synth_claim_limit": 15,
    # The two private keys. They ship EMPTY and are meant to be set in
    # config.local.json, which never leaves this machine. See the docstring.
    "never_ingest": [],
    "own_handles": [],
}

#: The one sentence a caller prints when :func:`require_own_handles` says no.
#: One sentence, plain words, and it names the fix — a member should be able to
#: act on it without reading any code.
REFUSAL_NO_OWN_HANDLES: str = (
    "texts: I don't know which numbers are yours yet, so I won't run — run "
    "`imessage.py check` and it will find them for you to confirm into "
    "config.local.json."
)

# Said at most once per process, so a missing engine folder costs one line, not
# one line per call.
_warned_missing_scripts = False


def _warn(message: str) -> None:
    """One plain line to stderr. Degrading loudly beats wedging the tool."""
    print(f"[imessage] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# The import path. Read the module docstring's sys.path section before editing.
# ---------------------------------------------------------------------------


def ensure_engine_path() -> bool:
    """Make the engine importable, then put THIS folder back in front.

    Two steps, and the order is load-bearing (see the module docstring):

    1. ``.claude/scripts`` goes on ``sys.path`` at index 0, but only if it is not
       already there and the folder really exists on disk;
    2. :data:`HOME` is then re-asserted at index 0, so a module in this plug-in
       always wins a name collision with an engine module of the same name.

    Step 2 is what the sibling plug-in is missing, and dropping it silently
    routes ``import <name>`` to the engine's copy.  Do not "tidy" it away.

    Idempotent: calling it repeatedly leaves ``sys.path`` in the same shape, with
    no duplicate entries for either folder.  Never raises — a missing or
    unreadable engine folder emits ONE stderr line for the process and returns,
    because the parts of this plug-in that do not touch the engine should still
    work on a half-set-up machine.

    Returns ``True`` if this call added the engine folder, ``False`` otherwise
    (already present, or not usable).  The return value is for a diagnostic; no
    caller needs to branch on it.
    """
    global _warned_missing_scripts

    scripts = str(SCRIPTS_DIR)
    added = False

    try:
        usable = SCRIPTS_DIR.is_dir()
    except OSError as exc:  # an unreadable mount, a permissions wall
        usable = False
        if not _warned_missing_scripts:
            _warned_missing_scripts = True
            _warn(
                f"could not check the engine scripts folder at {SCRIPTS_DIR} "
                f"({exc.__class__.__name__}); engine modules will not import."
            )

    if usable:
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
            added = True
    elif not _warned_missing_scripts:
        _warned_missing_scripts = True
        _warn(
            f"engine scripts folder not found at {SCRIPTS_DIR}; engine modules "
            "will not import. Everything here that does not need the engine "
            "still works."
        )

    # STEP 2 — always, and always after step 1. This is the line that stops an
    # engine module shadowing one of ours. Removing every existing copy first
    # keeps the call idempotent and de-duplicates a path added twice elsewhere.
    home = str(HOME)
    while home in sys.path:
        sys.path.remove(home)
    sys.path.insert(0, home)

    return added


# Called once, at import, so `import imconfig` is all any module in this
# plug-in has to do to get the ordering right for the whole process.
ensure_engine_path()


# ---------------------------------------------------------------------------
# Configuration.
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Return ``base`` with ``over`` laid on top; nested dicts merge key-by-key.

    Lists and scalars REPLACE wholesale — an ``own_handles`` in
    ``config.local.json`` is the whole list, not an addition to the default one,
    which is the only behaviour that lets a member remove a handle.  A partial
    file therefore only overrides what it actually names.  The result is a fresh
    deep copy, so neither input can be reached through it afterwards.
    """
    merged = copy.deepcopy(base)
    for key, value in over.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _read_json_object(path: Path) -> dict[str, Any] | None:
    """The file's top-level JSON object, or ``None`` with at most one warning.

    A missing file is the shipped state, not an error, and returns ``None``
    silently.  Anything else that is wrong — unreadable, not valid JSON, or a
    JSON value that is not an object — costs ONE plain stderr line naming the
    file, and then returns ``None`` so the caller falls back to what it already
    had.  Never raises: a fat-fingered config file must not wedge the tool.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _warn(f"could not read {path.name} ({exc.__class__.__name__}); skipping it.")
        return None

    try:
        loaded = json.loads(text)
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        _warn(f"{path.name} is not valid JSON ({exc}); skipping it.")
        return None

    if not isinstance(loaded, dict):
        _warn(
            f"{path.name} must hold a JSON object, not {type(loaded).__name__}; "
            "skipping it."
        )
        return None

    return loaded


def load_config(
    path: Path | None = None,
    local_path: Path | None = None,
) -> dict[str, Any]:
    """The merged settings: :data:`DEFAULTS` ← ``config.json`` ← ``config.local.json``.

    Last wins, so the member's private local file beats the shipped file, which
    beats the in-module defaults.  The merge is deep: nested objects merge
    key-by-key, while lists and scalars replace wholesale (see
    :func:`_deep_merge`).

    This NEVER raises.  A missing file is skipped silently; a corrupt one, or one
    whose top level is not an object, costs one stderr line naming the file and
    is then skipped, so the run continues on the layers that were sound.  That
    matters more here than elsewhere: this module is imported by every other
    module in the plug-in, so raising would take the whole tool down over a
    stray comma.

    The returned dict is always a FRESH deep copy.  A caller may mutate it
    freely — normalise a handle, drop a key — without poisoning :data:`DEFAULTS`
    or the next caller's copy.

    ``path`` and ``local_path`` default to :data:`CONFIG_PATH` and
    :data:`LOCAL_CONFIG_PATH`.  They are overridable so a check or a test can
    point at a copy without going anywhere near the member's real files.
    """
    layers = (
        CONFIG_PATH if path is None else Path(path),
        LOCAL_CONFIG_PATH if local_path is None else Path(local_path),
    )

    merged: dict[str, Any] = copy.deepcopy(DEFAULTS)
    for layer_path in layers:
        layer = _read_json_object(layer_path)
        if layer is not None:
            merged = _deep_merge(merged, layer)
    return merged


# ---------------------------------------------------------------------------
# The refusal. Read the module docstring's last section before relaxing this.
# ---------------------------------------------------------------------------


def require_own_handles(cfg: dict[str, Any] | None = None) -> list[str] | None:
    """The member's own handles, or ``None`` meaning REFUSE TO RUN.

    Returns a fresh list of stripped, non-empty handle strings when
    ``cfg["own_handles"]`` is sound.  Returns ``None`` when it is missing, not a
    list, empty, or holds anything that is not a non-empty string — a malformed
    entry is refused rather than skipped, because a handle that silently fails to
    match is a handle whose texts get attributed to the wrong person.

    A ``None`` means ``daily`` and ``backfill`` MUST NOT START.  The caller
    prints :data:`REFUSAL_NO_OWN_HANDLES` and exits; it does not warn and carry
    on, and it does not guess.  Why it is a refusal rather than a warning: the
    engine's owner filter is email-only (``meeting_scaffold.py:263``) and an
    iMessage handle is a phone number, while the member's own card
    (``people/<you>.md``) usually exists and already carries interaction lines —
    so with no ``own_handles`` the member's
    own outbound texts would be stamped onto the member's own card as if someone
    else had said them.  Full reasoning in the module docstring.

    ``cfg`` defaults to a fresh :func:`load_config`.  Never raises.
    """
    if cfg is None:
        cfg = load_config()

    raw = cfg.get("own_handles")
    if not isinstance(raw, list) or not raw:
        return None

    handles: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            return None
        handles.append(entry.strip())
    return handles
