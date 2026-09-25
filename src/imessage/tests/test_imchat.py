"""The database read — ordering, exclusions, identity, time, and the read-only floor.

``imchat`` is the only door onto ``~/Library/Messages/chat.db``, so a rule that
is wrong here is a text message filed onto the wrong person's card, or a year of
conversation that silently never arrives.  This suite holds those rules still in
two different ways, because neither alone is enough:

* **Fixtures** — a small SQLite database built to the real schema, where a rule
  can be cornered on purpose: ROWIDs that contradict their dates, a reaction, a
  system event, an outbound row whose handle is the recipient, a group chat with
  only two participants, a legacy seconds timestamp.  None of those can be
  arranged on demand in a live store.
* **The live store, read-only** — because a rule that has only ever met a fixture
  is a rule nobody has tested.  The live tests re-measure every figure the module
  docstring quotes, so a claim that stops being true fails a test instead of
  ageing quietly into a lie.

Each live assertion is a FLOOR or a bracket, never an exact number: the member
is texting while this runs and the database grows by a few rows an hour.  The
live tests run on any member's Mac, so every floor is scaled to the store's own
size or checked against an independent count taken in the test, never a figure
from one member's history; a store too small to measure, or one that simply has
none of a phenomenon, SKIPS with a plain reason (the fixtures still pin the rule).

PRIVACY — the rule this file is written under
----------------------------------------------
These tests read a member's real text messages.  **Nothing in this file may
print a message body, a real name or a real phone number** — not in output, not
in a failure message.  Counts, percentages, date spans and fully masked handle
shapes only.  Every fixture handle below is invented: the ``555-01xx`` range is
the reserved fictional block and the domains are ``example.com`` (RFC 2606).

Negative controls
------------------
Several assertions here would pass against a broken implementation if they were
written carelessly — an ordering check passes trivially if the fixture happens to
be sorted already, and a source guard passes trivially if its stripper ate the
code.  Every such assertion lives in a shared helper that a control class feeds a
deliberately wrong input to, and the control asserts the helper FAILS.
"""

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_HOME))

# The plug-in's own modules first, before anything reachable only because
# imconfig put `.claude/scripts` on sys.path. imchat imports the engine's
# `config` for the timezone, so the ordering matters here more than in the
# suites that touch no engine module at all.
import imconfig  # noqa: E402,F401
import imchat  # noqa: E402
import imtypedstream  # noqa: E402

import ast  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
import tempfile  # noqa: E402
import tokenize  # noqa: E402
import unittest  # noqa: E402
import unittest.mock  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers.
# ---------------------------------------------------------------------------

#: Apple's epoch, written out here INDEPENDENTLY of imchat: a test that built its
#: fixture timestamps with the function under test would agree with itself no
#: matter how wrong that function was.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

#: The reference threshold between legacy seconds and nanoseconds, written out
#: here independently of imchat.
NANOSECOND_THRESHOLD = 100_000_000_000

#: The size a live store must reach to be worth measuring at all; a smaller one
#: skips the live class (counted on the raw ``message`` table, not through imchat).
LIVE_MIN_ROWS = 10_000

#: How far back the "current behaviour" live measurements look.
LIVE_RECENT_DAYS = 180

#: Fictional handles (the reserved 555-01xx block; RFC 2606 domains).
ALICE = "+15555550101"
BOB = "+15555550102"
CAROL = "+15555550103"
MAILBOX = "someone@example.com"


def _apple_ns(moment: datetime) -> int:
    """An aware datetime as Apple nanoseconds — exact integer maths, no floats.

    Written out rather than imported so the fixtures are built against the
    format's definition, not against the module being tested.
    """
    delta = moment - APPLE_EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def _apple_seconds(moment: datetime) -> int:
    """The same instant as a LEGACY seconds value — the pre-2010 spelling."""
    delta = moment - APPLE_EPOCH
    return delta.days * 86400 + delta.seconds


def _mask(value):
    """A shape, never a value — for output that is allowed to be looked at."""
    if value is None:
        return "(none)"
    text = str(value)
    if "@" in text:
        local, _, domain = text.partition("@")
        return f"<local {len(local)}>@<domain {len(domain)}>"
    if text.startswith("+"):
        return f"+<{len(text) - 1} digits>"
    return f"<{len(text)} chars>"


def _typedstream(body: str) -> bytes:
    """A minimal real typedstream blob carrying ``body``.

    The same shape ``message.attributedBody`` holds: the fixed header, the
    ``NSString`` class name, the ``+`` inline-string marker, a single-byte
    length, then UTF-8.  Built from :mod:`imtypedstream`'s own published
    constants so it cannot drift away from the decoder's contract.
    """
    payload = body.encode("utf-8")
    assert len(payload) < 0x80, "fixture bodies stay in the single-byte length range"
    return (
        imtypedstream.HEADER
        + b"\x84\x84"
        + imtypedstream.NSSTRING
        + b"\x94\x84\x01"
        + imtypedstream.STRING_MARKER
        + bytes([len(payload)])
        + payload
    )


def _reset_warning_latches():
    """Clear imchat's once-per-process stderr latches between tests."""
    imchat._warned_unreadable = False
    imchat._warned_bad_timestamp = False
    imchat._warned_no_engine = False


# --- the fixture database -------------------------------------------------

#: Only the columns imchat actually touches, spelled as Apple spells them —
#: including a literal ``ROWID`` column, which is what the real schema has.
FIXTURE_SCHEMA = """
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY,
    date INTEGER,
    is_from_me INTEGER,
    handle_id INTEGER,
    text TEXT,
    attributedBody BLOB,
    associated_message_type INTEGER,
    item_type INTEGER,
    is_system_message INTEGER,
    date_edited INTEGER,
    message_summary_info BLOB
);
CREATE TABLE chat (
    ROWID INTEGER PRIMARY KEY,
    style INTEGER,
    display_name TEXT,
    chat_identifier TEXT
);
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
"""


def build_fixture(path, chats=(), handles=(), messages=(), participants=()):
    """Write a small Messages-shaped database at ``path``.

    ``chats``       : ``(rowid, style, display_name)``
    ``handles``     : ``(rowid, id)``
    ``participants``: ``(chat_rowid, handle_rowid)`` for ``chat_handle_join``
    ``messages``    : dicts; every key defaults to the harmless value, and
                      ``chat`` of ``None`` leaves the message out of
                      ``chat_message_join`` entirely (a message in no
                      conversation, which real stores do hold).
    """
    con = sqlite3.connect(str(path))
    try:
        con.executescript(FIXTURE_SCHEMA)
        con.executemany("INSERT INTO chat VALUES (?, ?, ?, ?)",
                        [(r, s, n, f"chat-{r}") for r, s, n in chats])
        con.executemany("INSERT INTO handle VALUES (?, ?)", list(handles))
        con.executemany("INSERT INTO chat_handle_join VALUES (?, ?)", list(participants))
        for row in messages:
            con.execute(
                "INSERT INTO message (ROWID, date, is_from_me, handle_id, text, "
                "attributedBody, associated_message_type, item_type, "
                "is_system_message, date_edited, message_summary_info) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["rowid"],
                    row.get("date"),
                    int(row.get("from_me", 0)),
                    row.get("handle_id", 0),
                    row.get("text"),
                    row.get("body"),
                    row.get("assoc", 0),
                    row.get("item", 0),
                    row.get("system", 0),
                    row.get("edited", 0),
                    row.get("summary"),
                ),
            )
            if row.get("chat") is not None:
                con.execute(
                    "INSERT INTO chat_message_join VALUES (?, ?)",
                    (row["chat"], row["rowid"]),
                )
        con.commit()
    finally:
        con.close()


