"""``imessage.py preview`` — the dry look, the write, and the wall it must not cross.

``preview`` is the verb a member runs *before* anything reaches their people, so the
promise it makes is unusually literal: **without ``--write`` it puts nothing on disk at
all, and in neither mode does it make any contact with the people spine.**  Both halves of
that are silent when they break — a stray write lands a transcript the member never asked
for, and a spine call stamps a card before the member has seen what a line even looks
like.  Neither raises, neither shows up in the report, and the member finds out by reading
a card that already says something.  So both are held still here.

What this suite pins, and what breaks if it stops
--------------------------------------------------
1. **A dry run writes nothing.**  Not a file, not a year folder, not a temp file.  The
   check is that the threads directory is still completely empty afterwards, with the
   reported counts non-zero in the same run — an empty directory proves nothing if the run
   found nothing to write.
2. **``--write`` writes exactly the expected files**, all inside the threads folder and
   all mode ``0600``.
3. **Re-running ``--write`` over the same window is stable** — same names, same bytes, no
   duplicates.  The filename carries the ``chat_rowid`` for exactly this reason, and a
   second run that renamed anything would leave a card pointing at yesterday's file.
4. **The own-handles refusal fires** without ``config.local.json`` and without the
   override, carrying :data:`imconfig.REFUSAL_NO_OWN_HANDLES` whole.
5. **``--assume-own-handle`` is labelled an assumption** in both the plain report and
   ``--json``.  An override that read like configuration is how a member comes to believe
   their own numbers are set when they are not.
6. **A bad window is one plain sentence and exit 0**, never a traceback.
7. **The no-spine invariant, checked mechanically** by parsing ``imessage.py`` with
   :mod:`ast` and walking the *reachable* preview code — with a negative control proving
   the walk bites.
8. **The same resolved paths from all three working directories**, and those runs leave
   the member's real threads folder exactly as they found it.
9. **Every included person-day is one line**, however often that person texted (the member,
   2026-09-24: no weekly fold).  Thirty straight days from one person is thirty lines.
10. **Cold starts are green beside their populated twins**: no ``state.json``, no
    ``threads/`` folder, an empty Contacts map, and a window with nothing in it.  Each is
    what a new member meets first, and none of them is reached by the populated tests.

PRIVACY — the rule this file is written under
----------------------------------------------
**Nothing here may print a message body, a real name or a real phone number** — not in
output, not in a failure message.  Every handle is invented (the reserved ``555-01xx``
block; RFC 2606 domains), every body is written for this file, and **no test here reads
the member's real Messages database or writes into the real threads folder**.  The three
working-directory subprocesses run ``--mask`` and never ``--write``, and they ask for a
day in 2001 that carries nothing.
"""

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_HOME))

# The plug-in's own modules first, before anything reachable only because imconfig
# put `.claude/scripts` on sys.path.
import imconfig  # noqa: E402
import imchat  # noqa: E402
import imcontacts  # noqa: E402
import imessage  # noqa: E402
import imthreads  # noqa: E402

import ast  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest import mock  # noqa: E402

BRAIN_ROOT = PLUGIN_HOME.parents[1]
ENGINE_SCRIPTS = Path(imconfig.SCRIPTS_DIR).resolve()

# ---------------------------------------------------------------------------
# Invented throughout. 555-01xx is the reserved fictional block; example.com is
# RFC 2606. None of these is anybody's number.
# ---------------------------------------------------------------------------

ALICE = "+15555550101"
BOB = "+15555550102"
ME_PHONE = "+15555550199"

#: The names the patched Contacts map hands back, and therefore the slugs in
#: every expected filename below. Invented people.
ALICE_NAME = "Alice Example"
BOB_NAME = "Bob Example"
GROUP_NAME = "Garden Group"

CONTACTS = {ALICE: ALICE_NAME, BOB: BOB_NAME}

#: Long enough to clear the substance floor and plainly a real exchange rather
#: than six rounds of "ok". Written for this file; nobody said either of them.
LONG_A = "Can we move the appointment to four o'clock, or is the morning easier for you?"
LONG_B = "Four works fine for me, I will let the front desk know and see you then."
THIN = "ok"

#: A fixed zone for the fixture, so the conversation-day boundaries under test
#: cannot move with whatever this machine happens to be set to.
ZONE = timezone.utc

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

#: The window every fixture test previews, and the days inside it.
WINDOW_FROM = "2026-03-01"
WINDOW_TO = "2026-03-05"
DAY_ONE = (2026, 3, 2)
DAY_TWO = (2026, 3, 3)
DAY_THIN = (2026, 3, 4)

#: The chats. `style` 45 is a one-to-one and 43 a group, exactly as Apple spells
#: it — never a participant count.
CHAT_ALICE = 1
CHAT_BOB = 2
CHAT_GROUP = 3


def _raw(moment):
    """An aware datetime as Apple nanoseconds — written out, not imported."""
    delta = moment - APPLE_EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def _at(day, hour, minute=0):
    return datetime(day[0], day[1], day[2], hour, minute, tzinfo=ZONE)


# ---------------------------------------------------------------------------
# The fixture database. Only the columns imchat actually touches, spelled as
# Apple spells them — a literal `ROWID` column included, which the real schema has.
# ---------------------------------------------------------------------------

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

#: ``(rowid, chat, when, is_from_me, handle_rowid, body)``.
#:
#: Five conversation-days, four of which clear the floor and one of which does
#: not — because "a dry run wrote nothing" and "``--write`` wrote four files"
#: both mean nothing unless the floor is visibly doing work in the same fixture.
FIXTURE_MESSAGES = (
    # chat 1, Alice, day one — two turns, two senders, real words: KEPT.
    (1, CHAT_ALICE, _at(DAY_ONE, 9, 0), 0, 1, LONG_A),
    (2, CHAT_ALICE, _at(DAY_ONE, 9, 5), 1, 1, LONG_B),
    # chat 1, Alice, day two — the same, on the next day: a second KEPT day.
    (3, CHAT_ALICE, _at(DAY_TWO, 10, 0), 0, 1, LONG_A),
    (4, CHAT_ALICE, _at(DAY_TWO, 10, 2), 1, 1, LONG_B),
    # chat 2, Bob, day one: KEPT.
    (5, CHAT_BOB, _at(DAY_ONE, 11, 0), 0, 2, LONG_A),
    (6, CHAT_BOB, _at(DAY_ONE, 11, 1), 1, 2, LONG_B),
    # chat 3, a named group, day one — two people who are not the member: KEPT,
    # and its topic quotes nothing, which the sample assertions rely on.
    (7, CHAT_GROUP, _at(DAY_ONE, 12, 0), 0, 1, LONG_A),
    (8, CHAT_GROUP, _at(DAY_ONE, 12, 1), 0, 2, LONG_B),
    # chat 2, Bob, day three — one turn, one sender, two characters: SET ASIDE.
    (9, CHAT_BOB, _at(DAY_THIN, 8, 0), 0, 2, THIN),
)

