"""imspine against the REAL engine, on a temp vault and a temp people database.

Every test here runs the engine's own code — the resolver, the single card writer,
the projector, the accept and dismiss doors — inside :mod:`imfixture`'s temp tree.
Nothing is mocked except where a failure has to be forced (a held lock, a disk
error), and even the lock test holds a real lock.

What each group holds still, and why it matters
-----------------------------------------------
* **Resolve** — all four outcomes reach the caller; ``ambiguous`` raises nothing and
  carries no proposal id (E2); a blank handle never reaches the name-only step.
* **Stamp** — the exact line the engine writes, and every ``stamped=False`` comes
  back as a recorded reason, never an exception (E7). A ledger that flipped on "no
  exception" would lose those days forever.
* **G1** — stamp, project, COMMIT, close, reopen, read the row back. The negative
  control skips the commit and shows the row vanishing while the card keeps its
  line: the silent failure the pre-build grade found.
* **Guards** — E3 (a dismissed row's id comes back from ``insert_proposal``), E4 (a
  drifted name mints a sibling, and the guard still finds the dismissal), the
  strict day rule, and the E1 two-yes road through the engine's real accept door.
* **Drift** — imspine's dismissed-guard and ``email_people``'s, over the same rows.
* **No escape** — a full cycle leaves the member's real people folder, undo ring
  and database exactly as they were; with a control that shows the check bites.

Privacy: every name, number and address is invented (``555-01xx``,
``fixture.example.com``). The only real-data contact is a listing of names and
mtimes and a read-only count query, and neither is printed.
"""

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_HOME))

# imconfig FIRST, before anything reachable only because it put the engine on sys.path.
import imconfig  # noqa: E402

imconfig.ensure_engine_path()

import json  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from datetime import date, timedelta  # noqa: E402

import imspine  # noqa: E402
from imfixture import (  # noqa: E402
    ALICE,
    BOB,
    CAROL,
    FIXTURE_EMAIL_DOMAIN,
    SHARED_LANDLINE,
    CardSpec,
    SpineFixture,
    SpineTestCase,
    patched,
    real_fixture_traces,
    real_state,
    render_card,
)

import config  # noqa: E402
import email_people  # noqa: E402
import memory_snapshot  # noqa: E402
import people_db  # noqa: E402
import people_lint  # noqa: E402
import people_norm  # noqa: E402
import people_stamp  # noqa: E402
import shared  # noqa: E402

DAY = "2026-09-20"
TOPIC = "Texts (3): Can we move to 4pm?"
LINK = "_local/imessage/threads/2026/2026-09-20 fixture-alice 0142.txt"
#: The line, typed out by hand — the engine's shape, pinned independently of the engine.
EXPECTED_LINE = f"- {DAY} — conversation (mutual): {TOPIC} (→ {LINK})"


def _proposal_count(conn):
    return conn.execute("SELECT COUNT(*) FROM person_proposal").fetchone()[0]


def _interaction_lines(text):
    """The bullet lines under every ``## Interactions`` heading, in order."""
    out, capturing = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            capturing = line[3:].strip().lower() == "interactions"
            continue
        if capturing and line.startswith("- "):
            out.append(line)
    return out


# ---------------------------------------------------------------------------
# Resolve — the four outcomes (E2)
# ---------------------------------------------------------------------------


class TestResolve(SpineTestCase):
    def test_resolved_by_phone(self):
        res = imspine.resolve(self.conn, ALICE.phones[0], "Fixture Alice", emit=True)
        self.assertEqual((res.status, res.person_id), ("resolved", ALICE.pid))

    def test_resolved_through_a_national_spelling(self):
        """E5: resolution already survives spelling; the resolver is not what needs fixing."""
        res = imspine.resolve(self.conn, "(555) 555-0142", None, emit=True)
        self.assertEqual((res.status, res.person_id), ("resolved", ALICE.pid))

    def test_resolved_by_email(self):
        res = imspine.resolve(self.conn, BOB.emails[0].upper(), None, emit=True)
        self.assertEqual((res.status, res.person_id), ("resolved", BOB.pid))

    def test_ambiguous_raises_nothing_and_carries_no_proposal(self):
        before = _proposal_count(self.conn)
        res = imspine.resolve(self.conn, SHARED_LANDLINE, "Fixture Dan", emit=True)
        self.assertEqual(res.status, "ambiguous")
        self.assertIsNone(res.proposal_id)
        self.assertIsNone(res.person_id)
        self.assertEqual(sorted(res.candidates), sorted([self.cards["fixture-dan"].pid,
                                                         self.cards["fixture-dee"].pid]))
        self.assertEqual(_proposal_count(self.conn), before)

    def test_proposed_new_stub_from_an_unknown_number(self):
        res = imspine.resolve(self.conn, "+15555550160", "Fixture Frank", emit=True)
        self.assertEqual((res.status, res.proposal_kind), ("proposed", "new_stub"))
        self.assertTrue(imspine.is_pending(self.conn, res.proposal_id))
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM person_proposal WHERE id = ?", (res.proposal_id,)
        ).fetchone()[0])
        self.assertEqual(payload["identifier"], "+15555550160")
        self.assertEqual(payload["kind"], "phone")
        self.assertEqual(payload["source"], "conversation")

    def test_proposed_add_identifier_for_a_known_name(self):
        res = imspine.resolve(self.conn, "+15555550161", "Fixture Erin", emit=True)
        self.assertEqual((res.status, res.proposal_kind), ("proposed", "add_identifier"))
        self.assertEqual(res.candidates, [self.cards["fixture-erin"].pid])

    def test_an_address_routes_as_an_email(self):
        res = imspine.resolve(self.conn, f"zed@{FIXTURE_EMAIL_DOMAIN}", "Fixture Zed", emit=True)
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM person_proposal WHERE id = ?", (res.proposal_id,)
        ).fetchone()[0])
        self.assertEqual(payload["kind"], "email")

    def test_unresolved_blank_or_digitless_and_never_name_only(self):
        """A blank handle must not fall through to the engine's name-only step."""
        before = _proposal_count(self.conn)
        for ident in ("", "   ", "no-digits-here"):
            with self.subTest(ident=ident):
                res = imspine.resolve(self.conn, ident, "Fixture Alice", emit=True)
                self.assertEqual(res.status, "unresolved")
                self.assertIsNone(res.person_id)
        self.assertEqual(_proposal_count(self.conn), before)

    def test_dry_lookup_writes_nothing(self):
        before = _proposal_count(self.conn)
        res = imspine.resolve(self.conn, "+15555550162", "Fixture Gus", emit=False)
        self.assertEqual(res.status, "proposed")
        self.assertIsNone(res.proposal_id)
        self.assertEqual(_proposal_count(self.conn), before)


