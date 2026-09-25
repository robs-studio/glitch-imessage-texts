"""The live invariant sweep — every real transcript on this Mac, read-only, counts only.

The fixture sweeps in ``test_imthreads`` prove the writer on invented data.  This suite
proves it on what is really on disk: every file under the REAL
:data:`imconfig.THREADS_DIR`, written by the member's own ``preview --write`` runs, held
to the invariants a card's link and a line-by-line reader rely on:

1. **no reaction body** — a tapback is an annotation Apple generated, not anything anybody
   typed, and quoting one onto a card invents a sentence the person never said;
2. **no** :mod:`imredact` **hit in a message BODY** — a hit means a secret the redactor
   exists to catch reached the disk;
3. **no** ``\\r`` **anywhere**, and no other line-breaking character — one message must be
   one line for every reader, and a carriage return splits it for some of them;
4. **mode** ``0600`` — the member's own conversations, readable by no other account;
5. **contained within the threads folder once resolved** — a symlink out of the folder is
   how a write, or a read, escapes it;
6. **a filename that follows the slug rule** — ``YYYY/YYYY-MM-DD-<slug>-<chat_rowid>.txt``,
   the slug ``[a-z0-9-]`` and at most 60 characters.  The card pointer is built from the
   name, and the engine's link parser stops at the first ``)``.

And every line must parse as a rendered message line, in the exact shape
:func:`imthreads.render_thread` writes (``HH:MM  Who: body``), because a line that does
not is either one message split in two or a file this plug-in did not write.

Why the redaction scan reads the BODY only
-------------------------------------------
The speaker label is an identifier, and a real identifier can be a long run of digits.
A business sender's handle can be 16 digits, and scanning whole lines trips
``long_digit_run`` on that sender's lines while the bodies trip nothing.  The label is
who spoke, not something anybody said, so it is not what redaction guards.  A control
below renders a line with a 16-digit speaker and shows both halves of that.

Why the reaction list is longer than the plan's
------------------------------------------------
The plan names ``Liked "`` / ``Loved "`` / ``Gefällt`` / ``ein Herz``, with STRAIGHT
quotes.  Measured over the reaction rows of a real Messages store: **every English
reaction opens with a CURLY quote** (``Liked “`` · ``Loved “`` · ``Laughed at “`` ·
``Emphasized “`` · ``Questioned “`` · ``Disliked “``), not one with a straight quote, and
some more are ``Reacted <emoji> to “…”``.  A sweep that knew only the straight forms
would pass over every real English reaction there is, so both spellings are checked, and
the emoji form too.

READ-ONLY, and PRIVATE — the rules this file is written under
-------------------------------------------------------------
This suite reads the member's real transcripts and **writes nothing** anywhere near them:
it opens files ``rb``, stats them, and walks the folder without following links.  **No
assertion message may print a message body, a handle or a name.**  A failure reports
counts, and file-name SHAPES with the person taken out
(``2026/2026-09-14-<9 chars>-248.txt``), because every real filename carries a Contacts
label.  The negative controls at the bottom plant their violations in a temp folder and
prove both that each check bites and that its report leaks nothing.
"""

import sys
from pathlib import Path

PLUGIN_HOME = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN_HOME))

# imconfig FIRST, before any other plug-in or engine module: importing it puts
# `.claude/scripts` on sys.path and re-asserts this plug-in's folder ahead of it,
# so no engine module can shadow one of ours. See imconfig's docstring.
import imconfig  # noqa: E402
import imchat  # noqa: E402
import imcontacts  # noqa: E402
import imredact  # noqa: E402
import imthreads  # noqa: E402

import os  # noqa: E402
import re  # noqa: E402
import stat  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

# The privacy-floor suite plants a decoy at the top of the REAL threads folder and
# removes it again.  Its path and body are imported from that suite, never
# re-typed, so the one exemption below cannot drift from the file it exempts.
# Appended, not inserted: this folder must never outrank the plug-in or the engine.
if str(TESTS_DIR) not in sys.path:
    sys.path.append(str(TESTS_DIR))
from test_privacy_floor import DECOY_BODY, THREADS_DECOY  # noqa: E402

# ---------------------------------------------------------------------------
# What each invariant looks for.
# ---------------------------------------------------------------------------

_REACTION_VERBS = ("Liked", "Loved", "Emphasized", "Laughed at", "Disliked", "Questioned")