class FixtureCase(unittest.TestCase):
    """A temp directory, a fixture database, and a read-only handle on it.

    The handle comes from :func:`imchat.connect`, so every fixture test in this
    file also exercises the real read-only open rather than a convenient
    read-write one built just for tests.
    """

    chats = ()
    handles = ()
    participants = ()
    messages = ()

    def setUp(self):
        _reset_warning_latches()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "chat.db"
        build_fixture(
            self.db,
            chats=self.chats,
            handles=self.handles,
            messages=self.messages,
            participants=self.participants,
        )
        self.conn = imchat.connect(path=self.db)
        self.addCleanup(self.conn.close)

    def read(self, since=None, until=None):
        return list(imchat.iter_messages(self.conn, since, until))


# --- assertions that a negative control has to be able to reuse -------------


def _assert_non_decreasing(case, moments, where):
    """The ordering assertion, in ONE place so the control uses the real one."""
    for index in range(1, len(moments)):
        if moments[index] < moments[index - 1]:
            case.fail(
                f"{where}: message {index} is dated before message {index - 1} "
                f"(a step of {(moments[index - 1] - moments[index]).total_seconds() / 86400:.1f} "
                "days backwards). The read is not in chronological order, so any "
                "cursor built on it will skip back-filled conversations forever."
            )


def _assert_live_floor(case, value, floor, what):
    """The live floor, in ONE place, for the same reason."""
    case.assertGreater(
        value,
        floor,
        f"{what} measured {value}, at or below the floor of {floor}. Either the "
        "query stopped matching the real schema or the store is not the member's.",
    )


# ---------------------------------------------------------------------------
# 1. Ordering — by date, never by ROWID.
# ---------------------------------------------------------------------------

_BASE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def _at(days):
    return _BASE + timedelta(days=days)


class TestOrderingFollowsDateNotRowid(FixtureCase):
    """Insertion order is not chronology, and the read must follow chronology.

    The fixture's ROWID order contradicts its date order on purpose, the way
    iCloud back-fill contradicts it on a real machine: 15.6% of rows there step
    backwards in ROWID order, some by months.
    """

    chats = ((1, imchat.ONE_TO_ONE_STYLE, None),)
    handles = ((7, ALICE),)
    # ROWID 1..5, dates deliberately scrambled. Expected date order: 4, 2, 1, 5, 3.
    messages = (
        {"rowid": 1, "chat": 1, "date": _apple_ns(_at(10)), "handle_id": 7, "text": "a"},
        {"rowid": 2, "chat": 1, "date": _apple_ns(_at(3)), "handle_id": 7, "text": "b"},
        {"rowid": 3, "chat": 1, "date": _apple_ns(_at(20)), "handle_id": 7, "text": "c"},
        {"rowid": 4, "chat": 1, "date": _apple_ns(_at(1)), "handle_id": 7, "text": "d"},
        {"rowid": 5, "chat": 1, "date": _apple_ns(_at(15)), "handle_id": 7, "text": "e"},
    )

    EXPECTED_BY_DATE = [_apple_ns(_at(n)) for n in (1, 3, 10, 15, 20)]

    def _rowid_ordered(self):
        """The naive read this module exists to refuse — ORDER BY ROWID."""
        return [
            row[0]
            for row in self.conn.execute("SELECT date FROM message ORDER BY ROWID")
        ]

    def test_messages_arrive_in_date_order(self):
        self.assertEqual([m.date_raw for m in self.read()], self.EXPECTED_BY_DATE)

    def test_the_yielded_order_is_not_the_rowid_order(self):
        """The fixture really does discriminate between the two rules."""
        self.assertNotEqual(self._rowid_ordered(), self.EXPECTED_BY_DATE)

    def test_dt_local_is_non_decreasing(self):
        _assert_non_decreasing(
            self, [m.dt_local for m in self.read()], "the fixture read"
        )

    def test_the_ordering_assertion_would_fail_on_a_rowid_ordered_read(self):
        """NEGATIVE CONTROL — the real assertion, fed the naive implementation."""
        zone = imchat._zone()
        naive = [imchat._apple_to_dt(raw, zone) for raw in self._rowid_ordered()]
        with self.assertRaises(self.failureException):
            _assert_non_decreasing(self, naive, "a ROWID-ordered read")


# ---------------------------------------------------------------------------
# 2. The exclusions.
# ---------------------------------------------------------------------------


class TestExclusions(FixtureCase):
    """Reactions and system events are out; edited messages and the 98% are in."""

    chats = ((1, imchat.ONE_TO_ONE_STYLE, None),)
    handles = ((7, ALICE),)
    messages = (
        {"rowid": 1, "chat": 1, "date": _apple_ns(_at(1)), "handle_id": 7, "text": "kept"},
        # A tapback. Its body is a localised sentence Apple wrote, not a message.
        {"rowid": 2, "chat": 1, "date": _apple_ns(_at(2)), "handle_id": 7,
         "text": "Gefällt „kept“", "assoc": 2000},
        # A system event — and note `system` stays 0, exactly as it is on every real
        # system row measured, so a filter on is_system_message would keep it.
        {"rowid": 3, "chat": 1, "date": _apple_ns(_at(3)), "handle_id": 0,
         "text": None, "item": 1, "system": 0},
        # message_summary_info non-NULL, never edited: the 98% case. KEPT.
        {"rowid": 4, "chat": 1, "date": _apple_ns(_at(4)), "handle_id": 7,
         "text": "summary but not edited", "summary": b"\x01\x02\x03"},
        # Genuinely edited. Still a message. KEPT.
        {"rowid": 5, "chat": 1, "date": _apple_ns(_at(5)), "handle_id": 7,
         "text": "edited", "summary": b"\x01\x02\x03", "edited": _apple_ns(_at(5))},
        # In no conversation at all — cannot be filed onto a conversation-day.
        {"rowid": 6, "chat": None, "date": _apple_ns(_at(6)), "handle_id": 7, "text": "orphan"},
        # No timestamp at all.
        {"rowid": 7, "chat": 1, "date": None, "handle_id": 7, "text": "undated"},
    )

    def test_only_the_qualifying_rows_are_yielded(self):
        kept = [m.text for m in self.read()]
        self.assertEqual(kept, ["kept", "summary but not edited", "edited"])

    def test_a_reaction_is_excluded_by_its_type_not_its_wording(self):
        """The German tapback body is why the filter is a type code, not a phrase."""
        bodies = [m.text for m in self.read()]
        self.assertFalse(
            any("Gefällt" in (b or "") for b in bodies),
            "a reaction reached the caller; its body is a localised sentence "
            "Apple generated in the sender's language, so some of them are not "
            "English and a wording-based filter would have let it through.",
        )

    def test_is_system_message_would_not_have_caught_the_system_row(self):
        """The column is 0 on every real row, including the genuine system events."""
        flag = self.conn.execute(
            "SELECT is_system_message FROM message WHERE ROWID = 3"
        ).fetchone()[0]
        self.assertEqual(flag, 0)
        self.assertNotIn(_apple_ns(_at(3)), [m.date_raw for m in self.read()])

    def test_an_edited_message_is_kept_and_the_summary_blob_is_ignored(self):
        """Gating on message_summary_info would throw away 98% of the database."""
        kept = {m.text for m in self.read()}
        self.assertIn("edited", kept)
        self.assertIn("summary but not edited", kept)

    def test_no_statement_this_module_builds_names_the_useless_columns(self):
        for sql in _every_statement():
            self.assertNotIn("message_summary_info", sql)
            self.assertNotIn("is_system_message", sql)


# ---------------------------------------------------------------------------
# 3. is_from_me branches first; handle is the counterpart, never the speaker.
# ---------------------------------------------------------------------------