#: Exactly what ``--write`` must produce, relative to the threads folder, and
#: nothing else. Written out rather than computed, so a change to the filename
#: rule fails here instead of quietly renaming every card's pointer.
EXPECTED_FILES = (
    "2026/2026-03-02-alice-example-1.txt",
    "2026/2026-03-02-bob-example-2.txt",
    "2026/2026-03-02-garden-group-3.txt",
    "2026/2026-03-03-alice-example-1.txt",
)


#: One person, thirty straight days, a real two-sided exchange on each. Pins
#: the no-weekly-fold rule where the member actually reads it: the line count.
THIRTY_DAY_WINDOW = ("2026-04-01", "2026-04-30")
THIRTY_DAYS = tuple(
    row
    for day in range(1, 31)
    for row in (
        (1000 + 2 * day, CHAT_ALICE, _at((2026, 4, day), 9, 0), 0, 1, LONG_A),
        (1001 + 2 * day, CHAT_ALICE, _at((2026, 4, day), 9, 5), 1, 1, LONG_B),
    )
)


def build_fixture(path, messages=FIXTURE_MESSAGES):
    """Write the small Messages-shaped database this suite previews."""
    con = sqlite3.connect(str(path))
    try:
        con.executescript(FIXTURE_SCHEMA)
        con.executemany(
            "INSERT INTO chat VALUES (?, ?, ?, ?)",
            [
                (CHAT_ALICE, imchat.ONE_TO_ONE_STYLE, None, "chat-1"),
                (CHAT_BOB, imchat.ONE_TO_ONE_STYLE, None, "chat-2"),
                (CHAT_GROUP, imchat.GROUP_STYLE, GROUP_NAME, "chat-3"),
            ],
        )
        con.executemany("INSERT INTO handle VALUES (?, ?)", [(1, ALICE), (2, BOB)])
        for rowid, chat, when, from_me, handle_rowid, body in messages:
            con.execute(
                "INSERT INTO message (ROWID, date, is_from_me, handle_id, text, "
                "attributedBody, associated_message_type, item_type, "
                "is_system_message, date_edited, message_summary_info) "
                "VALUES (?, ?, ?, ?, ?, NULL, 0, 0, 0, 0, NULL)",
                (rowid, _raw(when), int(from_me), handle_rowid, body),
            )
            con.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat, rowid))
        con.commit()
    finally:
        con.close()


def _listing(root):
    """``[(relative posix path, bytes)]`` for everything under ``root``, sorted.

    Bytes, not a hash and not a size: "stable" here means the second run produced
    the same files with the same contents, and a size comparison would pass a
    run that rewrote a transcript with different words of the same length.
    """
    root = Path(root)
    found = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            found.append((path.relative_to(root).as_posix(), path.read_bytes()))
    return found


