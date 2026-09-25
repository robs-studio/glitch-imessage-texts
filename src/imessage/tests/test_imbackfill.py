"""backfill — dry first, tranched, resumable after a real SIGKILL, and the copy G2 owes each card.

Every run here goes through the REAL pipeline (``imrun.Pipeline``), the real ledger and
the real engine (resolver, single card writer, projector, the review's accept door),
inside :mod:`imfixture`'s temp vault and database, against a small Messages-shaped
database in a temp folder (``test_imrun``'s harness) and a temp plug-in home.

What each group holds still
---------------------------
* **Dry** — the counts (conversation-days, people, cards, numbers held) and a duration
  measured from the read itself, and NOTHING written: every byte of the home, the vault
  and the database is where it was, and no ledger lock file is created.
* **Confirm** — resolved days queued with ``origin: "backfill"``, unknown numbers held
  with it, transcripts written, tranches committed one by one; the people queue gains
  nothing (a dismissed number is not re-opened, an accepted card is not followed
  through); daily's watermark is untouched and the progress lives beside it.
* **Resumable** — a backfill SIGKILLed mid-tranche in a real child process leaves that
  tranche uncommitted and its progress unmoved; the same dates again continue from the
  first unfinished tranche and every day ends up queued or held exactly once.  An
  exception mid-tranche (the in-process twin) does the same.
* **G2, the three roads** — before the FIRST backfilled line lands on a card, whether
  through the morning run's stale fallback, a summary commit, or a number ``review``
  released, the card is copied byte for byte into ``pre-backfill/<slug>.md`` (0600),
  exactly once; a copy already there is never overwritten; a daily day takes no copy;
  a copy that cannot be taken stops the line and leaves the day queued.

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
import json  # noqa: E402
import os  # noqa: E402
import signal  # noqa: E402
import sqlite3  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from datetime import timedelta  # noqa: E402
from unittest import mock  # noqa: E402

import imbackfill  # noqa: E402
import imessage  # noqa: E402
import imreview  # noqa: E402
import imrun  # noqa: E402
import imspine  # noqa: E402
import imsynth  # noqa: E402
import imthreads  # noqa: E402
from imfixture import ALICE, BOB, CAROL, ERIN  # noqa: E402
from test_imrun import (  # noqa: E402
    _HOME_FILE,
    ALICE_PHONE,
    BOB_EMAIL,
    CAROL_PHONE,
    D0,
    D1,
    D2,
    D3,
    D4,
    D6,
    ERIN_NEW,
    FRANK,
    RunCase,
    _changed,
    _conversation_lines,
    _tree_bytes,
    key,
    morning,
    record_writes,
)

#: (chat, number, the days it texts on).  Each its own one-to-one conversation.
SEED = [
    (1, ALICE_PHONE, [D1, D2, D3]),   # a card: queued
    (3, FRANK, [D1, D2]),             # Contacts name, no card: held
    (7, BOB_EMAIL, [D3]),             # a card: queued
    (10, ERIN_NEW, [D2]),             # Contacts name of a card without the number: held
    (2, CAROL_PHONE, [D4]),           # a card: queued
]
QUEUED = {key(ALICE_PHONE, D1), key(ALICE_PHONE, D2), key(ALICE_PHONE, D3),
          key(BOB_EMAIL, D3), key(CAROL_PHONE, D4)}
HELD = {FRANK: {key(FRANK, D1), key(FRANK, D2)}, ERIN_NEW: {key(ERIN_NEW, D2)}}
#: Transcripts the first tranche (D1-D2, two days a tranche) writes: Alice x2, Frank x2, Erin.
FIRST_TRANCHE_THREADS = 5

NOW = morning(D6)
SUMMARY = "Talked through the paperwork and settled on the new afternoon time"


class BackfillCase(RunCase):
    """``test_imrun``'s harness (temp vault + DB, temp home, temp chat.db), plus backfill."""

    def setUp(self):
        super().setUp()
        self.cfg["synth_stale_days"] = 30     # the stale fallback waits unless a test says
        self.cfg["backfill_tranche_days"] = 2
        self.copies = self.home / imrun.PRE_BACKFILL_DIRNAME

    def seed(self, extra=()):
        spec = [*SEED, *extra]
        for chat, _who, _days in spec:
            with contextlib.suppress(sqlite3.IntegrityError):
                self.chats.chat(chat)
        for day in sorted({d for _c, _w, days in spec for d in days}):
            for chat, who, days in spec:
                if day in days:
                    self.chats.exchange(chat, day, who)

    def backfill(self, date_from=D1, date_to=D4, *, now=NOW, confirm=False, **kw):
        kw.setdefault("source", imrun.ChatDbSource(self.chats.path))
        kw.setdefault("cfg", self.cfg)
        kw.setdefault("contacts", self.contacts)
        kw.setdefault("pause_s", 0)
        with contextlib.redirect_stderr(self.stderr):
            return imbackfill.backfill(
                date_from, date_to, confirm=confirm, now=now, home=self.home,
                threads_dir=self.threads, db_path=self.fx.db_path, **kw)

    def ledger_view(self):
        with self.ledger() as led:
            queued = {k: v for k, v in led.queued().items()}
            held = {i: led.held(i) for i in led.all_identifiers()}
            ids = led.all_identifiers()
            stamped = {k for k in led.stamped if led.is_stamped(k)}
        return queued, held, ids, stamped

    def card_bytes(self, *specs):
        return {s.slug: self.cards[s.slug].path.read_bytes() for s in specs}

    def copy_of(self, spec):
        return self.copies / f"{spec.slug}.md"

    def assert_copy(self, spec, expected):
        copy = self.copy_of(spec)
        self.assertTrue(copy.is_file(), f"no pre-backfill copy of {spec.slug}")
        self.assertEqual(copy.read_bytes(), expected,
                         f"{spec.slug}'s copy is not the card as it was before its first "
                         "backfilled line")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)

    def claim_and_commit(self, now):
        with contextlib.redirect_stderr(self.stderr):
            claimed = imsynth.claim(None, home=self.home, threads_dir=self.threads,
                                    db_path=self.fx.db_path, cfg=self.cfg, now=now,
                                    contacts={})
            self.assertIsNone(claimed["paused"], claimed["paused"])
            path = Path(claimed["claim_path"])
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["summaries"] = {u["id"]: f"{SUMMARY} ({u['day']})" for u in doc["units"]}
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            return imsynth.commit(path, home=self.home, db_path=self.fx.db_path,
                                  cfg=self.cfg, now=now)