class TestSenderAndHandle(FixtureCase):
    """On an outbound row ``handle_id`` is the RECIPIENT, on every outbound row measured."""

    chats = ((1, imchat.ONE_TO_ONE_STYLE, None),)
    handles = ((7, ALICE), (8, ""))
    participants = ((1, 7),)
    messages = (
        # Inbound: the handle is the sender.
        {"rowid": 1, "chat": 1, "date": _apple_ns(_at(1)), "from_me": 0,
         "handle_id": 7, "text": "from her"},
        # Outbound: the SAME handle, and now it is the recipient.
        {"rowid": 2, "chat": 1, "date": _apple_ns(_at(2)), "from_me": 1,
         "handle_id": 7, "text": "from me"},
        # Outbound with no destination handle recorded — common on a real store.
        {"rowid": 3, "chat": 1, "date": _apple_ns(_at(3)), "from_me": 1,
         "handle_id": 0, "text": "no handle"},
        # Inbound with no handle — the caller renders "Unknown".
        {"rowid": 4, "chat": 1, "date": _apple_ns(_at(4)), "from_me": 0,
         "handle_id": 0, "text": "unknown sender"},
        # A handle row whose id is blank is not a counterpart.
        {"rowid": 5, "chat": 1, "date": _apple_ns(_at(5)), "from_me": 0,
         "handle_id": 8, "text": "blank handle"},
    )

    def test_inbound_handle_is_the_sender(self):
        message = self.read()[0]
        self.assertFalse(message.is_from_me)
        self.assertEqual(message.handle, ALICE)

    def test_outbound_handle_is_the_recipient_and_is_from_me_is_the_only_tell(self):
        """Reading handle as 'who spoke' files the member's words onto Alice."""
        inbound, outbound = self.read()[0], self.read()[1]
        self.assertEqual(inbound.handle, outbound.handle)
        self.assertFalse(inbound.is_from_me)
        self.assertTrue(outbound.is_from_me)

    def test_a_missing_handle_id_is_none_not_a_guess(self):
        outbound_none, inbound_none = self.read()[2], self.read()[3]
        self.assertIsNone(outbound_none.handle)
        self.assertIsNone(inbound_none.handle)
        self.assertTrue(outbound_none.is_from_me)
        self.assertFalse(inbound_none.is_from_me)

    def test_a_blank_handle_string_is_none(self):
        self.assertIsNone(self.read()[4].handle)


# ---------------------------------------------------------------------------
# 4. Group vs one-to-one — chat.style, never a head count.
# ---------------------------------------------------------------------------


class TestGroupIsStyleNotHeadCount(FixtureCase):
    """Real group chats with exactly two participants are common."""

    chats = (
        (1, imchat.GROUP_STYLE, "the two of us"),   # a group with TWO people
        (2, imchat.ONE_TO_ONE_STYLE, None),         # genuinely one-to-one
        (3, imchat.GROUP_STYLE, None),              # a group with five
    )
    handles = ((7, ALICE), (8, BOB), (9, CAROL), (10, MAILBOX), (11, "+15555550104"))
    participants = (
        (1, 7), (1, 8),
        (2, 7),
        (3, 7), (3, 8), (3, 9), (3, 10), (3, 11),
    )
    messages = (
        {"rowid": 1, "chat": 1, "date": _apple_ns(_at(1)), "handle_id": 7, "text": "a"},
        {"rowid": 2, "chat": 2, "date": _apple_ns(_at(2)), "handle_id": 7, "text": "b"},
        {"rowid": 3, "chat": 3, "date": _apple_ns(_at(3)), "handle_id": 8, "text": "c"},
    )

    def test_a_two_person_group_is_still_a_group(self):
        two_person = self.read()[0]
        self.assertEqual(two_person.chat_style, imchat.GROUP_STYLE)
        self.assertTrue(two_person.is_group)

    def test_the_head_count_rule_would_get_that_chat_wrong(self):
        """NEGATIVE CONTROL — the participant count really does say 'not a group'."""
        count = self.conn.execute(
            "SELECT COUNT(*) FROM chat_handle_join WHERE chat_id = 1"
        ).fetchone()[0]
        self.assertEqual(count, 2)
        self.assertNotEqual(count > 2, self.read()[0].is_group)

    def test_one_to_one_and_a_five_person_group(self):
        self.assertFalse(self.read()[1].is_group)
        self.assertTrue(self.read()[2].is_group)

    def test_the_chat_name_rides_along_and_blank_becomes_none(self):
        self.assertEqual(self.read()[0].chat_name, "the two of us")
        self.assertIsNone(self.read()[1].chat_name)

    def test_no_statement_this_module_builds_reads_the_participant_table(self):
        for sql in _every_statement():
            self.assertNotIn("chat_handle_join", sql)


# ---------------------------------------------------------------------------
# 5. The window is half-open: [since, until).
# ---------------------------------------------------------------------------


class TestHalfOpenWindow(FixtureCase):
    """A message on the boundary belongs to the LATER window, exactly once."""

    chats = ((1, imchat.ONE_TO_ONE_STYLE, None),)
    handles = ((7, ALICE),)
    EARLY, EDGE, LATE = _at(1), _at(2), _at(3)
    messages = (
        {"rowid": 1, "chat": 1, "date": _apple_ns(_at(1)), "handle_id": 7, "text": "early"},
        {"rowid": 2, "chat": 1, "date": _apple_ns(_at(2)), "handle_id": 7, "text": "edge"},
        {"rowid": 3, "chat": 1, "date": _apple_ns(_at(3)), "handle_id": 7, "text": "late"},
    )

    def test_since_is_inclusive(self):
        self.assertEqual([m.text for m in self.read(since=self.EDGE)], ["edge", "late"])

    def test_until_is_exclusive(self):
        self.assertEqual([m.text for m in self.read(until=self.EDGE)], ["early"])

    def test_adjacent_windows_see_every_message_exactly_once(self):
        first = [m.text for m in self.read(self.EARLY, self.EDGE)]
        second = [m.text for m in self.read(self.EDGE, self.LATE)]
        third = [m.text for m in self.read(self.LATE, self.LATE + timedelta(days=1))]
        self.assertEqual(first + second + third, ["early", "edge", "late"])

    def test_an_empty_window_yields_nothing_and_does_not_raise(self):
        self.assertEqual(self.read(self.EDGE, self.EDGE), [])

    def test_a_naive_bound_is_read_in_the_configured_zone(self):
        zone = imchat._zone()
        aware = self.EDGE.astimezone(zone)
        naive = aware.replace(tzinfo=None)
        self.assertEqual(
            [m.date_raw for m in self.read(since=naive)],
            [m.date_raw for m in self.read(since=aware)],
        )


# ---------------------------------------------------------------------------
# 6. Time — nanoseconds, legacy seconds, and the zone.
# ---------------------------------------------------------------------------


class TestAppleTimestamps(unittest.TestCase):
    """The column is two units at once, and the zone comes from the engine."""

    def setUp(self):
        _reset_warning_latches()

    def test_a_nanosecond_and_a_seconds_value_for_the_same_instant_agree(self):
        moment = datetime(2009, 6, 1, 8, 30, tzinfo=timezone.utc)
        from_ns = imchat.apple_to_dt(_apple_ns(moment))
        from_seconds = imchat.apple_to_dt(_apple_seconds(moment))
        self.assertEqual(from_ns, from_seconds)
        self.assertEqual(from_ns.astimezone(timezone.utc), moment)

    def test_the_result_is_timezone_aware(self):
        result = imchat.apple_to_dt(_apple_ns(_BASE))
        self.assertIsNotNone(result.tzinfo)
        self.assertIsNotNone(result.utcoffset())

    def test_the_zone_really_comes_from_the_engines_config(self):
        """Change what ``config.now_local()`` says, and the conversion follows."""
        if imchat._engine_config is None:
            self.skipTest("the engine's config is not importable in this session")
        invented = timezone(timedelta(hours=9), "Test/Invented")
        with unittest.mock.patch.object(
            imchat._engine_config, "now_local", return_value=datetime.now(invented)
        ):
            result = imchat.apple_to_dt(_apple_ns(_BASE))
        self.assertEqual(result.utcoffset(), timedelta(hours=9))
        self.assertEqual(result.astimezone(timezone.utc), _BASE)

    def test_a_single_fixed_offset_would_be_wrong_for_half_the_year(self):
        """Why the machine's own local offset is not a substitute for the zone.

        ``datetime.now().astimezone().tzinfo`` is a FIXED offset — today's.
        Reading a message from the other side of a DST change through it is wrong
        by a full hour, which is enough to file a late-evening message on the
        wrong day.  Where the configured zone has no DST change there is nothing
        to prove and the test says so rather than pretending.
        """
        winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        summer = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        winter_offset = imchat.apple_to_dt(_apple_ns(winter)).utcoffset()
        summer_offset = imchat.apple_to_dt(_apple_ns(summer)).utcoffset()
        if winter_offset == summer_offset:
            self.skipTest("the configured zone has no DST change, so nothing differs")
        self.assertEqual(abs(winter_offset - summer_offset), timedelta(hours=1))
        machine_offset = datetime.now().astimezone().utcoffset()
        self.assertIn(
            machine_offset, {winter_offset, summer_offset},
            "today's machine offset matches neither season of the configured "
            "zone, so the two are different zones entirely",
        )

    def test_it_matches_the_reference_conversion(self):
        """The same answer as the reference conversion, ``raw / 1e9 + 978307200``."""
        zone = imchat._zone()
        for days in range(0, 400, 7):
            moment = _BASE + timedelta(days=days, seconds=days * 37)
            raw = _apple_ns(moment)
            reference = datetime.fromtimestamp(raw / 1e9 + 978307200, tz=zone)
            self.assertLess(
                abs(imchat.apple_to_dt(raw) - reference),
                timedelta(microseconds=2),
                "the conversion has drifted from the reference implementation",
            )

    def test_the_threshold_is_the_reference_threshold(self):
        self.assertEqual(imchat.NANOSECOND_THRESHOLD, NANOSECOND_THRESHOLD)


