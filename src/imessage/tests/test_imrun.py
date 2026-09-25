"""imrun — the daily pipeline, end to end, on a temp vault, a temp DB and a temp home.

Every run here goes through the REAL engine (resolver, single card writer, projector,
accept and dismiss doors) inside :mod:`imfixture`'s temp tree, reads a small
Messages-shaped database built in a temp folder through the real ``imchat`` reader
(``mode=ro``), and keeps its ledger, state and transcripts in a temp plug-in home.
Nothing here reads the member's chat.db, Contacts, cards or memory.db; the one real
place looked at is the fixture's read-only trace check.

What each group holds still
---------------------------
* **Queue** — a resolved day queues the contract's record; a re-run queues ZERO and
  says why; today's still-open day is never consumed.
* **Late messages** — an extra message on a queued day and on a stamped day: still one
  record, still one line, the transcript rewritten whole; a row that ARRIVES late with
  an OLD date is found by the ROWID watermark (a date watermark would never read it).
* **Fold** — a 1:1 plus two groups on one day is one unit; a silent group member gets
  nothing; the member is never a counterpart, not even through a handle they forgot.
* **Resolver branches and the status x operation grid** — the member's ruling of 2026-09-24: a
  new number is HELD on the texts review list and the engine's proposal table gains zero
  rows (new_stub-shaped, add_identifier-shaped, ambiguous, unresolved, seen again under a
  drifted name); a dismissed number stays dismissed even after later texts (re-opened
  only when a caller turns that on); a card raised by another pass is linked, never
  siblinged; E1 end to end through the engine's own accept door still attaches the
  number; ``emit_new=True`` still raises, for CP6's review.
* **Stale fallback + G1** — a day past ``synth_stale_days`` lands with its mechanical
  line and its ``interaction`` row survives a close and a reopen; a claim holds it
  back; ``max_stamps_per_run`` and the budget bound it; ``stamped=False`` is recorded
  and the day stays queued.
* **Recovery** — an intent killed after its card write settles to done (and is
  projected); one killed before it is dropped and lands later.
* **Nothing written** — a dry run and every gate leave every byte where it was; the
  write-path sweep proves a real run writes nowhere but its own places.

Privacy: every name, number and address is invented (``555-01xx``, ``example.com``).
"""

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_HOME))

# imconfig FIRST, before anything reachable only because it put the engine on sys.path.
import imconfig  # noqa: E402

imconfig.ensure_engine_path()

import builtins  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
import stat  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from datetime import UTC, date, datetime, timedelta  # noqa: E402
from unittest import mock  # noqa: E402

import imchat  # noqa: E402
import imessage  # noqa: E402
import imledger  # noqa: E402
import imrun  # noqa: E402
import imspine  # noqa: E402
import imthreads  # noqa: E402
from imfixture import (  # noqa: E402
    ALICE,
    BOB,
    CAROL,
    DAN,
    DEE,
    ERIN,
    SHARED_LANDLINE,
    CardSpec,
    SpineFixture,
    SpineTestCase,
    patched,
    real_fixture_traces,
    real_state,
)

import people_resolve  # noqa: E402
import people_stamp  # noqa: E402

# ---------------------------------------------------------------------------
# The cast. Invented: 555-01xx is the reserved fictional block, example.com is RFC 2606.
# ---------------------------------------------------------------------------

ZONE = UTC
ME = "+15555550199"
ALICE_PHONE = ALICE.phones[0]
CAROL_PHONE = CAROL.phones[0]
BOB_EMAIL = BOB.emails[0]
FRANK = "+15555550160"       # unknown, a Contacts name, no card -> new_stub
ERIN_NEW = "+15555550161"    # unknown number, Contacts says "Fixture Erin" -> add_identifier
LOOSE = "+15555550163"       # forced 'unresolved' by a stub resolver
OWNER_PHONE = "+15555550177"  # on the member's OWN card, but not in own_handles
OWNER_EMAIL = "me@fixture.example.com"

OWNER = CardSpec("fixture-owner", "Fixture Owner", "prs_fxownxx2",
                 phones=(OWNER_PHONE,), emails=(OWNER_EMAIL,), category="family")

CONTACTS = {
    ALICE_PHONE: "Fixture Alice",
    CAROL_PHONE: "Fixture Carol",
    BOB_EMAIL: "Fixture Bob",
    FRANK: "Fixture Frank",
    ERIN_NEW: "Fixture Erin",
    SHARED_LANDLINE: "Fixture Dan",
}

LONG_A = "Can we move the appointment to four o'clock, or is the morning easier for you?"
LONG_B = "Four works fine for me, I will let the front desk know and see you then."
LATE = "One more thing: bring the forms from last week, the office asked for them again."

D0 = date(2026, 2, 20)
D1 = date(2026, 3, 2)
D2 = date(2026, 3, 3)
D3 = date(2026, 3, 4)
D4 = date(2026, 3, 5)
D5 = date(2026, 3, 6)
D6 = date(2026, 3, 7)


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE)


def morning(day):
    return at(day, 6)


def key(ident, day):
    return imthreads.ledger_key(ident, imthreads.DAY_GRAIN, day)


# ---------------------------------------------------------------------------
# A Messages-shaped database, grown between runs the way iCloud grows the real one.
# ---------------------------------------------------------------------------

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)

CHAT_SCHEMA = """
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    date INTEGER, is_from_me INTEGER, handle_id INTEGER, text TEXT, attributedBody BLOB,
    associated_message_type INTEGER, item_type INTEGER, is_system_message INTEGER,
    date_edited INTEGER, message_summary_info BLOB
);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, style INTEGER, display_name TEXT,
                   chat_identifier TEXT);
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
"""


def _raw(moment):
    delta = moment - APPLE_EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


class ChatDb:
    """A small ``chat.db``: rows get ROWIDs in ARRIVAL order, whatever their date."""

    def __init__(self, path):
        self.path = Path(path)
        self._handles = {}
        self._run("script", CHAT_SCHEMA)

    def _run(self, kind, sql, params=()):
        con = sqlite3.connect(str(self.path))
        try:
            if kind == "script":
                con.executescript(sql)
                result = None
            else:
                result = con.execute(sql, params).lastrowid
            con.commit()
            return result
        finally:
            con.close()

    def chat(self, rowid, *, group=False, name=None):
        style = imchat.GROUP_STYLE if group else imchat.ONE_TO_ONE_STYLE
        self._run("one", "INSERT INTO chat VALUES (?, ?, ?, ?)",
                  (rowid, style, name, f"chat-{rowid}"))
        return rowid

    def _handle(self, ident):
        if ident is None:
            return 0
        if ident not in self._handles:
            self._handles[ident] = self._run("one", "INSERT INTO handle (id) VALUES (?)", (ident,))
        return self._handles[ident]

    def say(self, chat, when, text, *, sender=None, to=None):
        """``sender`` spoke (inbound); ``sender=None`` is the member (outbound to ``to``)."""
        from_me = sender is None
        handle = self._handle(to if from_me else sender)
        rowid = self._run(
            "one",
            "INSERT INTO message (date, is_from_me, handle_id, text, attributedBody, "
            "associated_message_type, item_type, is_system_message, date_edited, "
            "message_summary_info) VALUES (?, ?, ?, ?, NULL, 0, 0, 0, 0, NULL)",
            (_raw(when), int(from_me), handle, text),
        )
        self._run("one", "INSERT INTO chat_message_join VALUES (?, ?)", (chat, rowid))
        return rowid

    def exchange(self, chat, day, who, *, hour=9, reply=True):
        """They open, the member answers: two turns, two senders, real words."""
        first = self.say(chat, at(day, hour), LONG_A, sender=who)
        if reply:
            self.say(chat, at(day, hour, 5), LONG_B, to=who)
        return first


def _interaction_lines(text):
    out, capturing = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            capturing = line[3:].strip().lower() == "interactions"
            continue
        if capturing and line.startswith("- "):
            out.append(line)
    return out


def _conversation_lines(path, day=None):
    lines = [x for x in _interaction_lines(Path(path).read_text(encoding="utf-8"))
             if "— conversation (" in x]
    return [x for x in lines if day is None or x.startswith(f"- {day.isoformat()} ")]


def _tree_bytes(*roots):
    """``{path: bytes}`` for every file under the roots — "nothing written" is judged on this.

    One exclusion, and only one: SQLite's ``*-shm`` file, the write-ahead log's shared-
    memory index.  EVERY reader of a WAL database rewrites its read marks there, a
    ``mode=ro`` connection included (``TestTheShmExclusionIsHonest`` proves it), so it
    says nothing about whether anything was written.  The database and its ``-wal`` —
    where a write would land — are compared byte for byte.
    """
    found = {}
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.name.endswith("-shm"):
                found[str(path)] = path.read_bytes()
    return found