#: Reaction wording, both quote spellings, plus the two German forms tapbacks can
#: also arrive in. See the module docstring for the measurement.
REACTION_MARKERS: tuple[str, ...] = (
    *(f'{verb} "' for verb in _REACTION_VERBS),
    *(f"{verb} “" for verb in _REACTION_VERBS),
    "Gefällt",
    "ein Herz",
)

#: The emoji tapback: ``Reacted 👍 to “…”``. Anchored at the start of the body,
#: because "reacted … to" is ordinary English anywhere else in a sentence.
REACTED_WITH_EMOJI = re.compile(r"^Reacted .{1,16} to [\"“]")

#: Every character that breaks a line for SOME reader. ``\n`` ends a line by
#: design and is checked by the line parse instead; ``\r`` has its own invariant.
OTHER_LINE_BREAKS: tuple[str, ...] = (
    "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " ",
)

#: ``HH:MM  Who: body`` — the shape :func:`imthreads.render_thread` writes:
#: ``f"{dt_local:%H:%M}  {who}: {body}"``. The speaker is matched non-greedily,
#: so a body that itself contains ": " stays whole. The control class below
#: renders real lines through ``render_thread`` and parses them back, so this
#: pattern cannot drift from the writer unnoticed.
LINE_SHAPE = re.compile(r"^(\d{2}:\d{2})  (.+?): (.*)$")

#: ``YYYY-MM-DD-<slug>-<chat_rowid>.txt``. The slug is runs of ``[a-z0-9]``
#: joined by single dashes, exactly what :func:`imcontacts.slugify_label` returns.
FILENAME_SHAPE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})-([a-z0-9]+(?:-[a-z0-9]+)*)-(\d+)\.txt$"
)

YEAR_FOLDER = re.compile(r"^\d{4}$")

#: The invariants, in the order a failure reports them. One name each, so a
#: control can ask for exactly the one it planted.
REACTION = "a reaction body"
REDACTION = "an imredact hit in a message body"
CARRIAGE_RETURN = "a carriage return"
OTHER_BREAK = "another line-breaking character"
NOT_A_MESSAGE_LINE = "a line that is not a rendered message line"
LOOSE_MODE = "a mode other than 0600"
ESCAPES = "a path that resolves outside the threads folder"
BAD_NAME = "a filename that breaks the slug rule"

INVARIANTS: tuple[str, ...] = (
    REACTION,
    REDACTION,
    CARRIAGE_RETURN,
    OTHER_BREAK,
    NOT_A_MESSAGE_LINE,
    LOOSE_MODE,
    ESCAPES,
    BAD_NAME,
)

#: How many file-name shapes a failure lists per invariant. Enough to find the
#: files, few enough that the message stays a summary.
SHAPES_SHOWN = 5


# ---------------------------------------------------------------------------
# The sweep. One function, used by the live test and by every control.
# ---------------------------------------------------------------------------


def shape_of(relative: str) -> str:
    """A path's SHAPE with the person taken out: ``2026/2026-09-14-<9 chars>-248.txt``.

    The year folder, the date and the ``chat_rowid`` are structure and say nothing
    about anybody; the slug is a Contacts label.  Anything that is not a year folder
    or a recognised filename is masked whole, as its length, so an unexpected name
    fails closed rather than printing itself.
    """
    parts = relative.split("/")
    folders = [part if YEAR_FOLDER.match(part) else f"<{len(part)} chars>" for part in parts[:-1]]
    name = parts[-1]
    shaped = FILENAME_SHAPE.match(name)
    if shaped:
        year, month, day, slug, rowid = shaped.groups()
        name = f"{year}-{month}-{day}-<{len(slug)} chars>-{rowid}.txt"
    else:
        name = f"<{len(name)} chars>"
    return "/".join([*folders, name])


@dataclass
class SweepResult:
    """What a sweep found: totals, and per invariant the files that broke it.

    ``broken`` maps each invariant to ``{relative path: shape}``.  The path is the
    key so a file is counted once however many of its lines break the rule, and two
    files that happen to share a shape are still two; it stays in memory and is
    never printed.  Only :meth:`report` speaks, and it speaks in shapes.
    """

    files: int = 0
    lines: int = 0
    exempted: int = 0
    broken: dict[str, dict[str, str]] = field(default_factory=dict)

    def flag(self, invariant: str, relative: str) -> None:
        """Record one file breaking one invariant, once."""
        self.broken.setdefault(invariant, {}).setdefault(relative, shape_of(relative))

    def count(self, invariant: str) -> int:
        return len(self.broken.get(invariant, {}))

    def report(self, invariant: str) -> str:
        """Counts and shapes only. Never a body, a handle or a name."""
        shapes = list(self.broken.get(invariant, {}).values())
        shown = ", ".join(shapes[:SHAPES_SHOWN])
        more = f" (and {len(shapes) - SHAPES_SHOWN} more)" if len(shapes) > SHAPES_SHOWN else ""
        return (
            f"{len(shapes)} of {self.files} transcripts carry {invariant}: {shown}{more}"
        )


