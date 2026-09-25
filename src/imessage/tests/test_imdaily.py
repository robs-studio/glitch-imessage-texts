"""imdaily + imbrief: the morning stage's entry.  One JSON object and exit 0, on every path.

The engine runs ``imdaily.py`` once a day with cwd = this plug-in folder, stdin closed,
``GLITCH_BUDGET_S`` in the environment and a SIGKILL at the wall, and accepts only one
``{"headline": str, "items": [str, ...]}`` object on stdout (160 / 10 / 300 characters,
65,536 bytes).  A non-zero exit or anything else counts toward a latch that pauses the
feed after three.  So every test here asks the same two questions of a different path:
is the output exactly that object, and is what it says true?

What each group holds still
---------------------------
* **The real shim, for real** — the engine's own interpreter runs the real
  ``imdaily.py`` on a temp COPY of the plug-in whose ``config.local.json`` has no own
  handles.  That must hit the real "no own handles" refusal, print one valid object,
  exit 0, and leave the plug-in's private files exactly as they were.  It never runs
  in the real folder, because with the member's own handles configured that would be
  the real intake.
* **The engine agrees** — every brief passes the engine's own ``_validate_output``
  unchanged, the caps restated here match the engine's, and the engine discovers the
  descriptor with the shape the plan names.
* **Every refusal and failure path** — not a Mac, no own handles, no Full Disk Access,
  engine drift, a damaged ledger or state file, a busy lock, an unreadable store, a
  busy memory database, an unexpected exception, a run module that will not load,
  even ``SystemExit``: each a truthful line, none carrying a traceback, a message
  body or a number.
* **A populated run** on :mod:`imfixture`'s temp vault and a temp plug-in home: the
  headline counts real fields, zero parts are left out, the items name the words to
  say, and nothing identifying reaches the line.
* **The shim survives a broken helper** — an ImportError in-process; a SyntaxError,
  a stray print and garbage output in a real subprocess.
* **Size** — 10,000 queued days, 1,000 failures, 100 numbers: the line stays tiny.

Privacy: every name, number and address is invented (``555-01xx``, ``example.com``).
"""

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_HOME))

# imconfig FIRST, before anything reachable only because it put the engine on sys.path.
import imconfig  # noqa: E402

imconfig.ensure_engine_path()

import ast  # noqa: E402
import contextlib  # noqa: E402
import importlib.util  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from datetime import UTC, date, datetime, timedelta  # noqa: E402
from unittest import mock  # noqa: E402

import imbrief  # noqa: E402
import imchat  # noqa: E402
import imledger  # noqa: E402
import imrun  # noqa: E402
import imthreads  # noqa: E402
from imfixture import (  # noqa: E402
    ALICE,
    SHARED_LANDLINE,
    SpineTestCase,
    patched,
)

import people_stamp  # noqa: E402


def _import_engine_morning_reports():
    """The engine's morning runner, imported READ-ONLY for its validator and discovery.

    It puts ``.claude/scripts`` at ``sys.path[0]`` on import; the path is put back
    afterwards so the plug-in folder keeps winning every name (imconfig's law).
    """
    saved = list(sys.path)
    try:
        import morning_reports  # noqa: PLC0415
    finally:
        sys.path[:] = saved
    return morning_reports


morning_reports = _import_engine_morning_reports()

# ---------------------------------------------------------------------------
# The cast.  Invented: 555-01xx is the reserved fictional block, example.com is RFC 2606.
# ---------------------------------------------------------------------------

ZONE = UTC
ME = "+15555550199"
ALICE_PHONE = ALICE.phones[0]
FRANK = "+15555550160"   # unknown, a Contacts name, no card -> new_stub raised -> pending
CONTACTS = {ALICE_PHONE: "Fixture Alice", FRANK: "Fixture Frank", SHARED_LANDLINE: "Fixture Dan"}
LONG_A = "Can we move the appointment to four o'clock, or is the morning easier for you?"
LONG_B = "Four works fine for me, I will let the front desk know and see you then."
D1 = date(2026, 3, 2)
D2 = date(2026, 3, 3)

#: The interpreter the engine launches the stage with: ``uv run --directory
#: .claude/scripts python morning_reports.py run`` runs the scripts folder's venv.
ENGINE_PYTHON = imconfig.SCRIPTS_DIR / ".venv" / (
    "Scripts/python.exe" if sys.platform == "win32" else "bin/python3")

#: Everything a run could write into a plug-in home.  Compared by listing.  Not
#: ``config.local.json``: that is the member's own file, written only by ``check
#: --write-*`` on their yes, and they may be editing it while the suite runs.
RUN_WRITES = ("ledger", "state", "synth", "pre-backfill", "threads")

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE)


def morning(day):
    return at(day, 6)


def _raw(moment):
    delta = moment - APPLE_EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