def _changed(before, after):
    """The paths that appeared, vanished or changed — never a byte diff of a database."""
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


# ---------------------------------------------------------------------------
# The harness.
# ---------------------------------------------------------------------------


class RunCase(SpineTestCase):
    """A fixture vault + DB, a temp plug-in home, and a temp Messages database."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.threads = self.home / "threads"
        self.chats = ChatDb(self.root / "chat.db")
        self.cfg = {**imconfig.DEFAULTS, "own_handles": [ME], "never_ingest": []}
        self.contacts = dict(CONTACTS)
        self.stderr = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k != "GLITCH_BUDGET_S"}
        for patch in (
            mock.patch.object(imchat, "_is_macos", lambda: True),
            mock.patch.object(imchat, "_zone", lambda: ZONE),
            mock.patch.dict(os.environ, env, clear=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        imchat._warned_unreadable = False

    def daily(self, now, **kw):
        kw.setdefault("source", imrun.ChatDbSource(self.chats.path))
        kw.setdefault("cfg", self.cfg)
        kw.setdefault("contacts", self.contacts)
        with contextlib.redirect_stderr(self.stderr):
            return imrun.daily(now=now, home=self.home, threads_dir=self.threads,
                               db_path=self.fx.db_path, **kw)

    @contextlib.contextmanager
    def ledger(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with imledger.session(contextlib.nullcontext(), home=self.home) as led:
                yield led

    def state(self):
        return imledger.load_state(self.home)

    def proposals(self, ident=None):
        rows = self.fx.reopen().execute(
            "SELECT id, kind, status, payload FROM person_proposal ORDER BY rowid"
        ).fetchall()
        out = []
        for row in rows:
            payload = json.loads(row[3])
            if ident is None or payload.get("identifier") == ident:
                out.append((row[0], row[1], row[2], payload))
        return out

    def alice_day(self, day=D1, chat=1):
        with contextlib.suppress(sqlite3.IntegrityError):
            self.chats.chat(chat)
        return self.chats.exchange(chat, day, ALICE_PHONE)


# ---------------------------------------------------------------------------
# 1. A resolved day queues — the contract's record — and a re-run queues nothing.
# ---------------------------------------------------------------------------


class TestQueue(RunCase):
    def test_a_resolved_day_queues_the_contract_record(self):
        self.alice_day()
        report = self.daily(morning(D2))

        self.assertIsNone(report["paused"])
        self.assertEqual(report["queued"], 1)
        with self.ledger() as led:
            queue = led.queued()
            self.assertEqual(led.counts()["stamped"], 0)
        record = queue[key(ALICE_PHONE, D1)]
        link = "_local/imessage/threads/2026/2026-03-02-fixture-alice-1.txt"
        self.assertEqual(record["identifier"], ALICE_PHONE)
        self.assertEqual(record["person_id"], ALICE.pid)
        self.assertEqual(record["day"], "2026-03-02")
        self.assertEqual(record["links"], [link])
        self.assertEqual(record["direction"], "they_reached_out")
        self.assertEqual(record["turns"], 2)
        self.assertEqual(record["topic"], f"Texts (2): {LONG_A}")
        self.assertEqual(record["queued_at"], morning(D2).isoformat())

        thread = self.threads / "2026" / "2026-03-02-fixture-alice-1.txt"
        self.assertTrue(thread.is_file(), "the transcript a queued day links to must exist")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(thread.stat().st_mode), 0o600)
        # queued, NOT stamped: the summary pass (or the stale fallback) lands it
        self.assertEqual(_conversation_lines(self.cards["fixture-alice"].path), [])

        state = self.state()
        self.assertEqual(state["watermark"], 2)
        self.assertEqual(state["watermark_kind"], "rowid")
        self.assertTrue(report["watermark"]["first_run"])
        self.assertEqual(report["zone"], "UTC")

    def test_re_running_queues_zero_and_says_why(self):
        self.alice_day()
        self.daily(morning(D2))
        again = self.daily(morning(D2))
        self.assertEqual(again["queued"], 0)
        self.assertIn("no new texts have arrived since the last run", again["why_nothing_queued"])
        with self.ledger() as led:
            self.assertEqual(list(led.queued()), [key(ALICE_PHONE, D1)])

    def test_the_first_run_reads_yesterday_only(self):
        self.chats.chat(1)
        self.chats.exchange(1, D0, ALICE_PHONE)   # older history: backfill's job
        self.chats.exchange(1, D1, ALICE_PHONE)
        report = self.daily(morning(D2))
        with self.ledger() as led:
            self.assertEqual(list(led.queued()), [key(ALICE_PHONE, D1)])
        self.assertEqual(report["read"]["days"], 1)

    def test_todays_open_day_is_never_consumed(self):
        self.alice_day()                                   # rows 1, 2 (yesterday)
        today_row = self.chats.say(1, at(D2, 5), LONG_A, sender=ALICE_PHONE)  # row 3 (today)
        report = self.daily(morning(D2))
        self.assertEqual(self.state()["watermark"], today_row - 1)
        self.assertTrue(report["watermark"]["held_by_open_day"])

        # A later arrival for today, then the next run: only-today rows queue nothing yet.
        self.chats.say(1, at(D2, 5, 30), LONG_B, to=ALICE_PHONE)
        again = self.daily(at(D2, 7))
        self.assertEqual(again["queued"], 0)
        self.assertIn("only new texts are from today", again["why_nothing_queued"])
        self.assertEqual(self.state()["watermark"], today_row - 1)

        # Once today closes, the next run reads it.
        after = self.daily(morning(D3))
        self.assertEqual(after["queued"], 1)
        with self.ledger() as led:
            self.assertIn(key(ALICE_PHONE, D2), led.queued())


class TestAnEmptyStore(RunCase):
    """A Messages store with ZERO rows (a new Mac, a Messages that holds nothing yet)."""

    def test_zero_rows_run_after_run_is_a_quiet_no_op(self):
        first = self.daily(morning(D2))
        self.assertIsNone(first["paused"])
        self.assertEqual(self.state()["watermark"], 0, "the premise: an empty store reads as 0")
        for later in (morning(D3), morning(D4)):
            report = self.daily(later)
            self.assertIsNone(report["paused"])
            mark = report["watermark"]
            self.assertFalse(mark["store_reset"], "an empty store read as a rebuilt one")
            self.assertFalse(mark["first_run"])
            self.assertFalse(mark["moved"])
            self.assertEqual(self.state()["watermark"], 0)
            self.assertEqual(report["why_nothing_queued"],
                             "no new texts have arrived since the last run")
            self.assertNotIn("rebuilt", imessage.daily_render(report))

    def test_the_first_texts_after_an_empty_start_are_read(self):
        self.daily(morning(D1))                          # empty: watermark 0
        self.alice_day(D1)
        report = self.daily(morning(D2))
        self.assertFalse(report["watermark"]["store_reset"])
        self.assertEqual(report["queued"], 1)

    def test_a_store_that_emptied_after_rows_were_read_is_still_a_rebuild(self):
        self.alice_day()
        self.daily(morning(D2))
        self.assertEqual(self.state()["watermark"], 2)
        emptied = ChatDb(self.root / "emptied-chat.db")
        report = self.daily(morning(D3), source=imrun.ChatDbSource(emptied.path))
        self.assertTrue(report["watermark"]["store_reset"])
        self.assertIn("rebuilt", imessage.daily_render(report))

    def test_the_rebuild_rule_itself(self):
        cases = {(0, None): False, (5, None): True, (0, 3): False, (5, 5): False,
                 (5, 9): False, (5, 4): True}
        for (before, high), expected in cases.items():
            with self.subTest(before=before, high=high):
                self.assertIs(imrun.store_rebuilt(before, high), expected)


class TestMatchRoute(RunCase):
    """Every resolved number records HOW it matched, for review's "check these matches"."""

    SUFFIX_ONLY = "+5555550142"   # Alice's +15555550142 by its last ten digits, no more

    def test_an_exact_number_and_a_suffix_only_number_are_told_apart(self):
        self.alice_day()
        self.chats.chat(8)
        self.chats.exchange(8, D1, self.SUFFIX_ONLY)
        self.daily(morning(D2))
        with self.ledger() as led:
            exact = led.identifier(ALICE_PHONE)
            suffix = led.identifier(self.SUFFIX_ONLY)
            queue = led.queued()
        self.assertEqual((exact["state"], exact["match"]), ("accepted", "exact"))
        self.assertEqual((suffix["state"], suffix["match"], suffix["person_id"]),
                         ("accepted", "suffix", ALICE.pid))
        # the premise E5b states: the suffix number is filed on Alice's card, silently
        self.assertEqual(queue[key(self.SUFFIX_ONLY, D1)]["person_id"], ALICE.pid)
        route, card_value = imspine.match_route(self.fx.reopen(), self.SUFFIX_ONLY, ALICE.pid)
        self.assertEqual((route, card_value), ("suffix", ALICE_PHONE))


