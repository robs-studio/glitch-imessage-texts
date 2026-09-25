"""The ledger: what stands between an at-least-once scheduler and a double-filed day.

The morning stage re-runs an interrupted adapter by design and kills an overrunning
one with a SIGKILL that nothing can catch.  Every rule this suite holds still is one
whose failure is SILENT: an intent forgotten after a crash re-files a day, an
uncommitted change replayed moves a unit that never moved, a corrupt ledger read as
empty re-files a year of days, and a watermark written as a float skips messages at
the boundary.  None of that raises, so this file is the only thing that notices.

The SIGKILL test is real: a child process writes intents in a loop, is killed with
``SIGKILL`` mid-stream, and every intent it reported as written must come back.

PRIVACY
-------
Fixture identifiers only: the ``555-01xx`` fictional block and ``example.com``
(RFC 2606).  Every test writes into its own temp directory.  The real plug-in
folder's ``ledger.json`` / ``ledger.wal`` / ``state.json`` are never created or
touched, and one test pins that.
"""

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_HOME))

# The plug-in's own modules first, before anything reachable only because
# imconfig put `.claude/scripts` on sys.path.
import imconfig  # noqa: E402,F401
import imledger  # noqa: E402
import imthreads  # noqa: E402

# isort: split
import contextlib  # noqa: E402
import errno  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import textwrap  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from datetime import UTC, date, datetime, timedelta  # noqa: E402
from typing import Any  # noqa: E402
from unittest import mock  # noqa: E402

IDENT = "+15555550101"
IDENT2 = "+15555550102"
IDENT3 = "friend@example.com"
DAY = date(2026, 9, 14)
BASE = 1_790_000_000.0  # a fixed epoch "now" for lease tests
LINK = "[thread](_local/imessage/threads/2026/2026-09-14-friend-0101.txt)"


def key(ident: str = IDENT, day: date = DAY) -> str:
    return imthreads.ledger_key(ident, imthreads.DAY_GRAIN, day)


def intent_rec(topic: str = "Texts (3): see you at noon", **extra: Any) -> dict[str, Any]:
    return {"person_id": "p-0001", "turns": 3, "topic": topic, "link": LINK, **extra}


def unit(ident: str = IDENT, day: date = DAY, **extra: Any) -> dict[str, Any]:
    return {
        "key": key(ident, day),
        "identifier": ident,
        "person_id": None,
        "day": day.isoformat(),
        "links": [LINK],
        "direction": "mutual",
        "turns": 4,
        "topic": "Texts (4): are we still on for Saturday",
        **extra,
    }


class LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="imledger-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.p = imledger.paths(self.home)

    def open(self, timeout: float = 2.0) -> Any:
        return imledger.session(
            lambda: imledger.default_lock(self.home, timeout=timeout), home=self.home
        )

    @contextlib.contextmanager
    def quiet(self) -> Any:
        """Capture stderr; yields the buffer."""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            yield buf

    def wal_records(self) -> list[dict[str, Any]]:
        raw = self.p.wal.read_bytes() if self.p.wal.exists() else b""
        return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]

    def snapshot(self, led: imledger.Ledger, claim_ids: tuple[str, ...] = ()) -> dict[str, Any]:
        return {
            "stamped": json.loads(json.dumps(dict(led.stamped))),
            "open_intents": led.open_intents(),
            "identifiers": led.all_identifiers(),
            "held": {i: led.held(i) for i in (IDENT, IDENT2, IDENT3)},
            "queued": led.queued(),
            "claims": {c: led.claim_of(c) for c in claim_ids},
            "failures": led.failures(),
            "counts": led.counts(),
        }


# ---------------------------------------------------------------------------
# Keys.
# ---------------------------------------------------------------------------


class TestKeys(LedgerTestCase):
    def test_the_three_part_key_comes_from_imthreads(self) -> None:
        k = key()
        self.assertEqual(k, f"{IDENT}|day|2026-09-14")
        self.assertEqual(imthreads.parse_ledger_key(k), (IDENT, imthreads.DAY_GRAIN, DAY))
        with self.open() as led, led.run("r1"):
            led.stamp_intent(k, intent_rec())
            self.assertTrue(led.stamp_done(k))
        with self.open() as led:
            self.assertTrue(led.is_stamped(k))
            self.assertIn(k, led.stamped)

    def test_week_two_part_and_non_canonical_keys_are_refused_everywhere(self) -> None:
        bad = {
            "week": f"{IDENT}|week|2026-09-14",
            "two-part": f"{IDENT}|2026-09-14",
            "basic-iso": f"{IDENT}|day|20260914",
            "not-a-date": f"{IDENT}|day|someday",
        }
        with self.open() as led, led.run("r1"):
            for label, k in bad.items():
                with self.subTest(label):
                    with self.assertRaises(ValueError):
                        led.stamp_intent(k, intent_rec())
                    with self.assertRaises(ValueError):
                        led.stamp_done(k)
                    with self.assertRaises(ValueError):
                        led.enqueue({**unit(), "key": k})
                    with self.assertRaises(ValueError):
                        led.hold(IDENT, {**unit(), "key": k})
                    with self.assertRaises(ValueError):
                        led.record_failure(k, "x")
        with self.open() as led:
            self.assertEqual(led.counts()["stamped"], 0)
            self.assertEqual(led.counts()["queued"], 0)

    def test_error_messages_never_quote_the_identifier(self) -> None:
        with self.open() as led, led.run("r1"):
            for k in (f"{IDENT}|week|2026-09-14", f"{IDENT}|day|20260914"):
                with self.assertRaises(ValueError) as caught:
                    led.stamp_intent(k, intent_rec())
                self.assertNotIn("5555550101", str(caught.exception))

    def test_imthreads_stamped_periods_reads_the_ledger_object(self) -> None:
        with self.open() as led, led.run("r1"):
            for offset in (0, 1):
                led.stamp_done(key(day=DAY + timedelta(days=offset)), intent_rec())
            led.stamp_done(key(IDENT2), intent_rec())
            periods = sorted(imthreads.stamped_periods(IDENT, led))
        self.assertEqual(
            periods,
            [(imthreads.DAY_GRAIN, DAY), (imthreads.DAY_GRAIN, DAY + timedelta(days=1))],
        )


# ---------------------------------------------------------------------------
# Round trips.
# ---------------------------------------------------------------------------