def _is_privacy_floor_decoy(relative: str, data: bytes) -> bool:
    """The one exemption: the privacy-floor suite's decoy, by exact path AND exact body.

    That suite plants it at the top of the real threads folder and removes it, and a
    run of it at the same moment as this one must not read as a broken transcript.
    Both must match, so a real file that happened to take that name is still swept.
    """
    decoy = Path(THREADS_DECOY).relative_to("threads").as_posix()
    return relative == decoy and data == DECOY_BODY.encode("utf-8")


def _check_name(result: SweepResult, relative: str) -> None:
    parts = relative.split("/")
    shaped = FILENAME_SHAPE.match(parts[-1])
    if len(parts) != 2 or shaped is None:
        result.flag(BAD_NAME, relative)
        return
    year, month, day, slug, _rowid = shaped.groups()
    try:
        datetime(int(year), int(month), int(day))
    except ValueError:
        result.flag(BAD_NAME, relative)
        return
    if (
        parts[0] != year
        or len(slug) > imcontacts.MAX_LABEL_SLUG_LEN
        or imcontacts.slugify_label(slug) != slug
    ):
        result.flag(BAD_NAME, relative)


def _check_body(result: SweepResult, relative: str, body: str) -> None:
    if any(marker in body for marker in REACTION_MARKERS) or REACTED_WITH_EMOJI.match(body):
        result.flag(REACTION, relative)
    # The BODY only, never the whole line: the speaker label is an identifier and
    # may legitimately be a long digit run. A business sender's handle can be 16
    # digits, and trips long_digit_run when the whole line is scanned.
    if imredact.find_hits(body):
        result.flag(REDACTION, relative)


def _check_content(result: SweepResult, relative: str, data: bytes) -> None:
    if b"\r" in data:
        result.flag(CARRIAGE_RETURN, relative)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        result.flag(NOT_A_MESSAGE_LINE, relative)
        return
    if any(token in text for token in OTHER_LINE_BREAKS):
        result.flag(OTHER_BREAK, relative)

    # render_thread ends every file with "\n" and writes nothing for a unit with
    # no messages, so an empty file or a missing final newline is not its output.
    if not text or not text.endswith("\n"):
        result.flag(NOT_A_MESSAGE_LINE, relative)
        return
    for line in text[:-1].split("\n"):
        result.lines += 1
        parsed = LINE_SHAPE.match(line.rstrip("\r"))
        if parsed is None:
            result.flag(NOT_A_MESSAGE_LINE, relative)
            continue
        _check_body(result, relative, parsed.group(3))


def sweep(threads_dir) -> SweepResult:
    """Every file under ``threads_dir`` against every invariant. READ-ONLY.

    Walks without following links, so a symlinked folder is judged as an entry
    rather than walked into, and every entry, folder or file, is checked for
    containment once resolved.  Files are opened ``rb`` and never written.
    """
    base = Path(threads_dir).resolve()
    result = SweepResult()

    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        escaping: set[str] = set()
        for name in [*dirnames, *filenames]:
            entry = Path(dirpath) / name
            relative = entry.relative_to(base).as_posix()
            resolved = entry.resolve()
            if resolved == base or not resolved.is_relative_to(base):
                result.flag(ESCAPES, relative)
                escaping.add(name)

        for name in filenames:
            path = Path(dirpath) / name
            relative = path.relative_to(base).as_posix()
            if name in escaping:
                # Flagged above, and never opened: whatever a link out of the
                # folder points at is not this sweep's to read.
                result.files += 1
                continue
            try:
                data = path.read_bytes()
            except OSError:
                # A link that leads nowhere: already flagged above if it escapes,
                # and a file nobody can read is not a transcript either.
                result.files += 1
                result.flag(NOT_A_MESSAGE_LINE, relative)
                continue
            if _is_privacy_floor_decoy(relative, data):
                result.exempted += 1
                continue

            result.files += 1
            _check_name(result, relative)
            if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600:
                result.flag(LOOSE_MODE, relative)
            _check_content(result, relative, data)

    return result


