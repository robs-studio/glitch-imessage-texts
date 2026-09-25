"""The engine contract — every engine seam imspine leans on, pinned where a change shows.

``imspine`` is the plug-in's only road into the engine, and the engine updates
under it (``/update``) without asking.  If a signature it calls moves, the run
must PAUSE with one plain sentence rather than write something wrong, and this
suite is what notices first.

Three layers, deliberately separate:

1. **The plan's VERIFIED lists, typed out here by hand.**  Each engine callable's
   signature is compared against a literal copied from the plan's VERIFIED section
   (and re-read against the source on 2026-09-24).  They are NOT read from
   ``imspine.ENGINE_SIGNATURES``: a builder who "fixes" drift by editing imspine's
   table must also edit this file, which is the whole point of a pin.
2. **imspine's own table equals those literals**, so the runtime check and this test
   can never quietly disagree about what the contract is.
3. **``check_contract()`` answers None today and the sentence under drift** — drift
   simulated by swapping an engine attribute for the length of one test, never by
   editing the engine.

Also here: the vocabulary this plug-in writes is still legal, and a fresh
interpreter that imports imspine gets the ENGINE's modules, not a shadow.
"""

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_HOME))

# imconfig FIRST, before anything reachable only because it put the engine on sys.path.
import imconfig  # noqa: E402

imconfig.ensure_engine_path()

import dataclasses  # noqa: E402
import inspect  # noqa: E402
import os  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402

import imspine  # noqa: E402
from imfixture import patched  # noqa: E402

import config  # noqa: E402
import note_parse  # noqa: E402
import people_db  # noqa: E402
import people_index  # noqa: E402
import people_resolve  # noqa: E402
import people_stamp  # noqa: E402
import people_vocab  # noqa: E402
import shared  # noqa: E402

# ---------------------------------------------------------------------------
# The VERIFIED lists, by hand. Source: the build plan's "VERIFIED — engine surfaces"
# section (resolve, ResolveResult, stamp_interaction, _interaction_line), E3
# (insert_proposal), E7 (file_lock), E9 (init_structured_schema), G1 (project_changed);
# the rest re-read against the engine source 2026-09-24.  The plan itself is not shipped
# with the plug-in: these literals are the record.
# ---------------------------------------------------------------------------

VERIFIED_SIGNATURES = {
    "people_resolve.resolve": (
        "(conn, *, email=None, phone=None, handle=None, name=None, source='email', "
        "emit=True, propose_identities=True, account=None)"
    ),
    "people_stamp.stamp_interaction": (
        "(person_path, *, source, occurred_at, direction, topic=None, legacy_topic=None, "
        "link=None, note=None, source_meeting_id=None, conn=None)"
    ),
    "people_stamp._interaction_line": (
        "(source, occurred_at, direction, topic, link, source_meeting_id=None)"
    ),
    "people_db.init_structured_schema": "(conn=None, db_path=None)",
    "people_index.project_changed": "(changed_paths, *, conn=None)",
    "shared.file_lock": "(lock_path, timeout=30.0)",
    "people_resolve._identifier_key": "(email, phone, handle)",
    "people_db.lookup_person_by_slug": "(conn, slug)",
    "note_parse.split_frontmatter": "(text)",
    "note_parse.load_note": "(text)",
    "config.now_local": "()",
}

#: Pinned for the FIXTURE and the guards' reasoning, not called by imspine at run time:
#: E3's dedup is what makes is_pending mandatory, and the fixture raises proposals with it.
FIXTURE_SIGNATURES = {
    "people_db.insert_proposal": "(conn, kind, payload, *, created, person_id=None, account=None)",
    "people_db.set_proposal_status": "(conn, proposal_id, status)",
    "people_db.log_change": (
        "(conn, entity_type, entity_id, action, detail, *, created, field=None, "
        "old_value=None, new_value=None, source=None, confidence=None)"
    ),
}

VERIFIED_RESOLVE_FIELDS = ("status", "person_id", "proposal_kind", "proposal_id", "candidates")

MODULES = {
    "config": config,
    "note_parse": note_parse,
    "people_db": people_db,
    "people_index": people_index,
    "people_resolve": people_resolve,
    "people_stamp": people_stamp,
    "shared": shared,
}


def _bare(fn):
    """The signature as a string with annotations stripped — names, kinds, defaults only."""
    sig = inspect.signature(fn)
    return str(sig.replace(
        parameters=[p.replace(annotation=inspect.Parameter.empty) for p in sig.parameters.values()],
        return_annotation=inspect.Signature.empty,
    ))


def _live(dotted):
    module, _, attr = dotted.partition(".")
    return getattr(MODULES[module], attr)


class TestVerifiedSignatures(unittest.TestCase):
    """Each engine callable, against the literal typed out above."""

    def test_every_verified_signature_holds(self):
        for dotted, expected in VERIFIED_SIGNATURES.items():
            with self.subTest(dotted):
                self.assertEqual(_bare(_live(dotted)), expected)

    def test_fixture_signatures_hold(self):
        for dotted, expected in FIXTURE_SIGNATURES.items():
            with self.subTest(dotted):
                self.assertEqual(_bare(_live(dotted)), expected)

    def test_resolve_result_fields(self):
        names = [f.name for f in dataclasses.fields(people_resolve.ResolveResult)]
        # The VERIFIED five come first, in order, then detail (which imspine reads too).
        self.assertEqual(tuple(names[:5]), VERIFIED_RESOLVE_FIELDS)
        self.assertIn("detail", names)

    def test_stamp_result_fields(self):
        names = {f.name for f in dataclasses.fields(people_stamp.StampResult)}
        self.assertTrue({"stamped", "detail"} <= names)

    def test_imspine_table_is_the_verified_table(self):
        """The runtime check and this pin agree on what the contract IS."""
        self.assertEqual(imspine.ENGINE_SIGNATURES, VERIFIED_SIGNATURES)
        self.assertEqual(imspine.ENGINE_FIELDS["people_resolve.ResolveResult"][:5],
                         VERIFIED_RESOLVE_FIELDS)

    def test_imspine_signature_text_matches_this_files_reading(self):
        for dotted in VERIFIED_SIGNATURES:
            with self.subTest(dotted):
                self.assertEqual(imspine.signature_text(_live(dotted)), _bare(_live(dotted)))