# ---------------------------------------------------------------------------
# 1. The dry run.
# ---------------------------------------------------------------------------


class TestDry(BackfillCase):
    def test_a_dry_run_counts_measures_and_writes_nothing(self):
        self.seed()
        before = _tree_bytes(self.home, self.fx.root)
        measured = {}
        real_estimate = imbackfill.estimate

        def spy(**kw):
            measured.update(kw)
            return real_estimate(**kw)

        with mock.patch.object(imbackfill, "estimate", spy):
            report = self.backfill()
        self.assertGreater(measured["read_s"], 0, "the estimate is not built on a measured read")
        self.assertIsNone(report["paused"], report["paused"])
        self.assertIsNone(report["error"])
        self.assertFalse(report["confirm"])
        counts = report["counts"]
        self.assertEqual(counts["conversation_days"], 8)
        self.assertEqual(counts["person_days"], 8)
        self.assertEqual(counts["people"], 5)
        self.assertEqual(counts["cards"], 3, "Alice, Bob and Carol already have a card")
        self.assertEqual(counts["queued"], 5)
        self.assertEqual(counts["held_total"], 3)
        self.assertEqual(counts["numbers_held"], 2)
        self.assertEqual(counts["threads"], 8)
        self.assertEqual(counts["raised"], 0)
        self.assertEqual(report["window"], {"from": "2026-03-02", "to": "2026-03-05", "days": 4})
        self.assertEqual(report["tranches"], 2)

        est = report["estimate"]
        self.assertGreaterEqual(est["seconds"], 1)
        self.assertEqual(est["read_s"], round(measured["read_s"], 3))
        self.assertEqual(est["write_s"], round(8 * imbackfill.THREAD_WRITE_S, 2))
        self.assertEqual(est["landing"]["queued"], 5)
        self.assertEqual(est["landing"]["mornings"], 1)
        self.assertTrue(est["said"].startswith("about "))

        self.assertEqual(_changed(before, _tree_bytes(self.home, self.fx.root)), [],
                         "a dry backfill wrote something")
        self.assertFalse((self.home / "ledger.lock").exists(), "the dry run made a lock file")
        self.assertFalse(self.threads.exists())
        self.assertEqual(self.proposals(), [])

    def test_the_dry_run_says_the_duration_and_the_way_back_before_the_yes(self):
        self.seed()
        text = imessage.backfill_render(self.backfill())
        self.assertIn("a dry run: nothing was written, nothing was raised", text)
        self.assertIn("5 day(s) are with 3 people who already have a card", text)
        self.assertIn("wait on your texts review list", text)
        self.assertIn("Nothing goes on your main people queue", text)
        self.assertIn("How long: about", text)
        self.assertIn("measured, not guessed", text)
        self.assertIn("pre-backfill/", text)
        self.assertIn("undo that memory change", text)
        self.assertIn("--confirm", text)

    def test_the_window_and_the_gates_refuse_and_write_nothing(self):
        self.seed()
        before = _tree_bytes(self.home, self.fx.root)
        today = self.backfill(D1, D6)                  # today is not over yet
        self.assertIn("before today", today["error"])
        backwards = self.backfill(D4, D1)
        self.assertIn("is before --from", backwards["error"])
        bad = self.backfill("2026-3-2", D4)
        self.assertIn("YYYY-MM-DD", bad["error"])
        no_own = self.backfill(cfg={**self.cfg, "own_handles": []}, confirm=True)
        self.assertEqual(no_own["paused"]["reason"], "no_own_handles")
        self.assertEqual(_changed(before, _tree_bytes(self.home, self.fx.root)), [])