# ---------------------------------------------------------------------------
# The live sweep — the real threads folder, read-only, counts only.
# ---------------------------------------------------------------------------


class TestEveryRealTranscriptHoldsEveryInvariant(unittest.TestCase):
    """The REAL :data:`imconfig.THREADS_DIR`. Skipped on a machine with no transcripts yet."""

    @classmethod
    def setUpClass(cls):
        threads = Path(imconfig.THREADS_DIR)
        if not threads.is_dir():
            raise unittest.SkipTest(
                "there is no threads folder on this machine yet, so there is nothing "
                "to sweep. `imessage.py preview --write` makes one."
            )
        cls.result = sweep(threads)
        if cls.result.files == 0:
            raise unittest.SkipTest("the threads folder holds no transcript yet.")
        exempted = cls.result.exempted
        print(
            f"    live sweep: {cls.result.files} transcripts · {cls.result.lines} lines"
            + (f" · {exempted} privacy-floor decoy exempted" if exempted else "")
        )

    def _assert_clean(self, invariant, why):
        self.assertEqual(
            self.result.count(invariant), 0, f"LIVE SWEEP: {self.result.report(invariant)}. {why}"
        )

    def test_the_sweep_really_read_lines(self):
        """Without this, every check below could be passing over empty files."""
        self.assertGreaterEqual(
            self.result.lines,
            self.result.files,
            f"LIVE SWEEP: {self.result.files} transcripts gave only "
            f"{self.result.lines} lines, so some were read as empty.",
        )

    def test_no_reaction_body(self):
        self._assert_clean(
            REACTION,
            "A tapback is an annotation Apple generated; quoting one onto a card "
            "invents a sentence the person never said.",
        )

    def test_no_redaction_hit_in_a_body(self):
        self._assert_clean(REDACTION, "A secret the redactor exists to catch reached the disk.")

    def test_no_carriage_return_anywhere(self):
        self._assert_clean(
            CARRIAGE_RETURN, "One message must be one line, and \\r splits it for some readers."
        )

    def test_no_other_line_breaking_character(self):
        self._assert_clean(OTHER_BREAK, "One message must be one line for every reader.")

    def test_every_line_is_a_rendered_message_line(self):
        self._assert_clean(
            NOT_A_MESSAGE_LINE,
            "A line that does not parse is one message split in two, or a file this "
            "plug-in did not write.",
        )

    def test_every_file_is_owner_only(self):
        if os.name == "nt":
            self.skipTest("POSIX modes are advisory on Windows")
        self._assert_clean(
            LOOSE_MODE, "These are the member's own conversations, readable by any account."
        )

    def test_everything_resolves_inside_the_threads_folder(self):
        self._assert_clean(ESCAPES, "A link out of the folder is how a write or a read escapes it.")

    def test_every_filename_follows_the_slug_rule(self):
        self._assert_clean(
            BAD_NAME,
            "The card pointer is built from this name, and the engine's link parser "
            "stops at the first ')'.",
        )


# ---------------------------------------------------------------------------
# The line format is the writer's, not a guess — rendered, then parsed back.
# ---------------------------------------------------------------------------

#: Invented throughout: the reserved 555-01xx block, and a 16-digit run shaped
#: like the real long handle the module docstring describes. Nobody's number.
ZONE = timezone(timedelta(hours=-4))
ALICE = "+15555550101"
LONG_HANDLE = "5555550101234567"
ME_PHONE = "+15555550199"


def _message(hour, minute, *, handle=None, from_me=False, text="hello"):
    when = datetime(2026, 9, 14, hour, minute, tzinfo=ZONE)
    return imchat.Message(
        chat_rowid=7,
        chat_style=imchat.ONE_TO_ONE_STYLE,
        chat_name=None,
        is_group=False,
        date_raw=int(when.timestamp()) * 1_000_000_000,
        dt_local=when,
        is_from_me=from_me,
        handle=handle,
        text=text,
    )


