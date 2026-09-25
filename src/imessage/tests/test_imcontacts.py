"""The handle-identity suite — the canonical spelling, the label slug, the Contacts read.

``imcontacts.canonicalise`` mints the plug-in's PERMANENT ledger key and its
proposal-dedup key, so a rule that is wrong once is wrong forever: the ledger
re-ingests history it has already stamped, and every number mints a second
proposal card.  This suite is what holds that rule still.  It pins every branch
of the table, pins idempotency over a wide input set, and pins the member's ruling of
2026-09-24 — a number with no country code is American (``+1``) — together with
its two exemptions (a short code, and anything too long to be E.164) and the
reason for it: every phone the rule writes must pass the ENGINE's lint gate,
``people_norm.is_valid_phone``, or it could never be attached to a card.  The
engine is imported here, in the test only, so that gate is the real one; the
plug-in module itself imports no engine code.

It also runs the loader against the member's REAL Contacts stores, read-only,
because a loader that only ever sees a fixture is a loader nobody has tested.

PRIVACY — the rule this file is written under
----------------------------------------------
The live tests read a member's actual address book.  **Nothing in this file may
print a real name, a real number or a real address** — not into the test output,
not into a failure message.  Counts, lengths, key *shapes* and fully masked
samples only.  Every fixture handle below is invented: the ``555-01xx`` range is
the reserved fictional block, and the domains are ``example.com`` /
``example.org`` (RFC 2606).
"""

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_HOME))

# The plug-in's own modules first, before anything reachable only because
# imconfig put `.claude/scripts` on sys.path. The one engine module this suite
# imports, people_norm, comes after them, so a name collision can never let an
# engine module shadow a plug-in one.
import imconfig  # noqa: E402

imconfig.ensure_engine_path()

import imcontacts  # noqa: E402
import imthreads  # noqa: E402

# isort: split
# (A sorter would hoist the stdlib above the plug-in's modules; the order is a
# sys.path contract, not a style choice, so each group is fenced.)
import ast  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402

# isort: split
# The engine's lint gate and matcher: the real ones, not replays. Imported in
# the TEST only; imcontacts itself must never import engine code.
import people_norm  # noqa: E402

# The engine's pointer parser, copied verbatim from people_index.py:71. Copied
# rather than imported on purpose: this suite proves slugify_label survives THAT
# shape, and importing the engine module would make the proof depend on the
# engine being importable at all.
ENGINE_LINK_RE = re.compile(r"\(\s*→\s*([^)]+)\)")

#: The slug charset, asserted rather than assumed.
SLUG_RE = re.compile(r"^[a-z0-9-]+$")

#: The default floor the live Contacts read must clear, used by the negative
#: control.  The live test does not pin a number of contacts (it changes every
#: time the member saves one, and every member's address book is a different
#: size): it re-measures its floor from the stores themselves, see
#: :func:`_independent_named_handles`.
LIVE_MIN_ENTRIES = 100


def _check_live_bound(case, total, where, floor=LIVE_MIN_ENTRIES):
    """The live-count assertion, in ONE place so the negative control uses it too.

    Both the live test and :class:`TestLiveCountAssertionBites` call this, so the
    control proves the real assertion fails on an empty map rather than proving
    a look-alike written next to it.
    """
    case.assertGreater(
        total,
        floor,
        f"Contacts read {total} entries from {where}. At or below {floor} the "
        "loader is not really reading the stores (wrong glob, wrong schema, the "
        "Sources/* stores missed), and every texter would arrive nameless.",
    )


def _independent_named_handles(paths):
    """``(readable stores, distinct raw handles on a named record)``, the test's own SQL.

    Counted WITHOUT the loader, so the live floor is re-measured from each
    member's own address book rather than pinned to one member's figure.  Every
    raw phone and address string whose record carries a first name, a last name
    or an organisation is counted once, stripped.  That set is a SUPERSET of what
    ``load_map`` keys (it also drops non-handles, folds spellings of one line and
    keeps the first name for a key), so the loader can never exceed it and a
    loader that really reads the stores keeps most of it.  A store this process
    cannot open or query (no Full Disk Access) is not counted as readable.
    """
    readable, handles = 0, set()
    for path in paths:
        try:
            con = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True,
                                  timeout=5.0)
        except sqlite3.Error:
            continue
        try:
            columns = {str(row[1]) for row in con.execute("PRAGMA table_info(ZABCDRECORD)")}
            if not columns:
                continue
            org = "r.ZORGANIZATION" if "ZORGANIZATION" in columns else "NULL"
            named = (
                "(TRIM(IFNULL(r.ZFIRSTNAME, '')) <> '' OR TRIM(IFNULL(r.ZLASTNAME, '')) <> '' "
                f"OR TRIM(IFNULL({org}, '')) <> '')"
            )
            found = set()
            for table, column in (("ZABCDPHONENUMBER", "ZFULLNUMBER"),
                                  ("ZABCDEMAILADDRESS", "ZADDRESSNORMALIZED")):
                for (raw,) in con.execute(
                    f"SELECT x.{column} FROM {table} x JOIN ZABCDRECORD r ON r.Z_PK = x.ZOWNER "
                    f"WHERE x.{column} IS NOT NULL AND {named}"
                ):
                    found.add(str(raw).strip())
        except sqlite3.Error:
            continue
        finally:
            con.close()
        readable += 1
        handles |= found
    return readable, len(handles)


def _mask(value):
    """A shape, never a value — for output that is allowed to be looked at.

    ``+12025550123`` becomes ``+1<10 digits>``; an address becomes
    ``<local 5>@<domain 11>``.  Enough to see the loader produced the right KIND
    of key, with nothing identifying left in it.
    """
    text = str(value)
    if "@" in text:
        local, _, domain = text.partition("@")
        return f"<local {len(local)}>@<domain {len(domain)}>"
    if text.startswith("+"):
        return f"+<{len(text) - 1} digits>"
    return f"<{len(text)} bare digits>"


# ---------------------------------------------------------------------------
# Fixture stores — built on disk, never the member's real address book.
# ---------------------------------------------------------------------------

_GOOD_SCHEMA = """
CREATE TABLE ZABCDRECORD (
    Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZORGANIZATION TEXT
);
CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZFULLNUMBER TEXT);
CREATE TABLE ZABCDEMAILADDRESS (
    Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZADDRESSNORMALIZED TEXT
);
"""