class Chats:
    """Messages rows in ARRIVAL order (ROWIDs), handed to ``imrun.ListSource``."""

    def __init__(self):
        self.rows = []

    def say(self, chat, when, text, *, sender=None, to=None):
        """``sender`` spoke (inbound); ``sender=None`` is the member, to ``to``."""
        from_me = sender is None
        self.rows.append((len(self.rows) + 1, imchat.Message(
            chat_rowid=chat, chat_style=imchat.ONE_TO_ONE_STYLE, chat_name=None,
            is_group=False, date_raw=_raw(when), dt_local=when, is_from_me=from_me,
            handle=to if from_me else sender, text=text,
        )))

    def exchange(self, chat, day, who, hour=9):
        """They open, the member answers: two turns, two senders, real words."""
        self.say(chat, at(day, hour), LONG_A, sender=who)
        self.say(chat, at(day, hour, 5), LONG_B, to=who)

    def source(self):
        return imrun.ListSource(self.rows)


class FailingSource(imrun.ListSource):
    """A store that fails mid-read with whatever it is given."""

    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def messages_between(self, since, until):
        raise self.exc


def _run_listing(home):
    """``{relative path: (mtime_ns, size) | "dir"}`` for everything a run could write.

    A folder is listed as present, never by its mtime: another suite's decoy that
    comes and goes under ``threads/`` moves a folder's mtime without writing a file.
    """
    found = {}
    for top in sorted(Path(home).iterdir()):
        if not top.name.startswith(RUN_WRITES):
            continue
        for path in [top, *sorted(top.rglob("*"))] if top.is_dir() else [top]:
            key = path.relative_to(home).as_posix()
            if path.is_dir():
                found[key] = "dir"
            else:
                st = path.lstat()
                found[key] = (st.st_mtime_ns, st.st_size)
    return found


# ---------------------------------------------------------------------------
# The safety net: no test in this file may ever run the member's REAL intake.
# Every run here points at a temp home (and, in a subprocess, a temp COPY of the
# plug-in).  If any of them reached the real home, this goes red, naming what changed.
# ---------------------------------------------------------------------------

_REAL_HOME_BEFORE: dict = {}


def setUpModule():
    _REAL_HOME_BEFORE.update(_run_listing(PLUGIN_HOME))


def tearDownModule():
    after = _run_listing(PLUGIN_HOME)
    changed = sorted(k for k in set(_REAL_HOME_BEFORE) | set(after)
                     if _REAL_HOME_BEFORE.get(k) != after.get(k))
    if changed:
        raise AssertionError(f"a test wrote into the REAL plug-in home: {changed[:20]}")


# ---------------------------------------------------------------------------
# The one check every path's output must pass.
# ---------------------------------------------------------------------------


