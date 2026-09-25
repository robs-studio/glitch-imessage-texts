"""imsynth — the summary seam, end to end, on a temp vault, a temp DB and a temp home.

Every claim and commit here runs the REAL pieces: ``imrun.daily`` queues the days from a
small Messages-shaped database (``test_imrun``'s harness), ``imsynth.claim`` leases them
and writes the claim file, the test plays the writer by filling the ``summaries`` map,
and ``imsynth.commit`` lands each one through ``imrun.land`` → the engine's single card
writer → the projector, all inside :mod:`imfixture`'s temp tree.  Nothing here reads
the member's chat.db, Contacts, cards or memory.db, and one test proves a full cycle
leaves the member's real places exactly as they were.

What each group holds still
---------------------------
* **Round trip** — a claim file carries what the writer needs (names from cards,
  absolute transcripts, the mechanical topic, the instructions, an empty map), and a
  commit lands each summary AS the card's topic; the ``interaction`` row survives a close
  and a reopen (G1).
* **Refusals** — em dash, line break, too long, ``(→``, the mechanical line itself,
  empty: each goes back to the queue with its reason; a missing one simply goes back.
* **Ownership** — two claims never share a day (two real processes at once); a day the
  stale fallback filed while its claim sat is skipped, never filed twice; an expired
  lease lets the fallback or a new claim take the day; a re-commit lands nothing.
* **SIGKILL** — a commit killed after a card write, or after the database commit, then a
  re-commit or a daily run: exactly one line per day, and one ``interaction`` row.
* **Containment** — the claim file is owner-only and only ever directly inside
  ``synth/``; a commit refuses a file anywhere else; claim and commit write nowhere but
  the temp home, the cards (with their lock) and the undo ring.
* **Gates** — no own handles, a changed engine, a busy lock, a damaged ledger: one
  sentence, nothing written.

Privacy: every name, number and address is invented (``555-01xx``, ``example.com``).
"""

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_HOME))

# imconfig FIRST, before anything reachable only because it put the engine on sys.path.
import imconfig  # noqa: E402

imconfig.ensure_engine_path()

import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import sqlite3  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import textwrap  # noqa: E402
import threading  # noqa: E402
import unittest  # noqa: E402
from datetime import timedelta  # noqa: E402
from unittest import mock  # noqa: E402

import imcontacts  # noqa: E402
import imessage  # noqa: E402
import imledger  # noqa: E402
import imrun  # noqa: E402
import imsynth  # noqa: E402
from imfixture import ALICE, BOB, patched, real_fixture_traces, real_state  # noqa: E402
from test_imrun import (  # noqa: E402
    _HOME_FILE,
    _PERSON_FILE,
    ALICE_PHONE,
    BOB_EMAIL,
    CAROL_PHONE,
    D1,
    D2,
    D3,
    D4,
    FRANK,
    LONG_A,
    LONG_B,
    RunCase,
    _changed,
    _conversation_lines,
    _conversation_rows,
    _tree_bytes,
    at,
    key,
    morning,
    record_writes,
)

import people_stamp  # noqa: E402

ALICE_LINK = "_local/imessage/threads/2026/2026-03-02-fixture-alice-1.txt"
ALICE_TOPIC = f"Texts (2): {LONG_A}"
GOOD = "Moved the appointment to four and asked for last week's forms"

#: Two people, two days each: four units, two cards.
SEED = [(1, ALICE_PHONE, D1), (7, BOB_EMAIL, D1), (1, ALICE_PHONE, D2), (7, BOB_EMAIL, D2)]

_CLAIM_FILE = re.compile(r"^claim-synth-\d{8}T\d{6}-[0-9a-f]{8}\.json(\.[A-Za-z0-9_]+\.tmp)?$")


# ---------------------------------------------------------------------------
# The harness.
# ---------------------------------------------------------------------------