class PreviewCase(unittest.TestCase):
    """A fixture database, a temp threads folder, and no engine or real store in play.

    Everything that would otherwise reach the member's own machine is redirected
    here in one place: the message store, the Contacts map, the timezone, the
    settings files and the threads folder.  ``imchat._is_macos`` is forced true so
    the whole suite runs on Windows and Linux as well — the fixture is a plain
    SQLite file and has no reason to be Mac-only.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)

        self.db = root / "chat.db"
        build_fixture(self.db)

        self.threads = root / "threads"
        self.threads.mkdir()

        # Settings point at files that do not exist, so every knob is the shipped
        # default and a member's own edited config.json cannot move these counts.
        patches = (
            mock.patch.object(imchat, "DB_PATH", self.db),
            mock.patch.object(imchat, "_is_macos", lambda: True),
            mock.patch.object(imchat, "_zone", lambda: ZONE),
            mock.patch.object(imcontacts, "load_map", lambda *a, **k: dict(CONTACTS)),
            mock.patch.object(imconfig, "THREADS_DIR", self.threads),
            mock.patch.object(imconfig, "CONFIG_PATH", root / "absent-config.json"),
            mock.patch.object(imconfig, "LOCAL_CONFIG_PATH", root / "absent-local.json"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def preview(self, **kwargs):
        """``preview_gather`` over the fixture window, with our own handle assumed."""
        kwargs.setdefault("date_from", WINDOW_FROM)
        kwargs.setdefault("date_to", WINDOW_TO)
        kwargs.setdefault("assume_own_handles", [ME_PHONE])
        return imessage.preview_gather(**kwargs)


# ---------------------------------------------------------------------------
# 1. A dry run writes nothing — and found something, so the emptiness means something.
# ---------------------------------------------------------------------------


class TestDryRunWritesNothing(PreviewCase):
    def test_nothing_lands_on_disk_and_the_counts_are_real(self):
        report, code = self.preview()

        self.assertEqual(code, 0, f"a dry preview must exit 0; report: {report['error']!r}")

        leftovers = sorted(p.name for p in self.threads.rglob("*"))
        self.assertEqual(
            leftovers,
            [],
            "DRY RUN: the threads folder is not empty after a preview without "
            f"--write; it holds {leftovers}. A dry run that writes is the one "
            "failure a member cannot see coming, because the report says nothing "
            "was written.",
        )

        self.assertGreater(
            report["read"]["messages"],
            0,
            "DRY RUN: the run read no messages at all, so an empty threads folder "
            "proves nothing. Fix the fixture before trusting the test above.",
        )
        self.assertEqual(report["units"]["chat_days"], 5)
        self.assertEqual(report["units"]["included"], len(EXPECTED_FILES))
        self.assertEqual(report["units"]["set_aside"], 1)
        self.assertEqual(report["units"]["people"], 2)
        self.assertEqual(report["lines"]["count"], 3)
        self.assertEqual(report["write"]["performed"], False)
        self.assertEqual(report["write"]["would_write"], len(EXPECTED_FILES))

    def test_the_report_says_plainly_that_nothing_was_written(self):
        report, _ = self.preview()
        text = imessage.preview_render(report)
        self.assertIn("Nothing was written", text)
        self.assertIn("--write", text)

    def test_the_line_count_is_the_person_day_count(self):
        """Every included person-day is one line, or a day of contact went missing."""
        report, _ = self.preview()
        self.assertEqual(
            report["lines"]["count"],
            report["units"]["person_days"],
            "LINES: the lines reported are not one per person-day. A person-day "
            "without a line of its own is a day of contact nobody ever sees. "
            f"{report['lines']}",
        )


# ---------------------------------------------------------------------------
# 2. --write writes exactly the expected files, owner-only, inside the folder.
# ---------------------------------------------------------------------------


class TestWriteWritesExactlyWhatItSaid(PreviewCase):
    def test_exactly_the_expected_files_and_nothing_else(self):
        report, code = self.preview(write=True)

        self.assertEqual(code, 0, f"write errors: {report['write'].get('error')!r}")
        written = sorted(
            p.relative_to(self.threads).as_posix()
            for p in self.threads.rglob("*")
            if p.is_file()
        )
        self.assertEqual(
            written,
            sorted(EXPECTED_FILES),
            "WRITE: the files on disk are not the ones this window should produce. "
            "The name carries the date, the Contacts slug and the chat_rowid, and "
            "a card's pointer is built from it.",
        )
        self.assertEqual(report["write"]["files"], len(EXPECTED_FILES))
        self.assertEqual(report["write"]["performed"], True)
        self.assertGreater(report["write"]["bytes"], 0)

    def test_every_file_is_inside_the_threads_folder(self):
        self.preview(write=True)
        base = self.threads.resolve()
        for path in self.threads.rglob("*"):
            with self.subTest(path=path.name):
                self.assertTrue(
                    path.resolve().is_relative_to(base),
                    f"WRITE: {path} escaped the threads folder ({base}).",
                )

    @unittest.skipIf(os.name == "nt", "POSIX modes are advisory on Windows")
    def test_every_file_is_owner_only(self):
        self.preview(write=True)
        for path in self.threads.rglob("*"):
            if path.is_file():
                with self.subTest(path=path.name):
                    self.assertEqual(
                        stat.S_IMODE(path.stat().st_mode),
                        0o600,
                        f"WRITE: {path.name} is not owner-only. These are the "
                        "member's own conversations.",
                    )

    def test_no_temp_files_are_left_behind(self):
        self.preview(write=True)
        strays = [p.name for p in self.threads.rglob("*") if p.name.endswith(".tmp")]
        self.assertEqual(strays, [], f"WRITE: temp files left behind: {strays}")

    def test_the_report_says_where_they_live_and_that_they_stay_here(self):
        report, _ = self.preview(write=True)
        text = imessage.preview_render(report)
        self.assertIn(str(self.threads), text)
        self.assertIn("stay on this machine", text)


# ---------------------------------------------------------------------------
# 3. Re-running --write over the same window is stable.
# ---------------------------------------------------------------------------


class TestWriteIsStableAcrossRuns(PreviewCase):
    def test_a_second_run_leaves_the_directory_byte_for_byte_identical(self):
        first_report, _ = self.preview(write=True)
        first = _listing(self.threads)

        second_report, _ = self.preview(write=True)
        second = _listing(self.threads)

        self.assertEqual(
            [name for name, _ in second],
            [name for name, _ in first],
            "STABILITY: a second --write over the same window produced a different "
            "set of filenames. The chat_rowid suffix exists precisely so two "
            "conversations sharing a Contacts label cannot swap files between "
            "runs — a link written yesterday would open the other person's thread.",
        )
        self.assertEqual(
            second,
            first,
            "STABILITY: a second --write over the same window changed the bytes of "
            "at least one transcript, so the same days do not render the same way "
            "twice.",
        )
        self.assertEqual(second_report["write"]["files"], first_report["write"]["files"])
        self.assertEqual(second_report["write"]["bytes"], first_report["write"]["bytes"])

    def test_no_duplicates_appear(self):
        self.preview(write=True)
        self.preview(write=True)
        names = [p.name for p in self.threads.rglob("*") if p.is_file()]
        self.assertEqual(
            len(names),
            len(set(names)),
            f"STABILITY: duplicate transcript filenames after two runs: {names}",
        )
        self.assertEqual(len(names), len(EXPECTED_FILES))


# ---------------------------------------------------------------------------
# 4. The own-handles refusal — the honest path, not a guess.
# ---------------------------------------------------------------------------


class TestOwnHandlesRefusal(PreviewCase):
    def test_no_config_and_no_override_refuses_with_imconfig_s_own_sentence(self):
        report, code = self.preview(assume_own_handles=())

        self.assertEqual(code, 0, "a refusal is an expected condition and exits 0")
        own = report["own_handles"]
        self.assertEqual(
            own["refusal"],
            imconfig.REFUSAL_NO_OWN_HANDLES,
            "REFUSAL: preview invented its own wording instead of printing the one "
            "sentence imconfig publishes. One refusal, one place.",
        )
        self.assertEqual(own["count"], 0)
        self.assertFalse(own["assumed"])
        self.assertIsNone(report["read"], "it must refuse BEFORE reading anything")
        self.assertIsNone(report["access"])

    def test_the_refusal_names_the_command_that_fixes_it(self):
        report, _ = self.preview(assume_own_handles=())
        text = imessage.preview_render(report)
        self.assertIn(imconfig.REFUSAL_NO_OWN_HANDLES, text)
        self.assertIn("check --write-own-handles", text)
        self.assertIn("--assume-own-handle", text)

    def test_a_refusal_writes_nothing_even_with_write_asked_for(self):
        self.preview(assume_own_handles=(), write=True)
        self.assertEqual(
            sorted(p.name for p in self.threads.rglob("*")),
            [],
            "REFUSAL: --write wrote transcripts for a run that refused to start.",
        )

    def test_a_handle_that_is_not_a_handle_refuses_rather_than_guessing(self):
        report, code = self.preview(assume_own_handles=["-"])
        self.assertEqual(code, 0)
        self.assertEqual(report["own_handles"]["refusal"], imconfig.REFUSAL_NO_OWN_HANDLES)
        self.assertIn("--assume-own-handle", report["own_handles"]["detail"])


# ---------------------------------------------------------------------------
# 5. --assume-own-handle is loudly an assumption, never configuration.
# ---------------------------------------------------------------------------


class TestAssumedHandleIsLabelled(PreviewCase):
    def test_the_plain_report_calls_it_an_assumption(self):
        report, _ = self.preview()
        text = imessage.preview_render(report)
        self.assertIn(
            imessage.PREVIEW_ASSUMED_LABEL,
            text,
            "ASSUMPTION: the report does not say the handle was assumed. A member "
            "who reads this as configuration believes their own numbers are set "
            "when they are not, and then cannot explain why the daily run refuses.",
        )
        self.assertIn(
            imessage.PREVIEW_ASSUMED_NOTE,
            text,
            "ASSUMPTION: the report carries the label but not the sentence that "
            "says what an assumption is and is not.",
        )
        self.assertIn("Nothing about it is written anywhere", text)
        self.assertIn("check --write-own-handles", text)

    def test_the_json_carries_the_same_label(self):
        report, _ = self.preview()
        own = report["own_handles"]
        self.assertTrue(own["assumed"])
        self.assertEqual(own["source"], "assumed")
        self.assertEqual(own["label"], imessage.PREVIEW_ASSUMED_LABEL)
        self.assertIn(imessage.PREVIEW_ASSUMED_LABEL, own["note"])
        self.assertIn(
            imessage.PREVIEW_ASSUMED_LABEL,
            json.dumps(report),
            "ASSUMPTION: --json does not carry the label, so a tool reading the "
            "report cannot tell an assumption from a setting.",
        )

    def test_a_configured_run_is_not_labelled_an_assumption(self):
        """The negative side: without the flag, nothing claims an assumption."""
        local = Path(self._tmp.name) / "real-local.json"
        local.write_text(json.dumps({"own_handles": [ME_PHONE]}), encoding="utf-8")
        with mock.patch.object(imconfig, "LOCAL_CONFIG_PATH", local):
            report, _ = self.preview(assume_own_handles=())
        own = report["own_handles"]
        self.assertFalse(own["assumed"])
        self.assertEqual(own["source"], "config")
        self.assertIsNone(own["label"])
        self.assertNotIn(
            imessage.PREVIEW_ASSUMED_LABEL, imessage.preview_render(report)
        )

    def test_the_handles_are_masked_under_mask(self):
        report, _ = self.preview(mask=True)
        self.assertEqual(report["own_handles"]["handles"], [imessage.mask_handle(ME_PHONE)])
        self.assertNotIn(ME_PHONE, json.dumps(report))


# ---------------------------------------------------------------------------
# 6. Masking covers the quoted words too, not just the numbers.
# ---------------------------------------------------------------------------


class TestMaskingCoversTheSample(PreviewCase):
    def test_no_message_body_and_no_contact_name_survives_masking(self):
        report, _ = self.preview(mask=True)
        blob = json.dumps(report)
        self.assertTrue(report["sample"], "the sample is empty, so this proves nothing")
        for secret in (LONG_A, LONG_B, ALICE, BOB, "alice-example", "bob-example"):
            with self.subTest(secret=secret[:12]):
                self.assertNotIn(
                    secret,
                    blob,
                    "MASKING: --mask left real content in the report. This output "
                    "is written to be pasted into a plan or a bug report.",
                )

    def test_the_shape_is_still_readable(self):
        report, _ = self.preview(mask=True)
        topics = [entry["topic"] for entry in report["sample"]]
        self.assertTrue(
            any(topic.startswith("Texts (") for topic in topics),
            f"MASKING: a masked topic should still show its shape; got {topics}",
        )
        for entry in report["sample"]:
            self.assertTrue(entry["link"].startswith(imthreads.THREAD_LINK_PREFIX))

    def test_an_unrecognised_topic_is_masked_whole(self):
        """Fail closed: a topic shape this masker does not know leaks nothing."""
        self.assertEqual(
            imessage._preview_mask_topic("something new entirely"),
            "<22 chars>",
        )

    def test_an_unmasked_run_shows_the_real_line(self):
        """The control: without --mask the sample is the real thing, or the test above is vacuous."""
        report, _ = self.preview()
        blob = json.dumps(report)
        self.assertIn("alice-example", blob)

    def test_the_link_is_brain_root_relative(self):
        report, _ = self.preview()
        for entry in report["sample"]:
            self.assertTrue(
                entry["link"].startswith("_local/imessage/threads/"),
                f"LINK: {entry['link']!r} is not Brain-root-relative, so a card "
                "built from it would resolve against the vault and lead nowhere.",
            )


# ---------------------------------------------------------------------------
# 7. A bad window is one plain sentence and exit 0.
# ---------------------------------------------------------------------------


def _run_main(case, argv):
    """``imessage.main(argv)`` with stdout captured. Returns ``(code, stdout)``."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = imessage.main(argv)
    return code, buffer.getvalue()