class TestMixedUnitsInOneColumn(FixtureCase):
    """A legacy seconds row and a nanosecond row, ordered and windowed together.

    On a modern machine every row is nanoseconds, so this branch cannot be
    reached live and is pinned here instead. The fixture is
    arranged so a naive ``ORDER BY m.date`` gets it WRONG: the legacy row is the
    later of the two in real time, and the smaller of the two as an integer.
    """

    LEGACY_MOMENT = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
    MODERN_MOMENT = datetime(2023, 5, 1, 9, 0, tzinfo=timezone.utc)

    chats = ((1, imchat.ONE_TO_ONE_STYLE, None),)
    handles = ((7, ALICE),)
    messages = (
        {"rowid": 1, "chat": 1, "date": _apple_seconds(LEGACY_MOMENT),
         "handle_id": 7, "text": "legacy"},
        {"rowid": 2, "chat": 1, "date": _apple_ns(MODERN_MOMENT),
         "handle_id": 7, "text": "modern"},
    )

    def test_the_raw_integers_would_sort_the_wrong_way_round(self):
        """NEGATIVE CONTROL — the fixture really does defeat a naive sort."""
        raw = [r[0] for r in self.conn.execute("SELECT date FROM message ORDER BY date")]
        self.assertEqual(raw[0], _apple_seconds(self.LEGACY_MOMENT))
        self.assertGreater(self.LEGACY_MOMENT, self.MODERN_MOMENT)

    def test_the_read_orders_them_by_real_time(self):
        self.assertEqual([m.text for m in self.read()], ["modern", "legacy"])
        _assert_non_decreasing(self, [m.dt_local for m in self.read()], "mixed units")

    def test_date_raw_is_left_untouched(self):
        by_text = {m.text: m for m in self.read()}
        self.assertEqual(by_text["legacy"].date_raw, _apple_seconds(self.LEGACY_MOMENT))
        self.assertEqual(by_text["modern"].date_raw, _apple_ns(self.MODERN_MOMENT))

    def test_a_window_bounds_a_legacy_row_correctly(self):
        window = self.read(
            self.LEGACY_MOMENT - timedelta(hours=1),
            self.LEGACY_MOMENT + timedelta(hours=1),
        )
        self.assertEqual([m.text for m in window], ["legacy"])

    def test_both_rows_land_at_the_right_instant(self):
        by_text = {m.text: m for m in self.read()}
        self.assertEqual(by_text["legacy"].dt_local.astimezone(timezone.utc),
                         self.LEGACY_MOMENT)
        self.assertEqual(by_text["modern"].dt_local.astimezone(timezone.utc),
                         self.MODERN_MOMENT)


# ---------------------------------------------------------------------------
# 7. The body.
# ---------------------------------------------------------------------------


class TestBody(FixtureCase):
    """``text`` when it is there, the decoder otherwise, and None vs "" kept apart."""

    chats = ((1, imchat.ONE_TO_ONE_STYLE, None),)
    handles = ((7, ALICE),)
    messages = (
        {"rowid": 1, "chat": 1, "date": _apple_ns(_at(1)), "handle_id": 7,
         "text": "from the text column"},
        {"rowid": 2, "chat": 1, "date": _apple_ns(_at(2)), "handle_id": 7,
         "text": None, "body": _typedstream("from the decoder")},
        # Both present and disagreeing: the documented precedence is `text` first.
        {"rowid": 3, "chat": 1, "date": _apple_ns(_at(3)), "handle_id": 7,
         "text": "the column", "body": _typedstream("the blob")},
        # An attachment on its own: read successfully, and it carries no words.
        {"rowid": 4, "chat": 1, "date": _apple_ns(_at(4)), "handle_id": 7,
         "text": None, "body": _typedstream("￼")},
        # Nothing to read at all — a few real qualifying rows are like this.
        {"rowid": 5, "chat": 1, "date": _apple_ns(_at(5)), "handle_id": 7,
         "text": None, "body": None},
        # Bytes that are not a typedstream.
        {"rowid": 6, "chat": 1, "date": _apple_ns(_at(6)), "handle_id": 7,
         "text": None, "body": b"this is not a typedstream"},
        # A caption beside a photo: the placeholder goes, the words stay.
        {"rowid": 7, "chat": 1, "date": _apple_ns(_at(7)), "handle_id": 7,
         "text": None, "body": _typedstream("￼look at this")},
        # An empty text column is a body that was read and has no words.
        {"rowid": 8, "chat": 1, "date": _apple_ns(_at(8)), "handle_id": 7, "text": ""},
    )

    def bodies(self):
        return [m.text for m in self.read()]

    def test_the_text_column_is_used_when_it_is_there(self):
        self.assertEqual(self.bodies()[0], "from the text column")

    def test_the_decoder_is_used_when_the_text_column_is_null(self):
        self.assertEqual(self.bodies()[1], "from the decoder")

    def test_the_text_column_wins_when_both_are_present(self):
        self.assertEqual(self.bodies()[2], "the column")

    def test_an_attachment_reads_as_empty_string_not_none(self):
        """'' is a success (a photo); None is a failure. Conflating them loses photos."""
        self.assertEqual(self.bodies()[3], "")
        self.assertIsNotNone(self.bodies()[3])

    def test_no_bytes_at_all_reads_as_none(self):
        self.assertIsNone(self.bodies()[4])

    def test_undecodable_bytes_read_as_none_and_cost_one_row_not_the_run(self):
        self.assertIsNone(self.bodies()[5])
        self.assertEqual(len(self.bodies()), 8)

    def test_the_attachment_placeholder_is_stripped_from_a_caption(self):
        self.assertEqual(self.bodies()[6], "look at this")

    def test_an_empty_text_column_stays_empty_rather_than_becoming_none(self):
        self.assertEqual(self.bodies()[7], "")


# ---------------------------------------------------------------------------
# 8. count_rows / date_range describe exactly what iter_messages yields.
# ---------------------------------------------------------------------------