def assert_one_object(case, raw):
    """``raw`` (stdout bytes or a rendered line) is exactly one valid brief.  Returns it."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    case.assertLess(len(raw), morning_reports.OUTPUT_BYTE_CAP // 8, "far under the byte cap")
    text = raw.decode("ascii")  # ASCII only: no stdout encoding can break it
    case.assertEqual(len(text.rstrip("\n").splitlines()), 1, f"not one line: {text!r}")
    data = json.loads(text)
    case.assertEqual(sorted(data), ["headline", "items"])
    case.assertIsInstance(data["headline"], str)
    case.assertTrue(data["headline"].startswith("Texts"), data["headline"])
    case.assertLessEqual(len(data["headline"]), morning_reports.MAX_HEADLINE_CHARS)
    case.assertIsInstance(data["items"], list)
    case.assertLessEqual(len(data["items"]), morning_reports.MAX_ITEMS)
    for item in data["items"]:
        case.assertIsInstance(item, str)
        case.assertTrue(item.strip())
        case.assertLessEqual(len(item), morning_reports.MAX_ITEM_CHARS)
    # The engine's own validator accepts it, and has nothing to cut or flag.
    validated, why = morning_reports._validate_output(raw)
    case.assertEqual(why, "")
    case.assertEqual(validated, data)
    for s in [data["headline"], *data["items"]]:
        case.assertNotIn("Traceback", s)
        case.assertIsNone(re.search(r"\d{7,}", s), f"a number-shaped run in {s!r}")
        case.assertNotIn("@", s)
        case.assertNotIn("people queue", s)   # the member's ruling, 2026-09-24
        case.assertNotIn("—", s)
    return data


def _load_shim(name="imdaily_under_test"):
    """``imdaily.py`` as a module, without running it."""
    spec = importlib.util.spec_from_file_location(name, PLUGIN_HOME / "imdaily.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_shim_in(folder, env=None):
    """Run ``<folder>/imdaily.py`` the way the engine does: its folder as cwd, stdin
    closed, its own session (so a kill would take the tree), the budget in the env."""
    return subprocess.run(
        [str(ENGINE_PYTHON), "imdaily.py"], cwd=str(folder),
        env={**os.environ, "GLITCH_BUDGET_S": "20.0", **(env or {})},
        stdin=subprocess.DEVNULL, capture_output=True, timeout=120,
        start_new_session=(sys.platform != "win32"),
    )


# ---------------------------------------------------------------------------
# 1. The real shim, in the real folder, with the engine's interpreter.
# ---------------------------------------------------------------------------


def _temp_copy(case):
    """A throwaway Brain layout holding a COPY of this plug-in: ``(copy home, engine?)``.

    ``<tmp>/brain/_local/imessage/`` gets every top-level ``*.py`` and ``config.json``
    of the real plug-in, plus a ``config.local.json`` with NO own handles, so the
    copy's run must stop at the own-handles gate.  ``<tmp>/brain/.claude/scripts`` is
    a symlink to the real engine, so the copy imports the engine exactly as the real
    stage does; and because the engine's ``config`` roots itself on its UNRESOLVED
    ``__file__``, even its own paths (``memory.db``, the vault) land inside the temp
    tree.  Every path the copy's modules derive comes from their own ``__file__``, so
    nothing here can reach the member's real home.  Where a symlink cannot be made
    (Windows without the privilege), the copy runs without the engine, and the gate,
    which comes before anything needs the engine, answers the same.
    """
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    brain = Path(tmp.name).resolve() / "brain"
    home = brain / "_local" / PLUGIN_HOME.name
    home.mkdir(parents=True)
    (brain / ".claude").mkdir()
    try:
        (brain / ".claude" / "scripts").symlink_to(imconfig.SCRIPTS_DIR, target_is_directory=True)
        engine = True
    except OSError:
        engine = False
    for src in sorted(PLUGIN_HOME.glob("*.py")):
        shutil.copyfile(src, home / src.name)
    shutil.copyfile(PLUGIN_HOME / "config.json", home / "config.json")
    (home / "config.local.json").write_text(
        json.dumps({"own_handles": [], "never_ingest": []}) + "\n", encoding="utf-8")
    return home, engine


@unittest.skipUnless(ENGINE_PYTHON.is_file(), "the engine interpreter is not installed")
class TestTheShimForReal(unittest.TestCase):
    """The shim run as the engine runs it, on a temp COPY of the plug-in, never the real one.

    The member's own ``config.local.json`` carries their handles, so the real shim would
    be the real intake: it would write the real ledger, state and transcripts and their
    real ``memory.db``.  That first real run is the member's own, never a test's.
    """

    def test_the_shim_refuses_truthfully_and_the_real_home_is_untouched(self):
        home, engine = _temp_copy(self)
        # Belt and braces before launching anything: the copy's merged config must
        # refuse.  If it would not, fail here and launch nothing.
        cfg = imconfig.load_config(path=home / "config.json",
                                   local_path=home / "config.local.json")
        self.assertIsNone(imconfig.require_own_handles(cfg), "the copy would run for real")
        real_before = _run_listing(PLUGIN_HOME)
        proc = _run_shim_in(home, env={"PYTHONDONTWRITEBYTECODE": "1"})
        err = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, err)
        data = assert_one_object(self, proc.stdout)
        self.assertEqual(data["headline"], imbrief._PAUSED["no_own_handles"][0])
        self.assertEqual(len(data["items"]), 1)
        self.assertIn("imessage.py check", data["items"][0])
        self.assertNotIn("Traceback", err)
        if engine:
            self.assertNotIn("would not import", err, "the copy did not reach the engine")
        self.assertEqual(_run_listing(home), {}, "the refused run wrote into its own home")
        self.assertEqual(_run_listing(PLUGIN_HOME), real_before, "the REAL home changed")
        self.assertFalse((home.parents[1] / ".claude" / "data").exists(),
                         "the engine made its data folder: something opened a database")


class TestTheSafetyNetBites(unittest.TestCase):
    def test_the_listing_sees_every_file_a_run_could_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.local.json").write_text("{}", encoding="utf-8")
            (home / "imrun.py").write_text("", encoding="utf-8")
            empty = _run_listing(home)
            self.assertEqual(empty, {}, "the member's config and code are not run output")
            for name in ("ledger.json", "ledger.wal", "ledger.lock", "state.json",
                         "state.json.x1.tmp", "threads/2026/a.txt", "synth/claim-1.json"):
                path = home / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
                self.assertIn(name, _run_listing(home), name)


class TestTheEngineAgrees(unittest.TestCase):
    def test_the_caps_restated_here_are_the_engines(self):
        self.assertEqual(imbrief.MAX_HEADLINE_CHARS, morning_reports.MAX_HEADLINE_CHARS)
        self.assertEqual(imbrief.MAX_ITEMS, morning_reports.MAX_ITEMS)
        self.assertEqual(imbrief.MAX_ITEM_CHARS, morning_reports.MAX_ITEM_CHARS)
        self.assertLess(imbrief.MAX_LINE_BYTES, morning_reports.OUTPUT_BYTE_CAP // 2)

    def test_the_engine_discovers_the_descriptor_as_the_plan_declares_it(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            found = {a.name: a for a in morning_reports.discover()}
        self.assertIn("imessage", found)
        self.assertNotIn("imessage:", err.getvalue(), "the engine warned about it")
        adapter = found["imessage"]
        self.assertEqual(adapter.stage_kinds, ("morning",))
        self.assertEqual(adapter.entry.argv, ("imdaily.py",))
        self.assertEqual(adapter.timeout_s, morning_reports.MAX_TIMEOUT_S)
        self.assertEqual(adapter.folder, PLUGIN_HOME.resolve())
        self.assertEqual(adapter.script, (PLUGIN_HOME / "imdaily.py").resolve())
        declared = json.loads((PLUGIN_HOME / "capability.json").read_text(encoding="utf-8"))
        self.assertEqual(declared["name"], PLUGIN_HOME.name)   # casefold-match or it never runs
        self.assertEqual((declared["kind"], declared["feeds_into"], declared["way_in"]),
                         ("provider", "person", "bring my texts in"))
        timeout = declared["stages"]["morning"]["timeout_s"]
        self.assertIs(type(timeout), int)   # a bool or a float skips the whole stage
        self.assertGreater(timeout, 0)

    def test_both_fallback_lines_are_valid_briefs(self):
        assert_one_object(self, imbrief.FALLBACK_LINE)
        assert_one_object(self, _load_shim().FALLBACK)


# ---------------------------------------------------------------------------
# 2. Every refusal and failure path: one truthful line, nothing written.
# ---------------------------------------------------------------------------


class BriefCase(SpineTestCase):
    """A fixture vault + DB, a temp plug-in home, in-memory Messages rows."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.threads = self.home / "threads"
        self.chats = Chats()
        self.cfg = {**imconfig.DEFAULTS, "own_handles": [ME], "never_ingest": []}
        env = {k: v for k, v in os.environ.items() if k != "GLITCH_BUDGET_S"}
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def brief(self, now=None, **kw):
        kw.setdefault("source", self.chats.source())
        kw.setdefault("cfg", self.cfg)
        kw.setdefault("contacts", CONTACTS)
        with contextlib.redirect_stderr(io.StringIO()):
            line = imbrief.brief_line(now=now or morning(D2), home=self.home,
                                      threads_dir=self.threads, db_path=self.fx.db_path, **kw)
        return assert_one_object(self, line)

    def places(self):
        return {str(p): p.read_bytes() for p in sorted(self.home.rglob("*")) if p.is_file()}