# ---------------------------------------------------------------------------
# 2. Late messages.
# ---------------------------------------------------------------------------


class TestLateMessages(RunCase):
    def test_a_late_message_on_a_queued_day_keeps_one_frozen_record(self):
        self.alice_day()
        self.daily(morning(D2))
        with self.ledger() as led:
            before = led.queued()[key(ALICE_PHONE, D1)]
        self.chats.say(1, at(D1, 11), LATE, sender=ALICE_PHONE)   # arrives late

        report = self.daily(morning(D3))
        self.assertEqual(report["queued"], 0)
        self.assertEqual(report["already_queued"], 1)
        with self.ledger() as led:
            queue = led.queued()
        self.assertEqual(list(queue), [key(ALICE_PHONE, D1)])
        self.assertEqual(queue[key(ALICE_PHONE, D1)], before, "the queued record is frozen")
        thread = self.threads / "2026" / "2026-03-02-fixture-alice-1.txt"
        self.assertEqual(len(thread.read_text(encoding="utf-8").splitlines()), 3,
                         "the transcript is rewritten whole with the late message")

    def test_a_new_conversation_on_a_queued_day_joins_its_links_and_nothing_else(self):
        """Carried from CP4: the queued record stays frozen, but a chat that syncs late
        onto a day already queued must reach the summary writer, who reads only links."""
        self.alice_day()                                                # chat 1, D1
        self.daily(morning(D2))
        with self.ledger() as led:
            before = led.queued()[key(ALICE_PHONE, D1)]
        self.chats.chat(20, group=True, name="Walk")
        self.chats.say(20, at(D1, 18), LONG_A, sender=ALICE_PHONE)      # a NEW chat, late
        self.chats.say(20, at(D1, 18, 1), LONG_B, sender=CAROL_PHONE)
        walk = "_local/imessage/threads/2026/2026-03-02-walk-20.txt"

        dry = self.daily(morning(D3), dry_run=True)
        self.assertEqual(dry["links_refreshed"], 1)
        with self.ledger() as led:
            self.assertEqual(led.queued()[key(ALICE_PHONE, D1)], before, "a dry run wrote")

        report = self.daily(morning(D3))
        self.assertEqual(report["links_refreshed"], 1)
        self.assertEqual(report["already_queued"], 1)
        self.assertIn("late conversation", imessage.daily_render(report))
        with self.ledger() as led:
            after = led.queued()[key(ALICE_PHONE, D1)]
            self.assertIn(key(CAROL_PHONE, D1), led.queued())  # Carol's own day is new
        self.assertEqual(after["links"], [*before["links"], walk], "the first link moved")
        self.assertEqual({k: v for k, v in after.items() if k != "links"},
                         {k: v for k, v in before.items() if k != "links"},
                         "the frozen record changed (topic, turns or queued_at)")
        self.assertTrue((self.threads / "2026" / Path(walk).name).is_file())

        quiet = self.daily(morning(D3) + timedelta(hours=1))
        self.assertEqual(quiet["links_refreshed"], 0)

    def test_a_late_message_on_a_stamped_day_is_still_one_line(self):
        self.alice_day()
        self.daily(morning(D2))
        stamped = self.daily(morning(D2) + timedelta(days=3))
        self.assertEqual(stamped["stale"]["stamped"], 1)
        card = self.cards["fixture-alice"].path
        self.assertEqual(len(_conversation_lines(card, D1)), 1)

        self.chats.say(1, at(D1, 11), LATE, sender=ALICE_PHONE)
        report = self.daily(morning(D2) + timedelta(days=4))
        self.assertEqual(report["already_stamped"], 1)
        self.assertEqual(report["queued"], 0)
        self.assertEqual(len(_conversation_lines(card, D1)), 1, "a late message minted a line")
        thread = self.threads / "2026" / "2026-03-02-fixture-alice-1.txt"
        self.assertIn("the office asked for them again", thread.read_text(encoding="utf-8"))

    def test_a_row_that_arrives_late_with_an_old_date_is_read(self):
        self.alice_day()
        self.daily(morning(D2))
        newest_read = self.state()["newest_read_at"]
        self.chats.chat(7)
        self.chats.exchange(7, D0, BOB_EMAIL)   # dated ten days back, arriving now

        report = self.daily(morning(D3))
        self.assertEqual(report["queued"], 1)
        with self.ledger() as led:
            self.assertIn(key(BOB_EMAIL, D0), led.queued())
        # The negative control: every one of those rows is dated BEFORE the newest message
        # the last run consumed, so a date watermark would have skipped them for good.
        self.assertLess(at(D0, 9, 5), datetime.fromisoformat(newest_read))

    def test_an_old_late_arrival_from_an_unknown_number_raises_no_card(self):
        """A re-sync can deliver a year as "new"; its unknown numbers wait for review."""
        self.alice_day()
        self.daily(morning(D2))
        self.chats.chat(3)
        self.chats.exchange(3, D0, FRANK)   # ten days old, arriving now
        report = self.daily(morning(D3))
        self.assertEqual(report["raised"], {})
        self.assertEqual(report["held"], {"new": 1})
        self.assertEqual(self.proposals(FRANK), [])
        with self.ledger() as led:
            self.assertEqual(led.identifier(FRANK)["state"], "held")
        # the same number texting on a RECENT day is a known, held number: still dry
        self.chats.exchange(3, D2, FRANK)
        again = self.daily(morning(D3) + timedelta(hours=1))
        self.assertEqual(again["raised"], {})


# ---------------------------------------------------------------------------
# 3. The fold, and who earns a day.
# ---------------------------------------------------------------------------


class TestFold(RunCase):
    def test_a_one_to_one_and_two_groups_on_one_day_are_one_unit(self):
        self.chats.chat(1)
        self.chats.chat(10, group=True, name="Garden Group")
        self.chats.chat(11, group=True)
        self.chats.exchange(1, D1, ALICE_PHONE)                           # 1 turn of Alice's
        self.chats.say(10, at(D1, 12), LONG_A, sender=ALICE_PHONE)        # 2 turns of Alice's
        self.chats.say(10, at(D1, 12, 1), LONG_B, sender=CAROL_PHONE)
        self.chats.say(10, at(D1, 12, 2), LATE, sender=ALICE_PHONE)
        self.chats.say(11, at(D1, 15), LONG_A, sender=ALICE_PHONE)        # 1 turn of Alice's
        self.chats.say(11, at(D1, 15, 1), LONG_B)

        self.daily(morning(D2))
        with self.ledger() as led:
            queue = led.queued()
        alice_keys = [k for k in queue if k.startswith(ALICE_PHONE)]
        self.assertEqual(alice_keys, [key(ALICE_PHONE, D1)])
        record = queue[key(ALICE_PHONE, D1)]
        self.assertEqual(len(record["links"]), 3)
        self.assertEqual(record["links"][0],
                         "_local/imessage/threads/2026/2026-03-02-garden-group-10.txt",
                         "primary = the chat-day with the most of her own turns")
        self.assertEqual(record["topic"], "Group texts in Garden Group (3 messages)")
        for link in record["links"]:
            self.assertTrue((self.threads / "2026" / Path(link).name).is_file())
        # Carol spoke in the group, so she earns her own day there.
        self.assertIn(key(CAROL_PHONE, D1), queue)

    def test_a_group_member_who_sent_nothing_gets_nothing(self):
        self.chats.chat(12, group=True, name="Family")
        self.chats.say(12, at(D1, 9), LONG_A, sender=ALICE_PHONE)
        self.chats.say(12, at(D1, 9, 5), LONG_B, to=CAROL_PHONE)   # Carol only a recipient
        self.daily(morning(D2))
        with self.ledger() as led:
            queue, held = led.queued(), led.held(CAROL_PHONE)
        self.assertIn(key(ALICE_PHONE, D1), queue)
        self.assertNotIn(key(CAROL_PHONE, D1), queue)
        self.assertEqual(held, [])