class TestCountAndRange(FixtureCase):
    chats = ((1, imchat.ONE_TO_ONE_STYLE, None),)
    handles = ((7, ALICE),)
    messages = (
        {"rowid": 1, "chat": 1, "date": _apple_ns(_at(5)), "handle_id": 7, "text": "a"},
        {"rowid": 2, "chat": 1, "date": _apple_ns(_at(1)), "handle_id": 7, "text": "b"},
        {"rowid": 3, "chat": 1, "date": _apple_ns(_at(9)), "handle_id": 7, "text": "c"},
        {"rowid": 4, "chat": 1, "date": _apple_ns(_at(2)), "handle_id": 7,
         "text": "reaction", "assoc": 2000},
        {"rowid": 5, "chat": None, "date": _apple_ns(_at(3)), "handle_id": 7, "text": "orphan"},
    )

    def test_count_matches_what_is_yielded(self):
        self.assertEqual(imchat.count_rows(self.conn), len(self.read()))
        self.assertEqual(imchat.count_rows(self.conn), 3)

    def test_range_matches_the_first_and_last_yielded(self):
        messages = self.read()
        self.assertEqual(
            imchat.date_range(self.conn), (messages[0].dt_local, messages[-1].dt_local)
        )

    def test_an_empty_store_gives_none_none(self):
        empty = Path(self._tmp.name) / "empty.db"
        build_fixture(empty)
        conn = imchat.connect(path=empty)
        self.addCleanup(conn.close)
        self.assertEqual(imchat.count_rows(conn), 0)
        self.assertEqual(imchat.date_range(conn), (None, None))


# ---------------------------------------------------------------------------
# 9. The failure path — one sentence, nothing, no retry.
# ---------------------------------------------------------------------------


class _ExplodingConnection:
    """A connection that always refuses, and counts how often it was asked."""

    def __init__(self):
        self.calls = 0

    def execute(self, *_args, **_kwargs):
        self.calls += 1
        raise sqlite3.OperationalError("unable to open database file")


class TestUnreadableStoreIsQuiet(unittest.TestCase):
    """The 03:00 case: Messages idle, the WAL checkpointed away, the open fails."""

    def setUp(self):
        _reset_warning_latches()
        self.conn = _ExplodingConnection()

    def _run(self, call):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            result = call()
        return result, buffer.getvalue()

    def test_iter_messages_yields_nothing_and_says_one_sentence(self):
        result, err = self._run(lambda: list(imchat.iter_messages(self.conn)))
        self.assertEqual(result, [])
        self.assertEqual(len(err.strip().splitlines()), 1, f"expected one line, got: {err!r}")
        self.assertIn("read nothing this run", err)

    def test_it_never_retries(self):
        with contextlib.redirect_stderr(io.StringIO()):
            list(imchat.iter_messages(self.conn))
        self.assertEqual(
            self.conn.calls, 1,
            "the read asked the live message store more than once after a refusal; "
            "a retry loop against a member's Messages database at 03:00 is exactly "
            "what this module must never do.",
        )

    def test_count_rows_degrades_to_zero(self):
        result, err = self._run(lambda: imchat.count_rows(self.conn))
        self.assertEqual(result, 0)
        self.assertIn("[imessage]", err)

    def test_date_range_degrades_to_none_none(self):
        result, err = self._run(lambda: imchat.date_range(self.conn))
        self.assertEqual(result, (None, None))
        self.assertIn("[imessage]", err)

    def test_the_sentence_is_said_once_not_once_per_call(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            list(imchat.iter_messages(self.conn))
            list(imchat.iter_messages(self.conn))
            imchat.count_rows(self.conn)
        self.assertEqual(len(buffer.getvalue().strip().splitlines()), 1)

    def test_strict_raises_instead_of_reporting_nothing(self):
        """The daily run moves a watermark on what it read: "unreadable" must not look empty."""
        with self.assertRaises(sqlite3.Error):
            list(imchat.iter_messages(self.conn, strict=True))
        self.assertEqual(self.conn.calls, 1, "strict must not retry either")

    def test_the_arrival_scan_and_the_bounds_raise_too(self):
        with self.assertRaises(sqlite3.Error):
            list(imchat.iter_row_refs(self.conn, 0))
        with self.assertRaises(sqlite3.Error):
            imchat.rowid_bounds(self.conn, None)


# ---------------------------------------------------------------------------
# 9b. The arrival scan — ROWID says what is NEW, the date still says where it goes.
# ---------------------------------------------------------------------------


class TestArrivalScan(FixtureCase):
    """``iter_row_refs`` / ``rowid_bounds``: the daily watermark's two reads.

    ROWID 4 is the case the whole design exists for: it arrived LAST and is dated
    FIRST, the way a message the phone sent while the Mac slept lands.  A date
    watermark placed after ROWID 3 would never read it.
    """

    chats = ((1, imchat.ONE_TO_ONE_STYLE, None), (2, imchat.GROUP_STYLE, "Walk"))
    handles = ((7, ALICE), (8, BOB))
    messages = (
        {"rowid": 1, "chat": 1, "date": _apple_ns(_at(5)), "handle_id": 7, "text": "a"},
        {"rowid": 2, "chat": 2, "date": _apple_ns(_at(6)), "handle_id": 8, "text": "b"},
        {"rowid": 3, "chat": 1, "date": _apple_ns(_at(7)), "handle_id": 7, "text": "c"},
        # arrived last, dated first
        {"rowid": 4, "chat": 1, "date": _apple_ns(_at(1)), "handle_id": 7, "text": "late"},
        # never qualifying: a reaction, a system event, a message in no conversation
        {"rowid": 5, "chat": 1, "date": _apple_ns(_at(8)), "handle_id": 7, "text": "x",
         "assoc": 2000},
        {"rowid": 6, "chat": 1, "date": _apple_ns(_at(8)), "handle_id": 7, "item": 1},
        {"rowid": 7, "chat": None, "date": _apple_ns(_at(8)), "handle_id": 7, "text": "y"},
    )

    def refs(self, after):
        return list(imchat.iter_row_refs(self.conn, after))

    def test_only_rows_that_arrived_after_the_mark(self):
        self.assertEqual(sorted(r.rowid for r in self.refs(2)), [3, 4])
        self.assertEqual(sorted(r.rowid for r in self.refs(0)), [1, 2, 3, 4])
        self.assertEqual(self.refs(4), [])

    def test_a_late_arrival_with_an_old_date_is_found_by_arrival(self):
        late = [r for r in self.refs(3)]
        self.assertEqual([r.rowid for r in late], [4])
        self.assertEqual(late[0].dt_local, _at(1).astimezone(imchat._zone()))
        # the negative control: the same row is invisible to a read by date after row 3's date
        after_three = [m.text for m in self.read(since=_at(7))]
        self.assertNotIn("late", after_three)

    def test_each_ref_carries_its_conversation_and_its_raw_date(self):
        by_row = {r.rowid: r for r in self.refs(0)}
        self.assertEqual(by_row[2].chat_rowid, 2)
        self.assertEqual(by_row[1].date_raw, _apple_ns(_at(5)))
        self.assertEqual(by_row[1].dt_local.date(), self.read(since=_at(5))[0].dt_local.date())

    def test_the_exclusions_match_iter_messages(self):
        by_arrival = sorted(r.date_raw for r in self.refs(0))
        by_date = sorted(m.date_raw for m in self.read())
        self.assertEqual(by_arrival, by_date)

    def test_bounds(self):
        self.assertEqual(imchat.rowid_bounds(self.conn, _at(6)), (2, 4))
        self.assertEqual(imchat.rowid_bounds(self.conn, None), (1, 4))
        self.assertEqual(imchat.rowid_bounds(self.conn, _at(30)), (None, 4))


class TestArrivalScanOnAnEmptyStore(FixtureCase):
    chats = ((1, imchat.ONE_TO_ONE_STYLE, None),)

    def test_nothing_arrived_and_there_are_no_bounds(self):
        self.assertEqual(list(imchat.iter_row_refs(self.conn, 0)), [])
        self.assertEqual(imchat.rowid_bounds(self.conn, None), (None, None))


# ---------------------------------------------------------------------------
# 10. The open is read-only, in fact and not only in intent.
# ---------------------------------------------------------------------------


class TestConnectIsReadOnly(unittest.TestCase):
    def setUp(self):
        _reset_warning_latches()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "chat.db"
        build_fixture(
            self.db,
            chats=((1, imchat.ONE_TO_ONE_STYLE, None),),
            handles=((7, ALICE),),
            messages=({"rowid": 1, "chat": 1, "date": _apple_ns(_at(1)),
                       "handle_id": 7, "text": "a"},),
        )

    def test_the_uri_carries_mode_ro_and_nothing_else(self):
        uri = imchat._read_only_uri(self.db)
        self.assertTrue(uri.startswith("file://"), uri)
        self.assertTrue(uri.endswith("?mode=ro"), uri)
        self.assertNotIn("immutable", uri)

    def test_the_arguments_actually_passed_to_sqlite(self):
        """Captured at run time — the strongest form of this check."""
        seen = {}
        real = sqlite3.connect

        def spy(target, *args, **kwargs):
            seen["target"] = target
            seen["kwargs"] = kwargs
            return real(target, *args, **kwargs)

        with unittest.mock.patch.object(imchat.sqlite3, "connect", spy):
            conn = imchat.connect(path=self.db)
        self.addCleanup(conn.close)
        self.assertIn("mode=ro", seen["target"])
        self.assertNotIn("immutable", seen["target"])
        self.assertIs(seen["kwargs"].get("uri"), True)
        self.assertEqual(seen["kwargs"].get("timeout"), 5.0)

    def test_the_handle_really_refuses_a_write(self):
        conn = imchat.connect(path=self.db)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError) as caught:
            conn.execute("CREATE TABLE scribble (x INTEGER)")
        self.assertIn("readonly", str(caught.exception).lower())

    def test_a_path_with_spaces_still_opens(self):
        """as_uri() percent-encodes; naive string concatenation would not."""
        folder = Path(self._tmp.name) / "a folder with spaces"
        folder.mkdir()
        spaced = folder / "chat.db"
        build_fixture(
            spaced,
            chats=((1, imchat.ONE_TO_ONE_STYLE, None),),
            handles=((7, ALICE),),
            messages=({"rowid": 1, "chat": 1, "date": _apple_ns(_at(1)),
                       "handle_id": 7, "text": "a"},),
        )
        self.assertNotIn(" ", imchat._read_only_uri(spaced))
        conn = imchat.connect(path=spaced)
        self.addCleanup(conn.close)
        self.assertEqual(imchat.count_rows(conn), 1)