# The same store WITHOUT ZORGANIZATION — an older/other Apple schema. The loader
# must still read it, minus the organisation fallback.
_NO_ORG_SCHEMA = """
CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZLASTNAME TEXT);
CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZFULLNUMBER TEXT);
CREATE TABLE ZABCDEMAILADDRESS (
    Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZADDRESSNORMALIZED TEXT
);
"""


def _write_store(path, schema, records=(), phones=(), emails=()):
    """Write a fixture Contacts store at ``path``. Invented data only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.executescript(schema)
        width = len(records[0]) if records else 0
        if width:
            placeholders = ",".join("?" * width)
            con.executemany(f"INSERT INTO ZABCDRECORD VALUES ({placeholders})", records)
        con.executemany("INSERT INTO ZABCDPHONENUMBER VALUES (?,?,?)", phones)
        con.executemany("INSERT INTO ZABCDEMAILADDRESS VALUES (?,?,?)", emails)
        con.commit()
    finally:
        con.close()


def _capture_warnings(fn, *args, **kwargs):
    """Run ``fn`` and hand back ``(result, stderr_text)``.

    ``imcontacts._warn`` resolves ``sys.stderr`` at call time, so redirecting it
    catches every line. Used to prove a bad store warns ONCE and is named.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        result = fn(*args, **kwargs)
    return result, buffer.getvalue()


# ---------------------------------------------------------------------------
# canonicalise
# ---------------------------------------------------------------------------


#: Fictional handles for the member's ruling. The UK number is in Ofcom's reserved
#: drama block (07700 900xxx); every other digit string is invented.
UK_NATIONAL = "07700900123"
UK_WITH_COUNTRY_CODE = "+447700900123"
LOCAL_SEVEN = "5550123"
SHORTCODE_SIX = "262966"
FOURTEEN_DIGITS = "55501234567890"  # "+1" + 14 = 15 digits, E.164's ceiling
FIFTEEN_DIGITS = "555012345678901"  # "+1" + 15 = 16 digits, past it
LONG_ID_16 = "5555550101234567"  # the shape of a real 16-digit sender ID
LONG_ID_25 = "5555550101234567890123456"  # the shape of a real 25-digit one


class TestCanonicaliseTable(unittest.TestCase):
    """Every branch of the rule table, spelled out as (input, expected)."""

    CASES = (
        # --- nothing at all -> None
        (None, None, "None is not a handle"),
        ("", None, "empty is not a handle"),
        ("   ", None, "whitespace is not a handle"),
        ("\t\n ", None, "whitespace of any kind is not a handle"),
        ("+", None, "a lone + carries no digits"),
        ("---", None, "punctuation carries no digits"),
        # --- emails: lowercase + strip, the FIRST branch
        ("Someone@Example.COM", "someone@example.com", "an address lowercases"),
        ("  someone@example.com  ", "someone@example.com", "an address strips"),
        ("SOMEONE+tag@Example.com", "someone+tag@example.com", "the +tag is NOT stripped here"),
        ("+someone@example.com", "+someone@example.com", "@ beats the leading + branch"),
        # --- already E.164: keep the +, keep only digits
        ("+12025550123", "+12025550123", "a clean E.164 is unchanged"),
        ("+1 (202) 555-0123", "+12025550123", "E.164 loses its punctuation"),
        ("+44 7700 900123", "+447700900123", "a UK E.164 keeps its own country code"),
        ("+447700900123", "+447700900123", "a clean UK E.164 is unchanged"),
        ("+61 2 5550 1234", "+61255501234", "an AU E.164 is untouched by the +1 rule"),
        # --- exactly 10 digits -> +1
        ("2025550123", "+12025550123", "10 bare digits are North American"),
        ("202-555-0123", "+12025550123", "hyphens do not change the answer"),
        ("(202) 555-0123", "+12025550123", "brackets and spaces do not change the answer"),
        ("202.555.0123", "+12025550123", "dots do not change the answer"),
        # --- exactly 11 digits beginning with 1 -> + (it already has its 1)
        ("12025550123", "+12025550123", "11 digits starting 1 is North American"),
        ("1-202-555-0123", "+12025550123", "the long-distance 1 is recognised"),
        # --- the member's ruling, 2026-09-24: no country code means US, at ANY other length
        (UK_NATIONAL, "+107700900123", "an 11-digit number not starting 1 is read as US"),
        ("07700 900123", "+107700900123", "…keeping every digit, punctuation dropped"),
        ("447700900123", "+1447700900123", "12 digits without a + are read as US too"),
        (LOCAL_SEVEN, "+15550123", "7 digits, the shortest non-short-code, get +1"),
        ("55501234", "+155501234", "8 digits get +1"),
        (FOURTEEN_DIGITS, "+1" + FOURTEEN_DIGITS, "14 digits get +1: 15, E.164's ceiling"),
        (
            "0044 7700 900123",
            "+100447700900123",
            "a 00-prefixed international is NOT recognised; it is a bare number like any other",
        ),
        # --- exemption (a): a short code stays bare, so the short-code floor holds
        (SHORTCODE_SIX, SHORTCODE_SIX, "a 6-digit short code stays bare"),
        ("22395", "22395", "a 5-digit short code stays bare"),
        ("1", "1", "a single digit stays bare"),
        # --- exemption (b): +1 would pass E.164's 15 digits, so it is no phone
        (FIFTEEN_DIGITS, FIFTEEN_DIGITS, "15 digits stay bare: +1 would make 16"),
        (LONG_ID_16, LONG_ID_16, "a 16-digit sender ID stays bare"),
        (LONG_ID_25, LONG_ID_25, "a 25-digit sender ID stays bare"),
    )

    def test_table(self):
        for raw, expected, why in self.CASES:
            with self.subTest(why=why):
                self.assertEqual(
                    imcontacts.canonicalise(raw),
                    expected,
                    f"canonicalise({raw!r}) must be {expected!r} — {why}. This value is "
                    "the permanent ledger key; a change here re-ingests history and "
                    "duplicates every proposal card.",
                )

    def test_a_stray_non_string_does_not_raise(self):
        """A database column can hand back an int. It must cost a value, not a crash."""
        self.assertEqual(imcontacts.canonicalise(2025550123), "+12025550123")


