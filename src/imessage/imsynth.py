"""iMessage intake — the summary seam: claim queued days, take back one written line for each.

Why this module exists
----------------------
The morning run (:mod:`imrun`) can never call a model: the stage gives it about twenty
seconds, kills it with a SIGKILL when it overruns, and has no API key to call one
with (the plug-in's architectural wall).  So that run only QUEUES each
``(person, day)``.  This module is the other half, run in an ordinary session: a
sub-agent claims a batch of queued days, reads their stored transcripts, writes ONE
summary per day into the claim file, and :func:`commit` stamps each onto the person's
card through :func:`imrun.land`, the one path a card is ever written by.

It is the engine's ``digest.py`` shape: claim (Python) → the model writes → commit
(Python), with a claim file PER RUN so two drains can never overwrite each other's
work, and the model never touching a card or the ledger itself.

The claim
---------
:func:`claim` takes up to ``n`` queued days (default ``synth_claim_limit``) that no
live claim holds, oldest day first, leases them in the ledger for
:data:`LEASE_HOURS` (a second claim skips them, and the stale fallback holds back
from them), and writes ``synth/claim-<run_id>.json`` under the plug-in home:
serialised first, a temp file beside it, fsynced, owner-only, renamed into place, and
refused outright if the target resolves anywhere but directly inside ``synth/``.  Per
day the file carries a short id (``u01``), the ledger key, the person's display name,
the day, the direction, the ABSOLUTE paths of that day's transcripts, the links they
came from, and the mechanical topic (the line the day gets anyway, so the writer
knows what not to repeat).  It also carries :data:`DRAIN_INSTRUCTIONS` and an empty
``summaries`` map for the writer to fill.

A day whose transcripts are not on this machine is not claimed: nobody could
summarise it faithfully, and the stale fallback lands it with its plain line.

The commit, fail closed
-----------------------
:func:`commit` trusts the claim file for exactly two things: which day an id names
(cross-checked against the ledger's own record of the claim) and the summary text.
The line that lands is built from the LEDGER's queued record, never the file's copy.
Each summary must pass :func:`validate_summary`; a day is landed only while it is
still queued, still held by THIS claim (not stamped by the stale fallback, not
re-claimed by another pass after this lease ran out), and its transcripts are the
ones the writer was given.  Everything else goes back to the queue (or is reported
as no longer this claim's), with its reason.  The claim is then released, the
database committed (G1: the projection happens inside the transaction), the ledger
run committed and compacted, and only then is the claim file removed.  Committing
the same file twice lands nothing the second time: every day in it is on its card or
back in the queue.

Crash safety
------------
The same write-ahead discipline as :mod:`imrun`: :func:`imrun.land` writes the
ledger intent before the card and ``done`` after, and a commit starts by settling any
intent a killed commit left open (:func:`imrun.recover`), so a SIGKILL at any point
leaves exactly one line per day once the next commit or daily run has run.

What it never does
------------------
It never calls a model, never reads ``chat.db``, never writes a card except through
:func:`imrun.land`, and never writes outside the plug-in home except the cards that
land (and their engine lock and undo-ring copies).
"""

from __future__ import annotations

# imconfig FIRST, before any engine module: it puts `.claude/scripts` on sys.path and
# then re-asserts this folder ahead of it. See imconfig's docstring.
import imconfig

imconfig.ensure_engine_path()

import contextlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import unicodedata  # noqa: E402
from collections.abc import Iterable, Mapping  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import imcontacts  # noqa: E402
import imledger  # noqa: E402
import imrun  # noqa: E402
import imspine  # noqa: E402
import imthreads  # noqa: E402

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: The folder under the plug-in home that holds claim files.  Gitignored and
#: kept-local (``.gitignore`` carries ``synth/``): a claim file quotes names and days.
SYNTH_DIRNAME: str = "synth"

#: How long a claim holds its days.  Long enough for any drain (minutes, in practice);
#: short enough that a session that died mid-drain costs a day's delay, not a loss.
LEASE_HOURS: int = 24

#: The longest summary a card line may carry.
MAX_SUMMARY_CHARS: int = 400

#: How long a claim or commit waits for the ledger lock.  A session verb, so longer
#: than the morning run's two seconds; still short enough to say "busy" promptly.
LOCK_TIMEOUT_S: float = 5.0

#: The format and kind written into every claim file; a file without them is refused.
CLAIM_FORMAT: int = 1
CLAIM_KIND: str = "imessage-summary-claim"

#: The em dash.  The member reads it as AI-written, so no summary may carry one.
EM_DASH: str = "\u2014"