# ---------------------------------------------------------------------------
# 11. has_access — three different answers, three different fixes.
# ---------------------------------------------------------------------------


class TestHasAccess(unittest.TestCase):
    def setUp(self):
        _reset_warning_latches()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def test_a_missing_store_says_missing_and_never_mentions_permission(self):
        ok, sentence = imchat.has_access(path=self.folder / "not-here.db")
        self.assertFalse(ok)
        self.assertIn("no Messages database", sentence)
        self.assertNotIn("Full Disk Access", sentence)

    def test_a_denied_store_names_full_disk_access_and_the_app(self):
        present = self.folder / "chat.db"
        present.write_bytes(imchat.SQLITE_MAGIC + b"\x00" * 16)
        with unittest.mock.patch("builtins.open", side_effect=PermissionError):
            ok, sentence = imchat.has_access(path=present)
        self.assertFalse(ok)
        self.assertIn("Full Disk Access", sentence)
        self.assertIn(imchat._responsible_app(), sentence)

    def test_the_two_sentences_are_not_the_same_sentence(self):
        """SQLite's error text is identical for both; these must not be."""
        present = self.folder / "chat.db"
        present.write_bytes(imchat.SQLITE_MAGIC + b"\x00" * 16)
        _, missing = imchat.has_access(path=self.folder / "not-here.db")
        with unittest.mock.patch("builtins.open", side_effect=PermissionError):
            _, denied = imchat.has_access(path=present)
        self.assertNotEqual(missing, denied)

    def test_a_file_that_is_not_a_database_says_so(self):
        decoy = self.folder / "chat.db"
        decoy.write_bytes(b"not a database at all")
        ok, sentence = imchat.has_access(path=decoy)
        self.assertFalse(ok)
        self.assertIn("not a SQLite database", sentence)

    def test_off_a_mac_it_refuses_before_touching_the_disk(self):
        with unittest.mock.patch.object(imchat, "_is_macos", return_value=False):
            ok, sentence = imchat.has_access(path=self.folder / "anything.db")
        self.assertFalse(ok)
        self.assertIn("only keeps its database on a Mac", sentence)

    def test_a_real_readable_store_says_yes(self):
        real = self.folder / "chat.db"
        build_fixture(real)
        ok, sentence = imchat.has_access(path=real)
        self.assertTrue(ok, sentence)

    def test_the_responsible_app_is_a_name_not_a_path(self):
        name = imchat._responsible_app()
        self.assertTrue(name)
        self.assertNotIn("/", name)
        self.assertNotIn(".app", name)
        print(f"\n    Full Disk Access would be asked for: {name}")


# ---------------------------------------------------------------------------
# 12. The source guard.
# ---------------------------------------------------------------------------

WRITE_VOCABULARY = re.compile(r"\b(insert|update|delete|drop)\b", re.IGNORECASE)

IMCHAT_SOURCE = Path(imchat.__file__).read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """``source`` with every docstring and every comment removed.

    The guard runs over this rather than the raw file on purpose.  The module
    docstring DOCUMENTS the forbidden flag, with the measurement that proves why
    it is forbidden, and deleting that explanation to satisfy a grep would trade
    a real safeguard (a reader who knows why) for a fake one.  So prose may name
    it and code may not, and the check over code is the STRICTER of the two: it
    refuses the bare word, where a raw-file grep for ``immutable=1`` would sail
    past ``immutable = 1`` and ``immutable=True``.

    String literals other than docstrings are KEPT, because that is where the SQL
    and the connection URI live and they are exactly what has to be checked.
    """
    docstring_lines = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            first = body[0]
            docstring_lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))

    comment_at = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            row, column = token.start
            comment_at[row] = min(comment_at.get(row, column), column)

    kept = []
    for number, line in enumerate(source.splitlines(), start=1):
        if number in docstring_lines:
            continue
        if number in comment_at:
            line = line[: comment_at[number]]
        kept.append(line)
    return "\n".join(kept)


def _every_statement():
    """Every SQL statement imchat can build, so a guard can read them all."""
    zone = timezone.utc
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    statements = []
    for columns in (imchat._COLUMNS, "COUNT(*)",
                    f"MIN({imchat._APPLE_NS}), MAX({imchat._APPLE_NS})",
                    imchat._REF_COLUMNS, "MIN(m.ROWID)", "MAX(m.ROWID)"):
        for bounds in ((None, None), (moment, None), (None, moment), (moment, moment)):
            for order in (True, False):
                for after in (None, 42):
                    sql, _ = imchat._row_query(
                        columns, bounds[0], bounds[1], zone, order, after_rowid=after
                    )
                    statements.append(sql)
    return statements


