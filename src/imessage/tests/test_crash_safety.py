"""Crash safety: a daily run SIGKILLed mid-stamp, then run again.  The test that earns the design.

The engine's morning pass kills an adapter that overruns its grant with ``os.killpg(...,
SIGKILL)`` on the whole process tree, and re-runs an interrupted adapter, both on the
assumption that readers are side-effect-free.  This plug-in writes interactions onto
people's cards, so that assumption does not hold here, and the ledger's write-ahead
intent is the only thing standing where it used to.  This file kills a REAL child
process at a deterministic point inside a stamp, the way the engine would, and proves
what the next run does about it.

How the kill is placed
----------------------
A small child script (written to a temp folder) re-roots :mod:`imfixture`'s temp vault
and database onto a FIXED folder that outlives each child, and runs ``imrun.daily``
against a temp plug-in home and in-memory texts.  In the killing run it replaces
``imspine.stamp`` with one that, on the Nth call, either writes the card first (the
"after the card write" case) or does not (the "before" case), then drops a marker file
and sleeps.  The parent waits for the marker and kills the child's process group with
SIGKILL, exactly as ``morning_reports._kill_tree_posix`` does.  Nothing in the child can
catch that; no ``finally`` runs.

The timeline, every scenario
----------------------------
1. **Run A** (a morning, ``synth_stale_days`` 3): Alice, Carol and Bob's day-one texts
   are queued, not stamped.  Committed; the watermark placed.
2. **Run B** (two days later, stale days forced to 0, so every queued day lands): new
   texts for Carol and Bob on day two are read and queued, then the stale fallback
   starts filing, and the Nth stamp is KILLED.
3. **Run C**, a fresh child at the same moment (the stage's at-least-once re-run):
   recovery settles every open intent against its card, the rest lands.
4. **Run D**, once more: nothing left to do, and nothing done twice.

What is asserted
----------------
* After the kill: the watermark and ``ledger.json`` are byte-for-byte what run A left
  (nothing advanced past unfinished work), the WAL holds the killed run's ``begin`` and
  the killed day's ``stamp_intent`` but no ``committed``, the card carries the line only
  in the "after" case, and ``memory.db`` holds no row from the killed run.
* After run C: every one of the five person-days is stamped exactly once (one line on
  its card, one row in ``memory.db``), no card carries any duplicate line, no intent is
  left open, nothing is left queued, and the watermark covers day two.  Recovery
  completed each intent that reached its card (``recovered.done``) and dropped the one
  that did not (``recovered.dropped``), and a recovered card is re-projected: Alice has
  no day-two texts, so when her one stamp was the killed one, recovery is the ONLY path
  that could put her row in ``memory.db``.
* After run D: recovery found nothing, nothing was filed, and every card and row is
  exactly as run C left it.

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
import signal  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from datetime import UTC, date, datetime  # noqa: E402

import imledger  # noqa: E402
import imthreads  # noqa: E402
from imfixture import ALICE, BOB, CAROL, real_fixture_traces  # noqa: E402

ALICE_PHONE = ALICE.phones[0]
CAROL_PHONE = CAROL.phones[0]
BOB_EMAIL = BOB.emails[0]
ME = "+15555550199"
CONTACTS = {ALICE_PHONE: "Fixture Alice", CAROL_PHONE: "Fixture Carol", BOB_EMAIL: "Fixture Bob"}
CARDS = {ALICE_PHONE: ALICE, CAROL_PHONE: CAROL, BOB_EMAIL: BOB}
LONG_A = "Can we move the appointment to four o'clock, or is the morning easier for you?"
LONG_B = "Four works fine for me, I will let the front desk know and see you then."
D1 = date(2026, 3, 2)
D2 = date(2026, 3, 3)
D3 = date(2026, 3, 4)

#: The person-days the whole timeline files: Alice on day one only, on purpose.
ALL_DAYS = [(ALICE_PHONE, D1), (CAROL_PHONE, D1), (BOB_EMAIL, D1),
            (CAROL_PHONE, D2), (BOB_EMAIL, D2)]

#: How long the parent waits for a child to reach its kill point, or to finish.
CHILD_TIMEOUT_S = 120.0

#: Everything a run could write into a plug-in home (see test_imdaily's safety net).
RUN_WRITES = ("ledger", "state", "synth", "pre-backfill", "threads")


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def key(ident, day):
    return imthreads.ledger_key(ident, imthreads.DAY_GRAIN, day)


# ---------------------------------------------------------------------------
# The child.  Written to a temp folder and run with this interpreter.
# ---------------------------------------------------------------------------

CHILD = r'''
"""One daily run, on a temp vault + DB + plug-in home, that may be killed mid-stamp."""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
plugin = Path(plan["plugin_home"])
sys.path.insert(0, str(plugin / "tests"))
sys.path.insert(0, str(plugin))

import imconfig  # the plug-in's sys.path law: first

imconfig.ensure_engine_path()

import imchat  # noqa: E402
import imfixture  # noqa: E402
import imrun  # noqa: E402
import imspine  # noqa: E402

APPLE_EPOCH = datetime.fromisoformat("2001-01-01T00:00:00+00:00")


class KeptFixture(imfixture.SpineFixture):
    """imfixture's re-rooting on a FIXED root that outlives this process: never rmtree'd."""

    def keep(self, root, *, fresh):
        self.root = Path(root)
        self.people_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(exist_ok=True)
        self._patch_config()
        self._assert_contained()
        self.conn = imspine.open_conn(db_path=self.db_path)
        if fresh:
            self.standard_cards()
        return self


def message(row):
    when = datetime.fromisoformat(row["when"])
    delta = when - APPLE_EPOCH
    raw = (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
    from_me = row["sender"] is None
    return row["rowid"], imchat.Message(
        chat_rowid=row["chat"], chat_style=imchat.ONE_TO_ONE_STYLE, chat_name=None,
        is_group=False, date_raw=raw, dt_local=when, is_from_me=from_me,
        handle=row["to"] if from_me else row["sender"], text=row["text"])


fx = KeptFixture().keep(plan["vault"], fresh=plan["fresh"])

if plan.get("topic"):
    # Every line this run writes carries these words instead: a re-run whose text for
    # a day differs from the killed run's, as a written summary would.
    imrun.safe_topic = lambda _topic, _words=plan["topic"]: _words

kill = plan.get("kill")
if kill:
    real_stamp = imspine.stamp
    calls = {"n": 0}

    def pausing_stamp(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] != kill["at_call"]:
            return real_stamp(*args, **kwargs)
        wrote = None
        if kill["after_card_write"]:
            wrote = real_stamp(*args, **kwargs).stamped
        marker = Path(kill["marker"])
        tmp = marker.with_name(marker.name + ".tmp")
        tmp.write_text(json.dumps({"card": str(args[0]), "day": kwargs["occurred_at"],
                                   "wrote": wrote}), encoding="utf-8")
        os.replace(tmp, marker)
        time.sleep(600)  # the parent SIGKILLs the process group here
        raise SystemExit("never killed")

    imspine.stamp = pausing_stamp

report = imrun.daily(
    now=datetime.fromisoformat(plan["now"]),
    source=imrun.ListSource([message(r) for r in plan["rows"]]),
    cfg=plan["cfg"], home=plan["home"], threads_dir=plan["threads"],
    db_path=fx.db_path, contacts=plan["contacts"], budget_s=None,
)
Path(plan["result"]).write_text(json.dumps(report, default=str), encoding="utf-8")
fx.conn.close()
'''


def _rows(days):
    """In-memory texts, ROWIDs in arrival order: ``days`` is ``[(day, [who, ...]), ...]``."""
    chat_of = {ALICE_PHONE: 1, CAROL_PHONE: 2, BOB_EMAIL: 3}
    rows = []
    for day, people in days:
        for who in people:
            chat = chat_of[who]
            for minute, text, sender, to in ((chat, LONG_A, who, None),
                                             (chat + 5, LONG_B, None, who)):
                rows.append({"rowid": len(rows) + 1, "chat": chat,
                             "when": at(day, 9, minute).isoformat(), "text": text,
                             "sender": sender, "to": to})
    return rows


DAY_ONE = [(D1, [ALICE_PHONE, CAROL_PHONE, BOB_EMAIL])]
DAY_TWO = [*DAY_ONE, (D2, [CAROL_PHONE, BOB_EMAIL])]


def _interaction_lines(text):
    out, capturing = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            capturing = line[3:].strip().lower() == "interactions"
            continue
        if capturing and line.startswith("- "):
            out.append(line)
    return out


def _run_listing(home):
    found = {}
    for top in sorted(Path(home).iterdir()):
        if not top.name.startswith(RUN_WRITES):
            continue
        for path in [top, *sorted(top.rglob("*"))] if top.is_dir() else [top]:
            if path.is_dir():
                found[path.relative_to(home).as_posix()] = "dir"
            else:
                st = path.lstat()
                found[path.relative_to(home).as_posix()] = (st.st_mtime_ns, st.st_size)
    return found


_REAL_HOME_BEFORE: dict = {}


def setUpModule():
    _REAL_HOME_BEFORE.update(_run_listing(PLUGIN_HOME))


def tearDownModule():
    """No child here may ever reach the member's real home: they are all pointed away."""
    after = _run_listing(PLUGIN_HOME)
    changed = sorted(k for k in set(_REAL_HOME_BEFORE) | set(after)
                     if _REAL_HOME_BEFORE.get(k) != after.get(k))
    if changed:
        raise AssertionError(f"a child wrote into the REAL plug-in home: {changed[:20]}")