# ---------------------------------------------------------------------------
# Stamp — the engine's line, exactly
# ---------------------------------------------------------------------------


class TestStampShape(SpineTestCase):
    def test_writes_exactly_the_engine_line(self):
        alice = self.cards["fixture-alice"]
        out = imspine.stamp(alice.rel, occurred_at=DAY, direction="mutual", topic=TOPIC,
                            link=LINK, conn=self.conn)
        self.assertTrue(out.stamped, out.detail)
        self.assertEqual(out.detail, "stamped")
        self.assertEqual((out.path, out.rel), (alice.path, alice.rel))
        self.assertEqual(out.pointer, EXPECTED_LINE)
        text = alice.path.read_text(encoding="utf-8")
        self.assertIn(f"\n## Interactions\n{EXPECTED_LINE}\n", text)
        self.assertIn(f'last_contacted: "{DAY}"', text)
        self.assertEqual(_interaction_lines(text), [EXPECTED_LINE])
        self.assertTrue(imspine.has_pointer(alice.rel, occurred_at=DAY, direction="mutual",
                                            topic=TOPIC, link=LINK))
        lint = people_lint.lint_file(alice.path)
        self.assertEqual([f.detail for f in lint.flags if f.severity == "error"], [])

    def test_accepts_the_absolute_path_too(self):
        alice = self.cards["fixture-alice"]
        out = imspine.stamp(alice.path, occurred_at=DAY, direction="they_reached_out",
                            topic=None, link=None)
        self.assertTrue(out.stamped, out.detail)
        self.assertEqual(out.rel, alice.rel)

    def test_newest_line_goes_directly_under_the_heading(self):
        alice = self.cards["fixture-alice"]
        for day in ("2026-09-18", "2026-09-19"):
            self.assertTrue(imspine.stamp(alice.rel, occurred_at=day, direction="mutual",
                                          topic=f"Texts (2): day {day}", link=None).stamped)
        lines = _interaction_lines(alice.path.read_text(encoding="utf-8"))
        self.assertEqual([ln[2:12] for ln in lines], ["2026-09-19", "2026-09-18"])

    def test_the_section_is_created_when_the_card_has_none(self):
        carol = self.cards["fixture-carol"]
        self.assertNotIn("## Interactions", carol.path.read_text(encoding="utf-8"))
        out = imspine.stamp(carol.rel, occurred_at=DAY, direction="i_reached_out",
                            topic="Texts (2): See you Sunday", link=None)
        self.assertTrue(out.stamped, out.detail)
        text = carol.path.read_text(encoding="utf-8")
        self.assertEqual(_interaction_lines(text),
                         [f"- {DAY} — conversation (i_reached_out): Texts (2): See you Sunday"])
        self.fx.project([carol.rel])  # and it still projects cleanly

    def test_a_restamp_is_already_present_not_a_failure(self):
        alice = self.cards["fixture-alice"]
        kw = {"occurred_at": DAY, "direction": "mutual", "topic": TOPIC, "link": LINK}
        self.assertTrue(imspine.stamp(alice.rel, **kw).stamped)
        again = imspine.stamp(alice.rel, **kw)
        self.assertFalse(again.stamped)
        self.assertTrue(again.already_present)
        self.assertTrue(again.detail.startswith("idempotent"), again.detail)
        self.assertEqual(_interaction_lines(alice.path.read_text(encoding="utf-8")),
                         [EXPECTED_LINE])

    def test_a_multi_line_topic_is_flattened_to_one_line(self):
        alice = self.cards["fixture-alice"]
        out = imspine.stamp(alice.rel, occurred_at=DAY, direction="mutual",
                            topic="Texts (2): first\nsecond", link=None)
        self.assertTrue(out.stamped)
        self.assertIn(f"- {DAY} — conversation (mutual): Texts (2): first second",
                      alice.path.read_text(encoding="utf-8"))

    def test_the_backup_lands_in_the_fixture_ring(self):
        alice = self.cards["fixture-alice"]
        before = alice.path.read_bytes()
        self.assertTrue(imspine.stamp(alice.rel, occurred_at=DAY, direction="mutual",
                                      topic=TOPIC, link=LINK).stamped)
        backups = memory_snapshot.list_snapshots(alice.rel)
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].is_relative_to(self.fx.snapshots_dir))
        self.assertEqual(backups[0].read_bytes(), before)


# ---------------------------------------------------------------------------
# Stamp — every stamped=False is recorded, never raised (E7 + the quiet refusals)
# ---------------------------------------------------------------------------