class TestSourceGuard(unittest.TestCase):
    """This module reads. It must be unable to do anything else."""

    def test_no_write_vocabulary_anywhere_in_the_file(self):
        found = WRITE_VOCABULARY.findall(IMCHAT_SOURCE)
        self.assertEqual(
            found, [],
            f"imchat.py names a write verb ({sorted(set(found))}). This module is "
            "the read; a write belongs nowhere in it, not even in a docstring.",
        )

    def test_no_journal_pragma(self):
        self.assertNotIn("pragma journal", IMCHAT_SOURCE.lower())

    def test_no_code_line_carries_the_immutable_flag(self):
        code = _code_only(IMCHAT_SOURCE)
        self.assertNotIn(
            "immutable", code.lower(),
            "imchat.py opens the store with the immutable flag somewhere in its "
            "code. Measured on a live database, that flag loses the two newest "
            "messages because it lets SQLite ignore the write-ahead log.",
        )

    def test_the_stripper_did_not_simply_eat_the_code(self):
        """NEGATIVE CONTROL — the checked view still contains what it should."""
        code = _code_only(IMCHAT_SOURCE)
        self.assertIn("mode=ro", code)
        self.assertIn("ORDER BY", code)
        self.assertIn("sqlite3.connect(", code)

    def test_the_immutable_check_bites(self):
        """NEGATIVE CONTROL — the same check, over source that really is wrong."""
        wrong = '"""A docstring.\n\nExplains nothing.\n"""\nuri = path + "?immutable=1"\n'
        self.assertIn("immutable", _code_only(wrong).lower())
        right = '"""A docstring that mentions immutable=1 and why it is refused."""\nuri = "?mode=ro"\n'
        self.assertNotIn("immutable", _code_only(right).lower())

    def test_the_write_vocabulary_check_bites(self):
        self.assertTrue(WRITE_VOCABULARY.search("DELETE FROM message"))
        self.assertTrue(WRITE_VOCABULARY.search("  insert into x values (1)"))
        self.assertFalse(WRITE_VOCABULARY.search("the deleted rows were dropped later"))

    def test_every_sqlite_connect_is_inside_connect(self):
        import inspect

        whole = IMCHAT_SOURCE.count("sqlite3.connect(")
        inside = inspect.getsource(imchat.connect).count("sqlite3.connect(")
        self.assertEqual(whole, 1)
        self.assertEqual(inside, whole,
                         "a sqlite3.connect outside connect() is an open this "
                         "module's read-only rule does not cover")

    def test_every_statement_reads_and_orders_the_right_way(self):
        ordered = 0
        for sql in _every_statement():
            self.assertIn("FROM message", sql)
            self.assertNotIn("chat_handle_join", sql)
            self.assertNotIn("message_summary_info", sql)
            self.assertNotIn("is_system_message", sql)
            if "ORDER BY" in sql:
                ordered += 1
                clause = sql.split("ORDER BY", 1)[1]
                self.assertIn("m.date", clause)
                self.assertNotIn("ROWID", clause.upper())
        self.assertGreater(ordered, 0, "no statement was ordered at all")


# ---------------------------------------------------------------------------
# 13. The live store — read-only, counts only, every claim re-measured.
# ---------------------------------------------------------------------------