class TestRefusals(BriefCase):
    def assert_refused(self, reason, **kw):
        self.chats.exchange(1, D1, ALICE_PHONE)
        before = self.places()
        data = self.brief(**kw)
        self.assertEqual(data["headline"], imbrief._PAUSED[reason][0])
        self.assertEqual(self.places(), before, f"{reason} wrote something")
        self.assertFalse(self.threads.exists())
        return data

    def test_not_a_mac(self):
        with mock.patch.object(imchat, "_is_macos", lambda: False):
            data = self.assert_refused("not_mac", source=imrun.ChatDbSource(self.root / "x.db"))
        self.assertEqual(data["items"], [])

    def test_no_own_handles(self):
        self.cfg["own_handles"] = []
        data = self.assert_refused("no_own_handles")
        self.assertIn("imessage.py check", data["items"][0])

    def test_no_full_disk_access_names_the_app(self):
        sentence = ("texts: this Mac won't let Fixture Terminal near the Messages database. "
                    "Grant it Full Disk Access in System Settings, then run this again.")
        with mock.patch.object(imchat, "_is_macos", lambda: True), \
                mock.patch.object(imchat, "has_access", lambda path=None: (False, sentence)):
            data = self.assert_refused("no_access",
                                       source=imrun.ChatDbSource(self.root / "x.db"))
        self.assertEqual(data["items"], [sentence[len("texts: "):].replace("this", "This", 1)])

    def test_engine_drift(self):
        def changed(person_path, *, source):  # a signature the plug-in was not built for
            return None

        with patched(people_stamp, "stamp_interaction", changed):
            data = self.assert_refused("engine_changed")
        self.assertTrue(data["items"][0].startswith("The engine changed"), data["items"])

    def test_a_damaged_ledger_names_the_file_and_the_fix_within_the_cap(self):
        (self.home / "ledger.lock").write_bytes(b"")
        (self.home / "ledger.json").write_text("{not json", encoding="utf-8")
        data = self.assert_refused("ledger_damaged")
        self.assertTrue(data["items"][0].startswith("ledger.json is damaged ("), data["items"])
        self.assertIn("Repair or restore that file by hand", data["items"][0])
        self.assertNotIn(str(self.home), data["items"][0])

    def test_a_damaged_state_file(self):
        (self.home / "ledger.lock").write_bytes(b"")
        (self.home / "state.json").write_text("[1, 2", encoding="utf-8")
        data = self.assert_refused("ledger_damaged")
        self.assertTrue(data["items"][0].startswith("state.json is damaged ("), data["items"])

    def test_a_state_file_this_version_does_not_recognise(self):
        (self.home / "ledger.lock").write_bytes(b"")
        (self.home / "state.json").write_text('{"watermark": 812345678901234567}\n',
                                              encoding="utf-8")
        data = self.assert_refused("state_unrecognised")
        self.assertIn("move state.json aside", data["items"][0])

    def test_the_ledger_lock_is_busy(self):
        with imledger.default_lock(self.home), mock.patch.object(imrun, "LOCK_TIMEOUT_S", 0.2):
            data = self.assert_refused("busy")
        self.assertEqual(data["items"], [])