class TestStampRefusals(SpineTestCase):
    def _alice(self):
        card = self.cards["fixture-alice"]
        return card, card.path.read_bytes()

    def _assert_untouched(self, card, before):
        self.assertEqual(card.path.read_bytes(), before)

    def test_invalid_direction_is_passed_through(self):
        card, before = self._alice()
        out = imspine.stamp(card.rel, occurred_at=DAY, direction="sideways", topic=TOPIC,
                            link=None)
        self.assertFalse(out.stamped)
        self.assertEqual(out.detail, "invalid source/direction")
        self.assertFalse(out.already_present)
        self._assert_untouched(card, before)

    def test_no_frontmatter_is_passed_through(self):
        path = self.fx.write_raw("fixture-nofm.md", "## Interactions\n")
        out = imspine.stamp("people/fixture-nofm.md", occurred_at=DAY, direction="mutual",
                            topic=TOPIC, link=None)
        self.assertFalse(out.stamped)
        self.assertEqual(out.detail, "no frontmatter — refusing to stamp")
        self.assertEqual(path.read_text(encoding="utf-8"), "## Interactions\n")

    def test_a_snapshot_refusal_is_passed_through_and_nothing_is_written(self):
        card, before = self._alice()
        blocker = self.fx.root / "not-a-folder"
        blocker.write_text("a file where the ring's parent should be", encoding="utf-8")
        with patched(config, "MEMORY_SNAPSHOTS_DIR", blocker / "ring"):
            out = imspine.stamp(card.rel, occurred_at=DAY, direction="mutual", topic=TOPIC,
                                link=None)
        self.assertFalse(out.stamped)
        self.assertTrue(out.detail.startswith("couldn't back"), out.detail)
        self._assert_untouched(card, before)

    def test_a_held_lock_is_caught(self):
        """A REAL lock, held by this test; only the engine's 5 s wait is shortened."""
        card, before = self._alice()
        calls = []

        def short_lock(path, timeout=5.0):
            calls.append(timeout)
            return shared.file_lock(path, timeout=0.3)

        with patched(people_stamp, "file_lock", short_lock), shared.file_lock(card.path):
            out = imspine.stamp(card.rel, occurred_at=DAY, direction="mutual", topic=TOPIC,
                                link=None)
        self.assertEqual(calls, [5.0])  # the engine asked for its 5 s lock
        self.assertFalse(out.stamped)
        self.assertIn("busy", out.detail)
        self._assert_untouched(card, before)
        # and the card is fine once the lock is gone
        self.assertTrue(imspine.stamp(card.rel, occurred_at=DAY, direction="mutual",
                                      topic=TOPIC, link=None).stamped)

    def test_imspine_file_lock_is_the_engine_lock(self):
        card, _ = self._alice()
        with imspine.file_lock(card.path, timeout=0.2):
            with self.assertRaises(TimeoutError):
                with shared.file_lock(card.path, timeout=0.2):
                    pass

    def test_a_disk_error_on_write_is_caught(self):
        card, before = self._alice()

        def failing_write(path, text, *, encoding="utf-8"):
            raise PermissionError(13, "fixture: write refused")

        with patched(people_stamp, "atomic_write_text", failing_write):
            out = imspine.stamp(card.rel, occurred_at=DAY, direction="mutual", topic=TOPIC,
                                link=None)
        self.assertFalse(out.stamped)
        self.assertIn("disk error", out.detail)
        self.assertIn("PermissionError", out.detail)
        self._assert_untouched(card, before)

    def test_an_unreadable_card_is_caught(self):
        path = self.fx.people_dir / "fixture-binary.md"
        path.write_bytes(b"---\nid: \xff\xfe\n---\n")
        out = imspine.stamp("people/fixture-binary.md", occurred_at=DAY, direction="mutual",
                            topic=None, link=None)
        self.assertFalse(out.stamped)
        self.assertIn("not readable text", out.detail)

    def test_a_missing_card_leaves_no_lock_behind(self):
        out = imspine.stamp("people/fixture-nobody.md", occurred_at=DAY, direction="mutual",
                            topic=None, link=None)
        self.assertFalse(out.stamped)
        self.assertEqual(list(self.fx.people_dir.glob("fixture-nobody*")), [])

    def test_a_card_outside_the_vault_is_refused_not_written_unprotected(self):
        """snapshot_for_rewrite lets an out-of-vault write through with NO backup."""
        with tempfile.TemporaryDirectory() as elsewhere:
            stray = Path(elsewhere) / "people" / "fixture-stray.md"
            stray.parent.mkdir()
            text = render_card(CardSpec("fixture-stray", "Fixture Stray", "prs_fxstray2"))
            stray.write_text(text, encoding="utf-8")
            out = imspine.stamp(stray, occurred_at=DAY, direction="mutual", topic=None,
                                link=None)
            self.assertFalse(out.stamped)
            self.assertIsNone(out.path)
            self.assertEqual(stray.read_text(encoding="utf-8"), text)
        for rel in ("fixture-alice.md", "people/../fixture-alice.md", "brands/fixture-alice.md"):
            with self.subTest(rel=rel):
                self.assertFalse(imspine.stamp(rel, occurred_at=DAY, direction="mutual",
                                               topic=None, link=None).stamped)

    def test_lines_that_would_never_project_are_refused(self):
        card, before = self._alice()
        cases = [
            {"occurred_at": "2026-9-20", "topic": TOPIC, "link": None},
            {"occurred_at": "2026-09-20T10:00", "topic": TOPIC, "link": None},
            {"occurred_at": "2026-02-30", "topic": TOPIC, "link": None},
            {"occurred_at": DAY, "topic": TOPIC, "link": "threads/Mom (cell).txt"},
            {"occurred_at": DAY, "topic": TOPIC, "link": "threads/a\nb.txt"},
            {"occurred_at": DAY, "topic": "Moved (→ elsewhere)", "link": None},
        ]
        for kw in cases:
            with self.subTest(kw=kw):
                out = imspine.stamp(card.rel, direction="mutual", **kw)
                self.assertFalse(out.stamped)
                self.assertTrue(out.detail.endswith("nothing was written"), out.detail)
        self._assert_untouched(card, before)

    def test_an_arrow_alone_in_a_summary_is_fine(self):
        card, _ = self._alice()
        out = imspine.stamp(card.rel, occurred_at=DAY, direction="mutual",
                            topic="Texts (2): moved 3pm → 4pm", link=None)
        self.assertTrue(out.stamped, out.detail)