#: How a transcript names the MEMBER on each of their own lines:``HH:MM  Me: \u2026``, as
#: ``imthreads.render_thread`` writes it.  NOT ``imthreads.OWNER`` (``"(me)"``), which is
#: only the grouper's internal speaker key and never appears in a file the writer reads.
#: The instructions name this label so a writer never credits the member's words to the
#: other person; ``tests/test_imsynth.py`` reads it back off a rendered transcript.
MEMBER_LABEL: str = "Me"

_RUN_ID_RE = re.compile(r"^synth-\d{8}T\d{6}-[0-9a-f]{8}$")
_CLAIM_NAME_RE = re.compile(r"^claim-(synth-\d{8}T\d{6}-[0-9a-f]{8})\.json$")
_UNIT_ID_RE = re.compile(r"^u\d{2,}$")

#: ``people_index._LINK_RE`` starts at ``(`` + optional space + ``→``.
_ARROW_OPEN = re.compile(r"\(\s*→")
#: ``people_index._MTG_RE`` reads ``[mtg:…]`` as a meeting link.
_MTG_OPEN = "[mtg:"

#: Every character ``str.splitlines`` (which the projector uses) breaks a line on.
_LINE_BREAK_CHARS: frozenset[str] = frozenset(
    "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
)
#: Bidirectional overrides: they make a line on a card read differently from its bytes.
_BIDI_CONTROLS: frozenset[str] = frozenset(
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)

#: The mechanical topic's own wording (``imthreads.topic_for``), as word tokens.
_TOPIC_PREFIX_WORDS = re.compile(r"^(?:group texts(?: in)?|texts)(?: \d+)?(?: messages?)? ?")

#: The largest claim file :func:`commit` will read.  Fifteen days of summaries is a
#: few kilobytes; anything near this size is not a file this module wrote.
_MAX_CLAIM_BYTES: int = 4_000_000

DRAIN_INSTRUCTIONS: str = (
    "You are writing the one line that will sit on a person's card for one day of texts "
    "with them.\n"
    "\n"
    "For each entry in \"units\": read every file listed in its \"threads\" (that day's "
    f"stored conversations, one message per line; lines spoken by \"{MEMBER_LABEL}\" are "
    "the member's). \"name\" is the "
    "person whose card the line lands on. Then put ONE summary for that unit into the "
    "\"summaries\" map, keyed by the unit's \"id\" (for example \"u01\"), and save this "
    "file. Change nothing else in it.\n"
    "\n"
    "Each summary must be:\n"
    "- one line of plain, factual English about what was discussed, decided, planned or "
    "asked that day;\n"
    f"- at most {MAX_SUMMARY_CHARS} characters;\n"
    "- only what the transcripts say: never invent a fact, a time, a place or a person;\n"
    "- free of quotations longer than a few words;\n"
    "- free of em dashes (the long dash, U+2014), because the member reads them as "
    "AI-written: use a comma, a full stop or a colon instead;\n"
    "- free of the characters \"(→\" and of any line break;\n"
    "- written with people named as the transcripts name them; in a group conversation, "
    "credit each point to whoever the transcript shows said it, never to this person by "
    "default;\n"
    "- more than the unit's \"mechanical_topic\": that line is what the day gets anyway, "
    "so do not repeat it.\n"
    "\n"
    "If a day held only logistics, say the logistics in a few words. If you cannot write "
    "a faithful summary for a unit, leave it out: that day goes back to the queue and "
    "nothing is lost."
)

SENTENCE_BUSY_CLAIM: str = (
    "texts: another texts run is using the ledger right now, so I claimed nothing; try "
    "again in a minute."
)
SENTENCE_BUSY_COMMIT: str = (
    "texts: another texts run is using the ledger right now, so I filed nothing and left "
    "the claim file as it is; run the same commit again in a minute."
)
SENTENCE_MEMORY_BUSY: str = (
    "texts: the memory database was busy for longer than I can wait, so I undid this "
    "commit and left the claim file as it is; run the same commit again in a minute."
)