class SynthCase(RunCase):
    """``test_imrun``'s harness (temp vault + DB, temp home, temp chat.db), plus claims."""

    def setUp(self):
        super().setUp()
        # The stale fallback stays out of the way unless a test brings it in.
        self.cfg["synth_stale_days"] = 30
        self.synth = self.home / "synth"

    def queue_units(self, spec):
        """Text on each ``(chat, who, day)``, then run daily until every day is queued.

        The first run reads the first day only (older history is backfill's); the
        second reads the rest.  Returns the morning of that second run.
        """
        for chat, who, day in spec:
            with contextlib.suppress(sqlite3.IntegrityError):
                self.chats.chat(chat)
            self.chats.exchange(chat, day, who)
        days = sorted({day for _, _, day in spec})
        self.daily(morning(days[0] + timedelta(days=1)))
        last = morning(days[-1] + timedelta(days=1))
        if days[-1] != days[0]:
            self.daily(last)
        return last

    def claim(self, now, n=None, *, contacts=None):
        with contextlib.redirect_stderr(self.stderr):
            return imsynth.claim(
                n, home=self.home, threads_dir=self.threads, db_path=self.fx.db_path,
                cfg=self.cfg, now=now, contacts={} if contacts is None else contacts,
            )

    def commit(self, path, now):
        with contextlib.redirect_stderr(self.stderr):
            return imsynth.commit(path, home=self.home, db_path=self.fx.db_path, cfg=self.cfg,
                                  now=now)

    @staticmethod
    def doc(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def write(self, path, summaries):
        """The writer's side: fill the summaries map and save the file."""
        document = self.doc(path)
        document["summaries"].update(summaries)
        Path(path).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")

    def lines(self, card_slug, day=None):
        return _conversation_lines(self.cards[card_slug].path, day)

    def rows_per_day(self):
        rows = _conversation_rows(self.fx.reopen())
        counts = {}
        for person_id, day, *_ in rows:
            counts[(person_id, day)] = counts.get((person_id, day), 0) + 1
        return counts


# ---------------------------------------------------------------------------
# 1. The round trip, and G1.
# ---------------------------------------------------------------------------


class TestClaimRoundTrip(SynthCase):
    def test_a_claim_round_trips_and_lands_every_summary(self):
        t = self.queue_units(SEED) + timedelta(hours=1)
        report = self.claim(t)
        self.assertIsNone(report["paused"])
        self.assertEqual(report["claimed"], 4)
        path = Path(report["claim_path"])
        self.assertEqual(path.parent, self.synth.resolve())
        self.assertRegex(path.name, _CLAIM_FILE)

        doc = self.doc(path)
        self.assertEqual(doc["summaries"], {})
        self.assertEqual(doc["instructions"], imsynth.DRAIN_INSTRUCTIONS)
        self.assertEqual(doc["claim_id"], report["claim_id"])
        self.assertIn(str(path), doc["commit_with"])
        units = doc["units"]
        self.assertEqual([u["id"] for u in units], ["u01", "u02", "u03", "u04"])
        self.assertEqual([u["key"] for u in units], [
            key(ALICE_PHONE, D1), key(BOB_EMAIL, D1), key(ALICE_PHONE, D2), key(BOB_EMAIL, D2),
        ], "oldest day first")
        alice = units[0]
        self.assertEqual(alice["name"], "Fixture Alice")      # from her card
        self.assertEqual(units[1]["name"], "Fixture Bob")
        self.assertEqual((alice["day"], alice["direction"]), ("2026-03-02", "they_reached_out"))
        self.assertEqual(alice["mechanical_topic"], ALICE_TOPIC)
        self.assertEqual(alice["links"], [ALICE_LINK])
        for unit in units:
            self.assertTrue(unit["threads"])
            for thread in map(Path, unit["threads"]):
                self.assertTrue(thread.is_absolute() and thread.is_file())
                self.assertIn(self.threads.resolve(), thread.parents)
        with self.ledger() as led:
            for unit in units:
                self.assertEqual(led.live_claim(unit["key"], t), report["claim_id"])

        summaries = {u["id"]: f"Settled the {u['day']} plan, the appointment moves to four"
                     for u in units}
        self.write(path, summaries)
        result = self.commit(path, t + timedelta(minutes=10))
        self.assertIsNone(result["paused"])
        self.assertIsNone(result["error"])
        self.assertEqual(sorted(result["landed"]), ["u01", "u02", "u03", "u04"])
        for bucket in ("refused", "released", "skipped", "failed"):
            self.assertEqual(result[bucket], {}, bucket)
        self.assertTrue(result["claim_released"])
        self.assertTrue(result["claim_file_removed"])
        self.assertFalse(path.exists())

        self.assertEqual(self.lines("fixture-alice", D1), [
            f"- 2026-03-02 — conversation (they_reached_out): {summaries['u01']} (→ {ALICE_LINK})"
        ])
        self.assertEqual(len(self.lines("fixture-bob", D2)), 1)
        with self.ledger() as led:
            self.assertEqual(led.queued(), {})
            self.assertIsNone(led.claim_of(report["claim_id"]))
            for unit in units:
                record = led.stamp_record(unit["key"])
                self.assertEqual((record["status"], record["topic_kind"]), ("done", "summary"))
                self.assertEqual(record["topic"], summaries[unit["id"]])

        rendered = imessage.synthesise_commit_render(result)
        self.assertIn("Filed 4 summary line(s)", rendered)
        for secret in (ALICE_PHONE, "Fixture Alice", summaries["u01"]):
            self.assertNotIn(secret, rendered)

    def test_a_valid_summary_lands_as_the_topic_and_survives_close_and_reopen(self):
        """G1: the projection happens inside the commit's transaction, so a FRESH
        connection sees the row, with the summary as its text."""
        t = self.queue_units([(1, ALICE_PHONE, D1)]) + timedelta(hours=1)
        path = self.claim(t)["claim_path"]
        self.write(path, {"u01": GOOD})
        self.commit(path, t)
        rows = _conversation_rows(self.fx.reopen())
        self.assertEqual(rows, [(ALICE.pid, "2026-03-02", "they_reached_out", GOOD, ALICE_LINK)])

    def test_the_instructions_name_the_member_as_a_rendered_transcript_does(self):
        """The writer reads transcripts, so the instructions must name the member by the
        label a transcript actually carries on their lines, read back off a real one.

        ``"(me)"`` is only the grouper's internal speaker key: an instruction naming it
        leaves the writer with no member at all, and the member's words get credited to
        the other person on that person's card.
        """
        t = self.queue_units([(1, ALICE_PHONE, D1)]) + timedelta(hours=1)
        unit = self.doc(self.claim(t)["claim_path"])["units"][0]
        text = Path(unit["threads"][0]).read_text(encoding="utf-8")
        # the member's own line is the reply, LONG_B: read the label it carries
        mine = [line for line in text.splitlines() if line.endswith(f": {LONG_B}")]
        self.assertEqual(len(mine), 1, text)
        label = re.match(r"^\d{2}:\d{2}  (.+?): ", mine[0]).group(1)
        theirs = [line for line in text.splitlines() if line.endswith(f": {LONG_A}")]
        self.assertNotIn(f"  {label}: ", theirs[0], "the premise: the label is the member's")
        self.assertEqual(label, imsynth.MEMBER_LABEL)
        self.assertIn(f'"{label}"', imsynth.DRAIN_INSTRUCTIONS)
        self.assertNotIn('"(me)"', imsynth.DRAIN_INSTRUCTIONS)
        self.assertNotIn("(me)", text, "the internal key leaked into a transcript")

    def test_the_limit_takes_the_oldest_days_and_the_rest_wait(self):
        t = self.queue_units(SEED) + timedelta(hours=1)
        self.cfg["synth_claim_limit"] = 3
        first = self.claim(t)
        self.assertEqual((first["claimed"], first["left_waiting"]), (3, 1))
        self.assertEqual(first["days"], ["2026-03-02", "2026-03-03"])
        second = self.claim(t, n=5)
        self.assertEqual(second["claimed"], 1)
        self.assertEqual([u["key"] for u in self.doc(second["claim_path"])["units"]],
                         [key(BOB_EMAIL, D2)])
        third = self.claim(t)
        self.assertIsNone(third["claim_path"])
        self.assertIn("already with another summary pass", third["why_nothing"])
        with self.assertRaises(ValueError):
            self.claim(t, n=0)

    def test_nothing_waiting_is_said_plainly_and_writes_nothing(self):
        before = _tree_bytes(self.home, self.fx.root)
        cold = self.claim(morning(D2))
        self.assertIsNone(cold["claim_path"])
        self.assertEqual(cold["why_nothing"], "no texts have been queued yet")
        self.assertEqual(_changed(before, _tree_bytes(self.home, self.fx.root)), [])
        self.assertFalse((self.home / "ledger.lock").exists())

        t = self.queue_units([(1, ALICE_PHONE, D1)]) + timedelta(hours=1)
        path = self.claim(t)["claim_path"]
        self.write(path, {"u01": GOOD})
        self.commit(path, t)
        before = _tree_bytes(self.home)
        empty = self.claim(t + timedelta(hours=1))
        self.assertEqual(empty["why_nothing"], "nothing is waiting for a summary")
        self.assertEqual(_changed(before, _tree_bytes(self.home)), [])
        self.assertIn("Nothing to summarise right now", imessage.synthesise_render(empty))

    def test_a_day_with_no_transcript_on_this_machine_is_not_claimed(self):
        t = self.queue_units([(1, ALICE_PHONE, D1)]) + timedelta(hours=1)
        (self.threads / "2026" / Path(ALICE_LINK).name).unlink()
        report = self.claim(t)
        self.assertIsNone(report["claim_path"])
        self.assertEqual(report["no_transcript"], 1)
        self.assertIn("no transcript on this machine", report["why_nothing"])

    def test_display_name_prefers_the_card_then_contacts_then_a_masked_handle(self):
        conn = self.fx.reopen()
        gone = "prs_gonexx22"
        cases = [
            ({"person_id": ALICE.pid, "identifier": ALICE_PHONE, "name": "Other"}, {},
             "Fixture Alice"),
            ({"person_id": gone, "identifier": FRANK, "name": "Fixture Frank"}, {},
             "Fixture Frank"),
            ({"person_id": gone, "identifier": FRANK, "name": "Old Name"},
             {FRANK: "Fixture Frank Now"}, "Fixture Frank Now"),
            ({"person_id": None, "identifier": "+15555550164"}, {}, "+<11 digits>"),
            ({"person_id": None, "identifier": "someone@example.com"}, {},
             "<local 7>@<domain 11>"),
        ]
        for record, contacts, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(imsynth.display_name(conn, record, contacts), expected)
        # Only with no card, no captured name and no map given is the address book read.
        with mock.patch.object(imcontacts, "load_map", lambda *a, **k: {FRANK: "From Book"}):
            self.assertEqual(
                imsynth.display_name(conn, {"person_id": gone, "identifier": FRANK}), "From Book")


# ---------------------------------------------------------------------------
# 2. The rules, and every refusal returning to the queue with its reason.
# ---------------------------------------------------------------------------


class TestValidateSummary(unittest.TestCase):
    TOPIC = "Texts (2): Can we move the appointment to four o'clock, or is the morning easier…"
    GROUP = "Group texts in Garden Group (3 messages)"

    def test_a_good_summary_passes(self):
        for text in (GOOD, "Logistics: pickup at 3", "x" * imsynth.MAX_SUMMARY_CHARS,
                     f"  {GOOD}  ", "Confirmed the plan (see the forms)"):
            with self.subTest(text=text[:20]):
                self.assertIsNone(imsynth.validate_summary(text, self.TOPIC))

    def test_each_rule_refuses_with_a_plain_reason(self):
        cases = {
            "not text": [None, 42, ["a list"]],
            "more than one line": [f"{GOOD}\nmore", f"{GOOD}\r", f"{GOOD}\u2028more",
                                   f"{GOOD}\x85", f"{GOOD}\x0b", f"{GOOD}\u2029"],
            "control character": [f"{GOOD}\tmore", f"{GOOD}\x00", f"{GOOD}\u202e"],
            "empty": ["", "   "],
            "401 characters": ["y" * 401, "  " + "y" * 401],
            "em dash": ["Moved the appointment \u2014 and the forms"],
            "(→": ["Moved it (→ somewhere)", "Moved it ( → somewhere)"],
            "[mtg:": ["Moved it [mtg:mtg_abcdefgh]"],
            "only repeats": [
                self.TOPIC, self.TOPIC.lower(), "Can we move the appointment to four o'clock",
                "texts: can we move the appointment", "Texts (2)", "Texts",
            ],
        }
        for reason, texts in cases.items():
            for text in texts:
                with self.subTest(reason=reason, text=repr(text)[:30]):
                    said = imsynth.validate_summary(text, self.TOPIC)
                    self.assertIsNotNone(said)
                    self.assertIn(reason, said)

    def test_a_group_line_restated_is_refused_and_real_words_are_not(self):
        for text in (self.GROUP, "Group texts in Garden Group", "Garden Group", "garden group."):
            with self.subTest(text=text):
                self.assertIn("only repeats", imsynth.validate_summary(text, self.GROUP))
        self.assertIsNone(imsynth.validate_summary(
            "Garden Group set the camping trip for the 10th and asked for drivers", self.GROUP))

    def test_a_reason_never_quotes_the_summary(self):
        secret = "Fixture secret words \u2014 here"
        self.assertNotIn("Fixture secret", imsynth.validate_summary(secret, self.TOPIC))

    def test_the_instructions_follow_their_own_rules(self):
        text = imsynth.DRAIN_INSTRUCTIONS
        self.assertNotIn(imsynth.EM_DASH, text)
        for rule in ("ONE summary", "400 characters", "U+2014", "(→", "mechanical_topic",
                     "never invent", "logistics", "a comma, a full stop or a colon"):
            self.assertIn(rule, text)


class TestRefusalsReturnToTheQueue(SynthCase):
    def test_every_refusal_goes_back_with_its_reason_and_a_missing_one_just_goes_back(self):
        spec = [(1, ALICE_PHONE, d) for d in (D1, D2, D3, D4)]
        spec += [(7, BOB_EMAIL, d) for d in (D1, D2, D3, D4)]
        t = self.queue_units(spec) + timedelta(hours=1)
        report = self.claim(t, n=8)
        path = report["claim_path"]
        units = {u["id"]: u for u in self.doc(path)["units"]}
        self.assertEqual(len(units), 8)
        self.write(path, {
            "u01": "Moved the appointment \u2014 and the forms",
            "u02": "Moved the appointment\nand the forms",
            "u03": "z" * 401,
            "u04": "Moved it (→ somewhere else)",
            "u05": units["u05"]["mechanical_topic"],
            "u06": "   ",
            "u07": GOOD,
            # u08: nothing written
        })
        result = self.commit(path, t + timedelta(minutes=5))
        self.assertEqual(result["landed"], ["u07"])
        expected = {"u01": "em dash", "u02": "one line", "u03": "401 characters",
                    "u04": "(→", "u05": "only repeats", "u06": "empty"}
        self.assertEqual(sorted(result["refused"]), sorted(expected))
        for uid, reason in expected.items():
            self.assertIn(reason, result["refused"][uid])
        self.assertEqual(result["released"], {"u08": "no summary was written"})
        rendered = imessage.synthesise_commit_render(result)
        self.assertIn("Refused, back in the queue (6)", rendered)
        self.assertIn("u01 (2026-03-02): the summary holds an em dash", rendered)

        with self.ledger() as led:
            for uid, unit in units.items():
                if uid == "u07":
                    self.assertTrue(led.is_stamped(unit["key"]))
                    continue
                self.assertTrue(led.is_queued(unit["key"]), uid)
                self.assertFalse(led.is_stamped(unit["key"]), uid)
                self.assertIsNone(led.live_claim(unit["key"], t), uid)
            self.assertEqual(led.failures(), {})
            self.assertEqual(led.open_intents(), {})
        self.assertEqual(self.lines("fixture-alice"), [
            f"- 2026-03-05 — conversation (they_reached_out): {GOOD} "
            "(→ _local/imessage/threads/2026/2026-03-05-fixture-alice-1.txt)"
        ])
        self.assertEqual(self.lines("fixture-bob"), [])
        again = self.claim(t + timedelta(minutes=10), n=20)
        self.assertEqual(again["claimed"], 7, "the refused days were not back in the queue")

    def test_two_different_summaries_for_one_day_are_refused(self):
        t = self.queue_units([(1, ALICE_PHONE, D1)]) + timedelta(hours=1)
        path = self.claim(t)["claim_path"]
        self.write(path, {"u01": GOOD, key(ALICE_PHONE, D1): "Something else entirely here"})
        result = self.commit(path, t)
        self.assertIn("two different summaries", result["refused"]["u01"])
        self.assertEqual(self.lines("fixture-alice"), [])

    def test_a_summary_keyed_by_the_full_ledger_key_is_taken(self):
        t = self.queue_units([(1, ALICE_PHONE, D1)]) + timedelta(hours=1)
        path = self.claim(t)["claim_path"]
        self.write(path, {key(ALICE_PHONE, D1): GOOD, "u99": "names no unit at all here"})
        result = self.commit(path, t)
        self.assertEqual(result["landed"], ["u01"])
        self.assertEqual(result["unknown_summaries"], 1)

    def test_a_conversation_that_arrives_after_the_claim_sends_the_day_back(self):
        t = self.queue_units([(1, ALICE_PHONE, D1)]) + timedelta(hours=1)
        path = self.claim(t)["claim_path"]
        self.write(path, {"u01": GOOD})
        self.chats.chat(20, group=True, name="Walk")
        self.chats.say(20, at(D1, 18), LONG_A, sender=ALICE_PHONE)
        self.chats.say(20, at(D1, 18, 1), LONG_B, sender=CAROL_PHONE)
        self.assertEqual(self.daily(t + timedelta(hours=1))["links_refreshed"], 1)

        result = self.commit(path, t + timedelta(hours=2))
        self.assertIn("after it was claimed", result["released"]["u01"])
        self.assertEqual(self.lines("fixture-alice"), [])
        again = self.claim(t + timedelta(hours=3))
        alice = next(u for u in self.doc(again["claim_path"])["units"]
                     if u["key"] == key(ALICE_PHONE, D1))
        self.assertEqual(len(alice["threads"]), 2, "the new claim hands over both conversations")

    def test_a_card_that_refuses_the_write_keeps_the_day_queued(self):
        t = self.queue_units([(1, ALICE_PHONE, D1)]) + timedelta(hours=1)
        path = self.claim(t)["claim_path"]
        self.write(path, {"u01": GOOD})
        refusal = "couldn't back fixture-alice.md up before rewriting it (disk full)"

        def refusing(person_path, *, source, occurred_at, direction, topic=None,
                     legacy_topic=None, link=None, note=None, source_meeting_id=None,
                     conn=None):
            return people_stamp.StampResult(stamped=False, detail=refusal)

        with mock.patch.object(people_stamp, "stamp_interaction", refusing):
            result = self.commit(path, t)
        self.assertEqual(result["failed"], {"u01": refusal})
        with self.ledger() as led:
            self.assertTrue(led.is_queued(key(ALICE_PHONE, D1)))
            self.assertEqual(led.failures()[key(ALICE_PHONE, D1)]["detail"], refusal)
            self.assertEqual(led.open_intents(), {})


# ---------------------------------------------------------------------------
# 3. Ownership: concurrent claims, the stale fallback, expired leases, re-commits.
# ---------------------------------------------------------------------------

#: Re-roots the engine's config onto the parent test's fixture tree, exactly as
#: ``imfixture.SpineFixture._patch_config`` does, so a child process reaches only it.
CHILD_PRELUDE = textwrap.dedent(
    """
    import json, sys, time
    from datetime import datetime
    from pathlib import Path
    sys.path.insert(0, {plugin!r})
    import imconfig
    imconfig.ensure_engine_path()
    import config
    root = Path({root!r})
    real_memory = config.MEMORY_DIR
    moves = {{"MEMORY_SNAPSHOTS_DIR": root / "memory-snapshots",
             "LOCAL_SNAPSHOTS_DIR": root / "local-snapshots",
             "DATABASE_PATH": root / "memory.db"}}
    for name, value in list(vars(config).items()):
        if name.startswith("__") or not isinstance(value, Path):
            continue
        if value == real_memory or real_memory in value.parents:
            moves[name] = root / "Memory" / value.relative_to(real_memory)
    for name, value in moves.items():
        setattr(config, name, value)
    for name in ("MEMORY_DIR", "PEOPLE_DIR", "MEMORY_SNAPSHOTS_DIR", "DATABASE_PATH"):
        assert root in Path(getattr(config, name)).parents, name
    import imledger, imspine, imsynth
    cfg = json.loads({cfg!r})
    now = datetime.fromisoformat({now!r})
    home = Path({home!r})
    """
)

CLAIM_CHILD = CHILD_PRELUDE + textwrap.dedent(
    """
    print("READY", flush=True)
    go = Path({go!r})
    while not go.exists():
        time.sleep(0.002)
    report = imsynth.claim({n!r}, home=home, threads_dir=Path({threads!r}), cfg=cfg, now=now,
                           contacts={{}})
    print(json.dumps({{"claim_path": report["claim_path"], "paused": report["paused"]}}),
          flush=True)
    """
)

KILL_CHILD = CHILD_PRELUDE + textwrap.dedent(
    """
    point = {point!r}
    real_stamp = imspine.stamp
    calls = []

    def stamp(*args, **kwargs):
        out = real_stamp(*args, **kwargs)
        calls.append(out)
        if point == "after_second_card_write" and len(calls) == 2:
            print("KILL-POINT", flush=True)
            time.sleep(120)
        return out

    real_commit_run = imledger.Ledger.commit_run

    def commit_run(self, run_id):
        if point == "after_db_commit" and "-commit-" in run_id:
            print("KILL-POINT", flush=True)
            time.sleep(120)
        return real_commit_run(self, run_id)

    imspine.stamp = stamp
    imledger.Ledger.commit_run = commit_run
    imsynth.commit({path!r}, home=home, cfg=cfg, now=now)
    print("FINISHED", flush=True)
    """
)


class ChildCase(SynthCase):
    """Helpers for tests that run imsynth in a REAL second process."""

    def child_code(self, template, now, **extra):
        return template.format(
            plugin=str(PLUGIN_HOME), root=str(self.fx.root), cfg=json.dumps(self.cfg),
            now=now.isoformat(), home=str(self.home), **extra,
        )

    def spawn(self, code):
        self.fx.commit()
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        watchdog = threading.Timer(90.0, proc.kill)
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        self.addCleanup(self._reap, proc)
        return proc

    @staticmethod
    def _reap(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    def read_line(self, proc, wanted):
        line = proc.stdout.readline()
        if line.strip() != wanted and not (wanted == "*" and line.strip()):
            proc.kill()
            proc.wait(timeout=10)
            self.fail(f"the child said {line!r}, not {wanted!r}: {proc.stderr.read()[-800:]}")
        return line


@unittest.skipUnless(os.name == "posix", "the two-process claim race is run on POSIX")
class TestConcurrentClaims(ChildCase):
    def test_two_claims_at_once_never_share_a_day(self):
        t = self.queue_units(SEED) + timedelta(hours=1)
        go = self.root / "go"
        code = self.child_code(CLAIM_CHILD, t, go=str(go), n=2, threads=str(self.threads))
        first, second = self.spawn(code), self.spawn(code)
        for proc in (first, second):
            self.read_line(proc, "READY")
        go.touch()                                   # both released at the same moment
        paths = []
        for proc in (first, second):
            out = json.loads(self.read_line(proc, "*"))
            self.assertIsNone(out["paused"], out)
            paths.append(out["claim_path"])
        keys = [{u["key"] for u in self.doc(p)["units"]} for p in paths]
        self.assertEqual(keys[0] & keys[1], set(), "two claims share a day")
        self.assertEqual(keys[0] | keys[1], {key(p, d) for _, p, d in SEED})
        self.assertNotEqual(paths[0], paths[1])

    def test_a_second_claim_while_the_first_is_live_takes_nothing_of_it(self):
        t = self.queue_units(SEED) + timedelta(hours=1)
        first = self.claim(t, n=10)
        second = self.claim(t + timedelta(minutes=1), n=10)
        self.assertEqual(first["claimed"], 4)
        self.assertIsNone(second["claim_path"])
        self.assertEqual(second["held_by_other_claims"], 4)


class TestOwnership(SynthCase):
    def test_a_day_the_stale_fallback_filed_is_skipped_never_filed_twice(self):
        t = self.queue_units([(1, ALICE_PHONE, D1)]) + timedelta(hours=1)
        path = self.claim(t)["claim_path"]
        self.write(path, {"u01": GOOD})
        self.cfg["synth_stale_days"] = 1
        held = self.daily(t + timedelta(hours=23))       # stale by age, but claimed
        self.assertEqual(held["stale"]["due"], 0)
        filed = self.daily(t + timedelta(hours=25))      # the lease ran out: the net lands it
        self.assertEqual(filed["stale"]["stamped"], 1)

        result = self.commit(path, t + timedelta(hours=26))
        self.assertEqual(result["landed"], [])
        self.assertIn("already on the card", result["skipped"]["u01"])
        lines = self.lines("fixture-alice", D1)
        self.assertEqual(len(lines), 1, "a day was filed twice")
        self.assertIn(ALICE_TOPIC, lines[0])
        self.assertEqual(self.rows_per_day(), {(ALICE.pid, "2026-03-02"): 1})

    def test_an_expired_lease_lets_a_new_claim_take_the_day(self):
        t = self.queue_units([(1, ALICE_PHONE, D1)]) + timedelta(hours=1)
        path_a = Path(self.claim(t)["claim_path"])
        self.write(path_a, {"u01": "The first pass wrote this about the appointment"})
        saved = path_a.read_bytes()                      # the first writer, still holding it

        second = self.claim(t + timedelta(hours=25))
        self.assertEqual((second["claimed"], second["expired_claims_cleared"]), (1, 1))
        self.assertEqual(second["old_claim_files_removed"], 1)
        self.assertFalse(path_a.exists(), "the cleared claim's file was left behind")
        path_a.write_bytes(saved)                        # ...which it now saves back

        late = self.commit(path_a, t + timedelta(hours=26))
        self.assertEqual(late["landed"], [])
        self.assertIn("no longer holds it", late["skipped"]["u01"])
        self.write(second["claim_path"], {"u01": GOOD})
        result = self.commit(second["claim_path"], t + timedelta(hours=26))
        self.assertEqual(result["landed"], ["u01"])
        self.assertEqual(len(self.lines("fixture-alice", D1)), 1)
        self.assertIn(GOOD, self.lines("fixture-alice", D1)[0])

    def test_an_expired_lease_lets_the_stale_fallback_take_the_day(self):
        t = self.queue_units([(1, ALICE_PHONE, D1)]) + timedelta(hours=1)
        self.claim(t)
        self.cfg["synth_stale_days"] = 1
        report = self.daily(t + timedelta(hours=25))
        self.assertEqual(report["stale"]["stamped"], 1)
        self.assertEqual(len(self.lines("fixture-alice", D1)), 1)

    def test_committing_the_same_claim_again_lands_nothing(self):
        t = self.queue_units(SEED) + timedelta(hours=1)
        path = Path(self.claim(t)["claim_path"])
        self.write(path, {f"u0{i}": f"Plan number {i} was settled and the forms were sent"
                          for i in range(1, 5)})
        saved = path.read_bytes()
        first = self.commit(path, t)
        self.assertEqual(len(first["landed"]), 4)
        cards = {slug: self.lines(slug) for slug in ("fixture-alice", "fixture-bob")}

        path.write_bytes(saved)                          # the same file, committed again
        again = self.commit(path, t + timedelta(minutes=1))
        self.assertEqual(again["landed"], [])
        self.assertEqual(again["already_on_card"], [])
        self.assertEqual(len(again["skipped"]), 4)
        self.assertEqual({slug: self.lines(slug) for slug in cards}, cards)
        self.assertEqual(set(self.rows_per_day().values()), {1})
        self.assertEqual(len(self.rows_per_day()), 4)

        gone = self.commit(path, t + timedelta(minutes=2))
        self.assertIn("not there", gone["error"])


# ---------------------------------------------------------------------------
# 4. SIGKILL mid-commit, for real.
# ---------------------------------------------------------------------------


@unittest.skipUnless(hasattr(signal, "SIGKILL"), "SIGKILL is POSIX-only")
class TestSigkillMidCommit(ChildCase):
    SUMMARIES = {f"u0{i}": f"Plan number {i} was settled and the forms were sent"
                 for i in range(1, 5)}

    def killed_commit(self, point):
        """Queue four days, claim them, write their summaries, and SIGKILL the commit."""
        self.t = self.queue_units(SEED) + timedelta(hours=1)
        self.path = Path(self.claim(self.t)["claim_path"])
        self.write(self.path, self.SUMMARIES)
        proc = self.spawn(self.child_code(KILL_CHILD, self.t, point=point, path=str(self.path)))
        self.read_line(proc, "KILL-POINT")
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, -signal.SIGKILL)

    def assert_one_line_per_day(self):
        for slug, ident in (("fixture-alice", ALICE_PHONE), ("fixture-bob", BOB_EMAIL)):
            for day in (D1, D2):
                self.assertEqual(len(self.lines(slug, day)), 1, f"{slug} {day}")
        self.assertEqual(self.rows_per_day(), {
            (ALICE.pid, "2026-03-02"): 1, (ALICE.pid, "2026-03-03"): 1,
            (BOB.pid, "2026-03-02"): 1, (BOB.pid, "2026-03-03"): 1,
        })
        with self.ledger() as led:
            self.assertEqual(led.queued(), {})
            self.assertEqual(led.open_intents(), {})

    def test_killed_after_a_card_write_then_recommitted(self):
        self.killed_commit("after_second_card_write")
        # the premise: two lines reached the cards, and their rows died with the process
        landed = self.lines("fixture-alice", D1) + self.lines("fixture-bob", D1)
        self.assertEqual(len(landed), 2)
        self.assertEqual(_conversation_rows(self.fx.reopen()), [])
        self.assertTrue(self.path.exists())

        result = self.commit(self.path, self.t + timedelta(minutes=5))
        self.assertEqual(result["recovered"]["done"], 2)
        self.assertEqual(sorted(result["landed"]), ["u03", "u04"])
        self.assert_one_line_per_day()

    def test_killed_after_a_card_write_then_the_daily_run(self):
        self.killed_commit("after_second_card_write")
        self.cfg["synth_stale_days"] = 1
        report = self.daily(self.t + timedelta(hours=25))   # the claim's lease has run out
        self.assertEqual(report["recovered"]["done"], 2)
        self.assertEqual(report["stale"]["stamped"], 2)
        self.assert_one_line_per_day()
        late = self.commit(self.path, self.t + timedelta(hours=26))
        self.assertEqual(late["landed"], [])
        self.assert_one_line_per_day()

    def test_killed_after_the_database_commit_then_recommitted(self):
        self.killed_commit("after_db_commit")
        self.assertEqual(len(_conversation_rows(self.fx.reopen())), 4)
        with self.ledger() as led:
            self.assertEqual(len(led.open_intents()), 4, "the ledger run did not commit")
        result = self.commit(self.path, self.t + timedelta(minutes=5))
        self.assertEqual(result["recovered"]["done"], 4)
        self.assertEqual(result["landed"], [])
        self.assert_one_line_per_day()


# ---------------------------------------------------------------------------
# 5. Containment: where a claim file may be, and where claim and commit write.
# ---------------------------------------------------------------------------


class TestContainment(SynthCase):
    def test_the_claim_file_is_owner_only_and_directly_inside_synth(self):
        t = self.queue_units([(1, ALICE_PHONE, D1)]) + timedelta(hours=1)
        path = Path(self.claim(t)["claim_path"])
        self.assertEqual(path.parent, self.synth.resolve())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.synth.iterdir()], [path.name], "a temp file")

        name = "claim-synth-20260101T000000-abcdef12.json"
        for target in (self.home / name, self.synth / "nested" / name,
                       self.synth / "other.json", self.root / name):
            with self.subTest(target=str(target)), self.assertRaises(ValueError):
                imsynth._refuse_foreign_target(target, self.home)
        outside = self.root / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        planted = self.synth / name
        planted.symlink_to(outside)
        with self.assertRaises(ValueError):
            imsynth._refuse_foreign_target(planted, self.home)

    def test_a_synth_folder_that_leads_out_of_the_home_is_refused(self):
        home = self.root / "home2"
        home.mkdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (home / "synth").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(ValueError):
            imsynth._write_claim_file(home, "synth-20260101T000000-abcdef12", b"{}")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_commit_refuses_a_file_outside_synth_and_writes_nothing(self):
        t = self.queue_units([(1, ALICE_PHONE, D1)]) + timedelta(hours=1)
        path = Path(self.claim(t)["claim_path"])
        self.write(path, {"u01": GOOD})
        copy = self.home / path.name
        shutil.copy(path, copy)
        before = _tree_bytes(self.home, self.fx.root)
        for target in (copy, self.home / "synth" / ".." / path.name):
            with self.subTest(target=str(target)):
                result = self.commit(target, t)
                self.assertIn("claim files live only directly inside", result["error"])
        broken = self.synth / "claim-synth-20260101T000000-abcdef12.json"
        broken.write_text("{not json", encoding="utf-8")
        before = _tree_bytes(self.home, self.fx.root)
        self.assertIn("not valid JSON", self.commit(broken, t)["error"])
        self.assertEqual(_changed(before, _tree_bytes(self.home, self.fx.root)), [])
        self.assertEqual(self.lines("fixture-alice"), [])

    def allowed(self, path):
        p = Path(path).resolve()
        synth = self.synth.resolve()
        if p == synth or (p.parent == synth and _CLAIM_FILE.match(p.name)):
            return "synth"
        if p.parent == self.home and _HOME_FILE.match(p.name):
            return "home"
        if p.parent == self.fx.people_dir.resolve() and _PERSON_FILE.match(p.name):
            return "card"
        snaps = self.fx.snapshots_dir.resolve()
        if p == snaps or snaps in p.parents:
            return "snapshots"
        return None

    def test_claim_and_commit_write_only_in_their_own_places(self):
        t = self.queue_units(SEED) + timedelta(hours=1)
        with record_writes() as claimed:
            path = self.claim(t)["claim_path"]
        self.write(path, {f"u0{i}": f"Plan number {i} was settled and the forms were sent"
                          for i in range(1, 5)})       # the writer's own save, not ours
        with record_writes() as committed:
            result = self.commit(path, t)
        self.assertEqual(len(result["landed"]), 4)
        seen = claimed + committed
        strays = sorted({(p, w) for p, w in seen if self.allowed(p) is None})
        self.assertEqual(strays, [], "claim or commit wrote outside its own places")
        self.assertEqual({self.allowed(p) for p, _ in seen},
                         {"synth", "home", "card", "snapshots"},
                         "the sweep did not see every kind of write, so it proves too little")