# ---------------------------------------------------------------------------
# G1 — the projection only survives if the CALLER commits
# ---------------------------------------------------------------------------


def _conversation_rows(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT person_id, occurred_at, direction, summary, link FROM interaction "
        "WHERE source = 'conversation' ORDER BY occurred_at"
    ).fetchall()]


class TestG1Acceptance(SpineTestCase):
    def test_stamp_project_commit_close_reopen_reads_the_row(self):
        alice = self.cards["fixture-alice"]
        conn = self.conn
        self.assertTrue(imspine.stamp(alice.rel, occurred_at=DAY, direction="mutual",
                                      topic=TOPIC, link=LINK, conn=conn).stamped)
        result = imspine.project(conn, [alice.rel])
        self.assertEqual((result.projected, result.skipped), (1, 0))
        conn.commit()
        conn.close()
        reopened = self.fx.reopen()
        self.assertEqual(_conversation_rows(reopened),
                         [(ALICE.pid, DAY, "mutual", TOPIC, LINK)])
        last = reopened.execute(
            "SELECT last_contacted, last_topic, last_direction FROM person WHERE id = ?",
            (ALICE.pid,),
        ).fetchone()
        self.assertEqual(tuple(last), (DAY, TOPIC, "mutual"))

    def test_negative_control_close_without_commit_loses_the_row(self):
        """G1 itself: the card has the line, memory.db does not, and nothing said so."""
        alice = self.cards["fixture-alice"]
        conn = self.conn
        self.assertTrue(imspine.stamp(alice.rel, occurred_at=DAY, direction="mutual",
                                      topic=TOPIC, link=LINK).stamped)
        imspine.project(conn, [alice.rel])
        self.assertEqual(len(_conversation_rows(conn)), 1)  # visible inside the transaction
        conn.close()  # no commit
        reopened = self.fx.reopen()
        self.assertEqual(_conversation_rows(reopened), [])
        self.assertIn(EXPECTED_LINE, alice.path.read_text(encoding="utf-8"))

    def test_project_accepts_absolute_paths_and_drops_duplicates(self):
        alice = self.cards["fixture-alice"]
        result = imspine.project(self.conn, [alice.path, alice.rel, alice.rel])
        self.conn.commit()
        self.assertEqual(result.projected, 1)

    def test_project_refuses_to_run_without_the_callers_connection(self):
        with self.assertRaises(ValueError):
            imspine.project(None, ["people/fixture-alice.md"])


# ---------------------------------------------------------------------------
# person_path — identity is the id, never the filename
# ---------------------------------------------------------------------------


class TestPersonPath(SpineTestCase):
    def test_by_slug(self):
        alice = self.cards["fixture-alice"]
        found = imspine.person_path(self.conn, ALICE.pid)
        self.assertEqual(found, (alice.path, alice.rel))
        path, rel = found
        self.assertEqual(rel, "people/fixture-alice.md")

    def test_a_renamed_note_is_still_found_by_its_id(self):
        alice = self.cards["fixture-alice"]
        moved = alice.path.with_name("fixture-alice-renamed.md")
        alice.path.rename(moved)
        found = imspine.person_path(self.conn, ALICE.pid)
        self.assertEqual(found, (moved, "people/fixture-alice-renamed.md"))

    def test_a_reused_filename_is_not_trusted(self):
        """The slug's file now declares someone else: never stamp them by mistake."""
        alice = self.cards["fixture-alice"]
        alice.path.write_text(render_card(CardSpec("fixture-alice", "Fixture Other",
                                                   "prs_fxother2")), encoding="utf-8")
        self.assertIsNone(imspine.person_path(self.conn, ALICE.pid))

    def test_unknown_person(self):
        self.assertIsNone(imspine.person_path(self.conn, "prs_nobody22"))
        self.assertIsNone(imspine.person_path(self.conn, ""))


# ---------------------------------------------------------------------------
# The proposal guards
# ---------------------------------------------------------------------------