def _warn(message: str) -> None:
    """One diagnostic line on stderr.  Stdout belongs to the report."""
    print(f"[imessage] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# The summary rules.
# ---------------------------------------------------------------------------


def _words(text: str) -> str:
    """``text`` as lower-case word tokens joined by single spaces, for comparing."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.findall(r"\w+", folded))


def _contains(haystack: str, needle: str) -> bool:
    """``needle``'s words appear, whole and in order, inside ``haystack``'s."""
    return bool(needle) and f" {needle} " in f" {haystack} "


def is_trivial_restatement(summary: str, mechanical_topic: str | None) -> bool:
    """True when ``summary`` says nothing the mechanical line does not already say.

    Compared as words (case, punctuation and the trailing ellipsis ignored), with
    the ``Texts (N):`` / ``Group texts in … (N messages)`` wording stripped from the
    summary's front: a summary that IS the line, IS its opener, or is a run of the
    line's own words (a truncated opener, the group's name) is the mechanical line
    in other clothes, and the day already gets that one for free.
    """
    said = _words(summary)
    core = _TOPIC_PREFIX_WORDS.sub("", said).strip()
    if not core:
        return True
    # The opener is part of the line, so "a run of the line's words" covers it too.
    topic = _words(mechanical_topic or "")
    return any(_contains(topic, candidate) for candidate in (said, core))


def validate_summary(text: Any, mechanical_topic: str | None) -> str | None:
    """The reason ``text`` may not land on a card, or ``None`` when it may.

    Fail closed, in this order: not text · a line break of any kind (the projector
    splits lines with ``str.splitlines``, so a Unicode separator splits a card line
    as surely as a newline) · a control character · empty · longer than
    :data:`MAX_SUMMARY_CHARS` · an em dash · ``(→`` (the card would read the rest as
    its link) · ``[mtg:`` (the card would read a meeting link) · a trivial
    restatement of the mechanical topic.  Reasons are plain words, and never quote
    the summary back.
    """
    if not isinstance(text, str):
        return "the summary is not text"
    if any(ch in _LINE_BREAK_CHARS for ch in text):
        return "the summary runs over more than one line; it must be one line"
    for ch in text:
        if ch in _BIDI_CONTROLS or (unicodedata.category(ch) == "Cc"):
            return f"the summary holds a control character (U+{ord(ch):04X})"
    stripped = text.strip()
    if not stripped:
        return "the summary is empty"
    if len(stripped) > MAX_SUMMARY_CHARS:
        return (
            f"the summary is {len(stripped)} characters; a card line holds at most "
            f"{MAX_SUMMARY_CHARS}"
        )
    if EM_DASH in stripped:
        return "the summary holds an em dash (U+2014); use a comma, a full stop or a colon"
    if _ARROW_OPEN.search(stripped):
        return "the summary holds '(→', which the card would read as the start of its link"
    if _MTG_OPEN in stripped:
        return "the summary holds '[mtg:', which the card would read as a meeting link"
    if is_trivial_restatement(stripped, mechanical_topic):
        return (
            "the summary only repeats the plain line the day gets anyway; say what was "
            "discussed, decided, planned or asked"
        )
    return None


# ---------------------------------------------------------------------------
# Paths.
# ---------------------------------------------------------------------------


def synth_dir(home: Path | str | None = None) -> Path:
    """``<home>/synth``: where claim files live (default home: the plug-in folder)."""
    return Path(home if home is not None else imconfig.HOME) / SYNTH_DIRNAME


def _same(a: Path, b: Path) -> bool:
    """Two resolved paths name the same place.  Case-folded, as APFS and NTFS are."""
    return str(a).casefold() == str(b).casefold()


def _inside(child: Path, parent: Path) -> bool:
    """``child`` sits below ``parent`` (both resolved), case-folded like the rest."""
    try:
        return Path(str(child).casefold()).is_relative_to(Path(str(parent).casefold()))
    except ValueError:
        return False


def _checked_synth_dir(home_dir: Path) -> Path:
    """``synth/`` resolved, refused if it resolves outside the home (a planted link)."""
    base = synth_dir(home_dir).resolve()
    home = home_dir.resolve()
    if not _inside(base, home) or _same(base, home):
        raise ValueError(f"refusing {base}: the claims folder must sit inside {home}")
    return base


def _refuse_foreign_target(target: Path, home_dir: Path) -> Path:
    """Refuse any claim-file target that does not resolve DIRECTLY inside ``synth/``.

    Both sides are resolved before comparing: macOS temp folders and a planted
    symlink both make an unresolved comparison answer wrongly, and a claim file
    names people and days.  Returns the resolved target, so the caller touches the
    path that was checked.
    """
    base = _checked_synth_dir(home_dir)
    resolved = Path(target).resolve()
    if not _same(resolved.parent, base) or not _CLAIM_NAME_RE.match(resolved.name):
        raise ValueError(
            f"refusing {resolved}: claim files live only directly inside {base}, "
            "named claim-<run id>.json"
        )
    return resolved


def _write_claim_file(home_dir: Path, run_id: str, payload: bytes) -> Path:
    """The house atomic write, owner-only, into ``synth/`` and nowhere else.

    ``payload`` is serialised by the caller BEFORE this runs, so a value that cannot
    be written fails before any file exists.
    """
    base = synth_dir(home_dir)
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = _refuse_foreign_target(base / f"claim-{run_id}.json", home_dir)
    fd, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f"{target.name}.",
                                     suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _chmod_600(temp)
        os.replace(temp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    _chmod_600(target)
    return target


def _chmod_600(path: Path) -> None:
    """Owner-only where the OS means it; a silent no-op where it does not (Windows)."""
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(path, 0o600)


def thread_paths(links: Any, threads_dir: Path) -> tuple[list[str], int]:
    """``(absolute transcript paths that exist, how many links did not)``.

    A link is ``_local/imessage/threads/<YYYY>/<file>`` (Brain-root-relative); it maps
    onto ``threads_dir``.  A link that resolves outside ``threads_dir`` (a ``..`` or a
    planted symlink) is counted missing, never followed.
    """
    base = Path(threads_dir).resolve()
    prefix = imthreads.THREAD_LINK_PREFIX + "/"
    found: list[str] = []
    missing = 0
    for link in links if isinstance(links, list) else []:
        if not isinstance(link, str) or not link.startswith(prefix):
            missing += 1
            continue
        target = (base / link[len(prefix):]).resolve()
        if not _inside(target, base) or _same(target, base) or not target.is_file():
            missing += 1
            continue
        found.append(str(target))
    return found, missing


# ---------------------------------------------------------------------------
# Names.
# ---------------------------------------------------------------------------


def _clean(value: Any) -> str:
    """One tidy line of a name, or ``""``."""
    return imthreads.sanitise_body(value)[:120] if isinstance(value, str) else ""


def mask_handle(value: str) -> str:
    """A handle's SHAPE: ``+<11 digits>``, ``<local 7>@<domain 11>``.  The same
    spelling as ``imessage.mask_handle`` (that module is the CLI; this one may not
    import it)."""
    if "@" in value:
        local, _, domain = value.partition("@")
        return f"<local {len(local)}>@<domain {len(domain)}>"
    if value.startswith("+"):
        return f"+<{len(value) - 1} digits>"
    return f"<{len(value)} chars>"


def _card_name(path: Path) -> str:
    """The ``name:`` scalar from a card's frontmatter, read the way the engine's own
    ``people_stamp._read_scalars`` reads a scalar (no YAML parser needed for one
    line, and no engine import outside ``imspine``)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return ""
    for raw in lines[1:]:
        if raw.strip() == "---":
            break
        found = re.match(r"^name\s*:\s*(.*)$", raw)
        if found:
            return _clean(found.group(1).strip().strip('"').strip("'"))
    return ""


def display_name(
    conn: sqlite3.Connection,
    record: Mapping[str, Any],
    contacts: Mapping[str, str] | None = None,
) -> str:
    """Who a queued day is with: their card's name, else Contacts, else a masked handle.

    The card is found through :func:`imspine.person_path` (identity is the id, never
    the filename).  Contacts is ``contacts`` when given, else the Contacts name the
    morning run captured when it queued the day, else (only then) the address book
    itself.  The masked handle is the last resort: it tells the writer nothing, but
    it never puts a number into a file a model reads.
    """
    person_id = record.get("person_id")
    if person_id:
        card = imspine.person_path(conn, str(person_id))
        if card is not None:
            name = _card_name(card.path)
            if name:
                return name
    ident = str(record.get("identifier") or "")
    name = _clean(contacts.get(ident)) if contacts is not None else ""
    name = name or _clean(record.get("name"))
    if not name and contacts is None and ident:
        name = _clean(imcontacts.load_map().get(ident))
    return name or (mask_handle(ident) if ident else "someone")


# ---------------------------------------------------------------------------
# Gates.
# ---------------------------------------------------------------------------


def _pause(report: dict[str, Any], reason: str, sentence: str) -> dict[str, Any]:
    report["paused"] = {"reason": reason, "sentence": sentence}
    return report


def _first_gates(cfg: Mapping[str, Any]) -> tuple[str | None, str, frozenset[str]]:
    """The gates that need nothing but a question: own handles, then the engine.

    ``(reason, sentence, own_handles)``; ``reason`` is ``None`` when both pass.  The
    same two gates, in the same order, as ``imrun.daily``: with no own handles the
    member's own card cannot be walled off, and a changed engine is a pause, never a
    write on a contract nobody checked.
    """
    try:
        own = imthreads.owner_handles(cfg)
    except imthreads.OwnHandlesRequired as exc:
        return "no_own_handles", str(exc), frozenset()
    drift = imspine.check_contract()
    if drift:
        return "engine_changed", drift, own
    return None, "", own


def _session(home_dir: Path) -> contextlib.AbstractContextManager[imledger.Ledger]:
    """The ledger under the engine's lock on ``ledger.lock``, as ``imrun`` takes it."""
    return imledger.session(
        lambda: imspine.file_lock(imledger.lock_target(home_dir), timeout=LOCK_TIMEOUT_S),
        home=home_dir,
    )


def _new_run_id(moment: datetime) -> str:
    return f"synth-{moment:%Y%m%dT%H%M%S}-{secrets.token_hex(4)}"


def _unit_id(index: int) -> str:
    """The short id a writer keys a summary by: ``u01``, ``u02`` … in claim order."""
    return f"u{index + 1:02d}"


# ---------------------------------------------------------------------------
# claim()
# ---------------------------------------------------------------------------


def claim(
    n: int | None = None,
    *,
    home: Path | str | None = None,
    threads_dir: Path | str | None = None,
    db_path: Path | str | None = None,
    cfg: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    contacts: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Lease up to ``n`` queued days and write their claim file.  A plain-data report.

    ``report["claim_path"]`` is the claim file's path (``None`` when nothing was
    claimed); ``report["paused"]`` is ``{reason, sentence}`` when a gate refused, and
    then nothing was written.  ``n`` defaults to ``synth_claim_limit``.  Every other
    keyword exists so a test points the run at temp places, never the member's.
    """
    cfg = dict(cfg) if cfg is not None else imconfig.load_config()
    home_dir = Path(home) if home is not None else Path(imconfig.HOME)
    threads = Path(threads_dir) if threads_dir is not None else Path(imconfig.THREADS_DIR)
    moment = now if now is not None else imrun._engine_now()
    limit = int(n) if n is not None else imrun._int_cfg(cfg, "synth_claim_limit")
    if limit < 1:
        raise ValueError("a claim takes at least one day")
    report: dict[str, Any] = {
        "verb": "synthesise",
        "paused": None,
        "zone": imrun._zone_name(moment),
        "limit": limit,
        "claim_path": None,
        "claim_id": None,
        "claimed": 0,
        "days": [],
        "lease_until": None,
        "waiting": 0,
        "held_by_other_claims": 0,
        "no_transcript": 0,
        "left_waiting": 0,
        "expired_claims_cleared": 0,
        "old_claim_files_removed": 0,
        "why_nothing": None,
    }
    reason, sentence, _own = _first_gates(cfg)
    if reason is not None:
        return _pause(report, reason, sentence)
    if not imrun._ledger_exists(home_dir):
        report["why_nothing"] = "no texts have been queued yet"
        return report
    with contextlib.ExitStack() as stack:
        try:
            led = stack.enter_context(_session(home_dir))
        except TimeoutError:
            return _pause(report, "busy", SENTENCE_BUSY_CLAIM)
        except imledger.LedgerCorrupt as exc:
            return _pause(report, "ledger_damaged", f"texts paused: {exc}")
        conn = imspine.open_conn(db_path)
        try:
            drift = imspine.check_contract(conn)
            if drift:
                return _pause(report, "engine_changed", drift)
            return _claim_run(report, led=led, conn=conn, home_dir=home_dir, threads=threads,
                              moment=moment, limit=limit, contacts=contacts)
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            conn.close()
    return report  # pragma: no cover - the with block always returns


def _claim_run(
    report: dict[str, Any],
    *,
    led: imledger.Ledger,
    conn: sqlite3.Connection,
    home_dir: Path,
    threads: Path,
    moment: datetime,
    limit: int,
    contacts: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Pick, lease, write the file, commit the ledger run.  Under the ledger lock."""
    lease_until = moment + timedelta(hours=LEASE_HOURS)
    waiting = 0
    picked: list[tuple[str, dict[str, Any], list[str]]] = []
    for key, record in sorted(
        led.queued().items(), key=lambda kv: (str(kv[1].get("day", "")), kv[0])
    ):
        if led.is_stamped(key):
            continue
        if led.live_claim(key, moment) is not None:
            report["held_by_other_claims"] += 1
            continue
        waiting += 1
        if len(picked) >= limit:
            continue
        paths, _missing = thread_paths(record.get("links"), threads)
        if not paths:
            report["no_transcript"] += 1
            continue
        picked.append((key, record, paths))
    report["waiting"] = waiting

    has_claims = bool(led.counts()["claims"])
    if not picked and not has_claims:
        report["left_waiting"] = max(0, waiting - report["no_transcript"])
        report["why_nothing"] = _why_nothing(report)
        return report

    run_id = _new_run_id(moment)
    written: Path | None = None
    led.begin_run(run_id)
    try:
        report["expired_claims_cleared"] = len(led.prune_claims(moment))
        chosen = {key: (record, paths) for key, record, paths in picked}
        taken = led.claim(list(chosen), run_id, lease_until, moment) if chosen else []
        units = []
        for index, key in enumerate(taken):
            record, paths = chosen[key]
            units.append({
                "id": _unit_id(index),
                "key": key,
                "name": display_name(conn, record, contacts),
                "day": str(record.get("day") or ""),
                "direction": str(record.get("direction") or imthreads.MUTUAL),
                "threads": paths,
                "links": [link for link in record.get("links") or [] if isinstance(link, str)],
                "mechanical_topic": str(record.get("topic") or ""),
            })
        if units:
            document = {
                "format": CLAIM_FORMAT,
                "kind": CLAIM_KIND,
                "claim_id": run_id,
                "claimed_at": moment.isoformat(),
                "lease_until": lease_until.isoformat(),
                "zone": report["zone"],
                "max_chars": MAX_SUMMARY_CHARS,
                "instructions": DRAIN_INSTRUCTIONS,
                "commit_with": _commit_command(synth_dir(home_dir) / f"claim-{run_id}.json"),
                "units": units,
                "summaries": {},
            }
            payload = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            written = _write_claim_file(home_dir, run_id, payload)
        led.commit_run(run_id)
    except BaseException:
        if written is not None:
            with contextlib.suppress(OSError):
                written.unlink()
        if led.run_id == run_id:
            with contextlib.suppress(Exception):
                led.abort_run(run_id)
        raise
    led.compact()
    report["old_claim_files_removed"] = _sweep_claim_files(led, home_dir)

    report["claimed"] = len(taken)
    # A day with no transcript here waits for the stale fallback, not the next pass.
    report["left_waiting"] = max(0, waiting - len(taken) - report["no_transcript"])
    if written is not None:
        report.update(claim_path=str(written), claim_id=run_id,
                      lease_until=lease_until.isoformat(),
                      days=sorted({u["day"] for u in units}))
    else:
        report["why_nothing"] = _why_nothing(report)
    return report


def _why_nothing(report: Mapping[str, Any]) -> str:
    """The plain reason nothing was claimed: "zero" is never left unexplained."""
    reasons: list[str] = []
    if report["held_by_other_claims"]:
        reasons.append(
            f"{report['held_by_other_claims']} day(s) are already with another summary pass"
        )
    if report["no_transcript"]:
        reasons.append(
            f"{report['no_transcript']} day(s) have no transcript on this machine, so they "
            "will land with their plain line"
        )
    return "; ".join(reasons) or "nothing is waiting for a summary"


def _commit_command(path: Path) -> str:
    """The exact command that commits this file, for the writer to run when it is done."""
    return (
        f"uv run --directory {imconfig.SCRIPTS_DIR} python {imconfig.HOME / 'imessage.py'} "
        f"synthesise --commit {path}"
    )


def _sweep_claim_files(led: imledger.Ledger, home_dir: Path) -> int:
    """Remove claim files no live ledger claim stands behind.  Under the ledger lock.

    A claim file whose claim is gone (committed but the file could not be removed,
    written by a claim run that was killed before its ledger commit, or expired and
    cleared) can never land anything: :func:`commit` refuses a claim the ledger no
    longer holds.  Only exact ``claim-<run id>.json`` files and their temp files are
    touched, never a link, never anything else.
    """
    try:
        base = _checked_synth_dir(home_dir)
    except ValueError:
        return 0
    if not base.is_dir():
        return 0
    removed = 0
    for entry in base.iterdir():
        if entry.is_symlink() or not entry.is_file():
            continue
        match = _CLAIM_NAME_RE.match(entry.name)
        stray_temp = (entry.name.startswith("claim-synth-") and entry.name.endswith(".tmp")
                      and ".json." in entry.name)
        if (match and led.claim_of(match.group(1)) is None) or stray_temp:
            with contextlib.suppress(OSError):
                entry.unlink()
                removed += 1
    return removed


# ---------------------------------------------------------------------------
# commit()
# ---------------------------------------------------------------------------


class ClaimFileError(ValueError):
    """The claim file cannot be read as one this module wrote.  Nothing was changed."""


def _read_claim(path: Path) -> dict[str, Any]:
    """The claim file, checked for the shape :func:`claim` writes, or ClaimFileError."""
    try:
        size = path.stat().st_size
        if size > _MAX_CLAIM_BYTES:
            raise ClaimFileError("the claim file is far larger than any claim this plug-in writes")
        document = json.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        raise ClaimFileError(
            "that claim file is not there (already committed, or cleared after its lease "
            "ran out)"
        ) from None
    except UnicodeDecodeError:
        raise ClaimFileError("the claim file is not UTF-8 text") from None
    except json.JSONDecodeError as exc:
        raise ClaimFileError(
            f"the claim file is not valid JSON ({exc.msg} at line {exc.lineno}); fix it "
            "and commit again"
        ) from None
    if not isinstance(document, dict):
        raise ClaimFileError("the claim file does not hold a JSON object")
    if document.get("format") != CLAIM_FORMAT or document.get("kind") != CLAIM_KIND:
        raise ClaimFileError("the claim file is not one this plug-in wrote (format or kind)")
    claim_id = document.get("claim_id")
    name_match = _CLAIM_NAME_RE.match(path.name)
    if not (isinstance(claim_id, str) and _RUN_ID_RE.match(claim_id) and name_match
            and name_match.group(1) == claim_id):
        raise ClaimFileError("the claim file's id does not match its name")
    units = document.get("units")
    if not isinstance(units, list) or not all(isinstance(u, dict) for u in units):
        raise ClaimFileError("the claim file's units are not a list")
    ids = [u.get("id") for u in units]
    if len(set(map(str, ids))) != len(ids) or not all(
        isinstance(i, str) and _UNIT_ID_RE.match(i) for i in ids
    ):
        raise ClaimFileError("the claim file's unit ids are missing or repeated")
    summaries = document.get("summaries", {})
    if summaries is None:
        summaries = {}
    if not isinstance(summaries, dict):
        raise ClaimFileError("the claim file's summaries are not a map of id to text")
    document["summaries"] = summaries
    return document


def _claim_target(claim_path: Path | str, home_dir: Path) -> Path:
    """The resolved claim file, refused unless it sits directly inside ``synth/``."""
    try:
        return _refuse_foreign_target(Path(claim_path).expanduser(), home_dir)
    except ValueError as exc:
        raise ClaimFileError(str(exc)) from None


def _summary_for(summaries: Mapping[str, Any], unit_id: str, key: str) -> tuple[Any, bool]:
    """``(the summary written for this unit, whether two different ones were)``.

    Keyed by the unit's id, as the instructions say; the full ledger key is accepted
    too, because a writer that used it meant the same day.  Two different texts for
    one day is refused rather than picked between.
    """
    by_id, by_key = summaries.get(unit_id), summaries.get(key)
    if by_id is not None and by_key is not None and by_id != by_key:
        return None, True
    return (by_id if by_id is not None else by_key), False


def _new_commit_report(moment: datetime, path: Path | str) -> dict[str, Any]:
    return {
        "verb": "synthesise_commit",
        "paused": None,
        "error": None,
        "zone": imrun._zone_name(moment),
        "claim_path": str(path),
        "claim_id": None,
        "units": 0,
        "days": {},
        "landed": [],
        "already_on_card": [],
        "refused": {},
        "released": {},
        "skipped": {},
        "failed": {},
        "unknown_summaries": 0,
        "recovered": {"done": 0, "dropped": 0},
        "projected": 0,
        "projection_skipped": 0,
        "claim_released": False,
        "claim_file_removed": False,
    }


def commit(
    claim_path: Path | str,
    *,
    home: Path | str | None = None,
    db_path: Path | str | None = None,
    cfg: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate a claim file's summaries and land the good ones.  A plain-data result.

    ``landed`` (ids stamped now), ``already_on_card`` (the exact line was already
    there; the ledger caught up), ``refused`` (id → why the summary may not land;
    back in the queue), ``released`` (id → why nothing was landed; back in the
    queue), ``skipped`` (id → why it is no longer this claim's to land), ``failed``
    (id → the card write's own refusal; still queued, recorded for ``status``).
    ``paused`` is a gate's ``{reason, sentence}`` and ``error`` a claim file that
    could not be read; either way nothing was written and the file is untouched.
    """
    cfg = dict(cfg) if cfg is not None else imconfig.load_config()
    home_dir = Path(home) if home is not None else Path(imconfig.HOME)
    moment = now if now is not None else imrun._engine_now()
    report = _new_commit_report(moment, claim_path)
    reason, sentence, own = _first_gates(cfg)
    if reason is not None:
        return _pause(report, reason, sentence)
    try:
        target = _claim_target(claim_path, home_dir)
    except ClaimFileError as exc:
        report["error"] = str(exc)
        return report
    report["claim_path"] = str(target)
    if not target.is_file():
        report["error"] = (
            "that claim file is not there (already committed, or cleared after its lease "
            "ran out)"
        )
        return report
    with contextlib.ExitStack() as stack:
        try:
            led = stack.enter_context(_session(home_dir))
        except TimeoutError:
            return _pause(report, "busy", SENTENCE_BUSY_COMMIT)
        except imledger.LedgerCorrupt as exc:
            return _pause(report, "ledger_damaged", f"texts paused: {exc}")
        try:
            document = _read_claim(target)  # read under the lock: one commit at a time
        except ClaimFileError as exc:
            report["error"] = str(exc)
            return report
        conn = imspine.open_conn(db_path)
        try:
            drift = imspine.check_contract(conn)
            if drift:
                return _pause(report, "engine_changed", drift)
            return _commit_run(report, led=led, conn=conn, document=document, target=target,
                               own=own, moment=moment)
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            conn.close()
    return report  # pragma: no cover - the with block always returns


def _ownership(
    led: imledger.Ledger, claim_id: str, held: Iterable[str], key: str, moment: datetime
) -> str | None:
    """Why this day is no longer this claim's to land, or ``None`` when it still is."""
    if led.is_stamped(key):
        return ("already on the card (the daily run filed it with its plain line, or an "
                "earlier commit did)")
    if not led.is_queued(key):
        return "no longer waiting for a summary"
    if key not in held:
        return ("this claim no longer holds it: its lease ran out and it was cleared, or "
                "the claim was already committed")
    holder = led.live_claim(key, moment)
    if holder is not None and holder != claim_id:
        return "another summary pass took it after this claim's lease ran out"
    return None


def _commit_run(
    report: dict[str, Any],
    *,
    led: imledger.Ledger,
    conn: sqlite3.Connection,
    document: dict[str, Any],
    target: Path,
    own: frozenset[str],
    moment: datetime,
) -> dict[str, Any]:
    """Settle, validate, land, release, commit.  Under the ledger lock; one DB commit."""
    claim_id = str(document["claim_id"])
    report["claim_id"] = claim_id
    entry = led.claim_of(claim_id)
    held_keys = [k for k in (entry or {}).get("keys") or [] if isinstance(k, str)]
    expected = {_unit_id(i): k for i, k in enumerate(held_keys)}
    summaries: dict[str, Any] = document["summaries"]
    units: list[dict[str, Any]] = document["units"]
    report["units"] = len(units)
    known = {str(u.get("id")) for u in units} | {str(u.get("key")) for u in units}
    report["unknown_summaries"] = sum(1 for k in summaries if k not in known)

    run_id = f"{claim_id}-commit-{moment:%Y%m%dT%H%M%S}-{secrets.token_hex(4)}"
    owner_ids = imrun.owner_person_ids(conn, own)
    led.begin_run(run_id)
    try:
        done, dropped, recovered_rels = imrun.recover(conn, led)
        report["recovered"] = {"done": done, "dropped": dropped}

        items: list[tuple[dict[str, Any], str]] = []
        by_key: dict[str, str] = {}
        seen: set[str] = set()
        for unit in units:
            uid, key = str(unit.get("id")), str(unit.get("key") or "")
            report["days"][uid] = str(unit.get("day") or "")
            seen.add(uid)
            if entry is not None and expected.get(uid) != key:
                report["skipped"][uid] = "not a day this claim holds under that id"
                continue
            gone = _ownership(led, claim_id, held_keys, key, moment)
            if gone:
                report["skipped"][uid] = gone
                continue
            record = led.queued_record(key) or {}
            given = unit.get("links") if isinstance(unit.get("links"), list) else []
            if any(link not in given for link in record.get("links") or []):
                report["released"][uid] = (
                    "a conversation arrived for that day after it was claimed; it is back "
                    "in the queue so its summary covers all of it"
                )
                continue
            text, conflict = _summary_for(summaries, uid, key)
            if conflict:
                report["refused"][uid] = "two different summaries were written for this day"
                continue
            if text is None:
                report["released"][uid] = "no summary was written"
                continue
            problem = validate_summary(text, record.get("topic"))
            if problem:
                report["refused"][uid] = problem
                continue
            items.append((record, text.strip()))
            by_key[key] = uid
        for uid, key in expected.items():
            if uid not in seen and not led.is_stamped(key):
                report["released"][uid] = "missing from the claim file"

        rels: list[str] = []
        if items:
            result = imrun.land(conn, led, items, owner_ids=owner_ids, now=moment,
                                topic_kind="summary")
            report["landed"] = [by_key[k] for k in result.stamped]
            report["already_on_card"] = [by_key[k] for k in result.already_present]
            for k in result.skipped:
                report["skipped"][by_key[k]] = "already on the card"
            for k, why in result.refused.items():
                report["skipped"][by_key[k]] = why
            for k, why in result.failures.items():
                report["failed"][by_key[k]] = why
            rels = list(result.rels)
            _count_projection(report, result.projection)
        extra = [rel for rel in recovered_rels if rel not in rels]
        if extra:
            _count_projection(report, imspine.project(conn, extra))
        if entry is not None:
            report["claim_released"] = led.release_claim(claim_id)
        conn.commit()  # G1: the projection above is inside this transaction
    except sqlite3.OperationalError as exc:
        imrun._undo(conn, led, run_id, False)
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return _pause(report, "memory_busy", SENTENCE_MEMORY_BUSY)
        raise
    except BaseException:
        imrun._undo(conn, led, run_id, False)
        raise
    led.commit_run(run_id)
    led.compact()
    try:
        target.unlink()
        report["claim_file_removed"] = True
    except FileNotFoundError:
        report["claim_file_removed"] = True
    except OSError as exc:
        _warn(f"the claim file could not be removed ({type(exc).__name__}); the next claim "
              "clears it, and committing it again lands nothing.")
    return report


def _count_projection(report: dict[str, Any], projection: Any) -> None:
    if projection is None:
        return
    report["projected"] += int(getattr(projection, "projected", 0) or 0)
    report["projection_skipped"] += len(getattr(projection, "skipped_paths", []) or [])