class TestNothingEscapes(unittest.TestCase):
    def test_a_full_cycle_leaves_the_real_places_as_they_were(self):
        # Existence and mtime only, never a byte of content, compared before and after:
        # it holds the same on a fresh install (none of these exist yet) as on a live one.
        real_home = [PLUGIN_HOME / name for name in
                     ("synth", "ledger.json", "ledger.wal", "ledger.lock", "state.json")]

        def home_state():
            return [(p.exists(), p.stat().st_mtime_ns if p.exists() else None)
                    for p in real_home]

        before, traces, home_before = real_state(), real_fixture_traces(), home_state()
        for name in ("test_a_claim_round_trips_and_lands_every_summary",):
            result = unittest.TestResult()
            TestClaimRoundTrip(name).run(result)
            self.assertEqual(result.errors + result.failures, [])
        self.assertEqual(real_state(), before)
        self.assertEqual(real_fixture_traces(), traces)
        self.assertEqual(home_state(), home_before)


# ---------------------------------------------------------------------------
# 6. Gates: one sentence, nothing written.
# ---------------------------------------------------------------------------


class TestGates(SynthCase):
    def setUp(self):
        super().setUp()
        self.t = self.queue_units([(1, ALICE_PHONE, D1)]) + timedelta(hours=1)
        self.path = self.claim(self.t)["claim_path"]
        self.write(self.path, {"u01": GOOD})
        self.cfg["synth_claim_limit"] = 5

    def assert_paused_and_untouched(self, reason):
        for verb in ("claim", "commit"):
            with self.subTest(verb=verb):
                before = _tree_bytes(self.home, self.fx.root)
                if verb == "claim":
                    report = self.claim(self.t)
                else:
                    report = self.commit(self.path, self.t)
                self.assertEqual(report["paused"]["reason"], reason, report["paused"])
                self.assertTrue(report["paused"]["sentence"].strip())
                self.assertEqual(_changed(before, _tree_bytes(self.home, self.fx.root)), [],
                                 f"{verb} wrote something while paused ({reason})")
                self.assertIn(report["paused"]["sentence"],
                              imessage.synthesise_render(report) if verb == "claim"
                              else imessage.synthesise_commit_render(report))
        self.assertEqual(self.lines("fixture-alice"), [])

    def test_no_own_handles(self):
        self.cfg["own_handles"] = []
        self.assert_paused_and_untouched("no_own_handles")

    def test_engine_drift(self):
        def changed(person_path, *, source):  # a signature the plug-in was not built for
            return None

        with patched(people_stamp, "stamp_interaction", changed):
            self.assert_paused_and_untouched("engine_changed")

    def test_the_ledger_lock_is_busy(self):
        with imledger.default_lock(self.home), mock.patch.object(imsynth, "LOCK_TIMEOUT_S", 0.2):
            self.assert_paused_and_untouched("busy")

    def test_a_damaged_ledger(self):
        (self.home / "ledger.json").write_text("{not json", encoding="utf-8")
        self.assert_paused_and_untouched("ledger_damaged")