class TestGuards(SpineTestCase):
    def test_e3_insert_proposal_hands_back_a_dismissed_id_and_is_pending_says_no(self):
        payload = {"identifier": "+15555550170", "kind": "phone", "platform": None,
                   "name": "Fixture Gina", "source": "conversation"}
        first = self.fx.insert_proposal("new_stub", payload)
        self.fx.dismiss(first, at="2026-09-10T10:00:00-04:00")
        again = self.fx.insert_proposal("new_stub", payload)
        self.assertEqual(again, first)  # E3: the DISMISSED row's id comes back
        self.assertFalse(imspine.is_pending(self.conn, again))
        self.assertEqual(imspine.proposal_state(self.conn, again), "dismissed")

    def test_e4_a_drifted_name_mints_a_sibling_and_the_guard_still_finds_the_no(self):
        first = imspine.resolve(self.conn, "+15555550171", "Fixture Gina", emit=True)
        self.fx.commit()
        self.fx.dismiss(first.proposal_id, at="2026-09-10T10:00:00-04:00")
        drifted = imspine.resolve(self.conn, "+15555550171", "Gina F. (cell)", emit=True)
        self.fx.commit()
        self.assertNotEqual(drifted.proposal_id, first.proposal_id)  # E4, live
        self.assertTrue(imspine.is_pending(self.conn, drifted.proposal_id))
        self.assertEqual(imspine.dismissed_identifier(self.conn, "+15555550171"),
                         first.proposal_id)
        self.assertEqual(imspine.pending_identifier(self.conn, "+15555550171"),
                         drifted.proposal_id)

    def test_the_guard_reads_add_identifier_too(self):
        res = imspine.resolve(self.conn, "+15555550172", "Fixture Erin", emit=True)
        self.fx.commit()
        self.assertEqual(res.proposal_kind, "add_identifier")
        self.fx.dismiss(res.proposal_id, at="2026-09-10T10:00:00-04:00")
        self.assertEqual(imspine.dismissed_identifier(self.conn, "+15555550172"),
                         res.proposal_id)
        # the same number, spelled the way a person types it, is the same number
        self.assertEqual(imspine.dismissed_identifier(self.conn, "+1 (555) 555-0172"),
                         res.proposal_id)

    def test_newest_dismissal_wins_and_other_numbers_are_not_confused(self):
        older = self.fx.insert_proposal("new_stub", {"identifier": "+15555550173",
                                                     "kind": "phone", "name": "Fixture A"})
        newer = self.fx.insert_proposal("new_stub", {"identifier": "+15555550173",
                                                     "kind": "phone", "name": "Fixture B"})
        other = self.fx.insert_proposal("new_stub", {"identifier": "+15555550174",
                                                     "kind": "phone", "name": "Fixture C"})
        for pid in (older, newer, other):
            self.fx.dismiss(pid, at="2026-09-10T10:00:00-04:00")
        self.assertEqual(imspine.dismissed_identifier(self.conn, "+15555550173"), newer)
        self.assertEqual(imspine.dismissed_identifier(self.conn, "+15555550174"), other)
        self.assertIsNone(imspine.dismissed_identifier(self.conn, "+15555550175"))
        self.assertIsNone(imspine.dismissed_identifier(self.conn, ""))

    def test_a_pending_or_accepted_row_is_not_a_dismissal(self):
        pid = self.fx.insert_proposal("new_stub", {"identifier": "+15555550176",
                                                   "kind": "phone", "name": "Fixture D"})
        self.assertIsNone(imspine.dismissed_identifier(self.conn, "+15555550176"))
        people_db.set_proposal_status(self.conn, pid, "accepted")
        self.fx.commit()
        self.assertIsNone(imspine.dismissed_identifier(self.conn, "+15555550176"))
        self.assertIsNone(imspine.pending_identifier(self.conn, "+15555550176"))

    def test_dismissal_day_and_the_strict_day_rule(self):
        pid = self.fx.insert_proposal("new_stub", {"identifier": "+15555550177",
                                                   "kind": "phone", "name": "Fixture E"})
        self.fx.dismiss(pid, at="2026-09-10T23:30:00-04:00")
        self.assertEqual(imspine.dismissal_day(self.conn, pid), "2026-09-10")
        cases = {
            "2026-09-09": False,
            "2026-09-10": False,            # same day: conservative, never a nag
            "2026-09-10T23:59:59": False,   # day precision on the evidence side too
            "2026-09-11": True,             # strictly after
            "2026-09-11T00:00:01": True,
            "not-a-day": False,
            "": False,
        }
        for evidence, expected in cases.items():
            with self.subTest(evidence=evidence):
                self.assertIs(imspine.reopen_justified(self.conn, pid, evidence), expected)
        self.assertTrue(imspine.reopen_justified(self.conn, pid, date(2026, 9, 11)))
        self.assertFalse(imspine.reopen_justified(self.conn, pid, date(2026, 9, 10)))
        self.assertFalse(imspine.reopen_justified(self.conn, pid, None))

    def test_the_latest_logged_dismissal_counts(self):
        pid = self.fx.insert_proposal("new_stub", {"identifier": "+15555550178",
                                                   "kind": "phone", "name": "Fixture F"})
        self.fx.dismiss(pid, at="2026-09-10T10:00:00-04:00")
        people_db.set_proposal_status(self.conn, pid, "pending")  # re-opened …
        self.fx.dismiss(pid, at="2026-09-15T10:00:00-04:00")      # … and said no to again
        self.assertEqual(imspine.dismissal_day(self.conn, pid), "2026-09-15")
        self.assertFalse(imspine.reopen_justified(self.conn, pid, "2026-09-12"))

    def test_an_unlogged_dismissal_never_reopens(self):
        pid = self.fx.insert_proposal("new_stub", {"identifier": "+15555550179",
                                                   "kind": "phone", "name": "Fixture G"})
        people_db.set_proposal_status(self.conn, pid, "dismissed")  # no change_log row
        self.fx.commit()
        self.assertIsNone(imspine.dismissal_day(self.conn, pid))
        self.assertFalse(imspine.reopen_justified(self.conn, pid, "2099-01-01"))

    def test_the_engine_dismiss_door_logs_what_the_guard_reads(self):
        res = imspine.resolve(self.conn, "+15555550180", "Fixture Hal", emit=True)
        self.fx.dismiss(res.proposal_id)  # people.cmd_proposal_set, dated now
        today = config.now_local().date()
        self.assertEqual(imspine.dismissal_day(self.conn, res.proposal_id), today.isoformat())
        self.assertFalse(imspine.reopen_justified(self.conn, res.proposal_id, today))
        self.assertTrue(imspine.reopen_justified(self.conn, res.proposal_id,
                                                 today + timedelta(days=1)))

    def test_proposal_state(self):
        res = imspine.resolve(self.conn, "+15555550181", "Fixture Ida", emit=True)
        self.fx.commit()
        self.assertEqual(imspine.proposal_state(self.conn, res.proposal_id), "pending")
        self.assertIsNone(imspine.proposal_state(self.conn, "prp_nothere"))
        self.assertIsNone(imspine.proposal_state(self.conn, None))
        self.assertFalse(imspine.is_pending(self.conn, None))