class TestVocabulary(unittest.TestCase):
    """What this plug-in writes must still be legal at every layer that checks it."""

    def test_conversation_is_an_interaction_source(self):
        self.assertIn("conversation", people_vocab.INTERACTION_SOURCE)
        self.assertEqual(imspine.SOURCE, "conversation")

    def test_directions(self):
        self.assertEqual(
            tuple(people_vocab.DIRECTION), ("they_reached_out", "i_reached_out", "mutual")
        )

    def test_proposal_kinds_the_guards_scan_exist(self):
        for kind in ("new_stub", "add_identifier"):
            self.assertIn(kind, people_vocab.PROPOSAL_KIND)
        self.assertEqual(tuple(people_vocab.PROPOSAL_STATUS), ("pending", "accepted", "dismissed"))


class TestCheckContract(unittest.TestCase):
    """None today; the plain sentence, naming the seam, under simulated drift."""

    def _temp_conn(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = imspine.open_conn(db_path=Path(tmp.name) / "memory.db")
        self.addCleanup(conn.close)
        return conn

    def test_clean_today(self):
        self.assertIsNone(imspine.check_contract())

    def test_clean_today_with_the_tables(self):
        self.assertIsNone(imspine.check_contract(self._temp_conn()))

    def test_a_changed_stamp_signature_pauses(self):
        def stamp_interaction(person_path, *, source, occurred_at, direction, topic=None):
            raise AssertionError("never called")

        with patched(people_stamp, "stamp_interaction", stamp_interaction):
            sentence = imspine.check_contract()
        self.assertIsNotNone(sentence)
        self.assertTrue(sentence.startswith(imspine.REFUSAL_ENGINE_CHANGED))
        self.assertTrue(sentence.startswith("texts paused: the engine changed, update the plug-in"))
        self.assertIn("people_stamp.stamp_interaction", sentence)
        # and it recovers the moment the real function is back
        self.assertIsNone(imspine.check_contract())

    def test_an_added_keyword_also_pauses(self):
        """Exact, not compatible: an added keyword can change behaviour as easily."""

        def project_changed(changed_paths, *, conn=None, commit=True):
            raise AssertionError("never called")

        with patched(people_index, "project_changed", project_changed):
            sentence = imspine.check_contract()
        self.assertIn("people_index.project_changed", sentence or "")

    def test_a_removed_function_pauses(self):
        real = people_resolve._identifier_key
        del people_resolve._identifier_key
        try:
            sentence = imspine.check_contract()
        finally:
            people_resolve._identifier_key = real
        self.assertIn("people_resolve._identifier_key is gone", sentence or "")

    def test_vocabulary_drift_pauses(self):
        with patched(people_vocab, "INTERACTION_SOURCE", ("meeting", "email")):
            sentence = imspine.check_contract()
        self.assertIn("no longer allows conversation", sentence or "")

    def test_result_field_drift_pauses(self):
        @dataclasses.dataclass
        class StampResult:  # `detail` renamed away
            stamped: bool = False
            reason: str = ""

        with patched(people_stamp, "StampResult", StampResult):
            sentence = imspine.check_contract()
        self.assertIn("people_stamp.StampResult lost detail", sentence or "")

    def test_column_drift_pauses(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(
            "CREATE TABLE person(id TEXT, slug TEXT);"
            "CREATE TABLE person_proposal(id TEXT, kind TEXT, status TEXT);"  # no payload
            "CREATE TABLE change_log(entity_type TEXT, entity_id TEXT, action TEXT, "
            "field TEXT, detail TEXT, created TEXT);"
        )
        sentence = imspine.check_contract(conn)
        self.assertIn("table person_proposal lost payload", sentence or "")


class TestImportOrderInAFreshProcess(unittest.TestCase):
    """In a real process, imspine's engine modules are the ENGINE's, from any cwd.

    An in-process assertion cannot see this: the test file already put the plug-in
    home first. Run from a neutral folder, so nothing rides on the working directory.
    """

    def test_engine_modules_resolve_to_the_engine(self):
        code = (
            f"import sys; sys.path.insert(0, {str(PLUGIN_HOME)!r})\n"
            "import imspine, people_stamp, config\n"
            "print(sys.path[0]); print(people_stamp.__file__); print(config.__file__)\n"
            "print(imspine.check_contract())\n"
        )
        with tempfile.TemporaryDirectory() as neutral:
            out = subprocess.run(
                [sys.executable, "-c", code], cwd=neutral, capture_output=True, text=True,
                timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        self.assertEqual(out.returncode, 0, out.stderr)
        home, stamp_file, config_file, verdict = out.stdout.strip().splitlines()
        scripts = Path(imconfig.SCRIPTS_DIR).resolve()
        self.assertEqual(Path(home).resolve(), PLUGIN_HOME)
        self.assertEqual(Path(stamp_file).resolve().parent, scripts)
        self.assertEqual(Path(config_file).resolve().parent, scripts)
        self.assertEqual(verdict, "None")


if __name__ == "__main__":
    unittest.main()