class TestTheMemberIsNeverACounterpart(RunCase):
    def setUp(self):
        super().setUp()
        self.owner = self.fx.write_card(OWNER)
        self.cfg["own_handles"] = [ME, OWNER_EMAIL]

    def test_own_handle_rows_earn_nothing_and_a_forgotten_own_number_lands_nowhere(self):
        self.chats.chat(1)
        self.chats.chat(2)
        # the member writing from another of their numbers, into Alice's thread
        self.chats.say(1, at(D1, 9), LONG_A, sender=ME)
        self.chats.say(1, at(D1, 9, 5), LONG_B, sender=ALICE_PHONE)
        # a number on their OWN card that own_handles does not list
        self.chats.exchange(2, D1, OWNER_PHONE)

        conn = self.fx.reopen()
        self.assertEqual(imrun.owner_person_ids(conn, [ME, OWNER_EMAIL]),
                         frozenset({OWNER.pid}))
        report = self.daily(morning(D2))
        self.assertGreaterEqual(report["owner_skipped"], 1)
        with self.ledger() as led:
            queue = led.queued()
            self.assertEqual(led.held(OWNER_PHONE), [])
            self.assertIsNone(led.identifier(ME))
        self.assertFalse(any(k.startswith(ME) for k in queue))
        self.assertFalse(any(k.startswith(OWNER_PHONE) for k in queue))
        self.assertFalse(any(r.get("person_id") == OWNER.pid for r in queue.values()))
        # days later: the stale fallback files nothing onto their card either
        self.daily(morning(D2) + timedelta(days=3))
        self.assertEqual(_conversation_lines(self.owner.path), [])

    def test_land_refuses_the_members_own_card(self):
        record = {"key": key(OWNER_PHONE, D1), "identifier": OWNER_PHONE,
                  "person_id": OWNER.pid, "day": D1.isoformat(), "direction": "mutual",
                  "links": ["_local/imessage/threads/2026/x-1.txt"], "turns": 2,
                  "topic": "Texts (2)"}
        conn = self.fx.reopen()
        with self.ledger() as led:
            led.begin_run("t")
            led.enqueue(record)
            result = imrun.land(conn, led, [(record, "Texts (2)")],
                                owner_ids=frozenset({OWNER.pid}), now=morning(D2))
            self.assertFalse(led.is_queued(record["key"]))
            led.commit_run("t")
        self.assertEqual(list(result.refused), [record["key"]])
        self.assertEqual(result.stamped, [])
        self.assertEqual(_conversation_lines(self.owner.path), [])


# ---------------------------------------------------------------------------
# 4. The four resolver branches.
# ---------------------------------------------------------------------------


def _proposal_rows(fx):
    """How many rows the engine's person_proposal table holds, on a fresh connection."""
    return fx.reopen().execute("SELECT COUNT(*) FROM person_proposal").fetchone()[0]


class TestResolverBranches(RunCase):
    """The member's ruling, 2026-09-24: a new number waits on the texts review list, never on
    the main people queue.  ``daily`` looks every new number up DRY and holds it."""

    def test_a_new_number_is_held_for_review_and_nothing_is_raised(self):
        self.chats.chat(3)
        self.chats.exchange(3, D1, FRANK)
        # the premise: the engine WOULD raise a new card for this number if asked to
        would = imspine.resolve(self.fx.reopen(), FRANK, "Fixture Frank", emit=False)
        self.assertEqual((would.status, would.proposal_kind), ("proposed", "new_stub"))
        before = _proposal_rows(self.fx)

        report = self.daily(morning(D2))
        self.assertEqual(report["raised"], {})
        self.assertEqual(report["held"], {"new": 1})
        self.assertEqual((report["new_for_review"], report["awaiting_review"],
                          report["awaiting_yes"]), (1, 1, 0))
        self.assertEqual(_proposal_rows(self.fx), before, "daily put a card on the main queue")
        self.assertEqual(self.proposals(FRANK), [])
        with self.ledger() as led:
            ident = led.identifier(FRANK)
            held = led.held(FRANK)
        self.assertEqual((ident["state"], ident["hold_reason"]), ("held", "new"))
        self.assertIsNone(ident.get("proposal_id"))
        self.assertEqual([u["key"] for u in held], [key(FRANK, D1)])
        self.assertEqual(held[0]["links"], [
            "_local/imessage/threads/2026/2026-03-02-fixture-frank-3.txt"])
        text = imessage.daily_render(report)
        self.assertIn("1 new number(s) went onto your texts review list", text)
        self.assertIn("1 number(s) on your texts review list", text)

    def test_a_known_name_on_a_new_number_is_held_and_nothing_is_raised(self):
        self.chats.chat(4)
        self.chats.exchange(4, D1, ERIN_NEW)
        would = imspine.resolve(self.fx.reopen(), ERIN_NEW, "Fixture Erin", emit=False)
        self.assertEqual((would.status, would.proposal_kind), ("proposed", "add_identifier"))
        before = _proposal_rows(self.fx)
        report = self.daily(morning(D2))
        self.assertEqual(report["raised"], {})
        self.assertEqual(report["held"], {"new": 1})
        self.assertEqual(_proposal_rows(self.fx), before)
        self.assertEqual(self.proposals(ERIN_NEW), [])

    def test_unknown_numbers_across_runs_add_zero_proposal_rows(self):
        """The ruling's acceptance test: new_stub-shaped, add_identifier-shaped, ambiguous,
        seen again under a drifted name, texted on a later day, and a dry run too: the
        engine's person_proposal table gains ZERO rows."""
        for chat, who in ((3, FRANK), (4, ERIN_NEW), (5, SHARED_LANDLINE)):
            self.chats.chat(chat)
            self.chats.exchange(chat, D1, who)
        before = _proposal_rows(self.fx)
        first = self.daily(morning(D2))
        self.assertEqual(first["new_for_review"], 3)
        self.assertEqual(first["awaiting_review"], 3)
        self.contacts[FRANK] = "Frank Drifted"
        for chat, who in ((3, FRANK), (4, ERIN_NEW)):
            self.chats.exchange(chat, D2, who)
        self.daily(morning(D2), dry_run=True)
        second = self.daily(morning(D3))
        self.assertEqual(second["new_for_review"], 0, "a known number counted as new again")
        self.assertEqual(second["awaiting_review"], 3)
        for report in (first, second):
            self.assertEqual(report["raised"], {})
        self.assertEqual(_proposal_rows(self.fx), before)
        status = imrun.status(home=self.home, now=morning(D3))
        self.assertEqual(status["ledger"]["awaiting_review"], 3)
        self.assertEqual(status["ledger"]["awaiting_yes"], 0)
        self.assertIn("On your texts review list: 3 number(s)", imessage.status_render(status))

    def test_with_raising_turned_on_a_new_number_raises_its_card(self):
        """``emit_new`` stays, for CP6's review to raise a card on the member's word."""
        self.chats.chat(3)
        self.chats.exchange(3, D1, FRANK)
        report = self.daily(morning(D2), emit_new=True)
        self.assertEqual(report["raised"], {"new_stub": 1})
        self.assertEqual(report["held"], {"pending": 1})
        props = self.proposals(FRANK)
        self.assertEqual([(k, s) for _, k, s, _ in props], [("new_stub", "pending")])
        with self.ledger() as led:
            self.assertEqual(led.identifier(FRANK)["proposal_id"], props[0][0])

    def test_ambiguous_is_held_and_raises_nothing(self):
        self.chats.chat(5)
        self.chats.exchange(5, D1, SHARED_LANDLINE)
        before = len(self.proposals())
        report = self.daily(morning(D2))
        self.assertEqual(report["held"], {"ambiguous": 1})
        self.assertEqual(report["raised"], {})
        self.assertEqual(len(self.proposals()), before, "an ambiguous number raised a card")
        with self.ledger() as led:
            ident = led.identifier(SHARED_LANDLINE)
        self.assertEqual(ident["state"], "ambiguous")
        self.assertEqual(ident["candidates"], sorted([DAN.pid, DEE.pid]))

    def test_unresolved_is_held(self):
        real = imspine.resolve

        def resolve(conn, identifier, name=None, *, emit):
            if identifier == LOOSE:
                return people_resolve.ResolveResult("unresolved", detail="nothing to match")
            return real(conn, identifier, name, emit=emit)

        self.chats.chat(6)
        self.chats.exchange(6, D1, LOOSE)
        with mock.patch.object(imspine, "resolve", resolve):
            report = self.daily(morning(D2))
        self.assertEqual(report["held"], {"unresolved": 1})
        self.assertEqual(report["raised"], {})
        with self.ledger() as led:
            self.assertEqual(led.identifier(LOOSE)["state"], "held")


# ---------------------------------------------------------------------------
# 5. The status x operation grid.
# ---------------------------------------------------------------------------