class TestFailuresMidRun(BriefCase):
    def test_an_unreadable_store_undoes_the_run(self):
        data = self.brief(source=FailingSource(imrun.SourceUnreadable("DatabaseError")))
        self.assertEqual(data["headline"], imbrief._PAUSED["unreadable"][0])
        self.assertFalse((self.home / "state.json").exists(), "the watermark moved")

    def test_a_busy_memory_database_undoes_the_run(self):
        data = self.brief(source=FailingSource(sqlite3.OperationalError("database is locked")))
        self.assertEqual(data["headline"], imbrief._PAUSED["memory_busy"][0])
        self.assertFalse((self.home / "state.json").exists(), "the watermark moved")

    def test_an_unexpected_exception_is_named_never_quoted(self):
        self.chats.exchange(1, D1, ALICE_PHONE)
        boom = KeyError(f"{FRANK} bob@fixture.example.com {LONG_A}")
        with mock.patch.object(imthreads, "group", side_effect=boom):
            data = self.brief()
        self.assertIn("unexpected error (KeyError)", data["headline"])
        line = json.dumps(data)
        for secret in (FRANK, "fixture.example.com", "appointment"):
            self.assertNotIn(secret, line)
        self.assertFalse((self.home / "state.json").exists(), "the watermark moved")
        with contextlib.redirect_stderr(io.StringIO()):
            with imledger.session(contextlib.nullcontext(), home=self.home) as led:
                self.assertEqual(led.queued(), {}, "the failed run's queue survived")

    def test_system_exit_inside_the_run_still_yields_one_object(self):
        with mock.patch.object(imrun, "daily", side_effect=SystemExit(3)):
            data = self.brief()
        self.assertIn("(SystemExit)", data["headline"])

    def test_a_run_module_that_will_not_load(self):
        with mock.patch.dict(sys.modules, {"imrun": None}):
            data = self.brief()
        # sys.modules[name] = None raises ModuleNotFoundError, ImportError's own subclass
        self.assertIn("run module would not load (ModuleNotFoundError)", data["headline"])
        self.assertIn("imessage.py status", data["items"][0])

    def test_a_pause_reason_this_version_does_not_know(self):
        paused = {"paused": {"reason": "something_new",
                             "sentence": "texts paused: something new happened"}}
        with mock.patch.object(imrun, "daily", return_value=paused):
            data = self.brief()
        self.assertEqual(data["headline"], "Texts paused: Something new happened")

    def test_a_brief_that_cannot_render_prints_the_fallback(self):
        with mock.patch.object(imbrief, "finalise", side_effect=ValueError("x")):
            data = self.brief()
        self.assertEqual(data, json.loads(imbrief.FALLBACK_LINE))

    def test_main_prints_exactly_one_line_and_returns_zero(self):
        out = io.StringIO()
        with mock.patch.object(imbrief, "morning", side_effect=RuntimeError("x")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = imbrief.main()
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().endswith("\n"))
        assert_one_object(self, out.getvalue())


# ---------------------------------------------------------------------------
# 3. A run that went ahead: the counts are real, the words are the ones to say.
# ---------------------------------------------------------------------------