class TestBadWindowsAreRefusedPlainly(PreviewCase):
    def _refusal(self, date_from, date_to):
        code, out = _run_main(
            self,
            ["preview", "--from", date_from, "--to", date_to, "--json", "--mask"],
        )
        self.assertEqual(
            code,
            0,
            f"RANGE: `preview --from {date_from} --to {date_to}` exited {code}. A "
            "date typed wrong is an expected condition, not a failure.",
        )
        self.assertNotIn("Traceback", out)
        report = json.loads(out)
        self.assertIsInstance(report["error"], str)
        self.assertTrue(report["error"].strip().endswith("."))
        self.assertIsNone(
            report["read"], "RANGE: a refused window must not read anything"
        )
        return report["error"]

    def test_a_window_that_ends_in_the_future(self):
        message = self._refusal(WINDOW_FROM, "2999-01-01")
        self.assertIn("future", message)

    def test_a_window_that_starts_after_it_ends(self):
        message = self._refusal("2026-03-10", "2026-03-01")
        self.assertIn("ends before it begins", message)

    def test_a_date_that_is_not_a_date(self):
        message = self._refusal("not-a-date", WINDOW_TO)
        self.assertIn("year-month-day", message)

    def test_a_bad_window_writes_nothing(self):
        _run_main(
            self,
            ["preview", "--from", "2026-03-10", "--to", "2026-03-01", "--write"],
        )
        self.assertEqual(sorted(p.name for p in self.threads.rglob("*")), [])

    def test_the_default_end_is_yesterday(self):
        """Closed days only: a default that included today would change hour to hour."""
        today = imessage._preview_today(imchat)
        start, end = imessage.preview_window(WINDOW_FROM, None, today)
        self.assertEqual(start.isoformat(), WINDOW_FROM)
        self.assertEqual(end, today - timedelta(days=1))

    def test_today_itself_is_allowed_but_flagged(self):
        today = imessage._preview_today(imchat)
        _start, end = imessage.preview_window(today.isoformat(), today.isoformat(), today)
        self.assertEqual(end, today)