class TestAcceptedPerson(SpineTestCase):
    def test_e1_the_two_yes_road_through_the_engines_own_accept_door(self):
        number = "+15555550182"
        stub = imspine.resolve(self.conn, number, "Fixture Hana", emit=True)
        self.assertEqual(stub.proposal_kind, "new_stub")
        self.assertIsNone(imspine.accepted_person(self.conn, stub.proposal_id))  # pending

        out = self.fx.accept(stub.proposal_id)  # people.cmd_accept
        self.assertEqual(out.get("status"), "ok", out)
        self.assertEqual(imspine.proposal_state(self.conn, stub.proposal_id), "accepted")
        new_pid = imspine.accepted_person(self.conn, stub.proposal_id)
        self.assertEqual(new_pid, out["id"])
        card = imspine.person_path(self.conn, new_pid)
        self.assertIsNotNone(card)
        self.assertTrue(card.path.is_relative_to(self.fx.people_dir))

        # E1, live: the accepted person does NOT carry the number, so it is proposed again —
        # as an add_identifier naming exactly the new person.
        again = imspine.resolve(self.conn, number, "Fixture Hana", emit=True)
        self.assertEqual((again.status, again.proposal_kind), ("proposed", "add_identifier"))
        self.assertEqual(again.candidates, [new_pid])

        out2 = self.fx.accept(again.proposal_id)  # the second yes
        self.assertEqual(out2.get("status"), "ok", out2)
        final = imspine.resolve(self.conn, number, None, emit=False)
        self.assertEqual((final.status, final.person_id), ("resolved", new_pid))

    def test_an_email_stub_names_its_person_and_resolves_at_once(self):
        address = f"ivy@{FIXTURE_EMAIL_DOMAIN}"
        stub = imspine.resolve(self.conn, address, "Fixture Ivy", emit=True)
        out = self.fx.accept(stub.proposal_id)
        self.assertEqual(out.get("status"), "ok", out)
        pid = imspine.accepted_person(self.conn, stub.proposal_id)
        self.assertEqual(pid, out["id"])
        res = imspine.resolve(self.conn, address, None, emit=False)
        self.assertEqual((res.status, res.person_id), ("resolved", pid))

    def test_none_for_dismissed_unknown_and_other_kinds(self):
        stub = imspine.resolve(self.conn, "+15555550183", "Fixture Jo", emit=True)
        self.fx.dismiss(stub.proposal_id, at="2026-09-10T10:00:00-04:00")
        self.assertIsNone(imspine.accepted_person(self.conn, stub.proposal_id))
        self.assertIsNone(imspine.accepted_person(self.conn, "prp_nothere"))
        link = imspine.resolve(self.conn, "+15555550184", "Fixture Erin", emit=True)
        self.fx.commit()
        people_db.set_proposal_status(self.conn, link.proposal_id, "accepted")
        self.assertIsNone(imspine.accepted_person(self.conn, link.proposal_id))


# ---------------------------------------------------------------------------
# Drift — imspine's dismissed-guard against email_people's, over the same rows
# ---------------------------------------------------------------------------


class TestDriftAgainstEmailPeople(SpineTestCase):
    """Where the two guards overlap (email ``new_stub`` rows) they must give ONE answer.

    If either implementation changes how it keys a dismissal, this goes red.
    ``add_identifier`` is the one deliberate difference, asserted separately.
    """

    def _addr(self, local):
        return f"{local}@{FIXTURE_EMAIL_DOMAIN}"

    def _stub(self, local, name, status):
        pid = self.fx.insert_proposal("new_stub", {
            "identifier": people_norm.normalise_email(self._addr(local)), "kind": "email",
            "platform": None, "name": name, "source": "email"})
        if status == "dismissed":
            self.fx.dismiss(pid, at="2026-09-10T10:00:00-04:00")
        elif status == "accepted":
            people_db.set_proposal_status(self.conn, pid, "accepted")
            self.fx.commit()
        return pid

    def test_same_answer_on_every_shared_case(self):
        self._stub("drift-a", "Fixture A", "dismissed")
        self._stub("drift-a", "Fixture A Drifted", "dismissed")   # newest must win in both
        self._stub("drift-b", "Fixture B", "pending")
        self._stub("drift-c", "Fixture C", "dismissed")
        self._stub("drift-c", "Fixture C Again", "pending")
        self._stub("drift-d", "Fixture D", "accepted")
        broken = self.fx.insert_proposal("new_stub", {"identifier": "x", "kind": "email"})
        self.conn.execute("UPDATE person_proposal SET payload = 'not json', status = "
                          "'dismissed' WHERE id = ?", (broken,))
        self.fx.commit()
        for local in ("drift-a", "drift-b", "drift-c", "drift-d", "drift-e", "DRIFT-A"):
            addr = self._addr(local)
            with self.subTest(addr=addr):
                ours = imspine.dismissed_identifier(self.conn, addr)
                theirs = email_people._dismissed_email_stub(
                    self.conn, people_norm.normalise_email(addr))
                self.assertEqual(ours, theirs)
        # and the cases are not all None — the comparison is over real dismissals
        self.assertIsNotNone(imspine.dismissed_identifier(self.conn, self._addr("drift-a")))

    def test_pending_twins_agree_too(self):
        self._stub("twin-a", "Fixture A", "pending")
        self._stub("twin-a", "Fixture A Drifted", "pending")
        self._stub("twin-b", "Fixture B", "dismissed")
        for local in ("twin-a", "twin-b", "twin-c"):
            addr = self._addr(local)
            with self.subTest(addr=addr):
                self.assertEqual(
                    imspine.pending_identifier(self.conn, addr),
                    email_people._pending_email_stub(self.conn,
                                                     people_norm.normalise_email(addr)),
                )

    def test_add_identifier_is_the_deliberate_widening(self):
        addr = self._addr("widened")
        pid = self.fx.insert_proposal("add_identifier", {
            "identifier": addr, "kind": "email", "name": "Fixture W",
            "candidates": [BOB.pid], "source": "conversation"})
        self.fx.dismiss(pid, at="2026-09-10T10:00:00-04:00")
        self.assertEqual(imspine.dismissed_identifier(self.conn, addr), pid)
        self.assertIsNone(email_people._dismissed_email_stub(self.conn, addr))

    def test_the_reopen_rule_agrees_on_plain_days(self):
        pid = self._stub("reopen", "Fixture R", "dismissed")
        for day in ("2026-09-09", "2026-09-10", "2026-09-11"):
            with self.subTest(day=day):
                self.assertEqual(imspine.reopen_justified(self.conn, pid, day),
                                 email_people._reopen_justified(self.conn, pid, day))
                self.assertEqual(imspine.dismissal_day(self.conn, pid),
                                 email_people._dismissal_day(self.conn, pid))