class TestAPopulatedRun(BriefCase):
    def seed(self):
        self.chats.exchange(1, D1, ALICE_PHONE)       # a card carries it -> queued
        self.chats.exchange(3, D1, FRANK)             # unknown -> the texts review list
        self.chats.exchange(5, D1, SHARED_LANDLINE)   # on two cards -> ambiguous, held

    def assert_nothing_identifying(self, data):
        line = json.dumps(data)
        for secret in (ALICE_PHONE, FRANK, SHARED_LANDLINE, "5555550", "Fixture",
                       "appointment", "front desk"):
            self.assertNotIn(secret, line)

    def test_the_first_morning(self):
        self.seed()
        data = self.brief(morning(D2))
        # The member's ruling (2026-09-24): a new number waits on the texts review list and
        # daily raises no people proposal, so the line counts from awaiting_review.
        self.assertEqual(
            data["headline"],
            "Texts: 3 conversation-days in, 1 waiting for a summary, 2 numbers to review "
            "(say 'numbers to review')")
        self.assertEqual(data["items"], [
            "2 numbers on your texts review list, 2 of them new this run, holding 2 days of "
            "texts until you decide.",
            "1 day of texts is waiting for a summary, the oldest from 2026-03-02; say "
            "'summarise my texts'. A day still waiting after 3 days in the queue files with "
            "its plain line instead.",
            "This was the first run, so it read yesterday only; older days need a backfill.",
        ])
        self.assert_nothing_identifying(data)

    def test_three_days_later_the_stale_day_is_filed_and_zero_parts_are_left_out(self):
        self.seed()
        self.brief(morning(D2))
        data = self.brief(morning(D2) + timedelta(days=3))
        self.assertEqual(data["headline"], "Texts: 1 filed to a card, 2 numbers to review "
                                           "(say 'numbers to review')")
        self.assertEqual(data["items"], [
            "2 numbers on your texts review list, holding 2 days of texts until you decide."])
        self.assert_nothing_identifying(data)

    def test_nothing_new_says_why(self):
        # one short text that never clears the floor: read, nothing kept, watermark placed
        self.chats.say(1, at(D1, 9), "ok", sender=ALICE_PHONE)
        first = self.brief(morning(D2))
        self.assertEqual(first["headline"], "Texts: nothing new to file.")
        self.assertEqual(first["items"], ["Why: none of what was read cleared the floor for "
                                          "a real conversation."])
        data = self.brief(morning(D2) + timedelta(days=1))
        self.assertEqual(data["headline"], "Texts: nothing new to file.")
        self.assertEqual(data["items"],
                         ["Why: no new texts have arrived since the last run."])

    def test_a_day_that_would_not_file_is_surfaced_without_its_detail(self):
        self.chats.exchange(1, D1, ALICE_PHONE)
        self.brief(morning(D2))
        refusal = "couldn't back fixture-alice.md up before rewriting it (disk full)"

        def refusing(person_path, *, source, occurred_at, direction, topic=None,
                     legacy_topic=None, link=None, note=None, source_meeting_id=None,
                     conn=None):
            # the engine's exact signature, so the drift gate still passes
            return people_stamp.StampResult(stamped=False, detail=refusal)

        with mock.patch.object(people_stamp, "stamp_interaction", refusing):
            data = self.brief(morning(D2) + timedelta(days=3))
        self.assertEqual(data["headline"], "Texts: 1 waiting for a summary")
        self.assertEqual(data["items"][0],
                         "1 day would not file onto its card and stays queued to try again "
                         "next run; ask for your texts status to see why.")
        self.assertNotIn("fixture-alice", json.dumps(data))

    def test_a_run_out_of_time_says_so_instead_of_nothing_new(self):
        self.seed()
        self.cfg["budget_margin_s"] = 0
        data = self.brief(morning(D2), budget_s=0)
        self.assertEqual(data["headline"],
                         "Texts: the run ran out of time before anything came in; the next "
                         "run carries on from the same place.")
        self.assertEqual(data["items"], [])

    def test_ten_thousand_queued_days_still_make_a_tiny_line(self):
        ident = [f"+1555555{i:04d}" for i in range(100)]
        with contextlib.redirect_stderr(io.StringIO()):
            with imledger.session(contextlib.nullcontext(), home=self.home) as led:
                led.begin_run("seed")
                for n, who in enumerate(ident):
                    led.set_identifier(who, state="held", hold_reason="new")
                    for d in range(100):
                        day = D1 - timedelta(days=d)
                        key = imthreads.ledger_key(who, imthreads.DAY_GRAIN, day)
                        led.enqueue({"key": key, "identifier": who, "day": day.isoformat(),
                                     "person_id": "prs_fxalice2",
                                     "queued_at": morning(D1).isoformat()})
                        if d < 10:
                            led.record_failure(key, f"refused {who} {n}")
                led.commit_run("seed")
                led.compact()
        self.cfg["synth_stale_days"] = 100_000   # none due: this is about the line, not filing
        data = self.brief(morning(D2))
        self.assertEqual(
            data["headline"],
            "Texts: 10,000 waiting for a summary, 100 numbers to review (say 'numbers to "
            "review')")
        self.assertEqual(data["items"][1], "100 numbers on your texts review list.")
        self.assertIn("1,000 days would not file", data["items"][0])
        self.assertLess(len(json.dumps(data)), 2_000)