# ---------------------------------------------------------------------------
# 2. The confirmed run.
# ---------------------------------------------------------------------------


class TestConfirm(BackfillCase):
    def test_it_queues_holds_marks_the_origin_raises_nothing_and_leaves_the_watermark(self):
        self.seed()
        self.daily(morning(D1))                      # a watermark exists, as it will for the member
        state_before = self.state()
        self.assertIn("watermark", state_before)
        report = self.backfill(confirm=True)
        self.assertIsNone(report["paused"], report["paused"])
        self.assertTrue(report["complete"])
        self.assertEqual(report["done_tranches"], 2)
        self.assertEqual(report["watermark_before"], state_before["watermark"])
        self.assertEqual(report["watermark_after"], state_before["watermark"])

        queued, held, ids, stamped = self.ledger_view()
        self.assertEqual(set(queued), QUEUED)
        for record in queued.values():
            self.assertEqual(record["origin"], imrun.ORIGIN_BACKFILL)
            self.assertEqual(record["queued_at"], NOW.isoformat())
        for ident, keys in HELD.items():
            self.assertEqual({u["key"] for u in held[ident]}, keys)
            self.assertTrue(all(u["origin"] == imrun.ORIGIN_BACKFILL for u in held[ident]))
        self.assertEqual(ids[FRANK]["state"], "held")
        self.assertEqual(stamped, set(), "a backfill stamps nothing itself")
        for record in [*queued.values(), *(u for units in held.values() for u in units)]:
            for link in record["links"]:
                self.assertTrue((self.threads / link.split("/threads/", 1)[1]).is_file())

        state = self.state()
        self.assertEqual(state["watermark"], state_before["watermark"])
        self.assertEqual(state["watermark_kind"], state_before["watermark_kind"])
        self.assertEqual(state["last_run_at"], state_before["last_run_at"])
        progress = state[imbackfill.PROGRESS_KEY]
        self.assertEqual((progress["from"], progress["to"], progress["next"],
                          progress["complete"], progress["tranches_done"]),
                         ("2026-03-02", "2026-03-05", None, True, 2))

        self.assertEqual(self.proposals(), [], "a backfill put something on the people queue")
        for spec in (ALICE, BOB, CAROL):
            self.assertEqual(_conversation_lines(self.cards[spec.slug].path), [])
        text = imessage.backfill_render(report)
        self.assertIn("did not move", text)
        self.assertIn("Finished", text)

    def test_with_no_watermark_yet_it_creates_none(self):
        self.seed()
        self.backfill(confirm=True)
        state = self.state()
        self.assertNotIn("watermark", state, "a backfill set the morning run's watermark")
        # and the first daily run is still a first run: yesterday only
        first = self.daily(morning(D6))
        self.assertTrue(first["watermark"]["first_run"])

    def test_a_dismissed_number_is_never_reopened_by_a_backfill(self):
        stub = imspine.resolve(self.conn, FRANK, "Fixture Frank", emit=True).proposal_id
        self.fx.commit()
        self.fx.dismiss(stub, at="2026-02-20T09:00:00+00:00")   # the no, before every text
        self.seed()                                             # he is texted after it
        self.backfill(confirm=True)
        self.assertEqual([(i, s) for i, _k, s, _p in self.proposals(FRANK)], [(stub, "dismissed")])
        _q, held, ids, _s = self.ledger_view()
        self.assertEqual(ids[FRANK]["state"], "dismissed")
        self.assertEqual({u["reason"] for u in held[FRANK]}, {"dismissed"})

    def test_an_accepted_card_is_not_followed_through_by_a_backfill(self):
        stub = imspine.resolve(self.conn, FRANK, "Fixture Frank", emit=True).proposal_id
        self.fx.commit()
        self.seed()
        self.daily(morning(D2))                      # the ledger learns the pending card
        self.assertEqual(self.fx.accept(stub)["status"], "ok")
        self.backfill(D1, D2, confirm=True)
        kinds = [(k, s) for _i, k, s, _p in self.proposals(FRANK)]
        self.assertEqual(kinds, [("new_stub", "accepted")],
                         "a backfill raised the second yes itself; the daily run does that")

    def test_every_write_lands_in_the_home(self):
        self.seed()
        with record_writes() as seen:
            self.backfill(confirm=True)
        threads, home = self.threads.resolve(), self.home.resolve()

        def place(path):
            p = Path(path).resolve()
            if p == threads or threads in p.parents:
                return "threads"
            if p.parent == home and _HOME_FILE.match(p.name):
                return "home"
            return None

        strays = sorted({(p, w) for p, w in seen if place(p) is None})
        self.assertEqual(strays, [], "a backfill wrote outside its own places")
        self.assertEqual({place(p) for p, _ in seen}, {"threads", "home"})

    def test_a_finished_window_asked_again_rereads_harmlessly(self):
        self.seed()
        self.backfill(confirm=True)
        view = self.ledger_view()
        again = self.backfill(confirm=True)
        self.assertTrue(again["complete"])
        self.assertEqual(again["counts"]["queued"], 0)
        self.assertEqual(self.ledger_view()[:2], view[:2], "a second pass changed the ledger")