# ---------------------------------------------------------------------------
# 8. The no-spine invariant, asserted mechanically.
# ---------------------------------------------------------------------------

#: Any import whose module name starts with one of these is the people spine, or
#: the memory store, or the meetings spine. `preview` touches none of them.
FORBIDDEN_MODULE_PREFIXES = ("people_", "memory_", "meetings_")

#: Names that only ever mean a spine call. ``resolve`` is in here deliberately
#: and is the sharpest of the four: ``Path.resolve()`` is everywhere in this
#: plug-in, so a preview code path that mentions ``resolve`` at all is either
#: resolving a person or doing path work it has no business doing here.
FORBIDDEN_NAMES = ("stamp_interaction", "project_changed", "resolve", "insert_proposal")

IMESSAGE_SOURCE = (PLUGIN_HOME / "imessage.py").read_text(encoding="utf-8")
IMTHREADS_SOURCE = (PLUGIN_HOME / "imthreads.py").read_text(encoding="utf-8")


def _forbidden_module(name):
    """True when any dotted part of ``name`` is a spine module."""
    if not name:
        return False
    return any(
        part.startswith(FORBIDDEN_MODULE_PREFIXES) for part in str(name).split(".")
    )


def preview_closure(source):
    """Every top-level definition the ``preview`` verb can reach, by name.

    Seeded with every definition whose own name says it is preview code, then
    closed over the module-level names those bodies call.  The closure is what
    makes this a check on the *code path* rather than on a naming convention: a
    helper added without ``preview`` in its name is still walked, because the
    preview code calls it.
    """
    tree = ast.parse(source)
    defined = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    stack = [name for name in defined if "preview" in name.lower()]
    seen = set()
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        for child in ast.walk(defined[name]):
            if isinstance(child, ast.Name) and child.id in defined and child.id not in seen:
                stack.append(child.id)
    return {name: defined[name] for name in sorted(seen)}


def spine_contacts(source):
    """Every spine import or spine call inside the preview code path."""
    findings = []
    for name, node in preview_closure(source).items():
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    if _forbidden_module(alias.name):
                        findings.append(f"{name}: import {alias.name}")
            elif isinstance(child, ast.ImportFrom):
                if _forbidden_module(child.module):
                    findings.append(f"{name}: from {child.module} import ...")
            elif isinstance(child, ast.Name) and child.id in FORBIDDEN_NAMES:
                findings.append(f"{name}: {child.id}")
            elif isinstance(child, ast.Attribute) and child.attr in FORBIDDEN_NAMES:
                findings.append(f"{name}: .{child.attr}")
    return sorted(findings)


def all_imports(source):
    """Every module imported anywhere in ``source``, as written."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TestPreviewNeverTouchesTheSpine(unittest.TestCase):
    """Checkpoint 3's defining property, proved by reading the code rather than trusting it."""

    def test_the_closure_covers_the_real_entry_points(self):
        """Without this, the scan below could be walking an empty set."""
        closure = preview_closure(IMESSAGE_SOURCE)
        for expected in ("cmd_preview", "preview_gather", "preview_render", "preview_window"):
            self.assertIn(
                expected,
                closure,
                f"NO-SPINE: {expected} is not in the preview code path this scan "
                "walks, so the scan proves nothing about it.",
            )
        self.assertGreaterEqual(
            len(closure),
            8,
            f"NO-SPINE: the closure is only {sorted(closure)}, which is too small "
            "to be the real preview path. Check the reachability walk.",
        )

    def test_the_preview_path_makes_no_contact_with_the_spine(self):
        findings = spine_contacts(IMESSAGE_SOURCE)
        self.assertEqual(
            findings,
            [],
            "NO-SPINE: the preview code path reaches the people spine:\n  "
            + "\n  ".join(findings)
            + "\nPreview exists so a member can SEE what a run would do before "
            "anything lands on a card. A spine call here means it already landed.",
        )

    def test_the_whole_cli_imports_no_spine_module(self):
        offenders = sorted(n for n in all_imports(IMESSAGE_SOURCE) if _forbidden_module(n))
        self.assertEqual(offenders, [], f"NO-SPINE: imessage.py imports {offenders}")

    def test_the_grouper_preview_delegates_to_imports_no_spine_module(self):
        """imthreads is on the preview path, so its imports are on it too."""
        offenders = sorted(n for n in all_imports(IMTHREADS_SOURCE) if _forbidden_module(n))
        self.assertEqual(offenders, [], f"NO-SPINE: imthreads.py imports {offenders}")


#: A deliberately violating module. It is a STRING, never a file: nothing is
#: created in the plug-in folder and no importer can ever reach it.
#:
#: It breaks the rule five different ways on purpose — a module-level import, an
#: import inside the preview function, a spine call in the preview function, a
#: dotted ``.resolve``, and two calls in a helper with no "preview" in its name —
#: because each one is caught by a different clause of the two scans, and a
#: control that only exercises one of them leaves the others unproven.
CONTROL_SOURCE = '''
"""A preview that does everything preview must not do."""

import people_resolve
from memory_search import query


def preview_gather():
    import people_stamp
    from meetings_spine import link

    person = people_resolve.resolve("someone")
    people_stamp.stamp_interaction(person)
    return person, link


def _helper():
    insert_proposal({})
    project_changed()


def preview_render():
    return _helper()
'''