class TestTheBudget(unittest.TestCase):
    def setUp(self):
        self.seen = {}

        def fake_daily(**kw):
            self.seen.update(kw)
            return {"paused": {"reason": "busy", "sentence": "x"}}

        patch = mock.patch.object(imrun, "daily", side_effect=fake_daily)
        patch.start()
        self.addCleanup(patch.stop)

    def run_with(self, budget, spent=0.0):
        env = {k: v for k, v in os.environ.items() if k != "GLITCH_BUDGET_S"}
        if budget is not None:
            env["GLITCH_BUDGET_S"] = budget
        # as if the process had started `spent` seconds ago
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(imbrief, "_LOADED_AT", time.monotonic() - spent):
            imbrief.morning(cfg=dict(imconfig.DEFAULTS))
        return self.seen["budget_s"]

    def test_the_grant_less_what_is_spent_and_the_reserve(self):
        budget = self.run_with("20.0", spent=4.0)
        self.assertLessEqual(budget, 20.0 - 4.0 - imbrief.BRIEF_RESERVE_S)
        self.assertGreater(budget, 20.0 - 4.0 - imbrief.BRIEF_RESERVE_S - 1.0)

    def test_a_grant_already_spent_is_zero_not_negative(self):
        self.assertEqual(self.run_with("2.0", spent=5.0), 0.0)

    def test_no_grant_means_no_clock(self):
        self.assertIsNone(self.run_with(None))

    def test_an_unreadable_grant_is_left_to_imrun(self):
        self.assertIsNone(self.run_with("soon"))

    def test_an_explicit_budget_passes_through(self):
        with mock.patch.dict(os.environ, {"GLITCH_BUDGET_S": "20.0"}):
            imbrief.morning(cfg=dict(imconfig.DEFAULTS), budget_s=7.5)
        self.assertEqual(self.seen["budget_s"], 7.5)

    def test_little_time_left_skips_the_ledger_read(self):
        report = {"paused": None, "conversation_days": 2, "queue": {"depth": 2},
                  "awaiting_review": 1, "awaiting_yes": 1}
        with mock.patch.object(imrun, "daily", return_value=report), \
                mock.patch.object(imrun, "status") as status, \
                mock.patch.dict(os.environ, {"GLITCH_BUDGET_S": "0.5"}), \
                mock.patch.object(imbrief, "_LOADED_AT", time.monotonic()), \
                contextlib.redirect_stderr(io.StringIO()):
            brief = imbrief.morning(cfg=dict(imconfig.DEFAULTS))
        status.assert_not_called()
        self.assertEqual(brief["headline"],
                         "Texts: 2 conversation-days in, 2 waiting for a summary, "
                         "1 number to review (say 'numbers to review')")


# ---------------------------------------------------------------------------
# 4. The caps and the scrub, and the rarer items.
# ---------------------------------------------------------------------------


class TestCapsAndScrub(unittest.TestCase):
    def test_every_cap_holds_and_cuts_at_a_word(self):
        brief = imbrief.finalise("Texts: " + "word " * 100, ["item " * 200] * 25)
        self.assertLessEqual(len(brief["headline"]), imbrief.MAX_HEADLINE_CHARS)
        self.assertTrue(brief["headline"].endswith("word…"), brief["headline"][-12:])
        self.assertEqual(len(brief["items"]), imbrief.MAX_ITEMS)
        for item in brief["items"]:
            self.assertLessEqual(len(item), imbrief.MAX_ITEM_CHARS)
            self.assertTrue(item.endswith("item…"))
        assert_one_object(self, imbrief.render(brief))

    def test_the_scrub_masks_handles_and_flattens_but_keeps_dates_and_counts(self):
        text = ("Texts: +15555550142 and 5555550143 and bob@fixture.example.com\non "
                "2026-09-24 — 10,000 days done\t.")
        self.assertEqual(
            imbrief._scrub(text),
            "Texts: [number] and [number] and [address] on 2026-09-24; 10,000 days done .")

    def test_empty_items_are_dropped_and_a_blank_headline_still_starts_texts(self):
        brief = imbrief.finalise("", ["", "  ", "real"])
        self.assertEqual(brief, {"headline": "Texts:", "items": ["real"]})

    def test_a_too_big_line_is_refused_for_the_fallback(self):
        with mock.patch.object(imbrief, "MAX_LINE_BYTES", 10), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(imbrief.render({"headline": "Texts: hello", "items": []}),
                             imbrief.FALLBACK_LINE)

    def test_the_rarer_items(self):
        report = {
            "conversation_days": 1, "ledger_missing": True, "failures": 0,
            "stale": {"stamped": 0, "remaining": 4}, "queue": {"depth": 0},
            "recovered": {"done": 1, "dropped": 1},
            "watermark": {"store_reset": True, "first_run": True},
        }
        brief = imbrief.compose(report, None, imconfig.DEFAULTS)
        self.assertEqual(brief["headline"], "Texts: 1 conversation-day in")
        self.assertEqual(brief["items"], [
            "Warning: the texts ledger is missing but its watermark is not, so days queued "
            "or held before it went are no longer known; restore ledger.json if you have a "
            "copy.",
            "4 more days were due to file but left for the next run, which files them first.",
            "The previous run was cut off mid-filing; this run settled its 2 unfinished "
            "filings against the cards.",
            "Your Messages history looks rebuilt, so I read yesterday afresh; older days need "
            "a backfill.",
        ])

    def test_numbers_to_review_count_the_review_list_and_never_the_people_queue(self):
        report = {"awaiting_review": 3, "new_for_review": 1, "awaiting_yes": 2}
        ledger = {"exists": True, "held_units": 4, "awaiting_yes": 2, "awaiting_review": 3,
                  "identifiers_by_state": {"held": 2, "ambiguous": 1, "attach_pending": 2}}
        brief = imbrief.compose(report, ledger, imconfig.DEFAULTS)
        self.assertEqual(brief["headline"],
                         "Texts: 3 numbers to review (say 'numbers to review')")
        self.assertEqual(brief["items"], [
            "3 numbers on your texts review list, 1 of them new this run, holding 4 days of "
            "texts until you decide."])
        self.assertNotIn("queue", json.dumps(brief))

    def test_only_the_people_queue_waiting_says_nothing_about_numbers(self):
        brief = imbrief.compose({"awaiting_review": 0, "awaiting_yes": 2}, None,
                                imconfig.DEFAULTS)
        self.assertEqual(brief, {"headline": "Texts: nothing new to file.", "items": []})

    def test_one_new_number(self):
        brief = imbrief.compose({"awaiting_review": 1, "new_for_review": 1}, None,
                                imconfig.DEFAULTS)
        self.assertEqual(brief["headline"],
                         "Texts: 1 number to review (say 'numbers to review')")
        self.assertEqual(brief["items"], ["1 number on your texts review list, new this run."])

    def test_without_the_report_field_the_ledger_count_is_used(self):
        brief = imbrief.compose({}, {"exists": True, "awaiting_review": 2}, imconfig.DEFAULTS)
        self.assertEqual(brief["headline"],
                         "Texts: 2 numbers to review (say 'numbers to review')")

    def test_days_a_summary_pass_already_holds_are_not_asked_for_again(self):
        report = {"queue": {"depth": 3, "oldest_day": "2026-09-21"}}
        ledger = {"exists": True, "unclaimed": {"depth": 1}}
        item = imbrief.compose(report, ledger, imconfig.DEFAULTS)["items"][0]
        self.assertIn("(2 already taken by a summary pass); say 'summarise my texts'.", item)
        ledger["unclaimed"]["depth"] = 0
        item = imbrief.compose(report, ledger, imconfig.DEFAULTS)["items"][0]
        self.assertIn("(3 already taken by a summary pass).", item)
        self.assertNotIn("summarise my texts", item)

    def test_a_report_of_the_wrong_shape_degrades_instead_of_raising(self):
        brief = imbrief.compose({"conversation_days": "many", "queue": [], "stale": None,
                                 "stopped": "budget", "failures": -3, "awaiting_yes": True},
                                {"exists": True, "identifiers_by_state": "x"}, {})
        self.assertEqual(brief, {"headline": "Texts: nothing new to file.", "items": []})


