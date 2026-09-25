"""imreview — the bulk review, end to end, on a temp vault, a temp DB and a temp home.

Every act here goes through the REAL engine: the resolver raises each card, and the
member's own ``people.py proposals accept|dismiss`` doors (``people.cmd_accept`` /
``people.cmd_proposal_set``) create the person, attach the number, or record the no,
each on its own connection, inside :mod:`imfixture`'s temp tree.  The held numbers are
made the way the member's are: texts in a small Messages-shaped database, read by the
real daily run (``test_imrun``'s harness).  Nothing here reads the member's chat.db,
Contacts, cards or memory.db, and one test proves a full cycle leaves the member's
real places exactly as they were.

What each group holds still
---------------------------
* **The list** — every section in its place, each ranked by days held; numbers shown as
  their last four digits unless asked; the check section for a suffix-only match.
* **Rows acted on are the rows read** — a stale code, or none, is refused and writes
  nothing; a preview writes nothing (every byte of the home, the vault and the DB).
* **(b)** — the number goes onto the card through the engine's accept door, its held
  days go to the summary queue, and a later daily run files the number straight away.
* **(c) → (a)** — the full E1 two-yes road: the first yes makes the card WITHOUT the
  number and leaves nothing on the people queue, a daily run in between keeps it
  waiting, it comes back first on the list, the second yes attaches it and the days go.
* **dismiss** — the no is on the engine's change log, keyed on the number, and later
  daily runs keep it dismissed under a drifted Contacts name.
* **never** — a not-a-number row is never accepted, an ambiguous row never accepted;
  the people queue gains no pending card from any act; every card an act writes lints.

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
import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sqlite3  # noqa: E402
import stat  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from datetime import date, timedelta  # noqa: E402
from unittest import mock  # noqa: E402

import imessage  # noqa: E402
import imledger  # noqa: E402
import imreview  # noqa: E402
import imspine  # noqa: E402
from imfixture import (  # noqa: E402
    ALICE,
    DAN,
    DEE,
    ERIN,
    SHARED_LANDLINE,
    patched,
    real_fixture_traces,
    real_state,
)
from test_imrun import (  # noqa: E402
    ALICE_PHONE,
    D1,
    D2,
    D3,
    D4,
    D5,
    D6,
    ERIN_NEW,
    FRANK,
    RunCase,
    _changed,
    _tree_bytes,
    key,
    morning,
)

# ---------------------------------------------------------------------------
# The cast, beyond test_imrun's.  Invented, every one.
# ---------------------------------------------------------------------------

GAIL = "+15555550166"          # named in Contacts, no card: (c)
BARE = "+15555550164"          # no name anywhere: (d)
BARE2 = "+15555550165"         # no name anywhere, more days: (d), ranked first
BIZ = "1234567890123456"       # a 16-digit business ID, not a phone number: (e)
SUFFIX = "+5555550142"         # reaches Alice's +15555550142 by its last ten digits only
D7 = date(2026, 3, 8)
D8 = date(2026, 3, 9)

#: (chat, number, the days it texts on).  Each its own one-to-one conversation.
SEED = [
    (1, ALICE_PHONE, [D2]),
    (10, ERIN_NEW, [D1, D2]),
    (11, FRANK, [D1, D2, D3]),
    (12, GAIL, [D3]),
    (13, BARE, [D2, D4]),
    (14, BARE2, [D1, D2, D3, D4]),
    (15, BIZ, [D5]),
    (16, SHARED_LANDLINE, [D1, D5]),
    (17, SUFFIX, [D4]),
]

#: The ranked list the seed makes: (row, section, number).
EXPECTED = [
    ("1", "known", ERIN_NEW),
    ("2", "new", FRANK),
    ("3", "new", GAIL),
    ("4", "bare", BARE2),
    ("5", "bare", BARE),
    ("6", "not_a_number", BIZ),
    ("7", "ambiguous", SHARED_LANDLINE),
]

ATTACH_LABEL = "was added; say yes again to attach their number so their texts keep landing."


class ReviewCase(RunCase):
    """``test_imrun``'s harness (temp vault + DB, temp home, temp chat.db), plus review."""

    def setUp(self):
        super().setUp()
        self.contacts[GAIL] = "Fixture Gail"
        self.now = morning(D6)

    def seed(self):
        """Every exchange in day order (ROWIDs arrive in date order), then two runs: the
        first sets the watermark, the second reads D1-D5 and holds every new number."""
        for chat, _who, _days in SEED:
            with contextlib.suppress(sqlite3.IntegrityError):
                self.chats.chat(chat)
        for day in (D1, D2, D3, D4, D5):
            for chat, who, days in SEED:
                if day in days:
                    self.chats.exchange(chat, day, who)
        self.daily(morning(D1))
        report = self.daily(morning(D6))
        self.assertIsNone(report["paused"])
        return report

    def review(self, **kw):
        kw.setdefault("now", self.now)
        with contextlib.redirect_stderr(self.stderr):
            return imreview.review(home=self.home, db_path=self.fx.db_path, cfg=self.cfg,
                                   contacts=self.contacts, **kw)

    def act(self, **kw):
        """Read the list, then act on it with its code, as the member does."""
        code = self.review()["listing"]["code"]
        report = self.review(listing=code, confirm=True, **kw)
        self.assertIsNone(report["paused"], report["paused"])
        self.assertIsNone(report["refused"], report["refused"])
        self.assertTrue(report["confirmed"])
        return report

    @staticmethod
    def rows(report):
        return [(r["label"], r["section"], r["number"]) for r in report["listing"]["rows"]]

    @staticmethod
    def row(report, number):
        for item in [*report["listing"]["rows"], *report["listing"]["checks"]]:
            if item["number"] == number:
                return item
        return None

    def pending(self):
        """Every PENDING proposal on the people queue, on a fresh connection."""
        return {r[0] for r in self.fx.reopen().execute(
            "SELECT id FROM person_proposal WHERE status = 'pending'").fetchall()}

    def change_log(self, proposal_id):
        return [r[0] for r in self.fx.reopen().execute(
            "SELECT action FROM change_log WHERE entity_type = 'person_proposal' AND "
            "entity_id = ? ORDER BY id", (proposal_id,)).fetchall()]

    def identifier(self, number):
        with self.ledger() as led:
            return led.identifier(number)

    def places(self):
        return _tree_bytes(self.home, self.fx.root)

    def assert_no_new_pending(self, before):
        self.assertEqual(self.pending() - before, set(),
                         "an act left a new card waiting on the people queue")

    def assert_card_lints(self, person_id):
        card = imspine.person_path(self.fx.reopen(), person_id)
        self.assertIsNotNone(card, "no card for that person")
        self.assertEqual(imspine.lint_card(card.rel), [], f"{card.rel} does not lint clean")
        return card