class TestTheParserReadsWhatTheWriterWrites(unittest.TestCase):
    """:data:`LINE_SHAPE` against real :func:`imthreads.render_thread` output."""

    def _render(self, messages, contacts=None):
        units = imthreads.group(messages, own_handles={ME_PHONE})
        self.assertEqual(len(units), 1)
        return imthreads.render_thread(next(iter(units.values())), contacts)

    def test_every_rendered_line_parses_back_to_its_body(self):
        bodies = ("Can we move to 4pm?", "Works for me: see you then", "")
        messages = [
            _message(9, 0, handle=ALICE, text=bodies[0]),
            _message(9, 5, from_me=True, text=bodies[1]),
            _message(9, 9, handle=ALICE, text=bodies[2]),
        ]
        text = self._render(messages, {ALICE: "Mom (cell)"})
        parsed = [LINE_SHAPE.match(line) for line in text[:-1].split("\n")]
        self.assertTrue(all(parsed), "a line render_thread wrote does not parse")
        self.assertEqual(
            [(m.group(1), m.group(2), m.group(3)) for m in parsed],
            [
                ("09:00", "Mom (cell)", bodies[0]),
                ("09:05", "Me", bodies[1]),
                ("09:09", "Mom (cell)", imthreads.NO_WORDS),
            ],
        )

    def test_a_long_digit_speaker_trips_the_whole_line_but_not_the_body(self):
        """Why the scan reads the body only — shown, not asserted in a comment."""
        text = self._render(
            [
                _message(9, 0, handle=LONG_HANDLE, text="See you at the library at seven."),
                _message(9, 5, from_me=True, text="Sounds good."),
            ]
        )
        first = text.split("\n")[0]
        self.assertTrue(
            imredact.find_hits(first),
            "the whole line no longer trips the redactor, so this control no longer "
            "shows why the sweep reads bodies only.",
        )
        result = SweepResult(files=1)
        _check_body(result, "2026/2026-09-14-x-7.txt", LINE_SHAPE.match(first).group(3))
        self.assertEqual(result.count(REDACTION), 0)


# ---------------------------------------------------------------------------
# NEGATIVE CONTROLS — every check, fed the violation it exists to catch.
# ---------------------------------------------------------------------------

#: A slug and a body that must never reach a failure message.
PLANTED_SLUG = "private-person"
PLANTED_BODY = "an ordinary invented line"


def _a_body_that_trips_redaction(case):
    """A body the REAL redactor flags — asked of ``find_hits``, never assumed."""
    for candidate in ("your code is 483920", "the card number is 4242424242424242"):
        if imredact.find_hits(candidate):
            return candidate
    case.fail("imredact flagged none of the control bodies, so the redaction check proves nothing.")