class TestTheRulingNoCountryCodeMeansUS(unittest.TestCase):
    """The member's ruling, 2026-09-24: *"just assume us country code if there isn't one."*

    This class REPLACES the fence that held the opposite rule (an 11-digit
    non-US number left bare).  It pins the new rule on the same UK number that
    fence used, so the reversal is visible in one place.  The old fence's
    negative control keeps a new job: the reference measurement rule
    (``"+1" + digits[-10:]``, written out in the test below) also produced a ``+1``
    key for this number, but by DROPPING a digit (``[-10:]`` eats the trunk ``0``).
    The ruling adds ``+1`` and keeps every digit, and the two must never be
    mistaken for each other.
    """

    #: What the reference rule really produces for UK_NATIONAL, re-derived below
    #: every run so this control can never drift onto a value it does not make.
    REFERENCE_BUG = "+17700900123"

    def test_a_uk_national_number_is_read_as_us(self):
        got = imcontacts.canonicalise(UK_NATIONAL)
        self.assertEqual(
            got,
            "+107700900123",
            f"{UK_NATIONAL!r} -> {got!r}. The ruling is that a number with no country "
            "code is American, so it gains +1 and keeps all eleven of its digits.",
        )

    def test_the_new_rule_is_not_the_reference_bug(self):
        """The negative control: the reference rule really does drop a digit."""
        digits = re.sub(r"\D", "", UK_NATIONAL)
        reference = "+1" + digits[-10:] if len(digits) >= 10 else UK_NATIONAL
        self.assertEqual(
            reference,
            self.REFERENCE_BUG,
            "The reference rule no longer produces the value this control pins "
            f"(it produced {reference!r}); re-read the reference rule written out above.",
        )
        got = imcontacts.canonicalise(UK_NATIONAL)
        self.assertNotEqual(
            got,
            reference,
            "canonicalise has regressed onto the reference rule, which silently eats "
            "the national trunk 0 and so keys a different number.",
        )
        self.assertEqual(got, "+1" + digits, "+1 is ADDED in front of every digit")

    def test_no_digit_is_ever_dropped(self):
        """Every branch keeps every digit it was given; +1 only ever adds a 1."""
        for raw, _expected, why in TestCanonicaliseTable.CASES:
            if raw is None or "@" in str(raw):
                continue
            given = re.sub(r"\D", "", str(raw))
            if not given:
                continue
            with self.subTest(why=why):
                kept = re.sub(r"\D", "", imcontacts.canonicalise(raw))
                self.assertIn(
                    kept,
                    (given, "1" + given),
                    f"canonicalise({raw!r}) kept digits {kept!r} from {given!r}: a "
                    "dropped or reordered digit keys a different line.",
                )

    def test_the_trade_the_member_accepted_is_what_the_docstring_says(self):
        """A non-US number saved WITHOUT its country code no longer reaches its card.

        Checked with the ENGINE's own matcher, not a replay.  If this ever goes
        red the engine's ``same_phone`` has changed and the imcontacts docstring's
        statement of the trade is out of date.
        """
        self.assertFalse(
            people_norm.same_phone(imcontacts.canonicalise(UK_NATIONAL), UK_WITH_COUNTRY_CODE),
            "the +1 reading of a UK national number now matches a card storing the "
            "+44 form; update the trade described in imcontacts' docstring.",
        )
        # The same number saved WITH its country code is untouched and reaches it.
        with_code = imcontacts.canonicalise(UK_WITH_COUNTRY_CODE)
        self.assertEqual(with_code, UK_WITH_COUNTRY_CODE)
        self.assertTrue(people_norm.is_valid_phone(with_code))
        self.assertTrue(people_norm.same_phone(with_code, UK_WITH_COUNTRY_CODE))


class TestCanonicaliseCollapsesEverySpellingOfOneLine(unittest.TestCase):
    """Every spelling of one North American line must be ONE string."""

    SPELLINGS = (
        "202-555-0123",
        "(202) 555-0123",
        "+12025550123",
        "12025550123",
        "2025550123",
    )

    def test_every_spelling_produces_one_identical_string(self):
        results = {imcontacts.canonicalise(s) for s in self.SPELLINGS}
        self.assertEqual(
            len(results),
            1,
            "The spellings of one line produced more than one canonical string "
            f"({sorted(results)}). That is one person with two ledger keys, two "
            "proposal cards, and a conversation history split down the middle.",
        )
        self.assertEqual(results.pop(), "+12025550123")

    def test_extra_spellings_of_the_same_line_agree_too(self):
        more = ("1 202 555 0123", "+1 202-555-0123", "202.555.0123", " 2025550123 ")
        expected = imcontacts.canonicalise(self.SPELLINGS[0])
        for spelling in more:
            with self.subTest(spelling=spelling):
                self.assertEqual(imcontacts.canonicalise(spelling), expected)


class TestCanonicaliseIsIdempotent(unittest.TestCase):
    """``canonicalise(canonicalise(x)) == canonicalise(x)`` — for everything.

    The stored value is read back out of the ledger, out of a proposal card and
    out of ``config.local.json`` and re-normalised on the way in.  A rule that
    shifted on a second pass would split one person into two keys on the second
    run, which is the hardest class of bug to see.
    """

    INPUTS = (
        [raw for raw, _expected, _why in TestCanonicaliseTable.CASES]
        + list(TestCanonicaliseCollapsesEverySpellingOfOneLine.SPELLINGS)
        + [
            TestTheRulingNoCountryCodeMeansUS.REFERENCE_BUG,  # the reference rule's key
            "+107700900123",  # the ruling's key for the same UK number
            "+10700900123",  # the mangled value the original brief quoted
            UK_NATIONAL,
            UK_WITH_COUNTRY_CODE,
            LOCAL_SEVEN,
            SHORTCODE_SIX,
            "+1" + SHORTCODE_SIX,
            FOURTEEN_DIGITS,
            FIFTEEN_DIGITS,
            LONG_ID_16,
            LONG_ID_25,
            "0044 7700 900123",
            "00447700900123",
            "+00447700900123",
            "1",
            "12",
            "1234567890123456789",
            "++12025550123",
            "+1-202-555-0123 ext 9",
            "someone@example.org",
            "SOMEONE@EXAMPLE.ORG",
            "a@b.c",
            "@",
            "not a handle at all",
            "  +44 (0)7700 900123  ",
            2025550123,
        ]
    )

    def test_second_pass_changes_nothing(self):
        for raw in self.INPUTS:
            with self.subTest(raw=raw):
                once = imcontacts.canonicalise(raw)
                twice = imcontacts.canonicalise(once)
                self.assertEqual(
                    twice,
                    once,
                    f"canonicalise is not idempotent for {raw!r}: {once!r} -> {twice!r}. "
                    "The same handle would key differently on a second run.",
                )

    def test_three_passes_change_nothing_either(self):
        for raw in self.INPUTS:
            with self.subTest(raw=raw):
                once = imcontacts.canonicalise(raw)
                thrice = imcontacts.canonicalise(imcontacts.canonicalise(once))
                self.assertEqual(thrice, once)

    def test_no_result_is_ever_an_empty_string(self):
        """An empty key would be one bucket shared by every piece of junk."""
        for raw in self.INPUTS:
            with self.subTest(raw=raw):
                got = imcontacts.canonicalise(raw)
                self.assertNotEqual(got, "", f"canonicalise({raw!r}) returned ''.")
                if got is not None:
                    self.assertTrue(got.strip(), f"canonicalise({raw!r}) returned blank.")


