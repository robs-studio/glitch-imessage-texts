"""show — where did we leave off: the card's lines newest first, their texts, nothing written.

Every run goes through the real daily pipeline to put ``conversation`` lines on the
fixture cards (queued, then filed by the stale fallback), inside :mod:`imfixture`'s temp
vault and database, against ``test_imrun``'s temp Messages database and a temp home.

What each group holds still
---------------------------
* **The answer** — the person resolved from a name, a number or a card id; their card's
  lines newest first, each with the stored transcript it links to; the days not on the
  card yet (waiting for a summary, or held for review) in the same timeline; and every
  byte of the home, the vault and the database exactly where it was.
* **Bounded** — ``--limit`` and ``--since``; a long transcript shows its END; every cut is
  named, with the flag that shows more.
* **Never a guess** — a name that fits two people lists both and reads nothing more; a
  name that fits nobody says why.
* **Today, live** — ``--today`` reads today's still-open texts with that person from
  ``chat.db`` and nobody else's, and stores nothing.

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
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

import imessage  # noqa: E402
import imrun  # noqa: E402
import imshow  # noqa: E402
import imwho  # noqa: E402
from imfixture import ALICE  # noqa: E402
from test_imrun import (  # noqa: E402
    ALICE_PHONE,
    CAROL_PHONE,
    D1,
    D2,
    D3,
    D4,
    D5,
    FRANK,
    LONG_A,
    LONG_B,
    RunCase,
    _changed,
    _conversation_lines,
    _tree_bytes,
    at,
    morning,
)

FRANK_TWIN = "+15555550169"     # a second "Frank" in Contacts, no card
CAROL_WORDS = "Carol's own words about the fence repair and the gate latch today"


class ShowCase(RunCase):
    """Alice: three days on her card (D1-D3) and one waiting for its summary (D4).
    Frank: one day held for review (a Contacts name, no card)."""

    def setUp(self):
        super().setUp()
        self.chats.chat(1)
        self.chats.chat(2)
        self.chats.chat(3)
        for day in (D1, D2, D3):
            self.chats.exchange(1, day, ALICE_PHONE)
        self.chats.exchange(3, D1, FRANK)
        self.daily(morning(D2))                    # first run: D1
        self.daily(morning(D4))                    # D2, D3
        self.cfg["synth_stale_days"] = 0
        filed = self.daily(morning(D4))            # all three land with their plain line
        self.assertEqual(filed["stale"]["stamped"], 3)
        self.cfg["synth_stale_days"] = 30
        self.chats.exchange(1, D4, ALICE_PHONE)
        self.daily(morning(D5))                    # D4 waits for its summary
        self.alice = self.cards["fixture-alice"].path
        self.assertEqual(len(_conversation_lines(self.alice)), 3)

    def show(self, person, **kw):
        kw.setdefault("contacts", self.contacts)
        kw.setdefault("cfg", self.cfg)
        kw.setdefault("now", morning(D5))
        with contextlib.redirect_stderr(self.stderr):
            return imshow.show(person, home=self.home, threads_dir=self.threads,
                               db_path=self.fx.db_path, **kw)

    def places(self):
        return _tree_bytes(self.home, self.fx.root)


class TestTheAnswer(ShowCase):
    def test_it_lists_newest_first_with_each_transcript_and_writes_nothing(self):
        before = self.places()
        report = self.show("Fixture Alice")
        self.assertEqual(_changed(before, self.places()), [], "show wrote something")
        self.assertIsNone(report["paused"])
        self.assertEqual(report["match"]["status"], "one")
        self.assertEqual(report["match"]["who"]["name"], "Fixture Alice")
        self.assertEqual(report["match"]["who"]["card"], "people/fixture-alice.md")

        days = [(d["day"], d["state"]) for d in report["days"]]
        self.assertEqual(days, [("2026-03-05", "waiting"), ("2026-03-04", "on_card"),
                                ("2026-03-03", "on_card"), ("2026-03-02", "on_card")])
        self.assertEqual(report["total_days"], 4)
        self.assertEqual(report["cut"], [])
        for entry in report["days"]:
            self.assertEqual(entry["summary"], f"Texts (2): {LONG_A}")
            self.assertEqual(len(entry["threads"]), 1)
            text = entry["threads"][0]["text"]
            self.assertIn(f"Fixture Alice: {LONG_A}", text)
            self.assertIn(f"Me: {LONG_B}", text)
        # the card's lines are what the answer read, one for one
        self.assertEqual(len(_conversation_lines(self.alice)),
                         sum(1 for d in report["days"] if d["state"] == "on_card"))

    def test_a_number_and_a_card_id_find_the_same_person(self):
        by_name = self.show("Fixture Alice")
        for words in ("(555) 555-0142", ALICE_PHONE, ALICE.pid, "alice"):
            report = self.show(words)
            self.assertEqual(report["match"]["status"], "one", words)
            self.assertEqual([d["day"] for d in report["days"]],
                             [d["day"] for d in by_name["days"]], words)

    def test_a_held_number_still_shows_its_days_marked_not_on_a_card(self):
        report = self.show("Fixture Frank")
        self.assertEqual(report["match"]["who"]["card"], None)
        self.assertEqual([(d["day"], d["state"]) for d in report["days"]],
                         [("2026-03-02", "held")])
        self.assertIn(f"Fixture Frank: {LONG_A}", report["days"][0]["threads"][0]["text"])

    def test_the_rendered_answer_reads_as_plain_words(self):
        text = imessage.show_render(self.show("Fixture Alice"))
        self.assertIn("Texts with Fixture Alice (their card: people/fixture-alice.md).", text)
        self.assertIn("Newest first: 4 of 4 day(s) of texts.", text)
        self.assertIn("2026-03-05, they reached out, not on the card yet, waiting for its "
                      "summary", text)
        self.assertIn("From _local/imessage/threads/2026/2026-03-04-fixture-alice-1.txt:", text)
        self.assertIn(f"Me: {LONG_B}", text)


class TestBounded(ShowCase):
    def test_limit_and_since_bound_it_and_the_cut_is_named(self):
        report = self.show("Fixture Alice", limit=2)
        self.assertEqual([d["day"] for d in report["days"]], ["2026-03-05", "2026-03-04"])
        self.assertEqual(report["total_days"], 4)
        self.assertEqual(len(report["cut"]), 1)
        self.assertIn("2 older day(s) of texts are not shown", report["cut"][0])
        self.assertIn("--limit 4", report["cut"][0])

        since = self.show("Fixture Alice", since="2026-03-04")
        self.assertEqual([d["day"] for d in since["days"]], ["2026-03-05", "2026-03-04"])
        self.assertEqual(since["cut"], [])

    def test_a_long_transcript_shows_its_end_and_says_so(self):
        with mock.patch.object(imshow, "MAX_THREAD_LINES", 1):
            report = self.show("Fixture Alice", limit=1)
        thread = report["days"][0]["threads"][0]
        self.assertEqual(thread["lines_cut"], 1)
        self.assertIn(f"Me: {LONG_B}", thread["text"])        # the END of the conversation
        self.assertNotIn(LONG_A, thread["text"])
        self.assertTrue(any("only their last 1 lines" in c for c in report["cut"]))

    def test_the_output_cap_lists_the_rest_without_their_transcripts(self):
        with mock.patch.object(imshow, "MAX_TOTAL_CHARS", 250):
            report = self.show("Fixture Alice")
        read = [t for d in report["days"] for t in d["threads"] if t["text"]]
        capped = [t for d in report["days"] for t in d["threads"] if t["capped"]]
        self.assertEqual(len(read), 1)
        self.assertEqual(len(capped), 3)
        self.assertTrue(any("left out to keep this answer short" in c for c in report["cut"]))

    def test_a_bad_since_is_refused_plainly(self):
        report = self.show("Fixture Alice", since="last week")
        self.assertIn("YYYY-MM-DD", report["error"])
        self.assertEqual(report["days"], [])


class TestNeverAGuess(ShowCase):
    def test_a_name_that_fits_two_people_lists_both_and_reads_nothing(self):
        self.contacts[FRANK_TWIN] = "Fixture Frank Other"
        report = self.show("Frank")
        self.assertEqual(report["match"]["status"], "ambiguous")
        names = sorted(c["name"] for c in report["match"]["candidates"])
        self.assertEqual(names, ["Fixture Frank", "Fixture Frank Other"])
        self.assertEqual(report["days"], [])
        text = imessage.show_render(report)
        self.assertIn("I never guess which", text)
        self.assertIn("…0160", text)
        self.assertNotIn(FRANK, text, "a candidate list printed a whole number")

    def test_the_best_tier_wins_so_a_full_name_is_not_ambiguous(self):
        self.contacts[FRANK_TWIN] = "Fixture Frank Other"
        report = self.show("Fixture Frank")
        self.assertEqual(report["match"]["status"], "one")
        self.assertEqual(report["match"]["who"]["name"], "Fixture Frank")

    def test_a_name_that_fits_nobody_says_why(self):
        report = self.show("Nobody Atall")
        self.assertEqual(report["match"]["status"], "none")
        self.assertIn("card id", report["match"]["why"])
        self.assertIn("I found nobody", imessage.show_render(report))


class TestToday(ShowCase):
    def test_today_reads_live_for_that_person_only_and_stores_nothing(self):
        self.chats.say(1, at(D5, 8), "Are we still on for this afternoon at the office?",
                       sender=ALICE_PHONE)
        self.chats.say(2, at(D5, 9), CAROL_WORDS, sender=CAROL_PHONE)
        before = self.places()
        report = self.show("Fixture Alice", today=True, now=at(D5, 12),
                           source=imrun.ChatDbSource(self.chats.path))
        self.assertEqual(_changed(before, self.places()), [], "reading today wrote something")
        today = report["today"]
        self.assertTrue(today["read"])
        self.assertEqual(len(today["conversations"]), 1)
        self.assertIn("Fixture Alice: Are we still on for this afternoon",
                      today["conversations"][0]["text"])
        self.assertNotIn(CAROL_WORDS, str(report))
        self.assertIn("read live; nothing was stored", imessage.show_render(report))

    def test_today_without_access_says_so_and_reads_nothing_else(self):
        class Blocked(imrun.ListSource):
            def access(self):
                return False, "texts: no Full Disk Access for the app running me."

        report = self.show("Fixture Alice", today=True, source=Blocked())
        self.assertEqual(report["today"]["error"],
                         "texts: no Full Disk Access for the app running me.")
        self.assertEqual(len(report["days"]), 4)

    def test_a_never_ingest_conversation_is_not_shown_today(self):
        self.chats.say(1, at(D5, 8), "Are we still on for this afternoon at the office?",
                       sender=ALICE_PHONE)
        cfg = {**self.cfg, "never_ingest": [ALICE_PHONE]}
        report = self.show("Fixture Alice", today=True, now=at(D5, 12), cfg=cfg,
                           source=imrun.ChatDbSource(self.chats.path))
        self.assertEqual(report["today"]["conversations"], [])


class TestWhoTextedThatDay(ShowCase):
    def day(self, day, **kw):
        with contextlib.redirect_stderr(self.stderr):
            return imshow.show_day(day, home=self.home, db_path=self.fx.db_path,
                                   contacts=self.contacts, **kw)

    def test_it_lists_everyone_that_day_with_where_each_stands_and_writes_nothing(self):
        before = self.places()
        report = self.day("2026-03-02")
        self.assertEqual(_changed(before, self.places()), [])
        self.assertEqual([(p["name"], p["state"]) for p in report["people"]],
                         [("Fixture Alice", "on_card"), ("Fixture Frank", "held")])
        self.assertEqual(report["people"][0]["summary"], f"Texts (2): {LONG_A}")
        self.assertIsNone(report["people"][1]["summary"])
        self.assertEqual(report["people"][1]["number"], "…0160")
        self.assertIsNotNone(report["as_of"])
        text = imessage.show_day_render(report)
        self.assertIn("Fixture Alice: they reached out, on their card", text)
        self.assertIn("Fixture Frank: they reached out, their number is on your texts review "
                      "list", text)
        self.assertNotIn(FRANK, text)

    def test_a_waiting_day_and_an_empty_day(self):
        waiting = self.day("2026-03-05")
        self.assertEqual([(p["name"], p["state"]) for p in waiting["people"]],
                         [("Fixture Alice", "waiting")])
        empty = self.day("2026-03-06")
        self.assertEqual(empty["people"], [])
        self.assertIn("Nobody", imessage.show_day_render(empty))
        self.assertIn("YYYY-MM-DD", self.day("yesterday")["error"])


class TestPieces(unittest.TestCase):
    def test_card_lines_reads_the_projectors_shape(self):
        import tempfile  # noqa: PLC0415 - one test's scratch file

        with tempfile.TemporaryDirectory() as tmp:
            card = Path(tmp) / "x.md"
            card.write_text(
                "---\nid: prs_aaaaaaa2\n---\n\n## Interactions\n"
                "- 2026-08-30 — email (they_reached_out): Not a text\n"
                "- 2026-09-01 — conversation (mutual): Planned the trip "
                "(→ _local/imessage/threads/2026/2026-09-01-x-4.txt)\n"
                "- 2026-09-02 — conversation (i_reached_out)\n"
                "\n## Notes\n- 2026-09-03 — conversation (mutual): a note, not a line\n",
                encoding="utf-8")
            lines = imshow.card_lines(card)
        self.assertEqual([(x["day"], x["direction"], x["summary"], x["links"]) for x in lines], [
            ("2026-09-01", "mutual", "Planned the trip",
             ["_local/imessage/threads/2026/2026-09-01-x-4.txt"]),
            ("2026-09-02", "i_reached_out", None, []),
        ])

    def test_card_facts_reads_every_identifier_shape(self):
        import tempfile  # noqa: PLC0415 - one test's scratch file

        with tempfile.TemporaryDirectory() as tmp:
            card = Path(tmp) / "x.md"
            card.write_text(
                "---\nid: prs_aaaaaaa2\nname: \"Test Person\"\nphones:\n"
                '  - { value: "+15555550142", type: mobile, primary: true, active: true }\n'
                "  - value: '(555) 555-0143'\n    type: home\n"
                "  - +15555550144\n"
                "emails:\n  - { value: t@example.com, type: home }\n"
                "handles:\n  - { value: \"+15555550199\", platform: other }\n"
                "---\n", encoding="utf-8")
            name, found = imwho.card_facts(card)
        self.assertEqual(name, "Test Person")
        self.assertEqual(found, ["+15555550142", "+15555550143", "+15555550144",
                                 "t@example.com"])

    def test_tiers(self):
        q = imwho.words
        self.assertEqual(imwho.tier(q("castell nina"), "Nina Castell"), imwho.EXACT)
        self.assertEqual(imwho.tier(q("Nina"), "Nina Castell"), imwho.WORDS)
        self.assertEqual(imwho.tier(q("Nin"), "Nina Castell"), imwho.PREFIX)
        self.assertEqual(imwho.tier(q("N"), "Nina Castell"), 0)
        self.assertEqual(imwho.tier(q("Bob"), "Nina Castell"), 0)

    def test_the_parser_takes_the_verb(self):
        args = imessage._build_parser().parse_args(
            ["show", "--person", "Fixture Alice", "--since", "2026-03-01", "--limit", "3",
             "--today"])
        self.assertEqual((args.person, args.since, args.limit, args.today),
                         ("Fixture Alice", "2026-03-01", 3, True))
        by_day = imessage._build_parser().parse_args(["show", "--day", "2026-03-01"])
        self.assertEqual((by_day.day, by_day.person), ("2026-03-01", None))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            imessage._build_parser().parse_args(["show"])



if __name__ == "__main__":
    unittest.main()