class TestLiveMessagesDatabase(unittest.TestCase):
    """The member's real database. Nothing here prints a body, a name or a number.

    Runs on any member's Mac.  No Full Disk Access, no Messages store, or a store
    under :data:`LIVE_MIN_ROWS` rows skips the whole class.  Every floor below is
    scaled to this store's own size or gated on an independent count of the raw
    table, and a phenomenon this store simply does not have skips with a reason.
    """

    @classmethod
    def setUpClass(cls):
        ok, sentence = imchat.has_access()
        if not ok:
            raise unittest.SkipTest(f"the live store cannot be read here: {sentence}")
        cls.conn = imchat.connect()
        cls.table_rows = cls.conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        if cls.table_rows < LIVE_MIN_ROWS:
            cls.conn.close()
            cls.conn = None
            raise unittest.SkipTest(
                f"the live store holds fewer than {LIVE_MIN_ROWS:,} messages, too few for "
                "these measurements to mean anything; the fixture tests above cover "
                "every rule")

    @classmethod
    def tearDownClass(cls):
        conn = getattr(cls, "conn", None)
        if conn is not None:
            conn.close()

    def setUp(self):
        _reset_warning_latches()

    def _scalar(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()[0]

    def _rowid_ordered_dates(self):
        """Every qualifying-shaped row's raw date, in ROWID order: the naive read."""
        return [raw for (raw,) in self.conn.execute(
            "SELECT m.date FROM message m "
            "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            "WHERE m.date IS NOT NULL "
            "AND IFNULL(m.associated_message_type, 0) = 0 "
            "AND IFNULL(m.item_type, 0) = 0 "
            "ORDER BY m.ROWID"
        ).fetchall()]

    def test_the_store_is_big_enough_to_be_the_real_one(self):
        qualifying = imchat.count_rows(self.conn)
        table = self._scalar("SELECT COUNT(*) FROM message")
        print(f"\n    message rows: {table}   qualifying: {qualifying}")
        # Most of a real store is messages this plug-in reads: a read that finds half
        # the table or less has stopped matching the schema.
        _assert_live_floor(self, qualifying, table // 2, "qualifying rows")
        self.assertLessEqual(qualifying, table)

    def test_the_date_range_spans_real_history(self):
        oldest, newest = imchat.date_range(self.conn)
        self.assertIsNotNone(oldest)
        self.assertIsNotNone(newest)
        print(f"    date range: {oldest.date()} -> {newest.date()} "
              f"({(newest - oldest).days} days)")
        self.assertIsNotNone(oldest.tzinfo)
        self.assertGreater((newest - oldest).days, 30)

    def test_rowid_order_is_not_chronological_order(self):
        """The measurement the ORDER BY exists for."""
        normalised = [
            raw if raw > NANOSECOND_THRESHOLD else raw * 1_000_000_000
            for raw in self._rowid_ordered_dates()
        ]
        pairwise = 0
        worst = 0
        running = 0
        highest = None
        previous = None
        for value in normalised:
            if previous is not None and value < previous:
                pairwise += 1
                worst = max(worst, previous - value)
            if highest is not None and value < highest:
                running += 1
            previous = value
            highest = value if highest is None else max(highest, value)
        share = pairwise / len(normalised) * 100
        print(f"    ROWID-ordered rows out of chronological order: {pairwise} of "
              f"{len(normalised)} = {share:.1f}% (vs a running maximum: "
              f"{running / len(normalised) * 100:.1f}%)")
        print(f"    worst single step backwards: {worst / 1e9 / 86400:.1f} days")
        if pairwise == 0:
            self.skipTest("ROWID order is already chronological on this store (no "
                          "back-filled rows), so the ORDER BY has nothing to correct here; "
                          "the fixture tests pin it")
        self.assertGreater(share, 0.0)

    def test_the_read_itself_comes_back_in_order(self):
        """Also pins count_rows and date_range to what is actually yielded.

        The store is LIVE — the member may receive a text while this runs — so
        the three readings are bracketed rather than pinned to each other
        exactly. A new message can only ever push the count and the newest
        moment upwards, never down, and cannot change the oldest.
        """
        count_before = imchat.count_rows(self.conn)
        oldest_before, newest_before = imchat.date_range(self.conn)
        moments = [m.dt_local for m in imchat.iter_messages(self.conn)]
        count_after = imchat.count_rows(self.conn)
        oldest_after, newest_after = imchat.date_range(self.conn)

        print(f"    iter_messages yielded: {len(moments)}")
        _assert_non_decreasing(self, moments, "the live read")
        self.assertLessEqual(count_before, len(moments))
        self.assertLessEqual(len(moments), count_after)
        self.assertEqual(moments[0], oldest_before)
        self.assertEqual(moments[0], oldest_after)
        self.assertLessEqual(newest_before, moments[-1])
        self.assertLessEqual(moments[-1], newest_after)

    def test_the_ordering_assertion_would_fail_on_the_live_rowid_order(self):
        """NEGATIVE CONTROL — the real assertion, fed a live ROWID-ordered read."""
        zone = imchat._zone()
        moments = [imchat._apple_to_dt(raw, zone) for raw in self._rowid_ordered_dates()]
        if all(b >= a for a, b in zip(moments, moments[1:])):
            self.skipTest("ROWID order is already chronological on this store, so a "
                          "ROWID-ordered read gives the assertion nothing to catch")
        with self.assertRaises(self.failureException):
            _assert_non_decreasing(self, moments, "a live ROWID-ordered read")

    def test_the_exclusions_are_excluding_something_real(self):
        # Measured FIRST: the store only grows, and a new row can only raise the
        # right-hand side below (a new reaction adds one to the table AND one to the
        # excluded count), so a read taken first can never overtake it.
        qualifying = imchat.count_rows(self.conn)
        reactions = self._scalar(
            "SELECT COUNT(*) FROM message WHERE IFNULL(associated_message_type, 0) <> 0")
        items = self._scalar(
            "SELECT COUNT(*) FROM message WHERE IFNULL(item_type, 0) <> 0")
        edited = self._scalar(
            "SELECT COUNT(*) FROM message WHERE IFNULL(date_edited, 0) > 0")
        print(f"    reactions excluded: {reactions}   system events excluded: {items}"
              f"   edited (kept): {edited}")
        if reactions + items == 0:
            self.skipTest("this store holds no reaction and no system event, so the "
                          "exclusions have nothing to leave out here")
        table = self._scalar("SELECT COUNT(*) FROM message")
        excluded = self._scalar(
            "SELECT COUNT(*) FROM message WHERE IFNULL(associated_message_type, 0) <> 0 "
            "OR IFNULL(item_type, 0) <> 0")
        self.assertLessEqual(
            qualifying, table - excluded,
            "the read counts more rows than are left once every reaction and system "
            "event is set aside, so some of them are being read as messages")

    def test_is_system_message_is_zero_on_every_row(self):
        spread = self.conn.execute(
            "SELECT is_system_message, COUNT(*) FROM message GROUP BY 1").fetchall()
        print(f"    is_system_message spread: {spread}")
        self.assertEqual({flag for flag, _ in spread}, {0},
                         "is_system_message is no longer uniformly 0; the reason "
                         "this module ignores it may have changed")

    def test_the_summary_blob_is_useless_as_an_edit_signal(self):
        total = self._scalar("SELECT COUNT(*) FROM message")
        summarised = self._scalar(
            "SELECT COUNT(*) FROM message WHERE message_summary_info IS NOT NULL")
        edited = self._scalar(
            "SELECT COUNT(*) FROM message WHERE IFNULL(date_edited, 0) > 0")
        print(f"    message_summary_info non-null: {summarised} of {total} = "
              f"{summarised / total * 100:.1f}%   date_edited > 0: {edited}")
        if summarised == 0:
            self.skipTest("no row on this store carries the summary blob, so there is "
                          "nothing to mistake for an edit here")
        self.assertLess(edited, summarised / 10,
                        "the summary blob is no longer far more common than a real edit; "
                        "the reason edits are gated on date_edited may have changed")

    def test_handles_are_missing_often_enough_for_none_to_be_a_real_case(self):
        inbound = self._scalar(
            "SELECT COUNT(*) FROM message WHERE IFNULL(handle_id, 0) = 0 AND is_from_me = 0")
        outbound = self._scalar(
            "SELECT COUNT(*) FROM message WHERE IFNULL(handle_id, 0) = 0 AND is_from_me = 1")
        # An independent count of qualifying-shaped rows with no counterpart at all.
        raw_none = self._scalar(
            "SELECT COUNT(*) FROM message m "
            "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            "JOIN chat ch ON ch.ROWID = cmj.chat_id "
            "LEFT JOIN handle h ON h.ROWID = m.handle_id "
            "WHERE m.date IS NOT NULL "
            "AND IFNULL(m.associated_message_type, 0) = 0 "
            "AND IFNULL(m.item_type, 0) = 0 "
            "AND IFNULL(h.id, '') = ''")
        qualifying_none = sum(
            1 for m in imchat.iter_messages(self.conn) if m.handle is None)
        print(f"    handle_id = 0 in the table: {inbound} inbound, {outbound} outbound")
        print(f"    handle is None after the exclusions: {qualifying_none}")
        if raw_none == 0:
            self.skipTest("every message on this store has a counterpart handle, so the "
                          "None case cannot be seen live here; the fixture pins it")
        _assert_live_floor(self, qualifying_none, 0, "rows with no counterpart handle")

    def test_group_chats_with_only_two_participants_exist(self):
        two_person_groups = {rowid for (rowid,) in self.conn.execute(
            "SELECT ch.ROWID FROM chat ch "
            "JOIN chat_handle_join chj ON chj.chat_id = ch.ROWID "
            f"WHERE ch.style = {imchat.GROUP_STYLE} "
            "GROUP BY ch.ROWID HAVING COUNT(*) = 2")}
        all_groups = self._scalar(
            f"SELECT COUNT(*) FROM chat WHERE style = {imchat.GROUP_STYLE}")
        print(f"    group chats: {all_groups}, of which two-participant: "
              f"{len(two_person_groups)}")
        read_as = [m.is_group for m in imchat.iter_messages(self.conn)
                   if m.chat_rowid in two_person_groups]
        if not read_as:
            self.skipTest("this store has no message in a group chat with exactly two "
                          "participants, so the rule cannot be seen live here; the "
                          "fixture pins it")
        self.assertTrue(all(read_as),
                        "a message in a two-participant GROUP chat was read as "
                        "one-to-one: the group rule is counting heads again")

    def test_the_decoder_really_is_the_primary_path(self):
        """Over recent rows only: the claim is about how Messages stores a body TODAY."""
        since = _apple_ns(datetime.now().astimezone() - timedelta(days=LIVE_RECENT_DAYS))
        recent = self._scalar("SELECT COUNT(*) FROM message WHERE date > ?", (since,))
        if recent < LIVE_MIN_ROWS // 10:
            self.skipTest(f"too few messages in the last {LIVE_RECENT_DAYS} days to "
                          "measure how Messages stores a body today")
        with_text = self._scalar(
            "SELECT COUNT(*) FROM message WHERE date > ? AND text IS NOT NULL", (since,))
        with_blob = self._scalar(
            "SELECT COUNT(*) FROM message WHERE date > ? AND attributedBody IS NOT NULL",
            (since,))
        print(f"    last {LIVE_RECENT_DAYS} days: text non-null: {with_text} "
              f"({with_text / recent * 100:.1f}%)   attributedBody non-null: {with_blob} "
              f"({with_blob / recent * 100:.1f}%)")
        if with_text / recent >= 0.6:
            self.skipTest("the text column still carries most recent bodies on this Mac, "
                          "so the decoder is not the main read path here; it stays "
                          "correct either way")
        self.assertGreater(with_blob / recent, 0.9,
                           "the text column is mostly empty and the blob is missing too, "
                           "so recent bodies have nowhere to be read from")

    def test_the_bodies_that_come_back_have_the_expected_shape(self):
        unreadable = wordless = words = 0
        masked = None
        for message in imchat.iter_messages(self.conn):
            if message.text is None:
                unreadable += 1
            elif message.text == "":
                wordless += 1
            else:
                words += 1
            if masked is None and message.handle:
                masked = _mask(message.handle)
        read = words + wordless + unreadable
        print(f"    bodies: {words} with words, {wordless} wordless (attachments), "
              f"{unreadable} unreadable")
        print(f"    masked sample handle: {masked}")
        _assert_live_floor(self, words, read // 10, "messages carrying words")
        self.assertLess(unreadable / read, 0.01,
                        "more than 1% of bodies could not be read at all")

    def test_the_window_narrows_the_live_read(self):
        oldest, newest = imchat.date_range(self.conn)
        if (newest - oldest).days <= 7:
            self.skipTest("the whole store fits inside one week, so a week's window "
                          "cannot be narrower than it")
        since = newest - timedelta(days=7)
        week = sum(1 for _ in imchat.iter_messages(self.conn, since, None))
        everything = imchat.count_rows(self.conn)
        print(f"    last 7 days: {week} messages (of {everything})")
        self.assertLess(week, everything)
        self.assertGreater(week, 0)


class TestTheLiveFloorAssertionBites(unittest.TestCase):
    """NEGATIVE CONTROL — the shared live floor must fail when it should."""

    def test_a_zero_count_fails(self):
        with self.assertRaises(self.failureException):
            _assert_live_floor(self, 0, LIVE_MIN_ROWS, "a store that read nothing")

    def test_exactly_the_floor_fails(self):
        with self.assertRaises(self.failureException):
            _assert_live_floor(self, LIVE_MIN_ROWS, LIVE_MIN_ROWS, "exactly the floor")

    def test_one_over_the_floor_passes(self):
        _assert_live_floor(self, LIVE_MIN_ROWS + 1, LIVE_MIN_ROWS, "one over")


if __name__ == "__main__":
    unittest.main(verbosity=2)