class TestRoundTrip(LedgerTestCase):
    def fill(self, led: imledger.Ledger) -> None:
        k1, k2 = key(day=DAY), key(day=DAY + timedelta(days=1))
        k3, k4 = key(day=DAY + timedelta(days=2)), key(day=DAY + timedelta(days=3))
        led.stamp_intent(k1, intent_rec())
        led.stamp_done(k1, {"stamped_at": "2026-09-15T06:00:00+01:00"})
        led.stamp_intent(k2, intent_rec(topic="Texts (2): open intent"))
        led.set_identifier(
            IDENT2,
            state="pending",
            proposal_id=17,
            kind="phone",
            name="Test Person",
            first_seen="2026-09-01",
            last_seen="2026-09-14",
            days=3,
        )
        led.hold(IDENT2, unit(IDENT2, DAY))
        led.hold(IDENT2, unit(IDENT2, DAY + timedelta(days=1)))
        led.enqueue(unit(IDENT, DAY + timedelta(days=2)))
        self.assertEqual(led.claim([k3], "claim-1", BASE + 3600, BASE), [k3])
        led.record_failure(k4, "stamped=False: the note has no frontmatter")
        led.record_failure(k4, "stamped=False: the note has no frontmatter")

    def test_every_map_round_trips_through_compaction(self) -> None:
        with self.open() as led:
            with led.run("r1"):
                self.fill(led)
            before = self.snapshot(led, ("claim-1",))
            result = led.compact()
            self.assertFalse(result["skipped"])
        self.assertEqual(self.p.wal.stat().st_size, 0)
        with self.open() as led:
            self.assertEqual(self.snapshot(led, ("claim-1",)), before)
        # The shapes the contract names, read back from disk.
        record = before["stamped"][key()]
        for field in ("person_id", "grain", "turns", "topic", "link", "stamped_at", "status"):
            self.assertIn(field, record)
        self.assertEqual((record["grain"], record["status"]), ("day", "done"))
        self.assertEqual(record["stamped_at"], "2026-09-15T06:00:00+01:00")
        self.assertEqual(before["failures"][key(day=DAY + timedelta(days=3))]["count"], 2)
        self.assertEqual(before["counts"]["open_intents"], 1)
        self.assertEqual(before["counts"]["held_units"], 2)

    def test_the_same_state_comes_back_from_the_wal_alone(self) -> None:
        with self.open() as led:
            with led.run("r1"):
                self.fill(led)
            before = self.snapshot(led, ("claim-1",))
        self.assertFalse(self.p.ledger.exists())
        with self.open() as led:
            self.assertEqual(self.snapshot(led, ("claim-1",)), before)
            self.assertGreater(led.recovery.replayed, 0)
            self.assertEqual((led.recovery.discarded, led.recovery.intents_kept), (0, 0))

    def test_reads_are_copies(self) -> None:
        with self.open() as led:
            with led.run("r1"):
                led.enqueue(unit())
                led.set_identifier(IDENT, state="held")
                led.hold(IDENT, unit(day=DAY + timedelta(days=1)))
            led.queued()[key()]["topic"] = "tampered"
            led.identifier(IDENT)["state"] = "accepted"  # type: ignore[index]
            led.held(IDENT)[0]["turns"] = 99
            self.assertEqual(led.queued()[key()]["topic"], unit()["topic"])
            self.assertEqual(led.identifier(IDENT)["state"], "held")  # type: ignore[index]
            self.assertEqual(led.held(IDENT)[0]["turns"], 4)
            with self.assertRaises(TypeError):
                led.stamped["x"] = {}  # type: ignore[index]

    def test_a_date_reads_the_same_live_and_after_reload(self) -> None:
        with self.open() as led:
            with led.run("r1"):
                led.enqueue({**unit(), "day": DAY})
            live = led.queued()[key()]["day"]
        with self.open() as led:
            self.assertEqual(led.queued()[key()]["day"], live)
        self.assertEqual(live, "2026-09-14")

    def test_the_wal_is_bracketed_json_lines(self) -> None:
        with self.open() as led, led.run("daily-2026-09-15"):
            led.enqueue(unit())
            led.set_identifier(IDENT, state="accepted")
        records = self.wal_records()
        self.assertEqual(
            [r["op"] for r in records], ["begin", "enqueue", "set_identifier", "committed"]
        )
        self.assertTrue(all(r["run"] == "daily-2026-09-15" for r in records))
        self.assertTrue(self.p.wal.read_bytes().endswith(b"\n"))


# ---------------------------------------------------------------------------
# The uncommitted segment.
# ---------------------------------------------------------------------------


class TestUncommittedSegment(LedgerTestCase):
    def test_discarded_except_its_intents(self) -> None:
        k_open = key(day=DAY + timedelta(days=1))
        with self.quiet() as err:
            with self.open() as led:
                with led.run("committed"):
                    led.enqueue(unit())
                led.begin_run("killed")
                led.stamp_intent(
                    k_open, intent_rec(topic="Texts (5): the one that may be on the card")
                )
                led.enqueue(unit(day=DAY + timedelta(days=2)))
                led.set_identifier(IDENT2, state="pending", proposal_id=4)
                led.hold(IDENT2, unit(IDENT2))
                led.record_failure(key(day=DAY + timedelta(days=3)), "x")
        self.assertIn("without commit_run", err.getvalue())
        with self.quiet() as err, self.open() as led:
            intents = led.open_intents()
            self.assertEqual(list(intents), [k_open])
            self.assertEqual(intents[k_open]["topic"], "Texts (5): the one that may be on the card")
            self.assertTrue(led.is_queued(key()))
            self.assertFalse(led.is_queued(key(day=DAY + timedelta(days=2))))
            self.assertIsNone(led.identifier(IDENT2))
            self.assertEqual(led.held(IDENT2), [])
            self.assertEqual(led.failures(), {})
            self.assertEqual((led.recovery.discarded, led.recovery.intents_kept), (4, 1))
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertIn("4 change(s) discarded, 1 stamp intent(s) kept", err.getvalue())

    def test_the_intent_is_completed_by_a_later_run(self) -> None:
        k = key()
        with self.quiet(), self.open() as led:
            led.begin_run("killed")
            led.stamp_intent(k, intent_rec())
        with self.quiet(), self.open() as led:
            self.assertIn(k, led.open_intents())
            with led.run("recover"):
                self.assertTrue(led.stamp_done(k))
        # Still uncompacted, so the set-aside run is replayed (and re-reported) again.
        with self.quiet(), self.open() as led:
            self.assertTrue(led.is_stamped(k))
            self.assertEqual(led.open_intents(), {})

    def test_the_run_block_aborts_on_an_exception(self) -> None:
        with self.quiet(), self.open() as led:
            with led.run("r0"):
                led.enqueue(unit())
            with self.assertRaises(RuntimeError), led.run("r1"):
                led.dequeue(key())
                led.enqueue(unit(day=DAY + timedelta(days=1)))
                led.stamp_intent(key(day=DAY + timedelta(days=5)), intent_rec())
                raise RuntimeError("boom")
            self.assertIsNone(led.run_id)
            self.assertEqual(list(led.queued()), [key()])
            self.assertEqual(list(led.open_intents()), [key(day=DAY + timedelta(days=5))])
            memory = self.snapshot(led)
        with self.quiet(), self.open() as led:
            self.assertEqual(self.snapshot(led), memory)

    def test_a_reused_run_id_cannot_borrow_a_later_commit(self) -> None:
        # The morning job re-running the same day uses the same run id.
        with self.quiet(), self.open() as led:
            led.begin_run("daily-2026-09-15")
            led.enqueue(unit())
        with self.quiet(), self.open() as led, led.run("daily-2026-09-15"):
            led.enqueue(unit(day=DAY + timedelta(days=1)))
        with self.quiet(), self.open() as led:
            self.assertEqual(list(led.queued()), [key(day=DAY + timedelta(days=1))])

    def test_a_release_whose_enqueue_never_committed_keeps_the_units_held(self) -> None:
        with self.open() as led, led.run("r1"):
            led.hold(IDENT2, unit(IDENT2))
        with self.quiet(), self.open() as led:
            led.begin_run("r2")
            for held_unit in led.release(IDENT2):
                led.enqueue(held_unit)
        with self.quiet(), self.open() as led:
            self.assertEqual(len(led.held(IDENT2)), 1)
            self.assertEqual(led.queued(), {})