class TestTheSpineScanBites(unittest.TestCase):
    """The negative control. A scan that has never failed is decoration."""

    def test_the_control_is_caught(self):
        findings = spine_contacts(CONTROL_SOURCE)
        self.assertTrue(
            findings,
            "NO-SPINE CONTROL: a module that imports people_stamp and calls "
            "stamp_interaction was reported clean. The scan above is not looking "
            "at anything.",
        )
        joined = "\n".join(findings)
        for expected in (
            "import people_stamp",
            "from meetings_spine import",
            "stamp_interaction",
            ".resolve",
            "insert_proposal",
            "project_changed",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, joined)

    def test_the_import_scan_is_caught_too(self):
        """A spine import at the TOP of a file sits outside every function body.

        :func:`spine_contacts` walks definitions, so a module-level import is the
        one violation it cannot see — which is exactly why
        :meth:`TestPreviewNeverTouchesTheSpine.test_the_whole_cli_imports_no_spine_module`
        exists beside it, over every import in the file.  This is that scan's
        control, and it names the function-level imports too because
        :func:`all_imports` sweeps the whole tree rather than only its top.
        """
        offenders = sorted(
            name for name in all_imports(CONTROL_SOURCE) if _forbidden_module(name)
        )
        self.assertEqual(
            offenders,
            ["meetings_spine", "memory_search", "people_resolve", "people_stamp"],
        )

    def test_a_clean_module_is_reported_clean_by_the_import_scan(self):
        clean = "import json\nfrom pathlib import Path\n"
        self.assertEqual(
            [name for name in all_imports(clean) if _forbidden_module(name)],
            [],
        )

    def test_the_control_catches_a_violation_reached_only_indirectly(self):
        """`_helper` has no 'preview' in its name; the closure must still walk it."""
        joined = "\n".join(spine_contacts(CONTROL_SOURCE))
        self.assertIn(
            "_helper: insert_proposal",
            joined,
            "NO-SPINE CONTROL: a violation in a helper called BY preview was "
            "missed, so the scan is a naming check rather than a path check.",
        )

    def test_a_clean_control_is_reported_clean(self):
        """And the other way round, or the scan flags everything and means nothing."""
        clean = "def preview_gather():\n    return sorted([1, 2])\n"
        self.assertEqual(spine_contacts(clean), [])


# ---------------------------------------------------------------------------
# 9. The same answer from all three working directories.
# ---------------------------------------------------------------------------

#: The three places this plug-in is entered from: the Brain root (a member CLI),
#: `.claude/scripts` (where `uv run --directory` lands), and the plug-in folder
#: itself (the morning stage).
WORKING_DIRECTORIES = (BRAIN_ROOT, ENGINE_SCRIPTS, PLUGIN_HOME)

#: A day in 2001 carries nothing on any machine, so this subprocess reads almost
#: nothing and never writes. --mask because the output is real data; never
#: --write, because the member's own threads folder is not this suite's to fill.
TINY_WINDOW = ("--from", "2001-01-01", "--to", "2001-01-01")