# ---------------------------------------------------------------------------
# 1. The list.
# ---------------------------------------------------------------------------


class TestTheList(ReviewCase):
    def test_every_section_in_its_place_each_ranked_by_days_held(self):
        self.seed()
        report = self.review()
        self.assertEqual(report["mode"], "list")
        self.assertEqual(self.rows(report), EXPECTED)
        days = {r["number"]: r["days"] for r in report["listing"]["rows"]}
        self.assertEqual(days, {ERIN_NEW: 2, FRANK: 3, GAIL: 1, BARE2: 4, BARE: 2, BIZ: 1,
                                SHARED_LANDLINE: 2})
        known = self.row(report, ERIN_NEW)
        self.assertEqual((known["card"], known["card_id"], known["name"]),
                         ("Fixture Erin", ERIN.pid, "Fixture Erin"))
        ambiguous = self.row(report, SHARED_LANDLINE)
        self.assertEqual(sorted(c["card"] for c in ambiguous["candidates"]),
                         ["Fixture Dan", "Fixture Dee"])
        self.assertEqual(sorted(c["card_id"] for c in ambiguous["candidates"]),
                         sorted([DAN.pid, DEE.pid]))
        self.assertFalse(ambiguous["can_accept"])
        self.assertFalse(self.row(report, BIZ)["can_accept"])
        self.assertTrue(self.row(report, BARE)["needs_name"])
        # Alice's own number matches exactly: filed, never listed
        self.assertIsNone(self.row(report, ALICE_PHONE))

        # The check section: the suffix-only number, and the card it went to.
        checks = report["listing"]["checks"]
        self.assertEqual([(c["label"], c["number"], c["card"], c["card_id"]) for c in checks],
                         [("m1", SUFFIX, "Fixture Alice", ALICE.pid)])
        self.assertEqual((checks[0]["matched_digits"], checks[0]["days_queued"]), (10, 1))

    def test_the_plain_list_shows_names_and_last_four_never_whole_numbers(self):
        self.seed()
        report = self.review()
        text = imreview.render(report)
        for number in (ERIN_NEW, FRANK, GAIL, BARE, BARE2, BIZ, SHARED_LANDLINE, SUFFIX):
            self.assertNotIn(number, text, "a whole number in the plain list")
        for part in ("Fixture Frank (…0160): a new person. 3 days held.",
                     "Fixture Erin (…0161) matches your card for Fixture Erin",
                     "A number with no name (…0165)",
                     "…3456 can't be added by number",
                     "Fixture Dan (…0150) is on more than one card",
                     "m1. …0142 went to Fixture Alice's card, but only its last 10 digits",
                     f"--listing {report['listing']['code']}"):
            self.assertIn(part, text)
        order = [text.index(imreview.TITLES[s]) for s in imreview.ORDER
                 if imreview.TITLES[s] in text]
        self.assertEqual(order, sorted(order), "the sections are out of order")
        shown = imreview.render(report, show_numbers=True)
        self.assertIn(FRANK, shown)
        # --json follows the same rule
        public = json.dumps(imreview.public(report))
        for number in (FRANK, BIZ, SUFFIX):
            self.assertNotIn(number, public)
        self.assertIn(FRANK, json.dumps(imreview.public(report, show_numbers=True)))

    def test_nothing_to_review_yet_writes_nothing_and_makes_no_lock(self):
        before = self.places()
        report = self.review()
        self.assertIn("no texts have been read into the ledger yet", report["nothing"])
        self.assertIn("Nothing to review yet", imreview.render(report))
        self.assertEqual(_changed(before, self.places()), [])
        self.assertFalse((self.home / "ledger.lock").exists())

    def test_the_cli_lists_and_previews_read_only(self):
        self.seed()
        real = imreview.review

        def routed(**kw):  # the CLI's words, pointed at the temp places
            return real(home=self.home, db_path=self.fx.db_path, cfg=self.cfg,
                        contacts=self.contacts, now=self.now, **kw)

        def cli(*argv):
            out = io.StringIO()
            with mock.patch.object(imreview, "review", routed), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(self.stderr):
                code = imessage.main(["review", *argv])
            return code, out.getvalue()

        before = self.places()
        code, text = cli()
        self.assertEqual(code, 0)
        self.assertIn("Your texts review list", text)
        self.assertNotIn(FRANK, text)
        code, text = cli("--accept", "6")
        self.assertEqual(code, 1, "a refused preview is not a success")
        self.assertIn("can't be accepted", text)
        code, text = cli("--dismiss-below", "5", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(text)
        self.assertEqual([s["label"] for s in payload["plan"]], ["6", "7"])
        self.assertNotIn(FRANK, text)
        self.assertEqual(_changed(before, self.places()), [], "the CLI list or preview wrote")


# ---------------------------------------------------------------------------
# 2. The rows acted on are exactly the rows read.
# ---------------------------------------------------------------------------


class TestExactRows(ReviewCase):
    def test_a_stale_listing_is_refused_and_writes_nothing(self):
        self.seed()
        old = self.review()["listing"]["code"]
        # a new text arrives and the morning run holds it: the list the member read is gone
        self.chats.exchange(13, D6, BARE)
        self.daily(morning(D7))
        pending = self.pending()
        before = self.places()
        report = self.review(accept="1", listing=old, confirm=True)
        self.assertEqual(report["refused"], imreview.SENTENCE_STALE)
        self.assertEqual(report["results"], [])
        self.assertEqual(_changed(before, self.places()), [], "a stale act wrote something")
        self.assertEqual(self.pending(), pending)
        self.assertEqual(imreview.render(report), imreview.SENTENCE_STALE)

    def test_a_contacts_rename_also_makes_the_list_stale(self):
        self.seed()
        old = self.review()["listing"]["code"]
        self.contacts[FRANK] = "Frank Renamed"
        self.assertNotEqual(self.review()["listing"]["code"], old)
        self.assertEqual(self.review(dismiss="2", listing=old)["refused"],
                         imreview.SENTENCE_STALE)

    def test_an_act_without_the_code_is_refused(self):
        self.seed()
        before = self.places()
        report = self.review(accept="1", confirm=True)
        self.assertEqual(report["refused"], imreview.SENTENCE_NO_TOKEN)
        self.assertEqual(_changed(before, self.places()), [])

    def test_the_preview_writes_nothing_and_the_list_is_unchanged(self):
        self.seed()
        listing = self.review()
        pending = self.pending()
        before = self.places()
        preview = self.review(accept="1-4", dismiss_below=5, names={"4": "Fixture Hal"})
        self.assertEqual(preview["mode"], "preview")
        self.assertIsNone(preview["refused"])
        self.assertEqual([(s["label"], s["action"]) for s in preview["plan"]],
                         [("1", "accept"), ("2", "accept"), ("3", "accept"), ("4", "accept"),
                          ("6", "dismiss"), ("7", "dismiss")])
        self.assertEqual(_changed(before, self.places()), [], "the preview wrote something")
        self.assertEqual(self.pending(), pending)
        self.assertEqual(self.review()["listing"], listing["listing"])
        words = imreview.render(preview)
        self.assertIn("nothing has been written", words)
        self.assertIn("they come back at the top of this list for your second yes", words)
        self.assertIn("dismiss; it leaves this list", words)

    def test_bad_selections_are_one_sentence_and_nothing_written(self):
        self.seed()
        before = self.places()
        cases = {
            "accept=99": ({"accept": "99"}, "There is no row 99"),
            "accept=m2": ({"accept": "m2"}, "There is no row m2"),
            "overlap": ({"accept": "2", "dismiss": "1-3"}, "in both --accept and --dismiss"),
            "junk": ({"accept": "1-x"}, "I could not read"),
            "bare, no name": ({"accept": "4"}, 'add --name 4="Their Name"'),
            "name, not accepted": ({"names": {"4": "Hal"}}, "not in --accept"),
            "name on a known row": ({"accept": "1", "names": {"1": "Hal"}},
                                    "--name does not apply"),
            "below the end": ({"dismiss_below": 7}, "nothing below row 7"),
            "confirm, nothing named": ({"confirm": True}, "none were named"),
        }
        for label, (kw, words) in cases.items():
            with self.subTest(label):
                report = self.review(**kw)
                self.assertIn(words, report["refused"] or "")
                self.assertEqual(report["results"], [])
        self.assertEqual(_changed(before, self.places()), [])


# ---------------------------------------------------------------------------
# 3. (b): a number that matches someone you already have.
# ---------------------------------------------------------------------------


class TestKnown(ReviewCase):
    def test_accept_attaches_queues_the_held_days_and_the_next_run_files_it(self):
        self.seed()
        pending = self.pending()
        report = self.act(accept="1")
        self.assertEqual([(r["label"], r["outcome"]) for r in report["results"]], [("1", "done")])
        self.assertEqual(report["left_on_people_queue"], [])
        self.assert_no_new_pending(pending)
        self.assertIn("Number attached to Fixture Erin's card. 2 days went to the summary "
                      "queue.", report["results"][0]["words"])

        props = self.proposals(ERIN_NEW)
        self.assertEqual([(k, s) for _, k, s, _ in props], [("add_identifier", "accepted")])
        self.assertEqual(self.change_log(props[0][0])[-1], "accepted")
        card = self.assert_card_lints(ERIN.pid)
        self.assertIn(ERIN_NEW, card.path.read_text(encoding="utf-8"))
        res = imspine.resolve(self.fx.reopen(), ERIN_NEW, None, emit=False)
        self.assertEqual((res.status, res.person_id), ("resolved", ERIN.pid))

        record = self.identifier(ERIN_NEW)
        self.assertEqual((record["state"], record["person_id"], record["match"]),
                         ("accepted", ERIN.pid, "exact"))
        with self.ledger() as led:
            self.assertEqual(led.held(ERIN_NEW), [])
            queue = led.queued()
        for day in (D1, D2):
            self.assertEqual(queue[key(ERIN_NEW, day)]["person_id"], ERIN.pid)
            self.assertEqual(queue[key(ERIN_NEW, day)]["queued_at"], self.now.isoformat())
        self.assertIsNone(self.row(self.review(), ERIN_NEW), "still listed after its yes")

        # the next morning: her new texts go straight to the queue, never held
        self.chats.exchange(10, D6, ERIN_NEW)
        daily = self.daily(morning(D7))
        self.assertEqual(daily["held"].get("new", 0), 0)
        with self.ledger() as led:
            self.assertEqual(led.queued()[key(ERIN_NEW, D6)]["person_id"], ERIN.pid)
            self.assertEqual(led.held(ERIN_NEW), [])
        self.assert_no_new_pending(pending)


# ---------------------------------------------------------------------------
# 4. (c) → (a): the E1 two-yes road, through the REAL engine accept door.
# ---------------------------------------------------------------------------


class TestTwoYeses(ReviewCase):
    def test_new_then_attach_pending_then_second_yes_then_attached_then_days_queue(self):
        self.seed()
        pending = self.pending()

        # The first yes: the card is made, WITHOUT the number (E1), and nothing waits on
        # the people queue.
        first = self.act(accept="2")
        self.assertEqual(first["results"][0]["outcome"], "done")
        self.assertIn("Added Fixture Frank as a new person", first["results"][0]["words"])
        self.assert_no_new_pending(pending)
        self.assertEqual(first["left_on_people_queue"], [])
        stubs = self.proposals(FRANK)
        self.assertEqual([(k, s) for _, k, s, _ in stubs], [("new_stub", "accepted")])
        record = self.identifier(FRANK)
        new_person = record["person_id"]
        self.assertEqual((record["state"], record["proposal_id"], record["stub_proposal_id"]),
                         ("attach_pending", None, stubs[0][0]))
        self.assertEqual(imspine.accepted_person(self.fx.reopen(), stubs[0][0]), new_person)
        card = self.assert_card_lints(new_person)
        self.assertNotIn(FRANK, card.path.read_text(encoding="utf-8"), "E1 did not hold")
        self.assertIn("name: Fixture Frank", card.path.read_text(encoding="utf-8"))
        self.assertEqual(imspine.resolve(self.fx.reopen(), FRANK, None, emit=False).status,
                         "proposed")

        # A morning run in between keeps it waiting for the second yes: not "new" again,
        # nothing raised, and the new day is held with the rest.
        self.chats.exchange(11, D6, FRANK)
        daily = self.daily(morning(D7))
        self.assertEqual(daily["raised"], {})
        self.assertEqual(self.identifier(FRANK)["state"], "attach_pending")
        self.assert_no_new_pending(pending)
        with self.ledger() as led:
            self.assertEqual(len(led.held(FRANK)), 4)
        self.assertEqual((daily["awaiting_review"], daily["awaiting_yes"]), (7, 0))

        # It comes back FIRST on the list, labelled plainly.
        self.now = morning(D7)
        listing = self.review()
        top = listing["listing"]["rows"][0]
        self.assertEqual((top["label"], top["section"], top["number"], top["card_id"]),
                         ("1", "attach", FRANK, new_person))
        self.assertIn(f"1. Fixture Frank {ATTACH_LABEL}", imreview.render(listing))

        # The second yes: attached, resolved, every held day queued for the new card.
        second = self.act(accept="1")
        self.assertEqual(second["results"][0]["outcome"], "done")
        self.assertIn("4 days went to the summary queue", second["results"][0]["words"])
        self.assert_no_new_pending(pending)
        self.assertEqual([(k, s) for _, k, s, _ in self.proposals(FRANK)],
                         [("new_stub", "accepted"), ("add_identifier", "accepted")])
        res = imspine.resolve(self.fx.reopen(), FRANK, None, emit=False)
        self.assertEqual((res.status, res.person_id), ("resolved", new_person))
        self.assertIn(FRANK, self.assert_card_lints(new_person).path.read_text(encoding="utf-8"))
        record = self.identifier(FRANK)
        self.assertEqual((record["state"], record["person_id"]), ("accepted", new_person))
        with self.ledger() as led:
            self.assertEqual(led.held(FRANK), [])
            queue = led.queued()
        for day in (D1, D2, D3, D6):
            self.assertEqual(queue[key(FRANK, day)]["person_id"], new_person)
        self.assertIsNone(self.row(self.review(), FRANK))

    def test_a_bare_number_is_added_under_the_name_given(self):
        self.seed()
        pending = self.pending()
        report = self.act(accept="4", names=["4=Fixture Hal"])
        self.assertEqual(report["results"][0]["outcome"], "done")
        self.assert_no_new_pending(pending)
        record = self.identifier(BARE2)
        self.assertEqual(record["state"], "attach_pending")
        card = self.assert_card_lints(record["person_id"])
        self.assertIn("name: Fixture Hal", card.path.read_text(encoding="utf-8"))

    def test_a_name_that_is_already_a_card_attaches_there_instead_of_a_second_card(self):
        self.seed()
        pending = self.pending()
        preview = self.review(accept="5", names={"5": "Fixture Carol"})
        self.assertIn("you already have a card for Fixture Carol", preview["plan"][0]["words"])
        report = self.act(accept="5", names={"5": "Fixture Carol"})
        self.assertEqual(report["results"][0]["outcome"], "done")
        self.assert_no_new_pending(pending)
        carol = self.cards["fixture-carol"].pid
        self.assertEqual(self.identifier(BARE)["person_id"], carol)
        self.assertEqual(self.fx.reopen().execute(
            "SELECT COUNT(*) FROM person WHERE name = 'Fixture Carol'").fetchone()[0], 1)

    def test_an_address_needs_one_yes_the_engine_attaches_it_at_once(self):
        address = "new.face@fixture.example.com"
        self.contacts[address] = "Fixture Ivy"
        self.chats.chat(30)
        self.chats.exchange(30, D1, address)
        self.chats.exchange(30, D2, address)
        self.daily(morning(D1))
        self.daily(morning(D3))
        pending = self.pending()
        self.now = morning(D3)
        listing = self.review()
        label = self.row(listing, address)["label"]
        report = self.act(accept=label)
        self.assertIn("Added Fixture Ivy as a new person", report["results"][0]["words"])
        self.assertIn("2 days went to the summary queue", report["results"][0]["words"])
        self.assert_no_new_pending(pending)
        record = self.identifier(address)
        self.assertEqual(record["state"], "accepted")
        with self.ledger() as led:
            self.assertEqual(len([k for k in led.queued() if k.startswith(address)]), 2)


# ---------------------------------------------------------------------------
# 5. Dismiss.
# ---------------------------------------------------------------------------


class TestDismiss(ReviewCase):
    def test_the_no_is_on_the_change_log_and_stays_under_a_drifted_name(self):
        self.seed()
        pending = self.pending()
        report = self.act(dismiss="2")
        self.assertIn("Dismissed (recorded on your people records too)",
                      report["results"][0]["words"])
        self.assert_no_new_pending(pending)
        props = self.proposals(FRANK)
        self.assertEqual([(k, s) for _, k, s, _ in props], [("new_stub", "dismissed")])
        self.assertEqual(self.change_log(props[0][0]), ["dismissed"])
        self.assertEqual(imspine.dismissed_identifier(self.fx.reopen(), FRANK), props[0][0])
        record = self.identifier(FRANK)
        self.assertEqual((record["state"], record["proposal_id"]), ("dismissed", props[0][0]))

        # Later mornings, under a drifted Contacts name: still dismissed, nothing raised.
        self.contacts[FRANK] = "Frank Drifted"
        for day in (D6, D7):
            self.chats.exchange(11, day, FRANK)
            daily = self.daily(morning(day + timedelta(days=1)))
            self.assertEqual(daily["raised"], {})
        self.assertEqual(self.identifier(FRANK)["state"], "dismissed")
        self.assertEqual([(k, s) for _, k, s, _ in self.proposals(FRANK)],
                         [("new_stub", "dismissed")])
        self.assert_no_new_pending(pending)
        with self.ledger() as led:
            self.assertEqual(len(led.held(FRANK)), 5, "a dismissed number's days are kept held")
        self.now = morning(D8)
        again = self.review()
        self.assertIsNone(self.row(again, FRANK))
        self.assertEqual(again["listing"]["not_listed"].get("dismissed"), 1)

    def test_a_known_row_dismissed_records_the_no_against_the_number(self):
        self.seed()
        pending = self.pending()
        self.act(dismiss="1")
        self.assert_no_new_pending(pending)
        props = self.proposals(ERIN_NEW)
        self.assertEqual([(k, s) for _, k, s, _ in props], [("add_identifier", "dismissed")])
        self.assertEqual(self.change_log(props[0][0]), ["dismissed"])


# ---------------------------------------------------------------------------
# 6. What review never does.
# ---------------------------------------------------------------------------


class TestNever(ReviewCase):
    def test_a_not_a_number_row_can_only_be_dismissed(self):
        self.seed()
        rows_before = len(self.proposals())
        before = self.places()
        refused = self.review(accept="6", listing=self.review()["listing"]["code"],
                              confirm=True)
        self.assertIn("Row 6 can't be accepted", refused["refused"])
        self.assertEqual(_changed(before, self.places()), [])

        report = self.act(dismiss="6")
        self.assertEqual(report["results"][0]["outcome"], "done")
        self.assertEqual(len(self.proposals()), rows_before, "an engine card for a business ID")
        record = self.identifier(BIZ)
        self.assertEqual((record["state"], record["proposal_id"]), ("dismissed", None))
        self.chats.exchange(15, D6, BIZ)
        self.daily(morning(D7))
        self.assertEqual(self.identifier(BIZ)["state"], "dismissed", "flipped back to new")
        self.now = morning(D7)
        self.assertIsNone(self.row(self.review(), BIZ))

    def test_an_ambiguous_row_is_never_accepted_and_a_tail_sweep_dismisses_it(self):
        self.seed()
        refused = self.review(accept="7", listing=self.review()["listing"]["code"],
                              confirm=True)
        self.assertIn("I never guess whose it is", refused["refused"])
        pending = self.pending()
        rows_before = len(self.proposals())
        report = self.act(dismiss_below=6)
        self.assertEqual([r["label"] for r in report["results"]], ["7"])
        self.assertEqual(len(self.proposals()), rows_before)
        self.assert_no_new_pending(pending)
        self.assertEqual(self.identifier(SHARED_LANDLINE)["state"], "dismissed")
        self.chats.exchange(16, D6, SHARED_LANDLINE)
        self.daily(morning(D7))
        self.assertEqual(self.identifier(SHARED_LANDLINE)["state"], "dismissed",
                         "a dismissed shared number went back to ambiguous")
        # the tail sweep never reached the match to check
        self.assertEqual(self.identifier(SUFFIX)["state"], "accepted")

    def test_a_batch_accept_and_a_tail_dismiss_in_one_act(self):
        self.seed()
        pending = self.pending()
        report = self.act(accept="1-3", dismiss_below=3)
        self.assertEqual([(r["label"], r["action"], r["outcome"]) for r in report["results"]],
                         [("1", "accept", "done"), ("2", "accept", "done"),
                          ("3", "accept", "done"), ("4", "dismiss", "done"),
                          ("5", "dismiss", "done"), ("6", "dismiss", "done"),
                          ("7", "dismiss", "done")])
        self.assert_no_new_pending(pending)
        self.assertEqual(report["left_on_people_queue"], [])
        for person_id in {self.identifier(n)["person_id"] for n in (ERIN_NEW, FRANK, GAIL)}:
            self.assert_card_lints(person_id)
        self.assertIn("Nothing new is waiting on your people queue", imreview.render(report))
        after = self.review()
        self.assertEqual([(r[1], r[2]) for r in self.rows(after)],
                         [("attach", FRANK), ("attach", GAIL)])

    def test_an_engine_refusal_is_said_by_row_never_silent(self):
        self.seed()
        import people  # noqa: PLC0415

        real = people.cmd_accept

        def refusing(proposal_id, *, decide_gate=None, replace=False):
            if proposal_id in self.pending():
                return {"status": "needs_action", "reason": "fixture refusal"}
            return real(proposal_id, decide_gate=decide_gate, replace=replace)

        with patched(people, "cmd_accept", refusing):
            report = self.act(accept="2")
        self.assertEqual(report["results"][0]["outcome"], "needs_you")
        self.assertIn("fixture refusal", report["results"][0]["words"])
        self.assertEqual(report["left_on_people_queue"], ["2"])
        self.assertIn("Waiting on your people queue now", imreview.render(report))
        self.assertEqual(self.identifier(FRANK)["state"], "pending")


# ---------------------------------------------------------------------------
# 7. Check these matches (E5b).
# ---------------------------------------------------------------------------


class TestCheckMatches(ReviewCase):
    def test_confirming_a_match_takes_it_off_the_list_and_changes_no_card(self):
        self.seed()
        before = _tree_bytes(self.fx.root)
        report = self.act(accept="m1")
        self.assertIn("Confirmed: it is Fixture Alice.", report["results"][0]["words"])
        self.assertEqual(_changed(before, _tree_bytes(self.fx.root)), [])
        self.assertEqual(self.identifier(SUFFIX)["match_confirmed"], ALICE.pid)
        self.assertEqual(self.review()["listing"]["checks"], [])

    def test_the_wrong_person_stops_filing_there(self):
        self.seed()
        report = self.act(dismiss="m1")
        self.assertIn("Not Fixture Alice: 1 waiting day came off that card's queue",
                      report["results"][0]["words"])
        record = self.identifier(SUFFIX)
        self.assertEqual((record["state"], record["hold_reason"], record["match_person"]),
                         ("dismissed", "wrong_match", ALICE.pid))
        with self.ledger() as led:
            self.assertNotIn(key(SUFFIX, D4), led.queued())
            self.assertEqual([u["key"] for u in led.held(SUFFIX)], [key(SUFFIX, D4)])
        # a new text from that number: held, never queued onto Alice
        self.chats.exchange(17, D6, SUFFIX)
        daily = self.daily(morning(D7))
        self.assertEqual(daily["held"].get("wrong_match"), 1)
        with self.ledger() as led:
            self.assertFalse([k for k in led.queued() if k.startswith(SUFFIX)])
            self.assertEqual(len(led.held(SUFFIX)), 2)
        self.now = morning(D7)
        again = self.review()
        self.assertEqual(again["listing"]["checks"], [])
        self.assertEqual(again["listing"]["not_listed"].get("wrong_match"), 1)


# ---------------------------------------------------------------------------
# 8. Gates: one sentence, nothing written.
# ---------------------------------------------------------------------------


class TestGates(ReviewCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.code = self.review()["listing"]["code"]  # read BEFORE the gate is set up

    def assert_paused_and_untouched(self, reason):
        before = self.places()
        report = self.review(accept="1", listing=self.code, confirm=True)
        self.assertEqual((report["paused"] or {}).get("reason"), reason, report)
        self.assertTrue(report["paused"]["sentence"].strip())
        self.assertEqual(report["results"], [])
        self.assertEqual(_changed(before, self.places()), [], f"{reason} wrote something")
        self.assertIn(report["paused"]["sentence"], imreview.render(report))
        return report

    def test_the_ledger_lock_is_busy(self):
        with imledger.default_lock(self.home), mock.patch.object(imreview, "LOCK_TIMEOUT_S", 0.2):
            report = self.assert_paused_and_untouched("busy")
        self.assertEqual(report["paused"]["sentence"], imreview.SENTENCE_BUSY)

    def test_engine_drift(self):
        import people  # noqa: PLC0415

        def cmd_accept(proposal_id):  # a door the plug-in was not built for
            raise AssertionError("never called")

        with patched(people, "cmd_accept", cmd_accept):
            report = self.assert_paused_and_untouched("engine_changed")
        self.assertTrue(report["paused"]["sentence"].startswith(imspine.REFUSAL_ENGINE_CHANGED))

    def test_a_damaged_ledger(self):
        (self.home / "ledger.json").write_text("{not json", encoding="utf-8")
        report = self.assert_paused_and_untouched("ledger_damaged")
        self.assertIn("damaged", report["paused"]["sentence"])

    def test_no_own_handles(self):
        self.cfg["own_handles"] = []
        report = self.assert_paused_and_untouched("no_own_handles")
        self.assertEqual(report["paused"]["sentence"], imconfig.REFUSAL_NO_OWN_HANDLES)

    def test_a_database_that_is_not_the_engines_is_refused(self):
        before = self.places()
        with contextlib.redirect_stderr(self.stderr):
            report = imreview.review(home=self.home, db_path=self.root / "other.db",
                                     cfg=self.cfg, contacts=self.contacts, now=self.now)
        self.assertEqual(report["paused"]["sentence"], imreview.SENTENCE_DB_MISMATCH)
        self.assertEqual(_changed(before, self.places()), [])
        self.assertFalse((self.root / "other.db").exists())


# ---------------------------------------------------------------------------
# 9. Nothing escapes.
# ---------------------------------------------------------------------------


#: The member's private files at the top of the plug-in home.  A live install holds
#: them (the real daily run writes the ledger and state; ``check --write-*`` writes the
#: local config), so a test may never ask that they be ABSENT, only that it left them
#: exactly as it found them.
PRIVATE_FILES = ("ledger.json", "ledger.wal", "ledger.lock", "state.json", "config.local.json")
#: The member's private folders, walked to the last file.
PRIVATE_FOLDERS = ("synth", "pre-backfill", "threads")
_FIELDS = ("kind", "size", "mtime_ns", "sha256")


def _sha256(path):
    """The digest of a file's bytes.  The bytes themselves are never kept or shown."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(path):
    """``(kind, size, mtime_ns, sha256)`` for one path, never its content.

    ``lstat``: a link is recorded as a link (by the digest of where it points), never
    followed out of the home.  A folder is recorded only as present: its mtime moves
    when anything inside comes or goes, and every thing inside is its own entry.
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        return ("absent", None, None, None)
    if stat.S_ISDIR(st.st_mode):
        return ("dir", None, None, None)
    if stat.S_ISLNK(st.st_mode):
        where = hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
        return ("link", st.st_size, st.st_mtime_ns, where)
    if not stat.S_ISREG(st.st_mode):
        return ("other", st.st_size, st.st_mtime_ns, None)
    try:
        digest = _sha256(path)
    except OSError as exc:  # one run's lock held on a platform that blocks reads
        digest = f"unreadable: {type(exc).__name__}"
    return ("file", st.st_size, st.st_mtime_ns, digest)


def private_home_snapshot(home):
    """Every private path in ``home``: ``{relative path: fingerprint}``.  Reads only.

    Each private file and folder is always a key (``absent`` when it is not there),
    and every path under a private folder is its own key, so a path that appears, a
    path that goes, and a byte that changes under a restored mtime all show.
    """
    home = Path(home)
    snapshot = {name: _fingerprint(home / name) for name in PRIVATE_FILES}
    for folder in PRIVATE_FOLDERS:
        top = home / folder
        snapshot[f"{folder}/"] = _fingerprint(top)
        if snapshot[f"{folder}/"][0] != "dir":
            continue
        for dirpath, dirnames, filenames in os.walk(top):  # followlinks=False
            for name in (*dirnames, *filenames):
                path = Path(dirpath) / name
                snapshot[path.relative_to(home).as_posix()] = _fingerprint(path)
    return snapshot


def private_home_changes(before, after):
    """What differs between two snapshots, in paths and counts only.

    A top-level private path is named, with which of its fields moved.  Inside a
    private folder only counts are given: every name in there is built from a real
    Contacts label or a real person's slug, so naming one would print the member's
    people.  No field VALUE is ever put in a message, and no byte of any file.
    """
    changes = []
    for name in (*PRIVATE_FILES, *(f"{folder}/" for folder in PRIVATE_FOLDERS)):
        was, now = before[name], after[name]
        if was == now:
            continue
        if was[0] == "absent":
            changes.append(f"{name} appeared")
        elif now[0] == "absent":
            changes.append(f"{name} vanished")
        else:
            moved = [f for f, a, b in zip(_FIELDS, was, now, strict=True) if a != b]
            changes.append(f"{name} changed ({', '.join(moved)})")
    for folder in PRIVATE_FOLDERS:
        prefix = f"{folder}/"
        inside_before = {k: v for k, v in before.items() if k.startswith(prefix) and k != prefix}
        inside_after = {k: v for k, v in after.items() if k.startswith(prefix) and k != prefix}
        appeared = len(inside_after.keys() - inside_before.keys())
        vanished = len(inside_before.keys() - inside_after.keys())
        changed = sum(1 for k in inside_before.keys() & inside_after.keys()
                      if inside_before[k] != inside_after[k])
        if appeared or vanished or changed:
            changes.append(f"{prefix}: {appeared} path(s) appeared, {vanished} vanished, "
                           f"{changed} changed")
    return changes


class TestNothingEscapes(unittest.TestCase):
    #: The home whose private paths a full cycle must leave unchanged.  The negative
    #: control below points a subclass at a temp home; this one is always the REAL one.
    private_home = imconfig.HOME

    def cycle(self):
        """One full review cycle, on its own temp vault, DB and home."""
        case = TestNever("test_a_batch_accept_and_a_tail_dismiss_in_one_act")
        result = unittest.TestResult()
        case.run(result)
        return result

    def test_a_full_cycle_leaves_the_real_places_as_they_were(self):
        before = real_state()
        home_before = private_home_snapshot(self.private_home)
        result = self.cycle()
        self.assertEqual((result.errors, result.failures), ([], []))
        self.assertEqual(real_state(), before, "the real people folder or undo ring changed")
        self.assertEqual(real_fixture_traces(), [])
        changes = private_home_changes(home_before, private_home_snapshot(self.private_home))
        self.assertEqual(changes, [], "a full cycle changed the real plug-in home: "
                         + "; ".join(changes))


class TestTheHomeCheckBites(unittest.TestCase):
    """NEGATIVE CONTROL for the home check above, on a temp home only.

    A stand-in for a live install (every private file, a thread, a claim, a backfill
    copy) is built in a temp folder, and each kind of change a stray write could make
    is made there.  No real file is read, touched or named.
    """

    #: Planted in every file: a failure message that carried a byte would carry this.
    SECRET = "fixture-secret-body-never-printed"
    #: A thread name built the way a real one is, from a Contacts label.
    THREAD = "threads/2026/2026-03-01 Fixture Secretname 1.txt"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        for rel in (*PRIVATE_FILES, self.THREAD, "synth/synth-20260301T060000-abcdef12.json",
                    "pre-backfill/fixture-secretname.md"):
            path = self.home / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{self.SECRET}\n", encoding="utf-8")

    def changes_after(self, act):
        before = private_home_snapshot(self.home)
        act()
        changes = private_home_changes(before, private_home_snapshot(self.home))
        text = "; ".join(changes)
        self.assertNotIn(self.SECRET, text, "a failure message carried a file's bytes")
        self.assertNotIn("Secretname", text, "a failure message named a path inside a folder")
        return changes

    def test_an_untouched_home_reads_clean(self):
        self.assertEqual(self.changes_after(lambda: None), [])

    def test_the_same_size_and_mtime_with_new_bytes_is_caught_by_the_hash(self):
        ledger = self.home / "ledger.json"
        st = ledger.stat()

        def rewrite():
            ledger.write_text(f"{self.SECRET.upper()}\n", encoding="utf-8")
            os.utime(ledger, ns=(st.st_atime_ns, st.st_mtime_ns))

        self.assertEqual(self.changes_after(rewrite), ["ledger.json changed (sha256)"])

    def test_a_path_that_appears_or_goes_is_caught(self):
        def stray():
            (self.home / "ledger.lock").unlink()
            (self.home / "synth" / "synth-20260302T060000-00000000.json").write_text(
                "{}", encoding="utf-8")
            (self.home / self.THREAD).unlink()

        self.assertEqual(self.changes_after(stray), [
            "ledger.lock vanished",
            "synth/: 1 path(s) appeared, 0 vanished, 0 changed",
            "threads/: 0 path(s) appeared, 1 vanished, 0 changed",
        ])

    def test_a_folder_made_where_there_was_none_is_caught(self):
        (self.home / "pre-backfill" / "fixture-secretname.md").unlink()
        (self.home / "pre-backfill").rmdir()
        self.assertEqual(
            self.changes_after(lambda: (self.home / "pre-backfill" / "2026").mkdir(parents=True)),
            ["pre-backfill/ appeared", "pre-backfill/: 1 path(s) appeared, 0 vanished, 0 changed"],
        )

    def test_the_real_test_goes_red_when_its_cycle_touches_the_home(self):
        """The test above, run for real, with its home a temp one its cycle writes into."""
        home = self.home

        class Touching(TestNothingEscapes):
            private_home = home

            def cycle(self):
                (home / "synth" / "synth-20260302T060000-00000000.json").write_text(
                    "{}", encoding="utf-8")
                return unittest.TestResult()

        result = unittest.TestResult()
        Touching("test_a_full_cycle_leaves_the_real_places_as_they_were").run(result)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.failures), 1, "the check did not go red")
        message = result.failures[0][1]
        self.assertIn("synth/: 1 path(s) appeared, 0 vanished, 0 changed", message)
        self.assertNotIn(self.SECRET, message)


if __name__ == "__main__":
    unittest.main()
