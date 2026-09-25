"""exclude / include — backed up first, one writer, the waiting days dropped, reversible.

Every run here goes through the real daily pipeline (to queue and hold days), the real
ledger, the real engine's LOCAL undo ring (``local_snapshot``, through ``imspine``) and the
plug-in's own atomic ``config.local.json`` writer, inside :mod:`imfixture`'s temp vault and
database.  The plug-in home is a temp ``<root>/_local/imessage`` and ``config.PROJECT_ROOT``
points at ``<root>`` only for the length of each exclude/include call, because the engine's
local ring takes only ``_local/`` paths inside the Glitch folder; the ring itself is the
fixture's (``config.LOCAL_SNAPSHOTS_DIR`` is re-rooted by :mod:`imfixture`).

What each group holds still
---------------------------
* **Exclude** — a preview writes nothing; the act backs ``config.local.json`` up FIRST
  (the backup holds the bytes from before the write), then adds the numbers to
  ``never_ingest`` (keeping ``own_handles``), drops their queued-but-unfiled and held
  days, takes their numbers off the texts review list, and says that lines already on a
  card stay; a later daily run never reads that number again.
* **Include** — the reverse, backed up first, and the numbers go back where they stood.
* **Refusals** — an ambiguous name, a failed backup, the member's own number: nothing
  written.
* **G7** — ``local_snapshot`` is reached through ``imspine`` alone, at call time.

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
import json  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

import imessage  # noqa: E402
import imexclude  # noqa: E402
import imreview  # noqa: E402
import imrun  # noqa: E402
import imspine  # noqa: E402
from imfixture import patched  # noqa: E402
from test_imrun import (  # noqa: E402
    ALICE_PHONE,
    BOB_EMAIL,
    D1,
    D2,
    D3,
    D4,
    D5,
    FRANK,
    ME,
    RunCase,
    _changed,
    _conversation_lines,
    _tree_bytes,
    key,
    morning,
)

import config  # noqa: E402

BOB_PHONE = "+15555550170"      # Bob's number, in Contacts only (his card has his address)
FRANK_TWIN = "+15555550169"     # a second "Frank" in Contacts


class ExcludeCase(RunCase):
    """Alice (a card) has two days queued; Frank (Contacts, no card) has two days held."""

    def setUp(self):
        super().setUp()
        self.brain = self.fx.root
        self.home = self.brain / "_local" / "imessage"
        self.home.mkdir(parents=True)
        self.threads = self.home / "threads"
        self.local = self.home / "config.local.json"
        imessage.write_local_config([ME], target=self.local, home=self.home)
        self.ring = Path(config.LOCAL_SNAPSHOTS_DIR)
        for chat in (1, 3):
            self.chats.chat(chat)
        for day in (D1, D2):
            self.chats.exchange(1, day, ALICE_PHONE)
            self.chats.exchange(3, day, FRANK)
        self.daily(morning(D1))                  # the first run: nothing yet
        self.daily(morning(D3))                  # D1, D2: Alice queued, Frank held

    def act(self, verb, person, **kw):
        kw.setdefault("contacts", self.contacts)
        with patched(config, "PROJECT_ROOT", self.brain), \
                contextlib.redirect_stderr(self.stderr):
            kw.setdefault("cfg", self.local_cfg())
            return getattr(imexclude, verb)(person, home=self.home, db_path=self.fx.db_path,
                                            now=morning(D4), **kw)

    def local_cfg(self):
        return imconfig.load_config(local_path=self.local)

    def listed(self):
        return json.loads(self.local.read_text(encoding="utf-8")).get("never_ingest", [])

    def backups(self):
        return sorted(p for p in self.ring.rglob("*.json")) if self.ring.exists() else []

    def places(self):
        return _tree_bytes(self.home, self.fx.root)

    def view(self):
        with self.ledger() as led:
            return led.queued(), {i: led.held(i) for i in led.all_identifiers()}, \
                led.all_identifiers()


class TestExclude(ExcludeCase):
    def test_a_preview_changes_nothing(self):
        before = self.places()
        report = self.act("exclude", "Fixture Alice")
        self.assertFalse(report["done"])
        self.assertEqual(report["changes"], [ALICE_PHONE])
        self.assertEqual(report["ledger"]["queued_days"], 2)
        self.assertEqual(_changed(before, self.places()), [], "a preview wrote something")
        text = imessage.exclude_render(report)
        self.assertIn("a preview: nothing was changed", text)
        self.assertIn("…0142", text)
        self.assertNotIn(ALICE_PHONE, text)
        self.assertIn('exclude --person "Fixture Alice" --confirm', text)

    def test_the_act_backs_up_first_then_writes_the_list_and_drops_the_waiting_days(self):
        original = self.local.read_bytes()
        seen = {}
        real = imspine.snapshot_local

        def spy(path):
            seen["bytes_at_backup"] = Path(path).read_bytes()
            seen["path"] = Path(path)
            return real(path)

        with mock.patch.object(imspine, "snapshot_local", spy):
            report = self.act("exclude", "Fixture Alice", confirm=True)
        self.assertTrue(report["done"], report)
        self.assertEqual(seen["bytes_at_backup"], original, "the backup came after the write")
        self.assertEqual(seen["path"].resolve(), self.local.resolve())
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(Path(report["snapshot"]), backups[0])

        on_disk = json.loads(self.local.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["never_ingest"], [ALICE_PHONE])
        self.assertEqual(on_disk["own_handles"], [ME], "the merge lost own_handles")
        queued, _held, _ids = self.view()
        self.assertNotIn(key(ALICE_PHONE, D1), queued)
        self.assertNotIn(key(ALICE_PHONE, D2), queued)
        self.assertEqual(report["ledger"]["queued_days"], 2)
        text = imessage.exclude_render(report)
        self.assertIn("Lines already on their card stay", text)
        self.assertIn("local_snapshot.py restore", text)

    def test_a_later_daily_never_reads_that_number(self):
        self.act("exclude", "Fixture Alice", confirm=True)
        self.chats.exchange(1, D4, ALICE_PHONE)
        report = self.daily(morning(D5), cfg=self.local_cfg())
        self.assertEqual(report["queued"], 0)
        queued, held, _ids = self.view()
        self.assertNotIn(key(ALICE_PHONE, D4), queued)
        self.assertFalse(any(u["key"] == key(ALICE_PHONE, D4) for units in held.values()
                             for u in units))
        self.assertEqual(list(self.threads.rglob("2026-03-05-*")), [],
                         "a transcript was written for an excluded conversation")

    def test_lines_already_on_a_card_stay(self):
        self.cfg["synth_stale_days"] = 0
        self.daily(morning(D3))                    # both days land with their plain line
        card = self.cards["fixture-alice"].path
        lines = _conversation_lines(card)
        self.assertEqual(len(lines), 2)
        report = self.act("exclude", "Fixture Alice", confirm=True)
        self.assertTrue(report["done"])
        self.assertEqual(_conversation_lines(card), lines)
        self.assertEqual(report["ledger"]["queued_days"], 0)

    def test_a_held_number_leaves_the_review_list_and_include_puts_it_back(self):
        _q, held, ids = self.view()
        self.assertEqual(len(held[FRANK]), 2)
        self.assertEqual((ids[FRANK]["state"], ids[FRANK]["hold_reason"]), ("held", "new"))

        out = self.act("exclude", "Fixture Frank", confirm=True)
        self.assertTrue(out["done"], out)
        self.assertEqual((out["ledger"]["held_days"], out["ledger"]["numbers"]), (2, 1))
        _q, held, ids = self.view()
        self.assertEqual(held.get(FRANK, []), [])
        self.assertEqual((ids[FRANK]["state"], ids[FRANK]["hold_reason"]),
                         ("dismissed", imexclude.NEVER_INGEST))
        self.assertEqual(imrun.waiting_counts(ids), (0, 0))
        with contextlib.redirect_stderr(self.stderr):
            listing = imreview.review(home=self.home, db_path=self.fx.db_path, cfg=self.cfg,
                                      contacts=self.contacts, now=morning(D4))
        self.assertFalse(any(r["number"] == FRANK for r in listing["listing"]["rows"]))
        self.assertEqual(self.listed(), [FRANK])

        back = self.act("include", "Fixture Frank", confirm=True)
        self.assertTrue(back["done"], back)
        self.assertEqual(back["changes"], [FRANK])
        self.assertEqual(len(self.backups()), 2, "include did not back the file up first")
        self.assertEqual(self.listed(), [])
        _q, _held, ids = self.view()
        self.assertEqual((ids[FRANK]["state"], ids[FRANK]["hold_reason"]), ("held", "new"))
        self.assertIsNone(ids[FRANK]["excluded"])
        self.assertIn("a backfill over those dates reads them", imessage.exclude_render(back))

        self.chats.exchange(3, D4, FRANK)          # he texts again: held again, as before
        self.daily(morning(D5), cfg=self.local_cfg())
        _q, held, _ids = self.view()
        self.assertEqual([u["key"] for u in held[FRANK]], [key(FRANK, D4)])

    def test_a_name_covers_card_and_contacts_a_number_covers_only_itself(self):
        self.contacts[BOB_PHONE] = "Fixture Bob"
        by_name = self.act("exclude", "Fixture Bob")
        self.assertEqual(sorted(by_name["identifiers"]), sorted([BOB_EMAIL, BOB_PHONE]))
        by_number = self.act("exclude", "+1 555 555 0170")
        self.assertEqual(by_number["identifiers"], [BOB_PHONE])
        self.assertIn("Only that number", imessage.exclude_render(by_number))

    def test_include_by_number(self):
        self.act("exclude", FRANK, confirm=True)
        report = self.act("include", "555-555-0160", confirm=True)
        self.assertEqual(report["changes"], [FRANK])
        self.assertEqual(self.listed(), [])


class TestRefusals(ExcludeCase):
    def test_an_ambiguous_name_lists_them_and_changes_nothing(self):
        self.contacts[FRANK_TWIN] = "Fixture Frank Other"
        before = self.places()
        report = self.act("exclude", "Frank", confirm=True)
        self.assertEqual(report["match"]["status"], "ambiguous")
        self.assertIsNotNone(report["refused"])
        self.assertFalse(report["done"])
        self.assertEqual(_changed(before, self.places()), [])
        self.assertEqual(self.backups(), [])
        self.assertIn("I never guess which", imessage.exclude_render(report))

    def test_a_failed_backup_writes_nothing(self):
        before = self.places()
        with mock.patch.object(imspine, "snapshot_local",
                               side_effect=OSError("the ring is full")):
            report = self.act("exclude", "Fixture Alice", confirm=True)
        self.assertFalse(report["done"])
        self.assertIn("could not back up", report["refused"])
        self.assertEqual(_changed(before, self.places()), [], "it wrote without its backup")

    def test_a_changed_engine_ring_refuses_the_backup(self):
        with mock.patch.dict(imspine.LOCAL_SNAPSHOT_SIGNATURES,
                             {"local_snapshot.snapshot": "(path, extra)"}):
            report = self.act("exclude", "Fixture Alice", confirm=True)
        self.assertFalse(report["done"])
        self.assertIn("changed", report["refused"])
        self.assertEqual(self.listed(), [])

    def test_the_members_own_number_is_never_excluded(self):
        report = self.act("exclude", ME, confirm=True)
        self.assertEqual(report["own_skipped"], [ME])
        self.assertEqual(report["identifiers"], [])
        self.assertFalse(report["done"])
        self.assertEqual(self.listed(), [])

    def test_a_corrupt_config_is_refused_not_read_as_empty(self):
        self.local.write_text("{ not json", encoding="utf-8")
        report = self.act("exclude", "Fixture Alice", confirm=True)
        self.assertIn("not valid JSON", report["refused"])
        self.assertEqual(self.local.read_text(encoding="utf-8"), "{ not json")


class TestTheSeam(unittest.TestCase):
    """G7: the local undo ring is reached through imspine alone, and only when called."""

    def test_only_imspine_reaches_local_snapshot_and_never_at_import(self):
        """Code, not prose: an import of it, or a dotted name or ``__import__`` string for
        it, anywhere but imspine is a second road into the engine (G7)."""

        def reaches(tree):
            hits = []
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                      and node.value.split(".")[0] == "local_snapshot"):
                    names = [node.value]
                if any(n.split(".")[0] == "local_snapshot" for n in names):
                    hits.append(node.lineno)
            return hits

        found = {}
        for source in sorted(PLUGIN_HOME.glob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            if source.name == "imspine.py":
                top = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
                self.assertEqual(reaches(ast.Module(body=top, type_ignores=[])), [],
                                 "imspine imports local_snapshot at import time")
                self.assertTrue(reaches(tree), "imspine no longer reaches the ring at all")
                continue
            hits = reaches(tree)
            if hits:
                found[source.name] = hits
        self.assertEqual(found, {}, "local_snapshot is reached outside imspine")
        self.assertNotIn("local_snapshot", vars(imspine), "imspine bound local_snapshot")

    def test_the_ring_signature_is_pinned_by_hand_and_holds_today(self):
        self.assertEqual(imspine.LOCAL_SNAPSHOT_SIGNATURES, {"local_snapshot.snapshot": "(path)"})
        import local_snapshot  # noqa: PLC0415 - reading the live engine, as imspine does

        self.assertEqual(imspine.signature_text(local_snapshot.snapshot), "(path)")

    def test_the_parser_takes_both_verbs(self):
        for verb in ("exclude", "include"):
            args = imessage._build_parser().parse_args(
                [verb, "--person", "Fixture Alice", "--confirm", "--show-numbers"])
            self.assertEqual((args.verb, args.person, args.confirm, args.show_numbers),
                             (verb, "Fixture Alice", True, True))


class TestTheWriters(unittest.TestCase):
    """``remove_never_ingest``: the one road that shortens the list, with the writers' guards."""

    def setUp(self):
        import tempfile  # noqa: PLC0415 - this class's scratch folder

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.local = self.home / "config.local.json"
        self.local.write_text(json.dumps({"own_handles": [ME], "never_ingest": [
            "212-555-0142", "bot@rbm.example.com", "+15555550160"], "x": 1}), encoding="utf-8")

    def test_it_removes_by_canonical_spelling_and_keeps_everything_else(self):
        path, merged, removed = imessage.remove_never_ingest(
            ["+12125550142"], target=self.local, home=self.home)
        self.assertEqual(removed, ["212-555-0142"])
        on_disk = json.loads(self.local.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["never_ingest"], ["bot@rbm.example.com", "+15555550160"])
        self.assertEqual((on_disk["own_handles"], on_disk["x"]), ([ME], 1))

    def test_nothing_to_remove_writes_nothing_and_junk_refuses(self):
        before = self.local.read_bytes()
        _p, _m, removed = imessage.remove_never_ingest(["+15555550111"], target=self.local,
                                                       home=self.home)
        self.assertEqual(removed, [])
        with self.assertRaises(imessage.LocalConfigWriteError):
            imessage.remove_never_ingest(["not a number"], target=self.local, home=self.home)
        with self.assertRaises(imessage.LocalConfigWriteError):
            imessage.remove_never_ingest(["+15555550160"], target=self.home.parent / "x.json",
                                         home=self.home)
        self.assertEqual(self.local.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
