"""iMessage intake — the ONE canonical spelling of a handle, and the name behind it.

A raw iMessage handle arrives from ``chat.db`` as whatever the sender's device
happened to send: ``+12025550123`` from one thread, ``2025550123`` from another,
``(202) 555-0123`` typed by hand into a group, ``someone@example.com`` from a
Mac.  This module answers two questions about such a handle and nothing else:

* :func:`canonicalise` — what is the ONE string this plug-in stores for it?
* :func:`load_map` — what does macOS Contacts call it?

Standard library only.  Nothing here writes, nothing here reaches the network,
and every store this module opens is opened **read-only** through a
``file:…?mode=ro`` URI.  The module imports cleanly on Windows and Linux even
though the data it reads only exists on a Mac: the Mac-only part is a glob that
finds nothing and says so, never an import error.

Why ``canonicalise`` is the most dangerous function in the plug-in
------------------------------------------------------------------
Its return value is the plug-in's **permanent ledger key** and its
**proposal-dedup key**.  Once a conversation-day has been stamped under a key,
that key is what stops the next run re-stamping it, and it is what a member's
accepted "this number is <person>" proposal is filed against.  Change the rule
later and every historical key becomes unreachable: the ledger silently
re-ingests years of history, and every number mints a second proposal card.
There is no migration that fixes this cheaply, so the rule below is written
out in full and is not to be "improved" in passing.

The rule was last changed on 2026-09-24, by the member's ruling (next section), before
anything had ever been stamped — so that change orphaned no ledger key.  The
next change will not be so cheap.

The rule, exactly
-----------------
==========================================  =====================================
input                                       result
==========================================  =====================================
``None`` / ``""`` / all whitespace          ``None``
contains ``@``                              an email: ``value.strip().lower()``
no digits at all                            ``None`` (see "the one deviation")
starts with ``+``                           ``"+"`` followed by only its digits
fewer than 7 digits (a short code)          the bare digit string, unchanged
exactly 11 digits beginning with ``1``      ``"+"`` + those 11
``"+1"`` + digits would pass 15 digits      the bare digit string, unchanged
any other count (10, 7, 11 not ``1``, …)    ``"+1"`` + those digits
==========================================  =====================================

The member's ruling, 2026-09-24 — a number with no country code is American
---------------------------------------------------------------------------
The member: *"just assume us country code if there isn't one."*  So a number that
arrives without a ``+`` is read as North American and gets ``+1``, whatever its
length.  The why is the engine's lint gate, not tidiness: a phone identifier
can only be attached to a person's card if it passes
``people_norm.is_valid_phone`` (``.claude/scripts/people_norm.py:217-219``),
which accepts **only** the ``+country`` E.164 form (``^\\+[1-9]\\d{6,14}$``).  A
bare-digit key could never be accepted onto a card, so every such number would
sit in the review queue forever as "can't be added by number".  ``+1`` is also
the spelling Contacts, the Messages database and the member's existing cards
already agree on for US numbers.

The trade, said once: a non-US number saved without its country code is now
read as American — a UK ``07700900123`` becomes ``+107700900123``, which the
engine's ``same_phone`` does not match to a card storing ``+447700900123``.
The member made that call knowingly.

Two things stay bare on purpose, because ``+1`` would make them wrong rather
than merely unmatched:

* **A short code** (fewer than :data:`SHORTCODE_MIN_DIGITS` = 7 digits — a bank,
  a delivery firm, a 2FA robot).  ``imthreads.is_shortcode`` strips a leading
  ``+`` and counts what is left, so a 6-digit short code dressed as
  ``+1`` + 6 digits would count 7 and stop being recognised as one.  Leaving it
  bare keeps the short-code floor exactly where it was.
* **Anything whose ``+1`` form would pass E.164's 15-digit maximum.**  It is not
  a phone number at all (real message stores carry 16- and 25-digit sender
  IDs),
  no country code can rescue it, and ``+1`` would only dress it up as one.  It
  stays as its own digits and ``review`` labels it "can't be added by number".

(An 11-digit number beginning with ``1`` already carries the North American
country code, so it gets a ``+`` rather than a second ``1``.)

Why ``slugify_label`` exists (it is not tidy-up)
------------------------------------------------
A Contacts label reaches two places that both break on ordinary punctuation:
a **filename**, and the ``(→ …)`` pointer written onto a person's card by
``people_stamp.py:142`` (``line += f" (→ {link})"``).  The engine's parser for
that pointer is ``people_index.py:71``::

    _LINK_RE = re.compile(r"\\(\\s*→\\s*([^)]+)\\)")

— a negated character class that **stops at the first ``)``**, used to read the
link back out at ``people_index.py:810`` and to strip it from the visible
summary at ``:820``.  A typical label like ``Mom (cell)`` or
``Garden Group (2026)`` therefore truncates mid-link: the stored pointer becomes
``→ mom (cell`` and the tail of the filename leaks out of the link and into the
card's visible summary.  ``/`` and ``:`` are worse — they break the path itself.
:func:`slugify_label` reduces a label to ``[a-z0-9-]`` so neither can happen, and
never returns an empty string, because an empty filename component is a silent
collision.

``imconfig`` is imported for the ``sys.path`` discipline, not for a setting
---------------------------------------------------------------------------
This module needs no configuration.  It imports ``imconfig`` so that importing
``imcontacts`` FIRST in a process still leaves this plug-in ahead of the engine
on ``sys.path`` — see the ``sys.path ORDER`` section of ``imconfig``'s docstring
for why a plug-in module losing a name collision to an engine module of the same
name is a live, silent bug in a sibling plug-in.  ``ensure_engine_path`` is
idempotent and never raises, so the call below costs nothing and closes that
hole whichever module a caller happens to import first.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

import imconfig

# Idempotent, never raises. See the module docstring's last section: this is the
# one line that keeps `import imcontacts` a safe first import for the plug-in.
imconfig.ensure_engine_path()


# ---------------------------------------------------------------------------
# Where the Contacts stores live.
# ---------------------------------------------------------------------------

#: The filename every macOS Contacts store uses, in both places it can sit.
STORE_FILENAME: str = "AddressBook-v22.abcddb"

#: The folder macOS keeps Contacts in. Resolved from ``Path.home()`` rather than
#: a literal so it is correct for any user, and it is merely a path on a machine
#: that has no such folder — computing it never touches the disk.
DEFAULT_BASE: Path = Path.home() / "Library" / "Application Support" / "AddressBook"

#: The longest slug :func:`slugify_label` will return. Long enough for a real
#: label, short enough that ``<slug>.md`` plus a folder is nowhere near any
#: filesystem's path limit.
MAX_LABEL_SLUG_LEN: int = 60

#: What :func:`slugify_label` falls back to when a label reduces to nothing.
LABEL_FALLBACK: str = "label"

#: A bare number with fewer digits than this is a short code and is NOT given
#: ``+1``. It must equal ``imthreads.SHORTCODE_MIN_DIGITS``, and a test pins the
#: two together. Defined here rather than imported because ``imthreads`` imports
#: this module: importing it back would be a cycle.
SHORTCODE_MIN_DIGITS: int = 7

#: E.164's ceiling on the digits after the ``+`` (ITU-T E.164; the engine's
#: ``people_norm._E164_RE`` is ``^\+[1-9]\d{6,14}$``, i.e. 15 at most). A bare
#: number whose ``+1`` form would exceed it is not a phone number and stays bare.
E164_MAX_DIGITS: int = 15

_NON_DIGITS = re.compile(r"\D")
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def _warn(message: str) -> None:
    """One plain line to stderr, in the plug-in's house prefix.

    Matches ``imconfig._warn``'s shape on purpose: every line this plug-in
    prints to stderr is greppable as ``[imessage]``.  Degrading loudly beats
    wedging the tool, and beats a silent empty result even more — an empty
    Contacts map looks exactly like "you know nobody".
    """
    print(f"[imessage] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# THE canonical spelling. Read the module docstring before touching this.
# ---------------------------------------------------------------------------


def canonicalise(handle: str | None) -> str | None:
    """The ONE string this plug-in stores for ``handle`` — ledger key and dedup key.

    The full rule table, and the reasoning behind every row of it, is in the
    module docstring.  The short version, per the member's ruling of 2026-09-24: an
    email lowercases; a number that already carries a ``+`` keeps it with its
    digits; **a number with no country code is American** and becomes ``+1`` +
    its digits (an 11-digit one beginning with ``1`` just gains the ``+``).  The
    why is the engine's lint gate: ``people_norm.is_valid_phone`` accepts only
    ``+country`` E.164, so a bare number could never be attached to a card.

    Two kinds of bare number are left bare, because ``+1`` would make them wrong:
    a short code (fewer than :data:`SHORTCODE_MIN_DIGITS` digits), so
    ``imthreads.is_shortcode`` still recognises it; and anything whose ``+1``
    form would pass :data:`E164_MAX_DIGITS`, which is not a phone number at all.
    The trade the member accepted: a non-US number saved without its country code is
    now read as American.

    Guaranteed **idempotent**: ``canonicalise(canonicalise(x)) ==
    canonicalise(x)`` for every input, including ``None``.  That property is not
    decoration — the same value is read back out of the ledger, out of a
    proposal card and out of a member's config and re-normalised on the way, so a
    rule that shifted on a second pass would split one person into two keys.  A
    test pins it over a wide input set.

    Never raises.  A non-string (a stray ``int`` from a database column, say) is
    coerced with ``str`` rather than exploding, because a single odd row must not
    take down a run over a member's whole message history.

    Returns ``None`` — meaning "this is not a handle, skip it" — for empty,
    whitespace-only and digit-free input.  A caller must treat ``None`` as
    "drop this row", never as a key.
    """
    if handle is None:
        return None
    if not isinstance(handle, str):  # a stray non-text column value
        handle = str(handle)

    value = handle.strip()
    if not value:
        return None

    # An address is an address, even if it somehow also carries a `+`. This
    # branch is deliberately FIRST: `+tag@example.com` is an email, not a phone.
    if "@" in value:
        return value.lower()

    digits = _NON_DIGITS.sub("", value)
    if not digits:
        # The one deviation from the original brief, and it is deliberate: a
        # digit-only rule would hand back `""` (or a bare `"+"`) for input like
        # `"+"` or `"---"`. An empty string is a usable dict
        # key, so it would become a permanent ledger key shared by every piece of
        # junk that ever arrives — one bucket, many people. `None` says "not a
        # handle" and the caller drops the row.
        return None

    if value.startswith("+"):
        return f"+{digits}"

    # A short code stays bare: `imthreads.is_shortcode` strips a leading `+` and
    # counts the rest, so `+1` + 6 digits would count 7 and stop being one.
    if len(digits) < SHORTCODE_MIN_DIGITS:
        return digits

    # Already carries the North American country code; it only lacks the `+`.
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"

    # Longer than any phone number can be once `+1` is added: a sender ID, not
    # a line. `+1` would only disguise it as a phone the lint gate then accepts.
    if len(digits) + 1 > E164_MAX_DIGITS:
        return digits

    # The member's ruling, 2026-09-24: no country code means American. This is also
    # what makes the key attachable to a card, since the engine's lint gate
    # (`people_norm.is_valid_phone`) accepts only `+country` E.164.
    return f"+1{digits}"


# ---------------------------------------------------------------------------
# Labels that have to survive a filename AND the engine's `(→ …)` parser.
# ---------------------------------------------------------------------------


def slugify_label(name: str | None) -> str:
    """Reduce a Contacts label to ``[a-z0-9-]``, never empty.

    Accents fold first (``NFKD`` then drop combining marks, the same shape as
    the engine's ``people_norm.fold_marks``), so ``José`` slugs to ``jose``
    rather than being minced.  Everything outside ``a-z0-9`` then collapses to a
    single ``-``; leading and trailing dashes are trimmed; the result is capped
    at :data:`MAX_LABEL_SLUG_LEN` characters and re-trimmed, so a cut that lands
    mid-separator cannot leave a trailing dash.

    **Why this is load-bearing, not cosmetic.**  The label reaches a filename and
    the ``(→ …)`` pointer on a person's card, and the engine's pointer parser is
    ``people_index.py:71`` — ``re.compile(r"\\(\\s*→\\s*([^)]+)\\)")``, read back at
    ``:810`` and stripped from the summary at ``:820``, written by
    ``people_stamp.py:142``.  That class stops at the first ``)``, so a typical
    label like ``Mom (cell)`` truncates the stored link to ``mom (cell`` and
    spills the filename's tail into the card's visible summary — a corrupted
    pointer that still *looks* like a link.  ``/`` and ``:`` do not corrupt the
    link, they break the path outright.  Reducing to ``[a-z0-9-]`` removes both
    failure modes at the source rather than escaping them at each call site.

    Never returns ``""``.  An empty filename component silently collides with
    every other empty one, so a label that reduces to nothing (all punctuation,
    an emoji, a script with no Latin decomposition) falls back to
    ``label-<8 hex>`` derived from the original text: stable across runs and
    machines, and still distinct per label.  Genuinely empty input — ``None``,
    ``""``, whitespace — returns the bare :data:`LABEL_FALLBACK`, because there
    is nothing to keep distinct.

    Idempotent: the output charset is a subset of the input charset it accepts,
    and trimming and capping have both already happened, so re-slugging a slug
    returns it unchanged.

    ``name`` is typed ``str | None`` rather than the plain ``str`` a caller
    normally passes: this module is on the never-raises side of the plug-in, and
    a ``None`` display name out of a database column must cost a fallback, not a
    ``TypeError`` in the middle of a run.
    """
    if name is None:
        return LABEL_FALLBACK
    if not isinstance(name, str):
        name = str(name)

    original = name.strip()
    if not original:
        return LABEL_FALLBACK

    folded = "".join(
        ch for ch in unicodedata.normalize("NFKD", original) if not unicodedata.combining(ch)
    )
    slug = _NON_SLUG.sub("-", folded.lower()).strip("-")

    if len(slug) > MAX_LABEL_SLUG_LEN:
        slug = slug[:MAX_LABEL_SLUG_LEN].strip("-")

    if not slug:
        digest = hashlib.sha1(original.encode("utf-8")).hexdigest()[:8]
        return f"{LABEL_FALLBACK}-{digest}"
    return slug


# ---------------------------------------------------------------------------
# macOS Contacts — read-only, one warning per bad store, never a crash.
# ---------------------------------------------------------------------------


def store_paths(base: Path | str | None = None) -> list[Path]:
    """Every Contacts store to try, in the order :func:`load_map` reads them.

    Two places hold a store and BOTH are read:

    * ``<base>/Sources/*/AddressBook-v22.abcddb`` — one per account (iCloud, an
      Exchange account, "On My Mac"). This is where the real data is;
    * ``<base>/AddressBook-v22.abcddb`` — the top-level store, which on a normal
      machine holds a couple of records and is easy to mistake for "Contacts is
      empty" if it is read alone.

    Missing either one entirely is normal, not an error, and returns the other.
    The ``Sources`` list is **sorted** so the merge order in :func:`load_map` is
    the same on every run; the top-level store is read last because it is the
    thinnest.  Only paths that exist as files are returned.

    Never raises: an unreadable ``Sources`` folder yields no source stores
    instead of an ``OSError``.
    """
    root = DEFAULT_BASE if base is None else Path(base)
    found: list[Path] = []

    try:
        sources = sorted((root / "Sources").glob(f"*/{STORE_FILENAME}"))
    except OSError:  # an unreadable mount, a permissions wall
        sources = []
    for path in sources:
        try:
            if path.is_file():
                found.append(path)
        except OSError:
            continue

    top = root / STORE_FILENAME
    try:
        if top.is_file():
            found.append(top)
    except OSError:
        pass

    return found


def _ro_uri(path: Path) -> str:
    """A ``file:…?mode=ro`` URI for ``path``, correct on every OS.

    Built with ``Path.as_uri()`` rather than by string interpolation, because the
    real folder is ``~/Library/Application Support/AddressBook`` — it contains a
    space, and a Windows path contains backslashes and a drive letter.
    ``as_uri`` percent-encodes both correctly and SQLite decodes them back.
    ``mode=ro`` is what makes the open read-only; this plug-in never opens a
    member's Contacts store any other way, and never copies it.
    """
    return f"{path.resolve().as_uri()}?mode=ro"


def _display_name(first: object, last: object, organisation: object) -> str:
    """``"First Last"``, or the organisation when there is no personal name.

    A Contacts record for a business — a repair shop, a school office — carries no
    ``ZFIRSTNAME``/``ZLASTNAME`` at all, only ``ZORGANIZATION``.  Without this
    fallback every one of those numbers arrives nameless and mints a "who is
    this?" proposal for a place the member already has in their phone.  On a real
    address book, a handful of rows across the stores are organisation-only.

    Personal name wins whenever there is one; organisation is a fallback, never
    an addition.  Returns ``""`` when there is no name of either kind — the
    caller drops the row rather than storing a blank.
    """
    parts = [str(part).strip() for part in (first, last) if part is not None]
    personal = " ".join(part for part in parts if part)
    if personal:
        return personal
    if organisation is not None:
        org = str(organisation).strip()
        if org:
            return org
    return ""


def _record_columns(cursor: sqlite3.Cursor) -> set[str]:
    """The column names on ``ZABCDRECORD`` in this store, or an empty set.

    Checked rather than assumed.  ``ZORGANIZATION`` was present on every store
    checked when this was written, but the schema is
    Apple's, it has changed between macOS releases before, and a ``SELECT``
    naming a column that is not there fails the WHOLE query — phones, emails and
    all.  One cheap ``PRAGMA`` turns a total store loss into a missing fallback.
    """
    try:
        return {str(row[1]) for row in cursor.execute("PRAGMA table_info(ZABCDRECORD)")}
    except sqlite3.Error:
        return set()


def _read_store(path: Path, into: dict[str, str]) -> None:
    """Merge one store's phone and email rows into ``into``. Raises on failure.

    First non-empty name wins for a given canonical key, so the deterministic
    store order from :func:`store_paths` decides ties the same way every run.
    A row is skipped when :func:`canonicalise` returns ``None`` (not a handle) or
    when the record has no name of any kind — an entry with a blank name teaches
    the plug-in nothing and would still occupy the key.

    Phone rows carrying an ``@`` and email rows carrying none are skipped: the
    two key spaces must not cross-contaminate, because a mis-typed address in the
    phone column would otherwise mint a digits-only key for an email handle.

    The caller owns the error handling — see :func:`load_map`.
    """
    connection = sqlite3.connect(_ro_uri(path), uri=True, timeout=5.0)
    try:
        cursor = connection.cursor()
        columns = _record_columns(cursor)
        # No user input reaches this f-string: it is either a fixed column name
        # or the literal NULL, chosen by the PRAGMA above.
        org = "r.ZORGANIZATION" if "ZORGANIZATION" in columns else "NULL"

        phone_sql = (
            f"SELECT r.ZFIRSTNAME, r.ZLASTNAME, {org}, p.ZFULLNUMBER "
            "FROM ZABCDPHONENUMBER p JOIN ZABCDRECORD r ON r.Z_PK = p.ZOWNER"
        )
        email_sql = (
            f"SELECT r.ZFIRSTNAME, r.ZLASTNAME, {org}, e.ZADDRESSNORMALIZED "
            "FROM ZABCDEMAILADDRESS e JOIN ZABCDRECORD r ON r.Z_PK = e.ZOWNER"
        )

        for sql, wants_at in ((phone_sql, False), (email_sql, True)):
            for first, last, organisation, raw in cursor.execute(sql):
                if raw is None:
                    continue
                text = str(raw)
                if ("@" in text) != wants_at:
                    continue
                key = canonicalise(text)
                if not key:
                    continue
                name = _display_name(first, last, organisation)
                if not name:
                    continue
                into.setdefault(key, name)
    finally:
        connection.close()


def load_map(base: Path | str | None = None) -> dict[str, str]:
    """``{canonical_identifier: display_name}`` from every Contacts store on this Mac.

    Reads BOTH store locations (see :func:`store_paths`), opens each one
    ``mode=ro``, and merges phone numbers and email addresses into one dict keyed
    by :func:`canonicalise` — so a lookup works with whatever spelling the
    Messages database happens to hand over.

    **Never raises, and one bad store never hides the others.**  Each store is
    read inside its own guard: a store that is unreadable, locked, truncated or
    carrying a schema this code does not know costs exactly ONE stderr line
    naming that store, and the remaining stores are still read and still merged.
    That isolation is the point — the stores are per-account, so the single
    store most likely to be broken (a stale Exchange account, say) is also the
    one that matters least, and losing the whole address book over it would be
    absurd.

    A total failure returns ``{}`` with a warning.  Two other cases each warn
    once and return ``{}``: no store file found at all, and stores found but no
    entry loaded from any of them.  An empty map is indistinguishable from "this
    member knows nobody", so it is never silent.

    ``base`` overrides the folder the stores are looked for in.  **It exists so
    the robustness tests can point at a temp directory holding a deliberately
    broken store and never go anywhere near the member's real address book** —
    "what happens on a corrupt store" must be provable without reading private
    data to prove it.  Production callers pass nothing.
    """
    paths = store_paths(base)
    if not paths:
        root = DEFAULT_BASE if base is None else Path(base)
        _warn(
            f"no Contacts store found under {root}; texts from numbers you have "
            "saved will arrive without a name."
        )
        return {}

    names: dict[str, str] = {}
    for path in paths:
        try:
            _read_store(path, names)
        except (sqlite3.Error, OSError, ValueError, UnicodeError) as exc:
            # ONE line, naming the store, and on to the next one.
            _warn(
                f"contacts store skipped: {path} "
                f"({exc.__class__.__name__}: {exc}); the other stores were still read."
            )

    if not names:
        _warn(
            f"no Contacts entries loaded from {len(paths)} store(s); texts will "
            "arrive without names."
        )
    return names


if __name__ == "__main__":  # pragma: no cover - a member-facing self-check
    # Counts only. This module reads a member's real address book, so nothing it
    # prints may be a name, a number or an address.
    _map = load_map()
    _emails = sum(1 for _key in _map if "@" in _key)
    _e164 = sum(1 for _key in _map if _key.startswith("+"))
    print(f"contacts stores found: {len(store_paths())}")
    print(f"contacts entries: {len(_map)}")
    _bare = len(_map) - _emails - _e164
    print(f"  addresses: {_emails}   +E.164 numbers: {_e164}   bare digits: {_bare}")