# ---------------------------------------------------------------------------
# Torn and damaged WAL lines.
# ---------------------------------------------------------------------------


class TestTornTail(LedgerTestCase):
    def test_a_torn_last_line_is_ignored_with_one_stderr_line(self) -> None:
        with self.open() as led, led.run("r1"):
            led.enqueue(unit())
        with self.p.wal.open("ab") as handle:
            handle.write(b'{"op":"enqueue","run":"r2","key":"+1555')
        with self.quiet() as err, self.open() as led:
            self.assertEqual(list(led.queued()), [key()])
            self.assertTrue(led.recovery.torn_tail)
        lines = err.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("torn last line", lines[0])
        # The next change cuts the fragment off first, so it cannot glue itself on.
        with self.quiet() as err, self.open() as led, led.run("r3"):
            led.enqueue(unit(day=DAY + timedelta(days=1)))
        with self.quiet() as err, self.open() as led:
            self.assertEqual(len(led.queued()), 2)
            self.assertFalse(led.recovery.torn_tail)
        self.assertEqual(err.getvalue(), "")
        for line in self.p.wal.read_bytes().splitlines():
            json.loads(line)

    def test_a_whole_record_missing_only_its_newline_is_kept(self) -> None:
        with self.open() as led, led.run("r1"):
            led.enqueue(unit())
        raw = self.p.wal.read_bytes()
        self.p.wal.write_bytes(raw.rstrip(b"\n"))  # the `committed` marker loses its newline
        with self.quiet() as err, self.open() as led:
            self.assertEqual(list(led.queued()), [key()])  # still committed
            with led.run("r2"):
                led.enqueue(unit(day=DAY + timedelta(days=1)))
        self.assertEqual(err.getvalue(), "")
        with self.open() as led:
            self.assertEqual(len(led.queued()), 2)
        for line in self.p.wal.read_bytes().splitlines():
            json.loads(line)

    def test_damage_in_the_middle_raises_and_touches_nothing(self) -> None:
        with self.open() as led, led.run("r1"):
            led.enqueue(unit())
            led.enqueue(unit(day=DAY + timedelta(days=1)))
        lines = self.p.wal.read_bytes().splitlines(keepends=True)
        lines.insert(2, b"{not json\n")
        damaged = b"".join(lines)
        self.p.wal.write_bytes(damaged)
        with self.assertRaises(imledger.LedgerCorrupt) as caught, self.open():
            pass
        self.assertEqual(caught.exception.path, self.p.wal)
        self.assertIn(str(self.p.wal), str(caught.exception))
        self.assertEqual(self.p.wal.read_bytes(), damaged)

    def test_a_write_that_fails_part_way_is_cut_back(self) -> None:
        real_write = os.write
        calls = {"n": 0}

        def half_then_enospc(fd: int, data: Any) -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                real_write(fd, bytes(data)[: len(data) // 2])
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        with self.open() as led:
            led.begin_run("r1")
            with mock.patch("os.write", side_effect=half_then_enospc):
                with self.assertRaises(OSError):
                    led.enqueue(unit())
            self.assertFalse(led.is_queued(key()))  # memory never ran ahead of disk
            led.enqueue(unit(day=DAY + timedelta(days=1)))
            led.commit_run("r1")
        raw = self.p.wal.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        for line in raw.splitlines():
            json.loads(line)
        with self.open() as led:
            self.assertEqual(list(led.queued()), [key(day=DAY + timedelta(days=1))])


# ---------------------------------------------------------------------------
# Compaction.
# ---------------------------------------------------------------------------


class TestCompaction(LedgerTestCase):
    def test_truncates_the_wal_and_leaves_owner_only_files(self) -> None:
        with self.open() as led:
            with led.run("r1"):
                led.enqueue(unit())
            led.compact()
            led.save_state({"watermark": 780_000_000_000_000_001, "zone": "Europe/London"})
        self.assertEqual(self.p.wal.stat().st_size, 0)
        document = json.loads(self.p.ledger.read_text(encoding="utf-8"))
        self.assertEqual(document["version"], imledger.FORMAT_VERSION)
        for name in imledger.MAPS:
            self.assertIn(name, document)
        self.assertEqual(list(self.home.glob("*.tmp")), [])
        if sys.platform != "win32":
            for path in (self.p.ledger, self.p.wal, self.p.lock, self.p.state):
                with self.subTest(path.name):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_loose_modes_are_tightened(self) -> None:
        if sys.platform == "win32":
            self.skipTest("POSIX modes only")
        with self.open() as led:
            with led.run("r1"):
                led.enqueue(unit())
            led.compact()
        for path in (self.p.ledger, self.p.wal, self.p.lock):
            os.chmod(path, 0o644)
        with self.open() as led:
            with led.run("r2"):
                led.enqueue(unit(day=DAY + timedelta(days=1)))
            led.compact()
        for path in (self.p.ledger, self.p.wal, self.p.lock):
            with self.subTest(path.name):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_refused_while_a_run_is_open(self) -> None:
        with self.quiet(), self.open() as led:
            led.begin_run("r1")
            led.enqueue(unit())
            with self.assertRaises(imledger.LedgerError):
                led.compact()
            with self.assertRaises(imledger.LedgerError):
                led.save_state({"watermark": 1})
        self.assertFalse(self.p.ledger.exists())
        self.assertFalse(self.p.state.exists())

    def test_nothing_new_is_skipped(self) -> None:
        with self.open() as led:
            with led.run("r1"):
                led.enqueue(unit())
            self.assertFalse(led.compact()["skipped"])
            self.assertTrue(led.compact()["skipped"])

    def test_a_kill_between_replace_and_truncate_converges(self) -> None:
        # An uncommitted segment, then a committed one that resolves its intent
        # and exercises every operation, all still in the WAL at compaction time.
        k_open = key(day=DAY + timedelta(days=9))
        with self.quiet(), self.open() as led:
            led.begin_run("killed")
            led.stamp_intent(k_open, intent_rec())
            led.enqueue(unit(day=DAY + timedelta(days=8)))
        with self.quiet(), self.open() as led:
            with led.run("r2"):
                led.stamp_done(k_open)
                led.enqueue(unit())
                led.enqueue(unit(day=DAY + timedelta(days=1)))
                led.dequeue(key(day=DAY + timedelta(days=1)))
                led.set_identifier(IDENT2, state="pending", proposal_id=3)
                led.set_identifier(IDENT2, state="attach_pending")
                led.hold(IDENT2, unit(IDENT2))
                led.hold(IDENT2, unit(IDENT2, turns=9))
                led.hold(IDENT3, unit(IDENT3))
                led.release(IDENT3)
                led.claim([key()], "c1", BASE + 3600, BASE)
                led.record_failure(key(day=DAY + timedelta(days=4)), "stamped=False")
                led.record_failure(key(day=DAY + timedelta(days=4)), "stamped=False")
                led.stamp_intent(key(day=DAY + timedelta(days=6)), intent_rec())
                led.drop_intent(key(day=DAY + timedelta(days=6)))
            expected = self.snapshot(led, ("c1",))
            wal_before = self.p.wal.read_bytes()
            led.compact()
        self.p.wal.write_bytes(wal_before)  # as if killed before the truncate
        with self.quiet(), self.open() as led:
            self.assertEqual(self.snapshot(led, ("c1",)), expected)
        self.assertEqual(expected["failures"][key(day=DAY + timedelta(days=4))]["count"], 2)
        self.assertTrue(expected["stamped"][k_open]["status"] == "done")


# ---------------------------------------------------------------------------
# Corruption is never "start fresh".
# ---------------------------------------------------------------------------


class TestCorrupt(LedgerTestCase):
    def valid_document(self) -> dict[str, Any]:
        return {"version": 1, **{name: {} for name in imledger.MAPS}}

    def test_a_corrupt_ledger_raises_and_is_left_byte_identical(self) -> None:
        good = self.valid_document()
        cases: dict[str, bytes] = {
            "truncated JSON": b'{"version": 1, "stamped": {',
            "empty file": b"",
            "whitespace": b"  \n",
            "array": b"[]",
            "no version": json.dumps({k: v for k, v in good.items() if k != "version"}).encode(),
            "newer format": json.dumps({**good, "version": 99}).encode(),
            "stamped missing": json.dumps(
                {k: v for k, v in good.items() if k != "stamped"}
            ).encode(),
            "stamped is a list": json.dumps({**good, "stamped": []}).encode(),
            "entry not an object": json.dumps({**good, "stamped": {key(): "done"}}).encode(),
            "pending not a list": json.dumps({**good, "pending_units": {IDENT: {}}}).encode(),
            "not UTF-8": b'{"version": 1, "x": "\xff\xfe"}',
        }
        for label, payload in cases.items():
            with self.subTest(label):
                self.p.ledger.write_bytes(payload)
                before = sorted(p.name for p in self.home.iterdir() if p.name != imledger.LOCK_NAME)
                with self.assertRaises(imledger.LedgerCorrupt) as caught, self.open():
                    self.fail("a corrupt ledger must never open")
                self.assertEqual(caught.exception.path, self.p.ledger)
                self.assertIn(str(self.p.ledger), str(caught.exception))
                self.assertEqual(self.p.ledger.read_bytes(), payload)
                after = sorted(p.name for p in self.home.iterdir() if p.name != imledger.LOCK_NAME)
                self.assertEqual(after, before)

    def test_corrupt_state_raises_and_is_left_byte_identical(self) -> None:
        cases = {
            "garbage": b"{watermark",
            "empty": b"",
            "array": b"[1]",
            "float watermark": b'{"watermark": 7.8e17}',
            "string watermark": b'{"watermark": "780000000000000001"}',
            "bool watermark": b'{"watermark": true}',
        }
        for label, payload in cases.items():
            with self.subTest(label):
                self.p.state.write_bytes(payload)
                with self.assertRaises(imledger.LedgerCorrupt):
                    imledger.load_state(self.home)
                with self.open() as led, self.assertRaises(imledger.LedgerCorrupt):
                    led.load_state()
                self.assertEqual(self.p.state.read_bytes(), payload)

    def test_the_watermark_round_trips_as_an_exact_int(self) -> None:
        watermark = 780_123_456_789_012_345  # past 2**53: a float would move it
        self.assertNotEqual(int(float(watermark)), watermark)
        with self.open() as led:
            self.assertEqual(led.load_state(), {})
            led.save_state({"watermark": watermark})
            self.assertEqual(led.load_state()["watermark"], watermark)
        loaded = imledger.load_state(self.home)["watermark"]
        self.assertIs(type(loaded), int)
        self.assertEqual(loaded, watermark)
        self.assertIn(b"780123456789012345", self.p.state.read_bytes())

    def test_a_non_int_watermark_is_never_written(self) -> None:
        with self.open() as led:
            for value in (7.8e17, "780123456789012345", True, None):
                with self.subTest(repr(value)), self.assertRaises(ValueError):
                    led.save_state({"watermark": value})
        self.assertFalse(self.p.state.exists())

    def test_the_watermark_errors_call_it_a_row_number_not_a_date(self) -> None:
        """Carried from CP4: the watermark became the highest message.ROWID read, and a
        sentence that still called it the message date would send a member hunting for
        the wrong thing."""
        self.p.state.write_bytes(b'{"watermark": 7.5}')
        with self.assertRaises(imledger.LedgerCorrupt) as caught:
            imledger.load_state(self.home)
        said = str(caught.exception)
        self.assertIn("row", said)
        self.assertIn("ROWID", said)
        for stale in ("message.date", "nanosecond"):
            self.assertNotIn(stale, said)
        self.p.state.unlink()
        with self.open() as led, self.assertRaises(ValueError) as refused:
            led.save_state({"watermark": "12"})
        self.assertIn("ROWID", str(refused.exception))
        self.assertNotIn("message.date", str(refused.exception))

    def test_a_lost_ledger_with_a_watermark_is_said_out_loud(self) -> None:
        self.p.state.write_text('{"watermark": 780000000000000001}\n', encoding="utf-8")
        with self.quiet() as err, self.open() as led:
            self.assertTrue(led.missing_but_watermarked)
        self.assertIn("missing", err.getvalue())
        with self.quiet(), self.open() as led, led.run("r1"):
            led.enqueue(unit())
        with self.quiet() as err, self.open() as led:
            self.assertFalse(led.missing_but_watermarked)  # the WAL now says runs happened
        self.assertEqual(err.getvalue(), "")


# ---------------------------------------------------------------------------
# The summary queue.
# ---------------------------------------------------------------------------


class TestQueue(LedgerTestCase):
    def test_enqueue_is_idempotent_on_key(self) -> None:
        with self.open() as led, led.run("r1"):
            self.assertTrue(led.enqueue({**unit(), "queued_at": "2026-09-15T06:00:00+01:00"}))
            later = {**unit(), "turns": 7, "queued_at": "2026-09-18T06:00:00+01:00"}
            self.assertFalse(led.enqueue(later))
            queued = led.queued()[key()]
        self.assertEqual(queued["queued_at"], "2026-09-15T06:00:00+01:00")
        self.assertEqual(queued["turns"], 4)
        self.assertEqual([r["op"] for r in self.wal_records()].count("enqueue"), 1)

    def test_enqueue_fills_queued_at_when_absent(self) -> None:
        with self.open() as led, led.run("r1"):
            led.enqueue(unit())
            stamp = led.queued()[key()]["queued_at"]
        self.assertIsNotNone(datetime.fromisoformat(stamp).tzinfo)

    def test_enqueue_refuses_a_stamped_done_key(self) -> None:
        with self.open() as led, led.run("r1"):
            led.stamp_intent(key(), intent_rec())
            led.stamp_done(key())
            self.assertFalse(led.enqueue(unit()))
            self.assertFalse(led.is_queued(key()))
        self.assertNotIn("enqueue", [r["op"] for r in self.wal_records()])

    def test_an_open_intent_does_not_block_enqueue(self) -> None:
        with self.open() as led, led.run("r1"):
            led.stamp_intent(key(), intent_rec())
            self.assertTrue(led.enqueue(unit()))

    def test_landing_takes_the_day_off_the_queue_and_clears_its_failure(self) -> None:
        with self.open() as led, led.run("r1"):
            led.enqueue(unit())
            led.record_failure(key(), "stamped=False: snapshot refused")
            led.stamp_intent(key(), intent_rec())
            led.stamp_done(key())
            self.assertFalse(led.is_queued(key()))
            self.assertEqual(led.failures(), {})

    def test_dequeue_returns_the_record_once(self) -> None:
        with self.open() as led, led.run("r1"):
            led.enqueue(unit())
            self.assertEqual(led.dequeue(key())["topic"], unit()["topic"])  # type: ignore[index]
            self.assertIsNone(led.dequeue(key()))

    def test_a_record_naming_another_identifier_is_refused(self) -> None:
        with self.open() as led, led.run("r1"), self.assertRaises(ValueError):
            led.enqueue({**unit(), "identifier": IDENT2})

    def test_queued_record_is_one_copy(self) -> None:
        with self.open() as led, led.run("r1"):
            led.enqueue(unit())
            one = led.queued_record(key())
            self.assertEqual(one, led.queued()[key()])
            one["topic"] = "changed in the caller's hands"  # type: ignore[index]
            self.assertNotEqual(led.queued_record(key())["topic"], one["topic"])  # type: ignore[index]
            self.assertIsNone(led.queued_record(key(IDENT2)))


# ---------------------------------------------------------------------------
# refresh_links: a queued day's transcripts may grow; nothing else about it may move.
# ---------------------------------------------------------------------------


class TestRefreshLinks(LedgerTestCase):
    NEW = "_local/imessage/threads/2026/2026-09-14-walk-202.txt"

    def test_new_links_are_appended_after_the_first_and_nothing_else_moves(self) -> None:
        with self.open() as led, led.run("r1"):
            led.enqueue(unit())
            before = led.queued()[key()]
            self.assertTrue(led.refresh_links(key(), [self.NEW, LINK, self.NEW]))
            after = led.queued()[key()]
        self.assertEqual(after["links"], [LINK, self.NEW], "the first link must stay first")
        self.assertEqual({k: v for k, v in after.items() if k != "links"},
                         {k: v for k, v in before.items() if k != "links"},
                         "a refresh changed the frozen record (topic, turns, queued_at)")
        self.assertEqual([r["op"] for r in self.wal_records()].count("refresh_links"), 1)

    def test_it_survives_replay_and_compaction(self) -> None:
        with self.open() as led, led.run("r1"):
            led.enqueue(unit())
            led.refresh_links(key(), [self.NEW])
        with self.open() as led:              # replayed from the WAL
            self.assertEqual(led.queued()[key()]["links"], [LINK, self.NEW])
            led.compact()
        with self.open() as led:              # read back from the snapshot
            self.assertEqual(led.queued()[key()]["links"], [LINK, self.NEW])
        self.assertEqual(self.wal_records(), [])

    def test_nothing_new_writes_nothing(self) -> None:
        with self.open() as led, led.run("r1"):
            led.enqueue(unit())
            lines = len(self.wal_records())
            self.assertFalse(led.refresh_links(key(), [LINK]))
            self.assertFalse(led.refresh_links(key(), []))
            self.assertEqual(len(self.wal_records()), lines)

    def test_a_day_not_queued_is_left_alone(self) -> None:
        with self.open() as led, led.run("r1"):
            self.assertFalse(led.refresh_links(key(), [self.NEW]))
            led.hold(IDENT, unit())               # held is not queued: its record is rebuilt whole
            self.assertFalse(led.refresh_links(key(), [self.NEW]))
            self.assertEqual(led.queued(), {})

    def test_a_stamped_day_is_refused(self) -> None:
        with self.open() as led, led.run("r1"):
            led.enqueue(unit())
            led.stamp_intent(key(), intent_rec())
            led.stamp_done(key())
            with self.assertRaises(imledger.AlreadyStampedError):
                led.refresh_links(key(), [self.NEW])

    def test_an_open_intent_keeps_its_link_first(self) -> None:
        with self.open() as led, led.run("r1"):
            led.enqueue(unit())
            led.stamp_intent(key(), intent_rec(link=LINK))
            self.assertTrue(led.refresh_links(key(), [self.NEW]))
            self.assertEqual(led.queued()[key()]["links"][0], LINK)
            self.assertEqual(led.open_intents()[key()]["link"], LINK)

    def test_it_needs_a_run_and_real_links(self) -> None:
        with self.open() as led:
            with led.run("r1"):
                led.enqueue(unit())
            with self.assertRaises(imledger.LedgerError):
                led.refresh_links(key(), [self.NEW])
            with led.run("r2"):
                for bad in ([""], ["   "], [None], self.NEW):
                    with self.subTest(bad=bad), self.assertRaises((TypeError, ValueError)):
                        led.refresh_links(key(), bad)  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    led.refresh_links("not a ledger key", [self.NEW])
            self.assertEqual(led.queued()[key()]["links"], [LINK])

    def test_an_uncommitted_refresh_is_discarded(self) -> None:
        with self.quiet(), self.open() as led:
            with led.run("r1"):
                led.enqueue(unit())
            led.begin_run("killed")
            led.refresh_links(key(), [self.NEW])
        with self.quiet(), self.open() as led:
            self.assertEqual(led.queued()[key()]["links"], [LINK])


# ---------------------------------------------------------------------------
# Claims and leases.
# ---------------------------------------------------------------------------


class TestClaims(LedgerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.k1, self.k2, self.k3 = (key(day=DAY + timedelta(days=i)) for i in range(3))

    def queue_three(self, led: imledger.Ledger) -> None:
        for offset in range(3):
            led.enqueue(unit(day=DAY + timedelta(days=offset)))

    def test_a_live_claim_blocks_a_second_claim(self) -> None:
        with self.open() as led, led.run("r1"):
            self.queue_three(led)
            got = led.claim([self.k1, self.k2], "c1", BASE + 3600, BASE)
            self.assertEqual(got, [self.k1, self.k2])
            got = led.claim([self.k1, self.k2, self.k3], "c2", BASE + 3600, BASE + 60)
            self.assertEqual(got, [self.k3])
            self.assertEqual(led.live_claim(self.k1, BASE + 60), "c1")
            self.assertEqual(led.live_claim(self.k3, BASE + 60), "c2")

    def test_an_expired_lease_does_not_block(self) -> None:
        with self.open() as led, led.run("r1"):
            self.queue_three(led)
            led.claim([self.k1], "c1", BASE + 10, BASE)
            self.assertIsNone(led.live_claim(self.k1, BASE + 10))  # the lease end is exclusive
            self.assertEqual(led.claim([self.k1], "c2", BASE + 3600, BASE + 20), [self.k1])
            self.assertEqual(led.live_claim(self.k1, BASE + 20), "c2")

    def test_releasing_a_claim_frees_its_keys(self) -> None:
        with self.open() as led, led.run("r1"):
            self.queue_three(led)
            led.claim([self.k1], "c1", BASE + 3600, BASE)
            self.assertTrue(led.release_claim("c1"))
            self.assertFalse(led.release_claim("c1"))
            self.assertIsNone(led.live_claim(self.k1, BASE))
            self.assertEqual(led.claim([self.k1], "c2", BASE + 3600, BASE), [self.k1])
            self.assertTrue(led.is_queued(self.k1))  # a claim never takes a unit off the queue

    def test_unqueued_and_landed_keys_are_skipped(self) -> None:
        with self.open() as led, led.run("r1"):
            led.enqueue(unit())
            led.stamp_done(self.k2)
            got = led.claim([self.k1, self.k2, self.k3], "c1", BASE + 60, BASE)
            self.assertEqual(got, [self.k1])
            self.assertEqual(led.claim([self.k3], "c2", BASE + 60, BASE), [])
            self.assertIsNone(led.claim_of("c2"))

    def test_reclaiming_under_the_same_id_replaces_it(self) -> None:
        with self.open() as led, led.run("r1"):
            self.queue_three(led)
            led.claim([self.k1, self.k2], "c1", BASE + 60, BASE)
            got = led.claim([self.k2, self.k3], "c1", BASE + 7200, BASE)
            self.assertEqual(got, [self.k2, self.k3])
            self.assertEqual(led.claim_of("c1")["keys"], [self.k2, self.k3])  # type: ignore[index]
            self.assertIsNone(led.live_claim(self.k1, BASE))

    def test_lease_times_are_aware_datetimes_or_epoch_seconds(self) -> None:
        now = datetime.fromtimestamp(BASE, tz=UTC)
        with self.open() as led, led.run("r1"):
            self.queue_three(led)
            self.assertEqual(led.claim([self.k1], "c1", now + timedelta(hours=24), now), [self.k1])
            self.assertEqual(led.claim_of("c1")["lease_until"], BASE + 86400)  # type: ignore[index]
            with self.assertRaises(ValueError):
                led.claim([self.k2], "c2", datetime(2030, 1, 1), BASE)  # naive
            with self.assertRaises(ValueError):
                led.claim([self.k2], "c2", BASE - 1, BASE)  # already over
            with self.assertRaises(TypeError):
                led.claim(self.k2, "c2", BASE + 60, BASE)  # one string, not a list

    def test_claims_survive_a_reload_and_expired_ones_prune(self) -> None:
        with self.open() as led:
            with led.run("r1"):
                self.queue_three(led)
                led.claim([self.k1], "old", BASE + 10, BASE)
                led.claim([self.k2], "live", BASE + 3600, BASE)
            led.compact()
        with self.open() as led:
            self.assertEqual(led.live_claim(self.k2, BASE + 60), "live")
            with led.run("r2"):
                self.assertEqual(led.prune_claims(BASE + 60), ["old"])
            self.assertIsNone(led.claim_of("old"))
            self.assertIsNotNone(led.claim_of("live"))


# ---------------------------------------------------------------------------
# Held units.
# ---------------------------------------------------------------------------


class TestHold(LedgerTestCase):
    def test_hold_and_release(self) -> None:
        with self.open() as led:
            with led.run("r1"):
                self.assertTrue(led.hold(IDENT2, unit(IDENT2, DAY)))
                self.assertTrue(led.hold(IDENT2, unit(IDENT2, DAY + timedelta(days=1))))
            self.assertEqual(len(led.held(IDENT2)), 2)
            with led.run("r2"):
                released = led.release(IDENT2)
                self.assertEqual(led.release(IDENT2), [])
            self.assertEqual(
                [u["key"] for u in released],
                [key(IDENT2), key(IDENT2, DAY + timedelta(days=1))],
            )
            self.assertEqual(led.held(IDENT2), [])
        with self.open() as led:
            self.assertEqual(led.held(IDENT2), [])

    def test_holding_the_same_day_again_replaces_it(self) -> None:
        with self.open() as led, led.run("r1"):
            led.hold(IDENT2, unit(IDENT2, turns=4))
            led.hold(IDENT2, unit(IDENT2, turns=5))
            led.hold(IDENT2, unit(IDENT2, turns=5))  # identical: writes nothing
            self.assertEqual([u["turns"] for u in led.held(IDENT2)], [5])
        self.assertEqual([r["op"] for r in self.wal_records()].count("hold"), 2)

    def test_a_unit_is_never_held_under_another_identifier(self) -> None:
        with self.open() as led, led.run("r1"):
            with self.assertRaises(ValueError):
                led.hold(IDENT, unit(IDENT2))
            with self.assertRaises(TypeError):
                led.hold(IDENT, {"identifier": IDENT})  # no key at all

    def test_a_landed_day_is_not_held(self) -> None:
        with self.open() as led, led.run("r1"):
            led.stamp_done(key(IDENT2))
            self.assertFalse(led.hold(IDENT2, unit(IDENT2)))
            self.assertEqual(led.held(IDENT2), [])


# ---------------------------------------------------------------------------
# Intents: the write-ahead discipline.
# ---------------------------------------------------------------------------


class TestIntents(LedgerTestCase):
    def test_an_intent_on_a_landed_day_raises(self) -> None:
        with self.open() as led, led.run("r1"):
            led.stamp_intent(key(), intent_rec())
            led.stamp_done(key())
            with self.assertRaises(imledger.AlreadyStampedError):
                led.stamp_intent(key(), intent_rec())

    def test_a_different_intent_while_one_is_open_raises(self) -> None:
        with self.open() as led, led.run("r1"):
            led.stamp_intent(key(), intent_rec(topic="Texts (3): mechanical fallback"))
            with self.assertRaises(imledger.IntentOpenError):
                led.stamp_intent(key(), intent_rec(topic="Planned the Saturday trip"))
            self.assertTrue(led.drop_intent(key()))
            self.assertFalse(led.drop_intent(key()))
            led.stamp_intent(key(), intent_rec(topic="Planned the Saturday trip"))
            self.assertEqual(led.open_intents()[key()]["topic"], "Planned the Saturday trip")

    def test_an_identical_intent_is_a_no_op(self) -> None:
        with self.open() as led, led.run("r1"):
            led.stamp_intent(key(), intent_rec())
            led.stamp_intent(key(), intent_rec())
        self.assertEqual([r["op"] for r in self.wal_records()].count("stamp_intent"), 1)

    def test_a_landed_day_is_frozen(self) -> None:
        with self.open() as led, led.run("r1"):
            led.stamp_intent(key(), intent_rec(turns=3))
            self.assertTrue(led.stamp_done(key()))
            self.assertFalse(led.stamp_done(key(), {"topic": "Texts (4): changed", "turns": 4}))
            record = led.stamp_record(key())
        self.assertEqual((record["topic"], record["turns"]), (intent_rec()["topic"], 3))  # type: ignore[index]

    def test_an_intent_must_carry_its_topic(self) -> None:
        with self.open() as led, led.run("r1"):
            for rec in ({"link": LINK}, {"topic": "   "}, {"topic": None}):
                with self.subTest(rec), self.assertRaises(ValueError):
                    led.stamp_intent(key(), rec)
            with self.assertRaises(ValueError):
                led.stamp_intent(key(), intent_rec(grain="week"))


# ---------------------------------------------------------------------------
# Run discipline and identifiers.
# ---------------------------------------------------------------------------


class TestDiscipline(LedgerTestCase):
    def test_every_mutation_needs_an_open_run(self) -> None:
        with self.open() as led:
            attempts = {
                "stamp_intent": lambda: led.stamp_intent(key(), intent_rec()),
                "stamp_done": lambda: led.stamp_done(key()),
                "set_identifier": lambda: led.set_identifier(IDENT, state="held"),
                "hold": lambda: led.hold(IDENT, unit()),
                "enqueue": lambda: led.enqueue(unit()),
                "record_failure": lambda: led.record_failure(key(), "x"),
            }
            for name, attempt in attempts.items():
                with self.subTest(name), self.assertRaises(imledger.LedgerError):
                    attempt()
        self.assertFalse(self.p.wal.exists())  # a session that changed nothing wrote nothing

    def test_run_ids_must_match_and_never_nest(self) -> None:
        with self.quiet(), self.open() as led:
            with self.assertRaises(imledger.LedgerError):
                led.commit_run("r1")
            led.begin_run("r1")
            with self.assertRaises(imledger.LedgerError):
                led.begin_run("r2")
            with self.assertRaises(imledger.LedgerError):
                led.commit_run("r2")
            with self.assertRaises(ValueError):
                led.begin_run("  ")
            led.commit_run("r1")

    def test_a_closed_session_refuses_changes(self) -> None:
        with self.open() as led:
            pass
        with self.assertRaises(imledger.LedgerError):
            led.begin_run("late")

    def test_identifier_states(self) -> None:
        with self.open() as led, led.run("r1"):
            with self.assertRaises(ValueError):
                led.set_identifier(IDENT, proposal_id=1)  # a new one needs a state
            with self.assertRaises(ValueError):
                led.set_identifier(IDENT, state="maybe")
            for state in sorted(imledger.IDENTIFIER_STATES):
                led.set_identifier(IDENT3, state=state)
            led.set_identifier(IDENT, state="pending", proposal_id=12, days=1)
            merged = led.set_identifier(IDENT, days=2)
            self.assertEqual(merged, {"state": "pending", "proposal_id": 12, "days": 2})
            before = len(self.wal_records())
            led.set_identifier(IDENT, days=2)  # no change, no line
            self.assertEqual(len(self.wal_records()), before)


# ---------------------------------------------------------------------------
# The lock.
# ---------------------------------------------------------------------------


class RecordingLock:
    def __init__(self, events: list[str], fail: bool = False) -> None:
        self.events, self.fail = events, fail

    def __enter__(self) -> "RecordingLock":
        if self.fail:
            raise TimeoutError("busy")
        self.events.append("enter")
        return self

    def __exit__(self, *exc: object) -> None:
        self.events.append("exit")


class TestLock(LedgerTestCase):
    def test_a_second_session_times_out_while_the_first_holds_the_lock(self) -> None:
        with self.open():
            started = time.monotonic()
            with self.assertRaises(TimeoutError), self.open(timeout=0.2):
                pass
            self.assertLess(time.monotonic() - started, 2.0)
        with self.open():  # released on exit
            pass

    def test_the_injected_lock_is_held_for_the_whole_session(self) -> None:
        events: list[str] = []
        with imledger.session(lambda: RecordingLock(events), home=self.home) as led:
            self.assertEqual(events, ["enter"])
            with led.run("r1"):
                led.enqueue(unit())
            led.compact()
            led.save_state({"watermark": 5})
            self.assertEqual(events, ["enter"])
        self.assertEqual(events, ["enter", "exit"])

    def test_a_lock_object_is_accepted_as_well_as_a_factory(self) -> None:
        events: list[str] = []
        with imledger.session(RecordingLock(events), home=self.home):
            self.assertEqual(events, ["enter"])
        with imledger.session(imledger.default_lock(self.home, timeout=1.0), home=self.home):
            pass
        self.assertEqual(events, ["enter", "exit"])

    def test_a_lock_that_fails_touches_nothing(self) -> None:
        with self.assertRaises(TimeoutError), imledger.session(
            lambda: RecordingLock([], fail=True), home=self.home
        ):
            self.fail("the body must not run without the lock")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_lock_target_matches_the_engine_sidecar_rule(self) -> None:
        # shared.file_lock(p) locks p.with_suffix(p.suffix + ".lock").
        target = imledger.lock_target(self.home)
        self.assertEqual(target.with_suffix(target.suffix + ".lock"), self.p.lock)

    def test_the_default_home_is_the_plugin_folder_and_tests_never_touch_it(self) -> None:
        self.assertEqual(imledger.paths().home, imconfig.HOME)
        real = [PLUGIN_HOME / n for n in ("ledger.json", "ledger.wal", "state.json")]
        before = [(p.exists(), p.stat().st_mtime_ns if p.exists() else None) for p in real]
        with self.open() as led, led.run("r1"):
            led.enqueue(unit())
        after = [(p.exists(), p.stat().st_mtime_ns if p.exists() else None) for p in real]
        self.assertEqual(after, before)


# ---------------------------------------------------------------------------
# SIGKILL, for real.
# ---------------------------------------------------------------------------


CHILD = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {plugin!r})
    import imconfig  # noqa: F401
    from datetime import date, timedelta
    from pathlib import Path
    import imledger, imthreads

    home = Path({home!r})
    first = date(2020, 1, 1)
    with imledger.session(home=home) as led:
        with led.run("committed-first"):
            led.enqueue({{"key": imthreads.ledger_key({ident!r}, "day", first), "turns": 1}})
        led.begin_run("killed")
        for i in range(1_000_000):
            k = imthreads.ledger_key({ident!r}, "day", first + timedelta(days=i + 1))
            led.stamp_intent(k, {{"topic": "Texts (%d): intent" % i, "link": "L", "turns": i}})
            led.set_identifier({ident!r}, state="pending", days=i)  # must be discarded
            print(i, flush=True)
    """
)


@unittest.skipUnless(hasattr(signal, "SIGKILL"), "SIGKILL is POSIX-only")
class TestSigkill(LedgerTestCase):
    def test_every_flushed_intent_survives_a_sigkill(self) -> None:
        wanted = 300
        code = CHILD.format(plugin=str(PLUGIN_HOME), home=str(self.home), ident=IDENT)
        proc = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        confirmed: list[int] = []
        # A child that hangs without printing would block readline() for ever; the
        # watchdog kills it, which ends the read with EOF and fails the test below.
        watchdog = threading.Timer(60.0, proc.kill)
        watchdog.start()
        try:
            assert proc.stdout is not None
            while len(confirmed) < wanted:
                line = proc.stdout.readline()
                if not line:
                    break
                confirmed.append(int(line))
            if len(confirmed) < wanted:
                # Read the child's stderr only once it is dead: reading it from a
                # live child blocks until the child exits, which it never would.
                proc.kill()
                proc.wait(timeout=10)
                detail = proc.stderr.read().decode()[-500:] if proc.stderr else ""
                self.fail(f"the child stopped after {len(confirmed)} intents: {detail}")
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        finally:
            watchdog.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        first = date(2020, 1, 1)
        with self.quiet() as err, self.open(timeout=5.0) as led:  # the kill released the lock
            intents = led.open_intents()
            for i in confirmed:
                k = key(IDENT, first + timedelta(days=i + 1))
                self.assertIn(k, intents, f"intent {i} was reported written but is gone")
                self.assertEqual(intents[k]["turns"], i)
            self.assertTrue(led.is_queued(key(IDENT, first)))  # the committed run stands
            self.assertIsNone(led.identifier(IDENT))  # the killed run's other work is gone
            self.assertGreaterEqual(led.recovery.intents_kept, wanted)
            self.assertGreaterEqual(led.recovery.discarded, wanted)
        self.assertIn("set aside", err.getvalue())


# ---------------------------------------------------------------------------
# Performance: the 20-second budget.
# ---------------------------------------------------------------------------


SUMMARY = (
    "Confirmed the Saturday plan, lunch after, and moved the early session from 8am to 7am; "
    "agreed who drives and that the forms go in by Thursday. Also sorted the reimbursement "
    "for the school trip receipt and flagged the certificate that lapsed, with a follow-up "
    "owed next week."
)


class TestPerformance(LedgerTestCase):
    def test_two_hundred_stamps_and_a_compaction_fit_the_budget(self) -> None:
        stamped_n, pending_n, idents_n, pairs = 6_000, 5_000, 250, 200
        t0 = time.perf_counter()
        with self.open(timeout=5.0) as led:
            with led.run("build"):
                for i in range(stamped_n):
                    ident = f"person{i % idents_n}@example.com"
                    day = DAY - timedelta(days=i // idents_n)
                    record = {**intent_rec(SUMMARY), "person_id": f"p-{i:05d}"}
                    led.stamp_done(key(ident, day), record)
                for i in range(pending_n):
                    ident = f"unknown{i % idents_n}@example.com"
                    day = DAY - timedelta(days=i // idents_n)
                    led.hold(ident, {**unit(ident, day), "links": [LINK, LINK, LINK]})
            led.compact()
        build_s = time.perf_counter() - t0
        size_mb = self.p.ledger.stat().st_size / 1e6

        t1 = time.perf_counter()
        with self.open(timeout=5.0) as led:
            t_loaded = time.perf_counter()
            with led.run("daily"):
                for i in range(pairs):
                    k = key("fresh@example.com", DAY + timedelta(days=i + 1))
                    led.enqueue(unit("fresh@example.com", DAY + timedelta(days=i + 1)))
                    led.stamp_intent(k, intent_rec(SUMMARY))
                    self.assertTrue(led.stamp_done(k))
            t_pairs = time.perf_counter()
            result = led.compact()
            t_compacted = time.perf_counter()
            led.save_state({"watermark": 780_000_000_000_000_001})
        total = time.perf_counter() - t1

        load_s, pairs_s = t_loaded - t1, t_pairs - t_loaded
        compact_s = t_compacted - t_pairs
        print(
            f"\nimledger perf: ledger {size_mb:.2f} MB ({stamped_n} stamped + {pending_n} held) "
            f"built in {build_s:.2f}s | load {load_s * 1000:.0f} ms | {pairs} enqueue+intent+done "
            f"{pairs_s * 1000:.0f} ms ({pairs_s / pairs * 1000:.2f} ms each) | compact "
            f"{compact_s * 1000:.0f} ms | total {total:.2f}s of a 20s budget",
            file=sys.stderr,
        )
        with self.open() as led:
            counts = led.counts()
        self.assertEqual(counts["stamped"], stamped_n + pairs)
        self.assertEqual(counts["held_units"], pending_n)
        self.assertFalse(result["skipped"])
        self.assertLess(
            total, 5.0, "the ledger must leave the 20-second stage room for its real work"
        )


if __name__ == "__main__":
    unittest.main()