# ---------------------------------------------------------------------------
# 5. The shim survives a broken helper.
# ---------------------------------------------------------------------------


class TestTheShim(unittest.TestCase):
    def test_it_stays_a_shim(self):
        tree = ast.parse((PLUGIN_HOME / "imdaily.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported - {"__future__", "contextlib", "io", "json", "sys", "pathlib"},
                         {"imbrief"})

    def test_an_import_error_in_imbrief_still_prints_one_object(self):
        shim = _load_shim()
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"imbrief": None}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()) as err:
            code = shim.run()
        self.assertEqual(code, 0)
        self.assertEqual(assert_one_object(self, out.getvalue()), json.loads(shim.FALLBACK))
        self.assertIn("ModuleNotFoundError", err.getvalue())   # ImportError's subclass

    def test_the_shim_passes_imbriefs_line_through_untouched(self):
        shim = _load_shim()
        line = imbrief.render({"headline": "Texts: 2 conversation-days in", "items": ["one"]})
        out = io.StringIO()
        with mock.patch.object(imbrief, "brief_line", return_value=line), \
                contextlib.redirect_stdout(out):
            code = shim.run()
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), line + "\n")


@unittest.skipUnless(ENGINE_PYTHON.is_file(), "the engine interpreter is not installed")
class TestTheShimWithABrokenHelperForReal(unittest.TestCase):
    """A copy of the real shim beside a deliberately broken ``imbrief.py``, run for real."""

    def run_beside(self, imbrief_source):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        shutil.copyfile(PLUGIN_HOME / "imdaily.py", folder / "imdaily.py")
        (folder / "imbrief.py").write_text(imbrief_source, encoding="utf-8")
        proc = _run_shim_in(folder, env={"PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return assert_one_object(self, proc.stdout), proc.stderr.decode("utf-8", "replace")

    def test_a_syntax_error(self):
        data, err = self.run_beside("def main(:\n    pass\n")
        self.assertEqual(data, json.loads(_load_shim().FALLBACK))
        self.assertIn("SyntaxError", err)

    def test_a_stray_print_is_kept_off_stdout(self):
        source = (
            "import json\n"
            "print('a stray line from some helper')\n"
            "def main():\n"
            "    print(json.dumps({'headline': 'Texts: fine', 'items': []}))\n"
            "    return 0\n"
        )
        data, err = self.run_beside(source)
        self.assertEqual(data, {"headline": "Texts: fine", "items": []})
        self.assertIn("a stray line from some helper", err)

    def test_garbage_output_and_a_raise(self):
        data, err = self.run_beside("def main():\n    print('{not json')\n    raise OSError('x')\n")
        self.assertEqual(data, json.loads(_load_shim().FALLBACK))
        self.assertIn("OSError", err)


if __name__ == "__main__":
    unittest.main()