class TestGrid(RunCase):
    def frank(self, day, name=None, hour=9):
        with contextlib.suppress(sqlite3.IntegrityError):
            self.chats.chat(3)
        if name is not None:
            self.contacts[FRANK] = name
        self.chats.exchange(3, day, FRANK, hour=hour)

    def test_seen_again_under_a_drifted_name_raises_nothing(self):
        self.frank(D1)
        self.daily(morning(D2))
        self.frank(D2, name="Frank Drifted")
        report = self.daily(morning(D3))
        self.assertEqual(report["raised"], {})
        self.assertEqual(self.proposals(FRANK), [])
        with self.ledger() as led:
            self.assertEqual(len(led.held(FRANK)), 2)
            self.assertEqual(led.identifier(FRANK)["days"], 2)
            self.assertEqual(led.identifier(FRANK)["state"], "held")

    def test_a_dismissed_number_stays_dismissed_even_after_later_texts(self):
        """The member's ruling: the run never re-opens a "no" on its own.  The re-open rule is
        still there for a caller that turns it on, on the member's word."""
        stub = self.elsewhere()                      # a card raised by another pass
        self.frank(D1)
        self.daily(morning(D2))
        with self.ledger() as led:
            self.assertEqual(led.identifier(FRANK)["proposal_id"], stub)
        self.frank(D2, name="Frank Drifted")         # texted before the no
        self.fx.dismiss(stub, at="2026-03-04T08:00:00+00:00")   # the no, on D3

        # The guard is what stops a sibling: under the drifted name a real emit WOULD mint one.
        dry = imspine.resolve(self.fx.reopen(), FRANK, "Frank Drifted", emit=False)
        self.assertEqual((dry.status, dry.proposal_kind), ("proposed", "new_stub"))

        silent = self.daily(morning(D4))
        self.assertEqual(silent["raised"], {})
        self.assertEqual(silent["held"], {"dismissed": 1})
        self.assertEqual([(i, s) for i, _, s, _ in self.proposals(FRANK)], [(stub, "dismissed")])

        self.frank(D5)                               # the member texts that number again
        self.assertTrue(imspine.reopen_justified(self.fx.reopen(), stub, "2026-03-06"),
                        "the premise: the old rule WOULD re-open on this evidence")
        still = self.daily(morning(D6))
        self.assertEqual(still["raised"], {})
        self.assertEqual([(i, s) for i, _, s, _ in self.proposals(FRANK)], [(stub, "dismissed")])
        with self.ledger() as led:
            self.assertEqual(led.identifier(FRANK)["state"], "dismissed")
            self.assertEqual(len(led.held(FRANK)), 3)

        # On the member's word (CP6's review), the same evidence re-opens it.
        self.frank(D6)
        on_their_word = self.daily(morning(D6) + timedelta(days=1), reopen=True)
        self.assertEqual(on_their_word["raised"], {"reopened": 1})
        self.assertEqual([(i, s) for i, _, s, _ in self.proposals(FRANK)], [(stub, "pending")])
        log = self.fx.reopen().execute(
            "SELECT action FROM change_log WHERE entity_id = ? ORDER BY id", (stub,)
        ).fetchall()
        self.assertEqual([r[0] for r in log][-1], "pending")

    def elsewhere(self):
        """A card for FRANK raised OUTSIDE this ledger (the email pass, a lost ledger)."""
        res = imspine.resolve(self.conn, FRANK, "Fixture Frank", emit=True)
        self.fx.commit()
        return res.proposal_id

    def test_a_card_dismissed_before_the_ledger_knew_the_number_stays_dismissed(self):
        """E4 on first sighting: the identifier guard, not payload equality, finds the no."""
        stub = self.elsewhere()
        self.fx.dismiss(stub, at="2026-03-02T20:00:00+00:00")   # the same day he texted
        self.frank(D1, name="Frank Drifted")
        report = self.daily(morning(D2))
        self.assertEqual(report["raised"], {})
        self.assertEqual(report["held"], {"dismissed": 1})
        self.assertEqual([(i, s) for i, _, s, _ in self.proposals(FRANK)], [(stub, "dismissed")])
        with self.ledger() as led:
            self.assertEqual(
                (led.identifier(FRANK)["state"], led.identifier(FRANK)["proposal_id"]),
                ("dismissed", stub),
            )

    def test_a_card_pending_from_elsewhere_is_never_siblinged(self):
        stub = self.elsewhere()
        self.frank(D1, name="Frank Drifted")
        report = self.daily(morning(D2))
        self.assertEqual(report["raised"], {})
        self.assertEqual(report["held"], {"pending": 1})
        self.assertEqual([(i, s) for i, _, s, _ in self.proposals(FRANK)], [(stub, "pending")])
        with self.ledger() as led:
            self.assertEqual(led.identifier(FRANK)["proposal_id"], stub)

    def test_e1_accept_then_attach_then_every_held_day_queues(self):
        """The follow-through stays under the ruling: a card the member accepts on the
        main queue (raised by another pass) must still get its number attached."""
        stub = self.elsewhere()
        self.frank(D1)
        first = self.daily(morning(D2))
        self.assertEqual((first["raised"], first["held"]), ({}, {"pending": 1}))
        self.assertEqual(self.proposals(FRANK)[0][0], stub)
        self.assertEqual(self.fx.accept(stub).get("status"), "ok")   # the first yes
        new_person = imspine.accepted_person(self.fx.reopen(), stub)
        self.assertIsNotNone(new_person)
        self.frank(D2)                                               # still texting meanwhile

        second = self.daily(morning(D3))
        self.assertEqual(second["raised"], {"add_identifier": 1})
        with self.ledger() as led:
            ident = led.identifier(FRANK)
            self.assertEqual(len(led.held(FRANK)), 2)
        self.assertEqual((ident["state"], ident["person_id"]), ("attach_pending", new_person))
        attach = ident["proposal_id"]
        self.assertEqual(self.proposals(FRANK)[-1][:3], (attach, "add_identifier", "pending"))

        self.assertEqual(self.fx.accept(attach).get("status"), "ok")  # the second yes
        third = self.daily(morning(D4))
        self.assertEqual(third["queued"], 2)
        self.assertEqual(third["released_from_hold"], 2)
        with self.ledger() as led:
            queue = led.queued()
            self.assertEqual(led.held(FRANK), [])
            self.assertEqual(led.identifier(FRANK)["state"], "accepted")
        for day in (D1, D2):
            self.assertEqual(queue[key(FRANK, day)]["person_id"], new_person)

    def test_an_accepted_add_identifier_releases_the_held_days(self):
        raised = imspine.resolve(self.conn, ERIN_NEW, "Fixture Erin", emit=True)  # elsewhere
        self.fx.commit()
        self.chats.chat(4)
        self.chats.exchange(4, D1, ERIN_NEW)
        report = self.daily(morning(D2))
        self.assertEqual((report["raised"], report["held"]), ({}, {"pending": 1}))
        card = self.proposals(ERIN_NEW)[0][0]
        self.assertEqual(card, raised.proposal_id)
        self.assertEqual(self.fx.accept(card).get("status"), "ok")
        report = self.daily(morning(D3))
        self.assertEqual(report["released_from_hold"], 1)
        with self.ledger() as led:
            self.assertEqual(led.queued()[key(ERIN_NEW, D1)]["person_id"], ERIN.pid)

    def test_a_late_message_in_another_chat_never_shrinks_a_held_day(self):
        """A touched day is re-read WHOLE, so a person-day is never rebuilt from part of it."""
        self.frank(D1)                                   # chat 3: Frank + the member
        self.daily(morning(D2))
        self.chats.chat(20, group=True, name="Walk")
        self.chats.say(20, at(D1, 18), LONG_A, sender=FRANK)    # arrives late, another chat
        self.chats.say(20, at(D1, 18, 1), LONG_B, sender=ALICE_PHONE)
        self.daily(morning(D3))
        with self.ledger() as led:
            held = led.held(FRANK)
        self.assertEqual(len(held), 1)
        self.assertEqual(sorted(Path(link).name for link in held[0]["links"]),
                         ["2026-03-02-fixture-frank-3.txt", "2026-03-02-walk-20.txt"])
        self.assertEqual(held[0]["turns"], 4)

    def test_already_resolved_seen_again_is_idempotent(self):
        self.alice_day()
        self.daily(morning(D2))
        self.chats.say(1, at(D1, 13), LATE, sender=ALICE_PHONE)
        report = self.daily(morning(D3))
        self.assertEqual((report["queued"], report["already_queued"]), (0, 1))


# ---------------------------------------------------------------------------
# 6. The stale fallback, G1, and its bounds.
# ---------------------------------------------------------------------------


def _conversation_rows(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT person_id, occurred_at, direction, summary, link FROM interaction "
        "WHERE source = 'conversation' ORDER BY occurred_at"
    ).fetchall()]