def _is_bare(key):
    """A canonical key that is neither an address nor ``+``-prefixed."""
    return key is not None and "@" not in key and not key.startswith("+")


class TestEveryPhoneTheRuleWritesPassesTheEngineLintGate(unittest.TestCase):
    """The WHY of the ruling, checked against the engine's REAL gate.

    ``people_norm.is_valid_phone`` accepts only ``+country`` E.164.  A key that
    fails it can never be attached to a person's card, so a phone the rule
    writes and the gate refuses is a number stuck in review forever.
    """

    PHONE_SHAPED = (
        UK_NATIONAL,
        LOCAL_SEVEN,
        "55501234",
        "202-555-0123",
        "(202) 555-0123",
        "+12025550123",
        "12025550123",
        "2025550123",
        "447700900123",
        FOURTEEN_DIGITS,
    )
    NOT_PHONES = (LONG_ID_16, LONG_ID_25)

    def test_every_phone_shaped_output_passes_the_engine_gate(self):
        for raw in self.PHONE_SHAPED:
            got = imcontacts.canonicalise(raw)
            with self.subTest(raw=raw):
                self.assertTrue(
                    people_norm.is_valid_phone(got),
                    f"canonicalise({raw!r}) = {got!r}, which the engine's lint gate "
                    "refuses, so it could never be attached to a card.",
                )

    def test_the_two_long_ids_stay_bare_and_the_gate_refuses_them(self):
        for raw in self.NOT_PHONES:
            got = imcontacts.canonicalise(raw)
            with self.subTest(length=len(raw)):
                self.assertEqual(got, raw, "a sender ID too long for E.164 stays bare")
                self.assertFalse(people_norm.is_valid_phone(got))
                # …and +1 could not have rescued it: the gate refuses that too.
                self.assertFalse(people_norm.is_valid_phone("+1" + raw))

    def test_the_length_ceiling_is_exactly_the_engines(self):
        """``E164_MAX_DIGITS`` sits where the engine's gate does, proved at the edge."""
        self.assertEqual(len("1" + FOURTEEN_DIGITS), imcontacts.E164_MAX_DIGITS)
        self.assertTrue(
            people_norm.is_valid_phone("+1" + FOURTEEN_DIGITS),
            "15 digits after the + must pass, or the ceiling is set too high",
        )
        self.assertFalse(
            people_norm.is_valid_phone("+1" + FIFTEEN_DIGITS),
            "16 digits after the + must fail, or the ceiling is set too low",
        )

    def test_every_number_the_rule_prefixes_passes_and_only_the_exemptions_stay_bare(self):
        """Over the whole idempotence input set: the two invariants, together."""
        for raw in TestCanonicaliseIsIdempotent.INPUTS:
            got = imcontacts.canonicalise(raw)
            if got is None or "@" in got:
                continue
            text = str(raw).strip()
            with self.subTest(raw=raw):
                if _is_bare(got):
                    self.assertTrue(
                        len(got) < imcontacts.SHORTCODE_MIN_DIGITS
                        or len(got) + 1 > imcontacts.E164_MAX_DIGITS,
                        f"canonicalise({raw!r}) left {len(got)} digits bare, but only a "
                        "short code or a too-long ID may stay bare under the ruling.",
                    )
                elif not text.startswith("+"):
                    self.assertTrue(
                        people_norm.is_valid_phone(got),
                        f"the rule prefixed {raw!r} into {got!r}, which the engine refuses",
                    )