# ---------------------------------------------------------------------------
# The harness.
# ---------------------------------------------------------------------------


@unittest.skipIf(sys.platform == "win32" or not hasattr(signal, "SIGKILL"),
                 "SIGKILL and process groups are POSIX; the engine kills the tree another way "
                 "on Windows")
class CrashCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="imcrash-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.vault = self.tmp / "vault"
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.threads = self.home / "threads"
        self.script = self.tmp / "child.py"
        self.script.write_text(CHILD, encoding="utf-8")
        self.runs = 0
        # No clock: the kill is placed by the marker, never by a budget.
        self.env = {k: v for k, v in os.environ.items() if k != "GLITCH_BUDGET_S"}
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    # -- running children --------------------------------------------------------

    def plan(self, *, now, rows, stale_days, fresh=False, kill=None, topic=None):
        self.runs += 1
        plan = {
            "plugin_home": str(PLUGIN_HOME), "vault": str(self.vault),
            "home": str(self.home), "threads": str(self.threads), "fresh": fresh,
            "now": now.isoformat(), "rows": rows, "contacts": CONTACTS,
            "cfg": {**imconfig.DEFAULTS, "own_handles": [ME], "never_ingest": [],
                    "synth_stale_days": stale_days},
            "result": str(self.tmp / f"result-{self.runs}.json"), "kill": kill,
            "topic": topic,
        }
        path = self.tmp / f"plan-{self.runs}.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return plan, path

    def launch(self, plan_path):
        out = open(self.tmp / f"child-{self.runs}.out", "wb")  # noqa: SIM115
        err = open(self.tmp / f"child-{self.runs}.err", "wb")  # noqa: SIM115
        self.addCleanup(out.close)
        self.addCleanup(err.close)
        return subprocess.Popen(  # noqa: S603 - argv list, no shell, a temp script
            [sys.executable, str(self.script), str(plan_path)],
            stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=self.env,
            start_new_session=True,   # its own group, as the engine launches it
        )

    def child_err(self):
        return (self.tmp / f"child-{self.runs}.err").read_text(encoding="utf-8", errors="replace")

    def run_child(self, **kw):
        """A child that runs to the end; returns its daily report."""
        plan, path = self.plan(**kw)
        proc = self.launch(path)
        try:
            proc.wait(timeout=CHILD_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            self.fail("a child did not finish")
        self.assertEqual(proc.returncode, 0, self.child_err())
        return json.loads(Path(plan["result"]).read_text(encoding="utf-8"))

    def kill_child(self, *, at_call, after_card_write, **kw):
        """A child killed with SIGKILL inside its ``at_call``-th stamp.  Returns the marker."""
        marker = self.tmp / f"killpoint-{self.runs + 1}.json"
        kill = {"at_call": at_call, "after_card_write": after_card_write, "marker": str(marker)}
        plan, path = self.plan(kill=kill, **kw)
        proc = self.launch(path)
        deadline = time.monotonic() + CHILD_TIMEOUT_S
        while not marker.exists():
            if proc.poll() is not None:
                self.fail(f"the child exited ({proc.returncode}) before its kill point:\n"
                          f"{self.child_err()}")
            if time.monotonic() > deadline:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                self.fail("the child never reached its kill point")
            time.sleep(0.01)
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)   # morning_reports._kill_tree_posix
        proc.wait(timeout=30)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertFalse(Path(plan["result"]).exists(), "the killed run finished anyway")
        return json.loads(marker.read_text(encoding="utf-8"))

    # -- reading what is on disk -------------------------------------------------

    def card(self, ident):
        return self.vault / "Memory" / "people" / f"{CARDS[ident].slug}.md"

    def lines(self, ident, day=None):
        text = self.card(ident).read_text(encoding="utf-8")
        found = [x for x in _interaction_lines(text) if "— conversation (" in x]
        return [x for x in found if day is None or x.startswith(f"- {day.isoformat()} ")]

    def all_card_lines(self):
        people = self.vault / "Memory" / "people"
        return {p.name: _interaction_lines(p.read_text(encoding="utf-8"))
                for p in sorted(people.glob("*.md"))}

    def rows(self):
        conn = sqlite3.connect(str(self.vault / "memory.db"))
        try:
            return sorted(tuple(r) for r in conn.execute(
                "SELECT person_id, occurred_at FROM interaction "
                "WHERE source = 'conversation'").fetchall())
        finally:
            conn.close()

    @contextlib.contextmanager
    def ledger(self):
        """Read the ledger as the next run would load it.  Writes nothing."""
        with contextlib.redirect_stderr(io.StringIO()):
            with imledger.session(contextlib.nullcontext(), home=self.home) as led:
                yield led

    def wal(self):
        path = self.home / "ledger.wal"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    # -- the scenario ------------------------------------------------------------

    def scenario(self, *, at_call, after_card_write, rerun_topic=None):
        # Run A: day one queued, not stamped.
        first = self.run_child(now=at(D2, 6), rows=_rows(DAY_ONE), stale_days=3, fresh=True)
        self.assertIsNone(first["paused"], first["paused"])
        self.assertEqual(first["queued"], 3)
        state_a = (self.home / "state.json").read_bytes()
        ledger_a = (self.home / "ledger.json").read_bytes()
        self.assertEqual(json.loads(state_a)["watermark"], 6)
        self.assertEqual(self.rows(), [])

        # Run B: day two read and queued, every queued day due, KILLED in stamp N.
        stamp_order = [key(i, d) for i, d in sorted(
            [(ALICE_PHONE, D1), (CAROL_PHONE, D1), (BOB_EMAIL, D1)], key=lambda x: key(*x))]
        killed_key = stamp_order[at_call - 1]
        mark = self.kill_child(at_call=at_call, after_card_write=after_card_write,
                               now=at(D3, 6), rows=_rows(DAY_TWO), stale_days=0)
        self.assertEqual(mark["day"], D1.isoformat())
        self.assertEqual(mark["wrote"], True if after_card_write else None)

        # Nothing advanced past unfinished work.
        self.assertEqual((self.home / "state.json").read_bytes(), state_a,
                         "the watermark moved in a run that never finished")
        self.assertEqual((self.home / "ledger.json").read_bytes(), ledger_a,
                         "the ledger was compacted in a run that never finished")
        ops = self.wal()
        begins = [i for i, op in enumerate(ops) if op["op"] == "begin"]
        segment = ops[begins[-1]:]
        self.assertFalse([op for op in segment if op["op"] in ("committed", "aborted")],
                         "the killed run left a commit or abort marker")
        intents = [op["key"] for op in segment if op["op"] == "stamp_intent"]
        self.assertEqual(intents, stamp_order[:at_call], "the write-ahead intents")
        done = [op["key"] for op in segment if op["op"] == "stamp_done"]
        self.assertEqual(done, stamp_order[:at_call - 1])
        self.assertNotIn(killed_key, done)
        self.assertEqual(self.rows(), [], "a row from the killed run reached memory.db")
        # The cards carry exactly the lines written before the kill.
        written = stamp_order[:at_call] if after_card_write else stamp_order[:at_call - 1]
        for ident in (ALICE_PHONE, CAROL_PHONE, BOB_EMAIL):
            expect = 1 if key(ident, D1) in written else 0
            self.assertEqual(len(self.lines(ident, D1)), expect, ident)
            self.assertEqual(self.lines(ident, D2), [])
        # What the next load makes of it: the segment is set aside, its intents kept.
        with self.ledger() as led:
            self.assertEqual(sorted(led.open_intents()), sorted(stamp_order[:at_call]))
            self.assertFalse(any(led.is_stamped(k) for k in stamp_order))
            self.assertEqual(sorted(led.queued()), sorted(stamp_order), "day two's enqueue "
                             "belonged to the killed run and must be discarded with it")

        # Run C: the re-run.  Recovery settles each intent against its card, exactly once.
        again = self.run_child(now=at(D3, 6), rows=_rows(DAY_TWO), stale_days=0,
                               topic=rerun_topic)
        self.assertIsNone(again["paused"], again["paused"])
        on_card = len(written)
        self.assertEqual(again["recovered"]["done"], on_card)
        self.assertEqual(again["recovered"]["dropped"], at_call - on_card)
        self.assertEqual(again["stale"]["stamped"], len(ALL_DAYS) - on_card)
        self.assertEqual(again["stale"]["failures"], [])
        self.assert_every_day_filed_exactly_once()
        with self.ledger() as led:
            for k in stamp_order[:on_card]:
                self.assertTrue(led.stamp_record(k).get("recovered"), f"{k} not recovered")
            for k in stamp_order[on_card:]:
                self.assertFalse(led.stamp_record(k).get("recovered"), f"{k} recovered?")
        self.assertEqual(imledger.load_state(self.home)["watermark"], 10)
        cards_c, rows_c = self.all_card_lines(), self.rows()

        # Run D: nothing left, nothing done twice.
        last = self.run_child(now=at(D3, 6), rows=_rows(DAY_TWO), stale_days=0,
                              topic=rerun_topic)
        self.assertEqual((last["recovered"]["done"], last["recovered"]["dropped"]), (0, 0))
        self.assertEqual((last["queued"], last["stale"]["due"], last["stale"]["stamped"]),
                         (0, 0, 0))
        self.assertEqual(self.all_card_lines(), cards_c)
        self.assertEqual(self.rows(), rows_c)
        self.assertEqual(real_fixture_traces(), [])
        return again

    def assert_every_day_filed_exactly_once(self):
        with self.ledger() as led:
            self.assertEqual(led.open_intents(), {})
            self.assertEqual(led.queued(), {})
            for ident, day in ALL_DAYS:
                self.assertTrue(led.is_stamped(key(ident, day)), (ident, day))
        for ident in (ALICE_PHONE, CAROL_PHONE, BOB_EMAIL):
            for day in (D1, D2):
                expect = 1 if (ident, day) in ALL_DAYS else 0
                self.assertEqual(len(self.lines(ident, day)), expect, (ident, day))
        for name, lines in self.all_card_lines().items():
            self.assertEqual(len(lines), len(set(lines)), f"a duplicate line on {name}")
        expected = sorted((CARDS[ident].pid, day.isoformat()) for ident, day in ALL_DAYS)
        self.assertEqual(self.rows(), expected, "one memory.db row per person-day")