def _run_preview(case, cwd):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_HOME / "imessage.py"),
            "preview",
            *TINY_WINDOW,
            "--json",
            "--mask",
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    case.assertEqual(
        proc.returncode,
        0,
        f"CWD INDEPENDENCE: `preview` exited {proc.returncode} from {cwd}. It must "
        "exit 0 on every expected condition, including no access and no own "
        f"handles.\nstdout: {proc.stdout[:2000]!r}\nstderr: {proc.stderr[:2000]!r}",
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        case.fail(
            f"CWD INDEPENDENCE: --json output from {cwd} would not parse ({exc}). "
            "Diagnostics belong on stderr so stdout stays machine-readable.\n"
            f"stdout: {proc.stdout[:2000]!r}"
        )


class TestPreviewIsWorkingDirectoryIndependent(unittest.TestCase):
    def test_the_three_working_directories_are_really_different(self):
        resolved = {str(Path(d).resolve()) for d in WORKING_DIRECTORIES}
        self.assertEqual(
            len(resolved),
            3,
            "CWD INDEPENDENCE: the three working directories are not three "
            f"distinct places ({sorted(resolved)}), so the comparison proves nothing.",
        )

    def test_every_working_directory_reports_identical_paths(self):
        reports = {str(d): _run_preview(self, d) for d in WORKING_DIRECTORIES}

        first_cwd, first = next(iter(reports.items()))
        for cwd, report in reports.items():
            with self.subTest(cwd=cwd):
                self.assertEqual(
                    report["paths"],
                    first["paths"],
                    f"CWD INDEPENDENCE: `preview` run from {cwd} resolved different "
                    f"paths than the same command from {first_cwd}.\n"
                    f"  from {cwd}: {report['paths']}\n"
                    f"  from {first_cwd}: {first['paths']}\n"
                    "Something is reading Path.cwd() instead of __file__, and the "
                    "failure that causes is transcripts written in one entry point "
                    "and looked for in another.",
                )
                self.assertEqual(report["window"], first["window"])

        self.assertEqual(
            Path(first["paths"]["plugin_home"]).resolve(),
            PLUGIN_HOME,
            f"CWD INDEPENDENCE: preview points at {first['paths']['plugin_home']!r}, "
            "which is not this plug-in.",
        )
        self.assertEqual(
            Path(first["paths"]["threads_dir"]).resolve(),
            (PLUGIN_HOME / "threads").resolve(),
            "CWD INDEPENDENCE: the threads folder resolved somewhere unexpected: "
            f"{first['paths']['threads_dir']!r}",
        )

    def test_the_subprocesses_wrote_nothing_into_the_real_threads_folder(self):
        """A preview without --write leaves the member's real transcripts exactly as found.

        On a live install the real folder is NOT empty (the member's own runs fill
        it), and on a new one it may not exist yet, so "empty afterwards" would
        prove nothing either way.  What must hold is that these runs changed
        nothing in it, so the folder is snapshotted before and after (an absent
        folder snapshots as empty) and the two must be identical.
        """
        real = PLUGIN_HOME / "threads"
        before = _real_threads_snapshot(real)
        for cwd in WORKING_DIRECTORIES:
            _run_preview(self, cwd)
        after = _real_threads_snapshot(real)

        appeared = len(after.keys() - before.keys())
        vanished = len(before.keys() - after.keys())
        changed = sum(1 for key in before.keys() & after.keys() if before[key] != after[key])
        # Counts only: every name in that folder is built from a real Contacts
        # label, so a failure message listing them would print the member's people.
        self.assertEqual(
            (appeared, vanished, changed),
            (0, 0, 0),
            f"CWD INDEPENDENCE: a --json --mask preview without --write touched the "
            f"real threads folder: {appeared} entries appeared, {vanished} vanished, "
            f"{changed} changed. Preview without --write writes nothing, ever.",
        )


def _real_threads_snapshot(real):
    """``{relative path: (size, mtime_ns)}`` for the real threads folder's year folders.

    Metadata only, never a read: this is the member's real data.  Only the year
    folders are walked, because every file a preview can write lands in one
    (``threads/YYYY/…``, :func:`imthreads.thread_path`), and each year folder is
    itself an entry so a stray empty one is caught too.  The top level is left
    out on purpose: ``test_privacy_floor`` plants and removes a decoy there while
    it runs, and another suite running at the same moment must not read as a
    write by this one.
    """
    if not real.is_dir():
        return {}
    snapshot = {}
    for year in real.iterdir():
        if not (year.is_dir() and re.fullmatch(r"\d{4}", year.name)):
            continue
        snapshot[year.name] = None
        for path in year.rglob("*"):
            info = path.lstat()
            snapshot[path.relative_to(real).as_posix()] = (info.st_size, info.st_mtime_ns)
    return snapshot


class TestTheRealThreadsSnapshotBites(unittest.TestCase):
    """NEGATIVE CONTROL for :func:`_real_threads_snapshot`, run in a temp folder only.

    The check above compares two snapshots, so it proves nothing unless a
    snapshot really moves when a preview writes, and really holds still for the
    privacy-floor decoy it deliberately leaves out.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.threads = Path(self._tmp.name) / "threads"
        (self.threads / "2026").mkdir(parents=True)
        self.existing = self.threads / "2026" / "2026-03-02-someone-1.txt"
        self.existing.write_text("09:00  Me: an invented line\n", encoding="utf-8")
        self.before = _real_threads_snapshot(self.threads)

    def test_a_new_transcript_moves_it(self):
        (self.threads / "2026" / "2026-03-03-someone-1.txt").write_text("x\n", encoding="utf-8")
        self.assertNotEqual(_real_threads_snapshot(self.threads), self.before)

    def test_a_rewritten_transcript_moves_it(self):
        self.existing.write_text("09:00  Me: a longer invented line\n", encoding="utf-8")
        self.assertNotEqual(_real_threads_snapshot(self.threads), self.before)

    def test_a_new_empty_year_folder_moves_it(self):
        (self.threads / "2001").mkdir()
        self.assertNotEqual(_real_threads_snapshot(self.threads), self.before)

    def test_the_top_level_privacy_floor_decoy_does_not(self):
        (self.threads / "2020-01-01 decoy 1.txt").write_text("decoy\n", encoding="utf-8")
        self.assertEqual(_real_threads_snapshot(self.threads), self.before)


# ---------------------------------------------------------------------------
# 10. Every included person-day is one line — no weekly fold.
# ---------------------------------------------------------------------------


class TestEveryPersonDayIsOneLine(PreviewCase):
    """The member, 2026-09-24: however often someone texts, every day they texted is a line."""

    def test_the_rule_is_said_in_the_report_and_the_json_alike(self):
        report, _ = self.preview()
        self.assertEqual(report["lines"]["rule"], imessage.PREVIEW_LINE_RULE)
        self.assertIn(imessage.PREVIEW_LINE_RULE, imessage.preview_render(report))
        self.assertIn(imessage.PREVIEW_LINE_RULE, json.dumps(report))

    def test_the_report_says_nothing_about_weekly_lines(self):
        """A report promising weekly lines would be a promise the run breaks."""
        report, _ = self.preview()
        self.assertNotIn("fold", report, "the report still carries the weekly-fold section")
        # The threads path is taken out first: it is a temp folder whose random
        # name is not the report's words.
        text = imessage.preview_render(report).replace(str(self.threads), "")
        self.assertNotIn("week", text.lower())
        self.assertNotIn("week", json.dumps(report["lines"]).lower())

    def test_thirty_straight_days_from_one_person_are_thirty_lines(self):
        self.db.unlink()
        build_fixture(self.db, messages=THIRTY_DAYS)

        report, code = self.preview(date_from=THIRTY_DAY_WINDOW[0], date_to=THIRTY_DAY_WINDOW[1])

        self.assertEqual(code, 0, f"error: {report['error']!r}")
        self.assertEqual(report["units"]["people"], 1)
        self.assertEqual(report["units"]["person_days"], 30)
        self.assertEqual(
            report["lines"]["count"],
            30,
            "LINES: one person texting on thirty straight days did not get thirty "
            "lines. The busiest people are exactly the ones whose days must not be "
            "merged away.",
        )
        self.assertIn("30 lines", imessage.preview_render(report))


# ---------------------------------------------------------------------------
# 11. Cold starts — each one green beside its populated twin.
# ---------------------------------------------------------------------------

#: What --write must produce when Contacts is EMPTY: the same four
#: conversation-days, each one-to-one named by its handle's digits instead of a
#: Contacts name. The named group keeps its own name, which never came from
#: Contacts. Written out, like EXPECTED_FILES, so a change to the fallback fails
#: here instead of quietly renaming every card's pointer.
EXPECTED_FILES_NO_CONTACTS = (
    "2026/2026-03-02-15555550101-1.txt",
    "2026/2026-03-02-15555550102-2.txt",
    "2026/2026-03-02-garden-group-3.txt",
    "2026/2026-03-03-15555550101-1.txt",
)

#: A window inside the fixture's calendar that holds no message at all.
EMPTY_WINDOW = ("2026-02-01", "2026-02-05")

#: The units a window with nothing in it must report: zero everywhere, not missing.
ZERO_UNITS = {"chat_days": 0, "included": 0, "set_aside": 0, "person_days": 0, "people": 0}


def _relative_files(root):
    """Every file under ``root``, relative and sorted; ``[]`` when there is no folder."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


class TestColdStartTwins(PreviewCase):
    """A new member's first preview, run beside the populated one it must match.

    Four cold starts, each a state the tests above never reach and the first one
    a new member meets: no ``state.json`` yet, no ``threads/`` folder yet, an
    empty Contacts map, and a window with nothing in it.  Both twins run in the
    same test, so "green" is judged the same way on each side: exit 0, no error,
    a report that renders and serialises, and counts that say what really
    happened rather than a crash or a silent zero.
    """

    def assert_green(self, report, code, where):
        self.assertEqual(code, 0, f"{where}: exited {code}; error: {report['error']!r}")
        self.assertIsNone(report["error"], f"{where}: the preview reported an error")
        self.assertTrue(report["access"]["ok"], f"{where}: the fixture could not be read")
        json.dumps(report)  # --json must serialise a cold report too
        text = imessage.preview_render(report)
        self.assertIn(imessage.PREVIEW_LINE_RULE, text, f"{where}: the report did not render whole")
        return text

    def assert_populated_counts(self, report, where):
        self.assertEqual(report["units"]["chat_days"], 5, where)
        self.assertEqual(report["units"]["included"], len(EXPECTED_FILES), where)
        self.assertEqual(report["units"]["person_days"], 3, where)
        self.assertEqual(report["lines"]["count"], 3, where)

    # --- no state.json ---------------------------------------------------

    def test_no_state_file_beside_a_state_file(self):
        """Preview neither needs the run's state file nor touches it."""
        root = Path(self._tmp.name)
        present = root / "state-present.json"
        present.write_text(json.dumps({"watermark": "2026-03-04"}) + "\n", encoding="utf-8")
        before = present.read_bytes()
        absent = root / "state-absent.json"

        with mock.patch.object(imconfig, "STATE_PATH", present):
            populated, populated_code = self.preview()
        with mock.patch.object(imconfig, "STATE_PATH", absent):
            cold, cold_code = self.preview()
            cold_written, cold_written_code = self.preview(write=True)

        self.assert_green(populated, populated_code, "STATE present")
        self.assert_green(cold, cold_code, "STATE absent")
        self.assert_green(cold_written, cold_written_code, "STATE absent, --write")
        self.assert_populated_counts(populated, "STATE present")
        self.assertEqual(cold["units"], populated["units"])
        self.assertEqual(cold["lines"], populated["lines"])
        self.assertFalse(absent.exists(), "STATE: a preview created the run's state file.")
        self.assertEqual(present.read_bytes(), before, "STATE: a preview rewrote the state file.")

    # --- no threads/ folder ----------------------------------------------

    def test_no_threads_folder_beside_an_existing_one(self):
        """A dry run creates not even the folder; --write creates exactly the files."""
        populated, populated_code = self.preview(write=True)

        home = Path(self._tmp.name) / "fresh-home"
        home.mkdir()
        cold_threads = home / "threads"
        with mock.patch.object(imconfig, "THREADS_DIR", cold_threads):
            dry, dry_code = self.preview()
            self.assertFalse(
                cold_threads.exists(),
                "THREADS: a dry preview created the threads folder on a fresh machine.",
            )
            cold, cold_code = self.preview(write=True)

        self.assert_green(populated, populated_code, "THREADS existing, --write")
        self.assert_green(dry, dry_code, "THREADS absent, dry")
        self.assert_green(cold, cold_code, "THREADS absent, --write")
        self.assert_populated_counts(dry, "THREADS absent, dry")
        self.assertEqual(dry["write"]["would_write"], len(EXPECTED_FILES))
        self.assertEqual(
            _listing(cold_threads),
            _listing(self.threads),
            "THREADS: the first --write on a fresh machine did not produce the same "
            "files, byte for byte, as a run into a folder that already existed.",
        )
        self.assertEqual(_relative_files(cold_threads), sorted(EXPECTED_FILES))
        if os.name != "nt":
            for path in cold_threads.rglob("*"):
                if path.is_file():
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)

    # --- an empty Contacts map ---------------------------------------------

    def test_empty_contacts_beside_a_populated_map(self):
        """Contacts only NAME things: an empty map keeps every day, named by its handle."""
        populated, populated_code = self.preview(write=True)

        cold_threads = Path(self._tmp.name) / "no-contacts" / "threads"
        with mock.patch.object(imcontacts, "load_map", lambda *a, **k: {}):
            with mock.patch.object(imconfig, "THREADS_DIR", cold_threads):
                cold, cold_code = self.preview(write=True)

        self.assert_green(populated, populated_code, "CONTACTS populated")
        self.assert_green(cold, cold_code, "CONTACTS empty")
        self.assert_populated_counts(cold, "CONTACTS empty")
        self.assertEqual(cold["units"], populated["units"])
        self.assertEqual(cold["lines"], populated["lines"])
        self.assertEqual(_relative_files(self.threads), sorted(EXPECTED_FILES))
        self.assertEqual(
            _relative_files(cold_threads),
            sorted(EXPECTED_FILES_NO_CONTACTS),
            "CONTACTS: with no names to use, the transcripts were not named by their "
            "handles as the fallback promises.",
        )
        for path in cold_threads.rglob("*.txt"):
            text = path.read_text(encoding="utf-8")
            for name in (ALICE_NAME, BOB_NAME):
                self.assertNotIn(
                    name, text, "CONTACTS: a name appeared with no Contacts to give it."
                )

    # --- zero rows in the window ------------------------------------------

    def test_an_empty_window_beside_a_populated_one(self):
        populated, populated_code = self.preview()
        cold, cold_code = self.preview(date_from=EMPTY_WINDOW[0], date_to=EMPTY_WINDOW[1])

        self.assert_green(populated, populated_code, "WINDOW populated")
        text = self.assert_green(cold, cold_code, "WINDOW empty")
        self.assert_populated_counts(populated, "WINDOW populated")
        self.assertEqual(cold["read"]["messages"], 0)
        self.assertEqual(cold["units"], ZERO_UNITS)
        self.assertEqual(cold["lines"]["count"], 0)
        self.assertEqual(cold["sample"], [])
        self.assertEqual(cold["write"]["would_write"], 0)
        self.assertIn("0 lines", text)

    def test_an_empty_window_with_write_writes_nothing_then_a_full_one_writes_all(self):
        cold, cold_code = self.preview(
            date_from=EMPTY_WINDOW[0], date_to=EMPTY_WINDOW[1], write=True
        )
        self.assert_green(cold, cold_code, "WINDOW empty, --write")
        self.assertEqual(cold["write"]["files"], 0)
        self.assertFalse(cold["write"]["performed"])
        self.assertEqual(
            sorted(p.name for p in self.threads.rglob("*")),
            [],
            "WINDOW: a --write over a window with nothing in it left a year folder or a file.",
        )

        populated, populated_code = self.preview(write=True)
        self.assert_green(populated, populated_code, "WINDOW populated, --write")
        self.assertEqual(_relative_files(self.threads), sorted(EXPECTED_FILES))


if __name__ == "__main__":
    unittest.main()