class TestTheShortcodeFloorIsUnchanged(unittest.TestCase):
    """``+1`` must not move the short-code floor in ``imthreads``.

    ``imthreads.is_shortcode`` strips a leading ``+`` and counts what is left, so
    the exemption in ``canonicalise`` is what keeps a 6-digit bank or 2FA sender
    recognised as a short code rather than a person.
    """

    def test_the_threshold_is_the_one_imthreads_uses(self):
        self.assertEqual(
            imcontacts.SHORTCODE_MIN_DIGITS,
            imthreads.SHORTCODE_MIN_DIGITS,
            "imcontacts defines its own copy of the short-code threshold (importing "
            "imthreads would be a cycle); the two must never drift apart.",
        )

    def test_the_verdict_at_every_length_is_what_it_was_before_the_ruling(self):
        """The old rule prefixed only 10- and 11-digit numbers, so the verdict was
        ``length < 7`` at every length. It still must be."""
        for length in range(1, 26):
            for raw in ("5" * length, "1" + "5" * (length - 1)):
                got = imcontacts.canonicalise(raw)
                with self.subTest(length=length, leading=raw[0]):
                    self.assertEqual(
                        imthreads.is_shortcode(got),
                        length < imthreads.SHORTCODE_MIN_DIGITS,
                        f"a {length}-digit bare number canonicalised to {_mask(got)}, "
                        "and the short-code verdict on it moved.",
                    )

    def test_without_the_exemption_a_short_code_would_stop_being_one(self):
        """The negative control: the exemption is load-bearing, not tidy-up."""
        self.assertTrue(imthreads.is_shortcode(imcontacts.canonicalise(SHORTCODE_SIX)))
        self.assertFalse(
            imthreads.is_shortcode("+1" + SHORTCODE_SIX),
            "if +1 plus a 6-digit short code still counted as a short code, the "
            "exemption would be pointless and this control pins nothing.",
        )

    def test_imcontacts_imports_neither_imthreads_nor_the_engine(self):
        """No cycle (imthreads imports imcontacts) and no engine code in the plug-in."""
        tree = ast.parse((PLUGIN_HOME / "imcontacts.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        outside = sorted(
            name for name in imported if name not in sys.stdlib_module_names and name != "imconfig"
        )
        self.assertEqual(
            outside,
            [],
            "imcontacts may import only the standard library and imconfig: "
            "imthreads would be an import cycle and an engine module would break the "
            "plug-in's one-engine-importer rule (imspine).",
        )


# ---------------------------------------------------------------------------
# slugify_label
# ---------------------------------------------------------------------------


class TestSlugifyLabel(unittest.TestCase):
    """A label must survive a filename AND the engine's ``(→ …)`` parser."""

    CASES = (
        ("Mom (cell)", "mom-cell", "the bracket case — the one that corrupts the link"),
        ("Garden Group (2026)", "garden-group-2026", "brackets around a year"),
        ("Home/Work", "home-work", "a slash would break the path"),
        ("Work: mobile", "work-mobile", "a colon would break the path"),
        ("!!!", None, "entirely punctuation falls back"),
        ("", "label", "empty falls back to the bare fallback"),
        ("   ", "label", "whitespace falls back to the bare fallback"),
        (None, "label", "None falls back to the bare fallback"),
        ("iPhone", "iphone", "case folds"),
        ("  spaced  out  ", "spaced-out", "runs collapse and the ends are trimmed"),
        ("a---b", "a-b", "a run of dashes collapses to one"),
        ("-leading-and-trailing-", "leading-and-trailing", "the ends are trimmed"),
        ("José's Mobile", "jose-s-mobile", "accents fold rather than being minced"),
        ("Zoë (work)", "zoe-work", "accents and brackets together"),
        ("2026", "2026", "digits survive"),
    )

    def test_table(self):
        for raw, expected, why in self.CASES:
            with self.subTest(why=why):
                got = imcontacts.slugify_label(raw)
                if expected is not None:
                    self.assertEqual(got, expected, f"slugify_label({raw!r}) — {why}")
                self.assertTrue(
                    SLUG_RE.match(got),
                    f"slugify_label({raw!r}) returned {got!r}, which is outside "
                    "[a-z0-9-]. That value reaches a filename and a card link.",
                )

    def test_output_is_never_empty(self):
        for raw in [raw for raw, _e, _w in self.CASES] + ["…", "🙂", "()", "---", "/", ":"]:
            with self.subTest(raw=raw):
                got = imcontacts.slugify_label(raw)
                self.assertTrue(
                    got,
                    f"slugify_label({raw!r}) returned an empty string. An empty "
                    "filename component silently collides with every other one.",
                )

    def test_a_label_that_reduces_to_nothing_is_still_stable_and_distinct(self):
        """Two different punctuation-only labels must not share one filename."""
        a1 = imcontacts.slugify_label("!!!")
        a2 = imcontacts.slugify_label("!!!")
        b = imcontacts.slugify_label("???")
        self.assertEqual(a1, a2, "the fallback must be stable across calls")
        self.assertNotEqual(a1, b, "two distinct labels must not collapse to one slug")
        self.assertTrue(SLUG_RE.match(a1) and SLUG_RE.match(b))

    def test_length_is_capped_and_never_ends_in_a_dash(self):
        long_label = "Garden Group Quarterly Planning Meeting With Everybody Involved 2026"
        got = imcontacts.slugify_label(long_label)
        self.assertLessEqual(len(got), imcontacts.MAX_LABEL_SLUG_LEN)
        self.assertFalse(got.startswith("-"))
        self.assertFalse(got.endswith("-"))
        # A cap that lands exactly on a separator must not leave the dash behind.
        on_a_boundary = "a" * (imcontacts.MAX_LABEL_SLUG_LEN - 1) + " b"
        self.assertFalse(imcontacts.slugify_label(on_a_boundary).endswith("-"))

    def test_slugify_is_idempotent(self):
        for raw, _expected, _why in self.CASES:
            with self.subTest(raw=raw):
                once = imcontacts.slugify_label(raw)
                self.assertEqual(imcontacts.slugify_label(once), once)


class TestSlugSurvivesTheEngineLinkParser(unittest.TestCase):
    """The reason ``slugify_label`` exists, proved against the engine's own regex.

    ``people_index.py:71`` parses the pointer with ``r"\\(\\s*→\\s*([^)]+)\\)"``.
    The class stops at the first ``)``, so a raw bracketed label truncates the
    stored link and spills the filename's tail into the card's visible summary.
    """

    BRACKETED = ("Mom (cell)", "Garden Group (2026)")

    def test_a_raw_bracketed_label_really_does_truncate(self):
        """The negative control. Without this, the test below proves nothing."""
        for raw in self.BRACKETED:
            with self.subTest(raw=raw):
                intended = f"texts/{raw}.md"
                # The writer's shape, people_stamp.py:142.
                line = f"texted (→ {intended})"
                match = ENGINE_LINK_RE.search(line)
                self.assertIsNotNone(match, "the parser should still find a link")
                captured = match.group(1)
                self.assertNotEqual(
                    captured,
                    intended,
                    "The raw label no longer truncates under the engine parser, so "
                    "this control is pinned to nothing — re-read people_index.py:71.",
                )
                # Harm 1: the stored link is a truncated PREFIX of the real path,
                # cut at the label's first ')'. It still looks like a link.
                self.assertTrue(
                    intended.startswith(captured),
                    f"expected a truncated prefix of {intended!r}, got {captured!r}",
                )
                self.assertNotIn(")", captured, "the class stops at the first ')'")
                self.assertIn("(", captured, "…leaving the opening bracket dangling")
                # Harm 2: people_index.py:820 strips the matched link from the
                # visible summary — so whatever the match did NOT cover is left
                # sitting in the card's text.
                leftover = ENGINE_LINK_RE.sub("", line)
                self.assertIn(
                    ".md)",
                    leftover,
                    "the filename's tail must be shown leaking into the card summary, "
                    f"or this control understates the harm (leftover: {leftover!r})",
                )

    def test_the_slug_round_trips_through_the_parser_whole(self):
        for raw in self.BRACKETED + ("Home/Work", "Work: mobile", "!!!", "José's Mobile"):
            with self.subTest(raw=raw):
                link = f"texts/{imcontacts.slugify_label(raw)}.md"
                match = ENGINE_LINK_RE.search(f"texted (→ {link})")
                self.assertIsNotNone(match)
                self.assertEqual(
                    match.group(1),
                    link,
                    "The slugged link did not survive the engine's pointer parser "
                    "whole, so the card would carry a corrupted link.",
                )

    def test_the_slug_is_a_safe_path_component(self):
        for raw in ("Home/Work", "Work: mobile", "..", "../../etc", "C:\\x"):
            with self.subTest(raw=raw):
                slug = imcontacts.slugify_label(raw)
                for bad in ("/", "\\", ":", "..", " "):
                    self.assertNotIn(bad, slug, f"{slug!r} is not a safe path component")


# ---------------------------------------------------------------------------
# load_map — robustness, on fixture stores only.
# ---------------------------------------------------------------------------


class TestLoadMapRobustness(unittest.TestCase):
    """A broken store costs one warning, never a crash, and never the others.

    Every store here is built in a temp directory. ``load_map``'s ``base``
    parameter exists precisely so "what happens on a corrupt store" can be proved
    without reading the member's real address book to prove it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _good_store(self, path):
        _write_store(
            path,
            _GOOD_SCHEMA,
            records=[
                (1, "Ada", "Lovelace", None),
                (2, None, None, "Example Repair Shop"),  # organisation-only
                (3, None, None, None),  # no name of any kind
            ],
            phones=[
                (1, 1, "(555) 010-1234"),
                (2, 2, "+1 555 010 5678"),
                (3, 3, "5550109999"),  # nameless -> dropped
                (4, 1, "07700900123"),  # UK national on the same person
            ],
            emails=[(1, 1, "ada@example.com"), (2, 2, "lab@example.org")],
        )

    # -- (a) no stores at all ------------------------------------------------

    def test_no_stores_returns_empty_with_one_warning(self):
        result, err = _capture_warnings(imcontacts.load_map, self.base)
        self.assertEqual(result, {})
        self.assertEqual(
            err.count("no Contacts store found under"),
            1,
            f"expected exactly one 'no store' warning, got:\n{err}",
        )
        self.assertIn(str(self.base), err, "the warning must name where it looked")

    def test_store_paths_on_an_empty_base_is_empty_not_an_error(self):
        self.assertEqual(imcontacts.store_paths(self.base), [])

    # -- (b) a zero-byte file named like a store ------------------------------

    def test_zero_byte_store_warns_once_and_does_not_raise(self):
        (self.base / "Sources" / "AAA").mkdir(parents=True)
        decoy = self.base / "Sources" / "AAA" / imcontacts.STORE_FILENAME
        decoy.write_bytes(b"")
        result, err = _capture_warnings(imcontacts.load_map, self.base)
        self.assertEqual(result, {})
        self.assertEqual(
            err.count("contacts store skipped:"),
            1,
            f"expected exactly one 'skipped' warning, got:\n{err}",
        )
        self.assertIn(str(decoy), err, "the warning must NAME the store it skipped")

    # -- (c) a valid SQLite file with the wrong schema ------------------------

    def test_wrong_schema_store_warns_once_and_does_not_raise(self):
        wrong = self.base / "Sources" / "BBB" / imcontacts.STORE_FILENAME
        wrong.parent.mkdir(parents=True)
        con = sqlite3.connect(str(wrong))
        con.execute("CREATE TABLE something_else (id INTEGER, note TEXT)")
        con.execute("INSERT INTO something_else VALUES (1, 'not contacts')")
        con.commit()
        con.close()

        result, err = _capture_warnings(imcontacts.load_map, self.base)
        self.assertEqual(result, {})
        self.assertEqual(err.count("contacts store skipped:"), 1, err)
        self.assertIn(str(wrong), err)

    def test_a_non_sqlite_file_named_like_a_store_warns_once(self):
        junk = self.base / "Sources" / "CCC" / imcontacts.STORE_FILENAME
        junk.parent.mkdir(parents=True)
        junk.write_bytes(b"this is not a database at all, not even close\n" * 40)
        result, err = _capture_warnings(imcontacts.load_map, self.base)
        self.assertEqual(result, {})
        self.assertEqual(err.count("contacts store skipped:"), 1, err)

    # -- one bad store must not hide the others -------------------------------

    def test_a_bad_store_never_hides_a_good_one(self):
        bad = self.base / "Sources" / "AAA" / imcontacts.STORE_FILENAME
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"")
        self._good_store(self.base / "Sources" / "BBB" / imcontacts.STORE_FILENAME)

        result, err = _capture_warnings(imcontacts.load_map, self.base)
        self.assertEqual(err.count("contacts store skipped:"), 1, err)
        self.assertEqual(
            result.get("+15550101234"),
            "Ada Lovelace",
            f"the good store's rows were lost when a sibling store was broken: {result}",
        )
        self.assertNotIn(
            "no Contacts entries loaded",
            err,
            "a run that DID load entries must not also claim it loaded none",
        )

    def test_the_top_level_store_is_read_as_well_as_sources(self):
        """The top-level store holds a couple of records; reading only it looks 'empty'."""
        self._good_store(self.base / "Sources" / "AAA" / imcontacts.STORE_FILENAME)
        _write_store(
            self.base / imcontacts.STORE_FILENAME,
            _GOOD_SCHEMA,
            records=[(1, "Grace", "Hopper", None)],
            phones=[(1, 1, "555-010-4321")],
        )
        paths = imcontacts.store_paths(self.base)
        self.assertEqual(len(paths), 2, f"both store locations must be globbed: {paths}")
        self.assertEqual(paths[-1].parent, self.base, "the top-level store is read last")

        result, _err = _capture_warnings(imcontacts.load_map, self.base)
        self.assertEqual(result.get("+15550104321"), "Grace Hopper")
        self.assertEqual(result.get("+15550101234"), "Ada Lovelace")

    def test_sources_are_read_in_a_deterministic_order(self):
        """Ties are broken the same way every run, or the name flips between runs."""
        for folder in ("ZZZ", "AAA", "MMM"):
            _write_store(
                self.base / "Sources" / folder / imcontacts.STORE_FILENAME,
                _GOOD_SCHEMA,
                records=[(1, folder.title(), "Duplicate", None)],
                phones=[(1, 1, "555-010-7777")],
            )
        first = imcontacts.load_map(self.base)
        second = imcontacts.load_map(self.base)
        self.assertEqual(first, second)
        self.assertEqual(
            first.get("+15550107777"),
            "Aaa Duplicate",
            "the sorted-first source must win the tie",
        )

    # -- what the loader actually extracts ------------------------------------

    def test_organisation_only_records_get_a_name(self):
        self._good_store(self.base / "Sources" / "AAA" / imcontacts.STORE_FILENAME)
        result = imcontacts.load_map(self.base)
        self.assertEqual(
            result.get("+15550105678"),
            "Example Repair Shop",
            "a record with no first/last but an organisation must still be named, or "
            "every business in the member's phone mints a 'who is this?' proposal.",
        )
        self.assertEqual(result.get("lab@example.org"), "Example Repair Shop")

    def test_a_record_with_no_name_of_any_kind_is_dropped(self):
        self._good_store(self.base / "Sources" / "AAA" / imcontacts.STORE_FILENAME)
        result = imcontacts.load_map(self.base)
        self.assertNotIn(
            "+15550109999",
            result,
            "a nameless record must not occupy a key — it teaches nothing and blocks "
            "a later store that does know the name.",
        )

    def test_both_phones_and_emails_are_read(self):
        self._good_store(self.base / "Sources" / "AAA" / imcontacts.STORE_FILENAME)
        result = imcontacts.load_map(self.base)
        self.assertEqual(result.get("+15550101234"), "Ada Lovelace", "phones")
        self.assertEqual(result.get("ada@example.com"), "Ada Lovelace", "emails")
        self.assertEqual(
            result.get("+107700900123"),
            "Ada Lovelace",
            "a number saved without a country code must key as +1 plus every one of "
            "its digits (the member's ruling, 2026-09-24)",
        )
        self.assertNotIn(UK_NATIONAL, result, "the pre-ruling bare key is back")
        self.assertNotIn(
            TestTheRulingNoCountryCodeMeansUS.REFERENCE_BUG,
            result,
            "the reference rule's digit-dropping key is back",
        )

    def test_a_schema_without_the_organisation_column_still_reads(self):
        """The organisation column is CHECKED, not assumed; its absence is not fatal."""
        store = self.base / "Sources" / "AAA" / imcontacts.STORE_FILENAME
        _write_store(
            store,
            _NO_ORG_SCHEMA,
            records=[(1, "Ada", "Lovelace"), (2, None, None)],
            phones=[(1, 1, "555-010-1234"), (2, 2, "555-010-5678")],
            emails=[(1, 1, "ada@example.com")],
        )
        result, err = _capture_warnings(imcontacts.load_map, self.base)
        self.assertEqual(err.count("contacts store skipped:"), 0, f"unexpected skip:\n{err}")
        self.assertEqual(
            result.get("+15550101234"),
            "Ada Lovelace",
            "a store without ZORGANIZATION must still yield its named records — the "
            "column check exists so a missing column costs a fallback, not the store.",
        )
        self.assertNotIn("+15550105678", result)

    def test_every_key_it_returns_is_already_canonical(self):
        self._good_store(self.base / "Sources" / "AAA" / imcontacts.STORE_FILENAME)
        for key in imcontacts.load_map(self.base):
            with self.subTest(key=_mask(key)):
                self.assertEqual(imcontacts.canonicalise(key), key)

    def test_load_map_never_raises_on_a_base_that_is_not_a_directory(self):
        not_a_dir = self.base / "nope.txt"
        not_a_dir.write_text("x", encoding="utf-8")
        result, err = _capture_warnings(imcontacts.load_map, not_a_dir)
        self.assertEqual(result, {})
        self.assertIn("no Contacts store found under", err)


# ---------------------------------------------------------------------------
# The live read — the member's real stores, READ-ONLY, counts only.
# ---------------------------------------------------------------------------


class TestLiveContactsStores(unittest.TestCase):
    """The real address book, opened mode=ro. Counts and masked shapes only.

    A loader that has only ever met a fixture is a loader nobody has tested: the
    glob, the two store locations, the real Apple schema and Full Disk Access are
    all things a fixture cannot prove.  Nothing here prints a name, a number or
    an address.
    """

    @classmethod
    def setUpClass(cls):
        cls.paths = imcontacts.store_paths()
        if not cls.paths:
            raise unittest.SkipTest(
                "No macOS Contacts store found at "
                f"{imcontacts.DEFAULT_BASE} — either this is not a Mac, or the "
                "process has no Full Disk Access. The live count cannot be checked."
            )
        # Record every raw handle load_map canonicalises, so the live gate check
        # below can tell a number the RULE prefixed from one saved with its own +.
        cls.raw_handles = []
        real = imcontacts.canonicalise

        def spy(handle):
            cls.raw_handles.append(handle)
            return real(handle)

        imcontacts.canonicalise = spy
        try:
            cls.result, cls.err = _capture_warnings(imcontacts.load_map)
        finally:
            imcontacts.canonicalise = real
        # The floor is re-measured from this Mac's own stores, independently of the
        # loader, so it holds on any member's address book, large or small.
        cls.readable, cls.independent = _independent_named_handles(cls.paths)
        if not cls.readable:
            raise unittest.SkipTest(
                "Contacts stores exist but none can be opened here, which is what a "
                "process without Full Disk Access sees. The live count cannot be checked."
            )
        if not cls.independent:
            raise unittest.SkipTest(
                "the address book holds no named number or address, so there is "
                "nothing for the live read to find."
            )

    def test_live_entry_count_clears_the_floor(self):
        skipped = self.err.count("contacts store skipped:")
        print(
            f"\n    Contacts stores found: {len(self.paths)}  "
            f"(read: {len(self.paths) - skipped}, skipped: {skipped})"
        )
        print(f"    Contacts entries loaded: {len(self.result)}")
        emails = sum(1 for k in self.result if "@" in k)
        e164 = sum(1 for k in self.result if k.startswith("+"))
        print(
            f"    key shapes: {e164} +E.164 numbers, {emails} addresses, "
            f"{len(self.result) - e164 - emails} bare-digit"
        )
        sample = next(iter(sorted(self.result)), None)
        print(f"    masked sample key: {_mask(sample) if sample else '(none)'}")
        if self.err.strip():
            print(f"    warnings:\n{self.err.strip()}")
        print(f"    named handles counted independently: {self.independent} "
              f"in {self.readable} readable store(s)")
        # The loader folds spellings of one line and drops non-handles, so it may land
        # below the independent count, but a loader that really reads the stores keeps
        # most of it; one that misses the Sources/* stores or the schema loses most.
        _check_live_bound(self, len(self.result), f"{len(self.paths)} live store(s)",
                          floor=self.independent // 2)
        self.assertLessEqual(
            len(self.result), self.independent,
            "the loader returned more keys than there are named handles in the stores, "
            "so it is keying something that is not a saved number or address",
        )

    def test_the_sources_stores_are_where_the_data_is(self):
        """Reading only the top-level store looks like an empty address book."""
        own_glob = sorted(
            (imcontacts.DEFAULT_BASE / "Sources").glob(f"*/{imcontacts.STORE_FILENAME}"))
        if not own_glob:
            self.skipTest("this Mac keeps no per-account store under Sources/, so there "
                          "is no such store for the loader to miss")
        sources = [p for p in self.paths if p.parent.parent.name == "Sources"]
        self.assertTrue(
            sources,
            "No store was found under Sources/*, which is where the real records "
            "live; the top-level store holds only a couple of them.",
        )

    def test_every_live_key_is_canonical_and_idempotent(self):
        """Real data is the only place a rule that nearly works shows up."""
        for key in self.result:
            with self.subTest(shape=_mask(key)):
                self.assertEqual(
                    imcontacts.canonicalise(key),
                    key,
                    f"a live key of shape {_mask(key)} is not its own canonical form",
                )

    def test_every_live_bare_key_is_one_the_rule_says_must_stay_bare(self):
        """The rule's invariant, checked on real data.

        Under the ruling a bare key is only ever legitimate as one of the two
        exemptions: a short code (fewer than ``SHORTCODE_MIN_DIGITS`` digits) or
        an ID too long for E.164 once ``+1`` is added.  Any other bare key means
        the ``+1`` branch failed on a number it should have placed, and that
        contact's key could never pass the engine's lint gate.
        """
        for key in self.result:
            if not _is_bare(key):
                continue
            with self.subTest(shape=_mask(key)):
                exempt = (
                    len(key) < imcontacts.SHORTCODE_MIN_DIGITS
                    or len(key) + 1 > imcontacts.E164_MAX_DIGITS
                )
                self.assertTrue(
                    exempt,
                    f"a live bare key of shape {_mask(key)} should have been given +1 "
                    "under the ruling; left bare it can never be attached to a card.",
                )

    def test_every_live_number_the_rule_prefixed_passes_the_engine_gate(self):
        """Every real number saved WITHOUT a ``+`` that the rule placed is attachable.

        A number saved WITH its own ``+`` is kept as given, so whether it passes
        the gate is a property of what the member typed, not of this code; those
        are counted and printed, never asserted.
        """
        prefixed = typed_plus = typed_plus_refused = 0
        for raw in self.raw_handles:
            text = str(raw).strip()
            got = imcontacts.canonicalise(raw)
            if got is None or "@" in got or _is_bare(got):
                continue
            if text.startswith("+"):
                typed_plus += 1
                typed_plus_refused += not people_norm.is_valid_phone(got)
                continue
            prefixed += 1
            with self.subTest(shape=_mask(got)):
                self.assertTrue(
                    people_norm.is_valid_phone(got),
                    f"a live number the rule placed as {_mask(got)} fails the engine's "
                    "lint gate, so it could never be attached to a card.",
                )
        print(
            f"    live numbers the rule placed: {prefixed} (all must pass the gate); "
            f"saved with their own +: {typed_plus} ({typed_plus_refused} the gate refuses)"
        )

    def test_no_live_key_is_blank(self):
        for key in self.result:
            self.assertTrue(key and key.strip(), "a blank key would bucket everyone")

    def test_every_live_name_slugs_to_a_safe_component(self):
        """Every real Contacts label must survive the filename and the link parser."""
        for name in self.result.values():
            slug = imcontacts.slugify_label(name)
            with self.subTest(length=len(name)):
                self.assertTrue(
                    SLUG_RE.match(slug),
                    f"a live label of {len(name)} chars slugged outside [a-z0-9-]",
                )
                link = f"texts/{slug}.md"
                match = ENGINE_LINK_RE.search(f"texted (→ {link})")
                self.assertIsNotNone(match)
                self.assertEqual(
                    match.group(1),
                    link,
                    f"a live label of {len(name)} chars produced a link the engine's "
                    "parser truncates",
                )


class TestLiveCountAssertionBites(unittest.TestCase):
    """The negative control — the live floor must FAIL on an empty map.

    Without this, ``test_live_entry_count_clears_the_floor`` could be green
    because it asserts nothing at all.
    """

    def test_the_same_assertion_fails_when_the_loader_returns_nothing(self):
        with self.assertRaises(AssertionError):
            _check_live_bound(self, len({}), "a loader that returned {}")

    def test_it_also_fails_just_below_the_floor(self):
        with self.assertRaises(AssertionError):
            _check_live_bound(self, LIVE_MIN_ENTRIES, "exactly the floor")

    def test_and_passes_just_above_it(self):
        _check_live_bound(self, LIVE_MIN_ENTRIES + 1, "one over the floor")

    def test_a_re_measured_floor_bites_the_same_way(self):
        """The live test's floor is re-measured per Mac; it must still fail on {}."""
        with self.assertRaises(AssertionError):
            _check_live_bound(self, 0, "a loader that returned {}", floor=0)
        with self.assertRaises(AssertionError):
            _check_live_bound(self, 500, "half of 1,000 named handles", floor=1000 // 2)
        _check_live_bound(self, 501, "one over half", floor=1000 // 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