class TestStaleFallback(RunCase):
    LINK = "_local/imessage/threads/2026/2026-03-02-fixture-alice-1.txt"

    def test_a_stale_day_lands_mechanically_and_its_row_survives_close_and_reopen(self):
        self.alice_day()
        self.daily(morning(D2))
        early = self.daily(morning(D2) + timedelta(days=2))
        self.assertEqual(early["stale"]["due"], 0, "not stale yet")
        card = self.cards["fixture-alice"].path
        self.assertEqual(_conversation_lines(card), [])

        report = self.daily(morning(D2) + timedelta(days=3))
        self.assertEqual(report["stale"]["stamped"], 1)
        topic = f"Texts (2): {LONG_A}"
        expected = f"- 2026-03-02 — conversation (they_reached_out): {topic} (→ {self.LINK})"
        self.assertEqual(_conversation_lines(card), [expected])
        with self.ledger() as led:
            self.assertTrue(led.is_stamped(key(ALICE_PHONE, D1)))
            self.assertEqual(led.queued(), {})
        # G1: daily closed its connection; a fresh one must still see the row.
        rows = _conversation_rows(self.fx.reopen())
        self.assertEqual(rows, [(ALICE.pid, "2026-03-02", "they_reached_out", topic, self.LINK)])

    def test_a_live_claim_holds_it_back(self):
        self.alice_day()
        self.daily(morning(D2))
        with self.ledger() as led:
            led.begin_run("claim")
            led.claim([key(ALICE_PHONE, D1)], "c1",
                      lease_until=morning(D2) + timedelta(days=10), now=morning(D2))
            led.commit_run("claim")
        report = self.daily(morning(D2) + timedelta(days=3))
        self.assertEqual(report["stale"]["due"], 0)
        self.assertEqual(_conversation_lines(self.cards["fixture-alice"].path), [])

    def test_max_stamps_bounds_it_and_the_rest_waits(self):
        self.alice_day()
        self.chats.chat(7)
        self.chats.exchange(7, D1, BOB_EMAIL)
        self.daily(morning(D2))
        self.cfg["max_stamps_per_run"] = 1
        first = self.daily(morning(D2) + timedelta(days=3))
        self.assertEqual((first["stale"]["stamped"], first["stale"]["remaining"]), (1, 1))
        self.assertIn("limit", first["stopped"])
        second = self.daily(morning(D2) + timedelta(days=4))
        self.assertEqual(second["stale"]["stamped"], 1)
        with self.ledger() as led:
            self.assertEqual(led.queued(), {})

    def test_an_exhausted_budget_stops_cleanly_and_carries_everything(self):
        self.alice_day()
        self.cfg["budget_margin_s"] = 0
        report = self.daily(morning(D2), budget_s=0)
        self.assertIsNone(report["paused"])
        self.assertIn("budget", report["stopped"])
        self.assertEqual(report["queued"], 0)
        self.assertNotIn("watermark", self.state(), "a cut-short first run placed a watermark")
        again = self.daily(morning(D2))
        self.assertEqual(again["queued"], 1)

    def test_stamped_false_is_recorded_and_the_day_stays_queued(self):
        self.alice_day()
        self.daily(morning(D2))
        refusal = "couldn't back fixture-alice.md up before rewriting it (disk full)"
        def refusing(person_path, *, source, occurred_at, direction, topic=None,
                     legacy_topic=None, link=None, note=None, source_meeting_id=None,
                     conn=None):
            # the engine's exact signature, so the drift gate still passes: this is the
            # engine declining WITHOUT raising, which is the case under test
            return people_stamp.StampResult(stamped=False, detail=refusal)

        with mock.patch.object(people_stamp, "stamp_interaction", refusing):
            report = self.daily(morning(D2) + timedelta(days=3))
        self.assertEqual(report["stale"]["stamped"], 0)
        self.assertEqual(report["stale"]["failures"], [refusal])
        self.assertEqual(report["failures"], 1)
        k = key(ALICE_PHONE, D1)
        with self.ledger() as led:
            self.assertTrue(led.is_queued(k))
            self.assertFalse(led.is_stamped(k))
            self.assertEqual(led.open_intents(), {})
            self.assertEqual(led.failures()[k]["detail"], refusal)
        self.assertEqual(_conversation_lines(self.cards["fixture-alice"].path), [])

        status = imrun.status(home=self.home, now=morning(D5))
        self.assertEqual(
            [(f["identifier"], f["day"], f["detail"]) for f in status["ledger"]["failures"]],
            [(ALICE_PHONE, "2026-03-02", refusal)],
        )
        self.assertIn(refusal, imessage.status_render(status))

        healed = self.daily(morning(D2) + timedelta(days=4))
        self.assertEqual(healed["stale"]["stamped"], 1)
        with self.ledger() as led:
            self.assertEqual(led.failures(), {})


# ---------------------------------------------------------------------------
# 7. Recovery — an interrupted stamp settles exactly once.
# ---------------------------------------------------------------------------


class TestRecovery(RunCase):
    def interrupted(self, *, after_the_card_write):
        real = imspine.stamp

        def dying_stamp(*args, **kwargs):
            if after_the_card_write:
                real(*args, **kwargs)
            raise KeyboardInterrupt("killed mid-stamp")

        with mock.patch.object(imspine, "stamp", dying_stamp):
            with self.assertRaises(KeyboardInterrupt):
                self.daily(morning(D2) + timedelta(days=3))

    def test_an_intent_already_on_the_card_is_settled_done_and_projected(self):
        self.alice_day()
        self.daily(morning(D2))
        self.interrupted(after_the_card_write=True)
        with self.ledger() as led:
            self.assertEqual(list(led.open_intents()), [key(ALICE_PHONE, D1)])
        self.assertEqual(_conversation_rows(self.fx.reopen()), [], "rolled back with the run")

        report = self.daily(morning(D2) + timedelta(days=3))
        self.assertEqual(report["recovered"]["done"], 1)
        self.assertEqual(report["stale"]["stamped"], 0)
        with self.ledger() as led:
            self.assertTrue(led.is_stamped(key(ALICE_PHONE, D1)))
            self.assertEqual(led.queued(), {})
        self.assertEqual(len(_conversation_lines(self.cards["fixture-alice"].path)), 1)
        self.assertEqual(len(_conversation_rows(self.fx.reopen())), 1, "recovery re-projects")

    def test_an_intent_not_on_the_card_is_dropped_and_lands_later(self):
        self.alice_day()
        self.daily(morning(D2))
        self.interrupted(after_the_card_write=False)
        report = self.daily(morning(D2) + timedelta(days=3))
        self.assertEqual(report["recovered"]["dropped"], 1)
        self.assertEqual(report["stale"]["stamped"], 1)
        self.assertEqual(len(_conversation_lines(self.cards["fixture-alice"].path)), 1)


# ---------------------------------------------------------------------------
# 8. Nothing written: the dry run, and every gate.
# ---------------------------------------------------------------------------


class TestDryRun(RunCase):
    def places(self):
        return _tree_bytes(self.home, self.fx.root)

    def seed(self):
        self.alice_day()
        self.chats.chat(3)
        self.chats.exchange(3, D1, FRANK)
        self.chats.chat(5)
        self.chats.exchange(5, D1, SHARED_LANDLINE)

    def test_a_cold_dry_run_writes_nothing_anywhere(self):
        self.seed()
        # counted FIRST: proposals() reopens the fixture's connection, and closing the last
        # connection to a WAL database checkpoints it — a change the dry run did not make
        props = len(self.proposals())
        before = self.places()
        report = self.daily(morning(D2), dry_run=True)
        self.assertEqual(_changed(before, self.places()), [])
        self.assertFalse((self.home / "ledger.lock").exists(), "the dry run made the lock file")
        self.assertFalse(self.threads.exists())
        self.assertEqual(len(self.proposals()), props)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["queued"], 1)
        self.assertEqual(report["would_raise"], {})
        self.assertEqual(report["held"], {"ambiguous": 1, "new": 1})
        self.assertEqual((report["new_for_review"], report["awaiting_review"]), (2, 2))
        self.assertIn("dry run", imessage.daily_render(report))
        self.assertIn("2 new number(s) would go onto your texts review list",
                      imessage.daily_render(report))

    def test_a_dry_run_that_closes_the_last_connection_leaves_the_database_alone(self):
        """Found on the real machine: with nobody else holding memory.db, the dry run's
        connection is the LAST to close, and SQLite's default is then to checkpoint the
        WAL into the main file and delete it — no row changed, but the files did."""
        self.seed()
        self.fx.conn.close()          # nobody else holds the database, as on a real Mac
        self.fx.conn = None
        db = self.fx.db_path
        wal = db.with_name(db.name + "-wal")
        # Committed frames still sitting in the WAL, as the engine's own writers leave
        # them: written by a connection that skips its own checkpoint on close.
        writer = sqlite3.connect(str(db))
        writer.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        writer.execute("CREATE TABLE fixture_wal_probe (x INTEGER)")
        writer.commit()
        writer.close()
        self.assertTrue(wal.is_file() and wal.stat().st_size > 0,
                        "the premise: a WAL with frames and no open connection")
        before = {p.name: p.read_bytes() for p in (db, wal) if p.exists()}
        self.daily(morning(D2), dry_run=True)
        after = {p.name: p.read_bytes() for p in (db, wal) if p.exists()}
        self.assertEqual(sorted(after), sorted(before), "the dry run deleted the WAL")
        self.assertEqual(_changed(before, after), [], "the dry run checkpointed memory.db")
        self.fx.reopen()

    def test_a_dry_run_on_a_database_with_no_wal_leaves_no_wal_behind(self):
        """The twin: nothing to fold, so SQLite's own close must tidy up what it made."""
        self.seed()
        self.fx.conn.close()          # the last close: WAL folded in and deleted
        self.fx.conn = None
        db = self.fx.db_path
        siblings = sorted(p.name for p in db.parent.iterdir() if p.name.startswith(db.name))
        self.assertEqual(siblings, [db.name], "the premise: no WAL and no shm file")
        before = db.read_bytes()
        self.daily(morning(D2), dry_run=True)
        self.assertEqual(
            sorted(p.name for p in db.parent.iterdir() if p.name.startswith(db.name)),
            [db.name], "the dry run left a WAL or shm file behind")
        self.assertEqual(db.read_bytes(), before)
        self.fx.reopen()

    def test_a_warm_dry_run_writes_nothing_either(self):
        self.seed()
        self.daily(morning(D2))
        self.chats.say(1, at(D1, 11), LATE, sender=ALICE_PHONE)
        self.chats.exchange(1, D2, ALICE_PHONE)
        before = self.places()
        report = self.daily(morning(D2) + timedelta(days=3), dry_run=True)
        self.assertEqual(_changed(before, self.places()), [])
        self.assertEqual(report["queued"], 1)          # D2 would queue
        self.assertEqual(report["already_queued"], 1)  # D1 already is
        self.assertEqual(report["stale"]["due"], 1)    # D1 would land
        self.assertEqual(report["stale"]["stamped"], 0)