# ---------------------------------------------------------------------------
# No escape — the member's real vault, ring and database are untouched
# ---------------------------------------------------------------------------


class TestNothingEscapes(unittest.TestCase):
    def test_a_full_cycle_writes_nothing_real(self):
        before = real_state()
        traces_before = real_fixture_traces()
        with SpineFixture() as fx:
            cards = fx.standard_cards()
            conn = fx.db()
            alice = cards["fixture-alice"]
            self.assertTrue(imspine.stamp(alice.rel, occurred_at=DAY, direction="mutual",
                                          topic=TOPIC, link=LINK, conn=conn).stamped)
            self.assertTrue(imspine.stamp(cards["fixture-carol"].rel, occurred_at=DAY,
                                          direction="mutual", topic=TOPIC, link=None).stamped)
            imspine.project(conn, [alice.rel, cards["fixture-carol"].rel])
            conn.commit()
            stub = imspine.resolve(conn, "+15555550190", "Fixture Kit", emit=True)
            dismissed = imspine.resolve(conn, "+15555550191", "Fixture Lou", emit=True)
            fx.dismiss(dismissed.proposal_id)          # the engine's door, own connection
            self.assertEqual(fx.accept(stub.proposal_id).get("status"), "ok")  # ditto
            again = imspine.resolve(conn, "+15555550190", "Fixture Kit", emit=True)
            self.assertEqual(fx.accept(again.proposal_id).get("status"), "ok")
            # the work really happened — inside the fixture
            self.assertTrue(any(fx.snapshots_dir.iterdir()))
            self.assertTrue((fx.people_dir / "fixture-kit.md").is_file())
        self.assertEqual(real_state(), before)
        self.assertEqual(real_fixture_traces(), traces_before)
        self.assertEqual(traces_before, [], "a previous run already leaked into the real vault")
        # and config is back exactly
        from imfixture import REAL_DATABASE_PATH, REAL_MEMORY_DIR, REAL_SNAPSHOTS_DIR
        self.assertEqual(config.MEMORY_DIR, REAL_MEMORY_DIR)
        self.assertEqual(config.MEMORY_SNAPSHOTS_DIR, REAL_SNAPSHOTS_DIR)
        self.assertEqual(config.DATABASE_PATH, REAL_DATABASE_PATH)

    def test_control_the_listing_comparison_bites(self):
        with tempfile.TemporaryDirectory() as tmp:
            people, ring = Path(tmp) / "people", Path(tmp) / "ring"
            people.mkdir()
            (ring / "r1").mkdir(parents=True)
            (people / "someone.md").write_text("a", encoding="utf-8")
            before = real_state(people, ring)
            (ring / "r1" / "20260920T000000.000Z.md").write_text("b", encoding="utf-8")
            self.assertNotEqual(real_state(people, ring), before)

    def test_control_g3_the_backup_follows_the_snapshots_setting(self):
        """Why BOTH must move: leave MEMORY_SNAPSHOTS_DIR elsewhere and the backup goes there.

        Shown against a decoy folder standing in for the real ring — never the real one.
        """
        with SpineFixture() as fx, tempfile.TemporaryDirectory() as decoy:
            cards = fx.standard_cards()
            with patched(config, "MEMORY_SNAPSHOTS_DIR", Path(decoy)):
                self.assertTrue(imspine.stamp(cards["fixture-alice"].rel, occurred_at=DAY,
                                              direction="mutual", topic=None,
                                              link=None).stamped)
            self.assertTrue(any(Path(decoy).iterdir()))
            self.assertFalse(any(fx.snapshots_dir.iterdir()))

    def test_the_fixture_cards_are_lint_clean(self):
        with SpineFixture() as fx:
            for card in fx.standard_cards().values():
                with self.subTest(card=card.slug):
                    self.assertEqual(people_lint.lint_file(card.path).flags, [])
            self.assertEqual(CAROL.interactions_section, False)


# ---------------------------------------------------------------------------
# The review doors (CP6): the engine's own accept, dismiss and lint, and the reads
# ---------------------------------------------------------------------------

#: Typed out by hand from the engine's source, 2026-09-24 — NOT read from imspine, so a
#: builder who "fixes" drift by editing imspine's table has to edit this file too.
REVIEW_SIGNATURES_BY_HAND = {
    "people.cmd_accept": "(proposal_id, *, decide_gate=None, replace=False)",
    "people.cmd_proposal_set": "(proposal_id, status, *, decide_gate=None)",
    "people_lint.lint_file": "(path, *, known_person_ids=None, prior_created=None)",
    "people_norm.is_valid_phone": "(value)",
    "people_norm.is_valid_email": "(value)",
    "people_norm.same_phone": "(a, b)",
    "people_norm.phone_lookup_digits": "(value)",
}