# ---------------------------------------------------------------------------
# 7. The CLI and status.
# ---------------------------------------------------------------------------


class TestCli(SynthCase):
    def cli(self, *argv):
        out = io.StringIO()
        with (
            mock.patch.object(imconfig, "HOME", self.home),
            mock.patch.object(imconfig, "THREADS_DIR", self.threads),
            mock.patch.object(imconfig, "load_config", lambda *a, **k: dict(self.cfg)),
            mock.patch.object(imcontacts, "load_map", lambda *a, **k: {}),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = imessage.main(list(argv))
        return code, out.getvalue()

    def test_claim_prints_the_path_last_then_commit_files_it(self):
        self.queue_units([(1, ALICE_PHONE, D1), (7, BOB_EMAIL, D1)])
        code, text = self.cli("synthesise", "--limit", "1")
        self.assertEqual(code, 0)
        last = text.strip().splitlines()[-1]
        self.assertTrue(last.startswith("CLAIM_PATH: "), text)
        path = Path(last[len("CLAIM_PATH: "):])
        self.assertEqual(path.parent, self.synth.resolve())
        self.assertIn("Claimed 1 day(s)", text)
        self.assertIn("1 more day(s) wait", text)
        self.write(path, {"u01": GOOD})

        code, text = self.cli("synthesise", "--commit", str(path))
        self.assertEqual(code, 0, text)
        self.assertIn("Filed 1 summary line(s)", text)
        self.assertIn("The claim file was removed.", text)
        self.assertEqual(len(self.lines("fixture-alice", D1)), 1)

        code, text = self.cli("synthesise", "--commit", str(path))
        self.assertEqual(code, 1)
        self.assertIn("Nothing was filed", text)

    def test_json_mode_is_one_object_and_nothing_waiting_says_so(self):
        code, text = self.cli("synthesise", "--json")
        self.assertEqual(code, 0)
        report = json.loads(text)
        self.assertIsNone(report["claim_path"])
        code, text = self.cli("synthesise")
        self.assertNotIn("CLAIM_PATH", text)
        self.assertIn("Nothing to summarise right now: no texts have been queued yet.", text)

    def test_bad_arguments_are_refused_by_the_parser(self):
        for argv in (["synthesise", "--limit", "0"], ["synthesise", "--limit", "x"],
                     ["synthesise", "--limit", "2", "--commit", "p"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.cli(*argv)

    def test_no_own_handles_is_one_sentence_and_nothing_written(self):
        self.cfg["own_handles"] = []
        before = _tree_bytes(self.home)
        code, text = self.cli("synthesise")
        self.assertEqual(code, 0)
        self.assertEqual(text.strip(), imconfig.REFUSAL_NO_OWN_HANDLES)
        self.assertEqual(_changed(before, _tree_bytes(self.home)), [])

    def test_status_shows_the_live_claim_and_the_oldest_unclaimed_day(self):
        t = self.queue_units(SEED) + timedelta(hours=1)
        report = self.claim(t, n=1)
        status = imrun.status(home=self.home, now=t + timedelta(minutes=1))
        ledger = status["ledger"]
        self.assertEqual(ledger["queue"], {"depth": 4, "oldest_day": "2026-03-02"})
        self.assertEqual(ledger["unclaimed"], {"depth": 3, "oldest_day": "2026-03-02"})
        self.assertEqual([(c["claim"], c["days"]) for c in ledger["live_claims"]],
                         [(report["claim_id"], 1)])
        text = imessage.status_render(status)
        self.assertIn("a summary pass holds 1 of them until", text)
        self.assertIn("3 wait for a summary pass, the oldest from 2026-03-02.", text)


if __name__ == "__main__":
    unittest.main()