class TestGatesWriteNothing(RunCase):
    def setUp(self):
        super().setUp()
        self.alice_day()

    def assert_paused_and_untouched(self, reason, **kw):
        before = _tree_bytes(self.home, self.fx.root)
        report = self.daily(morning(D2), **kw)
        self.assertEqual(report["paused"]["reason"], reason, report["paused"])
        self.assertTrue(report["paused"]["sentence"].strip())
        self.assertEqual(_changed(before, _tree_bytes(self.home, self.fx.root)), [],
                         f"{reason} wrote something")
        self.assertFalse(self.threads.exists())
        return report

    def test_not_a_mac(self):
        with mock.patch.object(imchat, "_is_macos", lambda: False):
            report = self.assert_paused_and_untouched("not_mac")
        self.assertIn("not one", report["paused"]["sentence"])

    def test_no_own_handles(self):
        self.cfg["own_handles"] = []
        report = self.assert_paused_and_untouched("no_own_handles")
        self.assertEqual(report["paused"]["sentence"], imconfig.REFUSAL_NO_OWN_HANDLES)

    def test_no_full_disk_access_names_the_app(self):
        sentence = "texts: this Mac won't let Fixture Terminal near the Messages database."
        with mock.patch.object(imchat, "has_access", lambda path=None: (False, sentence)):
            report = self.assert_paused_and_untouched("no_access")
        self.assertIn("Fixture Terminal", report["paused"]["sentence"])

    def test_engine_drift(self):
        def changed(person_path, *, source):  # a signature the plug-in was not built for
            return None

        with patched(people_stamp, "stamp_interaction", changed):
            report = self.assert_paused_and_untouched("engine_changed")
        self.assertTrue(report["paused"]["sentence"].startswith(imspine.REFUSAL_ENGINE_CHANGED))

    def test_a_damaged_ledger(self):
        (self.home / "ledger.lock").write_bytes(b"")    # as a real earlier run leaves it
        (self.home / "ledger.json").write_text("{not json", encoding="utf-8")
        self.assert_paused_and_untouched("ledger_damaged")

    def test_a_damaged_state_file(self):
        (self.home / "ledger.lock").write_bytes(b"")
        (self.home / "state.json").write_text("[1, 2", encoding="utf-8")
        self.assert_paused_and_untouched("ledger_damaged")

    def test_a_state_file_this_version_does_not_recognise(self):
        (self.home / "ledger.lock").write_bytes(b"")
        (self.home / "state.json").write_text('{"watermark": 812345678901234567}\n',
                                              encoding="utf-8")
        self.assert_paused_and_untouched("state_unrecognised")

    def test_the_ledger_lock_is_busy(self):
        with imledger.default_lock(self.home), mock.patch.object(imrun, "LOCK_TIMEOUT_S", 0.2):
            self.assert_paused_and_untouched("busy")


# ---------------------------------------------------------------------------
# 9. The write-path sweep: a real run writes only in its own places.
# ---------------------------------------------------------------------------

_PERSON_FILE = re.compile(r"^[a-z0-9-]+\.md(\.lock|\.[0-9a-f]+\.tmp)?$")
_HOME_FILE = re.compile(r"^(state\.json|ledger\.json|ledger\.wal|ledger\.lock)"
                        r"(\.[A-Za-z0-9_]+\.tmp)?$")


@contextlib.contextmanager
def record_writes():
    """Every write-shaped call made inside the block, as ``[(path, what)]``.

    Shared with ``test_imsynth``'s sweep, so both suites judge "writes only in its
    own places" with the same net.
    """
    seen = []

    def note(target, what):
        if isinstance(target, int):
            return
        seen.append((str(target), what))

    real_open, real_io_open = builtins.open, io.open
    real_os_open = os.open

    def open_(file, mode="r", *a, **k):
        if any(c in str(mode) for c in "wax+"):
            note(file, f"open({mode})")
        return real_open(file, mode, *a, **k)

    def io_open(file, mode="r", *a, **k):
        if any(c in str(mode) for c in "wax+"):
            note(file, f"io.open({mode})")
        return real_io_open(file, mode, *a, **k)

    writing = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

    def os_open(path, flags, *a, **k):
        if flags & writing:
            note(path, "os.open")
        return real_os_open(path, flags, *a, **k)

    def wrap(name, fn, *, first_only=True, skip_existing_dir=False):
        def inner(*args, **kwargs):
            target = args[0] if args else None
            if not (skip_existing_dir and target is not None and Path(target).is_dir()):
                note(target, name)
                if not first_only and len(args) > 1:
                    note(args[1], name)
            return fn(*args, **kwargs)
        return inner

    def path_writer(name, real):
        def inner(self_path, *args, **kwargs):
            note(self_path, name)
            return real(self_path, *args, **kwargs)
        return inner

    patches = [
        mock.patch.object(builtins, "open", open_),
        mock.patch.object(io, "open", io_open),
        mock.patch.object(os, "open", os_open),
        mock.patch.object(os, "replace", wrap("os.replace", os.replace, first_only=False)),
        mock.patch.object(os, "rename", wrap("os.rename", os.rename, first_only=False)),
        mock.patch.object(os, "unlink", wrap("os.unlink", os.unlink)),
        mock.patch.object(os, "remove", wrap("os.remove", os.remove)),
        mock.patch.object(os, "chmod", wrap("os.chmod", os.chmod)),
        mock.patch.object(os, "utime", wrap("os.utime", os.utime)),
        mock.patch.object(os, "mkdir", wrap("os.mkdir", os.mkdir, skip_existing_dir=True)),
        mock.patch.object(Path, "write_text", path_writer("Path.write_text", Path.write_text)),
        mock.patch.object(Path, "write_bytes",
                          path_writer("Path.write_bytes", Path.write_bytes)),
        mock.patch.object(Path, "touch", path_writer("Path.touch", Path.touch)),
    ]
    for patch in patches:
        patch.start()
    try:
        yield seen
    finally:
        for patch in reversed(patches):
            patch.stop()


class TestWritePathSweep(RunCase):
    def allowed(self, path):
        p = Path(path).resolve()
        if p == self.threads or self.threads in p.parents:
            return "threads"
        if p.parent == self.home and _HOME_FILE.match(p.name):
            return "home"
        if p.parent == self.fx.people_dir.resolve() and _PERSON_FILE.match(p.name):
            return "card"
        snaps = self.fx.snapshots_dir.resolve()
        if p == snaps or snaps in p.parents:
            return "snapshots"
        return None

    def sweep(self):
        return record_writes()

    def test_every_write_of_a_full_run_lands_in_its_own_places(self):
        self.alice_day()
        self.chats.chat(3)
        self.chats.exchange(3, D1, FRANK)
        with self.sweep() as seen:
            first = self.daily(morning(D2))                         # queue + hold
            second = self.daily(morning(D2) + timedelta(days=3))    # stale stamp + project
        self.assertEqual((first["queued"], second["stale"]["stamped"]), (1, 1))

        strays = sorted({(p, w) for p, w in seen if self.allowed(p) is None})
        self.assertEqual(strays, [], "a run wrote outside its own places")
        kinds = {self.allowed(p) for p, _ in seen}
        self.assertEqual(kinds, {"threads", "home", "card", "snapshots"},
                         "the sweep did not see every kind of write, so it proves too little")

    def test_the_sweep_bites(self):
        with self.sweep() as seen:
            with open(self.root / "stray.txt", "w", encoding="utf-8") as fh:
                fh.write("x")
        self.assertTrue(any(self.allowed(p) is None for p, _ in seen))