class TestSigkillMidStamp(CrashCase):
    def test_killed_after_the_card_write_is_settled_done_and_reprojected(self):
        report = self.scenario(at_call=1, after_card_write=True)
        # Alice's only day was the killed stamp: nothing else touched her card in run C,
        # so her row can have come only from recovery's re-projection.
        self.assertIn((ALICE.pid, D1.isoformat()), self.rows())
        self.assertGreaterEqual(report["projected"], 1)

    def test_killed_before_the_card_write_is_dropped_and_lands_once(self):
        self.scenario(at_call=1, after_card_write=False)
        with self.ledger() as led:
            record = led.stamp_record(key(ALICE_PHONE, D1))
        self.assertEqual(record.get("topic_kind"), "mechanical")

    def test_killed_after_three_stamps_settles_all_three(self):
        # Two stamps finished whole (their `done` lines sit in the uncommitted segment and
        # are discarded) and the third was killed after its write: all three are settled.
        self.scenario(at_call=3, after_card_write=True)

    def test_killed_before_the_third_write_settles_two_and_drops_one(self):
        self.scenario(at_call=3, after_card_write=False)

    def test_a_rerun_with_different_words_still_files_one_line(self):
        # The engine's own idempotency is `pointer in body`, and the pointer includes the
        # words: a re-run whose line for a day differs (a summary, not the mechanical
        # opener) would sail past it and write a SECOND line.  Only the intent that
        # survived the SIGKILL stops that, so this is the case that proves it survived.
        new_words = "Texts about the appointment time, settled for four"
        self.scenario(at_call=3, after_card_write=True, rerun_topic=new_words)
        for ident in (ALICE_PHONE, CAROL_PHONE, BOB_EMAIL):
            (line,) = self.lines(ident, D1)
            self.assertIn(f"Texts (2): {LONG_A}", line, "the killed run's words stand")
            self.assertNotIn(new_words, line)
        for ident in (CAROL_PHONE, BOB_EMAIL):
            (line,) = self.lines(ident, D2)
            self.assertIn(new_words, line, "a day first filed by the re-run has its words")


if __name__ == "__main__":
    unittest.main()