class TestTheSweepBites(unittest.TestCase):
    """Each invariant, planted in a temp folder. The real threads folder is never touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.threads = Path(self._tmp.name) / "threads"
        (self.threads / "2026").mkdir(parents=True)

    def plant(
        self, body, name=f"2026-09-14-{PLANTED_SLUG}-7.txt", folder="2026", raw=None, mode=0o600
    ):
        target = self.threads / folder / name if folder else self.threads / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw if raw is not None else body.encode("utf-8"))
        os.chmod(target, mode)
        return target

    def assert_caught(self, invariant):
        result = sweep(self.threads)
        self.assertEqual(
            result.count(invariant), 1, f"the sweep missed {invariant}; it found {result.broken}"
        )
        report = result.report(invariant)
        self.assertNotIn(PLANTED_SLUG, report, "a failure message printed a person's slug")
        self.assertNotIn(PLANTED_BODY, report, "a failure message printed a message body")
        return result

    def test_a_clean_transcript_breaks_nothing(self):
        """The positive control: or every check below could be flagging everything."""
        self.plant(f"09:00  Mom (cell): {PLANTED_BODY}\n09:05  Me: {PLANTED_BODY}\n")
        result = sweep(self.threads)
        self.assertEqual(result.broken, {})
        self.assertEqual((result.files, result.lines), (1, 2))

    def test_a_straight_quote_reaction_is_caught(self):
        self.plant('09:00  Mom: Liked "the sentence somebody else typed"\n')
        self.assert_caught(REACTION)

    def test_a_curly_quote_reaction_is_caught(self):
        """The form every real English reaction takes (see the module docstring)."""
        self.plant("09:00  Mom: Loved “the sentence somebody else typed”\n")
        self.assert_caught(REACTION)

    def test_an_emoji_reaction_is_caught(self):
        self.plant("09:00  Mom: Reacted \U0001F44D to “the sentence”\n")
        self.assert_caught(REACTION)

    def test_a_german_reaction_is_caught(self):
        self.plant("09:00  Mom: Gefällt der Nachricht\n")
        self.assert_caught(REACTION)

    def test_reacted_inside_a_sentence_is_not_a_reaction(self):
        self.plant("09:00  Mom: He reacted well to “the news”, thank goodness\n")
        self.assertEqual(sweep(self.threads).count(REACTION), 0)

    def test_a_redaction_hit_in_a_body_is_caught(self):
        self.plant(f"09:00  Mom: {PLANTED_BODY} {_a_body_that_trips_redaction(self)}\n")
        self.assert_caught(REDACTION)

    def test_a_carriage_return_is_caught(self):
        self.plant("", raw=f"09:00  Mom: {PLANTED_BODY}\r\n".encode())
        self.assert_caught(CARRIAGE_RETURN)

    def test_a_unicode_line_separator_is_caught(self):
        self.plant(f"09:00  Mom: {PLANTED_BODY} second half\n")
        self.assert_caught(OTHER_BREAK)

    def test_a_split_message_is_caught(self):
        self.plant(f"09:00  Mom: {PLANTED_BODY}\n  second half of the same message\n")
        self.assert_caught(NOT_A_MESSAGE_LINE)

    def test_a_missing_final_newline_and_an_empty_file_are_caught(self):
        self.plant(f"09:00  Mom: {PLANTED_BODY}")
        self.plant("", name=f"2026-09-15-{PLANTED_SLUG}-7.txt")
        self.assertEqual(sweep(self.threads).count(NOT_A_MESSAGE_LINE), 2)

    @unittest.skipIf(os.name == "nt", "POSIX modes are advisory on Windows")
    def test_a_loose_mode_is_caught(self):
        self.plant(f"09:00  Mom: {PLANTED_BODY}\n", mode=0o644)
        self.assert_caught(LOOSE_MODE)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_link_out_of_the_folder_is_caught(self):
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text(f"09:00  Mom: {PLANTED_BODY}\n", encoding="utf-8")
        os.chmod(outside, 0o600)
        (self.threads / "2026" / f"2026-09-14-{PLANTED_SLUG}-7.txt").symlink_to(outside)
        self.assert_caught(ESCAPES)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_linked_folder_out_of_the_threads_folder_is_caught(self):
        outside = Path(self._tmp.name) / "elsewhere"
        outside.mkdir()
        (self.threads / "2025").symlink_to(outside, target_is_directory=True)
        self.assertEqual(sweep(self.threads).count(ESCAPES), 1)

    def test_a_bracketed_label_is_caught(self):
        self.plant(f"09:00  Mom: {PLANTED_BODY}\n", name="2026-09-14-Mom (cell)-7.txt")
        self.assert_caught(BAD_NAME)

    def test_a_file_in_the_wrong_year_folder_is_caught(self):
        self.plant(f"09:00  Mom: {PLANTED_BODY}\n", folder="2025")
        self.assert_caught(BAD_NAME)

    def test_a_file_outside_any_year_folder_is_caught(self):
        self.plant(f"09:00  Mom: {PLANTED_BODY}\n", folder=None)
        self.assert_caught(BAD_NAME)

    def test_an_over_long_slug_is_caught(self):
        slug = "-".join(["abcdefghij"] * 7)  # 76 characters, past the 60 cap
        self.plant(f"09:00  Mom: {PLANTED_BODY}\n", name=f"2026-09-14-{slug}-7.txt")
        self.assertEqual(sweep(self.threads).count(BAD_NAME), 1)

    def test_an_impossible_date_is_caught(self):
        self.plant(f"09:00  Mom: {PLANTED_BODY}\n", name=f"2026-02-30-{PLANTED_SLUG}-7.txt")
        self.assert_caught(BAD_NAME)

    def test_the_privacy_floor_decoy_is_exempt_only_when_it_is_exactly_the_decoy(self):
        decoy = Path(THREADS_DECOY).relative_to("threads").as_posix()
        self.plant(DECOY_BODY, name=decoy, folder=None)
        exempt = sweep(self.threads)
        self.assertEqual((exempt.files, exempt.exempted, exempt.broken), (0, 1, {}))

        self.plant(f"09:00  Mom: {PLANTED_BODY}\n", name=decoy, folder=None)
        swept = sweep(self.threads)
        self.assertEqual(swept.exempted, 0)
        self.assertEqual(
            swept.count(BAD_NAME), 1, "a real file under the decoy's name was waved through"
        )

    def test_a_shape_never_carries_the_slug(self):
        self.assertEqual(
            shape_of(f"2026/2026-09-14-{PLANTED_SLUG}-248.txt"),
            f"2026/2026-09-14-<{len(PLANTED_SLUG)} chars>-248.txt",
        )
        self.assertEqual(shape_of("Mom (cell)/notes.txt"), "<10 chars>/<9 chars>")


if __name__ == "__main__":
    unittest.main(verbosity=2)