# ---------------------------------------------------------------------------
# 10. status, the renderers, and nothing escaping to the member's real places.
# ---------------------------------------------------------------------------


class TestTheShmExclusionIsHonest(unittest.TestCase):
    """The one file ``_tree_bytes`` skips changes under a pure READ — and nothing else does."""

    def test_a_read_only_reader_rewrites_the_shm_index_and_nothing_else(self):
        with SpineFixture() as fx:
            fx.standard_cards()
            shm = fx.db_path.with_name(fx.db_path.name + "-shm")
            before, shm_before = _tree_bytes(fx.root), shm.read_bytes()
            ro = sqlite3.connect(f"{fx.db_path.as_uri()}?mode=ro", uri=True)
            try:
                ro.execute("SELECT COUNT(*) FROM person").fetchone()
            finally:
                ro.close()
            self.assertNotEqual(shm.read_bytes(), shm_before, "the premise no longer holds")
            self.assertEqual(_changed(before, _tree_bytes(fx.root)), [])

    def test_the_comparison_still_bites_on_a_real_write(self):
        with SpineFixture() as fx:
            fx.standard_cards()
            before = _tree_bytes(fx.root)
            fx.db().execute("CREATE TABLE fixture_probe (x INTEGER)")
            fx.commit()
            self.assertNotEqual(_changed(before, _tree_bytes(fx.root)), [])


class TestStatus(RunCase):
    def test_a_cold_status_reads_and_writes_nothing(self):
        before = _tree_bytes(self.home)
        report = imrun.status(home=self.home, now=morning(D2))
        self.assertEqual(_changed(before, _tree_bytes(self.home)), [])
        self.assertFalse(report["ledger"]["exists"])
        self.assertTrue(report["state"]["first_run_pending"])
        self.assertIn("none yet", imessage.status_render(report))

    def test_status_after_runs(self):
        self.alice_day()
        self.chats.chat(3)
        self.chats.exchange(3, D1, FRANK)
        self.daily(morning(D2))
        report = imrun.status(home=self.home, now=morning(D2))
        state, ledger = report["state"], report["ledger"]
        self.assertEqual((state["watermark"], state["watermark_kind"]), (4, "rowid"))
        self.assertTrue(state["newest_read_at"].startswith("2026-03-02"))
        self.assertEqual(ledger["queue"], {"depth": 1, "oldest_day": "2026-03-02"})
        self.assertEqual(ledger["unclaimed"], {"depth": 1, "oldest_day": "2026-03-02"})
        self.assertEqual(ledger["identifiers_by_state"], {"accepted": 1, "held": 1})
        self.assertEqual(ledger["held_units"], 1)
        self.assertEqual(ledger["awaiting_yes"], 0)
        self.assertEqual(ledger["awaiting_review"], 1)
        self.assertEqual(ledger["failures"], [])
        self.assertEqual(ledger["open_intents"], 0)
        text = imessage.status_render(report)
        self.assertIn("Queue: 1 day(s)", text)
        self.assertIn(report["zone"], text)
        json.dumps(report)

    def test_the_daily_renderer_covers_every_shape(self):
        self.alice_day()
        first = self.daily(morning(D2))
        self.assertIn("Queued 1", imessage.daily_render(first))
        again = self.daily(morning(D2))
        self.assertIn("Nothing new was queued", imessage.daily_render(again))
        stale = self.daily(morning(D2) + timedelta(days=3))
        self.assertIn("Filed 1 of 1", imessage.daily_render(stale))
        self.cfg["own_handles"] = []
        paused = self.daily(morning(D2))
        self.assertIn(imconfig.REFUSAL_NO_OWN_HANDLES, imessage.daily_render(paused))
        for report in (first, again, stale, paused):
            json.dumps(report)
            text = imessage.daily_render(report)
            for secret in (ALICE_PHONE, "Fixture Alice", LONG_A):
                self.assertNotIn(secret, text, "the daily line carried a name, number or words")


class TestNothingEscapes(unittest.TestCase):
    def test_a_full_cycle_leaves_the_real_places_as_they_were(self):
        before = real_state()
        traces = real_fixture_traces()
        case = TestGrid("test_e1_accept_then_attach_then_every_held_day_queues")
        result = unittest.TestResult()
        case.run(result)
        self.assertEqual(result.errors + result.failures, [])
        self.assertEqual(real_state(), before)
        self.assertEqual(real_fixture_traces(), traces)


# ---------------------------------------------------------------------------
# 11. check --write-never-ingest, and the preview nit.
# ---------------------------------------------------------------------------


class TestWriteNeverIngest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve() / "imessage"
        self.home.mkdir()
        self.target = self.home / "config.local.json"

    def seed(self, **values):
        self.target.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")

    def test_it_canonicalises_appends_and_never_removes(self):
        self.seed(_readme="mine", own_handles=[ME], never_ingest=["555-555-0190"],
                  synth_stale_days=5)
        _, merged, added = imessage.write_never_ingest(
            ["(555) 555-0190", "assistant@example.com", "+1 555 555 0191",
             "Assistant@Example.com"], target=self.target, home=self.home)
        self.assertEqual(added, ["assistant@example.com", "+15555550191"])
        on_disk = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, merged)
        self.assertEqual(on_disk["never_ingest"],
                         ["555-555-0190", "assistant@example.com", "+15555550191"],
                         "an existing entry was rewritten or dropped")
        self.assertEqual((on_disk["own_handles"], on_disk["_readme"],
                          on_disk["synth_stale_days"]), ([ME], "mine", 5))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        cfg = imconfig.load_config(path=self.home / "absent.json", local_path=self.target)
        self.assertIn("assistant@example.com", imthreads.never_ingest(cfg))

    def test_nothing_new_writes_nothing(self):
        self.seed(never_ingest=["+15555550191"])
        before = self.target.read_bytes()
        _, _, added = imessage.write_never_ingest(["555 555 0191"], target=self.target,
                                                  home=self.home)
        self.assertEqual(added, [])
        self.assertEqual(self.target.read_bytes(), before)

    def test_refusals_change_nothing(self):
        self.seed(never_ingest="not a list")
        before = self.target.read_bytes()
        for handles in (["no digits here"], [], ["+15555550191"]):
            with self.subTest(handles=handles), self.assertRaises(imessage.LocalConfigWriteError):
                imessage.write_never_ingest(handles, target=self.target, home=self.home)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["config.local.json"])

    def test_a_fresh_file_gets_a_readme(self):
        _, merged, added = imessage.write_never_ingest(["+15555550191"], target=self.target,
                                                       home=self.home)
        self.assertEqual(added, ["+15555550191"])
        self.assertIn("_readme", merged)

    def test_the_check_verb_writes_and_reports_it(self):
        db = self.home.parent / "chat.db"
        ChatDb(db)
        out = io.StringIO()
        with (
            mock.patch.object(imchat, "DB_PATH", db),
            mock.patch.object(imchat, "_is_macos", lambda: True),
            mock.patch.object(imessage.imcontacts, "load_map", lambda *a, **k: {}),
            mock.patch.object(imessage.imcontacts, "store_paths", lambda *a, **k: []),
            mock.patch.object(imessage, "USER_PROFILE_PATH", self.home / "absent-USER.md"),
            mock.patch.object(imconfig, "HOME", self.home),
            mock.patch.object(imconfig, "LOCAL_CONFIG_PATH", self.target),
            mock.patch.object(imconfig, "CONFIG_PATH", self.home / "absent-config.json"),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = imessage.main(["check", "--write-never-ingest", "assistant@example.com",
                                  "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["never_ingest"]["write"]["added"], ["assistant@example.com"])
        self.assertEqual(report["never_ingest"]["handles"], ["assistant@example.com"])
        self.assertTrue(report["never_ingest"]["shortcodes_already_filtered"])
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8"))["never_ingest"],
                         ["assistant@example.com"])


class TestPreviewEmptyWriteNit(unittest.TestCase):
    def test_an_empty_write_does_not_claim_transcripts_were_written(self):
        report = {"window": None, "error": "an empty window",
                  "write": {"requested": True, "performed": False, "files": 0, "bytes": 0,
                            "directory": "/nowhere"}}
        text = imessage.preview_render(report)
        self.assertNotIn("Transcripts were written", text)
        self.assertIn("No transcripts were written", text)
        report["write"]["files"] = 2
        self.assertIn("Transcripts were written", imessage.preview_render(report))


if __name__ == "__main__":
    unittest.main()