class TestReviewContract(unittest.TestCase):
    def test_the_table_is_the_hand_typed_one_and_it_holds_today(self):
        import people  # noqa: PLC0415 - the heavy CLI module, only for this pin

        self.assertEqual(imspine.REVIEW_SIGNATURES, REVIEW_SIGNATURES_BY_HAND)
        live = {"people": people, "people_lint": people_lint, "people_norm": people_norm}
        for dotted, expected in REVIEW_SIGNATURES_BY_HAND.items():
            module, _, attr = dotted.partition(".")
            with self.subTest(dotted):
                self.assertEqual(imspine.signature_text(getattr(live[module], attr)), expected)
        self.assertIsNone(imspine.check_review_contract())

    def test_a_changed_accept_door_pauses_the_review_alone(self):
        import people  # noqa: PLC0415

        def cmd_accept(proposal_id, *, decide_gate=None):  # `replace` gone
            raise AssertionError("never called")

        with patched(people, "cmd_accept", cmd_accept):
            sentence = imspine.check_review_contract()
            self.assertIsNone(imspine.check_contract(), "the daily feed must not pause for it")
        self.assertTrue((sentence or "").startswith(imspine.REFUSAL_ENGINE_CHANGED))
        self.assertIn("people.cmd_accept", sentence or "")
        self.assertIsNone(imspine.check_review_contract())

    def test_a_lost_column_pauses_the_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = imspine.open_conn(db_path=Path(tmp) / "memory.db")
            try:
                self.assertIsNone(imspine.check_review_contract(conn))
            finally:
                conn.close()
        import sqlite3  # noqa: PLC0415

        bare = sqlite3.connect(":memory:")
        self.addCleanup(bare.close)
        bare.executescript(
            "CREATE TABLE person(id TEXT, slug TEXT);"  # no name
            "CREATE TABLE person_identifier(person_id TEXT, kind TEXT, platform TEXT, "
            "normalised_value TEXT);"
            "CREATE TABLE person_proposal(id TEXT, kind TEXT, payload TEXT, status TEXT, "
            "person_id TEXT);"
            "CREATE TABLE change_log(entity_type TEXT, entity_id TEXT, action TEXT, "
            "field TEXT, detail TEXT, created TEXT);"
        )
        self.assertIn("table person lost name", imspine.check_review_contract(bare) or "")


class TestReviewDoors(SpineTestCase):
    def test_the_engine_db_path_is_the_fixtures(self):
        self.assertEqual(imspine.engine_db_path(), self.fx.db_path)

    def test_accept_attaches_through_the_engines_own_door_and_lints_clean(self):
        number = "+15555550181"
        raised = imspine.resolve(self.conn, number, "Fixture Erin", emit=True)
        self.assertEqual((raised.proposal_kind, raised.candidates),
                         ("add_identifier", [self.cards["fixture-erin"].pid]))
        self.fx.commit()                   # the door opens its own connection
        out = imspine.accept_proposal(raised.proposal_id)
        self.assertEqual(out.get("status"), "ok", out)
        self.assertEqual(imspine.proposal_state(self.conn, raised.proposal_id), "accepted")
        res = imspine.resolve(self.conn, number, None, emit=False)
        self.assertEqual((res.status, res.person_id), ("resolved", self.cards["fixture-erin"].pid))
        self.assertEqual(imspine.match_route(self.conn, number, res.person_id), ("exact", number))
        self.assertEqual(imspine.lint_card(self.cards["fixture-erin"].rel), [])

    def test_dismiss_writes_the_change_log_row_the_guards_read(self):
        number = "+15555550185"
        raised = imspine.resolve(self.conn, number, "Fixture Kit", emit=True)
        self.fx.commit()
        out = imspine.dismiss_proposal(raised.proposal_id)
        self.assertEqual(out.get("status"), "ok", out)
        self.assertEqual(imspine.proposal_state(self.conn, raised.proposal_id), "dismissed")
        self.assertIsNotNone(imspine.dismissal_day(self.conn, raised.proposal_id))
        self.assertEqual(imspine.dismissed_identifier(self.conn, number), raised.proposal_id)

    def test_a_refused_door_is_data_never_an_exception(self):
        self.assertEqual(imspine.accept_proposal("prp_nothere")["status"], "error")
        self.assertEqual(imspine.dismiss_proposal("prp_nothere")["status"], "error")

    def test_lint_card_reports_errors_and_refuses_a_path_outside_the_people_folder(self):
        bad = self.fx.write_raw("fixture-broken.md", render_card(CardSpec(
            "fixture-broken", "Fixture Broken", "prs_fxbrokn2")).replace(
                "tier: 2", "tier: 9"))
        self.assertTrue(imspine.lint_card(bad))
        outside = imspine.lint_card(self.fx.memory_dir / "notes.md")
        self.assertEqual(len(outside), 1)
        self.assertIn("not a card", outside[0])

    def test_valid_identifier_is_the_engines_own_rule(self):
        for ident, expected in {"+15555550142": True, "+107700900123": True,
                                "bob@fixture.example.com": True, "1234567890123456": False,
                                "1234567890123456789012345": False, "not-an-address@": False,
                                "": False, None: False}.items():
            with self.subTest(ident=ident):
                self.assertIs(imspine.valid_identifier(ident), expected)

    def test_match_route_tells_exact_from_the_suffix_fallback(self):
        alice = self.cards["fixture-alice"].pid
        self.assertEqual(imspine.match_route(self.conn, ALICE.phones[0], alice),
                         ("exact", ALICE.phones[0]))
        # E5b: a different country's number, reaching Alice by its last ten digits only
        res = imspine.resolve(self.conn, "+5555550142", None, emit=False)
        self.assertEqual((res.status, res.person_id), ("resolved", alice))
        self.assertEqual(imspine.match_route(self.conn, "+5555550142", alice),
                         ("suffix", ALICE.phones[0]))
        self.assertEqual(imspine.suffix_digits("+5555550142", ALICE.phones[0]), 10)
        self.assertEqual(imspine.match_route(self.conn, "+15555550199", alice), (None, None))
        self.assertEqual(imspine.match_route(self.conn, BOB.emails[0],
                                             self.cards["fixture-bob"].pid),
                         ("exact", BOB.emails[0]))

    def test_person_name_and_proposal_detail(self):
        self.assertEqual(imspine.person_name(self.conn, ALICE.pid), "Fixture Alice")
        self.assertIsNone(imspine.person_name(self.conn, "prs_nothere"))
        raised = imspine.resolve(self.conn, "+15555550186", "Fixture Lou", emit=True)
        detail = imspine.proposal_detail(self.conn, raised.proposal_id)
        self.assertEqual((detail["kind"], detail["status"], detail["name"], detail["identifier"]),
                         ("new_stub", "pending", "Fixture Lou", "+15555550186"))
        self.assertIsNone(imspine.proposal_detail(self.conn, "prp_nothere"))


if __name__ == "__main__":
    unittest.main()