# ---------------------------------------------------------------------------
# 3. Resumable: an exception and a real SIGKILL, mid-tranche.
# ---------------------------------------------------------------------------


class Killed(BaseException):
    """Stands in for a kill: nothing in the run catches it."""


class TestResumeInProcess(BackfillCase):
    def test_a_run_that_dies_mid_tranche_continues_from_that_tranche(self):
        self.seed()
        real, calls = imthreads.write_thread, {"n": 0}

        def dying(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == FIRST_TRANCHE_THREADS + 1:
                raise Killed
            return real(*args, **kwargs)

        # No cleanup either (a kill runs none): the tranche's run is never even aborted.
        with mock.patch.object(imthreads, "write_thread", dying), \
                mock.patch.object(imrun, "_undo", lambda *a, **k: None), \
                self.assertRaises(Killed):
            self.backfill(confirm=True)

        progress = self.state()[imbackfill.PROGRESS_KEY]
        self.assertEqual((progress["next"], progress["complete"]), ("2026-03-04", False))
        queued, held, _ids, _s = self.ledger_view()
        self.assertEqual(set(queued), {key(ALICE_PHONE, D1), key(ALICE_PHONE, D2)})
        self.assertNotIn(key(ALICE_PHONE, D3), queued)

        again = self.backfill(confirm=True)
        self.assertEqual(again["resumes_from"], "2026-03-04")
        self.assertEqual(again["done_tranches"], 1, "the finished tranche was read again")
        self.assertEqual(again["counts"]["person_days"], 3)
        self.assertTrue(again["complete"])
        queued, held, _ids, _s = self.ledger_view()
        self.assertEqual(set(queued), QUEUED)
        for ident, keys in HELD.items():
            self.assertEqual(sorted(u["key"] for u in held[ident]), sorted(keys))
        self.assertEqual(self.state()[imbackfill.PROGRESS_KEY]["complete"], True)


CHILD = r'''
"""One confirmed backfill on the parent's temp vault, DB and home; killed mid-tranche."""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
plugin = Path(plan["plugin_home"])
sys.path.insert(0, str(plugin / "tests"))
sys.path.insert(0, str(plugin))

import imconfig  # the plug-in's sys.path law: first

imconfig.ensure_engine_path()

import imbackfill  # noqa: E402
import imchat  # noqa: E402
import imfixture  # noqa: E402
import imrun  # noqa: E402
import imspine  # noqa: E402
import imthreads  # noqa: E402

imchat._is_macos = lambda: True
imchat._zone = lambda: timezone.utc


class Kept(imfixture.SpineFixture):
    """imfixture's re-rooting onto the PARENT's temp root, which the parent removes."""

    def keep(self, root):
        self.root = Path(root)
        self._patch_config()
        self._assert_contained()
        self.conn = imspine.open_conn(db_path=self.db_path)
        return self


fx = Kept().keep(plan["vault"])
real, calls = imthreads.write_thread, {"n": 0}


def pausing(*args, **kwargs):
    calls["n"] += 1
    if calls["n"] == plan["kill_at"]:
        marker = Path(plan["marker"])
        tmp = marker.with_name(marker.name + ".tmp")
        tmp.write_text("reached", encoding="utf-8")
        os.replace(tmp, marker)
        time.sleep(600)  # the parent SIGKILLs the process group here
        raise SystemExit("never killed")
    return real(*args, **kwargs)


imthreads.write_thread = pausing
imbackfill.backfill(
    plan["from"], plan["to"], confirm=True, source=imrun.ChatDbSource(plan["chat_db"]),
    now=datetime.fromisoformat(plan["now"]), cfg=plan["cfg"], home=plan["home"],
    threads_dir=plan["threads"], db_path=fx.db_path, contacts=plan["contacts"], pause_s=0,
)
fx.conn.close()
'''


@unittest.skipIf(sys.platform == "win32" or not hasattr(signal, "SIGKILL"),
                 "SIGKILL and process groups are POSIX")
class TestResumeAfterSigkill(BackfillCase):
    def test_a_backfill_killed_mid_tranche_continues_where_it_stopped(self):
        self.seed()
        script, marker = self.root / "child.py", self.root / "reached"
        script.write_text(CHILD, encoding="utf-8")
        plan = {
            "plugin_home": str(PLUGIN_HOME), "vault": str(self.fx.root),
            "chat_db": str(self.chats.path), "home": str(self.home),
            "threads": str(self.threads), "from": D1.isoformat(), "to": D4.isoformat(),
            "now": NOW.isoformat(), "cfg": self.cfg, "contacts": self.contacts,
            "kill_at": FIRST_TRANCHE_THREADS + 1, "marker": str(marker),
        }
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "GLITCH_BUDGET_S"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        err = open(self.root / "child.err", "wb")  # noqa: SIM115
        self.addCleanup(err.close)
        proc = subprocess.Popen(  # noqa: S603 - argv list, no shell, a temp script
            [sys.executable, str(script), str(plan_path)], stdout=subprocess.DEVNULL,
            stderr=err, stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        deadline = time.monotonic() + 120
        while not marker.exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                self.fail("the child never reached the kill point: "
                          + (self.root / "child.err").read_text(errors="replace")[-2000:])
            time.sleep(0.05)
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # as the engine kills a tree
        proc.wait(timeout=30)
        self.assertEqual(proc.returncode, -signal.SIGKILL)

        # The killed tranche left its begin in the log and no commit.
        wal = (self.home / "ledger.wal").read_text(encoding="utf-8").splitlines()
        runs = [json.loads(line) for line in wal if line.strip()]
        begins = [r["run"] for r in runs if r["op"] == "begin"]
        done = {r["run"] for r in runs if r["op"] == "committed"}
        self.assertEqual(len(begins), 1, "the first tranche's log should have been compacted")
        self.assertNotIn(begins[0], done)
        progress = self.state()[imbackfill.PROGRESS_KEY]
        self.assertEqual((progress["next"], progress["complete"]), ("2026-03-04", False))
        queued, _held, _ids, _s = self.ledger_view()
        self.assertEqual(set(queued), {key(ALICE_PHONE, D1), key(ALICE_PHONE, D2)})

        again = self.backfill(confirm=True)
        self.assertEqual(again["resumes_from"], "2026-03-04")
        self.assertTrue(again["complete"])
        queued, held, _ids, _s = self.ledger_view()
        self.assertEqual(set(queued), QUEUED)
        for ident, keys in HELD.items():
            self.assertEqual(sorted(u["key"] for u in held[ident]), sorted(keys))
        self.assertNotIn("watermark", self.state())
        self.assertEqual(self.proposals(), [])


# ---------------------------------------------------------------------------
# 4. G2: the copy before the first backfilled line, on each of the three roads.
# ---------------------------------------------------------------------------


class TestPreBackfillCopy(BackfillCase):
    def test_road_one_the_stale_fallback_copies_each_card_once_and_never_again(self):
        self.seed(extra=[(1, ALICE_PHONE, [D0])])
        self.backfill(confirm=True)
        before = self.card_bytes(ALICE, BOB, CAROL)
        self.cfg["synth_stale_days"] = 3
        first = self.daily(NOW + timedelta(days=3))
        self.assertEqual(first["stale"]["stamped"], 5)
        self.assertEqual(first["stale"]["pre_backfill_copies"], 3)
        for spec in (ALICE, BOB, CAROL):
            self.assert_copy(spec, before[spec.slug])
        self.assertEqual(len(_conversation_lines(self.cards["fixture-alice"].path)), 3)
        self.assertEqual(sorted(p.name for p in self.copies.iterdir()),
                         ["fixture-alice.md", "fixture-bob.md", "fixture-carol.md"],
                         "a stray file (a temp, an extra copy) was left in pre-backfill/")

        # A second backfill lands another line on Alice: her copy is never taken again.
        kept = self.copy_of(ALICE).read_bytes()
        self.backfill(D0, D0, now=NOW + timedelta(days=3), confirm=True)
        second = self.daily(NOW + timedelta(days=7))
        self.assertEqual(second["stale"]["stamped"], 1)
        self.assertEqual(second["stale"]["pre_backfill_copies"], 0)
        self.assertEqual(self.copy_of(ALICE).read_bytes(), kept)
        self.assertEqual(len(_conversation_lines(self.cards["fixture-alice"].path)), 4)
        self.assertIn("pre-backfill/", imessage.daily_render(first))

    def test_road_two_a_summary_commit_copies_before_its_first_line(self):
        self.seed()
        self.backfill(confirm=True)
        before = self.card_bytes(ALICE, BOB, CAROL)
        report = self.claim_and_commit(NOW + timedelta(hours=1))
        self.assertEqual(len(report["landed"]), 5, report)
        for spec in (ALICE, BOB, CAROL):
            self.assert_copy(spec, before[spec.slug])
            self.assertTrue(all(SUMMARY in line for line in
                                _conversation_lines(self.cards[spec.slug].path)))

    def test_road_three_a_number_review_releases_keeps_its_origin_and_copies_its_card(self):
        self.seed()
        self.backfill(confirm=True)
        now = NOW + timedelta(hours=1)
        with contextlib.redirect_stderr(self.stderr):
            listing = imreview.review(home=self.home, db_path=self.fx.db_path, cfg=self.cfg,
                                      contacts=self.contacts, now=now)
            row = next(r for r in listing["listing"]["rows"] if r["number"] == ERIN_NEW)
            act = imreview.review(accept=row["label"], listing=listing["listing"]["code"],
                                  confirm=True, home=self.home, db_path=self.fx.db_path,
                                  cfg=self.cfg, contacts=self.contacts, now=now)
        self.assertTrue(act["confirmed"], act)
        queued, _h, _i, _s = self.ledger_view()
        released = queued[key(ERIN_NEW, D2)]
        self.assertEqual((released["person_id"], released["origin"]),
                         (ERIN.pid, imrun.ORIGIN_BACKFILL))
        self.assertFalse(self.copy_of(ERIN).exists(), "copied before any line was due")
        erin_before = self.cards["fixture-erin"].path.read_bytes()   # the number attached
        self.assertIn(ERIN_NEW, erin_before.decode("utf-8"))

        report = self.claim_and_commit(now + timedelta(hours=1))
        self.assertEqual(len(report["landed"]), 6, report)
        self.assert_copy(ERIN, erin_before)
        self.assertEqual(len(_conversation_lines(self.cards["fixture-erin"].path)), 1)

    def test_a_daily_day_takes_no_copy(self):
        self.alice_day()
        self.daily(morning(D2))
        self.cfg["synth_stale_days"] = 0
        report = self.daily(morning(D3))
        self.assertEqual(report["stale"]["stamped"], 1)
        self.assertFalse(self.copies.exists(), "a daily day took a pre-backfill copy")

    def test_a_copy_already_there_is_never_overwritten(self):
        self.seed()
        self.backfill(confirm=True)
        self.copies.mkdir(mode=0o700)
        planted = self.copy_of(CAROL)
        planted.write_bytes(b"the member's own earlier copy\n")
        report = self.claim_and_commit(NOW + timedelta(hours=1))
        self.assertEqual(len(report["landed"]), 5)
        self.assertEqual(planted.read_bytes(), b"the member's own earlier copy\n")
        self.assertTrue(self.copy_of(ALICE).is_file())

    def test_a_copy_that_cannot_be_taken_stops_the_line_and_keeps_the_day_queued(self):
        self.seed()
        self.backfill(confirm=True)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, self.copies)          # a planted link out of the home
        before = self.card_bytes(ALICE, BOB, CAROL)
        report = self.claim_and_commit(NOW + timedelta(hours=1))
        self.assertEqual(report["landed"], [])
        self.assertEqual(len(report["failed"]), 5)
        self.assertTrue(all("could not be taken" in why for why in report["failed"].values()))
        self.assertEqual(self.card_bytes(ALICE, BOB, CAROL), before)
        self.assertEqual(list(elsewhere.iterdir()), [])
        queued, _h, _i, _s = self.ledger_view()
        self.assertEqual(set(queued), QUEUED, "a refused day left the queue")


class TestKeepCopy(unittest.TestCase):
    """``imrun.keep_pre_backfill_copy`` on its own: byte for byte, 0600, once, contained."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.card = self.root / "fixture-alice.md"
        self.card.write_bytes(b"---\nid: prs_fxalice2\n---\n\n## Interactions\n\xe2\x80\x94\n")

    def test_byte_for_byte_owner_only_and_once(self):
        made = imrun.keep_pre_backfill_copy(self.card, self.home)
        self.assertEqual(made, (self.home / "pre-backfill" / "fixture-alice.md").resolve())
        self.assertEqual(made.read_bytes(), self.card.read_bytes())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(made.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(made.parent.stat().st_mode), 0o700)
        self.card.write_bytes(b"changed")
        self.assertIsNone(imrun.keep_pre_backfill_copy(self.card, self.home))
        self.assertNotEqual(made.read_bytes(), b"changed")
        self.assertEqual([p.name for p in made.parent.iterdir()], ["fixture-alice.md"])

    def test_it_refuses_every_target_outside_pre_backfill(self):
        odd = self.root / "..md"
        odd.write_bytes(b"x")
        with self.assertRaises(ValueError):
            imrun.keep_pre_backfill_copy(odd, self.home)
        (self.home / "pre-backfill").mkdir()
        os.symlink(self.root / "outside.md", self.home / "pre-backfill" / "fixture-alice.md")
        with self.assertRaises(ValueError):
            imrun.keep_pre_backfill_copy(self.card, self.home)
        self.assertFalse((self.root / "outside.md").exists())

    def test_an_unreadable_card_leaves_no_file(self):
        with self.assertRaises(OSError):
            imrun.keep_pre_backfill_copy(self.root / "fixture-gone.md", self.home)
        self.assertEqual(list((self.home / "pre-backfill").iterdir()), [])


class TestSmallPieces(unittest.TestCase):
    def test_resume_point(self):
        days = [D1, D2, D3, D4]
        self.assertEqual(imbackfill.resume_point({}, days), (0, None))
        same = {"backfill": {"from": "2026-03-02", "to": "2026-03-05", "next": "2026-03-04",
                             "complete": False}}
        self.assertEqual(imbackfill.resume_point(same, days), (2, None))
        done = {"backfill": {**same["backfill"], "complete": True, "next": None}}
        self.assertEqual(imbackfill.resume_point(done, days), (0, None))
        other = {"backfill": {**same["backfill"], "from": "2025-01-01"}}
        self.assertEqual(imbackfill.resume_point(other, days), (0, other["backfill"]))

    def test_say_duration(self):
        self.assertEqual(imbackfill.say_duration(0.2), "about 1 second")
        self.assertEqual(imbackfill.say_duration(40.1), "about 41 seconds")
        self.assertEqual(imbackfill.say_duration(200), "about 4 minutes")
        self.assertEqual(imbackfill.say_duration(3 * 3600), "about 3.0 hours")

    def test_the_parser_takes_the_verb(self):
        args = imessage._build_parser().parse_args(
            ["backfill", "--from", "2026-01-01", "--to", "2026-01-31", "--confirm",
             "--max-seconds", "90"])
        self.assertEqual((args.date_from, args.date_to, args.confirm, args.max_seconds),
                         ("2026-01-01", "2026-01-31", True, 90.0))


if __name__ == "__main__":
    unittest.main()
